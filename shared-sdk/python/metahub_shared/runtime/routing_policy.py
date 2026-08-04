"""PRD-047/048 动态路由、质量门槛与效果成本加权的共享纯计算核心。

本模块只处理调用方已经按租户读取并归一化的安全事实，不接收数据库连接、配置对象、
日志器、HTTP 客户端或明文凭据。Gateway 与 Policy Dry-Run 必须复用这里的两阶段排序和
Token 估算口径，避免同一请求在线上与预演中得到不同的模型选择。PRD-048 新增的
``quality_cost`` 同样只消费调用方按当前租户、同一快照冻结的质量和价格事实。
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from typing import Literal

# 两候选是 ModelAlias default/fallback 的产品边界。固定上限同时保护数据库事务和纯计算耗时。
MAX_ROUTING_CANDIDATES = 2
# Token 遍历和计数上限防止畸形 OpenAI-compatible 结构在选路阶段消耗无界 CPU 或内存。
MAX_TOKEN_NODES = 100_000
MAX_INPUT_TOKENS = 1_000_000
MAX_OUTPUT_TOKENS = 131_072
# 模型价格入口当前允许每千 Token 单价最多 10 亿、12 位小数；快照恢复必须执行同样校验。
MAX_PRICE_PER_1K = Decimal("1000000000")
MAX_PRICE_DECIMAL_PLACES = 12
# 除以 1000 后最多产生 15 位小数；排序值和持久化解释必须使用同一量化结果。
COST_QUANT = Decimal("0.000000000000001")
# 平均延迟以毫秒三位小数冻结，和数据库 TEXT CHECK、Control API 展示口径完全一致。
LATENCY_QUANT = Decimal("0.001")
# 公式版本会持久化到 RoutingDecision，修改公式时必须新增版本，不能原地改变历史语义。
QUALITY_COST_FORMULA_VERSION = "quality_cost_v1"
# 质量公式使用固定 20 个先验样本；基点统一表达 0.00%-100.00%，禁止 float 百分比。
QUALITY_PRIOR_SAMPLE_COUNT = 20
MAX_BPS = 10_000
# 质量统计最终落在 SQLite INTEGER，纯计算入口也必须拒绝超出 signed 64-bit 的坏快照。
MAX_SQLITE_INTEGER = 9_223_372_036_854_775_807

RoutingMode = Literal["fixed", "cost_first", "availability_first", "quality_cost"]
CandidateRole = Literal["default", "fallback"]
ObjectStatus = Literal["active", "disabled", "missing"]
HealthState = Literal["healthy", "unhealthy", "unknown"]
KeyPoolState = Literal["available", "unavailable", "unknown", "not_evaluated"]
PriceSource = Literal["price_plan", "model_catalog", "missing", "not_evaluated"]
QualityEvidenceState = Literal["ready", "cold", "not_evaluated"]
CandidateFinalStatus = Literal["selected", "standby", "rejected"]
SelectionStatus = Literal["selected", "unavailable"]

# 运行时枚举白名单用于拒绝持久快照漂移，不能对未知字符串做大小写或默认值兼容。
_ROUTING_MODES = frozenset({"fixed", "cost_first", "availability_first", "quality_cost"})
_MODEL_STATUSES = frozenset({"active", "disabled", "missing"})
_PROVIDER_STATUSES = frozenset({"active", "disabled", "missing"})
_HEALTH_STATES = frozenset({"healthy", "unhealthy", "unknown"})
_KEYPOOL_STATES = frozenset({"available", "unavailable", "unknown", "not_evaluated"})
_PRICE_SOURCES = frozenset({"price_plan", "model_catalog", "missing", "not_evaluated"})
_CURRENCIES = frozenset({"CNY", "USD"})

# 原因码是数据库、Webhook、API 与 UI 共用的机器合同，禁止拼接 SQL、URL 或错误正文。
REASON_NO_AVAILABLE_CANDIDATE = "routing.no_available_candidate"
REASON_FIXED_PRIORITY = "routing.fixed_priority"
REASON_COST_LOWEST = "routing.cost_lowest"
REASON_COST_TIE = "routing.cost_tie_stable_priority"
REASON_COST_PRICE_MISSING = "routing.cost_fixed_fallback.price_missing"
REASON_COST_CURRENCY_INCOMPARABLE = "routing.cost_fixed_fallback.currency_incomparable"
REASON_AVAILABILITY_HEALTHY = "routing.availability_healthy"
REASON_AVAILABILITY_UNKNOWN = "routing.availability_unknown"
REASON_SINGLE_ELIGIBLE = "routing.single_eligible_candidate"
REASON_STANDBY = "routing.candidate.standby"
REASON_MODEL_MISSING = "routing.candidate.model_missing"
REASON_MODEL_DISABLED = "routing.candidate.model_disabled"
REASON_PROVIDER_MISSING = "routing.candidate.provider_missing"
REASON_PROVIDER_DISABLED = "routing.candidate.provider_disabled"
REASON_PROVIDER_UNHEALTHY = "routing.candidate.provider_unhealthy"
REASON_KEYPOOL_UNAVAILABLE = "routing.candidate.keypool_unavailable"
REASON_QUALITY_COST_HIGHEST_SCORE = "routing.quality_cost_highest_score"
REASON_QUALITY_COST_SCORE_TIE = "routing.quality_cost_score_tie_stable_priority"
REASON_QUALITY_COST_COLD_START_PRIORITY = "routing.quality_cost_cold_start_priority"
REASON_QUALITY_COST_EVIDENCE_PRIORITY = "routing.quality_cost_evidence_priority"
REASON_QUALITY_COST_PRICE_MISSING = "routing.quality_cost_fixed_fallback.price_missing"
REASON_QUALITY_COST_CURRENCY_INCOMPARABLE = (
    "routing.quality_cost_fixed_fallback.currency_incomparable"
)
REASON_QUALITY_COST_LATENCY_MISSING = "routing.quality_cost_fixed_fallback.latency_missing"
REASON_QUALITY_COST_SINGLE_ELIGIBLE = "routing.quality_cost_single_eligible_candidate"
REASON_QUALITY_BELOW_FLOOR = "routing.candidate.quality_below_floor"
REASON_QUALITY_COLD_START_DEFERRED = "routing.candidate.quality_cold_start_deferred"


@dataclass(frozen=True, slots=True)
class QualityCostConfig:
    """一条 Alias 的效果-成本路由配置。

    三项权重使用整数基点且总和必须为 10000。质量权重不能为零，因为本模式首先是一条
    质量治理策略；成本或延迟可以为零，零权重意味着运行时不得读取相应外部依赖。
    """

    quality_weight_bps: int = 5_000
    cost_weight_bps: int = 3_000
    latency_weight_bps: int = 2_000
    quality_floor_bps: int = 6_000
    minimum_sample_count: int = 20


@dataclass(frozen=True, slots=True)
class RoutingQualityFact:
    """某租户模型在同一质量快照中的安全统计事实。

    调用方必须用 ``tenant_id + model_id`` 读取，但 tenant_id 不进入算法或结果，避免把租户
    标识误当排序维度。对象仅含整数统计、人工先验和稳定版本，不含请求正文或上游错误。
    """

    model_id: str
    model_quality_score_id: str
    sample_count: int
    success_count: int
    error_count: int
    latency_sample_count: int
    total_latency_ms: int
    manual_quality_bps: int | None
    statistics_version: int
    feedback_version: int


@dataclass(frozen=True, slots=True)
class RoutingCandidateInput:
    """一条已按租户加载的 Alias 候选安全事实。

    字段只覆盖排序需要的模型、Provider、健康与 KeyPool 分类，不包含 Provider URL、认证
    头、Key ID/前缀、Prompt 或 Response。default/fallback 与优先级必须一一对应。
    """

    candidate_role: CandidateRole
    configured_priority: int
    model_id: str
    provider_id: str | None
    model_status: ObjectStatus
    provider_status: ObjectStatus
    health_state: HealthState
    health_fresh: bool
    keypool_state: KeyPoolState = "not_evaluated"


@dataclass(frozen=True, slots=True)
class RoutingPriceFact:
    """某候选在同一 ``as_of`` 时点生效的可比较价格事实。

    金额必须由数据库文本直接恢复为 Decimal，不能先经过 float。缺价使用 ``missing``
    构造器显式表达，目录零价只有携带版本时才可由调用方标记为 available。
    """

    model_id: str
    source: PriceSource
    input_price_per_1k: Decimal | None = None
    output_price_per_1k: Decimal | None = None
    currency: str | None = None
    price_schema_version: str | None = None

    @classmethod
    def missing(cls, model_id: str) -> RoutingPriceFact:
        """为没有有效 PricePlan/带版本目录价的模型构造明确缺价事实。"""

        return cls(model_id=model_id, source="missing")


@dataclass(frozen=True, slots=True)
class RoutingTokenShape:
    """从请求正文局部派生的不可逆长度形状，不保留任何原文或摘要。"""

    char_codepoints: int
    direct_token_count: int
    requested_output_tokens: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class TokenEstimate:
    """供成本路由、预算与限流共同引用的有界 Token 估算。"""

    input_tokens: int
    output_tokens: int
    truncated: bool

    @property
    def total_tokens(self) -> int:
        """返回输入与预留输出之和，调用方不得把它冒充 Provider 实际 Usage。"""

        # 合计值只服务请求前准入，不代表上游返回的真实计量结果。
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class RoutingPhaseOneCandidate:
    """第一阶段对单个候选给出的资格结论。"""

    candidate: RoutingCandidateInput
    eligible: bool
    rejection_reason: str | None
    health_rank: int


@dataclass(frozen=True, slots=True)
class RoutingCandidateResult:
    """可直接转换为不可变 RoutingCandidate 数据库事实的纯结果。"""

    candidate: RoutingCandidateInput
    rank: int | None
    status: CandidateFinalStatus
    reason_code: str
    price_available: bool = False
    price_source: PriceSource = "not_evaluated"
    price_schema_version: str | None = None
    currency: str | None = None
    estimated_cost_amount: Decimal | None = None
    # 旧模式和通用资格阶段已拒绝的候选保持 not_evaluated；质量字段必须成组为空。
    quality_evidence_state: QualityEvidenceState = "not_evaluated"
    model_quality_score_id: str | None = None
    quality_statistics_version: int | None = None
    quality_feedback_version: int | None = None
    quality_sample_count: int | None = None
    quality_success_count: int | None = None
    quality_error_count: int | None = None
    manual_quality_bps: int | None = None
    average_latency_ms: Decimal | None = None
    # 质量分随 Score 快照始终冻结；成本、延迟和总分仅在全部正权重依赖齐备后生成。
    quality_component_bps: int | None = None
    cost_component_bps: int | None = None
    latency_component_bps: int | None = None
    weighted_score_bps: int | None = None


@dataclass(frozen=True, slots=True)
class RoutingSelectionResult:
    """一次确定性路由选择及其最多两条候选解释。"""

    routing_mode: RoutingMode
    status: SelectionStatus
    selection_reason: str
    selected_model_id: str | None
    selected_provider_id: str | None
    candidate_results: tuple[RoutingCandidateResult, ...]
    eligible_candidate_count: int
    token_estimate: TokenEstimate | None = None
    cost_comparison_performed: bool = False
    # 只有 quality_cost 冻结公式和配置；旧模式必须保持 NULL/False 以兼容 PRD-047 历史事实。
    quality_formula_version: str | None = None
    quality_config: QualityCostConfig | None = None
    quality_scoring_performed: bool = False


@dataclass(frozen=True, slots=True)
class RoutingPhaseOneResult:
    """资格阶段结果；只有两条 cost 候选合格时才要求价格与 Token。"""

    routing_mode: RoutingMode
    candidates: tuple[RoutingPhaseOneCandidate, ...]
    eligible_candidates: tuple[RoutingPhaseOneCandidate, ...]
    needs_cost_comparison: bool
    final_selection: RoutingSelectionResult | None
    # quality_cost 至少有一条通用合格候选时，必须先读取一次质量快照再决定后续依赖。
    needs_quality_evaluation: bool = False


def build_routing_token_shape(
    request_kind: str,
    payload: Mapping[str, object],
) -> RoutingTokenShape:
    """把 OpenAI-compatible 请求转换成有界且不可逆的 Token 长度形状。

    参数：``request_kind`` 仅接受 ``chat`` 或 ``embeddings``；``payload`` 是已经通过接口
    基础结构校验的内存对象。返回：不包含正文的长度、直接 Token 数、输出预留和截断标记。
    关键约束：最多访问 100,000 个相关节点；越界时输入按 1,000,000 Token 饱和。
    """

    # 未知接口或非对象输入无法按稳定规则估算，必须在接触正文前失败关闭。
    if request_kind not in {"chat", "embeddings"} or not isinstance(payload, Mapping):
        raise ValueError("routing_policy.token_shape_invalid")

    if request_kind == "chat":
        codepoints, direct_tokens, input_truncated = _chat_input_shape(payload.get("messages"))
        requested_output, output_truncated = _requested_output_tokens(payload)
    else:
        codepoints, direct_tokens, input_truncated = _embedding_input_shape(payload.get("input"))
        requested_output = 0
        output_truncated = False

    # 一个超长字符串不增加节点数，但仍可能突破输入 Token 上限，因此字段本身也必须有界。
    bounded_codepoints = min(codepoints, MAX_INPUT_TOKENS * 4)
    bounded_direct_tokens = min(direct_tokens, MAX_INPUT_TOKENS)
    token_overflow = math.ceil(bounded_codepoints / 4) + bounded_direct_tokens > MAX_INPUT_TOKENS
    truncated = (
        input_truncated or output_truncated or token_overflow or codepoints > bounded_codepoints
    )
    if input_truncated:
        # 节点上限意味着未访问部分未知，必须 fail-safe 饱和而不是按已观察前缀低估成本。
        bounded_codepoints = MAX_INPUT_TOKENS * 4
        bounded_direct_tokens = 0
    # 形状对象只携带不可逆计数，可安全交给后续成本计算但不得写入正文衍生摘要。
    return RoutingTokenShape(
        char_codepoints=bounded_codepoints,
        direct_token_count=bounded_direct_tokens,
        requested_output_tokens=requested_output,
        truncated=truncated,
    )


def estimate_token_shape(shape: RoutingTokenShape) -> TokenEstimate:
    """把有界长度形状换算为稳定 Token 数，最少估算一个输入 Token。"""

    _validate_token_shape(shape)
    input_tokens = min(
        MAX_INPUT_TOKENS,
        max(1, math.ceil(shape.char_codepoints / 4) + shape.direct_token_count),
    )
    # 输出估算保持独立字段，预算和成本公式不能只使用总数而丢失输入输出单价差异。
    return TokenEstimate(
        input_tokens=input_tokens,
        output_tokens=shape.requested_output_tokens,
        truncated=shape.truncated,
    )


def estimate_request_tokens(
    request_kind: str,
    payload: Mapping[str, object],
) -> TokenEstimate:
    """按统一规则估算请求 Token，返回值只含数值和截断标记。"""

    # 两步适配复用同一纯函数，避免 Gateway 与 Dry-Run 分别维护字符换算规则。
    return estimate_token_shape(build_routing_token_shape(request_kind, payload))


def quality_component_bps(fact: RoutingQualityFact) -> int:
    """按 ``quality_cost_v1`` 计算带 20 个先验样本的质量分。

    参数：当前租户同一快照中的强类型质量事实。返回：0..10000 的整数基点分。
    关键约束：人工分只是先验，不覆盖真实样本；所有舍入使用 ``ROUND_HALF_UP``，禁止 float。
    """

    _validate_quality_fact(fact)
    prior_bps = fact.manual_quality_bps if fact.manual_quality_bps is not None else 5_000
    numerator = fact.success_count * MAX_BPS + QUALITY_PRIOR_SAMPLE_COUNT * prior_bps
    denominator = fact.sample_count + QUALITY_PRIOR_SAMPLE_COUNT
    return _round_half_up_ratio(numerator, denominator)


def evaluate_routing_candidates_phase_one(
    routing_mode: RoutingMode,
    candidates: Iterable[RoutingCandidateInput],
) -> RoutingPhaseOneResult:
    """执行通用资格过滤，并完成旧模式无需后续事实的选择。

    参数：候选最多两项，且角色/优先级必须是 default=0、fallback=1。返回：若
    ``needs_cost_comparison`` 为真时才允许读取价格并估算 Token；``quality_cost`` 至少有一条
    合格候选时只标记 ``needs_quality_evaluation``，此时必须先读 Score，不能提前读价格。
    """

    normalized_mode = _validate_routing_mode(routing_mode)
    normalized_candidates = _validate_candidates(tuple(candidates), normalized_mode)
    phase_candidates = tuple(
        _qualify_candidate(normalized_mode, candidate) for candidate in normalized_candidates
    )
    eligible = tuple(item for item in phase_candidates if item.eligible)

    if not eligible:
        final = _unavailable_selection(normalized_mode, phase_candidates)
        return RoutingPhaseOneResult(
            routing_mode=normalized_mode,
            candidates=phase_candidates,
            eligible_candidates=(),
            needs_cost_comparison=False,
            final_selection=final,
        )

    # 效果-成本模式必须先加载一次质量事实；单候选也可能因已知低质被门槛拒绝。
    if normalized_mode == "quality_cost":
        return RoutingPhaseOneResult(
            routing_mode=normalized_mode,
            candidates=phase_candidates,
            eligible_candidates=eligible,
            needs_cost_comparison=False,
            final_selection=None,
            needs_quality_evaluation=True,
        )

    # 只有两条动态成本候选均合格时才允许读取价格或扫描请求长度。
    if normalized_mode == "cost_first" and len(eligible) == 2:
        return RoutingPhaseOneResult(
            routing_mode=normalized_mode,
            candidates=phase_candidates,
            eligible_candidates=eligible,
            needs_cost_comparison=True,
            final_selection=None,
        )

    if len(eligible) == 1:
        selection_reason = REASON_SINGLE_ELIGIBLE
        ordered = eligible
    elif normalized_mode == "availability_first":
        ordered = tuple(
            sorted(
                eligible,
                key=lambda item: (
                    item.health_rank,
                    item.candidate.configured_priority,
                    item.candidate.model_id,
                ),
            )
        )
        selection_reason = (
            REASON_AVAILABILITY_HEALTHY
            if ordered[0].health_rank == 0
            else REASON_AVAILABILITY_UNKNOWN
        )
    else:
        # fixed 的稳定顺序只依赖 Alias default/fallback，不读取 KeyPool 或价格。
        ordered = tuple(
            sorted(
                eligible,
                key=lambda item: (item.candidate.configured_priority, item.candidate.model_id),
            )
        )
        selection_reason = REASON_FIXED_PRIORITY

    final = _selected_result(
        normalized_mode,
        phase_candidates,
        ordered,
        selection_reason=selection_reason,
    )
    return RoutingPhaseOneResult(
        routing_mode=normalized_mode,
        candidates=phase_candidates,
        eligible_candidates=eligible,
        needs_cost_comparison=False,
        final_selection=final,
    )


def complete_cost_routing(
    phase_one: RoutingPhaseOneResult,
    price_facts: Iterable[RoutingPriceFact],
    token_estimate: TokenEstimate,
) -> RoutingSelectionResult:
    """用同一 Token 估算和 ``as_of`` 价格完成两候选成本排序。

    缺价或混币不会比较数值，而是退回 Alias 固定优先级并记录明确原因。非法 Decimal、
    未知币种、错误模型映射或不完整价格组属于快照损坏，以 ``ValueError`` 交给包装层映射
    为统一错误码，不能伪装成正常缺价。
    """

    # 第二阶段必须绑定第一阶段的两条 cost 候选，禁止拿旧快照或其他模式结果补价格。
    if (
        phase_one.routing_mode != "cost_first"
        or not phase_one.needs_cost_comparison
        or len(phase_one.eligible_candidates) != 2
        or phase_one.final_selection is not None
    ):
        # 阶段身份不完整时必须拒绝，不能尝试从价格事实反推原候选集合。
        raise ValueError("routing_policy.cost_phase_invalid")
    _validate_token_estimate(token_estimate)

    eligible_by_model = {item.candidate.model_id: item for item in phase_one.eligible_candidates}
    prices: dict[str, RoutingPriceFact] = {}
    for fact in price_facts:
        _validate_price_fact(fact)
        # 价格只能属于本阶段候选且每个模型唯一，防止跨租户或重复事实影响排序。
        if fact.model_id not in eligible_by_model or fact.model_id in prices:
            raise ValueError("routing_policy.price_fact_invalid")
        prices[fact.model_id] = fact
    # 调用方没有返回某模型的价格，等价于权威查询确认缺价；不能把目录默认 0 当免费。
    for model_id in eligible_by_model:
        prices.setdefault(model_id, RoutingPriceFact.missing(model_id))

    available = all(
        prices[model_id].source in {"price_plan", "model_catalog"} for model_id in prices
    )
    # 即使另一条候选缺价，已有显式价格的候选仍必须保存可复现成本；否则会形成
    # price_available=1 但 estimated_cost=NULL 的矛盾事实，并被 0036 成组 CHECK 拒绝。
    costs: dict[str, Decimal] = {
        model_id: _estimated_cost(fact, token_estimate)
        for model_id, fact in prices.items()
        if fact.source in {"price_plan", "model_catalog"}
    }

    currencies = {
        fact.currency for fact in prices.values() if fact.source in {"price_plan", "model_catalog"}
    }
    stable_eligible = tuple(
        sorted(
            phase_one.eligible_candidates,
            key=lambda item: (item.candidate.configured_priority, item.candidate.model_id),
        )
    )
    if not available:
        ordered = stable_eligible
        reason = REASON_COST_PRICE_MISSING
    elif len(currencies) != 1:
        ordered = stable_eligible
        reason = REASON_COST_CURRENCY_INCOMPARABLE
    else:
        ordered = tuple(
            sorted(
                phase_one.eligible_candidates,
                key=lambda item: (
                    costs[item.candidate.model_id],
                    item.candidate.configured_priority,
                    item.candidate.model_id,
                ),
            )
        )
        first_cost = costs[ordered[0].candidate.model_id]
        second_cost = costs[ordered[1].candidate.model_id]
        reason = REASON_COST_TIE if first_cost == second_cost else REASON_COST_LOWEST

    return _selected_result(
        "cost_first",
        phase_one.candidates,
        ordered,
        selection_reason=reason,
        prices=prices,
        costs=costs,
        token_estimate=token_estimate,
        cost_comparison_performed=True,
    )


def complete_quality_cost_routing(
    phase_one: RoutingPhaseOneResult,
    quality_config: QualityCostConfig,
    quality_facts: Iterable[RoutingQualityFact],
    *,
    token_estimator: Callable[[], TokenEstimate] | None = None,
    price_loader: Callable[[tuple[RoutingCandidateInput, ...]], Iterable[RoutingPriceFact]]
    | None = None,
) -> RoutingSelectionResult:
    """完成 ``quality_cost`` 的门槛、依赖检查和确定性加权。

    参数：通用资格阶段、冻结配置、同一租户质量事实，以及仅在成本权重大于零时使用的
    Token/价格延迟回调。返回：可直接持久化的选择与候选解释。关键约束：延迟证据先于
    请求正文和价格检查；单候选、cold 分流、低质拒绝和依赖回退均不产生部分加权分。
    """

    if (
        not isinstance(phase_one, RoutingPhaseOneResult)
        or phase_one.routing_mode != "quality_cost"
        or not phase_one.needs_quality_evaluation
        or phase_one.needs_cost_comparison
        or phase_one.final_selection is not None
        or not phase_one.eligible_candidates
    ):
        # 阶段身份不匹配可能把其他租户或旧模式的 Score 拼入本次决策，必须整体拒绝。
        raise ValueError("routing_policy.quality_phase_invalid")
    _validate_quality_cost_config(quality_config)

    facts = _normalize_quality_facts(phase_one.eligible_candidates, quality_facts)
    quality_scores = {model_id: quality_component_bps(fact) for model_id, fact in facts.items()}
    evidence_states: dict[str, QualityEvidenceState] = {
        model_id: (
            "ready"
            if fact.manual_quality_bps is not None
            or fact.sample_count >= quality_config.minimum_sample_count
            else "cold"
        )
        for model_id, fact in facts.items()
    }
    average_latencies = {model_id: _average_latency_ms(fact) for model_id, fact in facts.items()}
    low_quality_models = {
        model_id
        for model_id, state in evidence_states.items()
        if state == "ready" and quality_scores[model_id] < quality_config.quality_floor_bps
    }
    stable_eligible = tuple(
        sorted(
            phase_one.eligible_candidates,
            key=lambda item: (item.candidate.configured_priority, item.candidate.model_id),
        )
    )
    executable = tuple(
        item for item in stable_eligible if item.candidate.model_id not in low_quality_models
    )

    # 门槛过滤后没有候选时直接 no-route；价格便宜不能重新激活已知低质量模型。
    if not executable:
        return _quality_selection_result(
            phase_one,
            quality_config,
            facts,
            evidence_states,
            average_latencies,
            (),
            low_quality_models=low_quality_models,
            selection_reason=REASON_NO_AVAILABLE_CANDIDATE,
        )

    # 单条可执行候选无需比较；既包含原始单候选，也包含另一条被质量门槛拒绝的情况。
    if len(executable) == 1:
        return _quality_selection_result(
            phase_one,
            quality_config,
            facts,
            evidence_states,
            average_latencies,
            executable,
            low_quality_models=low_quality_models,
            selection_reason=REASON_QUALITY_COST_SINGLE_ELIGIBLE,
        )

    first_model = executable[0].candidate.model_id
    second_model = executable[1].candidate.model_id
    states = (evidence_states[first_model], evidence_states[second_model])
    if states == ("cold", "cold"):
        # 全 cold 时没有足够事实比较，必须沿 Alias 配置顺序启动且不扫描正文或读取价格。
        return _quality_selection_result(
            phase_one,
            quality_config,
            facts,
            evidence_states,
            average_latencies,
            executable,
            low_quality_models=low_quality_models,
            selection_reason=REASON_QUALITY_COST_COLD_START_PRIORITY,
        )
    if "cold" in states:
        # ready-pass 永远优先于 cold，避免无证据模型仅因目录价低而抢占企业流量。
        ordered = tuple(
            sorted(
                executable,
                key=lambda item: (
                    1 if evidence_states[item.candidate.model_id] == "cold" else 0,
                    item.candidate.configured_priority,
                    item.candidate.model_id,
                ),
            )
        )
        deferred_model = next(
            item.candidate.model_id
            for item in ordered
            if evidence_states[item.candidate.model_id] == "cold"
        )
        return _quality_selection_result(
            phase_one,
            quality_config,
            facts,
            evidence_states,
            average_latencies,
            ordered,
            low_quality_models=low_quality_models,
            selection_reason=REASON_QUALITY_COST_EVIDENCE_PRIORITY,
            cold_deferred_model=deferred_model,
        )

    # 到这里恰有两条 ready-pass。延迟缺失必须先于 Token/价格，防止无用正文扫描和依赖读取。
    if quality_config.latency_weight_bps > 0 and any(
        average_latencies[item.candidate.model_id] is None for item in executable
    ):
        return _quality_selection_result(
            phase_one,
            quality_config,
            facts,
            evidence_states,
            average_latencies,
            executable,
            low_quality_models=low_quality_models,
            selection_reason=REASON_QUALITY_COST_LATENCY_MISSING,
        )

    token_estimate: TokenEstimate | None = None
    prices: dict[str, RoutingPriceFact] = {}
    costs: dict[str, Decimal] = {}
    cost_evaluation_performed = False
    # 成本权重大于零才允许扫描正文并读取同一快照价格，零权重必须完全跳过依赖。
    if quality_config.cost_weight_bps > 0:
        # 两个成本回调缺一都会破坏事实完整性，必须在任何正文或价格读取前失败关闭。
        if token_estimator is None or price_loader is None:
            raise ValueError("routing_policy.quality_dependencies_missing")
        # Token 只扫描一次并立即强校验；损坏估算不能继续触发价格读取。
        token_estimate = token_estimator()
        _validate_token_estimate(token_estimate)
        candidate_inputs = tuple(item.candidate for item in executable)
        loaded_prices = price_loader(candidate_inputs)
        prices, costs = _normalize_quality_prices(executable, loaded_prices, token_estimate)
        cost_evaluation_performed = True
        if any(fact.source == "missing" for fact in prices.values()):
            return _quality_selection_result(
                phase_one,
                quality_config,
                facts,
                evidence_states,
                average_latencies,
                executable,
                low_quality_models=low_quality_models,
                selection_reason=REASON_QUALITY_COST_PRICE_MISSING,
                prices=prices,
                costs=costs,
                token_estimate=token_estimate,
                cost_evaluation_performed=True,
            )
        currencies = {fact.currency for fact in prices.values()}
        if len(currencies) != 1:
            return _quality_selection_result(
                phase_one,
                quality_config,
                facts,
                evidence_states,
                average_latencies,
                executable,
                low_quality_models=low_quality_models,
                selection_reason=REASON_QUALITY_COST_CURRENCY_INCOMPARABLE,
                prices=prices,
                costs=costs,
                token_estimate=token_estimate,
                cost_evaluation_performed=True,
            )

    # 所有正权重依赖齐备后才生成分量；零权重分量保持空而不是伪造 0 分。
    quality_components = {
        item.candidate.model_id: quality_scores[item.candidate.model_id] for item in executable
    }
    cost_components = _relative_component_bps(costs) if quality_config.cost_weight_bps > 0 else {}
    latency_values = {
        item.candidate.model_id: average_latencies[item.candidate.model_id]
        for item in executable
        if average_latencies[item.candidate.model_id] is not None
    }
    latency_components = (
        _relative_component_bps(latency_values) if quality_config.latency_weight_bps > 0 else {}
    )
    weighted_scores = {
        item.candidate.model_id: _weighted_quality_cost_score(
            quality_config,
            quality_components[item.candidate.model_id],
            cost_components.get(item.candidate.model_id),
            latency_components.get(item.candidate.model_id),
        )
        for item in executable
    }
    ordered = tuple(
        sorted(
            executable,
            key=lambda item: (
                -weighted_scores[item.candidate.model_id],
                item.candidate.configured_priority,
                item.candidate.model_id,
            ),
        )
    )
    first_score = weighted_scores[ordered[0].candidate.model_id]
    second_score = weighted_scores[ordered[1].candidate.model_id]
    reason = (
        REASON_QUALITY_COST_SCORE_TIE
        if first_score == second_score
        else REASON_QUALITY_COST_HIGHEST_SCORE
    )
    return _quality_selection_result(
        phase_one,
        quality_config,
        facts,
        evidence_states,
        average_latencies,
        ordered,
        low_quality_models=low_quality_models,
        selection_reason=reason,
        prices=prices,
        costs=costs,
        token_estimate=token_estimate,
        cost_evaluation_performed=cost_evaluation_performed,
        quality_components=quality_components,
        cost_components=cost_components,
        latency_components=latency_components,
        weighted_scores=weighted_scores,
        quality_scoring_performed=True,
    )


def select_routing_candidates(
    routing_mode: RoutingMode,
    candidates: Iterable[RoutingCandidateInput],
    *,
    token_estimator: Callable[[], TokenEstimate] | None = None,
    price_loader: Callable[[tuple[RoutingCandidateInput, ...]], Iterable[RoutingPriceFact]]
    | None = None,
    quality_config: QualityCostConfig | None = None,
    quality_loader: Callable[[tuple[RoutingCandidateInput, ...]], Iterable[RoutingQualityFact]]
    | None = None,
) -> RoutingSelectionResult:
    """以延迟回调运行完整路由排序，便于调用方证明最小依赖读取。

    fixed、availability 和 cost 单候选不会调用任何回调；只有 cost 恰有两条合格候选时，
    Token 与价格回调才各调用一次。quality_cost 先且只读取一次 Score，再由证据状态和正权重
    决定后续依赖。所有回调仍由调用方负责在同一租户、同一数据库快照内实现。
    """

    phase_one = evaluate_routing_candidates_phase_one(routing_mode, candidates)
    # 质量模式单独执行配置和 Score 快照合同，旧三模式绝不触发这些延迟依赖。
    if phase_one.routing_mode == "quality_cost":
        # 缺少强类型配置时无法解释门槛和权重，不能静默套用默认配置改变线上行为。
        if quality_config is None:
            raise ValueError("routing_policy.quality_config_invalid")
        _validate_quality_cost_config(quality_config)
        # 零条通用合格候选不读 Score，但仍冻结并校验当前 Policy 配置和公式版本。
        if not phase_one.needs_quality_evaluation:
            if phase_one.final_selection is None:  # pragma: no cover - 第一阶段结构保证完整。
                raise ValueError("routing_policy.phase_one_invalid")
            return replace(
                phase_one.final_selection,
                quality_formula_version=QUALITY_COST_FORMULA_VERSION,
                quality_config=quality_config,
            )
        # 存在通用合格候选时必须读取权威 Score；缺少 loader 不能伪装成冷启动事实。
        if quality_loader is None:
            raise ValueError("routing_policy.quality_dependencies_missing")
        candidate_inputs = tuple(item.candidate for item in phase_one.eligible_candidates)
        # loader 只调用一次；complete 函数只消费冻结 Iterable，不会重新访问数据库。
        quality_facts = quality_loader(candidate_inputs)
        return complete_quality_cost_routing(
            phase_one,
            quality_config,
            quality_facts,
            token_estimator=token_estimator,
            price_loader=price_loader,
        )
    # 非两候选成本分支必须直接返回第一阶段结果，确保两个延迟回调调用次数都为零。
    if not phase_one.needs_cost_comparison:
        if phase_one.final_selection is None:  # pragma: no cover - 数据结构构造已保证完整。
            raise ValueError("routing_policy.phase_one_invalid")
        return phase_one.final_selection
    # 缺少任一延迟依赖时不能猜测成本顺序，调用包装层必须先形成同一快照。
    if token_estimator is None or price_loader is None:
        raise ValueError("routing_policy.cost_dependencies_missing")
    token_estimate = token_estimator()
    candidate_inputs = tuple(item.candidate for item in phase_one.eligible_candidates)
    prices = price_loader(candidate_inputs)
    # 两个回调各执行一次后立即完成排序，不能在完成阶段再次读取变化中的外部事实。
    return complete_cost_routing(phase_one, prices, token_estimate)


def _chat_input_shape(messages: object) -> tuple[int, int, bool]:
    """只访问 Chat message.content 与文本 part，忽略图片、工具参数和其他对象。"""

    if not isinstance(messages, list):
        return 0, 0, False
    node_count = 1
    codepoints = 0
    for message in messages:
        node_count += 1
        # 节点超限表示后续正文形状未知，必须饱和输入而不是按已扫描前缀低估。
        if node_count > MAX_TOKEN_NODES:
            return codepoints, 0, True
        if not isinstance(message, Mapping):
            continue
        content = message.get("content")
        node_count += 1
        # 每个 content 自身也计入资源上限，避免大量空容器绕过扫描边界。
        if node_count > MAX_TOKEN_NODES:
            return codepoints, 0, True
        if isinstance(content, str):
            codepoints += len(content)
            continue
        if not isinstance(content, list):
            continue
        for part in content:
            node_count += 1
            # content part 数量同样受硬上限保护，超限后不保存任何已扫描文本。
            if node_count > MAX_TOKEN_NODES:
                return codepoints, 0, True
            if isinstance(part, str):
                codepoints += len(part)
            elif isinstance(part, Mapping):
                text = part.get("text")
                node_count += 1
                # 对象内 text 是独立标量节点，必须纳入与字符串项相同的计数边界。
                if node_count > MAX_TOKEN_NODES:
                    return codepoints, 0, True
                if isinstance(text, str):
                    codepoints += len(text)
    return codepoints, 0, False


def _embedding_input_shape(value: object) -> tuple[int, int, bool]:
    """迭代扫描嵌套 Embedding input，避免深层数组触发 Python 递归上限。"""

    stack = [value]
    node_count = 0
    codepoints = 0
    direct_tokens = 0
    while stack:
        current = stack.pop()
        node_count += 1
        # 嵌套数组使用同一节点预算；超限时直接返回 fail-safe 截断标记。
        if node_count > MAX_TOKEN_NODES:
            return codepoints, direct_tokens, True
        if isinstance(current, str):
            codepoints += len(current)
        elif type(current) is int:
            # bool 是 int 子类但不属于 OpenAI token ID；其余真正整数均按一个直接 Token 计数。
            direct_tokens += 1
        elif isinstance(current, list):
            # 待处理节点也占用同一预算；先看 O(1) 长度，禁止超宽数组一次性复制进栈后
            # 才发现超限。只在整段可容纳时逐项反向压栈，内存始终受 100k 节点约束。
            remaining_slots = MAX_TOKEN_NODES - node_count - len(stack)
            if len(current) > remaining_slots:
                return codepoints, direct_tokens, True
            for index in range(len(current) - 1, -1, -1):
                stack.append(current[index])
    # 正常遍历完成才返回未截断，调用方据此区分精确估算与预算上限估算。
    return codepoints, direct_tokens, False


def _requested_output_tokens(payload: Mapping[str, object]) -> tuple[int, bool]:
    """按 max_completion_tokens 优先、max_tokens 次之提取正整数输出预留。"""

    primary = payload.get("max_completion_tokens")
    fallback = payload.get("max_tokens")
    value = primary if type(primary) is int and primary > 0 else fallback
    if type(value) is not int or value <= 0:
        return 0, False
    # 超大输出预留被饱和并显式标记，防止成本乘法处理攻击者提供的无界整数。
    return min(value, MAX_OUTPUT_TOKENS), value > MAX_OUTPUT_TOKENS


def _validate_routing_mode(value: object) -> RoutingMode:
    """严格校验路由模式，避免 truthy 对象或大小写漂移改变线上选择。"""

    # 模式是线上行为开关，未知值绝不能默认回退 fixed 后掩盖持久策略损坏。
    if type(value) is not str or value not in _ROUTING_MODES:
        raise ValueError("routing_policy.mode_invalid")
    return value  # type: ignore[return-value]


def _validate_candidates(
    candidates: tuple[RoutingCandidateInput, ...],
    routing_mode: RoutingMode,
) -> tuple[RoutingCandidateInput, ...]:
    """校验候选数量、角色、状态与安全枚举，并按配置优先级稳定排序。"""

    # 超过 Alias 主备上限表示调用方混入了非权威候选，必须整体拒绝而非截断。
    if len(candidates) > MAX_ROUTING_CANDIDATES:
        raise ValueError("routing_policy.candidate_count_invalid")
    roles: set[str] = set()
    model_ids: set[str] = set()
    for candidate in candidates:
        # 只接受冻结数据结构，字典或动态对象可能在两阶段之间被调用方修改。
        if not isinstance(candidate, RoutingCandidateInput):
            raise ValueError("routing_policy.candidate_invalid")
        expected_priority = {"default": 0, "fallback": 1}.get(candidate.candidate_role)
        # 角色与配置优先级必须一一对应，避免伪造 fallback=0 改变稳定平局顺序。
        if expected_priority is None or candidate.configured_priority != expected_priority:
            raise ValueError("routing_policy.candidate_invalid")
        # 重复角色或模型无法持久化为两条唯一候选事实，因此在排序前失败关闭。
        if candidate.candidate_role in roles or candidate.model_id in model_ids:
            raise ValueError("routing_policy.candidate_invalid")
        roles.add(candidate.candidate_role)
        model_ids.add(candidate.model_id)
        # 模型和 Provider ID 会进入审计事实，必须是有界无控制字符的内部标识。
        if not _safe_identifier(candidate.model_id):
            raise ValueError("routing_policy.candidate_invalid")
        # 非空 Provider ID 同样会进入事件和候选事实，控制字符可能污染审计链路。
        if candidate.provider_id is not None and not _safe_identifier(candidate.provider_id):
            raise ValueError("routing_policy.candidate_invalid")
        # 状态和健康分类必须由包装层严格归一化，未知枚举代表快照损坏。
        if candidate.model_status not in _MODEL_STATUSES:
            raise ValueError("routing_policy.candidate_invalid")
        # Provider 未知状态无法判断是否可调用，必须由包装层先完成严格归一化。
        if candidate.provider_status not in _PROVIDER_STATUSES:
            raise ValueError("routing_policy.candidate_invalid")
        # 健康状态与 freshness 必须是强类型，字符串布尔值会改变摘流语义。
        if candidate.health_state not in _HEALTH_STATES or type(candidate.health_fresh) is not bool:
            raise ValueError("routing_policy.candidate_invalid")
        # KeyPool 分类仅允许四个安全枚举，不能携带 Key 数量或错误正文。
        if candidate.keypool_state not in _KEYPOOL_STATES:
            raise ValueError("routing_policy.candidate_invalid")
        # Provider 缺失状态与 ID 必须成组一致，防止保存无法解释的半关联候选。
        if candidate.provider_status == "missing" and candidate.provider_id is not None:
            raise ValueError("routing_policy.candidate_invalid")
        # 非 missing 状态却没有 Provider ID 同样是无法持久化解释的半关联。
        if candidate.provider_status != "missing" and candidate.provider_id is None:
            raise ValueError("routing_policy.candidate_invalid")
        # 动态模式的通用合格候选必须带 KeyPool 结论；被模型/Provider 先行拒绝的候选可不读取。
        generic_eligible = (
            candidate.model_status == "active"
            and candidate.provider_status == "active"
            and not (candidate.health_state == "unhealthy" and candidate.health_fresh)
        )
        # 动态合格候选缺少 KeyPool 结论会绕过凭据过滤，因此不能继续到排序阶段。
        if (
            routing_mode != "fixed"
            and generic_eligible
            and candidate.keypool_state == "not_evaluated"
        ):
            # 动态候选未经过 KeyPool 快照时继续排序会绕过凭据治理边界。
            raise ValueError("routing_policy.candidate_invalid")
    return tuple(sorted(candidates, key=lambda item: (item.configured_priority, item.model_id)))


def _qualify_candidate(
    routing_mode: RoutingMode,
    candidate: RoutingCandidateInput,
) -> RoutingPhaseOneCandidate:
    """按固定错误优先级判断单候选资格，不读取任何外部依赖。"""

    reason: str | None = None
    # 模型错误优先于 Provider/健康，保持既有 Alias 失败原因的稳定业务顺序。
    if candidate.model_status == "missing":
        reason = REASON_MODEL_MISSING
    elif candidate.model_status != "active":
        reason = REASON_MODEL_DISABLED
    # 只有模型 active 后才解释 Provider 状态，避免下游错误覆盖更接近配置源的原因。
    elif candidate.provider_status == "missing":
        reason = REASON_PROVIDER_MISSING
    elif candidate.provider_status != "active":
        reason = REASON_PROVIDER_DISABLED
    elif candidate.health_state == "unhealthy" and candidate.health_fresh:
        reason = REASON_PROVIDER_UNHEALTHY
    elif routing_mode != "fixed" and candidate.keypool_state == "unavailable":
        reason = REASON_KEYPOOL_UNAVAILABLE
    health_rank = 0 if candidate.health_state == "healthy" and candidate.health_fresh else 1
    return RoutingPhaseOneCandidate(
        candidate=(
            replace(candidate, keypool_state="not_evaluated")
            if routing_mode == "fixed"
            else candidate
        ),
        eligible=reason is None,
        rejection_reason=reason,
        health_rank=health_rank,
    )


def _unavailable_selection(
    routing_mode: RoutingMode,
    candidates: tuple[RoutingPhaseOneCandidate, ...],
) -> RoutingSelectionResult:
    """把全部拒绝的资格事实转换成无可用候选结果。"""

    results = tuple(
        RoutingCandidateResult(
            candidate=item.candidate,
            rank=None,
            status="rejected",
            reason_code=item.rejection_reason or REASON_NO_AVAILABLE_CANDIDATE,
        )
        for item in candidates
    )
    return RoutingSelectionResult(
        routing_mode=routing_mode,
        status="unavailable",
        selection_reason=REASON_NO_AVAILABLE_CANDIDATE,
        selected_model_id=None,
        selected_provider_id=None,
        candidate_results=results,
        eligible_candidate_count=0,
    )


def _selected_result(
    routing_mode: RoutingMode,
    all_candidates: tuple[RoutingPhaseOneCandidate, ...],
    ordered_eligible: tuple[RoutingPhaseOneCandidate, ...],
    *,
    selection_reason: str,
    prices: Mapping[str, RoutingPriceFact] | None = None,
    costs: Mapping[str, Decimal] | None = None,
    token_estimate: TokenEstimate | None = None,
    cost_comparison_performed: bool = False,
) -> RoutingSelectionResult:
    """合并排序和拒绝事实，生成配置顺序稳定、rank 独立的最终结果。"""

    rank_by_model = {
        item.candidate.model_id: index for index, item in enumerate(ordered_eligible, start=1)
    }
    results: list[RoutingCandidateResult] = []
    prices = prices or {}
    costs = costs or {}
    for item in all_candidates:
        model_id = item.candidate.model_id
        if not item.eligible:
            results.append(
                RoutingCandidateResult(
                    candidate=item.candidate,
                    rank=None,
                    status="rejected",
                    reason_code=item.rejection_reason or REASON_NO_AVAILABLE_CANDIDATE,
                )
            )
            continue
        rank = rank_by_model[model_id]
        fact = prices.get(model_id)
        source: PriceSource = fact.source if fact is not None else "not_evaluated"
        price_available = source in {"price_plan", "model_catalog"}
        results.append(
            RoutingCandidateResult(
                candidate=item.candidate,
                rank=rank,
                status="selected" if rank == 1 else "standby",
                reason_code=selection_reason if rank == 1 else REASON_STANDBY,
                price_available=price_available,
                price_source=source,
                price_schema_version=fact.price_schema_version
                if price_available and fact
                else None,
                currency=fact.currency if price_available and fact else None,
                estimated_cost_amount=costs.get(model_id),
            )
        )
    selected = ordered_eligible[0].candidate
    return RoutingSelectionResult(
        routing_mode=routing_mode,
        status="selected",
        selection_reason=selection_reason,
        selected_model_id=selected.model_id,
        selected_provider_id=selected.provider_id,
        candidate_results=tuple(results),
        eligible_candidate_count=len(ordered_eligible),
        token_estimate=token_estimate,
        cost_comparison_performed=cost_comparison_performed,
    )


def _quality_selection_result(
    phase_one: RoutingPhaseOneResult,
    config: QualityCostConfig,
    facts: Mapping[str, RoutingQualityFact],
    evidence_states: Mapping[str, QualityEvidenceState],
    average_latencies: Mapping[str, Decimal | None],
    ordered_executable: tuple[RoutingPhaseOneCandidate, ...],
    *,
    low_quality_models: set[str],
    selection_reason: str,
    cold_deferred_model: str | None = None,
    prices: Mapping[str, RoutingPriceFact] | None = None,
    costs: Mapping[str, Decimal] | None = None,
    token_estimate: TokenEstimate | None = None,
    cost_evaluation_performed: bool = False,
    quality_components: Mapping[str, int] | None = None,
    cost_components: Mapping[str, int] | None = None,
    latency_components: Mapping[str, int] | None = None,
    weighted_scores: Mapping[str, int] | None = None,
    quality_scoring_performed: bool = False,
) -> RoutingSelectionResult:
    """把质量门槛、依赖事实和最终排序合并为成组可持久化结果。"""

    rank_by_model = {
        item.candidate.model_id: rank for rank, item in enumerate(ordered_executable, start=1)
    }
    prices = prices or {}
    costs = costs or {}
    # 质量分既用于门槛也属于 ready/cold 行的可复现事实，即使未完成加权也必须持久化。
    if quality_components is None:
        quality_components = {
            model_id: quality_component_bps(fact) for model_id, fact in facts.items()
        }
    cost_components = cost_components or {}
    latency_components = latency_components or {}
    weighted_scores = weighted_scores or {}
    results: list[RoutingCandidateResult] = []
    for phase_candidate in phase_one.candidates:
        candidate = phase_candidate.candidate
        model_id = candidate.model_id
        if not phase_candidate.eligible:
            # 通用资格已拒绝时绝不补读或伪造质量事实，保持 PRD-047 固定原因优先级。
            results.append(
                RoutingCandidateResult(
                    candidate=candidate,
                    rank=None,
                    status="rejected",
                    reason_code=phase_candidate.rejection_reason or REASON_NO_AVAILABLE_CANDIDATE,
                )
            )
            continue

        fact = facts[model_id]
        evidence_state = evidence_states[model_id]
        price = prices.get(model_id)
        price_source: PriceSource = price.source if price is not None else "not_evaluated"
        price_available = price_source in {"price_plan", "model_catalog"}
        # ready 且低于质量门槛的模型必须彻底退出可执行顺序，不能保留 rank 后被运行时复活。
        if model_id in low_quality_models:
            rank = None
            status: CandidateFinalStatus = "rejected"
            reason = REASON_QUALITY_BELOW_FLOOR
        else:
            rank = rank_by_model[model_id]
            status = "selected" if rank == 1 else "standby"
            # selected 使用决策原因；standby 只在 cold 延后时携带专用可解释原因。
            if rank == 1:
                reason = selection_reason
            elif model_id == cold_deferred_model:
                reason = REASON_QUALITY_COLD_START_DEFERRED
            else:
                reason = REASON_STANDBY
        results.append(
            RoutingCandidateResult(
                candidate=candidate,
                rank=rank,
                status=status,
                reason_code=reason,
                price_available=price_available,
                price_source=price_source,
                price_schema_version=(
                    price.price_schema_version if price_available and price is not None else None
                ),
                currency=price.currency if price_available and price is not None else None,
                estimated_cost_amount=costs.get(model_id),
                quality_evidence_state=evidence_state,
                model_quality_score_id=fact.model_quality_score_id,
                quality_statistics_version=fact.statistics_version,
                quality_feedback_version=fact.feedback_version,
                quality_sample_count=fact.sample_count,
                quality_success_count=fact.success_count,
                quality_error_count=fact.error_count,
                manual_quality_bps=fact.manual_quality_bps,
                average_latency_ms=average_latencies[model_id],
                quality_component_bps=quality_components.get(model_id),
                cost_component_bps=cost_components.get(model_id),
                latency_component_bps=latency_components.get(model_id),
                weighted_score_bps=weighted_scores.get(model_id),
            )
        )

    selected = ordered_executable[0].candidate if ordered_executable else None
    return RoutingSelectionResult(
        routing_mode="quality_cost",
        status="selected" if selected is not None else "unavailable",
        selection_reason=selection_reason,
        selected_model_id=selected.model_id if selected is not None else None,
        selected_provider_id=selected.provider_id if selected is not None else None,
        candidate_results=tuple(results),
        eligible_candidate_count=len(ordered_executable),
        token_estimate=token_estimate,
        cost_comparison_performed=cost_evaluation_performed,
        quality_formula_version=QUALITY_COST_FORMULA_VERSION,
        quality_config=config,
        quality_scoring_performed=quality_scoring_performed,
    )


def _validate_quality_cost_config(config: QualityCostConfig) -> None:
    """校验质量配置强类型、范围和 10000 基点总和。"""

    # 动态映射或其他配置类可能在调用期间漂移，纯计算只接受冻结的共享 DTO。
    if not isinstance(config, QualityCostConfig):
        raise ValueError("routing_policy.quality_config_invalid")
    integer_fields = (
        config.quality_weight_bps,
        config.cost_weight_bps,
        config.latency_weight_bps,
        config.quality_floor_bps,
        config.minimum_sample_count,
    )
    # 所有配置值必须是精确整数，尤其不能接受 Python 中可冒充整数的布尔值。
    if any(type(value) is not int for value in integer_fields):
        # bool 是 int 子类，必须用精确类型判断，避免权限配置中的 true 被解释成一个基点。
        raise ValueError("routing_policy.quality_config_invalid")
    # 权重、门槛和样本边界共同决定线上行为，任何越界或总和漂移都必须失败关闭。
    if (
        not 1 <= config.quality_weight_bps <= MAX_BPS
        or not 0 <= config.cost_weight_bps <= MAX_BPS
        or not 0 <= config.latency_weight_bps <= MAX_BPS
        or config.quality_weight_bps + config.cost_weight_bps + config.latency_weight_bps != MAX_BPS
        or not 1 <= config.quality_floor_bps <= MAX_BPS
        or not 1 <= config.minimum_sample_count <= MAX_BPS
    ):
        # 越界配置会改变真实选路语义，不能截断或重新归一化后继续执行。
        raise ValueError("routing_policy.quality_config_invalid")


def _validate_quality_fact(fact: RoutingQualityFact) -> None:
    """校验单条 Score 的 owner 标识、计数组约束和独立双版本。"""

    # Score 身份会进入不可变候选事实，非法或正文形态的标识不能污染审计链路。
    if (
        not isinstance(fact, RoutingQualityFact)
        or not _safe_identifier(fact.model_id)
        or not _safe_identifier(fact.model_quality_score_id)
    ):
        # 非法 Score 身份不能进入候选解释或后续持久化事务。
        raise ValueError("routing_policy.quality_fact_invalid")
    counts = (
        fact.sample_count,
        fact.success_count,
        fact.error_count,
        fact.latency_sample_count,
        fact.total_latency_ms,
        fact.statistics_version,
        fact.feedback_version,
    )
    # 统计与双版本只接受精确整数，避免布尔值或浮点值改变冷启动和门槛语义。
    if any(type(value) is not int for value in counts):
        raise ValueError("routing_policy.quality_fact_invalid")
    # 计数组、延迟组和版本必须来自同一原子快照，任一不自洽都不能继续排序。
    if (
        not 0 <= fact.sample_count <= MAX_SQLITE_INTEGER
        or not 0 <= fact.success_count <= MAX_SQLITE_INTEGER
        or not 0 <= fact.error_count <= MAX_SQLITE_INTEGER
        or fact.success_count + fact.error_count != fact.sample_count
        or not 0 <= fact.latency_sample_count <= fact.sample_count
        or not 0 <= fact.total_latency_ms <= MAX_SQLITE_INTEGER
        or (fact.latency_sample_count == 0 and fact.total_latency_ms != 0)
        or fact.statistics_version != fact.sample_count
        or not 1 <= fact.feedback_version <= MAX_SQLITE_INTEGER
    ):
        # 计数组损坏不能降级成 cold，否则会把本应失败关闭的跨版本快照带入线上排序。
        raise ValueError("routing_policy.quality_fact_invalid")
    # 人工先验允许显式零分，但非空值仍须是精确整数且位于完整基点范围内。
    if fact.manual_quality_bps is not None and (
        type(fact.manual_quality_bps) is not int or not 0 <= fact.manual_quality_bps <= MAX_BPS
    ):
        # 人工先验损坏时必须拒绝整个快照，不能用默认先验掩盖数据问题。
        raise ValueError("routing_policy.quality_fact_invalid")


def _normalize_quality_facts(
    eligible: tuple[RoutingPhaseOneCandidate, ...],
    quality_facts: Iterable[RoutingQualityFact],
) -> dict[str, RoutingQualityFact]:
    """把 loader 结果冻结成候选一一对应的质量映射。"""

    eligible_models = {item.candidate.model_id for item in eligible}
    normalized: dict[str, RoutingQualityFact] = {}
    try:
        for fact in quality_facts:
            _validate_quality_fact(fact)
            # 缺行是损坏而非 cold；多行或非候选行可能来自跨租户/跨快照拼接，全部拒绝。
            # 非候选或重复模型可能来自跨租户拼接，必须在形成评分映射前整体拒绝。
            if fact.model_id not in eligible_models or fact.model_id in normalized:
                raise ValueError("routing_policy.quality_fact_invalid")
            normalized[fact.model_id] = fact
    # loader 不可迭代属于依赖合同损坏，只暴露固定分类而不泄露原始异常正文。
    except TypeError as exc:
        raise ValueError("routing_policy.quality_fact_invalid") from exc
    # Score 表由模型触发器保证一一对应，缺失行不是 cold，必须作为坏快照失败关闭。
    if set(normalized) != eligible_models:
        raise ValueError("routing_policy.quality_fact_invalid")
    return normalized


def _average_latency_ms(fact: RoutingQualityFact) -> Decimal | None:
    """从整数累计值派生三位小数平均延迟；零样本明确返回缺失。"""

    if fact.latency_sample_count == 0:
        return None
    with localcontext() as context:
        # SQLite 两个 64-bit 整数相除最多需要约 38 位有效数字；50 位留下稳定计算余量。
        context.prec = 50
        return (Decimal(fact.total_latency_ms) / Decimal(fact.latency_sample_count)).quantize(
            LATENCY_QUANT,
            rounding=ROUND_HALF_UP,
        )


def _normalize_quality_prices(
    eligible: tuple[RoutingPhaseOneCandidate, ...],
    price_facts: Iterable[RoutingPriceFact],
    token_estimate: TokenEstimate,
) -> tuple[dict[str, RoutingPriceFact], dict[str, Decimal]]:
    """冻结 quality_cost 价格映射，并为显式价格生成同一请求成本。"""

    eligible_models = {item.candidate.model_id for item in eligible}
    prices: dict[str, RoutingPriceFact] = {}
    try:
        for fact in price_facts:
            _validate_price_fact(fact)
            # 跨候选或重复价格可能改变排序结果，必须在成本计算前整体拒绝快照。
            if fact.model_id not in eligible_models or fact.model_id in prices:
                raise ValueError("routing_policy.price_fact_invalid")
            prices[fact.model_id] = fact
    # loader 返回不可迭代对象时统一转换固定错误分类，禁止传播依赖内部异常文本。
    except TypeError as exc:
        raise ValueError("routing_policy.price_fact_invalid") from exc
    for model_id in eligible_models:
        prices.setdefault(model_id, RoutingPriceFact.missing(model_id))
    costs = {
        model_id: _estimated_cost(fact, token_estimate)
        for model_id, fact in prices.items()
        if fact.source in {"price_plan", "model_catalog"}
    }
    return prices, costs


def _relative_component_bps(values: Mapping[str, Decimal]) -> dict[str, int]:
    """按最小非负事实与候选事实之比生成成本或延迟分量。"""

    # 相对分量只能消费非负有限 Decimal；空集合或 float 都会破坏确定性排序。
    if not values or any(
        type(value) is not Decimal or not value.is_finite() or value < 0
        for value in values.values()
    ):
        # 分量事实损坏不能按零分继续，否则会静默改变候选排名。
        raise ValueError("routing_policy.quality_component_invalid")
    minimum = min(values.values())
    if minimum == 0:
        # 全零都代表并列最优；仅部分为零时非零候选不能执行除零，明确记 0 分。
        all_zero = all(value == 0 for value in values.values())
        return {
            model_id: MAX_BPS if all_zero or value == 0 else 0 for model_id, value in values.items()
        }
    try:
        with localcontext() as context:
            context.prec = 50
            return {
                model_id: int(
                    ((minimum / value) * Decimal(MAX_BPS)).quantize(
                        Decimal("1"), rounding=ROUND_HALF_UP
                    )
                )
                for model_id, value in values.items()
            }
    # Decimal 运算异常统一收敛为安全分类，避免底层精度上下文信息进入调用日志。
    except (InvalidOperation, OverflowError, ZeroDivisionError) as exc:
        raise ValueError("routing_policy.quality_component_invalid") from exc


def _weighted_quality_cost_score(
    config: QualityCostConfig,
    quality_bps: int,
    cost_bps: int | None,
    latency_bps: int | None,
) -> int:
    """仅在正权重分量齐备时计算最终整数基点分。"""

    # 任一正权重分量缺失都代表依赖检查被绕过，不能按零分或重分权继续计算。
    if (
        not 0 <= quality_bps <= MAX_BPS
        or (config.cost_weight_bps > 0 and cost_bps is None)
        or (config.latency_weight_bps > 0 and latency_bps is None)
    ):
        # 缺失正权重证据时必须失败关闭，禁止重分权或伪造零分。
        raise ValueError("routing_policy.quality_component_invalid")
    numerator = quality_bps * config.quality_weight_bps
    # 成本分量只有正成本权重时才存在，存在时按原始配置权重参与总分。
    if cost_bps is not None:
        numerator += cost_bps * config.cost_weight_bps
    # 延迟分量同样只在正权重且证据齐备时进入总分，零权重保持空值。
    if latency_bps is not None:
        numerator += latency_bps * config.latency_weight_bps
    return _round_half_up_ratio(numerator, MAX_BPS)


def _round_half_up_ratio(numerator: int, denominator: int) -> int:
    """对非负整数比值做精确 ROUND_HALF_UP，避免 Decimal 上下文或 float 漂移。"""

    # 该舍入器只服务非负基点公式，非法符号或零分母表示内部合同被破坏。
    if numerator < 0 or denominator <= 0:
        raise ValueError("routing_policy.quality_component_invalid")
    quotient, remainder = divmod(numerator, denominator)
    return quotient + (1 if remainder * 2 >= denominator else 0)


def _validate_price_fact(fact: RoutingPriceFact) -> None:
    """严格校验价格事实成组约束，区分正常缺价与持久数据损坏。"""

    # 价格对象和模型 ID 必须是共享模块定义的冻结安全事实，禁止动态映射混入。
    if not isinstance(fact, RoutingPriceFact) or not _safe_identifier(fact.model_id):
        raise ValueError("routing_policy.price_fact_invalid")
    # not_evaluated 只属于未进入成本阶段的持久表示，不能伪装成价格查询结果。
    if fact.source not in _PRICE_SOURCES or fact.source == "not_evaluated":
        raise ValueError("routing_policy.price_fact_invalid")
    values = (fact.input_price_per_1k, fact.output_price_per_1k)
    # 正常缺价的所有金额、币种和版本字段都必须为空，以区别损坏的半组价格。
    if fact.source == "missing":
        if any(value is not None for value in (*values, fact.currency, fact.price_schema_version)):
            raise ValueError("routing_policy.price_fact_invalid")
        return
    # 显式价格必须直接使用 Decimal；float 会在排序前产生不可复现的二进制误差。
    if any(type(value) is not Decimal for value in values):
        raise ValueError("routing_policy.price_fact_invalid")
    for value in values:
        # 单价沿用价格管理入口的非负、有限、上限和 12 位小数约束。
        if (
            value is None
            or not value.is_finite()
            or value < 0
            or value > MAX_PRICE_PER_1K
            or max(-value.as_tuple().exponent, 0) > MAX_PRICE_DECIMAL_PLACES
        ):
            # 任一越界单价都代表快照损坏，不能降级成正常缺价后按 fixed 放行。
            raise ValueError("routing_policy.price_fact_invalid")
    # 未知币种或无版本价格不是“缺价”，而是必须由包装层报错的持久快照损坏。
    if fact.currency not in _CURRENCIES or not _safe_identifier(fact.price_schema_version):
        raise ValueError("routing_policy.price_fact_invalid")


def _estimated_cost(fact: RoutingPriceFact, estimate: TokenEstimate) -> Decimal:
    """在高精度局部上下文中计算并按 15 位小数 ROUND_HALF_UP 量化成本。"""

    # 成本公式只接受已经通过成组校验的双单价，缺列不能按零值继续计算。
    if fact.input_price_per_1k is None or fact.output_price_per_1k is None:
        raise ValueError("routing_policy.price_fact_invalid")
    try:
        with localcontext() as context:
            context.prec = 50
            amount = (
                Decimal(estimate.input_tokens) * fact.input_price_per_1k
                + Decimal(estimate.output_tokens) * fact.output_price_per_1k
            ) / Decimal(1000)
            # Decimal 运算结果仍需再次检查有限性，防止异常上下文产生不可持久化金额。
            if not amount.is_finite():
                raise ValueError("routing_policy.price_fact_invalid")
            # 排序直接使用将要持久化的 15 位值，避免内存精度与解释事实不一致。
            return amount.quantize(COST_QUANT, rounding=ROUND_HALF_UP)
    # 高精度运算异常只暴露固定分类，禁止 Decimal 内部消息进入 API 或日志。
    except (InvalidOperation, OverflowError) as exc:
        raise ValueError("routing_policy.price_fact_invalid") from exc


def _validate_token_shape(shape: RoutingTokenShape) -> None:
    """校验长度形状的数值边界，防止外部手工构造绕过饱和合同。"""

    # 手工构造的越界形状会破坏饱和保证，必须在任何成本乘法前拒绝。
    if (
        not isinstance(shape, RoutingTokenShape)
        or type(shape.char_codepoints) is not int
        or not 0 <= shape.char_codepoints <= MAX_INPUT_TOKENS * 4
        or type(shape.direct_token_count) is not int
        or not 0 <= shape.direct_token_count <= MAX_INPUT_TOKENS
        or type(shape.requested_output_tokens) is not int
        or not 0 <= shape.requested_output_tokens <= MAX_OUTPUT_TOKENS
        or type(shape.truncated) is not bool
    ):
        # 非法形状不能进入成本公式，否则饱和与截断事实将不再可复现。
        raise ValueError("routing_policy.token_shape_invalid")


def _validate_token_estimate(estimate: TokenEstimate) -> None:
    """校验成本阶段只消费规范估算值，不接受 bool、负数或无界整数。"""

    # 成本阶段只接受规范估算对象，bool 和无界整数不能利用 Python 类型兼容绕过。
    if (
        not isinstance(estimate, TokenEstimate)
        or type(estimate.input_tokens) is not int
        or not 1 <= estimate.input_tokens <= MAX_INPUT_TOKENS
        or type(estimate.output_tokens) is not int
        or not 0 <= estimate.output_tokens <= MAX_OUTPUT_TOKENS
        or type(estimate.truncated) is not bool
    ):
        # 非法估算必须由调用包装层统一映射，不能修剪后伪造为原始共享结果。
        raise ValueError("routing_policy.token_estimate_invalid")


def _safe_identifier(value: object) -> bool:
    """校验内部 ID/版本为有界无控制字符文本，不把正文伪装成安全元数据。"""

    return (
        type(value) is str
        and 1 <= len(value) <= 128
        and not any(character.isspace() or ord(character) < 32 for character in value)
    )


__all__ = [
    "CandidateFinalStatus",
    "CandidateRole",
    "COST_QUANT",
    "HealthState",
    "KeyPoolState",
    "LATENCY_QUANT",
    "MAX_BPS",
    "MAX_INPUT_TOKENS",
    "MAX_OUTPUT_TOKENS",
    "MAX_ROUTING_CANDIDATES",
    "MAX_TOKEN_NODES",
    "ObjectStatus",
    "PriceSource",
    "QUALITY_COST_FORMULA_VERSION",
    "QualityCostConfig",
    "QualityEvidenceState",
    "REASON_AVAILABILITY_HEALTHY",
    "REASON_AVAILABILITY_UNKNOWN",
    "REASON_COST_CURRENCY_INCOMPARABLE",
    "REASON_COST_LOWEST",
    "REASON_COST_PRICE_MISSING",
    "REASON_COST_TIE",
    "REASON_FIXED_PRIORITY",
    "REASON_KEYPOOL_UNAVAILABLE",
    "REASON_MODEL_DISABLED",
    "REASON_MODEL_MISSING",
    "REASON_NO_AVAILABLE_CANDIDATE",
    "REASON_PROVIDER_DISABLED",
    "REASON_PROVIDER_MISSING",
    "REASON_PROVIDER_UNHEALTHY",
    "REASON_QUALITY_BELOW_FLOOR",
    "REASON_QUALITY_COLD_START_DEFERRED",
    "REASON_QUALITY_COST_COLD_START_PRIORITY",
    "REASON_QUALITY_COST_CURRENCY_INCOMPARABLE",
    "REASON_QUALITY_COST_EVIDENCE_PRIORITY",
    "REASON_QUALITY_COST_HIGHEST_SCORE",
    "REASON_QUALITY_COST_LATENCY_MISSING",
    "REASON_QUALITY_COST_PRICE_MISSING",
    "REASON_QUALITY_COST_SCORE_TIE",
    "REASON_QUALITY_COST_SINGLE_ELIGIBLE",
    "REASON_SINGLE_ELIGIBLE",
    "REASON_STANDBY",
    "RoutingCandidateInput",
    "RoutingCandidateResult",
    "RoutingPhaseOneCandidate",
    "RoutingPhaseOneResult",
    "RoutingPriceFact",
    "RoutingQualityFact",
    "RoutingSelectionResult",
    "RoutingTokenShape",
    "RoutingMode",
    "SelectionStatus",
    "TokenEstimate",
    "build_routing_token_shape",
    "complete_cost_routing",
    "complete_quality_cost_routing",
    "estimate_request_tokens",
    "estimate_token_shape",
    "evaluate_routing_candidates_phase_one",
    "quality_component_bps",
    "select_routing_candidates",
]
