"""Platform-only job catalog, management APIs and internal enqueue contract."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.db.connection import connect_database, database_request_scope
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.platform_access.service import (
    PlatformContext,
    authenticate_platform_session_in_transaction,
)

from .catalog import ARTIFACTS, handler_definition, validate_job_input
from .crypto import encrypt_job_value
from .models import (
    PlatformJobActionRequest,
    PlatformJobActionResponse,
    PlatformJobAttemptInfo,
    PlatformJobCheckpointInfo,
    PlatformJobDetailResponse,
    PlatformJobQueueResponse,
    PlatformJobSummary,
    PlatformJobTypeCommandRequest,
    PlatformJobTypeInfo,
    PlatformJobTypeResponse,
    PlatformWorkQueueInfo,
)

_JOB_ID_RE = re.compile(r"^pjob_[0-9a-f]{32}$")
_TYPE_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,79}$")
_SOURCE_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,79}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "dead_lettered"})


@dataclass(frozen=True, slots=True, repr=False)
class PreparedPlatformJob:
    job_id: str
    type_code: str
    input_envelope: str
    input_sha256: str


def prepare_platform_job(
    config: AppConfig,
    *,
    type_code: str,
    payload: object,
) -> PreparedPlatformJob:
    if _TYPE_CODE_RE.fullmatch(type_code) is None:
        raise ApiError("platform_job.request_invalid")
    try:
        validated = validate_job_input(type_code, payload)
    except ValueError:
        raise ApiError("platform_job.type_unavailable") from None
    job_id = _id("pjob_")
    prepared = encrypt_job_value(
        config,
        validated.model_dump(mode="json"),
        kind="input",
        owner_id=job_id,
        type_code=type_code,
    )
    return PreparedPlatformJob(
        job_id=job_id,
        type_code=type_code,
        input_envelope=prepared.envelope,
        input_sha256=prepared.plaintext_sha256,
    )


def enqueue_prepared_platform_job_in_transaction(
    connection,
    config: AppConfig,
    *,
    prepared: PreparedPlatformJob,
    priority: int,
    source_code: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
    scheduled_at: datetime | None = None,
) -> tuple[str, bool]:
    if (
        not 0 <= priority <= 100
        or _SOURCE_CODE_RE.fullmatch(source_code) is None
        or _IDEMPOTENCY_RE.fullmatch(idempotency_key) is None
        or not 1 <= len(request_id) <= 128
        or not 1 <= len(trace_id) <= 128
    ):
        raise ApiError("platform_job.request_invalid")
    definition = handler_definition(prepared.type_code)
    row = connection.execute(
        "SELECT * FROM platform_job_types WHERE type_code=?", (prepared.type_code,)
    ).fetchone()
    if (
        row is None
        or definition is None
        or row["status"] != "active"
        or row["artifact_sha256"] != definition.artifact_sha256
        or int(row["contract_version"]) != definition.contract_version
    ):
        raise ApiError("platform_job.type_unavailable")
    idempotency_sha256 = _hmac(config, "enqueue", idempotency_key)
    existing = connection.execute(
        "SELECT platform_job_id,input_sha256,source_code FROM platform_jobs "
        "WHERE type_code=? AND idempotency_sha256=?",
        (prepared.type_code, idempotency_sha256),
    ).fetchone()
    if existing is not None:
        if not (
            hmac.compare_digest(str(existing["input_sha256"]), prepared.input_sha256)
            and hmac.compare_digest(str(existing["source_code"]), source_code)
        ):
            raise ApiError("platform_job.idempotency_conflict")
        return str(existing["platform_job_id"]), True
    now = _utcnow()
    due = scheduled_at or now
    connection.execute(
        """
        INSERT INTO platform_jobs(
          platform_job_id,type_code,queue_code,priority,status,revision,generation,
          attempt_count,max_attempts,input_envelope,input_sha256,idempotency_sha256,
          source_code,request_id,trace_id,safe_outcome_code,scheduled_at,started_at,
          completed_at,created_at,updated_at)
        VALUES(?,?,?,?,'queued',1,0,0,?,?,?,?,?,?,?,NULL,?,NULL,NULL,?,?)
        """,
        (
            prepared.job_id,
            prepared.type_code,
            row["queue_code"],
            priority,
            int(row["max_attempts"]),
            prepared.input_envelope,
            prepared.input_sha256,
            idempotency_sha256,
            source_code,
            request_id,
            trace_id,
            _db_time(due),
            _db_time(now),
            _db_time(now),
        ),
    )
    return prepared.job_id, False


def enqueue_platform_job(
    config: AppConfig,
    *,
    type_code: str,
    payload: object,
    priority: int,
    source_code: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
    scheduled_at: datetime | None = None,
) -> tuple[str, bool]:
    with database_request_scope(reuse_connections=True):
        return _enqueue_platform_job_scoped(
            config,
            type_code=type_code,
            payload=payload,
            priority=priority,
            source_code=source_code,
            idempotency_key=idempotency_key,
            request_id=request_id,
            trace_id=trace_id,
            scheduled_at=scheduled_at,
        )


def _enqueue_platform_job_scoped(
    config: AppConfig,
    *,
    type_code: str,
    payload: object,
    priority: int,
    source_code: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
    scheduled_at: datetime | None,
) -> tuple[str, bool]:
    prepared = prepare_platform_job(config, type_code=type_code, payload=payload)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        result = enqueue_prepared_platform_job_in_transaction(
            connection,
            config,
            prepared=prepared,
            priority=priority,
            source_code=source_code,
            idempotency_key=idempotency_key,
            request_id=request_id,
            trace_id=trace_id,
            scheduled_at=scheduled_at,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("platform_job.unavailable") from None
    finally:
        connection.close()


def list_platform_job_queues(
    config: AppConfig,
    *,
    session_token: str,
    status: str | None,
    limit: int,
    request_id: str,
    trace_id: str,
) -> PlatformJobQueueResponse:
    allowed_statuses = {
        "queued",
        "leased",
        "running",
        "cancel_requested",
        "succeeded",
        "failed",
        "cancelled",
        "dead_lettered",
    }
    if status is not None and status not in allowed_statuses:
        raise ApiError("platform_job.request_invalid")
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = authenticate_platform_session_in_transaction(
            connection, config, session_token=session_token, touch=True
        )
        _require(actor, "platform.job.read")
        now = _utcnow()
        queues = []
        for row in connection.execute(
            "SELECT * FROM platform_work_queues ORDER BY priority_weight DESC,queue_code"
        ).fetchall():
            counts = connection.execute(
                """
                SELECT
                  SUM(CASE WHEN status='queued' THEN 1 ELSE 0 END) AS queued_count,
                  SUM(CASE WHEN status='dead_lettered' THEN 1 ELSE 0 END) AS dead_letter_count,
                  MIN(CASE WHEN status='queued' THEN created_at ELSE NULL END) AS oldest
                FROM platform_jobs WHERE queue_code=?
                """,
                (row["queue_code"],),
            ).fetchone()
            active = connection.execute(
                """SELECT COUNT(*) FROM platform_scheduler_leases lease
                   JOIN platform_jobs job ON job.platform_job_id=lease.platform_job_id
                   WHERE job.queue_code=? AND lease.status='active'
                     AND unixepoch(lease.expires_at)>unixepoch(?)""",
                (row["queue_code"], _db_time(now)),
            ).fetchone()[0]
            oldest = _optional_time(counts["oldest"])
            queues.append(
                PlatformWorkQueueInfo(
                    queue_code=row["queue_code"],
                    priority_weight=int(row["priority_weight"]),
                    max_concurrency=int(row["max_concurrency"]),
                    queued_count=int(counts["queued_count"] or 0),
                    active_lease_count=int(active),
                    dead_letter_count=int(counts["dead_letter_count"] or 0),
                    oldest_wait_seconds=(
                        max(0, int((now - oldest).total_seconds())) if oldest else None
                    ),
                )
            )
        predicate = "" if status is None else "WHERE status=?"
        parameters: tuple[object, ...] = () if status is None else (status,)
        jobs = connection.execute(
            f"SELECT * FROM platform_jobs {predicate} "
            "ORDER BY created_at DESC,platform_job_id DESC LIMIT ?",
            (*parameters, limit),
        ).fetchall()
        types = connection.execute(
            "SELECT * FROM platform_job_types ORDER BY type_code"
        ).fetchall()
        response = PlatformJobQueueResponse(
            queues=tuple(queues),
            recent_jobs=tuple(_job_info(row) for row in jobs),
            job_types=tuple(_type_info(row) for row in types),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("platform_job.unavailable") from None
    finally:
        connection.close()


def get_platform_job(
    config: AppConfig,
    *,
    session_token: str,
    job_id: str,
    request_id: str,
    trace_id: str,
) -> PlatformJobDetailResponse:
    _valid_job_id(job_id)
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = authenticate_platform_session_in_transaction(
            connection, config, session_token=session_token, touch=True
        )
        _require(actor, "platform.job.read")
        row = _job_row(connection, job_id)
        attempts = connection.execute(
            "SELECT * FROM platform_job_attempts WHERE platform_job_id=? "
            "ORDER BY attempt_number",
            (job_id,),
        ).fetchall()
        checkpoints = connection.execute(
            "SELECT * FROM platform_job_checkpoints WHERE platform_job_id=? "
            "ORDER BY generation,sequence_number",
            (job_id,),
        ).fetchall()
        response = PlatformJobDetailResponse(
            job=_job_info(row),
            attempts=tuple(_attempt_info(item) for item in attempts),
            checkpoints=tuple(_checkpoint_info(item) for item in checkpoints),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("platform_job.unavailable") from None
    finally:
        connection.close()


def update_platform_job(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    job_id: str,
    action: str,
    expected_revision: int,
    idempotency_key: str,
    payload: PlatformJobActionRequest,
    request_id: str,
    trace_id: str,
) -> PlatformJobActionResponse:
    _valid_job_id(job_id)
    if action not in {"retry", "cancel"} or _IDEMPOTENCY_RE.fullmatch(idempotency_key) is None:
        raise ApiError("platform_job.request_invalid")
    canonical = {
        "action": action,
        "expected_revision": expected_revision,
        "job_id": job_id,
        **payload.model_dump(mode="json"),
    }
    request_sha256 = _digest(canonical)
    idempotency_sha256 = _hmac(config, "command", idempotency_key)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_platform_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=True,
        )
        _require(actor, "platform.job.manage")
        replay = _command_replay(
            connection,
            actor,
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            expected_action=f"job.{action}",
            expected_object=job_id,
        )
        if replay:
            response = PlatformJobActionResponse(
                job=_job_info(_job_row(connection, job_id)),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return response
        row = _job_row(connection, job_id)
        if int(row["revision"]) != expected_revision:
            raise ApiError("platform_job.revision_conflict")
        now = _utcnow()
        if action == "cancel":
            if row["status"] == "queued":
                next_status = "cancelled"
                outcome = "platform_job.cancelled"
                completed = _db_time(now)
                _event(connection, row, "platform_job.completed", outcome, now=now)
            elif row["status"] in {"leased", "running"}:
                next_status = "cancel_requested"
                outcome = None
                completed = None
            else:
                raise ApiError("platform_job.conflict")
        else:
            if row["status"] not in {"failed", "dead_lettered"}:
                raise ApiError("platform_job.conflict")
            if int(row["attempt_count"]) >= 20:
                raise ApiError("platform_job.conflict")
            next_status = "queued"
            outcome = None
            completed = None
        updated = connection.execute(
            """
            UPDATE platform_jobs SET status=?,revision=revision+1,safe_outcome_code=?,
              scheduled_at=?,completed_at=?,updated_at=?
            WHERE platform_job_id=? AND revision=?
            """,
            (
                next_status,
                outcome,
                _db_time(now),
                completed,
                _db_time(_changed_time(now, row["updated_at"])),
                job_id,
                expected_revision,
            ),
        ).rowcount
        if updated != 1:
            raise ApiError("platform_job.revision_conflict")
        current = _job_row(connection, job_id)
        connection.execute(
            """
            INSERT INTO platform_job_commands(
              platform_job_command_id,operator_id,action_code,platform_job_id,type_code,
              idempotency_sha256,request_sha256,result_status,result_revision,created_at)
            VALUES(?,?,?, ?,NULL,?,?,?,?,?)
            """,
            (
                _id("pjm_"),
                actor.operator_id,
                f"job.{action}",
                job_id,
                idempotency_sha256,
                request_sha256,
                current["status"],
                int(current["revision"]),
                _db_time(now),
            ),
        )
        _audit(
            connection,
            actor=actor,
            action=f"platform_job.{action}_requested",
            object_type="platform_job",
            object_id=job_id,
            reason=payload.reason_code,
            request_id=request_id,
            trace_id=trace_id,
            after={"revision": int(current["revision"]), "status": current["status"]},
            now=now,
        )
        response = PlatformJobActionResponse(
            job=_job_info(current),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("platform_job.unavailable") from None
    finally:
        connection.close()


def update_platform_job_type(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    expected_revision: int,
    idempotency_key: str,
    payload: PlatformJobTypeCommandRequest,
    request_id: str,
    trace_id: str,
) -> PlatformJobTypeResponse:
    if _IDEMPOTENCY_RE.fullmatch(idempotency_key) is None:
        raise ApiError("platform_job.request_invalid")
    artifact = ARTIFACTS.get(payload.type_code)
    definition = handler_definition(payload.type_code)
    if artifact is None or (payload.action == "activate" and definition is None):
        raise ApiError("platform_job.type_unavailable")
    canonical = {"expected_revision": expected_revision, **payload.model_dump(mode="json")}
    request_sha256 = _digest(canonical)
    idempotency_sha256 = _hmac(config, "command", idempotency_key)
    action_code = f"type.{payload.action}"
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_platform_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=True,
        )
        _require(actor, "platform.job.type_manage")
        replay = _command_replay(
            connection,
            actor,
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            expected_action=action_code,
            expected_object=payload.type_code,
        )
        if replay:
            row = _type_row(connection, payload.type_code)
            response = PlatformJobTypeResponse(
                job_type=_type_info(row),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return response
        row = _type_row(connection, payload.type_code)
        if int(row["revision"]) != expected_revision:
            raise ApiError("platform_job.revision_conflict")
        if row["artifact_sha256"] != artifact:
            raise ApiError("platform_job.type_unavailable")
        next_status = "active" if payload.action == "activate" else "disabled"
        if row["status"] == next_status:
            raise ApiError("platform_job.conflict")
        now = _utcnow()
        updated = connection.execute(
            "UPDATE platform_job_types SET status=?,revision=revision+1,updated_at=? "
            "WHERE type_code=? AND revision=?",
            (
                next_status,
                _db_time(_changed_time(now, row["updated_at"])),
                payload.type_code,
                expected_revision,
            ),
        ).rowcount
        if updated != 1:
            raise ApiError("platform_job.revision_conflict")
        current = _type_row(connection, payload.type_code)
        connection.execute(
            """
            INSERT INTO platform_job_commands(
              platform_job_command_id,operator_id,action_code,platform_job_id,type_code,
              idempotency_sha256,request_sha256,result_status,result_revision,created_at)
            VALUES(?,?,?,NULL,?,?,?,?,?,?)
            """,
            (
                _id("pjm_"),
                actor.operator_id,
                action_code,
                payload.type_code,
                idempotency_sha256,
                request_sha256,
                current["status"],
                int(current["revision"]),
                _db_time(now),
            ),
        )
        _audit(
            connection,
            actor=actor,
            action=f"platform_job_type.{payload.action}",
            object_type="platform_job_type",
            object_id=payload.type_code,
            reason=payload.reason_code,
            request_id=request_id,
            trace_id=trace_id,
            after={"revision": int(current["revision"]), "status": current["status"]},
            now=now,
        )
        response = PlatformJobTypeResponse(
            job_type=_type_info(current), request_id=request_id, trace_id=trace_id
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("platform_job.unavailable") from None
    finally:
        connection.close()


def _command_replay(
    connection,
    actor: PlatformContext,
    *,
    idempotency_sha256: str,
    request_sha256: str,
    expected_action: str,
    expected_object: str,
) -> bool:
    row = connection.execute(
        "SELECT * FROM platform_job_commands WHERE operator_id=? AND idempotency_sha256=?",
        (actor.operator_id, idempotency_sha256),
    ).fetchone()
    if row is None:
        return False
    actual_object = row["platform_job_id"] or row["type_code"]
    if not (
        hmac.compare_digest(str(row["request_sha256"]), request_sha256)
        and row["action_code"] == expected_action
        and actual_object == expected_object
    ):
        raise ApiError("platform_job.idempotency_conflict")
    return True


def _job_row(connection, job_id: str):
    row = connection.execute(
        "SELECT * FROM platform_jobs WHERE platform_job_id=?", (job_id,)
    ).fetchone()
    if row is None:
        raise ApiError("platform_job.not_found")
    return row


def _type_row(connection, type_code: str):
    row = connection.execute(
        "SELECT * FROM platform_job_types WHERE type_code=?", (type_code,)
    ).fetchone()
    if row is None:
        raise ApiError("platform_job.type_unavailable")
    return row


def _job_info(row) -> PlatformJobSummary:
    return PlatformJobSummary(
        job_id=row["platform_job_id"],
        type_code=row["type_code"],
        queue_code=row["queue_code"],
        priority=int(row["priority"]),
        status=row["status"],
        revision=int(row["revision"]),
        generation=int(row["generation"]),
        attempt_count=int(row["attempt_count"]),
        max_attempts=int(row["max_attempts"]),
        safe_outcome_code=row["safe_outcome_code"],
        scheduled_at=_time(row["scheduled_at"]),
        started_at=_optional_time(row["started_at"]),
        completed_at=_optional_time(row["completed_at"]),
        created_at=_time(row["created_at"]),
        updated_at=_time(row["updated_at"]),
    )


def _type_info(row) -> PlatformJobTypeInfo:
    return PlatformJobTypeInfo(
        type_code=row["type_code"],
        contract_version=int(row["contract_version"]),
        queue_code=row["queue_code"],
        resource_class=row["resource_class"],
        max_attempts=int(row["max_attempts"]),
        timeout_seconds=int(row["timeout_seconds"]),
        status=row["status"],
        revision=int(row["revision"]),
    )


def _attempt_info(row) -> PlatformJobAttemptInfo:
    return PlatformJobAttemptInfo(
        attempt_id=row["platform_job_attempt_id"],
        attempt_number=int(row["attempt_number"]),
        generation=int(row["generation"]),
        status=row["status"],
        safe_outcome_code=row["safe_outcome_code"],
        leased_at=_time(row["leased_at"]),
        started_at=_optional_time(row["started_at"]),
        completed_at=_optional_time(row["completed_at"]),
    )


def _checkpoint_info(row) -> PlatformJobCheckpointInfo:
    return PlatformJobCheckpointInfo(
        checkpoint_id=row["platform_job_checkpoint_id"],
        generation=int(row["generation"]),
        sequence_number=int(row["sequence_number"]),
        checkpoint_sha256=row["checkpoint_sha256"],
        created_at=_time(row["created_at"]),
    )


def _event(connection, job, event_type: str, outcome: str | None, *, now: datetime) -> None:
    attempt = connection.execute(
        "SELECT platform_job_attempt_id FROM platform_job_attempts "
        "WHERE platform_job_id=? AND generation=?",
        (job["platform_job_id"], int(job["generation"])),
    ).fetchone()
    connection.execute(
        """
        INSERT INTO platform_job_event_outbox(
          platform_job_event_outbox_id,platform_job_id,platform_job_attempt_id,event_type,
          type_code,generation,safe_outcome_code,request_id,trace_id,status,attempt_count,
          next_attempt_at,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,'pending',0,?,?,?)
        """,
        (
            _id("pjo_"),
            job["platform_job_id"],
            attempt[0] if attempt is not None else None,
            event_type,
            job["type_code"],
            int(job["generation"]),
            outcome,
            job["request_id"],
            job["trace_id"],
            _db_time(now),
            _db_time(now),
            _db_time(now),
        ),
    )


def _audit(
    connection,
    *,
    actor: PlatformContext,
    action: str,
    object_type: str,
    object_id: str,
    reason: str,
    request_id: str,
    trace_id: str,
    after: object,
    now: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO platform_audit_logs(
          audit_id,actor_domain,actor_id,action_code,object_type,object_id,reason_code,
          request_id,trace_id,before_sha256,after_sha256,occurred_at)
        VALUES(?,'platform',?,?,?,?,?,?,?,NULL,?,?)
        """,
        (
            _id("pau_"),
            actor.operator_id,
            action,
            object_type,
            object_id,
            reason,
            request_id,
            trace_id,
            _digest(after),
            _db_time(now),
        ),
    )


