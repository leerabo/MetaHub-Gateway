"""管理助手稳定事件的可恢复 source outbox，负责租约、CAS、退避和终态清理。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from secrets import token_hex

from metahub_shared.db.connection import connect_database
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import build_assistant_queried_event

from metahub_control.webhooks.service import (
    dispatch_webhook_event,
    fail_pending_webhook_event_deliveries,
)

# 租约、最大尝试和退避是 source 状态机的固定合同；错误集合只接受安全机器码，
# 不允许把 Endpoint、响应正文或底层异常写入 AssistantAudit。
EVENT_LEASE_SECONDS = 90
EVENT_MAX_ATTEMPTS = 10
EVENT_BACKOFF_SECONDS = (5, 30, 300)
_FAILURE_CODES = {
    "webhook.event_type_invalid",
    "webhook.payload_invalid",
    "webhook.payload_sensitive",
    "webhook.delivery_unavailable",
    "admin_assistant.event_unavailable",
}


@dataclass(frozen=True, slots=True)
class AssistantEventClaim:
    """一次 tenant-qualified source 领取结果，私有 token 与 owner 共同构成 CAS 身份。"""

    assistant_audit_id: str
    assistant_event_id: str
    assistant_query_id: str
    tenant_id: str
    actor_user_id: str
    query_type: str
    outcome: str
    claim_count: int
    source_count: int
    redaction_count: int
    duration_ms: int
    operation_request_id: str
    operation_trace_id: str
    created_at: datetime
    claim_token: str
    lease_owner_id: str
    attempts: int
    lease_expires_at: datetime


def claim_next_assistant_event(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> AssistantEventClaim | None:
    """领取一个到期 source；过期第十次 attempt 会与 pending delivery 原子终结。"""

    current = _now(now)
    timestamp = _db_time(current)
    # worker owner 属于本次调用参数；必须在读取候选行前校验，避免无效调用方把健康 source
    # 误判为坏数据并触发隔离。
    owner = _worker_id(worker_id)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "CREATE TEMP TABLE prd077_bad_source_skip(source_rowid INTEGER PRIMARY KEY)"
        )
        # 第十次 attempt 若在终态提交前崩溃，租约过期后必须直接失败关闭，不能发生
        # 第十一次网络调用；同事务还会终结该 source 已创建的 original pending intent。
        _finish_one_exhausted_source(connection, timestamp=timestamp)
        # 物理损坏可能绕过 trigger 留下一条 SQLite 可比较、但 Python 无法安全解释的最早行。
        # keyset 游标保证该行即使无法原子终结，也只在本轮被跳过，后续 tenant 仍能被领取。
        while True:
            row = connection.execute(
                """
                SELECT audit.rowid AS source_rowid,audit.*,query.query_type
                FROM assistant_audits audit
                JOIN assistant_queries query
                  ON query.tenant_id=audit.tenant_id
                 AND query.assistant_query_id=audit.assistant_query_id
                WHERE audit.event_dispatch_status IN ('pending','processing')
                  AND audit.event_dispatch_attempts<?
                  AND ((audit.event_dispatch_status='pending'
                        AND unixepoch(audit.event_next_attempt_at)<=unixepoch(?))
                    OR (audit.event_dispatch_status='processing'
                        AND unixepoch(audit.event_lease_expires_at)<=unixepoch(?)))
                  AND audit.rowid NOT IN(
                    SELECT source_rowid FROM temp.prd077_bad_source_skip
                  )
                ORDER BY audit.created_at,audit.assistant_audit_id LIMIT 1
                """,
                (
                    EVENT_MAX_ATTEMPTS,
                    timestamp,
                    timestamp,
                ),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            source_rowid = _rowid(row["source_rowid"])
            try:
                status = _text(row["event_dispatch_status"])
                attempts = _int(row["event_dispatch_attempts"]) + 1
                updated_at = _parse_time(row["updated_at"])
                created_at = _parse_time(row["created_at"])
                if status == "pending":
                    _parse_time(row["event_next_attempt_at"])
                elif status == "processing":
                    _parse_time(row["event_lease_expires_at"])
                else:
                    # 未知状态无法证明 source 可领取，禁止把损坏终态重新解释为待投递事件。
                    raise ValueError("status")
                candidate = {
                    "assistant_audit_id": _text(row["assistant_audit_id"]),
                    "assistant_event_id": _text(row["assistant_event_id"]),
                    "assistant_query_id": _text(row["assistant_query_id"]),
                    "tenant_id": _text(row["tenant_id"]),
                    "actor_user_id": _text(row["actor_user_id"]),
                    "query_type": _text(row["query_type"]),
                    "outcome": _text(row["outcome"]),
                    "claim_count": _int(row["claim_count"]),
                    "source_count": _int(row["source_count"]),
                    "redaction_count": _int(row["redaction_count"]),
                    "duration_ms": _int(row["query_duration_ms"]),
                    "operation_request_id": _text(row["operation_request_id"]),
                    "operation_trace_id": _text(row["operation_trace_id"]),
                }
            # 任一持久字段类型漂移都不能进入事件构造或租约 CAS，只允许隔离当前坏行。
            except (TypeError, ValueError):
                # 能由 0070 识别为非 canonical 时间的坏 source 会原子失败关闭；更深的物理
                # 损坏若无法安全更新，也仍通过 keyset 游标隔离，不能阻断后续租户。
                _try_quarantine_bad_source(connection, row, timestamp=timestamp)
                connection.execute(
                    "INSERT INTO temp.prd077_bad_source_skip(source_rowid) VALUES(?)",
                    (source_rowid,),
                )
                continue
            token = token_hex(32)
            claim_time = max(current, updated_at + timedelta(seconds=1))
            lease_expires = claim_time + timedelta(seconds=EVENT_LEASE_SECONDS)
            updated = connection.execute(
                """
                UPDATE assistant_audits
                SET event_dispatch_status='processing',event_dispatch_attempts=?,
                    event_next_attempt_at=NULL,event_dispatched_at=NULL,event_claim_token=?,
                    event_lease_owner_id=?,event_lease_expires_at=?,event_last_error_code=NULL,
                    updated_at=?
                WHERE assistant_audit_id=? AND event_dispatch_attempts=?
                  AND ((event_dispatch_status='pending'
                        AND unixepoch(event_next_attempt_at)<=unixepoch(?))
                    OR (event_dispatch_status='processing'
                        AND unixepoch(event_lease_expires_at)<=unixepoch(?)))
                """,
                (
                    attempts,
                    token,
                    owner,
                    _db_time(lease_expires),
                    _db_time(claim_time),
                    candidate["assistant_audit_id"],
                    attempts - 1,
                    timestamp,
                    timestamp,
                ),
            )
            if updated.rowcount != 1:
                connection.rollback()
                return None
            claim = AssistantEventClaim(
                assistant_audit_id=candidate["assistant_audit_id"],
                assistant_event_id=candidate["assistant_event_id"],
                assistant_query_id=candidate["assistant_query_id"],
                tenant_id=candidate["tenant_id"],
                actor_user_id=candidate["actor_user_id"],
                query_type=candidate["query_type"],
                outcome=candidate["outcome"],
                claim_count=candidate["claim_count"],
                source_count=candidate["source_count"],
                redaction_count=candidate["redaction_count"],
                duration_ms=candidate["duration_ms"],
                operation_request_id=candidate["operation_request_id"],
                operation_trace_id=candidate["operation_trace_id"],
                created_at=created_at,
                claim_token=token,
                lease_owner_id=owner,
                attempts=attempts,
                lease_expires_at=lease_expires,
            )
            connection.commit()
            return claim
    # 数据库、时间或持久类型损坏均折叠为固定事件错误，不能泄露 SQL、路径或坏行内容。
    except ApiError:
        _rollback(connection)
        raise
    # 事务、时间或字段解析失败均隐藏底层细节，并确保本轮领取事务完整回滚。
    except (sqlite3.Error, TypeError, ValueError) as exc:
        _rollback(connection)
        # 调度器只消费稳定机器码，并由后续租约恢复决定是否重试当前 source。
        raise ApiError("admin_assistant.event_unavailable") from exc
    finally:
        connection.close()


def _try_quarantine_bad_source(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    timestamp: str,
) -> bool:
    """尽力原子失败关闭坏 source；无法证明 owner 时回滚局部更新并允许队列继续。"""

    try:
        assistant_audit_id = _text(row["assistant_audit_id"])
        status = _text(row["event_dispatch_status"])
        attempts = _int(row["event_dispatch_attempts"])
    # 连 source 主键或状态都无法证明时不能按路径猜测更新，只返回未隔离结果。
    except (TypeError, ValueError):
        return False
    connection.execute("SAVEPOINT prd077_bad_source")
    try:
        quarantined = connection.execute(
            """
            UPDATE assistant_audits
            SET event_dispatch_status='failed',event_dispatch_attempts=10,
                event_next_attempt_at=NULL,event_dispatched_at=NULL,event_claim_token=NULL,
                event_lease_owner_id=NULL,event_lease_expires_at=NULL,
                event_last_error_code='admin_assistant.event_unavailable',
                updated_at=strftime('%Y-%m-%dT%H:%M:%SZ',max(
                  coalesce(unixepoch(updated_at)+1,unixepoch(created_at)+1,unixepoch(?)),
                  unixepoch(?)
                ),'unixepoch')
            WHERE assistant_audit_id=? AND event_dispatch_status=?
              AND event_dispatch_attempts=?
            RETURNING tenant_id,assistant_event_id,updated_at
            """,
            (timestamp, timestamp, assistant_audit_id, status, attempts),
        ).fetchone()
        if quarantined is None:
            # CAS 未命中说明 owner 图已经漂移，本 savepoint 不得继续清理关联 delivery。
            raise ValueError("source")
        fail_pending_webhook_event_deliveries(
            connection,
            tenant_id=quarantined["tenant_id"],
            event_id=quarantined["assistant_event_id"],
            event_type="assistant.queried",
            updated_at=_text(quarantined["updated_at"]),
            failure_code="webhook.delivery_unavailable",
        )
        connection.execute("RELEASE SAVEPOINT prd077_bad_source")
        return True
    # 隔离 source 与关闭 pending delivery 必须同 savepoint 成败，禁止留下半终态。
    except (ApiError, sqlite3.Error, TypeError, ValueError):
        try:
            connection.execute("ROLLBACK TO SAVEPOINT prd077_bad_source")
            connection.execute("RELEASE SAVEPOINT prd077_bad_source")
        # savepoint 自身无法恢复时外层领取事务已不可证明，必须提升为固定领域失败。
        except sqlite3.Error as exc:
            # 无法恢复 savepoint 时整笔领取事务已不可信，只公开固定 source 错误。
            raise ApiError("admin_assistant.event_unavailable") from exc
        return False


def _finish_one_exhausted_source(
    connection: sqlite3.Connection,
    *,
    timestamp: str,
) -> bool:
    """终结一个过期第十次 claim，并跳过无法安全解释的坏行。"""

    while True:
        row = connection.execute(
            """
            SELECT rowid AS source_rowid,* FROM assistant_audits
            WHERE event_dispatch_status='processing' AND event_dispatch_attempts=10
              AND unixepoch(event_lease_expires_at)<=unixepoch(?)
              AND rowid NOT IN(
                SELECT source_rowid FROM temp.prd077_bad_source_skip
              )
            ORDER BY event_lease_expires_at,created_at,assistant_audit_id LIMIT 1
            """,
            (timestamp,),
        ).fetchone()
        if row is None:
            return False
        source_rowid = _rowid(row["source_rowid"])
        try:
            assistant_audit_id = _text(row["assistant_audit_id"])
            tenant_id = _text(row["tenant_id"])
            event_id = _text(row["assistant_event_id"])
            token = _text(row["event_claim_token"])
            owner = _text(row["event_lease_owner_id"])
            lease = _db_time(_parse_time(row["event_lease_expires_at"]))
        # 第十次 claim 的 owner 字段损坏时只隔离该 rowid，不能饿死后续租户 source。
        except (TypeError, ValueError):
            _try_quarantine_bad_source(connection, row, timestamp=timestamp)
            connection.execute(
                "INSERT INTO temp.prd077_bad_source_skip(source_rowid) VALUES(?)",
                (source_rowid,),
            )
            continue
        exhausted = connection.execute(
            """
            UPDATE assistant_audits
            SET event_dispatch_status='failed',event_next_attempt_at=NULL,
                event_dispatched_at=NULL,event_claim_token=NULL,event_lease_owner_id=NULL,
                event_lease_expires_at=NULL,
                event_last_error_code='admin_assistant.event_unavailable',updated_at=?
            WHERE tenant_id=? AND assistant_audit_id=? AND assistant_event_id=?
              AND event_dispatch_status='processing' AND event_dispatch_attempts=10
              AND event_claim_token=? AND event_lease_owner_id=?
              AND event_lease_expires_at=?
              AND unixepoch(event_lease_expires_at)<=unixepoch(?)
            RETURNING tenant_id,assistant_event_id
            """,
            (
                timestamp,
                tenant_id,
                assistant_audit_id,
                event_id,
                token,
                owner,
                lease,
                timestamp,
            ),
        ).fetchone()
        if exhausted is None:
            return False
        # Source 与 original delivery 必须共享同一个终态事务；否则 worker 崩溃会留下
        # 永远 pending、但再也没有 source attempt 可以推进的孤儿投递。
        fail_pending_webhook_event_deliveries(
            connection,
            tenant_id=tenant_id,
            event_id=event_id,
            event_type="assistant.queried",
            updated_at=timestamp,
            failure_code="webhook.delivery_unavailable",
        )
        return True


def dispatch_claimed_assistant_event(
    config: AppConfig,
    claim: AssistantEventClaim,
    *,
    now: datetime | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """只由仍持有完整 source lease 的 worker 投递，并以最新 lease CAS 收尾。"""

    current = _now(now)
    # Claim 返回后线程可能停顿到租约过期，或同一 source 已被新 worker 接管；构造事件和
    # 调用 dispatcher 前先复核完整身份，迟到 worker 必须保持零网络、零状态改写。
    active_updated_at = _active_source_updated_at(config, claim, current)
    if active_updated_at is None:
        return False
    active_lease_expires_at = claim.lease_expires_at

    def preserve_source_lease() -> None:
        """每次真实 HTTP attempt 前协作停服并对旧 lease 值执行严格 CAS 续租。"""

        nonlocal active_lease_expires_at, active_updated_at
        if should_stop is not None and should_stop():
            # 停服信号到达后禁止开始新的真实网络调用，让当前 source 由下次启动恢复。
            raise ApiError("admin_assistant.event_unavailable")
        active_lease_expires_at, active_updated_at = _renew_source_lease(
            config,
            claim,
            expected_lease_expires_at=active_lease_expires_at,
            current=_now(now),
        )

    error_code: str | None = None
    try:
        if should_stop is not None and should_stop():
            # 停服若先于首个 attempt 到达，不读取 Endpoint；本轮释放 lease 后由下次启动恢复。
            raise ApiError("admin_assistant.event_unavailable")
        event = build_assistant_queried_event(
            tenant_id=claim.tenant_id,
            actor_id=claim.actor_user_id,
            assistant_query_id=claim.assistant_query_id,
            event_id=claim.assistant_event_id,
            trace_id=claim.operation_trace_id,
            source_request_id=claim.operation_request_id,
            query_type=claim.query_type,
            outcome=claim.outcome,
            claim_count=claim.claim_count,
            source_count=claim.source_count,
            redaction_count=claim.redaction_count,
            duration_ms=claim.duration_ms,
            timestamp=claim.created_at,
        )
        dispatch_webhook_event(
            config,
            claim.tenant_id,
            event,
            request_id=claim.operation_request_id,
            trace_id=claim.operation_trace_id,
            resume_existing_pending=True,
            source_lease_guard=preserve_source_lease,
            create_snapshot_if_missing=False,
        )
    # Shared validator 的自由异常文本不得持久化，只允许已知安全码参与当前内存分支。
    except ValueError as exc:
        candidate = str(exc)
        error_code = candidate if candidate in _FAILURE_CODES else "webhook.event_type_invalid"
    # Webhook 服务错误可能来自 Endpoint、密钥或数据库，source 只记录固定不可用码。
    except ApiError:
        error_code = "webhook.delivery_unavailable"
    except Exception:
        error_code = "webhook.delivery_unavailable"
    completed = max(
        _now(now),
        claim.created_at + timedelta(seconds=1),
        active_updated_at + timedelta(seconds=1),
    )
    return _finish(
        config,
        claim,
        active_lease_expires_at,
        completed,
        error_code,
    )


def drain_assistant_event_once(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """执行一次有界 claim/dispatch；返回值仅表示是否领取过 source，不代表网络成功。"""

    if should_stop is not None and should_stop():
        return False
    claim = claim_next_assistant_event(config, worker_id=worker_id, now=now)
    if claim is None:
        return False
    dispatch_claimed_assistant_event(
        config,
        claim,
        now=now,
        should_stop=should_stop,
    )
    return True


def _active_source_updated_at(
    config: AppConfig,
    claim: AssistantEventClaim,
    current: datetime,
) -> datetime | None:
    """出网前复核完整 claim 与 exact lease，并返回当前 source 更新时间。"""

    connection = _connect(config)
    try:
        row = connection.execute(
            """
            SELECT updated_at FROM assistant_audits
            WHERE tenant_id=? AND assistant_audit_id=? AND assistant_event_id=?
              AND assistant_query_id=? AND event_dispatch_status='processing'
              AND event_dispatch_attempts=? AND event_claim_token=?
              AND event_lease_owner_id=? AND event_lease_expires_at=?
              AND unixepoch(event_lease_expires_at)>unixepoch(?)
            """,
            (
                claim.tenant_id,
                claim.assistant_audit_id,
                claim.assistant_event_id,
                claim.assistant_query_id,
                claim.attempts,
                claim.claim_token,
                claim.lease_owner_id,
                _db_time(claim.lease_expires_at),
                _db_time(current),
            ),
        ).fetchone()
        return _parse_time(row["updated_at"]) if row is not None else None
    # 出网前预检无法证明 exact claim 时必须零网络失败，不能退化为信任内存 claim。
    except (sqlite3.Error, TypeError, ValueError) as exc:
        # 预检不可用时禁止降级为盲目 resume；领域边界不公开 SQL、路径或持久行内容。
        raise ApiError("admin_assistant.event_unavailable") from exc
    finally:
        connection.close()


def _renew_source_lease(
    config: AppConfig,
    claim: AssistantEventClaim,
    *,
    expected_lease_expires_at: datetime,
    current: datetime,
) -> tuple[datetime, datetime]:
    """以完整 source 身份和旧 lease 值 CAS 续租，返回最新 lease 与更新时间。"""

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT updated_at,event_lease_expires_at FROM assistant_audits
            WHERE tenant_id=? AND assistant_audit_id=? AND assistant_event_id=?
              AND assistant_query_id=? AND event_dispatch_status='processing'
              AND event_dispatch_attempts=? AND event_claim_token=?
              AND event_lease_owner_id=? AND event_lease_expires_at=?
              AND unixepoch(event_lease_expires_at)>unixepoch(?)
            """,
            (
                claim.tenant_id,
                claim.assistant_audit_id,
                claim.assistant_event_id,
                claim.assistant_query_id,
                claim.attempts,
                claim.claim_token,
                claim.lease_owner_id,
                _db_time(expected_lease_expires_at),
                _db_time(current),
            ),
        ).fetchone()
        if row is None:
            connection.rollback()
            # 完整身份或活跃 lease 任一不匹配都代表当前 worker 已失去投递所有权。
            raise ApiError("admin_assistant.event_unavailable")
        old_updated_at = _parse_time(row["updated_at"])
        old_lease_expires_at = _parse_time(row["event_lease_expires_at"])
        if old_lease_expires_at != expected_lease_expires_at:
            connection.rollback()
            # 数据库 lease 与 worker 预期不一致时禁止覆盖新 owner 已推进的租约。
            raise ApiError("admin_assistant.event_unavailable")
        # Trigger 要求同 owner 续租严格推进两个时间；冻结测试时钟下也必须生成下一秒，
        # 同时保持旧 lease 在 renewed_at 时仍有效，不能复活已经过期的 claim。
        renewed_at = max(current, old_updated_at + timedelta(seconds=1))
        if old_lease_expires_at <= renewed_at:
            connection.rollback()
            # 已到期的旧 lease 不能借续租复活，否则迟到 worker 可能与新 owner 同时出网。
            raise ApiError("admin_assistant.event_unavailable")
        renewed_expires_at = max(
            old_lease_expires_at + timedelta(seconds=1),
            renewed_at + timedelta(seconds=EVENT_LEASE_SECONDS),
        )
        updated = connection.execute(
            """
            UPDATE assistant_audits
            SET event_lease_expires_at=?,updated_at=?
            WHERE tenant_id=? AND assistant_audit_id=? AND assistant_event_id=?
              AND assistant_query_id=? AND event_dispatch_status='processing'
              AND event_dispatch_attempts=? AND event_claim_token=?
              AND event_lease_owner_id=? AND event_lease_expires_at=?
              AND unixepoch(event_lease_expires_at)>unixepoch(?)
            """,
            (
                _db_time(renewed_expires_at),
                _db_time(renewed_at),
                claim.tenant_id,
                claim.assistant_audit_id,
                claim.assistant_event_id,
                claim.assistant_query_id,
                claim.attempts,
                claim.claim_token,
                claim.lease_owner_id,
                _db_time(old_lease_expires_at),
                _db_time(current),
            ),
        )
        if updated.rowcount != 1:
            connection.rollback()
            # exact CAS 未更新唯一 source 时，续租结果不可信且不得开始本次网络尝试。
            raise ApiError("admin_assistant.event_unavailable")
        connection.commit()
        return renewed_expires_at, renewed_at
    # 已归一的租约失败保留机器码，但仍需回滚可能已开启的短写事务。
    except ApiError:
        _rollback(connection)
        raise
    # SQLite 或 canonical 时间漂移只公开固定事件错误，避免泄露坏行和数据库结构。
    except (sqlite3.Error, TypeError, ValueError) as exc:
        _rollback(connection)
        # 租约事务无法完整提交时，调用方只能收到安全且可恢复的统一错误码。
        raise ApiError("admin_assistant.event_unavailable") from exc
    finally:
        connection.close()


