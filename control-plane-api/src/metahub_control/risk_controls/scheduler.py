"""Bounded scheduler for due PRD-110 restriction overlays."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI
from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.platform_jobs.service import (
    enqueue_prepared_platform_job_in_transaction,
    prepare_platform_job,
)

logger = logging.getLogger("metahub.control.risk.scheduler")


def enqueue_due_risk_jobs(config: AppConfig, *, now: datetime | None = None) -> int:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = (now or _database_now(connection)).astimezone(UTC)
        rows = connection.execute(
            """SELECT risk_restriction_id,revision FROM risk_restrictions
               WHERE status='active' AND unixepoch(expires_at)<=unixepoch(?)
               ORDER BY expires_at,risk_restriction_id LIMIT 100""",
            (_db_time(current),),
        ).fetchall()
        enqueued = 0
        for row in rows:
            payload = {
                "restriction_id": str(row["risk_restriction_id"]),
                "expected_revision": int(row["revision"]),
            }
            prepared = prepare_platform_job(
                config, type_code="risk.restriction.expire", payload=payload
            )
            _job_id, replay = enqueue_prepared_platform_job_in_transaction(
                connection,
                config,
                prepared=prepared,
                priority=90,
                source_code="risk.scheduler",
                idempotency_key=(f"risk-expire:{row['risk_restriction_id']}:{row['revision']}"),
                request_id="risk-scheduler",
                trace_id="risk-scheduler",
            )
            if not replay:
                enqueued += 1
        connection.commit()
        return enqueued
    except ApiError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("risk.unavailable") from None
    finally:
        connection.close()


def install_risk_scheduler(app: FastAPI) -> None:
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        async with original_lifespan(current_app):
            stop = threading.Event()
            task = asyncio.create_task(_loop(current_app.state.config, stop), name="risk-scheduler")
            try:
                yield
            finally:
                stop.set()
                await asyncio.gather(task, return_exceptions=True)

    app.router.lifespan_context = combined_lifespan


async def _loop(config: AppConfig, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.to_thread(enqueue_due_risk_jobs, config)
        except Exception:
            logger.warning(
                "risk scheduler iteration failed",
                extra={"event": "risk.scheduler_failed", "error_code": "risk.unavailable"},
            )
        for _index in range(50):
            if stop.is_set():
                return
            await asyncio.sleep(0.1)


def _connect(config: AppConfig):
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("risk.unavailable") from None


def _database_now(connection) -> datetime:
    value = connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


__all__ = ["enqueue_due_risk_jobs", "install_risk_scheduler"]