def _require(context: PlatformContext, permission: str) -> None:
    if permission not in context.permissions:
        raise ApiError("platform.permission_denied")


def _valid_job_id(job_id: str) -> None:
    if _JOB_ID_RE.fullmatch(job_id) is None:
        raise ApiError("platform_job.not_found")


def _connect(config: AppConfig):
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("platform_job.unavailable") from None


def _rollback(connection) -> None:
    try:
        if connection.in_transaction:
            connection.rollback()
    except Exception:
        pass


def _hmac(config: AppConfig, purpose: str, value: str) -> str:
    return hmac.new(
        config.platform_session_secret.encode("utf-8"),
        f"prd106/{purpose}/v1\0{value}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
            "ascii"
        )
    ).hexdigest()


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _changed_time(now: datetime, previous: object) -> datetime:
    prior = _time(previous)
    return now if now > prior else prior + timedelta(milliseconds=1)


def _time(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _optional_time(value: object | None) -> datetime | None:
    return None if value is None else _time(value)


__all__ = [
    "PreparedPlatformJob",
    "enqueue_platform_job",
    "enqueue_prepared_platform_job_in_transaction",
    "get_platform_job",
    "list_platform_job_queues",
    "prepare_platform_job",
    "update_platform_job",
    "update_platform_job_type",
]
