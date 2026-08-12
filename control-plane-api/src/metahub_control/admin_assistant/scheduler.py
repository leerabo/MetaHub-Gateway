"""管理助手 source event 的生命周期调度器，负责唤醒、轮询、停止与异常隔离。"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .outbox import drain_assistant_event_once

# worker owner 在进程生命周期内稳定且不含租户或凭据；日志只记录固定 event/error code。
logger = logging.getLogger("metahub.control.admin_assistant.scheduler")
# worker ID 仅作为 outbox 租约 CAS owner，随机值不包含租户、Session 或任何凭据。
WORKER_ID = f"admin-assistant-{uuid4().hex}"


def install_admin_assistant_scheduler(app: FastAPI) -> None:
    """组合既有 lifespan，在配置开启时启动且在应用退出时完整回收后台任务。"""

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        """保留原 lifespan 顺序，并把 wake event 与 loop owner 限定在当前应用实例。"""

        async with original_lifespan(current_app):
            # 同一 stop primitive 必须能被 event loop 与 asyncio.to_thread 中的
            # 同步 dispatcher 读取。
            stop_event = threading.Event()
            wake_event = asyncio.Event()
            owner_loop = asyncio.get_running_loop()
            from .service import (
                clear_admin_assistant_execution_runtime,
                initialize_admin_assistant_execution_runtime,
            )

            try:
                await initialize_admin_assistant_execution_runtime(current_app)
            except BaseException:
                # ready 前失败也必须移除当前 loop runtime，不能给后续 lifespan 留 closing owner。
                await clear_admin_assistant_execution_runtime(current_app, owner_loop)
                raise
            current_app.state.admin_assistant_wake_event = wake_event
            current_app.state.admin_assistant_event_loop = owner_loop
            task: asyncio.Task[None] | None = None
            if current_app.state.config.admin_assistant_event_enabled:
                task = asyncio.create_task(
                    _scheduler_loop(current_app, stop_event, wake_event),
                    name="admin-assistant-event-scheduler",
                )
            try:
                yield
            finally:
                stop_event.set()
                wake_event.set()
                interrupted: asyncio.CancelledError | None = None
                if task is not None:
                    interrupted = await _await_lifespan_task(
                        task,
                        suppress_exception=True,
                    )
                current_app.state.admin_assistant_wake_event = None
                current_app.state.admin_assistant_event_loop = None
                # app 可被测试或嵌入式宿主在新 loop 中重用；退出时显式移除旧 loop 原语。
                cleanup = asyncio.create_task(
                    clear_admin_assistant_execution_runtime(current_app, owner_loop)
                )
                cleanup_interrupted = await _await_lifespan_task(
                    cleanup,
                    suppress_exception=False,
                )
                if cleanup_interrupted is not None:
                    interrupted = cleanup_interrupted
                if interrupted is not None:
                    raise interrupted

    app.router.lifespan_context = combined_lifespan


async def _await_lifespan_task(
    task: asyncio.Task[object],
    *,
    suppress_exception: bool,
) -> asyncio.CancelledError | None:
    """屏蔽宿主取消直到 shutdown task 真正退出，并消费其固定异常。"""

    interrupted: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        # 宿主取消只被记下，必须等后台线程与数据库 runtime 真正退出后再向上传播。
        except asyncio.CancelledError as exc:
            if not task.done():
                interrupted = exc
        except BaseException:
            break
    if not task.cancelled():
        try:
            task.result()
        except BaseException:
            if not suppress_exception:
                raise
    return interrupted


def wake_admin_assistant_scheduler(app: FastAPI) -> None:
    """从同步提交路径线程安全唤醒当前应用 worker，不携带问题或事件正文。"""

    event = getattr(app.state, "admin_assistant_wake_event", None)
    loop = getattr(app.state, "admin_assistant_event_loop", None)
    if event is not None and loop is not None and not loop.is_closed():
        try:
            loop.call_soon_threadsafe(event.set)
        # loop 可在 is_closed 预检后关闭；提交后的 wake 失败不能反向改写业务响应。
        except RuntimeError:
            return


async def _scheduler_loop(
    app: FastAPI,
    stop_event: threading.Event,
    wake_event: asyncio.Event,
) -> None:
    """每轮最多 drain 一个 source；有工作立即续跑，无工作等待配置间隔或提交唤醒。"""

    config = app.state.config
    while not stop_event.is_set():
        worked = await asyncio.to_thread(
            run_admin_assistant_event_once,
            config,
            stop_event=stop_event,
        )
        if stop_event.is_set():
            break
        if worked:
            continue
        wake_event.clear()
        try:
            await asyncio.wait_for(
                wake_event.wait(),
                timeout=config.admin_assistant_event_poll_interval_seconds,
            )
        # poll timeout 只是正常空闲节拍，不记录错误也不改变任何 source 状态。
        except TimeoutError:
            continue


def run_admin_assistant_event_once(
    config: AppConfig,
    *,
    stop_event: threading.Event | None = None,
) -> bool:
    """执行单轮 source drain，并把全部异常隔离为无敏感正文的结构化 warning。"""

    try:
        return drain_assistant_event_once(
            config,
            worker_id=WORKER_ID,
            should_stop=stop_event.is_set if stop_event is not None else None,
        )
    # 已归一领域错误只记录机器码，数据库、Endpoint 或密钥细节均不进入日志。
    except ApiError as exc:
        logger.warning(
            "admin assistant event iteration failed",
            extra={"event": "admin_assistant.outbox_failed", "error_code": exc.code},
        )
        return False
    except Exception:
        logger.warning(
            "admin assistant event iteration failed",
            extra={
                "event": "admin_assistant.outbox_failed",
                "error_code": "admin_assistant.event_unavailable",
            },
        )
        return False


__all__ = [
    "install_admin_assistant_scheduler",
    "run_admin_assistant_event_once",
    "wake_admin_assistant_scheduler",
]
