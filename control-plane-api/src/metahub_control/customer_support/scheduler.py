"""Persistent PRD-132 SLA and attachment maintenance loop."""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .service import process_attachment_scans, process_support_sla_once

logger = logging.getLogger("metahub.control.customer_support.scheduler")
WORKER_ID = f"customer-support-worker-{uuid4().hex}"


def install_customer_support_scheduler(app: FastAPI) -> None:
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        async with original_lifespan(current_app):
            stop_event = threading.Event()
            task = asyncio.create_task(
                _scheduler_loop(current_app, stop_event), name="customer-support-scheduler"
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
        await asyncio.to_thread(run_customer_support_once, config)
        await _wait(stop_event, min(5.0, float(config.payment_worker_poll_interval_seconds)))


def run_customer_support_once(config: AppConfig) -> bool:
    try:
        sla = process_support_sla_once(config, worker_id=WORKER_ID)
        scans = process_attachment_scans(config)
        return sla > 0 or scans > 0
    except ApiError as exc:
        _warning(exc.code)
        return False
    except Exception:
        _warning("support.unavailable")
        return False


async def _wait(stop_event: threading.Event, delay: float) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + delay
    while not stop_event.is_set() and loop.time() < deadline:
        await asyncio.sleep(min(0.05, max(0.0, deadline - loop.time())))


def _warning(error_code: str) -> None:
    logger.warning(
        "customer support iteration failed",
        extra={"event": "support.iteration_failed", "error_code": error_code},
    )


__all__ = ["install_customer_support_scheduler", "run_customer_support_once"]
