"""产品事件源队列调度器负责有界轮询、停服协作和安全错误日志收敛。"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .outbox import drain_product_event_once

logger = logging.getLogger("metahub.control.product_analytics.scheduler")
# 每个进程使用独立 worker 身份；轮询下限防止异常配置形成数据库忙循环。
WORKER_ID = f"product-event-outbox-{uuid4().hex}"
DEFAULT_POLL_SECONDS = 2.0


def install_product_event_scheduler(app: FastAPI) -> None:
    """组合 lifespan，并在停服时等待当前有界投递协作退出。"""

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        """在原应用生命周期内启动调度任务，并在停服时等待租约处理安全结束。"""

        async with original_lifespan(current_app):
            stop_event = threading.Event()
            wake_event = asyncio.Event()
            task: asyncio.Task[None] | None = None
            if _enabled(current_app.state.config):
                task = asyncio.create_task(
                    _scheduler_loop(current_app, stop_event, wake_event),
                    name="product-event-outbox-scheduler",
                )
            try:
                yield
            finally:
                stop_event.set()
                wake_event.set()
                if task is not None:
                    await asyncio.gather(task, return_exceptions=True)

    app.router.lifespan_context = combined_lifespan


async def _scheduler_loop(
    app: FastAPI,
    stop_event: threading.Event,
    wake_event: asyncio.Event,
) -> None:
    """串行处理源事件，并根据是否取得工作动态选择短等待或配置轮询间隔。"""

    config = app.state.config
    while not stop_event.is_set():
        worked = await asyncio.to_thread(
            run_product_event_outbox_once,
            config,
            should_stop=stop_event.is_set,
        )
        try:
            await asyncio.wait_for(
                wake_event.wait(),
                timeout=0.05 if worked else _poll_seconds(config),
            )
        # 等待超时只是正常轮询节拍，不能记录成投递故障或终止后台恢复任务。
        except TimeoutError:
            continue


def run_product_event_outbox_once(config: AppConfig, *, should_stop=None) -> bool:
    """处理至多一个 source；日志只保留固定领域错误码。"""

    try:
        return drain_product_event_once(
            config,
            worker_id=WORKER_ID,
            should_stop=should_stop,
        )
    # 已知领域失败只记录固定错误码，不能把数据库、Endpoint 或正文细节写入日志。
    except ApiError as exc:
        logger.warning(
            "product event outbox iteration failed",
            extra={"event": "product_analytics.outbox_failed", "error_code": exc.code},
        )
        return False
    # 未知异常统一收敛为不可用错误，避免异常字符串泄露凭据或内部存储结构。
    except Exception:
        logger.warning(
            "product event outbox iteration failed",
            extra={
                "event": "product_analytics.outbox_failed",
                "error_code": "product_analytics.unavailable",
            },
        )
        return False


def _enabled(config: AppConfig) -> bool:
    """读取可关闭开关；错误类型回退启用，避免配置漂移静默停止可靠投递。"""

    value = getattr(config, "product_analytics_outbox_enabled", True)
    return value if type(value) is bool else True


def _poll_seconds(config: AppConfig) -> float:
    """把轮询间隔限制在安全范围内，并拒绝布尔值冒充数字配置。"""

    value = getattr(
        config,
        "product_analytics_outbox_poll_interval_seconds",
        DEFAULT_POLL_SECONDS,
    )
    if type(value) not in {int, float} or isinstance(value, bool):
        return DEFAULT_POLL_SECONDS
    return min(max(float(value), 0.1), 300.0)


__all__ = ["install_product_event_scheduler", "run_product_event_outbox_once"]
