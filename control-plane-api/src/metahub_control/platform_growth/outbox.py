"""Persistently leased PRD-141 growth event outbox."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

_WORKER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
_BACKOFF = (5, 30, 300)


@dataclass(frozen=True, slots=True)
class GrowthEventLease:
    outbox_id: str
    event_id: str
    event_type: str
    stable_event_id: str
    payload_json: str = field(repr=False)
    lease_owner_id: str
    lease_token: str = field(repr=False)
    attempt_count: int
    generation: int
    lease_expires_at: datetime


def claim_next_event(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> GrowthEventLease | None:
    if _WORKER.fullmatch(worker_id) is None:
        raise ApiError("platform.unavailable")
    current = _utc(now)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        stamp = _db_time(current)
        row = connection.execute(
            """SELECT outbox.*,event.event_type,event.payload_json,event.payload_sha256
                FROM platform_growth_event_outbox outbox
                JOIN platform_growth_events event
                  ON event.growth_event_id=outbox.growth_event_id
                WHERE (outbox.status='pending' AND outbox.next_attempt_at<=?)
                   OR (outbox.status='leased' AND outbox.lease_expires_at<=?)
                ORDER BY coalesce(outbox.next_attempt_at,outbox.lease_expires_at),
                  outbox.created_at,outbox.growth_event_outbox_id LIMIT 1""",
            (stamp, stamp),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        payload = str(row["payload_json"])
        if hashlib.sha256(payload.encode("ascii")).hexdigest() != row["payload_sha256"]:
            _fail_corrupt(connection, row, current)
            connection.commit()
            return None
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            _fail_corrupt(connection, row, current)
            connection.commit()
            return None
        if type(parsed) is not dict:
            _fail_corrupt(connection, row, current)
            connection.commit()
            return None
        attempt = int(row["attempt_count"]) + (1 if row["status"] == "pending" else 0)
        if not 1 <= attempt <= 10:
            raise ApiError("platform.unavailable")
        generation = int(row["generation"]) + 1
        token = secrets.token_urlsafe(48)
        expires = current + timedelta(seconds=60)
        frozen = row["snapshot_frozen_at"] or stamp
        snapshot = row["subscription_snapshot_json"] or "[]"
        connection.execute(
            """UPDATE platform_growth_event_outbox SET status='leased',attempt_count=?,
              generation=?,lease_owner_id=?,lease_token_sha256=?,lease_expires_at=?,
              next_attempt_at=NULL,subscription_snapshot_json=?,snapshot_frozen_at=?,
              last_error_code=NULL,updated_at=?
            WHERE growth_event_outbox_id=? AND status=? AND generation=?""",
            (
                attempt,
                generation,
                worker_id,
                _sha(token),
                _db_time(expires),
                snapshot,
                frozen,
                stamp,
                row["growth_event_outbox_id"],
                row["status"],
                row["generation"],
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            connection.rollback()
            return None
        lease = GrowthEventLease(
            outbox_id=row["growth_event_outbox_id"],
            event_id=row["growth_event_id"],
            event_type=row["event_type"],
            stable_event_id=row["stable_event_id"],
            payload_json=payload,
            lease_owner_id=worker_id,
            lease_token=token,
            attempt_count=attempt,
            generation=generation,
            lease_expires_at=expires,
        )
        connection.commit()
        return lease
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def complete_event(
    config: AppConfig,
    lease: GrowthEventLease,
    *,
    now: datetime | None = None,
) -> bool:
    current = _utc(now)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        stamp = _db_time(current)
        connection.execute(
            """UPDATE platform_growth_event_outbox SET status='sent',lease_owner_id=NULL,
              lease_token_sha256=NULL,lease_expires_at=NULL,delivered_at=?,updated_at=?
            WHERE growth_event_outbox_id=? AND status='leased' AND generation=?
              AND lease_owner_id=? AND lease_token_sha256=? AND lease_expires_at>?""",
            (
                stamp,
                stamp,
                lease.outbox_id,
                lease.generation,
                lease.lease_owner_id,
                _sha(lease.lease_token),
                stamp,
            ),
        )
        changed = connection.execute("SELECT changes()").fetchone()[0] == 1
        if changed:
            connection.commit()
        else:
            connection.rollback()
        return changed
    except (sqlite3.Error, DbError, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def fail_event(
    config: AppConfig,
    lease: GrowthEventLease,
    *,
    error_code: str,
    now: datetime | None = None,
) -> bool:
    if error_code not in {"growth.delivery_unavailable", "growth.payload_invalid"}:
        raise ApiError("platform.unavailable")
    current = _utc(now)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        stamp = _db_time(current)
        terminal = lease.attempt_count >= 10
        next_attempt = None
        if not terminal:
            delay = _BACKOFF[min(lease.attempt_count - 1, len(_BACKOFF) - 1)]
            next_attempt = _db_time(current + timedelta(seconds=delay))
        connection.execute(
            """UPDATE platform_growth_event_outbox SET status=?,lease_owner_id=NULL,
              lease_token_sha256=NULL,lease_expires_at=NULL,next_attempt_at=?,last_error_code=?,
              updated_at=? WHERE growth_event_outbox_id=? AND status='leased' AND generation=?
              AND lease_owner_id=? AND lease_token_sha256=?""",
            (
                "failed" if terminal else "pending",
                next_attempt,
                error_code,
                stamp,
                lease.outbox_id,
                lease.generation,
                lease.lease_owner_id,
                _sha(lease.lease_token),
            ),
        )
        changed = connection.execute("SELECT changes()").fetchone()[0] == 1
        if changed:
            connection.commit()
        else:
            connection.rollback()
        return changed
    except (sqlite3.Error, DbError, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def _fail_corrupt(connection: sqlite3.Connection, row: sqlite3.Row, now: datetime) -> None:
    attempt = min(10, int(row["attempt_count"]) + 1)
    terminal = attempt == 10
    next_attempt = None if terminal else _db_time(now + timedelta(seconds=_BACKOFF[0]))
    connection.execute(
        """UPDATE platform_growth_event_outbox SET status=?,attempt_count=?,generation=generation+1,
          lease_owner_id=NULL,lease_token_sha256=NULL,lease_expires_at=NULL,next_attempt_at=?,
          last_error_code='growth.payload_invalid',updated_at=? WHERE growth_event_outbox_id=?""",
        (
            "failed" if terminal else "pending",
            attempt,
            next_attempt,
            _db_time(now),
            row["growth_event_outbox_id"],
        ),
    )


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("platform.unavailable") from None


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


__all__ = ["GrowthEventLease", "claim_next_event", "complete_event", "fail_event"]
