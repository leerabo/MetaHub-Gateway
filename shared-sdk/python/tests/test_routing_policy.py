"""PRD-047 共享路由算法、Token 估算和延迟依赖单元测试。"""

from __future__ import annotations

from decimal import Decimal

import pytest
from metahub_shared.runtime.routing_policy import (
    COST_QUANT,
    MAX_INPUT_TOKENS,
    MAX_OUTPUT_TOKENS,
    QUALITY_COST_FORMULA_VERSION,
    REASON_AVAILABILITY_HEALTHY,
    REASON_COST_CURRENCY_INCOMPARABLE,
    REASON_COST_LOWEST,
    REASON_COST_PRICE_MISSING,
    REASON_COST_TIE,
    REASON_FIXED_PRIORITY,
    REASON_KEYPOOL_UNAVAILABLE,
    REASON_QUALITY_BELOW_FLOOR,
    REASON_QUALITY_COLD_START_DEFERRED,
    REASON_QUALITY_COST_COLD_START_PRIORITY,
    REASON_QUALITY_COST_CURRENCY_INCOMPARABLE,
    REASON_QUALITY_COST_EVIDENCE_PRIORITY,
    REASON_QUALITY_COST_HIGHEST_SCORE,
    REASON_QUALITY_COST_LATENCY_MISSING,
    REASON_QUALITY_COST_PRICE_MISSING,
    REASON_QUALITY_COST_SCORE_TIE,
    REASON_QUALITY_COST_SINGLE_ELIGIBLE,
    QualityCostConfig,
    RoutingCandidateInput,
    RoutingPriceFact,
    RoutingQualityFact,
    TokenEstimate,
    complete_cost_routing,
    estimate_request_tokens,
    evaluate_routing_candidates_phase_one,
    quality_component_bps,
    select_routing_candidates,
)


def _candidate(
    role: str,
    *,
    model_id: str | None = None,
    provider_id: str | None = None,
    model_status: str = "active",
    provider_status: str = "active",
    health_state: str = "healthy",
    health_fresh: bool = True,
    keypool_state: str = "available",
) -> RoutingCandidateInput:
    """构造只含安全分类的 default/fallback 测试候选。"""

    priority = 0 if role == "default" else 1
    return RoutingCandidateInput(
        candidate_role=role,  # type: ignore[arg-type]
        configured_priority=priority,
        model_id=model_id or f"model_{role}",
        provider_id=provider_id or f"provider_{role}",
        model_status=model_status,  # type: ignore[arg-type]
        provider_status=provider_status,  # type: ignore[arg-type]
        health_state=health_state,  # type: ignore[arg-type]
        health_fresh=health_fresh,
        keypool_state=keypool_state,  # type: ignore[arg-type]
    )


def _price(
    model_id: str,
    input_price: str,
    output_price: str,
    *,
    currency: str = "CNY",
) -> RoutingPriceFact:
    """构造带版本的显式 PricePlan Decimal 事实，禁止测试经过 float。"""

    return RoutingPriceFact(
        model_id=model_id,
        source="price_plan",
        input_price_per_1k=Decimal(input_price),
        output_price_per_1k=Decimal(output_price),
        currency=currency,
        price_schema_version=f"price-{model_id}",
    )


def _quality(
    model_id: str,
    *,
    sample_count: int = 20,
    success_count: int = 20,
    latency_sample_count: int | None = None,
    total_latency_ms: int = 2_000,
    manual_quality_bps: int | None = None,
) -> RoutingQualityFact:
    """构造计数自洽且带双版本的模型质量事实。"""

    latency_count = sample_count if latency_sample_count is None else latency_sample_count
    return RoutingQualityFact(
        model_id=model_id,
        model_quality_score_id=f"mqs_{model_id}",
        sample_count=sample_count,
        success_count=success_count,
        error_count=sample_count - success_count,
        latency_sample_count=latency_count,
        total_latency_ms=total_latency_ms if latency_count else 0,
        manual_quality_bps=manual_quality_bps,
        statistics_version=sample_count,
        feedback_version=1,
    )


