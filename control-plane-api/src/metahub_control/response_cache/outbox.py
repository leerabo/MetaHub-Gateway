"""Response Cache source outbox：冻结订阅快照并以租户租约恢复 Webhook 投递。"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import WebhookEvent, build_webhook_event

# 事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。
EVENT_LEASE_SECONDS = 90
EVENT_MAX_ATTEMPTS = 10
EVENT_BACKOFF_SECONDS = (5, 30, 300)
_EVENT_ID = re.compile(r"^rcev_[0-9a-f]{32}$")
_DELIVERY_ID = re.compile(r"^whd_[0-9a-f]{32}$")
_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


@dataclass(slots=True)
class ResponseCacheEventClaim:
    """一条 tenant-qualified source claim；续租会原子替换私有 token/generation。

    事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。 当前定义负责 ResponseCacheEventClaim 对应步骤。"""

    event_id: str
    tenant_id: str
    source_request_id: str
    source_trace_id: str
    event: WebhookEvent = field(repr=False)
    lease_owner_id: str
    lease_token: str = field(repr=False)
    attempt_count: int
    generation: int
    lease_expires_at: datetime


def enqueue_response_cache_event(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    event_type: str,
    object_id: str,
    actor_user_id: str | None,
    trace_id: str,
    source_request_id: str,
    payload: dict[str, object],
    created_at: str,
) -> str:
    """在调用方事务只插入 source；0069 trigger 会恰好创建一个 pending outbox。"""

    if not connection.in_transaction:
        # 事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。
        raise ValueError("response cache event requires transaction")
    if event_type not in {"cache.hit", "cache.invalidated"}:
        # 事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。
        raise ValueError("unsupported response cache event")
    event_id = f"rcev_{uuid4().hex}"
    canonical_payload = _canonical_json(payload)
    # 先经过 Shared exact validator，避免依赖数据库 trigger 的字段子集检查放行坏金额组合。
    build_webhook_event(
        tenant_id=tenant_id,
        event_type=event_type,
        event_id=event_id,
        timestamp=parse_outbox_time(created_at),
        actor_id=actor_user_id,
        object_id=object_id,
        trace_id=trace_id,
        source_request_id=source_request_id,
        payload=payload,
    )
    connection.execute(
        """
        INSERT INTO response_cache_events(
          response_cache_event_id,tenant_id,event_type,object_id,actor_user_id,
          trace_id,payload_json,occurred_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            event_id,
            tenant_id,
            event_type,
            object_id,
            actor_user_id,
            trace_id,
            canonical_payload,
            created_at,
        ),
    )
    return event_id


