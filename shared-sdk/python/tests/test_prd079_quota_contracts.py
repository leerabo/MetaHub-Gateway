"""PRD-079 套餐额度 Shared、RBAC、错误与 Webhook 合同专项测试。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from metahub_shared.quota import (
    QUOTA_CONSUMPTION_ORDER,
    QUOTA_ORDER_VERSION,
    QuotaContractError,
    canonical_quota_intent_sha256,
    classify_allocations,
    format_cny_micros,
    parse_cny_micros,
)
from metahub_shared.rbac import (
    CATALOG_REVISION,
    PERMISSION_BY_CODE,
    PERMISSION_CATALOG,
    ROLE_GRANTABLE_PERMISSIONS,
    permission_matrix_sha256,
)
from metahub_shared.runtime.errors import ERROR_SPECS
from metahub_shared.runtime.webhooks import build_webhook_event

# 固定时间和标识用于验证跨服务事件规范化，避免测试结果随运行环境漂移。
NOW = datetime(2026, 8, 14, 3, 0, 0, tzinfo=UTC)
PLAN_ID = "pln_" + "a" * 32
EVENT_ID = "qevt_" + "b" * 32


def test_quota_money_is_exact_ascii_micros_without_float_rounding() -> None:
    """金额只接受普通十进制文本，并可无损往返固定六位 CNY。"""

    assert parse_cny_micros("0") == 0
    assert parse_cny_micros("12.345678") == 12_345_678
    assert format_cny_micros(12_345_678) == "12.345678"
    for invalid in (1, 1.0, "", ".1", "01", "1.", "1.0000000", "+1", "1e2", "NaN"):
        with pytest.raises(QuotaContractError, match="quota.amount_invalid"):
            parse_cny_micros(invalid)
    with pytest.raises(QuotaContractError, match="quota.amount_invalid"):
        parse_cny_micros("0", allow_zero=False)


def test_quota_order_allocation_class_and_intent_hash_are_frozen() -> None:
    """试用先行顺序、跨桶分类和幂等意图序列化不受字典顺序影响。"""

    assert QUOTA_CONSUMPTION_ORDER == ("trial", "free", "gift", "paid")
    assert QUOTA_ORDER_VERSION == "trial_free_gift_paid_v1"
    assert classify_allocations(("trial",), amount_micros=1) == "trial"
    assert classify_allocations(("trial", "paid"), amount_micros=2) == "mixed"
    assert classify_allocations((), amount_micros=0) == "unallocated"
    left = {"quota_type": "gift", "direction": "credit", "amount_micros": 10, "reason": "grant"}
    right = {"reason": "grant", "amount_micros": 10, "direction": "credit", "quota_type": "gift"}
    assert canonical_quota_intent_sha256(left) == canonical_quota_intent_sha256(right)
    with pytest.raises(QuotaContractError, match="quota.intent_invalid"):
        canonical_quota_intent_sha256({"amount": 1.0})


def test_rbac_revision_15_grants_plan_and_quota_only_to_default_finance_roles() -> None:
    """四个独立权限默认只授予 admin 与 finance_admin，其他角色不被迁移扩权。"""

    codes = {
        "billing.plan.read",
        "billing.plan.write",
        "billing.quota.read",
        "billing.quota.write",
    }
    assert CATALOG_REVISION == 15
    assert len(PERMISSION_CATALOG) == 76
    assert PERMISSION_BY_CODE["billing.plan.read"].risk_level == "read"
    assert PERMISSION_BY_CODE["billing.quota.write"].risk_level == "critical"
    assert codes <= ROLE_GRANTABLE_PERMISSIONS["admin"]
    assert codes <= ROLE_GRANTABLE_PERMISSIONS["finance_admin"]
    for role in ("developer", "viewer", "security_admin", "ops_admin"):
        assert not codes & ROLE_GRANTABLE_PERMISSIONS[role]
    assert permission_matrix_sha256(ROLE_GRANTABLE_PERMISSIONS["admin"]) == (
        "9d92ea073fcfb7425ef0d46e881fc16cf50fdbb0192560812c0e8c5fe9c6946f"
    )
    assert permission_matrix_sha256(ROLE_GRANTABLE_PERMISSIONS["finance_admin"]) == (
        "5311448a5233c5f2430620095b90bd33af8489e26bab0357c861437ae1916c39"
    )


def test_quota_error_catalog_has_stable_status_and_retry_semantics() -> None:
    """管理与 Gateway 错误均进入统一 ErrorResponse 目录。"""

    expected = {
        "plan.request_invalid": (422, False),
        "plan.not_found": (404, False),
        "plan.revision_conflict": (409, False),
        "plan.suspended": (409, False),
        "plan.unavailable": (503, True),
        "quota.request_invalid": (422, False),
        "quota.adjustment_conflict": (409, False),
        "quota.not_found": (404, False),
        "quota.unavailable": (503, True),
        "gateway.plan_suspended": (402, False),
        "gateway.quota_exhausted": (402, False),
        "gateway.quota_unavailable": (503, True),
    }
    actual = {
        code: (ERROR_SPECS[code].http_status, ERROR_SPECS[code].retryable)
        for code in expected
    }
    assert actual == expected


def test_quota_exhausted_webhook_is_exact_and_plan_bound() -> None:
    """耗尽事件只外发 Plan 周期和安全金额，不接受 Bucket 或请求正文漂移。"""

    payload = {
        "schema_version": 1,
        "plan_id": PLAN_ID,
        "quota_generation": 3,
        "required_cny": "2.000000",
        "available_cny": "1.500000",
        "reason_code": "quota_exhausted",
        "effective_tier": "trial",
    }
    event = build_webhook_event(
        tenant_id="tenant_prd079",
        event_type="quota.exhausted",
        payload=payload,
        actor_id="quota-admission",
        object_id=PLAN_ID,
        trace_id="trc_prd079",
        source_request_id="req_prd079",
        event_id=EVENT_ID,
        timestamp=NOW,
    )
    assert event.payload == payload
    for drift in (
        {**payload, "bucket_id": "qbk_" + "c" * 32},
        {**payload, "available_cny": "2.000001"},
        {**payload, "reason_code": "provider_response"},
    ):
        with pytest.raises(ValueError, match="webhook.payload_invalid"):
            build_webhook_event(
                tenant_id="tenant_prd079",
                event_type="quota.exhausted",
                payload=drift,
                actor_id="quota-admission",
                object_id=PLAN_ID,
                trace_id="trc_prd079",
                source_request_id="req_prd079",
                event_id=EVENT_ID,
                timestamp=NOW,
            )


def test_trial_expired_webhook_requires_system_actor_and_matching_time() -> None:
    """试用到期只能由两条收敛路径发布，并与持久发生秒完全一致。"""

    payload = {
        "schema_version": 1,
        "plan_id": PLAN_ID,
        "trial_cycle": 1,
        "expired_at": "2026-08-14T03:00:00Z",
        "effective_tier": "paid",
    }
    event = build_webhook_event(
        tenant_id="tenant_prd079",
        event_type="trial.expired",
        payload=payload,
        actor_id="quota-trial-scheduler",
        object_id=PLAN_ID,
        trace_id="trc_prd079_expire",
        source_request_id="req_prd079_expire",
        event_id=EVENT_ID,
        timestamp=NOW,
    )
    assert event.payload == payload
    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        build_webhook_event(
            tenant_id="tenant_prd079",
            event_type="trial.expired",
            payload=payload,
            actor_id="finance-admin",
            object_id=PLAN_ID,
            trace_id="trc_prd079_expire",
            source_request_id="req_prd079_expire",
            event_id=EVENT_ID,
            timestamp=NOW,
        )


def test_finops_event_requires_authoritative_quota_allocation_summary() -> None:
    """Usage 事件必须声明真实分摊分类；人工项只能是 not_applicable。"""

    base = {
        "ledger_entry_id": "fle_" + "c" * 32,
        "entry_type": "usage_charge",
        "direction": "debit",
        "amount": "1.000000",
        "currency": "CNY",
        "normalized_cny": "1.000000",
        "cny_normalization_status": "normalized",
        "quota_type": "paid",
        "source_request_id": "req_prd079_finops",
        "price_snapshot_id": "prs_" + "d" * 32,
        "price_schema_version": "price-v1",
        "price_snapshot_source": "model-price",
        "price_evidence_status": "priced",
        "reversal_of_entry_id": None,
        "quota_allocation_class": "mixed",
        "quota_allocation_count": 2,
    }
    event = build_webhook_event(
        tenant_id="tenant_prd079",
        event_type="ledger.entry_created",
        payload=base,
        actor_id=None,
        object_id=base["ledger_entry_id"],
        trace_id="trc_prd079_finops",
        event_id="flevt_" + "e" * 32,
        timestamp=NOW,
    )
    assert event.payload["quota_allocation_class"] == "mixed"
    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        build_webhook_event(
            tenant_id="tenant_prd079",
            event_type="ledger.entry_created",
            payload={**base, "quota_allocation_count": 1},
            actor_id=None,
            object_id=base["ledger_entry_id"],
            trace_id="trc_prd079_finops",
            event_id="flevt_" + "e" * 32,
            timestamp=NOW,
        )
