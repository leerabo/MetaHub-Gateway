"""Bounded internal outbox drain for PRD-142 brand-domain events."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta

from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .service import _connect, _db_time


def drain_brand_event_once(
    config: AppConfig, *, worker_id: str, now: datetime | None = None
) -> bool:
    current = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT outbox.*,event.payload_json,event.payload_sha256
            FROM brand_event_outbox outbox
            JOIN brand_events event ON event.brand_event_id=outbox.brand_event_id
            WHERE (outbox.status='pending' AND unixepoch(outbox.next_attempt_at)<=unixepoch(?))
               OR (outbox.status='leased' AND unixepoch(outbox.lease_expires_at)<=unixepoch(?))
            ORDER BY COALESCE(outbox.next_attempt_at,outbox.lease_expires_at),
              outbox.created_at,outbox.brand_event_outbox_id LIMIT 1
            """,
            (_db_time(current), _db_time(current)),
        ).fetchone()
        if row is None:
            connection.commit()
            return False
        payload = str(row["payload_json"])
        if not hmac.compare_digest(
            str(row["payload_sha256"]), hashlib.sha256(payload.encode("utf-8")).hexdigest()
        ):
            raise ApiError("branding.event_unavailable")
        token = secrets.token_urlsafe(32)
        token_hmac = hmac.new(
            config.auth_token_secret.encode("utf-8"),
            b"brand-event-lease-v1\x00" + token.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        generation = int(row["generation"]) + 1
        attempt = int(row["attempt_count"]) + (1 if row["status"] == "pending" else 0)
        expires = current + timedelta(seconds=90)
        changed = connection.execute(
            """
            UPDATE brand_event_outbox
            SET status='leased',attempt_count=?,generation=?,lease_owner_id=?,
              lease_token_hmac=?,lease_expires_at=?,next_attempt_at=NULL,updated_at=?
            WHERE brand_event_outbox_id=? AND status=? AND generation=?
            """,
            (
                attempt,
                generation,
                worker_id,
                token_hmac,
                _db_time(expires),
                _db_time(current),
                row["brand_event_outbox_id"],
                row["status"],
                row["generation"],
            ),
        ).rowcount
        if changed != 1:
            connection.rollback()
            return False
        finished = connection.execute(
            """
            UPDATE brand_event_outbox
            SET status='delivered',generation=generation+1,lease_owner_id=NULL,
              lease_token_hmac=NULL,lease_expires_at=NULL,delivered_at=?,updated_at=?
            WHERE brand_event_outbox_id=? AND status='leased' AND generation=?
              AND lease_owner_id=? AND lease_token_hmac=?
            """,
            (
                _db_time(current),
                _db_time(current),
                row["brand_event_outbox_id"],
                generation,
                worker_id,
                token_hmac,
            ),
        ).rowcount
        if finished != 1:
            raise ApiError("branding.event_unavailable")
        connection.commit()
        return True
    except ApiError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except (sqlite3.Error, TypeError, ValueError):
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("branding.event_unavailable") from None
    finally:
        connection.close()


__all__ = ["drain_brand_event_once"]
