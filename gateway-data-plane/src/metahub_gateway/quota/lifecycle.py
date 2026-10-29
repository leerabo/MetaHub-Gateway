"""Gateway 额度 Reservation 的有界超期恢复生命周期。"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI
from metahub_shared.db.connection import connect_database
from metahub_shared.runtime.errors import ApiError

from .service import reconcile_expired_quota_reservations

logger = logging.getLogger("metahub.gateway.quota.lifecycle")
# 恢复循环采用有界轮询间隔和批量上限，避免异常配置拖垮网关事件循环。
_DEFAULT_POLL_SECONDS = 2.0
_DEFAULT_BATCH_SIZE = 50


def install_quota_reconciliation(app: FastAPI) -> None:
    """把额度超期恢复任务安全组合进既有网关生命周期。"""

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        """随网关启动恢复任务，并在停机时等待任务完整退出。"""

        async with original_lifespan(current_app):
            stop_event = asyncio.Event()
            worker = asyncio.create_task(
                _reconciliation_loop(current_app, stop_event),
                name="gateway-quota-reconciliation",
            )
            try:
                yield
            finally:
                stop_event.set()
                await worker

    app.router.lifespan_context = combined_lifespan


async def _reconciliation_loop(app: FastAPI, stop_event: asyncio.Event) -> None:
    """按有界间隔循环恢复超期额度，同时响应网关停机信号。"""

    connection: sqlite3.Connection | None = None
    try:
        while not stop_event.is_set():
            try:
                if connection is None:
                    connection = await asyncio.to_thread(
                        connect_database,
                        app.state.config.database_url,
                        check_same_thread=False,
                    )
                if not await _run_once(app, connection):
                    await asyncio.to_thread(connection.close)
                    connection = None
            except Exception:
                _warning("gateway.quota_unavailable")
                if connection is not None:
                    await asyncio.to_thread(connection.close)
                    connection = None
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=_poll_seconds(app))
            # 等待超时表示进入下一次正常轮询，不应当视为额度服务错误。
            except TimeoutError:
                pass
    finally:
        if connection is not None:
            await asyncio.to_thread(connection.close)


async def _run_once(app: FastAPI, connection: sqlite3.Connection) -> bool:
    """在线程池执行一轮数据库恢复，并将异常折叠为安全日志。"""

    try:
        await asyncio.to_thread(
            reconcile_expired_quota_reservations,
            app.state.config,
            limit=_batch_size(app),
            connection=connection,
        )
        return True
    # 统一错误只记录稳定错误码，避免把数据库异常或请求内容写入日志。
    except ApiError as exc:
        _warning(exc.code)
    except Exception:
        _warning("gateway.quota_unavailable")
    return False


def _poll_seconds(app: FastAPI) -> float:
    """读取并钳制恢复轮询秒数，拒绝布尔值和异常数值类型。"""

    value = getattr(
        app.state.config,
        "quota_reconciliation_poll_interval_seconds",
        _DEFAULT_POLL_SECONDS,
    )
    if type(value) not in {int, float} or isinstance(value, bool):
        return _DEFAULT_POLL_SECONDS
    return min(max(float(value), 0.1), 300.0)


def _batch_size(app: FastAPI) -> int:
    """读取并钳制单轮恢复批量，防止配置造成无界数据库事务。"""

    value = getattr(
        app.state.config,
        "quota_reconciliation_batch_size",
        _DEFAULT_BATCH_SIZE,
    )
    if type(value) is not int:
        return _DEFAULT_BATCH_SIZE
    return min(max(value, 1), 200)


def _warning(error_code: str) -> None:
    """仅以稳定事件和错误码记录恢复失败，不携带敏感异常文本。"""

    logger.warning(
        "quota reconciliation iteration failed",
        extra={"event": "quota.reconciliation_failed", "error_code": error_code},
    )


__all__ = ["install_quota_reconciliation"]
