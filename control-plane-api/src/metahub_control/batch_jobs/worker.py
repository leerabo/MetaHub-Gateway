"""Independent fair Scheduler/Worker core for PRD-074 Batch tasks.

All SQLite claims and state transitions are short ``BEGIN IMMEDIATE`` transactions. File
decryption and Gateway HTTP happen after commit. Secret tokens, grants, prompts, responses, and
file text are stored only in repr-hidden objects or encrypted envelopes and are never logged.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from secrets import token_urlsafe
from typing import Literal
from uuid import uuid4

import httpx
from metahub_shared.batch import (
    BATCH_KEY_VERSION,
    BATCH_OPERATION_TEMPLATE_VERSION,
    BatchCryptoError,
    BatchEnvelopeOwner,
    batch_payload_hmac,
    encrypt_batch_payload,
    render_batch_messages,
)
from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.auth import require_actor_transaction_permissions
from metahub_control.file_security import build_file_model_context_for_batch

from .service import (
    attempt_reserved_cost,
    decrypt_job_operation_spec,
    finalize_batch_job_if_terminal,
    rollover_batch_budget_holds,
    route_plan_hash,
)

_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_BATCH_ERROR_CODE = re.compile(r"^batch\.[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")
_MONEY_QUANT = Decimal("0.000001")
_TERMINAL_TASKS = frozenset(
    {"succeeded", "failed", "cancelled", "expired", "outcome_unknown"}
)
_RETRY_DELAYS = (5, 30)
_BUSY_BACKOFF_MIN_SECONDS = 0.05
_BUSY_BACKOFF_MAX_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class BatchTaskLease:
    tenant_id: str
    batch_job_id: str
    batch_task_id: str
    batch_attempt_id: str
    file_id: str
    submitted_by_user_id: str
    model_alias_id: str
    virtual_key_id: str
    route_plan_json: str
    operation_spec_ciphertext: str = field(repr=False)
    max_output_tokens: int = 0
    max_attempts: int = 0
    attempt_no: int = 0
    lease_token: str = field(default="", repr=False)
    lease_owner_id: str = ""
    lease_generation: int = 0
    lease_expires_at: datetime | None = None
    deadline_at: datetime | None = None
    existing_attempt: bool = False


@dataclass(frozen=True, slots=True)
class PreparedBatchAttempt:
    lease: BatchTaskLease
    request_id: str
    trace_id: str
    batch_execution_grant_id: str
    grant: str = field(repr=False)
    grant_generation: int = 0
    grant_expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class GatewayCallFact:
    outcome: Literal["accepted", "busy", "rejected", "transport_unknown"]
    error_code: str | None = None


def claim_next_batch_task(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> BatchTaskLease | None:
    """Fairly claim one due task while proving global and per-tenant DB caps."""

    owner = _worker_id(worker_id)
    current = _now(now)
    current_text = _db_time(current)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        active = connection.execute(
            """
            SELECT COUNT(*) FROM batch_tasks
            WHERE status IN ('leased','calling')
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (current_text,),
        ).fetchone()[0]
        if type(active) is not int or active >= config.batch_global_concurrency:
            connection.commit()
            return None
        tenant = connection.execute(
            """
            SELECT task.tenant_id,
                   COALESCE(cursor.last_dispatched_at,'1970-01-01T00:00:00Z') AS last_at,
                   COALESCE(cursor.dispatch_revision,0) AS dispatch_revision
            FROM batch_tasks task
            JOIN batch_jobs job
              ON job.tenant_id=task.tenant_id AND job.batch_job_id=task.batch_job_id
            LEFT JOIN batch_scheduler_tenants cursor ON cursor.tenant_id=task.tenant_id
            WHERE task.status IN ('queued','retry_wait')
              AND unixepoch(task.next_attempt_at)<=unixepoch(?)
              AND job.status IN ('queued','running') AND job.cancel_requested_at IS NULL
              AND unixepoch(job.deadline_at)>unixepoch(?)
              AND (
                SELECT COUNT(*) FROM batch_tasks active_task
                WHERE active_task.tenant_id=task.tenant_id
                  AND active_task.status IN ('leased','calling')
                  AND unixepoch(active_task.lease_expires_at)>unixepoch(?)
              ) < ?
            GROUP BY task.tenant_id
            ORDER BY last_at,dispatch_revision,task.tenant_id LIMIT 1
            """,
            (
                current_text,
                current_text,
                current_text,
                config.batch_tenant_concurrency,
            ),
        ).fetchone()
        if tenant is None:
            connection.commit()
            return None
        row = connection.execute(
            """
            SELECT task.*,job.submitted_by_user_id,job.model_alias_id,job.virtual_key_id,
                   job.route_plan_json,job.operation_spec_ciphertext,job.max_output_tokens,
                   job.max_attempts,job.deadline_at
            FROM batch_tasks task
            JOIN batch_jobs job
              ON job.tenant_id=task.tenant_id AND job.batch_job_id=task.batch_job_id
            WHERE task.tenant_id=? AND task.status IN ('queued','retry_wait')
              AND unixepoch(task.next_attempt_at)<=unixepoch(?)
              AND job.status IN ('queued','running') AND job.cancel_requested_at IS NULL
              AND unixepoch(job.deadline_at)>unixepoch(?)
            ORDER BY task.priority_rank DESC,task.next_attempt_at,
                     task.created_at,task.batch_task_id LIMIT 1
            """,
            (tenant["tenant_id"], current_text, current_text),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        try:
            require_actor_transaction_permissions(
                connection,
                tenant_id=str(row["tenant_id"]),
                user_id=str(row["submitted_by_user_id"]),
                permission_codes=("batch.write", "file.read"),
                unavailable_error_code="batch.worker_unavailable",
            )
        except ApiError as exc:
            if exc.code != "auth.permission_denied":
                raise
            _fail_unclaimed_task(
                connection,
                row,
                code="batch.authorization_revoked",
                now=current_text,
            )
            finalize_batch_job_if_terminal(
                connection,
                tenant_id=str(row["tenant_id"]),
                batch_job_id=str(row["batch_job_id"]),
                request_id=f"batch-worker-{row['batch_task_id']}",
                trace_id=f"batch-worker-{row['batch_task_id']}",
                now=current_text,
            )
            connection.commit()
            return None
        attempt_id = str(row["current_attempt_id"] or f"batm_{uuid4().hex}")
        token = token_urlsafe(32)
        generation = int(row["lease_generation"]) + 1
        deadline = _time(row["deadline_at"])
        expires = min(current + timedelta(seconds=config.batch_lease_seconds), deadline)
        updated = connection.execute(
            """
            UPDATE batch_tasks
            SET status='leased',lease_token=?,lease_owner_id=?,lease_generation=?,
                lease_expires_at=?,current_attempt_id=?,updated_at=?
            WHERE tenant_id=? AND batch_task_id=? AND batch_job_id=?
              AND status IN ('queued','retry_wait') AND lease_generation=?
              AND unixepoch(next_attempt_at)<=unixepoch(?)
            """,
            (
                token,
                owner,
                generation,
                _db_time(expires),
                attempt_id,
                current_text,
                row["tenant_id"],
                row["batch_task_id"],
                row["batch_job_id"],
                row["lease_generation"],
                current_text,
            ),
        )
        if updated.rowcount != 1:
            connection.rollback()
            return None
        connection.execute(
            """
            INSERT INTO batch_scheduler_tenants(
              tenant_id,last_dispatched_at,dispatch_revision,updated_at
            ) VALUES(?,?,1,?)
            ON CONFLICT(tenant_id) DO UPDATE SET
              last_dispatched_at=excluded.last_dispatched_at,
              dispatch_revision=batch_scheduler_tenants.dispatch_revision+1,
              updated_at=excluded.updated_at
            """,
            (row["tenant_id"], current_text, current_text),
        )
        connection.execute(
            """
            UPDATE batch_jobs
            SET status='running',started_at=COALESCE(started_at,?),updated_at=?
            WHERE tenant_id=? AND batch_job_id=? AND status='queued'
            """,
            (current_text, current_text, row["tenant_id"], row["batch_job_id"]),
        )
        existing = connection.execute(
            """
            SELECT 1 FROM batch_attempts
            WHERE tenant_id=? AND batch_attempt_id=? AND batch_task_id=? AND status='prepared'
            """,
            (row["tenant_id"], attempt_id, row["batch_task_id"]),
        ).fetchone()
        lease = BatchTaskLease(
            tenant_id=str(row["tenant_id"]),
            batch_job_id=str(row["batch_job_id"]),
            batch_task_id=str(row["batch_task_id"]),
            batch_attempt_id=attempt_id,
            file_id=str(row["file_id"]),
            submitted_by_user_id=str(row["submitted_by_user_id"]),
            model_alias_id=str(row["model_alias_id"]),
            virtual_key_id=str(row["virtual_key_id"]),
            route_plan_json=str(row["route_plan_json"]),
            operation_spec_ciphertext=str(row["operation_spec_ciphertext"]),
            max_output_tokens=int(row["max_output_tokens"]),
            max_attempts=int(row["max_attempts"]),
            attempt_no=int(row["attempt_count"]) + 1,
            lease_token=token,
            lease_owner_id=owner,
            lease_generation=generation,
            lease_expires_at=expires,
            deadline_at=deadline,
            existing_attempt=existing is not None,
        )
        connection.commit()
        return lease
    except ApiError:
        connection.rollback()
        raise
    except (sqlite3.Error, TypeError, ValueError):
        connection.rollback()
        raise ApiError("batch.worker_unavailable") from None
    finally:
        connection.close()


def prepare_batch_attempt(
    config: AppConfig,
    lease: BatchTaskLease,
    *,
    now: datetime | None = None,
) -> PreparedBatchAttempt:
    """Encrypt a fixed payload, allocate holds, and issue a one-time execution grant."""

    current = _now(now)
    if lease.existing_attempt:
        return _prepare_existing_attempt(config, lease, current=current)
    try:
        bounded = build_file_model_context_for_batch(
            config,
            tenant_id=lease.tenant_id,
            batch_job_id=lease.batch_job_id,
            batch_task_id=lease.batch_task_id,
        )
        operation = decrypt_job_operation_spec(
            config,
            tenant_id=lease.tenant_id,
            batch_job_id=lease.batch_job_id,
            ciphertext=lease.operation_spec_ciphertext,
        )
        system, user = render_batch_messages(operation, bounded.text)
        payload = json.dumps(
            {
                "schema_version": 1,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        payload_bytes = payload.encode("utf-8", errors="strict")
    except ApiError as exc:
        _terminalize_lease_error(config, lease, code=_batch_execution_code(exc.code), now=current)
        raise
    except (UnicodeError, TypeError, ValueError):
        _terminalize_lease_error(config, lease, code="batch.worker_unavailable", now=current)
        raise ApiError("batch.worker_unavailable") from None
    request_id = f"breq_{uuid4().hex}"
    trace_id = f"btrc_{uuid4().hex}"
    envelope_owner = BatchEnvelopeOwner(
        tenant_id=lease.tenant_id,
        batch_job_id=lease.batch_job_id,
        batch_task_id=lease.batch_task_id,
        batch_attempt_id=lease.batch_attempt_id,
        request_id=request_id,
        template_version=BATCH_OPERATION_TEMPLATE_VERSION,
        key_version=BATCH_KEY_VERSION,
    )
    secret = _batch_secret(config)
    try:
        ciphertext = encrypt_batch_payload(
            payload_bytes,
            secret=secret,
            owner=envelope_owner,
        )
        payload_hmac = batch_payload_hmac(
            payload_bytes,
            secret=secret,
            owner=envelope_owner,
        )
    except BatchCryptoError:
        _terminalize_lease_error(config, lease, code="batch.worker_unavailable", now=current)
        raise ApiError("batch.worker_unavailable") from None
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = _load_current_lease(connection, lease, current=current)
        rollover_batch_budget_holds(
            connection,
            tenant_id=lease.tenant_id,
            batch_job_id=lease.batch_job_id,
            current=current,
        )
        input_row = connection.execute(
            """
            SELECT extract.estimated_tokens
            FROM file_extracts extract
            WHERE extract.tenant_id=? AND extract.file_id=? AND extract.status='ready'
            """,
            (lease.tenant_id, lease.file_id),
        ).fetchone()
        if input_row is None or type(input_row["estimated_tokens"]) is not int:
            raise ApiError("batch.file_not_ready")
        input_tokens = int(input_row["estimated_tokens"]) + 256
        reserved = attempt_reserved_cost(
            lease.route_plan_json,
            input_tokens=input_tokens,
            output_tokens=lease.max_output_tokens,
        )
        now_text = _db_time(current)
        connection.execute(
            """
            INSERT INTO batch_attempts(
              batch_attempt_id,tenant_id,batch_job_id,batch_task_id,attempt_no,request_id,
              trace_id,status,execution_outcome,retry_safe,gateway_error_code,
              request_payload_ciphertext,request_payload_key_version,request_payload_hmac,
              route_plan_json,gateway_write_deadline_at,result_state,result_ciphertext,
              result_key_version,result_hmac,input_tokens,output_tokens,cost_cny,
              provider_started_at,gateway_completed_at,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,'prepared','not_invoked',0,NULL,?,?,?, ?,NULL,
                     'none',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,?,?)
            """,
            (
                lease.batch_attempt_id,
                lease.tenant_id,
                lease.batch_job_id,
                lease.batch_task_id,
                lease.attempt_no,
                request_id,
                trace_id,
                ciphertext,
                BATCH_KEY_VERSION,
                payload_hmac,
                lease.route_plan_json,
                now_text,
                now_text,
            ),
        )
        _allocate_attempt(
            connection,
            lease=lease,
            reserved=reserved,
            route_hash=route_plan_hash(lease.route_plan_json),
            now=now_text,
        )
        if row["cancel_requested_at"] is not None:
            raise ApiError("batch.execution_invalid")
        prepared = _insert_grant(
            connection,
            lease,
            request_id=request_id,
            trace_id=trace_id,
            current=current,
        )
        connection.commit()
        return prepared
    except ApiError as exc:
        connection.rollback()
        _terminalize_lease_error(config, lease, code=_batch_execution_code(exc.code), now=current)
        raise
    except (sqlite3.Error, TypeError, ValueError, ArithmeticError):
        connection.rollback()
        _terminalize_lease_error(config, lease, code="batch.worker_unavailable", now=current)
        raise ApiError("batch.worker_unavailable") from None
    finally:
        connection.close()


def refresh_batch_grant(
    config: AppConfig,
    prepared: PreparedBatchAttempt,
    *,
    now: datetime | None = None,
) -> PreparedBatchAttempt:
    """Rotate an unconsumed grant for the same prepared attempt and Provider identity."""

    return _prepare_existing_attempt(config, prepared.lease, current=_now(now))


def _prepare_existing_attempt(
    config: AppConfig,
    lease: BatchTaskLease,
    *,
    current: datetime,
) -> PreparedBatchAttempt:
    try:
        _ensure_existing_attempt_allocation(config, lease, current=current)
    except ApiError as exc:
        _terminalize_lease_error(
            config,
            lease,
            code=_batch_execution_code(exc.code),
            now=current,
        )
        raise
    return _issue_grant(config, lease, current=current)


def _ensure_existing_attempt_allocation(
    config: AppConfig,
    lease: BatchTaskLease,
    *,
    current: datetime,
) -> None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _load_current_lease(connection, lease, current=current)
        attempt = connection.execute(
            """
            SELECT status,execution_outcome,route_plan_json FROM batch_attempts
            WHERE tenant_id=? AND batch_attempt_id=? AND batch_task_id=?
            """,
            (lease.tenant_id, lease.batch_attempt_id, lease.batch_task_id),
        ).fetchone()
        if (
            attempt is None
            or attempt["status"] != "prepared"
            or attempt["execution_outcome"] != "not_invoked"
            or attempt["route_plan_json"] != lease.route_plan_json
        ):
            raise ApiError("batch.execution_invalid")
        rollover_batch_budget_holds(
            connection,
            tenant_id=lease.tenant_id,
            batch_job_id=lease.batch_job_id,
            current=current,
        )
        input_row = connection.execute(
            """
            SELECT estimated_tokens FROM file_extracts
            WHERE tenant_id=? AND file_id=? AND status='ready'
            """,
            (lease.tenant_id, lease.file_id),
        ).fetchone()
        if input_row is None or type(input_row["estimated_tokens"]) is not int:
            raise ApiError("batch.file_not_ready")
        reserved = attempt_reserved_cost(
            lease.route_plan_json,
            input_tokens=int(input_row["estimated_tokens"]) + 256,
            output_tokens=lease.max_output_tokens,
        )
        _allocate_attempt(
            connection,
            lease=lease,
            reserved=reserved,
            route_hash=route_plan_hash(lease.route_plan_json),
            now=_db_time(current),
        )
        connection.commit()
    except ApiError:
        connection.rollback()
        raise
    except (sqlite3.Error, TypeError, ValueError, ArithmeticError):
        connection.rollback()
        raise ApiError("batch.worker_unavailable") from None
    finally:
        connection.close()


def call_batch_gateway(
    config: AppConfig,
    prepared: PreparedBatchAttempt,
    *,
    client: httpx.Client | None = None,
) -> GatewayCallFact:
    """Send only the grant to the internal Gateway and return a fixed transport fact."""

    token = config.internal_batch_token
    if type(token) is not str or not token:
        raise ApiError("batch.worker_unavailable")
    url = config.gateway_api_base_url.rstrip("/") + "/internal/batch/chat/completions"
    own_client = client is None
    active_client = client or httpx.Client(
        timeout=httpx.Timeout(65.0, connect=5.0),
        follow_redirects=False,
    )
    stop = threading.Event()
    heartbeat = threading.Thread(
        target=_heartbeat_loop,
        args=(config, prepared.lease, stop),
        name=f"batch-heartbeat-{prepared.lease.batch_task_id}",
        daemon=True,
    )
    heartbeat.start()
    try:
        response = active_client.post(
            url,
            headers={
                "Content-Type": "application/json",
                "X-MetaHub-Internal-Batch-Token": token,
                "X-Request-ID": prepared.request_id,
                "X-Trace-ID": prepared.trace_id,
            },
            json={"grant": prepared.grant},
        )
        error_code = _gateway_error_code(response)
        if response.status_code == 200:
            return GatewayCallFact(outcome="accepted")
        if response.status_code == 429 and error_code == "batch.gateway_busy":
            return GatewayCallFact(outcome="busy", error_code=error_code)
        return GatewayCallFact(outcome="rejected", error_code=error_code)
    except httpx.TransportError:
        return GatewayCallFact(outcome="transport_unknown")
    finally:
        stop.set()
        heartbeat.join(timeout=max(float(config.batch_heartbeat_seconds), 1.0) + 1.0)
        if own_client:
            active_client.close()


def settle_batch_attempt(
    config: AppConfig,
    prepared: PreparedBatchAttempt,
    fact: GatewayCallFact,
    *,
    now: datetime | None = None,
) -> bool:
    """Project durable Gateway facts to Task state; never infer outcome from HTTP alone."""

    if fact.outcome in {"busy", "transport_unknown"}:
        return False
    current = _now(now)
    now_text = _db_time(current)
    lease = prepared.lease
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        task = _load_current_lease(connection, lease, current=None)
        attempt = connection.execute(
            """
            SELECT * FROM batch_attempts
            WHERE tenant_id=? AND batch_attempt_id=? AND batch_task_id=?
            """,
            (lease.tenant_id, lease.batch_attempt_id, lease.batch_task_id),
        ).fetchone()
        if attempt is None:
            connection.commit()
            return False
        cancelled = task["cancel_requested_at"] is not None
        expired = lease.deadline_at is not None and current >= lease.deadline_at
        revoked = False
        try:
            require_actor_transaction_permissions(
                connection,
                tenant_id=lease.tenant_id,
                user_id=lease.submitted_by_user_id,
                permission_codes=("batch.write", "file.read"),
                unavailable_error_code="batch.worker_unavailable",
            )
        except ApiError as exc:
            if exc.code == "auth.permission_denied":
                revoked = True
            else:
                raise
        if attempt["result_state"] == "available" and attempt["status"] == "provider_succeeded":
            if not _successful_attempt_usage_posted(
                connection,
                tenant_id=lease.tenant_id,
                batch_attempt_id=lease.batch_attempt_id,
                cost_cny=attempt["cost_cny"],
            ):
                connection.commit()
                return False
            if cancelled or expired or revoked:
                connection.execute(
                    """
                    UPDATE batch_attempts
                    SET result_state='discarded',result_ciphertext=NULL,result_key_version=NULL,
                        updated_at=?
                    WHERE tenant_id=? AND batch_attempt_id=? AND result_state='available'
                    """,
                    (now_text, lease.tenant_id, lease.batch_attempt_id),
                )
                terminal = "cancelled" if cancelled else "expired" if expired else "failed"
                code = None if cancelled else (
                    "batch.authorization_revoked" if revoked else "batch.worker_unavailable"
                )
                _finish_leased_task(
                    connection,
                    lease,
                    status=terminal,
                    failure_code=code,
                    request_id=str(attempt["request_id"]),
                    trace_id=str(attempt["trace_id"]),
                    result_available=False,
                    now=now_text,
                )
            else:
                _finish_leased_task(
                    connection,
                    lease,
                    status="succeeded",
                    failure_code=None,
                    request_id=str(attempt["request_id"]),
                    trace_id=str(attempt["trace_id"]),
                    result_available=True,
                    now=now_text,
                )
        elif attempt["status"] == "known_failed":
            retry = (
                attempt["retry_safe"] == 1
                and int(task["attempt_count"]) < lease.max_attempts
                and not cancelled
                and not expired
                and not revoked
            )
            if retry:
                delay_index = min(max(int(task["attempt_count"]) - 1, 0), len(_RETRY_DELAYS) - 1)
                connection.execute(
                    """
                    UPDATE batch_tasks
                    SET status='retry_wait',next_attempt_at=?,lease_token=NULL,lease_owner_id=NULL,
                        lease_expires_at=NULL,current_attempt_id=NULL,failure_code=?,updated_at=?
                    WHERE tenant_id=? AND batch_task_id=? AND current_attempt_id=?
                      AND lease_token=? AND lease_owner_id=? AND lease_generation=?
                    """,
                    (
                        _db_time(current + timedelta(seconds=_RETRY_DELAYS[delay_index])),
                        _safe_gateway_failure(attempt["gateway_error_code"]),
                        now_text,
                        lease.tenant_id,
                        lease.batch_task_id,
                        lease.batch_attempt_id,
                        lease.lease_token,
                        lease.lease_owner_id,
                        lease.lease_generation,
                    ),
                )
            else:
                status = "cancelled" if cancelled else "expired" if expired else "failed"
                code = None if cancelled else (
                    "batch.authorization_revoked"
                    if revoked
                    else _safe_gateway_failure(attempt["gateway_error_code"])
                )
                _finish_leased_task(
                    connection,
                    lease,
                    status=status,
                    failure_code=code,
                    request_id=str(attempt["request_id"]),
                    trace_id=str(attempt["trace_id"]),
                    result_available=False,
                    now=now_text,
                )
        elif attempt["status"] == "outcome_unknown":
            _finish_leased_task(
                connection,
                lease,
                status="outcome_unknown",
                failure_code="batch.provider_outcome_unknown",
                request_id=str(attempt["request_id"]),
                trace_id=str(attempt["trace_id"]),
                result_available=False,
                now=now_text,
            )
        elif attempt["status"] == "provider_succeeded":
            _finish_leased_task(
                connection,
                lease,
                status="failed",
                failure_code="batch.result_unavailable",
                request_id=str(attempt["request_id"]),
                trace_id=str(attempt["trace_id"]),
                result_available=False,
                now=now_text,
            )
        else:
            connection.commit()
            return False
        finalize_batch_job_if_terminal(
            connection,
            tenant_id=lease.tenant_id,
            batch_job_id=lease.batch_job_id,
            request_id=prepared.request_id,
            trace_id=prepared.trace_id,
            now=now_text,
        )
        connection.commit()
        return True
    except ApiError:
        connection.rollback()
        raise
    except (sqlite3.Error, TypeError, ValueError):
        connection.rollback()
        raise ApiError("batch.worker_unavailable") from None
    finally:
        connection.close()


def heartbeat_batch_task(
    config: AppConfig,
    lease: BatchTaskLease,
    *,
    now: datetime | None = None,
) -> bool:
    """Extend only the exact live lease owner and never beyond the Job deadline."""

    current = _now(now)
    if lease.deadline_at is None or current >= lease.deadline_at:
        return False
    expires = min(current + timedelta(seconds=config.batch_lease_seconds), lease.deadline_at)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        updated = connection.execute(
            """
            UPDATE batch_tasks
            SET lease_expires_at=?,updated_at=?
            WHERE tenant_id=? AND batch_job_id=? AND batch_task_id=?
              AND current_attempt_id=? AND lease_token=? AND lease_owner_id=?
              AND lease_generation=? AND status IN ('leased','calling')
            """,
            (
                _db_time(expires),
                _db_time(current),
                lease.tenant_id,
                lease.batch_job_id,
                lease.batch_task_id,
                lease.batch_attempt_id,
                lease.lease_token,
                lease.lease_owner_id,
                lease.lease_generation,
            ),
        )
        connection.commit()
        return updated.rowcount == 1
    except sqlite3.Error:
        connection.rollback()
        return False
    finally:
        connection.close()


def reap_expired_batch_tasks(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
    limit: int = 100,
) -> int:
    """Recover expired leases using durable result/outcome/routing evidence in fixed order."""

    _worker_id(worker_id)
    current = _now(now)
    now_text = _db_time(current)
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ApiError("batch.worker_unavailable")
    connection = _connect(config)
    progressed = 0
    try:
        connection.execute("BEGIN IMMEDIATE")
        _expire_unclaimed_work(connection, now=now_text)
        rows = connection.execute(
            """
            SELECT task.*,job.max_attempts,job.deadline_at,
                   job.cancel_requested_at AS job_cancel_requested_at,
                   job.submitted_by_user_id,
                   job.model_alias_id,job.virtual_key_id,job.operation_spec_ciphertext
            FROM batch_tasks task
            JOIN batch_jobs job
              ON job.tenant_id=task.tenant_id AND job.batch_job_id=task.batch_job_id
            WHERE task.status IN ('leased','calling')
              AND unixepoch(task.lease_expires_at)<=unixepoch(?)
            ORDER BY task.lease_expires_at,task.tenant_id,task.batch_task_id LIMIT ?
            """,
            (now_text, limit),
        ).fetchall()
        for task in rows:
            attempt = connection.execute(
                "SELECT * FROM batch_attempts WHERE tenant_id=? AND batch_attempt_id=?",
                (task["tenant_id"], task["current_attempt_id"]),
            ).fetchone()
            if attempt is None:
                _restore_task_without_attempt(connection, task, now=now_text)
                progressed += 1
                continue
            consumed = connection.execute(
                """
                SELECT 1 FROM batch_execution_grants
                WHERE tenant_id=? AND batch_attempt_id=? AND status='consumed'
                """,
                (task["tenant_id"], attempt["batch_attempt_id"]),
            ).fetchone()
            routing = connection.execute(
                """
                SELECT 1 FROM routing_attempts
                WHERE tenant_id=? AND request_id=? LIMIT 1
                """,
                (task["tenant_id"], attempt["request_id"]),
            ).fetchone()
            lease = _lease_from_rows(task, attempt)
            if attempt["result_state"] == "available":
                prepared = PreparedBatchAttempt(
                    lease=lease,
                    request_id=str(attempt["request_id"]),
                    trace_id=str(attempt["trace_id"]),
                    batch_execution_grant_id="reaper",
                    grant="reaper",
                )
                connection.commit()
                settled = settle_batch_attempt(
                    config,
                    prepared,
                    GatewayCallFact(outcome="accepted"),
                    now=current,
                )
                if settled:
                    progressed += 1
                connection.execute("BEGIN IMMEDIATE")
                continue
            if attempt["status"] == "prepared" and consumed is None and routing is None:
                connection.execute(
                    """
                    UPDATE batch_execution_grants
                    SET status='expired',revoked_at=?,updated_at=updated_at
                    WHERE tenant_id=? AND batch_attempt_id=? AND status='issued'
                    """.replace(",updated_at=updated_at", ""),
                    (now_text, task["tenant_id"], attempt["batch_attempt_id"]),
                )
                _release_lease_for_same_attempt(connection, task, now=now_text)
                progressed += 1
                continue
            if attempt["status"] == "known_failed":
                if attempt["retry_safe"] == 1:
                    _retry_or_finish_reaped(connection, task, attempt, now=current)
                else:
                    _finish_reaped_task(
                        connection,
                        task,
                        attempt,
                        status="failed",
                        code=_safe_gateway_failure(attempt["gateway_error_code"]),
                        now=now_text,
                    )
                progressed += 1
                continue
            if attempt["status"] == "provider_succeeded":
                _finish_reaped_task(
                    connection,
                    task,
                    attempt,
                    status="failed",
                    code="batch.result_unavailable",
                    now=now_text,
                )
                progressed += 1
                continue
            deadline = _optional_time(attempt["gateway_write_deadline_at"])
            if deadline is not None and current >= deadline:
                _mark_attempt_unknown(connection, task, attempt, now=now_text)
                progressed += 1
                continue
            if deadline is not None and current < deadline:
                connection.execute(
                    """
                    UPDATE batch_tasks SET lease_expires_at=?,updated_at=?
                    WHERE tenant_id=? AND batch_task_id=? AND lease_token=?
                      AND lease_generation=? AND lease_expires_at=?
                    """,
                    (
                        _db_time(deadline),
                        now_text,
                        task["tenant_id"],
                        task["batch_task_id"],
                        task["lease_token"],
                        task["lease_generation"],
                        task["lease_expires_at"],
                    ),
                )
        _finalize_all_terminal_jobs(connection, now=now_text)
        connection.commit()
        return progressed
    except ApiError:
        connection.rollback()
        raise
    except (sqlite3.Error, TypeError, ValueError):
        connection.rollback()
        raise ApiError("batch.worker_unavailable") from None
    finally:
        connection.close()


def run_batch_task_once(
    config: AppConfig,
    *,
    worker_id: str,
    client: httpx.Client | None = None,
    stop_event: threading.Event | None = None,
) -> bool:
    """Claim and process at most one task; a busy gate rotates only its grant."""

    if stop_event is not None and stop_event.is_set():
        return False
    reap_expired_batch_tasks(config, worker_id=worker_id)
    if stop_event is not None and stop_event.is_set():
        return False
    lease = claim_next_batch_task(config, worker_id=worker_id)
    if lease is None:
        return False
    if stop_event is not None and stop_event.is_set():
        return True
    try:
        prepared = prepare_batch_attempt(config, lease)
    except ApiError:
        return True
    consecutive_busy = 0
    while True:
        if stop_event is not None and stop_event.is_set():
            return True
        fact = call_batch_gateway(config, prepared, client=client)
        if fact.outcome == "busy":
            if stop_event is not None and stop_event.is_set():
                return True
            current = _utcnow()
            if lease.deadline_at is not None and current >= lease.deadline_at:
                return True
            consecutive_busy += 1
            delay = _busy_backoff_seconds(config, consecutive_busy)
            if lease.deadline_at is not None:
                delay = min(delay, max((lease.deadline_at - current).total_seconds(), 0.0))
            if delay <= 0:
                return True
            if stop_event is not None:
                if stop_event.wait(delay):
                    return True
            else:
                time.sleep(delay)
            if lease.deadline_at is not None and _utcnow() >= lease.deadline_at:
                return True
            try:
                prepared = refresh_batch_grant(config, prepared)
            except ApiError:
                return True
            continue
        settle_batch_attempt(config, prepared, fact)
        return True


def _issue_grant(
    config: AppConfig,
    lease: BatchTaskLease,
    *,
    current: datetime,
) -> PreparedBatchAttempt:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _load_current_lease(connection, lease, current=current)
        attempt = connection.execute(
            """
            SELECT request_id,trace_id,status FROM batch_attempts
            WHERE tenant_id=? AND batch_attempt_id=? AND batch_task_id=?
            """,
            (lease.tenant_id, lease.batch_attempt_id, lease.batch_task_id),
        ).fetchone()
        if attempt is None or attempt["status"] != "prepared":
            raise ApiError("batch.execution_invalid")
        connection.execute(
            """
            UPDATE batch_execution_grants
            SET status='revoked',revoked_at=?
            WHERE tenant_id=? AND batch_attempt_id=? AND status='issued'
            """,
            (_db_time(current), lease.tenant_id, lease.batch_attempt_id),
        )
        prepared = _insert_grant(
            connection,
            lease,
            request_id=str(attempt["request_id"]),
            trace_id=str(attempt["trace_id"]),
            current=current,
        )
        connection.commit()
        return prepared
    except ApiError:
        connection.rollback()
        raise
    except sqlite3.Error:
        connection.rollback()
        raise ApiError("batch.worker_unavailable") from None
    finally:
        connection.close()


def _insert_grant(
    connection: sqlite3.Connection,
    lease: BatchTaskLease,
    *,
    request_id: str,
    trace_id: str,
    current: datetime,
) -> PreparedBatchAttempt:
    generation = connection.execute(
        """
        SELECT COALESCE(MAX(grant_generation),0)+1 FROM batch_execution_grants
        WHERE tenant_id=? AND batch_attempt_id=?
        """,
        (lease.tenant_id, lease.batch_attempt_id),
    ).fetchone()[0]
    if type(generation) is not int or generation < 1:
        raise ApiError("batch.worker_unavailable")
    grant = token_urlsafe(32)
    grant_id = f"bgrt_{uuid4().hex}"
    expires = current + timedelta(seconds=60)
    connection.execute(
        """
        INSERT INTO batch_execution_grants(
          batch_execution_grant_id,tenant_id,batch_job_id,batch_task_id,batch_attempt_id,
          request_id,trace_id,virtual_key_id,model_alias_id,lease_token,lease_generation,
          grant_generation,grant_hash,status,issued_at,expires_at,used_at,revoked_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'issued',?,?,NULL,NULL)
        """,
        (
            grant_id,
            lease.tenant_id,
            lease.batch_job_id,
            lease.batch_task_id,
            lease.batch_attempt_id,
            request_id,
            trace_id,
            lease.virtual_key_id,
            lease.model_alias_id,
            lease.lease_token,
            lease.lease_generation,
            generation,
            hashlib.sha256(grant.encode("ascii")).hexdigest(),
            _db_time(current),
            _db_time(expires),
        ),
    )
    return PreparedBatchAttempt(
        lease=lease,
        request_id=request_id,
        trace_id=trace_id,
        batch_execution_grant_id=grant_id,
        grant=grant,
        grant_generation=generation,
        grant_expires_at=expires,
    )


def _allocate_attempt(
    connection: sqlite3.Connection,
    *,
    lease: BatchTaskLease,
    reserved: Decimal,
    route_hash: str,
    now: str,
) -> None:
    holds = connection.execute(
        """
        SELECT * FROM batch_budget_holds
        WHERE tenant_id=? AND batch_job_id=? AND status='active'
        ORDER BY budget_rule_id
        """,
        (lease.tenant_id, lease.batch_job_id),
    ).fetchall()
    amount = _money_text(reserved)
    for hold in holds:
        existing = connection.execute(
            """
            SELECT * FROM batch_budget_allocations
            WHERE tenant_id=? AND batch_attempt_id=? AND budget_rule_id=? AND budget_date=?
            """,
            (
                lease.tenant_id,
                lease.batch_attempt_id,
                hold["budget_rule_id"],
                hold["budget_date"],
            ),
        ).fetchone()
        if existing is not None:
            if (
                existing["batch_job_id"] != lease.batch_job_id
                or existing["batch_task_id"] != lease.batch_task_id
                or existing["batch_budget_hold_id"] != hold["batch_budget_hold_id"]
                or existing["reserved_cost_cny"] != amount
                or existing["route_plan_hash"] != route_hash
                or existing["execution_outcome"] != "not_invoked"
                or existing["charge_state"] != "none"
                or existing["status"] != "active"
            ):
                raise ApiError("batch.worker_unavailable")
            continue
        remaining = _money(hold["remaining_cost_cny"])
        if remaining < reserved:
            raise ApiError("batch.budget_exceeded")
        new_remaining = _money_text(remaining - reserved)
        updated = connection.execute(
            """
            UPDATE batch_budget_holds
            SET remaining_cost_cny=?,updated_at=?
            WHERE tenant_id=? AND batch_budget_hold_id=? AND status='active'
              AND remaining_cost_cny=?
            """,
            (
                new_remaining,
                now,
                lease.tenant_id,
                hold["batch_budget_hold_id"],
                hold["remaining_cost_cny"],
            ),
        )
        if updated.rowcount != 1:
            raise ApiError("batch.worker_unavailable")
        connection.execute(
            """
            INSERT INTO batch_budget_allocations(
              batch_budget_allocation_id,tenant_id,batch_job_id,batch_task_id,
              batch_attempt_id,batch_budget_hold_id,budget_rule_id,budget_date,
              reserved_cost_cny,route_plan_hash,execution_outcome,charge_state,status,
              created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,'not_invoked','none','active',?,?)
            """,
            (
                f"bbal_{uuid4().hex}",
                lease.tenant_id,
                lease.batch_job_id,
                lease.batch_task_id,
                lease.batch_attempt_id,
                hold["batch_budget_hold_id"],
                hold["budget_rule_id"],
                hold["budget_date"],
                amount,
                route_hash,
                now,
                now,
            ),
        )


def _load_current_lease(
    connection: sqlite3.Connection,
    lease: BatchTaskLease,
    *,
    current: datetime | None,
) -> sqlite3.Row:
    clauses = [
        "task.tenant_id=?",
        "task.batch_job_id=?",
        "task.batch_task_id=?",
        "task.current_attempt_id=?",
        "task.lease_token=?",
        "task.lease_owner_id=?",
        "task.lease_generation=?",
        "task.status IN ('leased','calling')",
    ]
    params: list[object] = [
        lease.tenant_id,
        lease.batch_job_id,
        lease.batch_task_id,
        lease.batch_attempt_id,
        lease.lease_token,
        lease.lease_owner_id,
        lease.lease_generation,
    ]
    if current is not None:
        clauses.extend(
            [
                "job.cancel_requested_at IS NULL",
                "unixepoch(task.lease_expires_at)>unixepoch(?)",
                "unixepoch(job.deadline_at)>unixepoch(?)",
            ]
        )
        current_text = _db_time(current)
        params.extend([current_text, current_text])
    row = connection.execute(
        f"""
        SELECT task.*,job.cancel_requested_at AS job_cancel_requested_at,job.deadline_at
        FROM batch_tasks task
        JOIN batch_jobs job
          ON job.tenant_id=task.tenant_id AND job.batch_job_id=task.batch_job_id
        WHERE {' AND '.join(clauses)}
        """,
        params,
    ).fetchone()
    if row is None:
        raise ApiError("batch.execution_invalid")
    return row


def _successful_attempt_usage_posted(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    batch_attempt_id: str,
    cost_cny: object,
) -> bool:
    if cost_cny is None:
        return False
    pending = connection.execute(
        """
        SELECT 1 FROM batch_budget_allocations
        WHERE tenant_id=? AND batch_attempt_id=?
          AND status!='released'
          AND NOT (
            execution_outcome='succeeded' AND charge_state='posted' AND status='posted'
          )
        LIMIT 1
        """,
        (tenant_id, batch_attempt_id),
    ).fetchone()
    return pending is None


def _finish_leased_task(
    connection: sqlite3.Connection,
    lease: BatchTaskLease,
    *,
    status: str,
    failure_code: str | None,
    request_id: str,
    trace_id: str,
    result_available: bool,
    now: str,
) -> None:
    if status not in _TERMINAL_TASKS:
        raise ApiError("batch.worker_unavailable")
    updated = connection.execute(
        """
        UPDATE batch_tasks
        SET status=?,lease_token=NULL,lease_owner_id=NULL,lease_expires_at=NULL,
            terminal_request_id=?,terminal_trace_id=?,failure_code=?,result_available=?,
            completed_at=?,updated_at=?
        WHERE tenant_id=? AND batch_job_id=? AND batch_task_id=?
          AND current_attempt_id=? AND lease_token=? AND lease_owner_id=?
          AND lease_generation=? AND status IN ('leased','calling')
        """,
        (
            status,
            request_id,
            trace_id,
            failure_code,
            int(result_available),
            now,
            now,
            lease.tenant_id,
            lease.batch_job_id,
            lease.batch_task_id,
            lease.batch_attempt_id,
            lease.lease_token,
            lease.lease_owner_id,
            lease.lease_generation,
        ),
    )
    if updated.rowcount != 1:
        raise ApiError("batch.execution_invalid")


def _terminalize_lease_error(
    config: AppConfig,
    lease: BatchTaskLease,
    *,
    code: str,
    now: datetime,
) -> None:
    connection = _connect(config)
    now_text = _db_time(now)
    try:
        connection.execute("BEGIN IMMEDIATE")
        task = connection.execute(
            """
            SELECT * FROM batch_tasks WHERE tenant_id=? AND batch_job_id=? AND batch_task_id=?
              AND current_attempt_id=? AND lease_token=? AND lease_owner_id=?
              AND lease_generation=? AND status='leased'
            """,
            (
                lease.tenant_id,
                lease.batch_job_id,
                lease.batch_task_id,
                lease.batch_attempt_id,
                lease.lease_token,
                lease.lease_owner_id,
                lease.lease_generation,
            ),
        ).fetchone()
        if task is None:
            connection.rollback()
            return
        connection.execute(
            """
            UPDATE batch_budget_allocations
            SET execution_outcome='not_invoked',charge_state='none',status='released',updated_at=?
            WHERE tenant_id=? AND batch_attempt_id=? AND status='active'
              AND execution_outcome='not_invoked' AND charge_state='none'
            """,
            (now_text, lease.tenant_id, lease.batch_attempt_id),
        )
        status = "cancelled" if task["cancel_requested_at"] is not None else "failed"
        _finish_leased_task(
            connection,
            lease,
            status=status,
            failure_code=None if status == "cancelled" else code,
            request_id=f"batch-worker-{lease.batch_task_id}",
            trace_id=f"batch-worker-{lease.batch_task_id}",
            result_available=False,
            now=now_text,
        )
        finalize_batch_job_if_terminal(
            connection,
            tenant_id=lease.tenant_id,
            batch_job_id=lease.batch_job_id,
            request_id=f"batch-worker-{lease.batch_task_id}",
            trace_id=f"batch-worker-{lease.batch_task_id}",
            now=now_text,
        )
        connection.commit()
    except (ApiError, sqlite3.Error):
        connection.rollback()
    finally:
        connection.close()


def _fail_unclaimed_task(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    code: str,
    now: str,
) -> None:
    connection.execute(
        """
        UPDATE batch_tasks
        SET status='failed',failure_code=?,terminal_request_id=?,terminal_trace_id=?,
            completed_at=?,updated_at=?
        WHERE tenant_id=? AND batch_task_id=? AND status IN ('queued','retry_wait')
        """,
        (
            code,
            f"batch-worker-{row['batch_task_id']}",
            f"batch-worker-{row['batch_task_id']}",
            now,
            now,
            row["tenant_id"],
            row["batch_task_id"],
        ),
    )


def _expire_unclaimed_work(connection: sqlite3.Connection, *, now: str) -> None:
    connection.execute(
        """
        UPDATE batch_tasks
        SET status=CASE WHEN cancel_requested_at IS NOT NULL THEN 'cancelled' ELSE 'expired' END,
            failure_code=NULL,terminal_request_id='batch-reaper',terminal_trace_id='batch-reaper',
            completed_at=?,updated_at=?
        WHERE status IN ('queued','retry_wait') AND EXISTS(
          SELECT 1 FROM batch_jobs job
          WHERE job.tenant_id=batch_tasks.tenant_id AND job.batch_job_id=batch_tasks.batch_job_id
            AND (unixepoch(job.deadline_at)<=unixepoch(?) OR job.cancel_requested_at IS NOT NULL)
        )
        """,
        (now, now, now),
    )


def _restore_task_without_attempt(
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    *,
    now: str,
) -> None:
    cancelled_at = task["cancel_requested_at"] or task["job_cancel_requested_at"]
    expired = _time(task["deadline_at"]) <= _time(now)
    if cancelled_at is not None or expired:
        connection.execute(
            """
            UPDATE batch_tasks
            SET status=?,cancel_requested_at=COALESCE(cancel_requested_at,?),
                lease_token=NULL,lease_owner_id=NULL,lease_expires_at=NULL,
                current_attempt_id=NULL,terminal_request_id='batch-reaper',
                terminal_trace_id='batch-reaper',failure_code=NULL,result_available=0,
                completed_at=?,updated_at=?
            WHERE tenant_id=? AND batch_task_id=? AND current_attempt_id=?
              AND lease_token=? AND lease_owner_id=? AND lease_generation=? AND lease_expires_at=?
            """,
            (
                "cancelled" if cancelled_at is not None else "expired",
                cancelled_at,
                now,
                now,
                task["tenant_id"],
                task["batch_task_id"],
                task["current_attempt_id"],
                task["lease_token"],
                task["lease_owner_id"],
                task["lease_generation"],
                task["lease_expires_at"],
            ),
        )
        return
    connection.execute(
        """
        UPDATE batch_tasks
        SET status='queued',lease_token=NULL,lease_owner_id=NULL,lease_expires_at=NULL,
            current_attempt_id=NULL,next_attempt_at=?,updated_at=?
        WHERE tenant_id=? AND batch_task_id=? AND current_attempt_id=?
          AND lease_token=? AND lease_owner_id=? AND lease_generation=? AND lease_expires_at=?
        """,
        (
            now,
            now,
            task["tenant_id"],
            task["batch_task_id"],
            task["current_attempt_id"],
            task["lease_token"],
            task["lease_owner_id"],
            task["lease_generation"],
            task["lease_expires_at"],
        ),
    )


def _release_lease_for_same_attempt(
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    *,
    now: str,
) -> None:
    connection.execute(
        """
        UPDATE batch_tasks
        SET status='retry_wait',lease_token=NULL,lease_owner_id=NULL,lease_expires_at=NULL,
            next_attempt_at=?,updated_at=?
        WHERE tenant_id=? AND batch_task_id=? AND current_attempt_id=?
          AND lease_token=? AND lease_owner_id=? AND lease_generation=? AND lease_expires_at=?
        """,
        (
            now,
            now,
            task["tenant_id"],
            task["batch_task_id"],
            task["current_attempt_id"],
            task["lease_token"],
            task["lease_owner_id"],
            task["lease_generation"],
            task["lease_expires_at"],
        ),
    )


def _retry_or_finish_reaped(
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    attempt: sqlite3.Row,
    *,
    now: datetime,
) -> None:
    if int(task["attempt_count"]) < int(task["max_attempts"]):
        index = min(max(int(task["attempt_count"]) - 1, 0), len(_RETRY_DELAYS) - 1)
        connection.execute(
            """
            UPDATE batch_tasks
            SET status='retry_wait',lease_token=NULL,lease_owner_id=NULL,lease_expires_at=NULL,
                current_attempt_id=NULL,next_attempt_at=?,failure_code=?,updated_at=?
            WHERE tenant_id=? AND batch_task_id=? AND current_attempt_id=?
              AND lease_token=? AND lease_owner_id=? AND lease_generation=? AND lease_expires_at=?
            """,
            (
                _db_time(now + timedelta(seconds=_RETRY_DELAYS[index])),
                _safe_gateway_failure(attempt["gateway_error_code"]),
                _db_time(now),
                task["tenant_id"],
                task["batch_task_id"],
                task["current_attempt_id"],
                task["lease_token"],
                task["lease_owner_id"],
                task["lease_generation"],
                task["lease_expires_at"],
            ),
        )
    else:
        _finish_reaped_task(
            connection,
            task,
            attempt,
            status="failed",
            code=_safe_gateway_failure(attempt["gateway_error_code"]),
            now=_db_time(now),
        )


def _finish_reaped_task(
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    attempt: sqlite3.Row,
    *,
    status: str,
    code: str,
    now: str,
) -> None:
    connection.execute(
        """
        UPDATE batch_tasks
        SET status=?,lease_token=NULL,lease_owner_id=NULL,lease_expires_at=NULL,
            terminal_request_id=?,terminal_trace_id=?,failure_code=?,result_available=0,
            completed_at=?,updated_at=?
        WHERE tenant_id=? AND batch_task_id=? AND current_attempt_id=?
          AND lease_token=? AND lease_owner_id=? AND lease_generation=? AND lease_expires_at=?
        """,
        (
            status,
            attempt["request_id"],
            attempt["trace_id"],
            code,
            now,
            now,
            task["tenant_id"],
            task["batch_task_id"],
            task["current_attempt_id"],
            task["lease_token"],
            task["lease_owner_id"],
            task["lease_generation"],
            task["lease_expires_at"],
        ),
    )


def _mark_attempt_unknown(
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    attempt: sqlite3.Row,
    *,
    now: str,
) -> None:
    connection.execute(
        """
        UPDATE batch_attempts
        SET status='outcome_unknown',execution_outcome='unknown',retry_safe=0,
            gateway_error_code='batch.provider_outcome_unknown',updated_at=?
        WHERE tenant_id=? AND batch_attempt_id=? AND status IN ('calling','prepared')
        """,
        (now, task["tenant_id"], attempt["batch_attempt_id"]),
    )
    connection.execute(
        """
        UPDATE batch_budget_allocations
        SET status='reconciliation',charge_state='possible',execution_outcome='unknown',updated_at=?
        WHERE tenant_id=? AND batch_attempt_id=? AND status='active'
        """,
        (now, task["tenant_id"], attempt["batch_attempt_id"]),
    )
    _finish_reaped_task(
        connection,
        task,
        attempt,
        status="outcome_unknown",
        code="batch.provider_outcome_unknown",
        now=now,
    )


def _finalize_all_terminal_jobs(connection: sqlite3.Connection, *, now: str) -> None:
    jobs = connection.execute(
        """
        SELECT tenant_id,batch_job_id FROM batch_jobs job
        WHERE status IN ('queued','running') AND NOT EXISTS(
          SELECT 1 FROM batch_tasks task
          WHERE task.tenant_id=job.tenant_id AND task.batch_job_id=job.batch_job_id
            AND task.status IN ('queued','leased','calling','retry_wait')
        )
        ORDER BY tenant_id,batch_job_id
        """
    ).fetchall()
    for job in jobs:
        finalize_batch_job_if_terminal(
            connection,
            tenant_id=str(job["tenant_id"]),
            batch_job_id=str(job["batch_job_id"]),
            request_id="batch-reaper",
            trace_id="batch-reaper",
            now=now,
        )


def _lease_from_rows(task: sqlite3.Row, attempt: sqlite3.Row) -> BatchTaskLease:
    return BatchTaskLease(
        tenant_id=str(task["tenant_id"]),
        batch_job_id=str(task["batch_job_id"]),
        batch_task_id=str(task["batch_task_id"]),
        batch_attempt_id=str(attempt["batch_attempt_id"]),
        file_id=str(task["file_id"]),
        submitted_by_user_id=str(task["submitted_by_user_id"]),
        model_alias_id=str(task["model_alias_id"]),
        virtual_key_id=str(task["virtual_key_id"]),
        route_plan_json=str(attempt["route_plan_json"]),
        operation_spec_ciphertext=str(task["operation_spec_ciphertext"]),
        max_attempts=int(task["max_attempts"]),
        attempt_no=int(attempt["attempt_no"]),
        lease_token=str(task["lease_token"]),
        lease_owner_id=str(task["lease_owner_id"]),
        lease_generation=int(task["lease_generation"]),
        lease_expires_at=_time(task["lease_expires_at"]),
        deadline_at=_time(task["deadline_at"]),
        existing_attempt=True,
    )


def _gateway_error_code(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except (ValueError, TypeError):
        return None
    if type(payload) is not dict:
        return None
    code = payload.get("code")
    if type(code) is not str and type(payload.get("error")) is dict:
        code = payload["error"].get("code")
    if type(code) is not str or len(code) > 96 or _BATCH_ERROR_CODE.fullmatch(code) is None:
        return None
    return code


def _safe_gateway_failure(value: object) -> str:
    if (
        type(value) is str
        and len(value) <= 96
        and _BATCH_ERROR_CODE.fullmatch(value) is not None
    ):
        return value
    return "batch.worker_unavailable"


def _batch_execution_code(value: str) -> str:
    allowed = {
        "batch.authorization_revoked",
        "batch.file_not_ready",
        "batch.file_security_blocked",
        "batch.budget_exceeded",
        "batch.execution_invalid",
    }
    return value if value in allowed else "batch.worker_unavailable"


def _heartbeat_loop(config: AppConfig, lease: BatchTaskLease, stop: threading.Event) -> None:
    while not stop.wait(float(config.batch_heartbeat_seconds)):
        if not heartbeat_batch_task(config, lease):
            return


def _worker_id(value: object) -> str:
    if type(value) is not str or _WORKER_ID.fullmatch(value) is None:
        raise ApiError("batch.worker_unavailable")
    return value


def _busy_backoff_seconds(config: AppConfig, consecutive_busy: int) -> float:
    milliseconds = config.batch_realtime_cooldown_milliseconds
    if type(milliseconds) is not int or milliseconds < 0 or consecutive_busy < 1:
        raise ApiError("batch.worker_unavailable")
    base = max(milliseconds / 1000.0, _BUSY_BACKOFF_MIN_SECONDS)
    exponent = min(consecutive_busy - 1, 16)
    return min(base * (2**exponent), _BUSY_BACKOFF_MAX_SECONDS)


def _batch_secret(config: AppConfig) -> str:
    value = config.batch_encryption_secret
    if type(value) is not str or not value:
        raise ApiError("batch.worker_unavailable")
    return value


def _money(value: object) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError
    text = str(value)
    if not text or text != text.strip() or "e" in text.lower():
        raise ValueError
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValueError from None
    if not amount.is_finite() or amount < 0 or amount != amount.quantize(_MONEY_QUANT):
        raise ValueError
    return amount


def _money_text(value: Decimal) -> str:
    return format(_money(value), ".6f")


def _optional_time(value: object) -> datetime | None:
    return None if value is None else _time(value)


def _time(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError
    return parsed.astimezone(UTC)


def _now(value: datetime | None) -> datetime:
    current = value or _utcnow()
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise ApiError("batch.worker_unavailable")
    return current.astimezone(UTC).replace(microsecond=0)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("batch.worker_unavailable") from None


__all__ = [
    "BatchTaskLease",
    "GatewayCallFact",
    "PreparedBatchAttempt",
    "call_batch_gateway",
    "claim_next_batch_task",
    "heartbeat_batch_task",
    "prepare_batch_attempt",
    "reap_expired_batch_tasks",
    "refresh_batch_grant",
    "run_batch_task_once",
    "settle_batch_attempt",
]
