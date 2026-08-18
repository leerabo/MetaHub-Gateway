"""PRD-088 EVT-100 source worker 的可停止生命周期。"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .outbox import drain_branding_event_once

logger = logging.getLogger("metahub.control.branding.scheduler")
WORKER_ID = f"branding-event-{uuid4().hex}"


def install_branding_scheduler(app: FastAPI) -> None:
    """安装串行轮询 worker，退出时等待最后一个同步 tick 完整收敛。"""

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        async with original_lifespan(current_app):
            stop = threading.Event()
            wake = asyncio.Event()
            task = asyncio.create_task(
                _loop(current_app.state.config, stop, wake), name="branding-scheduler"
            )
            try:
                yield
            finally:
                stop.set()
                wake.set()
                await asyncio.gather(task, return_exceptions=True)

    app.router.lifespan_context = combined_lifespan


async def _loop(config: AppConfig, stop: threading.Event, wake: asyncio.Event) -> None:
    """有积压快速 drain，空闲固定两秒轮询。"""

    while not stop.is_set():
        worked = await asyncio.to_thread(_safe_once, config, stop)
        try:
            await asyncio.wait_for(wake.wait(), timeout=0.05 if worked else 2.0)
        except TimeoutError:
            continue


def _safe_once(config: AppConfig, stop: threading.Event) -> bool:
    """后台异常只记固定错误码，不记录 payload、Logo、域名或租户正文。"""

    try:
        return drain_branding_event_once(config, worker_id=WORKER_ID, should_stop=stop.is_set)
    except ApiError as exc:
        logger.warning(
            "branding event scheduler iteration failed",
            extra={"event": "branding.scheduler_failed", "error_code": exc.code},
        )
    except Exception:
        logger.warning(
            "branding event scheduler iteration failed",
            extra={
                "event": "branding.scheduler_failed",
                "error_code": "branding.event_unavailable",
            },
        )
    return False


__all__ = ["install_branding_scheduler"]