def test_fixed_keeps_default_order_and_never_uses_keypool_or_callbacks() -> None:
    """fixed 黄金语义：KeyPool 即使不可用也留给实际调用阶段，且不读取价格/Token。"""

    callback_counts = {"token": 0, "price": 0}

    def token_estimator() -> TokenEstimate:
        """若被调用则记录错误的延迟估算访问。"""

        callback_counts["token"] += 1
        # 测试回调只返回安全数值，用于证明 fixed 分支不会触及正文估算。
        return TokenEstimate(1, 0, False)

    def price_loader(_candidates: tuple[RoutingCandidateInput, ...]):
        """若被调用则记录错误的价格依赖访问。"""

        callback_counts["price"] += 1
        return ()

    result = select_routing_candidates(
        "fixed",
        (
            _candidate("fallback", keypool_state="unavailable"),
            _candidate("default", keypool_state="unavailable"),
        ),
        token_estimator=token_estimator,
        price_loader=price_loader,
    )

    assert result.selected_model_id == "model_default"
    assert result.selection_reason == REASON_FIXED_PRIORITY
    assert result.token_estimate is None
    assert callback_counts == {"token": 0, "price": 0}
    assert {item.candidate.keypool_state for item in result.candidate_results} == {"not_evaluated"}


def test_availability_prefers_fresh_healthy_and_rejects_unavailable_keypool() -> None:
    """availability 主流程：新鲜 healthy 胜过 stale，动态 KeyPool unavailable 被硬拒绝。"""

    selected = select_routing_candidates(
        "availability_first",
        (
            _candidate("default", health_state="unhealthy", health_fresh=False),
            _candidate("fallback", health_state="healthy", health_fresh=True),
        ),
    )
    assert selected.selected_model_id == "model_fallback"
    assert selected.selection_reason == REASON_AVAILABILITY_HEALTHY
    assert [item.rank for item in selected.candidate_results] == [2, 1]

    unavailable = select_routing_candidates(
        "availability_first",
        (
            _candidate("default", keypool_state="unavailable"),
            _candidate("fallback", keypool_state="unavailable"),
        ),
    )
    assert unavailable.status == "unavailable"
    assert unavailable.eligible_candidate_count == 0
    assert {item.reason_code for item in unavailable.candidate_results} == {
        REASON_KEYPOOL_UNAVAILABLE
    }


def test_cost_first_uses_one_estimate_and_request_output_can_flip_selection() -> None:
    """成本主流程：两条候选只估算一次，输出预留会使低输入价模型不再最便宜。"""

    callbacks = {"token": 0, "price": 0}
    candidates = (_candidate("default"), _candidate("fallback"))

    def token_estimator() -> TokenEstimate:
        """返回同一请求冻结的一份输入/输出估算。"""

        callbacks["token"] += 1
        # 固定估算用于断言两候选共享同一输入，而不是每个模型重复扫描正文。
        return TokenEstimate(input_tokens=1, output_tokens=100, truncated=False)

    def price_loader(eligible: tuple[RoutingCandidateInput, ...]):
        """按同一冻结时点返回两条显式人民币价格，验证候选集合没有发生漂移。"""

        callbacks["price"] += 1
        assert {item.model_id for item in eligible} == {"model_default", "model_fallback"}
        return (
            _price("model_default", "1", "10"),
            _price("model_fallback", "2", "0"),
        )

    result = select_routing_candidates(
        "cost_first",
        candidates,
        token_estimator=token_estimator,
        price_loader=price_loader,
    )

    assert result.selected_model_id == "model_fallback"
    assert result.selection_reason == REASON_COST_LOWEST
    assert result.cost_comparison_performed is True
    assert result.token_estimate == TokenEstimate(1, 100, False)
    assert callbacks == {"token": 1, "price": 1}


def test_cost_missing_price_keeps_available_cost_but_falls_back_to_fixed() -> None:
    """一缺一有：整体按 fixed 选路，有价候选仍满足价格字段与成本成组约束。"""

    phase = evaluate_routing_candidates_phase_one(
        "cost_first", (_candidate("default"), _candidate("fallback"))
    )
    result = complete_cost_routing(
        phase,
        (
            _price("model_default", "1.5", "2.5"),
            RoutingPriceFact.missing("model_fallback"),
        ),
        TokenEstimate(input_tokens=4, output_tokens=6, truncated=False),
    )

    assert result.selected_model_id == "model_default"
    assert result.selection_reason == REASON_COST_PRICE_MISSING
    by_model = {item.candidate.model_id: item for item in result.candidate_results}
    assert by_model["model_default"].price_available is True
    assert by_model["model_default"].estimated_cost_amount == Decimal("0.021000000000000")
    assert by_model["model_fallback"].price_available is False
    assert by_model["model_fallback"].price_source == "missing"
    assert by_model["model_fallback"].price_schema_version is None
    assert by_model["model_fallback"].currency is None
    assert by_model["model_fallback"].estimated_cost_amount is None


