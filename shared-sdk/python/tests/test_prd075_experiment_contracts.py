"""验证 PRD-075 共享分桶、配额、统计、RBAC、错误目录及 Webhook 的冻结合同。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from metahub_shared.experiments import (
    EXPERIMENT_ALLOCATION_ALGORITHM_VERSION,
    EXPERIMENT_BUCKET_COUNT,
    ExperimentAllocationPlan,
    ExperimentRequestFact,
    aggregate_experiment_arm,
    build_experiment_buckets,
    build_experiment_create_request_fingerprint,
    build_experiment_idempotency_key_hash,
    build_experiment_key_fingerprint,
    build_experiment_route_config_fingerprint,
    calculate_variant_targets,
    choose_experiment_variant,
)
from metahub_shared.rbac import (
    CATALOG_REVISION,
    PERMISSION_BY_CODE,
    PERMISSION_CATALOG,
    ROLE_GRANTABLE_PERMISSIONS,
    permission_matrix_sha256,
)
from metahub_shared.runtime.config import ConfigError, load_app_config
from metahub_shared.runtime.error_diagnosis import diagnose_error
from metahub_shared.runtime.errors import ERROR_SPECS
from metahub_shared.runtime.i18n import error_label
from metahub_shared.runtime.webhooks import (
    WEBHOOK_EVENT_TYPES,
    WEBHOOK_SUBSCRIBABLE_EVENT_TYPES,
    build_webhook_event,
)

# 测试仅使用明确标注为非真实凭据的固定材料，以验证 golden vector 且避免敏感值进入证据。
SECRET = "prd075-unit-secret-that-is-not-a-real-credential"
IDEMPOTENCY_KEY = "123e4567-e89b-42d3-a456-426614174000"


def _create_fingerprint(**overrides: object) -> str:
    """以规范创建请求生成幂等指纹，并允许单字段反例覆盖验证 exact body。"""

    values: dict[str, object] = {
        "name": "August quality comparison",
        "project_id": "project_a",
        "environment_id": "environment_a",
        "model_alias_id": "alias_a",
        "model_a_id": "model_a",
        "model_b_id": "model_b",
        "target_sample_count": 1000,
        "traffic_sample_bps": 2500,
        "model_a_allocation_bps": 5000,
        "sampling_duration_seconds": 86400,
        "feedback_collection_enabled": True,
        "feedback_window_seconds": 86400,
    }
    values.update(overrides)
    return build_experiment_create_request_fingerprint(IDEMPOTENCY_KEY, **values)


def _route_fingerprint(**overrides: object) -> str:
    """以安全合成模型和 Provider 配置生成路由指纹，不接触任何真实 Endpoint 或密钥。"""

    values: dict[str, object] = {
        "tenant_id": "tenant_a",
        "model_id": "model_a",
        "model_status": "active",
        "provider_id": "provider_a",
        "provider_code": "deepseek_official",
        "provider_status": "active",
        "provider_type": "deepseek",
        "provider_auth_type": "bearer",
        "provider_base_url": "https://provider.invalid/v1",
        "upstream_model_name": "deepseek-chat",
        "capabilities": {"chat": True, "stream": True},
        "context_window": 65536,
        "adapter_catalog_version": "provider-adapter-catalog-v1",
        "adapter_catalog_hash": "a" * 64,
        "adapter_spec_id": "adapter-spec-deepseek-v1",
        "adapter_version": "1.0.0",
    }
    values.update(overrides)
    # helper 只向断言返回不可逆摘要，测试密钥和敏感配置原值不会写入任何证据。
    return build_experiment_route_config_fingerprint(SECRET, **values)


def test_buckets_are_stable_domain_separated_and_tenant_scoped() -> None:
    """证明采样与选臂域隔离、golden vector 稳定且不同租户不能复用分桶。"""

    first = build_experiment_buckets(
        SECRET,
        tenant_id="tenant_a",
        experiment_id="exp_a",
        request_id="req_a",
    )
    assert first == build_experiment_buckets(
        SECRET,
        tenant_id="tenant_a",
        experiment_id="exp_a",
        request_id="req_a",
    )
    assert first.algorithm_version == EXPERIMENT_ALLOCATION_ALGORITHM_VERSION
    assert first.sample_bucket == 2469
    assert first.variant_bucket == 6035
    assert 0 <= first.sample_bucket < EXPERIMENT_BUCKET_COUNT
    assert 0 <= first.variant_bucket < EXPERIMENT_BUCKET_COUNT
    assert first.sample_bucket != first.variant_bucket
    assert first != build_experiment_buckets(
        SECRET,
        tenant_id="tenant_b",
        experiment_id="exp_a",
        request_id="req_a",
    )


def test_key_fingerprint_is_stable_scoped_and_not_secret() -> None:
    """证明部署密钥指纹按实验隔离、长度固定且不会直接暴露测试根密钥。"""

    fingerprint = build_experiment_key_fingerprint(
        SECRET,
        tenant_id="tenant_a",
        experiment_id="exp_a",
    )
    assert len(fingerprint) == 64
    assert fingerprint == "f66f85a4111bc03c96db344f31a9c07ef65ca3444b76cb6e78e791ce9ac893c4"
    assert SECRET not in fingerprint
    assert fingerprint != build_experiment_key_fingerprint(
        SECRET,
        tenant_id="tenant_a",
        experiment_id="exp_b",
    )


def test_create_idempotency_fingerprints_are_stable_and_auth_root_independent() -> None:
    """冻结创建幂等摘要并证明其按租户和正文隔离且不依赖可轮换 Auth root。"""

    key_hash = build_experiment_idempotency_key_hash(
        IDEMPOTENCY_KEY,
        tenant_id="tenant_a",
        actor_user_id="user_a",
    )
    request_fingerprint = _create_fingerprint()
    assert key_hash == "49b43872aedaba98b3e25028646c6be9b5df49aca17d8b6f0ace570853589b56"
    assert request_fingerprint == "4999e3447134d42a5c35c44a0bbb2d7ae6a7194e5979bf3d12b3480f413fccaf"
    assert key_hash != build_experiment_idempotency_key_hash(
        IDEMPOTENCY_KEY,
        tenant_id="tenant_b",
        actor_user_id="user_a",
    )
    assert request_fingerprint != _create_fingerprint(traffic_sample_bps=2501)
    assert SECRET not in key_hash + request_fingerprint


@pytest.mark.parametrize(
    "value",
    (
        "123e4567-e89b-12d3-a456-426614174000",
        "123E4567-E89B-42D3-A456-426614174000",
        "not-a-uuid",
    ),
)
def test_idempotency_fingerprints_reject_noncanonical_uuid4(value: str) -> None:
    """拒绝非 v4、大写或非 UUID 幂等键，避免一个语义对应多个持久化文本。"""

    with pytest.raises(ValueError, match="experiment.idempotency_key_invalid"):
        build_experiment_idempotency_key_hash(
            value,
            tenant_id="tenant_a",
            actor_user_id="user_a",
        )


def test_create_request_fingerprint_rejects_invalid_exact_body_combinations() -> None:
    """拒绝反馈开关矛盾和相同 A/B 模型，确保指纹只覆盖合法 exact body。"""

    with pytest.raises(ValueError, match="experiment.create_fingerprint_invalid"):
        _create_fingerprint(feedback_collection_enabled=False, feedback_window_seconds=60)
    with pytest.raises(ValueError, match="experiment.create_fingerprint_invalid"):
        _create_fingerprint(model_b_id="model_a")


def test_route_config_fingerprint_is_stable_sensitive_and_drift_aware() -> None:
    """证明路由指纹隐藏敏感 URL、规范化能力顺序并能检测执行配置漂移。"""

    fingerprint = _route_fingerprint()
    assert fingerprint == "8c552c15fc14ef0f86efc9661bee56f2e7902d0ac028db18542d35dcd2e7e841"
    assert len(fingerprint) == 64
    assert "provider.invalid" not in fingerprint
    assert fingerprint != _route_fingerprint(
        provider_base_url="https://replacement.invalid/v1"
    )
    assert fingerprint != _route_fingerprint(adapter_catalog_hash="b" * 64)
    assert fingerprint == _route_fingerprint(
        capabilities={"stream": True, "chat": True}
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("adapter_catalog_hash", "A" * 64),
        ("capabilities", {"chat": 1}),
        ("context_window", True),
        ("provider_base_url", "https://provider.invalid/\nsecret"),
    ),
)
def test_route_config_fingerprint_rejects_unsafe_values(field: str, value: object) -> None:
    """拒绝非规范摘要、宽松布尔、控制字符和错误数值类型进入路由指纹。"""

    with pytest.raises(ValueError, match="experiment.route_config_invalid"):
        _route_fingerprint(**{field: value})


def test_rbac_revision_11_freezes_experiment_permissions_and_role_hashes() -> None:
    """冻结 revision 11 的实验权限风险、默认角色授权与完整矩阵摘要。"""

    assert CATALOG_REVISION == 11
    assert len(PERMISSION_CATALOG) == 66
    assert PERMISSION_BY_CODE["experiment.read"].risk_level == "read"
    assert PERMISSION_BY_CODE["experiment.write"].risk_level == "critical"
    expected = {
        "admin": "33fe73e93c4b09ad6cb33367484de80652388fa03d390dd4e5c35fef066e0148",
        "developer": "71313d68fd5b18c754ab9927743ef6a772f42ae4b8e7167e81222f1a788f48cd",
        "ops_admin": "0f024ad0b30f6f44d26d92156c12506b4348b6412201cf9212b6e7183a313f1a",
    }
    for role, matrix_hash in expected.items():
        grants = ROLE_GRANTABLE_PERMISSIONS[role]
        assert {"experiment.read", "experiment.write"} <= grants
        assert permission_matrix_sha256(grants) == matrix_hash
    for role in ("viewer", "finance_admin", "security_admin"):
        assert "experiment.read" not in ROLE_GRANTABLE_PERMISSIONS[role]
        assert "experiment.write" not in ROLE_GRANTABLE_PERMISSIONS[role]


def test_experiment_error_catalog_statuses_labels_and_diagnosis_are_frozen() -> None:
    """核对实验统一错误码的 HTTP 状态、重试属性、双语标签和安全诊断分类。"""

    expected = {
        "experiment.input_invalid": (422, False),
        "experiment.query_invalid": (422, False),
        "experiment.idempotency_required": (422, False),
        "experiment.model_invalid": (422, False),
        "experiment.not_found": (404, False),
        "experiment.idempotency_conflict": (409, False),
        "experiment.scope_conflict": (409, False),
        "experiment.unavailable": (503, True),
        "experiment.statistics_unavailable": (503, True),
        "gateway.experiment_unavailable": (503, True),
        "gateway.experiment_variant_unavailable": (503, True),
        "gateway.experiment_execution_timeout": (504, True),
    }
    for code, (status, retryable) in expected.items():
        spec = ERROR_SPECS[code]
        assert (spec.http_status, spec.retryable) == (status, retryable)
        assert error_label(code, "zh-CN") != code
        assert error_label(code, "en-US") != code
    assert diagnose_error(
        "gateway.experiment_unavailable", http_status=503
    ).category == "gateway_experiment_routing"
    assert diagnose_error(
        "gateway.experiment_execution_timeout", http_status=504
    ).category == "gateway_experiment_execution"


@pytest.mark.parametrize(
    ("target", "bps", "a_target", "b_target"),
    ((2, 5000, 1, 1), (3, 5000, 2, 1), (10, 2500, 3, 7), (100_000, 9999, 99_990, 10)),
)
def test_variant_targets_use_integer_half_up(
    target: int,
    bps: int,
    a_target: int,
    b_target: int,
) -> None:
    """验证 A/B 目标使用纯整数 half-up，覆盖最小、奇数和大样本边界。"""

    plan = calculate_variant_targets(target, bps)
    assert (plan.model_a_target_count, plan.model_b_target_count) == (a_target, b_target)


@pytest.mark.parametrize("target,bps", ((2, 1), (2, 9999), (100_001, 5000), (3, 0), (3, 10_000)))
def test_variant_targets_reject_empty_or_invalid_arms(target: int, bps: int) -> None:
    """拒绝会产生空臂、越界样本或非法 bps 的实验配额输入。"""

    with pytest.raises(ValueError, match="experiment.allocation_invalid"):
        calculate_variant_targets(target, bps)


@pytest.mark.parametrize("bps", (2500, 5000, 7400))
def test_prefix_allocation_stays_bounded_and_hits_exact_target(bps: int) -> None:
    """逐样本证明前缀偏差不超过 floor/ceil 且满额精确命中最终配额。"""

    plan = calculate_variant_targets(101, bps)
    assigned = model_a = model_b = 0
    for index in range(plan.target_sample_count):
        allocation = choose_experiment_variant(
            plan,
            assigned_count=assigned,
            model_a_count=model_a,
            model_b_count=model_b,
            variant_bucket=(index * 7919) % EXPERIMENT_BUCKET_COUNT,
        )
        assigned = allocation.assigned_count_after
        model_a = allocation.model_a_count_after
        model_b = allocation.model_b_count_after
        numerator = assigned * plan.model_a_target_count
        lower = numerator // plan.target_sample_count
        upper = (numerator + plan.target_sample_count - 1) // plan.target_sample_count
        assert lower <= model_a <= upper
    assert (model_a, model_b) == (plan.model_a_target_count, plan.model_b_target_count)


def test_allocation_rejects_noncanonical_plan_and_corrupt_counts() -> None:
    """拒绝手工伪造配额和不闭合计数，防止损坏状态继续消耗线上样本。"""

    invalid_plan = ExperimentAllocationPlan(
        target_sample_count=10,
        model_a_allocation_bps=5000,
        model_a_target_count=4,
        model_b_target_count=6,
    )
    with pytest.raises(ValueError, match="experiment.allocation_state_invalid"):
        choose_experiment_variant(
            invalid_plan,
            assigned_count=0,
            model_a_count=0,
            model_b_count=0,
            variant_bucket=0,
        )

    plan = calculate_variant_targets(10, 5000)
    with pytest.raises(ValueError, match="experiment.allocation_state_invalid"):
        choose_experiment_variant(
            plan,
            assigned_count=3,
            model_a_count=1,
            model_b_count=1,
            variant_bucket=0,
        )


@pytest.mark.parametrize(
    ("assigned", "model_a", "model_b", "bucket", "variant", "reason"),
    (
        (0, 0, 0, 0, "a", "hmac_preferred"),
        (1, 0, 1, 9999, "a", "floor_forced"),
        (1, 1, 0, 0, "b", "ceiling_forced"),
    ),
)
def test_allocation_persists_exact_assignment_reason(
    assigned: int,
    model_a: int,
    model_b: int,
    bucket: int,
    variant: str,
    reason: str,
) -> None:
    """覆盖 HMAC 偏好与两种强制分配原因，冻结数据库可追踪 reason。"""

    allocation = choose_experiment_variant(
        calculate_variant_targets(10, 5000),
        assigned_count=assigned,
        model_a_count=model_a,
        model_b_count=model_b,
        variant_bucket=bucket,
    )
    assert allocation.variant == variant
    assert allocation.assignment_reason == reason


@pytest.mark.parametrize(
    "field,value",
    (("tenant_id", " tenant"), ("experiment_id", "exp\nvalue"), ("request_id", "响应")),
)
def test_bucket_identity_rejects_unsafe_values(field: str, value: str) -> None:
    """拒绝空白、控制字符和非 ASCII 身份，避免跨语言 canonical 分桶歧义。"""

    arguments = {
        "tenant_id": "tenant_a",
        "experiment_id": "exp_a",
        "request_id": "req_a",
    }
    arguments[field] = value
    with pytest.raises(ValueError, match="experiment.identity_invalid"):
        build_experiment_buckets(SECRET, **arguments)


def test_integer_statistics_keep_nullable_and_feedback_zero_semantics() -> None:
    """验证整数统计、unknown cost、nullable 指标及 PRD-049 反馈零分母兼容语义。"""

    facts = [
        ExperimentRequestFact(
            variant="a",
            gateway_outcome="success",
            exposure="exposed",
            quality_eligible=True,
            quality_succeeded=True,
            latency_ms=10,
            settlement_status="recorded",
            gross_cost_cny_micros=100,
            feedback_rating=5,
            feedback_accepted=True,
        ),
        ExperimentRequestFact(
            variant="a",
            gateway_outcome="failed",
            exposure="exposed",
            quality_eligible=True,
            quality_succeeded=False,
            latency_ms=30,
            settlement_status="recorded",
            gross_cost_cny_micros=None,
            feedback_rating=1,
            feedback_accepted=False,
        ),
        ExperimentRequestFact(
            variant="a",
            gateway_outcome="unresolved",
            exposure="unknown",
            quality_eligible=False,
            quality_succeeded=None,
            latency_ms=None,
            settlement_status="pending",
            gross_cost_cny_micros=None,
        ),
    ]
    result = aggregate_experiment_arm("a", facts)
    assert result.assigned_count == 3
    assert result.gateway_success_rate_bps == 5_000
    assert result.quality_success_rate_bps == 5_000
    assert result.average_latency_micros == 20_000
    assert result.p95_latency_ms == 30
    assert result.gross_cost_cny_micros == 100
    assert result.cost_known_count == 1
    assert result.cost_unknown_count == 2
    assert result.average_rating_milli == 3_000
    assert result.adoption_rate_bps == 5_000

    empty = aggregate_experiment_arm("b", [])
    assert empty.gateway_success_rate_bps is None
    assert empty.quality_success_rate_bps is None
    assert empty.average_latency_micros is None
    assert empty.p95_latency_ms is None
    assert empty.average_cost_cny_micros is None
    # PRD-049 wire 兼容要求 feedback 零分母明确为 0，而不是 null。
    assert empty.average_rating_milli == 0
    assert empty.adoption_rate_bps == 0


def test_statistics_reject_owner_matrix_drift_and_signed_64_overflow() -> None:
    """拒绝暴露与质量矩阵漂移以及 gross cost 汇总越过 signed-64 上界。"""

    invalid = ExperimentRequestFact(
        variant="a",
        gateway_outcome="failed",
        exposure="not_exposed",
        quality_eligible=True,
        quality_succeeded=False,
        latency_ms=1,
        settlement_status="not_exposed",
        gross_cost_cny_micros=None,
    )
    with pytest.raises(ValueError, match="experiment.statistics_invalid"):
        aggregate_experiment_arm("a", [invalid])

    costly = ExperimentRequestFact(
        variant="a",
        gateway_outcome="success",
        exposure="exposed",
        quality_eligible=False,
        quality_succeeded=None,
        latency_ms=None,
        settlement_status="recorded",
        gross_cost_cny_micros=9_223_372_036_854_775_807,
    )
    with pytest.raises(ValueError, match="experiment.statistics_invalid"):
        aggregate_experiment_arm("a", [costly, costly])


def _experiment_completed_payload() -> dict[str, object]:
    """构造不含名称、正文或请求明细的 experiment.completed 安全 exact payload。"""

    return {
        "schema_version": 1,
        "experiment_id": "exp_0123456789abcdef0123456789abcdef",
        "project_id": "project_a",
        "environment_id": "environment_a",
        "model_alias_id": "alias_a",
        "model_a_id": "model_a",
        "model_b_id": "model_b",
        "status": "completed",
        "completion_reason": "target_reached",
        "assigned_count": 10,
        "model_a_assigned_count": 5,
        "model_b_assigned_count": 5,
        "gateway_success_count": 8,
        "gateway_failure_count": 1,
        "unresolved_count": 1,
        "cost_unknown_count": 1,
        "feedback_count": 2,
        "facts_version": 37,
        "facts_through_at": "2026-08-09T01:02:03Z",
    }


def test_experiment_completed_webhook_is_exact_and_source_bound() -> None:
    """验证完成事件 allowlist、source owner、exact 字段及计数闭合失败关闭。"""

    assert "experiment.completed" in WEBHOOK_EVENT_TYPES
    assert "experiment.completed" in WEBHOOK_SUBSCRIBABLE_EVENT_TYPES
    event = build_webhook_event(
        tenant_id="tenant_a",
        event_type="experiment.completed",
        payload=_experiment_completed_payload(),
        actor_id="user_a",
        object_id="exp_0123456789abcdef0123456789abcdef",
        trace_id="trace_create_a",
        source_request_id="request_create_a",
        event_id="exev_0123456789abcdef0123456789abcdef",
        timestamp=datetime(2026, 8, 9, 1, 2, 3, tzinfo=UTC),
    )
    assert event.event_type == "experiment.completed"
    assert event.payload["assigned_count"] == 10

    extra = _experiment_completed_payload()
    extra["name"] = "must-not-leave-control-plane"
    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        build_webhook_event(
            tenant_id="tenant_a",
            event_type="experiment.completed",
            payload=extra,
            actor_id="user_a",
            object_id="exp_0123456789abcdef0123456789abcdef",
            trace_id="trace_create_a",
            source_request_id="request_create_a",
            event_id="exev_0123456789abcdef0123456789abcdef",
        )

    inconsistent = _experiment_completed_payload()
    inconsistent["unresolved_count"] = 2
    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        build_webhook_event(
            tenant_id="tenant_a",
            event_type="experiment.completed",
            payload=inconsistent,
            actor_id="user_a",
            object_id="exp_0123456789abcdef0123456789abcdef",
            trace_id="trace_create_a",
            source_request_id="request_create_a",
            event_id="exev_0123456789abcdef0123456789abcdef",
        )


def test_experiment_runtime_config_defaults_and_heartbeat_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """冻结实验调度和租约默认值，并验证 heartbeat 必须严格小于租约三分之一。"""

    monkeypatch.delenv("METAHUB_CONFIG_FILE", raising=False)
    monkeypatch.setenv("METAHUB_ENV", "dev")
    config = load_app_config("control-plane-api")
    assert config.experiment_scheduler_interval_seconds == 5
    assert config.experiment_finalizer_lease_seconds == 90
    assert config.experiment_execution_lease_seconds == 90
    assert config.experiment_execution_heartbeat_seconds == 20
    assert config.experiment_scheduler_batch_limit == 20
    assert config.experiment_execution_max_seconds == 3600
    assert config.experiment_settlement_grace_seconds == 3600

    monkeypatch.setenv("METAHUB_EXPERIMENT_EXECUTION_HEARTBEAT_SECONDS", "30")
    with pytest.raises(ConfigError) as captured:
        load_app_config("gateway-data-plane")
    assert captured.value.code == "infra.config_invalid"
