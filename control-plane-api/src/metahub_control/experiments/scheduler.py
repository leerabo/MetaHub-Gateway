"""实验调度器：按数据库时钟和租约推进采样、反馈围栏、统计终态与完成事件。

Fair Control-plane scheduler and three-phase PRD-075 finalizer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import FastAPI
from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import build_webhook_event

from .models import ExperimentArmResult
from .outbox import drain_experiment_event_once
from .reaper import close_expired_settlements, reap_expired_executions
from .statistics import ExperimentStatisticsSnapshot, load_statistics_snapshot

logger = logging.getLogger("metahub.control.experiments.scheduler")
# 该常量组固定调度与 Outbox 工作者身份格式，租约所有权必须精确匹配。
SCHEDULER_WORKER_ID = f"experiment-scheduler-{uuid4().hex}"
OUTBOX_WORKER_ID = f"experiment-outbox-{uuid4().hex}"
_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


@dataclass(frozen=True, slots=True)
class SchedulerTenantClaim:
    """领取或验证实验调度租约，所有者与数据库时钟不匹配时拒绝推进。"""
    tenant_id: str
    lease_generation: int
    lease_owner_id: str
    lease_token: str = field(repr=False)
    lease_expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class _FinalizationClaim:
    """领取或验证实验调度租约，所有者与数据库时钟不匹配时拒绝推进。"""
    tenant_id: str
    experiment_id: str
    facts_version: int
    generation: int
    owner_id: str
    token: str = field(repr=False)


def claim_next_scheduler_tenant(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> SchedulerTenantClaim | None:
    """领取或验证实验调度租约，所有者与数据库时钟不匹配时拒绝推进。"""
    owner = _worker_id(worker_id)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        now_text = _db_time(current)
        connection.execute(
            """
            INSERT INTO experiment_scheduler_tenants(
              tenant_id,next_run_at,lease_generation,lease_token,lease_owner_id,
              lease_expires_at,created_at,updated_at
            )
            SELECT tenant.tenant_id,?,0,NULL,NULL,NULL,?,? FROM tenants tenant
            WHERE NOT EXISTS(
              SELECT 1 FROM experiment_scheduler_tenants current
              WHERE current.tenant_id=tenant.tenant_id
            )
            """,
            (now_text, now_text, now_text),
        )
        row = connection.execute(
            """
            SELECT * FROM experiment_scheduler_tenants
            WHERE unixepoch(next_run_at)<=unixepoch(?)
              AND (lease_token IS NULL OR unixepoch(lease_expires_at)<=unixepoch(?))
            ORDER BY next_run_at,tenant_id LIMIT 1
            """,
            (now_text, now_text),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        token = uuid4().hex
        generation = int(row["lease_generation"]) + 1
        expires = current + timedelta(seconds=config.experiment_finalizer_lease_seconds)
        next_run = current + timedelta(seconds=config.experiment_scheduler_interval_seconds)
        connection.execute(
            """
            UPDATE experiment_scheduler_tenants
            SET next_run_at=?,lease_generation=?,lease_token=?,lease_owner_id=?,
                lease_expires_at=?,updated_at=?
            WHERE tenant_id=? AND lease_generation=?
              AND (lease_token IS NULL OR unixepoch(lease_expires_at)<=unixepoch(?))
            """,
            (
                _db_time(next_run),
                generation,
                token,
                owner,
                _db_time(expires),
                now_text,
                row["tenant_id"],
                row["lease_generation"],
                now_text,
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            connection.rollback()
            return None
        connection.commit()
        # 调度租约、数据库时钟或终态快照失配时中止，防止并发重复结算。
        return SchedulerTenantClaim(
            tenant_id=str(row["tenant_id"]),
            lease_generation=generation,
            lease_owner_id=owner,
            lease_token=token,
            lease_expires_at=expires,
        )
    # 调度租约、数据库时钟或终态快照失配时中止，防止并发重复结算。
    except (sqlite3.Error, TypeError, ValueError):
        connection.rollback()
        # 调度租约、数据库时钟或终态快照失配时中止，防止并发重复结算。
        raise ApiError("experiment.unavailable") from None
    finally:
        connection.close()


def process_scheduler_claim(
    config: AppConfig,
    claim: SchedulerTenantClaim,
    *,
    now: datetime | None = None,
) -> bool:
    """领取或验证实验调度租约，所有者与数据库时钟不匹配时拒绝推进。

    Process at most one experiment and finalize only a post-fence snapshot.
    """

    final_claim: _FinalizationClaim | None = None
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        _require_scheduler_claim(connection, claim, current=current)
        now_text = _db_time(current)
        experiment = connection.execute(
            """
            SELECT experiment.* FROM model_experiments experiment
            WHERE experiment.tenant_id=? AND experiment.status!='completed'
              AND (
                (experiment.status='active' AND (
                  experiment.assigned_count>=experiment.target_sample_count
                  OR unixepoch(experiment.sampling_deadline_at)<=unixepoch(?)
                ))
                OR EXISTS (
                  SELECT 1 FROM experiment_request_executions execution
                  WHERE execution.tenant_id=experiment.tenant_id
                    AND execution.experiment_id=experiment.experiment_id
                    AND execution.status!='terminal'
                    AND (unixepoch(execution.lease_expires_at)<=unixepoch(?)
                      OR unixepoch(execution.absolute_deadline_at)<=unixepoch(?))
                )
                OR (experiment.status='collecting_feedback' AND (
                  (experiment.feedback_fence_closed_at IS NULL
                    AND unixepoch(experiment.feedback_deadline_at)<=unixepoch(?))
                  OR (unixepoch(experiment.settlement_deadline_at)<=unixepoch(?)
                    AND EXISTS (
                      SELECT 1 FROM experiment_usage_settlements settlement
                      WHERE settlement.tenant_id=experiment.tenant_id
                        AND settlement.experiment_id=experiment.experiment_id
                        AND settlement.status='pending'
                    ))
                  OR (experiment.feedback_fence_closed_at IS NOT NULL
                    AND (SELECT COUNT(*) FROM experiment_request_executions execution
                      WHERE execution.tenant_id=experiment.tenant_id
                        AND execution.experiment_id=experiment.experiment_id
                        AND execution.status='terminal')=experiment.assigned_count
                    AND (SELECT COUNT(*) FROM experiment_request_outcomes outcome
                      WHERE outcome.tenant_id=experiment.tenant_id
                        AND outcome.experiment_id=experiment.experiment_id)
                      =experiment.assigned_count
                    AND (SELECT COUNT(*) FROM experiment_usage_settlements settlement
                      WHERE settlement.tenant_id=experiment.tenant_id
                        AND settlement.experiment_id=experiment.experiment_id
                        AND settlement.status!='pending')=experiment.assigned_count)
                ))
              )
            ORDER BY experiment.created_at,experiment.experiment_id LIMIT 1
            """,
            (
                claim.tenant_id,
                now_text,
                now_text,
                now_text,
                now_text,
                now_text,
            ),
        ).fetchone()
        if experiment is None:
            _release_scheduler_claim(connection, claim, current=current)
            connection.commit()
            return False
        _transition_sampling_if_due(connection, experiment, current=current)
        experiment = _experiment_row(
            connection,
            tenant_id=claim.tenant_id,
            experiment_id=str(experiment["experiment_id"]),
        )
        _close_feedback_fence_if_due(connection, experiment, current=current)
        reap_expired_executions(
            connection,
            tenant_id=claim.tenant_id,
            experiment_id=str(experiment["experiment_id"]),
            now=current,
        )
        close_expired_settlements(
            connection,
            tenant_id=claim.tenant_id,
            experiment_id=str(experiment["experiment_id"]),
            now=current,
        )
        experiment = _experiment_row(
            connection,
            tenant_id=claim.tenant_id,
            experiment_id=str(experiment["experiment_id"]),
        )
        if _ready_to_finalize(connection, experiment):
            final_claim = _claim_finalization(
                connection,
                claim,
                experiment,
                current=current,
                lease_seconds=config.experiment_finalizer_lease_seconds,
            )
        if final_claim is None:
            _release_scheduler_claim(connection, claim, current=current)
        connection.commit()
    # 调度租约、数据库时钟或终态快照失配时中止，防止并发重复结算。
    except ApiError:
        connection.rollback()
        raise
    # 调度租约、数据库时钟或终态快照失配时中止，防止并发重复结算。
    except (sqlite3.Error, ArithmeticError, TypeError, ValueError):
        connection.rollback()
        # 调度租约、数据库时钟或终态快照失配时中止，防止并发重复结算。
        raise ApiError("experiment.unavailable") from None
    finally:
        connection.close()
    if final_claim is None:
        return True

    snapshot = _read_finalization_snapshot(config, final_claim, current=current)
    commit_current = current if now is not None else None
    return _commit_finalization(
        config,
        claim,
        final_claim,
        snapshot,
        current=commit_current,
    )


def run_experiment_scheduler_once(
    config: AppConfig,
    *,
    worker_id: str = SCHEDULER_WORKER_ID,
    now: datetime | None = None,
) -> bool:
    """运行可停止的实验调度循环，并与回收、Outbox 恢复共享有界单次工作量。"""
    claim = claim_next_scheduler_tenant(config, worker_id=worker_id, now=now)
    if claim is None:
        return False
    return process_scheduler_claim(config, claim, now=now)


def install_experiment_scheduler(app: FastAPI) -> None:
    """运行可停止的实验调度循环，并与回收、Outbox 恢复共享有界单次工作量。

    Compose the experiment scheduler and source outbox into the app lifespan.
    """

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        """运行可停止的实验调度循环，并与回收、Outbox 恢复共享有界单次工作量。"""
        async with original_lifespan(current_app):
            stop_event = threading.Event()
            task = asyncio.create_task(
                _scheduler_loop(current_app, stop_event),
                name="experiment-scheduler",
            )
            try:
                yield
            finally:
                stop_event.set()
                await asyncio.gather(task, return_exceptions=True)

    app.router.lifespan_context = combined_lifespan


async def _scheduler_loop(app: FastAPI, stop_event: threading.Event) -> None:
    """运行可停止的实验调度循环，并与回收、Outbox 恢复共享有界单次工作量。"""
    while not stop_event.is_set():
        worked = await asyncio.to_thread(
            _run_combined_once,
            app.state.config,
            stop_event.is_set,
        )
        delay = 0.05 if worked else float(app.state.config.experiment_scheduler_interval_seconds)
        try:
            await asyncio.wait_for(asyncio.to_thread(stop_event.wait, delay), timeout=delay + 0.25)
        # 调度租约、数据库时钟或终态快照失配时中止，防止并发重复结算。
        except TimeoutError:
            continue


def _run_combined_once(config: AppConfig, should_stop: Callable[[], bool]) -> bool:
    """运行可停止的实验调度循环，并与回收、Outbox 恢复共享有界单次工作量。"""
    if should_stop():
        return False
    scheduler_worked = False
    try:
        for _ in range(config.experiment_scheduler_batch_limit):
            if should_stop():
                break
            claim = claim_next_scheduler_tenant(
                config,
                worker_id=SCHEDULER_WORKER_ID,
            )
            if claim is None:
                break
            scheduler_worked = True
            process_scheduler_claim(config, claim)
    # 调度租约、数据库时钟或终态快照失配时中止，防止并发重复结算。
    except ApiError as exc:
        logger.warning(
            "experiment scheduler iteration failed",
            extra={"event": "experiment.scheduler_failed", "error_code": exc.code},
        )
    except Exception:
        logger.warning(
            "experiment scheduler iteration failed",
            extra={
                "event": "experiment.scheduler_failed",
                "error_code": "experiment.unavailable",
            },
        )
    if should_stop():
        return scheduler_worked
    outbox_worked = False
    try:
        for _ in range(config.experiment_scheduler_batch_limit):
            if should_stop() or not drain_experiment_event_once(
                config,
                worker_id=OUTBOX_WORKER_ID,
                should_stop=should_stop,
            ):
                break
            outbox_worked = True
    # 调度租约、数据库时钟或终态快照失配时中止，防止并发重复结算。
    except ApiError as exc:
        logger.warning(
            "experiment outbox iteration failed",
            extra={"event": "experiment.outbox_failed", "error_code": exc.code},
        )
        outbox_worked = False
    except Exception:
        logger.warning(
            "experiment outbox iteration failed",
            extra={
                "event": "experiment.outbox_failed",
                "error_code": "experiment.unavailable",
            },
        )
        outbox_worked = False
    return scheduler_worked or outbox_worked


def _transition_sampling_if_due(
    connection: sqlite3.Connection,
    experiment: sqlite3.Row,
    *,
    current: datetime,
) -> None:
    """按计划时间推进采样与反馈围栏，只在权威计数满足条件后允许结算。"""
    if experiment["status"] != "active":
        return
    deadline = _time(experiment["sampling_deadline_at"])
    target_reached = int(experiment["assigned_count"]) >= int(experiment["target_sample_count"])
    if not target_reached and current < deadline:
        return
    ended = current if target_reached else deadline
    feedback_deadline = ended + timedelta(seconds=int(experiment["feedback_window_seconds"]))
    settlement_deadline = max(
        feedback_deadline,
        ended
        + timedelta(
            seconds=int(experiment["execution_max_seconds"])
            + int(experiment["settlement_grace_seconds"])
        ),
    )
    connection.execute(
        """
        UPDATE model_experiments
        SET status='collecting_feedback',sampling_ended_at=?,feedback_deadline_at=?,
            settlement_deadline_at=?,updated_at=?
        WHERE tenant_id=? AND experiment_id=? AND status='active'
          AND (assigned_count>=target_sample_count
            OR unixepoch(sampling_deadline_at)<=unixepoch(?))
        """,
        (
            _db_time(ended),
            _db_time(feedback_deadline),
            _db_time(settlement_deadline),
            _db_time(current),
            experiment["tenant_id"],
            experiment["experiment_id"],
            _db_time(current),
        ),
    )


def _close_feedback_fence_if_due(
    connection: sqlite3.Connection,
    experiment: sqlite3.Row,
    *,
    current: datetime,
) -> None:
    """按计划时间推进采样与反馈围栏，只在权威计数满足条件后允许结算。"""
    if (
        experiment["status"] != "collecting_feedback"
        or experiment["feedback_fence_closed_at"] is not None
        or current < _time(experiment["feedback_deadline_at"])
    ):
        return
    now_text = _db_time(current)
    connection.execute(
        """
        UPDATE model_experiments
        SET feedback_fence_closed_at=?,updated_at=?
        WHERE tenant_id=? AND experiment_id=? AND status='collecting_feedback'
          AND feedback_fence_closed_at IS NULL
          AND unixepoch(feedback_deadline_at)<=unixepoch(?)
        """,
        (
            now_text,
            now_text,
            experiment["tenant_id"],
            experiment["experiment_id"],
            now_text,
        ),
    )


def _ready_to_finalize(connection: sqlite3.Connection, experiment: sqlite3.Row) -> bool:
    """从冻结快照计算并提交实验终态、双臂结果和完成事件，保持单次生效。"""
    if (
        experiment["status"] != "collecting_feedback"
        or experiment["feedback_fence_closed_at"] is None
    ):
        return False
    row = connection.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM experiment_request_executions execution
            WHERE execution.tenant_id=? AND execution.experiment_id=?
              AND execution.status='terminal') AS terminal_count,
          (SELECT COUNT(*) FROM experiment_request_outcomes outcome
            WHERE outcome.tenant_id=? AND outcome.experiment_id=?) AS outcome_count,
          (SELECT COUNT(*) FROM experiment_usage_settlements settlement
            WHERE settlement.tenant_id=? AND settlement.experiment_id=?
              AND settlement.status!='pending') AS settled_count
        """,
        (
            experiment["tenant_id"],
            experiment["experiment_id"],
            experiment["tenant_id"],
            experiment["experiment_id"],
            experiment["tenant_id"],
            experiment["experiment_id"],
        ),
    ).fetchone()
    assigned = int(experiment["assigned_count"])
    return row is not None and all(
        int(row[key]) == assigned
        for key in ("terminal_count", "outcome_count", "settled_count")
    )