def test_cost_mixed_currency_is_not_compared_and_quantized_tie_is_stable() -> None:
    """混币不直接比较；同币量化同价时按 default/fallback 稳定打破平局。"""

    phase = evaluate_routing_candidates_phase_one(
        "cost_first", (_candidate("default"), _candidate("fallback"))
    )
    mixed = complete_cost_routing(
        phase,
        (
            _price("model_default", "1", "0", currency="CNY"),
            _price("model_fallback", "0.1", "0", currency="USD"),
        ),
        TokenEstimate(1, 0, False),
    )
    assert mixed.selected_model_id == "model_default"
    assert mixed.selection_reason == REASON_COST_CURRENCY_INCOMPARABLE

    tie = complete_cost_routing(
        phase,
        (
            _price("model_default", "0.000000000001", "0"),
            _price("model_fallback", "0.000000000001", "0"),
        ),
        TokenEstimate(1, 0, False),
    )
    assert tie.selected_model_id == "model_default"
    assert tie.selection_reason == REASON_COST_TIE
    assert tie.candidate_results[0].estimated_cost_amount == COST_QUANT


def test_cost_single_eligible_does_not_require_price_or_token() -> None:
    """cost 只有一条合格候选时立即选择，损坏的未读取价格不能影响该分支。"""

    callbacks = {"token": 0, "price": 0}
    result = select_routing_candidates(
        "cost_first",
        (
            _candidate("default"),
            _candidate("fallback", model_status="disabled", keypool_state="not_evaluated"),
        ),
        token_estimator=lambda: callbacks.__setitem__("token", callbacks["token"] + 1),
        price_loader=lambda _items: callbacks.__setitem__("price", callbacks["price"] + 1),
    )
    assert result.selected_model_id == "model_default"
    assert result.selection_reason == "routing.single_eligible_candidate"
    assert callbacks == {"token": 0, "price": 0}


def test_chat_token_estimator_counts_only_text_parts_and_prefers_completion_limit() -> None:
    """Chat 结构化内容只统计文本字段，并验证新输出上限参数优先于旧参数。"""

    estimate = estimate_request_tokens(
        "chat",
        {
            "messages": [
                {"role": "user", "content": "1234"},
                {
                    "role": "user",
                    "content": [
                        "中文",
                        {"type": "text", "text": "ab"},
                        {"type": "image_url", "image_url": {"url": "private"}},
                        {"text": True},
                        None,
                    ],
                },
            ],
            "max_completion_tokens": 5,
            "max_tokens": 9,
        },
    )
    assert estimate == TokenEstimate(input_tokens=2, output_tokens=5, truncated=False)


def test_embedding_token_estimator_handles_nested_strings_and_true_integers() -> None:
    """Embedding 嵌套字符串按 code point，除 bool 外的整数项各计一个直接 Token。"""

    estimate = estimate_request_tokens(
        "embeddings",
        {
            "input": [
                "1234",
                ["中文", [1, 2, True, -1, 3.5, None]],
                {"ignored": "not-counted"},
            ]
        },
    )
    assert estimate == TokenEstimate(input_tokens=5, output_tokens=0, truncated=False)


def test_token_estimator_saturates_node_input_and_output_limits() -> None:
    """超节点、超长文本和超输出预留都使用 fail-safe 饱和且不返回正文。"""

    node_limited = estimate_request_tokens("embeddings", {"input": list(range(100_001))})
    assert node_limited == TokenEstimate(MAX_INPUT_TOKENS, 0, True)

    text_limited = estimate_request_tokens(
        "chat", {"messages": [{"role": "user", "content": "x" * 4_000_001}]}
    )
    assert text_limited == TokenEstimate(MAX_INPUT_TOKENS, 0, True)

    output_limited = estimate_request_tokens(
        "chat",
        {
            "messages": [{"role": "user", "content": "x"}],
            "max_completion_tokens": MAX_OUTPUT_TOKENS + 1,
        },
    )
    assert output_limited == TokenEstimate(1, MAX_OUTPUT_TOKENS, True)


def test_embedding_token_estimator_rejects_wide_array_before_bulk_reverse() -> None:
    """超宽数组在压栈复制前饱和，证明节点上限同时约束临时内存。"""

    class WideInput(list[int]):
        """若实现尝试反向遍历全部输入，本夹具立即暴露无界路径。"""

        def __reversed__(self):
            """禁止旧实现先遍历 150k 项再检查节点预算。"""

            # 触发此分支说明实现重新引入无界反向遍历，测试必须立即失败。
            raise AssertionError("wide input must be rejected before reversed traversal")

    estimate = estimate_request_tokens(
        "embeddings",
        {"input": WideInput(range(150_000))},
    )

    assert estimate == TokenEstimate(MAX_INPUT_TOKENS, 0, True)


