"""PRD-105 notification ownership, template rendering and leased delivery worker."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from metahub_shared.db.connection import connect_database
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.platform_access.service import authenticate_platform_session_in_transaction
from metahub_control.workspace_access.service import authenticate_workspace_session_in_transaction

from .models import (
    NotificationInfo,
    NotificationListResponse,
    NotificationPreferenceInfo,
    NotificationPreferencesPatchRequest,
    NotificationPreferencesResponse,
    NotificationReadResponse,
    NotificationTemplateCreateRequest,
    NotificationTemplateInfo,
    NotificationTemplateListResponse,
    NotificationTemplateResponse,
)

EVENT_CONTRACTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "security.sign_in_risk": ("security", ("event_label", "occurred_at")),
    "subscription.status_changed": ("billing", ("effective_at", "plan_name", "status")),
    "verification.status_changed": ("verification", ("status", "updated_at")),
    "payment.status_changed": ("payments", ("amount", "currency", "status", "updated_at")),
    "invoice.issued": (
        "invoices",
        ("currency", "invoice_number_masked", "issued_at", "total"),
    ),
    "invoice.reversed": ("invoices", ("invoice_number_masked", "reversed_at")),
    "infrastructure.backup_completed": ("operations", ("completed_at", "status")),
    "gateway.health_changed": ("operations", ("cell_label", "observed_at", "status")),
}
CATEGORIES = ("security", "billing", "verification", "payments", "invoices", "operations")
CHANNELS = ("in_app", "email", "webhook", "sms", "web_push")
TEMPLATE_CHANNELS = ("in_app", "email", "webhook")
BACKOFF_SECONDS = (1, 5, 30, 300, 900, 1800, 3600, 7200)
LEASE_SECONDS = 45
MAX_ATTEMPTS = 8
_ID_RE = re.compile(r"^[a-z]{3}_[0-9a-f]{32}$")
_TENANT_ID_RE = re.compile(
    r"^(?:ten_[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12})$"
)
_NOTIFICATION_ID_RE = re.compile(r"^ntf_[0-9a-f]{32}$")
_WORKER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,95}$")
_SOURCE_TYPE_RE = re.compile(r"^[a-z][a-z0-9_.-]{2,63}$")
_SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_TOKEN_RE = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True, slots=True, repr=False)
class NotificationClaim:
    outbox_id: str
    workspace_id: str
    tenant_id: str
    recipient_account_id: str
    event_type: str
    category: str
    source_owner_type: str
    source_owner_id: str
    ciphertext: str = field(repr=False)
    parameter_sha256: str
    snapshot_json: str = field(repr=False)
    attempt_count: int
    generation: int
    owner_id: str
    token: str = field(repr=False)
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True, repr=False)
class PreparedChannel:
    channel: str
    enabled: bool
    mandatory: bool
    template: sqlite3.Row | None
    subject: str | None
    body: str | None
    address: str | None = field(repr=False)
    suppressed: bool = False


class RetryableDeliveryError(Exception):
    def __init__(self, channel: str) -> None:
        self.channel = channel
        super().__init__("notification delivery unavailable")


DeliveryAdapter = Callable[[str, str, str, str], bool]
DELIVERY_ADAPTERS: dict[str, DeliveryAdapter] = {}


def enqueue_notification_in_transaction(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    workspace_id: str,
    tenant_id: str,
    recipient_account_id: str,
    event_type: str,
    source_owner_type: str,
    source_owner_id: str,
    parameters: dict[str, str],
    now: datetime | None = None,
) -> bool:
    """Atomically append one encrypted notification source without any external I/O."""

    category, allowlist = _event_contract(event_type)
    _owner(workspace_id, tenant_id, recipient_account_id, source_owner_type, source_owner_id)
    normalized = _safe_parameters(parameters, allowlist)
    stamp = _db_time(now or _utcnow())
    outbox_id = _id("nob_")
    canonical = _canonical_json(normalized)
    digest = hashlib.sha256(canonical.encode("ascii")).hexdigest()
    snapshot: dict[str, dict[str, object]] = {}
    rows = connection.execute(
        """
        SELECT * FROM notification_templates
        WHERE event_type=? AND locale='en' AND status='published'
        ORDER BY channel,version DESC,notification_template_id DESC
        """,
        (event_type,),
    ).fetchall()
    for row in rows:
        channel = str(row["channel"])
        if channel not in snapshot:
            snapshot[channel] = {
                "notification_template_id": str(row["notification_template_id"]),
                "version": int(row["version"]),
                "content_sha256": str(row["content_sha256"]),
            }
    ciphertext = _encrypt_parameters(
        config,
        canonical,
        outbox_id=outbox_id,
        workspace_id=workspace_id,
        recipient_account_id=recipient_account_id,
    )
    inserted = connection.execute(
        """
        INSERT INTO notification_outbox(
          notification_outbox_id,workspace_id,tenant_id,recipient_account_id,event_type,
          category,source_owner_type,source_owner_id,safe_parameters_ciphertext,
          safe_parameters_sha256,template_snapshot_json,status,attempt_count,
          lease_generation,lease_owner_id,lease_token,lease_expires_at,next_attempt_at,
          safe_error_code,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending',0,0,NULL,NULL,NULL,?,NULL,?,?)
        ON CONFLICT(workspace_id,recipient_account_id,event_type,source_owner_type,source_owner_id)
        DO NOTHING
        """,
        (
            outbox_id,
            workspace_id,
            tenant_id,
            recipient_account_id,
            event_type,
            category,
            source_owner_type,
            source_owner_id,
            ciphertext,
            digest,
            _canonical_json(snapshot),
            stamp,
            stamp,
            stamp,
        ),
    ).rowcount
    if inserted == 1:
        return True
    existing = connection.execute(
        """
        SELECT safe_parameters_sha256,category FROM notification_outbox
        WHERE workspace_id=? AND recipient_account_id=? AND event_type=?
          AND source_owner_type=? AND source_owner_id=?
        """,
        (workspace_id, recipient_account_id, event_type, source_owner_type, source_owner_id),
    ).fetchone()
    if (
        existing is None
        or existing["safe_parameters_sha256"] != digest
        or existing["category"] != category
    ):
        raise ApiError("notification.source_conflict")
    return False


def enqueue_workspace_owners_in_transaction(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    workspace_id: str,
    tenant_id: str,
    event_type: str,
    source_owner_type: str,
    source_owner_id: str,
    parameters: dict[str, str],
    now: datetime | None = None,
) -> int:
    """Append the same source for each active Workspace owner without resolving contact data."""

    rows = connection.execute(
        """SELECT account_id FROM workspace_memberships
           WHERE workspace_id=? AND tenant_id=? AND role_code='owner' AND status='active'
           ORDER BY account_id""",
        (workspace_id, tenant_id),
    ).fetchall()
    inserted = 0
    for row in rows:
        inserted += int(
            enqueue_notification_in_transaction(
                connection,
                config,
                workspace_id=workspace_id,
                tenant_id=tenant_id,
                recipient_account_id=str(row["account_id"]),
                event_type=event_type,
                source_owner_type=source_owner_type,
                source_owner_id=source_owner_id,
                parameters=parameters,
                now=now,
            )
        )
    return inserted


def list_notifications(
    config: AppConfig,
    *,
    session_token: str,
    unread_only: bool,
    limit: int,
    cursor: str | None,
    request_id: str,
    trace_id: str,
) -> NotificationListResponse:
    if cursor is not None and _NOTIFICATION_ID_RE.fullmatch(cursor) is None:
        raise ApiError("notification.request_invalid")
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = authenticate_workspace_session_in_transaction(
            connection, config, session_token=session_token, touch=True
        )
        cursor_values: tuple[object, ...] = ()
        cursor_clause = ""
        if cursor is not None:
            anchor = connection.execute(
                """SELECT created_at,notification_id FROM notifications
                   WHERE notification_id=? AND workspace_id=? AND recipient_account_id=?""",
                (cursor, actor.workspace_id, actor.account_id),
            ).fetchone()
            if anchor is None:
                raise ApiError("notification.request_invalid")
            cursor_clause = "AND (created_at<? OR(created_at=? AND notification_id<?))"
            cursor_values = (anchor["created_at"], anchor["created_at"], anchor["notification_id"])
        unread_clause = "AND read_at IS NULL" if unread_only else ""
        rows = connection.execute(
            f"""
            SELECT * FROM notifications
            WHERE workspace_id=? AND recipient_account_id=? {unread_clause} {cursor_clause}
            ORDER BY created_at DESC,notification_id DESC LIMIT ?
            """,
            (actor.workspace_id, actor.account_id, *cursor_values, limit + 1),
        ).fetchall()
        unread_count = int(
            connection.execute(
                """SELECT COUNT(*) FROM notifications
                   WHERE workspace_id=? AND recipient_account_id=? AND read_at IS NULL""",
                (actor.workspace_id, actor.account_id),
            ).fetchone()[0]
        )
        connection.commit()
        visible = rows[:limit]
        return NotificationListResponse(
            notifications=tuple(_notification_info(row) for row in visible),
            unread_count=unread_count,
            next_cursor=str(visible[-1]["notification_id"]) if len(rows) > limit else None,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, TypeError, ValueError, KeyError):
        _rollback(connection)
        raise ApiError("notification.unavailable") from None
    finally:
        connection.close()


def mark_notification_read(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    notification_id: str,
    request_id: str,
    trace_id: str,
) -> NotificationReadResponse:
    if _NOTIFICATION_ID_RE.fullmatch(notification_id) is None:
        raise ApiError("notification.not_found")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_workspace_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=True,
        )
        row = connection.execute(
            """SELECT * FROM notifications WHERE notification_id=?
               AND workspace_id=? AND recipient_account_id=?""",
            (notification_id, actor.workspace_id, actor.account_id),
        ).fetchone()
        if row is None:
            raise ApiError("notification.not_found")
        if row["read_at"] is None:
            stamp = _db_time(_utcnow())
            connection.execute(
                """UPDATE notifications SET read_at=?
                   WHERE notification_id=? AND read_at IS NULL""",
                (stamp, notification_id),
            )
            row = connection.execute(
                "SELECT * FROM notifications WHERE notification_id=?", (notification_id,)
            ).fetchone()
        connection.commit()
        return NotificationReadResponse(
            notification=_notification_info(row), request_id=request_id, trace_id=trace_id
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, TypeError, ValueError, KeyError):
        _rollback(connection)
        raise ApiError("notification.unavailable") from None
    finally:
        connection.close()


def get_notification_preferences(
    config: AppConfig,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> NotificationPreferencesResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_workspace_session_in_transaction(
            connection, config, session_token=session_token, touch=True
        )
        _ensure_preferences(connection, actor.workspace_id, actor.tenant_id, actor.account_id)
        rows = connection.execute(
            """SELECT * FROM notification_preferences
               WHERE workspace_id=? AND recipient_account_id=? ORDER BY category,channel""",
            (actor.workspace_id, actor.account_id),
        ).fetchall()
        connection.commit()
        return _preferences_response(rows, request_id, trace_id)
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, TypeError, ValueError, KeyError):
        _rollback(connection)
        raise ApiError("notification.unavailable") from None
    finally:
        connection.close()


def update_notification_preferences(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    expected_revision: int,
    payload: NotificationPreferencesPatchRequest,
    request_id: str,
    trace_id: str,
) -> NotificationPreferencesResponse:
    keys = [(change.category, change.channel) for change in payload.changes]
    if len(keys) != len(set(keys)):
        raise ApiError("notification.request_invalid")
    for change in payload.changes:
        if change.category == "security" and not change.enabled:
            raise ApiError("notification.mandatory_required")
        if change.channel in {"sms", "web_push"} and change.enabled:
            raise ApiError("notification.channel_unavailable")
    changes = {(change.category, change.channel): change.enabled for change in payload.changes}
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_workspace_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=True,
        )
        _ensure_preferences(connection, actor.workspace_id, actor.tenant_id, actor.account_id)
        rows = connection.execute(
            """SELECT * FROM notification_preferences
               WHERE workspace_id=? AND recipient_account_id=? ORDER BY category,channel""",
            (actor.workspace_id, actor.account_id),
        ).fetchall()
        revision = _preference_revision(rows)
        if revision != expected_revision:
            raise ApiError("notification.revision_conflict")
        stamp = _db_time(_utcnow())
        for row in rows:
            key = (str(row["category"]), str(row["channel"]))
            enabled = int(bool(changes.get(key, bool(row["enabled"]))))
            connection.execute(
                """UPDATE notification_preferences SET enabled=?,revision=revision+1,updated_at=?
                   WHERE workspace_id=? AND recipient_account_id=? AND category=? AND channel=?
                     AND revision=?""",
                (
                    enabled,
                    stamp,
                    actor.workspace_id,
                    actor.account_id,
                    row["category"],
                    row["channel"],
                    revision,
                ),
            )
        updated = connection.execute(
            """SELECT * FROM notification_preferences
               WHERE workspace_id=? AND recipient_account_id=? ORDER BY category,channel""",
            (actor.workspace_id, actor.account_id),
        ).fetchall()
        connection.commit()
        return _preferences_response(updated, request_id, trace_id)
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, TypeError, ValueError, KeyError):
        _rollback(connection)
        raise ApiError("notification.unavailable") from None
    finally:
        connection.close()


def list_notification_templates(
    config: AppConfig,
    *,
    platform_session_token: str,
    request_id: str,
    trace_id: str,
) -> NotificationTemplateListResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = authenticate_platform_session_in_transaction(
            connection, config, session_token=platform_session_token, touch=True
        )
        if "platform.notification_template.read" not in actor.permissions:
            raise ApiError("platform.permission_denied")
        rows = connection.execute(
            """SELECT * FROM notification_templates
               ORDER BY template_code,channel,locale,version DESC"""
        ).fetchall()
        revision = int(
            connection.execute(
                """SELECT catalog_revision FROM notification_template_catalog_state
                   WHERE singleton_id=1"""
            ).fetchone()[0]
        )
        connection.commit()
        return NotificationTemplateListResponse(
            templates=tuple(_template_info(row) for row in rows),
            catalog_revision=revision,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, TypeError, ValueError, KeyError):
        _rollback(connection)
        raise ApiError("notification.unavailable") from None
    finally:
        connection.close()


def create_notification_template(
    config: AppConfig,
    *,
    platform_session_token: str,
    csrf_token: str,
    expected_revision: int,
    idempotency_key: str,
    payload: NotificationTemplateCreateRequest,
    request_id: str,
    trace_id: str,
) -> NotificationTemplateResponse:
    if _IDEMPOTENCY_RE.fullmatch(idempotency_key) is None:
        raise ApiError("notification.request_invalid")
    category, allowlist = _event_contract(payload.event_type)
    if tuple(payload.parameter_allowlist) != allowlist:
        raise ApiError("notification.template_invalid")
    _validate_template(payload.subject_template, payload.body_template, allowlist)
    request_value = payload.model_dump(mode="json")
    request_sha = hashlib.sha256(_canonical_json(request_value).encode("ascii")).hexdigest()
    idem_sha = _domain_hmac(config.platform_session_secret, "template-idempotency", idempotency_key)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_platform_session_in_transaction(
            connection,
            config,
            session_token=platform_session_token,
            csrf_token=csrf_token,
            touch=True,
        )
        if "platform.notification_template.write" not in actor.permissions:
            raise ApiError("platform.permission_denied")
        existing = connection.execute(
            """SELECT * FROM notification_templates
               WHERE created_by_operator_id=? AND idempotency_sha256=?""",
            (actor.operator_id, idem_sha),
        ).fetchone()
        state = connection.execute(
            "SELECT * FROM notification_template_catalog_state WHERE singleton_id=1"
        ).fetchone()
        if existing is not None:
            if existing["request_sha256"] != request_sha:
                raise ApiError("notification.idempotency_conflict")
            connection.commit()
            return NotificationTemplateResponse(
                template=_template_info(existing),
                catalog_revision=int(state["catalog_revision"]),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
        if int(state["catalog_revision"]) != expected_revision:
            raise ApiError("notification.revision_conflict")
        prior = connection.execute(
            """SELECT event_type,category,MAX(version) version FROM notification_templates
               WHERE template_code=? AND channel=? AND locale=?""",
            (payload.template_code, payload.channel, payload.locale),
        ).fetchone()
        if prior["version"] is not None and (
            prior["event_type"] != payload.event_type or prior["category"] != category
        ):
            raise ApiError("notification.template_invalid")
        version = int(prior["version"] or 0) + 1
        now = _utcnow()
        stamp = _db_time(now)
        allow_json = _canonical_json(list(allowlist))
        content = {
            "event_type": payload.event_type,
            "channel": payload.channel,
            "locale": payload.locale,
            "version": version,
            "category": category,
            "subject": payload.subject_template,
            "body": payload.body_template,
            "parameter_allowlist": list(allowlist),
        }
        template_id = _id("ntp_")
        connection.execute(
            """
            INSERT INTO notification_templates(
              notification_template_id,template_code,event_type,category,channel,locale,version,
              parameter_allowlist_json,parameter_allowlist_sha256,subject_template,body_template,
              content_sha256,status,revision,created_by_operator_id,created_at,published_at,
              idempotency_sha256,request_sha256)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?)
            """,
            (
                template_id,
                payload.template_code,
                payload.event_type,
                category,
                payload.channel,
                payload.locale,
                version,
                allow_json,
                hashlib.sha256(allow_json.encode("ascii")).hexdigest(),
                payload.subject_template,
                payload.body_template,
                hashlib.sha256(_canonical_json(content).encode("ascii")).hexdigest(),
                payload.status,
                actor.operator_id,
                stamp,
                stamp if payload.status == "published" else None,
                idem_sha,
                request_sha,
            ),
        )
        next_revision = expected_revision + 1
        connection.execute(
            """UPDATE notification_template_catalog_state
               SET catalog_revision=?,updated_at=? WHERE singleton_id=1 AND catalog_revision=?""",
            (next_revision, stamp, expected_revision),
        )
        _template_audit(
            connection,
            actor.operator_id,
            template_id,
            payload.status,
            request_id,
            trace_id,
            request_sha,
            stamp,
        )
        row = connection.execute(
            "SELECT * FROM notification_templates WHERE notification_template_id=?", (template_id,)
        ).fetchone()
        connection.commit()
        return NotificationTemplateResponse(
            template=_template_info(row),
            catalog_revision=next_revision,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, TypeError, ValueError, KeyError):
        _rollback(connection)
        raise ApiError("notification.unavailable") from None
    finally:
        connection.close()


def claim_next_notification(
    config: AppConfig, *, worker_id: str, now: datetime | None = None
) -> NotificationClaim | None:
    if _WORKER_RE.fullmatch(worker_id) is None:
        raise ApiError("notification.unavailable")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = now or _utcnow()
        stamp = _db_time(current)
        row = connection.execute(
            """
            WITH queued AS(
              SELECT outbox.*,
                ROW_NUMBER() OVER(PARTITION BY workspace_id
                  ORDER BY created_at,notification_outbox_id) workspace_rank
              FROM notification_outbox outbox
              WHERE status IN('pending','processing')
            ),eligible AS(
              SELECT * FROM queued
              WHERE (status='pending' AND unixepoch(next_attempt_at)<=unixepoch(?))
                 OR(status='processing' AND unixepoch(lease_expires_at)<=unixepoch(?))
            )
            SELECT * FROM eligible ORDER BY workspace_rank,attempt_count,
              COALESCE(next_attempt_at,lease_expires_at),created_at,notification_outbox_id LIMIT 1
            """,
            (stamp, stamp),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        attempt = int(row["attempt_count"]) + 1
        generation = int(row["lease_generation"]) + 1
        token = secrets.token_hex(32)
        expires = current + timedelta(seconds=LEASE_SECONDS)
        updated = connection.execute(
            """
            UPDATE notification_outbox SET status='processing',attempt_count=?,lease_generation=?,
              lease_owner_id=?,lease_token=?,lease_expires_at=?,next_attempt_at=NULL,
              safe_error_code=NULL,updated_at=?
            WHERE notification_outbox_id=? AND status=? AND attempt_count=? AND lease_generation=?
              AND (status='pending' OR unixepoch(lease_expires_at)<=unixepoch(?))
            """,
            (
                attempt,
                generation,
                worker_id,
                token,
                _db_time(expires),
                stamp,
                row["notification_outbox_id"],
                row["status"],
                row["attempt_count"],
                row["lease_generation"],
                stamp,
            ),
        ).rowcount
        if updated != 1:
            connection.rollback()
            return None
        connection.commit()
        return NotificationClaim(
            outbox_id=str(row["notification_outbox_id"]),
            workspace_id=str(row["workspace_id"]),
            tenant_id=str(row["tenant_id"]),
            recipient_account_id=str(row["recipient_account_id"]),
            event_type=str(row["event_type"]),
            category=str(row["category"]),
            source_owner_type=str(row["source_owner_type"]),
            source_owner_id=str(row["source_owner_id"]),
            ciphertext=str(row["safe_parameters_ciphertext"]),
            parameter_sha256=str(row["safe_parameters_sha256"]),
            snapshot_json=str(row["template_snapshot_json"]),
            attempt_count=attempt,
            generation=generation,
            owner_id=worker_id,
            token=token,
            lease_expires_at=expires,
        )
    except (sqlite3.Error, TypeError, ValueError, KeyError):
        _rollback(connection)
        raise ApiError("notification.unavailable") from None
    finally:
        connection.close()


def process_notification_once(
    config: AppConfig, *, worker_id: str, now: datetime | None = None
) -> bool:
    claim = claim_next_notification(config, worker_id=worker_id, now=now)
    if claim is None:
        return False
    try:
        _parameters, prepared = _prepare_delivery(config, claim)
        results: dict[str, tuple[str, str | None]] = {}
        for item in prepared:
            if not item.enabled:
                results[item.channel] = ("suppressed", "notification.channel_disabled")
            elif item.suppressed:
                results[item.channel] = ("suppressed", "notification.suppressed")
            elif item.channel in {"sms", "web_push"}:
                results[item.channel] = ("unavailable", "notification.channel_unavailable")
            elif item.template is None or item.subject is None or item.body is None:
                results[item.channel] = ("unavailable", "notification.channel_unavailable")
            elif item.channel == "in_app":
                results[item.channel] = ("sent", None)
            elif item.address is None:
                results[item.channel] = ("unavailable", "notification.recipient_unavailable")
            else:
                adapter = DELIVERY_ADAPTERS.get(item.channel)
                if adapter is None:
                    results[item.channel] = ("unavailable", "notification.channel_unavailable")
                else:
                    try:
                        sent = adapter(
                            item.address,
                            item.subject,
                            item.body,
                            f"{claim.outbox_id}:{item.channel}:{claim.attempt_count}",
                        )
                    except TimeoutError as exc:
                        raise RetryableDeliveryError(item.channel) from exc
                    results[item.channel] = (
                        ("sent", None)
                        if sent
                        else ("failed", "notification.delivery_unavailable")
                    )
        return _finish_delivery(config, claim, prepared, results, now=now)
    except RetryableDeliveryError as exc:
        return _retry_claim(config, claim, channel=exc.channel, now=now)
    except (ApiError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return _quarantine_claim(config, claim, now=now)


def _prepare_delivery(
    config: AppConfig, claim: NotificationClaim
) -> tuple[dict[str, str], tuple[PreparedChannel, ...]]:
    canonical = _decrypt_parameters(config, claim)
    if hashlib.sha256(canonical.encode("ascii")).hexdigest() != claim.parameter_sha256:
        raise ApiError("notification.template_invalid")
    decoded = json.loads(canonical, object_pairs_hook=_unique_object)
    _category, allowlist = _event_contract(claim.event_type)
    parameters = _safe_parameters(decoded, allowlist)
    snapshot = json.loads(claim.snapshot_json, object_pairs_hook=_unique_object)
    if type(snapshot) is not dict or any(channel not in TEMPLATE_CHANNELS for channel in snapshot):
        raise ApiError("notification.template_invalid")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _ensure_preferences(
            connection, claim.workspace_id, claim.tenant_id, claim.recipient_account_id
        )
        preference_rows = connection.execute(
            """SELECT * FROM notification_preferences
               WHERE workspace_id=? AND recipient_account_id=?""",
            (claim.workspace_id, claim.recipient_account_id),
        ).fetchall()
        preferences = {
            (str(row["category"]), str(row["channel"])): row for row in preference_rows
        }
        templates: dict[str, sqlite3.Row] = {}
        for channel, value in snapshot.items():
            if type(value) is not dict or set(value) != {
                "notification_template_id",
                "version",
                "content_sha256",
            }:
                raise ApiError("notification.template_invalid")
            row = connection.execute(
                """SELECT * FROM notification_templates
                   WHERE notification_template_id=? AND event_type=? AND channel=?
                     AND version=? AND status='published'""",
                (
                    value["notification_template_id"],
                    claim.event_type,
                    channel,
                    value["version"],
                ),
            ).fetchone()
            if row is None or row["content_sha256"] != value["content_sha256"]:
                raise ApiError("notification.template_invalid")
            _verify_template_row(row, allowlist)
            templates[channel] = row
        if "in_app" not in templates:
            raise ApiError("notification.template_invalid")
        email_row = connection.execute(
            """SELECT email_normalized FROM global_accounts
               WHERE account_id=? AND status='active' AND login_enabled=1""",
            (claim.recipient_account_id,),
        ).fetchone()
        webhook_row = connection.execute(
            """SELECT endpoint_url FROM webhook_endpoints
               WHERE tenant_id=? AND status='active'
               ORDER BY created_at,webhook_endpoint_id LIMIT 1""",
            (claim.tenant_id,),
        ).fetchone()
        addresses = {
            "email": str(email_row["email_normalized"]) if email_row is not None else None,
            "webhook": str(webhook_row["endpoint_url"]) if webhook_row is not None else None,
        }
        prepared: list[PreparedChannel] = []
        for channel in CHANNELS:
            preference = preferences[(claim.category, channel)]
            mandatory = bool(preference["mandatory_security"])
            enabled = bool(preference["enabled"])
            template = templates.get(channel)
            subject = _render(str(template["subject_template"]), parameters) if template else None
            body = _render(str(template["body_template"]), parameters) if template else None
            address = addresses.get(channel)
            suppressed = bool(
                address
                and channel in {"email", "webhook"}
                and _is_suppressed(connection, config, channel, address)
            )
            prepared.append(
                PreparedChannel(
                    channel=channel,
                    enabled=enabled,
                    mandatory=mandatory,
                    template=template,
                    subject=subject,
                    body=body,
                    address=address,
                    suppressed=suppressed,
                )
            )
        connection.commit()
        return parameters, tuple(prepared)
    except Exception:
        _rollback(connection)
        raise
    finally:
        connection.close()


def _finish_delivery(
    config: AppConfig,
    claim: NotificationClaim,
    prepared: tuple[PreparedChannel, ...],
    results: dict[str, tuple[str, str | None]],
    *,
    now: datetime | None,
) -> bool:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        stamp = _db_time(now or _utcnow())
        if not _lease_owned(connection, claim, stamp):
            connection.rollback()
            return False
        in_app = next(item for item in prepared if item.channel == "in_app")
        notification_id: str | None = None
        if results["in_app"][0] == "sent" and in_app.template is not None:
            notification_id = _id("ntf_")
            connection.execute(
                """
                INSERT INTO notifications(
                  notification_id,workspace_id,tenant_id,recipient_account_id,event_type,category,
                  source_owner_type,source_owner_id,notification_template_id,template_version,
                  channel,subject_text,body_text,safe_parameters_ciphertext,
                  safe_parameters_sha256,read_at,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,'in_app',?,?,?,?,NULL,?)
                ON CONFLICT(
                  workspace_id,recipient_account_id,event_type,source_owner_type,source_owner_id
                )
                DO NOTHING
                """,
                (
                    notification_id,
                    claim.workspace_id,
                    claim.tenant_id,
                    claim.recipient_account_id,
                    claim.event_type,
                    claim.category,
                    claim.source_owner_type,
                    claim.source_owner_id,
                    in_app.template["notification_template_id"],
                    in_app.template["version"],
                    in_app.subject,
                    in_app.body,
                    claim.ciphertext,
                    claim.parameter_sha256,
                    stamp,
                ),
            )
            row = connection.execute(
                """SELECT notification_id FROM notifications
                   WHERE workspace_id=? AND recipient_account_id=? AND event_type=?
                     AND source_owner_type=? AND source_owner_id=?""",
                (
                    claim.workspace_id,
                    claim.recipient_account_id,
                    claim.event_type,
                    claim.source_owner_type,
                    claim.source_owner_id,
                ),
            ).fetchone()
            notification_id = str(row["notification_id"])
        for item in prepared:
            status, error_code = results[item.channel]
            provider = {
                "in_app": "internal",
                "email": "email_adapter" if item.channel in DELIVERY_ADAPTERS else "unconfigured",
                "webhook": (
                    "webhook_adapter" if item.channel in DELIVERY_ADAPTERS else "unconfigured"
                ),
                "sms": "unconfigured",
                "web_push": "unconfigured",
            }[item.channel]
            connection.execute(
                """
                INSERT INTO delivery_attempts(
                  delivery_attempt_id,notification_outbox_id,notification_id,channel,provider_code,
                  attempt_number,lease_generation,preference_enabled,mandatory_security,status,
                  safe_error_code,attempted_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    _id("nda_"),
                    claim.outbox_id,
                    notification_id if item.channel == "in_app" else None,
                    item.channel,
                    provider,
                    claim.attempt_count,
                    claim.generation,
                    int(item.enabled),
                    int(item.mandatory),
                    status,
                    error_code,
                    stamp,
                ),
            )
        final_status = "succeeded" if results["in_app"][0] == "sent" else "suppressed"
        updated = connection.execute(
            """UPDATE notification_outbox SET status=?,lease_generation=lease_generation+1,
              lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,next_attempt_at=NULL,
              safe_error_code=?,updated_at=?
              WHERE notification_outbox_id=? AND status='processing' AND attempt_count=?
                AND lease_generation=? AND lease_owner_id=? AND lease_token=?
                AND unixepoch(lease_expires_at)>unixepoch(?)""",
            (
                final_status,
                "notification.suppressed" if final_status == "suppressed" else None,
                stamp,
                claim.outbox_id,
                claim.attempt_count,
                claim.generation,
                claim.owner_id,
                claim.token,
                stamp,
            ),
        ).rowcount
        if updated != 1:
            connection.rollback()
            return False
        connection.commit()
        return True
    except (sqlite3.Error, TypeError, ValueError, KeyError):
        _rollback(connection)
        return False
    finally:
        connection.close()


