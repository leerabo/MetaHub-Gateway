"""Durable ``mcp.tool_called`` source outbox.

The invocation worker creates the immutable event, Webhook delivery intents, and source outbox in
the same SQLite transaction as the terminal invocation.  Recovery workers only handle the frozen
delivery set and use tenant/event/attempt/generation/owner/token/expiry CAS for every mutation.
The stable event ID gives at-least-once delivery; it does not claim network exactly-once.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.mcp.contracts import McpContractError
from metahub_shared.mcp.events import (
    MCP_TOOL_CALLED_EVENT_TYPE,
    validate_mcp_tool_called_event,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import WebhookEvent, build_webhook_event

EVENT_MAX_ATTEMPTS = 10
EVENT_BACKOFF_SECONDS = (5, 30, 300)
DEFAULT_EVENT_LEASE_SECONDS = 90

_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$", re.ASCII)
_EVENT_ID = re.compile(r"^mcpevt_[0-9a-f]{32}$")
_OUTBOX_ID = re.compile(r"^mcpob_[0-9a-f]{32}$")
_INVOCATION_ID = re.compile(r"^mcpi_[0-9a-f]{32}$")
_SERVER_ID = re.compile(r"^mcps_[0-9a-f]{32}$")
_TOOL_ID = re.compile(r"^mcpt_[0-9a-f]{32}$")
_DELIVERY_ID = re.compile(r"^whd_[0-9a-f]{32}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$", re.ASCII)


class _StopBeforeDispatch(Exception):
    """A cooperative stop observed before this source starts network I/O."""


@dataclass(slots=True)
class McpInvocationEventClaim:
    """One validated source event and its mutable private lease coordinates."""

    outbox_id: str
    event_id: str
    tenant_id: str
    invocation_id: str
    source_request_id: str
    source_trace_id: str
    event: WebhookEvent = field(repr=False)
    attempt_count: int
    lease_generation: int
    lease_owner_id: str
    lease_token: str = field(repr=False)
    lease_expires_at: datetime
    snapshot_json: str | None = field(default=None, repr=False)
    snapshot_frozen_at: str | None = None


def create_mcp_invocation_event_in_transaction(
    connection: sqlite3.Connection,
    invocation_row: sqlite3.Row | Mapping[str, object],
) -> tuple[str, str] | None:
    """Create the exact source, frozen deliveries, and outbox without committing.

    The caller must own the transaction that made the invocation terminal.  ``not_called``
    outcomes return ``None`` and must never manufacture ``mcp.tool_called``.  Re-entry for an
    already committed source is idempotent only after the event, invocation, and snapshot all
    pass an exact owner-graph comparison.
    """

    if not connection.in_transaction:
        raise ApiError("mcp.unavailable")
    try:
        tenant_id = _safe_text(_row_value(invocation_row, "tenant_id"), maximum=160)
        invocation_id = _matched(
            _row_value(invocation_row, "mcp_invocation_id"),
            _INVOCATION_ID,
        )
        invocation = _load_invocation(connection, tenant_id, invocation_id)
        existing = connection.execute(
            """
            SELECT * FROM mcp_invocation_events
            WHERE tenant_id=? AND mcp_invocation_id=?
            """,
            (tenant_id, invocation_id),
        ).fetchone()
        execution_status = _safe_text(invocation["execution_status"], maximum=32)
        if execution_status == "not_called":
            if existing is not None:
                raise ApiError("mcp.unavailable")
            return None
        if execution_status not in {"called", "unknown"}:
            raise ApiError("mcp.unavailable")

        if existing is not None:
            event = _event_from_source_row(connection, existing)
            outbox = connection.execute(
                """
                SELECT * FROM mcp_invocation_event_outbox
                WHERE tenant_id=? AND mcp_invocation_event_id=?
                """,
                (tenant_id, event.event_id),
            ).fetchone()
            if outbox is None or outbox["status"] not in {
                "pending",
                "processing",
                "succeeded",
                "failed",
            }:
                raise ApiError("mcp.unavailable")
            snapshot_json, _ = _verify_existing_snapshot(
                connection,
                event,
                snapshot_json=outbox["subscription_snapshot_json"],
                frozen_at=outbox["snapshot_frozen_at"],
            )
            if snapshot_json is None:
                raise ApiError("mcp.unavailable")
            return (
                _matched(existing["mcp_invocation_event_id"], _EVENT_ID),
                _matched(outbox["mcp_invocation_event_outbox_id"], _OUTBOX_ID),
            )

        event_id = f"mcpevt_{uuid4().hex}"
        event, payload = _event_from_invocation(invocation, event_id=event_id)
        connection.execute(
            """
            INSERT INTO mcp_invocation_events(
              mcp_invocation_event_id,tenant_id,mcp_invocation_id,event_type,schema_version,
              mcp_server_id,mcp_tool_id,status,risk_level,approval_required,network_attempted,
              input_dlp_status,input_dlp_match_count,output_dlp_status,
              output_dlp_match_count,redaction_count,probe_attempt_count,call_attempt_count,
              duration_ms,request_bytes,response_bytes,execution_status,invocation_count,
              tool_call_count,cost_status,error_code,completed_at,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                event_id,
                tenant_id,
                invocation_id,
                MCP_TOOL_CALLED_EVENT_TYPE,
                payload["schema_version"],
                payload["mcp_server_id"],
                payload["mcp_tool_id"],
                payload["status"],
                payload["risk_level"],
                int(payload["approval_required"]),
                int(payload["network_attempted"]),
                payload["input_dlp_status"],
                payload["input_dlp_match_count"],
                payload["output_dlp_status"],
                payload["output_dlp_match_count"],
                payload["redaction_count"],
                payload["probe_attempt_count"],
                payload["call_attempt_count"],
                payload["duration_ms"],
                payload["request_bytes"],
                payload["response_bytes"],
                payload["execution_status"],
                payload["invocation_count"],
                payload["tool_call_count"],
                payload["cost_status"],
                payload["error_code"],
                payload["completed_at"],
                payload["completed_at"],
            ),
        )

        # Webhook owns Endpoint filtering, payload format, wire bytes, and secret-version pinning.
        from metahub_control.webhooks.service import freeze_webhook_event_delivery_snapshot

        frozen_at = payload["completed_at"]
        freeze_webhook_event_delivery_snapshot(connection, event, frozen_at=frozen_at)
        snapshot_json = _canonical_json(_delivery_ids(connection, event))
        outbox_id = f"mcpob_{uuid4().hex}"
        connection.execute(
            """
            INSERT INTO mcp_invocation_event_outbox(
              mcp_invocation_event_outbox_id,tenant_id,mcp_invocation_event_id,status,
              attempt_count,lease_generation,lease_owner_id,lease_token,lease_expires_at,
              next_attempt_at,subscription_snapshot_json,snapshot_frozen_at,error_code,
              delivered_at,created_at,updated_at
            ) VALUES(?,?,?,'pending',0,0,NULL,NULL,NULL,?,?,?,?,NULL,?,?)
            """,
            (
                outbox_id,
                tenant_id,
                event_id,
                frozen_at,
                snapshot_json,
                frozen_at,
                None,
                frozen_at,
                frozen_at,
            ),
        )
        return event_id, outbox_id
    except ApiError:
        raise
    except (McpContractError, sqlite3.Error, TypeError, ValueError, KeyError) as exc:
        raise ApiError("mcp.unavailable") from exc


