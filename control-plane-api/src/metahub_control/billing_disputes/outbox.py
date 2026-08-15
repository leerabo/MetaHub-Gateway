"""PRD-080 ``billing_dispute.created`` 事件的持久恢复实现。

事件创建事务已经冻结原始 Webhook delivery 集合；worker 只重建并验证不可变 owner 图，随后以
tenant、owner、generation、token、attempt 和未过期租约完成 CAS。网络调用发生在短事务之外，
因此提供稳定事件 ID 的 at-least-once 语义，不宣称网络 exactly-once。
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import WebhookEvent, build_webhook_event

EVENT_LEASE_SECONDS = 90
MAX_ATTEMPTS = 10
BACKOFF_SECONDS = (5, 30, 300)
_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_EVENT_ID = re.compile(r"^bdevt_[0-9a-f]{32}$")
_OUTBOX_ID = re.compile(r"^bdob_[0-9a-f]{32}$")
_DISPUTE_ID = re.compile(r"^bdsp_[0-9a-f]{32}$")
_DELIVERY_ID = re.compile(r"^whd_[0-9a-f]{32}$")

# 租约、退避和私有 token 共同限制单次恢复；每次领取、续租和收敛都会推进 generation。


class _StopBeforeDispatch(Exception):
    """协作停服发生在本轮任何网络尝试之前。"""


@dataclass(slots=True)
class BillingDisputeEventClaim:
    """不可变 source 坐标与一个可续租的私有租约；事件、token 和快照禁止进入 repr。"""

    outbox_id: str
    event_id: str
    tenant_id: str
    source_request_id: str
    source_trace_id: str
    event: WebhookEvent = field(repr=False)
    attempt_count: int
    lease_generation: int
    lease_owner_id: str
    lease_token: str = field(repr=False)
    lease_expires_at: datetime
    snapshot_json: str = field(repr=False)


def claim_next_billing_dispute_event(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> BillingDisputeEventClaim | None:
    """按租户公平顺序领取一个到期事件；claim 不消耗投递 attempt。"""

    claim, _ = _claim_with_progress(config, worker_id=worker_id, now=now)
    return claim


def _claim_with_progress(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None,
) -> tuple[BillingDisputeEventClaim | None, bool]:
    """领取或隔离一条 source，并区分空队列与本轮已持久化处理进展。"""

    owner = _worker(worker_id)
    connection = _connect(config)
    progressed = False
    try:
        # BEGIN IMMEDIATE 把选取和租约 CAS 串为一个短写事务，tenant_id 参与每次 owner 更新。
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        current_text = _db_time(current)
        while True:
            row = connection.execute(
                """
                WITH due AS (
                  SELECT outbox.*,
                    COALESCE(outbox.next_attempt_at,outbox.lease_expires_at) AS due_at,
                    ROW_NUMBER() OVER (
                      PARTITION BY outbox.tenant_id
                      ORDER BY COALESCE(outbox.next_attempt_at,outbox.lease_expires_at),
                               outbox.created_at,outbox.billing_dispute_event_outbox_id
                    ) AS tenant_rank
                  FROM billing_dispute_event_outbox outbox
                  WHERE (
                    outbox.status='pending'
                    AND unixepoch(outbox.next_attempt_at)<=unixepoch(?)
                  ) OR (
                    outbox.status='processing'
                    AND unixepoch(outbox.lease_expires_at)<=unixepoch(?)
                  )
                ), tenant_due AS (
                  SELECT tenant_id,MIN(due_at) AS first_due_at
                  FROM due WHERE tenant_rank=1 GROUP BY tenant_id
                ), chosen_tenant AS (
                  SELECT tenant_due.tenant_id
                  FROM tenant_due
                  LEFT JOIN billing_dispute_scheduler_tenants scheduler
                    ON scheduler.tenant_id=tenant_due.tenant_id
                  ORDER BY COALESCE(scheduler.last_claim_sequence,0),
                           tenant_due.first_due_at,tenant_due.tenant_id
                  LIMIT 1
                )
                SELECT event.*,due.billing_dispute_event_outbox_id,due.status,
                  due.attempt_count,due.lease_generation,due.lease_owner_id,due.lease_token,
                  due.lease_expires_at,due.next_attempt_at,due.delivery_snapshot_json,
                  due.snapshot_frozen_at
                FROM due
                JOIN chosen_tenant ON chosen_tenant.tenant_id=due.tenant_id
                JOIN billing_dispute_events event
                  ON event.tenant_id=due.tenant_id
                 AND event.billing_dispute_event_id=due.billing_dispute_event_id
                WHERE due.tenant_rank=1
                ORDER BY due.due_at,due.created_at,due.billing_dispute_event_outbox_id
                LIMIT 1
                """,
                (current_text, current_text),
            ).fetchone()
            if row is None:
                connection.commit()
                return None, progressed
            token = secrets.token_hex(32)
            # takeover 与首次 claim 都只推进 generation；真实网络结果由 finish 消耗 attempt。
            attempt = _non_negative_int(row["attempt_count"])
            generation = _non_negative_int(row["lease_generation"]) + 1
            expires = current + timedelta(seconds=EVENT_LEASE_SECONDS)
            try:
                event = _event_from_row(connection, row)
                # 投递快照已在事件创建事务冻结，此处只验证，绝不能吸收后来新增的订阅。
                snapshot = _verify_snapshot(
                    connection,
                    event,
                    existing=row["delivery_snapshot_json"],
                    frozen_at=row["snapshot_frozen_at"],
                )
            except (ApiError, TypeError, ValueError, json.JSONDecodeError):
                _quarantine(
                    connection,
                    row,
                    owner=owner,
                    token=token,
                    attempt=attempt,
                    generation=generation,
                    expires=expires,
                    now=current_text,
                )
                _advance_fairness(connection, _text(row["tenant_id"]), current_text)
                connection.commit()
                progressed = True
                connection.execute("BEGIN IMMEDIATE")
                continue
            cursor = connection.execute(
                """
                UPDATE billing_dispute_event_outbox
                SET status='processing',attempt_count=?,lease_generation=?,
                    lease_owner_id=?,lease_token=?,
                    lease_expires_at=?,next_attempt_at=NULL,
                    last_error_code=NULL,updated_at=?
                WHERE tenant_id=? AND billing_dispute_event_outbox_id=? AND status=?
                  AND attempt_count=? AND lease_generation=?
                  AND (status='pending' OR unixepoch(lease_expires_at)<=unixepoch(?))
                """,
                (
                    attempt,
                    generation,
                    owner,
                    token,
                    _db_time(expires),
                    current_text,
                    row["tenant_id"],
                    row["billing_dispute_event_outbox_id"],
                    row["status"],
                    row["attempt_count"],
                    row["lease_generation"],
                    current_text,
                ),
            )
            if cursor.rowcount != 1:
                # CAS 未命中表示状态已由其他 owner 推进，当前 worker 不再取得网络调用权。
                connection.rollback()
                return None, progressed
            _advance_fairness(connection, _text(row["tenant_id"]), current_text)
            claim = BillingDisputeEventClaim(
                outbox_id=_matched(row["billing_dispute_event_outbox_id"], _OUTBOX_ID),
                event_id=_matched(row["billing_dispute_event_id"], _EVENT_ID),
                tenant_id=_text(row["tenant_id"]),
                source_request_id=_text(row["source_request_id"]),
                source_trace_id=_text(row["source_trace_id"]),
                event=event,
                attempt_count=attempt,
                lease_generation=generation,
                lease_owner_id=owner,
                lease_token=token,
                lease_expires_at=expires,
                snapshot_json=snapshot,
            )
            connection.commit()
            return claim, True
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as exc:
        _rollback(connection)
        raise ApiError("billing_dispute.unavailable") from exc
    finally:
        connection.close()


def dispatch_claimed_billing_dispute_event(
    config: AppConfig,
    claim: BillingDisputeEventClaim,
    *,
    now: datetime | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """在事务外投递冻结快照，并以可续租 CAS 收敛成功、重试或失败终态。"""

    network_attempts_started = 0

    def source_guard() -> None:
        """每次外部调用前续租；停服或失权时阻断迟到 worker。"""

        nonlocal network_attempts_started
        if should_stop is not None and should_stop():
            if network_attempts_started == 0:
                raise _StopBeforeDispatch
            raise ApiError("billing_dispute.unavailable")
        _renew(config, claim, now=now)
        network_attempts_started += 1

    error_code: str | None = None
    delivery_terminal = False
    try:
        # 网络调用在领取事务提交后执行，长连接不会持有 SQLite 写锁。
        if should_stop is not None and should_stop():
            raise _StopBeforeDispatch
        from metahub_control.webhooks.service import dispatch_webhook_event

        deliveries = dispatch_webhook_event(
            config,
            claim.tenant_id,
            claim.event,
            request_id=claim.source_request_id,
            trace_id=claim.source_trace_id,
            resume_existing_pending=True,
            source_lease_guard=source_guard,
            create_snapshot_if_missing=False,
        )
        if any(delivery.status == "failed" for delivery in deliveries):
            delivery_terminal = True
        if any(delivery.status != "success" for delivery in deliveries):
            error_code = "webhook.delivery_unavailable"
    except _StopBeforeDispatch:
        return _release_claim(config, claim, now=now)
    except ValueError:
        error_code = "webhook.payload_invalid"
    except Exception:
        error_code = "webhook.delivery_unavailable"
    return _finish(
        config,
        claim,
        error_code=error_code,
        delivery_terminal=delivery_terminal,
        now=now,
    )


def _release_claim(
    config: AppConfig,
    claim: BillingDisputeEventClaim,
    *,
    now: datetime | None,
) -> bool:
    """零外呼协作停服时立即归还租约，不消耗投递 attempt。"""

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current_text = _db_time(_transaction_now(connection, now))
        cursor = connection.execute(
            """
            UPDATE billing_dispute_event_outbox
            SET status='pending',lease_generation=lease_generation+1,
                lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
                next_attempt_at=?,last_error_code=NULL,succeeded_at=NULL,updated_at=?
            WHERE tenant_id=? AND billing_dispute_event_outbox_id=? AND status='processing'
              AND attempt_count=? AND lease_generation=? AND lease_owner_id=?
              AND lease_token=? AND delivery_snapshot_json=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                current_text,
                current_text,
                claim.tenant_id,
                claim.outbox_id,
                claim.attempt_count,
                claim.lease_generation,
                claim.lease_owner_id,
                claim.lease_token,
                claim.snapshot_json,
                current_text,
            ),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            return False
        connection.commit()
        return True
    except (ApiError, sqlite3.Error, TypeError, ValueError):
        _rollback(connection)
        return False
    finally:
        connection.close()


