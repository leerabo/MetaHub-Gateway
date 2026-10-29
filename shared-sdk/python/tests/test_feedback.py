"""PRD-049 shared Feedback 合同的纯函数、wire、错误和 Webhook 单元测试。

这些测试不依赖 Control/Gateway 框架，专门锁定双入口必须共用的类型、指纹、资格、整数
统计和安全事件语义；任何路径都不构造 Prompt、Response、Token 或 Key 明文。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from metahub_shared.feedback import (
    FeedbackEventRecord,
    evaluate_feedback_eligibility,
    feedback_event_to_webhook,
    normalize_feedback_payload,
    parse_feedback_json_bytes,
    resolve_feedback_operation_request_id,
    round_ratio_half_up,
)
from metahub_shared.runtime.error_diagnosis import diagnose_error
from metahub_shared.runtime.errors import ERROR_SPECS, ApiError
from metahub_shared.runtime.i18n import error_label
from metahub_shared.runtime.request_id import resolve_request_id, resolve_trace_id
from metahub_shared.runtime.webhooks import (
    WEBHOOK_SUBSCRIBABLE_EVENT_TYPES,
    build_webhook_event,
)

# 黄金 canonical JSON 和 SHA-256 固定跨入口 v1 指纹，未来字段变化必须另升 schema 版本。
GOLDEN_FINGERPRINT = "1baf6cd515497feffb6ce568f0574a66d3677ef17004bb95c0b3ea56131a2b6d"
FEEDBACK_ERROR_STATUS = {
    "feedback.request_not_found": 404,
    "feedback.request_not_feedbackable": 409,
    "feedback.already_exists": 409,
    "feedback.request_conflict": 409,
    "feedback.input_invalid": 422,
    "feedback.query_invalid": 422,
    "feedback.unavailable": 503,
    "feedback.record_failed": 500,
}


def test_feedback_normalization_orders_tags_and_matches_golden_fingerprint() -> None:
    """乱序标签必须按固定白名单排序，并得到设计冻结的 v1 黄金指纹。"""

    intent = normalize_feedback_payload(
        {
            "request_id": "req_source_call",
            "rating": 5,
            "accepted": True,
            "tags": ["helpful", "accurate"],
        }
    )
    assert intent.tags == ("accurate", "helpful")
    assert intent.request_fingerprint == GOLDEN_FINGERPRINT


@pytest.mark.parametrize(
    "payload",
    [
        {"request_id": "req", "rating": True, "accepted": True, "tags": []},
        {"request_id": "req", "rating": 0, "accepted": True, "tags": []},
        {"request_id": "req", "rating": 6, "accepted": True, "tags": []},
        {"request_id": "req", "rating": 4, "accepted": 1, "tags": []},
        {"request_id": "req", "rating": 4, "accepted": True, "tags": ["unknown"]},
        {"request_id": "req", "rating": 4, "accepted": True, "tags": ["clear", "clear"]},
        {
            "request_id": "req",
            "rating": 4,
            "accepted": True,
            "tags": ["accurate", "inaccurate"],
        },
        {"request_id": "req\nlog", "rating": 4, "accepted": True, "tags": []},
        {"request_id": "req", "rating": 4, "accepted": True, "tags": [], "tenant_id": "x"},
    ],
)
def test_feedback_normalization_rejects_type_tag_scope_and_identifier_drift(
    payload: dict[str, object],
) -> None:
    """bool 冒充整数、越界、标签冲突、控制字符和 tenant 注入均统一 422。"""

    with pytest.raises(ApiError, match="Feedback payload is invalid") as captured:
        normalize_feedback_payload(payload)
    assert captured.value.code == "feedback.input_invalid"


def test_wire_parser_accepts_exact_json_and_rejects_ambiguous_encodings() -> None:
    """wire 只接受 UTF-8 JSON，并拒绝重复键、NaN、控制字符和错误媒体类型。"""

    body = json.dumps(
        {"request_id": "req_wire", "rating": 3, "accepted": False, "tags": []},
        separators=(",", ":"),
    ).encode()
    parsed = parse_feedback_json_bytes(
        body,
        content_type='Application/JSON; charset="utf-8"',
        content_length=str(len(body)),
    )
    assert (parsed.source_request_id, parsed.rating, parsed.accepted) == ("req_wire", 3, False)

    invalid_bodies = (
        b'{"request_id":"a","request_id":"b","rating":3,"accepted":true,"tags":[]}',
        b'{"request_id":"a","rating":NaN,"accepted":true,"tags":[]}',
        b'{"request_id":"a\\u0000","rating":3,"accepted":true,"tags":[]}',
    )
    for invalid in invalid_bodies:
        with pytest.raises(ApiError) as captured:
            parse_feedback_json_bytes(
                invalid,
                content_type="application/json",
                content_length=str(len(invalid)),
            )
        assert captured.value.code == "feedback.input_invalid"

    with pytest.raises(ApiError):
        parse_feedback_json_bytes(
            body,
            content_type="application/problem+json",
            content_length=str(len(body)),
        )
    with pytest.raises(ApiError):
        parse_feedback_json_bytes(
            body,
            content_type="application/json; charset=gbk",
            content_length=str(len(body)),
        )


def test_raw_operation_request_id_detects_duplicate_rewrite_and_invalid_ascii() -> None:
    """原始 X-Request-ID 缺省可用 state，唯一同值通过，重复/改写/非 ASCII 均拒绝。"""

    assert resolve_feedback_operation_request_id([], "op_generated") == "op_generated"
    assert (
        resolve_feedback_operation_request_id(
            [(b"x-request-id", b"op_exact")], "op_exact"
        )
        == "op_exact"
    )
    for headers, state in (
        ([(b"x-request-id", b"a"), (b"X-Request-ID", b"a")], "a"),
        ([(b"x-request-id", b"a?")], "a_"),
        ([(b"x-request-id", b"\xff")], "generated"),
    ):
        with pytest.raises(ApiError) as captured:
            resolve_feedback_operation_request_id(headers, state)
        assert captured.value.code == "feedback.input_invalid"


def test_runtime_identifiers_replace_bearer_credential_and_significant_prefix() -> None:
    """完整 Bearer、装饰前缀和中段 Token 片段都必须在日志前替换为内部链路 ID。"""

    credential = "mhv_prd049_sensitive_credential_0123456789"
    authorization = f"Bearer {credential}"
    request_id = resolve_request_id(credential, authorization=authorization)
    trace_id = resolve_trace_id(authorization, request_id, authorization=authorization)
    prefix_id = resolve_request_id(credential[:24], authorization=authorization)
    decorated_prefix = f"prefix_{credential[:24]}"
    decorated_middle = f"trace_{credential[8:32]}_suffix"
    decorated_request_id = resolve_request_id(decorated_prefix, authorization=authorization)
    decorated_trace_id = resolve_trace_id(
        decorated_middle,
        "feedback_safe_operation",
        authorization=authorization,
    )
    malformed_authorizations = (
        f"Bearer\t{credential}",
        credential,
        f"Token {credential}",
    )
    assert request_id.startswith("req_") and credential not in request_id
    assert trace_id.startswith("trc_") and credential not in trace_id
    assert prefix_id.startswith("req_") and credential[:24] not in prefix_id
    assert decorated_request_id.startswith("req_") and credential[:24] not in decorated_request_id
    assert decorated_trace_id.startswith("trc_") and credential[8:32] not in decorated_trace_id
    # 认证格式错误仍会生成 401 响应与访问日志，脱敏不能依赖后端是否接受 scheme。
    assert all(
        resolve_request_id(
            decorated_prefix,
            authorization=malformed,
        ).startswith("req_")
        for malformed in malformed_authorizations
    )
    assert resolve_request_id("feedback_safe_operation", authorization=authorization) == (
        "feedback_safe_operation"
    )


def test_feedback_eligibility_freezes_legacy_final_attempt_and_reason_priority() -> None:
    """详情与 POST 共用 legacy/final-attempt 状态机，已有反馈和流状态优先级保持稳定。"""

    legacy = evaluate_feedback_eligibility(
        request_status="success",
        stream_session_id=None,
        stream_status=None,
        routing_attempt_count=0,
        final_routing_attempt_id=None,
        request_model_id="model_legacy",
        request_provider_id="provider_legacy",
        final_attempt_id=None,
        final_attempt_status=None,
        final_attempt_model_id=None,
        final_attempt_provider_id=None,
        has_feedback=False,
    )
    assert legacy.eligible is True
    assert (legacy.model_id, legacy.provider_id) == ("model_legacy", "provider_legacy")

    final = evaluate_feedback_eligibility(
        request_status="success",
        stream_session_id=None,
        stream_status=None,
        routing_attempt_count=2,
        final_routing_attempt_id="attempt_2",
        request_model_id="model_final",
        request_provider_id="provider_final",
        final_attempt_id="attempt_2",
        final_attempt_status="succeeded",
        final_attempt_model_id="model_final",
        final_attempt_provider_id="provider_final",
        has_feedback=False,
    )
    assert final.eligible is True
    assert final.model_id == "model_final"

    already = evaluate_feedback_eligibility(
        request_status="failed",
        stream_session_id="stream",
        stream_status="timed_out",
        routing_attempt_count=0,
        final_routing_attempt_id=None,
        request_model_id=None,
        request_provider_id=None,
        final_attempt_id=None,
        final_attempt_status=None,
        final_attempt_model_id=None,
        final_attempt_provider_id=None,
        has_feedback=True,
    )
    assert (already.eligible, already.reason) == (False, "already_recorded")

    incomplete = evaluate_feedback_eligibility(
        request_status="success",
        stream_session_id=None,
        stream_status=None,
        routing_attempt_count=1,
        final_routing_attempt_id=None,
        request_model_id="model",
        request_provider_id="provider",
        final_attempt_id=None,
        final_attempt_status=None,
        final_attempt_model_id=None,
        final_attempt_provider_id=None,
        has_feedback=False,
    )
    assert (incomplete.eligible, incomplete.reason) == (False, "attribution_unavailable")


def test_half_up_ratio_handles_zero_ties_and_arbitrary_size_integers() -> None:
    """平均分/采纳率使用任意精度整数 half-up，零样本固定为零且不受 64 位乘法限制。"""

    assert round_ratio_half_up(0, 0, 10_000) == 0
    assert round_ratio_half_up(17, 4, 1_000) == 4_250
    assert round_ratio_half_up(3, 8, 10_000) == 3_750
    huge = 9_223_372_036_854_775_807
    assert round_ratio_half_up(huge, huge, 10_000) == 10_000


def test_feedback_created_webhook_uses_exact_persistent_payload_and_envelope() -> None:
    """持久 fbe_、RequestFeedback、actor、trace 和七字段 payload 必须精确绑定。"""

    payload = {
        "request_feedback_id": "rfb_" + "1" * 32,
        "request_id": "req_feedback_event",
        "model_id": "model_feedback_event",
        "actor_type": "session_user",
        "rating": 5,
        "accepted": True,
        "tags": ["accurate", "helpful"],
    }
    record = FeedbackEventRecord(
        feedback_event_id="fbe_" + "2" * 32,
        tenant_id="tenant_feedback",
        request_feedback_id=payload["request_feedback_id"],
        source_request_id=payload["request_id"],
        event_type="feedback.created",
        actor_type="session_user",
        actor_id="user_feedback",
        operation_request_id="op_feedback",
        trace_id="trace_feedback",
        payload=payload,
        created_at=datetime(2026, 7, 20, 1, 0, tzinfo=UTC),
    )
    event = feedback_event_to_webhook(record)
    assert "feedback.created" in WEBHOOK_SUBSCRIBABLE_EVENT_TYPES
    assert event.event_id == record.feedback_event_id
    assert event.object_id == record.request_feedback_id
    assert event.payload == payload


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        (lambda value: value.__setitem__("prompt", "sensitive"), "webhook.payload_sensitive"),
        (lambda value: value.__setitem__("rating", True), "webhook.payload_invalid"),
        (lambda value: value.__setitem__("accepted", 1), "webhook.payload_invalid"),
        (
            lambda value: value.__setitem__("tags", ["helpful", "accurate"]),
            "webhook.payload_invalid",
        ),
        (lambda value: value.__setitem__("actor_type", "admin"), "webhook.payload_invalid"),
    ],
)
def test_feedback_created_webhook_rejects_extra_type_and_order_drift(
    mutation,
    expected_error: str,
) -> None:
    """敏感额外字段、强类型漂移、标签乱序和未知 actor 必须在签名前整体拒绝。"""

    payload = {
        "request_feedback_id": "rfb_" + "3" * 32,
        "request_id": "req_feedback_invalid",
        "model_id": "model_feedback_invalid",
        "actor_type": "virtual_key",
        "rating": 4,
        "accepted": False,
        "tags": ["accurate", "helpful"],
    }
    mutation(payload)
    with pytest.raises(ValueError, match=expected_error):
        build_webhook_event(
            tenant_id="tenant_feedback",
            event_type="feedback.created",
            payload=payload,
            event_id="fbe_" + "4" * 32,
            actor_id="virtual_key_feedback",
            object_id="rfb_" + "3" * 32,
            trace_id="trace_feedback",
        )


def test_feedback_errors_have_status_diagnosis_and_bilingual_labels() -> None:
    """八个 Feedback 错误必须进入统一目录，并提供固定状态、诊断及中英文 label。"""

    for code, status in FEEDBACK_ERROR_STATUS.items():
        assert ERROR_SPECS[code].http_status == status
        diagnosis = diagnose_error(code, http_status=status)
        assert diagnosis.matched_by == "error_code"
        assert diagnosis.rule_id != "unknown_error"
        assert error_label(code, "zh-CN") != code
        assert error_label(code, "en-US") != code