def claim_next_mcp_invocation_event(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> McpInvocationEventClaim | None:
    """Claim one due source; expired tenth attempts are dead-lettered before new work."""

    claim, _ = _claim_with_progress(config, worker_id=worker_id, now=now)
    return claim


def _claim_with_progress(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None,
) -> tuple[McpInvocationEventClaim | None, bool]:
    owner = _worker_id(worker_id)
    connection = _connect(config)
    progressed = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        current_text = _db_time(current)
        while True:
            row = connection.execute(
                """
                SELECT outbox.*,event.*
                FROM mcp_invocation_event_outbox outbox
                LEFT JOIN mcp_invocation_events event
                  ON event.tenant_id=outbox.tenant_id
                 AND event.mcp_invocation_event_id=outbox.mcp_invocation_event_id
                WHERE (outbox.status='pending' AND outbox.attempt_count<?
                       AND unixepoch(outbox.next_attempt_at)<=unixepoch(?))
                   OR (outbox.status='processing'
                       AND unixepoch(outbox.lease_expires_at)<=unixepoch(?))
                ORDER BY CASE outbox.status WHEN 'processing' THEN 0 ELSE 1 END,
                         COALESCE(outbox.lease_expires_at,outbox.next_attempt_at),
                         outbox.created_at,outbox.mcp_invocation_event_outbox_id
                LIMIT 1
                """,
                (EVENT_MAX_ATTEMPTS, current_text, current_text),
            ).fetchone()
            if row is None:
                connection.commit()
                return None, progressed

            old_attempt = _bounded_int(row["attempt_count"], 0, EVENT_MAX_ATTEMPTS)
            if row["status"] == "processing" and old_attempt >= EVENT_MAX_ATTEMPTS:
                _recover_exhausted(connection, row, current_text)
                connection.commit()
                progressed = True
                connection.execute("BEGIN IMMEDIATE")
                continue

            old_generation = _bounded_int(row["lease_generation"], 0, 2**63 - 2)
            attempt = old_attempt + 1
            generation = old_generation + 1
            token = secrets.token_hex(32)
            claim_time = max(current, _parse_time(row["updated_at"]))
            expires = claim_time + timedelta(seconds=_lease_seconds(config))
            claimed = connection.execute(
                """
                UPDATE mcp_invocation_event_outbox
                SET status='processing',attempt_count=?,lease_generation=?,lease_owner_id=?,
                    lease_token=?,lease_expires_at=?,next_attempt_at=NULL,error_code=NULL,
                    delivered_at=NULL,updated_at=?
                WHERE tenant_id=? AND mcp_invocation_event_outbox_id=?
                  AND mcp_invocation_event_id=? AND status=? AND attempt_count=?
                  AND lease_generation=?
                  AND ((status='pending' AND lease_owner_id IS NULL AND lease_token IS NULL
                        AND lease_expires_at IS NULL
                        AND unixepoch(next_attempt_at)<=unixepoch(?))
                    OR (status='processing' AND lease_owner_id=? AND lease_token=?
                        AND lease_expires_at=?
                        AND unixepoch(lease_expires_at)<=unixepoch(?)))
                """,
                (
                    attempt,
                    generation,
                    owner,
                    token,
                    _db_time(expires),
                    _db_time(claim_time),
                    row["tenant_id"],
                    row["mcp_invocation_event_outbox_id"],
                    row["mcp_invocation_event_id"],
                    row["status"],
                    old_attempt,
                    old_generation,
                    current_text,
                    row["lease_owner_id"],
                    row["lease_token"],
                    row["lease_expires_at"],
                    current_text,
                ),
            )
            if claimed.rowcount != 1:
                connection.rollback()
                return None, progressed
            try:
                source = connection.execute(
                    """
                    SELECT * FROM mcp_invocation_events
                    WHERE tenant_id=? AND mcp_invocation_event_id=?
                    """,
                    (row["tenant_id"], row["mcp_invocation_event_id"]),
                ).fetchone()
                if source is None:
                    raise ApiError("mcp.unavailable")
                event = _event_from_source_row(connection, source)
                invocation = _load_invocation(
                    connection,
                    _safe_text(row["tenant_id"], maximum=160),
                    _matched(row["mcp_invocation_id"], _INVOCATION_ID),
                )
                snapshot_json, snapshot_frozen_at = _verify_existing_snapshot(
                    connection,
                    event,
                    snapshot_json=row["subscription_snapshot_json"],
                    frozen_at=row["snapshot_frozen_at"],
                )
                claim = McpInvocationEventClaim(
                    outbox_id=_matched(row["mcp_invocation_event_outbox_id"], _OUTBOX_ID),
                    event_id=_matched(row["mcp_invocation_event_id"], _EVENT_ID),
                    tenant_id=_safe_text(row["tenant_id"], maximum=160),
                    invocation_id=_matched(row["mcp_invocation_id"], _INVOCATION_ID),
                    source_request_id=_safe_text(invocation["request_id"], maximum=128),
                    source_trace_id=_safe_text(invocation["trace_id"], maximum=128),
                    event=event,
                    attempt_count=attempt,
                    lease_generation=generation,
                    lease_owner_id=owner,
                    lease_token=token,
                    lease_expires_at=expires,
                    snapshot_json=snapshot_json,
                    snapshot_frozen_at=snapshot_frozen_at,
                )
            except (ApiError, McpContractError, TypeError, ValueError, KeyError):
                _quarantine_claimed(
                    connection,
                    row,
                    owner=owner,
                    token=token,
                    attempt=attempt,
                    generation=generation,
                    expires=expires,
                    now=_db_time(claim_time),
                )
                connection.commit()
                progressed = True
                connection.execute("BEGIN IMMEDIATE")
                continue
            connection.commit()
            return claim, True
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, TypeError, ValueError, KeyError) as exc:
        _rollback(connection)
        raise ApiError("mcp.unavailable") from exc
    finally:
        connection.close()


