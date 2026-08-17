"""PRD-086 客户健康两阶段租约调度器。"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import sqlite3
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import FastAPI
from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .outbox import drain_customer_health_outbox_once
from .service import collect_source_snapshot, materialize_customer_health

logger = logging.getLogger("metahub.control.customer_health.scheduler")
# 租约、退避、轮询间隔与 worker 标识共同限定调度并发和失败恢复边界。
SCHEDULER_LEASE_SECONDS = 90
FAILURE_BACKOFF_SECONDS = (5, 30, 300)
DEFAULT_POLL_SECONDS = 2.0
WORKER_ID = f"customer-health-{uuid4().hex}"
OUTBOX_WORKER_ID = f"customer-health-outbox-{uuid4().hex}"
_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


@dataclass(slots=True)
class CustomerHealthTenantClaim:
    """第一阶段冻结的 tenant lease；令牌不进入调试表示。"""

    tenant_id: str
    lease_owner_id: str
    lease_token: str = field(repr=False)
    generation: int
    consecutive_failure_count: int
    lease_expires_at: datetime


def claim_next_customer_health_tenant(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> CustomerHealthTenantClaim | None:
    """短事务公平领取一个到期 tenant，过期 lease 可由新 worker 接管。"""

    owner = _worker(worker_id)
    connection = _connect(config)
    try:
        current = _transaction_now(connection, now)
        now_text = _db_time(current)
        due = connection.execute(
            """
            SELECT 1 FROM customer_health_scheduler_tenants
            WHERE (status='pending' AND unixepoch(next_evaluation_at)<=unixepoch(?))
               OR (status='leased' AND unixepoch(lease_expires_at)<=unixepoch(?))
            LIMIT 1
            """,
            (now_text, now_text),
        ).fetchone()
        if due is None:
            return None
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        now_text = _db_time(current)
        row = connection.execute(
            """
            SELECT * FROM customer_health_scheduler_tenants
            WHERE (status='pending' AND unixepoch(next_evaluation_at)<=unixepoch(?))
               OR (status='leased' AND unixepoch(lease_expires_at)<=unixepoch(?))
            ORDER BY COALESCE(next_evaluation_at,lease_expires_at),tenant_id
            LIMIT 1
            """,
            (now_text, now_text),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        old_status = _status(row["status"])
        old_generation = _integer(row["generation"], minimum=0)
        failures = _integer(row["consecutive_failure_count"], minimum=0)
        token = secrets.token_hex(32)
        generation = old_generation + 1
        expires = current + timedelta(seconds=SCHEDULER_LEASE_SECONDS)
        connection.execute(
            """
            UPDATE customer_health_scheduler_tenants
            SET status='leased',generation=?,lease_owner_id=?,lease_token=?,
                lease_expires_at=?,updated_at=?
            WHERE tenant_id=? AND status=? AND generation=?
              AND consecutive_failure_count=?
              AND next_evaluation_at IS ?
              AND lease_owner_id IS ? AND lease_token IS ? AND lease_expires_at IS ?
              AND last_completed_window_end IS ? AND last_error_code IS ?
              AND (status='pending' OR unixepoch(lease_expires_at)<=unixepoch(?))
            """,
            (
                generation, owner, token, _db_time(expires), now_text,
                row["tenant_id"], old_status, old_generation, failures,
                row["next_evaluation_at"], row["lease_owner_id"], row["lease_token"],
                row["lease_expires_at"], row["last_completed_window_end"],
                row["last_error_code"], now_text,
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            connection.rollback()
            return None
        claim = CustomerHealthTenantClaim(
            tenant_id=_text(row["tenant_id"]),
            lease_owner_id=owner,
            lease_token=token,
            generation=generation,
            consecutive_failure_count=failures,
            lease_expires_at=expires,
        )
        connection.commit()
        return claim
    # 已分类业务错误保留固定错误码，事务必须回滚后再交给上层处理。
    except ApiError:
        connection.rollback()
        raise
    # 数据库或冻结字段异常统一映射为不可用，不能泄露底层异常正文。
    except (sqlite3.Error, TypeError, ValueError):
        connection.rollback()
        raise ApiError("customer_health.unavailable") from None
    finally:
        connection.close()


def evaluate_claimed_customer_health(
    config: AppConfig,
    claim: CustomerHealthTenantClaim,
    *,
    now: datetime | None = None,
) -> str | None:
    """第二段事务复核 lease，在一个快照内聚合、物化并推进游标。"""

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        now_text = _db_time(current)
        row = connection.execute(
            "SELECT * FROM customer_health_scheduler_tenants WHERE tenant_id=?",
            (claim.tenant_id,),
        ).fetchone()
        if row is None or not _claim_matches(row, claim, current):
            connection.rollback()
            return None
        source = collect_source_snapshot(connection, claim.tenant_id, evaluated_at=current)
        score_id, _inserted, _outbox_created = materialize_customer_health(connection, source)
        next_hour = current.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        connection.execute(
            """
            UPDATE customer_health_scheduler_tenants
            SET status='pending',next_evaluation_at=?,consecutive_failure_count=0,
                generation=generation+1,lease_owner_id=NULL,lease_token=NULL,
                lease_expires_at=NULL,last_completed_window_end=?,last_error_code=NULL,
                updated_at=?
            WHERE tenant_id=? AND status='leased' AND generation=?
              AND lease_owner_id=? AND lease_token=?
              AND lease_expires_at=? AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                _db_time(next_hour), source.window_end, now_text, claim.tenant_id,
                claim.generation, claim.lease_owner_id, claim.lease_token,
                _db_time(claim.lease_expires_at), now_text,
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            connection.rollback()
            return None
        connection.commit()
        return score_id
    # 已分类业务错误回滚后原样上抛，保留统一错误目录中的安全错误码。
    except ApiError:
        connection.rollback()
        raise
    # 聚合或持久化异常不得覆盖旧评分，并统一转换为安全不可用错误。
    except (sqlite3.Error, TypeError, ValueError):
        connection.rollback()
        raise ApiError("customer_health.unavailable") from None
    finally:
        connection.close()


