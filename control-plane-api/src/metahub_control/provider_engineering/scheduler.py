"""Single PRD-143 supervisor for legacy and Provider Engineering workers."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.adapter_contracts.scheduler import run_adapter_contract_event_once
from metahub_control.console_migration.execution import selected_scheduler_work
from metahub_control.error_maps.scheduler import run_provider_error_map_event_once

from .outbox import drain_provider_engineering_event_once
from .service import refresh_compatibility_projections, run_next_contract_command

logger = logging.getLogger("metahub.control.provider_engineering.scheduler")
WORKER_ID = f"provider-engineering-{uuid4().hex}"
POLL_SECONDS = 2.0
SHUTDOWN_TIMEOUT_SECONDS = 10.0
_WORKER_QUEUES = {
    "adapter_contract": "contract",
    "error_catalog": "error-catalog",
    "contract_command": "contract",
    "compatibility": "compatibility",
    "engineering_outbox": "main",
}


def install_platform_provider_engineering_scheduler(app: FastAPI) -> None:
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        async with original_lifespan(current_app):
            stop = asyncio.Event()
            task = asyncio.create_task(
                _loop(current_app.state.config, stop),
                name="platform-provider-engineering-scheduler",
            )
            try:
                yield
            finally:
                stop.set()
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=SHUTDOWN_TIMEOUT_SECONDS)
                except TimeoutError:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    app.router.lifespan_context = combined_lifespan


async def _loop(config: AppConfig, stop: asyncio.Event) -> None:
    while not stop.is_set():
        worked = await asyncio.to_thread(run_provider_engineering_once, config)
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.05 if worked else POLL_SECONDS)
        except TimeoutError:
            continue


def run_provider_engineering_once(config: AppConfig) -> bool:
    workers = {
        "adapter_contract": lambda: run_adapter_contract_event_once(config),
        "error_catalog": lambda: run_provider_error_map_event_once(config),
        "contract_command": lambda: run_next_contract_command(config),
        "compatibility": lambda: refresh_compatibility_projections(config),
        "engineering_outbox": lambda: drain_provider_engineering_event_once(config, worker_id=WORKER_ID),
    }
    for worker_code in selected_scheduler_work("platform-provider-engineering", _WORKER_QUEUES):
        try:
            if workers[worker_code]():
                return True
        except ApiError as exc:
            logger.warning(
                "provider engineering iteration failed",
                extra={
                    "event": "provider_engineering.worker_failed",
                    "error_code": exc.code,
                },
            )
            return False
        except Exception:
            logger.warning(
                "provider engineering iteration failed",
                extra={
                    "event": "provider_engineering.worker_failed",
                    "error_code": "platform.unavailable",
                },
            )
            return False
    return False


__all__ = [
    "install_platform_provider_engineering_scheduler",
    "run_provider_engineering_once",
]