def test_invalid_candidate_and_price_facts_fail_closed() -> None:
    """超过两候选、角色优先级漂移和 float 价格都不能被静默归一化。"""

    with pytest.raises(ValueError, match="candidate_count_invalid"):
        evaluate_routing_candidates_phase_one(
            "fixed",
            (
                _candidate("default", model_id="model_a"),
                _candidate("fallback", model_id="model_b"),
                _candidate("fallback", model_id="model_c"),
            ),
        )

    phase = evaluate_routing_candidates_phase_one(
        "cost_first", (_candidate("default"), _candidate("fallback"))
    )
    invalid_float = RoutingPriceFact(
        model_id="model_default",
        source="price_plan",
        input_price_per_1k=1.0,  # type: ignore[arg-type]
        output_price_per_1k=Decimal("1"),
        currency="CNY",
        price_schema_version="v1",
    )
    with pytest.raises(ValueError, match="price_fact_invalid"):
        complete_cost_routing(
            phase,
            (invalid_float, RoutingPriceFact.missing("model_fallback")),
            TokenEstimate(1, 0, False),
        )


def test_quality_component_uses_manual_prior_and_round_half_up_without_float() -> None:
    """质量分使用 20 个先验样本，人工分只作为先验并保持十进制半入舍入。"""

    assert (
        quality_component_bps(
            _quality("model_empty", sample_count=0, success_count=0, latency_sample_count=0)
        )
        == 5_000
    )
    assert (
        quality_component_bps(
            _quality(
                "model_manual",
                sample_count=0,
                success_count=0,
                latency_sample_count=0,
                manual_quality_bps=8_000,
            )
        )
        == 8_000
    )
    assert quality_component_bps(_quality("model_observed")) == 7_500
    assert (
        quality_component_bps(
            _quality(
                "model_round",
                sample_count=1,
                success_count=0,
                latency_sample_count=0,
            )
        )
        == 4_762
    )


@pytest.mark.parametrize(
    "config",
    [
        QualityCostConfig(quality_weight_bps=0, cost_weight_bps=8_000, latency_weight_bps=2_000),
        QualityCostConfig(quality_weight_bps=5_000, cost_weight_bps=2_999),
        QualityCostConfig(quality_floor_bps=0),
        QualityCostConfig(minimum_sample_count=True),  # type: ignore[arg-type]
        QualityCostConfig(cost_weight_bps=1.0),  # type: ignore[arg-type]
    ],
)
def test_quality_cost_config_rejects_zero_quality_weight_bool_float_and_bad_sum(
    config: QualityCostConfig,
) -> None:
    """策略配置必须执行强类型、边界与总和校验，不能利用 bool/float 绕过。"""

    callbacks = {"quality": 0}

    def quality_loader(_candidates: tuple[RoutingCandidateInput, ...]):
        """记录非法配置是否错误地先触发了外部事实读取。"""

        callbacks["quality"] += 1
        return (_quality("model_default"),)

    with pytest.raises(ValueError, match="quality_config_invalid"):
        select_routing_candidates(
            "quality_cost",
            (_candidate("default"),),
            quality_config=config,
            quality_loader=quality_loader,
        )
    assert callbacks == {"quality": 0}


def test_quality_cost_zero_generic_candidates_reads_no_quality_price_or_token() -> None:
    """零条通用合格候选直接 no-route，任何质量、正文或价格依赖都不得读取。"""

    def unexpected(*_args):
        """任何延迟依赖读取都代表状态矩阵被破坏。"""

        # 本分支用于证明无候选路径绝不会触发任何外部依赖读取。
        raise AssertionError("dependency must not be called")

    result = select_routing_candidates(
        "quality_cost",
        (_candidate("default", model_status="disabled", keypool_state="not_evaluated"),),
        quality_config=QualityCostConfig(),
        quality_loader=unexpected,
        token_estimator=unexpected,
        price_loader=unexpected,
    )

    assert result.status == "unavailable"
    assert result.quality_formula_version == QUALITY_COST_FORMULA_VERSION
    assert result.quality_scoring_performed is False
    assert result.candidate_results[0].quality_evidence_state == "not_evaluated"


