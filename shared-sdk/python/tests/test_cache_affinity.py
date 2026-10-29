"""PRD-050 shared canonical、路由偏好、配置隐私与 Webhook 合同专项测试。"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from metahub_shared.runtime.cache_affinity import (
    MAX_CACHE_AFFINITY_CANONICAL_NODES,
    MAX_CACHE_AFFINITY_PREFIX_CHARS,
    REASON_CACHE_AFFINITY_PENDING_PREFERRED,
    REASON_CACHE_AFFINITY_WARM_PREFERRED,
    apply_cache_affinity_endpoint_preference,
    build_cache_affinity_key_digest,
    build_cache_affinity_request_context,
    build_cache_affinity_scope_digest,
    build_endpoint_revision_digest,
    build_prompt_fingerprint,
)
from metahub_shared.runtime.config import (
    CACHE_AFFINITY_EXAMPLE_HMAC_SECRET,
    DEFAULT_CACHE_AFFINITY_HMAC_SECRET,
    ConfigError,
    ConfigValue,
    _validate_cache_affinity_hmac_secret,
    build_config_summary,
    load_app_config,
)
from metahub_shared.runtime.errors import ERROR_SPECS
from metahub_shared.runtime.routing_policy import (
    QUALITY_COST_FORMULA_VERSION,
    REASON_QUALITY_BELOW_FLOOR,
    REASON_STANDBY,
    QualityCostConfig,
    RoutingCandidateInput,
    RoutingCandidateResult,
    RoutingSelectionResult,
    TokenEstimate,
)
from metahub_shared.runtime.webhooks import build_webhook_event

# 测试密钥刻意与运行默认值不同，证明黄金摘要没有隐式依赖本地 .env。
TEST_SECRET_A = "a" * 32
TEST_SECRET_B = "b" * 32


def _candidate(
    model_id: str,
    provider_id: str,
    *,
    health_state: str = "healthy",
    health_fresh: bool = True,
) -> RoutingCandidateInput:
    """构造已通过基础资格检查的安全路由候选。"""

    return RoutingCandidateInput(
        candidate_role="default" if model_id.endswith("a") else "fallback",
        configured_priority=0 if model_id.endswith("a") else 1,
        model_id=model_id,
        provider_id=provider_id,
        model_status="active",
        provider_status="active",
        health_state=health_state,  # type: ignore[arg-type]
        health_fresh=health_fresh,
        keypool_state="available",
    )


def _selection(
    mode: str,
    *,
    preferred_health: tuple[str, bool] = ("healthy", True),
    selected_quality: str = "ready",
    preferred_quality: str = "ready",
    preferred_rejected: bool = False,
) -> RoutingSelectionResult:
    """构造带完整价格、质量、Token 和公式证据的两候选结果。"""

    quality_config = QualityCostConfig()
    selected = RoutingCandidateResult(
        candidate=_candidate("model_a", "provider_a"),
        rank=1,
        status="selected",
        reason_code="routing.base_selected",
        price_available=True,
        price_source="price_plan",
        price_schema_version="price-a-v3",
        currency="CNY",
        estimated_cost_amount=Decimal("0.123456789"),
        quality_evidence_state=selected_quality,  # type: ignore[arg-type]
        model_quality_score_id="mqs_a",
        quality_statistics_version=11,
        quality_feedback_version=7,
        quality_sample_count=100,
        quality_success_count=98,
        quality_error_count=2,
        manual_quality_bps=9000,
        average_latency_ms=Decimal("123.456"),
        quality_component_bps=9010,
        cost_component_bps=8000,
        latency_component_bps=7000,
        weighted_score_bps=8300,
    )
    preferred = RoutingCandidateResult(
        candidate=_candidate(
            "model_b",
            "provider_b",
            health_state=preferred_health[0],
            health_fresh=preferred_health[1],
        ),
        rank=None if preferred_rejected else 2,
        status="rejected" if preferred_rejected else "standby",
        reason_code=REASON_QUALITY_BELOW_FLOOR if preferred_rejected else REASON_STANDBY,
        price_available=True,
        price_source="model_catalog",
        price_schema_version="catalog-b-v8",
        currency="CNY",
        estimated_cost_amount=Decimal("0.111111111"),
        quality_evidence_state=preferred_quality,  # type: ignore[arg-type]
        model_quality_score_id="mqs_b",
        quality_statistics_version=13,
        quality_feedback_version=9,
        quality_sample_count=120,
        quality_success_count=117,
        quality_error_count=3,
        manual_quality_bps=None,
        average_latency_ms=Decimal("98.765"),
        quality_component_bps=8900,
        cost_component_bps=8400,
        latency_component_bps=7800,
        weighted_score_bps=8500,
    )
    return RoutingSelectionResult(
        routing_mode=mode,  # type: ignore[arg-type]
        status="selected",
        selection_reason="routing.base_selected",
        selected_model_id="model_a",
        selected_provider_id="provider_a",
        candidate_results=(selected, preferred),
        eligible_candidate_count=1 if preferred_rejected else 2,
        token_estimate=TokenEstimate(input_tokens=4321, output_tokens=321, truncated=False),
        cost_comparison_performed=True,
        quality_formula_version=(QUALITY_COST_FORMULA_VERSION if mode == "quality_cost" else None),
        quality_config=quality_config if mode == "quality_cost" else None,
        quality_scoring_performed=mode == "quality_cost",
    )


def _runtime_payload(
    *, reason: str = "binding_warm_reused", estimated_hit: bool = True
) -> dict[str, object]:
    """构造运行时 CacheAffinity 选择事件的精确安全 payload 测试对象。"""

    return {
        "trigger": "request_selected",
        "request_id": "req_cache_001",
        "cache_affinity_selection_id": "cas_" + "1" * 32,
        "cache_affinity_id": "caf_" + "2" * 32,
        "cache_affinity_version": 3,
        "model_alias_id": "ma_cache",
        "model_endpoint_id": "model_b",
        "selected_provider_id": "provider_b",
        "target_kind": "managed",
        "selection_reason": reason,
        "estimated_hit": estimated_hit,
    }


def _configuration_payload() -> dict[str, object]:
    """构造管理员创建 CacheAffinity 后的精确安全配置事件测试对象。"""

    return {
        "trigger": "configuration_changed",
        "request_id": "req_cache_config_001",
        "cache_affinity_id": "caf_" + "3" * 32,
        "cache_affinity_version": 1,
        "model_alias_id": "ma_cache",
        "change_kind": "created",
        "changed_fields": ["status", "data_class", "minimum_prefix_chars", "ttl_seconds"],
        "status": "enabled",
    }


def test_prompt_fingerprint_has_stable_golden_and_ignores_suffix_sampling() -> None:
    """黄金摘要固定 canonical 版本，user 后缀与采样参数不改变固定前缀域。"""

    payload = {
        "messages": [
            {"role": "system", "content": "企业安全规则"},
            {
                "role": "developer",
                "name": "guard",
                "content": [{"type": "text", "text": "不得输出密钥"}],
            },
            {"role": "user", "content": "问题甲"},
        ],
        "temperature": 0.1,
        "tools": [
            {"function": {"parameters": {"b": 2, "a": 1}, "name": "lookup"}, "type": "function"}
        ],
        "parallel_tool_calls": False,
    }
    digest = build_prompt_fingerprint(
        TEST_SECRET_A,
        request_kind="chat",
        payload=payload,
        minimum_prefix_chars=6,
    )
    assert digest == "85119a81c4e6a3f7d2dc77f30ca514736f90215b5ca66478558116eb4a5149d2"

    changed_suffix = dict(payload)
    changed_suffix["temperature"] = 2
    changed_suffix["messages"] = [*payload["messages"][:-1], {"role": "user", "content": "问题乙"}]
    assert (
        build_prompt_fingerprint(
            TEST_SECRET_A,
            request_kind="chat",
            payload=changed_suffix,
            minimum_prefix_chars=6,
        )
        == digest
    )


def test_prompt_canonical_preserves_null_missing_order_and_secret_domains() -> None:
    """canonical 保留结构与 Unicode 精确语义，仅忽略对象键的排列顺序。"""

    base = {
        "messages": [{"role": "system", "content": "x" * 12}],
        "tools": [{"type": "function", "function": {"name": "a", "parameters": {"z": 1, "a": 2}}}],
    }
    reordered_keys = {
        "tools": [{"function": {"parameters": {"a": 2, "z": 1}, "name": "a"}, "type": "function"}],
        "messages": base["messages"],
    }
    digest = build_prompt_fingerprint(
        TEST_SECRET_A, request_kind="chat", payload=base, minimum_prefix_chars=12
    )
    assert digest == build_prompt_fingerprint(
        TEST_SECRET_A,
        request_kind="chat",
        payload=reordered_keys,
        minimum_prefix_chars=12,
    )
    with_null = dict(base, tool_choice=None)
    assert (
        build_prompt_fingerprint(
            TEST_SECRET_A, request_kind="chat", payload=with_null, minimum_prefix_chars=12
        )
        != digest
    )
    assert (
        build_prompt_fingerprint(
            TEST_SECRET_B, request_kind="chat", payload=base, minimum_prefix_chars=12
        )
        != digest
    )

    # 每组左右输入都分别满足合法 Prompt 合同，且只改变待验证的单一语义边界；这可防止
    # canonical 实现误把不同角色、消息或文本合并到同一上游缓存域。
    fingerprint_pairs = (
        (
            "role",
            {"messages": [{"role": "system", "content": "固定系统规则文本"}]},
            {"messages": [{"role": "developer", "content": "固定系统规则文本"}]},
        ),
        (
            "message_boundary",
            {"messages": [{"role": "system", "content": "固定系统规则文本"}]},
            {
                "messages": [
                    {"role": "system", "content": "固定系统"},
                    {"role": "system", "content": "规则文本"},
                ]
            },
        ),
        (
            "part_type",
            {
                "messages": [
                    {
                        "role": "system",
                        "content": [{"type": "text", "text": "固定系统规则文本"}],
                    }
                ]
            },
            {
                "messages": [
                    {
                        "role": "system",
                        "content": [{"type": "input_text", "text": "固定系统规则文本"}],
                    }
                ]
            },
        ),
        (
            "text",
            {"messages": [{"role": "system", "content": "固定系统规则文本甲"}]},
            {"messages": [{"role": "system", "content": "固定系统规则文本乙"}]},
        ),
        (
            "unicode",
            {"messages": [{"role": "system", "content": "固定系统规则 Caf\u00e9 文本"}]},
            {"messages": [{"role": "system", "content": "固定系统规则 Cafe\u0301 文本"}]},
        ),
    )
    for contract_name, left_payload, right_payload in fingerprint_pairs:
        left_digest = build_prompt_fingerprint(
            TEST_SECRET_A,
            request_kind="chat",
            payload=left_payload,
            minimum_prefix_chars=4,
        )
        right_digest = build_prompt_fingerprint(
            TEST_SECRET_A,
            request_kind="chat",
            payload=right_payload,
            minimum_prefix_chars=4,
        )
        assert left_digest is not None, contract_name
        assert right_digest is not None, contract_name
        assert left_digest != right_digest, contract_name


@pytest.mark.parametrize(
    "payload",
    [
        {"messages": [{"role": "system", "content": "x" * 12, "unexpected": True}]},
        {"messages": [{"role": "system", "content": [{"type": "image_url", "text": "x" * 12}]}]},
        {
            "messages": [
                {"role": "system", "content": [{"type": "text", "text": "x" * 12, "extra": 1}]}
            ]
        },
        {"messages": [{"role": "system", "content": "x" * 12}], "tools": object()},
        {"messages": [{"role": "system", "content": "bad\x00text"}]},
        {"messages": [{"role": "system", "content": "x" * 12}], "response_format": float("nan")},
        {"messages": [{"role": [], "content": "x" * 12}]},
        {"messages": [{"role": "system", "content": [{"type": [], "text": "x" * 12}]}]},
        {"messages": [{"role": "system", "content": "x" * 11 + "\n"}]},
    ],
)
def test_prompt_fingerprint_closes_on_ambiguous_or_non_json_structure(
    payload: dict[str, object],
) -> None:
    """未知 part、extra key、控制字符和非标准 JSON 值均关闭亲和而不抛正文。"""

    assert (
        build_prompt_fingerprint(
            TEST_SECRET_A,
            request_kind="chat",
            payload=payload,
            minimum_prefix_chars=12,
        )
        is None
    )


def test_prompt_fingerprint_enforces_length_depth_node_tool_and_unicode_bounds() -> None:
    """字符、深度、节点、tools、UTF-8 和孤立 surrogate 六类资源边界均失败关闭。"""

    assert (
        build_prompt_fingerprint(
            TEST_SECRET_A,
            request_kind="embeddings",
            payload={"messages": [{"role": "system", "content": "x" * 12}]},
            minimum_prefix_chars=12,
        )
        is None
    )
    assert (
        build_prompt_fingerprint(
            TEST_SECRET_A,
            request_kind="chat",
            payload={
                "messages": [
                    {"role": "system", "content": "x" * (MAX_CACHE_AFFINITY_PREFIX_CHARS + 1)}
                ]
            },
            minimum_prefix_chars=12,
        )
        is None
    )
    assert (
        build_prompt_fingerprint(
            TEST_SECRET_A,
            request_kind="chat",
            payload={"messages": [{"role": "system", "content": "\ud800" * 12}]},
            minimum_prefix_chars=12,
        )
        is None
    )
    deep: object = "leaf"
    for _ in range(20):
        deep = {"next": deep}
    assert (
        build_prompt_fingerprint(
            TEST_SECRET_A,
            request_kind="chat",
            payload={"messages": [{"role": "system", "content": "x" * 12}], "tool_choice": deep},
            minimum_prefix_chars=12,
        )
        is None
    )
    assert (
        build_prompt_fingerprint(
            TEST_SECRET_A,
            request_kind="chat",
            payload={
                "messages": [{"role": "system", "content": "x" * 12}],
                "response_format": [0] * MAX_CACHE_AFFINITY_CANONICAL_NODES,
            },
            minimum_prefix_chars=12,
        )
        is None
    )
    assert (
        build_prompt_fingerprint(
            TEST_SECRET_A,
            request_kind="chat",
            payload={"messages": [{"role": "system", "content": "x" * 12}], "tools": [{}] * 129},
            minimum_prefix_chars=12,
        )
        is None
    )
    assert (
        build_prompt_fingerprint(
            TEST_SECRET_A,
            request_kind="chat",
            payload={
                "messages": [{"role": "system", "content": "😀" * MAX_CACHE_AFFINITY_PREFIX_CHARS}]
            },
            minimum_prefix_chars=12,
        )
        is None
    )


def test_scope_endpoint_key_digests_are_isolated_and_url_is_normalized() -> None:
    """四类 HMAC 不可互换，租户/端点变化隔离，等价 base URL 得到同 revision。"""

    prompt = build_prompt_fingerprint(
        TEST_SECRET_A,
        request_kind="chat",
        payload={"messages": [{"role": "system", "content": "x" * 12}]},
        minimum_prefix_chars=12,
    )
    assert prompt is not None
    scope = build_cache_affinity_scope_digest(
        TEST_SECRET_A,
        tenant_id="tenant_a",
        virtual_key_id="vk_a",
        data_class="internal",
        prompt_fingerprint=prompt,
    )
    other_tenant_scope = build_cache_affinity_scope_digest(
        TEST_SECRET_A,
        tenant_id="tenant_b",
        virtual_key_id="vk_a",
        data_class="internal",
        prompt_fingerprint=prompt,
    )
    assert scope != prompt != other_tenant_scope
    endpoint = build_endpoint_revision_digest(
        TEST_SECRET_A,
        tenant_id="tenant_a",
        model_endpoint_id="model_a",
        provider_id="provider_a",
        provider_type="openai_compatible",
        auth_type="bearer",
        base_url="HTTPS://EXAMPLE.COM:443/v1/",
        provider_model_id="upstream-model",
    )
    normalized_endpoint = build_endpoint_revision_digest(
        TEST_SECRET_A,
        tenant_id="tenant_a",
        model_endpoint_id="model_a",
        provider_id="provider_a",
        provider_type="openai_compatible",
        auth_type="bearer",
        base_url="https://example.com/v1",
        provider_model_id="upstream-model",
    )
    assert endpoint == normalized_endpoint
    key_digest = build_cache_affinity_key_digest(
        TEST_SECRET_A,
        affinity_scope_digest=scope,
        model_endpoint_id="model_a",
        endpoint_revision_digest=endpoint,
    )
    assert len({prompt, scope, endpoint, key_digest}) == 4


def test_request_context_requires_all_dimensions_and_hides_sensitive_repr() -> None:
    """隔离维度缺失或为空时关闭亲和，完整上下文保持 repr/asdict 隐私边界。"""

    payload = {"messages": [{"role": "system", "content": "固定系统规则文本"}]}
    valid_dimensions: dict[str, str | None] = {
        "tenant_id": "tenant_a",
        "virtual_key_id": "vk_a",
        "data_class": "internal",
    }
    # None 表示认证或服务端分类未提供，空字符串表示上游提供了无效值；两种情况都必须
    # 失败关闭，不能省略该维度后构造范围更宽、可能跨租户或跨 Key 的 Binding。
    for dimension_name in ("tenant_id", "virtual_key_id", "data_class"):
        for invalid_value in (None, ""):
            dimensions = dict(valid_dimensions)
            dimensions[dimension_name] = invalid_value
            assert (
                build_cache_affinity_request_context(
                    TEST_SECRET_A,
                    tenant_id=dimensions["tenant_id"],
                    virtual_key_id=dimensions["virtual_key_id"],
                    data_class=dimensions["data_class"],
                    request_kind="chat",
                    payload=payload,
                    minimum_prefix_chars=4,
                )
                is None
            ), (dimension_name, invalid_value)
    context = build_cache_affinity_request_context(
        TEST_SECRET_A,
        tenant_id="tenant_a",
        virtual_key_id="vk_a",
        data_class="internal",
        request_kind="chat",
        payload=payload,
        minimum_prefix_chars=4,
    )
    assert context is not None
    assert repr(context) == "CacheAffinityRequestContext()"
    # asdict 仍是内部显式操作，所以调用方必须只把该结构用于租户限定数据库参数，不能外发。
    assert set(asdict(context)) == {
        "tenant_id",
        "virtual_key_id",
        "data_class",
        "prompt_fingerprint",
        "affinity_scope_digest",
    }


@pytest.mark.parametrize("mode", ["cost_first", "availability_first", "quality_cost"])
def test_dynamic_modes_reorder_with_warm_reason_and_preserve_all_evidence(mode: str) -> None:
    """三种动态模式同层重排，并逐字段保留除 rank/status/reason 外的冻结证据。"""

    base = _selection(mode)
    outcome = apply_cache_affinity_endpoint_preference(
        base,
        preferred_model_id="model_b",
        preferred_provider_id="provider_b",
        preference_state="warm",
    )
    assert outcome.action == "reordered"
    assert outcome.selection.selection_reason == REASON_CACHE_AFFINITY_WARM_PREFERRED
    assert outcome.selection.selected_model_id == "model_b"
    assert [item.candidate.model_id for item in outcome.selection.candidate_results] == [
        "model_a",
        "model_b",
    ]
    assert outcome.selection.candidate_results[0].reason_code == REASON_STANDBY
    assert (
        outcome.selection.candidate_results[1].reason_code == REASON_CACHE_AFFINITY_WARM_PREFERRED
    )
    assert outcome.selection.token_estimate is base.token_estimate
    assert outcome.selection.quality_config is base.quality_config
    assert outcome.selection.cost_comparison_performed == base.cost_comparison_performed
    assert outcome.selection.quality_scoring_performed == base.quality_scoring_performed
    for before, after in zip(
        base.candidate_results, outcome.selection.candidate_results, strict=True
    ):
        before_values = (
            before.__dict__
            if hasattr(before, "__dict__")
            else {
                field_name: getattr(before, field_name)
                for field_name in before.__dataclass_fields__
            }
        )
        after_values = (
            after.__dict__
            if hasattr(after, "__dict__")
            else {
                field_name: getattr(after, field_name) for field_name in after.__dataclass_fields__
            }
        )
        for mutable_field in ("rank", "status", "reason_code"):
            before_values.pop(mutable_field)
            after_values.pop(mutable_field)
        assert after_values == before_values


def test_fixed_rejected_health_and_quality_tiers_cannot_be_overridden() -> None:
    """fixed、rejected、较弱健康层和 cold 质量层四类硬约束都返回 tier_blocked。"""

    cases = (
        _selection("fixed"),
        _selection("cost_first", preferred_rejected=True),
        _selection("availability_first", preferred_health=("unknown", False)),
        _selection("quality_cost", selected_quality="ready", preferred_quality="cold"),
    )
    for base in cases:
        outcome = apply_cache_affinity_endpoint_preference(
            base,
            preferred_model_id="model_b",
            preferred_provider_id="provider_b",
            preference_state="pending",
        )
        assert outcome.action == "tier_blocked"
        assert outcome.selection is base
        assert outcome.selection.selection_reason == "routing.base_selected"


def test_pending_reorder_and_already_selected_actions_have_distinct_reason_semantics() -> None:
    """实际 pending 重排写亲和原因，原本已选择时保留基础 reason 防止虚构改序。"""

    base = _selection("cost_first")
    pending = apply_cache_affinity_endpoint_preference(
        base,
        preferred_model_id="model_b",
        preferred_provider_id="provider_b",
        preference_state="pending",
    )
    assert pending.selection.selection_reason == REASON_CACHE_AFFINITY_PENDING_PREFERRED
    already = apply_cache_affinity_endpoint_preference(
        base,
        preferred_model_id="model_a",
        preferred_provider_id="provider_a",
        preference_state="warm",
    )
    assert already.action == "already_selected"
    assert already.selection is base
    assert already.selection.selection_reason == "routing.base_selected"


def test_cache_affinity_config_is_masked_and_excluded_from_repr_and_dump(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """开发默认密钥可运行，但配置摘要、repr 和 model_dump 绝不出现明文。"""

    monkeypatch.chdir(tmp_path)
    for key in list(__import__("os").environ):
        if key.startswith("METAHUB_"):
            monkeypatch.delenv(key, raising=False)
    config = load_app_config("gateway-data-plane")
    assert config.cache_affinity_hmac_secret == DEFAULT_CACHE_AFFINITY_HMAC_SECRET
    assert DEFAULT_CACHE_AFFINITY_HMAC_SECRET not in repr(config)
    assert "cache_affinity_hmac_secret" not in config.model_dump()
    summary = build_config_summary(config, request_id="req_config")
    item = next(item for item in summary.items if item.key == "METAHUB_CACHE_AFFINITY_HMAC_SECRET")
    assert item.status == "masked"
    assert item.value_preview == "***MASKED***"


@pytest.mark.parametrize(
    ("source", "secret", "auth_secret"),
    [
        ("default", DEFAULT_CACHE_AFFINITY_HMAC_SECRET, "z" * 32),
        ("env", "short", "z" * 32),
        ("env", "z" * 32, "z" * 32),
        ("env", CACHE_AFFINITY_EXAMPLE_HMAC_SECRET, "z" * 32),
    ],
)
def test_prod_gateway_rejects_default_short_shared_and_example_secrets(
    source: str, secret: str, auth_secret: str
) -> None:
    """生产 Gateway 拒绝默认、短值、登录共钥和模板样例四类不安全配置。"""

    with pytest.raises(ConfigError) as exc_info:
        _validate_cache_affinity_hmac_secret(
            service_name="gateway-data-plane",
            environment="prod",
            values={"METAHUB_CACHE_AFFINITY_HMAC_SECRET": ConfigValue(secret, source)},  # type: ignore[arg-type]
            cache_affinity_hmac_secret=secret,
            auth_token_secret=auth_secret,
        )
    assert exc_info.value.code == "infra.config_invalid"
    assert secret not in exc_info.value.message


def test_prod_gateway_accepts_explicit_independent_hmac_secret() -> None:
    """生产 Gateway 接受显式、足长且与登录密钥分域的 Prompt HMAC Secret。"""

    _validate_cache_affinity_hmac_secret(
        service_name="gateway-data-plane",
        environment="prod",
        values={"METAHUB_CACHE_AFFINITY_HMAC_SECRET": ConfigValue("c" * 40, "env")},
        cache_affinity_hmac_secret="c" * 40,
        auth_token_secret="d" * 40,
    )


def test_control_service_does_not_require_prompt_hmac_secret_validation() -> None:
    """Control 不计算 Prompt 指纹，生产启动不应被迫持有 Gateway 专属高熵密钥。"""

    _validate_cache_affinity_hmac_secret(
        service_name="control-plane-api",
        environment="prod",
        values={},
        cache_affinity_hmac_secret="",
        auth_token_secret="",
    )


def test_error_registry_contains_all_cache_affinity_contracts() -> None:
    """Control/Gateway 六个错误码的 HTTP 与 retryable 合同完整注册。"""

    expected = {
        "cache_affinity.version_conflict": (409, False),
        "cache_affinity.request_conflict": (409, False),
        "cache_affinity.input_invalid": (422, False),
        "cache_affinity.update_failed": (503, True),
        "gateway.cache_affinity_unavailable": (503, True),
        "gateway.execution_interrupted": (503, True),
    }
    assert {
        code: (ERROR_SPECS[code].http_status, ERROR_SPECS[code].retryable) for code in expected
    } == expected


def test_runtime_cache_affinity_webhook_requires_selection_identity_and_no_actor() -> None:
    """runtime 事件绑定 cas_ Selection、caf_ 对象和 trace，warm 才能声明预计命中。"""

    payload = _runtime_payload()
    event = build_webhook_event(
        tenant_id="tenant_a",
        event_type="cache.affinity_selected",
        payload=payload,
        event_id=payload["cache_affinity_selection_id"],  # type: ignore[arg-type]
        object_id=payload["cache_affinity_id"],  # type: ignore[arg-type]
        trace_id="trace_cache_001",
        timestamp=datetime(2026, 7, 20, tzinfo=UTC),
    )
    assert event.event_id == payload["cache_affinity_selection_id"]
    assert event.actor_id is None
    assert event.payload["estimated_hit"] is True

    invalid_target = _runtime_payload()
    invalid_target["target_kind"] = []
    invalid_reason = _runtime_payload()
    invalid_reason["selection_reason"] = []
    invalid_cases = [
        {"event_id": "cas_" + "9" * 32, "actor_id": None, "payload": payload},
        {
            "event_id": payload["cache_affinity_selection_id"],
            "actor_id": "user_a",
            "payload": payload,
        },
        {
            "event_id": payload["cache_affinity_selection_id"],
            "actor_id": None,
            "payload": _runtime_payload(reason="binding_created", estimated_hit=True),
        },
        {
            "event_id": payload["cache_affinity_selection_id"],
            "actor_id": None,
            "payload": invalid_target,
        },
        {
            "event_id": payload["cache_affinity_selection_id"],
            "actor_id": None,
            "payload": invalid_reason,
        },
    ]
    for case in invalid_cases:
        with pytest.raises(ValueError, match="webhook.payload_invalid"):
            build_webhook_event(
                tenant_id="tenant_a",
                event_type="cache.affinity_selected",
                payload=case["payload"],  # type: ignore[arg-type]
                event_id=case["event_id"],  # type: ignore[arg-type]
                actor_id=case["actor_id"],  # type: ignore[arg-type]
                object_id=payload["cache_affinity_id"],  # type: ignore[arg-type]
                trace_id="trace_cache_001",
            )


def test_configuration_cache_affinity_webhook_requires_actor_stable_fields_and_exact_envelope() -> (
    None
):
    """配置事件绑定 cae_、可信 actor、caf_ 对象并拒绝乱序、缺字段和敏感扩展。"""

    payload = _configuration_payload()
    event = build_webhook_event(
        tenant_id="tenant_a",
        event_type="cache.affinity_selected",
        payload=payload,
        event_id="cae_" + "4" * 32,
        actor_id="user_admin",
        object_id=payload["cache_affinity_id"],  # type: ignore[arg-type]
        trace_id="trace_cache_config_001",
    )
    assert event.actor_id == "user_admin"
    assert event.payload["change_kind"] == "created"

    for mutate in (
        lambda item: item.update({"changed_fields": ["ttl_seconds", "status"]}),
        lambda item: item.update({"status": []}),
        lambda item: item.pop("status"),
        lambda item: item.update({"prompt": "forbidden"}),
    ):
        invalid = _configuration_payload()
        mutate(invalid)
        expected_error = (
            "webhook.payload_sensitive" if "prompt" in invalid else "webhook.payload_invalid"
        )
        with pytest.raises(ValueError, match=expected_error):
            build_webhook_event(
                tenant_id="tenant_a",
                event_type="cache.affinity_selected",
                payload=invalid,
                event_id="cae_" + "4" * 32,
                actor_id="user_admin",
                object_id=payload["cache_affinity_id"],  # type: ignore[arg-type]
                trace_id="trace_cache_config_001",
            )

    with pytest.raises(ValueError, match="webhook.payload_invalid"):
        build_webhook_event(
            tenant_id="tenant_a",
            event_type="cache.affinity_selected",
            payload=payload,
            event_id="cae_" + "4" * 32,
            actor_id=None,
            object_id=payload["cache_affinity_id"],  # type: ignore[arg-type]
            trace_id="trace_cache_config_001",
        )
