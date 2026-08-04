"""PRD-069 统一错误、Webhook 与敏感请求链路的共享运行时合同测试。"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient
from metahub_shared.runtime.errors import ERROR_SPECS, ApiError
from metahub_shared.runtime.fastapi_app import create_service_app
from metahub_shared.runtime.logging import JsonFormatter
from metahub_shared.runtime.webhooks import (
    WEBHOOK_EVENT_TYPES,
    WEBHOOK_SUBSCRIBABLE_EVENT_TYPES,
    build_webhook_event,
)

# 固定领域 ID 和完成时间只验证共享 wire，不对应真实租户、用户或生产安全事件。
_RESULT_ID = "irr_" + "a" * 32
_EVENT_ID = "irevt_" + "b" * 32
_COMPLETED_AT = "2026-08-03T12:00:00Z"


def _intent_payload(**overrides: object) -> dict[str, object]:
    """构造不含正文、命中片段或输入 hash 的冻结异步检测摘要。"""

    payload: dict[str, object] = {
        "intent_risk_result_id": _RESULT_ID,
        "source_request_id": "req_prd069_gateway",
        "source_kind": "gateway",
        "classification_mode": "async_light",
        "risk_type": "jailbreak",
        "risk_score": 9600,
        "risk_score_scale": 10_000,
        "action": "observe",
        "reason_code": "explicit_instruction_override",
        "detector_version": "intent-rule-engine-v1",
        "ruleset_version": "intent-rules-2026-08-03-v1",
        "input_truncated": False,
        "completed_at": _COMPLETED_AT,
    }
    payload.update(overrides)
    return payload


def _build_intent_event(
    payload: dict[str, object] | None = None,
    **overrides: object,
):
    """构造 tenant、actor、result 和原始调用链均完整的持久安全事件。"""

    values: dict[str, object] = {
        "tenant_id": "tenant_default",
        "event_type": "security.intent_detected",
        "event_id": _EVENT_ID,
        "timestamp": datetime(2026, 8, 3, 12, 0, tzinfo=UTC),
        "actor_id": "vk_prd069_gateway",
        "object_id": _RESULT_ID,
        "trace_id": "trc_prd069_gateway",
        "source_request_id": "req_prd069_gateway",
        "payload": _intent_payload() if payload is None else payload,
    }
    values.update(overrides)
    return build_webhook_event(**values)


def test_intent_error_directory_is_complete_and_safe() -> None:
    """公开目录冻结 Control/Gateway 状态、HTTP 语义和可重试性且不包含敏感正文。"""

    expected = {
        "intent_risk.media_type_unsupported": (415, False, "intent_risk"),
        "intent_risk.payload_too_large": (413, False, "intent_risk"),
        "intent_risk.payload_invalid": (400, False, "intent_risk"),
        "intent_risk.text_invalid": (422, False, "intent_risk"),
        "intent_risk.classifier_unavailable": (503, True, "intent_risk"),
        "intent_risk.result_store_unavailable": (503, True, "intent_risk"),
        "intent_risk.result_integrity_failed": (503, False, "intent_risk"),
        "gateway.intent_blocked": (403, False, "gateway"),
        "gateway.intent_classifier_unavailable": (503, True, "gateway"),
        "gateway.intent_result_store_unavailable": (503, True, "gateway"),
    }

    assert {
        code: (
            ERROR_SPECS[code].http_status,
            ERROR_SPECS[code].retryable,
            ERROR_SPECS[code].owner,
        )
        for code in expected
    } == expected
    rendered = " ".join(
        f"{ERROR_SPECS[code].message} {ERROR_SPECS[code].suggestion}" for code in expected
    ).lower()
    assert all(term not in rendered for term in ("prompt", "response", "token", "secret"))


@pytest.mark.parametrize(
    "payload",
    [
        _intent_payload(),
        _intent_payload(
            source_kind="gateway",
            classification_mode="sync_strong",
            action="block",
        ),
        _intent_payload(
            source_kind="manual",
            classification_mode="manual_sync",
            action="review",
        ),
    ],
)
def test_intent_detected_is_subscribable_with_exact_safe_payload(
    payload: dict[str, object],
) -> None:
    """异步观察、同步阻断和手工复核共用同一精确安全事件合同。"""

    event = _build_intent_event(payload)

    assert "security.intent_detected" in WEBHOOK_EVENT_TYPES
    assert "security.intent_detected" in WEBHOOK_SUBSCRIBABLE_EVENT_TYPES
    assert event.event_id == _EVENT_ID
    assert event.object_id == _RESULT_ID
    assert event.payload == payload
    assert set(event.payload) == {
        "intent_risk_result_id",
        "source_request_id",
        "source_kind",
        "classification_mode",
        "risk_type",
        "risk_score",
        "risk_score_scale",
        "action",
        "reason_code",
        "detector_version",
        "ruleset_version",
        "input_truncated",
        "completed_at",
    }


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update({"prompt": "sensitive-canary"}),
        lambda value: value.pop("reason_code"),
        lambda value: value.update({"intent_risk_result_id": "irr_" + "A" * 32}),
        lambda value: value.update({"source_kind": []}),
        lambda value: value.update({"classification_mode": "sync_strong"}),
        lambda value: value.update({"risk_type": "none"}),
        lambda value: value.update({"risk_score": True}),
        lambda value: value.update({"risk_score": 9500}),
        lambda value: value.update({"risk_score_scale": 100}),
        lambda value: value.update({"reason_code": "free_form_reason"}),
        lambda value: value.update({"reason_code": []}),
        lambda value: value.update({"detector_version": "intent-rule-engine-v2"}),
        lambda value: value.update({"ruleset_version": "intent-rules-v2"}),
        lambda value: value.update({"input_truncated": 0}),
        lambda value: value.update({"completed_at": "2026-08-03 12:00:00"}),
    ],
)
def test_intent_detected_rejects_payload_drift(mutation) -> None:
    """额外/缺失字段、组合漂移、自由原因、未知版本和宽松类型均在签名前拒绝。"""

    payload = _intent_payload()
    mutation(payload)

    with pytest.raises(ValueError, match=r"webhook\.payload_(?:invalid|sensitive)"):
        _build_intent_event(payload)


@pytest.mark.parametrize(
    "overrides",
    [
        {"event_id": "evt_" + "b" * 32},
        {"tenant_id": "tenant with space"},
        {"actor_id": None},
        {"object_id": "irr_" + "c" * 32},
        {"trace_id": None},
        {"source_request_id": "req_other"},
        {"timestamp": datetime(2026, 8, 3, 12, 0, 1, tzinfo=UTC)},
    ],
)
def test_intent_detected_rejects_envelope_owner_drift(
    overrides: dict[str, object],
) -> None:
    """source event 的租户、actor、result、request、trace 和完成时刻必须逐字绑定。"""

    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        _build_intent_event(**overrides)


def _build_request_context_probe():
    """构造只回显内部链路元数据的隔离应用，不引入 Control 业务路由依赖。"""

    app = create_service_app("control-plane-api", "PRD-069 Request Context Test")

    @app.post("/security/classify-intent")
    async def classify_probe(request: Request, mode: str = "success") -> dict[str, str]:
        """读取但不返回敏感正文，并允许测试统一业务错误分支。"""

        await request.body()
        # 故障分支必须走统一 ApiError，才能同时验证错误响应也丢弃外部链路 Header。
        if mode == "error":
            # 固定错误对象不携带正文或底层异常，日志扫描只应看到内部 request_id。
            raise ApiError("intent_risk.classifier_unavailable")
        return {
            "request_id": request.state.request_id,
            "trace_id": request.state.trace_id,
        }

    @app.get("/security/classify-intent")
    async def classify_get_probe(request: Request) -> dict[str, str]:
        """证明敏感链路覆盖只作用于 POST，不改变同路径其他方法的公共中间件语义。"""

        return {
            "request_id": request.state.request_id,
            "trace_id": request.state.trace_id,
        }

    return app


@pytest.mark.anyio
async def test_classify_post_discards_external_chain_ids_on_success_and_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """正文可复制到 Header 时，成功和 503 都只能回显内部 req_/trc_ 且禁止缓存。"""

    app = _build_request_context_probe()
    marker = "SensitiveIntentHeaderCanary2026"
    caplog.set_level(logging.INFO, logger="metahub.runtime")
    headers = {"X-Request-ID": marker, "X-Trace-ID": marker}
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://control") as client:
        success = await client.post(
            "/security/classify-intent",
            headers=headers,
            json={"text": marker},
        )
        failure = await client.post(
            "/security/classify-intent?mode=error",
            headers=headers,
            json={"text": marker},
        )
        get_response = await client.get(
            "/security/classify-intent",
            headers={
                "X-Request-ID": "ordinary-read-id",
                "X-Trace-ID": "ordinary-read-trace",
            },
        )

    assert success.status_code == 200
    assert failure.status_code == 503
    for response in (success, failure):
        assert response.headers["x-request-id"].startswith("req_")
        assert response.headers["x-trace-id"].startswith("trc_")
        assert response.headers["cache-control"] == "no-store"
        assert marker not in response.text
    # 精确边界不能改变 GET：普通只读请求仍保留经过安全字符归一的外部链路 ID。
    assert get_response.headers["x-request-id"] == "ordinary-read-id"
    assert get_response.headers["x-trace-id"] == "ordinary-read-trace"
    rendered = "\n".join(
        JsonFormatter().format(record)
        for record in caplog.records
        if record.name == "metahub.runtime"
    )
    assert marker not in rendered
