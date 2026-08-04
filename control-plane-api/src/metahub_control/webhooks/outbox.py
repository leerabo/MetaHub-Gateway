"""PRD-057 Webhook replay 的持久 claim、lease 与 CAS worker。

该模块只处理已经提交的 replay delivery。网络调用复用 service 中经过安全审查的 pending
恢复原语；死信表只保存定位、状态和固定错误码，不复制 payload、URL、Secret 或签名。
"""

from __future__ import annotations

import math
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from . import service as webhook_service

# 短任务至少持有 30 秒；长超时 Endpoint 按剩余 attempts 扩展，避免合法发送中途被接管。
MINIMUM_LEASE_SECONDS = 30
LEASE_SAFETY_SECONDS = 15
MAXIMUM_LEASE_SECONDS = 120
REPLAY_BACKOFF_SECONDS = (1, 5, 30, 300)


@dataclass(frozen=True)
class ReplayClaim:
    """一次不可转让的 replay 领取凭据。

    token 和 owner 只在进程内、数据库 CAS 之间传递，禁止进入 API、普通日志或审计摘要。
    """

    tenant_id: str
    dead_letter_event_id: str
    replay_delivery_id: str
    claim_token: str
    worker_id: str
    lease_expires_at: datetime
    dispatch_attempt: int


