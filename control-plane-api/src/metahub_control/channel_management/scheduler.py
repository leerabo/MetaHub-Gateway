"""Persistently fenced PRD-139 statement and event worker."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import sqlite3
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import FastAPI
from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

logger = logging.getLogger("metahub.control.channel_management.scheduler")
WORKER_ID = f"channel-{uuid4().hex}"
POLL_SECONDS = 2.0
LEASE_SECONDS = 15


@dataclass(frozen=True, slots=True, repr=False)
class ChannelLease:
    worker_id: str
    token: str
    generation: int
    expires_at: datetime


def install_channel_scheduler(app: FastAPI) -> None:
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        async with original_lifespan(current_app):
            stop = threading.Event()
            wake = asyncio.Event()
            task = asyncio.create_task(
                _scheduler_loop(current_app.state.config, stop, wake),
                name="channel-management-scheduler",
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
        worked = await asyncio.to_thread(
            _safe_iteration,
            config,
            WORKER_ID,
            stop.is_set,
        )
        try:
            await asyncio.wait_for(wake.wait(), timeout=0.05 if worked else POLL_SECONDS)
        except TimeoutError:
            continue


def _safe_iteration(config: AppConfig, worker_id: str, should_stop) -> bool:
    try:
        return run_channel_scheduler_once(
            config,
            worker_id=worker_id,
            should_stop=should_stop,
        )
    except ApiError as exc:
        _warning(exc.code)
    except Exception:
        _warning("channel.scheduler_unavailable")
    return False


def run_channel_scheduler_once(
    config: AppConfig,
    *,
    worker_id: str,
    should_stop=lambda: False,
) -> bool:
    lease = acquire_channel_lease(config, worker_id=worker_id)
    if lease is None:
        return False
    try:
        if should_stop():
            return False
        return _process_fenced_batch(config, lease, should_stop)
    finally:
        _release_channel_lease(config, lease)


def acquire_channel_lease(config: AppConfig, *, worker_id: str) -> ChannelLease | None:
    if not 8 <= len(worker_id) <= 128:
        raise ApiError("channel.scheduler_unavailable")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_schema(connection)
        row = connection.execute(
            "SELECT * FROM channel_scheduler_state WHERE singleton_id=1"
        ).fetchone()
        now = _utcnow()
        if row is None:
            raise ApiError("channel.scheduler_unavailable")
        if row["lease_owner_id"] is not None and _parse_time(row["lease_expires_at"]) > now:
            connection.rollback()
            return None
        token = secrets.token_urlsafe(48)
        expires = now + timedelta(seconds=LEASE_SECONDS)
        generation = int(row["fencing_generation"]) + 1
        changed = connection.execute(
            """UPDATE channel_scheduler_state
                  SET lease_owner_id=?,lease_token_sha256=?,lease_expires_at=?,
                      fencing_generation=?,revision=revision+1,updated_at=?
                WHERE singleton_id=1 AND revision=?
                  AND (lease_owner_id IS NULL OR lease_expires_at<=?)""",
            (
                worker_id,
                _sha(token),
                _db_time(expires),
                generation,
                _db_time(now),
                int(row["revision"]),
                _db_time(now),
            ),
        ).rowcount
        if changed != 1:
            connection.rollback()
            return None
        connection.commit()
        return ChannelLease(worker_id, token, generation, expires)
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("channel.scheduler_unavailable") from None
    finally:
        connection.close()


_WORKER_QUEUES = {"statements": "statements", "outbox": "main"}


def _process_fenced_batch(config: AppConfig, lease: ChannelLease, should_stop) -> bool:
    from metahub_control.console_migration.execution import selected_scheduler_work

    selected = selected_scheduler_work("channel-attribution", _WORKER_QUEUES)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        state = _assert_fence(connection, lease)
        limit = int(state["bounded_batch_size"])
        closed = _close_due_statements(connection, lease, limit, should_stop) if "statements" in selected else 0
        delivered = _deliver_outbox(connection, lease, limit, should_stop) if "outbox" in selected else 0
        now = _utcnow()
        if now >= lease.expires_at:
            raise ApiError("channel.scheduler_fenced")
        updates: list[str] = []
        values: list[object] = []
        if closed:
            updates.append("statement_watermark_at=?")
            values.append(_db_time(now))
        if delivered:
            updates.append("outbox_watermark_at=?")
            values.append(_db_time(now))
        if updates:
            values.extend((_db_time(now), int(state["revision"]), lease.generation))
            changed = connection.execute(
                f"""UPDATE channel_scheduler_state SET {','.join(updates)},
                         revision=revision+1,updated_at=?
                       WHERE singleton_id=1 AND revision=? AND fencing_generation=?""",
                values,
            ).rowcount
            if changed != 1:
                raise ApiError("channel.scheduler_fenced")
        connection.commit()
        return bool(closed or delivered)
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("channel.scheduler_unavailable") from None
    finally:
        connection.close()


def _close_due_statements(
    connection: sqlite3.Connection,
    lease: ChannelLease,
    limit: int,
    should_stop,
) -> int:
    rows = connection.execute(
        """SELECT * FROM channel_statements
             WHERE status='open' AND period_end<=?
             ORDER BY period_end,channel_statement_id LIMIT ?""",
        (_db_time(_utcnow()), limit),
    ).fetchall()
    closed = 0
    for statement in rows:
        if should_stop():
            break
        lines = connection.execute(
            """SELECT source_kind,source_reference_id,amount_micros,
                      commission_amount_micros,token_count,estimated
                 FROM channel_statement_lines WHERE channel_statement_id=?
                ORDER BY source_kind,source_reference_id""",
            (statement["channel_statement_id"],),
        ).fetchall()
        amount = sum(int(line["amount_micros"]) for line in lines)
        commission = sum(int(line["commission_amount_micros"]) for line in lines)
        canonical = [tuple(line) for line in lines]
        now = _db_time(_utcnow())
        revision = int(statement["close_revision"]) + 1
        changed = connection.execute(
            """UPDATE channel_statements
                  SET total_amount_micros=?,total_commission_micros=?,line_count=?,
                      reconciliation_sha256=?,status='closed',close_revision=?,
                      closed_at=?,updated_at=?
                WHERE channel_statement_id=? AND status='open' AND close_revision=?""",
            (
                amount,
                commission,
                len(lines),
                _digest(canonical),
                revision,
                now,
                now,
                statement["channel_statement_id"],
                int(statement["close_revision"]),
            ),
        ).rowcount
        if changed != 1:
            raise ApiError("channel.scheduler_fenced")
        _emit_statement_event(connection, statement, revision, lease.generation, now)
        closed += 1
    return closed


def _emit_statement_event(
    connection: sqlite3.Connection,
    statement: sqlite3.Row,
    revision: int,
    generation: int,
    now: str,
) -> None:
    event_id = f"cev_{uuid4().hex}"
    connection.execute(
        """INSERT INTO channel_events(
             channel_event_id,channel_partner_id,owner_workspace_id,tenant_id,event_type,
             object_id,object_revision,payload_sha256,occurred_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            event_id,
            statement["channel_partner_id"],
            statement["owner_workspace_id"],
            statement["tenant_id"],
            "channel.statement_closed",
            statement["channel_statement_id"],
            revision,
            _digest(
                {
                    "channel_statement_id": statement["channel_statement_id"],
                    "revision": revision,
                    "status": "closed",
                }
            ),
            now,
        ),
    )
    connection.execute(
        """INSERT INTO channel_event_outbox(
             channel_event_outbox_id,channel_event_id,channel_partner_id,owner_workspace_id,
             tenant_id,status,attempt_count,fencing_generation,next_attempt_at,delivered_at,
             last_error_code,created_at,updated_at)
           VALUES(?,?,?,?,?,'pending',0,?,?,NULL,NULL,?,?)""",
        (
            f"ceo_{uuid4().hex}",
            event_id,
            statement["channel_partner_id"],
            statement["owner_workspace_id"],
            statement["tenant_id"],
            generation,
            now,
            now,
            now,
        ),
    )


