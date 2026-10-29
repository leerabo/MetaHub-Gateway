"""EVT-099 source outbox 的租户快照、续租、重试与恢复绑定。"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import WebhookEvent, build_webhook_event

from .service import _canonical_json, _connect, _count, _db_time, _sha256, _text, _transaction_now

EVENT_LEASE_SECONDS = 90
EVENT_MAX_ATTEMPTS = 10
EVENT_BACKOFF_SECONDS = (5, 30, 300)
_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_CUSTOMER_ID = re.compile(r"^pcu_[0-9a-f]{32}$")
_OUTBOX_ID = re.compile(r"^pco_[0-9a-f]{32}$")
_DELIVERY_ID = re.compile(r"^whd_[0-9a-f]{32}$")


@dataclass(slots=True)
class PartnerEventClaim:
    """保存一次 EVT-099 source lease；事件正文和令牌不参与调试表示。"""

    tenant_id: str
    partner_id: str
    partner_customer_id: str
    source_request_id: str
    source_trace_id: str
    event: WebhookEvent = field(repr=False)
    lease_owner_id: str
    lease_token: str = field(repr=False)
    attempt_count: int
    generation: int
    lease_expires_at: datetime


def claim_next_partner_event(
    config: AppConfig, *, worker_id: str, now: datetime | None = None
) -> PartnerEventClaim | None:
    """领取一个到期 source，并在首次领取事务中冻结 original delivery 集合。"""

    if type(worker_id) is not str or _WORKER_ID.fullmatch(worker_id) is None:
        raise ApiError("partner.event_unavailable")
    connection = _connect(config)
    try:
        current = _transaction_now(connection, now)
        now_text = _db_time(current)
        due = connection.execute(
            """
            SELECT 1 FROM partner_customer_outbox
            WHERE (status='pending' AND unixepoch(next_attempt_at)<=unixepoch(?))
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
            SELECT customer.*,partner.status partner_status,
                   outbox.partner_customer_outbox_id,outbox.webhook_event_id,
                   outbox.actor_user_id,outbox.source_request_id,outbox.source_trace_id,
                   outbox.event_timestamp,outbox.payload_json,
                   outbox.payload_sha256 AS outbox_payload_sha256,
                   outbox.status outbox_status,outbox.attempt_count,outbox.generation,
                   outbox.lease_owner_id,outbox.lease_token,outbox.lease_expires_at,
                   outbox.next_attempt_at,outbox.subscription_snapshot_json,
                   outbox.snapshot_frozen_at,outbox.last_error_code,outbox.delivered_at,
                   outbox.created_at outbox_created_at,outbox.updated_at outbox_updated_at
            FROM partner_customer_outbox outbox
            JOIN partner_customers customer
              ON customer.tenant_id=outbox.tenant_id
             AND customer.partner_customer_id=outbox.partner_customer_id
            JOIN partners partner
              ON partner.tenant_id=outbox.tenant_id AND partner.partner_id=outbox.partner_id
            WHERE (outbox.status='pending' AND unixepoch(outbox.next_attempt_at)<=unixepoch(?))
               OR (outbox.status='leased' AND unixepoch(outbox.lease_expires_at)<=unixepoch(?))
            ORDER BY COALESCE(outbox.next_attempt_at,outbox.lease_expires_at),
                     outbox.tenant_id,outbox.created_at,outbox.partner_customer_outbox_id
            LIMIT 1
            """,
            (now_text, now_text),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        event, source_request_id, source_trace_id = _event_from_row(row)
        frozen_at = row["snapshot_frozen_at"]
        if frozen_at is None:
            from metahub_control.webhooks.service import freeze_webhook_event_delivery_snapshot

            freeze_webhook_event_delivery_snapshot(connection, event, frozen_at=now_text)
            snapshot = _delivery_snapshot(connection, event)
            frozen_at = now_text
        else:
            snapshot = _parse_snapshot(row["subscription_snapshot_json"])
            if snapshot != _delivery_snapshot(connection, event):
                raise ApiError("partner.event_unavailable")
        old_status = _text(row["outbox_status"])
        old_attempt = _integer(row["attempt_count"], minimum=0, maximum=10)
        attempt = old_attempt + 1 if old_status == "pending" else old_attempt
        if not 1 <= attempt <= EVENT_MAX_ATTEMPTS:
            raise ApiError("partner.event_unavailable")
        generation = _integer(row["generation"], minimum=0) + 1
        token = secrets.token_hex(32)
        expires = current + timedelta(seconds=EVENT_LEASE_SECONDS)
        connection.execute(
            """
            UPDATE partner_customer_outbox
            SET status='leased',attempt_count=?,generation=?,lease_owner_id=?,lease_token=?,
                lease_expires_at=?,next_attempt_at=NULL,subscription_snapshot_json=?,
                snapshot_frozen_at=?,last_error_code=NULL,delivered_at=NULL,updated_at=?
            WHERE tenant_id=? AND partner_customer_id=? AND status=? AND attempt_count=?
              AND generation=? AND (status='pending' OR unixepoch(lease_expires_at)<=unixepoch(?))
            """,
            (
                attempt,
                generation,
                worker_id,
                token,
                _db_time(expires),
                _canonical_json(snapshot),
                frozen_at,
                now_text,
                row["tenant_id"],
                row["partner_customer_id"],
                old_status,
                old_attempt,
                row["generation"],
                now_text,
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            connection.rollback()
            return None
        claim = PartnerEventClaim(
            tenant_id=_text(row["tenant_id"]),
            partner_id=_text(row["partner_id"]),
            partner_customer_id=_text(row["partner_customer_id"]),
            source_request_id=source_request_id,
            source_trace_id=source_trace_id,
            event=event,
            lease_owner_id=worker_id,
            lease_token=token,
            attempt_count=attempt,
            generation=generation,
            lease_expires_at=expires,
        )
        connection.commit()
        return claim
    except ApiError:
        connection.rollback()
        raise
    except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
        connection.rollback()
        raise ApiError("partner.event_unavailable") from None
    finally:
        connection.close()


def dispatch_claimed_partner_event(
    config: AppConfig,
    claim: PartnerEventClaim,
    *,
    now: datetime | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """每次网络尝试前续租，完成后用完整 CAS 提交成功或固定失败。"""

    def source_guard() -> None:
        if should_stop is not None and should_stop():
            raise ApiError("partner.event_unavailable")
        _renew(config, claim, now=now)

    try:
        if should_stop is not None and should_stop():
            raise ApiError("partner.event_unavailable")
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
    except ValueError:
        return _finish(config, claim, error_code="webhook.payload_invalid", now=now)
    except Exception:
        return _finish(config, claim, error_code="webhook.delivery_unavailable", now=now)


def drain_partner_event_once(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """每轮最多领取和投递一个 EVT-099 source。"""

    if should_stop is not None and should_stop():
        return False
    claim = claim_next_partner_event(config, worker_id=worker_id, now=now)
    if claim is None:
        return False
    dispatch_claimed_partner_event(config, claim, now=now, should_stop=should_stop)
    return True


def recover_partner_customer_source_request_id(
    connection: sqlite3.Connection, envelope: dict[str, object]
) -> str:
    """按 tenant、customer 和 event 回读权威 owner 图，恢复原始 request_id。"""

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
        raise ApiError("webhook.delivery_unavailable")
    row = connection.execute(
        """
        SELECT customer.*,partner.status partner_status,
               outbox.partner_customer_outbox_id,outbox.webhook_event_id,
               outbox.actor_user_id,outbox.source_request_id,outbox.source_trace_id,
               outbox.event_timestamp,outbox.payload_json,
               outbox.payload_sha256 AS outbox_payload_sha256,
               outbox.status outbox_status,outbox.attempt_count,outbox.generation,
               outbox.subscription_snapshot_json,outbox.snapshot_frozen_at
        FROM partner_customer_outbox outbox
        JOIN partner_customers customer
          ON customer.tenant_id=outbox.tenant_id
         AND customer.partner_customer_id=outbox.partner_customer_id
        JOIN partners partner
          ON partner.tenant_id=outbox.tenant_id AND partner.partner_id=outbox.partner_id
        WHERE outbox.tenant_id=? AND outbox.partner_customer_id=? AND outbox.webhook_event_id=?
        """,
        (envelope.get("tenant_id"), envelope.get("object_id"), envelope.get("event_id")),
    ).fetchone()
    if row is None:
        raise ApiError("webhook.delivery_unavailable")
    try:
        event, request_id, _trace_id = _event_from_row(row)
    except (ApiError, TypeError, ValueError):
        raise ApiError("webhook.delivery_unavailable") from None
    if event.to_payload() != envelope:
        raise ApiError("webhook.delivery_unavailable")
    return request_id


def _event_from_row(row: sqlite3.Row) -> tuple[WebhookEvent, str, str]:
    customer_id = row["partner_customer_id"]
    outbox_id = row["partner_customer_outbox_id"]
    if (
        type(customer_id) is not str
        or _CUSTOMER_ID.fullmatch(customer_id) is None
        or type(outbox_id) is not str
        or _OUTBOX_ID.fullmatch(outbox_id) is None
        or outbox_id != _outbox_id(customer_id)
        or row["partner_status"] != "active"
    ):
        raise ApiError("partner.event_unavailable")
    request_id = _text(row["source_request_id"])
    trace_id = _text(row["source_trace_id"])
    event = build_webhook_event(
        tenant_id=_text(row["tenant_id"]),
        event_type="partner.customer_created",
        payload={
            "schema_version": "1.0",
            "partner_id": _text(row["partner_id"]),
            "partner_customer_id": customer_id,
            "customer_code": _text(row["customer_code"]),
            "project_id": _text(row["project_id"]),
            "status": "active",
            "commission_rate_bps": _count(row["commission_rate_bps"]),
        },
        actor_id=_text(row["actor_user_id"]),
        object_id=customer_id,
        trace_id=trace_id,
        source_request_id=request_id,
        event_id=_event_id(customer_id),
        timestamp=_canonical_time(row["event_timestamp"]),
    )
    payload_json = _text(row["payload_json"])
    if (
        row["webhook_event_id"] != event.event_id
        or payload_json != _canonical_json(event.payload)
        or row["outbox_payload_sha256"] != _sha256(payload_json.encode("utf-8"))
    ):
        raise ApiError("partner.event_unavailable")
    return event, request_id, trace_id


def _renew(config: AppConfig, claim: PartnerEventClaim, *, now: datetime | None) -> None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        token = secrets.token_hex(32)
        generation = claim.generation + 1
        expires = max(
            current + timedelta(seconds=EVENT_LEASE_SECONDS),
            claim.lease_expires_at + timedelta(seconds=1),
        )
        connection.execute(
            """
            UPDATE partner_customer_outbox
            SET generation=?,lease_token=?,lease_expires_at=?,updated_at=?
            WHERE tenant_id=? AND partner_customer_id=? AND status='leased'
              AND attempt_count=? AND generation=? AND lease_owner_id=? AND lease_token=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                generation,
                token,
                _db_time(expires),
                _db_time(current),
                claim.tenant_id,
                claim.partner_customer_id,
                claim.attempt_count,
                claim.generation,
                claim.lease_owner_id,
                claim.lease_token,
                _db_time(current),
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise ApiError("partner.event_unavailable")
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
    config: AppConfig, claim: PartnerEventClaim, *, error_code: str | None, now: datetime | None
) -> bool:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        now_text = _db_time(current)
        terminal = error_code is not None and claim.attempt_count >= EVENT_MAX_ATTEMPTS
        status = "sent" if error_code is None else ("failed" if terminal else "pending")
        if terminal:
            from metahub_control.webhooks.service import fail_pending_webhook_event_deliveries

            fail_pending_webhook_event_deliveries(
                connection,
                tenant_id=claim.tenant_id,
                event_id=claim.event.event_id,
                event_type="partner.customer_created",
                updated_at=now_text,
                failure_code=error_code,
            )
        delay = EVENT_BACKOFF_SECONDS[
            min(max(claim.attempt_count - 1, 0), len(EVENT_BACKOFF_SECONDS) - 1)
        ]
        next_attempt = _db_time(current + timedelta(seconds=delay)) if status == "pending" else None
        connection.execute(
            """
            UPDATE partner_customer_outbox
            SET status=?,generation=generation+1,lease_owner_id=NULL,lease_token=NULL,
                lease_expires_at=NULL,next_attempt_at=?,last_error_code=?,delivered_at=?,updated_at=?
            WHERE tenant_id=? AND partner_customer_id=? AND status='leased'
              AND attempt_count=? AND generation=? AND lease_owner_id=? AND lease_token=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                status,
                next_attempt,
                None if status == "sent" else error_code,
                now_text if status == "sent" else None,
                now_text,
                claim.tenant_id,
                claim.partner_customer_id,
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
    except (ApiError, sqlite3.Error, TypeError, ValueError):
        connection.rollback()
        return False
    finally:
        connection.close()


def _delivery_snapshot(connection: sqlite3.Connection, event: WebhookEvent) -> list[str]:
    rows = connection.execute(
        """
        SELECT webhook_delivery_id FROM webhook_deliveries
        WHERE tenant_id=? AND event_id=? AND delivery_kind='original'
        ORDER BY webhook_delivery_id
        """,
        (event.tenant_id, event.event_id),
    ).fetchall()
    values = [str(row[0]) for row in rows]
    if values != sorted(set(values)) or any(
        _DELIVERY_ID.fullmatch(value) is None for value in values
    ):
        raise ApiError("partner.event_unavailable")
    return values


def _parse_snapshot(value: object) -> list[str]:
    if type(value) is not str:
        raise ApiError("partner.event_unavailable")
    decoded = json.loads(value)
    if (
        type(decoded) is not list
        or decoded != sorted(set(decoded))
        or any(type(item) is not str or _DELIVERY_ID.fullmatch(item) is None for item in decoded)
    ):
        raise ApiError("partner.event_unavailable")
    return decoded


def _canonical_time(value: object) -> datetime:
    if type(value) is not str:
        raise ApiError("partner.event_unavailable")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ApiError("partner.event_unavailable") from None
    if parsed.tzinfo is None or _db_time(parsed) != value:
        raise ApiError("partner.event_unavailable")
    return parsed.astimezone(UTC)


def _integer(value: object, *, minimum: int, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ApiError("partner.event_unavailable")
    return value


def _event_id(customer_id: str) -> str:
    return f"evt_pcu_{hashlib.sha256(customer_id.encode('ascii')).hexdigest()[:32]}"


def _outbox_id(customer_id: str) -> str:
    return f"pco_{hashlib.sha256(customer_id.encode('ascii')).hexdigest()[:32]}"


__all__ = [
    "PartnerEventClaim",
    "claim_next_partner_event",
    "dispatch_claimed_partner_event",
    "drain_partner_event_once",
    "recover_partner_customer_source_request_id",
]
