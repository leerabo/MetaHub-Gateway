"""Persistent deadline scanner that creates, but never auto-approves, sunset gates."""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .service import schedule_due_deprecations

logger = logging.getLogger("metahub.control.developer_contracts.scheduler")
WORKER_ID = f"developer-contract-worker-{uuid4().hex}"


def install_developer_contract_scheduler(app: FastAPI) -> None:
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        async with original_lifespan(current_app):
            stop_event = threading.Event()
            task = asyncio.create_task(
                _scheduler_loop(current_app.state.config, stop_event),
                name="developer-contract-scheduler",
            )
            try:
                yield
            finally:
                stop_event.set()
                await asyncio.gather(task, return_exceptions=True)

    app.router.lifespan_context = combined_lifespan


async def _scheduler_loop(config: AppConfig, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        await asyncio.to_thread(run_developer_contract_scheduler_once, config)
        await _wait(stop_event, 5.0)


def run_developer_contract_scheduler_once(config: AppConfig) -> bool:
    try:
        return schedule_due_deprecations(config, worker_id=WORKER_ID) > 0
    except ApiError as exc:
        _warning(exc.code)
        return False
    except Exception:
        _warning("developer_contract.unavailable")
        return False


async def _wait(stop_event: threading.Event, delay: float) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + delay
    while not stop_event.is_set() and loop.time() < deadline:
        await asyncio.sleep(min(0.05, max(0.0, deadline - loop.time())))


def _warning(error_code: str) -> None:
    logger.warning(
        "developer contract scheduler iteration failed",
        extra={
            "event": "developer_contract.scheduler_failed",
            "error_code": error_code,
        },
    )


__all__ = ["install_developer_contract_scheduler", "run_developer_contract_scheduler_once"]
