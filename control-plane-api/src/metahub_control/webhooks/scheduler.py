"""Webhook 恢复调度：按租约投递待处理事件，失败可重试且日志不得包含业务载荷或凭据。

Control webhook scheduler for replay recovery and Batch source dispatch.

The scheduler runs only in the Control API lifespan. Synchronous HTTP and SQLite work stays
off the async event loop and never enters the Gateway model-call hot path.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import uuid4

from fastapi import FastAPI
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from ..batch_jobs.outbox import drain_batch_event_once
from .outbox import claim_next_replay, expire_dead_letters, process_claimed_replay

logger = logging.getLogger("metahub.control.webhooks.scheduler")
# owner 仅用于数据库 CAS，不包含租户、用户或主机敏感信息。
WORKER_ID = f"webhook-replay-{uuid4().hex}"
BATCH_EVENT_WORKER_ID = f"batch-event-{uuid4().hex}"
POLL_INTERVAL_SECONDS = 2.0


def install_webhook_replay_scheduler(app: FastAPI) -> None:
    """安装可停止的 Webhook 恢复调度器，并确保应用退出时回收后台任务。

    Compose replay and Batch source dispatch into the existing Control lifespan.
    """

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        """先进入已有生命周期，再启动一个可等待当前同步 tick 的串行任务。"""

        async with original_lifespan(current_app):
            stop_event = threading.Event()
            wake_event = asyncio.Event()
            task = asyncio.create_task(
                _scheduler_loop(current_app, stop_event, wake_event),
                name="webhook-replay-scheduler",
            )
            try:
                yield
            finally:
                # to_thread 不能被可靠取消；等待当前 tick 自然结束，避免服务退出后仍写数据库。
                stop_event.set()
                wake_event.set()
                await asyncio.gather(task, return_exceptions=True)

    app.router.lifespan_context = combined_lifespan


async def _scheduler_loop(
    app: FastAPI,
    stop_event: threading.Event,
    wake_event: asyncio.Event,
) -> None:
    """有积压时快速继续，空闲时按固定两秒间隔轮询。"""

    while not stop_event.is_set():
        worked = await asyncio.to_thread(
            run_webhook_replay_once,
            app.state.config,
            should_stop=stop_event.is_set,
        )
        delay = 0.05 if worked else POLL_INTERVAL_SECONDS
        try:
            await asyncio.wait_for(wake_event.wait(), timeout=delay)
        # 轮询等待到期是正常唤醒信号，不应升级为错误日志或中断持久 replay 恢复。
        except TimeoutError:
            # 超时只是正常轮询唤醒，不记录成业务故障。
            continue


def run_webhook_replay_once(
    config: AppConfig,
    *,
    now: datetime | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """领取并投递一条通用 Webhook 事件，租约丢失或停止请求时保留可恢复状态。

    Drain one Batch source event, expire dead letters, and process at most one replay.
    """

    batch_worked = run_batch_event_once(
        config,
        now=now,
        should_stop=should_stop,
    )
    if should_stop is not None and should_stop():
        return batch_worked
    try:
        expired_count = expire_dead_letters(config, now=now)
        claim = claim_next_replay(config, worker_id=WORKER_ID, now=now)
        if claim is None:
            return batch_worked or expired_count > 0
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
        return batch_worked
    except Exception:
        # 未知异常只记录固定分类，不序列化异常正文、payload、URL、Secret、token 或 owner。
        logger.warning(
            "webhook replay scheduler iteration failed",
            extra={
                "event": "webhook.replay_scheduler_failed",
                "error_code": "webhook.replay_unavailable",
            },
        )
        return batch_worked


def run_batch_event_once(
    config: AppConfig,
    *,
    now: datetime | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """领取并投递一条批任务源事件，严格复用租约与失败重试边界。

    Drain one Batch source event in the Control webhook process.
    """

    if should_stop is not None and should_stop():
        return False
    try:
        return drain_batch_event_once(
            config,
            worker_id=BATCH_EVENT_WORKER_ID,
            now=now,
            should_stop=should_stop,
        )
    # 单次投递失败按稳定错误处理；不得泄露事件载荷，也不能破坏后续租约恢复。
    except ApiError as exc:
        logger.warning(
            "batch webhook source iteration failed",
            extra={
                "event": "batch.event_dispatch_failed",
                "error_code": exc.code,
            },
        )
        return False
    except Exception:
        logger.warning(
            "batch webhook source iteration failed",
            extra={
                "event": "batch.event_dispatch_failed",
                "error_code": "batch.worker_unavailable",
            },
        )
        return False


__all__ = [
    "install_webhook_replay_scheduler",
    "run_batch_event_once",
    "run_webhook_replay_once",
]
