"""PRD-045 Gateway Policy 快照缓存、LKG 与纯求值专项测试。"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import time

import httpx
import pytest
from metahub_gateway.auth.virtual_key import GatewayAuthContext
from metahub_gateway.policies.cache import PolicySnapshotCache, PolicySnapshotLeaseDecision
from metahub_gateway.policies.runtime import (
    apply_policy_execution_result,
    evaluate_policy_snapshot,
    policy_execution_error,
)
from metahub_gateway.usage.ledger import RequestLogContext
from metahub_shared.runtime.config import AppConfig, load_app_config
from metahub_shared.runtime.policy_snapshot import (
    POLICY_SNAPSHOT_WIRE_SCHEMA_VERSION,
    PolicySnapshotArtifact,
    build_policy_snapshot_artifact,
    build_policy_snapshot_item,
)


class FakeMonotonic:
    """提供可前进但不受 wall clock 回拨影响的确定性 monotonic 时钟。"""

    def __init__(self) -> None:
        """从零开始，便于直接断言 max-age 边界。"""

        self.value = 0.0

    def __call__(self) -> float:
        """返回当前可控的单调时间值，供缓存新鲜度边界测试精确取样。"""

        return self.value


class SlowByteStream(httpx.AsyncByteStream):
    """模拟每个 chunk 都在 read timeout 内、但总响应时间超限的慢速内部流。"""

    def __init__(self, chunks: tuple[bytes, ...], delay_seconds: float) -> None:
        """保存有限 chunks 与逐块延迟；测试不创建无界生成器。"""

        self.chunks = chunks
        self.delay_seconds = delay_seconds

    async def __aiter__(self):
        """逐块等待后输出，专门验证总 deadline 而非相邻 read timeout。"""

        for chunk in self.chunks:
            await asyncio.sleep(self.delay_seconds)
            yield chunk


def _config(
    monkeypatch: pytest.MonkeyPatch,
    *,
    service_mode: str = "internal_only",
) -> AppConfig:
    """加载开启同步且时间门限最小可测的 test Gateway 配置。"""

    monkeypatch.setenv("METAHUB_ENV", "test")
    monkeypatch.setenv("METAHUB_SERVICE_MODE", service_mode)
    monkeypatch.setenv("METAHUB_POLICY_SNAPSHOT_SYNC_ENABLED", "true")
    monkeypatch.setenv("METAHUB_POLICY_SNAPSHOT_POLL_INTERVAL_SECONDS", "1")
    monkeypatch.setenv("METAHUB_POLICY_SNAPSHOT_MAX_AGE_SECONDS", "5")
    monkeypatch.setenv("METAHUB_POLICY_SNAPSHOT_EMERGENCY_MAX_AGE_SECONDS", "60")
    monkeypatch.setenv("METAHUB_CONTROL_API_BASE_URL", "http://127.0.0.1:8001")
    monkeypatch.setenv(
        "METAHUB_INTERNAL_CONFIG_TOKEN",
        "policy-snapshot-test-token-0123456789abcdef",
    )
    return load_app_config("gateway-data-plane")


def _snapshot(
    version: int = 1,
    *,
    tenant_id: str = "tenant_cache",
    action_type: str = "deny",
    expression: str = 'env == "prod"',
    target_model_id: str | None = None,
) -> PolicySnapshotArtifact:
    """构造由 shared 编译器签名的单策略不可变快照。"""

    item = build_policy_snapshot_item(
        evaluation_order=0,
        policy_id="pol_cache",
        policy_version=version,
        policy_rule_id="prule_cache",
        policy_action_id="pact_cache",
        priority=100,
        action_type=action_type,
        target_model_id=target_model_id,
        expression=expression,
    )
    return build_policy_snapshot_artifact(
        tenant_id=tenant_id,
        policy_snapshot_id=f"psnap_cache_{version}",
        snapshot_version=version,
        operation="publish",
        previous_snapshot_id=f"psnap_cache_{version - 1}" if version > 1 else None,
        previous_snapshot_version=version - 1 if version > 1 else None,
        policies=(item,),
    )


def _etag(
    snapshot: PolicySnapshotArtifact | None,
    tenant_id: str = "tenant_cache",
) -> str:
    """按照控制面线协议、租户、版本与制品哈希合同独立生成强校验标识。"""

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


def _response_payload(
    tenant_id: str,
    snapshot: PolicySnapshotArtifact | None,
) -> dict[str, object]:
    """生成 Control 内部 API 的严格 wrapper，不加入额外字段。"""

    return {
        "wire_schema_version": POLICY_SNAPSHOT_WIRE_SCHEMA_VERSION,
        "tenant_id": tenant_id,
        "snapshot": snapshot.as_wire_dict() if snapshot is not None else None,
        "request_id": "req_control_snapshot",
        "trace_id": "trc_control_snapshot",
    }


@pytest.mark.anyio
async def test_first_tenant_refresh_is_singleflight_and_freezes_current_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主流程：并发首请求只拉取一次，全部租约固定同一 v1 引用。"""

    config = _config(monkeypatch)
    artifact = _snapshot()
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        """记录唯一内部请求并返回有效 wire。"""

        calls.append(request)
        await asyncio.sleep(0)
        return httpx.Response(
            200,
            json=_response_payload("tenant_cache", artifact),
            headers={"ETag": _etag(artifact)},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = PolicySnapshotCache(config, client=client)
    decisions = await asyncio.gather(*(cache.acquire("tenant_cache") for _ in range(20)))

    assert len(calls) == 1
    assert calls[0].headers["X-Internal-Token"] == config.internal_config_token
    assert calls[0].headers["Accept-Encoding"] == "identity"
    assert all(item.executable and item.status == "current" for item in decisions)
    snapshots = {id(item.snapshot) for item in decisions}
    assert all(item.snapshot_version == 1 for item in decisions)
    assert len(snapshots) == 1
    await cache.close()
    await client.aclose()


@pytest.mark.anyio
async def test_invalid_payload_does_not_save_etag_and_same_version_can_retry(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """异常流程：损坏 hash 保留空 LKG，失败 ETag 不进入下一次 If-None-Match。"""

    config = _config(monkeypatch)
    artifact = _snapshot()
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        """第一次篡改 artifact hash，第二次返回同版本正确制品。"""

        requests.append(request)
        payload = _response_payload("tenant_cache", artifact)
        if len(requests) == 1:
            tampered = dict(payload["snapshot"])
            tampered["artifact_hash"] = "0" * 64
            payload["snapshot"] = tampered
        return httpx.Response(200, json=payload, headers={"ETag": _etag(artifact)})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = PolicySnapshotCache(config, client=client)

    unavailable = await cache.acquire("tenant_cache")
    recovered = await cache.acquire("tenant_cache")

    assert unavailable.executable is False
    assert unavailable.status == "unavailable"
    assert recovered.executable is True and recovered.snapshot_version == 1
    assert "If-None-Match" not in requests[1].headers
    failures = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "policy_snapshot.refresh_failed"
    ]
    assert failures
    assert getattr(failures[0], "request_id", "")
    assert getattr(failures[0], "trace_id", "")
    await cache.close()
    await client.aclose()


@pytest.mark.anyio
async def test_lkg_emergency_and_hard_expiry_use_monotonic_age(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证当前、常规兜底、应急兜底到硬过期全流程都使用单调时间且版本固定。"""

    config = _config(monkeypatch)
    clock = FakeMonotonic()
    artifact = _snapshot()
    call_count = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        """首次返回有效控制面快照，后续持续模拟服务不可用响应以推动兜底状态。"""

        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(
                200,
                json=_response_payload("tenant_cache", artifact),
                headers={"ETag": _etag(artifact)},
            )
        return httpx.Response(503, json={"error": {"code": "infra.unavailable"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = PolicySnapshotCache(config, client=client, monotonic=clock)
    assert (await cache.acquire("tenant_cache")).status == "current"
    await cache.refresh_tenant("tenant_cache")

    clock.value = 5
    lkg = await cache.acquire("tenant_cache")
    clock.value = 6
    emergency = await cache.acquire("tenant_cache")
    clock.value = 61
    expired = await cache.acquire("tenant_cache")

    assert (lkg.status, lkg.snapshot_version, lkg.executable) == ("lkg", 1, True)
    assert (emergency.status, emergency.snapshot_version, emergency.executable) == (
        "emergency_lkg",
        1,
        True,
    )
    assert (expired.status, expired.snapshot_version, expired.executable) == (
        "expired",
        1,
        False,
    )
    assert expired.error_code == "gateway.policy_snapshot_expired"
    await cache.close()
    await client.aclose()


@pytest.mark.anyio
async def test_public_service_blocks_at_normal_stale_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """公网模式：普通 max-age 后立即阻断，不使用 internal emergency 窗口。"""

    config = _config(monkeypatch, service_mode="public_service")
    clock = FakeMonotonic()
    artifact = _snapshot()
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        """首次返回 v1，第二次制造同步失败。"""

        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json=_response_payload("tenant_cache", artifact),
                headers={"ETag": _etag(artifact)},
            )
        return httpx.Response(503)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = PolicySnapshotCache(config, client=client, monotonic=clock)
    await cache.acquire("tenant_cache")
    await cache.refresh_tenant("tenant_cache")
    clock.value = 6

    blocked = await cache.acquire("tenant_cache")

    assert blocked.status == "expired"
    assert blocked.snapshot_version == 1
    assert blocked.error_code == "gateway.policy_snapshot_stale"
    await cache.close()
    await client.aclose()


@pytest.mark.anyio
async def test_no_head_baseline_remains_distinct_from_cold_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空基线：成功同步 null 是 not_published，故障后按 NULL-version LKG 生命周期演进。"""

    config = _config(monkeypatch)
    clock = FakeMonotonic()
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        """首次明确无 head，之后模拟网络故障。"""

        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json=_response_payload("tenant_cache", None),
                headers={"ETag": _etag(None)},
            )
        # 第二次请求模拟控制面网络中断，验证空基线同样按 LKG 生命周期保护。
        raise httpx.ConnectError("control unavailable")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = PolicySnapshotCache(config, client=client, monotonic=clock)
    baseline = await cache.acquire("tenant_cache")
    await cache.refresh_tenant("tenant_cache")
    clock.value = 2
    lkg = await cache.acquire("tenant_cache")

    assert (baseline.status, baseline.snapshot_version, baseline.executable) == (
        "not_published",
        None,
        True,
    )
    assert (lkg.status, lkg.snapshot_version, lkg.executable) == ("lkg", None, True)
    await cache.close()
    await client.aclose()


@pytest.mark.anyio
async def test_monotonic_age_expires_snapshot_even_without_recorded_refresh_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """scheduler 停滞边界：没有显式错误也不能让旧 v1 超过 hard max-age 后继续执行。"""

    config = _config(monkeypatch)
    clock = FakeMonotonic()
    artifact = _snapshot()

    async def handler(_: httpx.Request) -> httpx.Response:
        """只完成首次同步，之后测试直接推进 monotonic 模拟 scheduler 饥饿。"""

        return httpx.Response(
            200,
            json=_response_payload("tenant_cache", artifact),
            headers={"ETag": _etag(artifact)},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = PolicySnapshotCache(config, client=client, monotonic=clock)
    assert (await cache.acquire("tenant_cache")).status == "current"
    clock.value = 6
    assert (await cache.acquire("tenant_cache")).status == "emergency_lkg"
    clock.value = 61

    expired = await cache.acquire("tenant_cache")

    assert expired.executable is False
    assert expired.snapshot_version == 1
    assert expired.error_code == "gateway.policy_snapshot_expired"
    await cache.close()
    await client.aclose()


@pytest.mark.anyio
async def test_304_requires_exact_strong_etag_before_refreshing_freshness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缓存协议：缺 ETag 的 304 只能形成 LKG，正确回显后才恢复 current。"""

    config = _config(monkeypatch)
    clock = FakeMonotonic()
    artifact = _snapshot()
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        """依次返回有效 200、缺标识 304、正确强标识 304。"""

        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json=_response_payload("tenant_cache", artifact),
                headers={"ETag": _etag(artifact)},
            )
        assert request.headers["If-None-Match"] == _etag(artifact)
        if calls == 2:
            # 缺少 ETag 的 304 不能延长旧制品新鲜期。
            return httpx.Response(304)
        return httpx.Response(304, headers={"ETag": _etag(artifact)})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = PolicySnapshotCache(config, client=client, monotonic=clock)
    await cache.acquire("tenant_cache")
    clock.value = 2
    await cache.refresh_tenant("tenant_cache")
    assert (await cache.acquire("tenant_cache")).status == "lkg"
    clock.value = 3
    await cache.refresh_tenant("tenant_cache")

    refreshed = await cache.acquire("tenant_cache")

    assert refreshed.status == "current"
    assert refreshed.snapshot_version == 1
    await cache.close()
    await client.aclose()


@pytest.mark.anyio
async def test_deeply_nested_json_is_normalized_to_unavailable_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """协议攻击：小体积深嵌套 JSON 的 RecursionError 不得穿透为未统一 500。"""

    config = _config(monkeypatch)
    # 嵌套层数远高于 Python JSON 递归上限，但总字节仍低于 payload 门限，专门验证解析异常收敛。
    body = b"[" * 10_000 + b"null" + b"]" * 10_000

    async def handler(_: httpx.Request) -> httpx.Response:
        """返回合法大小和强 ETag、但结构深度恶意的内部响应。"""

        return httpx.Response(
            200,
            content=body,
            headers={"ETag": _etag(None), "Content-Type": "application/json"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = PolicySnapshotCache(config, client=client)

    decision = await cache.acquire("tenant_cache")

    assert decision.executable is False
    assert decision.status == "unavailable"
    assert decision.error_code == "gateway.policy_snapshot_unavailable"
    await cache.close()
    await client.aclose()


@pytest.mark.anyio
async def test_compressed_internal_response_is_rejected_under_identity_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """代理兼容：即使服务端忽略 identity 返回 gzip，也要安全失败而不是误读 raw JSON。"""

    config = _config(monkeypatch)
    artifact = _snapshot()
    encoded = json.dumps(
        _response_payload("tenant_cache", artifact),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    async def handler(request: httpx.Request) -> httpx.Response:
        """确认客户端禁压缩后，模拟违规反向代理仍返回 gzip 实体。"""

        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(
            200,
            content=gzip.compress(encoded),
            headers={
                "ETag": _etag(artifact),
                "Content-Type": "application/json",
                "Content-Encoding": "gzip",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = PolicySnapshotCache(config, client=client)

    decision = await cache.acquire("tenant_cache")

    assert decision.status == "unavailable"
    assert decision.executable is False
    await cache.close()
    await client.aclose()


@pytest.mark.anyio
async def test_slow_chunk_stream_obeys_whole_refresh_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有界网络：每个 chunk 都及时到达也不能让整个首次同步超过总超时。"""

    config = _config(monkeypatch).model_copy(
        update={"policy_snapshot_http_timeout_seconds": 1}
    )
    artifact = _snapshot()
    body = json.dumps(
        _response_payload("tenant_cache", artifact),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    midpoint = len(body) // 2

    async def handler(_: httpx.Request) -> httpx.Response:
        """两个 chunk 各延迟 0.6 秒：单次 read 未超时，但总耗时超过 1 秒。"""

        return httpx.Response(
            200,
            stream=SlowByteStream((body[:midpoint], body[midpoint:]), 0.6),
            headers={"ETag": _etag(artifact), "Content-Type": "application/json"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = PolicySnapshotCache(config, client=client)
    started = time.perf_counter()

    decision = await cache.acquire("tenant_cache")
    elapsed = time.perf_counter() - started

    assert elapsed < 1.8
    assert decision.status == "unavailable"
    await cache.close()
    await client.aclose()


@pytest.mark.anyio
async def test_refresh_slot_queue_time_is_bounded_for_new_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并发上限：四个慢 tenant 占满槽时，第五个首请求仍在自身 deadline 内失败关闭。"""

    config = _config(monkeypatch).model_copy(
        update={"policy_snapshot_http_timeout_seconds": 1}
    )
    four_entered = asyncio.Event()
    blocker = asyncio.Event()
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        """让前四个内部请求停在 transport，模拟全部刷新槽被慢租户占用。"""

        nonlocal calls
        calls += 1
        if calls >= 4:
            four_entered.set()
        await blocker.wait()
        return httpx.Response(503)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = PolicySnapshotCache(config, client=client)
    occupied = [
        asyncio.create_task(cache.acquire(f"tenant_slow_{index}"))
        for index in range(4)
    ]
    await four_entered.wait()
    started = time.perf_counter()

    fifth = await cache.acquire("tenant_waiting")
    elapsed = time.perf_counter() - started
    occupied_results = await asyncio.gather(*occupied)

    assert elapsed < 1.8
    assert fifth.status == "unavailable"
    assert all(item.status == "unavailable" for item in occupied_results)
    await cache.close()
    await client.aclose()


@pytest.mark.anyio
async def test_more_than_four_known_tenants_eventually_refresh_without_starvation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """调度公平性：超过并发槽数量的 tenant 分批排队后都能获得自己的有效快照。"""

    config = _config(monkeypatch).model_copy(
        update={"policy_snapshot_http_timeout_seconds": 1}
    )
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        """每个 tenant 在单次网络 deadline 内返回，但十二个请求必须经历三轮并发槽。"""

        tenant_id = request.url.path.split("/")[-2]
        seen.append(tenant_id)
        await asyncio.sleep(0.1)
        artifact = _snapshot(tenant_id=tenant_id)
        return httpx.Response(
            200,
            json=_response_payload(tenant_id, artifact),
            headers={"ETag": _etag(artifact, tenant_id)},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = PolicySnapshotCache(config, client=client)
    tenants = tuple(f"tenant_fair_{index}" for index in range(12))

    await asyncio.gather(*(cache.refresh_tenant(tenant_id) for tenant_id in tenants))
    decisions = await asyncio.gather(*(cache.acquire(tenant_id) for tenant_id in tenants))

    assert set(seen) == set(tenants)
    assert all(item.status == "current" for item in decisions)
    assert all(item.snapshot_version == 1 for item in decisions)
    await cache.close()
    await client.aclose()


@pytest.mark.anyio
async def test_cancelled_waiter_does_not_cancel_shared_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并发边界：首 waiter 取消后，共享刷新仍服务第二个请求且只调用一次 Control。"""

    config = _config(monkeypatch)
    artifact = _snapshot()
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        """阻塞内部响应，给测试制造可控取消窗口。"""

        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return httpx.Response(
            200,
            json=_response_payload("tenant_cache", artifact),
            headers={"ETag": _etag(artifact)},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = PolicySnapshotCache(config, client=client)
    first = asyncio.create_task(cache.acquire("tenant_cache"))
    await entered.wait()
    second = asyncio.create_task(cache.acquire("tenant_cache"))
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()

    decision = await second

    assert calls == 1
    assert decision.snapshot_version == 1 and decision.status == "current"
    await cache.close()
    await client.aclose()


@pytest.mark.anyio
async def test_close_cancels_hanging_refresh_and_closes_owned_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """关闭边界：挂起内部拉取必须有界取消，scheduler 退出且自有 HTTP client 被关闭。"""

    from metahub_gateway.policies import cache as cache_module

    config = _config(monkeypatch)
    entered = asyncio.Event()
    never_release = asyncio.Event()

    async def handler(_: httpx.Request) -> httpx.Response:
        """模拟 Control 已接收请求但永久不返回响应，直到 Gateway 生命周期取消任务。"""

        entered.set()
        await never_release.wait()
        # 该返回只满足 transport 类型合同；正确的 close 路径永远不会执行到这里。
        return httpx.Response(503)

    # 缩短的只是真实关闭硬上限，不改变生产默认 5 秒合同；测试据此验证不会无限等待。
    monkeypatch.setattr(cache_module, "SHUTDOWN_TIMEOUT_SECONDS", 0.02)
    owned_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = PolicySnapshotCache(config)
    # 保留构造时 owns_client=True，同时注入可控 transport，才能验证 close 的资源所有权分支。
    cache._client = owned_client
    await cache.start()
    refresh_waiter = asyncio.create_task(cache.refresh_tenant("tenant_shutdown"))
    await asyncio.wait_for(entered.wait(), timeout=1.0)

    started = time.perf_counter()
    await cache.close()
    elapsed = time.perf_counter() - started
    refresh_result = await asyncio.gather(refresh_waiter, return_exceptions=True)

    assert elapsed < 0.5
    assert isinstance(refresh_result[0], asyncio.CancelledError)
    assert owned_client.is_closed is True
    assert cache._client is None
    assert cache._scheduler_task is None
    assert cache._refresh_tasks == {}


def test_runtime_maps_deny_and_preserves_frozen_log_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """求值主流程：prod 命中 deny，日志摘要保留 lease v1 且不包含表达式。"""

    config = _config(monkeypatch).model_copy(
        update={"policy_snapshot_sync_enabled": False, "policy_evaluation_timeout_ms": 100}
    )
    artifact = _snapshot(expression='env == "prod"')
    lease = PolicySnapshotLeaseDecision(True, artifact, 1, "current", None, 1.0)
    auth = GatewayAuthContext(
        tenant_id="tenant_cache",
        virtual_key_id="vk_cache",
        key_prefix="mhv_test",
        environment="prod",
        project_id=None,
    )
    base = RequestLogContext(
        tenant_id="tenant_cache",
        request_id="req_policy_deny",
        trace_id="trc_policy_deny",
        snapshot_version=1,
        policy_snapshot_status="current",
    )

    result = evaluate_policy_snapshot(config, lease, auth, "default-chat")
    logged = apply_policy_execution_result(base, result)
    error = policy_execution_error(
        result,
        tenant_id=auth.tenant_id,
        request_id=base.request_id,
        trace_id=base.trace_id,
        snapshot_version=1,
    )

    assert result.decision == "deny" and result.policy_id == "pol_cache"
    assert logged.snapshot_version == 1 and logged.policy_reason_code == "policy.rule_matched"
    assert error is not None and error.code == "gateway.policy_denied"


def test_runtime_missing_budget_context_fails_closed_without_fabricated_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """异常流程：无适用预算规则时不伪造无限余额，实际访问变量会按 403 拒绝。"""

    config = _config(monkeypatch).model_copy(
        update={"policy_snapshot_sync_enabled": False, "policy_evaluation_timeout_ms": 100}
    )
    artifact = _snapshot(expression="budget_remaining < 10")
    lease = PolicySnapshotLeaseDecision(True, artifact, 1, "current", None, 1.0)
    auth = GatewayAuthContext(
        tenant_id="tenant_cache",
        virtual_key_id="vk_cache",
        key_prefix="mhv_test",
        environment="dev",
    )
    monkeypatch.setattr(
        "metahub_gateway.policies.runtime.load_policy_budget_remaining",
        lambda _config, _auth: None,
    )

    result = evaluate_policy_snapshot(config, lease, auth, "default-chat")
    error = policy_execution_error(
        result,
        tenant_id=auth.tenant_id,
        request_id="req_budget_missing",
        trace_id="trc_budget_missing",
        snapshot_version=1,
    )

    assert result.missing_variable == "budget_remaining"
    assert error is not None and error.code == "gateway.policy_context_missing"


def test_runtime_route_to_returns_only_safe_target_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """路由流程：route_to 只返回目标 ID 和稳定命中摘要，具体路由由权威 resolver 执行。"""

    config = _config(monkeypatch).model_copy(
        update={"policy_snapshot_sync_enabled": False, "policy_evaluation_timeout_ms": 100}
    )
    artifact = _snapshot(
        action_type="route_to",
        expression='tenant == "tenant_cache"',
        target_model_id="model_policy_target",
    )
    lease = PolicySnapshotLeaseDecision(True, artifact, 1, "current", None, 1.0)
    auth = GatewayAuthContext(
        tenant_id="tenant_cache",
        virtual_key_id="vk_cache",
        key_prefix="mhv_test",
        environment="dev",
    )

    result = evaluate_policy_snapshot(config, lease, auth, "default-chat")

    assert result.decision == "route_to"
    assert result.target_model_id == "model_policy_target"
    assert policy_execution_error(
        result,
        tenant_id=auth.tenant_id,
        request_id="req_route",
        trace_id="trc_route",
        snapshot_version=1,
    ) is None


def test_runtime_mask_is_modifier_and_keeps_first_safe_policy_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PRD-068 主流程：单独 mask 不成为最终决定，默认 allow 并保留首个稳定修饰 Policy ID。"""

    config = _config(monkeypatch).model_copy(
        update={"policy_snapshot_sync_enabled": False, "policy_evaluation_timeout_ms": 100}
    )
    artifact = _snapshot(action_type="mask", expression='project == "project_cache"')
    lease = PolicySnapshotLeaseDecision(True, artifact, 1, "current", None, 1.0)
    auth = GatewayAuthContext(
        tenant_id="tenant_cache",
        virtual_key_id="vk_cache",
        key_prefix="mhv_test",
        environment="prod",
        project_id="project_cache",
    )

    result = evaluate_policy_snapshot(config, lease, auth, "default-chat")

    assert result.decision == "allow"
    assert result.policy_id is None
    assert result.mask_required is True
    assert result.mask_policy_id == "pol_cache"
    assert policy_execution_error(
        result,
        tenant_id=auth.tenant_id,
        request_id="req_mask",
        trace_id="trc_mask",
        snapshot_version=1,
    ) is None
