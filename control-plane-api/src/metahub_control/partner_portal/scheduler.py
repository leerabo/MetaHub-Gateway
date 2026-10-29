"""PRD-087 分润物化与 EVT-099 source outbox 的可停止后台调度。"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .commission import run_commission_scheduler_once
from .outbox import drain_partner_event_once

logger = logging.getLogger("metahub.control.partner_portal.scheduler")
DEFAULT_POLL_SECONDS = 2.0
COMMISSION_WORKER_ID = f"partner-commission-{uuid4().hex}"
EVENT_WORKER_ID = f"partner-event-{uuid4().hex}"


def install_partner_portal_scheduler(app: FastAPI) -> None:
    """组合现有 lifespan，并在关闭时等待同步 tick 完整退出。"""

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        """先进入已有生命周期，再启动单个串行恢复循环。"""

        async with original_lifespan(current_app):
            stop = threading.Event()
            wake = asyncio.Event()
            task: asyncio.Task[None] | None = None
            if _enabled(current_app.state.config):
                task = asyncio.create_task(
                    _scheduler_loop(current_app.state.config, stop, wake),
                    name="partner-portal-scheduler",
                )
            try:
                yield
            finally:
                stop.set()
                wake.set()
                if task is not None:
                    await asyncio.gather(task, return_exceptions=True)

    app.router.lifespan_context = combined_lifespan


async def _scheduler_loop(config: AppConfig, stop: threading.Event, wake: asyncio.Event) -> None:
    """有工作时快速继续，空轮询时按部署级间隔等待。"""

    while not stop.is_set():
        worked = await asyncio.to_thread(_safe_iteration, config, stop)
        try:
            await asyncio.wait_for(wake.wait(), timeout=0.05 if worked else _poll_seconds(config))
        except TimeoutError:
            continue


def _safe_iteration(config: AppConfig, stop: threading.Event) -> bool:
    """每轮各处理一个 cursor 与 source，日志只保存固定事件和错误码。"""

    try:
        materialized = run_commission_scheduler_once(config, worker_id=COMMISSION_WORKER_ID)
        delivered = drain_partner_event_once(
            config, worker_id=EVENT_WORKER_ID, should_stop=stop.is_set
        )
        return materialized or delivered
    except ApiError as exc:
        logger.warning(
            "partner portal scheduler iteration failed",
            extra={"event": "partner.scheduler_failed", "error_code": exc.code},
        )
    except Exception:
        logger.warning(
            "partner portal scheduler iteration failed",
            extra={"event": "partner.scheduler_failed", "error_code": "partner.unavailable"},
        )
    return False


def _enabled(config: AppConfig) -> bool:
    """非布尔开关按安全默认启用，避免配置漂移静默停止恢复。"""

    value = getattr(config, "partner_portal_scheduler_enabled", True)
    return value if type(value) is bool else True


def _poll_seconds(config: AppConfig) -> float:
    """把轮询间隔限制在 0.1 到 300 秒，拒绝 bool 冒充数字。"""

    value = getattr(config, "partner_portal_scheduler_poll_interval_seconds", DEFAULT_POLL_SECONDS)
    if type(value) not in {int, float} or isinstance(value, bool):
        return DEFAULT_POLL_SECONDS
    return min(max(float(value), 0.1), 300.0)


__all__ = ["install_partner_portal_scheduler"]
