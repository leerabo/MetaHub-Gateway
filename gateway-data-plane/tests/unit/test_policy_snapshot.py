"""PRD-045 shared 策略快照 canonical、wire、安全错误与 Webhook 合同测试。"""

from __future__ import annotations

import copy
from dataclasses import FrozenInstanceError

import pytest
from metahub_shared.runtime.error_diagnosis import diagnose_error
from metahub_shared.runtime.errors import ERROR_SPECS
from metahub_shared.runtime.policy_snapshot import (
    POLICY_SNAPSHOT_CONTENT_SCHEMA_VERSION,
    POLICY_SNAPSHOT_WIRE_SCHEMA_VERSION,
    PolicySnapshotArtifact,
    PolicySnapshotValidationError,
    build_policy_snapshot_artifact,
    build_policy_snapshot_item,
    deserialize_policy_snapshot_wire,
    deserialize_policy_snapshot_wire_json,
    policy_snapshot_to_wire_dict,
    serialize_policy_snapshot_wire,
)
from metahub_shared.runtime.webhooks import (
    WEBHOOK_SUBSCRIBABLE_EVENT_TYPES,
    build_webhook_event,
)


def _item(
    order: int = 0,
    *,
    policy_id: str = "pol_a",
    policy_version: int = 1,
    expression: str = 'env == "prod"',
    action_type: str = "deny",
    target_model_id: str | None = None,
):
    """构造经 shared 编译器重编的测试项，ID 随 Policy 保持唯一。"""

    return build_policy_snapshot_item(
        evaluation_order=order,
        policy_id=policy_id,
        policy_version=policy_version,
        policy_rule_id=f"prule_{policy_id}",
        policy_action_id=f"pact_{policy_id}",
        priority=100 + order,
        expression=expression,
        action_type=action_type,
        target_model_id=target_model_id,
    )


def _snapshot(*, item=None) -> PolicySnapshotArtifact:
    """构造 v1 发布快照，供 wire 篡改测试复用。"""

    return build_policy_snapshot_artifact(
        tenant_id="ten_a",
        policy_snapshot_id="psnap_a",
        snapshot_version=1,
        operation="publish",
        items=[item or _item()],
    )


def test_fixed_double_hash_vector_and_strict_wire_round_trip() -> None:
    """主流程：独立固定向量锁定双 hash，wire 重编后仍得到同一不可变对象。"""

    snapshot = _snapshot()

    assert POLICY_SNAPSHOT_CONTENT_SCHEMA_VERSION == "policy-snapshot-content-v1"
    assert POLICY_SNAPSHOT_WIRE_SCHEMA_VERSION == "policy-snapshot-wire-v1"
    assert snapshot.content_hash == (
        "1109def3051bd1644c72424db7298742e48b0f73a95a8312112dd0685fbb9058"
    )
    assert snapshot.artifact_hash == (
        "cc299ac30fddfa1ec45720830244cd0869c0da18eadcd2ea4ee10211c6399526"
    )

    restored = deserialize_policy_snapshot_wire_json(
        serialize_policy_snapshot_wire(snapshot), expected_tenant_id="ten_a"
    )
    assert restored == snapshot
    assert restored.items is restored.policies
    assert restored.policies[0].compiled_expression.canonical_expression == 'env == "prod"'


def test_unicode_decimal_normalization_and_provenance_hash_split() -> None:
    """兼容性：Unicode/Decimal 等价源码共享 content，来源版本与 envelope 只改变 artifact。"""

    decomposed = _item(
        expression='project == "e\u0301" and budget_remaining == 100.00'
    )
    normalized = _item(
        policy_version=2,
        expression='project == "é" and budget_remaining == 100',
    )
    v1 = _snapshot(item=decomposed)
    v2 = build_policy_snapshot_artifact(
        tenant_id="ten_a",
        policy_snapshot_id="psnap_b",
        snapshot_version=2,
        operation="publish",
        previous_snapshot_id="psnap_a",
        previous_snapshot_version=1,
        policies=[normalized],
    )

    assert decomposed.canonical_expression == normalized.canonical_expression
    assert v1.content_hash == v2.content_hash
    assert v1.artifact_hash != v2.artifact_hash


