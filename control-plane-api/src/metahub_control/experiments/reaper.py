"""实验执行回收器：关闭超时执行与结算，不臆造上游响应、质量或成本事实。

Conservative execution and settlement recovery for PRD-075.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

# 该常量组固定可公开错误集合和回收器所有者，禁止透传上游原始异常。
_SAFE_ERROR = re.compile(r"^[a-z0-9][a-z0-9._]{2,127}$")
_REAPER_OWNER_ID = f"experiment-reaper-{uuid4().hex}"


@dataclass(frozen=True, slots=True)
class _RecoveredOutcome:
    """规范化回收器恢复结果、延迟或数据库时间，禁止生成新的业务事实。"""
    gateway_status: str
    exposure_status: str
    outcome_reason: str
    error_code: str | None
    routing_attempt_id: str | None
    stream_session_id: str | None
    quality_eligible: bool
    quality_succeeded: bool | None
    latency_ms: int | None


def reap_expired_executions(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    experiment_id: str,
    now: datetime,
) -> int:
    """关闭已过期的实验执行或结算，只恢复持久事实并保持单次终态。

    Close expired generations without inventing Provider or client-visible facts.
    """

    now_text = _db_time(now)
    rows = connection.execute(
        """
        SELECT execution.*,assignment.variant,assignment.model_id,assignment.provider_id,
               settlement.status AS settlement_status
        FROM experiment_request_executions execution
        JOIN experiment_assignments assignment
          ON assignment.tenant_id=execution.tenant_id
         AND assignment.assignment_id=execution.assignment_id
        JOIN experiment_usage_settlements settlement
          ON settlement.tenant_id=assignment.tenant_id
         AND settlement.assignment_id=assignment.assignment_id
        WHERE execution.tenant_id=? AND execution.experiment_id=?
          AND execution.status!='terminal'
          AND (unixepoch(execution.lease_expires_at)<=unixepoch(?)
            OR unixepoch(execution.absolute_deadline_at)<=unixepoch(?))
        ORDER BY execution.created_at,execution.execution_id
        """,
        (tenant_id, experiment_id, now_text, now_text),
    ).fetchall()
    recovered = 0
    for row in rows:
        generation = int(row["generation"]) + 1
        token = uuid4().hex
        connection.execute(
            """
            UPDATE experiment_request_executions
            SET generation=?,lease_owner_id=?,lease_token=?,updated_at=?
            WHERE tenant_id=? AND execution_id=? AND generation=? AND status=?
              AND lease_owner_id=? AND lease_token=?
              AND (unixepoch(lease_expires_at)<=unixepoch(?)
                OR unixepoch(absolute_deadline_at)<=unixepoch(?))
            """,
            (
                generation,
                _REAPER_OWNER_ID,
                token,
                now_text,
                tenant_id,
                row["execution_id"],
                row["generation"],
                row["status"],
                row["lease_owner_id"],
                row["lease_token"],
                now_text,
                now_text,
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            # 持久执行事实不完整或已被接管时停止回收，不臆造上游响应和成本。
            raise sqlite3.IntegrityError("experiment_execution_recovery_conflict")
        # 过期且仍非终态说明 Gateway 没有完成 Outcome CAS；残留 success 只能是旧写入，
        # 不能覆盖客户端已经收到的实验失败。先在同一事务内降级，再独立恢复 Provider 事实。
        _fail_residual_success_request_log(connection, row, now_text=now_text)
        outcome = _recover_global_terminal(connection, row)
        if outcome is None:
            if row["status"] == "assigned":
                outcome = _RecoveredOutcome(
                    gateway_status="unresolved",
                    exposure_status="not_exposed",
                    outcome_reason="abandoned_before_provider",
                    error_code="gateway.experiment_unavailable",
                    routing_attempt_id=None,
                    stream_session_id=None,
                    quality_eligible=False,
                    quality_succeeded=None,
                    latency_ms=None,
                )
            else:
                outcome = _RecoveredOutcome(
                    gateway_status="unresolved",
                    exposure_status="unknown",
                    outcome_reason="provider_outcome_unknown",
                    error_code="gateway.experiment_unavailable",
                    routing_attempt_id=None,
                    stream_session_id=None,
                    quality_eligible=False,
                    quality_succeeded=None,
                    latency_ms=None,
                )
        connection.execute(
            """
            INSERT INTO experiment_request_outcomes(
              outcome_id,tenant_id,experiment_id,assignment_id,execution_id,request_id,
              variant,model_id,provider_id,gateway_status,exposure_status,outcome_reason,
              error_code,routing_attempt_id,stream_session_id,quality_eligible,
              quality_succeeded,latency_ms,recovery_generation,facts_at,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                f"exoc_{uuid4().hex}",
                row["tenant_id"],
                row["experiment_id"],
                row["assignment_id"],
                row["execution_id"],
                row["request_id"],
                row["variant"],
                row["model_id"],
                row["provider_id"],
                outcome.gateway_status,
                outcome.exposure_status,
                outcome.outcome_reason,
                outcome.error_code,
                outcome.routing_attempt_id,
                outcome.stream_session_id,
                int(outcome.quality_eligible),
                (
                    None
                    if outcome.quality_succeeded is None
                    else int(outcome.quality_succeeded)
                ),
                outcome.latency_ms,
                generation,
                now_text,
                now_text,
            ),
        )
        if outcome.exposure_status == "not_exposed" and row["settlement_status"] == "pending":
            connection.execute(
                """
                UPDATE experiment_usage_settlements
                SET status='not_exposed',normalization_status='not_applicable',
                    settlement_reason='not_exposed',settled_at=?,updated_at=?
                WHERE tenant_id=? AND assignment_id=? AND status='pending'
                """,
                (now_text, now_text, tenant_id, row["assignment_id"]),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                # 持久执行事实不完整或已被接管时停止回收，不臆造上游响应和成本。
                raise sqlite3.IntegrityError("experiment_settlement_recovery_conflict")
        connection.execute(
            """
            UPDATE experiment_request_executions
            SET status='terminal',updated_at=?
            WHERE tenant_id=? AND execution_id=? AND generation=? AND status=?
              AND lease_owner_id=? AND lease_token=?
              AND (unixepoch(lease_expires_at)<=unixepoch(?)
                OR unixepoch(absolute_deadline_at)<=unixepoch(?))
            """,
            (
                now_text,
                tenant_id,
                row["execution_id"],
                generation,
                row["status"],
                _REAPER_OWNER_ID,
                token,
                now_text,
                now_text,
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            # 持久执行事实不完整或已被接管时停止回收，不臆造上游响应和成本。
            raise sqlite3.IntegrityError("experiment_execution_recovery_conflict")
        recovered += 1
    return recovered


def close_expired_settlements(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    experiment_id: str,
    now: datetime,
) -> int:
    """关闭已过期的实验执行或结算，只恢复持久事实并保持单次终态。

    Freeze pending cost as unavailable only after the experiment hard deadline.
    """

    now_text = _db_time(now)
    deadline = connection.execute(
        """
        SELECT settlement_deadline_at FROM model_experiments
        WHERE tenant_id=? AND experiment_id=? AND status='collecting_feedback'
          AND settlement_deadline_at IS NOT NULL
          AND unixepoch(settlement_deadline_at)<=unixepoch(?)
        """,
        (tenant_id, experiment_id, now_text),
    ).fetchone()
    if deadline is None:
        return 0
    connection.execute(
        """
        UPDATE experiment_usage_settlements
        SET status='unavailable',normalization_status='unavailable',
            settlement_reason='hard_deadline',settled_at=?,updated_at=?
        WHERE tenant_id=? AND experiment_id=? AND status='pending'
        """,
        (now_text, now_text, tenant_id, experiment_id),
    )
    return int(connection.execute("SELECT changes()").fetchone()[0])


def _recover_global_terminal(
    connection: sqlite3.Connection,
    execution: sqlite3.Row,
) -> _RecoveredOutcome | None:
    """关闭已过期的实验执行或结算，只恢复持久事实并保持单次终态。

    Recover only when durable RequestLog and terminal attempt/stream owners agree.
    """

    request_log = connection.execute(
        """
        SELECT status,error_code,latency_ms,model_id,provider_id
        FROM request_logs WHERE tenant_id=? AND request_id=?
        """,
        (execution["tenant_id"], execution["request_id"]),
    ).fetchone()
    if (
        request_log is None
        or request_log["model_id"] != execution["model_id"]
        or request_log["provider_id"] != execution["provider_id"]
        or request_log["status"] not in {"success", "failed"}
    ):
        return None
    attempt = connection.execute(
        """
        SELECT attempt.routing_attempt_id,attempt.status,attempt.latency_ms,
               attempt.upstream_http_status,attempt.usage_status,
               fact.transport_kind,fact.upstream_http_status AS fact_http_status
        FROM routing_attempts attempt
        LEFT JOIN provider_error_facts fact
          ON fact.tenant_id=attempt.tenant_id
         AND fact.routing_attempt_id=attempt.routing_attempt_id
        WHERE attempt.tenant_id=? AND attempt.request_id=?
          AND attempt.model_id=? AND attempt.provider_id=?
          AND attempt.attempt_sequence=1 AND attempt.attempt_role='primary'
          AND attempt.fallback_policy_id IS NULL
          AND attempt.status IN ('succeeded','failed')
          AND attempt.completed_at IS NOT NULL
        ORDER BY attempt.created_at DESC,attempt.routing_attempt_id DESC LIMIT 1
        """,
        (
            execution["tenant_id"],
            execution["request_id"],
            execution["model_id"],
            execution["provider_id"],
        ),
    ).fetchone()
    stream = connection.execute(
        """
        SELECT stream_session_id,status,error_code,upstream_http_status,bytes_sent
        FROM stream_sessions
        WHERE tenant_id=? AND request_id=? AND model_id=? AND provider_id=?
          AND status NOT IN ('starting','active') AND completed_at IS NOT NULL
        ORDER BY created_at DESC,stream_session_id DESC LIMIT 1
        """,
        (
            execution["tenant_id"],
            execution["request_id"],
            execution["model_id"],
            execution["provider_id"],
        ),
    ).fetchone()
    if (attempt is None) == (stream is None):
        return None
    if execution["status"] == "assigned":
        # 持久化发送闸门是权威事实；未跨过闸门的执行不能由 Provider 事实升级为已暴露结果。
        return None
    exposure = _recovered_exposure(attempt, stream)
    if request_log["status"] == "success":
        # 调用方已按完整 owner 降级 success；仍读到 success 表示事务内状态不一致，必须回滚。
        raise sqlite3.IntegrityError("experiment_request_log_recovery_conflict")
    if exposure == "not_exposed":
        return _RecoveredOutcome(
            gateway_status="failed",
            exposure_status="not_exposed",
            outcome_reason="recovered_terminal",
            error_code=_safe_error_code(request_log["error_code"]),
            routing_attempt_id=(
                attempt["routing_attempt_id"] if attempt is not None else None
            ),
            stream_session_id=(
                stream["stream_session_id"] if stream is not None else None
            ),
            quality_eligible=False,
            quality_succeeded=None,
            latency_ms=None,
        )
    if exposure != "exposed":
        return None
    error_code = _safe_error_code(request_log["error_code"])
    eligible, succeeded, latency = _recovered_quality(
        request_log,
        attempt,
        stream,
    )
    return _RecoveredOutcome(
        gateway_status="failed",
        exposure_status="exposed",
        outcome_reason="recovered_terminal",
        error_code=error_code,
        routing_attempt_id=(attempt["routing_attempt_id"] if stream is None else None),
        stream_session_id=(stream["stream_session_id"] if stream is not None else None),
        quality_eligible=eligible,
        quality_succeeded=succeeded,
        latency_ms=latency,
    )


def _fail_residual_success_request_log(
    connection: sqlite3.Connection,
    execution: sqlite3.Row,
    *,
    now_text: str,
) -> None:
    """将过期实验执行遗留的 Gateway success 收敛为固定失败，不改写 Provider 事实。"""

    connection.execute(
        """
        UPDATE request_logs
        SET status='failed',error_code='gateway.experiment_unavailable',updated_at=?
        WHERE tenant_id=? AND request_id=? AND model_id=? AND provider_id=?
          AND status='success'
        """,
        (
            now_text,
            execution["tenant_id"],
            execution["request_id"],
            execution["model_id"],
            execution["provider_id"],
        ),
    )
    residual = connection.execute(
        """
        SELECT 1 FROM request_logs
        WHERE tenant_id=? AND request_id=? AND status='success'
        LIMIT 1
        """,
        (execution["tenant_id"], execution["request_id"]),
    ).fetchone()
    if residual is not None:
        # 同 tenant/request 仍为 success 说明 owner 缺失或冲突；必须让外层事务回滚，
        # 不能同时留下客户端成功日志和 unresolved/failed 实验 Outcome。
        raise sqlite3.IntegrityError("experiment_request_log_recovery_conflict")


def _recovered_exposure(
    attempt: sqlite3.Row | None,
    stream: sqlite3.Row | None,
) -> str:
    """从持久请求事实恢复暴露、质量和安全错误码，不推断不存在的上游响应。"""
    if attempt is not None:
        if (
            attempt["status"] == "succeeded"
            or attempt["upstream_http_status"] is not None
            or attempt["fact_http_status"] is not None
        ):
            return "exposed"
        if attempt["transport_kind"] == "local_configuration":
            return "not_exposed"
        return "unknown"
    if stream is not None and (
        stream["status"] == "completed"
        or stream["upstream_http_status"] is not None
        or int(stream["bytes_sent"] or 0) > 0
    ):
        return "exposed"
    return "unknown"


def _recovered_quality(
    request_log: sqlite3.Row,
    attempt: sqlite3.Row | None,
    stream: sqlite3.Row | None,
) -> tuple[bool, bool | None, int | None]:
    """从持久请求事实恢复暴露、质量和安全错误码，不推断不存在的上游响应。"""
    latency = _latency(request_log["latency_ms"], attempt)
    if latency is None:
        return False, None, None
    if attempt is not None and attempt["status"] == "succeeded":
        return True, True, latency
    if (
        attempt is not None
        and attempt["status"] == "failed"
        and attempt["usage_status"] == "unavailable"
    ):
        return True, False, latency
    if stream is not None and stream["status"] == "completed":
        return True, True, latency
    return False, None, None


def _safe_error_code(value: object) -> str:
    """从持久请求事实恢复暴露、质量和安全错误码，不推断不存在的上游响应。"""
    # 持久执行事实不完整或已被接管时停止回收，不臆造上游响应和成本。
    if type(value) is str and _SAFE_ERROR.fullmatch(value) is not None:
        return value
    return "gateway.experiment_unavailable"


def _latency(value: object, attempt: sqlite3.Row | None) -> int | None:
    """规范化回收器恢复结果、延迟或数据库时间，禁止生成新的业务事实。"""
    candidate = value
    if type(candidate) is not int and attempt is not None:
        candidate = attempt["latency_ms"]
    return candidate if type(candidate) is int and candidate >= 0 else None


def _db_time(value: datetime) -> str:
    """规范化回收器恢复结果、延迟或数据库时间，禁止生成新的业务事实。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = ["close_expired_settlements", "reap_expired_executions"]
