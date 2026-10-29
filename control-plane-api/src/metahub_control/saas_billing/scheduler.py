"""Bounded background convergence for PRD-098 SaaS trial expiry."""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .service import expire_due_trials

logger = logging.getLogger("metahub.control.saas_billing.scheduler")
POLL_SECONDS = 2.0
BATCH_SIZE = 50


def install_saas_billing_scheduler(app: FastAPI) -> None:
    """Install the fixed-interval trial expiry loop into the application lifespan."""

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        async with original_lifespan(current_app):
            stop_event = threading.Event()
            task = asyncio.create_task(
                _scheduler_loop(current_app, stop_event),
                name="saas-trial-expiry-scheduler",
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
        await asyncio.to_thread(run_saas_trial_expiry_once, config)
        await _wait(stop_event, POLL_SECONDS)


def run_saas_trial_expiry_once(config: AppConfig) -> bool:
    """Converge one bounded batch and log only fixed event and error codes."""

    try:
        return expire_due_trials(config, limit=BATCH_SIZE) > 0
    except ApiError as exc:
        _warning(exc.code)
        return False
    except Exception:
        _warning("subscription.unavailable")
        return False


async def _wait(stop_event: threading.Event, delay: float) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + delay
    while not stop_event.is_set() and loop.time() < deadline:
        await asyncio.sleep(min(0.05, max(0.0, deadline - loop.time())))


def _warning(error_code: str) -> None:
    logger.warning(
        "saas trial expiry iteration failed",
        extra={"event": "saas_billing.trial_expiry_failed", "error_code": error_code},
    )


__all__ = ["install_saas_billing_scheduler", "run_saas_trial_expiry_once"]