def test_rollback_creates_monotonic_artifact_with_source_content() -> None:
    """回滚语义：v3 复制 v1 内容，content 相同而 artifact 绑定新版本和来源。"""

    source = _snapshot()
    rollback = build_policy_snapshot_artifact(
        tenant_id="ten_a",
        policy_snapshot_id="psnap_c",
        snapshot_version=3,
        operation="rollback",
        previous_snapshot_id="psnap_b",
        previous_snapshot_version=2,
        rollback_source_snapshot_id="psnap_a",
        rollback_source_snapshot_version=1,
        items=source.items,
    )

    assert rollback.snapshot_version == 3
    assert rollback.content_hash == source.content_hash
    assert rollback.artifact_hash != source.artifact_hash


def test_build_item_recompiles_and_checks_persisted_canonical_quadruple() -> None:
    """控制面合同：持久化 canonical/version/hash/variables 只能作为待核对值，不能被信任。"""

    source = _item()
    rebuilt = build_policy_snapshot_item(
        evaluation_order=source.evaluation_order,
        policy_id=source.policy_id,
        policy_version=source.policy_version,
        policy_rule_id=source.policy_rule_id,
        policy_action_id=source.policy_action_id,
        priority=source.priority,
        dsl_version=source.dsl_version,
        canonical_expression=source.canonical_expression,
        condition_hash=source.condition_hash,
        variables=source.variables,
        action_type=source.action_type,
        target_model_id=source.target_model_id,
    )

    assert rebuilt == source
    with pytest.raises(PolicySnapshotValidationError) as exc:
        build_policy_snapshot_item(
            evaluation_order=0,
            policy_id="pol_a",
            policy_version=1,
            policy_rule_id="prule_pol_a",
            policy_action_id="pact_pol_a",
            priority=100,
            canonical_expression='env == "prod"',
            dsl_version="policy-dsl-v1",
            condition_hash="0" * 64,
            variables=("env",),
            action_type="deny",
        )
    assert exc.value.reason == "condition_hash_mismatch"


@pytest.mark.parametrize(
    ("mutator", "reason"),
    [
        (lambda wire: wire.update({"unknown": 1}), "fields_invalid"),
        (lambda wire: wire.update({"snapshot_version": True}), "integer_required"),
        (lambda wire: wire.update({"tenant_id": "ten_b"}), "tenant_mismatch"),
        (
            lambda wire: wire["policies"][0].update({"evaluation_order": 1}),
            "evaluation_order_invalid",
        ),
        (
            lambda wire: wire["policies"][0].update({"action_type": "execute"}),
            "action_invalid",
        ),
        (
            lambda wire: wire["policies"][0].update({"variables": ["unknown"]}),
            "variable_unsupported",
        ),
        (
            lambda wire: wire["policies"][0].update(
                {"canonical_expression": '(env == "prod")'}
            ),
            "canonical_expression_mismatch",
        ),
    ],
)
def test_wire_rejects_unknown_bool_tenant_order_and_compiler_tampering(
    mutator, reason: str
) -> None:
    """异常流程：任一结构、租户、顺序或 DSL 篡改都整体拒绝，不产生部分运行对象。"""

    wire = copy.deepcopy(policy_snapshot_to_wire_dict(_snapshot()))
    mutator(wire)

    with pytest.raises(PolicySnapshotValidationError) as exc:
        deserialize_policy_snapshot_wire(wire, expected_tenant_id="ten_a")

    assert exc.value.reason == reason
    assert "env ==" not in str(exc.value)


def test_wire_rejects_hash_provenance_duplicate_json_key_and_oversize() -> None:
    """安全边界：双 hash/provenance、重复 JSON key 和实际响应体大小都必须严格校验。"""

    wire = policy_snapshot_to_wire_dict(_snapshot())
    wire["artifact_hash"] = "0" * 64
    with pytest.raises(PolicySnapshotValidationError) as tampered:
        deserialize_policy_snapshot_wire(wire, expected_tenant_id="ten_a")
    assert tampered.value.reason == "artifact_hash_mismatch"

    duplicate = b'{"wire_schema_version":"policy-snapshot-wire-v1","wire_schema_version":"x"}'
    with pytest.raises(PolicySnapshotValidationError) as duplicate_error:
        deserialize_policy_snapshot_wire_json(duplicate)
    assert duplicate_error.value.reason == "wire_json_invalid"

    with pytest.raises(PolicySnapshotValidationError) as oversized:
        deserialize_policy_snapshot_wire_json("x" * 1025, max_payload_bytes=1024)
    assert oversized.value.reason == "wire_too_large"


