"""Minute-bound scheduler that enqueues immutable observability aggregation jobs."""

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

logger = logging.getLogger("metahub.control.observability.scheduler")


def enqueue_due_observability_jobs(
    config: AppConfig, *, now: datetime | None = None
) -> int:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = (now or _database_now(connection)).astimezone(UTC).replace(
            second=0, microsecond=0
        )
        watermark = int(
            connection.execute("SELECT source_watermark FROM telemetry_ingest_head").fetchone()[0]
        )
        rows = connection.execute(
            """
            SELECT slo.slo_code,slo.version_number FROM service_level_objective_heads head
            JOIN service_level_objectives slo ON
              slo.service_level_objective_id=head.current_slo_id
              AND slo.version_number=head.current_version
            ORDER BY slo.slo_code
            """
        ).fetchall()
        enqueued = 0
        stamp = _db_time(current)
        for row in rows:
            payload = {
                "slo_code": str(row["slo_code"]),
                "slo_version": int(row["version_number"]),
                "source_watermark": watermark,
                "window_ended_at": stamp,
            }
            prepared = prepare_platform_job(
                config, type_code="observability.aggregate", payload=payload
            )
            _job_id, replay = enqueue_prepared_platform_job_in_transaction(
                connection,
                config,
                prepared=prepared,
                priority=80,
                source_code="observability.scheduler",
                idempotency_key=(
                    f"observability:{row['slo_code']}:{row['version_number']}:"
                    f"{watermark}:{int(current.timestamp())}"
                ),
                request_id="observability-scheduler",
                trace_id="observability-scheduler",
            )
            if not replay:
                enqueued += 1
        connection.commit()
        return enqueued
    except ApiError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except (sqlite3.Error, DbError, TypeError, ValueError):
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("observability.unavailable") from None
    finally:
        connection.close()


def install_observability_scheduler(app: FastAPI) -> None:
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        async with original_lifespan(current_app):
            stop = threading.Event()
            task = asyncio.create_task(
                _loop(current_app.state.config, stop), name="observability-scheduler"
            )
            try:
                yield
            finally:
                stop.set()
                await asyncio.gather(task, return_exceptions=True)

    app.router.lifespan_context = combined_lifespan


async def _loop(config: AppConfig, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.to_thread(enqueue_due_observability_jobs, config)
        except Exception:
            logger.warning(
                "observability scheduler iteration failed",
                extra={
                    "event": "observability.scheduler_failed",
                    "error_code": "observability.unavailable",
                },
            )
        # Ten-second polling leaves deterministic headroom under the <60s alert SLO.
        # The minute/watermark idempotency key suppresses duplicate jobs.
        for _index in range(100):
            if stop.is_set():
                return
            await asyncio.sleep(0.1)


def _connect(config: AppConfig):
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("observability.unavailable") from None


def _database_now(connection) -> datetime:
    value = connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


__all__ = ["enqueue_due_observability_jobs", "install_observability_scheduler"]
