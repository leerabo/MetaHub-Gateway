"""实验统计读取：只从冻结结果和聚合事实构造 A/B 指标，不接触 Prompt 或 Response。

Single-snapshot provisional and final statistics for PRD-075.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from metahub_shared.experiments import (
    ExperimentArmStatistics,
    ExperimentRequestFact,
    aggregate_experiment_arm,
)

from .models import ExperimentArmResult


@dataclass(frozen=True, slots=True)
class ExperimentStatisticsSnapshot:
    """规范化统计布尔、计数和时间字段，异常数据库值按不可用处理。"""
    arms: tuple[ExperimentArmResult, ExperimentArmResult]
    facts_version: int
    facts_through_at: datetime
    pending_execution_count: int
    pending_settlement_count: int


def load_statistics_snapshot(
    connection: sqlite3.Connection,
    experiment: sqlite3.Row,
    *,
    database_now: datetime,
) -> ExperimentStatisticsSnapshot:
    """按实验状态加载暂态或冻结统计快照，所有查询均绑定同一租户实验。

    Load provisional facts or the two immutable final rows in one read transaction.
    """

    if experiment["status"] == "completed":
        return _load_final(connection, experiment)
    if experiment["status"] not in {"active", "collecting_feedback"}:
        # 统计所有者、计数或冻结结果异常时返回不可用，不拼接不同租户或阶段的数据。
        raise ValueError("experiment.statistics_invalid")
    return _load_provisional(connection, experiment, database_now=database_now)


def _load_provisional(
    connection: sqlite3.Connection,
    experiment: sqlite3.Row,
    *,
    database_now: datetime,
) -> ExperimentStatisticsSnapshot:
    """按实验状态加载暂态或冻结统计快照，所有查询均绑定同一租户实验。"""
    rows = connection.execute(
        """
        SELECT assignment.assignment_id,assignment.variant,assignment.model_id,
               assignment.provider_id,assignment.provider_code,
               execution.status AS execution_status,
               outcome.gateway_status,outcome.exposure_status,outcome.quality_eligible,
               outcome.quality_succeeded,outcome.latency_ms,
               settlement.status AS settlement_status,settlement.normalization_status,
               settlement.gross_cny_micros,
               attribution.rating,attribution.accepted
        FROM experiment_assignments assignment
        JOIN experiment_request_executions execution
          ON execution.tenant_id=assignment.tenant_id
         AND execution.assignment_id=assignment.assignment_id
         AND execution.execution_id=assignment.execution_id
        JOIN experiment_usage_settlements settlement
          ON settlement.tenant_id=assignment.tenant_id
         AND settlement.assignment_id=assignment.assignment_id
         AND settlement.usage_settlement_id=assignment.usage_settlement_id
        LEFT JOIN experiment_request_outcomes outcome
          ON outcome.tenant_id=assignment.tenant_id
         AND outcome.assignment_id=assignment.assignment_id
         AND outcome.execution_id=assignment.execution_id
        LEFT JOIN experiment_feedback_attributions attribution
          ON attribution.tenant_id=assignment.tenant_id
         AND attribution.assignment_id=assignment.assignment_id
        WHERE assignment.tenant_id=? AND assignment.experiment_id=?
        ORDER BY assignment.assignment_sequence,assignment.assignment_id
        """,
        (experiment["tenant_id"], experiment["experiment_id"]),
    ).fetchall()
    if len(rows) != int(experiment["assigned_count"]):
        # 统计所有者、计数或冻结结果异常时返回不可用，不拼接不同租户或阶段的数据。
        raise ValueError("experiment.statistics_invalid")

    facts: dict[str, list[ExperimentRequestFact]] = {"a": [], "b": []}
    pending_execution_count = 0
    pending_settlement_count = 0
    for row in rows:
        variant = str(row["variant"])
        expected = _arm_owner(experiment, variant)
        if (
            str(row["model_id"]) != expected[0]
            or str(row["provider_id"]) != expected[1]
            or str(row["provider_code"]) != expected[2]
        ):
            # 统计所有者、计数或冻结结果异常时返回不可用，不拼接不同租户或阶段的数据。
            raise ValueError("experiment.statistics_invalid")
        execution_pending = row["execution_status"] != "terminal"
        pending_execution_count += execution_pending
        settlement_status = str(row["settlement_status"])
        pending_settlement_count += settlement_status == "pending"
        if row["gateway_status"] is None:
            if not execution_pending:
                # 统计所有者、计数或冻结结果异常时返回不可用，不拼接不同租户或阶段的数据。
                raise ValueError("experiment.statistics_invalid")
            gateway_status = "unresolved"
            exposure_status = "unknown"
            quality_eligible = False
            quality_succeeded = None
            latency_ms = None
        else:
            if execution_pending:
                # 统计所有者、计数或冻结结果异常时返回不可用，不拼接不同租户或阶段的数据。
                raise ValueError("experiment.statistics_invalid")
            gateway_status = str(row["gateway_status"])
            exposure_status = str(row["exposure_status"])
            quality_eligible = _db_bool(row["quality_eligible"])
            quality_succeeded = _optional_db_bool(row["quality_succeeded"])
            latency_ms = row["latency_ms"]
        gross_cost = (
            row["gross_cny_micros"]
            if settlement_status == "recorded"
            and row["normalization_status"] == "normalized"
            else None
        )
        feedback_rating = row["rating"]
        feedback_accepted = _optional_db_bool(row["accepted"])
        if not bool(experiment["feedback_collection_enabled"]) and (
            feedback_rating is not None or feedback_accepted is not None
        ):
            # 统计所有者、计数或冻结结果异常时返回不可用，不拼接不同租户或阶段的数据。
            raise ValueError("experiment.statistics_invalid")
        facts[variant].append(
            ExperimentRequestFact(
                variant=variant,  # type: ignore[arg-type]
                gateway_outcome=gateway_status,  # type: ignore[arg-type]
                exposure=exposure_status,  # type: ignore[arg-type]
                quality_eligible=quality_eligible,
                quality_succeeded=quality_succeeded,
                latency_ms=latency_ms,
                settlement_status=settlement_status,  # type: ignore[arg-type]
                gross_cost_cny_micros=gross_cost,
                feedback_rating=feedback_rating,
                feedback_accepted=feedback_accepted,
            )
        )

    a_stats = aggregate_experiment_arm("a", facts["a"])
    b_stats = aggregate_experiment_arm("b", facts["b"])
    if (
        a_stats.assigned_count != int(experiment["model_a_assigned_count"])
        or b_stats.assigned_count != int(experiment["model_b_assigned_count"])
    ):
        # 统计所有者、计数或冻结结果异常时返回不可用，不拼接不同租户或阶段的数据。
        raise ValueError("experiment.statistics_invalid")
    return ExperimentStatisticsSnapshot(
        arms=(
            _arm_result(experiment, a_stats),
            _arm_result(experiment, b_stats),
        ),
        facts_version=_nonnegative_int(experiment["facts_version"]),
        facts_through_at=database_now,
        pending_execution_count=pending_execution_count,
        pending_settlement_count=pending_settlement_count,
    )


def _load_final(
    connection: sqlite3.Connection,
    experiment: sqlite3.Row,
) -> ExperimentStatisticsSnapshot:
    """按实验状态加载暂态或冻结统计快照，所有查询均绑定同一租户实验。"""
    rows = connection.execute(
        """
        SELECT * FROM experiment_results
        WHERE tenant_id=? AND experiment_id=?
        ORDER BY variant
        """,
        (experiment["tenant_id"], experiment["experiment_id"]),
    ).fetchall()
    if len(rows) != 2 or [row["variant"] for row in rows] != ["a", "b"]:
        # 统计所有者、计数或冻结结果异常时返回不可用，不拼接不同租户或阶段的数据。
        raise ValueError("experiment.statistics_invalid")
    facts_version = _nonnegative_int(experiment["facts_version"])
    facts_through = rows[0]["facts_through_at"]
    results: list[ExperimentArmResult] = []
    for row in rows:
        variant = str(row["variant"])
        model_id, provider_id, provider_code = _arm_owner(experiment, variant)
        expected_assigned = int(
            experiment[
                "model_a_assigned_count" if variant == "a" else "model_b_assigned_count"
            ]
        )
        if (
            row["model_id"] != model_id
            or row["provider_id"] != provider_id
            or row["provider_code"] != provider_code
            or int(row["assigned_count"]) != expected_assigned
            or int(row["facts_version"]) != facts_version
            or row["facts_through_at"] != facts_through
            or int(row["feedback_enabled"])
            != int(experiment["feedback_collection_enabled"])
        ):
            # 统计所有者、计数或冻结结果异常时返回不可用，不拼接不同租户或阶段的数据。
            raise ValueError("experiment.statistics_invalid")
        results.append(_final_arm_result(row))
    if sum(result.assigned_count for result in results) != int(experiment["assigned_count"]):
        # 统计所有者、计数或冻结结果异常时返回不可用，不拼接不同租户或阶段的数据。
        raise ValueError("experiment.statistics_invalid")
    return ExperimentStatisticsSnapshot(
        arms=(results[0], results[1]),
        facts_version=facts_version,
        facts_through_at=_time(facts_through),
        pending_execution_count=0,
        pending_settlement_count=0,
    )


def _arm_result(
    experiment: sqlite3.Row,
    statistics: ExperimentArmStatistics,
) -> ExperimentArmResult:
    """构造单臂聚合结果，样本数、成功率、成本和延迟只取自权威事实。"""
    model_id, provider_id, provider_code = _arm_owner(experiment, statistics.variant)
    values = statistics.__dict__ if hasattr(statistics, "__dict__") else {
        field: getattr(statistics, field)
        for field in statistics.__dataclass_fields__
    }
    return ExperimentArmResult(
        **values,
        model_id=model_id,
        provider_id=provider_id,
        provider_code=provider_code,
        feedback_collection_enabled=bool(experiment["feedback_collection_enabled"]),
    )


def _final_arm_result(row: sqlite3.Row) -> ExperimentArmResult:
    """构造单臂聚合结果，样本数、成功率、成本和延迟只取自权威事实。"""
    return ExperimentArmResult(
        variant=row["variant"],
        model_id=row["model_id"],
        provider_id=row["provider_id"],
        provider_code=row["provider_code"],
        assigned_count=row["assigned_count"],
        gateway_success_count=row["gateway_success_count"],
        gateway_failure_count=row["gateway_failure_count"],
        unresolved_count=row["unresolved_count"],
        gateway_success_rate_bps=row["gateway_success_rate_bps"],
        exposed_count=row["exposed_count"],
        not_exposed_count=row["not_exposed_count"],
        exposure_unknown_count=row["exposure_unknown_count"],
        quality_success_count=row["quality_success_count"],
        quality_failure_count=row["quality_failure_count"],
        quality_success_rate_bps=row["quality_success_rate_bps"],
        latency_sample_count=row["latency_sample_count"],
        total_latency_ms=row["total_latency_ms"],
        average_latency_micros=row["average_latency_micros"],
        p95_latency_ms=row["p95_latency_ms"],
        settlement_recorded_count=row["settlement_recorded_count"],
        settlement_not_exposed_count=row["settlement_not_exposed_count"],
        settlement_unavailable_count=row["settlement_unavailable_count"],
        settlement_pending_count=0,
        gross_cost_cny_micros=row["gross_normalized_cny_micros"],
        cost_known_count=row["cost_known_count"],
        cost_unknown_count=row["cost_unknown_count"],
        average_cost_cny_micros=row["average_cost_cny_micros"],
        feedback_collection_enabled=bool(row["feedback_enabled"]),
        feedback_count=row["feedback_count"],
        rating_sum=row["rating_sum"],
        average_rating_milli=row["average_rating_milli"],
        accepted_count=row["accepted_count"],
        adoption_rate_bps=row["adoption_rate_bps"],
    )


def _arm_owner(experiment: sqlite3.Row, variant: str) -> tuple[str, str, str]:
    """校验统计臂属于目标实验和租户，拒绝重复、缺失或交叉归属。"""
    if variant not in {"a", "b"}:
        # 统计所有者、计数或冻结结果异常时返回不可用，不拼接不同租户或阶段的数据。
        raise ValueError("experiment.statistics_invalid")
    return (
        str(experiment[f"model_{variant}_id"]),
        str(experiment[f"model_{variant}_provider_id"]),
        str(experiment[f"model_{variant}_provider_code"]),
    )


def _db_bool(value: object) -> bool:
    """规范化统计布尔、计数和时间字段，异常数据库值按不可用处理。"""
    if type(value) is not int or value not in {0, 1}:
        # 统计所有者、计数或冻结结果异常时返回不可用，不拼接不同租户或阶段的数据。
        raise ValueError("experiment.statistics_invalid")
    return bool(value)


def _optional_db_bool(value: object) -> bool | None:
    """规范化统计布尔、计数和时间字段，异常数据库值按不可用处理。"""
    return None if value is None else _db_bool(value)


def _nonnegative_int(value: object) -> int:
    """规范化统计布尔、计数和时间字段，异常数据库值按不可用处理。"""
    if type(value) is not int or value < 0:
        # 统计所有者、计数或冻结结果异常时返回不可用，不拼接不同租户或阶段的数据。
        raise ValueError("experiment.statistics_invalid")
    return value


def _time(value: object) -> datetime:
    """规范化统计布尔、计数和时间字段，异常数据库值按不可用处理。"""
    if type(value) is not str:
        # 统计所有者、计数或冻结结果异常时返回不可用，不拼接不同租户或阶段的数据。
        raise ValueError("experiment.statistics_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # 统计所有者、计数或冻结结果异常时返回不可用，不拼接不同租户或阶段的数据。
    except ValueError:
        # 统计所有者、计数或冻结结果异常时返回不可用，不拼接不同租户或阶段的数据。
        raise ValueError("experiment.statistics_invalid") from None
    if parsed.tzinfo is None:
        # 统计所有者、计数或冻结结果异常时返回不可用，不拼接不同租户或阶段的数据。
        raise ValueError("experiment.statistics_invalid")
    return parsed


__all__ = ["ExperimentStatisticsSnapshot", "load_statistics_snapshot"]
