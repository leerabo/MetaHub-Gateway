"""PRD-079 试用到期与额度事件恢复调度器。

调度循环只处理服务端持有的租户事实，并以有界批次和固定错误码维持故障隔离。
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

from .outbox import drain_quota_event_once
from .service import expire_due_trials

logger = logging.getLogger("metahub.control.plan_billing.scheduler")
# 实例与批次常量约束后台任务的唯一 owner、轮询节奏和单轮数据库负载。
_INSTANCE_ID = uuid4().hex
EXPIRY_WORKER_ID = f"quota-expiry-{_INSTANCE_ID}"
EVENT_WORKER_ID = f"quota-event-{_INSTANCE_ID}"
DEFAULT_POLL_SECONDS = 2.0
DEFAULT_BATCH_SIZE = 50


def install_plan_billing_scheduler(app: FastAPI) -> None:
    """把有界试用到期和事件恢复循环装配进既有应用生命周期。"""

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        """随应用启动后台任务，并在退出前等待本实例拥有的循环完成清理。"""

        async with original_lifespan(current_app):
            stop_event = threading.Event()
            tasks: list[asyncio.Task[None]] = []
            if _enabled(current_app.state.config, "plan_expiry_scheduler_enabled", True):
                tasks.append(
                    asyncio.create_task(
                        _expiry_loop(current_app, stop_event),
                        name="plan-trial-expiry-scheduler",
                    )
                )
            if _enabled(current_app.state.config, "quota_outbox_enabled", True):
                tasks.append(
                    asyncio.create_task(
                        _event_loop(current_app, stop_event),
                        name="quota-event-outbox",
                    )
                )
            try:
                yield
            finally:
                stop_event.set()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

    app.router.lifespan_context = combined_lifespan


async def _expiry_loop(app: FastAPI, stop_event: threading.Event) -> None:
    """按配置轮询到期套餐，单轮有工作时缩短下一次等待时间。"""

    config = app.state.config
    while not stop_event.is_set():
        worked = await asyncio.to_thread(run_plan_expiry_once, config)
        delay = 0.05 if worked else _poll(config, "plan_expiry_poll_interval_seconds")
        await _wait(stop_event, delay)


async def _event_loop(app: FastAPI, stop_event: threading.Event) -> None:
    """串行恢复额度事件 source，停止信号会传入网络投递边界。"""

    config = app.state.config
    while not stop_event.is_set():
        worked = await asyncio.to_thread(
            run_quota_event_once,
            config,
            should_stop=stop_event.is_set,
        )
        delay = 0.05 if worked else _poll(config, "quota_outbox_poll_interval_seconds")
        await _wait(stop_event, delay)


def run_plan_expiry_once(config: AppConfig) -> bool:
    """执行一批租户试用到期收敛，日志只保留固定事件和错误码。"""

    try:
        return expire_due_trials(config, limit=_batch_size(config)) > 0
    # 领域错误仅记录固定错误码并保持调度循环存活，不向日志复制内部异常正文。
    except ApiError as exc:
        _warning("plan.expiry_failed", exc.code)
        return False
    except Exception:
        _warning("plan.expiry_failed", "plan.unavailable")
        return False


def run_quota_event_once(config: AppConfig, *, should_stop=None) -> bool:
    """最多恢复一个额度事件 source，并把停止检查交给投递租约流程。"""

    try:
        return drain_quota_event_once(
            config,
            worker_id=EVENT_WORKER_ID,
            should_stop=should_stop,
        )
    # 投递领域错误只形成固定告警，避免单个租户事件终止全局恢复循环。
    except ApiError as exc:
        _warning("quota.outbox_failed", exc.code)
        return False
    except Exception:
        _warning("quota.outbox_failed", "quota.unavailable")
        return False


async def _wait(stop_event: threading.Event, delay: float) -> None:
    """以短分片等待轮询期限，使应用停止信号能够及时终止后台循环。"""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + delay
    while not stop_event.is_set() and loop.time() < deadline:
        await asyncio.sleep(min(0.05, max(0.0, deadline - loop.time())))


def _enabled(config: AppConfig, name: str, default: bool) -> bool:
    """读取严格布尔调度开关，类型异常时使用受控默认值。"""

    value = getattr(config, name, default)
    return value if type(value) is bool else default


def _poll(config: AppConfig, name: str) -> float:
    """读取并夹紧轮询秒数，拒绝布尔值冒充数值配置。"""

    value = getattr(config, name, DEFAULT_POLL_SECONDS)
    if type(value) not in {int, float} or isinstance(value, bool):
        return DEFAULT_POLL_SECONDS
    return min(max(float(value), 0.1), 300.0)


def _batch_size(config: AppConfig) -> int:
    """读取并夹紧单轮到期批量，避免配置错误放大数据库锁占用。"""

    value = getattr(config, "plan_expiry_batch_size", DEFAULT_BATCH_SIZE)
    if type(value) is not int:
        return DEFAULT_BATCH_SIZE
    return min(max(value, 1), 200)


def _warning(event: str, error_code: str) -> None:
    """记录不含租户正文和原始异常的固定后台恢复告警。"""

    logger.warning(
        "plan billing recovery iteration failed",
        extra={"event": event, "error_code": error_code},
    )


__all__ = [
    "install_plan_billing_scheduler",
    "run_plan_expiry_once",
    "run_quota_event_once",
]
