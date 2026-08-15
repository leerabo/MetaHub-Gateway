"""PRD-081 标准安全事件、JSON/CEF wire 与 Webhook 目录合同。"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

import pytest
from metahub_shared.runtime.security_events import (
    SECURITY_EVENT_ACTOR,
    SECURITY_EVENT_TYPE,
    normalize_webhook_payload_format,
    security_event_cef_bytes,
    validate_security_event_envelope,
    validate_security_event_payload,
    webhook_test_cef_bytes,
)
from metahub_shared.runtime.webhooks import (
    WEBHOOK_EVENT_TYPES,
    WEBHOOK_SUBSCRIBABLE_EVENT_TYPES,
    build_webhook_event,
)

EVENT_ID = "sevt_" + "a" * 32
TENANT_ID = "tenant_prd081"
WINDOW_END = datetime(2026, 8, 15, 1, 2, 5, tzinfo=UTC)


def _payload(
    *,
    source_event_type: str = "security.dlp_matched",
    action: str = "block",
) -> dict[str, object]:
    contracts = {
        ("security.dlp_matched", "block"): (
            "data_loss_prevention",
            "high",
            8,
            "blocked",
            "dlp_rule",
        ),
        ("security.session_high_risk", "freeze_session"): (
            "session_risk",
            "critical",
            10,
            "frozen",
            "session_risk_score",
        ),
        ("security.provider_key_anomaly", "decrypt_failed"): (
            "provider_credential",
            "critical",
            9,
            "rejected",
            "provider",
        ),
    }
    category, severity, score, outcome, resource_type = contracts[(source_event_type, action)]
    return {
        "schema_version": 1,
        "security_event_id": EVENT_ID,
        "status": "aggregated",
        "category": category,
        "source_event_type": source_event_type,
        "severity": severity,
        "severity_score": score,
        "action": action,
        "outcome": outcome,
        "resource_type": resource_type,
        "resource_id": "resource_prd081",
        "event_count": 3,
        "source_event_count": 2,
        "first_seen_at": "2026-08-15T01:02:01Z",
        "last_seen_at": "2026-08-15T01:02:04Z",
        "window_started_at": "2026-08-15T01:02:00Z",
        "window_ended_at": "2026-08-15T01:02:05Z",
        "first_request_id": "req_prd081_first",
        "last_request_id": "req_prd081_last",
        "created_at": "2026-08-15T01:02:01Z",
        "updated_at": "2026-08-15T01:02:04Z",
    }


def _envelope(**payload_options: str) -> dict[str, object]:
    return {
        "event_id": EVENT_ID,
        "event_type": SECURITY_EVENT_TYPE,
        "timestamp": "2026-08-15T01:02:05Z",
        "tenant_id": TENANT_ID,
        "actor_id": SECURITY_EVENT_ACTOR,
        "object_id": EVENT_ID,
        "trace_id": "trc_prd081_last",
        "payload": _payload(**payload_options),
    }


def test_security_event_is_subscribable_and_format_is_fail_closed() -> None:
    """标准事件进入现有目录，format 只接受规范化后的 JSON/CEF 枚举。"""

    assert SECURITY_EVENT_TYPE in WEBHOOK_EVENT_TYPES
    assert SECURITY_EVENT_TYPE in WEBHOOK_SUBSCRIBABLE_EVENT_TYPES
    assert normalize_webhook_payload_format(" JSON ") == "json"
    assert normalize_webhook_payload_format("CeF") == "cef"
    for value in (None, "", "xml", True, 1):
        with pytest.raises(ValueError, match="webhook.format_invalid"):
            normalize_webhook_payload_format(value)


@pytest.mark.parametrize(
    ("source_event_type", "action"),
    (
        ("security.dlp_matched", "block"),
        ("security.session_high_risk", "freeze_session"),
        ("security.provider_key_anomaly", "decrypt_failed"),
    ),
)
def test_security_event_contracts_bind_payload_and_envelope(
    source_event_type: str,
    action: str,
) -> None:
    """三类来源都必须绑定稳定聚合身份、固定 actor、窗口与最后请求。"""

    envelope = _envelope(source_event_type=source_event_type, action=action)
    assert validate_security_event_payload(envelope["payload"]) == envelope["payload"]
    assert (
        validate_security_event_envelope(
            envelope,
            source_request_id="req_prd081_last",
        )
        == envelope
    )
    event = build_webhook_event(
        tenant_id=TENANT_ID,
        event_type=SECURITY_EVENT_TYPE,
        payload=deepcopy(envelope["payload"]),
        actor_id=SECURITY_EVENT_ACTOR,
        object_id=EVENT_ID,
        trace_id="trc_prd081_last",
        source_request_id="req_prd081_last",
        event_id=EVENT_ID,
        timestamp=WINDOW_END,
    )
    assert event.to_payload() == envelope


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("schema_version", True),
        ("event_count", True),
        ("source_event_count", 0),
        ("severity_score", 8.0),
        ("window_ended_at", "2026-08-15T01:02:06Z"),
        ("last_seen_at", "2026-08-15T01:02:05Z"),
        ("updated_at", "2026-08-15T01:02:00Z"),
    ),
)
def test_security_event_payload_rejects_type_time_and_count_drift(
    field: str,
    value: object,
) -> None:
    """bool 冒充整数、非 5 秒窗口与不一致时序必须失败关闭。"""

    payload = _payload()
    payload[field] = value
    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        validate_security_event_payload(payload)


def test_security_event_rejects_unknown_fields_and_envelope_drift() -> None:
    """聚合正文与信封均不允许自由字段、随机 actor 或错误来源请求。"""

    payload = _payload()
    payload["provider_key"] = "must-never-be-accepted"
    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        validate_security_event_payload(payload)

    envelope = _envelope()
    envelope["actor_id"] = "usr_untrusted"
    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        validate_security_event_envelope(envelope)

    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        validate_security_event_envelope(
            _envelope(),
            source_request_id="req_prd081_other",
        )


def test_cef_wire_is_deterministic_single_line_and_fixed_order() -> None:
    """CEF 对实际安全信封做完整校验，并按冻结字段顺序生成单行 UTF-8。"""

    body = security_event_cef_bytes(_envelope())
    assert body.decode("utf-8") == (
        "CEF:0|MetaHub|AI Gateway|1|security.dlp_matched|DLP match|8|"
        f"externalId={EVENT_ID} rt=1786755724000 start=1786755721000 "
        "end=1786755724000 cnt=3 cat=data_loss_prevention act=block outcome=blocked "
        f"cs1Label=TenantID cs1={TENANT_ID} cs2Label=ResourceType cs2=dlp_rule "
        "cs3Label=ResourceID cs3=resource_prd081 cs4Label=TraceID "
        "cs4=trc_prd081_last cs5Label=FirstRequestID cs5=req_prd081_first "
        "cs6Label=LastRequestID cs6=req_prd081_last "
        "cn1Label=SourceEventCount cn1=2"
    )
    assert b"\r" not in body and b"\n" not in body


def test_cef_test_event_never_impersonates_production_event() -> None:
    """Endpoint 测试使用独立 CEF 标识，不制造可被 SIEM 当作生产告警的事件。"""

    test_envelope = {
        "event_id": "evt_" + "b" * 32,
        "event_type": "webhook.test",
        "timestamp": "2026-08-15T01:02:05Z",
        "tenant_id": TENANT_ID,
        "actor_id": "usr_prd081",
        "object_id": "whep_" + "c" * 32,
        "trace_id": "trc_prd081_test",
        "payload": {"status": "test"},
    }
    body = webhook_test_cef_bytes(test_envelope)
    assert body.startswith(b"CEF:0|MetaHub|AI Gateway|1|webhook.test|Webhook test|1|")
    assert SECURITY_EVENT_TYPE.encode("ascii") not in body
    with pytest.raises(ValueError, match="webhook.format_invalid"):
        webhook_test_cef_bytes(_envelope())
