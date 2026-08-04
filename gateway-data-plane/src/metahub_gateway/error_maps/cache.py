"""按租户同步 Provider 错误覆盖规则，并发放请求期不可变租约。

错误归一规则只影响诊断精度，不是访问控制或路由策略。Control 不可用、冷启动或 LKG 超龄时
稳定回落 Shared 内置目录，绝不阻断模型主链路；日志只记录固定错误码、租户和版本元数据，
不记录内部 Token、URL、pattern、Provider 正文或底层异常字符串。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import quote

import httpx
from fastapi import FastAPI
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.provider_error_map import (
    ProviderErrorMapArtifact,
    ProviderErrorMapLease,
    builtin_provider_error_map_lease,
    deserialize_provider_error_map_snapshot,
    provider_error_map_etag,
)
from metahub_shared.runtime.request_id import resolve_request_id, resolve_trace_id

logger = logging.getLogger("metahub.gateway.provider_error_maps")

# 强 ETag、Content-Length 语法和关闭时限共同保护缓存的 wire 与生命周期边界；
# 这些常量不包含 Token、URL 或规则原文，日志也只能记录固定错误码和版本元数据。
_STRONG_ETAG_RE = re.compile(r'"[0-9a-f]{64}"\Z')
_CONTENT_LENGTH_RE = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_SHUTDOWN_TIMEOUT_SECONDS = 5.0


@dataclass(slots=True)
class _TenantErrorMapState:
    """单租户缓存状态；所有可变字段只允许在 cache lock 内读写。"""

    artifact: ProviderErrorMapArtifact | None = None
    etag: str | None = None
    last_success_monotonic: float | None = None
    initial_refresh_scheduled: bool = False
    refresh_failed_since_success: bool = False
    generation: int = 0
    fallback_warning_generation: int | None = None


class ProviderErrorMapCache:
    """管理 per-tenant ETag、singleflight、LKG 与后台轮询。

    构造阶段不发网络请求。``acquire`` 的 tenant_id 必须来自已认证 Virtual Key；首次请求只注册
    状态并触发后台同步，当次立即得到 builtin 租约，避免诊断配置给模型热路径增加网络尾延迟。
    """

    def __init__(
        self,
        config: AppConfig,
        *,
        client: httpx.AsyncClient | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """创建有界缓存，并兼容尚未显式传入 PRD-056 新字段的旧 AppConfig。"""

        self._config = config
        self._monotonic = monotonic
        self._states: dict[str, _TenantErrorMapState] = {}
        self._refresh_tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        self._refresh_slots = asyncio.Semaphore(self._int_config("refresh_concurrency", 4))
        self._stop_event = asyncio.Event()
        self._scheduler_task: asyncio.Task[None] | None = None
        self._client = client
        self._owns_client = client is None
        self._closing = False

    def _config_value(self, suffix: str, default: object) -> object:
        """读取新配置字段并提供兼容默认值，部署配置仍由 Shared AppConfig 启动校验负责。"""

        return getattr(self._config, f"provider_error_map_{suffix}", default)

    def _int_config(self, suffix: str, default: int) -> int:
        """返回已由 AppConfig 校验的整数配置，直接构造旧配置时使用安全默认值。"""

        value = self._config_value(suffix, default)
        return int(value) if not isinstance(value, bool) else default

    def _float_config(self, suffix: str, default: float) -> float:
        """返回已由 AppConfig 校验的秒级配置，测试可使用小数 deadline。"""

        value = self._config_value(suffix, default)
        return float(value) if not isinstance(value, bool) else default

    @property
    def enabled(self) -> bool:
        """返回独立同步开关；关闭时不创建状态、任务或故障日志。"""

        return bool(self._config_value("sync_enabled", False))

    async def start(self) -> None:
        """启动只轮询已见租户的 scheduler；关闭同步时保持零后台副作用。"""

        if not self.enabled or self._scheduler_task is not None or self._closing:
            return
        self._scheduler_task = asyncio.create_task(
            self._scheduler_loop(), name="gateway-provider-error-map-scheduler"
        )

    async def close(self) -> None:
        """禁止新刷新，取消并有界等待全部 owner task，最后有界关闭 HTTP client。"""

        self._closing = True
        self._stop_event.set()
        scheduler = self._scheduler_task
        self._scheduler_task = None
        if scheduler is not None:
            scheduler.cancel()
            await asyncio.gather(scheduler, return_exceptions=True)
        async with self._lock:
            pending = tuple(self._refresh_tasks.values())
            self._refresh_tasks.clear()
        for task in pending:
            task.cancel()
        unfinished: set[asyncio.Task[None]] = set()
        if pending:
            # wait_for(gather()) 在子任务吞掉 CancelledError 时会继续等待取消完成，不能形成硬时限；
            # asyncio.wait 只观察 owner task，在 deadline 到达时必定把控制权交回关闭流程。
            done, unfinished = await asyncio.wait(
                pending,
                timeout=_SHUTDOWN_TIMEOUT_SECONDS,
            )
            for task in done:
                self._observe_refresh_task_result(task)
            for task in unfinished:
                task.cancel()
                task.add_done_callback(self._observe_refresh_task_result)
            if unfinished:
                logger.warning(
                    "provider_error_map.refresh_close_timeout",
                    extra={
                        "event": "provider_error_map.refresh_close_timeout",
                        "error_code": "error_map.snapshot_unavailable",
                        "payload": {"task_count": len(unfinished)},
                    },
                )
        if self._owns_client and self._client is not None:
            client = self._client
            self._client = None
            try:
                await asyncio.wait_for(client.aclose(), timeout=_SHUTDOWN_TIMEOUT_SECONDS)
            # HTTP client 关闭超时只能记录固定事件，底层异常可能包含内部 URL，不能直接输出。
            except TimeoutError:
                logger.warning(
                    "provider_error_map.client_close_timeout",
                    extra={
                        "event": "provider_error_map.client_close_timeout",
                        "error_code": "error_map.snapshot_unavailable",
                    },
                )

    async def acquire(self, tenant_id: str) -> ProviderErrorMapLease:
        """注册认证租户、后台触发 singleflight，并立即冻结当前 fresh/LKG/builtin 引用。"""

        # 同步关闭或服务退出期间不创建任何租户状态与任务，稳定返回当前租户 builtin。
        if not self.enabled or self._closing:
            return builtin_provider_error_map_lease(tenant_id)
        async with self._lock:
            state = self._states.setdefault(tenant_id, _TenantErrorMapState())
            # 首次刷新即使失败也不能由后续模型请求重试，否则 Control 故障会被业务流量放大；
            # 后续重试只能由固定节奏 scheduler 或显式运维刷新触发。
            if not state.initial_refresh_scheduled:
                self._ensure_refresh_task_locked(tenant_id)
            artifact = state.artifact
            last_success = state.last_success_monotonic
            failed = state.refresh_failed_since_success
            generation = state.generation
        # 尚无一次成功快照时没有可信 LKG，必须按当前认证租户使用不可变 builtin。
        if artifact is None or last_success is None:
            return builtin_provider_error_map_lease(tenant_id)
        age = max(0.0, self._monotonic() - last_success)
        fresh_seconds = self._float_config("fresh_seconds", 300.0)
        lkg_seconds = self._float_config("lkg_seconds", 3600.0)
        if age <= fresh_seconds and not failed:
            return ProviderErrorMapLease(artifact=artifact, source="fresh")
        if age <= lkg_seconds:
            return ProviderErrorMapLease(artifact=artifact, source="lkg")
        await self._warn_builtin_fallback_once(tenant_id, generation, artifact.revision, int(age))
        # LKG 超龄后不再暴露旧租户覆盖，降级只影响诊断精度而不能阻断模型主链路。
        return builtin_provider_error_map_lease(tenant_id)

    async def refresh_tenant(self, tenant_id: str) -> None:
        """刷新一个已认证租户，并合并刚完成的首次同步与紧邻轮询。"""

        if not self.enabled or self._closing:
            return
        async with self._lock:
            state = self._states.setdefault(tenant_id, _TenantErrorMapState())
            # 首次后台同步可能恰在显式 waiter 取得锁前完成。一个轮询间隔内直接复用其 fresh
            # 结果，避免紧邻的第二个 GET；调度器在下一间隔仍会正常创建刷新任务。
            last_success = state.last_success_monotonic
            if (
                tenant_id not in self._refresh_tasks
                and last_success is not None
                and self._monotonic() - last_success
                < self._float_config("poll_interval_seconds", 2.0)
            ):
                return
            task = self._ensure_refresh_task_locked(tenant_id)
        if task is not None:
            await asyncio.shield(task)

    async def known_tenants(self) -> tuple[str, ...]:
        """返回排序后的已见租户 ID，仅供 scheduler 与测试观察。"""

        async with self._lock:
            return tuple(sorted(self._states))

    def _ensure_refresh_task_locked(self, tenant_id: str) -> asyncio.Task[None] | None:
        """在持锁状态创建或复用 keyed task；close 后绝不重新创建任务。"""

        if self._closing:
            return None
        state = self._states[tenant_id]
        # 记录“曾调度”而非“曾成功”，避免首次失败后 acquire 形成无退避重试风暴。
        state.initial_refresh_scheduled = True
        task = self._refresh_tasks.get(tenant_id)
        if task is None:
            task = asyncio.create_task(
                self._run_refresh_task(tenant_id),
                name=f"gateway-provider-error-map-refresh:{tenant_id}",
            )
            self._refresh_tasks[tenant_id] = task
        return task

    async def _run_refresh_task(self, tenant_id: str) -> None:
        """在 queue 与 HTTP 两层 deadline 内刷新，并原子移除当前 singleflight owner。"""

        current = asyncio.current_task()
        request_id = resolve_request_id(None)
        trace_id = resolve_trace_id(None, request_id)
        acquired = False
        try:
            try:
                await asyncio.wait_for(
                    self._refresh_slots.acquire(),
                    timeout=self._float_config("refresh_queue_timeout_seconds", 2.0),
                )
                acquired = True
            # 队列超时说明并发预算已满，只标记该租户失败，不能拖住业务请求或其他租户。
            except TimeoutError:
                await self._mark_failure(tenant_id, request_id=request_id, trace_id=trace_id)
                return
            async with asyncio.timeout(self._float_config("http_timeout_seconds", 2.0)):
                await self._refresh_once(tenant_id, request_id=request_id, trace_id=trace_id)
        # close 或调用方取消必须原样传播，不能被普通刷新失败逻辑吞掉。
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._mark_failure(tenant_id, request_id=request_id, trace_id=trace_id)
        finally:
            if acquired:
                self._refresh_slots.release()
            async with self._lock:
                # 只有当前 owner 能移除 keyed task，旧任务结束不能删除后创建的新一代刷新。
                if self._refresh_tasks.get(tenant_id) is current:
                    self._refresh_tasks.pop(tenant_id, None)

    async def _refresh_once(self, tenant_id: str, *, request_id: str, trace_id: str) -> None:
        """调用内部接口并完整验证 200/304 后原子替换租户制品。"""

        async with self._lock:
            state = self._states[tenant_id]
            etag = state.etag
        headers = {
            "X-Internal-Token": str(self._config.internal_config_token or ""),
            "X-Request-ID": request_id,
            "X-Trace-ID": trace_id,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        if etag is not None:
            headers["If-None-Match"] = etag
        url = (
            self._config.control_api_base_url.rstrip("/")
            + "/internal/error-maps/"
            + quote(tenant_id, safe="")
            + "/current"
        )
        async with self._http_client().stream("GET", url, headers=headers) as response:
            body = await self._read_bounded_body(response)
            if response.status_code == 304:
                # 304 必须无正文且回显当前强 ETag，否则不能推进快照的新鲜时间。
                if body or not await self._accept_not_modified(tenant_id, response.headers):
                    await self._mark_failure(tenant_id, request_id=request_id, trace_id=trace_id)
                return
            if response.status_code != 200:
                await self._mark_failure(tenant_id, request_id=request_id, trace_id=trace_id)
                return
            artifact, response_etag = self._parse_success(tenant_id, body, response.headers)
            # 单调 revision/hash 校验失败时保留原 LKG，禁止用回退或冲突快照覆盖缓存。
            if not await self._accept_success(tenant_id, artifact, response_etag):
                await self._mark_failure(tenant_id, request_id=request_id, trace_id=trace_id)

    async def _read_bounded_body(self, response: httpx.Response) -> bytes:
        """使用 raw streaming 在解析前执行压缩、声明长度和实际字节三层上限。"""

        limit = self._int_config("max_payload_bytes", 262_144)
        encoding = str(response.headers.get("Content-Encoding") or "").strip().lower()
        # 压缩响应会绕过原始字节上限，内部快照只接受未压缩 identity 编码。
        if encoding not in {"", "identity"}:
            raise ValueError("compressed provider error map snapshot is unsupported")
        length_values = response.headers.get_list("Content-Length")
        declared_length: int | None = None
        if length_values:
            # 重复 Content-Length 即使值相同也会造成代理解释差异，必须按不可信 wire 拒绝。
            if len(length_values) != 1 or _CONTENT_LENGTH_RE.fullmatch(length_values[0]) is None:
                raise ValueError("provider error map content length is invalid")
            declared_length = int(length_values[0])
            # 声明长度已越界时在读取任何正文前拒绝，避免无界内存占用。
            if declared_length > limit:
                raise ValueError("provider error map snapshot is too large")
        collected = bytearray()
        if response.is_stream_consumed:
            buffered = response.content
            # 已缓冲响应仍要重复执行上限，测试 transport 不能绕过真实 stream 门禁。
            if len(buffered) > limit:
                raise ValueError("provider error map snapshot is too large")
            collected.extend(buffered)
        else:
            async for chunk in response.aiter_raw():
                # 每个 chunk 合并前检查累计长度，单个超大 chunk 也不会造成临时越界分配。
                if len(collected) + len(chunk) > limit:
                    raise ValueError("provider error map snapshot is too large")
                collected.extend(chunk)
        # 声明长度与实际 EOF 不一致时快照不完整或被拼接，必须保留现有 LKG。
        if declared_length is not None and declared_length != len(collected):
            raise ValueError("provider error map content length does not match")
        return bytes(collected)

    def _parse_success(
        self, tenant_id: str, body: bytes, headers: httpx.Headers
    ) -> tuple[ProviderErrorMapArtifact, str]:
        """严格解析 JSON、重复 key、租户、目录、hash 和强 ETag。"""

        content_type = str(headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        # 内部成功响应只接受 JSON，HTML 错页和代理文本不能进入规则解析器。
        if content_type != "application/json":
            raise ValueError("provider error map snapshot content type is invalid")

        def object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
            """拒绝重复 key，防止签名/校验器与执行器读取不同字段。"""

            result: dict[str, object] = {}
            for key, value in pairs:
                # 重复键在解析器间可能选择不同值，拒绝整个快照才能保持租户与 hash 校验确定。
                if key in result:
                    raise ValueError("duplicate provider error map snapshot field")
                result[key] = value
            return result

        def reject_constant(_: str) -> None:
            """拒绝 NaN/Infinity 等非标准 JSON 常量，避免跨运行库产生不同快照摘要。"""

            # 非有限值不属于内部 wire 合同，统一中止解析而不是将其字符串化。
            raise ValueError("non-standard provider error map JSON constant")

        raw = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=object_without_duplicates,
            parse_constant=reject_constant,
        )
        # 顶层必须是 exact-schema object，数组或标量没有租户与 revision 归属。
        if not isinstance(raw, dict):
            raise ValueError("provider error map snapshot must be an object")
        artifact = deserialize_provider_error_map_snapshot(raw, expected_tenant_id=tenant_id)
        etag = headers.get("ETag")
        expected = provider_error_map_etag(
            tenant_id,
            artifact.revision,
            artifact.overlay_content_hash,
            artifact.builtin_catalog_version,
        )
        # 强 ETag 必须语法合法并与重新计算值一致，防止跨租户或旧 revision 缓存复用。
        if etag is None or _STRONG_ETAG_RE.fullmatch(etag) is None or etag != expected:
            raise ValueError("provider error map snapshot etag does not match")
        return artifact, etag

    async def _accept_not_modified(self, tenant_id: str, headers: httpx.Headers) -> bool:
        """304 只有回显当前强 ETag 才能刷新新鲜时间。"""

        async with self._lock:
            state = self._states[tenant_id]
            response_etag = headers.get("ETag")
            if (
                state.artifact is None
                or state.etag is None
                or response_etag != state.etag
                or _STRONG_ETAG_RE.fullmatch(response_etag or "") is None
            ):
                state.refresh_failed_since_success = True
                return False
            state.last_success_monotonic = self._monotonic()
            state.refresh_failed_since_success = False
            state.generation += 1
            state.fallback_warning_generation = None
            return True

    async def _accept_success(
        self, tenant_id: str, artifact: ProviderErrorMapArtifact, etag: str
    ) -> bool:
        """拒绝版本回退和同 revision 异 hash 后原子替换完整不可变制品。"""

        async with self._lock:
            state = self._states[tenant_id]
            current = state.artifact
            if current is not None:
                if artifact.revision < current.revision:
                    state.refresh_failed_since_success = True
                    return False
                if artifact.revision == current.revision and (
                    artifact.overlay_content_hash != current.overlay_content_hash
                    or artifact.builtin_catalog_version != current.builtin_catalog_version
                ):
                    state.refresh_failed_since_success = True
                    return False
            state.artifact = artifact
            state.etag = etag
            state.last_success_monotonic = self._monotonic()
            state.refresh_failed_since_success = False
            state.generation += 1
            state.fallback_warning_generation = None
            return True

    async def _mark_failure(self, tenant_id: str, *, request_id: str, trace_id: str) -> None:
        """保留 LKG/ETag，并只记录固定同步失败元数据。"""

        async with self._lock:
            state = self._states.setdefault(tenant_id, _TenantErrorMapState())
            state.refresh_failed_since_success = True
            generation = state.generation
        logger.warning(
            "provider_error_map.refresh_failed",
            extra={
                "event": "provider_error_map.refresh_failed",
                "tenant_id": tenant_id,
                "request_id": request_id,
                "trace_id": trace_id,
                "error_code": "error_map.snapshot_unavailable",
                "payload": {"generation": generation},
            },
        )

    async def _warn_builtin_fallback_once(
        self, tenant_id: str, generation: int, revision: int, age_seconds: int
    ) -> None:
        """LKG 超龄时每个 generation 最多记录一次固定降级告警。"""

        async with self._lock:
            state = self._states[tenant_id]
            if state.generation != generation or state.fallback_warning_generation == generation:
                return
            state.fallback_warning_generation = generation
        logger.warning(
            "provider_error_map.builtin_fallback",
            extra={
                "event": "provider_error_map.builtin_fallback",
                "tenant_id": tenant_id,
                "error_code": "error_map.snapshot_unavailable",
                "payload": {"revision": revision, "age_seconds": age_seconds},
            },
        )

    async def _scheduler_loop(self) -> None:
        """按固定间隔并行刷新所有已见 tenant，单租户失败不影响其他租户。"""

        interval = self._float_config("poll_interval_seconds", 2.0)
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
                return
            # 正常轮询间隔到期不是故障；继续刷新已见租户且不记录底层异常。
            except TimeoutError:
                pass
            tenants = await self.known_tenants()
            await asyncio.gather(
                *(self.refresh_tenant(tenant_id) for tenant_id in tenants),
                return_exceptions=True,
            )

    @staticmethod
    def _observe_refresh_task_result(task: asyncio.Task[None]) -> None:
        """消费已脱离关闭等待窗口的任务结果，避免异常正文或未检索告警泄漏。

        参数：已完成或完成后回调的 refresh owner task。返回：无。
        约束：只观察取消/异常状态，不记录底层 transport 异常文本，也不重新抛出关闭期异常。
        """

        if not task.cancelled():
            task.exception()

    def _http_client(self) -> httpx.AsyncClient:
        """延迟创建禁代理、禁重定向 client，避免内部 Token 被环境代理或 3xx 转发。"""

        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._float_config("http_timeout_seconds", 2.0),
                trust_env=False,
                follow_redirects=False,
            )
        return self._client


def install_provider_error_map_cache(app: FastAPI) -> None:
    """把错误规则 cache 组合进现有 lifespan，不覆盖事件、Policy 或用量恢复任务。"""

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        """原 lifespan 成功后启动 cache，退出前有界清理全部内部网络资源。"""

        async with original_lifespan(current_app):
            cache = ProviderErrorMapCache(current_app.state.config)
            current_app.state.provider_error_map_cache = cache
            await cache.start()
            try:
                yield
            finally:
                await cache.close()
                current_app.state.provider_error_map_cache = None

    app.router.lifespan_context = combined_lifespan