def _finish(
    config: AppConfig,
    claim: AssistantEventClaim,
    expected_lease_expires_at: datetime,
    completed_at: datetime,
    error_code: str | None,
) -> bool:
    """使用完整 source 身份和最新 exact lease CAS 提交唯一下一状态。"""

    final = error_code is not None and claim.attempts >= EVENT_MAX_ATTEMPTS
    status = "dispatched" if error_code is None else ("failed" if final else "pending")
    delay = EVENT_BACKOFF_SECONDS[min(max(claim.attempts - 1, 0), 2)]
    timestamp = _db_time(completed_at)
    next_attempt = (
        _db_time(completed_at + timedelta(seconds=delay))
        if error_code is not None and not final
        else None
    )
    # 具体 Webhook 错误只用于本次内存分支；持久 source 统一为固定域错误，避免下游错误
    # 细节进入验收证据，也与 0070 的有限枚举保持一致。
    reason = None if error_code is None else "admin_assistant.event_unavailable"
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        updated = connection.execute(
            """
            UPDATE assistant_audits
            SET event_dispatch_status=?,event_next_attempt_at=?,event_dispatched_at=?,
                event_claim_token=NULL,event_lease_owner_id=NULL,event_lease_expires_at=NULL,
                event_last_error_code=?,updated_at=?
            WHERE tenant_id=? AND assistant_audit_id=? AND assistant_event_id=?
              AND assistant_query_id=? AND event_dispatch_status='processing'
              AND event_dispatch_attempts=? AND event_claim_token=?
              AND event_lease_owner_id=? AND event_lease_expires_at=?
              AND unixepoch(event_lease_expires_at)>unixepoch(?)
            """,
            (
                status,
                next_attempt,
                timestamp if error_code is None else None,
                reason,
                timestamp,
                claim.tenant_id,
                claim.assistant_audit_id,
                claim.assistant_event_id,
                claim.assistant_query_id,
                claim.attempts,
                claim.claim_token,
                claim.lease_owner_id,
                _db_time(expected_lease_expires_at),
                timestamp,
            ),
        )
        if updated.rowcount != 1:
            connection.rollback()
            return False
        if final:
            fail_pending_webhook_event_deliveries(
                connection,
                tenant_id=claim.tenant_id,
                event_id=claim.assistant_event_id,
                event_type="assistant.queried",
                updated_at=timestamp,
                failure_code="webhook.delivery_unavailable",
            )
        connection.commit()
        return True
    # CAS 提交和 pending cleanup 任一数据库错误都回滚同一终态事务。
    except sqlite3.Error as exc:
        _rollback(connection)
        # 对外只公开固定 source 错误，索引名、SQL 与 delivery 摘要不得进入日志或证据。
        raise ApiError("admin_assistant.event_unavailable") from exc
    finally:
        connection.close()


