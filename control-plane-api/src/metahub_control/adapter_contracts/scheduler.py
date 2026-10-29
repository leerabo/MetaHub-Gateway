"""PRD-064 Adapter 合同失败事件 Source Outbox 后台调度器。

调度器只安装在 Control API 生命周期，每轮在线程中处理一条 source，避免同步 SQLite 和
Webhook I/O 阻塞异步管理接口。空队列使用固定间隔，不形成全天候外部 Provider 监控。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .outbox import drain_adapter_contract_event_once


logger = logging.getLogger("metahub.control.adapter_contracts.scheduler")
# Worker 身份用于 lease 所有权，轮询与停机上限共同约束后台恢复任务的资源占用。
WORKER_ID = f"adapter-contract-{uuid4().hex}"
POLL_INTERVAL_SECONDS = 2.0
SHUTDOWN_TIMEOUT_SECONDS = 10.0


def install_adapter_contract_event_scheduler(app: FastAPI) -> None:
    """把失败事件恢复 worker 组合进已有 lifespan，不覆盖其他后台任务。"""

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        """进入已有生命周期后启动串行 worker，停服时执行有界等待。"""

        async with original_lifespan(current_app):
            stop_event = asyncio.Event()
            task = asyncio.create_task(
                _scheduler_loop(current_app, stop_event),
                name="adapter-contract-event-scheduler",
            )
            try:
                yield
            finally:
                stop_event.set()
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=SHUTDOWN_TIMEOUT_SECONDS)
                # 停机超时不能无限阻塞 Control API 生命周期，必须取消尚未退出的调度任务。
                except TimeoutError:
                    # dispatcher 已有网络超时；额外硬上限防止损坏测试桩让 Control 无法退出。
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    app.router.lifespan_context = combined_lifespan


async def _scheduler_loop(app: FastAPI, stop_event: asyncio.Event) -> None:
    """有积压时快速继续，空闲时按两秒节拍等待。"""

    while not stop_event.is_set():
        worked = await asyncio.to_thread(run_adapter_contract_event_once, app.state.config)
        delay = 0.05 if worked else POLL_INTERVAL_SECONDS
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        # 轮询等待到期是正常调度信号，不应作为错误写入日志或改变 Outbox 状态。
        except TimeoutError:
            # 正常轮询唤醒不写日志，避免空队列污染生产观测。
            continue


def run_adapter_contract_event_once(config: AppConfig) -> bool:
    """处理至多一条失败事件，供 scheduler 与专项运行验收直接调用。"""

    try:
        return drain_adapter_contract_event_once(config, worker_id=WORKER_ID)
    # 已归一化领域错误只记录固定错误码，避免数据库或投递细节进入日志。
    except ApiError as exc:
        logger.warning(
            "adapter contract event iteration failed",
            extra={"event": "adapter.contract_outbox_failed", "error_code": exc.code},
        )
        return False
    except Exception:
        # 未知异常可能含 Endpoint、Token 或响应正文，日志只记录固定安全分类。
        logger.warning(
            "adapter contract event iteration failed",
            extra={
                "event": "adapter.contract_outbox_failed",
                "error_code": "adapter.contract_storage_unavailable",
            },
        )
        return False


__all__ = [
    "install_adapter_contract_event_scheduler",
    "run_adapter_contract_event_once",
]
