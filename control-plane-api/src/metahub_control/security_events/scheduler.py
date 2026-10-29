"""PRD-081 标准安全事件 outbox 的启动恢复、轮询与协作停服。"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .outbox import drain_security_event_once

logger = logging.getLogger("metahub.control.security_events.scheduler")
WORKER_ID = f"security-event-{uuid4().hex}"
POLL_INTERVAL_SECONDS = 2.0


def install_security_event_scheduler(app: FastAPI) -> None:
    """把安全事件 worker 组合进已有 lifespan，并等待同步 tick 安全退出。"""

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        async with original_lifespan(current_app):
            stop_event = threading.Event()
            wake_event = asyncio.Event()
            task = asyncio.create_task(
                _scheduler_loop(current_app, stop_event, wake_event),
                name="security-event-scheduler",
            )
            try:
                yield
            finally:
                stop_event.set()
                wake_event.set()
                await asyncio.gather(task, return_exceptions=True)

    app.router.lifespan_context = combined_lifespan


async def _scheduler_loop(
    app: FastAPI,
    stop_event: threading.Event,
    wake_event: asyncio.Event,
) -> None:
    """有积压时串行快速排空，空闲时每两秒检查跨进程 trigger 新增的 source。"""

    while not stop_event.is_set():
        worked = await asyncio.to_thread(
            run_security_event_once,
            app.state.config,
            stop_event=stop_event,
        )
        delay = 0.05 if worked else POLL_INTERVAL_SECONDS
        try:
            await asyncio.wait_for(wake_event.wait(), timeout=delay)
            wake_event.clear()
        except TimeoutError:
            continue


def run_security_event_once(
    config: AppConfig,
    *,
    stop_event: threading.Event | None = None,
) -> bool:
    """处理最多一个 aggregate，日志只保留固定事件名和统一错误码。"""

    try:
        return drain_security_event_once(
            config,
            worker_id=WORKER_ID,
            should_stop=stop_event.is_set if stop_event is not None else None,
        )
    except ApiError as exc:
        logger.warning(
            "security event outbox iteration failed",
            extra={"event": "security_event.outbox_failed", "error_code": exc.code},
        )
        return False
    except Exception:
        logger.warning(
            "security event outbox iteration failed",
            extra={
                "event": "security_event.outbox_failed",
                "error_code": "security_event.unavailable",
            },
        )
        return False


__all__ = ["install_security_event_scheduler", "run_security_event_once"]
