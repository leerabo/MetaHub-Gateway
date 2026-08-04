"""`audit.exported` source outbox 的启动恢复、周期扫描与有界停服调度器。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .export_outbox import drain_audit_export_event_once

logger = logging.getLogger("metahub.control.audit.export_scheduler")
# 进程级 owner、空队列轮询、停服硬时限和唤醒状态名共同定义 worker 生命周期。
WORKER_ID = f"audit-export-{uuid4().hex}"
DEFAULT_POLL_SECONDS = 2.0
SHUTDOWN_TIMEOUT_SECONDS = 10.0
_WAKE_STATE_NAME = "audit_export_source_wake_event"


def install_audit_export_scheduler(app: FastAPI) -> None:
    """把 source worker 组合进已有 FastAPI lifespan，不覆盖其他后台任务。"""

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        """进入原生命周期后启动恢复 worker，退出时停止领取并有界等待。"""

        async with original_lifespan(current_app):
            stop_event = asyncio.Event()
            wake_event = asyncio.Event()
            setattr(current_app.state, _WAKE_STATE_NAME, wake_event)
            task: asyncio.Task[None] | None = None
            if _enabled(current_app.state.config):
                task = asyncio.create_task(
                    _scheduler_loop(current_app, stop_event, wake_event),
                    name="audit-export-source-scheduler",
                )
            try:
                yield
            finally:
                stop_event.set()
                wake_event.set()
                if task is not None:
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(task),
                            timeout=SHUTDOWN_TIMEOUT_SECONDS,
                        )
                    # 超过停服硬时限意味着 dispatcher 或测试桩失控，必须取消而不能永久占端口。
                    except TimeoutError:
                        # Dispatcher 已有网络超时；这里再加硬上限，防止损坏测试桩阻塞停服。
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                if getattr(current_app.state, _WAKE_STATE_NAME, None) is wake_event:
                    delattr(current_app.state, _WAKE_STATE_NAME)

    app.router.lifespan_context = combined_lifespan


def wake_audit_export_scheduler(app: FastAPI) -> None:
    """提示已运行 worker 立即扫描；调用方不会因此取得 lease 或执行网络恢复。"""

    event = getattr(app.state, _WAKE_STATE_NAME, None)
    if isinstance(event, asyncio.Event):
        event.set()


async def _scheduler_loop(
    app: FastAPI,
    stop_event: asyncio.Event,
    wake_event: asyncio.Event,
) -> None:
    """启动即 drain；有积压时快速串行处理，空队列时等待唤醒或轮询。"""

    config = app.state.config
    while not stop_event.is_set():
        worked = await asyncio.to_thread(run_audit_export_event_once, config)
        if worked:
            # 每轮只处理一条，短暂让出事件循环后继续清理积压，避免单租户独占线程。
            await asyncio.sleep(0.05)
            continue
        wake_event.clear()
        try:
            await asyncio.wait_for(wake_event.wait(), timeout=_poll_seconds(config))
        # 等待超时只是周期扫描节拍，不属于系统故障。
        except TimeoutError:
            # 空队列轮询超时是正常节拍，不写 warning 污染生产日志。
            continue


def run_audit_export_event_once(config: AppConfig) -> bool:
    """执行最多一个 source event，供调度器、专项测试和实际运行验收复用。"""

    try:
        return drain_audit_export_event_once(config, worker_id=WORKER_ID)
    # 已知 source 错误只记录固定 code，scheduler 下一轮仍可恢复其他事件。
    except ApiError as exc:
        logger.warning(
            "audit export source iteration failed",
            extra={"event": "audit_export.outbox_failed", "error_code": exc.code},
        )
        return False
    except Exception:
        # 未知异常可能携带数据库路径、Endpoint 或响应正文，只记录固定领域码。
        logger.warning(
            "audit export source iteration failed",
            extra={
                "event": "audit_export.outbox_failed",
                "error_code": "audit_export.unavailable",
            },
        )
        return False


def _enabled(config: AppConfig) -> bool:
    """读取可选部署开关；旧 AppConfig 没有字段时默认恢复 source。"""

    value = getattr(config, "audit_export_event_enabled", True)
    return value if type(value) is bool else True


def _poll_seconds(config: AppConfig) -> float:
    """读取可选轮询间隔，并限制在 0.1..300 秒。"""

    value = getattr(config, "audit_export_event_poll_interval_seconds", DEFAULT_POLL_SECONDS)
    if type(value) not in {int, float} or isinstance(value, bool):
        return DEFAULT_POLL_SECONDS
    return min(max(float(value), 0.1), 300.0)


__all__ = [
    "install_audit_export_scheduler",
    "run_audit_export_event_once",
    "wake_audit_export_scheduler",
]
