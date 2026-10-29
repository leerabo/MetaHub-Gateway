"""Fenced bounded delivery for PRD-144 commercial accounting events."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError


@dataclass(frozen=True, slots=True)
class CommercialAccountingEventClaim:
    outbox_id: str
    event_id: str
    event_type: str
    generation: int
    lease_token: str
    payload_sha256: str
    payload_json: str


def drain_commercial_accounting_event_once(
    config: AppConfig,
    *,
    worker_id: str,
) -> bool:
    claim = _claim(config, worker_id)
    if claim is None:
        return False
    try:
        payload = json.loads(claim.payload_json)
        if (
            type(payload) is not dict
            or not hmac.compare_digest(
                hashlib.sha256(claim.payload_json.encode("ascii")).hexdigest(),
                claim.payload_sha256,
            )
        ):
            raise ValueError
        _ack(config, worker_id, claim)
        return True
    except (ValueError, UnicodeError, json.JSONDecodeError):
        _fail(config, worker_id, claim)
        raise ApiError("platform.unavailable") from None


def _claim(
    config: AppConfig, worker_id: str
) -> CommercialAccountingEventClaim | None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _now()
        stamp = _db_time(now)
        row = connection.execute(
            """
            SELECT outbox.*,event.event_type,event.payload_sha256,event.payload_json
            FROM commercial_accounting_event_outbox outbox
            JOIN commercial_accounting_events event USING(commercial_accounting_event_id)
            WHERE outbox.status IN('pending','failed') AND outbox.next_attempt_at<=?
              AND(outbox.lease_expires_at IS NULL OR outbox.lease_expires_at<=?)
              AND outbox.attempt_count<20
            ORDER BY outbox.next_attempt_at,outbox.created_at,
              outbox.commercial_accounting_event_outbox_id
            LIMIT 1
            """,
            (stamp, stamp),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        generation = int(row["lease_generation"]) + 1
        lease_token = hashlib.sha256(
            f"{worker_id}\x1f{row['commercial_accounting_event_outbox_id']}\x1f"
            f"{generation}\x1f{stamp}".encode()
        ).hexdigest()
        updated = connection.execute(
            """
            UPDATE commercial_accounting_event_outbox
            SET status='processing',attempt_count=attempt_count+1,lease_generation=?,
              lease_owner_id=?,lease_token_hmac=?,lease_expires_at=?,updated_at=?
            WHERE commercial_accounting_event_outbox_id=?
              AND status IN('pending','failed') AND lease_generation=?
              AND(lease_expires_at IS NULL OR lease_expires_at<=?)
            """,
            (
                generation,
                worker_id,
                _lease_hmac(config, lease_token),
                _db_time(now + timedelta(seconds=30)),
                stamp,
                row["commercial_accounting_event_outbox_id"],
                row["lease_generation"],
                stamp,
            ),
        ).rowcount
        if updated != 1:
            connection.rollback()
            return None
        connection.commit()
        return CommercialAccountingEventClaim(
            outbox_id=row["commercial_accounting_event_outbox_id"],
            event_id=row["commercial_accounting_event_id"],
            event_type=row["event_type"],
            generation=generation,
            lease_token=lease_token,
            payload_sha256=row["payload_sha256"],
            payload_json=row["payload_json"],
        )
    except sqlite3.Error:
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def _ack(
    config: AppConfig,
    worker_id: str,
    claim: CommercialAccountingEventClaim,
) -> None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        stamp = _db_time(_now())
        updated = connection.execute(
            """
            UPDATE commercial_accounting_event_outbox
            SET status='succeeded',lease_owner_id=NULL,lease_token_hmac=NULL,
              lease_expires_at=NULL,dispatched_at=?,updated_at=?
            WHERE commercial_accounting_event_outbox_id=? AND status='processing'
              AND lease_generation=? AND lease_owner_id=? AND lease_token_hmac=?
              AND lease_expires_at>?
            """,
            (
                stamp,
                stamp,
                claim.outbox_id,
                claim.generation,
                worker_id,
                _lease_hmac(config, claim.lease_token),
                stamp,
            ),
        ).rowcount
        if updated != 1:
            raise ApiError("platform.unavailable")
        connection.commit()
    except ApiError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except sqlite3.Error:
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def _fail(
    config: AppConfig,
    worker_id: str,
    claim: CommercialAccountingEventClaim,
) -> None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _now()
        connection.execute(
            """
            UPDATE commercial_accounting_event_outbox
            SET status='failed',lease_owner_id=NULL,lease_token_hmac=NULL,
              lease_expires_at=NULL,next_attempt_at=?,updated_at=?
            WHERE commercial_accounting_event_outbox_id=? AND status='processing'
              AND lease_generation=? AND lease_owner_id=? AND lease_token_hmac=?
            """,
            (
                _db_time(now + timedelta(seconds=30)),
                _db_time(now),
                claim.outbox_id,
                claim.generation,
                worker_id,
                _lease_hmac(config, claim.lease_token),
            ),
        )
        connection.commit()
    except sqlite3.Error:
        if connection.in_transaction:
            connection.rollback()
    finally:
        connection.close()


def _lease_hmac(config: AppConfig, token: str) -> str:
    return hmac.new(
        config.workspace_session_secret.encode("utf-8"),
        f"commercial-outbox-v1\x00{token}".encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("platform.unavailable") from None


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = ["drain_commercial_accounting_event_once"]
