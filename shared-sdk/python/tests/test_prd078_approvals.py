"""PRD-078 审批 Shared 合同、RBAC、Webhook 与配置边界专项测试。"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from metahub_shared.approvals import (
    CREATE_INTENT_DOMAIN,
    CREATE_KEY_DOMAIN,
    CURSOR_DOMAIN,
    DECISION_INTENT_DOMAIN,
    DECISION_KEY_DOMAIN,
    STEP_UP_DOMAIN,
    TARGET_FINGERPRINT_DOMAIN,
    ApprovalContractError,
    ApprovalKeyring,
    VersionedApprovalSecret,
    canonical_approval_bytes,
)
from metahub_shared.rbac import (
    CATALOG_REVISION,
    PERMISSION_BY_CODE,
    ROLE_GRANTABLE_PERMISSIONS,
)
from metahub_shared.runtime.config import ConfigError, load_app_config
from metahub_shared.runtime.webhooks import build_webhook_event

# 共享契约测试使用固定时钟和不透明虚构标识，保证序列化断言稳定且不混入真实业务数据。
NOW = datetime(2026, 8, 12, 8, 30, 0, tzinfo=UTC)
APPROVAL_ID = "apr_" + "a" * 32
EVENT_ID = "apevt_" + "b" * 32


def _clear_metahub_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离宿主机环境，确保配置断言只观察仓库默认值和当前用例覆盖。"""

    for key in tuple(os.environ):
        if key.startswith("METAHUB_"):
            monkeypatch.delenv(key, raising=False)


def test_approval_hmac_domains_are_independent_and_rotation_is_current_first() -> None:
    """所有审批用途固定分域；旧 key 只参与有界验证，不能签发新事实。"""

    previous = VersionedApprovalSecret(3, "previous-approval-secret-" + "p" * 32)
    current = VersionedApprovalSecret(4, "current-approval-secret-" + "c" * 32)
    keyring = ApprovalKeyring(current=current, previous=previous)
    payload = {
        "tenant_id": "tenant_prd078",
        "approval_id": APPROVAL_ID,
        "revision": 1,
        "active": True,
    }
    domains = (
        CREATE_KEY_DOMAIN,
        CREATE_INTENT_DOMAIN,
        DECISION_KEY_DOMAIN,
        DECISION_INTENT_DOMAIN,
        STEP_UP_DOMAIN,
        CURSOR_DOMAIN,
        TARGET_FINGERPRINT_DOMAIN,
    )

    current_digests = [keyring.digest(domain, payload) for domain in domains]
    assert {item.version for item in current_digests} == {4}
    assert len({item.digest for item in current_digests}) == len(domains)

    candidates = keyring.digest_candidates(STEP_UP_DOMAIN, payload)
    assert [item.version for item in candidates] == [4, 3]
    previous_only = ApprovalKeyring(current=previous).digest(STEP_UP_DOMAIN, payload)
    assert candidates[1].digest == previous_only.digest
    assert candidates[0].digest != candidates[1].digest
    assert current.secret.decode("utf-8") not in repr(keyring)
    assert previous.secret.decode("utf-8") not in repr(keyring)


def test_approval_canonical_json_is_stable_and_rejects_implicit_values() -> None:
    """规范 JSON 不接受 float、bytes、空键或过深结构，避免跨语言摘要漂移。"""

    left = {"z": [1, True, None], "a": {"text": "审批"}}
    right = {"a": {"text": "审批"}, "z": [1, True, None]}
    assert canonical_approval_bytes(left) == canonical_approval_bytes(right)
    assert canonical_approval_bytes(left).decode("utf-8") == (
        '{"a":{"text":"审批"},"z":[1,true,null]}'
    )

    invalid_values = (
        {"value": 1.0},
        {"value": float("nan")},
        {"value": b"opaque"},
        {"": "empty-key"},
        {1: "non-string-key"},
    )
    for value in invalid_values:
        with pytest.raises(ApprovalContractError) as captured:
            canonical_approval_bytes(value)  # type: ignore[arg-type]
        assert captured.value.code == "approval.hmac_input_invalid"

    nested: object = "leaf"
    for _ in range(34):
        nested = {"next": nested}
    with pytest.raises(ApprovalContractError, match="approval.hmac_input_invalid"):
        canonical_approval_bytes(nested)  # type: ignore[arg-type]


def test_rbac_approval_permissions_remain_scoped_after_revision_14() -> None:
    """审批职责和目标域职责分离，后续目录版本不得扩大默认审批角色。"""

    assert CATALOG_REVISION >= 14
    assert PERMISSION_BY_CODE["approval.read"].risk_level == "read"
    assert PERMISSION_BY_CODE["approval.decide"].risk_level == "critical"
    approval_permissions = {"approval.read", "approval.decide"}

    for role in ("admin", "finance_admin", "security_admin"):
        assert approval_permissions <= ROLE_GRANTABLE_PERMISSIONS[role]
    for role in ("developer", "viewer", "ops_admin"):
        assert not approval_permissions & ROLE_GRANTABLE_PERMISSIONS[role]

    assert "billing.budget.write" in ROLE_GRANTABLE_PERMISSIONS["finance_admin"]
    assert "session_risk.manage" in ROLE_GRANTABLE_PERMISSIONS["security_admin"]
    assert "session_risk.manage" not in ROLE_GRANTABLE_PERMISSIONS["finance_admin"]


