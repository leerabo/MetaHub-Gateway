"""管理实验 execution 的请求级 heartbeat、发送围栏和最终 CAS。

Guard 从 Assignment 提交后持续持有 owner/generation/token，租约丢失或绝对截止时立即取消
本地等待；它不记录 token、Provider Key、Prompt、Response 或 Endpoint。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, TypeVar
from uuid import uuid4

from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .models import (
    ExperimentExecutionHandle,
    ExperimentTerminalIntent,
    ProviderOutcomeFacts,
)

# 泛型只描述一次 Provider 调用返回值；进程 owner 只在内存和数据库 CAS 中使用。
_T = TypeVar("_T")
_PROCESS_LEASE_OWNER_ID = f"gwe_{uuid4().hex}"
SendPermissionResult = Literal["acquired", "expired", "lost"]


def process_lease_owner_id() -> str:
    """返回 Gateway 进程启动时生成的一次性有界租约 owner。"""

    return _PROCESS_LEASE_OWNER_ID


class ExperimentExecutionStore(Protocol):
    """抽象 0068 冻结的续租、发送许可和终结三类原子存储操作。"""

    def renew_execution(
        self,
        handle: ExperimentExecutionHandle,
        *,
        lease_seconds: int,
    ) -> bool:
        """按完整 owner 组合延长租约，但绝不越过 absolute deadline。"""

        ...

    def acquire_send_permission(
        self,
        handle: ExperimentExecutionHandle,
        *,
        lease_seconds: int,
    ) -> SendPermissionResult:
        """把 assigned 线性化为 provider_calling，并区分过期与 owner 丢失。"""

        ...

    def finalize_execution(
        self,
        handle: ExperimentExecutionHandle,
        intent: ExperimentTerminalIntent,
    ) -> bool:
        """在同一事务追加最终 Outcome、处置用量 Settlement，并围栏更新 execution。"""

        ...


class ExperimentExecutionGuard:
    """从 Assignment 提交一直持有 execution，直到客户端可见逻辑终态确定。"""

    def __init__(
        self,
        config: AppConfig,
        store: ExperimentExecutionStore,
        handle: ExperimentExecutionHandle,
    ) -> None:
        """绑定配置、存储和不可变 handle，并初始化尚未暴露的 Provider 事实。"""

        self.config = config
        self.store = store
        self.handle = handle
        self.facts = ProviderOutcomeFacts()
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._owner_task: asyncio.Task[Any] | None = None
        # 仅记录 Guard 自己成功发出的那一次 cancel 及其精确 owner。仅凭
        # interruption_error_code 无法区分同时到达的 ASGI/客户端取消。
        self._cancelled_owner_task: asyncio.Task[Any] | None = None
        self._stop_event = asyncio.Event()
        self._failure: Literal["timeout", "lease_lost"] | None = None
        self._terminal = False
        self._terminal_intent: ExperimentTerminalIntent | None = None
        self._finalize_intent: ExperimentTerminalIntent | None = None
        self._finalize_task: asyncio.Task[bool] | None = None

    @property
    def interruption_error_code(self) -> str | None:
        """把内部 timeout/lease-lost 状态映射为统一公开错误码。"""

        if self._failure == "timeout":
            return "gateway.experiment_execution_timeout"
        if self._failure == "lease_lost":
            return "gateway.experiment_unavailable"
        return None

    @property
    def terminal_committed(self) -> bool:
        """返回当前 Guard 是否已经确定提交唯一 Outcome 与 execution 终态。"""

        return self._terminal

    @property
    def terminal_intent(self) -> ExperimentTerminalIntent | None:
        """返回实际提交的安全终态意图；CAS 失败或仍在执行时返回 ``None``。"""

        return self._terminal_intent

    @property
    def remaining_seconds(self) -> float:
        """基于 UTC 绝对截止返回非负剩余秒数，不允许 heartbeat 扩大窗口。"""

        return max(0.0, (self.handle.absolute_deadline - datetime.now(UTC)).total_seconds())

    async def start(self) -> None:
        """绑定当前请求 task 并启动唯一 heartbeat，重复启动视为编程错误。"""

        if self._heartbeat_task is not None:
            # 一个 handle 只能存在一个 heartbeat，重复启动会破坏租约与取消所有权。
            raise RuntimeError("experiment heartbeat already started")
        self.attach_current_task()
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat(),
            name="gateway-experiment-heartbeat",
        )

    def attach_current_task(self) -> None:
        """把当前 asyncio task 设为丢失租约时唯一允许取消的请求 owner。"""

        owner = asyncio.current_task()
        if owner is None:
            # 无 asyncio owner 时无法在 deadline/lease 丢失后停止 Provider 等待，必须拒绝。
            raise RuntimeError("experiment execution requires an asyncio task")
        self._owner_task = owner

    def consume_owner_cancellation(self, owner: asyncio.Task[Any]) -> bool:
        """消费 Guard 对精确 owner 发出的唯一一次取消标记。

        调用方还必须同步检查 ``Task.cancelling()`` 并只 ``uncancel`` 一次；剩余
        计数属于外部或重复取消，必须继续传播，不能转换成普通 HTTP/SSE 错误。
        """

        if self._cancelled_owner_task is not owner:
            return False
        self._cancelled_owner_task = None
        return True

    def bind_attempt(self, routing_attempt_id: str) -> None:
        """把非流 Attempt ID 绑定到待提交事实，不把行存在误作已出网。"""

        self.facts.bind_attempt(routing_attempt_id)

    def bind_stream(self, stream_session_id: str) -> None:
        """把 StreamSession ID 绑定到待提交事实，最终状态由流 finalizer 决定。"""

        self.facts.bind_stream(stream_session_id)

    async def acquire_send_permission(self) -> None:
        """在线程中执行最终发送 CAS，失败后禁止构造或调用 Provider Adapter。"""

        self.raise_if_interrupted()
        result = await asyncio.to_thread(
            self.store.acquire_send_permission,
            self.handle,
            lease_seconds=self.config.experiment_execution_lease_seconds,
        )
        if result == "acquired":
            self.facts.mark_send_fenced()
            return
        self._trip("timeout" if result == "expired" else "lease_lost", cancel_owner=False)
        self.raise_if_interrupted()

    async def run_provider_call(self, call: Callable[[], Awaitable[_T]]) -> _T:
        """在不可变实验截止时间内构造并等待一次 Provider 调用。"""

        self.raise_if_interrupted()
        remaining = self.remaining_seconds
        if remaining <= 0:
            self._trip("timeout", cancel_owner=False)
            self.raise_if_interrupted()
        # 只有围栏和本地截止检查都通过后才构造协程，禁止失败路径留下未等待调用，
        # 也让测试能够证明发送许可失败时 Adapter 调用次数严格为零。
        awaitable = call()
        try:
            return await asyncio.wait_for(awaitable, timeout=remaining)
        # Provider 等待达到不可变绝对截止后只返回统一 timeout，不透传上游异常正文。
        except TimeoutError:
            self._trip("timeout", cancel_owner=False)
            # 此处尚在当前请求 task 内，固定错误由全局 envelope 添加 request_id。
            raise ApiError("gateway.experiment_execution_timeout") from None

    def mark_response(self, *, latency_ms: int) -> None:
        """记录非流 Provider 成功候选，Gateway 成功仍需等待后续持久化。"""

        self.facts.mark_response(latency_ms=latency_ms)

    def mark_stream_handshake(self) -> None:
        """记录流式 2xx 暴露事实，但不在握手时提前冻结质量或 Gateway 成功。"""

        self.facts.mark_stream_handshake()

    def mark_stream_terminal(
        self,
        *,
        quality_eligible: bool,
        quality_succeeded: bool,
        latency_ms: int,
    ) -> None:
        """在 shielded 流 finalizer 后记录 Session 决定的质量与延迟事实。"""

        self.facts.mark_stream_terminal(
            quality_eligible=quality_eligible,
            quality_succeeded=quality_succeeded,
            latency_ms=latency_ms,
        )

    def mark_provider_error(self, error: Any, *, latency_ms: int) -> None:
        """规范化 Provider 错误后独立记录 exposure 与 quality，不保存原始异常。"""

        from metahub_gateway.providers.base import NormalizedProviderError

        self.facts.mark_provider_error(
            error if isinstance(error, NormalizedProviderError) else None,
            latency_ms=latency_ms,
        )

    def mark_local_failure(self) -> None:
        """记录 Provider 发送许可后的本地失败，防止把不确定暴露误标为零调用。"""

        self.facts.mark_local_failure()

    def raise_if_interrupted(self) -> None:
        """在每个热路径边界检查租约和 deadline，并只抛统一安全错误。"""

        code = self.interruption_error_code
        if code is not None:
            # 租约失败原因已经固定映射，不能把数据库 CAS 细节透传给调用方。
            raise ApiError(code)
        if self.remaining_seconds <= 0:
            self._trip("timeout", cancel_owner=False)
            # 绝对截止覆盖 heartbeat 租期，即使租约仍有效也必须停止向客户端发送。
            raise ApiError("gateway.experiment_execution_timeout")

    async def finalize(
        self,
        *,
        gateway_outcome: Literal["success", "failed", "unresolved"],
        outcome_reason: str,
        error_code: str | None,
        latency_ms: int,
    ) -> bool:
        """在独立 shielded Task 中提交终态，并在传播取消前确定 CAS 结果。

        success CAS 启动前已经存在的取消必须由调用方改写为 failed/cancelled；CAS 一旦
        启动就不能取消 SQLite 工作线程，因此当前协程会确定性等待它收敛，再原样传播
        取消。调用方可据 ``terminal_committed`` 与 ``terminal_intent`` 区分提交前后。
        """

        intent = ExperimentTerminalIntent(
            gateway_outcome=gateway_outcome,
            exposure=self.facts.exposure,
            outcome_reason=outcome_reason,
            error_code=error_code,
            routing_attempt_id=self.facts.routing_attempt_id,
            stream_session_id=self.facts.stream_session_id,
            quality_eligible=self.facts.quality_eligible,
            quality_succeeded=(
                self.facts.quality_succeeded if self.facts.quality_eligible else None
            ),
            latency_ms=(self.facts.quality_latency_ms if self.facts.quality_eligible else None),
            settlement_disposition=self.facts.settlement_disposition,
        )
        if self._terminal:
            # 数据库 CAS 已提交后只允许完全相同的终态重放；相反意图不能伪装成幂等成功。
            if self._terminal_intent != intent:
                raise RuntimeError("experiment terminal finalize already in progress")
            return True

        owner = asyncio.current_task()
        if gateway_outcome == "success" and owner is not None and owner.cancelling() > 0:
            # 取消在 success CAS 线性化点之前已经存在时禁止启动后台提交；外层会先写
            # failed RequestLog，再用独立清理 task 提交 cancelled Outcome。
            raise asyncio.CancelledError

        task = self._finalize_task
        if task is None:
            # 从创建独立 task 起即进入 Outcome CAS 线性化区；先停止后续 heartbeat 轮次，
            # 但 store 仍以数据库 owner、generation、lease 和 absolute deadline 最终裁决。
            self._stop_event.set()
            self._finalize_intent = intent
            task = asyncio.create_task(
                self._commit_terminal(intent),
                name="gateway-experiment-terminal-finalizer",
            )
            self._finalize_task = task
        elif self._finalize_intent != intent:
            # 一个 execution 不允许两个并发调用者用不同事实争抢唯一 Outcome。
            raise RuntimeError("experiment terminal finalize already in progress")

        cancellation_observed = False
        try:
            while not task.done():
                try:
                    await asyncio.shield(task)
                # 外层取消只能中断等待者，不能取消已进入 SQLite 线程的唯一终态 CAS。
                except asyncio.CancelledError:
                    if task.cancelled():
                        raise
                    # 调用方取消不能取消 SQLite 线程或独立清理 task；继续等待结果，
                    # 收敛后再抛出一次取消，让 ASGI 保留原有断连语义。
                    cancellation_observed = True
            finalized = task.result()
        except BaseException:
            if self._finalize_task is task and task.done() and not self._terminal:
                self._finalize_task = None
                self._finalize_intent = None
            raise
        if not finalized and self._finalize_task is task:
            # CAS 未提交时允许同一请求在严格失败日志落盘后尝试一次 failed Outcome。
            self._finalize_task = None
            self._finalize_intent = None
        if cancellation_observed:
            # 独立 CAS 已确定收敛后再恢复原取消语义，避免调用方误收普通成功返回。
            raise asyncio.CancelledError
        return finalized

    async def _commit_terminal(self, intent: ExperimentTerminalIntent) -> bool:
        """提交唯一终态并停止 heartbeat；该 task 不继承请求 task 的取消。"""

        try:
            finalized = await asyncio.to_thread(
                self.store.finalize_execution,
                self.handle,
                intent,
            )
            if finalized:
                # 必须在线程结果返回的同一独立 task 内发布状态，避免调用方取消后只看到
                # 数据库已提交、内存仍未终结的裂脑窗口。
                self._terminal = True
                self._terminal_intent = intent
            return finalized
        finally:
            await self.stop()

    async def stop(self) -> None:
        """通知 heartbeat 退出并等待其真正结束，避免释放后遗留续租任务。"""

        self._stop_event.set()
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task is None or task is asyncio.current_task():
            return
        await asyncio.gather(task, return_exceptions=True)

    async def _heartbeat(self) -> None:
        """按配置间隔续租，deadline、CAS 失败或存储异常均触发请求取消。"""

        interval = float(self.config.experiment_execution_heartbeat_seconds)
        while not self._stop_event.is_set():
            wait_seconds = min(interval, self.remaining_seconds)
            if wait_seconds <= 0:
                self._trip("timeout")
                return
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=wait_seconds)
                return
            # wait_for 超时表示到达本轮续租时点，不属于错误终态。
            except TimeoutError:
                pass
            if self.remaining_seconds <= 0:
                self._trip("timeout")
                return
            try:
                renewed = await asyncio.to_thread(
                    self.store.renew_execution,
                    self.handle,
                    lease_seconds=self.config.experiment_execution_lease_seconds,
                )
            except Exception:
                renewed = False
            if self._stop_event.is_set():
                # finalize 已建立 CAS 线性化点时，本轮旧续租结果只能退出，不能再取消 owner。
                return
            if not renewed:
                self._trip("lease_lost")
                return

    def _trip(
        self,
        failure: Literal["timeout", "lease_lost"],
        *,
        cancel_owner: bool = True,
    ) -> None:
        """只记录首个中断原因，并可取消仍由当前 Guard 持有的请求 task。"""

        if self._failure is None:
            self._failure = failure
        owner = self._owner_task
        if (
            cancel_owner
            and owner is not None
            and not owner.done()
            and self._cancelled_owner_task is not owner
            and owner.cancel()
        ):
            # ``Task.cancel`` 返回 true 才表示本 Guard 确实增加了一次取消请求；
            # marker 与 owner 一起保存，禁止未来 body task 消费旧握手 task 的取消。
            self._cancelled_owner_task = owner