def dispatch_claimed_mcp_invocation_event(
    config: AppConfig,
    claim: McpInvocationEventClaim,
    *,
    now: datetime | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """Resume only the frozen delivery set and converge through the current source lease."""

    network_attempts_started = 0

    def preserve_source_lease() -> None:
        nonlocal network_attempts_started
        if should_stop is not None and should_stop():
            if network_attempts_started == 0:
                raise _StopBeforeDispatch
            raise ApiError("mcp.unavailable")
        _renew(config, claim, now=now)
        network_attempts_started += 1

    error_code: str | None = None
    delivery_terminal = False
    try:
        if should_stop is not None and should_stop():
            raise _StopBeforeDispatch
        _freeze_snapshot(config, claim, now=now)
        from metahub_control.webhooks.service import dispatch_webhook_event

        deliveries = dispatch_webhook_event(
            config,
            claim.tenant_id,
            claim.event,
            request_id=claim.source_request_id,
            trace_id=claim.source_trace_id,
            resume_existing_pending=True,
            source_lease_guard=preserve_source_lease,
            create_snapshot_if_missing=False,
        )
        statuses = [getattr(delivery, "status", None) for delivery in deliveries]
        delivery_terminal = any(status == "failed" for status in statuses)
        if any(status != "success" for status in statuses):
            error_code = "webhook.delivery_unavailable"
    except _StopBeforeDispatch:
        return _release_claim(config, claim, now=now)
    except ValueError:
        error_code = "webhook.payload_invalid"
    except Exception:
        # Endpoint, response, credential, and third-party exception text must never be persisted.
        error_code = "webhook.delivery_unavailable"
    return _finish(
        config,
        claim,
        error_code=error_code,
        delivery_terminal=delivery_terminal,
        now=now,
    )


def drain_mcp_invocation_event_once(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """Claim and process at most one source; malformed-source progress also returns ``True``."""

    if should_stop is not None and should_stop():
        return False
    claim, progressed = _claim_with_progress(config, worker_id=worker_id, now=now)
    if claim is None:
        return progressed
    dispatch_claimed_mcp_invocation_event(
        config,
        claim,
        now=now,
        should_stop=should_stop,
    )
    return True


def recover_mcp_invocation_source_request_id(
    connection: sqlite3.Connection,
    envelope: dict[str, object],
) -> str:
    """Recover the private request ID only after exact immutable-envelope reconstruction."""

    if type(envelope) is not dict:
        raise ApiError("webhook.delivery_unavailable")
    row = connection.execute(
        """
        SELECT * FROM mcp_invocation_events
        WHERE tenant_id=? AND mcp_invocation_event_id=?
        """,
        (envelope.get("tenant_id"), envelope.get("event_id")),
    ).fetchone()
    if row is None:
        raise ApiError("webhook.delivery_unavailable")
    try:
        event = _event_from_source_row(connection, row)
        if event.to_payload() != envelope:
            raise ApiError("webhook.delivery_unavailable")
        invocation = _load_invocation(
            connection,
            _safe_text(row["tenant_id"], maximum=160),
            _matched(row["mcp_invocation_id"], _INVOCATION_ID),
        )
        return _safe_text(invocation["request_id"], maximum=128)
    except (ApiError, McpContractError, TypeError, ValueError, KeyError) as exc:
        raise ApiError("webhook.delivery_unavailable") from exc


def _freeze_snapshot(
    config: AppConfig,
    claim: McpInvocationEventClaim,
    *,
    now: datetime | None,
) -> None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        row = _active_claim(connection, claim, current=current)
        event = _reload_claim_event(connection, claim)
        snapshot_json, frozen_at = _verify_existing_snapshot(
            connection,
            event,
            snapshot_json=row["subscription_snapshot_json"],
            frozen_at=row["snapshot_frozen_at"],
        )
        if snapshot_json is not None:
            if snapshot_json != claim.snapshot_json or frozen_at != claim.snapshot_frozen_at:
                raise ApiError("mcp.unavailable")
            connection.commit()
            return

        from metahub_control.webhooks.service import freeze_webhook_event_delivery_snapshot

        updated_at = max(current, _parse_time(row["updated_at"]))
        updated_text = _db_time(updated_at)
        freeze_webhook_event_delivery_snapshot(connection, event, frozen_at=updated_text)
        snapshot_json = _canonical_json(_delivery_ids(connection, event))
        generation = claim.lease_generation + 1
        token = secrets.token_hex(32)
        expires = max(
            claim.lease_expires_at,
            updated_at + timedelta(seconds=_lease_seconds(config)),
        )
        updated = connection.execute(
            """
            UPDATE mcp_invocation_event_outbox
            SET lease_generation=?,lease_token=?,lease_expires_at=?,
                subscription_snapshot_json=?,snapshot_frozen_at=?,updated_at=?
            WHERE tenant_id=? AND mcp_invocation_event_outbox_id=?
              AND mcp_invocation_event_id=? AND status='processing' AND attempt_count=?
              AND lease_generation=? AND lease_owner_id=? AND lease_token=?
              AND lease_expires_at=? AND subscription_snapshot_json IS NULL
              AND snapshot_frozen_at IS NULL
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                generation,
                token,
                _db_time(expires),
                snapshot_json,
                updated_text,
                updated_text,
                claim.tenant_id,
                claim.outbox_id,
                claim.event_id,
                claim.attempt_count,
                claim.lease_generation,
                claim.lease_owner_id,
                claim.lease_token,
                _db_time(claim.lease_expires_at),
                _db_time(current),
            ),
        )
        if updated.rowcount != 1:
            raise ApiError("mcp.unavailable")
        connection.commit()
        claim.lease_generation = generation
        claim.lease_token = token
        claim.lease_expires_at = expires
        claim.snapshot_json = snapshot_json
        claim.snapshot_frozen_at = updated_text
    except Exception:
        _rollback(connection)
        raise
    finally:
        connection.close()


def _renew(
    config: AppConfig,
    claim: McpInvocationEventClaim,
    *,
    now: datetime | None,
) -> None:
    """Renew with a rotated token and generation before every real Endpoint attempt."""

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        row = _active_claim(connection, claim, current=current)
        event = _reload_claim_event(connection, claim)
        snapshot_json, frozen_at = _verify_existing_snapshot(
            connection,
            event,
            snapshot_json=row["subscription_snapshot_json"],
            frozen_at=row["snapshot_frozen_at"],
        )
        if (
            snapshot_json is None
            or snapshot_json != claim.snapshot_json
            or frozen_at != claim.snapshot_frozen_at
        ):
            raise ApiError("mcp.unavailable")
        updated_at = max(current, _parse_time(row["updated_at"]))
        generation = claim.lease_generation + 1
        token = secrets.token_hex(32)
        expires = max(
            claim.lease_expires_at,
            updated_at + timedelta(seconds=_lease_seconds(config)),
        )
        updated = connection.execute(
            """
            UPDATE mcp_invocation_event_outbox
            SET lease_generation=?,lease_token=?,lease_expires_at=?,updated_at=?
            WHERE tenant_id=? AND mcp_invocation_event_outbox_id=?
              AND mcp_invocation_event_id=? AND status='processing' AND attempt_count=?
              AND lease_generation=? AND lease_owner_id=? AND lease_token=?
              AND lease_expires_at=? AND subscription_snapshot_json=?
              AND snapshot_frozen_at=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                generation,
                token,
                _db_time(expires),
                _db_time(updated_at),
                claim.tenant_id,
                claim.outbox_id,
                claim.event_id,
                claim.attempt_count,
                claim.lease_generation,
                claim.lease_owner_id,
                claim.lease_token,
                _db_time(claim.lease_expires_at),
                claim.snapshot_json,
                claim.snapshot_frozen_at,
                _db_time(current),
            ),
        )
        if updated.rowcount != 1:
            raise ApiError("mcp.unavailable")
        connection.commit()
        claim.lease_generation = generation
        claim.lease_token = token
        claim.lease_expires_at = expires
    except Exception:
        _rollback(connection)
        raise
    finally:
        connection.close()


def _release_claim(
    config: AppConfig,
    claim: McpInvocationEventClaim,
    *,
    now: datetime | None,
) -> bool:
    """Return a zero-I/O claim immediately; the tenth claim must converge to failed."""

    if claim.attempt_count >= EVENT_MAX_ATTEMPTS:
        return _finish(
            config,
            claim,
            error_code="webhook.delivery_unavailable",
            delivery_terminal=True,
            now=now,
        )
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        current_text = _db_time(current)
        updated = connection.execute(
            """
            UPDATE mcp_invocation_event_outbox
            SET status='pending',lease_generation=lease_generation+1,
                lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
                next_attempt_at=?,error_code=NULL,delivered_at=NULL,updated_at=?
            WHERE tenant_id=? AND mcp_invocation_event_outbox_id=?
              AND mcp_invocation_event_id=? AND status='processing' AND attempt_count=?
              AND lease_generation=? AND lease_owner_id=? AND lease_token=?
              AND lease_expires_at=? AND subscription_snapshot_json IS ?
              AND snapshot_frozen_at IS ?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                current_text,
                current_text,
                claim.tenant_id,
                claim.outbox_id,
                claim.event_id,
                claim.attempt_count,
                claim.lease_generation,
                claim.lease_owner_id,
                claim.lease_token,
                _db_time(claim.lease_expires_at),
                claim.snapshot_json,
                claim.snapshot_frozen_at,
                current_text,
            ),
        )
        if updated.rowcount != 1:
            connection.rollback()
            return False
        connection.commit()
        return True
    except (ApiError, sqlite3.Error, TypeError, ValueError):
        _rollback(connection)
        return False
    finally:
        connection.close()


def _finish(
    config: AppConfig,
    claim: McpInvocationEventClaim,
    *,
    error_code: str | None,
    delivery_terminal: bool = False,
    now: datetime | None,
) -> bool:
    allowed = {None, "webhook.payload_invalid", "webhook.delivery_unavailable", "mcp.unavailable"}
    reason = error_code if error_code in allowed else "webhook.delivery_unavailable"
    terminal = reason in {"webhook.payload_invalid", "mcp.unavailable"} or (
        reason is not None
        and (delivery_terminal or claim.attempt_count >= EVENT_MAX_ATTEMPTS)
    )
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        current_text = _db_time(current)
        if reason is None:
            status = "succeeded"
        elif terminal:
            status = "failed"
            _fail_pending_deliveries(
                connection,
                tenant_id=claim.tenant_id,
                event_id=claim.event_id,
                updated_at=current_text,
                source_error=reason,
            )
        else:
            status = "pending"
        delay = EVENT_BACKOFF_SECONDS[
            min(max(claim.attempt_count - 1, 0), len(EVENT_BACKOFF_SECONDS) - 1)
        ]
        next_attempt = (
            _db_time(current + timedelta(seconds=delay)) if status == "pending" else None
        )
        updated = connection.execute(
            """
            UPDATE mcp_invocation_event_outbox
            SET status=?,lease_generation=lease_generation+1,
                lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
                next_attempt_at=?,error_code=?,delivered_at=?,updated_at=?
            WHERE tenant_id=? AND mcp_invocation_event_outbox_id=?
              AND mcp_invocation_event_id=? AND status='processing' AND attempt_count=?
              AND lease_generation=? AND lease_owner_id=? AND lease_token=?
              AND lease_expires_at=? AND subscription_snapshot_json IS ?
              AND snapshot_frozen_at IS ?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                status,
                next_attempt,
                None if status == "succeeded" else reason,
                current_text if status == "succeeded" else None,
                current_text,
                claim.tenant_id,
                claim.outbox_id,
                claim.event_id,
                claim.attempt_count,
                claim.lease_generation,
                claim.lease_owner_id,
                claim.lease_token,
                _db_time(claim.lease_expires_at),
                claim.snapshot_json,
                claim.snapshot_frozen_at,
                current_text,
            ),
        )
        if updated.rowcount != 1:
            connection.rollback()
            return False
        connection.commit()
        return True
    except (ApiError, sqlite3.Error, TypeError, ValueError):
        _rollback(connection)
        return False
    finally:
        connection.close()


