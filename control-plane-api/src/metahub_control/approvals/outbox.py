"""PRD-078 审批事件与 Session Risk 回写恢复 worker。

每次 claim、投递和终态更新都同时校验 tenant_id、租约代际与所有者；载荷只保存冻结的
脱敏快照，令牌、密码、Prompt、Response 与 Provider Key 不得进入队列或日志。
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import WebhookEvent, build_webhook_event

from metahub_control.session_risks.service import (
    SessionRiskStoreSyncIntent,
    _build_store_sync_intent,
    get_session_risk_store,
    synchronize_session_risk_store,
)

# 队列租约、重试节奏及不透明标识格式共同限制重复投递和跨租户错误认领。
EVENT_LEASE_SECONDS = 90
WRITEBACK_LEASE_SECONDS = 90
MAX_ATTEMPTS = 10
BACKOFF_SECONDS = (5, 30, 300)
REDIS_SYNC_TIMEOUT_SECONDS = 30

_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_EVENT_ID = re.compile(r"^apevt_[0-9a-f]{32}$")
_OUTBOX_ID = re.compile(r"^apob_[0-9a-f]{32}$")
_APPROVAL_ID = re.compile(r"^apr_[0-9a-f]{32}$")
_WRITEBACK_ID = re.compile(r"^apwb_[0-9a-f]{32}$")
_ACTION_ID = re.compile(r"^sra_[0-9a-f]{32}$")


@dataclass(frozen=True, slots=True)
class ApprovalEventClaim:
    """One immutable approval source event held by an exact database lease.
    ``ApprovalEventClaim`` 维护审批事件或回写队列租约，tenant_id、代际和所有权必须在同一事务校验。
    """

    outbox_id: str
    event_id: str
    tenant_id: str
    approval_id: str
    source_request_id: str
    source_trace_id: str
    event: WebhookEvent = field(repr=False)
    attempt_count: int = 0
    lease_generation: int = 0
    lease_owner_id: str = ""
    lease_token: str = field(default="", repr=False)
    lease_expires_at: datetime | None = None
    snapshot_json: str = field(default="[]", repr=False)


@dataclass(frozen=True, slots=True)
class ApprovalWritebackClaim:
    """Exact persisted coordinates for one recoverable Session Risk projection.
    审批回写租约必须在同一事务校验 tenant_id、代际和所有权。
    """

    writeback_id: str
    tenant_id: str
    approval_id: str
    target_fact_id: str
    target_revision: int
    attempt_count: int
    lease_generation: int
    lease_owner_id: str
    lease_token: str = field(repr=False)
    lease_expires_at: datetime | None = None


def claim_next_approval_event(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> ApprovalEventClaim | None:
    """Claim one due event after atomically freezing its Webhook delivery snapshot.
    事件租约必须在同一事务校验 tenant_id、代际和所有权。
    """

    claim, _ = _claim_next_approval_event_with_progress(
        config,
        worker_id=worker_id,
        now=now,
    )
    return claim


def _claim_next_approval_event_with_progress(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None,
) -> tuple[ApprovalEventClaim | None, bool]:
    """领取一条事件并报告是否形成持久进展。"""
    owner = _worker_id(worker_id)
    connection = _connect(config)
    progressed = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        current_text = _db_time(current)
        exhausted = connection.execute(
            """
            SELECT outbox.*,event.event_type
            FROM approval_event_outbox outbox
            JOIN approval_events event
              ON event.tenant_id=outbox.tenant_id
             AND event.approval_event_id=outbox.approval_event_id
            WHERE outbox.status='processing' AND outbox.attempt_count=?
              AND unixepoch(outbox.lease_expires_at)<=unixepoch(?)
            ORDER BY outbox.lease_expires_at,outbox.tenant_id,outbox.approval_outbox_id
            LIMIT 1
            """,
            (MAX_ATTEMPTS, current_text),
        ).fetchone()
        if exhausted is not None:
            _fail_pending_deliveries(
                connection,
                tenant_id=_text(exhausted["tenant_id"]),
                event_id=_text(exhausted["approval_event_id"]),
                event_type=_text(exhausted["event_type"]),
                updated_at=current_text,
                failure_code="webhook.delivery_unavailable",
            )
            updated = connection.execute(
                """
                UPDATE approval_event_outbox
                SET status='failed',lease_generation=lease_generation+1,
                    lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
                    next_attempt_at=NULL,last_error_code='webhook.delivery_unavailable',
                    delivered_at=NULL,updated_at=?
                WHERE tenant_id=? AND approval_outbox_id=? AND status='processing'
                  AND attempt_count=? AND lease_generation=? AND lease_owner_id=?
                  AND lease_token=? AND lease_expires_at=?
                  AND unixepoch(lease_expires_at)<=unixepoch(?)
                """,
                (
                    current_text,
                    exhausted["tenant_id"],
                    exhausted["approval_outbox_id"],
                    MAX_ATTEMPTS,
                    exhausted["lease_generation"],
                    exhausted["lease_owner_id"],
                    exhausted["lease_token"],
                    exhausted["lease_expires_at"],
                    current_text,
                ),
            )
            if updated.rowcount != 1:
                # 事件快照、投递租约或来源链路不完整时保留可恢复事实并固定失败，不能伪造投递成功。
                raise ApiError("approval.event_unavailable")
            connection.commit()
            progressed = True
            connection.execute("BEGIN IMMEDIATE")

        while True:
            row = connection.execute(
                """
                SELECT outbox.*,event.approval_task_id,event.event_type,event.payload_json,
                       event.actor_type,event.actor_id,event.source_request_id,
                       event.source_trace_id,event.occurred_at
                FROM approval_event_outbox outbox
                JOIN approval_events event
                  ON event.tenant_id=outbox.tenant_id
                 AND event.approval_event_id=outbox.approval_event_id
                WHERE (outbox.status='pending' AND outbox.attempt_count<?
                         AND unixepoch(outbox.next_attempt_at)<=unixepoch(?))
                   OR (outbox.status='processing' AND outbox.attempt_count<?
                         AND unixepoch(outbox.lease_expires_at)<=unixepoch(?))
                ORDER BY CASE outbox.status WHEN 'processing' THEN 0 ELSE 1 END,
                         CASE outbox.status WHEN 'processing' THEN outbox.lease_expires_at
                           ELSE outbox.next_attempt_at END,
                         outbox.created_at,outbox.approval_outbox_id
                LIMIT 1
                """,
                (MAX_ATTEMPTS, current_text, MAX_ATTEMPTS, current_text),
            ).fetchone()
            if row is None:
                connection.commit()
                return None, progressed
            attempt = _integer(row["attempt_count"], minimum=0, maximum=9) + 1
            generation = _integer(row["lease_generation"], minimum=0) + 1
            token = secrets.token_hex(32)
            claim_time = max(current, _parse_time(row["updated_at"]))
            lease_expires = claim_time + timedelta(seconds=EVENT_LEASE_SECONDS)
            try:
                event = _event_from_row(row)
                snapshot_json = _freeze_event_snapshot(
                    connection,
                    event,
                    existing_snapshot=row["subscription_snapshot_json"],
                    frozen_at=_db_time(claim_time),
                )
            # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
            except (ApiError, TypeError, ValueError, json.JSONDecodeError):
                _quarantine_invalid_event(
                    connection,
                    row,
                    owner=owner,
                    token=token,
                    attempt=attempt,
                    generation=generation,
                    claim_time=claim_time,
                    lease_expires=lease_expires,
                )
                connection.commit()
                progressed = True
                connection.execute("BEGIN IMMEDIATE")
                continue
            updated = connection.execute(
                """
                UPDATE approval_event_outbox
                SET status='processing',attempt_count=?,lease_generation=?,
                    lease_owner_id=?,lease_token=?,lease_expires_at=?,next_attempt_at=NULL,
                    subscription_snapshot_json=?,snapshot_frozen_at=COALESCE(snapshot_frozen_at,?),
                    last_error_code=NULL,delivered_at=NULL,updated_at=?
                WHERE tenant_id=? AND approval_outbox_id=? AND attempt_count=?
                  AND lease_generation=? AND (
                    (status='pending' AND unixepoch(next_attempt_at)<=unixepoch(?))
                    OR (status='processing' AND unixepoch(lease_expires_at)<=unixepoch(?))
                  )
                """,
                (
                    attempt,
                    generation,
                    owner,
                    token,
                    _db_time(lease_expires),
                    snapshot_json,
                    _db_time(claim_time),
                    _db_time(claim_time),
                    row["tenant_id"],
                    row["approval_outbox_id"],
                    attempt - 1,
                    generation - 1,
                    current_text,
                    current_text,
                ),
            )
            if updated.rowcount != 1:
                connection.rollback()
                return None, progressed
            claim = ApprovalEventClaim(
                outbox_id=_outbox_id(row["approval_outbox_id"]),
                event_id=_event_id(row["approval_event_id"]),
                tenant_id=_text(row["tenant_id"]),
                approval_id=_approval_id(row["approval_task_id"]),
                source_request_id=_text(row["source_request_id"]),
                source_trace_id=_text(row["source_trace_id"]),
                event=event,
                attempt_count=attempt,
                lease_generation=generation,
                lease_owner_id=owner,
                lease_token=token,
                lease_expires_at=lease_expires,
                snapshot_json=snapshot_json,
            )
            connection.commit()
            return claim, True
    # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
    except ApiError:
        _rollback(connection)
        raise
    # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
    except (sqlite3.Error, TypeError, ValueError) as exc:
        _rollback(connection)
        # 事件快照、投递租约或来源链路不完整时保留可恢复事实并固定失败，不能伪造投递成功。
        raise ApiError("approval.event_unavailable") from exc
    finally:
        connection.close()


def dispatch_claimed_approval_event(
    config: AppConfig,
    claim: ApprovalEventClaim,
    *,
    now: datetime | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """Dispatch outside SQLite and finish only while the exact source lease is live.
    网络投递前后都必须复核事件租约所有权。
    """

    active_claim = claim

    def source_guard() -> None:
        """在每次外部投递前续租并复核当前事件 source owner，阻断迟到 worker 覆盖新租约。"""
        nonlocal active_claim
        if should_stop is not None and should_stop():
            # 事件快照、投递租约或来源链路不完整时保留可恢复事实并固定失败，不能伪造投递成功。
            raise ApiError("approval.event_unavailable")
        active_claim = _renew_event_lease(config, active_claim, now=now)

    error_code: str | None = None
    try:
        source_guard()
        from metahub_control.webhooks.service import dispatch_webhook_event

        dispatch_webhook_event(
            config,
            claim.tenant_id,
            claim.event,
            request_id=claim.source_request_id,
            trace_id=claim.source_trace_id,
            resume_existing_pending=True,
            source_lease_guard=source_guard,
            create_snapshot_if_missing=False,
        )
    # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
    except ValueError:
        error_code = "webhook.payload_invalid"
    except Exception:
        error_code = "webhook.delivery_unavailable"
    return _finish_event(config, active_claim, error_code=error_code, now=now)


def drain_approval_event_once(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """Claim and dispatch at most one event; the return value means durable progress.
    返回值只表示本轮是否形成持久进展。
    """

    if should_stop is not None and should_stop():
        return False
    claim, progressed = _claim_next_approval_event_with_progress(
        config,
        worker_id=worker_id,
        now=now,
    )
    if claim is None:
        return progressed
    dispatch_claimed_approval_event(config, claim, now=now, should_stop=should_stop)
    return True


def claim_next_approval_writeback(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> ApprovalWritebackClaim | None:
    """Claim one exact Session Risk projection or reap an exhausted orphan.
    回写租约必须在同一事务校验 tenant_id、代际和所有权。
    """

    claim, _ = _claim_next_approval_writeback_with_progress(
        config,
        worker_id=worker_id,
        now=now,
    )
    return claim


def _claim_next_approval_writeback_with_progress(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None,
) -> tuple[ApprovalWritebackClaim | None, bool]:
    """领取一条回写并报告是否形成持久进展。"""
    owner = _worker_id(worker_id)
    connection = _connect(config)
    progressed = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        current_text = _db_time(current)
        exhausted = connection.execute(
            """
            SELECT * FROM approval_writebacks
            WHERE adapter_kind='session_risk_store_sync' AND status='processing'
              AND attempt_count=? AND unixepoch(lease_expires_at)<=unixepoch(?)
            ORDER BY lease_expires_at,tenant_id,approval_writeback_id LIMIT 1
            """,
            (MAX_ATTEMPTS, current_text),
        ).fetchone()
        if exhausted is not None:
            updated = connection.execute(
                """
                UPDATE approval_writebacks
                SET status='failed',lease_generation=lease_generation+1,
                    lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
                    next_attempt_at=NULL,last_error_code='approval.writeback_unavailable',
                    completed_at=NULL,updated_at=?
                WHERE tenant_id=? AND approval_writeback_id=? AND status='processing'
                  AND attempt_count=? AND lease_generation=? AND lease_owner_id=?
                  AND lease_token=? AND lease_expires_at=?
                  AND unixepoch(lease_expires_at)<=unixepoch(?)
                """,
                (
                    current_text,
                    exhausted["tenant_id"],
                    exhausted["approval_writeback_id"],
                    MAX_ATTEMPTS,
                    exhausted["lease_generation"],
                    exhausted["lease_owner_id"],
                    exhausted["lease_token"],
                    exhausted["lease_expires_at"],
                    current_text,
                ),
            )
            if updated.rowcount != 1:
                # 回写意图、租约或目标修订不一致时保留待恢复状态，不能把审批决策误标为已生效。
                raise ApiError("approval.writeback_unavailable")
            connection.commit()
            progressed = True
            connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT * FROM approval_writebacks
            WHERE adapter_kind='session_risk_store_sync' AND (
              (status='pending' AND attempt_count<?
                AND unixepoch(next_attempt_at)<=unixepoch(?))
              OR (status='processing' AND attempt_count<?
                AND unixepoch(lease_expires_at)<=unixepoch(?))
            )
            ORDER BY CASE status WHEN 'processing' THEN 0 ELSE 1 END,
                     CASE status WHEN 'processing' THEN lease_expires_at ELSE next_attempt_at END,
                     created_at,approval_writeback_id LIMIT 1
            """,
            (MAX_ATTEMPTS, current_text, MAX_ATTEMPTS, current_text),
        ).fetchone()
        if row is None:
            connection.commit()
            return None, progressed
        attempt = _integer(row["attempt_count"], minimum=0, maximum=9) + 1
        generation = _integer(row["lease_generation"], minimum=0) + 1
        token = secrets.token_hex(32)
        claim_time = max(current, _parse_time(row["updated_at"]))
        expires = claim_time + timedelta(seconds=WRITEBACK_LEASE_SECONDS)
        updated = connection.execute(
            """
            UPDATE approval_writebacks
            SET status='processing',attempt_count=?,lease_generation=?,lease_owner_id=?,
                lease_token=?,lease_expires_at=?,next_attempt_at=NULL,last_error_code=NULL,
                completed_at=NULL,updated_at=?
            WHERE tenant_id=? AND approval_writeback_id=? AND attempt_count=?
              AND lease_generation=? AND (
                (status='pending' AND unixepoch(next_attempt_at)<=unixepoch(?))
                OR (status='processing' AND unixepoch(lease_expires_at)<=unixepoch(?))
              )
            """,
            (
                attempt,
                generation,
                owner,
                token,
                _db_time(expires),
                _db_time(claim_time),
                row["tenant_id"],
                row["approval_writeback_id"],
                attempt - 1,
                generation - 1,
                current_text,
                current_text,
            ),
        )
        if updated.rowcount != 1:
            connection.rollback()
            return None, progressed
        claim = ApprovalWritebackClaim(
            writeback_id=_writeback_id(row["approval_writeback_id"]),
            tenant_id=_text(row["tenant_id"]),
            approval_id=_approval_id(row["approval_task_id"]),
            target_fact_id=_action_id(row["target_fact_id"]),
            target_revision=_integer(row["target_revision"], minimum=1),
            attempt_count=attempt,
            lease_generation=generation,
            lease_owner_id=owner,
            lease_token=token,
            lease_expires_at=expires,
        )
        connection.commit()
        return claim, True
    # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
    except ApiError:
        _rollback(connection)
        raise
    # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
    except (sqlite3.Error, TypeError, ValueError) as exc:
        _rollback(connection)
        # 回写意图、租约或目标修订不一致时保留待恢复状态，不能把审批决策误标为已生效。
        raise ApiError("approval.writeback_unavailable") from exc
    finally:
        connection.close()