def test_quality_cost_empty_candidates_freezes_formula_without_loaders() -> None:
    """字面空候选也应直接 no-route，并冻结当前公式而不要求任何依赖回调。"""

    result = select_routing_candidates(
        "quality_cost",
        (),
        quality_config=QualityCostConfig(),
    )
    assert result.status == "unavailable"
    assert result.candidate_results == ()
    assert result.quality_formula_version == QUALITY_COST_FORMULA_VERSION


def test_quality_floor_equal_is_ready_pass_and_ready_low_is_removed() -> None:
    """质量分恰等门槛视为通过，另一条低质候选必须被移出 rank。"""

    result = select_routing_candidates(
        "quality_cost",
        (_candidate("default"), _candidate("fallback")),
        quality_config=QualityCostConfig(minimum_sample_count=5),
        quality_loader=lambda _items: (
            _quality("model_default", sample_count=5, success_count=5),
            _quality("model_fallback", sample_count=5, success_count=4),
        ),
    )
    assert (
        quality_component_bps(_quality("model_default", sample_count=5, success_count=5)) == 6_000
    )
    assert result.selected_model_id == "model_default"
    assert result.selection_reason == REASON_QUALITY_COST_SINGLE_ELIGIBLE
    by_model = {item.candidate.model_id: item for item in result.candidate_results}
    assert by_model["model_fallback"].reason_code == REASON_QUALITY_BELOW_FLOOR
    assert by_model["model_fallback"].rank is None


def test_quality_cost_single_cold_and_single_low_follow_threshold_matrix() -> None:
    """单 cold 可启动；单 ready-low 必须在价格前拒绝并返回 no-route。"""

    callbacks = {"token": 0, "price": 0}

    def token_estimator() -> TokenEstimate:
        """记录不应发生的正文扫描，验证单候选路径保持最小依赖。"""

        callbacks["token"] += 1
        # 返回合法估算仅用于防止回调内容干扰“是否调用”的核心断言。
        return TokenEstimate(1, 0, False)

    def price_loader(_candidates: tuple[RoutingCandidateInput, ...]):
        """记录不应发生的价格读取，验证质量门槛先于成本依赖。"""

        callbacks["price"] += 1
        return ()

    cold = select_routing_candidates(
        "quality_cost",
        (_candidate("default"),),
        quality_config=QualityCostConfig(),
        quality_loader=lambda _items: (
            _quality("model_default", sample_count=0, success_count=0, latency_sample_count=0),
        ),
        token_estimator=token_estimator,
        price_loader=price_loader,
    )
    assert cold.selected_model_id == "model_default"
    assert cold.selection_reason == REASON_QUALITY_COST_SINGLE_ELIGIBLE
    assert cold.candidate_results[0].quality_evidence_state == "cold"
    assert cold.candidate_results[0].quality_component_bps == 5_000

    low = select_routing_candidates(
        "quality_cost",
        (_candidate("default"),),
        quality_config=QualityCostConfig(),
        quality_loader=lambda _items: (_quality("model_default", success_count=0),),
        token_estimator=token_estimator,
        price_loader=price_loader,
    )
    assert low.status == "unavailable"
    assert low.candidate_results[0].rank is None
    assert low.candidate_results[0].reason_code == REASON_QUALITY_BELOW_FLOOR
    assert low.candidate_results[0].quality_component_bps == 2_500
    assert callbacks == {"token": 0, "price": 0}


def test_quality_cost_cold_ready_and_low_combinations_are_deterministic() -> None:
    """双 cold 按配置顺序；ready 压过 cold；低质剔除后 cold 仍可作为唯一候选。"""

    candidates = (_candidate("fallback"), _candidate("default"))
    both_cold = select_routing_candidates(
        "quality_cost",
        candidates,
        quality_config=QualityCostConfig(),
        quality_loader=lambda _items: (
            _quality("model_fallback", sample_count=0, success_count=0, latency_sample_count=0),
            _quality("model_default", sample_count=0, success_count=0, latency_sample_count=0),
        ),
    )
    assert both_cold.selected_model_id == "model_default"
    assert both_cold.selection_reason == REASON_QUALITY_COST_COLD_START_PRIORITY

    ready_and_cold = select_routing_candidates(
        "quality_cost",
        candidates,
        quality_config=QualityCostConfig(),
        quality_loader=lambda _items: (
            _quality("model_default", sample_count=0, success_count=0, latency_sample_count=0),
            _quality("model_fallback"),
        ),
    )
    assert ready_and_cold.selected_model_id == "model_fallback"
    assert ready_and_cold.selection_reason == REASON_QUALITY_COST_EVIDENCE_PRIORITY
    by_model = {item.candidate.model_id: item for item in ready_and_cold.candidate_results}
    assert by_model["model_default"].reason_code == REASON_QUALITY_COLD_START_DEFERRED

    low_and_cold = select_routing_candidates(
        "quality_cost",
        candidates,
        quality_config=QualityCostConfig(),
        quality_loader=lambda _items: (
            _quality("model_default", success_count=0),
            _quality("model_fallback", sample_count=0, success_count=0, latency_sample_count=0),
        ),
    )
    assert low_and_cold.selected_model_id == "model_fallback"
    assert low_and_cold.selection_reason == REASON_QUALITY_COST_SINGLE_ELIGIBLE
    by_model = {item.candidate.model_id: item for item in low_and_cold.candidate_results}
    assert by_model["model_default"].status == "rejected"
    assert by_model["model_default"].rank is None


