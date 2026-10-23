"""PRD-046 shared 纯预演核心的边界、确定性与无副作用状态测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from metahub_shared.runtime.policy_preview import (
    BudgetRuleFact,
    CandidatePolicyFact,
    DlpRuleFact,
    PriceFact,
    RouteCandidateFact,
    policy_candidate_fingerprint,
    preview_budget,
    preview_dlp,
    preview_route,
)
from metahub_shared.runtime.routing_policy import TokenEstimate


def test_candidate_fingerprint_is_stable_and_tenant_bound() -> None:
    """排序变化不改变同一候选指纹，tenant relabel 与执行字段变化必须改变。"""

    first = CandidatePolicyFact(
        policy_id="pol_a",
        policy_version=1,
        policy_rule_id="prule_a",
        policy_action_id="pact_a",
        priority=20,
        condition_hash="a" * 64,
        action_type="allow",
        target_model_id=None,
    )
    second = CandidatePolicyFact(
        policy_id="pol_b",
        policy_version=2,
        policy_rule_id="prule_b",
        policy_action_id="pact_b",
        priority=10,
        condition_hash="b" * 64,
        action_type="route_to",
        target_model_id="model_b",
    )
    fingerprint = policy_candidate_fingerprint("tenant_a", (first, second))
    assert fingerprint == policy_candidate_fingerprint("tenant_a", (second, first))
    assert fingerprint != policy_candidate_fingerprint("tenant_b", (first, second))


def test_route_without_candidate_is_unavailable_and_chat_check_does_not_select_fallback() -> None:
    """无候选是 unavailable；default 不支持 Chat 时仍先选 default，再形成 capability 错误。"""

    now = datetime(2026, 7, 18, tzinfo=UTC)
    empty = preview_route(
        (),
        routing_reason_prefix="alias",
        unavailable_error_code="gateway.model_disabled",
        provider_health_stale_seconds=180,
        now=now,
    )
    assert empty.status == "unavailable"
    assert empty.would_error_code == "gateway.model_disabled"

    unsupported = RouteCandidateFact(
        role="default",
        model_id="model_a",
        internal_model_id="chat-a",
        model_status="active",
        provider_id="provider_a",
        provider_code="provider-a",
        provider_status="active",
        provider_health_status="healthy",
        provider_health_updated_at="2026-07-18T00:00:00Z",
        capabilities={"chat": False},
    )
    supported_fallback = RouteCandidateFact(
        role="fallback",
        model_id="model_b",
        internal_model_id="chat-b",
        model_status="active",
        provider_id="provider_b",
        provider_code="provider-b",
        provider_status="active",
        provider_health_status="healthy",
        provider_health_updated_at="2026-07-18T00:00:00Z",
        capabilities={"chat": True},
    )
    result = preview_route(
        (unsupported, supported_fallback),
        routing_reason_prefix="alias",
        unavailable_error_code="gateway.model_not_found",
        provider_health_stale_seconds=180,
        now=now,
    )
    assert result.status == "selected"
    assert result.selected_model_id == "model_a"
    assert result.chat_capability_supported is False
    assert result.would_error_code == "gateway.model_not_found"
    assert result.candidates[0].status == "selected"
    assert result.candidates[1].status == "standby"


def test_invalid_persisted_dlp_regex_is_safe_not_evaluated_result() -> None:
    """损坏 regex 形成可返回 200 的 DLP unavailable what-if，不抛异常或保留 pattern。"""

    result = preview_dlp(
        "local sentinel that must not survive",
        (
            DlpRuleFact(
                dlp_rule_id="dlp_invalid",
                rule_type="regex",
                pattern="(a+)+",
                action="block",
                priority=1,
            ),
        ),
    )
    assert result.status == "not_evaluated"
    assert result.reason_code == "dlp.rule_unavailable"
    assert result.matches == ()


def test_budget_ignores_alert_only_stale_block_and_honors_hard_block() -> None:
    """alert_only 存量 block 不误阻断，block 模式的当前事实继续形成最坏准入拒绝。"""

    price = PriceFact(
        input_price_per_1k=Decimal("1"),
        output_price_per_1k=Decimal("1"),
        currency="CNY",
    )
    alert_only = BudgetRuleFact(
        budget_rule_id="budget_alert",
        scope_type="tenant",
        spent_cny=Decimal("1"),
        reserved_cny=Decimal("0"),
        limit_cny=Decimal("100"),
        warning_threshold_percent=80,
        exhausted_action="alert_only",
        active_block=True,
    )
    hard = BudgetRuleFact(
        budget_rule_id="budget_block",
        scope_type="project",
        spent_cny=Decimal("1"),
        reserved_cny=Decimal("0"),
        limit_cny=Decimal("100"),
        warning_threshold_percent=80,
        exhausted_action="block",
        active_block=True,
    )
    allowed = preview_budget(4, 1, price, (alert_only,))
    blocked = preview_budget(4, 1, price, (hard,))
    assert allowed.blocked is False
    assert allowed.rules[0].status == "within_limit"
    assert blocked.blocked is True
    assert blocked.rules[0].reason_code == "budget.active_block"


def test_budget_preview_prices_cache_hits_separately() -> None:
    """预算预演使用缓存命中价格，不把缓存 Token 重复按普通输入价收费。"""

    price = PriceFact(
        input_price_per_1k=Decimal("10"),
        output_price_per_1k=Decimal("0"),
        cache_price_per_1k=Decimal("1"),
        currency="CNY",
    )
    result = preview_budget(
        400,
        0,
        price,
        (),
        token_estimate=TokenEstimate(
            input_tokens=100,
            output_tokens=0,
            truncated=False,
            cache_tokens=80,
        ),
    )
    assert result.estimated_cost_cny == Decimal("0.000280")


def test_budget_preview_converts_usd_with_the_frozen_fx_rate() -> None:
    """成本优先使用的 USD 价格在有汇率时应与 CNY 一样可计价。"""

    price = PriceFact(
        input_price_per_1k=Decimal("2"),
        output_price_per_1k=Decimal("4"),
        cache_price_per_1k=Decimal("0.5"),
        currency="USD",
        comparison_currency="CNY",
        comparison_fx_rate=Decimal("7.2"),
        comparison_fx_rate_version="fx-v3",
    )
    result = preview_budget(
        400,
        0,
        price,
        (),
        token_estimate=TokenEstimate(
            input_tokens=100,
            output_tokens=0,
            truncated=False,
            cache_tokens=80,
        ),
    )
    assert result.price_available is True
    assert result.estimated_cost_cny == Decimal("0.000576")
