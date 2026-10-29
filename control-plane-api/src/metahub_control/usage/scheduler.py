"""PRD-043 可恢复 Usage CSV 导出调度器。

调度器仅安装在 Control API lifespan。每轮先清理过期 BLOB，再用数据库 lease 领取一条任务；
CSV 生成在线程中执行，不进入 Gateway 热路径，也不在异步事件循环上阻塞大查询。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from metahub_shared.runtime.errors import ApiError

from .export_service import (
    claim_next_usage_export,
    expire_usage_exports,
    process_claimed_usage_export,
)

logger = logging.getLogger("metahub.control.usage.export.scheduler")


def install_usage_export_scheduler(app: FastAPI) -> None:
    """把导出 worker 组合进现有 lifespan，不覆盖健康探测或其他服务生命周期。"""

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        """先进入原生命周期，再启动一个可有界停止的串行导出任务。"""

        async with original_lifespan(current_app):
            stop_event = asyncio.Event()
            task: asyncio.Task[None] | None = None
            # 配置关闭时不创建后台任务，避免仍然领取客户导出或产生停机等待。
            if current_app.state.config.usage_export_enabled:
                task = asyncio.create_task(
                    _scheduler_loop(current_app, stop_event),
                    name="usage-export-scheduler",
                )
            try:
                yield
            finally:
                stop_event.set()
                if task is not None:
                    try:
                        await asyncio.wait_for(task, timeout=5)
                    # 停机等待有界，超时后只取消协程，避免应用生命周期无限阻塞。
                    except TimeoutError:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

    app.router.lifespan_context = combined_lifespan


async def _scheduler_loop(app: FastAPI, stop_event: asyncio.Event) -> None:
    """按配置间隔串行运行一个任务；有工作时立即尝试下一条，空闲时休眠。"""

    config = app.state.config
    while not stop_event.is_set():
        worked = await asyncio.to_thread(run_usage_export_once, config)
        delay = 0.05 if worked else config.usage_export_poll_interval_seconds
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        # 轮询超时表示正常进入下一轮，不属于需要记录的导出故障。
        except TimeoutError:
            continue


def run_usage_export_once(config) -> bool:
    """清理到期制品并处理一个 lease 任务，供 scheduler 和集成测试直接调用。"""

    try:
        expire_usage_exports(config)
        claim = claim_next_usage_export(config)
        if claim is None:
            return False
        process_claimed_usage_export(config, claim)
        return True
    # 后台日志只保留固定错误码，数据库路径、筛选和 CSV 内容不得沿异常链输出。
    except ApiError as exc:
        logger.warning(
            "usage export scheduler iteration failed",
            extra={"event": "usage.export.scheduler_failed", "error_code": exc.code},
        )
        return False
    except Exception:
        logger.warning(
            "usage export scheduler iteration failed",
            extra={"event": "usage.export.scheduler_failed", "error_code": "usage.export_unavailable"},
        )
        return False
