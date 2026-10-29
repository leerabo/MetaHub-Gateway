"""PRD-075 模型实验的整数统计合同。

本模块只接受已经由数据库 owner 图验证过的请求级安全事实。它不接收 Prompt、Response、
Provider Key、Token、Endpoint 或自由错误正文，并以 Python 任意精度整数完成中间运算，
最终再验证 SQLite signed-64 边界，避免 float 舍入和数据库乘法溢出。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from metahub_shared.feedback import round_ratio_half_up

from .contracts import MAX_EXPERIMENT_SAMPLE_COUNT, ExperimentVariant

# 所有最终可持久化计数与金额都受 SQLite signed-64 上界约束，中间计算仍使用 Python 任意精度整数。
SQLITE_SIGNED_64_MAX = 9_223_372_036_854_775_807

GatewayOutcome = Literal["success", "failed", "unresolved"]
ProviderExposure = Literal["not_exposed", "exposed", "unknown"]
SettlementStatus = Literal["pending", "recorded", "not_exposed", "unavailable"]


@dataclass(frozen=True, slots=True)
class ExperimentRequestFact:
    """一个 Assignment 的终态/暂态安全投影。

    ``gross_cost_cny_micros=None`` 在 ``recorded`` 状态表示账本已存在但归一化金额未知，
    不能被当成零成本。反馈字段必须成对出现，避免页面从不完整事实自行猜测。
    """

    variant: ExperimentVariant
    gateway_outcome: GatewayOutcome
    exposure: ProviderExposure
    quality_eligible: bool
    quality_succeeded: bool | None
    latency_ms: int | None
    settlement_status: SettlementStatus
    gross_cost_cny_micros: int | None
    feedback_rating: int | None = None
    feedback_accepted: bool | None = None


@dataclass(frozen=True, slots=True)
class ExperimentArmStatistics:
    """同一 SQLite snapshot 中一个实验 arm 的权威整数汇总。"""

    variant: ExperimentVariant
    assigned_count: int
    gateway_success_count: int
    gateway_failure_count: int
    unresolved_count: int
    gateway_success_rate_bps: int | None
    exposed_count: int
    not_exposed_count: int
    exposure_unknown_count: int
    quality_success_count: int
    quality_failure_count: int
    quality_success_rate_bps: int | None
    latency_sample_count: int
    total_latency_ms: int
    average_latency_micros: int | None
    p95_latency_ms: int | None
    settlement_recorded_count: int
    settlement_not_exposed_count: int
    settlement_unavailable_count: int
    settlement_pending_count: int
    gross_cost_cny_micros: int
    cost_known_count: int
    cost_unknown_count: int
    average_cost_cny_micros: int | None
    feedback_count: int
    rating_sum: int
    average_rating_milli: int
    accepted_count: int
    adoption_rate_bps: int


def aggregate_experiment_arm(
    variant: ExperimentVariant,
    facts: tuple[ExperimentRequestFact, ...] | list[ExperimentRequestFact],
) -> ExperimentArmStatistics:
    """聚合同一 arm 的事实，并拒绝 owner 漂移、非法矩阵和整数溢出。"""

    if variant not in {"a", "b"} or type(facts) not in {tuple, list}:
        # 只接受冻结的两臂枚举和显式序列，拒绝生成器在聚合期间改变事实集合。
        raise ValueError("experiment.statistics_invalid")
    if len(facts) > MAX_EXPERIMENT_SAMPLE_COUNT:
        # 汇总行数不得超过实验冻结样本上限，异常 owner 图不能被截断后返回部分统计。
        raise ValueError("experiment.statistics_invalid")

    gateway_success = gateway_failure = unresolved = 0
    exposed = not_exposed = exposure_unknown = 0
    quality_success = quality_failure = 0
    settlement_recorded = settlement_not_exposed = 0
    settlement_unavailable = settlement_pending = 0
    gross_cost = cost_known = cost_unknown = 0
    feedback_count = rating_sum = accepted_count = 0
    latencies: list[int] = []

    for fact in facts:
        _validate_fact(fact, expected_variant=variant)
        gateway_success += fact.gateway_outcome == "success"
        gateway_failure += fact.gateway_outcome == "failed"
        unresolved += fact.gateway_outcome == "unresolved"
        exposed += fact.exposure == "exposed"
        not_exposed += fact.exposure == "not_exposed"
        exposure_unknown += fact.exposure == "unknown"

        if fact.quality_eligible:
            quality_success += fact.quality_succeeded is True
            quality_failure += fact.quality_succeeded is False
            # eligibility 合同保证 latency 为非负真实整数，不能把本地预检耗时混入。
            latencies.append(fact.latency_ms)  # type: ignore[arg-type]

        settlement_recorded += fact.settlement_status == "recorded"
        settlement_not_exposed += fact.settlement_status == "not_exposed"
        settlement_unavailable += fact.settlement_status == "unavailable"
        settlement_pending += fact.settlement_status == "pending"
        # 只有已入账且完成币种归一的 gross usage charge 才能累加，未知金额不能当零成本。
        if fact.settlement_status == "recorded" and fact.gross_cost_cny_micros is not None:
            gross_cost += fact.gross_cost_cny_micros
            cost_known += 1
        if (
            fact.exposure == "unknown"
            or fact.settlement_status == "unavailable"
            or (
                fact.settlement_status == "recorded"
                and fact.gross_cost_cny_micros is None
            )
        ):
            # 一条请求即使同时命中多个未知条件也只计一次，避免 unknown 重复放大。
            cost_unknown += 1

        if fact.feedback_rating is not None:
            feedback_count += 1
            rating_sum += fact.feedback_rating
            accepted_count += fact.feedback_accepted is True

    total_latency = sum(latencies)
    _ensure_signed_64(
        total_latency,
        gross_cost,
        rating_sum,
        *(len(facts), gateway_success, gateway_failure, unresolved),
    )
    latency_count = len(latencies)
    sorted_latencies = sorted(latencies)
    p95_index = ((95 * latency_count + 99) // 100) - 1 if latency_count else None
    gateway_denominator = gateway_success + gateway_failure
    quality_denominator = quality_success + quality_failure

    return ExperimentArmStatistics(
        variant=variant,
        assigned_count=len(facts),
        gateway_success_count=gateway_success,
        gateway_failure_count=gateway_failure,
        unresolved_count=unresolved,
        gateway_success_rate_bps=_optional_ratio(
            gateway_success, gateway_denominator, 10_000
        ),
        exposed_count=exposed,
        not_exposed_count=not_exposed,
        exposure_unknown_count=exposure_unknown,
        quality_success_count=quality_success,
        quality_failure_count=quality_failure,
        quality_success_rate_bps=_optional_ratio(
            quality_success, quality_denominator, 10_000
        ),
        latency_sample_count=latency_count,
        total_latency_ms=total_latency,
        average_latency_micros=_optional_ratio(
            total_latency, latency_count, 1_000
        ),
        p95_latency_ms=(sorted_latencies[p95_index] if p95_index is not None else None),
        settlement_recorded_count=settlement_recorded,
        settlement_not_exposed_count=settlement_not_exposed,
        settlement_unavailable_count=settlement_unavailable,
        settlement_pending_count=settlement_pending,
        gross_cost_cny_micros=gross_cost,
        cost_known_count=cost_known,
        cost_unknown_count=cost_unknown,
        average_cost_cny_micros=_optional_ratio(gross_cost, cost_known, 1),
        feedback_count=feedback_count,
        rating_sum=rating_sum,
        average_rating_milli=round_ratio_half_up(
            rating_sum, feedback_count, 1_000
        ),
        accepted_count=accepted_count,
        adoption_rate_bps=round_ratio_half_up(
            accepted_count, feedback_count, 10_000
        ),
    )


def _validate_fact(
    fact: ExperimentRequestFact,
    *,
    expected_variant: ExperimentVariant,
) -> None:
    """验证请求级事实矩阵，禁止用默认值修补损坏的持久 owner 图。"""

    if not isinstance(fact, ExperimentRequestFact) or fact.variant != expected_variant:
        # 请求事实必须属于正在聚合的同一 arm，跨臂 owner 漂移不能通过默认值修复。
        raise ValueError("experiment.statistics_invalid")
    if fact.gateway_outcome not in {"success", "failed", "unresolved"}:
        # Gateway 终态只接受冻结枚举，缺失或扩展值必须先升级统计合同。
        raise ValueError("experiment.statistics_invalid")
    if fact.exposure not in {"not_exposed", "exposed", "unknown"}:
        # Provider 暴露是独立三态，不能从 Attempt 存在与否推断或折叠 unknown。
        raise ValueError("experiment.statistics_invalid")
    if fact.settlement_status not in {
        "pending",
        "recorded",
        "not_exposed",
        "unavailable",
    }:
        # 结算状态必须保留 pending/unavailable，禁止把未知用量伪装成已知零成本。
        raise ValueError("experiment.statistics_invalid")
    if type(fact.quality_eligible) is not bool:
        # eligibility 只接受严格布尔，整数真值会污染模型质量分母。
        raise ValueError("experiment.statistics_invalid")
    if fact.quality_eligible:
        if (
            fact.exposure != "exposed"
            or type(fact.quality_succeeded) is not bool
            or type(fact.latency_ms) is not int
            or fact.latency_ms < 0
        ):
            # 可计质量事实必须已暴露，并同时具备布尔结果和非负整数延迟。
            raise ValueError("experiment.statistics_invalid")
    elif fact.quality_succeeded is not None or fact.latency_ms is not None:
        # 不可计质量事实不得夹带成功或延迟，避免页面从残留字段重算指标。
        raise ValueError("experiment.statistics_invalid")
    if fact.settlement_status == "recorded":
        if fact.gross_cost_cny_micros is not None and (
            type(fact.gross_cost_cny_micros) is not int
            or not 0 <= fact.gross_cost_cny_micros <= SQLITE_SIGNED_64_MAX
        ):
            # 已入账金额若存在必须是 signed-64 范围内的非负 CNY micros。
            raise ValueError("experiment.statistics_invalid")
    elif fact.gross_cost_cny_micros is not None:
        # 未入账状态不能携带金额，尤其不得把待结算或未暴露请求写成零成本。
        raise ValueError("experiment.statistics_invalid")
    feedback_pair = (fact.feedback_rating, fact.feedback_accepted)
    if feedback_pair == (None, None):
        return
    if (
        type(fact.feedback_rating) is not int
        or not 1 <= fact.feedback_rating <= 5
        or type(fact.feedback_accepted) is not bool
    ):
        # 反馈评分与采纳值必须成对且严格有界，避免不完整事实改变反馈分母。
        raise ValueError("experiment.statistics_invalid")


def _optional_ratio(numerator: int, denominator: int, scale: int) -> int | None:
    """按 half-up 返回有分母的指标；无分母保持 null，而不是伪造零。"""

    if denominator == 0:
        return None
    return round_ratio_half_up(numerator, denominator, scale)


def _ensure_signed_64(*values: int) -> None:
    """最终可持久化整数必须处于 SQLite signed-64 非负范围。"""

    if any(type(value) is not int or not 0 <= value <= SQLITE_SIGNED_64_MAX for value in values):
        # 任一总计越界就拒绝整个结果，不能截断后返回看似有效的部分统计。
        raise ValueError("experiment.statistics_invalid")