def _retry_claim(
    config: AppConfig, claim: NotificationClaim, *, channel: str, now: datetime | None
) -> bool:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = now or _utcnow()
        stamp = _db_time(current)
        terminal = claim.attempt_count >= MAX_ATTEMPTS
        next_at = None if terminal else _db_time(
            current + timedelta(seconds=BACKOFF_SECONDS[min(claim.attempt_count - 1, 7)])
        )
        connection.execute(
            """INSERT INTO delivery_attempts(
              delivery_attempt_id,notification_outbox_id,notification_id,channel,provider_code,
              attempt_number,lease_generation,preference_enabled,mandatory_security,status,
              safe_error_code,attempted_at)
              VALUES(?,?,NULL,?,?,?,?,1,?,'failed','notification.delivery_unavailable',?)""",
            (
                _id("nda_"),
                claim.outbox_id,
                channel,
                "email_adapter" if channel == "email" else "webhook_adapter",
                claim.attempt_count,
                claim.generation,
                int(claim.category == "security"),
                stamp,
            ),
        )
        updated = connection.execute(
            """UPDATE notification_outbox SET status=?,lease_generation=lease_generation+1,
              lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,next_attempt_at=?,
              safe_error_code='notification.delivery_unavailable',updated_at=?
              WHERE notification_outbox_id=? AND status='processing' AND attempt_count=?
                AND lease_generation=? AND lease_owner_id=? AND lease_token=?
                AND unixepoch(lease_expires_at)>unixepoch(?)""",
            (
                "failed" if terminal else "pending",
                next_at,
                stamp,
                claim.outbox_id,
                claim.attempt_count,
                claim.generation,
                claim.owner_id,
                claim.token,
                stamp,
            ),
        ).rowcount
        if updated != 1:
            connection.rollback()
            return False
        connection.commit()
        return True
    except (sqlite3.Error, TypeError, ValueError):
        _rollback(connection)
        return False
    finally:
        connection.close()