def recover_assistant_source_request_id(
    connection: sqlite3.Connection,
    envelope: dict[str, object],
) -> str:
    """从不可变 Query/Audit 图恢复非公开 request owner，并拒绝被篡改的 delivery。"""

    expected = {
        "event_id",
        "event_type",
        "timestamp",
        "tenant_id",
        "actor_id",
        "object_id",
        "trace_id",
        "payload",
    }
    # delivery 信封必须是 webhook exact schema，未知字段可能伪造 source owner 或携带正文。
    if type(envelope) is not dict or set(envelope) != expected:
        raise ApiError("admin_assistant.event_unavailable")
    row = connection.execute(
        """
        SELECT audit.assistant_event_id,audit.tenant_id,audit.assistant_query_id,
               audit.actor_user_id,audit.operation_request_id,audit.operation_trace_id,
               audit.outcome,audit.claim_count,audit.source_count,audit.redaction_count,
               audit.query_duration_ms,audit.created_at,query.query_type
        FROM assistant_audits audit
        JOIN assistant_queries query
          ON query.tenant_id=audit.tenant_id
         AND query.assistant_query_id=audit.assistant_query_id
         AND query.actor_user_id=audit.actor_user_id
         AND query.actor_session_id=audit.actor_session_id
        WHERE audit.tenant_id=? AND audit.assistant_event_id=?
          AND audit.assistant_query_id=?
        """,
        (envelope.get("tenant_id"), envelope.get("event_id"), envelope.get("object_id")),
    ).fetchone()
    # source 行不存在时不能信任 delivery 自报的 request owner，也不能凭信封继续投递。
    if row is None:
        raise ApiError("admin_assistant.event_unavailable")
    try:
        event = build_assistant_queried_event(
            tenant_id=str(row["tenant_id"]),
            actor_id=str(row["actor_user_id"]),
            assistant_query_id=str(row["assistant_query_id"]),
            event_id=str(row["assistant_event_id"]),
            trace_id=str(row["operation_trace_id"]),
            source_request_id=str(row["operation_request_id"]),
            query_type=str(row["query_type"]),
            outcome=str(row["outcome"]),
            claim_count=_int(row["claim_count"]),
            source_count=_int(row["source_count"]),
            redaction_count=_int(row["redaction_count"]),
            duration_ms=_int(row["query_duration_ms"]),
            timestamp=_parse_time(row["created_at"]),
        )
    # 任一类型或时间解析漂移都说明不可变审计图无法重建有效事件，统一失败关闭。
    except (TypeError, ValueError):
        raise ApiError("admin_assistant.event_unavailable") from None
    if event.to_payload() != envelope:
        # actor、trace、发生时间和七字段 payload 都必须来自同一 source row；不能为了恢复
        # request_id 而信任 delivery 自己携带的可篡改副本。
        raise ApiError("admin_assistant.event_unavailable")
    return str(row["operation_request_id"])


