"""PRD-080 账单争议事件的后台恢复调度器。

调度器只负责串行唤起持久 outbox；领取、租户隔离、租约和重试状态均由数据库 worker 决定。
停服时使用线程安全标记等待当前同步网络线程收尾，不能取消外层任务后遗留后台写库。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .outbox import drain_billing_dispute_event_once

logger = logging.getLogger("metahub.control.billing_disputes.scheduler")
EVENT_WORKER_ID = f"billing-dispute-event-{uuid4().hex}"
DEFAULT_POLL_SECONDS = 2.0


def install_billing_dispute_scheduler(app: FastAPI) -> None:
    """把持久事件循环组合进现有 FastAPI lifespan，不覆盖其他后台任务。"""

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        """原 lifespan 启动后恢复 outbox，退出时协作停止并等待线程完成。"""

        async with original_lifespan(current_app):
            stop_event = threading.Event()
            task: asyncio.Task[None] | None = None
            if _enabled(current_app.state.config):
                task = asyncio.create_task(
                    _event_loop(current_app, stop_event),
                    name="billing-dispute-event-outbox",
                )
            try:
                yield
            finally:
                # 停服只设置协作标记；同步网络线程必须观察标记后自行退出，再由 lifespan 等待收尾。
                stop_event.set()
                if task is not None:
                    await asyncio.gather(task, return_exceptions=True)

    app.router.lifespan_context = combined_lifespan


async def _event_loop(app: FastAPI, stop_event: threading.Event) -> None:
    """有积压时短间隔排空，空队列时按受限配置轮询，并持续观察停服标记。"""

    config = app.state.config
    while not stop_event.is_set():
        worked = await asyncio.to_thread(
            run_billing_dispute_event_once,
            config,
            should_stop=stop_event.is_set,
        )
        await _wait(stop_event, 0.05 if worked else _poll(config))


def run_billing_dispute_event_once(config: AppConfig, *, should_stop=None) -> bool:
    """处理至多一个持久事件；日志仅保留固定错误码，不记录异常正文或 Endpoint。"""

    try:
        return drain_billing_dispute_event_once(
            config,
            worker_id=EVENT_WORKER_ID,
            should_stop=should_stop,
        )
    except ApiError as exc:
        _warning(exc.code)
        return False
    except Exception:
        _warning("billing_dispute.unavailable")
        return False


async def _wait(stop_event: threading.Event, delay: float) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + delay
    while not stop_event.is_set() and loop.time() < deadline:
        await asyncio.sleep(min(0.05, max(0.0, deadline - loop.time())))


def _enabled(config: AppConfig) -> bool:
    value = getattr(config, "billing_dispute_outbox_enabled", True)
    return value if type(value) is bool else True


def _poll(config: AppConfig) -> float:
    value = getattr(config, "billing_dispute_outbox_poll_interval_seconds", DEFAULT_POLL_SECONDS)
    if type(value) not in {int, float} or isinstance(value, bool):
        return DEFAULT_POLL_SECONDS
    return min(max(float(value), 0.1), 300.0)


def _warning(error_code: str) -> None:
    logger.warning(
        "billing dispute recovery iteration failed",
        extra={"event": "billing_dispute.outbox_failed", "error_code": error_code},
    )


__all__ = ["install_billing_dispute_scheduler", "run_billing_dispute_event_once"]