def _quarantine_claim(
    config: AppConfig, claim: NotificationClaim, *, now: datetime | None
) -> bool:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        stamp = _db_time(now or _utcnow())
        updated = connection.execute(
            """UPDATE notification_outbox SET status='failed',lease_generation=lease_generation+1,
              lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,next_attempt_at=NULL,
              safe_error_code='notification.template_invalid',updated_at=?
              WHERE notification_outbox_id=? AND status='processing' AND attempt_count=?
                AND lease_generation=? AND lease_owner_id=? AND lease_token=?
                AND unixepoch(lease_expires_at)>unixepoch(?)""",
            (
                stamp,
                claim.outbox_id,
                claim.attempt_count,
                claim.generation,
                claim.owner_id,
                claim.token,
                stamp,
            ),
        ).rowcount
        if updated != 1:
            connection.rollback()
            return False
        connection.commit()
        return True
    except sqlite3.Error:
        _rollback(connection)
        return False
    finally:
        connection.close()


def _ensure_preferences(
    connection: sqlite3.Connection, workspace_id: str, tenant_id: str, account_id: str
) -> None:
    stamp = _db_time(_utcnow())
    for category in CATEGORIES:
        mandatory = int(category == "security")
        for channel in CHANNELS:
            enabled = int(channel not in {"sms", "web_push"})
            connection.execute(
                """INSERT INTO notification_preferences(
                  workspace_id,tenant_id,recipient_account_id,category,channel,enabled,
                  mandatory_security,revision,created_at,updated_at)
                  VALUES(?,?,?,?,?,?,?,1,?,?) ON CONFLICT DO NOTHING""",
                (
                    workspace_id,
                    tenant_id,
                    account_id,
                    category,
                    channel,
                    enabled,
                    mandatory,
                    stamp,
                    stamp,
                ),
            )


