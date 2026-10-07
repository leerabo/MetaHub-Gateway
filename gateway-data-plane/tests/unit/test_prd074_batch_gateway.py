"""本文件验证 PRD-074 批处理调度边界，覆盖租户隔离、权限拒绝、并发领取、失败追踪与回退。

定义：模块职责与安全边界。
所有调用路径均维持租户隔离、脱敏和失败追踪。

定义：模块职责与安全边界。
所有调用路径均维持租户隔离、脱敏和失败追踪。

PRD-074 Gateway admission, trusted invocation, and durable Usage contracts.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_gateway.api import openai_compatible as openai_api
from metahub_gateway.api.internal_batch import (
    _authenticate_internal_service,
    _read_bounded_body,
    _validate_request_surface,
    _validated_request_payload,
    _validated_route_plan,
)
from metahub_gateway.api.internal_batch import (
    router as internal_batch_router,
)
from metahub_gateway.auth.virtual_key import GatewayAuthContext
from metahub_gateway.batch import (
    BatchAllocationCoverage,
    BatchGatewayGate,
    BatchRouteCandidateCoverage,
    BatchRouteCoverage,
    TrustedBatchContext,
    TrustedGatewayInvocation,
)
from metahub_gateway.rate_limits import service as rate_limit_service
from metahub_gateway.rate_limits.models import RateLimitRule
from metahub_gateway.rate_limits.store import (
    BatchRateLimitDimension,
    InMemoryRateLimitStore,
)
from metahub_gateway.routing import models as routing_models
from metahub_gateway.usage.ledger import (
    CostEstimate,
    PriceSnapshotEvidence,
    RequestLogContext,
    _deserialize_usage_spool_record,
    _prepare_usage_ledger_intent,
    _serialize_usage_spool_record,
)
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import create_service_app
from metahub_shared.runtime.logging import JsonFormatter
from metahub_shared.runtime.routing_policy import TokenEstimate
from starlette.requests import Request


def _auth(*, tenant_id: str = "tenant_prd074") -> GatewayAuthContext:
    """验证批处理场景。

    定义：_auth。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    # 权限以后端校验结果为准；失败时拒绝并保留脱敏审计事实。
    return GatewayAuthContext(
        tenant_id=tenant_id,
        virtual_key_id="vkey_prd074",
        key_prefix="mh_batch",
        environment="dev",
        environment_id="env_prd074",
        environment_name="Development",
        environment_status="active",
        project_id="project_prd074",
        application_id=None,
    )


def _batch_context(*, tenant_id: str = "tenant_prd074") -> TrustedBatchContext:
    """验证批处理场景。

    定义：_batch_context。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    candidate = BatchRouteCandidateCoverage(
        role="default",
        model_id="model_prd074",
        provider_id="provider_prd074",
        input_price_per_1k="0.000000",
        output_price_per_1k="0.000000",
        price_source="price_plan",
        price_version="price_v1",
    )
    return TrustedBatchContext(
        tenant_id=tenant_id,
        batch_job_id="bjob_" + "a" * 32,
        batch_task_id="btsk_" + "b" * 32,
        batch_attempt_id="batm_" + "c" * 32,
        request_id="req_prd074_gateway",
        trace_id="trc_prd074_gateway",
        route_plan_hash="d" * 64,
        route=BatchRouteCoverage((candidate,)),
        token_estimate=TokenEstimate(
            input_tokens=272,
            output_tokens=256,
            truncated=False,
        ),
        allocations=(
            BatchAllocationCoverage(
                batch_budget_allocation_id="bbal_" + "e" * 32,
                budget_rule_id="budget_prd074",
                budget_date="2026-08-08",
                reserved_cost_cny="0.000000",
            ),
        ),
    )


def test_trusted_invocation_rejects_cross_source_and_partial_batch_context() -> None:
    """验证批处理场景。

    定义：test_trusted_invocation_rejects_cross_source_and_partial_batch_context。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    batch = _batch_context()
    invocation = TrustedGatewayInvocation(
        source="batch",
        auth_context=_auth(),
        payload={"model": "alias", "messages": [], "stream": False},
        batch=batch,
    )
    original = invocation.payload
    original["stream"] = True
    assert invocation.payload["stream"] is False

    with pytest.raises(ValueError, match="trusted_gateway_invocation_invalid"):
        TrustedGatewayInvocation(
            source="batch",
            auth_context=_auth(tenant_id="tenant_other"),
            payload={"stream": False},
            batch=batch,
        )
    with pytest.raises(ValueError, match="trusted_gateway_invocation_invalid"):
        TrustedGatewayInvocation(
            source="batch",
            auth_context=_auth(),
            payload={"stream": True},
            batch=batch,
        )
    with pytest.raises(ValueError, match="trusted_gateway_invocation_invalid"):
        TrustedGatewayInvocation(
            source="enterprise_chat",
            auth_context=_auth(),
            payload={"stream": False},
            batch=batch,
        )