def _claim_finalization(
    connection: sqlite3.Connection,
    scheduler_claim: SchedulerTenantClaim,
    experiment: sqlite3.Row,
    *,
    current: datetime,
    lease_seconds: int,
) -> _FinalizationClaim | None:
    """领取或验证实验调度租约，所有者与数据库时钟不匹配时拒绝推进。"""
    token = uuid4().hex
    generation = int(experiment["finalization_generation"]) + 1
    now_text = _db_time(current)
    connection.execute(
        """
        UPDATE model_experiments
        SET finalization_generation=?,finalization_owner_id=?,finalization_lease_token=?,
            finalization_lease_expires_at=?,updated_at=?
        WHERE tenant_id=? AND experiment_id=? AND status='collecting_feedback'
          AND facts_version=? AND feedback_fence_closed_at IS NOT NULL
          AND (finalization_lease_token IS NULL
            OR unixepoch(finalization_lease_expires_at)<=unixepoch(?))
        """,
        (
            generation,
            scheduler_claim.lease_owner_id,
            token,
            _db_time(current + timedelta(seconds=lease_seconds)),
            now_text,
            experiment["tenant_id"],
            experiment["experiment_id"],
            experiment["facts_version"],
            now_text,
        ),
    )
    if connection.execute("SELECT changes()").fetchone()[0] != 1:
        return None
    return _FinalizationClaim(
        tenant_id=str(experiment["tenant_id"]),
        experiment_id=str(experiment["experiment_id"]),
        facts_version=int(experiment["facts_version"]),
        generation=generation,
        owner_id=scheduler_claim.lease_owner_id,
        token=token,
    )


