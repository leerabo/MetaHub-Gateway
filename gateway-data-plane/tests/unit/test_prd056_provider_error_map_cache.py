"""PRD-056 Gateway 错误规则缓存、租约、singleflight 与 LKG 专项测试。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from metahub_gateway.error_maps.cache import ProviderErrorMapCache
from metahub_shared.runtime.config import AppConfig, load_app_config
from metahub_shared.runtime.provider_error_map import (
    BUILTIN_CATALOG_VERSION,
    ProviderErrorMapArtifact,
    compile_provider_error_rule,
    provider_error_map_etag,
    provider_error_overlay_content_hash,
    serialize_provider_error_map_snapshot,
)


class FakeMonotonic:
    """提供不受 wall clock 回拨影响的可控单调时钟。"""

    def __init__(self) -> None:
        """从零开始，便于精确断言 fresh/LKG 硬边界。"""

        self.value = 0.0

    def __call__(self) -> float:
        """返回当前测试单调时间，用于精确验证 fresh、LKG 与超龄回落的硬边界。"""

        return self.value


def _config(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> AppConfig:
    """构造开启 PRD-056 同步的 Gateway 配置，兼容 Shared 字段并行落盘前后的测试。"""

    monkeypatch.setenv("METAHUB_ENV", "test")
    monkeypatch.setenv("METAHUB_SERVICE_MODE", "internal_only")
    monkeypatch.setenv("METAHUB_CONTROL_API_BASE_URL", "http://127.0.0.1:8001")
    monkeypatch.setenv(
        "METAHUB_INTERNAL_CONFIG_TOKEN",
        "provider-error-map-test-token-0123456789abcdef",
    )
    config = load_app_config("gateway-data-plane")
    values = {
        "provider_error_map_sync_enabled": True,
        "provider_error_map_poll_interval_seconds": 1,
        "provider_error_map_http_timeout_seconds": 1.0,
        "provider_error_map_refresh_queue_timeout_seconds": 1.0,
        "provider_error_map_fresh_seconds": 5.0,
        "provider_error_map_lkg_seconds": 60.0,
        "provider_error_map_max_payload_bytes": 262_144,
        "provider_error_map_refresh_concurrency": 4,
    }
    values.update(overrides)
    for key, value in values.items():
        # Pydantic 旧模型暂未知新字段时使用 object 写入；正式 Shared 字段落盘后仍得到同一属性语义。
        object.__setattr__(config, key, value)
    return config


def _rule() -> object:
    """构造一条完整 active 租户覆盖规则，供快照 hash、ETag 与缓存租约测试复用。"""

    return compile_provider_error_rule(
        {
            "error_map_id": "pem_0123456789abcdef0123456789abcdef",
            "tenant_id": "tenant-cache",
            "provider_id": "provider-a",
            "priority": 500,
            "http_statuses": [429],
            "http_status_class": None,
            "error_field": "error.code",
            "field_operator": "equals",
            "field_pattern": "tenant_quota",
            "message_operator": None,
            "message_pattern": None,
            "output_error_code": "gateway.upstream_quota_exceeded",
            "status": "active",
            "version": 1,
            "pattern_hash": "",
        }
    )


def _artifact(revision: int = 1) -> ProviderErrorMapArtifact:
    """构造 Shared 编译过的不可变租户覆盖制品。"""

    rule = _rule()
    return ProviderErrorMapArtifact(
        tenant_id="tenant-cache",
        revision=revision,
        overlay_content_hash=provider_error_overlay_content_hash((rule,)),
        builtin_catalog_version=BUILTIN_CATALOG_VERSION,
        rules=(rule,),
    )


def _response(artifact: ProviderErrorMapArtifact) -> httpx.Response:
    """生成带强 ETag 和链路标识的有效 Control 内部响应，模拟可信内部快照。"""

    payload = serialize_provider_error_map_snapshot(
        artifact, request_id="req-control", trace_id="trace-control"
    )
    return httpx.Response(
        200,
        json=payload,
        headers={
            "ETag": provider_error_map_etag(
                artifact.tenant_id,
                artifact.revision,
                artifact.overlay_content_hash,
                artifact.builtin_catalog_version,
            )
        },
    )


@pytest.mark.anyio
async def test_first_acquire_is_non_blocking_then_next_request_uses_fresh_singleflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首批请求立即使用 builtin，只创建一次后台拉取；完成后下一请求冻结同一 fresh 制品。"""

    config = _config(monkeypatch)
    artifact = _artifact()
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        """记录唯一内部请求并返回有效 v1 wire。"""

        calls.append(request)
        await asyncio.sleep(0)
        return _response(artifact)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = ProviderErrorMapCache(config, client=client)
    first = await asyncio.gather(*(cache.acquire("tenant-cache") for _ in range(20)))
    assert all(item.source == "builtin_fallback" and item.artifact.revision == 0 for item in first)
    await cache.refresh_tenant("tenant-cache")
    fresh = await cache.acquire("tenant-cache")
    assert len(calls) == 1
    assert calls[0].headers["Accept-Encoding"] == "identity"
    assert calls[0].headers["X-Internal-Token"] == config.internal_config_token
    assert fresh.source == "fresh" and fresh.artifact is artifact or fresh.artifact == artifact
    await cache.close()
    await client.aclose()