def _preferences_response(
    rows: list[sqlite3.Row],
    request_id: str,
    trace_id: str,
) -> NotificationPreferencesResponse:
    available = {
        "in_app": True,
        "email": "email" in DELIVERY_ADAPTERS,
        "webhook": "webhook" in DELIVERY_ADAPTERS,
        "sms": False,
        "web_push": False,
    }
    return NotificationPreferencesResponse(
        preferences=tuple(
            NotificationPreferenceInfo(
                category=row["category"],
                channel=row["channel"],
                enabled=bool(row["enabled"]),
                mandatory=bool(row["mandatory_security"]),
                available=available[str(row["channel"])],
            )
            for row in rows
        ),
        revision=_preference_revision(rows),
        request_id=request_id,
        trace_id=trace_id,
    )


def _preference_revision(rows: list[sqlite3.Row]) -> int:
    revisions = {int(row["revision"]) for row in rows}
    if len(rows) != len(CATEGORIES) * len(CHANNELS) or len(revisions) != 1:
        raise ApiError("notification.unavailable")
    return revisions.pop()


def _notification_info(row: sqlite3.Row) -> NotificationInfo:
    return NotificationInfo(
        notification_id=row["notification_id"],
        event_type=row["event_type"],
        category=row["category"],
        subject=row["subject_text"],
        body=row["body_text"],
        read=row["read_at"] is not None,
        created_at=_parse_time(row["created_at"]),
    )


