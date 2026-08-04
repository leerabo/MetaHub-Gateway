"""PRD-046 共享错误、Webhook 与审计安全合同单元测试。

这些测试只构造安全摘要，不使用 Prompt、DSL/AST、Provider URL、预算余额或任何密钥，
从而能在不启动完整 Control API 的情况下锁定跨模块稳定字段。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest
from metahub_control.audit.service import (
    ACTION_OBJECT_TYPES,
    SUMMARY_FIELDS,
    AuditRequestMeta,
    record_audit_log,
)
from metahub_control.auth.models import AuthSession, AuthTenant, AuthUser
from metahub_control.auth.service import AuthContext
from metahub_shared.runtime.error_diagnosis import diagnose_error
from metahub_shared.runtime.errors import ERROR_SPECS, ApiError
from metahub_shared.runtime.webhooks import (
    WEBHOOK_SUBSCRIBABLE_EVENT_TYPES,
    build_webhook_event,
)

# 错误与审计字段集合锁定跨模块稳定合同，确保权限拒绝、异常诊断和安全摘要不会各自漂移。
DRY_RUN_ERROR_CONTRACT = {
    "policy.dry_run_payload_invalid": (422, False, "policy_dry_run_payload_invalid"),
    "policy.dry_run_payload_too_large": (413, False, "policy_dry_run_payload_invalid"),
    "policy.dry_run_context_not_found": (404, False, "policy_dry_run_context_not_found"),
    "policy.dry_run_request_id_reused": (409, False, "policy_dry_run_request_id_reused"),
    "policy.dry_run_rate_limited": (429, True, "policy_dry_run_rate_limited"),
    "policy.dry_run_unavailable": (503, True, "policy_dry_run_unavailable"),
}

DRY_RUN_AUDIT_FIELDS = {
    "policy_explanation_id",
    "policy_set_fingerprint",
    "decision",
    "decision_reason_code",
    "matched_policy_id",
    "matched_priority",
    "matched_action",
    "route_status",
    "dlp_status",
    "budget_status",
    "would_error_code",
    "checked_policy_count",
    "runtime_compatible",
    "coverage_complete",
}


def _webhook_payload() -> dict[str, object]:
    """返回 Webhook 精确 13 键摘要；可空字段仍显式出现。"""

    return {
        "policy_explanation_id": "pexp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "request_id": "req_prd046_contract",
        "policy_set_fingerprint": "b" * 64,
        "decision": "allow",
        "decision_reason_code": "policy.default_allow",
        "matched_policy_id": None,
        "route_status": "selected",
        "dlp_status": "clean",
        "budget_status": "within_limit",
        "would_error_code": None,
        "checked_policy_count": 0,
        "runtime_compatible": True,
        "coverage_complete": False,
    }


def _audit_after() -> dict[str, object]:
    """返回审计精确 14 键摘要，与 Webhook 的输入无关字段保持分离。"""

    payload = _webhook_payload()
    return {
        "policy_explanation_id": payload["policy_explanation_id"],
        "policy_set_fingerprint": payload["policy_set_fingerprint"],
        "decision": payload["decision"],
        "decision_reason_code": payload["decision_reason_code"],
        "matched_policy_id": None,
        "matched_priority": None,
        "matched_action": None,
        "route_status": payload["route_status"],
        "dlp_status": payload["dlp_status"],
        "budget_status": payload["budget_status"],
        "would_error_code": None,
        "checked_policy_count": payload["checked_policy_count"],
        "runtime_compatible": payload["runtime_compatible"],
        "coverage_complete": payload["coverage_complete"],
    }


def _auth_context() -> AuthContext:
    """构造仅含审计写入所需可信身份字段的本地上下文。"""

    # 固定 tenant/user 配对用于验证审计租户隔离；测试令牌只驻留内存且不得进入摘要或持久字段。
    return AuthContext(
        user=AuthUser(
            user_id="user_prd046_contract",
            tenant_id="tenant_prd046_contract",
            email="admin@example.invalid",
            display_name="Contract Admin",
            role="admin",
        ),
        tenant=AuthTenant(
            tenant_id="tenant_prd046_contract",
            slug="prd046-contract",
            name="PRD-046 Contract",
        ),
        session=AuthSession(
            session_id="ses_prd046_contract",
            expires_at=datetime(2026, 7, 19, tzinfo=UTC),
        ),
        token="test-token-not-persisted",
        token_jti="jti_prd046_contract",
    )


def _audit_connection() -> sqlite3.Connection:
    """创建最小内存审计表，只验证服务生成的固定 JSON 与 changed_fields。"""

    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE audit_logs(
          audit_log_id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          actor_user_id TEXT NOT NULL,
          actor_email TEXT NOT NULL,
          actor_role TEXT NOT NULL,
          action TEXT NOT NULL,
          object_type TEXT NOT NULL,
          object_id TEXT NOT NULL,
          object_name TEXT,
          before_summary_json TEXT NOT NULL,
          after_summary_json TEXT NOT NULL,
          changed_fields_json TEXT NOT NULL,
          client_ip TEXT,
          user_agent TEXT,
          request_id TEXT NOT NULL,
          trace_id TEXT NOT NULL,
          created_at TEXT NOT NULL
        )
        """
    )
    return connection