def drain_billing_dispute_event_once(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """领取并恢复至多一个事件；停止信号和真正空队列均返回无工作。"""

    if should_stop is not None and should_stop():
        return False
    claim, progressed = _claim_with_progress(config, worker_id=worker_id, now=now)
    if claim is None:
        return progressed
    dispatch_claimed_billing_dispute_event(
        config,
        claim,
        now=now,
        should_stop=should_stop,
    )
    return True


def recover_billing_dispute_source_request_id(
    connection: sqlite3.Connection,
    envelope: dict[str, object],
) -> str:
    """仅从不可变 owner 图恢复私有 request_id，并逐字段比对公开信封。"""

    if type(envelope) is not dict:
        raise ApiError("webhook.delivery_unavailable")
    row = connection.execute(
        """SELECT * FROM billing_dispute_events
           WHERE tenant_id=? AND billing_dispute_event_id=?""",
        (envelope.get("tenant_id"), envelope.get("event_id")),
    ).fetchone()
    if row is None:
        raise ApiError("webhook.delivery_unavailable")
    event = _event_from_row(connection, row)
    if event.to_payload() != envelope:
        raise ApiError("webhook.delivery_unavailable")
    return _text(row["source_request_id"])


def _event_from_row(connection: sqlite3.Connection, row: sqlite3.Row) -> WebhookEvent:
    """重建安全事件，并从冻结 item 重算计数和金额汇总以识别持久污染。"""

    event_id = _matched(row["billing_dispute_event_id"], _EVENT_ID)
    tenant_id = _text(row["tenant_id"])
    dispute_id = _matched(row["billing_dispute_id"], _DISPUTE_ID)
    if _text(row["event_type"]) != "billing_dispute.created":
        raise ApiError("billing_dispute.integrity_failed")
    try:
        payload = json.loads(_text(row["payload_json"]), object_pairs_hook=_unique_object)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ApiError("billing_dispute.integrity_failed") from exc
    dispute = connection.execute(
        """SELECT * FROM billing_disputes
           WHERE tenant_id=? AND billing_dispute_id=?""",
        (tenant_id, dispute_id),
    ).fetchone()
    items = connection.execute(
        """SELECT currency,disputable_micros FROM billing_dispute_items
           WHERE tenant_id=? AND billing_dispute_id=? ORDER BY ledger_sequence""",
        (tenant_id, dispute_id),
    ).fetchall()
    if dispute is None or len(items) != _positive_int(dispute["item_count"]):
        raise ApiError("billing_dispute.integrity_failed")
    totals: dict[str, int] = {}
    for item in items:
        currency = _text(item["currency"])
        if currency not in {"CNY", "USD"}:
            raise ApiError("billing_dispute.integrity_failed")
        totals[currency] = totals.get(currency, 0) + _positive_int(item["disputable_micros"])
    expected_payload = {
        "schema_version": 1,
        "billing_dispute_id": dispute_id,
        "scope_type": _text(dispute["scope_type"]),
        "item_count": len(items),
        "currency_totals": {
            currency: _format_micros(amount) for currency, amount in sorted(totals.items())
        },
        "status": "submitted",
        "created_at": _text(dispute["created_at"]),
    }
    if (
        type(payload) is not dict
        or payload != expected_payload
        or _canonical_json(payload) != row["payload_json"]
        or dispute["status"]
        not in {"submitted", "under_review", "approved", "rejected", "cancelled"}
        or row["actor_user_id"] != dispute["created_by_user_id"]
        or row["source_request_id"] != dispute["create_request_id"]
        or row["source_trace_id"] != dispute["create_trace_id"]
        or row["occurred_at"] != dispute["created_at"]
        or row["created_at"] != row["occurred_at"]
    ):
        raise ApiError("billing_dispute.integrity_failed")
    return build_webhook_event(
        tenant_id=tenant_id,
        event_type="billing_dispute.created",
        event_id=event_id,
        timestamp=_parse_time(row["occurred_at"]),
        actor_id=_text(row["actor_user_id"]),
        object_id=dispute_id,
        trace_id=_text(row["source_trace_id"]),
        source_request_id=_text(row["source_request_id"]),
        payload=payload,
    )


def _verify_snapshot(
    connection: sqlite3.Connection,
    event: WebhookEvent,
    *,
    existing: object,
    frozen_at: object,
) -> str:
    """验证创建时 delivery 快照与当前权威 original intent 集合完全一致。"""

    if type(frozen_at) is not str:
        raise ApiError("billing_dispute.integrity_failed")
    _parse_time(frozen_at)
    values = _snapshot(existing)
    if values != _delivery_ids(connection, event):
        raise ApiError("billing_dispute.integrity_failed")
    return _canonical_json(values)


def _renew(config: AppConfig, claim: BillingDisputeEventClaim, *, now: datetime | None) -> None:
    """以精确租约续期并推进 generation；成功提交后才更新内存 claim。"""

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        token = secrets.token_hex(32)
        generation = claim.lease_generation + 1
        expires = current + timedelta(seconds=EVENT_LEASE_SECONDS)
        cursor = connection.execute(
            """
            UPDATE billing_dispute_event_outbox
            SET lease_generation=?,lease_token=?,lease_expires_at=?,updated_at=?
            WHERE tenant_id=? AND billing_dispute_event_outbox_id=? AND status='processing'
              AND attempt_count=? AND lease_generation=? AND lease_owner_id=?
              AND lease_token=? AND delivery_snapshot_json=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                generation,
                token,
                _db_time(expires),
                _db_time(current),
                claim.tenant_id,
                claim.outbox_id,
                claim.attempt_count,
                claim.lease_generation,
                claim.lease_owner_id,
                claim.lease_token,
                claim.snapshot_json,
                _db_time(current),
            ),
        )
        if cursor.rowcount != 1:
            # 过期、接管或任一私有坐标变化都会让旧 owner 立即失权。
            raise ApiError("billing_dispute.unavailable")
        connection.commit()
        claim.lease_generation = generation
        claim.lease_token = token
        claim.lease_expires_at = expires
    except Exception:
        _rollback(connection)
        raise
    finally:
        connection.close()


def _finish(
    config: AppConfig,
    claim: BillingDisputeEventClaim,
    *,
    error_code: str | None,
    delivery_terminal: bool = False,
    now: datetime | None,
) -> bool:
    """用最新租约提交终态或有界退避；只有 finish 才增加一次投递 attempt。"""

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        current_text = _db_time(current)
        # 一轮实际 dispatcher 结果只记一次 attempt；续租次数不会膨胀失败计数。
        attempts = claim.attempt_count + 1
        terminal = (
            error_code == "webhook.payload_invalid"
            or (delivery_terminal and error_code == "webhook.delivery_unavailable")
            or attempts >= MAX_ATTEMPTS
        )
        if error_code is None:
            status = "succeeded"
        elif terminal:
            status = "failed"
            from metahub_control.webhooks.service import fail_pending_webhook_event_deliveries

            fail_pending_webhook_event_deliveries(
                connection,
                tenant_id=claim.tenant_id,
                event_id=claim.event_id,
                event_type="billing_dispute.created",
                updated_at=current_text,
                failure_code=error_code,
            )
        else:
            status = "pending"
        delay = BACKOFF_SECONDS[min(max(attempts - 1, 0), len(BACKOFF_SECONDS) - 1)]
        next_attempt = _db_time(current + timedelta(seconds=delay)) if status == "pending" else None
        cursor = connection.execute(
            """
            UPDATE billing_dispute_event_outbox
            SET status=?,attempt_count=?,lease_generation=lease_generation+1,
                lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
                next_attempt_at=?,last_error_code=?,succeeded_at=?,updated_at=?
            WHERE tenant_id=? AND billing_dispute_event_outbox_id=? AND status='processing'
              AND attempt_count=? AND lease_generation=? AND lease_owner_id=?
              AND lease_token=? AND delivery_snapshot_json=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                status,
                attempts,
                next_attempt,
                None if status == "succeeded" else error_code,
                current_text if status == "succeeded" else None,
                current_text,
                claim.tenant_id,
                claim.outbox_id,
                claim.attempt_count,
                claim.lease_generation,
                claim.lease_owner_id,
                claim.lease_token,
                claim.snapshot_json,
                current_text,
            ),
        )
        if cursor.rowcount != 1:
            # late owner 的 generation/token 已失效，不能覆盖接管者写入的新状态。
            connection.rollback()
            return False
        connection.commit()
        return True
    except (ApiError, sqlite3.Error, TypeError, ValueError):
        _rollback(connection)
        return False
    finally:
        connection.close()