def _template_info(row: sqlite3.Row) -> NotificationTemplateInfo:
    allowlist = json.loads(row["parameter_allowlist_json"])
    if type(allowlist) is not list or any(type(value) is not str for value in allowlist):
        raise ApiError("notification.template_invalid")
    return NotificationTemplateInfo(
        notification_template_id=row["notification_template_id"],
        template_code=row["template_code"],
        event_type=row["event_type"],
        category=row["category"],
        channel=row["channel"],
        locale=row["locale"],
        version=int(row["version"]),
        parameter_allowlist=tuple(allowlist),
        subject_template=row["subject_template"],
        body_template=row["body_template"],
        content_sha256=row["content_sha256"],
        status=row["status"],
        revision=int(row["revision"]),
        created_at=_parse_time(row["created_at"]),
        published_at=_parse_time(row["published_at"]) if row["published_at"] else None,
    )


def _verify_template_row(row: sqlite3.Row, allowlist: tuple[str, ...]) -> None:
    stored = json.loads(row["parameter_allowlist_json"])
    allow_json = _canonical_json(list(allowlist))
    if (
        stored != list(allowlist)
        or row["parameter_allowlist_sha256"]
        != hashlib.sha256(allow_json.encode("ascii")).hexdigest()
    ):
        raise ApiError("notification.template_invalid")
    _validate_template(str(row["subject_template"]), str(row["body_template"]), allowlist)
    content = {
        "event_type": row["event_type"],
        "channel": row["channel"],
        "locale": row["locale"],
        "version": int(row["version"]),
        "category": row["category"],
        "subject": row["subject_template"],
        "body": row["body_template"],
        "parameter_allowlist": list(allowlist),
    }
    if row["content_sha256"] != hashlib.sha256(
        _canonical_json(content).encode("ascii")
    ).hexdigest():
        raise ApiError("notification.template_invalid")


