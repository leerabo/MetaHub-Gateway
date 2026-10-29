"""Single PRD-142 scheduler slot replacing the legacy branding installer."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import FastAPI
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.branding.outbox import drain_branding_event_once
from metahub_control.console_migration.execution import selected_scheduler_work

from .outbox import drain_brand_event_once
from .service import _connect, _db_time, scan_brand_asset_once

logger = logging.getLogger("metahub.control.brand_domains.scheduler")
WORKER_ID = f"brand-domain-{uuid4().hex}"
_WORKER_QUEUES = {
    "legacy_outbox": "main",
    "asset_scan": "verification",
    "brand_event_outbox": "main",
}


def install_brand_domain_scheduler(app: FastAPI) -> None:
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        async with original_lifespan(current_app):
            stop = threading.Event()
            wake = asyncio.Event()
            task = asyncio.create_task(
                _loop(current_app.state.config, stop, wake), name="brand-domain-scheduler"
            )
            try:
                yield
            finally:
                stop.set()
                wake.set()
                await asyncio.gather(task, return_exceptions=True)

    app.router.lifespan_context = combined_lifespan


async def _loop(config: AppConfig, stop: threading.Event, wake: asyncio.Event) -> None:
    while not stop.is_set():
        worked = await asyncio.to_thread(_safe_once, config, stop)
        try:
            await asyncio.wait_for(wake.wait(), timeout=0.05 if worked else 2.0)
        except TimeoutError:
            continue


def _safe_once(config: AppConfig, stop: threading.Event) -> bool:
    if stop.is_set():
        return False
    try:
        return run_brand_domain_once(config, worker_id=WORKER_ID)
    except ApiError as exc:
        logger.warning(
            "brand domain scheduler iteration failed",
            extra={"event": "brand.scheduler_failed", "error_code": exc.code},
        )
    except Exception:
        logger.warning(
            "brand domain scheduler iteration failed",
            extra={"event": "brand.scheduler_failed", "error_code": "branding.unavailable"},
        )
    return False


def run_brand_domain_once(config: AppConfig, *, worker_id: str) -> bool:
    selected = selected_scheduler_work("brand-domain", _WORKER_QUEUES)
    jobs = (
        ("legacy_outbox", lambda: drain_branding_event_once(config, worker_id=worker_id)),
        ("asset_scan", lambda: scan_brand_asset_once(config)),
        ("brand_event_outbox", lambda: drain_brand_event_once(config, worker_id=worker_id)),
    )
    for job_code, operation in jobs:
        if job_code not in selected:
            continue
        claim = _claim_job(config, job_code=job_code, worker_id=worker_id)
        if claim is None:
            continue
        try:
            worked = bool(operation())
        finally:
            _release_job(config, job_code=job_code, worker_id=worker_id, claim=claim)
        if worked:
            return True
    return False


def _claim_job(config: AppConfig, *, job_code: str, worker_id: str) -> tuple[int, str] | None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = datetime.now(UTC).replace(microsecond=0)
        row = connection.execute(
            "SELECT * FROM brand_scheduler_state WHERE job_code=?", (job_code,)
        ).fetchone()
        if row is None or (
            row["lease_expires_at"] is not None
            and datetime.fromisoformat(str(row["lease_expires_at"]).replace("Z", "+00:00")) > now
        ):
            connection.commit()
            return None
        generation = int(row["fence_generation"]) + 1
        token = secrets.token_urlsafe(32)
        token_hmac = _lease_hmac(config, token)
        changed = connection.execute(
            """
            UPDATE brand_scheduler_state
            SET fence_generation=?,lease_owner_id=?,lease_token_hmac=?,lease_expires_at=?,
              updated_at=? WHERE job_code=? AND fence_generation=?
            """,
            (
                generation,
                worker_id,
                token_hmac,
                _db_time(now + timedelta(seconds=90)),
                _db_time(now),
                job_code,
                row["fence_generation"],
            ),
        ).rowcount
        if changed != 1:
            connection.rollback()
            return None
        connection.commit()
        return generation, token_hmac
    finally:
        connection.close()


def _release_job(
    config: AppConfig,
    *,
    job_code: str,
    worker_id: str,
    claim: tuple[int, str],
) -> None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE brand_scheduler_state
            SET lease_owner_id=NULL,lease_token_hmac=NULL,lease_expires_at=NULL,updated_at=?
            WHERE job_code=? AND fence_generation=? AND lease_owner_id=? AND lease_token_hmac=?
            """,
            (_db_time(datetime.now(UTC)), job_code, claim[0], worker_id, claim[1]),
        )
        connection.commit()
    finally:
        connection.close()


def _lease_hmac(config: AppConfig, token: str) -> str:
    return hmac.new(
        config.auth_token_secret.encode("utf-8"),
        b"brand-scheduler-lease-v1\x00" + token.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


__all__ = ["install_brand_domain_scheduler", "run_brand_domain_once"]
