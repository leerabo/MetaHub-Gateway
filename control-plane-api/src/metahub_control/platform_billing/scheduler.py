"""Fixed-interval workers for platform meter projection and billing-cycle closure."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .service import close_due_cycles, project_usage_once

logger = logging.getLogger("metahub.control.platform_billing.scheduler")
WORKER_ID = f"platform-meter-{uuid4().hex}"
POLL_SECONDS = 2.0
CLOSE_BATCH_SIZE = 50
FAILURE_BACKOFF_SECONDS = 15.0
RECONCILIATION_BACKOFF_SECONDS = 300.0
_METER_RETRY_AFTER = 0.0
_METER_RECONCILIATION_RETRY_AFTER = 0.0
_CYCLE_RETRY_AFTER = 0.0


def install_platform_billing_scheduler(app: FastAPI) -> None:
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        async with original_lifespan(current_app):
            stop_event = threading.Event()
            task = asyncio.create_task(
                _scheduler_loop(current_app, stop_event),
                name="platform-metering-billing-scheduler",
            )
            try:
                yield
            finally:
                stop_event.set()
                await asyncio.gather(task, return_exceptions=True)

    app.router.lifespan_context = combined_lifespan


async def _scheduler_loop(app: FastAPI, stop_event: threading.Event) -> None:
    config = app.state.config
    while not stop_event.is_set():
        await asyncio.to_thread(run_platform_meter_once, config)
        await asyncio.to_thread(run_platform_cycle_close_once, config)
        await _wait(stop_event, POLL_SECONDS)


def run_platform_meter_once(config: AppConfig) -> bool:
    global _METER_RETRY_AFTER, _METER_RECONCILIATION_RETRY_AFTER
    now = time.monotonic()
    if now < max(_METER_RETRY_AFTER, _METER_RECONCILIATION_RETRY_AFTER):
        return False
    try:
        worked = project_usage_once(config, worker_id=WORKER_ID)
        _METER_RETRY_AFTER = 0.0
        _METER_RECONCILIATION_RETRY_AFTER = 0.0
        return worked
    except ApiError as exc:
        if exc.code == "platform_metering.reconciliation_required":
            _METER_RECONCILIATION_RETRY_AFTER = time.monotonic() + RECONCILIATION_BACKOFF_SECONDS
            _warning("platform_billing.reconciliation_blocked", exc.code)
        else:
            _METER_RETRY_AFTER = time.monotonic() + FAILURE_BACKOFF_SECONDS
            _warning("platform_billing.meter_projection_failed", exc.code)
        return False
    except Exception:
        _METER_RETRY_AFTER = time.monotonic() + FAILURE_BACKOFF_SECONDS
        _warning("platform_billing.meter_projection_failed", "platform_metering.unavailable")
        return False


def run_platform_cycle_close_once(config: AppConfig) -> bool:
    global _CYCLE_RETRY_AFTER
    now = time.monotonic()
    if now < _CYCLE_RETRY_AFTER:
        return False
    try:
        worked = close_due_cycles(config, limit=CLOSE_BATCH_SIZE) > 0
        _CYCLE_RETRY_AFTER = 0.0
        return worked
    except ApiError as exc:
        _CYCLE_RETRY_AFTER = time.monotonic() + FAILURE_BACKOFF_SECONDS
        _warning("platform_billing.cycle_close_failed", exc.code)
        return False
    except Exception:
        _CYCLE_RETRY_AFTER = time.monotonic() + FAILURE_BACKOFF_SECONDS
        _warning("platform_billing.cycle_close_failed", "platform_billing.unavailable")
        return False


async def _wait(stop_event: threading.Event, delay: float) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + delay
    while not stop_event.is_set() and loop.time() < deadline:
        await asyncio.sleep(min(0.05, max(0.0, deadline - loop.time())))


def _warning(event: str, error_code: str) -> None:
    logger.warning(
        "platform billing iteration failed",
        extra={"event": event, "error_code": error_code},
    )


__all__ = [
    "install_platform_billing_scheduler",
    "run_platform_cycle_close_once",
    "run_platform_meter_once",
]
