"""Control 侧短期 Gateway placement 签名制品的有界续签循环。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .snapshots import refresh_gateway_snapshot_once

logger = logging.getLogger("metahub.control.platform_gateway.scheduler")


def install_gateway_snapshot_scheduler(app: FastAPI) -> None:
    """把有界发布任务组合进现有 lifespan，不覆盖其他后台任务。"""

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        """原生命周期就绪后启动续签，退出时等待当前数据库轮次完成。"""

        async with original_lifespan(current_app):
            stop_event = asyncio.Event()
            task: asyncio.Task[None] | None = None
            # 未配置独立签名密钥时不启动续签任务，避免生成无法验证的制品。
            if current_app.state.config.gateway_placement_signing_secret:
                task = asyncio.create_task(
                    _publisher_loop(current_app, stop_event),
                    name="gateway-placement-snapshot-publisher",
                )
            try:
                yield
            finally:
                stop_event.set()
                if task is not None:
                    await asyncio.gather(task, return_exceptions=True)

    app.router.lifespan_context = combined_lifespan


async def _publisher_loop(app: FastAPI, stop_event: asyncio.Event) -> None:
    """积压时快速排空，空闲时按 TTL 三分之一且不超过两秒轮询。"""

    config = app.state.config
    while not stop_event.is_set():
        try:
            worked = await asyncio.to_thread(refresh_gateway_snapshot_once, config)
        except Exception:
            logger.warning(
                "gateway snapshot publication failed",
                extra={
                    "event": "gateway_config.publish_failed",
                    "error_code": "platform_gateway.unavailable",
                },
            )
            worked = False
        delay = 0.05 if worked else min(
            2.0, max(0.2, config.gateway_placement_snapshot_ttl_seconds / 3)
        )
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        # 正常轮询超时只进入下一轮，其他异常由发布分支统一记录固定错误。
        except TimeoutError:
            continue


__all__ = ["install_gateway_snapshot_scheduler"]