def claim_next_replay(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
    dead_letter_event_id: str | None = None,
    replay_delivery_id: str | None = None,
) -> ReplayClaim | None:
    """原子领取一条到期 replay，或领取指定死信供 API 做一次即时 drain。

    参数：worker_id 是当前进程内稳定 owner；可选死信 ID 和 replay delivery ID 只收窄
    内部查询，不能替代租户权限。API 即时 drain 必须同时传入两者，避免旧幂等键回读时
    误领同一根死信下较新的 pending replay；scheduler 不传筛选条件，仍按全局顺序领取。
    返回：领取成功时返回 CAS 凭据，无待办时返回 None。
    约束：lease 至少 30 秒，并覆盖当前 delivery 剩余 attempts 的最坏 Endpoint 超时。
    """

    owner = str(worker_id or "").strip()
    # owner 参与 lease CAS，空值或超长值会破坏失权判定，因此在接触数据库前统一拒绝。
    if not owner or len(owner) > 128:
        raise ApiError("webhook.dead_letter_unavailable")
    current_time = _as_utc(now or _utcnow())
    current_text = _to_db_time(current_time)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        clauses = [
            "dead.status='replay_pending'",
            "dead.current_replay_delivery_id IS NOT NULL",
            "(dead.replay_next_attempt_at IS NULL OR dead.replay_next_attempt_at<=?)",
            "(dead.replay_claim_token IS NULL OR dead.replay_lease_expires_at<=?)",
        ]
        params: list[object] = [current_text, current_text]
        if dead_letter_event_id is not None:
            clauses.append("dead.dead_letter_event_id=?")
            params.append(str(dead_letter_event_id))
        if replay_delivery_id is not None:
            clauses.append("dead.current_replay_delivery_id=?")
            params.append(str(replay_delivery_id))
        row = connection.execute(
            f"""
            SELECT dead.*, delivery.status AS replay_delivery_status,
                   delivery.error_code AS replay_delivery_error_code,
                   delivery.attempt_count, delivery.max_attempts, endpoint.timeout_ms
            FROM webhook_dead_letter_events dead
            JOIN webhook_deliveries delivery
              ON delivery.tenant_id=dead.tenant_id
             AND delivery.webhook_delivery_id=dead.current_replay_delivery_id
            JOIN webhook_endpoints endpoint
              ON endpoint.tenant_id=dead.tenant_id
             AND endpoint.webhook_endpoint_id=dead.webhook_endpoint_id
            WHERE {' AND '.join(clauses)}
            ORDER BY COALESCE(dead.replay_next_attempt_at, dead.replay_requested_at) ASC,
                     dead.failed_at ASC, dead.dead_letter_event_id ASC
            LIMIT 1
            """,
            params,
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        replay_status = str(row["replay_delivery_status"])
        if replay_status in {"success", "failed"}:
            # delivery 已持久终结但旧进程未推进根状态时，只做本地收敛，禁止再次出网。
            replay_succeeded = replay_status == "success"
            terminal_error_code = None
            if not replay_succeeded:
                terminal_error_code = _safe_worker_error_code(
                    row["replay_delivery_error_code"] or "webhook.delivery_failed"
                )
            terminal_cursor = connection.execute(
                """
                UPDATE webhook_dead_letter_events
                SET status=?, replay_claim_token=NULL, replay_lease_owner_id=NULL,
                    replay_lease_expires_at=NULL, replay_next_attempt_at=NULL,
                    last_replay_error_code=?, replayed_at=?, updated_at=?
                WHERE tenant_id=? AND dead_letter_event_id=? AND status='replay_pending'
                  AND current_replay_delivery_id=?
                  AND (replay_next_attempt_at IS NULL OR replay_next_attempt_at<=?)
                  AND (replay_claim_token IS NULL OR replay_lease_expires_at<=?)
                  AND EXISTS (
                    SELECT 1 FROM webhook_deliveries replay
                    WHERE replay.tenant_id=webhook_dead_letter_events.tenant_id
                      AND replay.webhook_delivery_id=current_replay_delivery_id
                      AND replay.status=?
                  )
                """,
                (
                    "replayed" if replay_succeeded else "dead_letter",
                    terminal_error_code,
                    current_text if replay_succeeded else None,
                    current_text,
                    row["tenant_id"],
                    row["dead_letter_event_id"],
                    row["current_replay_delivery_id"],
                    current_text,
                    current_text,
                    replay_status,
                ),
            )
            if terminal_cursor.rowcount != 1:
                connection.rollback()
                return None
            connection.commit()
            return None
        if replay_status != "pending":
            # 未知 delivery 状态不能被猜测为 pending 后领取；外层会折叠为固定错误码。
            raise ValueError("invalid replay delivery status")


        lease_expires_at = current_time + timedelta(
            seconds=_lease_seconds(
                attempt_count=int(row["attempt_count"]),
                max_attempts=int(row["max_attempts"]),
                timeout_ms=int(row["timeout_ms"]),
            )
        )
        claim_token = secrets.token_urlsafe(24)
        cursor = connection.execute(
            """
            UPDATE webhook_dead_letter_events
            SET replay_claim_token=?, replay_lease_owner_id=?, replay_lease_expires_at=?,
                replay_dispatch_attempts=replay_dispatch_attempts+1,
                replay_next_attempt_at=NULL, updated_at=?
            WHERE tenant_id=? AND dead_letter_event_id=? AND status='replay_pending'
              AND current_replay_delivery_id=?
              AND (replay_next_attempt_at IS NULL OR replay_next_attempt_at<=?)
              AND (replay_claim_token IS NULL OR replay_lease_expires_at<=?)
            """,
            (
                claim_token,
                owner,
                _to_db_time(lease_expires_at),
                current_text,
                row["tenant_id"],
                row["dead_letter_event_id"],
                row["current_replay_delivery_id"],
                current_text,
                current_text,
            ),
        )
        if cursor.rowcount != 1:
            # SQLite 写锁已串行化当前窗口；CAS 仍防止未来数据库实现放宽锁粒度后误领。
            connection.rollback()
            return None
        connection.commit()
        return ReplayClaim(
            tenant_id=str(row["tenant_id"]),
            dead_letter_event_id=str(row["dead_letter_event_id"]),
            replay_delivery_id=str(row["current_replay_delivery_id"]),
            claim_token=claim_token,
            worker_id=owner,
            lease_expires_at=lease_expires_at,
            dispatch_attempt=int(row["replay_dispatch_attempts"]) + 1,
        )
    # 数据库或持久字段损坏时回滚整个领取事务，绝不暴露 SQL、token 或 owner 细节。
    except (sqlite3.Error, TypeError, ValueError) as exc:
        connection.rollback()
        # claim 失败只公开固定错误码，SQL、数据库路径、token 和 owner 均不得沿异常外泄。
        raise ApiError("webhook.dead_letter_unavailable") from exc
    finally:
        connection.close()


def process_claimed_replay(
    config: AppConfig,
    claim: ReplayClaim,
    *,
    now: datetime | None = None,
) -> bool:
    """恢复已领取 replay，并以 tenant/dead-letter/delivery/token/owner 五重 CAS 提交终态。

    返回 True 表示当前 claim 提交 replayed/dead_letter，False 表示已经失权。
    可分类的 ApiError 会按 1/5/30/300 秒退避；进程异常保留 lease，供到期接管。
    """

    connection = _connect(config)
    try:
        load_time = _as_utc(now or _utcnow())
        owned = _load_owned_dead_letter(connection, claim, now=load_time)
        if owned is None:
            return False
        delivery = webhook_service._load_delivery_row(
            connection,
            claim.tenant_id,
            str(owned["webhook_endpoint_id"]),
            claim.replay_delivery_id,
        )
        # intent 指向的 delivery 缺失说明持久事实不完整，不能猜测正文或改写根死信状态。
        if delivery is None:
            raise ApiError("webhook.replay_unavailable")
        result = webhook_service._resume_pending_delivery(
            config,
            connection,
            delivery,
            request_id=f"worker_{claim.dead_letter_event_id}",
            trace_id=f"worker_{claim.replay_delivery_id}",
        )
        # 生产请求可能在出网期间跨过 lease 到期点；提交前重新取时钟，旧 worker 必须立即失权。
        # 测试显式注入 now 时仍复用固定时间，保证边界用例可确定地推进。
        finish_time = _as_utc(now or _utcnow())
        return _finish_claim(
            connection,
            claim,
            replay_succeeded=result.status == "success",
            error_code=result.error_code,
            now=finish_time,
        )
    # 可分类领域失败会释放当前 lease 并写有界退避，避免同一坏任务形成忙循环。
    except ApiError as exc:
        connection.rollback()
        connection.close()
        _reschedule_claim(config, claim, error_code=exc.code, now=now)
        # 对外只返回 replay 固定错误，底层分类已经安全保存到死信状态供排障使用。
        raise ApiError("webhook.replay_unavailable") from exc
    # 基础设施或持久值异常无法证明 owner，必须保留 lease 让到期接管而不是提前重试。
    except (sqlite3.Error, TypeError, ValueError) as exc:
        connection.rollback()
        # 无法证明 owner 时保留 lease；到期后新 token 可恢复，旧 worker 的 finish 必然不命中。
        raise ApiError("webhook.replay_unavailable") from exc
    finally:
        try:
            connection.close()
        # 重复关闭失败不改变已经提交或回滚的业务终态，且不得覆盖原始统一错误。
        except sqlite3.Error:
            pass


def expire_dead_letters(
    config: AppConfig,
    *,
    now: datetime | None = None,
) -> int:
    """原子标记已到期且无活跃 replay 的死信，保留全部 delivery、replay 和 attempt 证据。"""

    current_text = _to_db_time(_as_utc(now or _utcnow()))
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """
            UPDATE webhook_dead_letter_events
            SET status='expired', replay_claim_token=NULL, replay_lease_owner_id=NULL,
                replay_lease_expires_at=NULL, replay_next_attempt_at=NULL, updated_at=?
            WHERE status IN ('dead_letter','replayed') AND expires_at<=?
            """,
            (current_text, current_text),
        )
        connection.commit()
        return max(int(cursor.rowcount), 0)
    # 过期批处理必须全量回滚，避免部分死信过期后让列表与 replay 权限状态不一致。
    except sqlite3.Error as exc:
        connection.rollback()
        raise ApiError("webhook.dead_letter_unavailable") from exc
    finally:
        connection.close()