def _read_finalization_snapshot(
    config: AppConfig,
    claim: _FinalizationClaim,
    *,
    current: datetime,
) -> ExperimentStatisticsSnapshot:
    """从冻结快照计算并提交实验终态、双臂结果和完成事件，保持单次生效。"""
    connection = _connect(config)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        experiment = _experiment_row(
            connection,
            tenant_id=claim.tenant_id,
            experiment_id=claim.experiment_id,
        )
        snapshot = load_statistics_snapshot(
            connection,
            experiment,
            database_now=current,
        )
        if (
            snapshot.facts_version != claim.facts_version
            or snapshot.pending_execution_count != 0
            or snapshot.pending_settlement_count != 0
        ):
            # 调度租约、数据库时钟或终态快照失配时中止，防止并发重复结算。
            raise ApiError("experiment.unavailable")
        connection.commit()
        return snapshot
    # 调度租约、数据库时钟或终态快照失配时中止，防止并发重复结算。
    except ApiError:
        connection.rollback()
        raise
    # 调度租约、数据库时钟或终态快照失配时中止，防止并发重复结算。
    except (sqlite3.Error, ArithmeticError, TypeError, ValueError):
        connection.rollback()
        # 调度租约、数据库时钟或终态快照失配时中止，防止并发重复结算。
        raise ApiError("experiment.unavailable") from None
    finally:
        connection.close()


