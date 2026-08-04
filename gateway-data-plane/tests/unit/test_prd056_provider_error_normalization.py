"""PRD-056 Gateway 归一化兼容调用与请求期冻结租约合同测试。"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from metahub_gateway.api.openai_compatible import (
    _acquire_provider_error_map_lease,
    _provider_request,
    _provider_request_with_affinity,
)
from metahub_gateway.error_maps.facts import insert_provider_error_fact
from metahub_gateway.providers.base import (
    NormalizedProviderError,
    ProviderRequest,
    clear_normalize_error_capability_cache_for_tests,
    invoke_normalize_error,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.provider_error_map import (
    ProviderErrorMapLease,
    builtin_provider_error_map_lease,
    extract_provider_error_facts,
    normalize_provider_error,
)
from starlette.requests import Request


def _normalized_error() -> NormalizedProviderError:
    """构造不含任意上游正文的固定归一化结果，供兼容调用断言复用。"""

    return NormalizedProviderError(
        code="gateway.upstream_error",
        message="Upstream provider returned an error.",
        http_status=503,
        retryable=True,
        provider_error_type="server_error",
    )


def _request(app_state: object | None = None) -> Request:
    """构造最小 ASGI 请求，并固定安全 request_id/trace_id 供 ProviderRequest 复制。"""

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [],
            "app": SimpleNamespace(state=app_state or SimpleNamespace()),
        }
    )
    request.state.request_id = "req_prd056_gateway"
    request.state.trace_id = "trace_prd056_gateway"
    return request


def _provider_request_fixture(tenant_id: str) -> ProviderRequest:
    """返回无密钥、无正文日志副作用的最小标准 Provider 请求。"""

    return ProviderRequest(
        provider_id="provider-prd056",
        provider_code="provider_prd056",
        provider_type="openai_compatible",
        adapter_name="test",
        endpoint="chat/completions",
        url="https://provider.invalid/v1/chat/completions",
        headers={},
        payload={},
        tenant_id=tenant_id,
        model_id="model-prd056",
    )


def test_legacy_normalize_error_override_receives_only_two_positionals() -> None:
    """旧 override 不接收新关键字，兼容器必须只调用一次两个位置参数。"""

    class LegacyAdapter:
        """模拟升级前只实现 ``response, exc`` 的第三方 Adapter。"""

        def __init__(self) -> None:
            """初始化可观察调用次数，验证禁止 TypeError 探测重试。"""

            self.calls: list[tuple[httpx.Response | None, Exception | None]] = []

        def normalize_error(
            self,
            response: httpx.Response | None = None,
            exc: Exception | None = None,
        ) -> NormalizedProviderError:
            """记录旧合同参数并返回固定安全错误。"""

            self.calls.append((response, exc))
            return _normalized_error()

    clear_normalize_error_capability_cache_for_tests()
    adapter = LegacyAdapter()
    response = httpx.Response(503)
    result = invoke_normalize_error(
        cast(Any, adapter),
        response,
        None,
        request=_provider_request_fixture("tenant-a"),
        extracted_facts=None,
    )
    assert result.code == "gateway.upstream_error"
    assert adapter.calls == [(response, None)]


def test_normalize_error_internal_type_error_is_not_retried() -> None:
    """新 Adapter 内部真实 TypeError 必须原样传播，不能被误当成签名不兼容。"""

    class RaisingAdapter:
        """接受新合同但在业务实现中抛出 TypeError 的测试 Adapter。"""

        def __init__(self) -> None:
            """初始化调用计数器，确保异常路径没有第二次副作用。"""

            self.calls = 0

        def normalize_error(
            self,
            response: httpx.Response | None = None,
            exc: Exception | None = None,
            *,
            request: ProviderRequest | None = None,
            extracted_facts: object | None = None,
        ) -> NormalizedProviderError:
            """记录一次调用后模拟 Adapter 内部类型错误。"""

            self.calls += 1
            # 业务函数内部 TypeError 必须原样传播，不能被误判为不支持新关键字后再调用一次。
            raise TypeError("adapter-internal-type-error")

    clear_normalize_error_capability_cache_for_tests()
    adapter = RaisingAdapter()
    with pytest.raises(TypeError, match="adapter-internal-type-error"):
        invoke_normalize_error(
            cast(Any, adapter),
            request=_provider_request_fixture("tenant-a"),
        )
    assert adapter.calls == 1


@pytest.mark.anyio
async def test_request_acquires_one_lease_and_main_fallback_share_identity() -> None:
    """同一认证请求只 acquire 一次，主调用和无亲和 Fallback 复制同一不可变引用。"""

    lease = builtin_provider_error_map_lease("tenant-a")

    class FakeCache:
        """返回固定租约并记录 acquire 次数的应用级缓存。"""

        def __init__(self) -> None:
            """初始化调用计数，tenant 参数仍须由断言验证。"""

            self.calls: list[str] = []

        async def acquire(self, tenant_id: str) -> ProviderErrorMapLease:
            """记录可信租户并返回同一不可变租约。"""

            self.calls.append(tenant_id)
            return lease

    class LegacyBuildAdapter:
        """模拟尚未声明 error_map_lease 关键字的旧 build_request 实现。"""

        def __init__(self) -> None:
            """初始化构造次数，证明租约复制不要求调用新关键字。"""

            self.calls = 0

        def build_request(
            self,
            route: object,
            endpoint: str,
            payload: dict[str, Any],
            config: AppConfig,
        ) -> ProviderRequest:
            """按可信路由 tenant 返回最小请求，不访问缓存或控制面。"""

            self.calls += 1
            # 请求 tenant 只能复制可信路由值，旧 Adapter 也不得从 payload 或外部参数推导租户。
            return _provider_request_fixture(cast(Any, route).tenant_id)

    cache = FakeCache()
    request = _request(SimpleNamespace(provider_error_map_cache=cache))
    first = await _acquire_provider_error_map_lease(request, "tenant-a")
    second = await _acquire_provider_error_map_lease(request, "tenant-a")
    assert first is lease and second is lease
    assert cache.calls == ["tenant-a"]

    route = SimpleNamespace(tenant_id="tenant-a")
    adapter = LegacyBuildAdapter()
    config = cast(AppConfig, object())
    main = _provider_request(
        cast(Any, adapter), cast(Any, route), "chat/completions", {}, config, request
    )
    fallback = _provider_request_with_affinity(
        cast(Any, adapter),
        cast(Any, route),
        "chat/completions",
        {},
        config,
        request,
        affinity=None,
        attempt_sequence=2,
        routing_attempt_id="rta_prd056",
        stream_session_id=None,
    )
    assert main.error_map_lease is lease
    assert fallback.error_map_lease is lease
    assert main.error_map_lease is fallback.error_map_lease
    assert adapter.calls == 2
    # Endpoint、完整 URL 和请求体都使用 repr=False，异常工具不得偶然把它们写入日志。
    assert "provider.invalid" not in repr(main)
    assert "chat/completions" not in repr(main)


def test_provider_error_fact_writer_persists_only_frozen_projection() -> None:
    """Fact 写入器字段与占位符一致，且数据库中不出现原始上游 message。"""

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(
            """
            CREATE TABLE provider_error_facts(
              provider_error_fact_id TEXT, tenant_id TEXT, request_id TEXT, trace_id TEXT,
              routing_attempt_id TEXT, stream_session_id TEXT, provider_id TEXT, model_id TEXT,
              provider_code TEXT, upstream_http_status INTEGER, transport_kind TEXT,
              provider_error_code_class TEXT, provider_error_type_class TEXT,
              provider_error_code_present INTEGER, provider_error_type_present INTEGER,
              message_present INTEGER, redacted_projection_json TEXT, projection_hash TEXT,
              body_length_bucket TEXT, body_truncated INTEGER, is_json INTEGER,
              matched_error_map_id TEXT, matched_rule_version INTEGER, matched_pattern_hash TEXT,
              matched_rule_source TEXT, error_map_revision INTEGER, overlay_content_hash TEXT,
              builtin_catalog_version TEXT, error_map_snapshot_source TEXT, match_basis TEXT,
              normalized_output_error_code TEXT, owner_terminal_error_code TEXT, created_at TEXT
            )
            """
        )
        secret_message = "private-provider-token-sentinel"
        raw_body = (
            '{"error":{"type":"rate_limit_error","message":"' + secret_message + '"}}'
        ).encode()
        facts = extract_provider_error_facts(
            upstream_http_status=429,
            transport_kind="none",
            raw_body=raw_body,
            body_truncated=False,
            known_body_length=len(raw_body),
        )
        result = normalize_provider_error(
            facts,
            lease=builtin_provider_error_map_lease("tenant-a"),
            provider_id="provider-prd056",
        )
        fact_id = insert_provider_error_fact(
            connection,
            tenant_id="tenant-a",
            request_id="req-prd056-fact",
            trace_id="trace-prd056-fact",
            routing_attempt_id="rta-prd056-fact",
            provider_id="provider-prd056",
            model_id="model-prd056",
            provider_code="provider_prd056",
            evidence=result.evidence,
            owner_terminal_error_code="gateway.upstream_rate_limited",
            created_at="2026-07-29T10:00:00.000000Z",
        )
        row = connection.execute(
            "SELECT * FROM provider_error_facts WHERE provider_error_fact_id=?",
            (fact_id,),
        ).fetchone()
        assert row is not None
        assert row["tenant_id"] == "tenant-a"
        assert row["routing_attempt_id"] == "rta-prd056-fact"
        assert row["stream_session_id"] is None
        assert row["normalized_output_error_code"] == "gateway.upstream_rate_limited"
        assert row["message_present"] == 1
        assert secret_message not in row["redacted_projection_json"]
        assert "[REDACTED]" in row["redacted_projection_json"]
    finally:
        connection.close()


def test_provider_error_fact_writer_rejects_missing_or_double_owner() -> None:
    """Fact owner 必须精确二选一，错误输入在执行 SQL 前即被拒绝。"""

    facts = extract_provider_error_facts(
        upstream_http_status=503,
        transport_kind="none",
        raw_body=b"",
        body_truncated=False,
        known_body_length=0,
    )
    evidence = normalize_provider_error(
        facts,
        lease=builtin_provider_error_map_lease("tenant-a"),
        provider_id="provider-prd056",
    ).evidence
    connection = sqlite3.connect(":memory:")
    common = {
        "tenant_id": "tenant-a",
        "request_id": "req",
        "trace_id": "trace",
        "provider_id": "provider",
        "model_id": "model",
        "provider_code": "provider_code",
        "evidence": evidence,
        "owner_terminal_error_code": "gateway.upstream_error",
        "created_at": "2026-07-29T10:00:00.000000Z",
    }
    try:
        with pytest.raises(ValueError, match="provider_error_fact_owner_invalid"):
            insert_provider_error_fact(connection, **common)
        with pytest.raises(ValueError, match="provider_error_fact_owner_invalid"):
            insert_provider_error_fact(
                connection,
                routing_attempt_id="rta-owner",
                stream_session_id="str-owner",
                **common,
            )
    finally:
        connection.close()
