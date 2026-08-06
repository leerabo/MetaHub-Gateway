"""PRD-073 文件安全 source outbox 的启动恢复、周期扫描与有界停服调度器。"""

from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .outbox import drain_file_security_event_once

logger = logging.getLogger("metahub.control.file_security.scheduler")
# Worker owner 只用于数据库 CAS；轮询、唤醒和停服上限共同定义后台任务生命周期。
WORKER_ID = f"file-security-{uuid4().hex}"
POLL_INTERVAL_SECONDS = 2.0
_WAKE_STATE_NAME = "file_security_source_wake_event"
_WAKE_LOOP_STATE_NAME = "file_security_source_wake_loop"


def install_file_security_event_scheduler(app: FastAPI) -> None:
    """把 source worker 组合进已有 FastAPI lifespan，不覆盖其他后台任务。"""

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        """进入原生命周期后启动恢复 worker，退出时停止领取并有界等待。"""

        async with original_lifespan(current_app):
            # threading.Event 同时供事件循环和 asyncio.to_thread 内的同步 dispatcher 安全读取。
            stop_event = threading.Event()
            wake_event = asyncio.Event()
            setattr(current_app.state, _WAKE_STATE_NAME, wake_event)
            setattr(current_app.state, _WAKE_LOOP_STATE_NAME, asyncio.get_running_loop())
            task = asyncio.create_task(
                _scheduler_loop(current_app, stop_event, wake_event),
                name="file-security-source-scheduler",
            )
            try:
                yield
            finally:
                stop_event.set()
                wake_event.set()
                # asyncio.to_thread 的同步 HTTP 线程不能被 task.cancel 安全终止；lease guard 会在
                # 当前最长 30 秒 attempt 返回后观察 stop，不再外呼，lifespan 等待数据库收尾。
                await asyncio.gather(task, return_exceptions=True)
                if getattr(current_app.state, _WAKE_STATE_NAME, None) is wake_event:
                    delattr(current_app.state, _WAKE_STATE_NAME)
                if hasattr(current_app.state, _WAKE_LOOP_STATE_NAME):
                    delattr(current_app.state, _WAKE_LOOP_STATE_NAME)

    app.router.lifespan_context = combined_lifespan


def wake_file_security_event_scheduler(app: FastAPI) -> None:
    """提示已运行 worker 立即扫描；调用方不会因此取得 lease 或直接执行网络投递。"""

    event = getattr(app.state, _WAKE_STATE_NAME, None)
    loop = getattr(app.state, _WAKE_LOOP_STATE_NAME, None)
    if isinstance(event, asyncio.Event) and isinstance(loop, asyncio.AbstractEventLoop):
        try:
            # 路由、测试客户端或未来同步调用方可能不在 scheduler 线程，必须线程安全地唤醒所属循环。
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            # 关停竞态下循环可能刚刚关闭；持久 pending 行仍会由下次启动 drain 恢复。
            return


async def _scheduler_loop(
    app: FastAPI,
    stop_event: threading.Event,
    wake_event: asyncio.Event,
) -> None:
    """启动即恢复；有积压时快速串行处理，空队列等待唤醒或固定轮询。"""

    config = app.state.config
    while not stop_event.is_set():
        # 在扫描前消费旧信号；扫描期间新到达的提交信号必须保留到 wait，不能在查到空队列后擦除。
        wake_event.clear()
        worked = await asyncio.to_thread(
            run_file_security_event_once,
            config,
            stop_event=stop_event,
        )
        if worked:
            # 每轮至多一条并短暂让出事件循环，防止单租户积压独占控制面线程。
            await asyncio.sleep(0.05)
            continue
        try:
            await asyncio.wait_for(wake_event.wait(), timeout=POLL_INTERVAL_SECONDS)
        # 空队列等待到期只是正常恢复节拍，不能形成持续 warning 或告警噪声。
        except TimeoutError:
            continue


def run_file_security_event_once(
    config: AppConfig,
    *,
    stop_event: threading.Event | None = None,
) -> bool:
    """处理最多一个 source event，供 scheduler、专项测试和实际运行验收复用。"""

    try:
        return drain_file_security_event_once(
            config,
            worker_id=WORKER_ID,
            should_stop=stop_event.is_set if stop_event is not None else None,
        )
    # 已归一化 source 错误只记录固定 code，下一轮仍可处理其他 tenant 的事件。
    except ApiError as exc:
        logger.warning(
            "file security source iteration failed",
            extra={"event": "file_security.outbox_failed", "error_code": exc.code},
        )
        return False
    except Exception:
        # 未知异常可能携带数据库路径、Endpoint 或正文，只记录固定领域分类。
        logger.warning(
            "file security source iteration failed",
            extra={
                "event": "file_security.outbox_failed",
                "error_code": "file.security_scan_unavailable",
            },
        )
        return False


__all__ = [
    "install_file_security_event_scheduler",
    "run_file_security_event_once",
    "wake_file_security_event_scheduler",
]