def _commit_finalization(
    config: AppConfig,
    scheduler_claim: SchedulerTenantClaim,
    final_claim: _FinalizationClaim,
    snapshot: ExperimentStatisticsSnapshot,
    *,
    current: datetime | None,
) -> bool:
    """从冻结快照计算并提交实验终态、双臂结果和完成事件，保持单次生效。"""
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        commit_current = _transaction_now(connection, current)
        now_text = _db_time(commit_current)
        _require_scheduler_claim(
            connection,
            scheduler_claim,
            current=commit_current,
        )
        experiment = _experiment_row(
            connection,
            tenant_id=final_claim.tenant_id,
            experiment_id=final_claim.experiment_id,
        )
        if (
            experiment["status"] != "collecting_feedback"
            or int(experiment["facts_version"]) != final_claim.facts_version
            or int(experiment["finalization_generation"]) != final_claim.generation
            or experiment["finalization_owner_id"] != final_claim.owner_id
            or experiment["finalization_lease_token"] != final_claim.token
            or _time(experiment["finalization_lease_expires_at"]) <= commit_current
            or not _ready_to_finalize(connection, experiment)
        ):
            connection.rollback()
            return False
        for arm in snapshot.arms:
            _insert_result(
                connection,
                experiment,
                arm,
                facts_version=final_claim.facts_version,
                facts_through_at=_db_time(snapshot.facts_through_at),
                created_at=now_text,
            )
        completion_reason = (
            "target_reached"
            if int(experiment["assigned_count"]) == int(experiment["target_sample_count"])
            else "sampling_deadline_reached"
        )
        payload = _completion_payload(
            experiment,
            snapshot,
            completion_reason=completion_reason,
        )
        event_id = f"exev_{uuid4().hex}"
        try:
            build_webhook_event(
                tenant_id=final_claim.tenant_id,
                event_type="experiment.completed",
                event_id=event_id,
                timestamp=commit_current,
                actor_id=str(experiment["created_by"]),
                object_id=final_claim.experiment_id,
                trace_id=str(experiment["operation_trace_id"]),
                source_request_id=str(experiment["operation_request_id"]),
                payload=payload,
            )
        # 调度租约、数据库时钟或终态快照失配时中止，防止并发重复结算。
        except ValueError:
            # 调度租约、数据库时钟或终态快照失配时中止，防止并发重复结算。
            raise ApiError("experiment.unavailable") from None
        connection.execute(
            """
            INSERT INTO experiment_events(
              experiment_event_id,tenant_id,experiment_id,event_type,actor_type,actor_id,
              object_type,object_id,source_request_id,source_trace_id,payload_json,occurred_at
            ) VALUES(?,?,?,'experiment.completed','user',?,'experiment',?,?,?,?,?)
            """,
            (
                event_id,
                final_claim.tenant_id,
                final_claim.experiment_id,
                experiment["created_by"],
                final_claim.experiment_id,
                experiment["operation_request_id"],
                experiment["operation_trace_id"],
                json.dumps(
                    payload,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                now_text,
            ),
        )
        connection.execute(
            """
            UPDATE model_experiments
            SET status='completed',completion_reason=?,completed_at=?,updated_at=?,
                finalization_owner_id=NULL,finalization_lease_token=NULL,
                finalization_lease_expires_at=NULL
            WHERE tenant_id=? AND experiment_id=? AND status='collecting_feedback'
              AND facts_version=? AND finalization_generation=?
              AND finalization_owner_id=? AND finalization_lease_token=?
              AND unixepoch(finalization_lease_expires_at)>unixepoch(?)
            """,
            (
                completion_reason,
                now_text,
                now_text,
                final_claim.tenant_id,
                final_claim.experiment_id,
                final_claim.facts_version,
                final_claim.generation,
                final_claim.owner_id,
                final_claim.token,
                now_text,
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            # 调度租约、数据库时钟或终态快照失配时中止，防止并发重复结算。
            raise ApiError("experiment.unavailable")
        _release_scheduler_claim(
            connection,
            scheduler_claim,
            current=commit_current,
        )
        connection.commit()
        return True
    # 调度租约、数据库时钟或终态快照失配时中止，防止并发重复结算。
    except ApiError:
        connection.rollback()
        raise
    # 调度租约、数据库时钟或终态快照失配时中止，防止并发重复结算。
    except (sqlite3.Error, ArithmeticError, TypeError, ValueError):
        connection.rollback()
        # 调度租约、数据库时钟或终态快照失配时中止，防止并发重复结算。
        raise ApiError("experiment.unavailable") from None
    finally:
        connection.close()


def _insert_result(
    connection: sqlite3.Connection,
    experiment: sqlite3.Row,
    arm: ExperimentArmResult,
    *,
    facts_version: int,
    facts_through_at: str,
    created_at: str,
) -> None:
    """从冻结快照计算并提交实验终态、双臂结果和完成事件，保持单次生效。"""
    route_fingerprint = experiment[f"model_{arm.variant}_route_config_fingerprint"]
    connection.execute(
        """
        INSERT INTO experiment_results(
          experiment_result_id,tenant_id,experiment_id,variant,model_id,provider_id,
          provider_code,route_config_fingerprint,assigned_count,gateway_success_count,
          gateway_failure_count,unresolved_count,exposed_count,not_exposed_count,
          exposure_unknown_count,quality_success_count,quality_failure_count,
          gateway_success_rate_bps,quality_success_rate_bps,latency_sample_count,
          total_latency_ms,average_latency_micros,p95_latency_ms,
          settlement_recorded_count,settlement_not_exposed_count,
          settlement_unavailable_count,gross_normalized_cny_micros,cost_known_count,
          cost_unknown_count,average_cost_cny_micros,feedback_enabled,feedback_count,
          rating_sum,average_rating_milli,accepted_count,adoption_rate_bps,
          statistics_version,facts_version,facts_through_at,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)
        """,
        (
            f"exrs_{uuid4().hex}",
            experiment["tenant_id"],
            experiment["experiment_id"],
            arm.variant,
            arm.model_id,
            arm.provider_id,
            arm.provider_code,
            route_fingerprint,
            arm.assigned_count,
            arm.gateway_success_count,
            arm.gateway_failure_count,
            arm.unresolved_count,
            arm.exposed_count,
            arm.not_exposed_count,
            arm.exposure_unknown_count,
            arm.quality_success_count,
            arm.quality_failure_count,
            arm.gateway_success_rate_bps,
            arm.quality_success_rate_bps,
            arm.latency_sample_count,
            arm.total_latency_ms,
            arm.average_latency_micros,
            arm.p95_latency_ms,
            arm.settlement_recorded_count,
            arm.settlement_not_exposed_count,
            arm.settlement_unavailable_count,
            arm.gross_cost_cny_micros,
            arm.cost_known_count,
            arm.cost_unknown_count,
            arm.average_cost_cny_micros,
            int(arm.feedback_collection_enabled),
            arm.feedback_count,
            arm.rating_sum,
            arm.average_rating_milli,
            arm.accepted_count,
            arm.adoption_rate_bps,
            facts_version,
            facts_through_at,
            created_at,
        ),
    )


def _completion_payload(
    experiment: sqlite3.Row,
    snapshot: ExperimentStatisticsSnapshot,
    *,
    completion_reason: str,
) -> dict[str, object]:
    """从冻结快照计算并提交实验终态、双臂结果和完成事件，保持单次生效。"""
    return {
        "schema_version": 1,
        "experiment_id": experiment["experiment_id"],
        "project_id": experiment["project_id"],
        "environment_id": experiment["environment_id"],
        "model_alias_id": experiment["model_alias_id"],
        "model_a_id": experiment["model_a_id"],
        "model_b_id": experiment["model_b_id"],
        "status": "completed",
        "completion_reason": completion_reason,
        "assigned_count": experiment["assigned_count"],
        "model_a_assigned_count": experiment["model_a_assigned_count"],
        "model_b_assigned_count": experiment["model_b_assigned_count"],
        "gateway_success_count": sum(arm.gateway_success_count for arm in snapshot.arms),
        "gateway_failure_count": sum(arm.gateway_failure_count for arm in snapshot.arms),
        "unresolved_count": sum(arm.unresolved_count for arm in snapshot.arms),
        "cost_unknown_count": sum(arm.cost_unknown_count for arm in snapshot.arms),
        "feedback_count": sum(arm.feedback_count for arm in snapshot.arms),
        "facts_version": snapshot.facts_version,
        "facts_through_at": _db_time(snapshot.facts_through_at),
    }


def _require_scheduler_claim(
    connection: sqlite3.Connection,
    claim: SchedulerTenantClaim,
    *,
    current: datetime,
) -> None:
    """领取或验证实验调度租约，所有者与数据库时钟不匹配时拒绝推进。"""
    row = connection.execute(
        """
        SELECT 1 FROM experiment_scheduler_tenants
        WHERE tenant_id=? AND lease_generation=? AND lease_owner_id=? AND lease_token=?
          AND unixepoch(lease_expires_at)>unixepoch(?)
        """,
        (
            claim.tenant_id,
            claim.lease_generation,
            claim.lease_owner_id,
            claim.lease_token,
            _db_time(current),
        ),
    ).fetchone()
    if row is None:
        # 调度租约、数据库时钟或终态快照失配时中止，防止并发重复结算。
        raise ApiError("experiment.unavailable")


def _release_scheduler_claim(
    connection: sqlite3.Connection,
    claim: SchedulerTenantClaim,
    *,
    current: datetime,
) -> None:
    """领取或验证实验调度租约，所有者与数据库时钟不匹配时拒绝推进。"""
    connection.execute(
        """
        UPDATE experiment_scheduler_tenants
        SET lease_token=NULL,lease_owner_id=NULL,lease_expires_at=NULL,updated_at=?
        WHERE tenant_id=? AND lease_generation=? AND lease_owner_id=? AND lease_token=?
        """,
        (
            _db_time(current),
            claim.tenant_id,
            claim.lease_generation,
            claim.lease_owner_id,
            claim.lease_token,
        ),
    )
    if connection.execute("SELECT changes()").fetchone()[0] != 1:
        # 调度租约、数据库时钟或终态快照失配时中止，防止并发重复结算。
        raise ApiError("experiment.unavailable")


def _experiment_row(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    experiment_id: str,
) -> sqlite3.Row:
    """读取实验调度的数据库时间或租户行，调用方必须持有匹配租约。"""
    row = connection.execute(
        "SELECT * FROM model_experiments WHERE tenant_id=? AND experiment_id=?",
        (tenant_id, experiment_id),
    ).fetchone()
    if row is None:
        # 调度租约、数据库时钟或终态快照失配时中止，防止并发重复结算。
        raise ApiError("experiment.unavailable")
    return row


def _worker_id(value: object) -> str:
    """封装实验调度状态，租户、游标、租约所有者和到期时间必须同时匹配。"""
    if type(value) is not str or _WORKER_ID.fullmatch(value) is None:
        # 调度租约、数据库时钟或终态快照失配时中止，防止并发重复结算。
        raise ApiError("experiment.unavailable")
    return value


def _connect(config: AppConfig) -> sqlite3.Connection:
    """读取实验调度的数据库时间或租户行，调用方必须持有匹配租约。"""
    try:
        return connect_database(config.database_url)
    # 调度租约、数据库时钟或终态快照失配时中止，防止并发重复结算。
    except DbError:
        # 调度租约、数据库时钟或终态快照失配时中止，防止并发重复结算。
        raise ApiError("experiment.unavailable") from None


def _time(value: object) -> datetime:
    """读取实验调度的数据库时间或租户行，调用方必须持有匹配租约。"""
    if type(value) is not str:
        # 调度租约、数据库时钟或终态快照失配时中止，防止并发重复结算。
        raise ValueError
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        # 调度租约、数据库时钟或终态快照失配时中止，防止并发重复结算。
        raise ValueError
    return parsed.astimezone(UTC)


def _db_time(value: datetime) -> str:
    """读取实验调度的数据库时间或租户行，调用方必须持有匹配租约。"""
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _now(value: datetime | None) -> datetime:
    """封装实验调度状态，租户、游标、租约所有者和到期时间必须同时匹配。"""
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).replace(microsecond=0)


def _transaction_now(
    connection: sqlite3.Connection,
    value: datetime | None,
) -> datetime:
    """封装实验调度状态，租户、游标、租约所有者和到期时间必须同时匹配。"""
    if value is not None:
        return _now(value)
    row = connection.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')"
    ).fetchone()
    if row is None:
        # 调度租约、数据库时钟或终态快照失配时中止，防止并发重复结算。
        raise ValueError
    return _time(row[0])


__all__ = [
    "SchedulerTenantClaim",
    "claim_next_scheduler_tenant",
    "install_experiment_scheduler",
    "process_scheduler_claim",
    "run_experiment_scheduler_once",
]
