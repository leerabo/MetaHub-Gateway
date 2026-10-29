"""Bounded ciphertext-retention worker for PRD-125 support bundles."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .support_bundle_service import reap_expired_support_bundles

logger = logging.getLogger("metahub.control.edge_management.scheduler")


def install_edge_support_bundle_scheduler(app: FastAPI) -> None:
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        async with original_lifespan(current_app):
            stop = asyncio.Event()
            task = asyncio.create_task(
                _reaper_loop(current_app.state.config, stop),
                name="edge-support-bundle-reaper",
            )
            try:
                yield
            finally:
                stop.set()
                await asyncio.gather(task, return_exceptions=True)

    app.router.lifespan_context = combined_lifespan


async def _reaper_loop(config: AppConfig, stop: asyncio.Event) -> None:
    failure_delay = 1.0
    while not stop.is_set():
        try:
            deleted = await asyncio.to_thread(reap_expired_support_bundles, config)
            delay = 0.05 if deleted else 30.0
            failure_delay = 1.0
        except ApiError as exc:
            logger.warning(
                "support bundle reaper iteration failed",
                extra={"event": "edge.support_bundle_reaper_failed", "error_code": exc.code},
            )
            delay = failure_delay
            failure_delay = min(30.0, failure_delay * 2)
        except Exception:
            logger.warning(
                "support bundle reaper iteration failed",
                extra={
                    "event": "edge.support_bundle_reaper_failed",
                    "error_code": "edge.support_bundle_storage_unavailable",
                },
            )
            delay = failure_delay
            failure_delay = min(30.0, failure_delay * 2)
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except TimeoutError:
            continue


__all__ = ("install_edge_support_bundle_scheduler",)