def test_internal_batch_token_rejects_missing_wrong_and_duplicate_headers() -> None:
    """验证批处理场景。

    定义：test_internal_batch_token_rejects_missing_wrong_and_duplicate_headers。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    expected = "test-only-prd074-internal-token"
    config = SimpleNamespace(internal_batch_token=expected)

    valid = Request(
        {
            "type": "http",
            "headers": [(b"x-metahub-internal-batch-token", expected.encode("ascii"))],
        }
    )
    _authenticate_internal_service(valid, config)

    rejected_headers = (
        [],
        [(b"x-metahub-internal-batch-token", b"wrong-test-token")],
        [
            (b"x-metahub-internal-batch-token", expected.encode("ascii")),
            (b"x-metahub-internal-batch-token", expected.encode("ascii")),
        ],
    )
    for headers in rejected_headers:
        with pytest.raises(ApiError) as captured:
            _authenticate_internal_service(
                Request({"type": "http", "headers": headers}),
                config,
            )
        assert captured.value.code == "batch.execution_invalid"


@pytest.mark.anyio
async def test_duplicate_batch_tokens_cannot_collide_with_chain_ids(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """验证批处理场景。

    定义：test_duplicate_batch_tokens_cannot_collide_with_chain_ids。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    synthetic_token = "test-only-prd074-duplicate-token-00000001"
    app = create_service_app("gateway-data-plane", "PRD-074 Batch Token Boundary")
    app.state.config = app.state.config.model_copy(
        update={"internal_batch_token": synthetic_token}
    )
    app.include_router(internal_batch_router)
    caplog.set_level(logging.INFO, logger="metahub.runtime")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://gateway") as client:
        response = await client.post(
            "/internal/batch/chat/completions",
            headers=[
                ("X-MetaHub-Internal-Batch-Token", "test-only-wrong-token"),
                ("X-MetaHub-Internal-Batch-Token", synthetic_token),
                ("X-Request-ID", synthetic_token),
                ("X-Trace-ID", synthetic_token),
                ("Content-Type", "application/json"),
            ],
            content=b'{"grant":"grant_test_abcdefghijklmnopqrstuvwxyz0123456789"}',
        )

    assert response.status_code == 403
    assert response.json()["code"] == "batch.execution_invalid"
    assert response.headers["x-request-id"].startswith("req_")
    assert response.headers["x-trace-id"].startswith("trc_")
    assert synthetic_token not in response.text
    assert synthetic_token not in "\n".join(
        JsonFormatter().format(record)
        for record in caplog.records
        if record.name == "metahub.runtime"
    )


@pytest.mark.anyio
async def test_public_authentication_precedes_realtime_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证批处理场景。

    定义：test_public_authentication_precedes_realtime_gate。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    gate_touched = False

    class TrackingLease:
        """验证批处理场景。

        定义：TrackingLease。
        固定租户隔离、权限拒绝、失败追踪和回退边界。
        """
        async def release(self) -> None:
            """验证批处理场景。

            定义：release。
            固定租户隔离、权限拒绝、失败追踪和回退边界。
            """
            return None

    class TrackingGate:
        """验证批处理场景。

        定义：TrackingGate。
        固定租户隔离、权限拒绝、失败追踪和回退边界。
        """
        async def enter_realtime(self) -> TrackingLease:
            """验证批处理场景。

            定义：enter_realtime。
            固定租户隔离、权限拒绝、失败追踪和回退边界。
            """
            nonlocal gate_touched
            gate_touched = True
            return TrackingLease()

    def reject_authentication(*_args: object, **_kwargs: object) -> None:
        """验证批处理场景。

        定义：reject_authentication。
        固定租户隔离、权限拒绝、失败追踪和回退边界。
        """
        # 权限以后端校验结果为准；失败时拒绝并保留脱敏审计事实。
        raise ApiError("gateway.auth_invalid")

    monkeypatch.setattr(openai_api, "_config", lambda _request: SimpleNamespace())
    monkeypatch.setattr(openai_api, "authenticate_virtual_key", reject_authentication)
    monkeypatch.setattr(
        openai_api, "get_batch_gateway_gate", lambda *_args, **_kwargs: TrackingGate()
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [(b"authorization", b"Bearer invalid-test-key")],
            "app": SimpleNamespace(state=SimpleNamespace()),
        }
    )

    with pytest.raises(ApiError) as captured:
        await openai_api._handle_gateway_request(
            request,
            endpoint="chat/completions",
            request_kind="chat",
        )
    assert captured.value.code == "gateway.auth_invalid"
    assert gate_touched is False