async def dispatch_claimed_approval_writeback(
    config: AppConfig,
    app_state: object,
    claim: ApprovalWritebackClaim,
    *,
    now: datetime | None = None,
) -> bool:
    """Rebuild an exact intent, perform bounded Redis I/O, then CAS the durable result.
    Redis I/O 发生在事务外，完成状态仍由精确租约 CAS 提交。
    """

    error = False
    try:
        intent = _load_writeback_intent(config, claim, now=now)
        store = get_session_risk_store(config, app_state)
        await asyncio.wait_for(
            synchronize_session_risk_store(store, intent),
            timeout=REDIS_SYNC_TIMEOUT_SECONDS,
        )
    except Exception:
        error = True
    return _finish_writeback(config, claim, failed=error, now=now)


async def drain_approval_writeback_once(
    config: AppConfig,
    app_state: object,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> bool:
    """Claim and reconcile at most one durable Session Risk writeback.
    返回值只表示本轮是否形成持久进展。
    """

    # Claiming is synchronous SQLite work. Keep it off the FastAPI event loop so
    # an idle writeback queue cannot delay login or dashboard reads.
    claim, progressed = await asyncio.to_thread(
        _claim_next_approval_writeback_with_progress,
        config,
        worker_id=worker_id,
        now=now,
    )
    if claim is None:
        return progressed
    await dispatch_claimed_approval_writeback(config, app_state, claim, now=now)
    return True


def recover_approval_source_request_id(
    connection: sqlite3.Connection,
    envelope: dict[str, object],
) -> str:
    """Recover a hidden source request only from the immutable approval owner graph.
    source request 只能从不可变的同租户 owner 图恢复。
    """

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
    if type(envelope) is not dict or set(envelope) != expected:
        # 事件快照、投递租约或来源链路不完整时保留可恢复事实并固定失败，不能伪造投递成功。
        raise ApiError("webhook.delivery_unavailable")
    event_id = envelope.get("event_id")
    tenant_id = envelope.get("tenant_id")
    if type(event_id) is not str or _EVENT_ID.fullmatch(event_id) is None:
        # 事件快照、投递租约或来源链路不完整时保留可恢复事实并固定失败，不能伪造投递成功。
        raise ApiError("webhook.delivery_unavailable")
    # tenant_id 必须来自可信所有者关系并参与每次读写条件；缺失或不一致时立即失败关闭。
    if type(tenant_id) is not str:
        # 事件快照、投递租约或来源链路不完整时保留可恢复事实并固定失败，不能伪造投递成功。
        raise ApiError("webhook.delivery_unavailable")
    rows = connection.execute(
        """
        SELECT event.*,task.approval_task_id
        FROM approval_events event
        JOIN approval_tasks task
          ON task.tenant_id=event.tenant_id
         AND task.approval_task_id=event.approval_task_id
        WHERE event.tenant_id=? AND event.approval_event_id=?
        LIMIT 2
        """,
        (tenant_id, event_id),
    ).fetchall()
    if len(rows) != 1:
        # 事件快照、投递租约或来源链路不完整时保留可恢复事实并固定失败，不能伪造投递成功。
        raise ApiError("webhook.delivery_unavailable")
    event = _event_from_row(rows[0])
    if event.to_payload() != envelope:
        # 事件快照、投递租约或来源链路不完整时保留可恢复事实并固定失败，不能伪造投递成功。
        raise ApiError("webhook.delivery_unavailable")
    return _text(rows[0]["source_request_id"])


def _freeze_event_snapshot(
    connection: sqlite3.Connection,
    event: WebhookEvent,
    *,
    existing_snapshot: object,
    frozen_at: str,
) -> str:
    """冻结并复核事件的 Webhook 投递快照。"""
    if existing_snapshot is None:
        from metahub_control.webhooks.service import freeze_webhook_event_delivery_snapshot

        freeze_webhook_event_delivery_snapshot(connection, event, frozen_at=frozen_at)
    rows = connection.execute(
        """
        SELECT webhook_delivery_id FROM webhook_deliveries
        WHERE tenant_id=? AND event_id=? AND delivery_kind='original'
        ORDER BY created_at,webhook_delivery_id
        """,
        (event.tenant_id, event.event_id),
    ).fetchall()
    snapshot = json.dumps(
        [_text(row["webhook_delivery_id"]) for row in rows],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    if existing_snapshot is not None and _canonical_snapshot(existing_snapshot) != snapshot:
        # 事件快照、投递租约或来源链路不完整时保留可恢复事实并固定失败，不能伪造投递成功。
        raise ApiError("approval.event_unavailable")
    return snapshot


def _quarantine_invalid_event(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    owner: str,
    token: str,
    attempt: int,
    generation: int,
    claim_time: datetime,
    lease_expires: datetime,
) -> None:
    """以当前租约隔离损坏的待投递事件。"""
    claim_text = _db_time(claim_time)
    claimed = connection.execute(
        """
        UPDATE approval_event_outbox
        SET status='processing',attempt_count=?,lease_generation=?,lease_owner_id=?,
            lease_token=?,lease_expires_at=?,next_attempt_at=NULL,last_error_code=NULL,
            delivered_at=NULL,updated_at=?
        WHERE tenant_id=? AND approval_outbox_id=? AND attempt_count=?
          AND lease_generation=? AND status IN('pending','processing')
        """,
        (
            attempt,
            generation,
            owner,
            token,
            _db_time(lease_expires),
            claim_text,
            row["tenant_id"],
            row["approval_outbox_id"],
            attempt - 1,
            generation - 1,
        ),
    )
    if claimed.rowcount != 1:
        # 事件快照、投递租约或来源链路不完整时保留可恢复事实并固定失败，不能伪造投递成功。
        raise ApiError("approval.event_unavailable")
    _fail_pending_deliveries(
        connection,
        tenant_id=_text(row["tenant_id"]),
        event_id=_text(row["approval_event_id"]),
        event_type=_text(row["event_type"]),
        updated_at=claim_text,
        failure_code="webhook.payload_invalid",
    )
    failed = connection.execute(
        """
        UPDATE approval_event_outbox
        SET status='failed',lease_generation=lease_generation+1,lease_owner_id=NULL,
            lease_token=NULL,lease_expires_at=NULL,next_attempt_at=NULL,
            last_error_code='webhook.payload_invalid',delivered_at=NULL,updated_at=?
        WHERE tenant_id=? AND approval_outbox_id=? AND status='processing'
          AND attempt_count=? AND lease_generation=? AND lease_owner_id=? AND lease_token=?
        """,
        (
            claim_text,
            row["tenant_id"],
            row["approval_outbox_id"],
            attempt,
            generation,
            owner,
            token,
        ),
    )
    if failed.rowcount != 1:
        # 事件快照、投递租约或来源链路不完整时保留可恢复事实并固定失败，不能伪造投递成功。
        raise ApiError("approval.event_unavailable")


def _renew_event_lease(
    config: AppConfig,
    claim: ApprovalEventClaim,
    *,
    now: datetime | None,
) -> ApprovalEventClaim:
    """每次真实外呼前以完整 owner 和旧 lease 精确续租。"""

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        row = connection.execute(
            """
            SELECT updated_at,lease_expires_at,subscription_snapshot_json
            FROM approval_event_outbox
            WHERE tenant_id=? AND approval_outbox_id=? AND approval_event_id=?
              AND status='processing' AND attempt_count=? AND lease_generation=?
              AND lease_owner_id=? AND lease_token=? AND lease_expires_at=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                claim.tenant_id,
                claim.outbox_id,
                claim.event_id,
                claim.attempt_count,
                claim.lease_generation,
                claim.lease_owner_id,
                claim.lease_token,
                _db_time(_required_time(claim.lease_expires_at)),
                _db_time(current),
            ),
        ).fetchone()
        if row is None or row["subscription_snapshot_json"] != claim.snapshot_json:
            # 事件快照、投递租约或来源链路不完整时保留可恢复事实并固定失败，不能伪造投递成功。
            raise ApiError("approval.event_unavailable")
        previous_updated = _parse_time(row["updated_at"])
        previous_expires = _parse_time(row["lease_expires_at"])
        renewed_at = max(current, previous_updated + timedelta(seconds=1))
        if previous_expires <= renewed_at:
            # 事件快照、投递租约或来源链路不完整时保留可恢复事实并固定失败，不能伪造投递成功。
            raise ApiError("approval.event_unavailable")
        renewed_expires = renewed_at + timedelta(seconds=EVENT_LEASE_SECONDS)
        generation = claim.lease_generation + 1
        updated = connection.execute(
            """
            UPDATE approval_event_outbox
            SET lease_generation=?,lease_expires_at=?,updated_at=?
            WHERE tenant_id=? AND approval_outbox_id=? AND approval_event_id=?
              AND status='processing' AND attempt_count=? AND lease_generation=?
              AND lease_owner_id=? AND lease_token=? AND lease_expires_at=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
              AND subscription_snapshot_json=?
            """,
            (
                generation,
                _db_time(renewed_expires),
                _db_time(renewed_at),
                claim.tenant_id,
                claim.outbox_id,
                claim.event_id,
                claim.attempt_count,
                claim.lease_generation,
                claim.lease_owner_id,
                claim.lease_token,
                _db_time(previous_expires),
                _db_time(renewed_at),
                claim.snapshot_json,
            ),
        )
        if updated.rowcount != 1:
            # 事件快照、投递租约或来源链路不完整时保留可恢复事实并固定失败，不能伪造投递成功。
            raise ApiError("approval.event_unavailable")
        connection.commit()
        return replace(
            claim,
            lease_generation=generation,
            lease_expires_at=renewed_expires,
        )
    # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
    except ApiError:
        _rollback(connection)
        raise
    # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
    except (sqlite3.Error, TypeError, ValueError) as exc:
        _rollback(connection)
        # 事件快照、投递租约或来源链路不完整时保留可恢复事实并固定失败，不能伪造投递成功。
        raise ApiError("approval.event_unavailable") from exc
    finally:
        connection.close()


def _finish_event(
    config: AppConfig,
    claim: ApprovalEventClaim,
    *,
    error_code: str | None,
    now: datetime | None,
) -> bool:
    """``_finish_event`` 维护审批事件或回写队列租约，tenant_id、代际和所有权必须在同一事务校验。"""
    terminal = error_code == "webhook.payload_invalid" or (
        error_code is not None and claim.attempt_count >= MAX_ATTEMPTS
    )
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = max(
            _transaction_now(connection, now),
            _required_time(claim.lease_expires_at) - timedelta(seconds=EVENT_LEASE_SECONDS),
        )
        current_text = _db_time(current)
        if terminal:
            _fail_pending_deliveries(
                connection,
                tenant_id=claim.tenant_id,
                event_id=claim.event_id,
                event_type=claim.event.event_type,
                updated_at=current_text,
                failure_code=error_code or "webhook.delivery_unavailable",
            )
        # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
        if error_code is None:
            status = "delivered"
            next_attempt = None
            delivered_at = current_text
        elif terminal:
            status = "failed"
            next_attempt = None
            delivered_at = None
        else:
            status = "pending"
            delay = BACKOFF_SECONDS[min(claim.attempt_count - 1, len(BACKOFF_SECONDS) - 1)]
            next_attempt = _db_time(current + timedelta(seconds=delay))
            delivered_at = None
        updated = connection.execute(
            """
            UPDATE approval_event_outbox
            SET status=?,lease_generation=lease_generation+1,lease_owner_id=NULL,
                lease_token=NULL,lease_expires_at=NULL,next_attempt_at=?,last_error_code=?,
                delivered_at=?,updated_at=?
            WHERE tenant_id=? AND approval_outbox_id=? AND approval_event_id=?
              AND status='processing' AND attempt_count=? AND lease_generation=?
              AND lease_owner_id=? AND lease_token=? AND lease_expires_at=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
              AND subscription_snapshot_json=?
            """,
            (
                status,
                next_attempt,
                error_code,
                delivered_at,
                current_text,
                claim.tenant_id,
                claim.outbox_id,
                claim.event_id,
                claim.attempt_count,
                claim.lease_generation,
                claim.lease_owner_id,
                claim.lease_token,
                _db_time(_required_time(claim.lease_expires_at)),
                current_text,
                claim.snapshot_json,
            ),
        )
        if updated.rowcount != 1:
            connection.rollback()
            return False
        connection.commit()
        return True
    # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
    except (sqlite3.Error, TypeError, ValueError):
        _rollback(connection)
        return False
    finally:
        connection.close()


def _load_writeback_intent(
    config: AppConfig,
    claim: ApprovalWritebackClaim,
    *,
    now: datetime | None,
) -> SessionRiskStoreSyncIntent:
    """从同租户持久事实重建精确回写意图。"""
    connection = _connect(config)
    try:
        current = _transaction_now(connection, now)
        row = connection.execute(
            """
            SELECT score.*
            FROM approval_writebacks writeback
            JOIN approval_tasks task
              ON task.tenant_id=writeback.tenant_id
             AND task.approval_task_id=writeback.approval_task_id
            JOIN approval_decisions decision
              ON decision.tenant_id=task.tenant_id
             AND decision.approval_task_id=task.approval_task_id
            JOIN session_risk_actions action
              ON action.tenant_id=writeback.tenant_id
             AND action.session_risk_action_id=writeback.target_fact_id
             AND action.session_risk_score_id=task.target_id
             AND action.state_revision=writeback.target_revision
            JOIN session_risk_scores score
              ON score.tenant_id=action.tenant_id
             AND score.session_risk_score_id=action.session_risk_score_id
            WHERE writeback.tenant_id=? AND writeback.approval_writeback_id=?
              AND writeback.approval_task_id=?
              AND writeback.adapter_kind='session_risk_store_sync'
              AND writeback.target_fact_id=? AND writeback.target_revision=?
              AND writeback.status='processing' AND writeback.attempt_count=?
              AND writeback.lease_generation=? AND writeback.lease_owner_id=?
              AND writeback.lease_token=? AND writeback.lease_expires_at=?
              AND unixepoch(writeback.lease_expires_at)>unixepoch(?)
              AND task.status='approved' AND task.target_type='session_risk_action'
              AND task.writeback_status='pending' AND decision.decision='approve'
              AND score.state_revision=writeback.target_revision
            """,
            (
                claim.tenant_id,
                claim.writeback_id,
                claim.approval_id,
                claim.target_fact_id,
                claim.target_revision,
                claim.attempt_count,
                claim.lease_generation,
                claim.lease_owner_id,
                claim.lease_token,
                _db_time(_required_time(claim.lease_expires_at)),
                _db_time(current),
            ),
        ).fetchone()
        if row is None:
            # 回写意图、租约或目标修订不一致时保留待恢复状态，不能把审批决策误标为已生效。
            raise ApiError("approval.writeback_unavailable")
        intent = _build_store_sync_intent(config, row)
        # 授权只能使用后端刚刷新且与当前 tenant_id 绑定的权限快照，客户端声明不能扩大可执行范围。
        if intent.authoritative_revision != claim.target_revision:
            # 回写意图、租约或目标修订不一致时保留待恢复状态，不能把审批决策误标为已生效。
            raise ApiError("approval.writeback_unavailable")
        return intent
    # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
    except ApiError:
        raise
    # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
    except (sqlite3.Error, TypeError, ValueError) as exc:
        # 回写意图、租约或目标修订不一致时保留待恢复状态，不能把审批决策误标为已生效。
        raise ApiError("approval.writeback_unavailable") from exc
    finally:
        connection.close()


def _finish_writeback(
    config: AppConfig,
    claim: ApprovalWritebackClaim,
    *,
    failed: bool,
    now: datetime | None,
) -> bool:
    """仅由当前有效租约提交回写结果。"""
    terminal = failed and claim.attempt_count >= MAX_ATTEMPTS
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        current_text = _db_time(current)
        if not failed:
            status = "completed"
            next_attempt = None
            error_code = None
            completed_at = current_text
        elif terminal:
            status = "failed"
            next_attempt = None
            error_code = "approval.writeback_unavailable"
            completed_at = None
        else:
            status = "pending"
            delay = BACKOFF_SECONDS[min(claim.attempt_count - 1, len(BACKOFF_SECONDS) - 1)]
            next_attempt = _db_time(current + timedelta(seconds=delay))
            error_code = "approval.writeback_unavailable"
            completed_at = None
        updated = connection.execute(
            """
            UPDATE approval_writebacks
            SET status=?,lease_generation=lease_generation+1,lease_owner_id=NULL,
                lease_token=NULL,lease_expires_at=NULL,next_attempt_at=?,last_error_code=?,
                completed_at=?,updated_at=?
            WHERE tenant_id=? AND approval_writeback_id=? AND approval_task_id=?
              AND target_fact_id=? AND target_revision=? AND status='processing'
              AND attempt_count=? AND lease_generation=? AND lease_owner_id=?
              AND lease_token=? AND lease_expires_at=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                status,
                next_attempt,
                error_code,
                completed_at,
                current_text,
                claim.tenant_id,
                claim.writeback_id,
                claim.approval_id,
                claim.target_fact_id,
                claim.target_revision,
                claim.attempt_count,
                claim.lease_generation,
                claim.lease_owner_id,
                claim.lease_token,
                _db_time(_required_time(claim.lease_expires_at)),
                current_text,
            ),
        )
        if updated.rowcount != 1:
            connection.rollback()
            return False
        connection.commit()
        return True
    # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
    except (sqlite3.Error, TypeError, ValueError):
        _rollback(connection)
        return False
    finally:
        connection.close()


def _event_from_row(row: sqlite3.Row) -> WebhookEvent:
    """把持久事件恢复为严格的公开 Webhook 事件。"""
    event_id = _event_id(row["approval_event_id"])
    approval_id = _approval_id(row["approval_task_id"])
    event_type = _text(row["event_type"])
    try:
        payload = json.loads(_text(row["payload_json"]))
    # JSON 与响应合同必须完整且拒绝未知形状；解析失败不得回显上游正文或内部字段。
    except json.JSONDecodeError:
        # 事件快照、投递租约或来源链路不完整时保留可恢复事实并固定失败，不能伪造投递成功。
        raise ApiError("approval.event_unavailable") from None
    if type(payload) is not dict:
        # 事件快照、投递租约或来源链路不完整时保留可恢复事实并固定失败，不能伪造投递成功。
        raise ApiError("approval.event_unavailable")
    timestamp = _parse_time(row["occurred_at"])
    return build_webhook_event(
        tenant_id=_text(row["tenant_id"]),
        event_type=event_type,
        payload=payload,
        actor_id=_text(row["actor_id"]),
        object_id=approval_id,
        trace_id=_text(row["source_trace_id"]),
        source_request_id=_text(row["source_request_id"]),
        event_id=event_id,
        timestamp=timestamp,
    )


def _canonical_snapshot(value: object) -> str:
    """规范化只包含 delivery ID 的冻结快照，拒绝类型漂移和可变投递集合。"""
    if type(value) is not str:
        # 事件快照、投递租约或来源链路不完整时保留可恢复事实并固定失败，不能伪造投递成功。
        raise ApiError("approval.event_unavailable")
    try:
        parsed = json.loads(value)
    # JSON 与响应合同必须完整且拒绝未知形状；解析失败不得回显上游正文或内部字段。
    except json.JSONDecodeError:
        # 事件快照、投递租约或来源链路不完整时保留可恢复事实并固定失败，不能伪造投递成功。
        raise ApiError("approval.event_unavailable") from None
    if type(parsed) is not list or any(type(item) is not str for item in parsed):
        # 事件快照、投递租约或来源链路不完整时保留可恢复事实并固定失败，不能伪造投递成功。
        raise ApiError("approval.event_unavailable")
    return json.dumps(parsed, ensure_ascii=True, separators=(",", ":"))


def _fail_pending_deliveries(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    event_id: str,
    event_type: str,
    updated_at: str,
    failure_code: str,
) -> None:
    """将当前事件尚未完成的投递统一标记为失败。"""
    from metahub_control.webhooks.service import fail_pending_webhook_event_deliveries

    fail_pending_webhook_event_deliveries(
        connection,
        tenant_id=tenant_id,
        event_id=event_id,
        event_type=event_type,
        updated_at=updated_at,
        failure_code=failure_code,
    )


def _transaction_now(connection: sqlite3.Connection, override: datetime | None) -> datetime:
    """读取事务数据库时钟；测试覆盖值仍需严格 UTC。"""
    if override is not None:
        return _required_time(override).astimezone(UTC).replace(microsecond=0)
    row = connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')").fetchone()
    if row is None:
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable")
    return _parse_time(row[0])


def _connect(config: AppConfig) -> sqlite3.Connection:
    """``_connect`` 维护审批事件或回写队列租约，tenant_id、代际和所有权必须在同一事务校验。"""
    try:
        return connect_database(config.database_url)
    # 数据库异常必须回滚当前事务并转换为固定不可用错误，SQL 和敏感参数不得进入响应。
    except DbError as exc:
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable") from exc


def _rollback(connection: sqlite3.Connection) -> None:
    """``_rollback`` 维护审批事件或回写队列租约，tenant_id、代际和所有权必须在同一事务校验。"""
    try:
        if connection.in_transaction:
            connection.rollback()
    # 数据库异常必须回滚当前事务并转换为固定不可用错误，SQL 和敏感参数不得进入响应。
    except sqlite3.Error:
        return


def _worker_id(value: object) -> str:
    """``_worker_id`` 维护审批事件或回写队列租约，tenant_id、代际和所有权必须在同一事务校验。"""
    if type(value) is not str or _WORKER_ID.fullmatch(value) is None:
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable")
    return value


def _event_id(value: object) -> str:
    """``_event_id`` 维护审批事件或回写队列租约，tenant_id、代际和所有权必须在同一事务校验。"""
    if type(value) is not str or _EVENT_ID.fullmatch(value) is None:
        # 事件快照、投递租约或来源链路不完整时保留可恢复事实并固定失败，不能伪造投递成功。
        raise ApiError("approval.event_unavailable")
    return value


def _outbox_id(value: object) -> str:
    """``_outbox_id`` 维护审批事件或回写队列租约，tenant_id、代际和所有权必须在同一事务校验。"""
    if type(value) is not str or _OUTBOX_ID.fullmatch(value) is None:
        # 事件快照、投递租约或来源链路不完整时保留可恢复事实并固定失败，不能伪造投递成功。
        raise ApiError("approval.event_unavailable")
    return value


def _approval_id(value: object) -> str:
    """``_approval_id`` 维护审批事件或回写队列租约，tenant_id、代际和所有权必须在同一事务校验。"""
    if type(value) is not str or _APPROVAL_ID.fullmatch(value) is None:
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable")
    return value


def _writeback_id(value: object) -> str:
    """``_writeback_id`` 维护审批事件或回写队列租约，tenant_id、代际和所有权必须在同一事务校验。"""
    if type(value) is not str or _WRITEBACK_ID.fullmatch(value) is None:
        # 回写意图、租约或目标修订不一致时保留待恢复状态，不能把审批决策误标为已生效。
        raise ApiError("approval.writeback_unavailable")
    return value


def _action_id(value: object) -> str:
    """``_action_id`` 维护审批事件或回写队列租约，tenant_id、代际和所有权必须在同一事务校验。"""
    if type(value) is not str or _ACTION_ID.fullmatch(value) is None:
        # 回写意图、租约或目标修订不一致时保留待恢复状态，不能把审批决策误标为已生效。
        raise ApiError("approval.writeback_unavailable")
    return value


def _text(value: object) -> str:
    """``_text`` 维护审批事件或回写队列租约，tenant_id、代际和所有权必须在同一事务校验。"""
    if type(value) is not str or not value:
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable")
    return value


def _integer(value: object, *, minimum: int, maximum: int | None = None) -> int:
    """``_integer`` 维护审批事件或回写队列租约，tenant_id、代际和所有权必须在同一事务校验。"""
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable")
    return value


def _required_time(value: datetime | None) -> datetime:
    """``_required_time`` 维护审批事件或回写队列租约，tenant_id、代际和所有权必须在同一事务校验。"""
    if type(value) is not datetime or value.tzinfo is None:
        # 内部依赖、摘要或持久化事实异常统一失败关闭；只暴露固定错误码而不泄露敏感上下文。
        raise ApiError("approval.unavailable")
    return value.astimezone(UTC)


def _parse_time(value: object) -> datetime:
    """``_parse_time`` 维护审批事件或回写队列租约，tenant_id、代际和所有权必须在同一事务校验。"""
    if type(value) is not str:
        # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
        raise ValueError("time invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
        raise ValueError("time invalid")
    return parsed.astimezone(UTC)


def _db_time(value: datetime) -> str:
    """``_db_time`` 维护审批事件或回写队列租约，tenant_id、代际和所有权必须在同一事务校验。"""
    return _required_time(value).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "ApprovalEventClaim",
    "ApprovalWritebackClaim",
    "claim_next_approval_event",
    "claim_next_approval_writeback",
    "dispatch_claimed_approval_event",
    "dispatch_claimed_approval_writeback",
    "drain_approval_event_once",
    "drain_approval_writeback_once",
    "recover_approval_source_request_id",
]
