"""Single persistently fenced supervisor slot for the nine PRD-141 jobs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import secrets
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

from metahub_control.console_migration.execution import selected_scheduler_work
from metahub_control.product_analytics.outbox import drain_product_event_once

from .outbox import claim_next_event, complete_event, fail_event
from .projection import (
    apply_privacy_corrections,
    enforce_experiment_guardrails,
    evaluate_source_eligibility,
    materialize_attribution,
    materialize_cohort_projection,
    materialize_daily_funnel,
)
from .service import build_next_export

logger = logging.getLogger("metahub.control.platform_growth.scheduler")
WORKER_ID = f"platform-growth-{uuid4().hex}"
POLL_SECONDS = 1.0
LEASE_SECONDS = 90
_WORKER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
_INTERVAL = {
    "source_eligibility": 5,
    "daily_funnel": 60,
    "cohort_projection": 300,
    "attribution": 30,
    "experiment_guardrail": 60,
    "export_build": 2,
    "privacy_correction": 5,
    "growth_event_outbox": 2,
    "product_event_outbox": 2,
}
_ORDER = tuple(_INTERVAL)
_WORKER_QUEUES = {
    code: "events" if code in {"growth_event_outbox", "product_event_outbox"} else "main"
    for code in _ORDER
}


def install_platform_growth_scheduler(app: FastAPI) -> None:
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        async with original_lifespan(current_app):
            stop = threading.Event()
            wake = asyncio.Event()
            task = asyncio.create_task(
                _scheduler_loop(current_app.state.config, stop, wake),
                name="platform-growth-scheduler",
            )
            try:
                yield
            finally:
                stop.set()
                wake.set()
                await asyncio.gather(task, return_exceptions=True)

    app.router.lifespan_context = combined_lifespan


async def _scheduler_loop(config: AppConfig, stop: threading.Event, wake: asyncio.Event) -> None:
    while not stop.is_set():
        worked = await asyncio.to_thread(
            _safe_iteration,
            config,
            WORKER_ID,
            stop.is_set,
        )
        try:
            await asyncio.wait_for(wake.wait(), timeout=0.05 if worked else POLL_SECONDS)
        except TimeoutError:
            continue


def _safe_iteration(config: AppConfig, worker_id: str, should_stop) -> bool:
    try:
        return run_platform_growth_scheduler_once(
            config,
            worker_id=worker_id,
            should_stop=should_stop,
        )
    except ApiError as error:
        _warning(error.code)
    except Exception:
        _warning("platform.growth_scheduler_unavailable")
    return False


def run_platform_growth_scheduler_once(
    config: AppConfig,
    *,
    worker_id: str,
    should_stop=lambda: False,
    now: datetime | None = None,
) -> bool:
    if _WORKER.fullmatch(worker_id) is None:
        raise ApiError("platform.unavailable")
    current = _utc(now)
    lease = _claim_job(config, worker_id=worker_id, now=current)
    if lease is None:
        return False
    job_code, generation, token = lease
    if should_stop():
        _release_job(
            config,
            job_code=job_code,
            worker_id=worker_id,
            generation=generation,
            token=token,
            now=current,
        )
        return False
    try:
        result = _process_job(
            config,
            job_code=job_code,
            worker_id=worker_id,
            generation=generation,
            token=token,
            should_stop=should_stop,
            now=current,
        )
        if should_stop():
            _release_job(
                config,
                job_code=job_code,
                worker_id=worker_id,
                generation=generation,
                token=token,
                now=current,
            )
            return False
        _complete_job(
            config,
            job_code=job_code,
            worker_id=worker_id,
            generation=generation,
            token=token,
            now=current,
        )
        return bool(result or job_code)
    except ApiError:
        _fail_job(
            config,
            job_code=job_code,
            worker_id=worker_id,
            generation=generation,
            token=token,
            now=current,
        )
        raise


def _claim_job(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime,
) -> tuple[str, int, str] | None:
    selected = selected_scheduler_work("platform-growth", _WORKER_QUEUES)
    if not selected:
        return None
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if not _schema_ready(connection):
            connection.commit()
            return None
        stamp = _db_time(now)
        order = "CASE job_code " + " ".join(
            f"WHEN '{code}' THEN {index}" for index, code in enumerate(_ORDER)
        ) + " END"
        row = connection.execute(
            f"""SELECT * FROM platform_growth_scheduler_state
                WHERE job_code IN ({','.join('?' for _ in selected)}) AND next_run_at<=? AND(
                  status IN('idle','failed') OR(status='leased' AND lease_expires_at<=?))
                ORDER BY {order},next_run_at,job_code LIMIT 1""",
            (*selected, stamp, stamp),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        token = secrets.token_urlsafe(48)
        generation = int(row["generation"]) + 1
        connection.execute(
            """UPDATE platform_growth_scheduler_state SET status='leased',generation=?,
              lease_owner_id=?,lease_token_sha256=?,lease_expires_at=?,last_error_code=NULL,
              updated_at=? WHERE job_code=? AND generation=?
              AND (status IN('idle','failed') OR lease_expires_at<=?)""",
            (
                generation,
                worker_id,
                _sha(token),
                _db_time(now + timedelta(seconds=LEASE_SECONDS)),
                stamp,
                row["job_code"],
                row["generation"],
                stamp,
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            connection.rollback()
            return None
        connection.commit()
        return str(row["job_code"]), generation, token
    except (sqlite3.Error, DbError, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def _process_job(
    config: AppConfig,
    *,
    job_code: str,
    worker_id: str,
    generation: int,
    token: str,
    should_stop,
    now: datetime,
) -> int | bool:
    _verify_lease(config, job_code, worker_id, generation, token, now)
    if job_code == "product_event_outbox":
        if not getattr(config, "product_analytics_outbox_enabled", True):
            return False
        return drain_product_event_once(
            config,
            worker_id=worker_id + "-product",
            now=now,
            should_stop=should_stop,
        )
    if job_code == "growth_event_outbox":
        return _deliver_growth_event(config, worker_id, now)
    if job_code == "export_build":
        return build_next_export(config, now=now)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _verify_lease_in_transaction(
            connection,
            job_code,
            worker_id,
            generation,
            token,
            now,
        )
        if should_stop():
            connection.rollback()
            return False
        if job_code == "source_eligibility":
            result = evaluate_source_eligibility(connection, config, now=now)
        elif job_code == "daily_funnel":
            result = materialize_daily_funnel(connection, now=now)
        elif job_code == "cohort_projection":
            result = materialize_cohort_projection(connection, now=now)
        elif job_code == "attribution":
            result = materialize_attribution(connection, config, now=now)
        elif job_code == "experiment_guardrail":
            result = enforce_experiment_guardrails(connection, now=now)
        elif job_code == "privacy_correction":
            result = apply_privacy_corrections(connection, now=now)
        else:
            raise ApiError("platform.unavailable")
        _verify_lease_in_transaction(
            connection,
            job_code,
            worker_id,
            generation,
            token,
            now,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def _deliver_growth_event(config: AppConfig, worker_id: str, now: datetime) -> bool:
    lease = claim_next_event(config, worker_id=worker_id + "-event", now=now)
    if lease is None:
        return False
    try:
        payload = json.loads(lease.payload_json)
        expected = {
            "growth.conversion_recorded": {
                "conversion_id",
                "formula_version",
                "revision",
                "schema_version",
                "stage",
            },
            "growth.campaign_changed": {
                "campaign_id",
                "revision",
                "schema_version",
                "source_digest",
                "status",
            },
            "growth.experiment_decided": {
                "decision",
                "experiment_id",
                "revision",
                "schema_version",
                "source_digest",
            },
            "growth.exported": {
                "export_id",
                "revision",
                "row_count",
                "schema_version",
                "status",
            },
        }[lease.event_type]
        if type(payload) is not dict or set(payload) != expected:
            fail_event(config, lease, error_code="growth.payload_invalid", now=now)
            return False
        return complete_event(config, lease, now=now)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        fail_event(config, lease, error_code="growth.payload_invalid", now=now)
        return False


def _complete_job(
    config: AppConfig,
    *,
    job_code: str,
    worker_id: str,
    generation: int,
    token: str,
    now: datetime,
) -> None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        stamp = _db_time(now)
        cursor = json.dumps(
            {"completed_generation": generation},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        watermark = _job_watermark(connection, job_code)
        connection.execute(
            """UPDATE platform_growth_scheduler_state SET status='idle',cursor_json=?,
              source_watermark=?,next_run_at=?,consecutive_failure_count=0,
              lease_owner_id=NULL,lease_token_sha256=NULL,lease_expires_at=NULL,
              last_error_code=NULL,updated_at=? WHERE job_code=? AND status='leased'
              AND generation=? AND lease_owner_id=? AND lease_token_sha256=?""",
            (
                cursor,
                watermark,
                _db_time(now + timedelta(seconds=_INTERVAL[job_code])),
                stamp,
                job_code,
                generation,
                worker_id,
                _sha(token),
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise ApiError("platform.unavailable")
        connection.commit()
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def _release_job(config: AppConfig, **values) -> None:
    _finish_failed_or_released(config, failed=False, **values)


def _fail_job(config: AppConfig, **values) -> None:
    _finish_failed_or_released(config, failed=True, **values)


def _finish_failed_or_released(
    config: AppConfig,
    *,
    job_code: str,
    worker_id: str,
    generation: int,
    token: str,
    now: datetime,
    failed: bool,
) -> None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT consecutive_failure_count FROM platform_growth_scheduler_state
                WHERE job_code=?""",
            (job_code,),
        ).fetchone()
        failures = int(row[0]) + 1 if failed and row else 0
        delay = min(300, 2 ** min(failures, 8)) if failed else 1
        connection.execute(
            """UPDATE platform_growth_scheduler_state SET status=?,next_run_at=?,
              consecutive_failure_count=?,lease_owner_id=NULL,lease_token_sha256=NULL,
              lease_expires_at=NULL,last_error_code=?,updated_at=? WHERE job_code=?
              AND status='leased' AND generation=? AND lease_owner_id=?
              AND lease_token_sha256=?""",
            (
                "failed" if failed else "idle",
                _db_time(now + timedelta(seconds=delay)),
                failures,
                "platform.growth_scheduler_unavailable" if failed else None,
                _db_time(now),
                job_code,
                generation,
                worker_id,
                _sha(token),
            ),
        )
        connection.commit()
    except (sqlite3.Error, DbError, ValueError, TypeError):
        _rollback(connection)
    finally:
        connection.close()


