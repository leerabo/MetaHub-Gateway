"""PRD-078 审批 timeout、事件投递与回写恢复调度器。

调度器只编排数据库持久化租约；每个 worker 必须在事务内复核 tenant_id、租约代际和所有者，
停机时等待有界网络调用结束，不能靠进程内状态宣告任务完成。
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import secrets
import sqlite3
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import FastAPI
from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.request_id import new_request_id, new_trace_id

from .outbox import drain_approval_event_once, drain_approval_writeback_once
from .service import SchedulerTenantLease, timeout_one_due_approval

logger = logging.getLogger("metahub.control.approvals.scheduler")

# 调度租约和进程级 worker 标识用于隔离并发实例，过期租约只能通过数据库原子接管。
SCHEDULER_LEASE_SECONDS = 90
_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_INSTANCE_ID = uuid4().hex
TIMEOUT_WORKER_ID = f"approval-timeout-{_INSTANCE_ID}"
EVENT_WORKER_ID = f"approval-event-{_INSTANCE_ID}"
WRITEBACK_WORKER_ID = f"approval-writeback-{_INSTANCE_ID}"


def install_approval_scheduler(app: FastAPI) -> None:
    """Compose independently controlled approval workers into the app lifespan.
    ``install_approval_scheduler`` 维护审批超时调度租约，扫描、续租和终态写入必须保持租户隔离。
    """

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        """``combined_lifespan`` 维护审批超时调度租约，扫描、续租和终态写入必须保持租户隔离。"""
        async with original_lifespan(current_app):
            stop_event = threading.Event()
            config = current_app.state.config
            tasks: list[asyncio.Task[None]] = []
            if config.approval_scheduler_enabled:
                tasks.append(
                    asyncio.create_task(
                        _timeout_loop(current_app, stop_event),
                        name="approval-timeout-scheduler",
                    )
                )
            if config.approval_outbox_enabled:
                tasks.append(
                    asyncio.create_task(
                        _event_loop(current_app, stop_event),
                        name="approval-event-outbox",
                    )
                )
            if config.approval_writeback_enabled:
                tasks.append(
                    asyncio.create_task(
                        _writeback_loop(current_app, stop_event),
                        name="approval-writeback-recovery",
                    )
                )
            try:
                yield
            finally:
                stop_event.set()
                # A claimed network attempt owns a durable lease and cannot be cancelled safely.
                # Dispatchers have bounded timeouts and observe stop before another claim.
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

    app.router.lifespan_context = combined_lifespan


def claim_next_scheduler_tenant(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> SchedulerTenantLease | None:
    """Fairly claim one due idle tenant or take over one expired tenant lease.
    ``claim_next_scheduler_tenant`` 维护审批超时调度租约，扫描、续租和终态写入必须保持租户隔离。
    """

    owner = _worker_id(worker_id)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        current_text = _db_time(current)
        row = connection.execute(
            """
            SELECT * FROM approval_scheduler_tenants
            WHERE (status='idle' AND unixepoch(next_scan_at)<=unixepoch(?))
               OR (status='leased' AND unixepoch(lease_expires_at)<=unixepoch(?))
            ORDER BY
              CASE status WHEN 'leased' THEN lease_expires_at ELSE next_scan_at END,
              tenant_id
            LIMIT 1
            """,
            (current_text, current_text),
        ).fetchone()
        if row is None:
            connection.commit()
            return None

        token = secrets.token_hex(32)
        expires_at = current + timedelta(seconds=SCHEDULER_LEASE_SECONDS)
        expires_text = _db_time(expires_at)
        cursor = connection.execute(
            """
            UPDATE approval_scheduler_tenants
            SET status='leased',lease_generation=lease_generation+1,
                lease_owner_id=?,lease_token=?,lease_expires_at=?,updated_at=?
            WHERE tenant_id=? AND status=? AND lease_generation=?
              AND updated_at=?
              AND ((status='idle' AND next_scan_at=?)
                OR (status='leased' AND lease_owner_id=? AND lease_token=?
                  AND lease_expires_at=? AND unixepoch(lease_expires_at)<=unixepoch(?)))
            """,
            (
                owner,
                token,
                expires_text,
                current_text,
                row["tenant_id"],
                row["status"],
                row["lease_generation"],
                row["updated_at"],
                row["next_scan_at"],
                row["lease_owner_id"],
                row["lease_token"],
                row["lease_expires_at"],
                current_text,
            ),
        )
        if cursor.rowcount != 1:
            # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
            raise ApiError("approval.unavailable")
        lease = SchedulerTenantLease(
            tenant_id=_text(row["tenant_id"]),
            lease_generation=int(row["lease_generation"]) + 1,
            lease_owner_id=owner,
            lease_token=token,
            lease_expires_at=expires_at,
        )
        connection.commit()
        return lease
    # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
    except ApiError:
        _rollback(connection)
        raise
    # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
    except (sqlite3.Error, TypeError, ValueError) as exc:
        _rollback(connection)
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable") from exc
    finally:
        connection.close()


def run_approval_timeout_once(
    config: AppConfig,
    *,
    worker_id: str = TIMEOUT_WORKER_ID,
    now: datetime | None = None,
) -> bool:
    """Claim one tenant and report only a durable approval state transition.
    ``run_approval_timeout_once`` 维护审批超时调度租约，扫描、续租和终态写入必须保持租户隔离。
    """

    try:
        lease = claim_next_scheduler_tenant(config, worker_id=worker_id, now=now)
        if lease is None:
            return False
        result = timeout_one_due_approval(
            config,
            lease,
            request_id=new_request_id(),
            trace_id=new_trace_id(),
            next_scan_delay_seconds=_scan_delay(config),
        )
        # tenant lease 的领取与释放只是调度维护，不代表仍有业务积压。若本轮没有真正
        # 终结到期审批，必须返回 False 进入正常 poll 等待，避免空队列形成高频写锁循环。
        return result is not None
    except ApiError as exc:
        _warning("approval.timeout_scheduler_failed", exc.code)
        return False
    except Exception:
        _warning("approval.timeout_scheduler_failed", "approval.unavailable")
        return False


def run_approval_event_once(
    config: AppConfig,
    *,
    stop_event: threading.Event | None = None,
    worker_id: str = EVENT_WORKER_ID,
) -> bool:
    """Recover at most one approval event without logging payloads or endpoints.
    ``run_approval_event_once`` 维护审批超时调度租约，扫描、续租和终态写入必须保持租户隔离。
    """

    try:
        return drain_approval_event_once(
            config,
            worker_id=worker_id,
            should_stop=stop_event.is_set if stop_event is not None else None,
        )
    # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
    except ApiError as exc:
        _warning("approval.event_worker_failed", exc.code)
        return False
    except Exception:
        _warning("approval.event_worker_failed", "approval.event_unavailable")
        return False


async def run_approval_writeback_once(
    config: AppConfig,
    app_state: object,
    *,
    worker_id: str = WRITEBACK_WORKER_ID,
) -> bool:
    """Recover at most one exact Session Risk projection.
    ``run_approval_writeback_once`` 维护审批超时调度租约，扫描、续租和终态写入必须保持租户隔离。
    """

    try:
        return await drain_approval_writeback_once(
            config,
            app_state,
            worker_id=worker_id,
        )
    # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
    except ApiError as exc:
        _warning("approval.writeback_worker_failed", exc.code)
        return False
    except Exception:
        _warning("approval.writeback_worker_failed", "approval.writeback_unavailable")
        return False


async def _timeout_loop(app: FastAPI, stop_event: threading.Event) -> None:
    """``_timeout_loop`` 维护审批超时调度租约，扫描、续租和终态写入必须保持租户隔离。"""
    config = app.state.config
    while not stop_event.is_set():
        worked = await _run_sync_batch(
            stop_event,
            config.approval_scheduler_batch_size,
            lambda: run_approval_timeout_once(config),
        )
        await _wait(stop_event, 0.05 if worked else config.approval_scheduler_poll_interval_seconds)


async def _event_loop(app: FastAPI, stop_event: threading.Event) -> None:
    """``_event_loop`` 维护审批超时调度租约，扫描、续租和终态写入必须保持租户隔离。"""
    config = app.state.config
    while not stop_event.is_set():
        worked = await _run_sync_batch(
            stop_event,
            config.approval_outbox_batch_size,
            lambda: run_approval_event_once(config, stop_event=stop_event),
        )
        await _wait(stop_event, 0.05 if worked else config.approval_outbox_poll_interval_seconds)


async def _writeback_loop(app: FastAPI, stop_event: threading.Event) -> None:
    """``_writeback_loop`` 维护审批超时调度租约，扫描、续租和终态写入必须保持租户隔离。"""
    config = app.state.config
    while not stop_event.is_set():
        worked = False
        for _ in range(config.approval_writeback_batch_size):
            if stop_event.is_set():
                break
            current = await run_approval_writeback_once(config, app.state)
            worked = worked or current
            if not current:
                break
            await asyncio.sleep(0)
        await _wait(stop_event, 0.05 if worked else config.approval_writeback_poll_interval_seconds)


async def _run_sync_batch(
    stop_event: threading.Event,
    limit: int,
    operation,
) -> bool:
    """``_run_sync_batch`` 维护审批超时调度租约，扫描、续租和终态写入必须保持租户隔离。"""
    worked = False
    for _ in range(limit):
        if stop_event.is_set():
            break
        current = await asyncio.to_thread(operation)
        worked = worked or current
        if not current:
            break
        await asyncio.sleep(0)
    return worked


async def _wait(stop_event: threading.Event, delay: float) -> None:
    """``_wait`` 维护审批超时调度租约，扫描、续租和终态写入必须保持租户隔离。"""
    if stop_event.is_set():
        return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + delay
    while not stop_event.is_set() and loop.time() < deadline:
        await asyncio.sleep(min(0.05, max(0.0, deadline - loop.time())))


def _scan_delay(config: AppConfig) -> int:
    """``_scan_delay`` 维护审批超时调度租约，扫描、续租和终态写入必须保持租户隔离。"""
    return max(1, min(60, math.ceil(config.approval_scheduler_poll_interval_seconds)))


def _transaction_now(connection: sqlite3.Connection, override: datetime | None) -> datetime:
    """``_transaction_now`` 维护审批超时调度租约，扫描、续租和终态写入必须保持租户隔离。"""
    if override is not None:
        if override.tzinfo is None:
            # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
            raise ApiError("approval.unavailable")
        return override.astimezone(UTC).replace(microsecond=0)
    row = connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')").fetchone()
    if row is None:
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable")
    return datetime.strptime(_text(row[0]), _TIMESTAMP_FORMAT).replace(tzinfo=UTC)


def _db_time(value: datetime) -> str:
    """``_db_time`` 维护审批超时调度租约，扫描、续租和终态写入必须保持租户隔离。"""
    if value.tzinfo is None:
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable")
    return value.astimezone(UTC).replace(microsecond=0).strftime(_TIMESTAMP_FORMAT)


def _connect(config: AppConfig) -> sqlite3.Connection:
    """``_connect`` 维护审批超时调度租约，扫描、续租和终态写入必须保持租户隔离。"""
    try:
        return connect_database(config.database_url)
    # 数据库异常必须回滚当前事务并转换为固定不可用错误，SQL 和敏感参数不得进入响应。
    except DbError as exc:
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable") from exc


def _rollback(connection: sqlite3.Connection) -> None:
    """``_rollback`` 维护审批超时调度租约，扫描、续租和终态写入必须保持租户隔离。"""
    try:
        if connection.in_transaction:
            connection.rollback()
    # 数据库异常必须回滚当前事务并转换为固定不可用错误，SQL 和敏感参数不得进入响应。
    except sqlite3.Error:
        return


def _worker_id(value: object) -> str:
    """``_worker_id`` 维护审批超时调度租约，扫描、续租和终态写入必须保持租户隔离。"""
    if type(value) is not str or _WORKER_ID.fullmatch(value) is None:
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable")
    return value


def _text(value: object) -> str:
    """``_text`` 维护审批超时调度租约，扫描、续租和终态写入必须保持租户隔离。"""
    if type(value) is not str or not value:
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable")
    return value


def _warning(event: str, error_code: str) -> None:
    """``_warning`` 维护审批超时调度租约，扫描、续租和终态写入必须保持租户隔离。"""
    logger.warning(
        "approval recovery iteration failed",
        extra={"event": event, "error_code": error_code},
    )


__all__ = [
    "claim_next_scheduler_tenant",
    "install_approval_scheduler",
    "run_approval_event_once",
    "run_approval_timeout_once",
    "run_approval_writeback_once",
]