def test_quality_cost_all_ready_low_returns_no_route_before_dependencies() -> None:
    """两条已知低质候选全部拒绝，不得用价格低掩盖质量门槛。"""

    callbacks = {"token": 0, "price": 0}
    result = select_routing_candidates(
        "quality_cost",
        (_candidate("default"), _candidate("fallback")),
        quality_config=QualityCostConfig(),
        quality_loader=lambda _items: (
            _quality("model_default", success_count=0),
            _quality("model_fallback", success_count=0),
        ),
        token_estimator=lambda: callbacks.__setitem__("token", callbacks["token"] + 1),
        price_loader=lambda _items: callbacks.__setitem__("price", callbacks["price"] + 1),
    )
    assert result.status == "unavailable"
    assert result.eligible_candidate_count == 0
    assert {item.reason_code for item in result.candidate_results} == {REASON_QUALITY_BELOW_FLOOR}
    assert callbacks == {"token": 0, "price": 0}


def test_quality_cost_latency_missing_precedes_token_and_price() -> None:
    """正延迟权重缺证据时稳定回退，且优先于 Token 扫描和价格问题。"""

    callbacks = {"token": 0, "price": 0}

    def token_estimator() -> TokenEstimate:
        """记录错误触发的正文扫描，验证缺延迟路径及时停止。"""

        callbacks["token"] += 1
        # 合法返回值只用于隔离依赖顺序，测试不允许该回调真正执行。
        return TokenEstimate(1, 0, False)

    def price_loader(_items: tuple[RoutingCandidateInput, ...]):
        """即使将返回缺价，延迟缺失也必须阻止本回调。"""

        callbacks["price"] += 1
        return (RoutingPriceFact.missing("model_default"),)

    result = select_routing_candidates(
        "quality_cost",
        (_candidate("default"), _candidate("fallback")),
        quality_config=QualityCostConfig(),
        quality_loader=lambda _items: (
            _quality("model_default", latency_sample_count=0),
            _quality("model_fallback"),
        ),
        token_estimator=token_estimator,
        price_loader=price_loader,
    )
    assert result.selected_model_id == "model_default"
    assert result.selection_reason == REASON_QUALITY_COST_LATENCY_MISSING
    assert result.quality_scoring_performed is False
    assert {item.quality_component_bps for item in result.candidate_results} == {7_500}
    assert callbacks == {"token": 0, "price": 0}


def test_quality_cost_zero_cost_weight_skips_callbacks_and_latency_can_win() -> None:
    """成本权重为零时完全跳过正文和价格，按质量与延迟完成加权。"""

    callbacks = {"token": 0, "price": 0}
    result = select_routing_candidates(
        "quality_cost",
        (_candidate("default"), _candidate("fallback")),
        quality_config=QualityCostConfig(
            quality_weight_bps=5_000,
            cost_weight_bps=0,
            latency_weight_bps=5_000,
        ),
        quality_loader=lambda _items: (
            _quality("model_default", total_latency_ms=2_000),
            _quality("model_fallback", total_latency_ms=1_000),
        ),
        token_estimator=lambda: callbacks.__setitem__("token", callbacks["token"] + 1),
        price_loader=lambda _items: callbacks.__setitem__("price", callbacks["price"] + 1),
    )
    assert result.selected_model_id == "model_fallback"
    assert result.selection_reason == REASON_QUALITY_COST_HIGHEST_SCORE
    assert result.quality_scoring_performed is True
    assert callbacks == {"token": 0, "price": 0}
    by_model = {item.candidate.model_id: item for item in result.candidate_results}
    assert by_model["model_default"].cost_component_bps is None
    assert by_model["model_default"].average_latency_ms == Decimal("100.000")
    assert by_model["model_default"].latency_component_bps == 5_000
    assert by_model["model_fallback"].latency_component_bps == 10_000


