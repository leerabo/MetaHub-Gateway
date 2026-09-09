"""Single lifespan supervisor for Control background schedulers and platform jobs."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from uuid import uuid4

from fastapi import FastAPI, routing
from metahub_shared.runtime.errors import ApiError

from metahub_control.console_migration.execution import (
    scheduler_queue_proof,
    selected_scheduler_work,
)
from metahub_control.console_migration.outbox import dispatch_console_migration_event_once
from metahub_control.console_migration.scheduler import (
    SchedulerContextBinding,
    scheduler_coordinator_lifespan,
)

from .runtime import PlatformJobOuterFence, fence_platform_worker, run_platform_job_once

logger = logging.getLogger("metahub.control.platform_jobs.supervisor")
Installer = Callable[[FastAPI], None]
WORKER_CODE = f"platform-job-worker-{uuid4().hex}"
_WORKER_QUEUES = {"jobs": "jobs", "migration_outbox": "main"}


@dataclass(frozen=True, slots=True)
class SchedulerInstallerBinding:
    scheduler_code: str
    installer: Installer


@asynccontextmanager
async def _noop_lifespan(_app: FastAPI):
    yield


def install_platform_job_supervisor(
    app: FastAPI,
    *,
    scheduler_installers: Sequence[Installer | SchedulerInstallerBinding] = (),
) -> None:
    """Capture scheduler contexts and publish one application lifespan."""

    base_lifespan = app.router.lifespan_context
    registrations = tuple(scheduler_installers)
    structured = bool(registrations) and all(
        isinstance(item, SchedulerInstallerBinding) for item in registrations
    )
    if any(isinstance(item, SchedulerInstallerBinding) for item in registrations) and not (
        structured
    ):
        raise ValueError("scheduler installers must be uniformly structured")
    legacy_contexts = []
    registry_contexts = []
    try:
        for registration in registrations:
            if isinstance(registration, SchedulerInstallerBinding):
                installer = registration.installer
                scheduler_code = registration.scheduler_code
            else:
                installer = registration
                scheduler_code = None
            app.router.lifespan_context = _noop_lifespan
            installer(app)
            captured = app.router.lifespan_context
            if scheduler_code is None:
                legacy_contexts.append(captured)
            else:
                registry_contexts.append(
                    SchedulerContextBinding(scheduler_code, captured)
                )
    finally:
        app.router.lifespan_context = base_lifespan
    replica_code = f"control-replica-{uuid4().hex}"

    @asynccontextmanager
    async def supervised_lifespan(current_app: FastAPI):
        # Newer FastAPI lazily compiles included routers. Prepare metadata before
        # workers and readiness, without calling dependencies or request handlers.
        route_contexts = getattr(routing, "iter_route_contexts", None)
        if route_contexts is not None:
            for _context in route_contexts(current_app.routes):
                pass
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(base_lifespan(current_app))
            if structured:
                contexts = list(registry_contexts)
                if current_app.state.config.control_background_schedulers_enabled:
                    contexts.append(
                        SchedulerContextBinding(
                            "platform-job-runtime", _platform_job_lifespan
                        )
                    )
                await stack.enter_async_context(
                    scheduler_coordinator_lifespan(
                        current_app,
                        contexts,
                        replica_code=replica_code,
                    )
                )
                yield
                return
            for context in legacy_contexts:
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
    app.state.platform_job_scheduler_context_count = len(registrations)
    app.state.scheduler_cluster_context_count = len(registry_contexts) + (
        1 if structured and app.state.config.control_background_schedulers_enabled else 0
    )


@asynccontextmanager
async def _platform_job_lifespan(current_app: FastAPI):
    stop_event = threading.Event()
    worker = asyncio.create_task(
        _worker_loop(current_app, stop_event),
        name="platform-job-runtime",
    )
    try:
        yield
    finally:
        stop_event.set()
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


async def _worker_loop(app: FastAPI, stop_event: threading.Event) -> None:
    config = app.state.config
    poll_seconds = float(getattr(config, "platform_job_poll_interval_seconds", 0.5))
    while not stop_event.is_set():
        registry_leases = getattr(app.state, "scheduler_registry_leases", None)
        proof = scheduler_queue_proof("platform-job-runtime")
        if registry_leases is not None and proof is None:
            await _wait(stop_event, poll_seconds)
            continue
        outer_fence = (
            None
            if proof is None
            else PlatformJobOuterFence(
                scheduler_code=proof.scheduler_code,
                partition_code=proof.partition_code,
                lease_token=proof.token,
                fencing_generation=proof.generation,
                registry_generation=proof.registry_generation,
            )
        )
        try:
            selected = selected_scheduler_work("platform-job-runtime", _WORKER_QUEUES)
            progressed = False
            if "jobs" in selected:
                progressed = await asyncio.to_thread(
                    run_platform_job_once,
                    config,
                    worker_code=WORKER_CODE,
                    outer_fence=outer_fence,
                )
            sink = getattr(app.state, "console_migration_event_sink", None)
            if (
                "migration_outbox" in selected and not progressed
                and outer_fence is not None and callable(sink)
            ):
                progressed = await asyncio.to_thread(
                    dispatch_console_migration_event_once,
                    config,
                    worker_code=WORKER_CODE,
                    outer_fence=outer_fence,
                    sink=sink,
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


__all__ = [
    "WORKER_CODE",
    "SchedulerInstallerBinding",
    "install_platform_job_supervisor",
]
