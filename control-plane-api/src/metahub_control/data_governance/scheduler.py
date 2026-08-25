"""Daily bounded scheduler for PRD-117 retention deletion jobs."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import FastAPI
from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.platform_jobs.service import (
    enqueue_prepared_platform_job_in_transaction,
    prepare_platform_job,
)

logger = logging.getLogger("metahub.control.data_governance.scheduler")


def enqueue_due_deletion_jobs(config: AppConfig, *, now: datetime | None = None) -> int:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = (now or _database_now(connection)).astimezone(UTC)
        daily_due = current.replace(hour=0, minute=0, second=0, microsecond=0)
        rows = connection.execute(
            "SELECT retention.*,fence.hold_generation FROM retention_policies retention "
            "JOIN residency_policies policy ON policy.workspace_id=retention.workspace_id "
            "AND policy.tenant_id=retention.tenant_id "
            "AND policy.generation=retention.generation AND policy.status='active' "
            "JOIN data_governance_fences fence ON fence.workspace_id=retention.workspace_id "
            "AND fence.tenant_id=retention.tenant_id "
            "WHERE retention.status='active' "
            "ORDER BY retention.workspace_id,retention.classification_code LIMIT 500"
        ).fetchall()
        enqueued = 0
        for row in rows:
            if not _tenant_lifecycle_active(connection, str(row["tenant_id"])):
                continue
            eligible_at = _time(row["updated_at"]) + timedelta(
                days=int(row["retention_days"]) + int(row["backup_grace_days"])
            )
            if eligible_at > current:
                continue
            due_at = _db_time(daily_due)
            existing = connection.execute(
                "SELECT * FROM deletion_schedules WHERE workspace_id=? "
                "AND retention_policy_id=? AND due_at=?",
                (row["workspace_id"], row["retention_policy_id"], due_at),
            ).fetchone()
            if existing is None:
                schedule_id = "dgs_" + uuid4().hex
                job_id = _enqueue(
                    connection,
                    config,
                    schedule_id=schedule_id,
                    row=row,
                    hold_generation=int(row["hold_generation"]),
                    idempotency_key=f"retention-due:{row['retention_policy_id']}:{due_at}",
                )
                stamp = _db_time(current)
                connection.execute(
                    "INSERT INTO deletion_schedules("
                    "deletion_schedule_id,workspace_id,tenant_id,retention_policy_id,"
                    "classification_code,due_at,status,policy_generation,hold_generation,"
                    "batch_size,checkpoint_code,deleted_count,retained_count,manifest_sha256,"
                    "safe_outcome_code,platform_job_id,revision,started_at,completed_at,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,'queued',?,?,1000,'queued',"
                    "0,0,NULL,NULL,?,1,NULL,NULL,?,?)",
                    (
                        schedule_id,
                        row["workspace_id"],
                        row["tenant_id"],
                        row["retention_policy_id"],
                        row["classification_code"],
                        due_at,
                        row["generation"],
                        row["hold_generation"],
                        job_id,
                        stamp,
                        stamp,
                    ),
                )
                enqueued += 1
            elif _requeue_blocked(connection, config, existing, row, current):
                enqueued += 1
        connection.commit()
        return enqueued
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("data_governance.unavailable") from None
    finally:
        connection.close()


def install_data_governance_scheduler(app: FastAPI) -> None:
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        async with original_lifespan(current_app):
            stop = threading.Event()
            task = asyncio.create_task(
                _loop(current_app.state.config, stop), name="data-governance-scheduler"
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
            await asyncio.to_thread(enqueue_due_deletion_jobs, config)
        except Exception:
            logger.warning(
                "data governance scheduler iteration failed",
                extra={
                    "event": "data_governance.scheduler_failed",
                    "error_code": "data_governance.unavailable",
                },
            )
        for _index in range(600):
            if stop.is_set():
                return
            await asyncio.sleep(0.1)


def _requeue_blocked(connection, config, schedule, row, current: datetime) -> bool:
    if schedule["status"] != "blocked" or int(schedule["policy_generation"]) != int(
        row["generation"]
    ):
        return False
    previous = connection.execute(
        "SELECT status FROM platform_jobs WHERE platform_job_id=?",
        (schedule["platform_job_id"],),
    ).fetchone()
    if previous is None or previous["status"] not in {
        "succeeded",
        "failed",
        "cancelled",
        "dead_lettered",
    }:
        return False
    if _dependencies_active(connection, row, current):
        return False
    hold_generation = int(row["hold_generation"])
    job_id = _enqueue(
        connection,
        config,
        schedule_id=str(schedule["deletion_schedule_id"]),
        row=row,
        hold_generation=hold_generation,
        idempotency_key=(
            f"retention-retry:{schedule['deletion_schedule_id']}:{int(schedule['revision']) + 1}"
        ),
    )
    stamp = _db_time(_changed_time(current, schedule["updated_at"]))
    changed = connection.execute(
        "UPDATE deletion_schedules SET status='queued',hold_generation=?,"
        "checkpoint_code='queued',safe_outcome_code=NULL,platform_job_id=?,"
        "revision=revision+1,updated_at=? WHERE deletion_schedule_id=? "
        "AND status='blocked' AND revision=?",
        (
            hold_generation,
            job_id,
            stamp,
            schedule["deletion_schedule_id"],
            int(schedule["revision"]),
        ),
    ).rowcount
    return changed == 1


def _enqueue(connection, config, *, schedule_id, row, hold_generation, idempotency_key):
    prepared = prepare_platform_job(
        config,
        type_code="data.retention.delete",
        payload={
            "schedule_id": schedule_id,
            "workspace_id": str(row["workspace_id"]),
            "tenant_id": str(row["tenant_id"]),
            "policy_generation": int(row["generation"]),
            "hold_generation": hold_generation,
        },
    )
    job_id, _replay = enqueue_prepared_platform_job_in_transaction(
        connection,
        config,
        prepared=prepared,
        priority=70,
        source_code="data_governance.scheduler",
        idempotency_key=idempotency_key,
        request_id="data-governance-scheduler",
        trace_id="data-governance-scheduler",
    )
    return job_id


def _dependencies_active(connection, row, current: datetime) -> bool:
    tenant_id = str(row["tenant_id"])
    workspace_id = str(row["workspace_id"])
    classification = str(row["classification_code"])
    now = _db_time(current)
    lifecycle = connection.execute(
        "SELECT lifecycle_status FROM tenant_data_states WHERE tenant_id=?", (tenant_id,)
    ).fetchone()
    checks = (
        connection.execute(
            "SELECT 1 FROM legal_holds WHERE workspace_id=? AND tenant_id=? "
            "AND classification_code=? AND status='active' "
            "AND(expires_at IS NULL OR expires_at>?) LIMIT 1",
            (workspace_id, tenant_id, classification, now),
        ).fetchone(),
        connection.execute(
            "SELECT 1 FROM tenant_export_jobs WHERE tenant_id=? "
            "AND status IN('pending','processing') LIMIT 1",
            (tenant_id,),
        ).fetchone(),
        connection.execute(
            "SELECT 1 FROM deletion_requests WHERE tenant_id=? "
            "AND status IN('requested','quiescing','deleting') LIMIT 1",
            (tenant_id,),
        ).fetchone(),
        connection.execute(
            "SELECT 1 FROM backup_sets WHERE workspace_id=? AND tenant_id=? "
            "AND status NOT IN('expired','failed') "
            "AND unixepoch(retention_until)>unixepoch(?) LIMIT 1",
            (workspace_id, tenant_id, now),
        ).fetchone(),
    )
    return (
        lifecycle is None
        or lifecycle["lifecycle_status"] != "active"
        or any(check is not None for check in checks)
    )


def _tenant_lifecycle_active(connection, tenant_id: str) -> bool:
    row = connection.execute(
        "SELECT lifecycle_status FROM tenant_data_states WHERE tenant_id=?", (tenant_id,)
    ).fetchone()
    return row is not None and row["lifecycle_status"] == "active"


def _connect(config: AppConfig):
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("data_governance.unavailable") from None


def _rollback(connection) -> None:
    if connection.in_transaction:
        connection.rollback()


def _database_now(connection) -> datetime:
    value = connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]
    return _time(value)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _changed_time(now: datetime, previous: object) -> datetime:
    prior = _time(previous)
    return now if now > prior else prior + timedelta(milliseconds=1)


def _time(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


__all__ = ["enqueue_due_deletion_jobs", "install_data_governance_scheduler"]
