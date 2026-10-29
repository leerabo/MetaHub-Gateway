"""验证 PRD-075 execution 租约、发送围栏、绝对截止和终态提交。"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from metahub_gateway.api import openai_compatible as api_module
from metahub_gateway.experiments.guard import ExperimentExecutionGuard
from metahub_gateway.experiments.models import (
    ExperimentExecutionHandle,
    ExperimentTerminalIntent,
)
from metahub_shared.runtime.errors import ApiError


class _Store:
    """用可控返回值模拟 SQLite CAS，并保存不含敏感正文的调用事实。"""

    def __init__(
        self,
        *,
        renew_result: bool = True,
        send_result: str = "acquired",
        finalize_result: bool = True,
    ) -> None:
        """初始化实验测试替身的最小同步状态，不保存 Provider Key、Prompt 或 Response。"""
        self.renew_result = renew_result
        self.send_result = send_result
        self.finalize_result = finalize_result
        self.renew_calls = 0
        self.send_calls = 0
        self.intents: list[ExperimentTerminalIntent] = []

    def renew_execution(self, _handle, *, lease_seconds: int) -> bool:
        """记录续租次数并返回测试指定的 owner CAS 结果。"""

        assert lease_seconds > 0
        self.renew_calls += 1
        return self.renew_result

    def acquire_send_permission(self, _handle, *, lease_seconds: int) -> str:
        """记录最终发送围栏，并返回 acquired、expired 或 lost。"""

        assert lease_seconds > 0
        self.send_calls += 1
        return self.send_result

    def finalize_execution(
        self,
        _handle,
        intent: ExperimentTerminalIntent,
    ) -> bool:
        """保存 append-only Outcome 意图，并模拟同事务 execution 终结。"""

        self.intents.append(intent)
        return self.finalize_result


class _BlockingFinalizeStore(_Store):
    """在线程 CAS 的可控窗口内阻塞，用于精确注入 owner 取消。"""

    def __init__(self) -> None:
        """初始化同步屏障和数据库提交事实。"""

        super().__init__()
        self.finalize_started = threading.Event()
        self.allow_finalize = threading.Event()
        self.committed = False

    def finalize_execution(
        self,
        _handle,
        intent: ExperimentTerminalIntent,
    ) -> bool:
        """等测试释放屏障后模拟 SQLite 事务提交。"""

        self.finalize_started.set()
        if not self.allow_finalize.wait(timeout=5):
            # 测试屏障未被放行说明 CAS 收敛路径失去控制，不能继续产生虚假成功断言。
            raise TimeoutError("synthetic finalize barrier timed out")
        self.intents.append(intent)
        self.committed = True
        return True


def _config(*, heartbeat_seconds: float = 0.01) -> SimpleNamespace:
    """构造 Guard 实际读取的最小部署配置，不允许客户端覆盖。"""

    return SimpleNamespace(
        experiment_execution_lease_seconds=90,
        experiment_execution_heartbeat_seconds=heartbeat_seconds,
    )


def _handle(*, deadline_after: float = 1.0) -> ExperimentExecutionHandle:
    """构造不包含可显示 lease token 的单请求 owner。"""

    return ExperimentExecutionHandle(
        tenant_id="tenant-prd075-guard",
        experiment_id="exp_" + "a" * 32,
        assignment_id="exas_" + "b" * 32,
        execution_id="exrn_" + "c" * 32,
        request_id="req-prd075-guard",
        trace_id="trc-prd075-guard",
        generation=1,
        lease_owner_id="gwe_" + "d" * 32,
        lease_token="test-only-hidden-token",
        absolute_deadline=datetime.now(UTC) + timedelta(seconds=deadline_after),
    )


@pytest.mark.anyio
async def test_send_fence_loss_prevents_provider_factory_call() -> None:
    """旧 owner 失去发送许可时 Adapter 工厂必须保持零次调用。"""

    store = _Store(send_result="lost")
    guard = ExperimentExecutionGuard(_config(), store, _handle())  # type: ignore[arg-type]
    provider_calls = 0

    async def provider_call() -> str:
        """若被错误执行就增加计数，正文仅返回固定测试值。"""

        nonlocal provider_calls
        provider_calls += 1
        return "unexpected"

    with pytest.raises(ApiError) as denied:
        await guard.acquire_send_permission()
        await guard.run_provider_call(provider_call)

    assert denied.value.code == "gateway.experiment_unavailable"
    assert store.send_calls == 1
    assert provider_calls == 0
    assert guard.facts.send_fenced is False


@pytest.mark.anyio
async def test_expired_deadline_does_not_construct_provider_awaitable() -> None:
    """本地绝对截止已过时，不得创建未等待协程或调用 Provider。"""

    guard = ExperimentExecutionGuard(
        _config(),
        _Store(),
        _handle(deadline_after=-0.01),
    )  # type: ignore[arg-type]
    provider_calls = 0

    async def provider_call() -> str:
        """记录调用次数，用于证明截止检查先于协程构造。"""

        nonlocal provider_calls
        provider_calls += 1
        return "unexpected"

    with pytest.raises(ApiError) as expired:
        await guard.run_provider_call(provider_call)

    assert expired.value.code == "gateway.experiment_execution_timeout"
    assert provider_calls == 0


@pytest.mark.anyio
async def test_provider_wait_is_bounded_by_absolute_deadline() -> None:
    """已取得发送围栏后，慢 Provider 仍须在同一 absolute deadline 收口。"""

    store = _Store()
    guard = ExperimentExecutionGuard(
        _config(),
        store,
        _handle(deadline_after=0.03),
    )  # type: ignore[arg-type]
    provider_started = asyncio.Event()

    async def slow_provider() -> str:
        """模拟已开始但未在实验截止前返回的上游调用。"""

        provider_started.set()
        await asyncio.sleep(1)
        return "late"

    await guard.acquire_send_permission()
    with pytest.raises(ApiError) as timed_out:
        await guard.run_provider_call(slow_provider)

    assert timed_out.value.code == "gateway.experiment_execution_timeout"
    assert provider_started.is_set()
    assert guard.facts.send_fenced is True


@pytest.mark.anyio
async def test_heartbeat_lease_loss_cancels_only_current_execution_owner() -> None:
    """续租 CAS 失败会取消绑定请求 task，并留下固定实验不可用码。"""

    store = _Store(renew_result=False)
    guard = ExperimentExecutionGuard(
        _config(heartbeat_seconds=0.01),
        store,
        _handle(deadline_after=1),
    )  # type: ignore[arg-type]

    async def own_execution() -> str:
        """把 Guard 绑定到独立 task，避免取消测试运行器本身。"""

        await guard.start()
        try:
            await asyncio.Event().wait()
        # 测试辅助分支仅接受合成且受控的实验事实，异常输入必须关闭而不泄露敏感值。
        except asyncio.CancelledError:
            return guard.interruption_error_code or "missing"
        finally:
            await guard.stop()

    owner = asyncio.create_task(own_execution())
    assert await asyncio.wait_for(owner, timeout=1) == "gateway.experiment_unavailable"
    assert store.renew_calls == 1


@pytest.mark.anyio
async def test_guard_cancellation_is_consumed_for_uniform_http_error() -> None:
    """Guard 的单次内部取消可转换为 HTTP 错误，且不留下取消计数。"""

    async def consume() -> tuple[str | None, int]:
        """在独立 task 中模拟 heartbeat 发出的唯一一次 cancel。"""

        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        try:
            await asyncio.sleep(0)
        # 此处必须观察并精确消费 Guard 自己发出的取消，外部取消不参与本用例。
        except asyncio.CancelledError:
            code = api_module._consume_experiment_guard_cancellation(
                SimpleNamespace(
                    interruption_error_code="gateway.experiment_execution_timeout",
                    consume_owner_cancellation=lambda owner: owner is task,
                )
            )
            return code, task.cancelling()
        # 未收到预期取消表示 Guard 没有真正中断 owner，后续计数断言已无意义。
        raise AssertionError("cancellation was not delivered")

    assert await asyncio.create_task(consume()) == (
        "gateway.experiment_execution_timeout",
        0,
    )


@pytest.mark.anyio
async def test_external_cancellation_is_not_consumed_or_converted() -> None:
    """无 Guard 中断的真实外部取消必须保留给 ASGI 调用栈。"""

    async def preserve() -> tuple[str | None, int]:
        """在独立 task 中检查外部取消计数不会被 helper 消费。"""

        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        try:
            await asyncio.sleep(0)
        # 无 Guard 时取消必须完整保留，不能被兼容 helper 当作内部中断消费。
        except asyncio.CancelledError:
            code = api_module._consume_experiment_guard_cancellation(None)
            remaining = task.cancelling()
            task.uncancel()
            return code, remaining
        # 取消未到达说明测试没有覆盖真实 ASGI 断连边界，必须立即失败。
        raise AssertionError("cancellation was not delivered")

    assert await asyncio.create_task(preserve()) == (None, 1)


@pytest.mark.anyio
async def test_interrupted_guard_without_owner_marker_preserves_external_cancel() -> None:
    """仅有 Guard 错误码不能冒充取消来源，真实外部取消必须保留。"""

    async def preserve() -> tuple[str | None, int]:
        """模拟租约已失败、但当前 cancel 并非 Guard 发出的竞争窗口。"""

        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        try:
            await asyncio.sleep(0)
        # owner 不匹配时即使错误码相同也不得消费另一任务收到的外部取消。
        except asyncio.CancelledError:
            code = api_module._consume_experiment_guard_cancellation(
                SimpleNamespace(
                    interruption_error_code="gateway.experiment_unavailable",
                    consume_owner_cancellation=lambda _owner: False,
                )
            )
            remaining = task.cancelling()
            task.uncancel()
            return code, remaining
        # 外部取消没有被当前 task 观察时无法证明 owner 隔离，测试必须停止。
        raise AssertionError("cancellation was not delivered")

    assert await asyncio.create_task(preserve()) == (None, 1)


@pytest.mark.anyio
async def test_guard_cancel_plus_external_cancel_preserves_external_count() -> None:
    """同时存在 Guard 和外部取消时只减去 Guard 的一次计数。"""

    async def preserve() -> tuple[str | None, int]:
        """在独立 task 内构造两个取消请求并清理剩余计数。"""

        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        task.cancel()
        try:
            await asyncio.sleep(0)
        # 双重取消中只允许 helper 精确减去 Guard 所有的一次，另一计数必须保留。
        except asyncio.CancelledError:
            code = api_module._consume_experiment_guard_cancellation(
                SimpleNamespace(
                    interruption_error_code="gateway.experiment_unavailable",
                    consume_owner_cancellation=lambda owner: owner is task,
                )
            )
            remaining = task.cancelling()
            while task.cancelling():
                task.uncancel()
            return code, remaining
        # 重复取消没有进入异常分支会掩盖计数消费错误，因此不能继续断言。
        raise AssertionError("cancellation was not delivered")

    assert await asyncio.create_task(preserve()) == (None, 1)


@pytest.mark.anyio
async def test_external_cancel_during_guard_cleanup_is_rethrown() -> None:
    """Guard-only cancel 消费后，shield 清理期间新到达的外部取消不得被吞掉。"""

    async def preserve() -> tuple[str | None, int]:
        """先消费 Guard cancel，再由独立 task 在 finalizer 内取消 owner 并核验计数。"""

        owner = asyncio.current_task()
        assert owner is not None
        owner.cancel()
        try:
            await asyncio.sleep(0)
        # 首次异常只代表 Guard 主动取消，消费后还要继续观察清理期间的新外部取消。
        except asyncio.CancelledError:
            code = api_module._consume_experiment_guard_cancellation(
                SimpleNamespace(
                    interruption_error_code="gateway.experiment_unavailable",
                    consume_owner_cancellation=lambda task: task is owner,
                )
            )

            async def finalizer() -> None:
                """给外部取消提供确定的清理期注入点。"""

                owner.cancel()
                await asyncio.sleep(0)

            with pytest.raises(asyncio.CancelledError):
                await api_module._run_stream_finalizer_shielded(
                    finalizer(),
                    cancellation_already_active=code is None,
                )
            remaining = owner.cancelling()
            while owner.cancelling():
                owner.uncancel()
            return code, remaining
        # Guard 主动取消未送达时本用例无法建立后续外部取消边界，必须立即失败。
        raise AssertionError("guard cancellation was not delivered")

    assert await asyncio.create_task(preserve()) == (
        "gateway.experiment_unavailable",
        1,
    )


@pytest.mark.anyio
async def test_finalize_keeps_gateway_and_provider_quality_facts_independent() -> None:
    """Gateway 本地失败仍可保存已确认的 Provider 暴露和质量成功事实。"""

    store = _Store()
    guard = ExperimentExecutionGuard(_config(), store, _handle())  # type: ignore[arg-type]
    await guard.acquire_send_permission()
    guard.bind_attempt("rta-prd075-guard")
    guard.mark_response(latency_ms=17)

    assert await guard.finalize(
        gateway_outcome="failed",
        outcome_reason="gateway_failed",
        error_code="gateway.output_safety_unavailable",
        latency_ms=23,
    )

    assert len(store.intents) == 1
    intent = store.intents[0]
    assert intent.gateway_outcome == "failed"
    assert intent.exposure == "exposed"
    assert intent.quality_eligible is True
    assert intent.quality_succeeded is True
    assert intent.latency_ms == 17
    assert intent.routing_attempt_id == "rta-prd075-guard"
    assert intent.error_code == "gateway.output_safety_unavailable"


@pytest.mark.anyio
async def test_cancelled_finalize_owner_waits_for_terminal_cas_and_rethrows() -> None:
    """SQLite CAS 启动后的取消必须先确定收敛，再向调用方传播。"""

    store = _BlockingFinalizeStore()
    guard = ExperimentExecutionGuard(_config(), store, _handle())  # type: ignore[arg-type]
    await guard.acquire_send_permission()
    guard.bind_attempt("rta-prd075-cancelled-finalize")
    guard.mark_response(latency_ms=19)

    finalize_owner = asyncio.create_task(
        guard.finalize(
            gateway_outcome="success",
            outcome_reason="completed",
            error_code=None,
            latency_ms=23,
        )
    )
    try:
        assert await asyncio.to_thread(store.finalize_started.wait, 1)
        finalize_owner.cancel()
        await asyncio.sleep(0)
        assert finalize_owner.done() is False
    finally:
        store.allow_finalize.set()

    with pytest.raises(asyncio.CancelledError):
        await finalize_owner

    assert finalize_owner.cancelled() is True
    assert store.committed is True
    assert len(store.intents) == 1
    assert guard.terminal_committed is True
    assert guard.terminal_intent == store.intents[0]
    assert guard.terminal_intent.gateway_outcome == "success"
    assert guard.terminal_intent.outcome_reason == "completed"
    assert not any(
        task.get_name() == "gateway-experiment-terminal-finalizer" and not task.done()
        for task in asyncio.all_tasks()
    )


@pytest.mark.anyio
async def test_committed_guard_rejects_opposite_terminal_intent() -> None:
    """已提交 success 后的 failed 重调必须报冲突，不能伪装为同一终态幂等。"""

    store = _Store()
    guard = ExperimentExecutionGuard(_config(), store, _handle())  # type: ignore[arg-type]
    await guard.acquire_send_permission()
    guard.bind_attempt("rta-prd075-opposite-terminal")
    guard.mark_response(latency_ms=29)

    assert await guard.finalize(
        gateway_outcome="success",
        outcome_reason="completed",
        error_code=None,
        latency_ms=31,
    )
    committed = guard.terminal_intent
    with pytest.raises(RuntimeError, match="experiment terminal finalize already in progress"):
        await guard.finalize(
            gateway_outcome="failed",
            outcome_reason="cancelled",
            error_code="gateway.client_disconnected",
            latency_ms=31,
        )

    assert len(store.intents) == 1
    assert store.intents[0].gateway_outcome == "success"
    assert guard.terminal_intent == committed
    assert not any(
        task.get_name() == "gateway-experiment-terminal-finalizer" and not task.done()
        for task in asyncio.all_tasks()
    )
