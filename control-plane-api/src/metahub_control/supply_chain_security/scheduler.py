"""Lifecycle-owned scheduler for vulnerability expiry and outbox recovery."""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .service import process_supply_chain_once

LOGGER = logging.getLogger("metahub.control.supply_chain_security.scheduler")
WORKER_ID = f"supply-chain-worker-{uuid4().hex}"


def install_supply_chain_scheduler(app: FastAPI) -> None:
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        async with original_lifespan(current_app):
            stop_event = threading.Event()
            task = asyncio.create_task(
                _scheduler_loop(current_app, stop_event),
                name="supply-chain-security-scheduler",
            )
            try:
                yield
            finally:
                stop_event.set()
                await asyncio.gather(task, return_exceptions=True)

    app.router.lifespan_context = combined_lifespan


async def _scheduler_loop(app: FastAPI, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        await asyncio.to_thread(run_supply_chain_once, app.state.config)
        await _wait(
            stop_event, min(5.0, float(app.state.config.payment_worker_poll_interval_seconds))
        )


def run_supply_chain_once(config: AppConfig) -> bool:
    try:
        return process_supply_chain_once(config, worker_id=WORKER_ID) > 0
    except ApiError as exc:
        _warning(exc.code)
        return False
    except Exception:
        _warning("supply_chain.unavailable")
        return False


async def _wait(stop_event: threading.Event, delay: float) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + delay
    while not stop_event.is_set() and loop.time() < deadline:
        await asyncio.sleep(min(0.05, max(0.0, deadline - loop.time())))


def _warning(error_code: str) -> None:
    LOGGER.warning(
        "supply chain scheduler iteration failed",
        extra={"event": "supply_chain.iteration_failed", "error_code": error_code},
    )


__all__ = ["install_supply_chain_scheduler", "run_supply_chain_once"]