def _validate_template(subject: str, body: str, allowlist: tuple[str, ...]) -> None:
    combined = f"{subject}\n{body}"
    if (
        "<" in combined
        or ">" in combined
        or "{%" in combined
        or "{#" in combined
        or _CONTROL_RE.search(combined)
    ):
        raise ApiError("notification.template_invalid")
    tokens = _TOKEN_RE.findall(combined)
    residue = _TOKEN_RE.sub("", combined)
    if "{" in residue or "}" in residue or set(tokens) != set(allowlist):
        raise ApiError("notification.template_invalid")


def _render(template: str, parameters: dict[str, str]) -> str:
    try:
        rendered = _TOKEN_RE.sub(lambda match: parameters[match.group(1)], template)
    except KeyError as exc:
        raise ApiError("notification.template_invalid") from exc
    if "{" in rendered or "}" in rendered or "<" in rendered or ">" in rendered:
        raise ApiError("notification.template_invalid")
    return rendered


def _safe_parameters(value: object, allowlist: tuple[str, ...]) -> dict[str, str]:
    if type(value) is not dict or tuple(sorted(value)) != allowlist:
        raise ApiError("notification.template_invalid")
    normalized: dict[str, str] = {}
    for key in allowlist:
        item = value[key]
        if (
            type(item) is not str
            or not item
            or len(item.encode("utf-8")) > 256
            or "<" in item
            or ">" in item
            or "{" in item
            or "}" in item
            or _CONTROL_RE.search(item)
        ):
            raise ApiError("notification.template_invalid")
        normalized[key] = item
    return normalized