def _active_claim(
    connection: sqlite3.Connection,
    claim: McpInvocationEventClaim,
    *,
    current: datetime,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT * FROM mcp_invocation_event_outbox
        WHERE tenant_id=? AND mcp_invocation_event_outbox_id=?
          AND mcp_invocation_event_id=? AND status='processing' AND attempt_count=?
          AND lease_generation=? AND lease_owner_id=? AND lease_token=?
          AND lease_expires_at=? AND subscription_snapshot_json IS ?
          AND snapshot_frozen_at IS ?
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
            _db_time(claim.lease_expires_at),
            claim.snapshot_json,
            claim.snapshot_frozen_at,
            _db_time(current),
        ),
    ).fetchone()
    if row is None:
        raise ApiError("mcp.unavailable")
    return row


def _reload_claim_event(
    connection: sqlite3.Connection,
    claim: McpInvocationEventClaim,
) -> WebhookEvent:
    row = connection.execute(
        """
        SELECT * FROM mcp_invocation_events
        WHERE tenant_id=? AND mcp_invocation_event_id=? AND mcp_invocation_id=?
        """,
        (claim.tenant_id, claim.event_id, claim.invocation_id),
    ).fetchone()
    if row is None:
        raise ApiError("mcp.unavailable")
    event = _event_from_source_row(connection, row)
    if event.to_payload() != claim.event.to_payload():
        raise ApiError("mcp.unavailable")
    return event