def _advance_fairness(connection: sqlite3.Connection, tenant_id: str, now: str) -> None:
    """在领取事务内推进全局序列，使不同历史计数的积压 tenant 仍严格轮转。"""

    connection.execute(
        """
        INSERT INTO billing_dispute_scheduler_tenants(
          tenant_id,last_claimed_at,last_claim_sequence,state_revision,created_at,updated_at
        ) VALUES(
          ?,?,
          (SELECT COALESCE(MAX(last_claim_sequence),0)+1
             FROM billing_dispute_scheduler_tenants),
          1,?,?
        )
        ON CONFLICT(tenant_id) DO UPDATE SET
          last_claimed_at=excluded.last_claimed_at,
          last_claim_sequence=excluded.last_claim_sequence,
          state_revision=billing_dispute_scheduler_tenants.state_revision+1,
          updated_at=excluded.updated_at
        """,
        (tenant_id, now, now, now),
    )


def _quarantine(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    owner: str,
    token: str,
    attempt: int,
    generation: int,
    expires: datetime,
    now: str,
) -> None:
    """先取得私有租约，再用第二次 CAS 终结无法重建的 source 及其 pending intent。"""

    claimed = connection.execute(
        """
        UPDATE billing_dispute_event_outbox
        SET status='processing',attempt_count=?,lease_generation=?,
            lease_owner_id=?,lease_token=?,
            lease_expires_at=?,next_attempt_at=NULL,last_error_code=NULL,updated_at=?
        WHERE tenant_id=? AND billing_dispute_event_outbox_id=? AND status=?
          AND attempt_count=? AND lease_generation=?
          AND (status='pending' OR unixepoch(lease_expires_at)<=unixepoch(?))
        """,
        (
            attempt,
            generation,
            owner,
            token,
            _db_time(expires),
            now,
            row["tenant_id"],
            row["billing_dispute_event_outbox_id"],
            row["status"],
            row["attempt_count"],
            row["lease_generation"],
            now,
        ),
    )
    if claimed.rowcount != 1:
        raise ApiError("billing_dispute.unavailable")
    if row["event_type"] == "billing_dispute.created":
        from metahub_control.webhooks.service import fail_pending_webhook_event_deliveries

        fail_pending_webhook_event_deliveries(
            connection,
            tenant_id=_text(row["tenant_id"]),
            event_id=_text(row["billing_dispute_event_id"]),
            event_type="billing_dispute.created",
            updated_at=now,
            failure_code="webhook.payload_invalid",
        )
    failed = connection.execute(
        """
        UPDATE billing_dispute_event_outbox
        SET status='failed',attempt_count=attempt_count+1,
            lease_generation=lease_generation+1,lease_owner_id=NULL,lease_token=NULL,
            lease_expires_at=NULL,next_attempt_at=NULL,
            last_error_code='webhook.payload_invalid',succeeded_at=NULL,updated_at=?
        WHERE tenant_id=? AND billing_dispute_event_outbox_id=? AND status='processing'
          AND attempt_count=? AND lease_generation=? AND lease_owner_id=? AND lease_token=?
        """,
        (
            now,
            row["tenant_id"],
            row["billing_dispute_event_outbox_id"],
            attempt,
            generation,
            owner,
            token,
        ),
    )
    if failed.rowcount != 1:
        raise ApiError("billing_dispute.unavailable")


