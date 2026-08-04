"""PRD-056 error_map.updated Source Outbox 的可关闭后台调度器。

调度器只安装在 Control API lifespan，Gateway 热路径不会扫描控制面事件。每轮在线程中处理
至多一条 source event；停服设置 stop 后等待当前有界投递，并以固定上限结束生命周期。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .outbox import drain_provider_error_map_event_once

logger = logging.getLogger("metahub.control.error_maps.scheduler")
# 进程级 worker ID、空队列轮询节拍与停服硬时限共同定义调度生命周期；
# 日志只记录固定事件和错误码，不记录数据库路径、Endpoint、Token 或响应正文。
WORKER_ID = f"provider-error-map-{uuid4().hex}"
DEFAULT_POLL_SECONDS = 2.0
SHUTDOWN_TIMEOUT_SECONDS = 10.0


def install_provider_error_map_scheduler(app: FastAPI) -> None:
    """把 source outbox worker 组合进已有 lifespan，不覆盖其他后台任务。"""

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        """进入原生命周期后启动 worker，退出时执行有界等待和清理。"""

        async with original_lifespan(current_app):
            stop_event = asyncio.Event()
            task: asyncio.Task[None] | None = None
            if _enabled(current_app.state.config):
                task = asyncio.create_task(
                    _scheduler_loop(current_app, stop_event),
                    name="provider-error-map-event-scheduler",
                )
            try:
                yield
            finally:
                stop_event.set()
                if task is not None:
                    try:
                        # dispatcher 自身已有网络超时；此上限还防止损坏测试桩或底层库
                        # 让停服无界等待。
                        await asyncio.wait_for(
                            asyncio.shield(task), timeout=SHUTDOWN_TIMEOUT_SECONDS
                        )
                    # 停服超过硬时限时取消 worker，避免损坏 dispatcher 让 Control 永久无法退出。
                    except TimeoutError:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

    app.router.lifespan_context = combined_lifespan


async def _scheduler_loop(app: FastAPI, stop_event: asyncio.Event) -> None:
    """串行清空积压；空队列时按有界部署间隔等待。"""

    config = app.state.config
    while not stop_event.is_set():
        worked = await asyncio.to_thread(run_provider_error_map_event_once, config)
        delay = 0.05 if worked else _poll_seconds(config)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        # 轮询超时是正常节拍，不记录 warning，避免空 Outbox 污染生产日志。
        except TimeoutError:
            # 正常轮询节拍不记录 warning，避免空 outbox 污染生产日志。
            continue


def run_provider_error_map_event_once(config: AppConfig) -> bool:
    """执行最多一个 source event，供 scheduler 与运行验收同步调用。"""

    try:
        return drain_provider_error_map_event_once(config, worker_id=WORKER_ID)
    # 已知 Outbox 失败只记录统一 code，不展开可能携带数据库或订阅信息的异常正文。
    except ApiError as exc:
        logger.warning(
            "provider error map event iteration failed",
            extra={"event": "error_map.outbox_failed", "error_code": exc.code},
        )
        return False
    except Exception:
        # 未知异常可能含数据库路径、Endpoint 或响应正文，日志只保留固定领域码。
        logger.warning(
            "provider error map event iteration failed",
            extra={
                "event": "error_map.outbox_failed",
                "error_code": "error_map.event_unavailable",
            },
        )
        return False


def _enabled(config: AppConfig) -> bool:
    """读取可选部署开关；旧 AppConfig 没有该字段时保持事件恢复默认开启。"""

    value = getattr(config, "provider_error_map_event_enabled", True)
    return value if type(value) is bool else True


def _poll_seconds(config: AppConfig) -> float:
    """读取可选轮询间隔，并把异常构造的配置限制在 0.1..300 秒。"""

    value = getattr(
        config,
        "provider_error_map_event_poll_interval_seconds",
        DEFAULT_POLL_SECONDS,
    )
    if type(value) not in {int, float} or isinstance(value, bool):
        return DEFAULT_POLL_SECONDS
    return min(max(float(value), 0.1), 300.0)


__all__ = [
    "install_provider_error_map_scheduler",
    "run_provider_error_map_event_once",
]
