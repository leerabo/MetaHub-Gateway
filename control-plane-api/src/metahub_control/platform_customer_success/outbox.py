"""Fenced PRD-140 customer-success event outbox."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError


@dataclass(frozen=True, slots=True, repr=False)
class CustomerSuccessEventLease:
    outbox_id: str
    event_id: str
    worker_id: str
    token: str
    generation: int
    payload_json: str


def record_event(
    connection: sqlite3.Connection,
    account: sqlite3.Row,
    *,
    event_type: str,
    object_kind: str,
    object_id: str,
    revision: int,
    reason_code: str,
    safe_count: int,
    source_digest: str,
    occurred_at: str,
) -> str:
    event_id = "cse_" + uuid4().hex
    payload = {
        "customer_account_id": account["customer_account_id"],
        "event_id": event_id,
        "object_id": object_id,
        "occurred_at": occurred_at,
        "reason_code": reason_code,
        "revision": revision,
        "safe_count": safe_count,
        "schema_version": "customer-success-event-v1",
        "source_digest": source_digest,
        "tenant_id": account["tenant_id"],
        "workspace_id": account["workspace_id"],
    }
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    payload_hash = _sha(payload_json)
    connection.execute(
        """INSERT INTO platform_customer_success_events(
          customer_success_event_id,customer_account_id,workspace_id,tenant_id,event_type,
          object_kind,object_id,object_revision,reason_code,safe_count,payload_json,
          payload_sha256,schema_version,occurred_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event_id,
            account["customer_account_id"],
            account["workspace_id"],
            account["tenant_id"],
            event_type,
            object_kind,
            object_id,
            revision,
            reason_code,
            safe_count,
            payload_json,
            payload_hash,
            "customer-success-event-v1",
            occurred_at,
        ),
    )
    connection.execute(
        """INSERT INTO platform_customer_success_event_outbox(
          customer_success_event_outbox_id,customer_success_event_id,customer_account_id,
          workspace_id,tenant_id,source_outbox_id,status,attempt_count,fencing_generation,
          lease_owner_id,lease_token_sha256,lease_expires_at,next_attempt_at,
          subscription_snapshot_json,snapshot_frozen_at,last_error_code,delivered_at,
          recovery_watermark,created_at,updated_at)
        VALUES(?,?,?,?,?,NULL,'pending',0,0,NULL,NULL,NULL,?,NULL,NULL,NULL,NULL,0,?,?)""",
        (
            "cso_" + uuid4().hex,
            event_id,
            account["customer_account_id"],
            account["workspace_id"],
            account["tenant_id"],
            occurred_at,
            occurred_at,
            occurred_at,
        ),
    )
    return event_id


def claim_next_event(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> CustomerSuccessEventLease | None:
    if not 8 <= len(worker_id) <= 128:
        raise ApiError("platform.unavailable")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _utc(now)
        stamp = _db_time(current)
        row = connection.execute(
            """SELECT outbox.*,event.payload_json
                 FROM platform_customer_success_event_outbox outbox
                 JOIN platform_customer_success_events event
                   ON event.customer_success_event_id=outbox.customer_success_event_id
                WHERE (outbox.status='pending' AND outbox.next_attempt_at<=?)
                   OR (outbox.status='leased' AND outbox.lease_expires_at<=?)
                ORDER BY coalesce(outbox.next_attempt_at,outbox.lease_expires_at),outbox.created_at,
                         outbox.customer_success_event_outbox_id LIMIT 1""",
            (stamp, stamp),
        ).fetchone()
        if row is None:
            connection.rollback()
            return None
        token = secrets.token_urlsafe(48)
        generation = int(row["fencing_generation"]) + 1
        expires = _db_time(current + timedelta(seconds=30))
        changed = connection.execute(
            """UPDATE platform_customer_success_event_outbox
                  SET status='leased',attempt_count=attempt_count+1,fencing_generation=?,
                      lease_owner_id=?,lease_token_sha256=?,lease_expires_at=?,
                      next_attempt_at=NULL,last_error_code=NULL,updated_at=?
                WHERE customer_success_event_outbox_id=? AND fencing_generation=?
                  AND ((status='pending' AND next_attempt_at<=?)
                    OR(status='leased' AND lease_expires_at<=?))""",
            (
                generation,
                worker_id,
                _sha(token),
                expires,
                stamp,
                row["customer_success_event_outbox_id"],
                int(row["fencing_generation"]),
                stamp,
                stamp,
            ),
        ).rowcount
        if changed != 1:
            connection.rollback()
            return None
        connection.commit()
        return CustomerSuccessEventLease(
            outbox_id=row["customer_success_event_outbox_id"],
            event_id=row["customer_success_event_id"],
            worker_id=worker_id,
            token=token,
            generation=generation,
            payload_json=row["payload_json"],
        )
    except (sqlite3.Error, DbError, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def complete_event(
    config: AppConfig,
    lease: CustomerSuccessEventLease,
    *,
    now: datetime | None = None,
) -> bool:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        stamp = _db_time(_utc(now))
        changed = connection.execute(
            """UPDATE platform_customer_success_event_outbox
                  SET status='sent',lease_owner_id=NULL,lease_token_sha256=NULL,
                      lease_expires_at=NULL,delivered_at=?,recovery_watermark=?,updated_at=?
                WHERE customer_success_event_outbox_id=? AND status='leased'
                  AND lease_owner_id=? AND lease_token_sha256=? AND fencing_generation=?
                  AND lease_expires_at>?""",
            (
                stamp,
                lease.generation,
                stamp,
                lease.outbox_id,
                lease.worker_id,
                _sha(lease.token),
                lease.generation,
                stamp,
            ),
        ).rowcount
        connection.commit()
        return changed == 1
    except (sqlite3.Error, DbError):
        _rollback(connection)
        return False
    finally:
        connection.close()


def fail_event(
    config: AppConfig,
    lease: CustomerSuccessEventLease,
    *,
    error_code: str = "event.delivery_unavailable",
    now: datetime | None = None,
) -> bool:
    if error_code not in {"event.delivery_unavailable", "event.payload_invalid"}:
        raise ApiError("platform.unavailable")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _utc(now)
        stamp = _db_time(current)
        row = connection.execute(
            """SELECT attempt_count FROM platform_customer_success_event_outbox
                WHERE customer_success_event_outbox_id=? AND status='leased'
                  AND lease_owner_id=? AND lease_token_sha256=? AND fencing_generation=?
                  AND lease_expires_at>?""",
            (lease.outbox_id, lease.worker_id, _sha(lease.token), lease.generation, stamp),
        ).fetchone()
        if row is None:
            connection.rollback()
            return False
        terminal = int(row["attempt_count"]) >= 10
        connection.execute(
            """UPDATE platform_customer_success_event_outbox
                  SET status=?,lease_owner_id=NULL,lease_token_sha256=NULL,
                      lease_expires_at=NULL,next_attempt_at=?,last_error_code=?,updated_at=?
                WHERE customer_success_event_outbox_id=?""",
            (
                "failed" if terminal else "pending",
                None if terminal else _db_time(current + timedelta(seconds=30)),
                error_code,
                stamp,
                lease.outbox_id,
            ),
        )
        connection.commit()
        return True
    except (sqlite3.Error, DbError):
        _rollback(connection)
        return False
    finally:
        connection.close()


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


__all__ = [
    "CustomerSuccessEventLease",
    "claim_next_event",
    "complete_event",
    "fail_event",
    "record_event",
]