def _verify_lease(
    config: AppConfig,
    job_code: str,
    worker_id: str,
    generation: int,
    token: str,
    now: datetime,
) -> None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        _verify_lease_in_transaction(
            connection, job_code, worker_id, generation, token, now
        )
        connection.commit()
    finally:
        connection.close()


def _verify_lease_in_transaction(
    connection: sqlite3.Connection,
    job_code: str,
    worker_id: str,
    generation: int,
    token: str,
    now: datetime,
) -> None:
    if connection.execute(
        """SELECT 1 FROM platform_growth_scheduler_state WHERE job_code=?
            AND status='leased' AND generation=? AND lease_owner_id=?
            AND lease_token_sha256=? AND lease_expires_at>?""",
        (job_code, generation, worker_id, _sha(token), _db_time(now)),
    ).fetchone() is None:
        raise ApiError("platform.unavailable")


def _job_watermark(connection: sqlite3.Connection, job_code: str) -> str | None:
    if job_code == "product_event_outbox":
        return connection.execute(
            "SELECT max(updated_at) FROM product_event_outbox"
        ).fetchone()[0]
    if job_code == "growth_event_outbox":
        return connection.execute(
            "SELECT max(updated_at) FROM platform_growth_event_outbox"
        ).fetchone()[0]
    return connection.execute(
        """SELECT source_watermark FROM platform_growth_migration_cursors
            WHERE source_code='product_events'"""
    ).fetchone()[0]


def _schema_ready(connection: sqlite3.Connection) -> bool:
    return connection.execute(
        "SELECT 1 FROM schema_migrations WHERE version='0133' AND status='applied'"
    ).fetchone() is not None


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


def _warning(error_code: str) -> None:
    logger.warning(
        "platform growth scheduler iteration failed",
        extra={"event": "platform.growth_scheduler_failed", "error_code": error_code},
    )


__all__ = ["install_platform_growth_scheduler", "run_platform_growth_scheduler_once"]