def _delivery_ids(connection: sqlite3.Connection, event: WebhookEvent) -> list[str]:
    rows = connection.execute(
        """SELECT webhook_delivery_id FROM webhook_deliveries
           WHERE tenant_id=? AND event_id=? AND delivery_kind='original'
           ORDER BY webhook_delivery_id""",
        (event.tenant_id, event.event_id),
    ).fetchall()
    values = [str(row["webhook_delivery_id"]) for row in rows]
    if any(_DELIVERY_ID.fullmatch(value) is None for value in values):
        raise ApiError("billing_dispute.integrity_failed")
    return values


def _snapshot(value: object) -> list[str]:
    """解析去重且稳定排序的 delivery ID 快照，拒绝类型漂移和重复值。"""

    if type(value) is not str:
        raise ValueError("snapshot")
    decoded = json.loads(value)
    if (
        type(decoded) is not list
        or decoded != sorted(set(decoded))
        or any(type(item) is not str or _DELIVERY_ID.fullmatch(item) is None for item in decoded)
    ):
        raise ValueError("snapshot")
    return decoded


def _transaction_now(connection: sqlite3.Connection, override: datetime | None) -> datetime:
    if override is not None:
        if override.tzinfo is None:
            raise ApiError("billing_dispute.unavailable")
        return override.astimezone(UTC).replace(microsecond=0)
    row = connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')").fetchone()
    if row is None:
        raise ApiError("billing_dispute.unavailable")
    return _parse_time(row[0])