def _wire_request(headers: list[tuple[bytes, bytes]], body: bytes) -> Request:
    """验证批处理场景。

    定义：_wire_request。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    sent = False

    async def receive() -> dict[str, object]:
        """验证批处理场景。

        定义：receive。
        固定租户隔离、权限拒绝、失败追踪和回退边界。
        """
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/internal/batch/chat/completions",
            "query_string": b"",
            "headers": headers,
        },
        receive,
    )


@pytest.mark.anyio
async def test_internal_batch_wire_headers_are_exact_and_length_bound() -> None:
    """验证批处理场景。

    定义：test_internal_batch_wire_headers_are_exact_and_length_bound。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    body = b'{"grant":"grant_test_abcdefghijklmnopqrstuvwxyz0123456789"}'
    length = str(len(body)).encode("ascii")
    valid_headers = [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", length),
    ]
    valid = _wire_request(valid_headers, body)
    _validate_request_surface(valid)
    assert await _read_bounded_body(valid) == body

    invalid_content_types = (
        [],
        [(b"content-type", b"application/json"), (b"content-type", b"application/json")],
        [(b"content-type", b"text/plain")],
    )
    for content_type_headers in invalid_content_types:
        request = _wire_request(content_type_headers, body)
        with pytest.raises(ApiError) as captured:
            _validate_request_surface(request)
        assert captured.value.code == "batch.request_invalid"

    invalid_length_headers = (
        [
            (b"content-type", b"application/json"),
            (b"content-length", length),
            (b"content-length", length),
        ],
        [(b"content-type", b"application/json"), (b"content-length", b"+64")],
        [(b"content-type", b"application/json"), (b"content-length", b"64x")],
        [(b"content-type", b"application/json"), (b"content-length", b"1")],
    )
    for headers in invalid_length_headers:
        request = _wire_request(headers, body)
        with pytest.raises(ApiError) as captured:
            await _read_bounded_body(request)
        assert captured.value.code == "batch.request_invalid"


@pytest.mark.anyio
async def test_batch_gate_prioritizes_active_and_recent_realtime_requests() -> None:
    """验证批处理场景。

    定义：test_batch_gate_prioritizes_active_and_recent_realtime_requests。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    clock = [100.0]
    gate = BatchGatewayGate(
        batch_concurrency=1,
        realtime_cooldown_milliseconds=250,
        monotonic=lambda: clock[0],
    )
    batch = await gate.try_enter_batch()
    assert batch is not None
    assert await gate.try_enter_batch() is None

    realtime = await asyncio.wait_for(gate.enter_realtime(), timeout=0.1)
    assert await gate.try_enter_batch() is None
    await realtime.release()
    await batch.release()
    assert await gate.try_enter_batch() is None

    clock[0] += 0.251
    after_cooldown = await gate.try_enter_batch()
    assert after_cooldown is not None
    await after_cooldown.release()


@pytest.mark.anyio
async def test_batch_limit_rejection_does_not_mutate_any_counter() -> None:
    """验证批处理场景。

    定义：test_batch_limit_rejection_does_not_mutate_any_counter。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    store = InMemoryRateLimitStore()
    first = (
        BatchRateLimitDimension("rpm", "rpm", 1, 2, 90),
        BatchRateLimitDimension("tpm", "tpm", 3, 3, 90),
        BatchRateLimitDimension("concurrency", "concurrency", 1, 1, 120),
    )
    assert (await store.try_acquire_batch_limits(first)).allowed is True
    await store.release_concurrency("concurrency")
    before = copy.deepcopy(store._values)

    rejected = await store.try_acquire_batch_limits(
        (
            BatchRateLimitDimension("rpm", "rpm", 1, 2, 90),
            BatchRateLimitDimension("tpm", "tpm", 1, 3, 90),
            BatchRateLimitDimension("concurrency", "concurrency", 1, 1, 120),
        )
    )
    assert rejected.allowed is False
    assert rejected.limit_type == "tpm"
    assert store._values == before


