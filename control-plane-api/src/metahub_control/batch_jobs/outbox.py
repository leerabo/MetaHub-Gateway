"""Recoverable source outbox for ``batch_job.created/completed`` Webhooks."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import WebhookEvent, build_webhook_event

EVENT_LEASE_SECONDS = 90
EVENT_MAX_ATTEMPTS = 10
EVENT_BACKOFF_SECONDS = (5, 30, 300)
_EVENT_ID = re.compile(r"^bjev_[0-9a-f]{32}$")
_JOB_ID = re.compile(r"^bjob_[0-9a-f]{32}$")
_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


@dataclass(frozen=True, slots=True)
class BatchEventClaim:
    event_id: str
    tenant_id: str
    batch_job_id: str
    event_type: str
    actor_id: str
    source_request_id: str
    source_trace_id: str
    occurred_at: datetime
    payload: dict[str, object] = field(repr=False)
    claim_token: str = field(default="", repr=False)
    lease_owner_id: str = ""
    dispatch_attempts: int = 0
    lease_expires_at: datetime | None = None


def claim_next_batch_event(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> BatchEventClaim | None:
    current = _now(now)
    owner = _worker_id(worker_id)
    current_text = _db_time(current)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE batch_job_event_outbox
            SET dispatch_status='failed',claim_token=NULL,lease_owner_id=NULL,
                lease_expires_at=NULL,next_attempt_at=NULL,last_error_code='webhook.delivery_unavailable',
                updated_at=?
            WHERE dispatch_status='processing' AND dispatch_attempts>=?
              AND unixepoch(lease_expires_at)<=unixepoch(?)
            """,
            (current_text, EVENT_MAX_ATTEMPTS, current_text),
        )
        row = connection.execute(
            """
            SELECT event.*,outbox.dispatch_attempts
            FROM batch_job_event_outbox outbox
            JOIN batch_job_events event
              ON event.tenant_id=outbox.tenant_id
             AND event.batch_job_event_id=outbox.batch_job_event_id
            WHERE (
                outbox.dispatch_status='pending'
                AND unixepoch(outbox.next_attempt_at)<=unixepoch(?)
              ) OR (
                outbox.dispatch_status='processing'
                AND outbox.dispatch_attempts<?
                AND unixepoch(outbox.lease_expires_at)<=unixepoch(?)
              )
            ORDER BY outbox.created_at,outbox.batch_job_event_id LIMIT 1
            """,
            (current_text, EVENT_MAX_ATTEMPTS, current_text),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        token = uuid4().hex
        attempts = int(row["dispatch_attempts"]) + 1
        expires = current + timedelta(seconds=EVENT_LEASE_SECONDS)
        updated = connection.execute(
            """
            UPDATE batch_job_event_outbox
            SET dispatch_status='processing',dispatch_attempts=?,next_attempt_at=NULL,
                dispatched_at=NULL,claim_token=?,lease_owner_id=?,lease_expires_at=?,
                last_error_code=NULL,updated_at=?
            WHERE tenant_id=? AND batch_job_event_id=? AND dispatch_attempts=?
              AND dispatch_status IN ('pending','processing')
            """,
            (
                attempts,
                token,
                owner,
                _db_time(expires),
                current_text,
                row["tenant_id"],
                row["batch_job_event_id"],
                row["dispatch_attempts"],
            ),
        )
        if updated.rowcount != 1:
            connection.rollback()
            return None
        try:
            claim = _claim_from_row(
                row,
                token=token,
                owner=owner,
                attempts=attempts,
                expires=expires,
            )
        except (ApiError, TypeError, ValueError):
            isolated = connection.execute(
                """
                UPDATE batch_job_event_outbox
                SET dispatch_status='failed',next_attempt_at=NULL,dispatched_at=NULL,
                    claim_token=NULL,lease_owner_id=NULL,lease_expires_at=NULL,
                    last_error_code='webhook.payload_invalid',updated_at=?
                WHERE tenant_id=? AND batch_job_event_id=?
                  AND dispatch_status='processing' AND dispatch_attempts=?
                  AND claim_token=? AND lease_owner_id=?
                  AND unixepoch(lease_expires_at)>unixepoch(?)
                """,
                (
                    current_text,
                    row["tenant_id"],
                    row["batch_job_event_id"],
                    attempts,
                    token,
                    owner,
                    current_text,
                ),
            )
            if isolated.rowcount != 1:
                raise ApiError("batch.worker_unavailable") from None
            connection.commit()
            return None
        connection.commit()
        return claim
    except ApiError:
        connection.rollback()
        raise
    except (sqlite3.Error, TypeError, ValueError):
        connection.rollback()
        raise ApiError("batch.worker_unavailable") from None
    finally:
        connection.close()


def dispatch_claimed_batch_event(
    config: AppConfig,
    claim: BatchEventClaim,
    *,
    now: datetime | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    current = _now(now)

    def preserve_source_lease() -> None:
        if should_stop is not None and should_stop():
            raise ApiError("batch.worker_unavailable")
        _renew(config, claim)

    try:
        if should_stop is not None and should_stop():
            raise ApiError("batch.worker_unavailable")
        event = _event(claim)
        _freeze_snapshot(config, claim, event, current=current)
        from metahub_control.webhooks.service import dispatch_webhook_event

        dispatch_webhook_event(
            config,
            claim.tenant_id,
            event,
            request_id=f"req_{claim.event_id}",
            trace_id=claim.source_trace_id,
            resume_existing_pending=True,
            source_lease_guard=preserve_source_lease,
            create_snapshot_if_missing=False,
        )
        return _finish(config, claim, error_code=None, now=_now(now))
    except ValueError:
        return _finish(config, claim, error_code="webhook.payload_invalid", now=_now(now))
    except Exception:
        return _finish(
            config,
            claim,
            error_code="webhook.delivery_unavailable",
            now=_now(now),
        )


def drain_batch_event_once(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    if should_stop is not None and should_stop():
        return False
    claim = claim_next_batch_event(config, worker_id=worker_id, now=now)
    if claim is None:
        return False
    dispatch_claimed_batch_event(
        config,
        claim,
        now=now,
        should_stop=should_stop,
    )
    return True


def _freeze_snapshot(
    config: AppConfig,
    claim: BatchEventClaim,
    event: WebhookEvent,
    *,
    current: datetime,
) -> None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = _active_claim(connection, claim, current=current)
        if row["delivery_snapshot_frozen_at"] is None:
            from metahub_control.webhooks.service import freeze_webhook_event_delivery_snapshot

            frozen = _db_time(current)
            freeze_webhook_event_delivery_snapshot(connection, event, frozen_at=frozen)
            updated = connection.execute(
                """
                UPDATE batch_job_event_outbox
                SET delivery_snapshot_frozen_at=?,updated_at=?
                WHERE tenant_id=? AND batch_job_event_id=? AND claim_token=?
                  AND lease_owner_id=? AND dispatch_status='processing'
                  AND delivery_snapshot_frozen_at IS NULL
                """,
                (
                    frozen,
                    frozen,
                    claim.tenant_id,
                    claim.event_id,
                    claim.claim_token,
                    claim.lease_owner_id,
                ),
            )
            if updated.rowcount != 1:
                raise ApiError("batch.worker_unavailable")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _renew(config: AppConfig, claim: BatchEventClaim) -> None:
    current = _utcnow()
    expires = current + timedelta(seconds=EVENT_LEASE_SECONDS)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        updated = connection.execute(
            """
            UPDATE batch_job_event_outbox
            SET lease_expires_at=?,updated_at=?
            WHERE tenant_id=? AND batch_job_event_id=? AND dispatch_status='processing'
              AND dispatch_attempts=? AND claim_token=? AND lease_owner_id=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                _db_time(expires),
                _db_time(current),
                claim.tenant_id,
                claim.event_id,
                claim.dispatch_attempts,
                claim.claim_token,
                claim.lease_owner_id,
                _db_time(current),
            ),
        )
        if updated.rowcount != 1:
            raise ApiError("batch.worker_unavailable")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _finish(
    config: AppConfig,
    claim: BatchEventClaim,
    *,
    error_code: str | None,
    now: datetime,
) -> bool:
    terminal = error_code == "webhook.payload_invalid" or (
        error_code is not None and claim.dispatch_attempts >= EVENT_MAX_ATTEMPTS
    )
    delay = EVENT_BACKOFF_SECONDS[
        min(max(claim.dispatch_attempts - 1, 0), len(EVENT_BACKOFF_SECONDS) - 1)
    ]
    status = "dispatched" if error_code is None else "failed" if terminal else "pending"
    now_text = _db_time(now)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        updated = connection.execute(
            """
            UPDATE batch_job_event_outbox
            SET dispatch_status=?,next_attempt_at=?,dispatched_at=?,claim_token=NULL,
                lease_owner_id=NULL,lease_expires_at=NULL,last_error_code=?,updated_at=?
            WHERE tenant_id=? AND batch_job_event_id=? AND dispatch_status='processing'
              AND dispatch_attempts=? AND claim_token=? AND lease_owner_id=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                status,
                (
                    _db_time(now + timedelta(seconds=delay))
                    if status == "pending"
                    else None
                ),
                now_text if status == "dispatched" else None,
                error_code,
                now_text,
                claim.tenant_id,
                claim.event_id,
                claim.dispatch_attempts,
                claim.claim_token,
                claim.lease_owner_id,
                now_text,
            ),
        )
        connection.commit()
        return updated.rowcount == 1
    except sqlite3.Error:
        connection.rollback()
        return False
    finally:
        connection.close()


def _active_claim(
    connection: sqlite3.Connection,
    claim: BatchEventClaim,
    *,
    current: datetime,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT delivery_snapshot_frozen_at FROM batch_job_event_outbox
        WHERE tenant_id=? AND batch_job_event_id=? AND dispatch_status='processing'
          AND dispatch_attempts=? AND claim_token=? AND lease_owner_id=?
          AND unixepoch(lease_expires_at)>unixepoch(?)
        """,
        (
            claim.tenant_id,
            claim.event_id,
            claim.dispatch_attempts,
            claim.claim_token,
            claim.lease_owner_id,
            _db_time(current),
        ),
    ).fetchone()
    if row is None:
        raise ApiError("batch.worker_unavailable")
    return row


def _claim_from_row(
    row: sqlite3.Row,
    *,
    token: str,
    owner: str,
    attempts: int,
    expires: datetime,
) -> BatchEventClaim:
    event_id = str(row["batch_job_event_id"])
    job_id = str(row["batch_job_id"])
    event_type = str(row["event_type"])
    if (
        _EVENT_ID.fullmatch(event_id) is None
        or _JOB_ID.fullmatch(job_id) is None
        or event_type not in {"batch_job.created", "batch_job.completed"}
    ):
        raise ApiError("batch.worker_unavailable")
    try:
        payload = json.loads(str(row["payload_json"]))
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ApiError("batch.worker_unavailable") from None
    _validate_payload(event_type, payload, job_id=job_id)
    return BatchEventClaim(
        event_id=event_id,
        tenant_id=str(row["tenant_id"]),
        batch_job_id=job_id,
        event_type=event_type,
        actor_id=str(row["actor_id"]),
        source_request_id=str(row["source_request_id"]),
        source_trace_id=str(row["source_trace_id"]),
        occurred_at=_time(row["occurred_at"]),
        payload=payload,
        claim_token=token,
        lease_owner_id=owner,
        dispatch_attempts=attempts,
        lease_expires_at=expires,
    )


def _validate_payload(event_type: str, payload: object, *, job_id: str) -> None:
    if type(payload) is not dict or payload.get("batch_job_id") != job_id:
        raise ApiError("batch.worker_unavailable")
    expected = (
        {
            "schema_version",
            "batch_job_id",
            "operation",
            "priority",
            "task_count",
            "estimated_cost_cny",
        }
        if event_type == "batch_job.created"
        else {
            "schema_version",
            "batch_job_id",
            "status",
            "succeeded_count",
            "failed_count",
            "cancelled_count",
            "actual_cost_cny",
            "possible_cost_cny",
        }
    )
    if set(payload) != expected or payload.get("schema_version") != 1:
        raise ApiError("batch.worker_unavailable")


def _event(claim: BatchEventClaim) -> WebhookEvent:
    return build_webhook_event(
        tenant_id=claim.tenant_id,
        event_type=claim.event_type,
        event_id=claim.event_id,
        timestamp=claim.occurred_at,
        actor_id=claim.actor_id,
        object_id=claim.batch_job_id,
        trace_id=claim.source_trace_id,
        source_request_id=claim.source_request_id,
        payload=claim.payload,
    )


def _worker_id(value: object) -> str:
    if type(value) is not str or _WORKER_ID.fullmatch(value) is None:
        raise ApiError("batch.worker_unavailable")
    return value


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
    "BatchEventClaim",
    "claim_next_batch_event",
    "dispatch_claimed_batch_event",
    "drain_batch_event_once",
]
