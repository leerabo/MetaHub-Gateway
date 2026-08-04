"""PRD-057 死信过期与持久 replay 恢复调度器。

调度器只安装在 Control API lifespan，每轮至多处理一条 replay；同步 HTTP 与 SQLite 工作
在线程中执行，不阻塞异步事件循环，也不进入 Gateway 模型调用热路径。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import uuid4

from fastapi import FastAPI
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .outbox import claim_next_replay, expire_dead_letters, process_claimed_replay

logger = logging.getLogger("metahub.control.webhooks.scheduler")
# owner 仅用于数据库 CAS，不包含租户、用户或主机敏感信息。
WORKER_ID = f"webhook-replay-{uuid4().hex}"
POLL_INTERVAL_SECONDS = 2.0


def install_webhook_replay_scheduler(app: FastAPI) -> None:
    """把死信 worker 组合进现有 lifespan，不覆盖健康检查和其他后台任务。"""

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        """先进入已有生命周期，再启动一个可等待当前同步 tick 的串行任务。"""

        async with original_lifespan(current_app):
            stop_event = asyncio.Event()
            task = asyncio.create_task(
                _scheduler_loop(current_app, stop_event),
                name="webhook-replay-scheduler",
            )
            try:
                yield
            finally:
                # to_thread 不能被可靠取消；等待当前 tick 自然结束，避免服务退出后仍写数据库。
                stop_event.set()
                await asyncio.gather(task, return_exceptions=True)

    app.router.lifespan_context = combined_lifespan


async def _scheduler_loop(app: FastAPI, stop_event: asyncio.Event) -> None:
    """有积压时快速继续，空闲时按固定两秒间隔轮询。"""

    while not stop_event.is_set():
        worked = await asyncio.to_thread(run_webhook_replay_once, app.state.config)
        delay = 0.05 if worked else POLL_INTERVAL_SECONDS
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        # 轮询等待到期是正常唤醒信号，不应升级为错误日志或中断持久 replay 恢复。
        except TimeoutError:
            # 超时只是正常轮询唤醒，不记录成业务故障。
            continue


def run_webhook_replay_once(
    config: AppConfig,
    *,
    now: datetime | None = None,
) -> bool:
    """过期到期死信并处理至多一个 replay，供 scheduler 与专项验收直接调用。"""

    try:
        expired_count = expire_dead_letters(config, now=now)
        claim = claim_next_replay(config, worker_id=WORKER_ID, now=now)
        if claim is None:
            return expired_count > 0
        process_claimed_replay(config, claim, now=now)
        return True
    # 已统一分类的 replay/数据库失败只记录固定 code，下一轮仍可继续处理其他租户任务。
    except ApiError as exc:
        logger.warning(
            "webhook replay scheduler iteration failed",
            extra={
                "event": "webhook.replay_scheduler_failed",
                "error_code": exc.code,
            },
        )
        return False
    except Exception:
        # 未知异常只记录固定分类，不序列化异常正文、payload、URL、Secret、token 或 owner。
        logger.warning(
            "webhook replay scheduler iteration failed",
            extra={
                "event": "webhook.replay_scheduler_failed",
                "error_code": "webhook.replay_unavailable",
            },
        )
        return False


__all__ = ["install_webhook_replay_scheduler", "run_webhook_replay_once"]
