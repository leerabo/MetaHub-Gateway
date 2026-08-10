"""PRD-076 Response Cache source outbox 的可关闭 Control 后台调度器。

控制面缓存能力必须保持租户、权限、版本和错误合同一致，不能信任客户端归属。
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

from .outbox import drain_response_cache_event_once

logger = logging.getLogger("metahub.control.response_cache.scheduler")
# 控制面缓存能力必须保持租户、权限、版本和错误合同一致，不能信任客户端归属。
WORKER_ID = f"response-cache-outbox-{uuid4().hex}"
DEFAULT_POLL_SECONDS = 2.0


def install_response_cache_outbox_scheduler(app: FastAPI) -> None:
    """组合进既有 lifespan；停服先通知 worker，再以硬上限等待当前有界投递。"""

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        """保留全部已安装生命周期，并只在配置允许时启动一个串行 worker。"""

        async with original_lifespan(current_app):
            stop_event = threading.Event()
            wake_event = asyncio.Event()
            task: asyncio.Task[None] | None = None
            if _enabled(current_app.state.config):
                task = asyncio.create_task(
                    _scheduler_loop(current_app, stop_event, wake_event),
                    name="response-cache-outbox-scheduler",
                )
            try:
                yield
            finally:
                stop_event.set()
                wake_event.set()
                if task is not None:
                    # asyncio.to_thread 不能被 task.cancel 安全终止；等待有界 webhook attempt
                    # 返回并完成 lease CAS，避免旧 lifespan 退出后仍写库或出网。
                    await asyncio.gather(task, return_exceptions=True)

    app.router.lifespan_context = combined_lifespan


async def _scheduler_loop(
    app: FastAPI,
    stop_event: threading.Event,
    wake_event: asyncio.Event,
) -> None:
    """每轮在线程中处理一条，避免 SQLite/HTTP 同步恢复阻塞 ASGI event loop。"""

    config = app.state.config
    while not stop_event.is_set():
        worked = await asyncio.to_thread(
            run_response_cache_outbox_once,
            config,
            should_stop=stop_event.is_set,
        )
        delay = 0.05 if worked else _poll_seconds(config)
        try:
            await asyncio.wait_for(wake_event.wait(), timeout=delay)
        # 控制面缓存能力必须保持租户、权限、版本和错误合同一致，不能信任客户端归属。
        except TimeoutError:
            # 空队列轮询是正常节拍，不写日志以免长期污染生产诊断。
            continue


def run_response_cache_outbox_once(
    config: AppConfig,
    *,
    should_stop=None,
) -> bool:
    """执行至多一个 source，日志只保留固定领域码而不展开 payload/数据库异常。"""

    try:
        return drain_response_cache_event_once(
            config,
            worker_id=WORKER_ID,
            should_stop=should_stop,
        )
    # 控制面缓存能力必须保持租户、权限、版本和错误合同一致，不能信任客户端归属。
    except ApiError as exc:
        logger.warning(
            "response cache outbox iteration failed",
            extra={"event": "response_cache.outbox_failed", "error_code": exc.code},
        )
        return False
    except Exception:
        logger.warning(
            "response cache outbox iteration failed",
            extra={
                "event": "response_cache.outbox_failed",
                "error_code": "response_cache.unavailable",
            },
        )
        return False


def _enabled(config: AppConfig) -> bool:
    """旧配置对象无专用开关时默认恢复 source，避免升级后 pending 永久滞留。"""

    value = getattr(config, "response_cache_outbox_enabled", True)
    return value if type(value) is bool else True


def _poll_seconds(config: AppConfig) -> float:
    """将可选部署节拍限制在 0.1..300 秒，bool 不得冒充数字。"""

    value = getattr(
        config,
        "response_cache_outbox_poll_interval_seconds",
        DEFAULT_POLL_SECONDS,
    )
    if type(value) not in {int, float} or isinstance(value, bool):
        return DEFAULT_POLL_SECONDS
    return min(max(float(value), 0.1), 300.0)


__all__ = [
    "install_response_cache_outbox_scheduler",
    "run_response_cache_outbox_once",
]