def _parse_time(value: object) -> datetime:
    if type(value) is not str or not value.endswith("Z") or "." in value:
        raise ValueError("time")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or _db_time(parsed) != value:
        raise ValueError("time")
    return parsed.astimezone(UTC)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _worker(value: object) -> str:
    if type(value) is not str or _WORKER_ID.fullmatch(value) is None:
        raise ApiError("billing_dispute.unavailable")
    return value


def _matched(value: object, pattern: re.Pattern[str]) -> str:
    text = _text(value)
    if pattern.fullmatch(text) is None:
        raise ApiError("billing_dispute.integrity_failed")
    return text


def _text(value: object) -> str:
    if type(value) is not str or not value:
        raise ApiError("billing_dispute.integrity_failed")
    return value


def _non_negative_int(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ApiError("billing_dispute.integrity_failed")
    return value


def _positive_int(value: object) -> int:
    if type(value) is not int or value < 1:
        raise ApiError("billing_dispute.integrity_failed")
    return value


def _format_micros(value: int) -> str:
    """仅用整数微单位生成六位小数金额，避免浮点舍入进入 Webhook 证据。"""

    amount = _positive_int(value)
    whole, fraction = divmod(amount, 1_000_000)
    return f"{whole}.{fraction:06d}"


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate json")
        result[key] = value
    return result


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except DbError as exc:
        raise ApiError("billing_dispute.unavailable") from exc


def _rollback(connection: sqlite3.Connection) -> None:
    try:
        if connection.in_transaction:
            connection.rollback()
    except sqlite3.Error:
        return


__all__ = [
    "BillingDisputeEventClaim",
    "claim_next_billing_dispute_event",
    "dispatch_claimed_billing_dispute_event",
    "drain_billing_dispute_event_once",
    "recover_billing_dispute_source_request_id",
]