def _event_from_source_row(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> WebhookEvent:
    tenant_id = _safe_text(row["tenant_id"], maximum=160)
    invocation_id = _matched(row["mcp_invocation_id"], _INVOCATION_ID)
    event_id = _matched(row["mcp_invocation_event_id"], _EVENT_ID)
    if row["event_type"] != MCP_TOOL_CALLED_EVENT_TYPE or row["created_at"] != row["completed_at"]:
        raise ApiError("mcp.unavailable")
    invocation = _load_invocation(connection, tenant_id, invocation_id)
    event, payload = _event_from_invocation(invocation, event_id=event_id)
    expected = {
        "schema_version": row["schema_version"],
        "mcp_invocation_id": invocation_id,
        "mcp_server_id": row["mcp_server_id"],
        "mcp_tool_id": row["mcp_tool_id"],
        "status": row["status"],
        "risk_level": row["risk_level"],
        "approval_required": _sqlite_bool(row["approval_required"]),
        "network_attempted": _sqlite_bool(row["network_attempted"]),
        "input_dlp_status": row["input_dlp_status"],
        "input_dlp_match_count": row["input_dlp_match_count"],
        "output_dlp_status": row["output_dlp_status"],
        "output_dlp_match_count": row["output_dlp_match_count"],
        "redaction_count": row["redaction_count"],
        "probe_attempt_count": row["probe_attempt_count"],
        "call_attempt_count": row["call_attempt_count"],
        "duration_ms": row["duration_ms"],
        "request_bytes": row["request_bytes"],
        "response_bytes": row["response_bytes"],
        "execution_status": row["execution_status"],
        "invocation_count": row["invocation_count"],
        "tool_call_count": row["tool_call_count"],
        "cost_status": row["cost_status"],
        "error_code": row["error_code"],
        "completed_at": row["completed_at"],
    }
    if expected != payload:
        raise ApiError("mcp.unavailable")
    return event


def _event_from_invocation(
    invocation: sqlite3.Row,
    *,
    event_id: str,
) -> tuple[WebhookEvent, dict[str, Any]]:
    tenant_id = _safe_text(invocation["tenant_id"], maximum=160)
    invocation_id = _matched(invocation["mcp_invocation_id"], _INVOCATION_ID)
    actor_id = _safe_text(invocation["requested_by"], maximum=160)
    request_id = _safe_text(invocation["request_id"], maximum=128)
    trace_id = _safe_text(invocation["trace_id"], maximum=128)
    completed_text = _canonical_time(invocation["completed_at"])
    completed_at = _parse_time(completed_text)
    execution_status = _safe_text(invocation["execution_status"], maximum=32)
    output_status = invocation["output_dlp_status"] or "not_scanned"
    payload: dict[str, Any] = {
        "schema_version": 1,
        "mcp_invocation_id": invocation_id,
        "mcp_server_id": _matched(invocation["mcp_server_id"], _SERVER_ID),
        "mcp_tool_id": _matched(invocation["mcp_tool_id"], _TOOL_ID),
        "status": invocation["status"],
        "risk_level": invocation["risk_level"],
        "approval_required": _sqlite_bool(invocation["approval_required"]),
        "network_attempted": True,
        "input_dlp_status": invocation["input_dlp_status"],
        "input_dlp_match_count": invocation["input_dlp_match_count"],
        "output_dlp_status": output_status,
        "output_dlp_match_count": invocation["output_dlp_match_count"] or 0,
        "redaction_count": invocation["result_redaction_count"] or 0,
        "probe_attempt_count": invocation["probe_attempt_count"],
        "call_attempt_count": invocation["call_attempt_count"],
        "duration_ms": invocation["duration_ms"],
        "request_bytes": invocation["request_bytes"],
        "response_bytes": invocation["response_bytes"],
        "execution_status": execution_status,
        "invocation_count": invocation["invocation_count"],
        "tool_call_count": invocation["tool_call_count"],
        "cost_status": invocation["cost_status"],
        "error_code": invocation["error_code"],
        "completed_at": completed_text,
    }
    safe_payload = validate_mcp_tool_called_event(
        payload,
        event_id=event_id,
        tenant_id=tenant_id,
        actor_id=actor_id,
        object_id=invocation_id,
        trace_id=trace_id,
        source_request_id=request_id,
        occurred_at=completed_at,
    )
    event = build_webhook_event(
        tenant_id=tenant_id,
        event_type=MCP_TOOL_CALLED_EVENT_TYPE,
        event_id=event_id,
        timestamp=completed_at,
        actor_id=actor_id,
        object_id=invocation_id,
        trace_id=trace_id,
        source_request_id=request_id,
        payload=safe_payload,
    )
    return event, safe_payload


def _load_invocation(
    connection: sqlite3.Connection,
    tenant_id: str,
    invocation_id: str,
) -> sqlite3.Row:
    # Only the event-safe projection is loaded. Arguments, result, endpoint, schema, and hashes
    # never enter this module's runtime values or exception paths.
    row = connection.execute(
        """
        SELECT tenant_id,mcp_invocation_id,mcp_server_id,mcp_tool_id,requested_by,
               request_id,trace_id,status,risk_level,approval_required,input_dlp_status,
               input_dlp_match_count,output_dlp_status,output_dlp_match_count,
               result_redaction_count,probe_attempt_count,call_attempt_count,duration_ms,
               request_bytes,response_bytes,execution_status,invocation_count,
               tool_call_count,cost_status,error_code,completed_at
        FROM mcp_invocations
        WHERE tenant_id=? AND mcp_invocation_id=?
        """,
        (tenant_id, invocation_id),
    ).fetchone()
    if row is None:
        raise ApiError("mcp.unavailable")
    return row


def _verify_existing_snapshot(
    connection: sqlite3.Connection,
    event: WebhookEvent,
    *,
    snapshot_json: object,
    frozen_at: object,
) -> tuple[str | None, str | None]:
    if snapshot_json is None and frozen_at is None:
        if _delivery_ids(connection, event):
            raise ApiError("mcp.unavailable")
        return None, None
    if type(snapshot_json) is not str or type(frozen_at) is not str:
        raise ApiError("mcp.unavailable")
    _parse_time(frozen_at)
    values = _snapshot(snapshot_json)
    if values != _delivery_ids(connection, event):
        raise ApiError("mcp.unavailable")
    canonical = _canonical_json(values)
    if canonical != snapshot_json:
        raise ApiError("mcp.unavailable")
    return canonical, frozen_at


def _recover_exhausted(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    now: str,
) -> None:
    _fail_pending_deliveries(
        connection,
        tenant_id=row["tenant_id"],
        event_id=row["mcp_invocation_event_id"],
        updated_at=now,
        source_error="webhook.delivery_unavailable",
    )
    updated = connection.execute(
        """
        UPDATE mcp_invocation_event_outbox
        SET status='failed',lease_generation=lease_generation+1,
            lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
            next_attempt_at=NULL,error_code='webhook.delivery_unavailable',
            delivered_at=NULL,updated_at=?
        WHERE tenant_id=? AND mcp_invocation_event_outbox_id=?
          AND mcp_invocation_event_id=? AND status='processing' AND attempt_count=?
          AND lease_generation=? AND lease_owner_id=? AND lease_token=?
          AND lease_expires_at=? AND unixepoch(lease_expires_at)<=unixepoch(?)
        """,
        (
            now,
            row["tenant_id"],
            row["mcp_invocation_event_outbox_id"],
            row["mcp_invocation_event_id"],
            row["attempt_count"],
            row["lease_generation"],
            row["lease_owner_id"],
            row["lease_token"],
            row["lease_expires_at"],
            now,
        ),
    )
    if updated.rowcount != 1:
        raise ApiError("mcp.unavailable")


def _quarantine_claimed(
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
    if row["event_type"] == MCP_TOOL_CALLED_EVENT_TYPE:
        _fail_pending_deliveries(
            connection,
            tenant_id=row["tenant_id"],
            event_id=row["mcp_invocation_event_id"],
            updated_at=now,
            source_error="mcp.unavailable",
        )
    failed = connection.execute(
        """
        UPDATE mcp_invocation_event_outbox
        SET status='failed',lease_generation=lease_generation+1,
            lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
            next_attempt_at=NULL,error_code='mcp.unavailable',delivered_at=NULL,updated_at=?
        WHERE tenant_id=? AND mcp_invocation_event_outbox_id=?
          AND mcp_invocation_event_id=? AND status='processing' AND attempt_count=?
          AND lease_generation=? AND lease_owner_id=? AND lease_token=?
          AND lease_expires_at=? AND unixepoch(lease_expires_at)>unixepoch(?)
        """,
        (
            now,
            row["tenant_id"],
            row["mcp_invocation_event_outbox_id"],
            row["mcp_invocation_event_id"],
            attempt,
            generation,
            owner,
            token,
            _db_time(expires),
            now,
        ),
    )
    if failed.rowcount != 1:
        raise ApiError("mcp.unavailable")


def _fail_pending_deliveries(
    connection: sqlite3.Connection,
    *,
    tenant_id: object,
    event_id: object,
    updated_at: str,
    source_error: str,
) -> None:
    from metahub_control.webhooks.service import fail_pending_webhook_event_deliveries

    fail_pending_webhook_event_deliveries(
        connection,
        tenant_id=tenant_id,
        event_id=event_id,
        event_type=MCP_TOOL_CALLED_EVENT_TYPE,
        updated_at=updated_at,
        failure_code=(
            "webhook.payload_invalid"
            if source_error in {"webhook.payload_invalid", "mcp.unavailable"}
            else "webhook.delivery_unavailable"
        ),
    )


def _delivery_ids(connection: sqlite3.Connection, event: WebhookEvent) -> list[str]:
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
        raise ApiError("mcp.unavailable")
    return values


def _snapshot(value: str) -> list[str]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        raise ApiError("mcp.unavailable") from None
    if (
        type(decoded) is not list
        or decoded != sorted(set(decoded))
        or any(type(item) is not str or _DELIVERY_ID.fullmatch(item) is None for item in decoded)
    ):
        raise ApiError("mcp.unavailable")
    return decoded


def _lease_seconds(config: AppConfig) -> int:
    value = getattr(config, "mcp_invocation_lease_seconds", DEFAULT_EVENT_LEASE_SECONDS)
    if type(value) is not int or not 30 <= value <= 3_600:
        raise ApiError("mcp.unavailable")
    return value


def _transaction_now(
    connection: sqlite3.Connection,
    override: datetime | None,
) -> datetime:
    if override is not None:
        if override.tzinfo is None:
            raise ApiError("mcp.unavailable")
        return override.astimezone(UTC).replace(microsecond=0)
    row = connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')").fetchone()
    if row is None:
        raise ApiError("mcp.unavailable")
    return _parse_time(row[0])


def _parse_time(value: object) -> datetime:
    if type(value) is not str or len(value) != 20 or not value.endswith("Z"):
        raise ApiError("mcp.unavailable")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ApiError("mcp.unavailable") from None
    if parsed.tzinfo is None or _db_time(parsed) != value:
        raise ApiError("mcp.unavailable")
    return parsed.astimezone(UTC)


def _canonical_time(value: object) -> str:
    parsed = _parse_time(value)
    return _db_time(parsed)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _worker_id(value: object) -> str:
    if type(value) is not str or _WORKER_ID.fullmatch(value) is None:
        raise ApiError("mcp.unavailable")
    return value


def _matched(value: object, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise ApiError("mcp.unavailable")
    return value


def _safe_text(value: object, *, maximum: int) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= maximum
        or _SAFE_ID.fullmatch(value) is None
    ):
        raise ApiError("mcp.unavailable")
    return value


def _sqlite_bool(value: object) -> bool:
    if type(value) is not int or value not in {0, 1}:
        raise ApiError("mcp.unavailable")
    return bool(value)


def _bounded_int(value: object, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ApiError("mcp.unavailable")
    return value


def _row_value(row: sqlite3.Row | Mapping[str, object], key: str) -> object:
    return row[key]


def _canonical_json(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 65_536:
        raise ApiError("mcp.unavailable")
    return encoded


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except DbError as exc:
        raise ApiError("mcp.unavailable") from exc


def _rollback(connection: sqlite3.Connection) -> None:
    try:
        if connection.in_transaction:
            connection.rollback()
    except sqlite3.Error:
        return


__all__ = [
    "McpInvocationEventClaim",
    "claim_next_mcp_invocation_event",
    "create_mcp_invocation_event_in_transaction",
    "dispatch_claimed_mcp_invocation_event",
    "drain_mcp_invocation_event_once",
    "recover_mcp_invocation_source_request_id",
]