def _connect(config: AppConfig) -> sqlite3.Connection:
    """连接部署配置指定的 SQLite，并把路径或驱动异常折叠为固定 source 错误。"""

    try:
        return connect_database(config.database_url)
    # 数据库 URL 与本机路径属于部署信息，不能随底层异常越过 outbox 边界。
    except Exception as exc:
        raise ApiError("admin_assistant.event_unavailable") from exc


def _worker_id(value: str) -> str:
    """校验只在数据库内部使用的有界 worker owner，拒绝控制字符和空身份。"""

    # owner 会进入租约 CAS；类型、长度或控制字符不合法时必须在写入前失败关闭。
    if type(value) is not str or not 1 <= len(value) <= 128 or any(ord(c) < 0x20 for c in value):
        raise ApiError("admin_assistant.event_unavailable")
    return value


def _int(value: object) -> int:
    """读取 SQLite 非负整数计数，显式拒绝 bool、文本和负数混淆。"""

    # 事件计数会被外发，必须 exact type 校验，不能让 true 冒充一次事实。
    if type(value) is not int or value < 0:
        raise ValueError("integer")
    return value


def _rowid(value: object) -> int:
    """验证 SQLite 内部 rowid 游标，只接受正整数。"""

    if type(value) is not int or value <= 0:
        # rowid 用于本轮临时隔离游标，非正整数会导致跳过错误 source，必须拒绝。
        raise ValueError("rowid")
    return value


