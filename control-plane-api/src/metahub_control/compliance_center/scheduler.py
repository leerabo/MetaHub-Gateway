"""Recoverable freshness, exception expiry, report, and outbox scheduler."""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .service import process_compliance_once

logger = logging.getLogger("metahub.control.compliance_center.scheduler")
WORKER_ID = f"compliance-worker-{uuid4().hex}"


def install_compliance_scheduler(app: FastAPI) -> None:
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        async with original_lifespan(current_app):
            stop_event = threading.Event()
            task = asyncio.create_task(
                _scheduler_loop(current_app, stop_event), name="compliance-center-scheduler"
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
        await asyncio.to_thread(run_compliance_once, config)
        await _wait(stop_event, min(5.0, float(config.payment_worker_poll_interval_seconds)))


def run_compliance_once(config: AppConfig) -> bool:
    try:
        return process_compliance_once(config, worker_id=WORKER_ID) > 0
    except ApiError as exc:
        _warning(exc.code)
        return False
    except Exception:
        _warning("compliance.unavailable")
        return False


async def _wait(stop_event: threading.Event, delay: float) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + delay
    while not stop_event.is_set() and loop.time() < deadline:
        await asyncio.sleep(min(0.05, max(0.0, deadline - loop.time())))


def _warning(error_code: str) -> None:
    logger.warning(
        "compliance scheduler iteration failed",
        extra={"event": "compliance.iteration_failed", "error_code": error_code},
    )


__all__ = ["install_compliance_scheduler", "run_compliance_once"]
