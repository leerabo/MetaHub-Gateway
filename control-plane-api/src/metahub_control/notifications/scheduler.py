"""Bounded background loop for the PRD-105 notification outbox."""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .service import process_notification_once

logger = logging.getLogger("metahub.control.notifications.scheduler")
WORKER_ID = f"notification-worker-{uuid4().hex}"
BATCH_SIZE = 40


def install_notification_scheduler(app: FastAPI) -> None:
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        async with original_lifespan(current_app):
            stop_event = threading.Event()
            task = asyncio.create_task(
                _scheduler_loop(current_app, stop_event), name="notification-outbox-scheduler"
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
        await asyncio.to_thread(run_notifications_once, config)
        await _wait(stop_event, float(config.payment_worker_poll_interval_seconds))


def run_notifications_once(config: AppConfig) -> bool:
    try:
        handled = False
        for _index in range(BATCH_SIZE):
            progressed = process_notification_once(config, worker_id=WORKER_ID)
            handled = handled or progressed
            if not progressed:
                break
        return handled
    except ApiError as exc:
        _warning(exc.code)
        return False
    except Exception:
        _warning("notification.unavailable")
        return False


async def _wait(stop_event: threading.Event, delay: float) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + delay
    while not stop_event.is_set() and loop.time() < deadline:
        await asyncio.sleep(min(0.05, max(0.0, deadline - loop.time())))


def _warning(error_code: str) -> None:
    logger.warning(
        "notification iteration failed",
        extra={"event": "notification.iteration_failed", "error_code": error_code},
    )


__all__ = ["install_notification_scheduler", "run_notifications_once"]