@pytest.mark.anyio
async def test_batch_rate_limit_uses_frozen_token_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证批处理场景。

    定义：test_batch_rate_limit_uses_frozen_token_estimate。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    store = InMemoryRateLimitStore()
    rule = RateLimitRule(
        tenant_id="tenant_prd074",
        virtual_key_id="vkey_prd074",
        rpm_limit=100,
        tpm_limit=1_000,
        concurrency_limit=None,
    )
    monkeypatch.setattr(
        rate_limit_service,
        "load_virtual_key_rate_limit_rule",
        lambda *_args: rule,
    )
    monkeypatch.setattr(
        rate_limit_service,
        "get_rate_limit_store",
        lambda *_args: store,
    )

    def reject_body_estimation(*_args: object, **_kwargs: object) -> int:
        """验证批处理场景。

        定义：reject_body_estimation。
        固定租户隔离、权限拒绝、失败追踪和回退边界。
        """
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise AssertionError("Batch TPM must not estimate decrypted request content")

    monkeypatch.setattr(rate_limit_service, "estimate_request_tokens", reject_body_estimation)
    lease = await rate_limit_service.enforce_virtual_key_rate_limit(
        SimpleNamespace(batch_realtime_reserve_percent=25),
        SimpleNamespace(),
        _auth(),
        SimpleNamespace(),
        {"messages": [{"role": "user", "content": "not inspected"}]},
        endpoint="chat/completions",
        request_kind="chat",
        request_id="req_prd074_frozen_tpm",
        trace_id="trc_prd074_frozen_tpm",
        batch_lane=True,
        batch_token_estimate=TokenEstimate(
            input_tokens=272,
            output_tokens=256,
            truncated=False,
        ),
    )
    tpm_values = [
        value[0] for key, value in store._values.items() if ":tpm:" in key
    ]
    assert tpm_values == [528]
    assert lease is not None
    await lease.release()


def test_batch_routing_uses_frozen_token_estimate_before_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证批处理场景。

    定义：test_batch_routing_uses_frozen_token_estimate_before_payload。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    frozen = TokenEstimate(input_tokens=272, output_tokens=256, truncated=False)

    def reject_body_estimation(*_args: object, **_kwargs: object) -> TokenEstimate:
        """验证批处理场景。

        定义：reject_body_estimation。
        固定租户隔离、权限拒绝、失败追踪和回退边界。
        """
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise AssertionError("Batch routing must not estimate decrypted request content")

    monkeypatch.setattr(routing_models, "estimate_request_tokens", reject_body_estimation)
    assert routing_models._routing_request_token_estimate(
        "chat",
        {"messages": [{"role": "user", "content": "not inspected"}]},
        trusted_token_estimate=frozen,
    ) is frozen


def _canonical_route_plan() -> str:
    """验证批处理场景。

    定义：_canonical_route_plan。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    payload = {
        "schema_version": 1,
        "model_alias_id": "model_alias_prd074",
        "environment_id": "environment_prd074",
        "virtual_key_id": "virtual_key_prd074",
        "application_id": None,
        "fallback_policy_version": None,
        "candidates": [
            {
                "role": "default",
                "model_id": "model_prd074",
                "provider_id": "provider_prd074",
                "input_price_per_1k": "0.000000000000",
                "output_price_per_1k": "0.000000000000",
                "cache_price_per_1k": "0.000000000000",
                "currency": "CNY",
                "price_source": "price_plan",
                "price_version": "price_v1",
            }
        ],
    }
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def test_route_plan_and_attempt_payload_are_canonical_exact_sets() -> None:
    """验证批处理场景。

    定义：test_route_plan_and_attempt_payload_are_canonical_exact_sets。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    value = _canonical_route_plan()
    route, digest = _validated_route_plan(
        value,
        model_alias_id="model_alias_prd074",
        environment_id="environment_prd074",
        virtual_key_id="virtual_key_prd074",
        application_id=None,
    )
    assert route.default.model_id == "model_prd074"
    assert digest == hashlib.sha256(value.encode("utf-8")).hexdigest()

    expanded = json.loads(value)
    expanded["prompt"] = "must-not-be-accepted"
    with pytest.raises(ApiError) as captured:
        _validated_route_plan(
            json.dumps(expanded, sort_keys=True, separators=(",", ":")),
            model_alias_id="model_alias_prd074",
            environment_id="environment_prd074",
            virtual_key_id="virtual_key_prd074",
            application_id=None,
        )
    assert captured.value.code == "batch.execution_invalid"

    messages = _validated_request_payload(
        json.dumps(
            {
                "schema_version": 1,
                "messages": [
                    {"role": "system", "content": "fixed template"},
                    {"role": "user", "content": "untrusted file"},
                ],
            }
        ).encode("utf-8")
    )
    assert [item["role"] for item in messages] == ["system", "user"]
    with pytest.raises(ApiError) as duplicate:
        _validated_request_payload(b'{"schema_version":1,"schema_version":1,"messages":[]}')
    assert duplicate.value.code == "batch.worker_unavailable"


