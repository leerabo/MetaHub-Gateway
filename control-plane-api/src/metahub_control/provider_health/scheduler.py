"""Provider 健康周期调度器。

调度只存在于控制面进程，数据面热路径永不现场探测。生命周期安装函数会组合而非覆盖
FastAPI 原有 lifespan，并在关闭时先停止领取、再有界等待网络线程。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI

from metahub_shared.runtime.errors import ApiError

from .service import claim_next_scheduled, probe_provider_for_tenant

logger = logging.getLogger("metahub.control.provider_health.scheduler")


def install_provider_health_scheduler(app: FastAPI) -> None:
    """把健康调度组合进现有应用生命周期，保证测试和后续模块的 lifespan 不丢失。"""

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        """先启动原生命周期，再按配置启动一个可关闭的调度任务。"""

        async with original_lifespan(current_app):
            stop_event = asyncio.Event()
            task: asyncio.Task[None] | None = None
            # 调度开关只控制后台领取，新 GET/POST API 始终保留，便于人工排障。
            if current_app.state.config.provider_health_enabled:
                task = asyncio.create_task(
                    _scheduler_loop(current_app, stop_event),
                    name="provider-health-scheduler",
                )
            try:
                yield
            finally:
                stop_event.set()
                if task is not None:
                    timeout = current_app.state.config.provider_health_timeout_seconds + 2
                    try:
                        await asyncio.wait_for(task, timeout=timeout)
                    # 网络线程无法被强制取消，超时后只取消 awaiter 并完成任务对象回收。
                    except TimeoutError:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

    app.router.lifespan_context = combined_lifespan


async def _scheduler_loop(app: FastAPI, stop_event: asyncio.Event) -> None:
    """串行领取到期 Provider；默认单并发降低 SQLite 写锁争用。"""

    config = app.state.config
    while not stop_event.is_set():
        worked = await asyncio.to_thread(run_scheduled_once, config)
        delay = 0.1 if worked else min(5, config.provider_health_interval_seconds)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        # 等待超时表示进入下一轮领取，不属于探测失败或错误日志事件。
        except TimeoutError:
            continue


def run_scheduled_once(config) -> bool:
    """执行一条到期探测，供调度循环和集成测试直接调用。"""

    target = claim_next_scheduled(config)
    if target is None:
        return False
    tenant_id, provider_id = target
    request_id = f"req_health_{uuid4().hex}"
    trace_id = f"trace_health_{uuid4().hex}"
    try:
        probe_provider_for_tenant(
            config,
            tenant_id,
            provider_id,
            trigger_type="scheduled",
            request_id=request_id,
            trace_id=trace_id,
        )
    # 可预期业务竞争只记录固定错误码，不能把数据库或 HTTP 异常链写入日志。
    except ApiError as exc:
        # 多执行者抢占、配置变化或刚被手工探测都属于正常竞争，不终止调度循环。
        if exc.code not in {
            "provider_health.probe_in_progress",
            "provider_health.probe_not_due",
            "provider_health.probe_superseded",
        }:
            logger.warning(
                "provider health scheduled probe failed",
                extra={"provider_id": provider_id, "tenant_id": tenant_id, "error_code": exc.code},
            )
    except Exception:
        # 原始异常不写入日志，避免 httpx request 中的 Authorization 沿异常链泄露。
        logger.warning(
            "provider health scheduled probe failed",
            extra={"provider_id": provider_id, "tenant_id": tenant_id, "error_code": "infra.unhandled"},
        )
    return True