def test_quality_cost_single_zero_latency_gets_full_component() -> None:
    """仅一条平均延迟为零时该候选得满分，另一条明确为零分且不发生除零。"""

    result = select_routing_candidates(
        "quality_cost",
        (_candidate("default"), _candidate("fallback")),
        quality_config=QualityCostConfig(
            quality_weight_bps=5_000,
            cost_weight_bps=0,
            latency_weight_bps=5_000,
        ),
        quality_loader=lambda _items: (
            _quality("model_default", total_latency_ms=0),
            _quality("model_fallback", total_latency_ms=2_000),
        ),
    )
    by_model = {item.candidate.model_id: item for item in result.candidate_results}
    assert result.selected_model_id == "model_default"
    assert by_model["model_default"].latency_component_bps == 10_000
    assert by_model["model_fallback"].latency_component_bps == 0


def test_quality_cost_positive_cost_weight_requires_and_validates_callbacks_in_order() -> None:
    """两条 ready-pass 的正成本权重必须具备双回调，并在读价格前验证 Token。"""

    candidates = (_candidate("default"), _candidate("fallback"))
    facts = (
        _quality("model_default", latency_sample_count=0),
        _quality("model_fallback", latency_sample_count=0),
    )
    config = QualityCostConfig(latency_weight_bps=0, cost_weight_bps=5_000)
    with pytest.raises(ValueError, match="quality_dependencies_missing"):
        select_routing_candidates(
            "quality_cost",
            candidates,
            quality_config=config,
            quality_loader=lambda _items: facts,
        )

    callbacks = {"price": 0}

    def price_loader(_items: tuple[RoutingCandidateInput, ...]):
        """记录坏 Token 是否错误越过校验并触发价格快照读取。"""

        callbacks["price"] += 1
        return (_price("model_default", "1", "0"), _price("model_fallback", "1", "0"))

    with pytest.raises(ValueError, match="token_estimate_invalid"):
        select_routing_candidates(
            "quality_cost",
            candidates,
            quality_config=config,
            quality_loader=lambda _items: facts,
            token_estimator=lambda: TokenEstimate(0, 0, False),
            price_loader=price_loader,
        )
    assert callbacks == {"price": 0}


def test_quality_cost_zero_price_and_combined_score_use_decimal_components() -> None:
    """零价获得满成本分，非零候选得零分，三项总分只经 Decimal/整数计算。"""

    callbacks = {"token": 0, "price": 0}

    def token_estimator() -> TokenEstimate:
        """为两个候选冻结同一请求估算。"""

        callbacks["token"] += 1
        # 两个价格事实必须共享这一份合法估算，避免候选间正文扫描漂移。
        return TokenEstimate(1, 0, False)

    def price_loader(_items: tuple[RoutingCandidateInput, ...]):
        """返回一条显式零价和一条非零人民币价格。"""

        callbacks["price"] += 1
        return (_price("model_default", "0", "0"), _price("model_fallback", "1", "0"))

    result = select_routing_candidates(
        "quality_cost",
        (_candidate("fallback"), _candidate("default")),
        quality_config=QualityCostConfig(
            quality_weight_bps=4_000,
            cost_weight_bps=6_000,
            latency_weight_bps=0,
        ),
        quality_loader=lambda _items: (
            _quality("model_fallback"),
            _quality("model_default"),
        ),
        token_estimator=token_estimator,
        price_loader=price_loader,
    )
    assert result.selected_model_id == "model_default"
    assert result.quality_scoring_performed is True
    assert callbacks == {"token": 1, "price": 1}
    by_model = {item.candidate.model_id: item for item in result.candidate_results}
    assert by_model["model_default"].cost_component_bps == 10_000
    assert by_model["model_fallback"].cost_component_bps == 0
    assert by_model["model_default"].weighted_score_bps == 9_000
    assert by_model["model_fallback"].weighted_score_bps == 3_000


