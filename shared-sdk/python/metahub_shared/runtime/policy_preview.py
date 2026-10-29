"""策略 Dry-Run 使用的纯预演计算核心。

本模块只接受调用方已经按租户读取、归一化后的不可变输入，不接收数据库连接、HTTP
客户端或日志对象，也不会创建 Usage、DLP 命中、预算预留或路由决策。控制面和数据面可
用单元测试锁定相同的路由健康、DLP 与预算语义，而不会误调用带副作用的 Gateway 包装器。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal

from .routing_policy import TokenEstimate
from .safe_regex import UnsafeRegexError, compile_safe_dlp_regex

# 纯预演的金额精度与候选、DLP 上限必须和 Gateway 热路径保持同一资源边界。
MONEY_QUANT = Decimal("0.000001")
MAX_ROUTE_CANDIDATES = 2
MAX_DLP_RULES = 100


@dataclass(frozen=True, slots=True)
class CandidatePolicyFact:
    """计算 active candidate 指纹所需的最小安全策略事实。"""

    policy_id: str
    policy_version: int
    policy_rule_id: str
    policy_action_id: str
    priority: int
    condition_hash: str
    action_type: str
    target_model_id: str | None


@dataclass(frozen=True, slots=True)
class RouteCandidateFact:
    """一条租户内模型/Provider 配置候选，不包含 URL、认证方式或上游模型名。"""

    role: str
    model_id: str
    internal_model_id: str | None
    model_status: str
    provider_id: str | None
    provider_code: str | None
    provider_status: str | None
    provider_health_status: str | None
    provider_health_updated_at: str | None
    capabilities: dict[str, object]
    provider_auth_type: str | None = None
    keypool_state: str = "unknown"


@dataclass(frozen=True, slots=True)
class RouteCandidatePreview:
    """页面可见的单条路由候选摘要。

    质量字段来自同一次只读 Score 快照；旧模式和通用资格阶段已拒绝候选保持
    ``not_evaluated`` 与成组空值，调用方不得使用当前 Score 回算历史或预演结果。
    """

    role: str
    model_id: str
    internal_model_id: str | None
    provider_id: str | None
    provider_code: str | None
    status: Literal["selected", "rejected", "standby"]
    reason_code: str
    rank: int | None = None
    health_state: str = "unknown"
    health_fresh: bool = False
    keypool_state: str = "not_evaluated"
    price_available: bool = False
    price_source: str = "not_evaluated"
    price_schema_version: str | None = None
    currency: str | None = None
    estimated_cost_amount: Decimal | None = None
    model_quality_score_id: str | None = None
    quality_statistics_version: int | None = None
    quality_feedback_version: int | None = None
    quality_evidence_state: str = "not_evaluated"
    quality_sample_count: int | None = None
    quality_success_count: int | None = None
    quality_error_count: int | None = None
    manual_quality_bps: int | None = None
    average_latency_ms: Decimal | None = None
    quality_component_bps: int | None = None
    cost_component_bps: int | None = None
    latency_component_bps: int | None = None
    weighted_score_bps: int | None = None


@dataclass(frozen=True, slots=True)
class RoutePreviewResult:
    """配置路由预演结果；selected 不代表已经调用真实 Provider。

    route-level 质量配置只在 ``quality_cost`` 中出现并与候选 Score 使用同一 ``as_of``；
    ``quality_scoring_performed`` 只表示全部正权重分量完成，不表示产生 Usage 或真实调用。
    """

    status: Literal["selected", "unavailable"]
    reason_code: str
    would_error_code: str | None
    selected_model_id: str | None
    internal_model_id: str | None
    provider_id: str | None
    provider_code: str | None
    routing_reason: str
    chat_capability_supported: bool | None
    candidates: tuple[RouteCandidatePreview, ...]
    runtime_fallback_model_id: str | None
    fallback_cost_included: bool
    routing_policy_id: str | None = None
    routing_policy_version: int | None = None
    routing_mode: str | None = None
    price_as_of: datetime | None = None
    estimated_input_tokens: int | None = None
    estimated_output_tokens: int | None = None
    token_estimate_truncated: bool | None = None
    eligible_candidate_count: int | None = None
    quality_as_of: datetime | None = None
    quality_formula_version: str | None = None
    quality_scoring_performed: bool = False
    quality_weight_bps: int | None = None
    cost_weight_bps: int | None = None
    latency_weight_bps: int | None = None
    quality_floor_bps: int | None = None
    minimum_sample_count: int | None = None


@dataclass(frozen=True, slots=True)
class DlpRuleFact:
    """内存扫描需要的 DLP 规则；pattern 永远不会进入结果对象。"""

    dlp_rule_id: str
    rule_type: Literal["keyword", "regex"]
    pattern: str
    action: Literal["allow", "log_only", "block"]
    priority: int


@dataclass(frozen=True, slots=True)
class DlpMatchPreview:
    """不含命中文本、pattern、名称、位置或摘要的 DLP 命中事实。"""

    dlp_rule_id: str
    priority: int
    action: str


@dataclass(frozen=True, slots=True)
class DlpPreviewResult:
    """描述单条内存 Prompt 的纯 DLP 结果，不保留任何正文派生信息。"""

    status: Literal["clean", "matched", "blocked", "not_evaluated"]
    reason_code: str
    matches: tuple[DlpMatchPreview, ...]
    match_count: int
    scan_truncated: bool


@dataclass(frozen=True, slots=True)
class PriceFact:
    """某个已选模型在当前快照时刻生效的价格。"""

    input_price_per_1k: Decimal
    output_price_per_1k: Decimal
    currency: str
    available: bool = True
    # 快照同时携带缓存命中单价；当前 TokenEstimate 没有独立缓存 Token，预演不虚构用量。
    cache_price_per_1k: Decimal = Decimal("0")
    # USD 价格在预算预演中统一折算为 CNY；汇率必须来自同一观察时点的版本。
    comparison_currency: str | None = None
    comparison_fx_rate: Decimal | None = None
    comparison_fx_rate_version: str | None = None


@dataclass(frozen=True, slots=True)
class BudgetRuleFact:
    """适用于 tenant/project 的预算规则及当前业务日权威聚合。"""

    budget_rule_id: str
    scope_type: Literal["tenant", "project"]
    spent_cny: Decimal
    reserved_cny: Decimal
    limit_cny: Decimal
    warning_threshold_percent: int
    exhausted_action: Literal["alert_only", "block"]
    active_block: bool


@dataclass(frozen=True, slots=True)
class BudgetRulePreview:
    """页面可见的单条预算判定，所有金额由调用方格式化为固定 Decimal string。"""

    budget_rule_id: str
    scope_type: str
    spent_cny: Decimal
    reserved_cny: Decimal
    projected_cny: Decimal
    limit_cny: Decimal
    status: Literal["within_limit", "threshold_reached", "exhausted"]
    action: Literal["allow", "warn", "block"]
    reason_code: str


@dataclass(frozen=True, slots=True)
class BudgetPreviewResult:
    """汇总 tenant/project 预算的纯预演结果，不创建预留、阻断或评估事实。"""

    status: Literal["no_rules", "within_limit", "threshold_reached", "exhausted"]
    reason_code: str
    estimated_input_tokens: int
    estimated_output_tokens: int
    estimated_cost_cny: Decimal
    price_available: bool
    fallback_cost_included: bool
    blocked: bool
    rules: tuple[BudgetRulePreview, ...]


def policy_candidate_fingerprint(
    tenant_id: str,
    policies: tuple[CandidatePolicyFact, ...],
) -> str:
    """生成绑定租户和候选执行事实的稳定 SHA-256，不纳入 DSL 或请求上下文。"""

    ordered = sorted(policies, key=lambda item: (item.priority, item.policy_id))
    payload = {
        "schema_version": "policy-dry-run-candidate-v1",
        "tenant_id": tenant_id,
        "policies": [
            {
                "policy_id": item.policy_id,
                "policy_version": item.policy_version,
                "policy_rule_id": item.policy_rule_id,
                "policy_action_id": item.policy_action_id,
                "priority": item.priority,
                "condition_hash": item.condition_hash,
                "action_type": item.action_type,
                "target_model_id": item.target_model_id,
            }
            for item in ordered
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def preview_route(
    candidates: tuple[RouteCandidateFact, ...],
    *,
    routing_reason_prefix: str,
    unavailable_error_code: str,
    provider_health_stale_seconds: int,
    now: datetime,
    runtime_fallback_enabled: bool = False,
) -> RoutePreviewResult:
    """先按 Gateway 配置/健康选路由，再单独校验初始候选 Chat capability。"""

    bounded = candidates[:MAX_ROUTE_CANDIDATES]
    previews: list[RouteCandidatePreview] = []
    selected: RouteCandidateFact | None = None
    selected_index: int | None = None
    for index, candidate in enumerate(bounded):
        rejected = _route_rejection(candidate, provider_health_stale_seconds, now)
        if selected is None and rejected is None:
            selected = candidate
            selected_index = index
            previews.append(_route_candidate_preview(candidate, "selected", "route.selected"))
        elif rejected is None:
            previews.append(
                _route_candidate_preview(candidate, "standby", "route.candidate_standby")
            )
        else:
            previews.append(_route_candidate_preview(candidate, "rejected", rejected))

    if selected is None:
        return RoutePreviewResult(
            status="unavailable",
            reason_code="route.no_available_candidate",
            would_error_code=unavailable_error_code,
            selected_model_id=None,
            internal_model_id=None,
            provider_id=None,
            provider_code=None,
            routing_reason=routing_reason_prefix,
            chat_capability_supported=None,
            candidates=tuple(previews),
            runtime_fallback_model_id=None,
            fallback_cost_included=False,
        )

    runtime_fallback: RouteCandidateFact | None = None
    # 只有 alias.default 被选中时，PRD-036 enabled 策略才可能在一次真实请求中产生第二次计费。
    if runtime_fallback_enabled and selected_index == 0 and len(bounded) > 1:
        fallback = bounded[1]
        if (
            fallback.model_id != selected.model_id
            and _route_rejection(fallback, provider_health_stale_seconds, now) is None
            and fallback.capabilities.get("chat") is not False
        ):
            runtime_fallback = fallback

    chat_supported = selected.capabilities.get("chat") is not False

    return RoutePreviewResult(
        status="selected",
        reason_code="route.selected" if chat_supported else "route.chat_unsupported",
        would_error_code=None if chat_supported else "gateway.model_not_found",
        selected_model_id=selected.model_id,
        internal_model_id=selected.internal_model_id,
        provider_id=selected.provider_id,
        provider_code=selected.provider_code,
        routing_reason=f"{routing_reason_prefix}.{selected.role}",
        chat_capability_supported=chat_supported,
        candidates=tuple(previews),
        runtime_fallback_model_id=(runtime_fallback.model_id if runtime_fallback else None),
        fallback_cost_included=runtime_fallback is not None,
    )


def preview_dlp(prompt: str, rules: tuple[DlpRuleFact, ...]) -> DlpPreviewResult:
    """只在当前函数栈扫描 Prompt，结果不保留任何可逆或不可逆正文派生值。"""

    scan_text = prompt[:20_000]
    truncated = len(prompt) > len(scan_text)
    matches: list[DlpMatchPreview] = []
    try:
        for rule in rules[:MAX_DLP_RULES]:
            if rule.rule_type == "keyword":
                matched = rule.pattern in scan_text
            else:
                matched = (
                    compile_safe_dlp_regex(rule.pattern).compiled.search(scan_text) is not None
                )
            if matched:
                matches.append(
                    DlpMatchPreview(
                        dlp_rule_id=rule.dlp_rule_id,
                        priority=rule.priority,
                        action=rule.action,
                    )
                )
    # 持久规则损坏属于将来真实 DLP 会失败的 what-if 结果，不应回显 pattern 或异常消息。
    except (UnsafeRegexError, TypeError, ValueError):
        return DlpPreviewResult(
            status="not_evaluated",
            reason_code="dlp.rule_unavailable",
            matches=(),
            match_count=0,
            scan_truncated=truncated,
        )

    blocking = next((item for item in matches if item.action == "block"), None)
    if blocking is not None:
        status: Literal["clean", "matched", "blocked", "not_evaluated"] = "blocked"
        reason = "dlp.block_rule_matched"
    elif matches:
        status = "matched"
        reason = "dlp.rule_matched"
    else:
        status = "clean"
        reason = "dlp.no_match"
    return DlpPreviewResult(
        status=status,
        reason_code=reason,
        matches=tuple(matches),
        match_count=len(matches),
        scan_truncated=truncated,
    )


def preview_budget(
    prompt_codepoints: int,
    max_output_tokens: int | None,
    primary_price: PriceFact,
    rules: tuple[BudgetRuleFact, ...],
    *,
    fallback_price: PriceFact | None = None,
    token_estimate: TokenEstimate | None = None,
) -> BudgetPreviewResult:
    """复用 canonical Token 估算并对 tenant/project 规则做不落库的最坏成本判定。"""

    # 旧调用方仍可传长度；PRD-047 Dry-Run 必须把与路由相同的 TokenEstimate 显式传入。
    input_tokens = (
        token_estimate.input_tokens
        if token_estimate is not None
        else max(1, (max(0, prompt_codepoints) + 3) // 4)
    )
    output_tokens = (
        token_estimate.output_tokens
        if token_estimate is not None
        else (max_output_tokens or 0)
    )
    cache_tokens = token_estimate.cache_tokens if token_estimate is not None else 0
    primary_cost, primary_available = _price_cost(
        primary_price, input_tokens, output_tokens, cache_tokens
    )
    estimated_input = input_tokens
    estimated_output = output_tokens
    estimated_cost = primary_cost
    price_available = primary_available
    fallback_included = fallback_price is not None
    if fallback_price is not None:
        fallback_cost, fallback_available = _price_cost(
            fallback_price, input_tokens, output_tokens, cache_tokens
        )
        estimated_input += input_tokens
        estimated_output += output_tokens
        estimated_cost = (estimated_cost + fallback_cost).quantize(MONEY_QUANT)
        price_available = price_available and fallback_available

    previews = tuple(_preview_budget_rule(rule, estimated_cost) for rule in rules)
    if not previews:
        status: Literal["no_rules", "within_limit", "threshold_reached", "exhausted"] = (
            "no_rules"
        )
        reason = "budget.no_rules"
    elif any(item.status == "exhausted" for item in previews):
        status = "exhausted"
        reason = "budget.exhausted"
    elif any(item.status == "threshold_reached" for item in previews):
        status = "threshold_reached"
        reason = "budget.threshold_reached"
    else:
        status = "within_limit"
        reason = "budget.within_limit"
    return BudgetPreviewResult(
        status=status,
        reason_code=reason,
        estimated_input_tokens=estimated_input,
        estimated_output_tokens=estimated_output,
        estimated_cost_cny=estimated_cost,
        price_available=price_available,
        fallback_cost_included=fallback_included,
        blocked=any(item.action == "block" for item in previews),
        rules=previews,
    )


def fixed_money(value: Decimal) -> str:
    """把非负金额输出为固定六位 Decimal string，禁止浮点或科学计数法进入合同。"""

    return format(max(value, Decimal("0")).quantize(MONEY_QUANT), "f")


def parse_nonnegative_decimal(value: object) -> Decimal:
    """严格恢复数据库金额；损坏值由调用方当作快照不可用处理。"""

    try:
        parsed = Decimal(str(value))
    # 数据库金额异常不能被 float 或零值掩盖，否则预算预演可能错误放行。
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("invalid decimal fact") from exc
    # 非有限或负金额不具备合法账本语义，调用方必须把整个快照视为不可用。
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("invalid decimal fact")
    return parsed.quantize(MONEY_QUANT)


def _route_rejection(
    candidate: RouteCandidateFact,
    stale_seconds: int,
    now: datetime,
) -> str | None:
    """返回配置/健康拒绝码；Chat capability 在初始配置路由选定后独立判断。"""

    if candidate.model_status != "active":
        return "route.model_disabled"
    # Provider 缺失或禁用发生在能力判断之前，必须保留真实路由错误优先级。
    if candidate.provider_id is None or candidate.provider_status != "active":
        return "route.provider_disabled"
    if _fresh_unhealthy(candidate, stale_seconds, now):
        return "route.provider_unhealthy"
    return None


def _fresh_unhealthy(candidate: RouteCandidateFact, stale_seconds: int, now: datetime) -> bool:
    """判断新鲜 unhealthy 证据；无时间或过期状态与 Gateway 一样按 unknown 放行。"""

    # 没有带时间的新鲜异常证据时按 unknown 处理，避免陈旧探测永久摘流。
    if candidate.provider_health_status != "unhealthy" or not candidate.provider_health_updated_at:
        return False
    try:
        checked = datetime.fromisoformat(
            candidate.provider_health_updated_at.replace("Z", "+00:00")
        )
    # 损坏时间文本不能成为摘流依据，和 Gateway 一样退化为陈旧 unknown。
    except ValueError:
        return False
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=UTC)
    return checked.astimezone(UTC) >= now.astimezone(UTC) - timedelta(seconds=stale_seconds)


def _route_candidate_preview(
    candidate: RouteCandidateFact,
    status: Literal["selected", "rejected", "standby"],
    reason_code: str,
) -> RouteCandidatePreview:
    """删除纯计算不需要向 API 暴露的能力与健康原始事实。"""

    return RouteCandidatePreview(
        role=candidate.role,
        model_id=candidate.model_id,
        internal_model_id=candidate.internal_model_id,
        provider_id=candidate.provider_id,
        provider_code=candidate.provider_code,
        status=status,
        reason_code=reason_code,
    )


def _price_cost(
    price: PriceFact,
    input_tokens: int,
    output_tokens: int,
    cache_tokens: int = 0,
) -> tuple[Decimal, bool]:
    """把 CNY 或带有效汇率的 USD 价格折算为 CNY。"""

    if not price.available:
        return Decimal("0").quantize(MONEY_QUANT), False
    if type(cache_tokens) is not int or cache_tokens < 0 or cache_tokens > input_tokens:
        return Decimal("0").quantize(MONEY_QUANT), False
    amount = (
        Decimal(input_tokens - cache_tokens) * price.input_price_per_1k
        + Decimal(cache_tokens) * price.cache_price_per_1k
        + Decimal(output_tokens) * price.output_price_per_1k
    ) / Decimal(1_000_000)
    currency = price.currency.upper()
    if currency == "CNY":
        pass
    elif (
        currency == "USD"
        and price.comparison_currency == "CNY"
        and type(price.comparison_fx_rate) is Decimal
        and price.comparison_fx_rate.is_finite()
        and price.comparison_fx_rate > 0
        and isinstance(price.comparison_fx_rate_version, str)
        and price.comparison_fx_rate_version
    ):
        amount *= price.comparison_fx_rate
    else:
        return Decimal("0").quantize(MONEY_QUANT), False
    return amount.quantize(MONEY_QUANT), True


def _preview_budget_rule(rule: BudgetRuleFact, estimate: Decimal) -> BudgetRulePreview:
    """复用 active block、告警阈值与 alert_only 放行语义计算单条规则。"""

    spent = rule.spent_cny.quantize(MONEY_QUANT)
    reserved = rule.reserved_cny.quantize(MONEY_QUANT)
    projected = (spent + reserved + estimate).quantize(MONEY_QUANT)
    # alert_only 存量 active block 必须忽略；Dry-Run 不执行真实 Guard 的释放写操作。
    hard_active_block = rule.active_block and rule.exhausted_action == "block"
    if hard_active_block:
        status: Literal["within_limit", "threshold_reached", "exhausted"] = "exhausted"
        action: Literal["allow", "warn", "block"] = "block"
        reason = "budget.active_block"
    elif projected >= rule.limit_cny:
        status = "exhausted"
        action = "block" if rule.exhausted_action == "block" else "allow"
        reason = "budget.exhausted.block" if action == "block" else "budget.exhausted.alert_only"
    else:
        warning = (
            rule.limit_cny * Decimal(rule.warning_threshold_percent) / Decimal(100)
        ).quantize(MONEY_QUANT)
        if projected >= warning:
            status = "threshold_reached"
            action = "warn"
            reason = "budget.threshold_reached"
        else:
            status = "within_limit"
            action = "allow"
            reason = "budget.within_limit"
    return BudgetRulePreview(
        budget_rule_id=rule.budget_rule_id,
        scope_type=rule.scope_type,
        spent_cny=spent,
        reserved_cny=reserved,
        projected_cny=projected,
        limit_cny=rule.limit_cny.quantize(MONEY_QUANT),
        status=status,
        action=action,
        reason_code=reason,
    )