@pytest.mark.anyio
async def test_failed_initial_refresh_is_retried_only_by_scheduler_or_explicit_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """冷启动失败后业务 acquire 不得放大 Control 故障，显式刷新仍可按既定节奏重试。"""

    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        """持续返回固定 503，用调用次数观察是否出现请求驱动的失败风暴。"""

        nonlocal calls
        calls += 1
        return httpx.Response(503)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = ProviderErrorMapCache(_config(monkeypatch), client=client)
    first = await cache.acquire("tenant-cache")
    owner = cache._refresh_tasks["tenant-cache"]
    await owner

    repeated = await asyncio.gather(*(cache.acquire("tenant-cache") for _ in range(20)))
    await asyncio.sleep(0)
    assert first.source == "builtin_fallback"
    assert all(item.source == "builtin_fallback" for item in repeated)
    assert calls == 1

    await cache.refresh_tenant("tenant-cache")
    assert calls == 2
    await cache.close()
    await client.aclose()


@pytest.mark.anyio
async def test_304_extends_freshness_only_with_exact_current_etag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """严格 304 主流程刷新新鲜时间，漂移 ETag 则保留 LKG 并标记失败。"""

    clock = FakeMonotonic()
    artifact = _artifact()
    etag = provider_error_map_etag(
        artifact.tenant_id,
        artifact.revision,
        artifact.overlay_content_hash,
        artifact.builtin_catalog_version,
    )
    call_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        """第一次返回 200，第二次返回严格 304，第三次篡改 ETag。"""

        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _response(artifact)
        assert request.headers["If-None-Match"] == etag
        return httpx.Response(
            304, headers={"ETag": etag if call_count == 2 else '"' + "0" * 64 + '"'}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = ProviderErrorMapCache(_config(monkeypatch), client=client, monotonic=clock)
    await cache.acquire("tenant-cache")
    await cache.refresh_tenant("tenant-cache")
    clock.value = 4
    await cache.refresh_tenant("tenant-cache")
    assert (await cache.acquire("tenant-cache")).source == "fresh"
    clock.value = 8
    await cache.refresh_tenant("tenant-cache")
    assert (await cache.acquire("tenant-cache")).source == "lkg"
    await cache.close()
    await client.aclose()


@pytest.mark.anyio
async def test_lkg_hard_expiry_falls_back_without_blocking_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同步失败后按单调年龄进入 LKG，超过硬上限稳定回落 revision-0 builtin。"""

    clock = FakeMonotonic()
    artifact = _artifact()
    call_count = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        """首次成功，后续持续返回固定 503。"""

        nonlocal call_count
        call_count += 1
        return _response(artifact) if call_count == 1 else httpx.Response(503)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = ProviderErrorMapCache(_config(monkeypatch), client=client, monotonic=clock)
    await cache.acquire("tenant-cache")
    await cache.refresh_tenant("tenant-cache")
    await cache.refresh_tenant("tenant-cache")
    clock.value = 30
    assert (await cache.acquire("tenant-cache")).source == "lkg"
    clock.value = 61
    fallback = await cache.acquire("tenant-cache")
    assert fallback.source == "builtin_fallback"
    assert fallback.artifact.revision == 0
    await cache.close()
    await client.aclose()


@pytest.mark.anyio
async def test_tampered_tenant_hash_catalog_and_duplicate_json_keep_builtin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不可信 wire 的租户、hash、目录或重复 key 均不能污染 per-tenant cache。"""

    artifact = _artifact()
    valid = serialize_provider_error_map_snapshot(artifact, request_id="req", trace_id="trace")
    payloads = []
    for key, value in (
        ("tenant_id", "tenant-other"),
        ("overlay_content_hash", "0" * 64),
        ("builtin_catalog_version", "future-catalog"),
    ):
        item = dict(valid)
        item[key] = value
        payloads.append(json.dumps(item).encode())
    payloads.append(b'{"wire_schema_version":1,"wire_schema_version":1}')
    index = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        """逐次返回四类损坏响应，ETag 本身保持格式合法。"""

        nonlocal index
        body = payloads[index]
        index += 1
        return httpx.Response(
            200,
            content=body,
            headers={"Content-Type": "application/json", "ETag": '"' + "0" * 64 + '"'},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = ProviderErrorMapCache(_config(monkeypatch), client=client)
    for _ in payloads:
        await cache.acquire("tenant-cache")
        await cache.refresh_tenant("tenant-cache")
        assert (await cache.acquire("tenant-cache")).source == "builtin_fallback"
    await cache.close()
    await client.aclose()


@pytest.mark.anyio
async def test_waiter_cancellation_does_not_cancel_shared_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单个 waiter 取消后 cache owner 任务继续，其他 waiter 最终取得同一 v1 制品。"""

    artifact = _artifact()
    release = asyncio.Event()
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        """等待测试释放网络响应，制造可控共享任务窗口。"""

        nonlocal calls
        calls += 1
        await release.wait()
        return _response(artifact)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = ProviderErrorMapCache(_config(monkeypatch), client=client)
    await cache.acquire("tenant-cache")
    cancelled = asyncio.create_task(cache.refresh_tenant("tenant-cache"))
    survivor = asyncio.create_task(cache.refresh_tenant("tenant-cache"))
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    release.set()
    await survivor
    assert calls == 1
    assert (await cache.acquire("tenant-cache")).artifact.revision == 1
    await cache.close()
    await client.aclose()


@pytest.mark.anyio
async def test_close_cancels_refresh_and_clears_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """关闭期间卡住的内部请求被取消，close 返回后不能残留或重建 singleflight。"""

    entered = asyncio.Event()

    async def handler(_: httpx.Request) -> httpx.Response:
        """模拟不返回的 Control 请求，直到 cache close 主动取消。"""

        entered.set()
        await asyncio.Event().wait()
        # 若执行到此处说明 close 未及时取消内部请求，测试必须明确失败。
        raise AssertionError("unreachable")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = ProviderErrorMapCache(_config(monkeypatch), client=client)
    await cache.acquire("tenant-cache")
    await entered.wait()
    await cache.close()
    assert not cache._refresh_tasks
    assert (await cache.acquire("tenant-cache")).source == "builtin_fallback"
    await client.aclose()


@pytest.mark.anyio
async def test_close_has_hard_deadline_when_transport_swallows_first_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """异常 transport 吞掉首次取消时，关闭仍须按硬 deadline 返回且清空 owner 表。"""

    import metahub_gateway.error_maps.cache as cache_module

    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(_: httpx.Request) -> httpx.Response:
        """模拟持续忽略关闭期取消的不受控 transport，并允许测试最终释放任务。"""

        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            # transport 故意吞掉首次取消，用于证明 cache close 仍受硬 deadline 约束。
            except asyncio.CancelledError:
                continue
        return httpx.Response(503)

    monkeypatch.setattr(cache_module, "_SHUTDOWN_TIMEOUT_SECONDS", 0.01)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = ProviderErrorMapCache(_config(monkeypatch), client=client)
    await cache.acquire("tenant-cache")
    await entered.wait()
    owner = cache._refresh_tasks["tenant-cache"]

    await asyncio.wait_for(cache.close(), timeout=0.2)
    assert not cache._refresh_tasks
    assert not owner.done()

    release.set()
    await asyncio.wait_for(owner, timeout=0.2)
    await client.aclose()
