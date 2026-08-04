"""PRD-053 成本异常检测与 Webhook outbox 的可关闭后台调度器。

调度器只安装在 Control API lifespan；Gateway 热路径不会扫描正式账本。每一轮在线程中
领取并处理至多一个检测窗口和一个 source event，SQLite 连接不跨 thread/await 共享。
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

from .detector import claim_next_detection, process_claimed_detection
from .outbox import drain_cost_anomaly_event_once

logger = logging.getLogger("metahub.control.cost_anomalies.scheduler")
# 每个进程使用不可预测且不含租户信息的 worker 标识，供检测租约和 outbox CAS 区分所有者。
WORKER_ID = f"cost-anomaly-{uuid4().hex}"


def install_cost_anomaly_scheduler(app: FastAPI) -> None:
    """把检测/outbox worker 组合进现有 lifespan，不覆盖其他后台任务。"""

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        """先进入原生命周期，再启动一个可等待当前数据库 tick 的串行治理任务。"""

        async with original_lifespan(current_app):
            stop_event = asyncio.Event()
            task: asyncio.Task[None] | None = None
            # 部署开关只决定是否启动后台治理任务，不改变 Gateway 请求主链路。
            if current_app.state.config.cost_anomaly_enabled:
                task = asyncio.create_task(
                    _scheduler_loop(current_app, stop_event),
                    name="cost-anomaly-scheduler",
                )
            try:
                yield
            finally:
                if task is not None:
                    await _stop_scheduler_task(stop_event, task)
                else:
                    stop_event.set()

    app.router.lifespan_context = combined_lifespan


async def _stop_scheduler_task(
    stop_event: asyncio.Event,
    task: asyncio.Task[None],
) -> None:
    """通知调度器停止，并等待已经进入同步数据库阶段的当前 tick 自然完成。

    `asyncio.to_thread` 背后的线程不能由 `task.cancel()` 终止；提前返回 lifespan 会让服务
    看似关闭后仍继续写库。因此这里不设虚假的五秒超时，也不主动取消活跃 worker。
    """

    stop_event.set()
    await asyncio.gather(task, return_exceptions=True)


async def _scheduler_loop(app: FastAPI, stop_event: asyncio.Event) -> None:
    """按部署间隔串行迭代；有工作时短暂让出执行权后继续清空积压。"""

    config = app.state.config
    while not stop_event.is_set():
        worked = await asyncio.to_thread(run_cost_anomaly_once, config)
        delay = 0.05 if worked else config.cost_anomaly_poll_interval_seconds
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        # 轮询等待到期属于正常唤醒，不记录错误，也不改变当前 worker 的租约语义。
        except TimeoutError:
            continue


def run_cost_anomaly_once(
    config: AppConfig,
    *,
    now: datetime | None = None,
) -> bool:
    """执行一个检测窗口和一个 outbox 事件，供 scheduler 与 Runtime 验收调用。"""

    worked = run_cost_anomaly_detection_once(config, now=now)
    try:
        event_worked = drain_cost_anomaly_event_once(
            config,
            worker_id=WORKER_ID,
            now=now,
        )
    # outbox 失败不得阻断检测轮次，日志仅记录统一 code，禁止写入事件 payload。
    except ApiError as exc:
        logger.warning(
            "cost anomaly outbox iteration failed",
            extra={
                "event": "cost_anomaly.outbox_failed",
                "error_code": exc.code,
            },
        )
        event_worked = False
    except Exception:
        logger.warning(
            "cost anomaly outbox iteration failed",
            extra={
                "event": "cost_anomaly.outbox_failed",
                "error_code": "cost_anomaly.detector_unavailable",
            },
        )
        event_worked = False
    return worked or event_worked


def run_cost_anomaly_detection_once(
    config: AppConfig,
    *,
    now: datetime | None = None,
) -> bool:
    """领取并完成一个检测窗口；固定日志不包含账本值、SQL 或异常正文。"""

    try:
        claim = claim_next_detection(config, worker_id=WORKER_ID, now=now)
        if claim is None:
            return False
        process_claimed_detection(config, claim, now=now)
        return True
    # 检测领取或完成失败不能影响业务流量，日志只保留固定错误码和事件名。
    except ApiError as exc:
        logger.warning(
            "cost anomaly detector iteration failed",
            extra={
                "event": "cost_anomaly.detector_failed",
                "error_code": exc.code,
            },
        )
        return False
    except Exception:
        logger.warning(
            "cost anomaly detector iteration failed",
            extra={
                "event": "cost_anomaly.detector_failed",
                "error_code": "cost_anomaly.detector_unavailable",
            },
        )
        return False


__all__ = [
    "install_cost_anomaly_scheduler",
    "run_cost_anomaly_detection_once",
    "run_cost_anomaly_once",
]