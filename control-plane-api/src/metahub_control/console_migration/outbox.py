"""Recoverable, scheduler-fenced delivery for PRD-147 migration events."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.platform_jobs.runtime import (
    PlatformJobOuterFence,
    require_platform_job_outer_fence,
)

_WORKER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,95}$")
_LEASE_SECONDS = 30
_MAX_ATTEMPTS = 20
_MAX_PAYLOAD_BYTES = 4096


@dataclass(frozen=True, slots=True, repr=False)
class ConsoleMigrationEventClaim:
    outbox_id: str
    event_id: str
    event_type: str
    payload: dict[str, object]
    attempt_count: int
    lease_generation: int
    worker_code: str
    lease_token: str
    lease_expires_at: datetime


EventSink = Callable[[str, str, dict[str, object]], None]


def dispatch_console_migration_event_once(
    config: AppConfig,
    *,
    worker_code: str,
    outer_fence: PlatformJobOuterFence,
    sink: EventSink,
) -> bool:
    """Deliver at most one event; the sink must deduplicate by stable event ID."""

    claim = claim_console_migration_event(
        config,
        worker_code=worker_code,
        outer_fence=outer_fence,
    )
    if claim is None:
        return False
    try:
        sink(claim.event_id, claim.event_type, claim.payload)
    except Exception:
        fail_console_migration_event(config, claim, outer_fence=outer_fence)
        return True
    finalize_console_migration_event(config, claim, outer_fence=outer_fence)
    return True


def claim_console_migration_event(
    config: AppConfig,
    *,
    worker_code: str,
    outer_fence: PlatformJobOuterFence,
    lease_seconds: int = _LEASE_SECONDS,
) -> ConsoleMigrationEventClaim | None:
    if _WORKER_RE.fullmatch(worker_code) is None or not 10 <= lease_seconds <= 300:
        raise ApiError("console_migration.invalid")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _database_now(connection)
        stamp = _db_time(now)
        require_platform_job_outer_fence(connection, config, outer_fence, now=now)
        row = connection.execute(
            """SELECT outbox.*,event.event_type,event.payload_json
               FROM platform_console_migration_event_outbox outbox
               JOIN platform_console_migration_events event
                 USING(platform_console_migration_event_id)
               WHERE outbox.attempt_count<? AND(
                 (outbox.status IN('pending','failed')
                   AND unixepoch(outbox.next_attempt_at)<=unixepoch(?))
                 OR(outbox.status='processing'
                   AND unixepoch(outbox.lease_expires_at)<=unixepoch(?)))
               ORDER BY outbox.next_attempt_at,outbox.created_at,
                 outbox.platform_console_migration_event_outbox_id LIMIT 1""",
            (_MAX_ATTEMPTS, stamp, stamp),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        payload_text = str(row["payload_json"])
        payload = json.loads(payload_text)
        if (
            type(payload) is not dict
            or len(payload_text.encode("ascii")) > _MAX_PAYLOAD_BYTES
            or payload.get("event_id") != row["platform_console_migration_event_id"]
            or payload.get("event_type") != row["event_type"]
        ):
            raise ValueError("invalid migration event payload")
        generation = int(row["lease_generation"]) + 1
        attempt = int(row["attempt_count"]) + 1
        token = secrets.token_urlsafe(32)
        expires = now + timedelta(seconds=lease_seconds)
        updated = connection.execute(
            """UPDATE platform_console_migration_event_outbox
               SET status='processing',attempt_count=?,lease_owner_id=?,
                 lease_generation=?,lease_token_hmac=?,lease_expires_at=?,updated_at=?
               WHERE platform_console_migration_event_outbox_id=?
                 AND lease_generation=? AND attempt_count=? AND(
                   status IN('pending','failed') OR(
                     status='processing' AND unixepoch(lease_expires_at)<=unixepoch(?)))""",
            (
                attempt,
                worker_code,
                generation,
                _lease_hmac(config, token),
                _db_time(expires),
                stamp,
                row["platform_console_migration_event_outbox_id"],
                row["lease_generation"],
                row["attempt_count"],
                stamp,
            ),
        ).rowcount
        if updated != 1:
            connection.rollback()
            return None
        connection.commit()
        return ConsoleMigrationEventClaim(
            outbox_id=str(row["platform_console_migration_event_outbox_id"]),
            event_id=str(row["platform_console_migration_event_id"]),
            event_type=str(row["event_type"]),
            payload=payload,
            attempt_count=attempt,
            lease_generation=generation,
            worker_code=worker_code,
            lease_token=token,
            lease_expires_at=expires,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, json.JSONDecodeError, UnicodeError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("console_migration.dependency_unavailable") from None
    finally:
        connection.close()


def renew_console_migration_event(
    config: AppConfig,
    claim: ConsoleMigrationEventClaim,
    *,
    outer_fence: PlatformJobOuterFence,
    lease_seconds: int = _LEASE_SECONDS,
) -> ConsoleMigrationEventClaim | None:
    if not 10 <= lease_seconds <= 300:
        raise ApiError("console_migration.invalid")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _database_now(connection)
        require_platform_job_outer_fence(connection, config, outer_fence, now=now)
        expires = now + timedelta(seconds=lease_seconds)
        updated = connection.execute(
            """UPDATE platform_console_migration_event_outbox SET lease_expires_at=?,updated_at=?
               WHERE platform_console_migration_event_outbox_id=? AND status='processing'
                 AND lease_owner_id=? AND lease_generation=? AND lease_token_hmac=?
                 AND unixepoch(lease_expires_at)>unixepoch(?)""",
            (
                _db_time(expires),
                _db_time(now),
                claim.outbox_id,
                claim.worker_code,
                claim.lease_generation,
                _lease_hmac(config, claim.lease_token),
                _db_time(now),
            ),
        ).rowcount
        connection.commit()
        return None if updated != 1 else ConsoleMigrationEventClaim(
            outbox_id=claim.outbox_id,
            event_id=claim.event_id,
            event_type=claim.event_type,
            payload=claim.payload,
            attempt_count=claim.attempt_count,
            lease_generation=claim.lease_generation,
            worker_code=claim.worker_code,
            lease_token=claim.lease_token,
            lease_expires_at=expires,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("console_migration.dependency_unavailable") from None
    finally:
        connection.close()


def finalize_console_migration_event(
    config: AppConfig,
    claim: ConsoleMigrationEventClaim,
    *,
    outer_fence: PlatformJobOuterFence,
) -> bool:
    return _finish(config, claim, outer_fence=outer_fence, sent=True)


def fail_console_migration_event(
    config: AppConfig,
    claim: ConsoleMigrationEventClaim,
    *,
    outer_fence: PlatformJobOuterFence,
) -> bool:
    return _finish(config, claim, outer_fence=outer_fence, sent=False)


def _finish(
    config: AppConfig,
    claim: ConsoleMigrationEventClaim,
    *,
    outer_fence: PlatformJobOuterFence,
    sent: bool,
) -> bool:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _database_now(connection)
        stamp = _db_time(now)
        require_platform_job_outer_fence(connection, config, outer_fence, now=now)
        next_attempt = now + timedelta(seconds=min(300, 2 ** min(claim.attempt_count, 8)))
        updated = connection.execute(
            """UPDATE platform_console_migration_event_outbox
               SET status=?,next_attempt_at=?,lease_owner_id=NULL,
                 lease_token_hmac=NULL,lease_expires_at=NULL,dispatched_at=?,updated_at=?
               WHERE platform_console_migration_event_outbox_id=? AND status='processing'
                 AND lease_owner_id=? AND lease_generation=? AND lease_token_hmac=?
                 AND unixepoch(lease_expires_at)>unixepoch(?)""",
            (
                "sent" if sent else "failed",
                stamp if sent else _db_time(next_attempt),
                stamp if sent else None,
                stamp,
                claim.outbox_id,
                claim.worker_code,
                claim.lease_generation,
                _lease_hmac(config, claim.lease_token),
                stamp,
            ),
        ).rowcount
        connection.commit()
        return updated == 1
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("console_migration.dependency_unavailable") from None
    finally:
        connection.close()


def _lease_hmac(config: AppConfig, token: str) -> str:
    return hmac.new(
        str(config.cache_affinity_hmac_secret).encode("utf-8"),
        f"prd147/migration-outbox/v1\x00{token}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except (sqlite3.Error, DbError):
        raise ApiError("console_migration.dependency_unavailable") from None


def _database_now(connection: sqlite3.Connection) -> datetime:
    value = connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')").fetchone()[0]
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(UTC)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _rollback(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        connection.rollback()


__all__ = [
    "ConsoleMigrationEventClaim",
    "claim_console_migration_event",
    "dispatch_console_migration_event_once",
    "fail_console_migration_event",
    "finalize_console_migration_event",
    "renew_console_migration_event",
]