def test_snapshot_objects_are_frozen_and_mutated_compiled_ast_is_detected() -> None:
    """不可变合同：字段不可替换；即使外部原地改 AST，序列化前重编也会拒绝。"""

    snapshot = _snapshot()
    with pytest.raises(FrozenInstanceError):
        snapshot.snapshot_version = 2  # type: ignore[misc]

    snapshot.items[0].compiled_expression.ast["operator"] = "ne"
    with pytest.raises(PolicySnapshotValidationError) as exc:
        policy_snapshot_to_wire_dict(snapshot)
    assert exc.value.reason == "compiled_policy_mismatch"


def test_error_registry_and_diagnosis_cover_all_snapshot_runtime_failures() -> None:
    """统一错误：控制面、同步、求值与 request claim 都有固定状态、重试性和安全诊断。"""

    expected = {
        "policy_snapshot.payload_invalid": (422, False),
        "policy_snapshot.if_match_required": (428, False),
        "policy_snapshot.version_conflict": (409, False),
        "policy_snapshot.candidate_conflict": (409, False),
        "policy_snapshot.idempotency_conflict": (409, False),
        "policy_snapshot.not_found": (404, False),
        "policy_snapshot.tenant_unavailable": (404, False),
        "policy_snapshot.rollback_target_invalid": (409, False),
        "policy_snapshot.no_changes": (409, False),
        "policy_snapshot.action_unsupported": (422, False),
        "policy_snapshot.variable_unsupported": (422, False),
        "policy_snapshot.route_target_invalid": (422, False),
        "policy_snapshot.graph_invalid": (503, True),
        "policy_snapshot.unavailable": (503, True),
        "gateway.policy_snapshot_unavailable": (503, True),
        "gateway.policy_snapshot_stale": (503, True),
        "gateway.policy_snapshot_expired": (503, True),
        "gateway.request_id_reused": (409, False),
        "gateway.request_log_unavailable": (503, True),
        "gateway.policy_denied": (403, False),
        "gateway.policy_evaluation_timeout": (403, False),
        "gateway.policy_context_missing": (403, False),
        "gateway.policy_context_unavailable": (503, True),
        "gateway.policy_route_unavailable": (503, True),
        "gateway.policy_action_unsupported": (403, False),
        "gateway.policy_mask_unsupported": (422, False),
        "gateway.policy_mask_unavailable": (503, True),
    }
    assert {
        code: (ERROR_SPECS[code].http_status, ERROR_SPECS[code].retryable)
        for code in expected
    } == expected
    for code, (status, retryable) in expected.items():
        diagnosis = diagnose_error(code, http_status=status)
        assert diagnosis.matched_by == "error_code"
        assert diagnosis.retryable is retryable
        assert "secret" not in diagnosis.safe_summary.lower()


def test_policy_snapshot_webhook_is_subscribable_and_payload_is_minimal() -> None:
    """Webhook：发布事件只携带版本/hash/计数/provenance，并拒绝原因正文和临时 event ID。"""

    assert "policy_snapshot.published" in WEBHOOK_SUBSCRIBABLE_EVENT_TYPES
    event = build_webhook_event(
        tenant_id="ten_a",
        event_type="policy_snapshot.published",
        event_id="evt_psevt_a",
        object_id="psnap_a",
        payload={
            "version": 1,
            "content_hash": "a" * 64,
            "policy_count": 0,
            "operation": "publish",
            "previous_version": None,
            "rollback_source_version": None,
        },
    )
    assert event.event_id == "evt_psevt_a"
    assert "change_reason" not in event.payload

    with pytest.raises(ValueError, match="webhook.payload_sensitive"):
        build_webhook_event(
            tenant_id="ten_a",
            event_type="policy_snapshot.published",
            event_id="evt_psevt_b",
            payload={**event.payload, "change_reason": "sensitive"},
        )