def test_batch_usage_spool_v6_is_exact_and_excludes_sensitive_content() -> None:
    """验证批处理场景。

    定义：test_batch_usage_spool_v6_is_exact_and_excludes_sensitive_content。
    固定租户隔离、权限拒绝、失败追踪和回退边界。
    """
    secret_prompt = "prd074-sensitive-prompt"
    secret_response = "prd074-sensitive-response"
    intent = _prepare_usage_ledger_intent(
        {
            "prompt_tokens": 2,
            "completion_tokens": 1,
            "total_tokens": 3,
            "raw_usage": {"prompt": secret_prompt, "response": secret_response},
            "usage_source": "upstream",
        }
    )
    snapshot_id = "prs_" + "f" * 32
    evidence = PriceSnapshotEvidence(
        price_snapshot_id=snapshot_id,
        model_id="model_prd074",
        provider_id="provider_prd074",
        provider_code="provider",
        currency="CNY",
        input_price_per_1k="0.000000",
        output_price_per_1k="0.000000",
        price_schema_version="price_v1",
        source="model_catalog",
    )
    intent.captured_estimate = CostEstimate(
        amount=Decimal("0.000000"),
        currency="CNY",
        price_schema_version="price_v1",
        cost_cny=Decimal("0.000000"),
        cost_usd=Decimal("0.000000"),
        price_snapshot_id=snapshot_id,
        price_snapshot_evidence=evidence,
    )
    context = RequestLogContext(
        tenant_id="tenant_prd074",
        request_id="req_prd074_spool",
        trace_id="trc_prd074_spool",
        endpoint=secret_prompt,
        output_safety_scan_id="oss_" + "1" * 32,
        output_safety_scan_status="completed",
        output_safety_decision="clean",
        billing_disposition="not_required",
        batch_job_id="bjob_" + "2" * 32,
        batch_task_id="btsk_" + "3" * 32,
        batch_attempt_id="batm_" + "4" * 32,
        batch_route_plan_hash="5" * 64,
    )

    payload = _serialize_usage_spool_record(context, intent)
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    assert payload["schema_version"] == 8
    assert payload["context"]["batch_allocation_settlement_cny"] == "0.000000"
    assert "endpoint" not in payload["context"]
    assert secret_prompt not in serialized
    assert secret_response not in serialized

    restored, restored_intent = _deserialize_usage_spool_record(payload)
    assert restored.batch_job_id == context.batch_job_id
    assert restored.batch_allocation_settlement_cny == "0.000000"
    assert restored_intent.raw_usage_value == {}

    expanded = copy.deepcopy(payload)
    expanded["context"]["prompt"] = secret_prompt
    with pytest.raises(ValueError, match="finops_spool_payload_invalid"):
        _deserialize_usage_spool_record(expanded)

    quota_payload = _serialize_usage_spool_record(
        replace(context, quota_reservation_outstanding_cny="0.001000"),
        intent,
    )
    assert quota_payload["schema_version"] == 8
    quota_restored, _quota_intent = _deserialize_usage_spool_record(quota_payload)
    assert quota_restored.quota_reservation_outstanding_cny == "0.001000"

    missing_scan = copy.deepcopy(quota_payload)
    for field in (
        "output_safety_scan_id",
        "output_safety_scan_status",
        "output_safety_decision",
        "billing_disposition",
    ):
        missing_scan["context"][field] = None
    with pytest.raises(ValueError, match="finops_spool_output_safety_missing"):
        _deserialize_usage_spool_record(missing_scan)

    drifted_settlement = copy.deepcopy(quota_payload)
    drifted_settlement["context"]["batch_allocation_settlement_cny"] = "0.000001"
    with pytest.raises(ValueError, match="finops_spool_payload_invalid"):
        _deserialize_usage_spool_record(drifted_settlement)