def fail_claimed_customer_health(
    config: AppConfig,
    claim: CustomerHealthTenantClaim,
    *,
    error_code: str = "customer_health.unavailable",
    now: datetime | None = None,
) -> bool:
    """失败只推进固定退避，不覆盖最后可信 score。"""

    # Scheduler 失败游标只保存固定错误码，任何调用方文本都不能进入数据库。
    if error_code != "customer_health.unavailable":
        error_code = "customer_health.unavailable"
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        failures = claim.consecutive_failure_count + 1
        delay = FAILURE_BACKOFF_SECONDS[min(failures - 1, len(FAILURE_BACKOFF_SECONDS) - 1)]
        now_text = _db_time(current)
        connection.execute(
            """
            UPDATE customer_health_scheduler_tenants
            SET status='pending',next_evaluation_at=?,consecutive_failure_count=?,
                generation=generation+1,lease_owner_id=NULL,lease_token=NULL,
                lease_expires_at=NULL,last_error_code=?,updated_at=?
            WHERE tenant_id=? AND status='leased' AND generation=?
              AND lease_owner_id=? AND lease_token=? AND lease_expires_at=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                _db_time(current + timedelta(seconds=delay)), failures, error_code,
                now_text, claim.tenant_id, claim.generation, claim.lease_owner_id,
                claim.lease_token, _db_time(claim.lease_expires_at), now_text,
            ),
        )
        changed = connection.execute("SELECT changes()").fetchone()[0] == 1
        if changed:
            connection.commit()
        else:
            connection.rollback()
        return changed
    # 失败恢复本身异常时回滚并返回假值，避免日志记录敏感异常字符串。
    except (sqlite3.Error, TypeError, ValueError, ApiError):
        connection.rollback()
        return False
    finally:
        connection.close()


def run_customer_health_scheduler_once(
    config: AppConfig,
    *,
    worker_id: str = WORKER_ID,
    now: datetime | None = None,
) -> bool:
    """处理至多一个 tenant；源故障收敛为安全退避。"""

    claim = claim_next_customer_health_tenant(config, worker_id=worker_id, now=now)
    if claim is None:
        return False
    try:
        evaluate_claimed_customer_health(config, claim, now=now)
    except Exception:
        fail_claimed_customer_health(config, claim, now=now)
    return True


def install_customer_health_scheduler(app: FastAPI) -> None:
    """组合 lifespan，启动可关闭的物化与 source outbox 恢复轮询。"""

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        """组合原 lifespan 与可关闭后台任务，关闭时等待 worker 完整退出。"""

        async with original_lifespan(current_app):
            stop = threading.Event()
            wake = asyncio.Event()
            task: asyncio.Task[None] | None = None
            if _enabled(current_app.state.config):
                task = asyncio.create_task(
                    _scheduler_loop(current_app.state.config, stop, wake),
                    name="customer-health-scheduler",
                )
            try:
                yield
            finally:
                stop.set()
                wake.set()
                if task is not None:
                    await asyncio.gather(task, return_exceptions=True)

    app.router.lifespan_context = combined_lifespan


async def _scheduler_loop(config: AppConfig, stop: threading.Event, wake: asyncio.Event) -> None:
    """循环执行单次物化和 outbox 恢复，并支持停止事件即时唤醒。"""

    while not stop.is_set():
        worked = await asyncio.to_thread(_safe_iteration, config, stop)
        try:
            await asyncio.wait_for(
                wake.wait(), timeout=0.05 if worked else _poll_seconds(config)
            )
        # 正常轮询超时只进入下一轮，不记录异常或把超时误判为业务失败。
        except TimeoutError:
            continue


def _safe_iteration(config: AppConfig, stop: threading.Event) -> bool:
    """日志只保留固定事件/错误码，不记录异常字符串或 traceback。"""

    try:
        evaluated = run_customer_health_scheduler_once(config)
        delivered = drain_customer_health_outbox_once(
            config,
            worker_id=OUTBOX_WORKER_ID,
            should_stop=stop.is_set,
        )
        return evaluated or delivered
    # 已分类错误日志只写固定事件名和错误码，不记录异常正文与堆栈。
    except ApiError as exc:
        logger.warning(
            "customer health scheduler iteration failed",
            extra={"event": "customer_health.scheduler_failed", "error_code": exc.code},
        )
    # 未分类异常同样收敛为固定不可用码，避免 Prompt、Token 或响应进入日志。
    except Exception:
        logger.warning(
            "customer health scheduler iteration failed",
            extra={
                "event": "customer_health.scheduler_failed",
                "error_code": "customer_health.unavailable",
            },
        )
    return False


def _claim_matches(row: sqlite3.Row, claim: CustomerHealthTenantClaim, now: datetime) -> bool:
    """精确核对租约代次、owner、令牌和到期时间，阻止旧 worker 继续提交。"""

    return (
        row["status"] == "leased"
        and row["generation"] == claim.generation
        and row["consecutive_failure_count"] == claim.consecutive_failure_count
        and row["lease_owner_id"] == claim.lease_owner_id
        and row["lease_token"] == claim.lease_token
        and row["lease_expires_at"] == _db_time(claim.lease_expires_at)
        and claim.lease_expires_at > now
    )


def _connect(config: AppConfig) -> sqlite3.Connection:
    """创建带忙等待的 SQLite 连接，并把连接错误收敛为统一错误码。"""

    try:
        connection = connect_database(config.database_url)
        connection.execute("PRAGMA busy_timeout=5000")
        return connection
    # 数据库地址或连接失败只暴露固定不可用码，不透出文件路径与底层异常。
    except DbError:
        raise ApiError("customer_health.unavailable") from None


def _transaction_now(connection: sqlite3.Connection, override: datetime | None) -> datetime:
    """取得事务内 UTC 整秒时钟，测试覆盖值也必须携带明确时区。"""

    if override is not None:
        # 无时区覆盖值会破坏租约比较，必须在进入数据库写入前失败关闭。
        if override.tzinfo is None:
            raise ApiError("customer_health.unavailable")
        return override.astimezone(UTC).replace(microsecond=0)
    row = connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')").fetchone()
    # 数据库无法提供事务时钟时停止调度，不能退回进程本地的漂移时间。
    if row is None:
        raise ApiError("customer_health.unavailable")
    return datetime.fromisoformat(str(row[0]).replace("Z", "+00:00")).astimezone(UTC)


def _db_time(value: datetime) -> str:
    """把有时区时间规范化为数据库使用的 UTC Z 整秒文本。"""

    # 租约时间缺失时区会导致 takeover 判断不一致，必须拒绝写入。
    if value.tzinfo is None:
        raise ApiError("customer_health.unavailable")
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _worker(value: object) -> str:
    """校验 worker 标识的类型、长度和安全字符，避免令牌字段被污染。"""

    # worker owner 进入租约 CAS，非法值不得进入数据库或日志字段。
    if type(value) is not str or _WORKER_ID.fullmatch(value) is None:
        raise ApiError("customer_health.unavailable")
    return value


def _text(value: object) -> str:
    """读取数据库必填文本，空值或类型漂移统一视为不可用。"""

    # 关键租户和状态文本缺失时失败关闭，不能以字符串强制转换掩盖腐坏。
    if type(value) is not str or not value:
        raise ApiError("customer_health.unavailable")
    return value


def _integer(value: object, *, minimum: int) -> int:
    """读取有下限的数据库整数，并拒绝布尔值或其他隐式数值。"""

    # 租约代次和失败次数必须是原生整数，类型或范围异常立即阻断。
    if type(value) is not int or value < minimum:
        raise ApiError("customer_health.unavailable")
    return value


def _status(value: object) -> str:
    """校验 Scheduler 状态只允许 pending 或 leased 两种冻结值。"""

    # 未知状态可能绕过租约 CAS，必须统一视为持久化数据不可用。
    if value not in {"pending", "leased"}:
        raise ApiError("customer_health.unavailable")
    return str(value)


def _enabled(config: AppConfig) -> bool:
    """读取后台调度开关，非布尔配置按安全默认启用恢复能力。"""

    value = getattr(config, "customer_health_scheduler_enabled", True)
    return value if type(value) is bool else True


def _poll_seconds(config: AppConfig) -> float:
    """读取并限制轮询间隔，避免忙循环或超长停顿影响恢复。"""

    value = getattr(config, "customer_health_scheduler_poll_interval_seconds", DEFAULT_POLL_SECONDS)
    if type(value) not in {int, float} or isinstance(value, bool):
        return DEFAULT_POLL_SECONDS
    return min(max(float(value), 0.1), 300.0)


__all__ = [
    "CustomerHealthTenantClaim",
    "claim_next_customer_health_tenant",
    "evaluate_claimed_customer_health",
    "fail_claimed_customer_health",
    "install_customer_health_scheduler",
    "run_customer_health_scheduler_once",
]