def _load_owned_dead_letter(
    connection: sqlite3.Connection,
    claim: ReplayClaim,
    *,
    now: datetime,
) -> sqlite3.Row | None:
    """按五重 owner 与未过期条件回读当前 lease，不接受部分标识命中。"""

    return connection.execute(
        """
        SELECT * FROM webhook_dead_letter_events
        WHERE tenant_id=? AND dead_letter_event_id=? AND status='replay_pending'
          AND current_replay_delivery_id=? AND replay_claim_token=? AND replay_lease_owner_id=?
          AND replay_lease_expires_at>?
        """,
        (
            claim.tenant_id,
            claim.dead_letter_event_id,
            claim.replay_delivery_id,
            claim.claim_token,
            claim.worker_id,
            _to_db_time(now),
        ),
    ).fetchone()


def _finish_claim(
    connection: sqlite3.Connection,
    claim: ReplayClaim,
    *,
    replay_succeeded: bool,
    error_code: str | None,
    now: datetime,
) -> bool:
    """以 claim 五元组 CAS 提交 replay 终态，旧 worker 失权时只返回 False。"""

    now_text = _to_db_time(now)
    cursor = connection.execute(
        """
        UPDATE webhook_dead_letter_events
        SET status=?, replay_claim_token=NULL, replay_lease_owner_id=NULL,
            replay_lease_expires_at=NULL, replay_next_attempt_at=NULL,
            last_replay_error_code=?, replayed_at=?, updated_at=?
        WHERE tenant_id=? AND dead_letter_event_id=? AND status='replay_pending'
          AND current_replay_delivery_id=? AND replay_claim_token=? AND replay_lease_owner_id=?
          AND replay_lease_expires_at>?
        """,
        (
            "replayed" if replay_succeeded else "dead_letter",
            None if replay_succeeded else (error_code or "webhook.delivery_failed"),
            now_text if replay_succeeded else None,
            now_text,
            claim.tenant_id,
            claim.dead_letter_event_id,
            claim.replay_delivery_id,
            claim.claim_token,
            claim.worker_id,
            now_text,
        ),
    )
    connection.commit()
    return cursor.rowcount == 1