def _deliver_outbox(
    connection: sqlite3.Connection,
    lease: ChannelLease,
    limit: int,
    should_stop,
) -> int:
    rows = connection.execute(
        """SELECT channel_event_outbox_id FROM channel_event_outbox
             WHERE status IN('pending','failed')
               AND (next_attempt_at IS NULL OR next_attempt_at<=?)
             ORDER BY created_at,channel_event_outbox_id LIMIT ?""",
        (_db_time(_utcnow()), limit),
    ).fetchall()
    delivered = 0
    for row in rows:
        if should_stop():
            break
        now = _db_time(_utcnow())
        changed = connection.execute(
            """UPDATE channel_event_outbox
                  SET status='delivered',attempt_count=attempt_count+1,
                      fencing_generation=?,next_attempt_at=NULL,delivered_at=?,
                      last_error_code=NULL,updated_at=?
                WHERE channel_event_outbox_id=? AND status IN('pending','failed')
                  AND attempt_count<10""",
            (lease.generation, now, now, row["channel_event_outbox_id"]),
        ).rowcount
        delivered += changed
    return delivered


def _assert_fence(connection: sqlite3.Connection, lease: ChannelLease) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM channel_scheduler_state WHERE singleton_id=1"
    ).fetchone()
    now = _utcnow()
    if (
        row is None
        or row["lease_owner_id"] != lease.worker_id
        or int(row["fencing_generation"]) != lease.generation
        or not secrets.compare_digest(str(row["lease_token_sha256"]), _sha(lease.token))
        or _parse_time(row["lease_expires_at"]) <= now
    ):
        raise ApiError("channel.scheduler_fenced")
    return row


def _release_channel_lease(config: AppConfig, lease: ChannelLease) -> None:
    try:
        connection = _connect(config)
    except ApiError:
        return
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT revision FROM channel_scheduler_state WHERE singleton_id=1"
        ).fetchone()
        if row is None:
            connection.rollback()
            return
        now = _db_time(_utcnow())
        connection.execute(
            """UPDATE channel_scheduler_state
                  SET lease_owner_id=NULL,lease_token_sha256=NULL,lease_expires_at=NULL,
                      revision=revision+1,updated_at=?
                WHERE singleton_id=1 AND revision=? AND lease_owner_id=?
                  AND lease_token_sha256=? AND fencing_generation=?""",
            (
                now,
                int(row["revision"]),
                lease.worker_id,
                _sha(lease.token),
                lease.generation,
            ),
        )
        connection.commit()
    except (sqlite3.Error, DbError, ValueError, TypeError):
        _rollback(connection)
    finally:
        connection.close()


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("channel.scheduler_unavailable") from None


def _require_schema(connection: sqlite3.Connection) -> None:
    if connection.execute(
        "SELECT 1 FROM schema_migrations WHERE version='0131' AND status='applied'"
    ).fetchone() is None:
        raise ApiError("channel.scheduler_unavailable")


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
            "ascii"
        )
    ).hexdigest()


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _rollback(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        connection.rollback()


def _warning(error_code: str) -> None:
    logger.warning(
        "channel scheduler iteration failed",
        extra={"event": "channel.scheduler_failed", "error_code": error_code},
    )


__all__ = [
    "ChannelLease",
    "acquire_channel_lease",
    "install_channel_scheduler",
    "run_channel_scheduler_once",
]
