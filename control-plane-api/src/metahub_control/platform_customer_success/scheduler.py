"""Single persistently fenced supervisor slot for the six PRD-140 jobs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import sqlite3
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import FastAPI
from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .outbox import claim_next_event, complete_event, fail_event
from .projection import (
    emit_due_task_events,
    generate_qbr,
    materialize_health_v2,
    reconcile_accounts,
    reconcile_renewals,
)

logger = logging.getLogger("metahub.control.platform_customer_success.scheduler")
WORKER_ID = f"customer-success-{uuid4().hex}"
POLL_SECONDS = 1.0
LEASE_SECONDS = 30
_INTERVAL = {
    "account_projection": 60,
    "health_evaluation": 60,
    "renewal_projection": 60,
    "task_due": 15,
    "qbr_snapshot": 300,
    "event_outbox": 2,
}


def install_platform_customer_success_scheduler(app: FastAPI) -> None:
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        async with original_lifespan(current_app):
            stop = threading.Event()
            wake = asyncio.Event()
            task = asyncio.create_task(
                _scheduler_loop(current_app.state.config, stop, wake),
                name="platform-customer-success-scheduler",
            )
            try:
                yield
            finally:
                stop.set()
                wake.set()
                await asyncio.gather(task, return_exceptions=True)

    app.router.lifespan_context = combined_lifespan


async def _scheduler_loop(config: AppConfig, stop: threading.Event, wake: asyncio.Event) -> None:
    while not stop.is_set():
        worked = await asyncio.to_thread(_safe_iteration, config, WORKER_ID, stop.is_set)
        try:
            await asyncio.wait_for(wake.wait(), timeout=0.05 if worked else POLL_SECONDS)
        except TimeoutError:
            continue


def _safe_iteration(config: AppConfig, worker_id: str, should_stop) -> bool:
    try:
        return run_platform_customer_success_scheduler_once(
            config, worker_id=worker_id, should_stop=should_stop
        )
    except ApiError as exc:
        _warning(exc.code)
    except Exception:
        _warning("platform.customer_success_scheduler_unavailable")
    return False


def run_platform_customer_success_scheduler_once(
    config: AppConfig,
    *,
    worker_id: str,
    should_stop=lambda: False,
    now: datetime | None = None,
) -> bool:
    if not 8 <= len(worker_id) <= 128:
        raise ApiError("platform.unavailable")
    current = _utc(now)
    connection = _connect(config)
    job_code = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_schema(connection)
        reconcile_accounts(connection, now=current)
        stamp = _db_time(current)
        row = connection.execute(
            """SELECT * FROM platform_customer_success_scheduler_state
                WHERE next_run_at<=? AND(
                  status='pending' OR(status='leased' AND lease_expires_at<=?))
                ORDER BY next_run_at,job_code,customer_success_scheduler_id LIMIT 1""",
            (stamp, stamp),
        ).fetchone()
        if row is None or should_stop():
            connection.commit()
            return False
        job_code = str(row["job_code"])
        token = secrets.token_urlsafe(48)
        generation = int(row["fencing_generation"]) + 1
        changed = connection.execute(
            """UPDATE platform_customer_success_scheduler_state
                  SET status='leased',fencing_generation=?,lease_owner_id=?,
                      lease_token_sha256=?,lease_expires_at=?,revision=revision+1,updated_at=?
                WHERE customer_success_scheduler_id=? AND revision=?
                  AND (status='pending' OR lease_expires_at<=?)""",
            (
                generation,
                worker_id,
                _sha(token),
                _db_time(current + timedelta(seconds=LEASE_SECONDS)),
                stamp,
                row["customer_success_scheduler_id"],
                int(row["revision"]),
                stamp,
            ),
        ).rowcount
        if changed != 1:
            connection.rollback()
            return False
        connection.execute("SAVEPOINT platform_customer_success_job")
        try:
            result = _process(connection, row, current, should_stop)
            connection.execute("RELEASE platform_customer_success_job")
        except (ApiError, sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
            connection.execute("ROLLBACK TO platform_customer_success_job")
            connection.execute("RELEASE platform_customer_success_job")
            _release_failed_job(
                connection,
                row,
                worker_id=worker_id,
                token=token,
                generation=generation,
                now=current,
            )
            connection.commit()
            raise ApiError("platform.unavailable") from None
        if should_stop():
            connection.rollback()
            return False
        refreshed = connection.execute(
            """SELECT revision FROM platform_customer_success_scheduler_state
                WHERE customer_success_scheduler_id=? AND status='leased'
                  AND lease_owner_id=? AND lease_token_sha256=? AND fencing_generation=?""",
            (row["customer_success_scheduler_id"], worker_id, _sha(token), generation),
        ).fetchone()
        if refreshed is None:
            connection.rollback()
            return False
        watermark = _sequence_watermark(connection, row["customer_account_id"], job_code)
        connection.execute(
            """UPDATE platform_customer_success_scheduler_state
                  SET status='pending',next_run_at=?,scan_sequence_watermark=?,
                      scan_time_watermark=?,consecutive_failure_count=0,
                      lease_owner_id=NULL,lease_token_sha256=NULL,lease_expires_at=NULL,
                      last_error_code=NULL,revision=revision+1,updated_at=?
                WHERE customer_success_scheduler_id=? AND revision=?
                  AND lease_owner_id=? AND lease_token_sha256=? AND fencing_generation=?""",
            (
                _db_time(current + timedelta(seconds=_INTERVAL[job_code])),
                max(int(row["scan_sequence_watermark"]), watermark),
                max(str(row["scan_time_watermark"]), stamp),
                stamp,
                row["customer_success_scheduler_id"],
                int(refreshed["revision"]),
                worker_id,
                _sha(token),
                generation,
            ),
        )
        connection.commit()
        if job_code == "event_outbox":
            _deliver_one(config, worker_id, current)
        return bool(result or job_code)
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def _release_failed_job(connection, row, *, worker_id, token, generation, now) -> None:
    failure_count = int(row["consecutive_failure_count"]) + 1
    delay = min(300, 2 ** min(failure_count, 8))
    changed = connection.execute(
        """UPDATE platform_customer_success_scheduler_state
              SET status='pending',next_run_at=?,consecutive_failure_count=?,
                  lease_owner_id=NULL,lease_token_sha256=NULL,lease_expires_at=NULL,
                  last_error_code='platform.customer_success_scheduler_unavailable',
                  revision=revision+1,updated_at=?
            WHERE customer_success_scheduler_id=? AND revision=?
              AND lease_owner_id=? AND lease_token_sha256=? AND fencing_generation=?""",
        (
            _db_time(now + timedelta(seconds=delay)),
            failure_count,
            _db_time(now),
            row["customer_success_scheduler_id"],
            int(row["revision"]) + 1,
            worker_id,
            _sha(token),
            generation,
        ),
    ).rowcount
    if changed != 1:
        raise ApiError("platform.unavailable")


def _process(connection, row, now, should_stop):
    if should_stop():
        return 0
    code = row["job_code"]
    if code == "account_projection":
        return reconcile_accounts(connection, now=now)
    if code == "health_evaluation":
        return int(
            materialize_health_v2(connection, row["customer_account_id"], now=now) is not None
        )
    if code == "renewal_projection":
        return reconcile_renewals(connection, now=now)
    if code == "task_due":
        return emit_due_task_events(connection, now=now)
    if code == "qbr_snapshot":
        return int(generate_qbr(connection, row["customer_account_id"], now=now) is not None)
    if code == "event_outbox":
        return 1
    raise ApiError("platform.unavailable")


def _deliver_one(config: AppConfig, worker_id: str, now: datetime) -> bool:
    lease = claim_next_event(config, worker_id=worker_id + "-outbox", now=now)
    if lease is None:
        return False
    try:
        payload = json.loads(lease.payload_json)
        if type(payload) is not dict or set(payload) != {
            "customer_account_id",
            "event_id",
            "object_id",
            "occurred_at",
            "reason_code",
            "revision",
            "safe_count",
            "schema_version",
            "source_digest",
            "tenant_id",
            "workspace_id",
        }:
            fail_event(config, lease, error_code="event.payload_invalid", now=now)
            return False
        return complete_event(config, lease, now=now)
    except (ValueError, TypeError, json.JSONDecodeError):
        fail_event(config, lease, error_code="event.payload_invalid", now=now)
        return False


def _sequence_watermark(connection, account_id, code):
    if code == "health_evaluation":
        row = connection.execute(
            """SELECT source_sequence FROM platform_customer_health_heads
                WHERE customer_account_id=?""",
            (account_id,),
        ).fetchone()
        return 0 if row is None else int(row[0])
    if code == "event_outbox":
        row = connection.execute(
            """SELECT max(recovery_watermark) FROM platform_customer_success_event_outbox
                WHERE customer_account_id=?""",
            (account_id,),
        ).fetchone()
        return 0 if row[0] is None else int(row[0])
    return 0


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("platform.unavailable") from None


def _require_schema(connection: sqlite3.Connection) -> None:
    if (
        connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version='0132' AND status='applied'"
        ).fetchone()
        is None
    ):
        raise ApiError("platform.unavailable")


def _utc(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        raise ApiError("platform.unavailable")
    return current.astimezone(UTC)


def _db_time(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _rollback(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        connection.rollback()


def _warning(error_code: str) -> None:
    logger.warning(
        "customer success scheduler iteration failed",
        extra={"event": "platform.customer_success_scheduler_failed", "error_code": error_code},
    )


__all__ = [
    "install_platform_customer_success_scheduler",
    "run_platform_customer_success_scheduler_once",
]
