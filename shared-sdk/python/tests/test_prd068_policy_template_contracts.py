"""PRD-068 策略模板共享 RBAC 与非终止 mask 合同测试。"""

from __future__ import annotations

from metahub_shared.rbac import (
    CATALOG_REVISION,
    PERMISSION_BY_CODE,
    ROLE_GRANTABLE_PERMISSIONS,
)
from metahub_shared.runtime.errors import ERROR_SPECS
from metahub_shared.runtime.policy_dsl import PolicyEvaluationInput, evaluate_policy_set
from metahub_shared.runtime.policy_snapshot import (
    build_policy_snapshot_artifact,
    build_policy_snapshot_item,
    deserialize_policy_snapshot_wire_json,
    serialize_policy_snapshot_wire,
)


def test_policy_template_permissions_are_separate_and_role_bounded() -> None:
    """权限主流程：四个职责管理员获得模板能力，Developer/Viewer 不得获得页面或应用权限。"""

    # 当前共享目录已由 PRD-074 提升到 revision 10；本测试只冻结模板权限原有边界。
    assert CATALOG_REVISION == 10
    assert PERMISSION_BY_CODE["policy_template.read"].risk_level == "read"
    assert PERMISSION_BY_CODE["policy_template.apply"].risk_level == "critical"
    for role_code in ("admin", "finance_admin", "security_admin", "ops_admin"):
        assert "policy_template.read" in ROLE_GRANTABLE_PERMISSIONS[role_code]
        assert "policy_template.apply" in ROLE_GRANTABLE_PERMISSIONS[role_code]
    for role_code in ("developer", "viewer"):
        assert "policy_template.read" not in ROLE_GRANTABLE_PERMISSIONS[role_code]
        assert "policy_template.apply" not in ROLE_GRANTABLE_PERMISSIONS[role_code]


def test_policy_template_invariant_conflict_has_frozen_safe_contract() -> None:
    """持续发布冲突固定为不可重试 409，文案不得复制 DSL、绑定值或 Policy 身份。"""

    spec = ERROR_SPECS["policy_snapshot.template_invariant_conflict"]
    assert (spec.http_status, spec.retryable, spec.owner) == (
        409,
        False,
        "policy_snapshot",
    )
    assert spec.message == (
        "The active policy candidate conflicts with an applied policy template."
    )
    assert spec.suggestion == (
        "Disable the conflicting policy or roll back the affected template application."
    )
    rendered = f"{spec.message} {spec.suggestion}".lower()
    assert all(
        sensitive not in rendered
        for sensitive in ("dsl", "binding", "prompt", "response", "token", "key")
    )


def test_mask_snapshot_wire_and_safe_evaluation_summary_round_trip() -> None:
    """跨服务合同：mask 动作经双哈希 wire 往返后仍为非终止修饰事实，摘要不携带 DSL。"""

    item = build_policy_snapshot_item(
        evaluation_order=0,
        policy_id="pol_mask_customer_service",
        policy_version=1,
        policy_rule_id="prule_mask_customer_service",
        policy_action_id="pact_mask_customer_service",
        priority=10,
        expression='project == "prj_customer_service"',
        action_type="mask",
    )
    snapshot = build_policy_snapshot_artifact(
        tenant_id="tenant_mask",
        policy_snapshot_id="psnap_mask_v1",
        snapshot_version=1,
        operation="publish",
        policies=(item,),
    )

    wire = serialize_policy_snapshot_wire(snapshot)
    restored = deserialize_policy_snapshot_wire_json(
        wire,
        expected_tenant_id="tenant_mask",
    )

    assert restored == snapshot
    assert restored.policies[0].action_type == "mask"
    assert restored.policies[0].target_model_id is None
    assert b'"action_type":"mask"' in wire

    restored_item = restored.policies[0]
    evaluated = evaluate_policy_set(
        (
            PolicyEvaluationInput(
                policy_id=restored_item.policy_id,
                policy_rule_id=restored_item.policy_rule_id,
                policy_action_id=restored_item.policy_action_id,
                priority=restored_item.priority,
                compiled_expression=restored_item.compiled_expression,
                action_type=restored_item.action_type,
                target_model_id=restored_item.target_model_id,
            ),
        ),
        {
            "tenant": "tenant_mask",
            "env": "prod",
            "project": "prj_customer_service",
            "model": "customer-service",
        },
        timeout_ms=50,
    )
    summary = evaluated.as_dict()
    assert summary["decision"] == "allow"
    assert summary["mask_required"] is True
    assert summary["policy_reason"] is None
    assert summary["mask_matches"][0]["action_type"] == "mask"
    assert "canonical_expression" not in summary["mask_matches"][0]