def _reschedule_claim(
    config: AppConfig,
    claim: ReplayClaim,
    *,
    error_code: str,
    now: datetime | None,
) -> None:
    """只允许仍持有同一五元组的 worker 清 lease，并写固定错误码与有界退避。"""

    current_time = _as_utc(now or _utcnow())
    index = min(max(claim.dispatch_attempt - 1, 0), len(REPLAY_BACKOFF_SECONDS) - 1)
    next_attempt = current_time + timedelta(seconds=REPLAY_BACKOFF_SECONDS[index])
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE webhook_dead_letter_events
            SET replay_claim_token=NULL, replay_lease_owner_id=NULL, replay_lease_expires_at=NULL,
                replay_next_attempt_at=?, last_replay_error_code=?, updated_at=?
            WHERE tenant_id=? AND dead_letter_event_id=? AND status='replay_pending'
              AND current_replay_delivery_id=? AND replay_claim_token=? AND replay_lease_owner_id=?
              AND replay_lease_expires_at>?
            """,
            (
                _to_db_time(next_attempt),
                _safe_worker_error_code(error_code),
                _to_db_time(current_time),
                claim.tenant_id,
                claim.dead_letter_event_id,
                claim.replay_delivery_id,
                claim.claim_token,
                claim.worker_id,
                _to_db_time(current_time),
            ),
        )
        connection.commit()
    # 退避写入失败必须回滚，保留原 lease 后由超时接管，不能制造无 owner 的立即重试。
    except sqlite3.Error as exc:
        connection.rollback()
        raise ApiError("webhook.dead_letter_unavailable") from exc
    finally:
        connection.close()


def _lease_seconds(*, attempt_count: int, max_attempts: int, timeout_ms: int) -> int:
    """计算覆盖剩余 attempts 的租期，对损坏计数使用保守有界值。"""

    remaining = max(1, min(3, max_attempts) - max(0, attempt_count))
    bounded_timeout = min(max(timeout_ms, 1000), 30000)
    required = math.ceil((remaining * bounded_timeout) / 1000) + LEASE_SAFETY_SECONDS
    return min(max(required, MINIMUM_LEASE_SECONDS), MAXIMUM_LEASE_SECONDS)


def _safe_worker_error_code(value: object) -> str:
    """折叠内部失败为有界固定 code，禁止异常正文和数据库信息进入死信表。"""

    code = str(value or "webhook.replay_unavailable")
    if not code.startswith("webhook.") or len(code) > 120:
        return "webhook.replay_unavailable"
    return code


def _connect(config: AppConfig) -> sqlite3.Connection:
    """创建 worker 短连接，并把底层数据库错误转换为死信统一错误码。"""

    try:
        return connect_database(config.database_url)
    # 连接配置和数据库路径属于部署细节，统一折叠后才能进入 scheduler 或 API 响应。
    except DbError as exc:
        raise ApiError("webhook.dead_letter_unavailable") from exc


def _as_utc(value: datetime) -> datetime:
    """规范化测试注入时间与运行时间，数据库只写秒级 UTC。"""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC, microsecond=0)
    return value.astimezone(UTC).replace(microsecond=0)


def _utcnow() -> datetime:
    """返回秒级 UTC 当前时间，避免同一 CAS 因微秒格式差异产生含糊比较。"""

    return datetime.now(UTC).replace(microsecond=0)


def _to_db_time(value: datetime) -> str:
    """把 UTC 时间转换为 SQLite 可比较的 ISO 文本。"""

    return _as_utc(value).isoformat().replace("+00:00", "Z")


__all__ = [
    "ReplayClaim",
    "claim_next_replay",
    "expire_dead_letters",
    "process_claimed_replay",
]
