"""Single lifespan supervisor for Control background schedulers and platform jobs."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI
from metahub_shared.runtime.errors import ApiError

from .runtime import fence_platform_worker, run_platform_job_once

logger = logging.getLogger("metahub.control.platform_jobs.supervisor")
Installer = Callable[[FastAPI], None]
WORKER_CODE = f"platform-job-worker-{uuid4().hex}"


@asynccontextmanager
async def _noop_lifespan(_app: FastAPI):
    yield


def install_platform_job_supervisor(
    app: FastAPI,
    *,
    scheduler_installers: Sequence[Installer] = (),
) -> None:
    """Capture legacy installers as peers and publish one application lifespan."""

    base_lifespan = app.router.lifespan_context
    scheduler_contexts = []
    try:
        for installer in scheduler_installers:
            app.router.lifespan_context = _noop_lifespan
            installer(app)
            scheduler_contexts.append(app.router.lifespan_context)
    finally:
        app.router.lifespan_context = base_lifespan

    @asynccontextmanager
    async def supervised_lifespan(current_app: FastAPI):
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(base_lifespan(current_app))
            for context in scheduler_contexts:
                await stack.enter_async_context(context(current_app))
            stop_event = threading.Event()
            worker: asyncio.Task[None] | None = None
            if current_app.state.config.control_background_schedulers_enabled:
                worker = asyncio.create_task(
                    _worker_loop(current_app, stop_event),
                    name="platform-job-runtime",
                )
            try:
                yield
            finally:
                stop_event.set()
                if worker is not None:
                    timeout = float(
                        getattr(current_app.state.config, "platform_job_lease_seconds", 45)
                    ) + 5.0
                    try:
                        await asyncio.wait_for(worker, timeout=timeout)
                    except TimeoutError:
                        try:
                            await asyncio.to_thread(
                                fence_platform_worker,
                                current_app.state.config,
                                worker_code=WORKER_CODE,
                            )
                        except ApiError:
                            _warning("platform_job.unavailable")
                        worker.cancel()
                        await asyncio.gather(worker, return_exceptions=True)

    app.router.lifespan_context = supervised_lifespan
    app.state.platform_job_scheduler_context_count = len(scheduler_contexts)


async def _worker_loop(app: FastAPI, stop_event: threading.Event) -> None:
    config = app.state.config
    poll_seconds = float(getattr(config, "platform_job_poll_interval_seconds", 0.5))
    while not stop_event.is_set():
        try:
            progressed = await asyncio.to_thread(
                run_platform_job_once, config, worker_code=WORKER_CODE
            )
        except ApiError as exc:
            progressed = False
            _warning(exc.code)
        except Exception:
            progressed = False
            _warning("platform_job.unavailable")
        await _wait(stop_event, 0.02 if progressed else poll_seconds)


async def _wait(stop_event: threading.Event, delay: float) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.01, delay)
    while not stop_event.is_set() and loop.time() < deadline:
        await asyncio.sleep(min(0.05, max(0.0, deadline - loop.time())))


def _warning(error_code: str) -> None:
    logger.warning(
        "platform job runtime iteration failed",
        extra={
            "event": "platform_job.runtime_iteration_failed",
            "error_code": error_code,
        },
    )


__all__ = ["WORKER_CODE", "install_platform_job_supervisor"]