def test_policy_dry_run_errors_have_exact_http_retry_and_fixed_diagnosis() -> None:
    """错误目录：六个 HTTP 错误全部显式登记，且诊断不回退到 unknown。"""

    for code, (http_status, retryable, rule_id) in DRY_RUN_ERROR_CONTRACT.items():
        spec = ERROR_SPECS[code]
        diagnosis = diagnose_error(code, http_status=http_status, message="prompt sentinel")

        assert (spec.http_status, spec.retryable, spec.owner) == (
            http_status,
            retryable,
            "policy_dry_run",
        )
        assert diagnosis.rule_id == rule_id
        assert diagnosis.matched_by == "error_code"
        assert diagnosis.retryable is retryable
        assert "sentinel" not in diagnosis.safe_summary


def test_policy_dry_run_webhook_requires_exact_safe_payload_and_persistent_id() -> None:
    """Webhook：事件可订阅且只接受 13 键、decision_reason_code 和固定 false 覆盖状态。"""

    payload = _webhook_payload()
    assert "policy.dry_run_executed" in WEBHOOK_SUBSCRIBABLE_EVENT_TYPES
    event = build_webhook_event(
        tenant_id="tenant_prd046_contract",
        event_type="policy.dry_run_executed",
        event_id="evt_pevt_prd046_contract",
        actor_id="user_prd046_contract",
        object_id="pexp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        trace_id="trc_prd046_contract",
        payload=payload,
    )

    assert event.event_id == "evt_pevt_prd046_contract"
    assert event.payload == payload
    assert set(event.payload) == set(_webhook_payload())
    assert "decision_reason_code" in event.payload
    assert "reason_code" not in event.payload

    invalid_payloads = []
    for key, value in (
        ("reason_code", "policy.dry_run_executed"),
        ("prompt_length", 12),
        ("coverage_complete", True),
        ("checked_policy_count", True),
    ):
        candidate = _webhook_payload()
        candidate[key] = value
        invalid_payloads.append(candidate)
    missing_field = _webhook_payload()
    missing_field.pop("decision_reason_code")
    invalid_payloads.append(missing_field)

    for candidate in invalid_payloads:
        with pytest.raises(ValueError, match="webhook.payload_invalid"):
            build_webhook_event(
                tenant_id="tenant_prd046_contract",
                event_type="policy.dry_run_executed",
                event_id="evt_pevt_prd046_invalid",
                payload=candidate,
            )
    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        build_webhook_event(
            tenant_id="tenant_prd046_contract",
            event_type="policy.dry_run_executed",
            payload=_webhook_payload(),
        )


def test_policy_dry_run_audit_uses_exact_after_and_fixed_changed_fields() -> None:
    """审计：before 固定为空，after/changed_fields 固定为 14 个安全摘要键。"""

    assert ACTION_OBJECT_TYPES["policy.dry_run_executed"] == "policy_explanation"
    assert SUMMARY_FIELDS["policy_explanation"] == DRY_RUN_AUDIT_FIELDS
    connection = _audit_connection()
    after = _audit_after()
    record = record_audit_log(
        connection,
        _auth_context(),
        action="policy.dry_run_executed",
        object_type="policy_explanation",
        object_id="pexp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        object_name=None,
        before_summary={},
        after_summary=after,
        request_meta=AuditRequestMeta(client_ip=None, user_agent=None),
        request_id="req_prd046_contract",
        trace_id="trc_prd046_contract",
    )

    assert record is not None
    assert record.changed_fields == tuple(sorted(DRY_RUN_AUDIT_FIELDS))
    row = connection.execute(
        "SELECT before_summary_json, after_summary_json, changed_fields_json FROM audit_logs"
    ).fetchone()
    assert json.loads(row[0]) == {}
    assert json.loads(row[1]) == after
    assert json.loads(row[2]) == sorted(DRY_RUN_AUDIT_FIELDS)

    missing_after = _audit_after()
    missing_after.pop("coverage_complete")
    with pytest.raises(ApiError) as missing_exc:
        record_audit_log(
            connection,
            _auth_context(),
            action="policy.dry_run_executed",
            object_type="policy_explanation",
            object_id="pexp_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            object_name=None,
            before_summary={},
            after_summary=missing_after,
            request_meta=AuditRequestMeta(client_ip=None, user_agent=None),
            request_id="req_prd046_missing",
            trace_id="trc_prd046_missing",
        )
    assert missing_exc.value.code == "audit_log.summary_invalid"

    with pytest.raises(ApiError) as before_exc:
        record_audit_log(
            connection,
            _auth_context(),
            action="policy.dry_run_executed",
            object_type="policy_explanation",
            object_id="pexp_cccccccccccccccccccccccccccccccc",
            object_name=None,
            before_summary={"decision": "deny"},
            after_summary=_audit_after(),
            request_meta=AuditRequestMeta(client_ip=None, user_agent=None),
            request_id="req_prd046_before",
            trace_id="trc_prd046_before",
        )
    assert before_exc.value.code == "audit_log.summary_invalid"
