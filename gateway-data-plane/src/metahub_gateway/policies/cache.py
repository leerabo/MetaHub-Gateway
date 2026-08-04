"""按租户同步不可变 Policy 快照，并生成请求期 LKG 租约。

本模块只处理内部 Control API wire、缓存原子替换和可达性决策，不读取模型请求正文，
也不执行 Policy。同步日志严格限制为固定错误码和计数，绝不记录内部 Token、URL、
canonical expression、响应体或底层异常正文。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote

import httpx
from fastapi import FastAPI
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.policy_snapshot import (
    POLICY_SNAPSHOT_WIRE_SCHEMA_VERSION,
    PolicySnapshotArtifact,
    deserialize_policy_snapshot_wire,
)
from metahub_shared.runtime.request_id import resolve_request_id, resolve_trace_id

logger = logging.getLogger("metahub.gateway.policy_snapshots")

# 有界并发避免大量新租户同时触发内部请求而耗尽 Gateway 连接与文件描述符。
MAX_REFRESH_CONCURRENCY = 4
SHUTDOWN_TIMEOUT_SECONDS = 5.0
_STRONG_ETAG_RE = re.compile(r'"[0-9a-f]{64}"\Z')

PolicySnapshotStatus = Literal[
    "disabled",
    "not_published",
    "current",
    "lkg",
    "emergency_lkg",
    "expired",
    "unavailable",
]


@dataclass(frozen=True, slots=True)
class PolicySnapshotLeaseDecision:
    """一次请求在读取正文前冻结的快照可执行性决定。

    ``snapshot`` 是 frozen dataclass；后台刷新只能替换缓存引用，不能改变已经持有该对象
    的非流式、Fallback 或流式请求。阻断状态也返回 decision，调用方必须先 claim 和写
    失败日志，再抛出 ``error_code`` 对应的统一错误。
    """

    executable: bool
    snapshot: PolicySnapshotArtifact | None
    snapshot_version: int | None
    status: PolicySnapshotStatus
    error_code: str | None
    acquired_monotonic: float


@dataclass(slots=True)
class _TenantCacheState:
    """保存单租户原子缓存状态；所有字段只允许在 ``PolicySnapshotCache._lock`` 内修改。"""

    snapshot: PolicySnapshotArtifact | None = None
    etag: str | None = None
    synchronized: bool = False
    last_success_monotonic: float | None = None
    refresh_failed_since_success: bool = False
    last_error_code: str | None = None
    generation: int = 0
    # emergency 告警按最后一次成功 generation 去重；同一份 LKG 被大量请求复用时不能刷屏。
    emergency_warning_generation: int | None = None


class PolicySnapshotCache:
    """管理 per-tenant ETag、singleflight 刷新、LKG 与后台轮询。

    参数：config 是启动时验证过的 Gateway 配置；可选 client/monotonic 仅用于确定性测试。
    返回：通过 ``acquire`` 生成不可变租约，通过 ``start``/``close`` 管理后台任务。
    约束：缓存 key 永远来自已认证 Virtual Key 的 tenant_id，不能接受请求体 tenant。
    """

    def __init__(
        self,
        config: AppConfig,
        *,
        client: httpx.AsyncClient | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """创建空缓存；构造阶段不发网络请求，避免未知 tenant 的伪同步。"""

        self._config = config
        self._monotonic = monotonic
        self._states: dict[str, _TenantCacheState] = {}
        self._refresh_tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        self._refresh_slots = asyncio.Semaphore(MAX_REFRESH_CONCURRENCY)
        self._stop_event = asyncio.Event()
        self._scheduler_task: asyncio.Task[None] | None = None
        self._client = client
        self._owns_client = client is None

    async def start(self) -> None:
        """启动只轮询已知 tenant 的 scheduler；同步关闭时保持无后台副作用。"""

        if not self._config.policy_snapshot_sync_enabled or self._scheduler_task is not None:
            return
        self._scheduler_task = asyncio.create_task(
            self._scheduler_loop(), name="gateway-policy-snapshot-scheduler"
        )

    async def close(self) -> None:
        """先停止调度，再有界等待共享刷新，最后关闭本模块拥有的 HTTP client。"""

        self._stop_event.set()
        scheduler = self._scheduler_task
        if scheduler is not None:
            try:
                await asyncio.wait_for(asyncio.shield(scheduler), timeout=SHUTDOWN_TIMEOUT_SECONDS)
            # 调度器超时后必须主动取消，避免进程退出时仍持有内部 Token 或网络资源。
            except TimeoutError:
                scheduler.cancel()
                await asyncio.gather(scheduler, return_exceptions=True)
            self._scheduler_task = None

        async with self._lock:
            pending = tuple(self._refresh_tasks.values())
        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        *(asyncio.shield(task) for task in pending),
                        return_exceptions=True,
                    ),
                    timeout=SHUTDOWN_TIMEOUT_SECONDS,
                )
            # 共享刷新超过关闭时限时统一取消，保证生命周期退出不会无限等待 Control API。
            except TimeoutError:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def acquire(self, tenant_id: str) -> PolicySnapshotLeaseDecision:
        """注册已认证 tenant、执行首次 singleflight 刷新并返回当前保护决定。

        同步关闭只允许配置层已经认可的 dev/test internal 基线，返回 ``disabled``；启用时
        首次请求必须等待有界刷新。waiter 取消通过 shield 与共享 task 隔离。
        """

        now = self._monotonic()
        if not self._config.policy_snapshot_sync_enabled:
            return PolicySnapshotLeaseDecision(True, None, None, "disabled", None, now)

        async with self._lock:
            state = self._states.setdefault(tenant_id, _TenantCacheState())
            needs_initial_refresh = not state.synchronized
        if needs_initial_refresh:
            try:
                # 业务 waiter 只等待一个有界窗口；超时不取消 shield 保护的共享刷新，后续
                # scheduler/请求仍可复用其结果，同时当前请求及时取得 unavailable decision。
                async with asyncio.timeout(
                    float(self._config.policy_snapshot_http_timeout_seconds)
                ):
                    await self.refresh_tenant(tenant_id)
            # waiter 到期只结束当前请求的等待；shield 下的 shared task
            # 继续供 scheduler 和后续请求复用。
            except TimeoutError:
                pass
        # 每次请求只获得当前租户的不可变决定，后续后台刷新不得修改已发放租约。
        return await self._lease_decision(tenant_id)

    async def refresh_tenant(self, tenant_id: str) -> None:
        """刷新一个 tenant，并让所有调用方复用同一个 keyed task。"""

        async with self._lock:
            self._states.setdefault(tenant_id, _TenantCacheState())
            task = self._refresh_tasks.get(tenant_id)
            if task is None:
                task = asyncio.create_task(
                    self._run_refresh_task(tenant_id),
                    name=f"gateway-policy-snapshot-refresh:{tenant_id}",
                )
                self._refresh_tasks[tenant_id] = task
        # 客户端取消不能取消其他 waiter 或 scheduler 共用的同步任务。
        await asyncio.shield(task)

    async def known_tenants(self) -> tuple[str, ...]:
        """返回排序后的已认证 tenant 快照，供调度和测试观察，不暴露快照正文。"""

        async with self._lock:
            return tuple(sorted(self._states))

    async def _run_refresh_task(self, tenant_id: str) -> None:
        """公平取得并发槽后执行带网络总 deadline 的刷新，并原子移除 singleflight task。"""

        current = asyncio.current_task()
        request_id = resolve_request_id(None)
        trace_id = resolve_trace_id(None, request_id)
        try:
            # scheduler 允许在有界 semaphore 后公平排队，网络总 deadline 从真正取得槽后开始；
            # 若把排队也计入 shared task deadline，固定顺序的第 5+ tenant 会每轮永久饥饿。
            async with self._refresh_slots:
                await self._refresh_once(
                    tenant_id,
                    request_id=request_id,
                    trace_id=trace_id,
                )
        # Gateway 生命周期取消必须传播给 close()，不能伪装成普通 Control 故障。
        except asyncio.CancelledError:
            raise
        # semaphore 或其他意外边界异常都形成同一安全失败事实，首请求随后取得 unavailable decision。
        except Exception:
            await self._mark_failure(
                tenant_id,
                "gateway.policy_snapshot_unavailable",
                request_id=request_id,
                trace_id=trace_id,
            )
        finally:
            async with self._lock:
                # 仅由仍登记的当前任务移除槽位，防止旧任务误删同租户的新一轮刷新。
                if self._refresh_tasks.get(tenant_id) is current:
                    self._refresh_tasks.pop(tenant_id, None)

    async def _refresh_once(
        self,
        tenant_id: str,
        *,
        request_id: str,
        trace_id: str,
    ) -> None:
        """用既定链路 ID 调用内部 API，完整验证后才原子替换单租户缓存。"""

        async with self._lock:
            etag = self._states[tenant_id].etag
        headers = {
            "X-Internal-Token": str(self._config.internal_config_token or ""),
            "X-Request-ID": request_id,
            "X-Trace-ID": trace_id,
            "Accept": "application/json",
            # 内部 wire 的 Content-Length 和硬上限按原始 JSON 字节定义；显式禁用压缩，避免
            # 代理按默认 gzip 编码后 raw 字节无法解析或出现解压后体积放大。
            "Accept-Encoding": "identity",
        }
        if etag is not None:
            headers["If-None-Match"] = etag
        url = (
            self._config.control_api_base_url.rstrip("/")
            + "/internal/policy-snapshots/"
            + quote(tenant_id, safe="")
            + "/current"
        )
        try:
            # httpx read timeout 只限制相邻 chunk 的空闲时间；外层总 deadline 还必须覆盖
            # 整个 stream、解析和原子替换，防止慢速滴流长期占满 singleflight/并发槽。
            async with asyncio.timeout(
                float(self._config.policy_snapshot_http_timeout_seconds)
            ):
                # 使用 streaming API 先检查声明长度，再逐块执行实际字节上限；普通 get() 会在
                # 校验前把超大响应完整载入内存，无法满足恶意/损坏 Control 响应的资源边界。
                async with self._http_client().stream(
                    "GET", url, headers=headers
                ) as response:
                    if response.status_code == 304:
                        body = await self._read_bounded_body(response)
                        accepted = await self._accept_not_modified(
                            tenant_id, body=body, headers=response.headers
                        )
                        # 缺失/漂移 ETag 或异常 body 都是同步完整性失败，必须携带本次链路 ID 告警。
                        if not accepted:
                            await self._mark_failure(
                                tenant_id,
                                "gateway.policy_snapshot_unavailable",
                                request_id=request_id,
                                trace_id=trace_id,
                            )
                        return
                    if response.status_code != 200:
                        await self._mark_failure(
                            tenant_id,
                            "gateway.policy_snapshot_unavailable",
                            request_id=request_id,
                            trace_id=trace_id,
                        )
                        return
                    body = await self._read_bounded_body(response)
                    snapshot, response_etag = self._parse_success_response(
                        tenant_id, body, response.headers
                    )
                    accepted = await self._accept_success(
                        tenant_id, snapshot, response_etag
                    )
                    # 低版本、同版异 hash 或 snapshot->null 回退都保留 LKG，并输出一次安全链路告警。
                    if not accepted:
                        await self._mark_failure(
                            tenant_id,
                            "gateway.policy_snapshot_unavailable",
                            request_id=request_id,
                            trace_id=trace_id,
                        )
        # 生命周期取消必须原样传播，不能被同步失败处理吞掉而阻塞 Gateway 关闭。
        except asyncio.CancelledError:
            raise
        # 内部响应属于不可信协议边界；除显式取消外，RecursionError 等解析器资源异常也必须
        # 收敛为固定同步失败，不能在 claim 前穿透成未统一 500 或暴露底层异常正文。
        except Exception:
            # 异常正文可能含内部 URL 或响应片段，结构化日志只能记录固定错误码。
            await self._mark_failure(
                tenant_id,
                "gateway.policy_snapshot_unavailable",
                request_id=request_id,
                trace_id=trace_id,
            )

    async def _read_bounded_body(self, response: httpx.Response) -> bytes:
        """在分配完整响应前按声明长度和实际 raw 字节执行单租户硬上限。"""

        limit = self._config.policy_snapshot_max_payload_bytes
        content_encoding = str(response.headers.get("Content-Encoding") or "").strip().lower()
        # 即使反向代理忽略 identity 请求，Gateway 也不能对压缩实体猜测解码或放宽大小口径。
        if content_encoding not in {"", "identity"}:
            raise ValueError("compressed policy snapshot payload is unsupported")
        raw_length = response.headers.get("Content-Length")
        if raw_length is not None:
            try:
                declared_length = int(raw_length)
            # 非法长度无法建立可信资源预算，禁止进入逐块 JSON 解析。
            except ValueError as exc:
                raise ValueError("invalid content length") from exc
            # 负数或超过部署上限的响应在读取第一个 body chunk 前即失败关闭。
            if declared_length < 0 or declared_length > limit:
                raise ValueError("policy snapshot payload is too large")
        collected = bytearray()
        # MockTransport/ASGITransport 可能在交给调用方前已安全缓冲响应；真实网络 transport
        # 仍走下面的 raw streaming。注入 transport 已消费时只能校验现有 bytes，不能二次迭代。
        if response.is_stream_consumed:
            buffered = response.content
            # 已缓冲 transport 仍必须执行同一实际字节上限，测试注入不能成为资源门限旁路。
            if len(buffered) > limit:
                raise ValueError("policy snapshot payload is too large")
            return buffered
        async for chunk in response.aiter_raw():
            collected.extend(chunk)
            # Control 未声明长度或使用分块传输时仍按实际字节立即中止，避免无界内存增长。
            if len(collected) > limit:
                raise ValueError("policy snapshot payload is too large")
        return bytes(collected)

    def _parse_success_response(
        self,
        tenant_id: str,
        body: bytes,
        headers: httpx.Headers,
    ) -> tuple[PolicySnapshotArtifact | None, str]:
        """严格解析控制面响应信封、租户归属、资源上限、强校验标识与共享快照制品。"""

        configured_limit = self._config.policy_snapshot_max_payload_bytes
        content_type = str(headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        # 200 快照必须明确是 JSON；HTML/文本错误页即使状态码被代理改成 200 也不能进入解析器。
        if content_type != "application/json":
            raise ValueError("policy snapshot content type is invalid")
        raw_length = headers.get("Content-Length")
        if raw_length is not None:
            try:
                declared_length = int(raw_length)
            # 非十进制长度无法建立可靠资源上限，必须在 JSON 解析前直接拒绝响应。
            except ValueError as exc:
                raise ValueError("invalid content length") from exc
            # 声明长度越界代表潜在资源消耗攻击，不能继续读取为可信策略快照。
            if declared_length < 0 or declared_length > configured_limit:
                raise ValueError("policy snapshot payload is too large")
            # 声明长度与实际字节不一致可能是截断响应，禁止接受不完整策略制品。
            if declared_length != len(body):
                raise ValueError("policy snapshot content length mismatch")
        # 即使上游省略长度头，也必须依据实际字节数执行相同的资源上限保护。
        if len(body) > configured_limit:
            raise ValueError("policy snapshot payload is too large")
        etag = headers.get("ETag")
        # 仅接受格式确定的强 ETag，弱标识无法证明租户和制品内容的完整绑定关系。
        if etag is None or _STRONG_ETAG_RE.fullmatch(etag) is None:
            raise ValueError("strong policy snapshot etag is required")
        wrapper = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_json_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
        if not isinstance(wrapper, dict) or set(wrapper) != {
            "wire_schema_version",
            "tenant_id",
            "snapshot",
            "request_id",
            "trace_id",
        }:
            # 信封字段必须精确匹配协议，额外或缺失字段都可能造成新旧版本解释差异。
            raise ValueError("invalid policy snapshot wrapper")
        # 不支持的线协议版本必须失败关闭，避免用旧解析器误执行新格式策略。
        if wrapper["wire_schema_version"] != POLICY_SNAPSHOT_WIRE_SCHEMA_VERSION:
            raise ValueError("unsupported policy snapshot wrapper schema")
        # 响应租户必须与认证后的请求租户完全一致，禁止任何跨租户缓存污染。
        if wrapper["tenant_id"] != tenant_id:
            raise ValueError("policy snapshot tenant mismatch")
        # 链路标识必须保留字符串类型，避免异常元数据被下游错误拼接或记录。
        if not isinstance(wrapper["request_id"], str) or not isinstance(wrapper["trace_id"], str):
            raise ValueError("invalid policy snapshot request metadata")
        raw_snapshot = wrapper["snapshot"]
        if raw_snapshot is None:
            # 空 head 也必须通过租户绑定 ETag 验证，防止伪造未发布状态覆盖有效缓存。
            if etag != _expected_internal_etag(tenant_id, None):
                raise ValueError("policy snapshot etag mismatch")
            return None, etag
        snapshot = deserialize_policy_snapshot_wire(
            raw_snapshot, expected_tenant_id=tenant_id
        )
        # 非空制品的 ETag 必须绑定重算后的版本和哈希，校验失败时继续使用旧 LKG。
        if etag != _expected_internal_etag(tenant_id, snapshot):
            raise ValueError("policy snapshot etag mismatch")
        return snapshot, etag

    async def _accept_not_modified(
        self,
        tenant_id: str,
        *,
        body: bytes,
        headers: httpx.Headers,
    ) -> bool:
        """仅在已有成功 ETag 时接受 304；返回是否可刷新 monotonic 新鲜时间。"""

        if body:
            return False
        async with self._lock:
            state = self._states[tenant_id]
            if not state.synchronized or state.etag is None:
                state.refresh_failed_since_success = True
                state.last_error_code = "gateway.policy_snapshot_unavailable"
                return False
            response_etag = headers.get("ETag")
            # 304 必须回显此前验证过的强 per-tenant ETag；缺失也不能延长 LKG 新鲜期，
            # 否则中间层可用无标识 304 永久遮蔽已经过期或变更的策略。
            if (
                response_etag is None
                or _STRONG_ETAG_RE.fullmatch(response_etag) is None
                or response_etag != state.etag
            ):
                state.refresh_failed_since_success = True
                state.last_error_code = "gateway.policy_snapshot_unavailable"
                return False
            state.last_success_monotonic = self._monotonic()
            state.refresh_failed_since_success = False
            state.last_error_code = None
            state.generation += 1
            state.emergency_warning_generation = None
            return True

    async def _accept_success(
        self,
        tenant_id: str,
        snapshot: PolicySnapshotArtifact | None,
        etag: str,
    ) -> bool:
        """校验版本单调性后原子替换全部状态，并返回响应是否被接受。"""

        async with self._lock:
            state = self._states[tenant_id]
            current = state.snapshot
            # Control head 只能单调前进；曾有真实快照后返回 null 或低版本均视为损坏响应。
            if current is not None and snapshot is None:
                state.refresh_failed_since_success = True
                state.last_error_code = "gateway.policy_snapshot_unavailable"
                return False
            if current is not None and snapshot is not None:
                if snapshot.snapshot_version < current.snapshot_version:
                    state.refresh_failed_since_success = True
                    state.last_error_code = "gateway.policy_snapshot_unavailable"
                    return False
                if snapshot.snapshot_version == current.snapshot_version and (
                    snapshot.content_hash != current.content_hash
                    or snapshot.artifact_hash != current.artifact_hash
                ):
                    state.refresh_failed_since_success = True
                    state.last_error_code = "gateway.policy_snapshot_unavailable"
                    return False
            state.snapshot = snapshot
            state.etag = etag
            state.synchronized = True
            state.last_success_monotonic = self._monotonic()
            state.refresh_failed_since_success = False
            state.last_error_code = None
            state.generation += 1
            state.emergency_warning_generation = None
            return True

    async def _mark_failure(
        self,
        tenant_id: str,
        error_code: str,
        *,
        request_id: str,
        trace_id: str,
    ) -> None:
        """保留 LKG/ETag，并用本次内部链路 ID 输出不含异常正文的安全日志。"""

        async with self._lock:
            state = self._states.setdefault(tenant_id, _TenantCacheState())
            state.refresh_failed_since_success = True
            state.last_error_code = error_code
            generation = state.generation
        logger.warning(
            "policy_snapshot.refresh_failed",
            extra={
                "event": "policy_snapshot.refresh_failed",
                "tenant_id": tenant_id,
                "request_id": request_id,
                "trace_id": trace_id,
                "error_code": error_code,
                "payload": {"generation": generation},
            },
        )

    async def _lease_decision(self, tenant_id: str) -> PolicySnapshotLeaseDecision:
        """按 monotonic age、服务模式和硬上限生成不可变请求租约。"""

        now = self._monotonic()
        emergency_warning: tuple[int | None, int, int] | None = None
        async with self._lock:
            state = self._states.setdefault(tenant_id, _TenantCacheState())
            snapshot = state.snapshot
            version = snapshot.snapshot_version if snapshot is not None else None
            last_success = state.last_success_monotonic
            failed = state.refresh_failed_since_success
            synchronized = state.synchronized
            # 冷启动从未成功同步与明确的 null head 完全不同，前者必须先 claim unavailable。
            if not synchronized or last_success is None:
                decision = PolicySnapshotLeaseDecision(
                    False,
                    None,
                    None,
                    "unavailable",
                    "gateway.policy_snapshot_unavailable",
                    now,
                )
            else:
                age = max(0.0, now - last_success)
                # 即使 scheduler 被停止/饥饿且没有显式 refresh failure，monotonic age 也必须
                # 独立触发 LKG 硬边界，禁止旧策略无限保持 current。
                if age <= self._config.policy_snapshot_max_age_seconds:
                    status: PolicySnapshotStatus = (
                        "lkg"
                        if failed
                        else ("current" if snapshot is not None else "not_published")
                    )
                    decision = PolicySnapshotLeaseDecision(
                        True, snapshot, version, status, None, now
                    )
                elif (
                    self._config.service_mode == "internal_only"
                    and age <= self._config.policy_snapshot_emergency_max_age_seconds
                ):
                    decision = PolicySnapshotLeaseDecision(
                        True, snapshot, version, "emergency_lkg", None, now
                    )
                    if state.emergency_warning_generation != state.generation:
                        state.emergency_warning_generation = state.generation
                        emergency_warning = (version, state.generation, int(age))
                else:
                    error_code = (
                        "gateway.policy_snapshot_stale"
                        if self._config.service_mode == "public_service"
                        else "gateway.policy_snapshot_expired"
                    )
                    decision = PolicySnapshotLeaseDecision(
                        False, snapshot, version, "expired", error_code, now
                    )
        if emergency_warning is not None:
            # 每个成功 generation 最多记录一次安全告警，不包含快照正文、URL 或内部 Token。
            warning_version, warning_generation, warning_age = emergency_warning
            logger.warning(
                "policy_snapshot.emergency_lkg",
                extra={
                    "event": "policy_snapshot.emergency_lkg",
                    "tenant_id": tenant_id,
                    "payload": {
                        "snapshot_version": warning_version,
                        "generation": warning_generation,
                        "age_seconds": warning_age,
                    },
                },
            )
        return decision

    async def _scheduler_loop(self) -> None:
        """按固定间隔轮询已注册 tenant；单租户失败不会中断其他租户。"""

        interval = self._config.policy_snapshot_poll_interval_seconds
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                return
            # 轮询间隔到期是正常调度信号，只有显式 stop 才结束后台循环。
            except TimeoutError:
                pass
            tenants = await self.known_tenants()
            await asyncio.gather(
                *(self.refresh_tenant(tenant_id) for tenant_id in tenants),
                return_exceptions=True,
            )

    def _http_client(self) -> httpx.AsyncClient:
        """延迟创建禁代理、禁重定向的内部 client，防止 Token 被环境代理或 3xx 转发。"""

        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=float(self._config.policy_snapshot_http_timeout_seconds),
                trust_env=False,
                follow_redirects=False,
            )
        return self._client


def install_policy_snapshot_cache(app: FastAPI) -> None:
    """把快照缓存组合进 Gateway 现有 lifespan，不覆盖事件 worker 或共享配置。"""

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        """原 lifespan 启动后安装缓存，退出前按边界顺序停止同步任务。"""

        async with original_lifespan(current_app):
            cache = PolicySnapshotCache(current_app.state.config)
            current_app.state.policy_snapshot_cache = cache
            await cache.start()
            try:
                yield
            finally:
                await cache.close()
                current_app.state.policy_snapshot_cache = None

    app.router.lifespan_context = combined_lifespan


def _json_object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """构造 JSON object 并拒绝重复 key，避免安全字段被解析器静默覆盖。"""

    result: dict[str, object] = {}
    for key, value in pairs:
        # 重复字段可能让签名校验与执行器读取不同值，必须在构造对象前拒绝。
        if key in result:
            raise ValueError("duplicate policy snapshot field")
        result[key] = value
    return result


def _reject_json_constant(_: str) -> None:
    """拒绝非标准 JSON 数值常量，防止不同语言运行时产生不一致的策略比较语义。"""

    # 非有限数无法参与可移植的规范化与哈希计算，因此不得进入共享策略制品。
    raise ValueError("non-standard JSON constant")


def _expected_internal_etag(
    tenant_id: str,
    snapshot: PolicySnapshotArtifact | None,
) -> str:
    """独立重算 schema/tenant/version/artifact-bound ETag，避免错误标识永久遮蔽刷新。"""

    payload = {
        "wire_schema_version": POLICY_SNAPSHOT_WIRE_SCHEMA_VERSION,
        "tenant_id": tenant_id,
        "snapshot_version": snapshot.snapshot_version if snapshot is not None else None,
        "artifact_hash": snapshot.artifact_hash if snapshot is not None else None,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f'"{hashlib.sha256(canonical).hexdigest()}"'
