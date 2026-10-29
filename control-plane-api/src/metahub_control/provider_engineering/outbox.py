"""Durable PRD-143 Provider Engineering event publication acknowledgements."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from metahub_shared.db.connection import connect_database
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_LEASE_SECONDS = 30


@dataclass(frozen=True, slots=True)
class ProviderEngineeringEventClaim:
    outbox_id: str
    event_id: str
    event_type: str
    object_id: str
    generation: int
    payload: dict[str, object]
    worker_id: str
    token_hmac: str
    lease_expires_at: datetime


def drain_provider_engineering_event_once(
    config: AppConfig,
    *,
    worker_id: str,
) -> bool:
    claim = _claim(config, worker_id)
    if claim is None:
        return False
    _ack(config, claim)
    return True


def _claim(config: AppConfig, worker_id: str) -> ProviderEngineeringEventClaim | None:
    if type(worker_id) is not str or _SAFE.fullmatch(worker_id) is None:
        raise ApiError("platform.unavailable")
    connection = connect_database(config.database_url)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = datetime.now(UTC).replace(microsecond=0)
        stamp = _db_time(now)
        row = connection.execute(
            """
            SELECT outbox.*,event.event_type,event.object_id,event.generation,
              event.safe_payload_json
            FROM provider_engineering_event_outbox outbox
            JOIN provider_engineering_events event
              ON event.provider_engineering_event_id=outbox.provider_engineering_event_id
            WHERE (outbox.status='pending' AND (outbox.next_attempt_at IS NULL
                    OR unixepoch(outbox.next_attempt_at)<=unixepoch(?)))
               OR(outbox.status='processing' AND unixepoch(outbox.lease_expires_at)<=unixepoch(?))
            ORDER BY outbox.created_at,outbox.provider_engineering_outbox_id LIMIT 1
            """,
            (stamp, stamp),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        attempt = int(row["attempt_count"]) + 1
        if attempt > 10:
            connection.execute(
                """UPDATE provider_engineering_event_outbox
                   SET status='failed',last_error_code='provider_engineering.delivery_exhausted',
                       lease_owner_id=NULL,lease_token_hmac=NULL,lease_expires_at=NULL,updated_at=?
                   WHERE provider_engineering_outbox_id=?""",
                (stamp, row["provider_engineering_outbox_id"]),
            )
            connection.commit()
            return True
        token_hmac = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
        expires = now + timedelta(seconds=_LEASE_SECONDS)
        updated = connection.execute(
            """UPDATE provider_engineering_event_outbox
               SET status='processing',attempt_count=?,lease_owner_id=?,lease_token_hmac=?,
                   lease_expires_at=?,last_error_code=NULL,updated_at=?
               WHERE provider_engineering_outbox_id=? AND attempt_count=?""",
            (
                attempt,
                worker_id,
                token_hmac,
                _db_time(expires),
                stamp,
                row["provider_engineering_outbox_id"],
                row["attempt_count"],
            ),
        ).rowcount
        if updated != 1:
            connection.rollback()
            return False
        try:
            payload = json.loads(str(row["safe_payload_json"]))
        except (json.JSONDecodeError, TypeError, ValueError):
            raise ApiError("platform.unavailable") from None
        if type(payload) is not dict or len(str(row["safe_payload_json"])) > 4096:
            raise ApiError("platform.unavailable")
        claim = ProviderEngineeringEventClaim(
            outbox_id=str(row["provider_engineering_outbox_id"]),
            event_id=str(row["provider_engineering_event_id"]),
            event_type=str(row["event_type"]),
            object_id=str(row["object_id"]),
            generation=int(row["generation"]),
            payload=payload,
            worker_id=worker_id,
            token_hmac=token_hmac,
            lease_expires_at=expires,
        )
        connection.commit()
        return claim
    except ApiError:
        connection.rollback()
        raise
    except sqlite3.Error:
        connection.rollback()
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def _ack(config: AppConfig, claim: ProviderEngineeringEventClaim) -> None:
    connection = connect_database(config.database_url)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = datetime.now(UTC).replace(microsecond=0)
        stamp = _db_time(now)
        updated = connection.execute(
            """UPDATE provider_engineering_event_outbox
               SET status='delivered',delivered_at=?,next_attempt_at=NULL,
                   lease_owner_id=NULL,lease_token_hmac=NULL,lease_expires_at=NULL,
                   last_error_code=NULL,updated_at=?
               WHERE provider_engineering_outbox_id=? AND status='processing'
                 AND lease_owner_id=? AND lease_token_hmac=?
                 AND unixepoch(lease_expires_at)>unixepoch(?)""",
            (
                stamp,
                stamp,
                claim.outbox_id,
                claim.worker_id,
                claim.token_hmac,
                stamp,
            ),
        ).rowcount
        if updated != 1:
            connection.rollback()
            return
        connection.commit()
    except sqlite3.Error:
        connection.rollback()
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = ["ProviderEngineeringEventClaim", "drain_provider_engineering_event_once"]
