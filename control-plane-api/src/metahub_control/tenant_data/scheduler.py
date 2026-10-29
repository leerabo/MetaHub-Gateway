"""Bounded, cancellable PRD-089 export/deletion worker."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .service import run_tenant_data_once


def install_tenant_data_scheduler(app: FastAPI) -> None:
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(current_app: FastAPI):
        async with original_lifespan(current_app):
            task: asyncio.Task[None] | None = None
            stop = asyncio.Event()
            config = current_app.state.config

            async def worker() -> None:
                while not stop.is_set():
                    try:
                        await asyncio.to_thread(run_tenant_data_once, config)
                    except Exception:
                        # The next bounded poll retries; sensitive database details never escape.
                        pass
                    try:
                        await asyncio.wait_for(
                            stop.wait(),
                            timeout=max(1, int(config.tenant_data_poll_interval_seconds)),
                        )
                    except TimeoutError:
                        continue

            if config.tenant_data_scheduler_enabled:
                task = asyncio.create_task(worker(), name="tenant-data-worker")
            try:
                yield
            finally:
                stop.set()
                if task is not None:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

    app.router.lifespan_context = lifespan


__all__ = ["install_tenant_data_scheduler"]