def test_approval_created_webhook_has_exact_safe_card_payload() -> None:
    """企微、钉钉、飞书只收到打开 MetaHub 的固定卡片摘要，不能承载授权命令。"""

    payload = {
        "schema_version": 1,
        "approval_id": APPROVAL_ID,
        "status": "pending",
        "target_type": "budget_block_release",
        "target_action": "release",
        "risk_level": "standard",
        "requires_step_up": False,
        "expires_at": "2026-08-12T08:35:00Z",
        "entry": "approval-card",
    }
    event = build_webhook_event(
        tenant_id="tenant_prd078",
        event_type="approval.created",
        payload=payload,
        actor_id="internal-budget-producer",
        object_id=APPROVAL_ID,
        trace_id="trc_prd078_created",
        source_request_id="req_prd078_created",
        event_id=EVENT_ID,
        timestamp=NOW,
    )
    assert event.payload == payload
    assert event.to_payload() == {
        "event_id": EVENT_ID,
        "event_type": "approval.created",
        "timestamp": "2026-08-12T08:30:00Z",
        "tenant_id": "tenant_prd078",
        "actor_id": "internal-budget-producer",
        "object_id": APPROVAL_ID,
        "trace_id": "trc_prd078_created",
        "payload": payload,
    }

    for drift in (
        {**payload, "approve_url": "https://outside.invalid/approve"},
        {**payload, "status": "approved"},
        {**payload, "requires_step_up": True},
        {**payload, "target_action": "arbitrary_command"},
    ):
        with pytest.raises(ValueError, match="webhook.payload_invalid"):
            build_webhook_event(
                tenant_id="tenant_prd078",
                event_type="approval.created",
                payload=drift,
                actor_id="internal-budget-producer",
                object_id=APPROVAL_ID,
                trace_id="trc_prd078_created",
                source_request_id="req_prd078_created",
                event_id=EVENT_ID,
                timestamp=NOW,
            )


def test_approval_timeout_webhook_is_bound_to_scheduler_terminal_fact() -> None:
    """timeout 只能由固定 scheduler actor 发布，信封时间必须等于终态时间。"""

    payload = {
        "schema_version": 1,
        "approval_id": APPROVAL_ID,
        "status": "rejected",
        "target_type": "session_risk_action",
        "target_action": "freeze_session",
        "resolution_reason": "timeout",
        "decided_at": "2026-08-12T08:30:00Z",
    }
    event = build_webhook_event(
        tenant_id="tenant_prd078",
        event_type="approval.timeout",
        payload=payload,
        actor_id="approval-timeout-scheduler",
        object_id=APPROVAL_ID,
        trace_id="trc_prd078_timeout",
        source_request_id="req_prd078_timeout",
        event_id=EVENT_ID,
        timestamp=NOW,
    )
    assert event.payload == payload

    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        build_webhook_event(
            tenant_id="tenant_prd078",
            event_type="approval.timeout",
            payload=payload,
            actor_id="external-platform",
            object_id=APPROVAL_ID,
            trace_id="trc_prd078_timeout",
            source_request_id="req_prd078_timeout",
            event_id=EVENT_ID,
            timestamp=NOW,
        )


def test_approval_config_is_independent_masked_and_prod_missing_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """生产缺审批 key 不得回退 Auth/SessionRisk；显式 key 必须独立且永不序列化。"""

    _clear_metahub_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("METAHUB_ENV", "prod")
    production = load_app_config("control-plane-api")
    assert production.approval_hmac_secret is None

    approval_secret = "prod-independent-approval-secret-" + "a" * 32
    monkeypatch.setenv("METAHUB_APPROVAL_HMAC_VERSION", "2")
    monkeypatch.setenv("METAHUB_APPROVAL_HMAC_SECRET", approval_secret)
    configured = load_app_config("control-plane-api")
    assert configured.approval_hmac_version == 2
    assert configured.approval_hmac_secret == approval_secret
    rendered = repr(configured) + repr(configured.model_dump())
    assert approval_secret not in rendered
    summary = {item.key: item for item in configured.config_items}
    assert summary["METAHUB_APPROVAL_HMAC_SECRET"].status == "masked"
    assert approval_secret not in summary["METAHUB_APPROVAL_HMAC_SECRET"].value_preview

    monkeypatch.setenv("METAHUB_AUTH_TOKEN_SECRET", approval_secret)
    with pytest.raises(ConfigError) as captured:
        load_app_config("control-plane-api")
    assert captured.value.code == "infra.config_invalid"
    assert approval_secret not in captured.value.message
