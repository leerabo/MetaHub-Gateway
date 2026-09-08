"""Single fenced supervisor for PRD-144 commercial accounting workers."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import FastAPI
from metahub_shared.db.connection import connect_database, database_request_scope
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.billing_disputes.scheduler import run_billing_dispute_event_once
from metahub_control.console_migration.execution import selected_scheduler_work
from metahub_control.invoice_reconciliation.scheduler import run_invoice_reconciliation_once
from metahub_control.payment_commerce.scheduler import run_payment_commerce_once
from metahub_control.plan_billing.scheduler import run_plan_expiry_once, run_quota_event_once
from metahub_control.platform_billing.scheduler import (
    run_platform_cycle_close_once,
    run_platform_meter_once,
)
from metahub_control.saas_billing.scheduler import run_saas_trial_expiry_once

from .outbox import drain_commercial_accounting_event_once
from .service import refresh_subscription_projections

logger = logging.getLogger("metahub.control.commercial_accounting.scheduler")
WORKER_ID = f"commercial-accounting-{uuid4().hex}"
POLL_SECONDS = 2.0
LEASE_SECONDS = 30
SHUTDOWN_TIMEOUT_SECONDS = 10.0
_WORKER_KINDS = (
    "quota_trial_outbox",
    "subscription_trial",
    "meter",
    "payment_dunning_refund",
    "invoice_reconciliation",
    "legacy_dispute",
    "projection",
    "commercial_outbox",
    "reference_rate",
    "cleanup",
)
_WORKER_QUEUES = {
    "quota_trial_outbox": "plan",
    "subscription_trial": "subscription",
    "meter": "main",
    "payment_dunning_refund": "payment",
    "invoice_reconciliation": "invoice",
    "legacy_dispute": "dispute",
    "projection": "subscription",
    "commercial_outbox": "main",
    "reference_rate": "reconciliation",
    "cleanup": "main",
}


@dataclass(frozen=True, slots=True)
class _SchedulerLease:
    worker_kind: str
    generation: int
    token: str


def install_commercial_accounting_scheduler(app: FastAPI) -> None:
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        async with original_lifespan(current_app):
            stop = asyncio.Event()
            task = asyncio.create_task(
                _loop(current_app.state.config, stop),
                name="commercial-accounting-scheduler",
            )
            try:
                yield
            finally:
                stop.set()
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task), timeout=SHUTDOWN_TIMEOUT_SECONDS
                    )
                except TimeoutError:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    app.router.lifespan_context = combined_lifespan


async def _loop(config: AppConfig, stop: asyncio.Event) -> None:
    while not stop.is_set():
        worked = await asyncio.to_thread(run_commercial_accounting_once, config)
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.05 if worked else POLL_SECONDS)
        except TimeoutError:
            continue


def run_commercial_accounting_once(config: AppConfig) -> bool:
    with database_request_scope(reuse_connections=True):
        return _run_commercial_accounting_once(config)


def _run_commercial_accounting_once(config: AppConfig) -> bool:
    for worker_kind in selected_scheduler_work("commercial-accounting", _WORKER_QUEUES):
        lease = _claim(config, worker_kind)
        if lease is None:
            continue
        worked = False
        try:
            worked = _run_worker(config, worker_kind)
            _release(config, lease, worked=worked)
        except ApiError as exc:
            _release_after_failure(config, lease)
            _warning(worker_kind, exc.code)
        except Exception:
            _release_after_failure(config, lease)
            _warning(worker_kind, "platform.unavailable")
        if worked:
            return True
    return False


def _run_worker(config: AppConfig, worker_kind: str) -> bool:
    if worker_kind == "quota_trial_outbox":
        return run_plan_expiry_once(config) or run_quota_event_once(config)
    if worker_kind == "subscription_trial":
        return run_saas_trial_expiry_once(config)
    if worker_kind == "meter":
        return run_platform_meter_once(config) or run_platform_cycle_close_once(config)
    if worker_kind == "payment_dunning_refund":
        return run_payment_commerce_once(config)
    if worker_kind == "invoice_reconciliation":
        return run_invoice_reconciliation_once(config)
    if worker_kind == "legacy_dispute":
        return run_billing_dispute_event_once(config)
    if worker_kind == "projection":
        return refresh_subscription_projections(config, limit=50) > 0
    if worker_kind == "commercial_outbox":
        return drain_commercial_accounting_event_once(config, worker_id=WORKER_ID)
    if worker_kind == "reference_rate":
        _verify_reference_rate_heads(config)
        return False
    if worker_kind == "cleanup":
        return False
    raise ApiError("platform.unavailable")


def _claim(config: AppConfig, worker_kind: str) -> _SchedulerLease | None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _now()
        stamp = _db_time(now)
        row = connection.execute(
            "SELECT * FROM commercial_accounting_scheduler_state WHERE worker_kind=?",
            (worker_kind,),
        ).fetchone()
        if row is None:
            raise ApiError("platform.unavailable")
        if row["lease_expires_at"] is not None and row["lease_expires_at"] > stamp:
            connection.commit()
            return None
        generation = int(row["fence_generation"]) + 1
        token = uuid4().hex + uuid4().hex
        updated = connection.execute(
            """
            UPDATE commercial_accounting_scheduler_state
            SET fence_generation=?,lease_owner_id=?,lease_token_hmac=?,lease_expires_at=?,
              updated_at=?
            WHERE worker_kind=? AND fence_generation=?
              AND(lease_expires_at IS NULL OR lease_expires_at<=?)
            """,
            (
                generation,
                WORKER_ID,
                _lease_hmac(config, token),
                _db_time(now + timedelta(seconds=LEASE_SECONDS)),
                stamp,
                worker_kind,
                row["fence_generation"],
                stamp,
            ),
        ).rowcount
        if updated != 1:
            connection.rollback()
            return None
        connection.commit()
        return _SchedulerLease(worker_kind, generation, token)
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


def _release(config: AppConfig, lease: _SchedulerLease, *, worked: bool) -> None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        stamp = _db_time(_now())
        updated = connection.execute(
            """
            UPDATE commercial_accounting_scheduler_state
            SET cursor_value=?,lease_owner_id=NULL,lease_token_hmac=NULL,
              lease_expires_at=NULL,updated_at=?
            WHERE worker_kind=? AND fence_generation=? AND lease_owner_id=?
              AND lease_token_hmac=? AND lease_expires_at>?
            """,
            (
                f"worked:{stamp}" if worked else f"idle:{stamp}",
                stamp,
                lease.worker_kind,
                lease.generation,
                WORKER_ID,
                _lease_hmac(config, lease.token),
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


def _release_after_failure(config: AppConfig, lease: _SchedulerLease) -> None:
    try:
        _release(config, lease, worked=False)
    except Exception:
        _warning(lease.worker_kind, "platform.unavailable")


def _verify_reference_rate_heads(config: AppConfig) -> None:
    connection = _connect(config)
    try:
        invalid = connection.execute(
            """
            SELECT count(*)
            FROM platform_provider_reference_rate_heads head
            LEFT JOIN platform_provider_reference_rate_releases release
              ON release.provider_reference_rate_release_id=
                 head.provider_reference_rate_release_id
             AND release.provider_id=head.provider_id AND release.model_id=head.model_id
             AND release.currency=head.currency
            WHERE release.provider_reference_rate_release_id IS NULL
              OR release.status NOT IN('released','retired')
              OR release.release_generation!=head.release_generation
            """
        ).fetchone()[0]
        if invalid:
            raise ApiError("platform.unavailable")
    except sqlite3.Error:
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def _lease_hmac(config: AppConfig, token: str) -> str:
    return hmac.new(
        config.workspace_session_secret.encode("utf-8"),
        f"commercial-scheduler-v1\x00{token}".encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("platform.unavailable") from None


def _warning(worker_kind: str, error_code: str) -> None:
    logger.warning(
        "commercial accounting iteration failed",
        extra={
            "event": "commercial_accounting.worker_failed",
            "error_code": error_code,
            "worker_kind": worker_kind,
        },
    )


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = ["install_commercial_accounting_scheduler", "run_commercial_accounting_once"]