def _text(value: object) -> str:
    """读取持久非空文本，拒绝数字、bytes 和 bool 被 ``str`` 静默改写。"""

    if type(value) is not str or not value:
        # owner 与租户字段禁止隐式字符串化，避免数字或 bool 进入 exact CAS 身份。
        raise ValueError("text")
    return value


def _now(value: datetime | None) -> datetime:
    """取得 UTC 秒级 worker 时钟；测试注入也必须携带可信时区。"""

    # naive 时间无法证明租约先后关系，不能静默按本地时区解释。
    result = value or datetime.now(UTC)
    if result.tzinfo is None:
        raise ApiError("admin_assistant.event_unavailable")
    return result.astimezone(UTC).replace(microsecond=0)


def _parse_time(value: object) -> datetime:
    """只解析 canonical UTC 秒；offset、naive、空格或小数秒一律视为坏 source。"""

    # source 时间来自不可变 owner 图，非字符串不能容错转换。
    if type(value) is not str:
        raise ValueError("time")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # 缺少 offset 的时间会使 stale lease 判断依赖机器时区，必须拒绝。
    if result.tzinfo is None:
        raise ValueError("time")
    canonical = result.astimezone(UTC).replace(microsecond=0)
    if _db_time(canonical) != value:
        # 数据库只持久化 UTC 秒；接受等价 offset 或小数秒会让 SQL/Python 排序合同分裂。
        raise ValueError("time")
    return canonical


def _db_time(value: datetime) -> str:
    """把已验证时间规范化为数据库与 Webhook 共用的 canonical UTC 秒。"""

    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _rollback(connection: sqlite3.Connection) -> None:
    """仅在当前连接持有事务时回滚，供全部异常路径统一释放写锁。"""

    if connection.in_transaction:
        connection.rollback()


__all__ = [
    "AssistantEventClaim",
    "claim_next_assistant_event",
    "dispatch_claimed_assistant_event",
    "drain_assistant_event_once",
    "recover_assistant_source_request_id",
]
