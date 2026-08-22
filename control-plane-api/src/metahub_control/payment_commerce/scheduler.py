"""PRD-103 支付 receipt 与商业 outbox 的有界后台收敛循环。"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .service import dispatch_commercial_effects, process_payment_receipts

logger = logging.getLogger("metahub.control.payment_commerce.scheduler")
WORKER_ID = f"payment-worker-{uuid4().hex}"
BATCH_SIZE = 50


def install_payment_commerce_scheduler(app: FastAPI) -> None:
    """把 receipt 与 outbox worker 合并进 Control 应用 lifespan。"""

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        async with original_lifespan(current_app):
            stop_event = threading.Event()
            task = asyncio.create_task(
                _scheduler_loop(current_app, stop_event), name="payment-commerce-scheduler"
            )
            try:
                yield
            finally:
                stop_event.set()
                await asyncio.gather(task, return_exceptions=True)

    app.router.lifespan_context = combined_lifespan


async def _scheduler_loop(app: FastAPI, stop_event: threading.Event) -> None:
    """按部署轮询周期串行处理 receipt 和独立商业 outbox。"""

    config = app.state.config
    while not stop_event.is_set():
        await asyncio.to_thread(run_payment_commerce_once, config)
        await _wait(stop_event, float(config.payment_worker_poll_interval_seconds))


def run_payment_commerce_once(config: AppConfig) -> bool:
    """运行一轮有界收敛，日志只包含固定事件和错误码。"""

    try:
        receipts = process_payment_receipts(config, worker_id=WORKER_ID, limit=BATCH_SIZE)
        effects = dispatch_commercial_effects(config, limit=BATCH_SIZE)
        return receipts + effects > 0
    except ApiError as exc:
        _warning(exc.code)
        return False
    except Exception:
        _warning("payment.unavailable")
        return False


async def _wait(stop_event: threading.Event, delay: float) -> None:
    """用短 sleep 响应关闭事件，避免 shutdown 被完整轮询周期阻塞。"""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + delay
    while not stop_event.is_set() and loop.time() < deadline:
        await asyncio.sleep(min(0.05, max(0.0, deadline - loop.time())))


def _warning(error_code: str) -> None:
    """固定日志正文和结构字段，绝不拼接回调或渠道异常。"""

    logger.warning(
        "payment commerce iteration failed",
        extra={"event": "payment_commerce.iteration_failed", "error_code": error_code},
    )


__all__ = ["install_payment_commerce_scheduler", "run_payment_commerce_once"]