def _event_contract(event_type: str) -> tuple[str, tuple[str, ...]]:
    contract = EVENT_CONTRACTS.get(event_type)
    if contract is None:
        raise ApiError("notification.template_invalid")
    return contract


def _owner(
    workspace_id: str,
    tenant_id: str,
    account_id: str,
    source_owner_type: str,
    source_owner_id: str,
) -> None:
    if (
        _ID_RE.fullmatch(workspace_id) is None
        or _TENANT_ID_RE.fullmatch(tenant_id) is None
        or _ID_RE.fullmatch(account_id) is None
        or _SOURCE_TYPE_RE.fullmatch(source_owner_type) is None
        or _SOURCE_ID_RE.fullmatch(source_owner_id) is None
    ):
        raise ApiError("notification.template_invalid")


def _encrypt_parameters(
    config: AppConfig,
    plaintext: str,
    *,
    outbox_id: str,
    workspace_id: str,
    recipient_account_id: str,
) -> str:
    nonce = secrets.token_bytes(12)
    owner = _parameter_owner(outbox_id, workspace_id, recipient_account_id)
    encrypted = AESGCM(_notification_key(config)).encrypt(
        nonce, plaintext.encode("ascii"), owner
    )
    return "n1." + base64.urlsafe_b64encode(nonce + encrypted).decode("ascii").rstrip("=")


