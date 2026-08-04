"""PRD-055 调用日志查询事件 outbox 的可关闭后台调度器。

调度器只安装在 Control API lifespan；Gateway 热路径不会扫描 QueryEvent。每轮在线程中
领取并处理至多一条 source event，SQLite 连接不跨 thread/await 共享。服务关闭时等待
已经进入同步数据库或网络阶段的当前 tick 自然完成，避免退出后仍写库。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .outbox import drain_request_log_query_event_once

logger = logging.getLogger("metahub.control.request_logs.scheduler")
# 进程级 ID 只用于 lease owner/CAS；重启后改变，旧进程的过期 lease 才能被安全接管。
WORKER_ID = f"request-log-query-{uuid4().hex}"


def install_request_log_query_event_scheduler(app: FastAPI) -> None:
    """把查询事件 worker 组合进已有 lifespan，不覆盖其他后台任务。

    参数：已创建且 ``state.config`` 为 AppConfig 的 Control API 应用。
    返回：无；函数原地组合应用生命周期。部署开关关闭时不创建后台 task。
    """

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        """进入原生命周期后启动任务，并在退出前等待当前同步 tick 完成。"""

        async with original_lifespan(current_app):
            stop_event = asyncio.Event()
            task: asyncio.Task[None] | None = None
            if current_app.state.config.request_log_query_event_enabled:
                task = asyncio.create_task(
                    _scheduler_loop(current_app, stop_event),
                    name="request-log-query-event-scheduler",
                )
            try:
                yield
            finally:
                stop_event.set()
                if task is not None:
                    # to_thread 中的 SQLite/网络工作不能靠 cancel 安全终止，否则会遗留未知
                    # source 状态；等待当前 tick 后再退出，下一次启动可继续处理 pending。
                    await asyncio.gather(task, return_exceptions=True)

    app.router.lifespan_context = combined_lifespan


async def _scheduler_loop(app: FastAPI, stop_event: asyncio.Event) -> None:
    """串行清空积压；无工作时按部署级有界间隔等待。"""

    config = app.state.config
    while not stop_event.is_set():
        worked = await asyncio.to_thread(run_request_log_query_event_once, config)
        delay = 0.05 if worked else config.request_log_query_event_poll_interval_seconds
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        # 到时唤醒是正常轮询节拍，不应污染 outbox 故障告警或触发重试逻辑。
        except TimeoutError:
            # 正常轮询超时不是投递错误，禁止空 outbox 持续制造 warning 和告警噪声。
            continue


def run_request_log_query_event_once(config: AppConfig) -> bool:
    """执行一个 source event，供 scheduler 与实际运行验收同步调用。"""

    try:
        return drain_request_log_query_event_once(config, worker_id=WORKER_ID)
    # 领域异常只允许记录稳定 code，数据库路径、filters、URL 和远端正文不得进入日志。
    except ApiError as exc:
        # 领域错误日志只保留稳定 code；数据库路径、URL、filters 与远端正文不得进入日志。
        logger.warning(
            "request log query event iteration failed",
            extra={
                "event": "request_log.query_outbox_failed",
                "error_code": exc.code,
            },
        )
        return False
    except Exception:
        # 未知异常可能包含数据库或网络细节，日志统一折叠为固定领域 code。
        logger.warning(
            "request log query event iteration failed",
            extra={
                "event": "request_log.query_outbox_failed",
                "error_code": "request_log.query_event_unavailable",
            },
        )
        return False


__all__ = [
    "install_request_log_query_event_scheduler",
    "run_request_log_query_event_once",
]