def claim_next_response_cache_event(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> ResponseCacheEventClaim | None:
    """领取一条到期 source，并在同事务冻结 original delivery 快照。"""

    owner = _worker_id(worker_id)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        now_text = _db_time(current)
        row = connection.execute(
            """
            SELECT event.*,outbox.status,outbox.attempt_count,outbox.generation,
                   outbox.lease_owner_id,outbox.lease_token,outbox.lease_expires_at,
                   outbox.subscription_snapshot_json,outbox.snapshot_frozen_at,
                   outbox.next_attempt_at
            FROM response_cache_event_outbox outbox
            JOIN response_cache_events event
              ON event.tenant_id=outbox.tenant_id
             AND event.response_cache_event_id=outbox.response_cache_event_id
            WHERE (outbox.status='pending'
                    AND unixepoch(outbox.next_attempt_at)<=unixepoch(?))
               OR (outbox.status='leased'
                    AND unixepoch(outbox.lease_expires_at)<=unixepoch(?))
            ORDER BY outbox.created_at,event.response_cache_event_id
            LIMIT 1
            """,
            (now_text, now_text),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        try:
            event, source_request_id = _event_from_row(connection, row)
        # 事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。
        except (ApiError, TypeError, ValueError):
            _dead_letter_corrupt_source(connection, row, owner=owner, current=current)
            connection.commit()
            return None

        frozen_at = row["snapshot_frozen_at"]
        if frozen_at is None:
            from metahub_control.webhooks.service import (
                freeze_webhook_event_delivery_snapshot,
            )

            frozen_at = now_text
            freeze_webhook_event_delivery_snapshot(
                connection,
                event,
                frozen_at=frozen_at,
            )
            snapshot = _delivery_snapshot(connection, event)
        else:
            snapshot = _parse_snapshot(row["subscription_snapshot_json"])
            if snapshot != _delivery_snapshot(connection, event):
                # 事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。
                raise ApiError("response_cache.unavailable")

        token = secrets.token_hex(32)
        generation = int(row["generation"]) + 1
        expires = current + timedelta(seconds=EVENT_LEASE_SECONDS)
        connection.execute(
            """
            UPDATE response_cache_event_outbox
            SET status='leased',generation=?,lease_owner_id=?,lease_token=?,
                lease_expires_at=?,subscription_snapshot_json=?,snapshot_frozen_at=?,
                last_error_code=NULL,updated_at=?
            WHERE tenant_id=? AND response_cache_event_id=? AND status=?
              AND attempt_count=? AND generation=?
              AND (status='pending' OR unixepoch(lease_expires_at)<=unixepoch(?))
            """,
            (
                generation,
                owner,
                token,
                _db_time(expires),
                _canonical_json(snapshot),
                frozen_at,
                now_text,
                row["tenant_id"],
                row["response_cache_event_id"],
                row["status"],
                row["attempt_count"],
                row["generation"],
                now_text,
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            connection.rollback()
            return None
        claim = ResponseCacheEventClaim(
            event_id=str(row["response_cache_event_id"]),
            tenant_id=str(row["tenant_id"]),
            source_request_id=source_request_id,
            source_trace_id=str(row["trace_id"]),
            event=event,
            lease_owner_id=owner,
            lease_token=token,
            attempt_count=int(row["attempt_count"]),
            generation=generation,
            lease_expires_at=expires,
        )
        connection.commit()
        return claim
    # 事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。
    except ApiError:
        connection.rollback()
        raise
    # 事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。
    except (sqlite3.Error, TypeError, ValueError):
        connection.rollback()
        # 事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。
        raise ApiError("response_cache.unavailable") from None
    finally:
        connection.close()


def dispatch_claimed_response_cache_event(
    config: AppConfig,
    claim: ResponseCacheEventClaim,
    *,
    now: datetime | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """恢复冻结快照中的 pending delivery，并以 source CAS 记录成功或有界重试。"""

    def source_guard() -> None:
        """每次真实网络尝试前续租，停服或 owner 漂移后禁止继续出网。"""

        if should_stop is not None and should_stop():
            # 事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。
            raise ApiError("response_cache.unavailable")
        _renew(config, claim, now=now)

    try:
        if should_stop is not None and should_stop():
            # 事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。
            raise ApiError("response_cache.unavailable")
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
        return _finish(config, claim, error_code=None, now=now)
    # 事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。
    except ValueError:
        return _finish(config, claim, error_code="webhook.payload_invalid", now=now)
    except Exception:
        return _finish(config, claim, error_code="webhook.delivery_unavailable", now=now)


def drain_response_cache_event_once(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """每轮最多恢复一条 source，空队列不创建任何数据库或网络副作用。"""

    if should_stop is not None and should_stop():
        return False
    claim = claim_next_response_cache_event(config, worker_id=worker_id, now=now)
    if claim is None:
        return False
    dispatch_claimed_response_cache_event(
        config,
        claim,
        now=now,
        should_stop=should_stop,
    )
    return True


def recover_response_cache_source_request_id(
    connection: sqlite3.Connection,
    envelope: dict[str, object],
) -> str:
    """从 immutable source graph 恢复不公开的 request owner，拒绝篡改 delivery 信封。"""

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
        # 事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。
        raise ApiError("webhook.delivery_unavailable")
    row = connection.execute(
        """
        SELECT event.*,outbox.status,outbox.attempt_count,outbox.generation,
               outbox.lease_owner_id,outbox.lease_token,outbox.lease_expires_at,
               outbox.subscription_snapshot_json,outbox.snapshot_frozen_at,
               outbox.next_attempt_at
        FROM response_cache_events event
        JOIN response_cache_event_outbox outbox
          ON outbox.tenant_id=event.tenant_id
         AND outbox.response_cache_event_id=event.response_cache_event_id
        WHERE event.tenant_id=? AND event.response_cache_event_id=?
        """,
        (envelope.get("tenant_id"), envelope.get("event_id")),
    ).fetchone()
    if row is None:
        # 事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。
        raise ApiError("webhook.delivery_unavailable")
    try:
        event, source_request_id = _event_from_row(connection, row)
    # 事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。
    except (ApiError, TypeError, ValueError):
        # 事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。
        raise ApiError("webhook.delivery_unavailable") from None
    if event.to_payload() != envelope:
        # 事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。
        raise ApiError("webhook.delivery_unavailable")
    return source_request_id


def _event_from_row(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> tuple[WebhookEvent, str]:
    """重建 source payload 并逐字段绑定 hit/invalidation owner，而非信任 outbox JSON。"""

    event_id = str(row["response_cache_event_id"])
    tenant_id = str(row["tenant_id"])
    event_type = str(row["event_type"])
    object_id = str(row["object_id"])
    if _EVENT_ID.fullmatch(event_id) is None or event_type not in {
        "cache.hit",
        "cache.invalidated",
    }:
        # 事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。
        raise ApiError("response_cache.unavailable")
    if event_type == "cache.invalidated":
        source = connection.execute(
            """
            SELECT * FROM response_cache_invalidations
            WHERE tenant_id=? AND cache_invalidation_id=?
            """,
            (tenant_id, object_id),
        ).fetchone()
        if (
            source is None
            or source["actor_user_id"] != row["actor_user_id"]
            or source["operation_trace_id"] != row["trace_id"]
            or source["created_at"] != row["occurred_at"]
        ):
            # 事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。
            raise ApiError("response_cache.unavailable")
        payload = {
            "schema_version": 1,
            "cache_invalidation_id": object_id,
            "scope": source["scope"],
            "match_mode": source["match_mode"],
            "tag_count": int(source["tag_count"]),
            "matched_entry_count": int(source["matched_entry_count"]),
            "deleted_entry_count": int(source["deleted_entry_count"]),
            "fenced_fill_count": int(source["fenced_fill_count"]),
            "reason_code": source["reason_code"],
        }
        source_request_id = str(source["operation_request_id"])
    else:
        source = connection.execute(
            """
            SELECT * FROM response_cache_hits
            WHERE tenant_id=? AND response_cache_hit_id=?
            """,
            (tenant_id, object_id),
        ).fetchone()
        if (
            source is None
            or row["actor_user_id"] is not None
            or source["trace_id"] != row["trace_id"]
            or source["created_at"] != row["occurred_at"]
        ):
            # 事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。
            raise ApiError("response_cache.unavailable")
        payload = _hit_payload(source)
        source_request_id = str(source["request_id"])
    if row["payload_json"] != _canonical_json(payload):
        # 事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。
        raise ApiError("response_cache.unavailable")
    event = build_webhook_event(
        tenant_id=tenant_id,
        event_type=event_type,
        event_id=event_id,
        timestamp=parse_outbox_time(row["occurred_at"]),
        actor_id=row["actor_user_id"],
        object_id=object_id,
        trace_id=str(row["trace_id"]),
        source_request_id=source_request_id,
        payload=payload,
    )
    return event, source_request_id


def _hit_payload(row: sqlite3.Row) -> dict[str, object]:
    """从 hit 不可变列构造 exact 十三字段 payload，不读取 entry/ciphertext/source 正文。"""

    return {
        "schema_version": 1,
        "cache_hit_id": str(row["response_cache_hit_id"]),
        "cache_policy_id": str(row["cache_policy_id"]),
        "cache_policy_version": int(row["cache_policy_version"]),
        "charge_cache_hits": bool(row["charge_cache_hits"]),
        "source_generation_cost_status": str(row["source_generation_cost_status"]),
        "source_generation_cost_cny": _wire_amount(
            row["source_generation_cost_status"],
            row["source_generation_cost_cny_micros"],
        ),
        "current_reference_cost_status": str(row["current_reference_cost_status"]),
        "current_reference_cost_cny": _wire_amount(
            row["current_reference_cost_status"],
            row["current_reference_cost_cny_micros"],
        ),
        "saved_cost_status": str(row["saved_cost_status"]),
        "saved_cost_cny": _wire_amount(row["saved_cost_status"], row["saved_cost_cny_micros"]),
        "billed_cost_status": str(row["billed_cost_status"]),
        "billed_cost_cny": _wire_amount(row["billed_cost_status"], row["billed_cost_cny_micros"]),
    }


def _wire_amount(status: object, value: object) -> str | None:
    """把 known 微元转为六位小数；unavailable 必须保持 null。"""

    if status == "unavailable" and value is None:
        return None
    if status != "known" or type(value) is not int or value < 0:
        # 事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。
        raise ApiError("response_cache.unavailable")
    units, micros = divmod(value, 1_000_000)
    return f"{units}.{micros:06d}"


def _delivery_snapshot(
    connection: sqlite3.Connection,
    event: WebhookEvent,
) -> list[str]:
    """冻结 original delivery ID 集合；URL、Secret 与 payload 不进入 source outbox。

    事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。 当前定义负责 _delivery_snapshot 对应步骤。"""

    rows = connection.execute(
        """
        SELECT webhook_delivery_id FROM webhook_deliveries
        WHERE tenant_id=? AND event_id=? AND delivery_kind='original'
        ORDER BY webhook_delivery_id
        """,
        (event.tenant_id, event.event_id),
    ).fetchall()
    values = [str(row["webhook_delivery_id"]) for row in rows]
    if any(_DELIVERY_ID.fullmatch(value) is None for value in values):
        # 事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。
        raise ApiError("response_cache.unavailable")
    return values


def _parse_snapshot(value: object) -> list[str]:
    """严格读取一次冻结的 delivery ID 数组，禁止恢复时追加后来订阅者。"""

    try:
        decoded = json.loads(str(value))
    # 事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。
    except (TypeError, ValueError, json.JSONDecodeError):
        # 事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。
        raise ApiError("response_cache.unavailable") from None
    if (
        type(decoded) is not list
        or decoded != sorted(set(decoded))
        or any(type(item) is not str or _DELIVERY_ID.fullmatch(item) is None for item in decoded)
    ):
        # 事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。
        raise ApiError("response_cache.unavailable")
    return decoded


def _renew(
    config: AppConfig,
    claim: ResponseCacheEventClaim,
    *,
    now: datetime | None,
) -> None:
    """以新 token/generation 续租；旧 claim 随后不能完成 source。

    事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。 当前定义负责 _renew 对应步骤。"""

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        token = secrets.token_hex(32)
        generation = claim.generation + 1
        expires = current + timedelta(seconds=EVENT_LEASE_SECONDS)
        connection.execute(
            """
            UPDATE response_cache_event_outbox
            SET generation=?,lease_token=?,lease_expires_at=?,updated_at=?
            WHERE tenant_id=? AND response_cache_event_id=? AND status='leased'
              AND attempt_count=? AND generation=? AND lease_owner_id=? AND lease_token=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                generation,
                token,
                _db_time(expires),
                _db_time(current),
                claim.tenant_id,
                claim.event_id,
                claim.attempt_count,
                claim.generation,
                claim.lease_owner_id,
                claim.lease_token,
                _db_time(current),
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            # 事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。
            raise ApiError("response_cache.unavailable")
        connection.commit()
        claim.generation = generation
        claim.lease_token = token
        claim.lease_expires_at = expires
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _finish(
    config: AppConfig,
    claim: ResponseCacheEventClaim,
    *,
    error_code: str | None,
    now: datetime | None,
) -> bool:
    """以 lease CAS 完成、重试或 dead-letter，计数只在网络 attempt 终结时增加。"""

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        now_text = _db_time(current)
        attempt_count = claim.attempt_count + 1
        terminal = error_code == "webhook.payload_invalid" or attempt_count >= EVENT_MAX_ATTEMPTS
        # 事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。
        if error_code is None:
            status = "dispatched"
        elif terminal:
            status = "dead_letter"
            attempt_count = EVENT_MAX_ATTEMPTS
            _fail_pending_deliveries(
                connection,
                tenant_id=claim.tenant_id,
                event_id=claim.event_id,
                event_type=claim.event.event_type,
                updated_at=now_text,
                failure_code=error_code,
            )
        else:
            status = "pending"
        delay = EVENT_BACKOFF_SECONDS[
            min(max(attempt_count - 1, 0), len(EVENT_BACKOFF_SECONDS) - 1)
        ]
        next_attempt_at = (
            _db_time(current + timedelta(seconds=delay)) if status == "pending" else now_text
        )
        connection.execute(
            """
            UPDATE response_cache_event_outbox
            SET status=?,attempt_count=?,generation=generation+1,
                lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
                next_attempt_at=?,last_error_code=?,dispatched_at=?,updated_at=?
            WHERE tenant_id=? AND response_cache_event_id=? AND status='leased'
              AND attempt_count=? AND generation=? AND lease_owner_id=? AND lease_token=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                status,
                attempt_count,
                next_attempt_at,
                None if status == "dispatched" else error_code,
                now_text if status == "dispatched" else None,
                now_text,
                claim.tenant_id,
                claim.event_id,
                claim.attempt_count,
                claim.generation,
                claim.lease_owner_id,
                claim.lease_token,
                now_text,
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            connection.rollback()
            return False
        connection.commit()
        return True
    # 事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。
    except (ApiError, sqlite3.Error, TypeError, ValueError):
        connection.rollback()
        return False
    finally:
        connection.close()


def _dead_letter_corrupt_source(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    owner: str,
    current: datetime,
) -> None:
    """用两个合法 transition 隔离损坏 source，避免 pending 行无限占用队首。"""

    now_text = _db_time(current)
    token = secrets.token_hex(32)
    generation = int(row["generation"]) + 1
    connection.execute(
        """
        UPDATE response_cache_event_outbox
        SET status='leased',generation=?,lease_owner_id=?,lease_token=?,
            lease_expires_at=?,updated_at=?
        WHERE tenant_id=? AND response_cache_event_id=? AND status=?
          AND attempt_count=? AND generation=?
          AND (status='pending' OR unixepoch(lease_expires_at)<=unixepoch(?))
        """,
        (
            generation,
            owner,
            token,
            _db_time(current + timedelta(seconds=EVENT_LEASE_SECONDS)),
            now_text,
            row["tenant_id"],
            row["response_cache_event_id"],
            row["status"],
            row["attempt_count"],
            row["generation"],
            now_text,
        ),
    )
    if connection.execute("SELECT changes()").fetchone()[0] != 1:
        # 事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。
        raise ApiError("response_cache.unavailable")
    _fail_pending_deliveries(
        connection,
        tenant_id=str(row["tenant_id"]),
        event_id=str(row["response_cache_event_id"]),
        event_type=str(row["event_type"]),
        updated_at=now_text,
        failure_code="webhook.payload_invalid",
    )
    connection.execute(
        """
        UPDATE response_cache_event_outbox
        SET status='dead_letter',attempt_count=10,generation=generation+1,
            lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
            next_attempt_at=?,last_error_code='webhook.payload_invalid',updated_at=?
        WHERE tenant_id=? AND response_cache_event_id=? AND status='leased'
          AND generation=? AND lease_owner_id=? AND lease_token=?
        """,
        (
            now_text,
            now_text,
            row["tenant_id"],
            row["response_cache_event_id"],
            generation,
            owner,
            token,
        ),
    )
    if connection.execute("SELECT changes()").fetchone()[0] != 1:
        # 事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。
        raise ApiError("response_cache.unavailable")


def _fail_pending_deliveries(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    event_id: str,
    event_type: str,
    updated_at: str,
    failure_code: str,
) -> None:
    """source dead-letter 与中心 delivery 终态共享事务，不能遗留不可恢复 pending。"""

    from metahub_control.webhooks.service import fail_pending_webhook_event_deliveries

    fail_pending_webhook_event_deliveries(
        connection,
        tenant_id=tenant_id,
        event_id=event_id,
        event_type=event_type,
        updated_at=updated_at,
        failure_code=failure_code,
    )


def parse_outbox_time(value: object) -> datetime:
    """解析 canonical UTC 秒时间，拒绝数据库中的模糊本地时间。"""

    if type(value) is not str:
        # 事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。
        raise ValueError("invalid response cache outbox time")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        # 事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。
        raise ValueError("invalid response cache outbox time")
    return parsed.astimezone(UTC)


def _transaction_now(
    connection: sqlite3.Connection,
    override: datetime | None,
) -> datetime:
    """生产使用数据库 UTC 秒；显式 override 仅供确定性租约测试。"""

    if override is not None:
        if override.tzinfo is None:
            # 事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。
            raise ApiError("response_cache.unavailable")
        return override.astimezone(UTC).replace(microsecond=0)
    row = connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')").fetchone()
    if row is None:
        # 事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。
        raise ApiError("response_cache.unavailable")
    return parse_outbox_time(row[0])


def _db_time(value: datetime) -> str:
    """把租约时间规范成 SQLite 与 Webhook 共用的 UTC 秒文本。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical_json(value: object) -> str:
    """安全 payload/snapshot 使用稳定 ASCII JSON，禁止日志化返回值。"""

    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _worker_id(value: object) -> str:
    """限制持久 lease owner，避免控制字符进入数据库或结构化日志。"""

    if type(value) is not str or _WORKER_ID.fullmatch(value) is None:
        # 事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。
        raise ApiError("response_cache.unavailable")
    return value


def _connect(config: AppConfig) -> sqlite3.Connection:
    """创建项目标准连接；数据库 URL 和底层异常不得进入 source 错误。"""

    try:
        return connect_database(config.database_url)
    # 事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。
    except DbError:
        # 事件恢复必须以租户和租约所有者的 CAS 约束推进，防止迟到任务覆盖终态。
        raise ApiError("response_cache.unavailable") from None


__all__ = [
    "ResponseCacheEventClaim",
    "claim_next_response_cache_event",
    "dispatch_claimed_response_cache_event",
    "drain_response_cache_event_once",
    "enqueue_response_cache_event",
    "parse_outbox_time",
    "recover_response_cache_source_request_id",
]
