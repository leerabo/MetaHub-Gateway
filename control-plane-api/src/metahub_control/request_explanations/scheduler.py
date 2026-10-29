"""PRD-054 请求解释查看事件 outbox 的可关闭后台调度器。

调度器只安装在 Control API lifespan；Gateway 热路径不会扫描 RequestExplanation。每轮在线程
中领取并处理至多一条 source event，SQLite 连接不跨 thread/await 共享。关闭时等待已进入
同步数据库阶段的当前 tick 自然完成，避免服务退出后仍写库。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .outbox import drain_request_explanation_event_once

logger = logging.getLogger("metahub.control.request_explanations.scheduler")
# 进程级 worker ID 只用于 lease owner/CAS，重启后变化可让过期租约被新进程安全接管。
WORKER_ID = f"request-explanation-{uuid4().hex}"


def install_request_explanation_scheduler(app: FastAPI) -> None:
    """把 outbox worker 组合进已有 lifespan，不覆盖其他后台任务。"""

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        """进入原生命周期后按独立部署开关启动可等待的 outbox task。"""

        async with original_lifespan(current_app):
            stop_event = asyncio.Event()
            task: asyncio.Task[None] | None = None
            if current_app.state.config.request_explanation_event_enabled:
                task = asyncio.create_task(
                    _scheduler_loop(current_app, stop_event),
                    name="request-explanation-event-scheduler",
                )
            try:
                yield
            finally:
                stop_event.set()
                if task is not None:
                    # to_thread 中的 SQLite/网络工作不能靠 cancel 安全终止，必须等待本轮结束。
                    await asyncio.gather(task, return_exceptions=True)

    app.router.lifespan_context = combined_lifespan


async def _scheduler_loop(app: FastAPI, stop_event: asyncio.Event) -> None:
    """串行清空积压；无工作时按部署级有界间隔等待。"""

    config = app.state.config
    while not stop_event.is_set():
        worked = await asyncio.to_thread(run_request_explanation_event_once, config)
        delay = 0.05 if worked else config.request_explanation_event_poll_interval_seconds
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        # 超时只是正常轮询节拍，不是投递错误；禁止把空队列持续写成告警。
        except TimeoutError:
            # 正常轮询唤醒不记录 warning，避免空 outbox 污染日志和告警。
            continue


def run_request_explanation_event_once(config: AppConfig) -> bool:
    """执行一个 source event，供 scheduler 与实际运行验收同步调用。"""

    try:
        return drain_request_explanation_event_once(config, worker_id=WORKER_ID)
    # 领域错误只记录稳定 code，数据库路径、URL 和远端响应不得进入日志。
    except ApiError as exc:
        logger.warning(
            "request explanation event iteration failed",
            extra={
                "event": "request_explanation.outbox_failed",
                "error_code": exc.code,
            },
        )
        return False
    except Exception:
        # 未知异常正文可能含数据库或网络信息，日志只保留固定事件和领域 code。
        logger.warning(
            "request explanation event iteration failed",
            extra={
                "event": "request_explanation.outbox_failed",
                "error_code": "request_explanation.event_unavailable",
            },
        )
        return False


__all__ = [
    "install_request_explanation_scheduler",
    "run_request_explanation_event_once",
]
