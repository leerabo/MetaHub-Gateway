"""Durable local model call event recovery and Webhook delivery."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import FastAPI
from metahub_shared.db.connection import connect_database
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.webhook_delivery import dispatch_runtime_webhook_event
from metahub_shared.runtime.webhooks import WebhookEvent, build_webhook_event

logger = logging.getLogger("metahub.gateway.local_model_events")
_POLL_SECONDS = 0.5
# Control enforces at most 20 webhook Endpoints per tenant. One source event can
# therefore perform 20 * 3 * 30 seconds of serial network work. Claim one source at
# a time and include two minutes of database/scheduler margin in its lease.
_LEASE_SECONDS = (20 * 3 * 30) + 120
_BATCH_SIZE = 1
_MAX_ATTEMPTS = 10


@dataclass(frozen=True, slots=True)
class LocalModelEventClaim:
    outbox_id: str
    tenant_id: str
    event_id: str
    lease_generation: int
    lease_token: str
    attempt_count: int


def install_local_model_event_recovery(app: FastAPI) -> None:
    """Recover uncertain calls before serving traffic and run the durable outbox worker."""

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        async with original_lifespan(current_app):
            await asyncio.to_thread(recover_stale_local_attempts, current_app.state.config)
            stop = asyncio.Event()
            worker_id = f"gateway-local-events-{uuid4().hex[:24]}"
            worker = asyncio.create_task(
                _outbox_worker(current_app.state.config, worker_id, stop),
                name="gateway-local-model-events",
            )
            try:
                yield
            finally:
                stop.set()
                try:
                    await asyncio.wait_for(worker, timeout=5.0)
                except TimeoutError:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)

    app.router.lifespan_context = combined_lifespan


def recover_stale_local_attempts(config: AppConfig, *, now: datetime | None = None) -> int:
    """Converge stale local attempts without ever sending another Provider request."""

    completed_at = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    stale_before = completed_at - timedelta(
        seconds=config.local_endpoint_overall_timeout_seconds + 30
    )
    connection = connect_database(config.database_url)
    recovered = 0
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            """
            SELECT attempt.tenant_id,attempt.routing_attempt_id,attempt.provider_call_status,
                   attempt.created_at,attempt.provider_call_started_at
            FROM routing_attempts attempt
            JOIN providers provider ON provider.tenant_id=attempt.tenant_id
              AND provider.provider_id=attempt.provider_id
            WHERE provider.provider_type='local_endpoint' AND attempt.status='started'
              AND CASE WHEN attempt.provider_call_status='called'
                    THEN attempt.provider_call_started_at ELSE attempt.updated_at END<=?
            ORDER BY attempt.tenant_id,attempt.created_at,attempt.routing_attempt_id
            """,
            (_db_time(stale_before),),
        ).fetchall()
        for row in rows:
            called = row["provider_call_status"] == "called"
            usage_status = "unavailable" if called else "not_applicable"
            error_code = (
                "gateway.local_endpoint_outcome_unknown"
                if called
                else "gateway.local_endpoint_unavailable"
            )
            latency_ms = _bounded_latency_ms(
                row["provider_call_started_at"] or row["created_at"], completed_at
            )
            cursor = connection.execute(
                """
                UPDATE routing_attempts
                SET status='failed',error_code=?,error_category='other',
                    upstream_http_status=NULL,retryable=0,
                    latency_ms=?,usage_status=?,completed_at=?,updated_at=?
                WHERE tenant_id=? AND routing_attempt_id=? AND status='started'
                  AND provider_call_status=?
                """,
                (
                    error_code,
                    latency_ms,
                    usage_status,
                    _db_time(completed_at),
                    _db_time(completed_at),
                    row["tenant_id"],
                    row["routing_attempt_id"],
                    row["provider_call_status"],
                ),
            )
            recovered += cursor.rowcount
        connection.commit()
        return recovered
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def claim_local_model_events(
    config: AppConfig,
    worker_id: str,
    *,
    now: datetime | None = None,
    limit: int = _BATCH_SIZE,
) -> list[LocalModelEventClaim]:
    """Lease at most one due event per tenant in each fair batch."""

    claimed_at = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    now_text = _db_time(claimed_at)
    lease_until = _db_time(claimed_at + timedelta(seconds=_LEASE_SECONDS))
    connection = connect_database(config.database_url)
    claims: list[LocalModelEventClaim] = []
    try:
        connection.execute("BEGIN IMMEDIATE")
        _recover_expired_leases(connection, now_text)
        rows = connection.execute(
            """
            WITH ranked AS(
              SELECT local_model_call_event_outbox_id,tenant_id,local_model_call_event_id,
                     attempt_count,lease_generation,created_at,
                     ROW_NUMBER() OVER(
                       PARTITION BY tenant_id ORDER BY created_at,local_model_call_event_outbox_id
                     ) AS tenant_rank
              FROM local_model_call_event_outbox
              WHERE status='pending' AND next_attempt_at<=? AND attempt_count<?
            )
            SELECT * FROM ranked WHERE tenant_rank=1
            ORDER BY created_at,tenant_id,local_model_call_event_outbox_id LIMIT ?
            """,
            (now_text, _MAX_ATTEMPTS, max(1, min(limit, _BATCH_SIZE))),
        ).fetchall()
        for row in rows:
            token = hashlib.sha256(uuid4().bytes).hexdigest()
            updated = connection.execute(
                """
                UPDATE local_model_call_event_outbox
                SET status='leased',attempt_count=attempt_count+1,
                    lease_generation=lease_generation+1,lease_owner_id=?,lease_token=?,
                    lease_expires_at=?,next_attempt_at=NULL,error_code=NULL,updated_at=?
                WHERE tenant_id=? AND local_model_call_event_outbox_id=?
                  AND status='pending' AND attempt_count=? AND lease_generation=?
                  AND next_attempt_at<=?
                """,
                (
                    worker_id,
                    token,
                    lease_until,
                    now_text,
                    row["tenant_id"],
                    row["local_model_call_event_outbox_id"],
                    row["attempt_count"],
                    row["lease_generation"],
                    now_text,
                ),
            )
            if updated.rowcount == 1:
                claims.append(
                    LocalModelEventClaim(
                        outbox_id=row["local_model_call_event_outbox_id"],
                        tenant_id=row["tenant_id"],
                        event_id=row["local_model_call_event_id"],
                        lease_generation=int(row["lease_generation"]) + 1,
                        lease_token=token,
                        attempt_count=int(row["attempt_count"]) + 1,
                    )
                )
        connection.commit()
        return claims
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def dispatch_claimed_local_model_event(
    config: AppConfig,
    claim: LocalModelEventClaim,
    *,
    now: datetime | None = None,
) -> bool:
    """Rebuild one exact source event, hand it to delivery, then settle the lease."""

    completed_at = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    try:
        event = _load_source_event(config, claim)
        dispatch_runtime_webhook_event(config, event)
    except Exception:
        _settle_failed(config, claim, completed_at)
        logger.warning(
            "local_model.event_delivery_failed",
            extra={
                "event": "local_model.event_delivery_failed",
                "tenant_id": claim.tenant_id,
                "object_id": claim.event_id,
                "payload": {"error_code": "local_endpoint.unavailable"},
            },
        )
        return False
    settled = _settle_sent(config, claim, completed_at)
    if not settled:
        # A pending delivery intent or lost source lease must never be reported as
        # sent. The same fenced claim is requeued when it is still ours; a stale
        # worker's CAS is a no-op and the current owner remains authoritative.
        _settle_failed(config, claim, completed_at)
    return settled


async def _outbox_worker(config: AppConfig, worker_id: str, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            claims = await asyncio.to_thread(claim_local_model_events, config, worker_id)
            for claim in claims:
                await asyncio.to_thread(dispatch_claimed_local_model_event, config, claim)
        except Exception:
            logger.warning(
                "local_model.event_worker_failed",
                extra={
                    "event": "local_model.event_worker_failed",
                    "payload": {"error_code": "local_endpoint.unavailable"},
                },
            )
            claims = []
        if claims:
            continue
        try:
            await asyncio.wait_for(stop.wait(), timeout=_POLL_SECONDS)
        except TimeoutError:
            pass


def _load_source_event(config: AppConfig, claim: LocalModelEventClaim) -> WebhookEvent:
    connection = connect_database(config.database_url)
    try:
        row = connection.execute(
            """
            SELECT event.* FROM local_model_call_events event
            JOIN local_model_call_event_outbox outbox
              ON outbox.tenant_id=event.tenant_id
             AND outbox.local_model_call_event_id=event.local_model_call_event_id
            WHERE outbox.tenant_id=? AND outbox.local_model_call_event_outbox_id=?
              AND outbox.status='leased' AND outbox.lease_generation=?
              AND outbox.lease_token=? AND event.local_model_call_event_id=?
            """,
            (
                claim.tenant_id,
                claim.outbox_id,
                claim.lease_generation,
                claim.lease_token,
                claim.event_id,
            ),
        ).fetchone()
        if row is None:
            raise sqlite3.IntegrityError("local_model_event_lease_lost")
    finally:
        connection.close()
    payload = {
        "schema_version": "1.0",
        "local_endpoint_id": row["local_endpoint_id"],
        "provider_id": row["provider_id"],
        "model_id": row["model_id"],
        "routing_attempt_id": row["routing_attempt_id"],
        "request_id": row["request_id"],
        "attempt_sequence": int(row["attempt_sequence"]),
        "attempt_role": row["attempt_role"],
        "execution_status": "called",
        "outcome": row["outcome"],
        "error_code": row["error_code"],
        "latency_ms": int(row["latency_ms"] or 0),
        "usage_status": row["usage_status"],
        "completed_at": row["completed_at"],
    }
    return build_webhook_event(
        tenant_id=claim.tenant_id,
        event_type="local_model.called",
        payload=payload,
        object_id=row["local_endpoint_id"],
        trace_id=row["trace_id"],
        source_request_id=row["request_id"],
        event_id=f"evt_{row['local_model_call_event_id']}",
        timestamp=_parse_time(row["completed_at"]),
    )


def _recover_expired_leases(connection: sqlite3.Connection, now: str) -> None:
    connection.execute(
        """
        UPDATE local_model_call_event_outbox
        SET status='failed',lease_generation=lease_generation+1,
            lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,next_attempt_at=NULL,
            error_code='local_endpoint.unavailable',updated_at=?
        WHERE status='leased' AND lease_expires_at<=? AND attempt_count>=?
        """,
        (now, now, _MAX_ATTEMPTS),
    )
    connection.execute(
        """
        UPDATE local_model_call_event_outbox
        SET status='pending',lease_generation=lease_generation+1,
            lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,next_attempt_at=?,
            error_code=NULL,updated_at=?
        WHERE status='leased' AND lease_expires_at<=? AND attempt_count<?
        """,
        (now, now, now, _MAX_ATTEMPTS),
    )


def _settle_sent(config: AppConfig, claim: LocalModelEventClaim, now: datetime) -> bool:
    connection = connect_database(config.database_url)
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """
            UPDATE local_model_call_event_outbox
            SET status='sent',lease_generation=lease_generation+1,
                lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
                next_attempt_at=NULL,error_code=NULL,delivered_at=?,updated_at=?
            WHERE tenant_id=? AND local_model_call_event_outbox_id=? AND status='leased'
              AND lease_generation=? AND lease_token=?
              AND NOT EXISTS(
                SELECT 1 FROM webhook_deliveries delivery
                WHERE delivery.tenant_id=local_model_call_event_outbox.tenant_id
                  AND delivery.event_id=? AND delivery.event_type='local_model.called'
                  AND delivery.delivery_kind='original' AND delivery.status='pending'
              )
            """,
            (
                _db_time(now),
                _db_time(now),
                claim.tenant_id,
                claim.outbox_id,
                claim.lease_generation,
                claim.lease_token,
                f"evt_{claim.event_id}",
            ),
        )
        connection.commit()
        return cursor.rowcount == 1
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _settle_failed(config: AppConfig, claim: LocalModelEventClaim, now: datetime) -> None:
    terminal = claim.attempt_count >= _MAX_ATTEMPTS
    next_attempt = None if terminal else now + timedelta(seconds=min(2**claim.attempt_count, 60))
    connection = connect_database(config.database_url)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE local_model_call_event_outbox
            SET status=?,lease_generation=lease_generation+1,
                lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
                next_attempt_at=?,error_code=?,updated_at=?
            WHERE tenant_id=? AND local_model_call_event_outbox_id=? AND status='leased'
              AND lease_generation=? AND lease_token=?
            """,
            (
                "failed" if terminal else "pending",
                _db_time(next_attempt) if next_attempt else None,
                "local_endpoint.unavailable" if terminal else None,
                _db_time(now),
                claim.tenant_id,
                claim.outbox_id,
                claim.lease_generation,
                claim.lease_token,
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _bounded_latency_ms(created_at: str, completed_at: datetime) -> int:
    try:
        started = _parse_time(created_at)
    except ValueError:
        return 0
    return max(0, min(int((completed_at - started).total_seconds() * 1000), 86_400_000))


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("local_model_event_time_invalid")
    return parsed.astimezone(UTC)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "LocalModelEventClaim",
    "claim_local_model_events",
    "dispatch_claimed_local_model_event",
    "install_local_model_event_recovery",
    "recover_stale_local_attempts",
]