@pytest.mark.parametrize(
    ("prices", "reason"),
    [
        (
            (_price("model_default", "1", "0"), RoutingPriceFact.missing("model_fallback")),
            REASON_QUALITY_COST_PRICE_MISSING,
        ),
        (
            (
                _price("model_default", "1", "0", currency="CNY"),
                _price("model_fallback", "1", "0", currency="USD"),
            ),
            REASON_QUALITY_COST_CURRENCY_INCOMPARABLE,
        ),
    ],
)
def test_quality_cost_price_dependency_fallback_is_stable(
    prices: tuple[RoutingPriceFact, ...],
    reason: str,
) -> None:
    """缺价优先于币种比较；两类依赖不足都按配置优先级稳定回退。"""

    result = select_routing_candidates(
        "quality_cost",
        (_candidate("fallback"), _candidate("default")),
        quality_config=QualityCostConfig(latency_weight_bps=0, cost_weight_bps=5_000),
        quality_loader=lambda _items: (_quality("model_default"), _quality("model_fallback")),
        token_estimator=lambda: TokenEstimate(1, 0, False),
        price_loader=lambda _items: prices,
    )
    assert result.selected_model_id == "model_default"
    assert result.selection_reason == reason
    assert result.quality_scoring_performed is False
    assert all(item.weighted_score_bps is None for item in result.candidate_results)


def test_quality_cost_equal_components_tie_is_input_order_independent() -> None:
    """全部正权重分量同分时按配置优先级和模型 ID 破局，不依赖输入迭代顺序。"""

    result = select_routing_candidates(
        "quality_cost",
        (_candidate("fallback"), _candidate("default")),
        quality_config=QualityCostConfig(),
        quality_loader=lambda _items: (_quality("model_fallback"), _quality("model_default")),
        token_estimator=lambda: TokenEstimate(1, 0, False),
        price_loader=lambda _items: (
            _price("model_fallback", "1", "0"),
            _price("model_default", "1", "0"),
        ),
    )
    assert result.selected_model_id == "model_default"
    assert result.selection_reason == REASON_QUALITY_COST_SCORE_TIE
    assert {item.weighted_score_bps for item in result.candidate_results} == {8_750}


@pytest.mark.parametrize("routing_mode", ["fixed", "availability_first", "cost_first"])
def test_legacy_modes_never_read_quality_facts(routing_mode: str) -> None:
    """旧三模式黄金回归：休眠质量配置和 loader 不能改变既有依赖边界。"""

    callbacks = {"quality": 0}

    def quality_loader(_items: tuple[RoutingCandidateInput, ...]):
        """记录旧模式是否错误读取质量 Score，锁定休眠配置的兼容边界。"""

        callbacks["quality"] += 1
        return (_quality("model_default"),)

    result = select_routing_candidates(
        routing_mode,  # type: ignore[arg-type]
        (_candidate("default"),),
        quality_config=QualityCostConfig(),
        quality_loader=quality_loader,
    )
    assert result.selected_model_id == "model_default"
    assert result.quality_formula_version is None
    assert result.candidate_results[0].quality_evidence_state == "not_evaluated"
    assert callbacks == {"quality": 0}


@pytest.mark.parametrize(
    "fact",
    [
        _quality("model_invalid", sample_count=True, success_count=0),  # type: ignore[arg-type]
        _quality("model_invalid", sample_count=20, success_count=21),
        _quality("model_invalid", latency_sample_count=0).__class__(
            model_id="model_invalid",
            model_quality_score_id="mqs_model_invalid",
            sample_count=20,
            success_count=20,
            error_count=0,
            latency_sample_count=0,
            total_latency_ms=1,
            manual_quality_bps=None,
            statistics_version=20,
            feedback_version=1,
        ),
        _quality("model_invalid").__class__(
            model_id="model_invalid",
            model_quality_score_id="mqs_model_invalid",
            sample_count=20,
            success_count=20,
            error_count=0,
            latency_sample_count=20,
            total_latency_ms=2_000,
            manual_quality_bps=1.0,  # type: ignore[arg-type]
            statistics_version=20,
            feedback_version=1,
        ),
    ],
)
def test_quality_fact_rejects_bool_inconsistent_group_and_float(fact: RoutingQualityFact) -> None:
    """损坏的 Score 事实必须失败关闭，不能修剪、补零或把 float 归一化。"""

    with pytest.raises(ValueError, match="quality_fact_invalid"):
        quality_component_bps(fact)


def test_quality_loader_requires_exact_unique_candidate_mapping() -> None:
    """质量 loader 缺行、重复行或跨候选行都不能被解释为冷启动。"""

    candidates = (_candidate("default"), _candidate("fallback"))
    for facts in (
        (_quality("model_default"),),
        (_quality("model_default"), _quality("model_default")),
        (_quality("model_default"), _quality("model_other")),
    ):
        with pytest.raises(ValueError, match="quality_fact_invalid"):
            select_routing_candidates(
                "quality_cost",
                candidates,
                quality_config=QualityCostConfig(),
                quality_loader=lambda _items, values=facts: values,
            )
