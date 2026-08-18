"""EVT-100 source outbox：快照、租约、CAS 和固定退避恢复。"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import (
    BRANDING_UPDATED_EVENT_TYPE,
    WebhookEvent,
    build_webhook_event,
)

from .service import _canonical_json, _connect, _db_time, _integer, _sha256, _transaction_now

EVENT_LEASE_SECONDS = 90
EVENT_MAX_ATTEMPTS = 10
EVENT_BACKOFF_SECONDS = (5, 30, 300)
_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_EVENT_ID = re.compile(r"^evt_brd_[0-9a-f]{32}$")


@dataclass(slots=True)
class BrandingEventClaim:
    """一次 source lease；令牌和事件正文不参与 repr。"""

    tenant_id: str
    source_request_id: str
    source_trace_id: str
    event: WebhookEvent = field(repr=False)
    lease_owner_id: str
    lease_token: str = field(repr=False)
    attempt_count: int
    generation: int
    lease_expires_at: datetime
    snapshot: tuple[str, ...]


def drain_branding_event_once(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
    should_stop=None,
) -> bool:
    """每轮最多领取并投递一个 Branding source。"""

    if type(worker_id) is not str or _WORKER_ID.fullmatch(worker_id) is None:
        raise ApiError("branding.event_unavailable")
    if should_stop is not None and should_stop():
        return False
    claim = _claim(config, worker_id=worker_id, now=now)
    if claim is None:
        return False
    error_code = None
    try:
        from metahub_control.webhooks.service import dispatch_webhook_event

        dispatch_webhook_event(
            config,
            claim.tenant_id,
            claim.event,
            request_id=claim.source_request_id,
            trace_id=claim.source_trace_id,
            resume_existing_pending=True,
            create_snapshot_if_missing=False,
            source_lease_guard=lambda: _renew(config, claim, now=now),
        )
    except ValueError:
        error_code = "webhook.payload_invalid"
    except Exception:
        error_code = "webhook.delivery_unavailable"
    return _finish(config, claim, error_code=error_code, now=now)


def _claim(config: AppConfig, *, worker_id: str, now: datetime | None) -> BrandingEventClaim | None:
    """原子领取 pending/过期 leased source，并校验 payload hash。"""

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        now_text = _db_time(current)
        row = connection.execute(
            """
            SELECT event.*,outbox.*
            FROM branding_event_outbox outbox
            JOIN branding_events event
              ON event.tenant_id=outbox.tenant_id
             AND event.branding_event_id=outbox.branding_event_id
            WHERE (outbox.status='pending' AND unixepoch(outbox.next_attempt_at)<=unixepoch(?))
               OR (outbox.status='leased' AND unixepoch(outbox.lease_expires_at)<=unixepoch(?))
            ORDER BY COALESCE(outbox.next_attempt_at,outbox.lease_expires_at),
                     outbox.tenant_id,outbox.created_at,outbox.branding_event_outbox_id
            LIMIT 1
            """,
            (now_text, now_text),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        event = _event_from_row(row)
        snapshot = _snapshot(row["subscription_snapshot_json"])
        old_status = str(row["status"])
        old_attempt = _integer(row["attempt_count"], minimum=0)
        attempt = old_attempt + 1 if old_status == "pending" else old_attempt
        if not 1 <= attempt <= EVENT_MAX_ATTEMPTS:
            raise ApiError("branding.event_unavailable")
        generation = _integer(row["generation"], minimum=0) + 1
        token = secrets.token_hex(32)
        expires = current + timedelta(seconds=EVENT_LEASE_SECONDS)
        connection.execute(
            """
            UPDATE branding_event_outbox
            SET status='leased',attempt_count=?,generation=?,lease_owner_id=?,lease_token=?,
                lease_expires_at=?,next_attempt_at=NULL,last_error_code=NULL,updated_at=?
            WHERE tenant_id=? AND branding_event_outbox_id=? AND status=?
              AND attempt_count=? AND generation=?
              AND (status='pending' OR unixepoch(lease_expires_at)<=unixepoch(?))
            """,
            (
                attempt,
                generation,
                worker_id,
                token,
                _db_time(expires),
                now_text,
                row["tenant_id"],
                row["branding_event_outbox_id"],
                old_status,
                old_attempt,
                row["generation"],
                now_text,
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            connection.rollback()
            return None
        claim = BrandingEventClaim(
            tenant_id=str(row["tenant_id"]),
            source_request_id=str(row["request_id"]),
            source_trace_id=str(row["trace_id"]),
            event=event,
            lease_owner_id=worker_id,
            lease_token=token,
            attempt_count=attempt,
            generation=generation,
            lease_expires_at=expires,
            snapshot=tuple(snapshot),
        )
        connection.commit()
        return claim
    except ApiError:
        connection.rollback()
        raise
    except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
        connection.rollback()
        raise ApiError("branding.event_unavailable") from None
    finally:
        connection.close()


def _renew(config: AppConfig, claim: BrandingEventClaim, *, now: datetime | None) -> None:
    """每次网络尝试前用旧 generation/token CAS 续租。"""

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        generation = claim.generation + 1
        token = secrets.token_hex(32)
        expires = max(current + timedelta(seconds=EVENT_LEASE_SECONDS), claim.lease_expires_at)
        connection.execute(
            """
            UPDATE branding_event_outbox
            SET generation=?,lease_token=?,lease_expires_at=?,updated_at=?
            WHERE tenant_id=? AND branding_event_outbox_id=? AND status='leased'
              AND attempt_count=? AND generation=? AND lease_owner_id=? AND lease_token=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                generation,
                token,
                _db_time(expires),
                _db_time(current),
                claim.tenant_id,
                _outbox_id_for_event(claim.event.event_id),
                claim.attempt_count,
                claim.generation,
                claim.lease_owner_id,
                claim.lease_token,
                _db_time(current),
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise ApiError("branding.event_unavailable")
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
    claim: BrandingEventClaim,
    *,
    error_code: str | None,
    now: datetime | None,
) -> bool:
    """用完整 lease CAS 收敛 delivered/no_subscription/pending/failed。"""

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        old_status = "leased"
        if error_code is None:
            status = "no_subscription" if not claim.snapshot else "delivered"
            next_attempt = None
            terminal_error = None
            delivered = _db_time(current)
        else:
            status = "failed" if claim.attempt_count >= EVENT_MAX_ATTEMPTS else "pending"
            delay_index = min(claim.attempt_count - 1, len(EVENT_BACKOFF_SECONDS) - 1)
            delay = EVENT_BACKOFF_SECONDS[delay_index]
            next_attempt = (
                _db_time(current + timedelta(seconds=delay)) if status == "pending" else None
            )
            terminal_error = error_code
            delivered = None
        connection.execute(
            """
            UPDATE branding_event_outbox
            SET status=?,generation=generation+1,lease_owner_id=NULL,lease_token=NULL,
                lease_expires_at=NULL,next_attempt_at=?,last_error_code=?,delivered_at=?,updated_at=?
            WHERE tenant_id=? AND branding_event_outbox_id=? AND status=?
              AND attempt_count=? AND generation=? AND lease_owner_id=? AND lease_token=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                status,
                next_attempt,
                terminal_error,
                delivered,
                _db_time(current),
                claim.tenant_id,
                _outbox_id_for_event(claim.event.event_id),
                old_status,
                claim.attempt_count,
                claim.generation,
                claim.lease_owner_id,
                claim.lease_token,
                _db_time(current),
            ),
        )
        changed = connection.execute("SELECT changes()").fetchone()[0] == 1
        if changed:
            connection.commit()
        else:
            connection.rollback()
        return changed
    except (sqlite3.Error, TypeError, ValueError):
        connection.rollback()
        return False
    finally:
        connection.close()


def _event_from_row(row: sqlite3.Row) -> WebhookEvent:
    """从 immutable source row 重建事件，outbox JSON 只作为 hash 对照。"""

    payload_json = str(row["payload_json"])
    if row["payload_sha256"] != _sha256(payload_json.encode("utf-8")):
        raise ApiError("branding.event_unavailable")
    payload = json.loads(payload_json)
    event_id = str(row["webhook_event_id"])
    if _EVENT_ID.fullmatch(event_id) is None:
        raise ApiError("branding.event_unavailable")
    event = build_webhook_event(
        tenant_id=str(row["tenant_id"]),
        event_type=BRANDING_UPDATED_EVENT_TYPE,
        payload=payload,
        actor_id=str(row["actor_user_id"]),
        object_id=str(row["branding_config_id"]),
        trace_id=str(row["trace_id"]),
        source_request_id=str(row["request_id"]),
        event_id=event_id,
        timestamp=datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00")),
    )
    if _canonical_json(event.payload) != payload_json:
        raise ApiError("branding.event_unavailable")
    return event


def _snapshot(value: object) -> list[str]:
    """验证 immutable delivery ID 快照，空列表也是已冻结事实。"""

    if type(value) is not str:
        raise ApiError("branding.event_unavailable")
    decoded = json.loads(value)
    if type(decoded) is not list or decoded != sorted(set(decoded)):
        raise ApiError("branding.event_unavailable")
    if any(type(item) is not str or not item.startswith("whd_") for item in decoded):
        raise ApiError("branding.event_unavailable")
    return decoded


def _outbox_id_for_event(event_id: str) -> str:
    """按公开事件 ID 导出 outbox owner ID。"""

    if _EVENT_ID.fullmatch(event_id) is None:
        raise ApiError("branding.event_unavailable")
    return "bro_" + event_id[8:]


__all__ = ["BrandingEventClaim", "drain_branding_event_once"]
