"""PRD-080 账单争议 RBAC、错误、i18n 与 Webhook Shared 合同。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from metahub_shared.rbac import (
    CATALOG_REVISION,
    PERMISSION_BY_CODE,
    PERMISSION_CATALOG,
    ROLE_GRANTABLE_PERMISSIONS,
    permission_matrix_sha256,
)
from metahub_shared.runtime.errors import ERROR_SPECS
from metahub_shared.runtime.i18n import error_label
from metahub_shared.runtime.webhooks import build_webhook_event

NOW = datetime(2026, 8, 15, 1, 2, 3, tzinfo=UTC)
DISPUTE_ID = "bdsp_" + "a" * 32
EVENT_ID = "bdevt_" + "b" * 32


def test_prd080_rbac_revision_16_is_finance_scoped() -> None:
    """争议读、申请和审核默认只扩展不可变管理员与财务管理员。"""

    codes = {
        "billing.dispute.read",
        "billing.dispute.create",
        "billing.dispute.review",
    }
    assert CATALOG_REVISION == 16
    assert len(PERMISSION_CATALOG) == 79
    assert PERMISSION_BY_CODE["billing.dispute.read"].risk_level == "read"
    assert PERMISSION_BY_CODE["billing.dispute.create"].risk_level == "write"
    assert PERMISSION_BY_CODE["billing.dispute.review"].risk_level == "critical"
    assert codes <= ROLE_GRANTABLE_PERMISSIONS["admin"]
    assert codes <= ROLE_GRANTABLE_PERMISSIONS["finance_admin"]
    for role in ("developer", "viewer", "security_admin", "ops_admin"):
        assert not codes & ROLE_GRANTABLE_PERMISSIONS[role]
    assert permission_matrix_sha256(ROLE_GRANTABLE_PERMISSIONS["admin"]) == (
        "c56c271d650b89b1d4d6d49c1b8e326c89b075bb274116930a335f4c8f230e16"
    )
    assert permission_matrix_sha256(ROLE_GRANTABLE_PERMISSIONS["finance_admin"]) == (
        "86b64ca3b997f5fa252f027ecf07430e677252e6d2c17677b72ac049130cba8c"
    )


def test_prd080_error_catalog_and_i18n_are_complete() -> None:
    """固定错误码进入统一信封目录，并为中英文界面提供静态安全标签。"""

    expected = {
        "billing_dispute.query_invalid": (422, False),
        "billing_dispute.request_invalid": (422, False),
        "billing_dispute.not_found": (404, False),
        "billing_dispute.scope_not_found": (404, False),
        "billing_dispute.scope_too_large": (422, False),
        "billing_dispute.hold_conflict": (409, False),
        "billing_dispute.revision_conflict": (409, False),
        "billing_dispute.transition_invalid": (409, False),
        "billing_dispute.self_review_forbidden": (403, False),
        "billing_dispute.adjustment_invalid": (422, False),
        "billing_dispute.adjustment_conflict": (409, False),
        "billing_dispute.idempotency_conflict": (409, False),
        "billing_dispute.integrity_failed": (503, True),
        "billing_dispute.unavailable": (503, True),
    }
    actual = {
        code: (ERROR_SPECS[code].http_status, ERROR_SPECS[code].retryable)
        for code in expected
    }
    assert actual == expected
    for code in expected:
        assert error_label(code, "zh-CN")
        assert error_label(code, "en-US")


def test_billing_dispute_created_webhook_is_exact_and_owner_bound() -> None:
    """创建事件只携带可重建汇总，并绑定持久事件、申请人和创建链路。"""

    payload = {
        "schema_version": 1,
        "billing_dispute_id": DISPUTE_ID,
        "scope_type": "request_ids",
        "item_count": 2,
        "currency_totals": {"CNY": "1.250000", "USD": "2.000000"},
        "status": "submitted",
        "created_at": "2026-08-15T01:02:03Z",
    }
    event = build_webhook_event(
        tenant_id="tenant_prd080",
        event_type="billing_dispute.created",
        payload=payload,
        actor_id="usr_prd080_finance",
        object_id=DISPUTE_ID,
        trace_id="trc_prd080_create",
        source_request_id="req_prd080_create",
        event_id=EVENT_ID,
        timestamp=NOW,
    )
    assert event.payload == payload
    assert event.to_payload()["event_id"] == EVENT_ID

    aggregate_payload = {
        **payload,
        "item_count": 100,
        "currency_totals": {"CNY": "100000000000.000000"},
    }
    aggregate_event = build_webhook_event(
        tenant_id="tenant_prd080",
        event_type="billing_dispute.created",
        payload=aggregate_payload,
        actor_id="usr_prd080_finance",
        object_id=DISPUTE_ID,
        trace_id="trc_prd080_create",
        source_request_id="req_prd080_create",
        event_id=EVENT_ID,
        timestamp=NOW,
    )
    assert aggregate_event.payload == aggregate_payload

    drifted_payloads = (
        {**payload, "reason": "must-not-leave-authoritative-db"},
        {**payload, "currency_totals": {"CNY": 1.25}},
        {**payload, "currency_totals": {"EUR": "1.000000"}},
        {**payload, "currency_totals": {"CNY": "2000000000.000001"}},
        {**payload, "status": "approved"},
    )
    for drifted in drifted_payloads:
        with pytest.raises(ValueError, match="webhook.payload_invalid"):
            build_webhook_event(
                tenant_id="tenant_prd080",
                event_type="billing_dispute.created",
                payload=drifted,
                actor_id="usr_prd080_finance",
                object_id=DISPUTE_ID,
                trace_id="trc_prd080_create",
                source_request_id="req_prd080_create",
                event_id=EVENT_ID,
                timestamp=NOW,
            )


@pytest.mark.parametrize(
    ("event_id", "actor_id", "object_id", "timestamp"),
    (
        ("evt_" + "b" * 32, "usr_prd080_finance", DISPUTE_ID, NOW),
        (EVENT_ID, None, DISPUTE_ID, NOW),
        (EVENT_ID, "usr_prd080_finance", "bdsp_" + "c" * 32, NOW),
        (
            EVENT_ID,
            "usr_prd080_finance",
            DISPUTE_ID,
            datetime(2026, 8, 15, 1, 2, 4, tzinfo=UTC),
        ),
    ),
)
def test_billing_dispute_created_webhook_rejects_envelope_drift(
    event_id: str,
    actor_id: str | None,
    object_id: str,
    timestamp: datetime,
) -> None:
    """恢复 worker 不能替换 source event 的身份、对象、申请人或发生秒。"""

    payload = {
        "schema_version": 1,
        "billing_dispute_id": DISPUTE_ID,
        "scope_type": "time_range",
        "item_count": 1,
        "currency_totals": {"CNY": "1.000000"},
        "status": "submitted",
        "created_at": "2026-08-15T01:02:03Z",
    }
    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        build_webhook_event(
            tenant_id="tenant_prd080",
            event_type="billing_dispute.created",
            payload=payload,
            actor_id=actor_id,
            object_id=object_id,
            trace_id="trc_prd080_create",
            source_request_id="req_prd080_create",
            event_id=event_id,
            timestamp=timestamp,
        )