def _decrypt_parameters(config: AppConfig, claim: NotificationClaim) -> str:
    if not claim.ciphertext.startswith("n1."):
        raise ApiError("notification.template_invalid")
    encoded = claim.ciphertext[3:]
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        plaintext = AESGCM(_notification_key(config)).decrypt(
            raw[:12],
            raw[12:],
            _parameter_owner(claim.outbox_id, claim.workspace_id, claim.recipient_account_id),
        )
        return plaintext.decode("ascii")
    except Exception as exc:
        raise ApiError("notification.template_invalid") from exc


def _parameter_owner(outbox_id: str, workspace_id: str, account_id: str) -> bytes:
    return f"metahub/notification/parameters/v1\0{outbox_id}\0{workspace_id}\0{account_id}".encode(
        "ascii"
    )


def _notification_key(config: AppConfig) -> bytes:
    secret = config.identity_pii_encryption_secret
    if type(secret) is not str or not secret:
        raise ApiError("notification.unavailable")
    return hmac.new(
        secret.encode("utf-8"), b"metahub/notification/aes-gcm/v1", hashlib.sha256
    ).digest()


def _is_suppressed(
    connection: sqlite3.Connection, config: AppConfig, channel: str, address: str
) -> bool:
    blind = _domain_hmac(config.identity_pii_encryption_secret, f"suppression/{channel}", address)
    row = connection.execute(
        """SELECT 1 FROM channel_suppressions WHERE recipient_blind_index=? AND channel=?
           AND (expires_at IS NULL OR unixepoch(expires_at)>unixepoch('now')) LIMIT 1""",
        (blind, channel),
    ).fetchone()
    return row is not None


def _lease_owned(connection: sqlite3.Connection, claim: NotificationClaim, stamp: str) -> bool:
    return (
        connection.execute(
            """SELECT 1 FROM notification_outbox WHERE notification_outbox_id=?
               AND status='processing' AND attempt_count=? AND lease_generation=?
               AND lease_owner_id=? AND lease_token=?
               AND unixepoch(lease_expires_at)>unixepoch(?)""",
            (
                claim.outbox_id,
                claim.attempt_count,
                claim.generation,
                claim.owner_id,
                claim.token,
                stamp,
            ),
        ).fetchone()
        is not None
    )


def _template_audit(
    connection: sqlite3.Connection,
    operator_id: str,
    template_id: str,
    status: str,
    request_id: str,
    trace_id: str,
    digest: str,
    stamp: str,
) -> None:
    connection.execute(
        """INSERT INTO platform_audit_logs(
          audit_id,actor_domain,actor_id,action_code,object_type,object_id,reason_code,
          request_id,trace_id,before_sha256,after_sha256,occurred_at)
          VALUES(?,'platform',?,'notification_template.created','notification_template',?,?,
            ?,?,NULL,?,?)""",
        (
            _id("pau_"),
            operator_id,
            template_id,
            "published" if status == "published" else "draft_created",
            request_id,
            trace_id,
            digest,
            stamp,
        ),
    )


def _domain_hmac(secret: str, domain: str, value: str) -> str:
    key = hmac.new(
        secret.encode("utf-8"), f"metahub/notification/{domain}/v1".encode("ascii"), hashlib.sha256
    ).digest()
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate json key")
        result[key] = value
    return result


def _id(prefix: str) -> str:
    return f"{prefix}{uuid4().hex}"


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _db_time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timezone required")
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_time(value: object) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise ValueError("invalid time")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if _db_time(parsed) != value:
        raise ValueError("invalid time")
    return parsed


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except Exception as exc:
        raise ApiError("notification.unavailable") from exc


def _rollback(connection: sqlite3.Connection) -> None:
    try:
        if connection.in_transaction:
            connection.rollback()
    except sqlite3.Error:
        pass


__all__ = [
    "DELIVERY_ADAPTERS",
    "EVENT_CONTRACTS",
    "claim_next_notification",
    "create_notification_template",
    "enqueue_notification_in_transaction",
    "enqueue_workspace_owners_in_transaction",
    "get_notification_preferences",
    "list_notification_templates",
    "list_notifications",
    "mark_notification_read",
    "process_notification_once",
    "update_notification_preferences",
]
