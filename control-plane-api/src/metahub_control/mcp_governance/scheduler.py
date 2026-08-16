"""Durable, tenant-fair PRD-082 MCP Scheduler and Worker orchestration.

Every network operation is separated from its claim/finalize transactions.  A
``calling`` row is the durable at-most-once call intent: an expired calling
lease is never retried and converges to ``outcome_unknown``.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import secrets
import sqlite3
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi import FastAPI
from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.mcp import (
    MCP_NETWORK_BLOCKED_EVENT_TYPE,
    MCP_SANDBOX_RESPONSE_RESERVE_SECONDS,
    McpContractError,
    McpCryptoOwner,
    McpHmacOwner,
    McpNetworkPolicy,
    decrypt_mcp_value_from_keyring,
    encrypt_mcp_value,
    mcp_hmac,
    project_mcp_result,
    validate_mcp_network_blocked_event,
    validate_mcp_network_policy,
    validate_result,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import WebhookEvent, build_webhook_event

from metahub_control.approvals.service import mcp_approval_target_coordinates
from metahub_control.audit import (
    AuditActor,
    AuditRequestMeta,
    record_audit_log_for_actor,
)
from metahub_control.auth import require_actor_transaction_permissions

from .executor import (
    McpCallRequest,
    McpCallResult,
    McpConnectionMaterial,
    McpDiscoverRequest,
    McpDiscoveryResult,
    McpExecutorError,
    McpProbeRequest,
    McpProbeResult,
    call_mcp_tool,
    discover_mcp_server,
    probe_mcp_tool,
)
from .outbox import (
    create_mcp_invocation_event_in_transaction,
    drain_mcp_invocation_event_once,
)
from .sandbox_client import (
    McpSandboxClientError,
    McpSandboxDispatch,
    McpSandboxHttpClient,
    McpSandboxPermitResponse,
    McpSandboxResponse,
    mcp_sandbox_lease_token_hmac,
    parse_sandbox_call,
    parse_sandbox_discovery,
    parse_sandbox_permit,
    parse_sandbox_probe,
)
from .service import (
    _SYNC_FINALIZE_RESERVE_SECONDS,
    DlpDecision,
    _current_encryption,
    _current_hmac,
    _encryption_keyring,
    _load_server,
    _load_tool_with_counts,
    _scan_dlp,
    _schema_limits,
    _server_audit_summary,
    _sha256,
    _tool_audit_summary,
    _tool_role_ids,
    _tool_user_ids,
)

logger = logging.getLogger("metahub.control.mcp.scheduler")

_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$", re.ASCII)
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_INSTANCE_ID = uuid4().hex
WORKER_ID = f"mcp-worker-{_INSTANCE_ID}"
OUTBOX_WORKER_ID = f"mcp-outbox-{_INSTANCE_ID}"
_MAX_PROBE_ATTEMPTS = 4
_NETWORK_EVENT_ID = re.compile(r"^mcpne_[0-9a-f]{32}$")
_NETWORK_OUTBOX_ID = re.compile(r"^mcpno_[0-9a-f]{32}$")
_WEBHOOK_DELIVERY_ID = re.compile(r"^whd_[0-9a-f]{32}$")
_NETWORK_EVENT_MAX_ATTEMPTS = 10
_NETWORK_EVENT_BACKOFF_SECONDS = (5, 30, 300)
_OUTBOX_SOURCE_LOCK = threading.Lock()
_NETWORK_OUTBOX_NEXT = False


class _NetworkStopBeforeDispatch(Exception):
    """A cooperative stop observed before an EVT-095 Endpoint attempt."""


@dataclass(frozen=True, slots=True)
class McpSchedulerTenantLease:
    tenant_id: str
    lease_generation: int
    lease_owner_id: str
    lease_token: str = field(repr=False)
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class McpSyncJobClaim:
    tenant_id: str
    job_id: str
    server_id: str
    operation_id: str
    job_type: str
    candidate_credential_revision: int | None
    attempt_count: int
    lease_generation: int
    lease_owner_id: str
    lease_token: str = field(repr=False)
    lease_expires_at: datetime
    deadline_at: datetime


@dataclass(frozen=True, slots=True)
class McpInvocationClaim:
    tenant_id: str
    invocation_id: str
    server_id: str
    tool_id: str
    attempt_id: str
    attempt_number: int
    lease_generation: int
    lease_owner_id: str
    lease_token: str = field(repr=False)
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class _InvocationMaterial:
    claim: McpInvocationClaim
    connection: McpConnectionMaterial = field(repr=False)
    arguments: dict[str, Any] = field(repr=False)
    input_dlp: DlpDecision = field(repr=False)
    remote_name: str


@dataclass(frozen=True, slots=True)
class _CallMaterial:
    tenant_id: str
    invocation_id: str
    attempt_id: str
    lease_generation: int
    lease_owner_id: str
    lease_token: str = field(repr=False)
    connection: McpConnectionMaterial = field(repr=False)
    arguments: dict[str, Any] = field(repr=False)
    remote_name: str


@dataclass(frozen=True, slots=True)
class _ToolRefreshAudit:
    tool_id: str
    before_summary: dict[str, object] = field(repr=False)
    after_summary: dict[str, object] = field(repr=False)


@dataclass(frozen=True, slots=True)
class _ServerRefreshAudit:
    action: str
    before_summary: dict[str, object] = field(repr=False)
    after_summary: dict[str, object] = field(repr=False)


@dataclass(frozen=True, slots=True)
class _NetworkFence:
    tenant_id: str
    server_id: str
    policy_id: str
    policy_revision: int
    policy_fingerprint: str
    policy_status: str
    failure_threshold: int
    open_seconds: int
    phase: str
    owner_type: str
    owner_id: str
    lease_generation: int
    lease_owner_id: str
    lease_token: str = field(repr=False)
    attempt_number: int
    circuit_revision: int
    half_open: bool
    request_id: str
    trace_id: str


@dataclass(slots=True)
class _NetworkEventClaim:
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
    snapshot_json: str | None = field(default=None, repr=False)
    snapshot_frozen_at: str | None = None


@dataclass(frozen=True, slots=True)
class _SandboxSyncMaterial:
    claim: McpSyncJobClaim
    policy: McpNetworkPolicy = field(repr=False)
    fence: _NetworkFence
    endpoint_ciphertext: str = field(repr=False)
    endpoint_hmac: str
    endpoint_hmac_key_version: int
    auth_type: str
    credential_id: str | None
    credential_ciphertext: str | None = field(default=None, repr=False)
    credential_revision: int | None = None


@dataclass(frozen=True, slots=True)
class _SandboxInvocationMaterial:
    claim: McpInvocationClaim
    policy: McpNetworkPolicy = field(repr=False)
    fence: _NetworkFence
    endpoint_ciphertext: str = field(repr=False)
    endpoint_hmac: str
    endpoint_hmac_key_version: int
    auth_type: str
    credential_id: str | None
    credential_ciphertext: str | None = field(repr=False)
    credential_revision: int | None
    arguments: dict[str, Any] = field(repr=False)
    arguments_ciphertext: str = field(repr=False)
    arguments_hmac: str
    arguments_hmac_key_version: int
    input_dlp: DlpDecision = field(repr=False)
    remote_name: str


@dataclass(frozen=True, slots=True)
class _SandboxCallPlan:
    material: _SandboxInvocationMaterial
    probe: McpProbeResult
    policy: McpNetworkPolicy = field(repr=False)
    fence: _NetworkFence
    network_decision_id: str
    call_intent_id: str
    schema_revision: int
    tool_policy_revision: int
    tool_policy_fingerprint: str
    call_generation: int
    call_owner_id: str
    call_token: str = field(repr=False)
    call_expires_at: datetime


@dataclass(frozen=True, slots=True)
class _SandboxCallMaterial:
    legacy: _CallMaterial
    dispatch: McpSandboxDispatch = field(repr=False)
    fence: _NetworkFence


def install_mcp_scheduler(app: FastAPI) -> None:
    """Install bounded Scheduler and source-outbox recovery loops."""

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        async with original_lifespan(current_app):
            stop_event = threading.Event()
            config = current_app.state.config
            tasks: list[asyncio.Task[None]] = []
            if config.mcp_enabled and config.mcp_scheduler_enabled:
                tasks.append(
                    asyncio.create_task(
                        _scheduler_loop(config, stop_event), name="mcp-governance-scheduler"
                    )
                )
                tasks.append(
                    asyncio.create_task(
                        _outbox_loop(config, stop_event), name="mcp-governance-outbox"
                    )
                )
            try:
                yield
            finally:
                stop_event.set()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

    app.router.lifespan_context = combined_lifespan


def claim_next_scheduler_tenant(
    config: AppConfig,
    *,
    worker_id: str = WORKER_ID,
    now: datetime | None = None,
) -> McpSchedulerTenantLease | None:
    """Claim the least-recently served tenant that currently has due work."""

    owner = _worker_id(worker_id)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        timestamp = _db_time(current)
        row = connection.execute(
            """
            SELECT scheduler.* FROM mcp_scheduler_tenants scheduler
            JOIN tenants tenant ON tenant.tenant_id=scheduler.tenant_id
            WHERE tenant.status='active'
              AND ((scheduler.status='idle'
                    AND unixepoch(scheduler.next_scan_at)<=unixepoch(?))
                OR (scheduler.status='leased'
                    AND unixepoch(scheduler.lease_expires_at)<=unixepoch(?)))
              AND (
                (SELECT COUNT(*) FROM mcp_server_sync_jobs active_job
                  WHERE active_job.tenant_id=scheduler.tenant_id
                    AND active_job.status='processing'
                    AND unixepoch(active_job.lease_expires_at)>unixepoch(?))
                +
                (SELECT COUNT(*) FROM mcp_invocations active_invocation
                  WHERE active_invocation.tenant_id=scheduler.tenant_id
                    AND active_invocation.status IN('probing','calling')
                    AND unixepoch(active_invocation.lease_expires_at)>unixepoch(?))
                < ?
              )
              AND (
                EXISTS(SELECT 1 FROM mcp_server_sync_jobs job
                  WHERE job.tenant_id=scheduler.tenant_id
                    AND ((job.status='pending'
                          AND unixepoch(job.next_attempt_at)<=unixepoch(?))
                      OR (job.status='processing'
                          AND unixepoch(job.lease_expires_at)<=unixepoch(?))))
                OR EXISTS(SELECT 1 FROM mcp_invocations invocation
                  WHERE invocation.tenant_id=scheduler.tenant_id
                    AND ((invocation.status='queued'
                          AND unixepoch(invocation.next_attempt_at)<=unixepoch(?))
                      OR (invocation.status IN('probing','calling')
                          AND unixepoch(invocation.lease_expires_at)<=unixepoch(?))))
              )
            ORDER BY scheduler.last_claim_sequence,
              coalesce(scheduler.last_claimed_at,scheduler.created_at),scheduler.tenant_id
            LIMIT 1
            """,
            (
                timestamp,
                timestamp,
                timestamp,
                timestamp,
                _tenant_concurrency(config),
                timestamp,
                timestamp,
                timestamp,
                timestamp,
            ),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        token = secrets.token_hex(32)
        expires = current + timedelta(seconds=_tenant_lease_seconds(config))
        changed = connection.execute(
            """
            UPDATE mcp_scheduler_tenants
            SET status='leased',lease_generation=lease_generation+1,
                lease_owner_id=?,lease_token=?,lease_expires_at=?,updated_at=?
            WHERE tenant_id=? AND status=? AND lease_generation=? AND updated_at=?
            """,
            (
                owner,
                token,
                _db_time(expires),
                timestamp,
                row["tenant_id"],
                row["status"],
                row["lease_generation"],
                row["updated_at"],
            ),
        ).rowcount
        if changed != 1:
            raise ApiError("mcp.unavailable")
        lease = McpSchedulerTenantLease(
            tenant_id=_text(row["tenant_id"]),
            lease_generation=int(row["lease_generation"]) + 1,
            lease_owner_id=owner,
            lease_token=token,
            lease_expires_at=expires,
        )
        connection.commit()
        return lease
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, TypeError, ValueError) as exc:
        _rollback(connection)
        raise ApiError("mcp.unavailable") from exc
    finally:
        connection.close()


def run_mcp_scheduler_once(
    config: AppConfig,
    *,
    worker_id: str = WORKER_ID,
    now: datetime | None = None,
    stop_event: threading.Event | None = None,
) -> bool:
    """Run at most one durable item while holding one tenant fairness lease."""

    lease: McpSchedulerTenantLease | None = None
    worked = False
    try:
        if (
            (stop_event is not None and stop_event.is_set())
            or not config.mcp_enabled
            or not config.mcp_scheduler_enabled
        ):
            return False
        lease = claim_next_scheduler_tenant(config, worker_id=worker_id, now=now)
        if lease is None:
            return False
        if _recover_expired_calling(config, lease, now=now):
            worked = True
        else:
            sync_claim = _claim_next_sync_job(config, lease, worker_id=worker_id, now=now)
            if sync_claim is not None:
                _release_scheduler_tenant(config, lease, worked=True)
                lease = None
                worked = True
                _process_sync_job(config, sync_claim, stop_event=stop_event)
            else:
                invocation_claim = _claim_next_invocation(
                    config, lease, worker_id=worker_id, now=now
                )
                if invocation_claim is not None:
                    _release_scheduler_tenant(config, lease, worked=True)
                    lease = None
                    worked = True
                    _process_invocation(config, invocation_claim, stop_event=stop_event)
        return worked
    except ApiError as exc:
        _warning("mcp.scheduler_failed", exc.code)
        return False
    except Exception:
        _warning("mcp.scheduler_failed", "mcp.unavailable")
        return False
    finally:
        if lease is not None:
            try:
                _release_scheduler_tenant(config, lease, worked=worked)
            except Exception:
                _warning("mcp.scheduler_release_failed", "mcp.unavailable")


def run_mcp_outbox_once(
    config: AppConfig,
    *,
    worker_id: str = OUTBOX_WORKER_ID,
    stop_event: threading.Event | None = None,
) -> bool:
    """Recover one EVT-094 or EVT-095 source without logging its payload."""

    try:
        should_stop = stop_event.is_set if stop_event is not None else None
        if should_stop is not None and should_stop():
            return False
        with _OUTBOX_SOURCE_LOCK:
            global _NETWORK_OUTBOX_NEXT
            network_first = _NETWORK_OUTBOX_NEXT
            _NETWORK_OUTBOX_NEXT = not _NETWORK_OUTBOX_NEXT
        drains: tuple[Callable[[], bool], Callable[[], bool]] = (
            (
                lambda: _drain_network_event_once(
                    config,
                    worker_id=worker_id,
                    should_stop=should_stop,
                )
            ),
            (
                lambda: drain_mcp_invocation_event_once(
                    config,
                    worker_id=worker_id,
                    should_stop=should_stop,
                )
            ),
        )
        if not network_first:
            drains = (drains[1], drains[0])
        for drain in drains:
            if drain():
                return True
        return False
    except ApiError as exc:
        _warning("mcp.outbox_failed", exc.code)
        return False
    except Exception:
        _warning("mcp.outbox_failed", "mcp.unavailable")
        return False


def _drain_network_event_once(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    if should_stop is not None and should_stop():
        return False
    claim, progressed = _claim_network_event_with_progress(
        config,
        worker_id=worker_id,
        now=now,
    )
    if claim is None:
        return progressed
    _dispatch_claimed_network_event(
        config,
        claim,
        now=now,
        should_stop=should_stop,
    )
    return True


def _claim_network_event_with_progress(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None,
) -> tuple[_NetworkEventClaim | None, bool]:
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
                WITH due AS (
                  SELECT candidate.*,
                    ROW_NUMBER() OVER(
                      PARTITION BY candidate.tenant_id
                      ORDER BY CASE candidate.status WHEN 'processing' THEN 0 ELSE 1 END,
                        COALESCE(candidate.lease_expires_at,candidate.next_attempt_at),
                        candidate.created_at,candidate.mcp_network_event_outbox_id
                    ) AS tenant_rank
                  FROM mcp_network_event_outbox candidate
                  WHERE ((candidate.status='pending' AND candidate.attempt_count<?
                           AND unixepoch(candidate.next_attempt_at)<=unixepoch(?))
                      OR (candidate.status='processing'
                           AND unixepoch(candidate.lease_expires_at)<=unixepoch(?)))
                    AND NOT EXISTS(
                      SELECT 1 FROM mcp_network_event_outbox active
                      WHERE active.tenant_id=candidate.tenant_id
                        AND active.status='processing'
                        AND active.mcp_network_event_outbox_id
                            !=candidate.mcp_network_event_outbox_id
                        AND unixepoch(active.lease_expires_at)>unixepoch(?)
                    )
                ), tenant_heads AS (
                  SELECT * FROM due WHERE tenant_rank=1
                )
                SELECT head.* FROM tenant_heads head
                ORDER BY COALESCE((
                    SELECT MAX(served.updated_at)
                    FROM mcp_network_event_outbox served
                    WHERE served.tenant_id=head.tenant_id AND served.attempt_count>0
                  ),'1970-01-01T00:00:00Z'),
                  CASE head.status WHEN 'processing' THEN 0 ELSE 1 END,
                  COALESCE(head.lease_expires_at,head.next_attempt_at),
                  head.created_at,head.tenant_id,head.mcp_network_event_outbox_id
                LIMIT 1
                """,
                (
                    _NETWORK_EVENT_MAX_ATTEMPTS,
                    current_text,
                    current_text,
                    current_text,
                ),
            ).fetchone()
            if row is None:
                connection.commit()
                return None, progressed
            old_attempt = _network_bounded_int(row["attempt_count"], 0, _NETWORK_EVENT_MAX_ATTEMPTS)
            if row["status"] == "processing" and old_attempt >= _NETWORK_EVENT_MAX_ATTEMPTS:
                _recover_exhausted_network_event(connection, row, current_text)
                connection.commit()
                progressed = True
                connection.execute("BEGIN IMMEDIATE")
                continue
            old_generation = _network_bounded_int(row["lease_generation"], 0, 2**63 - 2)
            attempt = old_attempt + 1
            generation = old_generation + 1
            token = secrets.token_hex(32)
            claim_time = max(current, _parse_db_time(row["updated_at"]))
            expires = claim_time + timedelta(seconds=_network_event_lease_seconds(config))
            claimed = connection.execute(
                """
                UPDATE mcp_network_event_outbox
                SET status='processing',attempt_count=?,lease_generation=?,lease_owner_id=?,
                    lease_token=?,lease_expires_at=?,next_attempt_at=NULL,error_code=NULL,
                    delivered_at=NULL,updated_at=?
                WHERE tenant_id=? AND mcp_network_event_outbox_id=?
                  AND mcp_network_event_id=? AND status=? AND attempt_count=?
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
                    row["mcp_network_event_outbox_id"],
                    row["mcp_network_event_id"],
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
                event = _load_network_webhook_event(
                    connection,
                    tenant_id=row["tenant_id"],
                    event_id=row["mcp_network_event_id"],
                )
                source = connection.execute(
                    """
                    SELECT request_id,trace_id FROM mcp_network_events
                    WHERE tenant_id=? AND mcp_network_event_id=?
                    """,
                    (row["tenant_id"], row["mcp_network_event_id"]),
                ).fetchone()
                if source is None:
                    raise ApiError("mcp.unavailable")
                snapshot_json, snapshot_frozen_at = _verify_network_snapshot(
                    connection,
                    event,
                    snapshot_json=row["subscription_snapshot_json"],
                    frozen_at=row["snapshot_frozen_at"],
                )
                claim = _NetworkEventClaim(
                    outbox_id=_network_matched(
                        row["mcp_network_event_outbox_id"], _NETWORK_OUTBOX_ID
                    ),
                    event_id=_network_matched(row["mcp_network_event_id"], _NETWORK_EVENT_ID),
                    tenant_id=_network_safe_text(row["tenant_id"], maximum=160),
                    source_request_id=_network_safe_text(source["request_id"], maximum=128),
                    source_trace_id=_network_safe_text(source["trace_id"], maximum=128),
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
                _quarantine_claimed_network_event(
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


def _dispatch_claimed_network_event(
    config: AppConfig,
    claim: _NetworkEventClaim,
    *,
    now: datetime | None,
    should_stop: Callable[[], bool] | None,
) -> bool:
    network_attempts_started = 0

    def preserve_source_lease() -> None:
        nonlocal network_attempts_started
        if should_stop is not None and should_stop():
            if network_attempts_started == 0:
                raise _NetworkStopBeforeDispatch
            raise ApiError("mcp.unavailable")
        _renew_network_event_claim(config, claim, now=now)
        network_attempts_started += 1

    error_code: str | None = None
    delivery_terminal = False
    try:
        if should_stop is not None and should_stop():
            raise _NetworkStopBeforeDispatch
        _freeze_network_event_snapshot(config, claim, now=now)
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
    except _NetworkStopBeforeDispatch:
        return _release_network_event_claim(config, claim, now=now)
    except ValueError:
        error_code = "webhook.payload_invalid"
    except Exception:
        error_code = "webhook.delivery_unavailable"
    return _finish_network_event_claim(
        config,
        claim,
        error_code=error_code,
        delivery_terminal=delivery_terminal,
        now=now,
    )


def _load_network_webhook_event(
    connection: sqlite3.Connection,
    *,
    tenant_id: object,
    event_id: object,
) -> WebhookEvent:
    safe_tenant = _network_safe_text(tenant_id, maximum=160)
    safe_event_id = _network_matched(event_id, _NETWORK_EVENT_ID)
    row = connection.execute(
        """
        SELECT * FROM mcp_network_events
        WHERE tenant_id=? AND mcp_network_event_id=?
        """,
        (safe_tenant, safe_event_id),
    ).fetchone()
    if row is None:
        raise ApiError("mcp.unavailable")
    decision = connection.execute(
        """
        SELECT * FROM mcp_network_decisions
        WHERE tenant_id=? AND mcp_network_decision_id=?
        """,
        (safe_tenant, row["mcp_network_decision_id"]),
    ).fetchone()
    decision_fields = (
        "mcp_server_id",
        "mcp_server_operation_id",
        "mcp_invocation_id",
        "phase",
        "reason_code",
        "policy_revision",
        "address_count",
        "attempt_number",
        "request_id",
        "trace_id",
    )
    if (
        decision is None
        or decision["result"] != "blocked"
        or decision["created_at"] != row["occurred_at"]
        or any(decision[field] != row[field] for field in decision_fields)
    ):
        raise ApiError("mcp.unavailable")
    operation_id = row["mcp_server_operation_id"]
    invocation_id = row["mcp_invocation_id"]
    if operation_id is not None:
        owner = connection.execute(
            """
            SELECT 1 FROM mcp_server_operations
            WHERE tenant_id=? AND mcp_server_operation_id=?
              AND status='failed' AND error_code='mcp.network_blocked'
            """,
            (safe_tenant, operation_id),
        ).fetchone()
    else:
        owner = connection.execute(
            """
            SELECT 1 FROM mcp_invocations invocation
            WHERE invocation.tenant_id=? AND invocation.mcp_invocation_id=?
              AND invocation.status='blocked'
              AND invocation.error_code='mcp.network_blocked'
              AND invocation.execution_status='not_called'
              AND invocation.tool_call_count=0
              AND invocation.cost_status='not_applicable'
              AND NOT EXISTS(SELECT 1 FROM mcp_network_call_intents intent
                WHERE intent.tenant_id=invocation.tenant_id
                  AND intent.mcp_invocation_id=invocation.mcp_invocation_id)
              AND NOT EXISTS(SELECT 1 FROM mcp_invocation_events event
                WHERE event.tenant_id=invocation.tenant_id
                  AND event.mcp_invocation_id=invocation.mcp_invocation_id)
            """,
            (safe_tenant, invocation_id),
        ).fetchone()
    if owner is None:
        raise ApiError("mcp.unavailable")
    occurred_text = _canonical_db_time(row["occurred_at"])
    payload: dict[str, Any] = {
        "schema_version": 1,
        "mcp_server_id": _network_safe_text(row["mcp_server_id"], maximum=38),
        "operation_id": operation_id,
        "mcp_invocation_id": invocation_id,
        "phase": row["phase"],
        "reason_code": row["reason_code"],
        "policy_revision": row["policy_revision"],
        "address_count": row["address_count"],
        "attempt_number": row["attempt_number"],
        "tool_call_count": 0,
        "cost_status": "not_applicable",
        "occurred_at": occurred_text,
    }
    occurred_at = _parse_db_time(occurred_text)
    request_id = _network_safe_text(row["request_id"], maximum=128)
    trace_id = _network_safe_text(row["trace_id"], maximum=128)
    object_id = operation_id or invocation_id
    safe_payload = validate_mcp_network_blocked_event(
        payload,
        event_id=safe_event_id,
        tenant_id=safe_tenant,
        actor_id=None,
        object_id=object_id,
        trace_id=trace_id,
        source_request_id=request_id,
        occurred_at=occurred_at,
    )
    event = build_webhook_event(
        tenant_id=safe_tenant,
        event_type=MCP_NETWORK_BLOCKED_EVENT_TYPE,
        event_id=safe_event_id,
        timestamp=occurred_at,
        actor_id=None,
        object_id=object_id,
        trace_id=trace_id,
        source_request_id=request_id,
        payload=safe_payload,
    )
    return event


def _freeze_network_event_snapshot(
    config: AppConfig,
    claim: _NetworkEventClaim,
    *,
    now: datetime | None,
) -> None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        row = _active_network_event_claim(connection, claim, current=current)
        event = _reload_claimed_network_event(connection, claim)
        snapshot_json, frozen_at = _verify_network_snapshot(
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

        updated_at = max(current, _parse_db_time(row["updated_at"]))
        updated_text = _db_time(updated_at)
        freeze_webhook_event_delivery_snapshot(connection, event, frozen_at=updated_text)
        snapshot_json = _network_canonical_json(_network_delivery_ids(connection, event))
        generation = claim.lease_generation + 1
        token = secrets.token_hex(32)
        expires = max(
            claim.lease_expires_at,
            updated_at + timedelta(seconds=_network_event_lease_seconds(config)),
        )
        updated = connection.execute(
            """
            UPDATE mcp_network_event_outbox
            SET lease_generation=?,lease_token=?,lease_expires_at=?,
                subscription_snapshot_json=?,snapshot_frozen_at=?,updated_at=?
            WHERE tenant_id=? AND mcp_network_event_outbox_id=?
              AND mcp_network_event_id=? AND status='processing' AND attempt_count=?
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


def _renew_network_event_claim(
    config: AppConfig,
    claim: _NetworkEventClaim,
    *,
    now: datetime | None,
) -> None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        row = _active_network_event_claim(connection, claim, current=current)
        event = _reload_claimed_network_event(connection, claim)
        snapshot_json, frozen_at = _verify_network_snapshot(
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
        updated_at = max(current, _parse_db_time(row["updated_at"]))
        generation = claim.lease_generation + 1
        token = secrets.token_hex(32)
        expires = max(
            claim.lease_expires_at,
            updated_at + timedelta(seconds=_network_event_lease_seconds(config)),
        )
        updated = connection.execute(
            """
            UPDATE mcp_network_event_outbox
            SET lease_generation=?,lease_token=?,lease_expires_at=?,updated_at=?
            WHERE tenant_id=? AND mcp_network_event_outbox_id=?
              AND mcp_network_event_id=? AND status='processing' AND attempt_count=?
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


def _release_network_event_claim(
    config: AppConfig,
    claim: _NetworkEventClaim,
    *,
    now: datetime | None,
) -> bool:
    if claim.attempt_count >= _NETWORK_EVENT_MAX_ATTEMPTS:
        return _finish_network_event_claim(
            config,
            claim,
            error_code="webhook.delivery_unavailable",
            delivery_terminal=True,
            now=now,
        )
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current_text = _db_time(_transaction_now(connection, now))
        updated = connection.execute(
            """
            UPDATE mcp_network_event_outbox
            SET status='pending',lease_generation=lease_generation+1,
                lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
                next_attempt_at=?,error_code=NULL,delivered_at=NULL,updated_at=?
            WHERE tenant_id=? AND mcp_network_event_outbox_id=?
              AND mcp_network_event_id=? AND status='processing' AND attempt_count=?
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


def _finish_network_event_claim(
    config: AppConfig,
    claim: _NetworkEventClaim,
    *,
    error_code: str | None,
    delivery_terminal: bool,
    now: datetime | None,
) -> bool:
    allowed = {
        None,
        "webhook.payload_invalid",
        "webhook.delivery_unavailable",
        "mcp.unavailable",
    }
    reason = error_code if error_code in allowed else "webhook.delivery_unavailable"
    terminal = reason in {"webhook.payload_invalid", "mcp.unavailable"} or (
        reason is not None
        and (delivery_terminal or claim.attempt_count >= _NETWORK_EVENT_MAX_ATTEMPTS)
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
            _fail_network_event_deliveries(
                connection,
                tenant_id=claim.tenant_id,
                event_id=claim.event_id,
                updated_at=current_text,
                source_error=reason,
            )
        else:
            status = "pending"
        delay = _NETWORK_EVENT_BACKOFF_SECONDS[
            min(
                max(claim.attempt_count - 1, 0),
                len(_NETWORK_EVENT_BACKOFF_SECONDS) - 1,
            )
        ]
        next_attempt = _db_time(current + timedelta(seconds=delay)) if status == "pending" else None
        updated = connection.execute(
            """
            UPDATE mcp_network_event_outbox
            SET status=?,lease_generation=lease_generation+1,
                lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
                next_attempt_at=?,error_code=?,delivered_at=?,updated_at=?
            WHERE tenant_id=? AND mcp_network_event_outbox_id=?
              AND mcp_network_event_id=? AND status='processing' AND attempt_count=?
              AND lease_generation=? AND lease_owner_id=? AND lease_token=?
              AND lease_expires_at=? AND subscription_snapshot_json IS ?
              AND snapshot_frozen_at IS ?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                status,
                next_attempt,
                reason if status == "failed" else None,
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


def _active_network_event_claim(
    connection: sqlite3.Connection,
    claim: _NetworkEventClaim,
    *,
    current: datetime,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT * FROM mcp_network_event_outbox
        WHERE tenant_id=? AND mcp_network_event_outbox_id=?
          AND mcp_network_event_id=? AND status='processing' AND attempt_count=?
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


def _reload_claimed_network_event(
    connection: sqlite3.Connection,
    claim: _NetworkEventClaim,
) -> WebhookEvent:
    event = _load_network_webhook_event(
        connection,
        tenant_id=claim.tenant_id,
        event_id=claim.event_id,
    )
    if event.to_payload() != claim.event.to_payload():
        raise ApiError("mcp.unavailable")
    return event


def _verify_network_snapshot(
    connection: sqlite3.Connection,
    event: WebhookEvent,
    *,
    snapshot_json: object,
    frozen_at: object,
) -> tuple[str | None, str | None]:
    if snapshot_json is None and frozen_at is None:
        if _network_delivery_ids(connection, event):
            raise ApiError("mcp.unavailable")
        return None, None
    if type(snapshot_json) is not str or type(frozen_at) is not str:
        raise ApiError("mcp.unavailable")
    if _canonical_db_time(frozen_at) != frozen_at:
        raise ApiError("mcp.unavailable")
    values = _network_snapshot(snapshot_json)
    if values != _network_delivery_ids(connection, event):
        raise ApiError("mcp.unavailable")
    canonical = _network_canonical_json(values)
    if canonical != snapshot_json:
        raise ApiError("mcp.unavailable")
    return canonical, frozen_at


def _recover_exhausted_network_event(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    now: str,
) -> None:
    _fail_network_event_deliveries(
        connection,
        tenant_id=row["tenant_id"],
        event_id=row["mcp_network_event_id"],
        updated_at=now,
        source_error="webhook.delivery_unavailable",
    )
    updated = connection.execute(
        """
        UPDATE mcp_network_event_outbox
        SET status='failed',lease_generation=lease_generation+1,
            lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
            next_attempt_at=NULL,error_code='webhook.delivery_unavailable',
            delivered_at=NULL,updated_at=?
        WHERE tenant_id=? AND mcp_network_event_outbox_id=?
          AND mcp_network_event_id=? AND status='processing' AND attempt_count=?
          AND lease_generation=? AND lease_owner_id=? AND lease_token=?
          AND lease_expires_at=? AND unixepoch(lease_expires_at)<=unixepoch(?)
        """,
        (
            now,
            row["tenant_id"],
            row["mcp_network_event_outbox_id"],
            row["mcp_network_event_id"],
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


def _quarantine_claimed_network_event(
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
    _fail_network_event_deliveries(
        connection,
        tenant_id=row["tenant_id"],
        event_id=row["mcp_network_event_id"],
        updated_at=now,
        source_error="mcp.unavailable",
    )
    failed = connection.execute(
        """
        UPDATE mcp_network_event_outbox
        SET status='failed',lease_generation=lease_generation+1,
            lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
            next_attempt_at=NULL,error_code='mcp.unavailable',delivered_at=NULL,updated_at=?
        WHERE tenant_id=? AND mcp_network_event_outbox_id=?
          AND mcp_network_event_id=? AND status='processing' AND attempt_count=?
          AND lease_generation=? AND lease_owner_id=? AND lease_token=?
          AND lease_expires_at=? AND unixepoch(lease_expires_at)>unixepoch(?)
        """,
        (
            now,
            row["tenant_id"],
            row["mcp_network_event_outbox_id"],
            row["mcp_network_event_id"],
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


def _fail_network_event_deliveries(
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
        event_type=MCP_NETWORK_BLOCKED_EVENT_TYPE,
        updated_at=updated_at,
        failure_code=(
            "webhook.payload_invalid"
            if source_error in {"webhook.payload_invalid", "mcp.unavailable"}
            else "webhook.delivery_unavailable"
        ),
    )


def _network_delivery_ids(
    connection: sqlite3.Connection,
    event: WebhookEvent,
) -> list[str]:
    rows = connection.execute(
        """
        SELECT webhook_delivery_id FROM webhook_deliveries
        WHERE tenant_id=? AND event_id=? AND delivery_kind='original'
        ORDER BY webhook_delivery_id
        """,
        (event.tenant_id, event.event_id),
    ).fetchall()
    values = [str(row["webhook_delivery_id"]) for row in rows]
    if any(_WEBHOOK_DELIVERY_ID.fullmatch(value) is None for value in values):
        raise ApiError("mcp.unavailable")
    return values


def _network_snapshot(value: str) -> list[str]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        raise ApiError("mcp.unavailable") from None
    if (
        type(decoded) is not list
        or decoded != sorted(set(decoded))
        or any(
            type(item) is not str or _WEBHOOK_DELIVERY_ID.fullmatch(item) is None
            for item in decoded
        )
    ):
        raise ApiError("mcp.unavailable")
    return decoded


def _network_event_lease_seconds(config: AppConfig) -> int:
    value = getattr(config, "mcp_invocation_lease_seconds", 90)
    if type(value) is not int or not 30 <= value <= 3_600:
        raise ApiError("mcp.unavailable")
    return value


def _network_canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _canonical_db_time(value: object) -> str:
    return _db_time(_parse_db_time(value))


def _network_matched(value: object, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise ApiError("mcp.unavailable")
    return value


def _network_safe_text(value: object, *, maximum: int) -> str:
    if type(value) is not str or not 1 <= len(value) <= maximum or "\x00" in value:
        raise ApiError("mcp.unavailable")
    return value


def _network_bounded_int(value: object, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ApiError("mcp.unavailable")
    return value


def _release_scheduler_tenant(
    config: AppConfig,
    lease: McpSchedulerTenantLease,
    *,
    worked: bool,
) -> None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _transaction_now(connection, None)
        timestamp = _db_time(now)
        next_scan = now if worked else now + timedelta(seconds=_poll_seconds(config))
        changed = connection.execute(
            """
            UPDATE mcp_scheduler_tenants
            SET status='idle',last_claimed_at=?,last_claim_sequence=last_claim_sequence+1,
                lease_generation=lease_generation+1,lease_owner_id=NULL,lease_token=NULL,
                lease_expires_at=NULL,next_scan_at=?,updated_at=?
            WHERE tenant_id=? AND status='leased' AND lease_generation=?
              AND lease_owner_id=? AND lease_token=?
            """,
            (
                timestamp,
                _db_time(next_scan),
                timestamp,
                lease.tenant_id,
                lease.lease_generation,
                lease.lease_owner_id,
                lease.lease_token,
            ),
        ).rowcount
        if changed != 1:
            raise ApiError("mcp.state_conflict")
        connection.commit()
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.Error as exc:
        _rollback(connection)
        raise ApiError("mcp.unavailable") from exc
    finally:
        connection.close()


def _claim_next_sync_job(
    config: AppConfig,
    tenant_lease: McpSchedulerTenantLease,
    *,
    worker_id: str,
    now: datetime | None,
) -> McpSyncJobClaim | None:
    owner = _worker_id(worker_id)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        timestamp = _db_time(current)
        _require_tenant_lease(connection, tenant_lease, timestamp)
        row = connection.execute(
            """
            SELECT job.* FROM mcp_server_sync_jobs job
            JOIN mcp_server_operations operation
              ON operation.tenant_id=job.tenant_id
             AND operation.mcp_server_operation_id=job.mcp_server_operation_id
            WHERE job.tenant_id=? AND operation.status IN('queued','processing')
              AND ((job.status='pending'
                    AND unixepoch(job.next_attempt_at)<=unixepoch(?))
                OR (job.status='processing'
                    AND unixepoch(job.lease_expires_at)<=unixepoch(?)))
            ORDER BY CASE job.status WHEN 'processing' THEN job.lease_expires_at
                     ELSE job.next_attempt_at END,job.created_at,job.mcp_server_sync_job_id
            LIMIT 1
            """,
            (tenant_lease.tenant_id, timestamp, timestamp),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        token = secrets.token_hex(32)
        expires = current + timedelta(seconds=_operation_lease_seconds(config))
        changed = connection.execute(
            """
            UPDATE mcp_server_sync_jobs
            SET status='processing',attempt_count=attempt_count+1,
                lease_generation=lease_generation+1,lease_owner_id=?,lease_token=?,
                lease_expires_at=?,next_attempt_at=NULL,started_at=coalesce(started_at,?),
                updated_at=?
            WHERE tenant_id=? AND mcp_server_sync_job_id=? AND status=?
              AND attempt_count=? AND lease_generation=? AND updated_at=?
            """,
            (
                owner,
                token,
                _db_time(expires),
                timestamp,
                timestamp,
                row["tenant_id"],
                row["mcp_server_sync_job_id"],
                row["status"],
                row["attempt_count"],
                row["lease_generation"],
                row["updated_at"],
            ),
        ).rowcount
        if changed != 1:
            raise ApiError("mcp.state_conflict")
        if row["status"] == "pending":
            changed_operation = connection.execute(
                """
                UPDATE mcp_server_operations SET status='processing',updated_at=?
                WHERE tenant_id=? AND mcp_server_operation_id=? AND status='queued'
                """,
                (timestamp, row["tenant_id"], row["mcp_server_operation_id"]),
            ).rowcount
            if changed_operation != 1:
                raise ApiError("mcp.state_conflict")
        claim = McpSyncJobClaim(
            tenant_id=_text(row["tenant_id"]),
            job_id=_text(row["mcp_server_sync_job_id"]),
            server_id=_text(row["mcp_server_id"]),
            operation_id=_text(row["mcp_server_operation_id"]),
            job_type=_text(row["job_type"]),
            candidate_credential_revision=(
                int(row["candidate_credential_revision"])
                if row["candidate_credential_revision"] is not None
                else None
            ),
            attempt_count=int(row["attempt_count"]) + 1,
            lease_generation=int(row["lease_generation"]) + 1,
            lease_owner_id=owner,
            lease_token=token,
            lease_expires_at=expires,
            deadline_at=_parse_db_time(row["deadline_at"]),
        )
        connection.commit()
        return claim
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, TypeError, ValueError) as exc:
        _rollback(connection)
        raise ApiError("mcp.unavailable") from exc
    finally:
        connection.close()


def _process_sync_job(
    config: AppConfig,
    claim: McpSyncJobClaim,
    *,
    stop_event: threading.Event | None = None,
) -> None:
    if _sandbox_http(config):
        _process_sync_job_sandbox(config, claim, stop_event=stop_event)
        return
    if stop_event is not None and stop_event.is_set():
        return
    try:
        deadline_seconds = _remaining_deadline_seconds(
            claim.deadline_at,
            lease_expires_at=claim.lease_expires_at,
            configured=config.mcp_probe_deadline_seconds,
        )
    except ApiError:
        _finalize_sync_failure(config, claim, error_code="mcp.unavailable")
        return
    if deadline_seconds is None:
        _finalize_sync_failure(
            config,
            claim,
            error_code=_sync_error_code(claim.job_type, "mcp.sync_failed"),
        )
        return
    try:
        material = _load_sync_connection_material(config, claim)
    except (ApiError, McpContractError, UnicodeError, ValueError, TypeError):
        _finalize_sync_failure(config, claim, error_code="mcp.unavailable")
        return
    if stop_event is not None and stop_event.is_set():
        return
    try:
        deadline_seconds = _remaining_deadline_seconds(
            claim.deadline_at,
            lease_expires_at=claim.lease_expires_at,
            configured=config.mcp_probe_deadline_seconds,
        )
    except ApiError:
        _finalize_sync_failure(config, claim, error_code="mcp.unavailable")
        return
    if deadline_seconds is None:
        _finalize_sync_failure(
            config,
            claim,
            error_code=_sync_error_code(claim.job_type, "mcp.sync_failed"),
        )
        return
    try:
        result = discover_mcp_server(
            config,
            McpDiscoverRequest(
                connection=material,
                deadline_seconds=deadline_seconds,
            ),
        )
    except McpExecutorError as exc:
        code = _sync_error_code(claim.job_type, exc.code)
        _finalize_sync_failure(config, claim, error_code=code)
        return
    except (ApiError, McpContractError, UnicodeError, ValueError, TypeError):
        _finalize_sync_failure(config, claim, error_code="mcp.unavailable")
        return
    try:
        _finalize_sync_success(config, claim, result)
    except ApiError as exc:
        if exc.code != "mcp.state_conflict":
            raise
        _finalize_sync_failure(config, claim, error_code="mcp.state_conflict")


def _process_sync_job_sandbox(
    config: AppConfig,
    claim: McpSyncJobClaim,
    *,
    stop_event: threading.Event | None = None,
) -> None:
    if stop_event is not None and stop_event.is_set():
        return
    try:
        dispatch_deadline = _sandbox_sync_dispatch_deadline(
            claim,
            configured=config.mcp_probe_deadline_seconds,
        )
    except ApiError:
        _finalize_sync_failure(config, claim, error_code="mcp.unavailable")
        return
    if dispatch_deadline is None:
        _finalize_sync_failure(
            config,
            claim,
            error_code=_sync_error_code(claim.job_type, "mcp.sync_failed"),
        )
        return
    material: _SandboxSyncMaterial | None = None
    try:
        material = _prepare_sandbox_sync(config, claim)
        if stop_event is not None and stop_event.is_set():
            return
        dispatch_deadline = _sandbox_sync_dispatch_deadline(
            claim,
            configured=config.mcp_probe_deadline_seconds,
        )
        if dispatch_deadline is None:
            _finalize_sync_failure(
                config,
                claim,
                error_code=_sync_error_code(claim.job_type, "mcp.sync_failed"),
                network=material.fence,
            )
            return
        action = "discover" if claim.job_type == "discover" else "probe"
        response = McpSandboxHttpClient(config).dispatch(
            McpSandboxDispatch(
                action=action,
                tenant_id=claim.tenant_id,
                server_id=claim.server_id,
                operation_id=claim.operation_id,
                invocation_id=None,
                lease_generation=claim.lease_generation,
                lease_owner_id=claim.lease_owner_id,
                lease_token=claim.lease_token,
                policy=material.policy,
                policy_revision=material.fence.policy_revision,
                policy_fingerprint=material.fence.policy_fingerprint,
                deadline_at=dispatch_deadline,
                request_id=material.fence.request_id,
                endpoint_ciphertext=material.endpoint_ciphertext,
                endpoint_hmac=material.endpoint_hmac,
                endpoint_hmac_key_version=material.endpoint_hmac_key_version,
                auth_type=material.auth_type,
                credential_id=material.credential_id,
                credential_ciphertext=material.credential_ciphertext,
                credential_revision=material.credential_revision,
            )
        )
        if response.status == "blocked":
            _finalize_sync_failure(
                config,
                claim,
                error_code="mcp.network_blocked",
                network=material.fence,
                network_response=response,
            )
            return
        if response.status == "failed":
            failure_code = response.failure_code or "mcp.sandbox_unavailable"
            _finalize_sync_failure(
                config,
                claim,
                error_code=(
                    "mcp.sandbox_unavailable"
                    if failure_code == "mcp.sandbox_unavailable"
                    else _sync_error_code(claim.job_type, failure_code)
                ),
                network=material.fence,
                network_response=response,
            )
            return
        result = parse_sandbox_discovery(response, config=config)
        _finalize_sync_success(
            config,
            claim,
            result,
            network=material.fence,
            network_response=response,
        )
    except McpExecutorError as exc:
        _finalize_sync_failure(
            config,
            claim,
            error_code=_sync_error_code(claim.job_type, exc.code),
            network=material.fence if material is not None else None,
            network_response=response if "response" in locals() else None,
        )
    except McpSandboxClientError:
        _finalize_sync_failure(
            config,
            claim,
            error_code="mcp.sandbox_unavailable",
            network=material.fence if material is not None else None,
        )
    except ApiError as exc:
        code = (
            _sync_error_code(claim.job_type, exc.code)
            if exc.code == "mcp.server_unavailable"
            else exc.code
            if exc.code == "mcp.state_conflict"
            else "mcp.unavailable"
        )
        _finalize_sync_failure(
            config,
            claim,
            error_code=code,
            network=material.fence if material is not None else None,
        )
    except (McpContractError, UnicodeError, TypeError, ValueError):
        _finalize_sync_failure(
            config,
            claim,
            error_code="mcp.sandbox_unavailable",
            network=material.fence if material is not None else None,
        )


def _prepare_sandbox_sync(
    config: AppConfig,
    claim: McpSyncJobClaim,
) -> _SandboxSyncMaterial:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, None)
        timestamp = _db_time(current)
        _require_sync_claim(connection, claim, timestamp=timestamp)
        row = connection.execute(
            """
            SELECT server.endpoint_ciphertext,server.endpoint_hmac,
              server.endpoint_hmac_key_version,server.transport,
              operation.start_server_revision,operation.start_credential_revision,
              operation.candidate_credential_revision,operation.status AS operation_status,
              operation.request_id,operation.trace_id,
              credential.mcp_server_credential_id,credential.credential_revision,
              credential.auth_type,credential.credential_ciphertext,
              credential.status AS credential_status,
              policy.mcp_network_policy_id,policy.current_revision,
              policy.current_fingerprint,policy.status AS network_status,
              revision.egress_mode,revision.connect_timeout_seconds,
              revision.probe_timeout_seconds,revision.call_timeout_seconds,
              revision.failure_threshold,revision.open_seconds
            FROM mcp_server_sync_jobs job
            JOIN mcp_server_operations operation
              ON operation.tenant_id=job.tenant_id
             AND operation.mcp_server_operation_id=job.mcp_server_operation_id
            JOIN mcp_servers server ON server.tenant_id=job.tenant_id
             AND server.mcp_server_id=job.mcp_server_id
            JOIN mcp_network_policies policy ON policy.tenant_id=server.tenant_id
             AND policy.mcp_server_id=server.mcp_server_id
            JOIN mcp_network_policy_revisions revision
              ON revision.tenant_id=policy.tenant_id
             AND revision.mcp_network_policy_id=policy.mcp_network_policy_id
             AND revision.policy_revision=policy.current_revision
            LEFT JOIN mcp_server_credentials credential
              ON credential.tenant_id=server.tenant_id
             AND credential.mcp_server_id=server.mcp_server_id
             AND credential.credential_revision=coalesce(
                   job.candidate_credential_revision,server.active_credential_revision)
            WHERE job.tenant_id=? AND job.mcp_server_sync_job_id=?
            """,
            (claim.tenant_id, claim.job_id),
        ).fetchone()
        if (
            row is None
            or row["operation_status"] != "processing"
            or row["network_status"] != "active"
            or int(row["start_server_revision"])
            != int(_load_server(connection, claim.tenant_id, claim.server_id)["state_revision"])
            or (
                claim.candidate_credential_revision is not None
                and (
                    row["credential_revision"] != claim.candidate_credential_revision
                    or row["credential_status"] != "candidate"
                )
            )
        ):
            raise ApiError("mcp.state_conflict")
        policy = _network_policy_from_row(row, config=config)
        fence = _start_network_fence(
            connection,
            tenant_id=claim.tenant_id,
            server_id=claim.server_id,
            policy_row=row,
            phase="sync" if claim.job_type == "discover" else "probe",
            owner_type="operation",
            owner_id=claim.operation_id,
            lease_generation=claim.lease_generation,
            lease_owner_id=claim.lease_owner_id,
            lease_token=claim.lease_token,
            lease_expires_at=claim.lease_expires_at,
            attempt_number=claim.attempt_count,
            request_id=_text(row["request_id"]),
            trace_id=_text(row["trace_id"]),
            current=current,
        )
        material = _SandboxSyncMaterial(
            claim=claim,
            policy=policy,
            fence=fence,
            endpoint_ciphertext=_text(row["endpoint_ciphertext"]),
            endpoint_hmac=_text(row["endpoint_hmac"]),
            endpoint_hmac_key_version=int(row["endpoint_hmac_key_version"]),
            auth_type=_text(row["auth_type"]),
            credential_id=(
                _text(row["mcp_server_credential_id"])
                if row["mcp_server_credential_id"] is not None
                else None
            ),
            credential_ciphertext=(
                _text(row["credential_ciphertext"])
                if row["credential_ciphertext"] is not None
                else None
            ),
            credential_revision=(
                int(row["credential_revision"]) if row["credential_revision"] is not None else None
            ),
        )
        connection.commit()
        return material
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, McpContractError, TypeError, ValueError) as exc:
        _rollback(connection)
        raise ApiError("mcp.unavailable") from exc
    finally:
        connection.close()


def _load_sync_connection_material(
    config: AppConfig, claim: McpSyncJobClaim
) -> McpConnectionMaterial:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        timestamp = _db_time(_transaction_now(connection, None))
        row = connection.execute(
            """
            SELECT server.endpoint_ciphertext,server.transport,
              operation.start_server_revision,operation.start_credential_revision,
              operation.candidate_credential_revision,operation.status AS operation_status,
              credential.mcp_server_credential_id,credential.auth_type,
              credential.credential_ciphertext,credential.status AS credential_status
            FROM mcp_server_sync_jobs job
            JOIN mcp_server_operations operation
              ON operation.tenant_id=job.tenant_id
             AND operation.mcp_server_operation_id=job.mcp_server_operation_id
            JOIN mcp_servers server ON server.tenant_id=job.tenant_id
             AND server.mcp_server_id=job.mcp_server_id
            JOIN mcp_server_credentials credential ON credential.tenant_id=server.tenant_id
             AND credential.mcp_server_id=server.mcp_server_id
             AND credential.credential_revision=coalesce(
               job.candidate_credential_revision,server.active_credential_revision)
            WHERE job.tenant_id=? AND job.mcp_server_sync_job_id=?
              AND job.status='processing' AND job.lease_generation=?
              AND job.lease_owner_id=? AND job.lease_token=?
              AND unixepoch(job.lease_expires_at)>unixepoch(?)
              AND unixepoch(job.deadline_at)>unixepoch(?)
            """,
            (
                claim.tenant_id,
                claim.job_id,
                claim.lease_generation,
                claim.lease_owner_id,
                claim.lease_token,
                timestamp,
                timestamp,
            ),
        ).fetchone()
        if row is None or row["operation_status"] != "processing":
            raise ApiError("mcp.state_conflict")
        if claim.candidate_credential_revision is not None:
            if row["credential_status"] != "candidate":
                raise ApiError("mcp.state_conflict")
        elif row["credential_status"] != "active":
            raise ApiError("mcp.state_conflict")
        endpoint, _ = decrypt_mcp_value_from_keyring(
            row["endpoint_ciphertext"],
            keyring=_encryption_keyring(config),
            tenant_id=claim.tenant_id,
            owner_id=claim.server_id,
            kind="endpoint",
        )
        bearer: bytes | None = None
        if row["auth_type"] == "bearer":
            bearer, _ = decrypt_mcp_value_from_keyring(
                row["credential_ciphertext"],
                keyring=_encryption_keyring(config),
                tenant_id=claim.tenant_id,
                owner_id=_text(row["mcp_server_credential_id"]),
                kind="bearer",
            )
        connection.commit()
        return McpConnectionMaterial(
            endpoint=endpoint.decode("utf-8", errors="strict"),
            bearer_token=(bearer.decode("utf-8", errors="strict") if bearer is not None else None),
            transport=_text(row["transport"]),  # type: ignore[arg-type]
        )
    except (ApiError, McpContractError):
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, UnicodeError, TypeError, ValueError) as exc:
        _rollback(connection)
        raise ApiError("mcp.unavailable") from exc
    finally:
        connection.close()


def _finalize_sync_success(
    config: AppConfig,
    claim: McpSyncJobClaim,
    result: McpDiscoveryResult,
    *,
    network: _NetworkFence | None = None,
    network_response: McpSandboxResponse | None = None,
) -> None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _transaction_now(connection, None)
        timestamp = _db_time(now)
        job = _require_sync_claim(connection, claim, timestamp=timestamp)
        operation = connection.execute(
            """
            SELECT operation.*,user.status AS actor_status,
              user.email AS actor_email,user.role AS actor_role
            FROM mcp_server_operations operation
            JOIN users user ON user.tenant_id=operation.tenant_id
             AND user.user_id=operation.actor_user_id
            WHERE operation.tenant_id=? AND operation.mcp_server_operation_id=?
            """,
            (claim.tenant_id, claim.operation_id),
        ).fetchone()
        server = _load_server(connection, claim.tenant_id, claim.server_id)
        if (
            operation is None
            or server is None
            or operation["status"] != "processing"
            or operation["actor_status"] != "active"
            or int(server["state_revision"]) != int(operation["start_server_revision"])
            or job["mcp_server_operation_id"] != claim.operation_id
        ):
            raise ApiError("mcp.state_conflict")
        if network is not None:
            if network_response is None or network_response.status != "succeeded":
                raise ApiError("mcp.state_conflict")
            _complete_network_success(
                connection,
                network,
                response=network_response,
                current=now,
            )
        server_before_summary = _server_audit_summary(server)

        if claim.candidate_credential_revision is not None:
            activated = connection.execute(
                """
                UPDATE mcp_server_credentials
                SET status='active',activated_at=?
                WHERE tenant_id=? AND mcp_server_id=? AND credential_revision=?
                  AND status='candidate' AND probe_operation_id=?
                """,
                (
                    timestamp,
                    claim.tenant_id,
                    claim.server_id,
                    claim.candidate_credential_revision,
                    claim.operation_id,
                ),
            ).rowcount
            if activated != 1:
                raise ApiError("mcp.state_conflict")

        tool_audits: tuple[_ToolRefreshAudit, ...] = ()
        if claim.job_type == "discover":
            tool_audits = _reconcile_discovered_tools(
                connection,
                claim=claim,
                actor_id=_text(operation["actor_user_id"]),
                result=result,
                timestamp=timestamp,
            )

        active_revision = (
            claim.candidate_credential_revision
            if claim.candidate_credential_revision is not None
            else server["active_credential_revision"]
        )
        server_changed = (
            active_revision != server["active_credential_revision"]
            or server["status"] != "active"
            or result.protocol_version != server["protocol_version"]
            or result.capabilities_sha256 != server["capabilities_sha256"]
            or bool(tool_audits)
        )
        if server_changed:
            changed = connection.execute(
                """
                UPDATE mcp_servers
                SET active_credential_revision=?,protocol_version=?,capabilities_sha256=?,
                    status='active',state_revision=state_revision+1,updated_by=?,updated_at=?
                WHERE tenant_id=? AND mcp_server_id=? AND state_revision=?
                """,
                (
                    active_revision,
                    result.protocol_version,
                    result.capabilities_sha256,
                    operation["actor_user_id"],
                    timestamp,
                    claim.tenant_id,
                    claim.server_id,
                    operation["start_server_revision"],
                ),
            ).rowcount
            if changed != 1:
                raise ApiError("mcp.state_conflict")

        finalized_job = connection.execute(
            """
            UPDATE mcp_server_sync_jobs
            SET status='succeeded',lease_generation=lease_generation+1,
                lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
                next_attempt_at=NULL,error_code=NULL,completed_at=?,updated_at=?
            WHERE tenant_id=? AND mcp_server_sync_job_id=? AND status='processing'
              AND lease_generation=? AND lease_owner_id=? AND lease_token=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
              AND unixepoch(deadline_at)>unixepoch(?)
            """,
            (
                timestamp,
                timestamp,
                claim.tenant_id,
                claim.job_id,
                claim.lease_generation,
                claim.lease_owner_id,
                claim.lease_token,
                timestamp,
                timestamp,
            ),
        ).rowcount
        finalized_operation = connection.execute(
            """
            UPDATE mcp_server_operations
            SET status='succeeded',error_code=NULL,completed_at=?,updated_at=?
            WHERE tenant_id=? AND mcp_server_operation_id=? AND status='processing'
            """,
            (timestamp, timestamp, claim.tenant_id, claim.operation_id),
        ).rowcount
        if finalized_job != 1 or finalized_operation != 1:
            raise ApiError("mcp.state_conflict")
        completed_operation = connection.execute(
            """
            SELECT * FROM mcp_server_operations
            WHERE tenant_id=? AND mcp_server_operation_id=?
            """,
            (claim.tenant_id, claim.operation_id),
        ).fetchone()
        if completed_operation is None:
            raise ApiError("mcp.state_conflict")
        completed_server = _load_server(connection, claim.tenant_id, claim.server_id)
        if completed_server is None:
            raise ApiError("mcp.state_conflict")
        server_audit = None
        server_action = {
            "refresh": "mcp.server_refreshed",
            "rotate": "mcp.server_credential_rotated",
        }.get(_text(operation["operation_type"]))
        if server_changed and server_action is not None:
            server_audit = _ServerRefreshAudit(
                action=server_action,
                before_summary=server_before_summary,
                after_summary=_server_audit_summary(completed_server),
            )
        _record_sync_audits(
            connection,
            operation_before=operation,
            operation_after=completed_operation,
            server_audit=server_audit,
            tool_audits=tool_audits,
            now=now,
        )
        connection.commit()
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, McpContractError, TypeError, ValueError) as exc:
        _rollback(connection)
        raise ApiError("mcp.unavailable") from exc
    finally:
        connection.close()


def _reconcile_discovered_tools(
    connection: sqlite3.Connection,
    *,
    claim: McpSyncJobClaim,
    actor_id: str,
    result: McpDiscoveryResult,
    timestamp: str,
) -> tuple[_ToolRefreshAudit, ...]:
    existing = {
        _text(row["remote_name"]): row
        for row in connection.execute(
            """
            SELECT * FROM mcp_tools
            WHERE tenant_id=? AND mcp_server_id=? ORDER BY remote_name
            """,
            (claim.tenant_id, claim.server_id),
        ).fetchall()
    }
    discovered_names: set[str] = set()
    audits: list[_ToolRefreshAudit] = []
    for tool in result.tools:
        discovered_names.add(tool.name)
        input_schema = tool.schema_bundle.input
        output_schema = tool.schema_bundle.output
        current = existing.get(tool.name)
        values = (
            tool.description,
            input_schema.original_json,
            input_schema.original_sha256,
            input_schema.effective_json,
            input_schema.effective_sha256,
            output_schema.original_json if output_schema is not None else None,
            output_schema.original_sha256 if output_schema is not None else None,
            output_schema.effective_json if output_schema is not None else None,
            output_schema.effective_sha256 if output_schema is not None else None,
            tool.annotations.canonical_json,
            tool.annotations.sha256,
        )
        if current is None:
            tool_id = f"mcpt_{uuid4().hex}"
            connection.execute(
                """
                INSERT INTO mcp_tools(
                  mcp_tool_id,tenant_id,mcp_server_id,remote_name,title,description,
                  input_schema_original_json,input_schema_original_sha256,
                  input_schema_effective_json,input_schema_effective_sha256,
                  output_schema_original_json,output_schema_original_sha256,
                  output_schema_effective_json,output_schema_effective_sha256,
                  annotations_json,annotations_sha256,schema_revision,policy_revision,
                  risk_level,approval_required,approval_expires_seconds,
                  required_permission_codes_json,result_redaction_pointers_json,status,
                  created_by,updated_by,created_at,updated_at
                ) VALUES(?,?,?,?,NULL,?,?,?,?,?,?,?,?,?,?,?,1,1,'standard',0,3600,
                         '[]','[]','review_required',?,?,?,?)
                """,
                (
                    tool_id,
                    claim.tenant_id,
                    claim.server_id,
                    tool.name,
                    *values,
                    actor_id,
                    actor_id,
                    timestamp,
                    timestamp,
                ),
            )
            audits.append(
                _ToolRefreshAudit(
                    tool_id=tool_id,
                    before_summary={},
                    after_summary=_tool_refresh_summary(
                        connection,
                        tenant_id=claim.tenant_id,
                        tool_id=tool_id,
                    ),
                )
            )
            continue
        frozen = (
            current["description"],
            current["input_schema_original_json"],
            current["input_schema_original_sha256"],
            current["input_schema_effective_json"],
            current["input_schema_effective_sha256"],
            current["output_schema_original_json"],
            current["output_schema_original_sha256"],
            current["output_schema_effective_json"],
            current["output_schema_effective_sha256"],
            current["annotations_json"],
            current["annotations_sha256"],
        )
        if frozen == values:
            touched = connection.execute(
                """
                UPDATE mcp_tools SET updated_by=?,updated_at=?
                WHERE tenant_id=? AND mcp_tool_id=? AND status=?
                  AND schema_revision=? AND policy_revision=? AND updated_at=?
                """,
                (
                    actor_id,
                    timestamp,
                    claim.tenant_id,
                    current["mcp_tool_id"],
                    current["status"],
                    current["schema_revision"],
                    current["policy_revision"],
                    current["updated_at"],
                ),
            ).rowcount
            if touched != 1:
                raise ApiError("mcp.state_conflict")
            continue
        if frozen != values:
            tool_id = _text(current["mcp_tool_id"])
            before_summary = _tool_refresh_summary(
                connection,
                tenant_id=claim.tenant_id,
                tool_id=tool_id,
            )
            updated = connection.execute(
                """
                UPDATE mcp_tools
                SET description=?,input_schema_original_json=?,
                    input_schema_original_sha256=?,input_schema_effective_json=?,
                    input_schema_effective_sha256=?,output_schema_original_json=?,
                    output_schema_original_sha256=?,output_schema_effective_json=?,
                    output_schema_effective_sha256=?,annotations_json=?,annotations_sha256=?,
                    schema_revision=schema_revision+1,policy_revision=policy_revision+1,
                    status='review_required',updated_by=?,updated_at=?
                WHERE tenant_id=? AND mcp_tool_id=? AND schema_revision=?
                  AND policy_revision=?
                """,
                (
                    *values,
                    actor_id,
                    timestamp,
                    claim.tenant_id,
                    tool_id,
                    current["schema_revision"],
                    current["policy_revision"],
                ),
            ).rowcount
            if updated != 1:
                raise ApiError("mcp.state_conflict")
            audits.append(
                _ToolRefreshAudit(
                    tool_id=tool_id,
                    before_summary=before_summary,
                    after_summary=_tool_refresh_summary(
                        connection,
                        tenant_id=claim.tenant_id,
                        tool_id=tool_id,
                    ),
                )
            )
    for remote_name, current in existing.items():
        if remote_name in discovered_names or current["status"] == "disabled":
            continue
        tool_id = _text(current["mcp_tool_id"])
        before_summary = _tool_refresh_summary(
            connection,
            tenant_id=claim.tenant_id,
            tool_id=tool_id,
        )
        disabled = connection.execute(
            """
            UPDATE mcp_tools SET status='disabled',updated_by=?,updated_at=?
            WHERE tenant_id=? AND mcp_tool_id=? AND status IN('active','review_required')
              AND schema_revision=? AND policy_revision=?
            """,
            (
                actor_id,
                timestamp,
                claim.tenant_id,
                tool_id,
                current["schema_revision"],
                current["policy_revision"],
            ),
        ).rowcount
        if disabled != 1:
            raise ApiError("mcp.state_conflict")
        audits.append(
            _ToolRefreshAudit(
                tool_id=tool_id,
                before_summary=before_summary,
                after_summary=_tool_refresh_summary(
                    connection,
                    tenant_id=claim.tenant_id,
                    tool_id=tool_id,
                ),
            )
        )
    return tuple(audits)


def _tool_refresh_summary(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    tool_id: str,
) -> dict[str, object]:
    row = _load_tool_with_counts(connection, tenant_id, tool_id)
    if row is None:
        raise ApiError("mcp.state_conflict")
    return _tool_audit_summary(row)


def _operation_audit_summary(row: sqlite3.Row) -> dict[str, object]:
    return {
        "mcp_server_operation_id": _text(row["mcp_server_operation_id"]),
        "mcp_server_id": _text(row["mcp_server_id"]),
        "operation_type": _text(row["operation_type"]),
        "status": _text(row["status"]),
        "start_server_revision": int(row["start_server_revision"]),
        "candidate_credential_revision": (
            int(row["candidate_credential_revision"])
            if row["candidate_credential_revision"] is not None
            else None
        ),
        "error_code": (_text(row["error_code"]) if row["error_code"] is not None else None),
    }


def _record_sync_audits(
    connection: sqlite3.Connection,
    *,
    operation_before: sqlite3.Row,
    operation_after: sqlite3.Row,
    server_audit: _ServerRefreshAudit | None,
    tool_audits: tuple[_ToolRefreshAudit, ...],
    now: datetime,
) -> None:
    actor = AuditActor(
        tenant_id=_text(operation_before["tenant_id"]),
        user_id=_text(operation_before["actor_user_id"]),
        email=_text(operation_before["actor_email"]),
        role=_text(operation_before["actor_role"]),
    )
    request_meta = AuditRequestMeta(None, None)
    request_id = _text(operation_before["request_id"])
    trace_id = _text(operation_before["trace_id"])
    if server_audit is not None:
        record = record_audit_log_for_actor(
            connection,
            actor,
            action=server_audit.action,
            object_type="mcp_server",
            object_id=_text(operation_before["mcp_server_id"]),
            object_name=None,
            before_summary=server_audit.before_summary,
            after_summary=server_audit.after_summary,
            request_meta=request_meta,
            request_id=request_id,
            trace_id=trace_id,
            created_at=now,
        )
        if record is None:
            raise ApiError("mcp.state_conflict")
    for audit in tool_audits:
        record = record_audit_log_for_actor(
            connection,
            actor,
            action="mcp.tool_refreshed",
            object_type="mcp_tool",
            object_id=audit.tool_id,
            object_name=None,
            before_summary=audit.before_summary,
            after_summary=audit.after_summary,
            request_meta=request_meta,
            request_id=request_id,
            trace_id=trace_id,
            created_at=now,
        )
        if record is None:
            raise ApiError("mcp.state_conflict")
    operation_record = record_audit_log_for_actor(
        connection,
        actor,
        action="mcp.server_operation_completed",
        object_type="mcp_server_operation",
        object_id=_text(operation_before["mcp_server_operation_id"]),
        object_name=None,
        before_summary=_operation_audit_summary(operation_before),
        after_summary=_operation_audit_summary(operation_after),
        request_meta=request_meta,
        request_id=request_id,
        trace_id=trace_id,
        created_at=now,
    )
    if operation_record is None:
        raise ApiError("mcp.state_conflict")


def _finalize_sync_failure(
    config: AppConfig,
    claim: McpSyncJobClaim,
    *,
    error_code: str,
    network: _NetworkFence | None = None,
    network_response: McpSandboxResponse | None = None,
) -> None:
    allowed = {
        "mcp.executor_disabled",
        "mcp.sync_failed",
        "mcp.credential_invalid",
        "mcp.state_conflict",
        "mcp.unavailable",
        "mcp.network_blocked",
        "mcp.sandbox_unavailable",
    }
    code = error_code if error_code in allowed else "mcp.unavailable"
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _transaction_now(connection, None)
        timestamp = _db_time(now)
        _require_sync_claim(
            connection,
            claim,
            timestamp=timestamp,
            allow_expired_deadline=True,
        )
        operation = connection.execute(
            """
            SELECT operation.*,user.status AS actor_status,
              user.email AS actor_email,user.role AS actor_role
            FROM mcp_server_operations operation
            JOIN users user ON user.tenant_id=operation.tenant_id
             AND user.user_id=operation.actor_user_id
            WHERE operation.tenant_id=? AND operation.mcp_server_operation_id=?
            """,
            (claim.tenant_id, claim.operation_id),
        ).fetchone()
        server = connection.execute(
            "SELECT * FROM mcp_servers WHERE tenant_id=? AND mcp_server_id=?",
            (claim.tenant_id, claim.server_id),
        ).fetchone()
        if operation is None or server is None or operation["status"] != "processing":
            raise ApiError("mcp.state_conflict")
        network_decision_id: str | None = None
        if network is not None:
            if network_response is not None and network_response.status == "blocked":
                network_decision_id = _record_network_block(
                    connection,
                    network,
                    response=network_response,
                    current=now,
                )
            elif network_response is not None and network_response.status == "succeeded":
                _complete_network_success(
                    connection,
                    network,
                    response=network_response,
                    current=now,
                )
            else:
                _complete_network_failure(
                    connection,
                    network,
                    error_code=code,
                    availability_reason=_network_failure_reason(
                        network_response,
                        error_code=code,
                    ),
                    current=now,
                )
        if claim.candidate_credential_revision is not None:
            connection.execute(
                """
                UPDATE mcp_server_credentials SET status='rejected',rejected_at=?
                WHERE tenant_id=? AND mcp_server_id=? AND credential_revision=?
                  AND status='candidate' AND probe_operation_id=?
                """,
                (
                    timestamp,
                    claim.tenant_id,
                    claim.server_id,
                    claim.candidate_credential_revision,
                    claim.operation_id,
                ),
            )
        if (
            operation["operation_type"] == "register"
            and server["active_credential_revision"] is None
            and operation["actor_status"] == "active"
            and int(server["state_revision"]) == int(operation["start_server_revision"])
        ):
            connection.execute(
                """
                UPDATE mcp_servers SET status='sync_failed',state_revision=state_revision+1,
                    updated_by=?,updated_at=?
                WHERE tenant_id=? AND mcp_server_id=? AND state_revision=?
                  AND status IN('pending_sync','sync_failed')
                """,
                (
                    operation["actor_user_id"],
                    timestamp,
                    claim.tenant_id,
                    claim.server_id,
                    operation["start_server_revision"],
                ),
            )
        job_changed = connection.execute(
            """
            UPDATE mcp_server_sync_jobs
            SET status='failed',lease_generation=lease_generation+1,
                lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
                next_attempt_at=NULL,error_code=?,completed_at=?,updated_at=?
            WHERE tenant_id=? AND mcp_server_sync_job_id=? AND status='processing'
              AND lease_generation=? AND lease_owner_id=? AND lease_token=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                code,
                timestamp,
                timestamp,
                claim.tenant_id,
                claim.job_id,
                claim.lease_generation,
                claim.lease_owner_id,
                claim.lease_token,
                timestamp,
            ),
        ).rowcount
        operation_changed = connection.execute(
            """
            UPDATE mcp_server_operations SET status='failed',error_code=?,
                completed_at=?,updated_at=?
            WHERE tenant_id=? AND mcp_server_operation_id=? AND status='processing'
            """,
            (code, timestamp, timestamp, claim.tenant_id, claim.operation_id),
        ).rowcount
        if job_changed != 1 or operation_changed != 1:
            raise ApiError("mcp.state_conflict")
        if network_decision_id is not None:
            _create_network_event_outbox(
                connection,
                network,
                decision_id=network_decision_id,
                reason_code=_network_block_reason(network_response),
                address_count=network_response.address_count,
                attempt_number=claim.attempt_count,
                current=now,
            )
        completed_operation = connection.execute(
            """
            SELECT * FROM mcp_server_operations
            WHERE tenant_id=? AND mcp_server_operation_id=?
            """,
            (claim.tenant_id, claim.operation_id),
        ).fetchone()
        if completed_operation is None:
            raise ApiError("mcp.state_conflict")
        _record_sync_audits(
            connection,
            operation_before=operation,
            operation_after=completed_operation,
            server_audit=None,
            tool_audits=(),
            now=now,
        )
        connection.commit()
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, TypeError, ValueError) as exc:
        _rollback(connection)
        raise ApiError("mcp.unavailable") from exc
    finally:
        connection.close()


def _recover_expired_calling(
    config: AppConfig,
    tenant_lease: McpSchedulerTenantLease,
    *,
    now: datetime | None,
) -> bool:
    """Converge an expired call intent, or an exhausted expired probe, without retrying it."""

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        timestamp = _db_time(current)
        _require_tenant_lease(connection, tenant_lease, timestamp)
        row = connection.execute(
            """
            SELECT * FROM mcp_invocations
            WHERE tenant_id=? AND status='calling'
              AND unixepoch(lease_expires_at)<=unixepoch(?)
            ORDER BY lease_expires_at,mcp_invocation_id LIMIT 1
            """,
            (tenant_lease.tenant_id, timestamp),
        ).fetchone()
        if row is not None:
            attempt = connection.execute(
                """
                SELECT * FROM mcp_invocation_attempts
                WHERE tenant_id=? AND mcp_invocation_id=? AND attempt_kind='call'
                  AND attempt_number=? AND lease_generation=? AND status='processing'
                """,
                (
                    row["tenant_id"],
                    row["mcp_invocation_id"],
                    row["call_attempt_count"],
                    row["lease_generation"],
                ),
            ).fetchone()
            if attempt is None:
                raise ApiError("mcp.state_conflict")
            duration = _duration_ms(row["started_at"], current)
            _finish_attempt(
                connection,
                attempt,
                status="outcome_unknown",
                error_code="mcp.outcome_unknown",
                request_bytes=int(row["request_bytes"] or row["arguments_bytes"]),
                response_bytes=0,
                timestamp=timestamp,
                duration_ms=duration,
            )
            changed = connection.execute(
                """
                UPDATE mcp_invocations
                SET status='outcome_unknown',state_revision=state_revision+1,
                    execution_status='unknown',tool_call_count=NULL,cost_status='unknown',
                    cost_micros=NULL,error_code='mcp.outcome_unknown',response_bytes=0,
                    duration_ms=?,lease_generation=lease_generation+1,
                    lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
                    next_attempt_at=NULL,completed_at=?,updated_at=?
                WHERE tenant_id=? AND mcp_invocation_id=? AND status='calling'
                  AND state_revision=? AND lease_generation=?
                  AND lease_owner_id=? AND lease_token=?
                """,
                (
                    duration,
                    timestamp,
                    timestamp,
                    row["tenant_id"],
                    row["mcp_invocation_id"],
                    row["state_revision"],
                    row["lease_generation"],
                    row["lease_owner_id"],
                    row["lease_token"],
                ),
            ).rowcount
            if changed != 1:
                raise ApiError("mcp.state_conflict")
            terminal = _load_invocation_row(
                connection, _text(row["tenant_id"]), _text(row["mcp_invocation_id"])
            )
            create_mcp_invocation_event_in_transaction(connection, terminal)
            connection.commit()
            return True

        exhausted = connection.execute(
            """
            SELECT * FROM mcp_invocations
            WHERE tenant_id=? AND status='probing' AND probe_attempt_count>=?
              AND unixepoch(lease_expires_at)<=unixepoch(?)
            ORDER BY lease_expires_at,mcp_invocation_id LIMIT 1
            """,
            (tenant_lease.tenant_id, _MAX_PROBE_ATTEMPTS, timestamp),
        ).fetchone()
        if exhausted is None:
            connection.commit()
            return False
        attempt = _load_processing_attempt(connection, exhausted, kind="probe")
        duration = _duration_ms(exhausted["started_at"], current)
        _finish_attempt(
            connection,
            attempt,
            status="failed",
            error_code="mcp.server_unavailable",
            request_bytes=attempt["request_bytes"],
            response_bytes=attempt["response_bytes"],
            timestamp=timestamp,
            duration_ms=_duration_ms(attempt["started_at"], current),
        )
        _terminalize_without_call(
            connection,
            exhausted,
            status="failed",
            error_code="mcp.server_unavailable",
            timestamp=timestamp,
            duration_ms=duration,
        )
        connection.commit()
        return True
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, TypeError, ValueError) as exc:
        _rollback(connection)
        raise ApiError("mcp.unavailable") from exc
    finally:
        connection.close()


def _claim_next_invocation(
    config: AppConfig,
    tenant_lease: McpSchedulerTenantLease,
    *,
    worker_id: str,
    now: datetime | None,
) -> McpInvocationClaim | None:
    owner = _worker_id(worker_id)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        timestamp = _db_time(current)
        _require_tenant_lease(connection, tenant_lease, timestamp)
        row = connection.execute(
            """
            SELECT * FROM mcp_invocations
            WHERE tenant_id=? AND (
              (status='queued' AND unixepoch(next_attempt_at)<=unixepoch(?))
              OR (status='probing' AND probe_attempt_count<?
                  AND unixepoch(lease_expires_at)<=unixepoch(?)))
            ORDER BY CASE status WHEN 'probing' THEN lease_expires_at ELSE next_attempt_at END,
              created_at,mcp_invocation_id LIMIT 1
            """,
            (tenant_lease.tenant_id, timestamp, _MAX_PROBE_ATTEMPTS, timestamp),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        if row["status"] == "probing":
            previous = _load_processing_attempt(connection, row, kind="probe")
            _finish_attempt(
                connection,
                previous,
                status="failed",
                error_code="mcp.server_unavailable",
                request_bytes=previous["request_bytes"],
                response_bytes=previous["response_bytes"],
                timestamp=timestamp,
                duration_ms=_duration_ms(previous["started_at"], current),
            )
        token = secrets.token_hex(32)
        expires = current + timedelta(seconds=_invocation_lease_seconds(config))
        next_attempt = int(row["probe_attempt_count"]) + 1
        changed = connection.execute(
            """
            UPDATE mcp_invocations
            SET status='probing',state_revision=state_revision+1,
                probe_attempt_count=probe_attempt_count+1,
                lease_generation=lease_generation+1,lease_owner_id=?,lease_token=?,
                lease_expires_at=?,next_attempt_at=NULL,started_at=coalesce(started_at,?),
                updated_at=?
            WHERE tenant_id=? AND mcp_invocation_id=? AND status=?
              AND state_revision=? AND lease_generation=?
            """,
            (
                owner,
                token,
                _db_time(expires),
                timestamp,
                timestamp,
                row["tenant_id"],
                row["mcp_invocation_id"],
                row["status"],
                row["state_revision"],
                row["lease_generation"],
            ),
        ).rowcount
        if changed != 1:
            raise ApiError("mcp.state_conflict")
        attempt_id = f"mcpa_{uuid4().hex}"
        connection.execute(
            """
            INSERT INTO mcp_invocation_attempts(
              mcp_invocation_attempt_id,tenant_id,mcp_invocation_id,attempt_kind,
              attempt_number,status,lease_generation,error_code,request_bytes,response_bytes,
              started_at,call_started_at,completed_at,duration_ms
            ) VALUES(?,?,?,'probe',?,'processing',?,NULL,NULL,NULL,?,NULL,NULL,NULL)
            """,
            (
                attempt_id,
                row["tenant_id"],
                row["mcp_invocation_id"],
                next_attempt,
                int(row["lease_generation"]) + 1,
                timestamp,
            ),
        )
        claim = McpInvocationClaim(
            tenant_id=_text(row["tenant_id"]),
            invocation_id=_text(row["mcp_invocation_id"]),
            server_id=_text(row["mcp_server_id"]),
            tool_id=_text(row["mcp_tool_id"]),
            attempt_id=attempt_id,
            attempt_number=next_attempt,
            lease_generation=int(row["lease_generation"]) + 1,
            lease_owner_id=owner,
            lease_token=token,
            lease_expires_at=expires,
        )
        connection.commit()
        return claim
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, TypeError, ValueError) as exc:
        _rollback(connection)
        raise ApiError("mcp.unavailable") from exc
    finally:
        connection.close()


def _process_invocation(
    config: AppConfig,
    claim: McpInvocationClaim,
    *,
    stop_event: threading.Event | None = None,
) -> None:
    if _sandbox_http(config):
        _process_invocation_sandbox(config, claim, stop_event=stop_event)
        return
    if stop_event is not None and stop_event.is_set():
        return
    material = _prepare_invocation(config, claim)
    if material is None:
        return
    if stop_event is not None and stop_event.is_set():
        return
    try:
        probe = probe_mcp_tool(
            config,
            McpProbeRequest(connection=material.connection, tool_name=material.remote_name),
        )
    except McpExecutorError as exc:
        _finalize_probe_failure(
            config,
            claim,
            error_code=exc.code,
            input_dlp=material.input_dlp,
        )
        return
    except Exception:
        _finalize_probe_failure(
            config,
            claim,
            error_code="mcp.unavailable",
            input_dlp=material.input_dlp,
        )
        return
    if stop_event is not None and stop_event.is_set():
        return
    call_material = _begin_call_after_probe(config, material, probe)
    if call_material is None:
        return
    try:
        result = call_mcp_tool(
            config,
            McpCallRequest(
                connection=call_material.connection,
                tool_name=call_material.remote_name,
                arguments=call_material.arguments,
            ),
        )
    except McpExecutorError as exc:
        code = exc.code
        if code in {"mcp.outcome_unknown", "mcp.server_unavailable"}:
            _finalize_call_unknown(config, call_material)
        else:
            _finalize_call_failure(
                config,
                call_material,
                error_code=(
                    code if code in {"mcp.result_invalid", "mcp.unavailable"} else "mcp.unavailable"
                ),
            )
        return
    except Exception:
        _finalize_call_unknown(config, call_material)
        return
    _finalize_call_result(config, call_material, result)


def _process_invocation_sandbox(
    config: AppConfig,
    claim: McpInvocationClaim,
    *,
    stop_event: threading.Event | None = None,
) -> None:
    if stop_event is not None and stop_event.is_set():
        return
    material: _SandboxInvocationMaterial | None = None
    plan: _SandboxCallPlan | None = None
    response: McpSandboxResponse | None = None
    try:
        material = _prepare_sandbox_invocation(config, claim)
        if material is None or (stop_event is not None and stop_event.is_set()):
            return
        client = McpSandboxHttpClient(config)
        response = client.dispatch(_sandbox_probe_dispatch(material))
        if response.status == "blocked":
            _finalize_probe_network_blocked(config, material, response)
            return
        if response.status == "failed":
            _finalize_probe_failure(
                config,
                claim,
                error_code=response.failure_code or "mcp.sandbox_unavailable",
                input_dlp=material.input_dlp,
                network=material.fence,
                network_response=response,
            )
            return
        probe = parse_sandbox_probe(
            response,
            config=config,
            tool_name=material.remote_name,
        )
        plan = _prepare_sandbox_call_plan(config, material, probe, response)
        if plan is None or (stop_event is not None and stop_event.is_set()):
            return
        response = client.dispatch(_sandbox_authorize_dispatch(plan))
        if response.status == "blocked":
            _finalize_authorize_network_blocked(
                config,
                plan,
                response,
            )
            return
        if response.status == "failed":
            _finalize_probe_failure(
                config,
                claim,
                error_code=response.failure_code or "mcp.sandbox_unavailable",
                input_dlp=material.input_dlp,
                network=plan.fence,
                network_response=response,
            )
            return
        permit = parse_sandbox_permit(
            response,
            deadline_at=plan.call_expires_at,
        )
        call_material = _commit_sandbox_call_intent(config, plan, permit)
        if call_material is None:
            return
        if stop_event is not None and stop_event.is_set():
            return
        try:
            call_response = client.dispatch(call_material.dispatch)
        except McpSandboxClientError:
            _finalize_call_unknown(
                config,
                call_material.legacy,
                network=call_material.fence,
            )
            return
        if call_response.status != "succeeded":
            _finalize_call_unknown(
                config,
                call_material.legacy,
                network=call_material.fence,
                network_response=call_response,
            )
            return
        try:
            result = parse_sandbox_call(call_response, config=config)
        except McpExecutorError:
            _finalize_call_failure(
                config,
                call_material.legacy,
                error_code="mcp.result_invalid",
                network=call_material.fence,
                network_response=call_response,
            )
            return
        _finalize_call_result(
            config,
            call_material.legacy,
            result,
            network=call_material.fence,
            network_response=call_response,
        )
    except McpSandboxClientError:
        if material is not None:
            _finalize_probe_failure(
                config,
                claim,
                error_code="mcp.sandbox_unavailable",
                input_dlp=material.input_dlp,
                network=plan.fence if plan is not None else material.fence,
                network_response=response,
            )
        else:
            _finalize_probe_failure(config, claim, error_code="mcp.sandbox_unavailable")
    except McpExecutorError as exc:
        _finalize_probe_failure(
            config,
            claim,
            error_code=exc.code,
            input_dlp=material.input_dlp if material is not None else None,
            network=(
                plan.fence if plan is not None else material.fence if material is not None else None
            ),
            network_response=response,
        )
    except ApiError as exc:
        _finalize_probe_failure(
            config,
            claim,
            error_code=(
                exc.code
                if exc.code in {"mcp.server_unavailable", "mcp.state_conflict", "mcp.unavailable"}
                else "mcp.unavailable"
            ),
            input_dlp=material.input_dlp if material is not None else None,
            network=(
                plan.fence if plan is not None else material.fence if material is not None else None
            ),
            network_response=response,
        )


def _prepare_sandbox_invocation(
    config: AppConfig,
    claim: McpInvocationClaim,
) -> _SandboxInvocationMaterial | None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, None)
        invocation = _require_invocation_claim(
            connection,
            claim,
            status="probing",
            timestamp=_db_time(current),
        )
        tool = connection.execute(
            """
            SELECT tool.*,server.status AS server_status,
              server.state_revision AS current_server_revision,
              server.active_credential_revision AS current_credential_revision,
              server.endpoint_ciphertext,server.endpoint_hmac,
              server.endpoint_hmac_key_version,server.transport,
              credential.mcp_server_credential_id,credential.auth_type,
              credential.credential_ciphertext,credential.status AS credential_status,
              binding.mcp_network_policy_id,binding.policy_revision AS bound_revision,
              binding.policy_fingerprint AS bound_fingerprint,
              binding.status AS bound_status,binding.egress_mode AS bound_egress_mode,
              policy.current_revision,policy.current_fingerprint,
              policy.status AS network_status,revision.egress_mode,
              revision.connect_timeout_seconds,revision.probe_timeout_seconds,
              revision.call_timeout_seconds,revision.failure_threshold,revision.open_seconds
            FROM mcp_tools tool
            JOIN mcp_servers server ON server.tenant_id=tool.tenant_id
             AND server.mcp_server_id=tool.mcp_server_id
            JOIN mcp_invocation_network_bindings binding
              ON binding.tenant_id=tool.tenant_id
             AND binding.mcp_invocation_id=?
             AND binding.mcp_server_id=server.mcp_server_id
             AND binding.mcp_tool_id=tool.mcp_tool_id
            JOIN mcp_network_policies policy ON policy.tenant_id=server.tenant_id
             AND policy.mcp_server_id=server.mcp_server_id
            JOIN mcp_network_policy_revisions revision
              ON revision.tenant_id=policy.tenant_id
             AND revision.mcp_network_policy_id=policy.mcp_network_policy_id
             AND revision.policy_revision=policy.current_revision
            LEFT JOIN mcp_server_credentials credential ON credential.tenant_id=server.tenant_id
             AND credential.mcp_server_id=server.mcp_server_id
             AND credential.credential_revision=server.active_credential_revision
            WHERE tool.tenant_id=? AND tool.mcp_tool_id=?
            """,
            (claim.invocation_id, claim.tenant_id, claim.tool_id),
        ).fetchone()
        failure = _current_authorization_failure(connection, config, invocation, tool)
        if failure is None:
            required = tuple(_json_string_list(tool["required_permission_codes_json"]))
            try:
                require_actor_transaction_permissions(
                    connection,
                    tenant_id=claim.tenant_id,
                    user_id=_text(invocation["requested_by"]),
                    permission_codes=tuple(dict.fromkeys(("mcp.invoke", *required))),
                    unavailable_error_code="mcp.unavailable",
                )
            except ApiError as exc:
                failure = (
                    "mcp.authorization_revoked"
                    if exc.code == "auth.permission_denied"
                    else "mcp.unavailable"
                )
        try:
            arguments_bytes, _ = decrypt_mcp_value_from_keyring(
                invocation["arguments_ciphertext"],
                keyring=_encryption_keyring(config),
                tenant_id=claim.tenant_id,
                owner_id=claim.invocation_id,
                kind="arguments",
            )
            arguments = json.loads(arguments_bytes)
            if type(arguments) is not dict:
                raise ValueError
        except (McpContractError, UnicodeError, TypeError, ValueError):
            failure = "mcp.unavailable"
            arguments = {}
        policy_disabled = False
        if tool is None or (
            tool["mcp_network_policy_id"] is None
            or tool["bound_revision"] != tool["current_revision"]
            or tool["bound_fingerprint"] != tool["current_fingerprint"]
            or tool["bound_status"] != tool["network_status"]
            or tool["bound_egress_mode"] != tool["egress_mode"]
        ):
            failure = "mcp.authorization_revoked"
        elif tool["network_status"] == "disabled":
            policy_disabled = True
        elif tool["network_status"] != "active":
            failure = "mcp.authorization_revoked"
        if failure is None:
            dlp = _scan_dlp(
                connection,
                config,
                tenant_id=claim.tenant_id,
                invocation_id=claim.invocation_id,
                value=arguments,
                scope="input",
            )
            if dlp.status == "blocked":
                _close_probe_and_terminalize(
                    connection,
                    invocation,
                    claim,
                    status="blocked",
                    error_code="mcp.arguments_blocked",
                    attempt_error_code="mcp.arguments_blocked",
                    current=current,
                    input_dlp=dlp,
                )
                connection.commit()
                return None
            if dlp.status == "unavailable":
                failure = "mcp.unavailable"
        else:
            dlp = None
        if failure is not None or dlp is None:
            code = failure or "mcp.unavailable"
            _close_probe_and_terminalize(
                connection,
                invocation,
                claim,
                status="failed",
                error_code=code,
                attempt_error_code=code,
                current=current,
            )
            connection.commit()
            return None
        policy = _network_policy_from_row(tool, config=config)
        if policy_disabled:
            fence = _start_network_fence(
                connection,
                tenant_id=claim.tenant_id,
                server_id=claim.server_id,
                policy_row=tool,
                phase="probe",
                owner_type="invocation",
                owner_id=claim.invocation_id,
                lease_generation=claim.lease_generation,
                lease_owner_id=claim.lease_owner_id,
                lease_token=claim.lease_token,
                lease_expires_at=claim.lease_expires_at,
                attempt_number=claim.attempt_number,
                request_id=_text(invocation["request_id"]),
                trace_id=_text(invocation["trace_id"]),
                current=current,
                allow_disabled=True,
            )
            response = McpSandboxResponse(
                status="blocked",
                failure_code="mcp.network_blocked",
                dispatch_id=f"local-policy-disabled-{claim.invocation_id}",
                address_count=0,
                duration_ms=0,
                request_bytes=0,
                response_bytes=0,
                payload={"reason_code": "policy_disabled"},
            )
            decision_id = _record_network_block(
                connection,
                fence,
                response=response,
                current=current,
            )
            _close_probe_and_terminalize(
                connection,
                invocation,
                claim,
                status="blocked",
                error_code="mcp.network_blocked",
                attempt_error_code="mcp.network_blocked",
                current=current,
                request_bytes=0,
                response_bytes=0,
                input_dlp=dlp,
            )
            _create_network_event_outbox(
                connection,
                fence,
                decision_id=decision_id,
                reason_code="policy_disabled",
                address_count=0,
                attempt_number=claim.attempt_number,
                current=current,
            )
            connection.commit()
            return None
        fence = _start_network_fence(
            connection,
            tenant_id=claim.tenant_id,
            server_id=claim.server_id,
            policy_row=tool,
            phase="probe",
            owner_type="invocation",
            owner_id=claim.invocation_id,
            lease_generation=claim.lease_generation,
            lease_owner_id=claim.lease_owner_id,
            lease_token=claim.lease_token,
            lease_expires_at=claim.lease_expires_at,
            attempt_number=claim.attempt_number,
            request_id=_text(invocation["request_id"]),
            trace_id=_text(invocation["trace_id"]),
            current=current,
        )
        material = _SandboxInvocationMaterial(
            claim=claim,
            policy=policy,
            fence=fence,
            endpoint_ciphertext=_text(tool["endpoint_ciphertext"]),
            endpoint_hmac=_text(tool["endpoint_hmac"]),
            endpoint_hmac_key_version=int(tool["endpoint_hmac_key_version"]),
            auth_type=_text(tool["auth_type"]),
            credential_id=(
                _text(tool["mcp_server_credential_id"])
                if tool["auth_type"] == "bearer" and tool["mcp_server_credential_id"] is not None
                else None
            ),
            credential_ciphertext=(
                _text(tool["credential_ciphertext"])
                if tool["auth_type"] == "bearer" and tool["credential_ciphertext"] is not None
                else None
            ),
            credential_revision=int(invocation["credential_revision"]),
            arguments=arguments,
            arguments_ciphertext=_text(invocation["arguments_ciphertext"]),
            arguments_hmac=_text(invocation["arguments_hmac"]),
            arguments_hmac_key_version=int(invocation["arguments_hmac_key_version"]),
            input_dlp=dlp,
            remote_name=_text(tool["remote_name"]),
        )
        connection.commit()
        return material
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, McpContractError, UnicodeError, TypeError, ValueError) as exc:
        _rollback(connection)
        raise ApiError("mcp.unavailable") from exc
    finally:
        connection.close()


def _sandbox_probe_dispatch(material: _SandboxInvocationMaterial) -> McpSandboxDispatch:
    claim = material.claim
    return McpSandboxDispatch(
        action="probe",
        tenant_id=claim.tenant_id,
        server_id=claim.server_id,
        operation_id=None,
        invocation_id=claim.invocation_id,
        lease_generation=claim.lease_generation,
        lease_owner_id=claim.lease_owner_id,
        lease_token=claim.lease_token,
        policy=material.policy,
        policy_revision=material.fence.policy_revision,
        policy_fingerprint=material.fence.policy_fingerprint,
        deadline_at=claim.lease_expires_at,
        request_id=material.fence.request_id,
        endpoint_ciphertext=material.endpoint_ciphertext,
        endpoint_hmac=material.endpoint_hmac,
        endpoint_hmac_key_version=material.endpoint_hmac_key_version,
        auth_type=material.auth_type,
        credential_id=material.credential_id,
        credential_ciphertext=material.credential_ciphertext,
        credential_revision=material.credential_revision,
        tool_name=material.remote_name,
        arguments_hmac=material.arguments_hmac,
        arguments_hmac_key_version=material.arguments_hmac_key_version,
    )


def _prepare_sandbox_call_plan(
    config: AppConfig,
    material: _SandboxInvocationMaterial,
    probe: McpProbeResult,
    probe_response: McpSandboxResponse,
) -> _SandboxCallPlan | None:
    claim = material.claim
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, None)
        timestamp = _db_time(current)
        invocation = _require_invocation_claim(
            connection,
            claim,
            status="probing",
            timestamp=timestamp,
        )
        tool = connection.execute(
            """
            SELECT tool.*,server.status AS server_status,
              server.state_revision AS current_server_revision,
              server.active_credential_revision AS current_credential_revision,
              server.protocol_version AS current_protocol_version,
              server.capabilities_sha256 AS current_capabilities_sha256,
              credential.status AS credential_status,
              policy.mcp_network_policy_id,policy.current_revision,
              policy.current_fingerprint,policy.status AS network_status,
              revision.egress_mode,revision.connect_timeout_seconds,
              revision.probe_timeout_seconds,revision.call_timeout_seconds,
              revision.failure_threshold,revision.open_seconds
            FROM mcp_tools tool
            JOIN mcp_servers server ON server.tenant_id=tool.tenant_id
             AND server.mcp_server_id=tool.mcp_server_id
            JOIN mcp_network_policies policy ON policy.tenant_id=server.tenant_id
             AND policy.mcp_server_id=server.mcp_server_id
            JOIN mcp_network_policy_revisions revision
              ON revision.tenant_id=policy.tenant_id
             AND revision.mcp_network_policy_id=policy.mcp_network_policy_id
             AND revision.policy_revision=policy.current_revision
            LEFT JOIN mcp_server_credentials credential ON credential.tenant_id=server.tenant_id
             AND credential.mcp_server_id=server.mcp_server_id
             AND credential.credential_revision=server.active_credential_revision
            WHERE tool.tenant_id=? AND tool.mcp_tool_id=?
            """,
            (claim.tenant_id, claim.tool_id),
        ).fetchone()
        failure = _current_authorization_failure(connection, config, invocation, tool)
        if failure is None:
            required = tuple(_json_string_list(tool["required_permission_codes_json"]))
            try:
                require_actor_transaction_permissions(
                    connection,
                    tenant_id=claim.tenant_id,
                    user_id=_text(invocation["requested_by"]),
                    permission_codes=tuple(dict.fromkeys(("mcp.invoke", *required))),
                    unavailable_error_code="mcp.unavailable",
                )
            except ApiError as exc:
                failure = (
                    "mcp.authorization_revoked"
                    if exc.code == "auth.permission_denied"
                    else "mcp.unavailable"
                )
        if (
            tool is None
            or tool["network_status"] != "active"
            or tool["mcp_network_policy_id"] != material.fence.policy_id
            or int(tool["current_revision"]) != material.fence.policy_revision
            or tool["current_fingerprint"] != material.fence.policy_fingerprint
        ):
            failure = "mcp.authorization_revoked"
        if failure is None:
            dlp = _scan_dlp(
                connection,
                config,
                tenant_id=claim.tenant_id,
                invocation_id=claim.invocation_id,
                value=material.arguments,
                scope="input",
            )
            if dlp.status == "blocked":
                _close_probe_and_terminalize(
                    connection,
                    invocation,
                    claim,
                    status="blocked",
                    error_code="mcp.arguments_blocked",
                    attempt_error_code="mcp.arguments_blocked",
                    current=current,
                    request_bytes=probe.request_bytes,
                    response_bytes=probe.response_bytes,
                    input_dlp=dlp,
                )
                connection.commit()
                return None
            if dlp.status == "unavailable":
                failure = "mcp.unavailable"
        else:
            dlp = material.input_dlp
        if failure is not None:
            _close_probe_and_terminalize(
                connection,
                invocation,
                claim,
                status="failed",
                error_code=failure,
                attempt_error_code=failure,
                current=current,
                request_bytes=probe.request_bytes,
                response_bytes=probe.response_bytes,
                input_dlp=dlp,
            )
            connection.commit()
            return None
        if _probe_has_drift(tool=tool, probe=probe):
            _close_probe_and_terminalize(
                connection,
                invocation,
                claim,
                status="failed",
                error_code="mcp.state_conflict",
                attempt_error_code="mcp.state_conflict",
                current=current,
                request_bytes=probe.request_bytes,
                response_bytes=probe.response_bytes,
                input_dlp=dlp,
            )
            connection.commit()
            return None
        _complete_network_success(
            connection,
            material.fence,
            response=probe_response,
            current=current,
        )
        call_generation = claim.lease_generation + 1
        call_token = secrets.token_hex(32)
        call_expires = current + timedelta(seconds=_invocation_lease_seconds(config))
        call_fence = _start_network_fence(
            connection,
            tenant_id=claim.tenant_id,
            server_id=claim.server_id,
            policy_row=tool,
            phase="call_preflight",
            owner_type="invocation",
            owner_id=claim.invocation_id,
            lease_generation=call_generation,
            lease_owner_id=claim.lease_owner_id,
            lease_token=call_token,
            lease_expires_at=call_expires,
            attempt_number=claim.attempt_number,
            request_id=_text(invocation["request_id"]),
            trace_id=_text(invocation["trace_id"]),
            current=current,
        )
        plan = _SandboxCallPlan(
            material=replace(material, input_dlp=dlp),
            probe=probe,
            policy=_network_policy_from_row(tool, config=config),
            fence=call_fence,
            network_decision_id=f"mcpnd_{uuid4().hex}",
            call_intent_id=f"mcpni_{uuid4().hex}",
            schema_revision=int(tool["schema_revision"]),
            tool_policy_revision=int(tool["policy_revision"]),
            tool_policy_fingerprint=_text(invocation["policy_fingerprint"]),
            call_generation=call_generation,
            call_owner_id=claim.lease_owner_id,
            call_token=call_token,
            call_expires_at=call_expires,
        )
        connection.commit()
        return plan
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, McpContractError, TypeError, ValueError) as exc:
        _rollback(connection)
        raise ApiError("mcp.unavailable") from exc
    finally:
        connection.close()


def _sandbox_authorize_dispatch(plan: _SandboxCallPlan) -> McpSandboxDispatch:
    material = plan.material
    claim = material.claim
    return McpSandboxDispatch(
        action="authorize-call",
        tenant_id=claim.tenant_id,
        server_id=claim.server_id,
        operation_id=None,
        invocation_id=claim.invocation_id,
        lease_generation=plan.call_generation,
        lease_owner_id=plan.call_owner_id,
        lease_token=plan.call_token,
        policy=plan.policy,
        policy_revision=plan.fence.policy_revision,
        policy_fingerprint=plan.fence.policy_fingerprint,
        deadline_at=plan.call_expires_at,
        request_id=plan.fence.request_id,
        endpoint_ciphertext=material.endpoint_ciphertext,
        endpoint_hmac=material.endpoint_hmac,
        endpoint_hmac_key_version=material.endpoint_hmac_key_version,
        auth_type=material.auth_type,
        credential_id=material.credential_id,
        credential_ciphertext=material.credential_ciphertext,
        credential_revision=material.credential_revision,
        tool_name=material.remote_name,
        arguments_hmac=material.arguments_hmac,
        arguments_hmac_key_version=material.arguments_hmac_key_version,
        tool_id=claim.tool_id,
        schema_revision=plan.schema_revision,
        tool_policy_revision=plan.tool_policy_revision,
        tool_policy_fingerprint=plan.tool_policy_fingerprint,
        network_decision_id=plan.network_decision_id,
        call_intent_id=plan.call_intent_id,
    )


def _commit_sandbox_call_intent(
    config: AppConfig,
    plan: _SandboxCallPlan,
    permit: McpSandboxPermitResponse,
) -> _SandboxCallMaterial | None:
    material = plan.material
    claim = material.claim
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, None)
        timestamp = _db_time(current)
        invocation = _require_invocation_claim(
            connection,
            claim,
            status="probing",
            timestamp=timestamp,
        )
        if plan.call_expires_at <= current:
            raise ApiError("mcp.state_conflict")
        circuit = _require_network_fence(connection, plan.fence)
        tool = connection.execute(
            """
            SELECT tool.*,server.status AS server_status,
              server.state_revision AS current_server_revision,
              server.active_credential_revision AS current_credential_revision,
              credential.status AS credential_status,
              policy.mcp_network_policy_id,policy.current_revision,
              policy.current_fingerprint,policy.status AS network_status,
              revision.egress_mode,revision.connect_timeout_seconds,
              revision.probe_timeout_seconds,revision.call_timeout_seconds,
              revision.failure_threshold,revision.open_seconds
            FROM mcp_tools tool
            JOIN mcp_servers server ON server.tenant_id=tool.tenant_id
             AND server.mcp_server_id=tool.mcp_server_id
            JOIN mcp_network_policies policy ON policy.tenant_id=server.tenant_id
             AND policy.mcp_server_id=server.mcp_server_id
            JOIN mcp_network_policy_revisions revision
              ON revision.tenant_id=policy.tenant_id
             AND revision.mcp_network_policy_id=policy.mcp_network_policy_id
             AND revision.policy_revision=policy.current_revision
            LEFT JOIN mcp_server_credentials credential ON credential.tenant_id=server.tenant_id
             AND credential.mcp_server_id=server.mcp_server_id
             AND credential.credential_revision=server.active_credential_revision
            WHERE tool.tenant_id=? AND tool.mcp_tool_id=?
            """,
            (claim.tenant_id, claim.tool_id),
        ).fetchone()
        failure = _current_authorization_failure(connection, config, invocation, tool)
        if failure is None:
            required = tuple(_json_string_list(tool["required_permission_codes_json"]))
            try:
                require_actor_transaction_permissions(
                    connection,
                    tenant_id=claim.tenant_id,
                    user_id=_text(invocation["requested_by"]),
                    permission_codes=tuple(dict.fromkeys(("mcp.invoke", *required))),
                    unavailable_error_code="mcp.unavailable",
                )
            except ApiError as exc:
                failure = (
                    "mcp.authorization_revoked"
                    if exc.code == "auth.permission_denied"
                    else "mcp.unavailable"
                )
        if (
            tool is None
            or tool["network_status"] != "active"
            or tool["mcp_network_policy_id"] != plan.fence.policy_id
            or int(tool["current_revision"]) != plan.fence.policy_revision
            or tool["current_fingerprint"] != plan.fence.policy_fingerprint
            or int(tool["schema_revision"]) != plan.schema_revision
            or int(tool["policy_revision"]) != plan.tool_policy_revision
            or invocation["policy_fingerprint"] != plan.tool_policy_fingerprint
            or int(invocation["credential_revision"]) != int(material.credential_revision)
        ):
            failure = "mcp.authorization_revoked"
        if failure is not None:
            _close_network_circuit(
                connection,
                plan.fence,
                circuit=circuit,
                current=current,
            )
            _close_probe_and_terminalize(
                connection,
                invocation,
                claim,
                status="failed",
                error_code=failure,
                attempt_error_code=failure,
                current=current,
                request_bytes=plan.probe.request_bytes,
                response_bytes=plan.probe.response_bytes,
                input_dlp=material.input_dlp,
            )
            connection.commit()
            return None
        attempt = _load_processing_attempt(connection, invocation, kind="probe")
        _finish_attempt(
            connection,
            attempt,
            status="succeeded",
            error_code=None,
            request_bytes=_bounded_metric(plan.probe.request_bytes),
            response_bytes=_bounded_metric(plan.probe.response_bytes),
            timestamp=timestamp,
            duration_ms=_duration_ms(attempt["started_at"], current),
        )
        changed = connection.execute(
            """
            UPDATE mcp_invocations
            SET status='calling',state_revision=state_revision+1,call_attempt_count=1,
                call_started_at=?,execution_status='unknown',tool_call_count=NULL,
                cost_status='unknown',cost_micros=NULL,input_dlp_status=?,
                input_dlp_match_count=?,input_dlp_rule_id=?,input_dlp_ruleset_hmac=?,
                input_dlp_ruleset_hmac_key_version=?,input_dlp_scanned_at=?,
                lease_generation=?,lease_owner_id=?,lease_token=?,lease_expires_at=?,
                next_attempt_at=NULL,updated_at=?
            WHERE tenant_id=? AND mcp_invocation_id=? AND status='probing'
              AND state_revision=? AND lease_generation=?
              AND lease_owner_id=? AND lease_token=?
            """,
            (
                timestamp,
                material.input_dlp.status,
                material.input_dlp.match_count,
                material.input_dlp.rule_id,
                material.input_dlp.ruleset_hmac,
                material.input_dlp.ruleset_hmac_key_version,
                timestamp,
                plan.call_generation,
                plan.call_owner_id,
                plan.call_token,
                _db_time(plan.call_expires_at),
                timestamp,
                claim.tenant_id,
                claim.invocation_id,
                invocation["state_revision"],
                claim.lease_generation,
                claim.lease_owner_id,
                claim.lease_token,
            ),
        ).rowcount
        if changed != 1:
            raise ApiError("mcp.state_conflict")
        _insert_network_decision(
            connection,
            plan.fence,
            decision_id=plan.network_decision_id,
            response=permit.dispatch,
            result="allowed",
            reason_code="allowed",
            permit_digest=permit.permit_digest,
            observed_circuit_revision=int(circuit["state_revision"]),
            current=current,
        )
        completed_circuit_revision = _close_network_circuit(
            connection,
            plan.fence,
            circuit=circuit,
            current=current,
        )
        lease_token_hmac = mcp_sandbox_lease_token_hmac(
            tenant_id=claim.tenant_id,
            owner_id=claim.invocation_id,
            lease_generation=plan.call_generation,
            lease_token=plan.call_token,
            envelope_secret=_text(config.mcp_sandbox_envelope_secret),
        )
        connection.execute(
            """
            INSERT INTO mcp_network_call_intents(
              mcp_network_call_intent_id,tenant_id,mcp_invocation_id,mcp_server_id,
              mcp_tool_id,mcp_network_decision_id,mcp_network_policy_id,
              policy_revision,policy_fingerprint,permit_digest,endpoint_hmac,
              endpoint_hmac_key_version,credential_revision,schema_revision,
              tool_policy_revision,tool_policy_fingerprint,arguments_hmac,
              arguments_hmac_key_version,lease_generation,lease_owner_id,
              lease_token_hmac,authorized_at,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                plan.call_intent_id,
                claim.tenant_id,
                claim.invocation_id,
                claim.server_id,
                claim.tool_id,
                plan.network_decision_id,
                plan.fence.policy_id,
                plan.fence.policy_revision,
                plan.fence.policy_fingerprint,
                permit.permit_digest,
                material.endpoint_hmac,
                material.endpoint_hmac_key_version,
                invocation["credential_revision"],
                invocation["schema_revision"],
                invocation["policy_revision"],
                invocation["policy_fingerprint"],
                invocation["arguments_hmac"],
                invocation["arguments_hmac_key_version"],
                plan.call_generation,
                plan.call_owner_id,
                lease_token_hmac,
                timestamp,
                timestamp,
            ),
        )
        call_attempt_id = f"mcpa_{uuid4().hex}"
        connection.execute(
            """
            INSERT INTO mcp_invocation_attempts(
              mcp_invocation_attempt_id,tenant_id,mcp_invocation_id,attempt_kind,
              attempt_number,status,lease_generation,error_code,request_bytes,response_bytes,
              started_at,call_started_at,completed_at,duration_ms
            ) VALUES(?,?,?,'call',1,'processing',?,NULL,?,NULL,?,?,NULL,NULL)
            """,
            (
                call_attempt_id,
                claim.tenant_id,
                claim.invocation_id,
                plan.call_generation,
                int(invocation["arguments_bytes"]),
                timestamp,
                timestamp,
            ),
        )
        call_fence = replace(
            plan.fence,
            circuit_revision=completed_circuit_revision,
            half_open=False,
        )
        dispatch = McpSandboxDispatch(
            action="call",
            tenant_id=claim.tenant_id,
            server_id=claim.server_id,
            operation_id=None,
            invocation_id=claim.invocation_id,
            lease_generation=plan.call_generation,
            lease_owner_id=plan.call_owner_id,
            lease_token=plan.call_token,
            policy=plan.policy,
            policy_revision=plan.fence.policy_revision,
            policy_fingerprint=plan.fence.policy_fingerprint,
            deadline_at=plan.call_expires_at,
            request_id=plan.fence.request_id,
            endpoint_ciphertext=material.endpoint_ciphertext,
            endpoint_hmac=material.endpoint_hmac,
            endpoint_hmac_key_version=material.endpoint_hmac_key_version,
            auth_type=material.auth_type,
            credential_id=material.credential_id,
            credential_ciphertext=material.credential_ciphertext,
            credential_revision=material.credential_revision,
            tool_name=material.remote_name,
            arguments_ciphertext=material.arguments_ciphertext,
            arguments_hmac=material.arguments_hmac,
            arguments_hmac_key_version=material.arguments_hmac_key_version,
            tool_id=claim.tool_id,
            schema_revision=plan.schema_revision,
            tool_policy_revision=plan.tool_policy_revision,
            tool_policy_fingerprint=plan.tool_policy_fingerprint,
            network_decision_id=plan.network_decision_id,
            call_intent_id=plan.call_intent_id,
            permit=permit.permit,
            permit_digest=permit.permit_digest,
        )
        legacy = _CallMaterial(
            tenant_id=claim.tenant_id,
            invocation_id=claim.invocation_id,
            attempt_id=call_attempt_id,
            lease_generation=plan.call_generation,
            lease_owner_id=plan.call_owner_id,
            lease_token=plan.call_token,
            connection=McpConnectionMaterial(endpoint="sandbox", bearer_token=None),
            arguments=material.arguments,
            remote_name=material.remote_name,
        )
        connection.commit()
        return _SandboxCallMaterial(legacy=legacy, dispatch=dispatch, fence=call_fence)
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, McpContractError, TypeError, ValueError) as exc:
        _rollback(connection)
        raise ApiError("mcp.unavailable") from exc
    finally:
        connection.close()


def _finalize_probe_network_blocked(
    config: AppConfig,
    material: _SandboxInvocationMaterial,
    response: McpSandboxResponse,
) -> None:
    _finalize_invocation_network_blocked(
        config,
        material,
        fence=material.fence,
        response=response,
    )


def _finalize_authorize_network_blocked(
    config: AppConfig,
    plan: _SandboxCallPlan,
    response: McpSandboxResponse,
) -> None:
    _finalize_invocation_network_blocked(
        config,
        plan.material,
        fence=plan.fence,
        response=response,
    )


def _finalize_invocation_network_blocked(
    config: AppConfig,
    material: _SandboxInvocationMaterial,
    *,
    fence: _NetworkFence,
    response: McpSandboxResponse,
) -> None:
    claim = material.claim
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, None)
        invocation = _require_invocation_claim(
            connection,
            claim,
            status="probing",
            timestamp=_db_time(current),
        )
        decision_id = _record_network_block(
            connection,
            fence,
            response=response,
            current=current,
        )
        _close_probe_and_terminalize(
            connection,
            invocation,
            claim,
            status="blocked",
            error_code="mcp.network_blocked",
            attempt_error_code="mcp.network_blocked",
            current=current,
            request_bytes=response.request_bytes,
            response_bytes=response.response_bytes,
            input_dlp=material.input_dlp,
        )
        _create_network_event_outbox(
            connection,
            fence,
            decision_id=decision_id,
            reason_code=_network_block_reason(response),
            address_count=response.address_count,
            attempt_number=claim.attempt_number,
            current=current,
        )
        connection.commit()
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, TypeError, ValueError) as exc:
        _rollback(connection)
        raise ApiError("mcp.unavailable") from exc
    finally:
        connection.close()


def _prepare_invocation(config: AppConfig, claim: McpInvocationClaim) -> _InvocationMaterial | None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, None)
        invocation = _require_invocation_claim(
            connection,
            claim,
            status="probing",
            timestamp=_db_time(current),
        )
        tool = connection.execute(
            """
            SELECT tool.*,server.status AS server_status,
              server.state_revision AS current_server_revision,
              server.active_credential_revision AS current_credential_revision,
              server.endpoint_ciphertext,server.transport,
              credential.mcp_server_credential_id,credential.auth_type,
              credential.credential_ciphertext,credential.status AS credential_status
            FROM mcp_tools tool
            JOIN mcp_servers server ON server.tenant_id=tool.tenant_id
             AND server.mcp_server_id=tool.mcp_server_id
            LEFT JOIN mcp_server_credentials credential ON credential.tenant_id=server.tenant_id
             AND credential.mcp_server_id=server.mcp_server_id
             AND credential.credential_revision=server.active_credential_revision
            WHERE tool.tenant_id=? AND tool.mcp_tool_id=?
            """,
            (claim.tenant_id, claim.tool_id),
        ).fetchone()
        failure = _current_authorization_failure(connection, config, invocation, tool)
        if failure is None:
            required = tuple(_json_string_list(tool["required_permission_codes_json"]))
            try:
                require_actor_transaction_permissions(
                    connection,
                    tenant_id=claim.tenant_id,
                    user_id=_text(invocation["requested_by"]),
                    permission_codes=tuple(dict.fromkeys(("mcp.invoke", *required))),
                    unavailable_error_code="mcp.unavailable",
                )
            except ApiError as exc:
                failure = (
                    "mcp.authorization_revoked"
                    if exc.code == "auth.permission_denied"
                    else "mcp.unavailable"
                )
        try:
            arguments_bytes, _ = decrypt_mcp_value_from_keyring(
                invocation["arguments_ciphertext"],
                keyring=_encryption_keyring(config),
                tenant_id=claim.tenant_id,
                owner_id=claim.invocation_id,
                kind="arguments",
            )
            arguments = json.loads(arguments_bytes)
            if type(arguments) is not dict:
                raise ValueError
        except (McpContractError, UnicodeError, TypeError, ValueError):
            failure = "mcp.unavailable"
            arguments = {}
        if failure is None:
            dlp = _scan_dlp(
                connection,
                config,
                tenant_id=claim.tenant_id,
                invocation_id=claim.invocation_id,
                value=arguments,
                scope="input",
            )
            if dlp.status == "blocked":
                _close_probe_and_terminalize(
                    connection,
                    invocation,
                    claim,
                    status="blocked",
                    error_code="mcp.arguments_blocked",
                    attempt_error_code="mcp.arguments_blocked",
                    current=current,
                    input_dlp=dlp,
                )
                connection.commit()
                return None
            if dlp.status == "unavailable":
                _close_probe_and_terminalize(
                    connection,
                    invocation,
                    claim,
                    status="failed",
                    error_code="mcp.unavailable",
                    attempt_error_code="mcp.unavailable",
                    current=current,
                    input_dlp=dlp,
                )
                connection.commit()
                return None
        if failure is not None:
            _close_probe_and_terminalize(
                connection,
                invocation,
                claim,
                status="failed",
                error_code=failure,
                attempt_error_code=(
                    failure
                    if failure
                    in {
                        "mcp.authorization_revoked",
                        "mcp.state_conflict",
                        "mcp.unavailable",
                    }
                    else "mcp.unavailable"
                ),
                current=current,
            )
            connection.commit()
            return None
        endpoint, _ = decrypt_mcp_value_from_keyring(
            tool["endpoint_ciphertext"],
            keyring=_encryption_keyring(config),
            tenant_id=claim.tenant_id,
            owner_id=claim.server_id,
            kind="endpoint",
        )
        bearer: bytes | None = None
        if tool["auth_type"] == "bearer":
            bearer, _ = decrypt_mcp_value_from_keyring(
                tool["credential_ciphertext"],
                keyring=_encryption_keyring(config),
                tenant_id=claim.tenant_id,
                owner_id=_text(tool["mcp_server_credential_id"]),
                kind="bearer",
            )
        connection.commit()
        return _InvocationMaterial(
            claim=claim,
            connection=McpConnectionMaterial(
                endpoint=endpoint.decode("utf-8", errors="strict"),
                bearer_token=(
                    bearer.decode("utf-8", errors="strict") if bearer is not None else None
                ),
                transport=_text(tool["transport"]),  # type: ignore[arg-type]
            ),
            arguments=arguments,
            input_dlp=dlp,
            remote_name=_text(tool["remote_name"]),
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, McpContractError, UnicodeError, TypeError, ValueError) as exc:
        _rollback(connection)
        raise ApiError("mcp.unavailable") from exc
    finally:
        connection.close()


def _current_authorization_failure(
    connection: sqlite3.Connection,
    config: AppConfig,
    invocation: sqlite3.Row,
    tool: sqlite3.Row | None,
) -> str | None:
    if tool is None:
        return "mcp.authorization_revoked"
    role_ids = _tool_role_ids(
        connection, _text(invocation["tenant_id"]), _text(invocation["mcp_tool_id"])
    )
    user_ids = _tool_user_ids(
        connection, _text(invocation["tenant_id"]), _text(invocation["mcp_tool_id"])
    )
    required = _json_string_list(tool["required_permission_codes_json"])
    actor = connection.execute(
        "SELECT role,status FROM users WHERE tenant_id=? AND user_id=?",
        (invocation["tenant_id"], invocation["requested_by"]),
    ).fetchone()
    role = (
        connection.execute(
            "SELECT role_id FROM roles WHERE tenant_id=? AND code=? AND status='active'",
            (invocation["tenant_id"], actor["role"]),
        ).fetchone()
        if actor is not None
        else None
    )
    role_id = _text(role["role_id"]) if role is not None else None
    output_original = tool["output_schema_original_sha256"]
    output_effective = tool["output_schema_effective_sha256"]
    approval_failure = _approval_authorization_failure(connection, config, invocation)
    if (
        actor is None
        or actor["status"] != "active"
        or tool["status"] != "active"
        or tool["server_status"] != "active"
        or tool["credential_status"] != "active"
        or int(tool["current_server_revision"]) != int(invocation["server_state_revision"])
        or int(tool["current_credential_revision"]) != int(invocation["credential_revision"])
        or int(tool["schema_revision"]) != int(invocation["schema_revision"])
        or int(tool["policy_revision"]) != int(invocation["policy_revision"])
        or tool["input_schema_original_sha256"] != invocation["input_schema_original_sha256"]
        or tool["input_schema_effective_sha256"] != invocation["input_schema_effective_sha256"]
        or output_original != invocation["output_schema_original_sha256"]
        or output_effective != invocation["output_schema_effective_sha256"]
        or tool["risk_level"] != invocation["risk_level"]
        or int(tool["approval_required"]) != int(invocation["approval_required"])
        or tool["result_redaction_pointers_json"] != invocation["result_redaction_pointers_json"]
        or not hmac.compare_digest(
            _sha256(required), _text(invocation["required_permissions_sha256"])
        )
        or not hmac.compare_digest(
            _sha256([list(role_ids), list(user_ids)]), _text(invocation["grant_sha256"])
        )
        or (invocation["requested_by"] not in user_ids and role_id not in role_ids)
        or approval_failure is not None
    ):
        return approval_failure or "mcp.authorization_revoked"
    return None


def _approval_authorization_failure(
    connection: sqlite3.Connection,
    config: AppConfig,
    invocation: sqlite3.Row,
) -> str | None:
    if int(invocation["approval_required"]) == 0:
        return None if invocation["approval_task_id"] is None else "mcp.authorization_revoked"
    approval_id = invocation["approval_task_id"]
    deadline = invocation["approval_deadline_at"]
    if approval_id is None or deadline is None:
        return "mcp.authorization_revoked"
    task_key = connection.execute(
        """
        SELECT create_hmac_key_version FROM approval_tasks
        WHERE tenant_id=? AND approval_task_id=?
        """,
        (invocation["tenant_id"], approval_id),
    ).fetchone()
    if task_key is None:
        return "mcp.authorization_revoked"
    try:
        target_version, target_fingerprint = mcp_approval_target_coordinates(
            config,
            fingerprint_key_version=int(task_key["create_hmac_key_version"]),
            invocation_id=_text(invocation["mcp_invocation_id"]),
            tenant_id=_text(invocation["tenant_id"]),
            server_id=_text(invocation["mcp_server_id"]),
            tool_id=_text(invocation["mcp_tool_id"]),
            requested_by=_text(invocation["requested_by"]),
            server_state_revision=int(invocation["server_state_revision"]),
            credential_revision=int(invocation["credential_revision"]),
            schema_revision=int(invocation["schema_revision"]),
            policy_revision=int(invocation["policy_revision"]),
            input_schema_original_sha256=_text(invocation["input_schema_original_sha256"]),
            input_schema_effective_sha256=_text(invocation["input_schema_effective_sha256"]),
            output_schema_original_sha256=(
                _text(invocation["output_schema_original_sha256"])
                if invocation["output_schema_original_sha256"] is not None
                else None
            ),
            output_schema_effective_sha256=(
                _text(invocation["output_schema_effective_sha256"])
                if invocation["output_schema_effective_sha256"] is not None
                else None
            ),
            required_permissions_sha256=_text(invocation["required_permissions_sha256"]),
            grant_sha256=_text(invocation["grant_sha256"]),
            arguments_hmac=_text(invocation["arguments_hmac"]),
            arguments_hmac_key_version=int(invocation["arguments_hmac_key_version"]),
            approval_deadline_at=_text(deadline),
        )
    except (ApiError, TypeError, ValueError):
        return "mcp.unavailable"
    row = connection.execute(
        """
        SELECT 1
        FROM approval_tasks task
        JOIN approval_decisions decision
          ON decision.tenant_id=task.tenant_id
         AND decision.approval_task_id=task.approval_task_id
        JOIN approval_writebacks writeback
          ON writeback.tenant_id=task.tenant_id
         AND writeback.approval_task_id=task.approval_task_id
        WHERE task.tenant_id=? AND task.approval_task_id=?
          AND task.source_type='mcp' AND task.source_id=?
          AND task.target_type='mcp_tool_invocation' AND task.target_id=?
          AND task.target_action='invoke' AND task.target_reason_code IS NULL
          AND task.target_version=? AND task.target_fingerprint=?
          AND task.create_hmac_key_version=?
          AND task.risk_level='high' AND task.required_permission_code='mcp.approve'
          AND task.created_by_type='user' AND task.created_by_id=?
          AND task.mcp_invocation_id=? AND task.expires_at=?
          AND task.source_request_id=? AND task.source_trace_id=?
          AND task.status='approved' AND task.resolution_reason='approved'
          AND task.state_revision=2 AND task.writeback_status='completed'
          AND task.decided_by_user_id IS NOT NULL
          AND task.decided_by_user_id!=? AND task.decided_at=task.updated_at
          AND decision.decision='approve'
          AND decision.reason_code='approved_business_need'
          AND decision.actor_type='user' AND decision.actor_id=task.decided_by_user_id
          AND decision.revision_before=1 AND decision.revision_after=task.state_revision
          AND decision.step_up_used=1 AND decision.writeback_status='completed'
          AND decision.request_id=task.decision_request_id
          AND decision.trace_id=task.decision_trace_id
          AND decision.decided_at=task.decided_at
          AND writeback.adapter_kind='mcp_invocation_enqueue'
          AND writeback.target_fact_id=? AND writeback.target_revision=?
          AND writeback.status='completed' AND writeback.attempt_count=0
          AND writeback.lease_generation=0 AND writeback.lease_owner_id IS NULL
          AND writeback.lease_token IS NULL AND writeback.lease_expires_at IS NULL
          AND writeback.next_attempt_at IS NULL AND writeback.last_error_code IS NULL
          AND writeback.completed_at=decision.decided_at
          AND writeback.created_at=decision.decided_at
          AND writeback.updated_at=decision.decided_at
        """,
        (
            invocation["tenant_id"],
            approval_id,
            invocation["mcp_invocation_id"],
            invocation["mcp_invocation_id"],
            target_version,
            target_fingerprint,
            task_key["create_hmac_key_version"],
            invocation["requested_by"],
            invocation["mcp_invocation_id"],
            deadline,
            invocation["request_id"],
            invocation["trace_id"],
            invocation["requested_by"],
            invocation["mcp_invocation_id"],
            invocation["policy_revision"],
        ),
    ).fetchone()
    return None if row is not None else "mcp.authorization_revoked"


def _finalize_probe_failure(
    config: AppConfig,
    claim: McpInvocationClaim,
    *,
    error_code: str,
    input_dlp: DlpDecision | None = None,
    network: _NetworkFence | None = None,
    network_response: McpSandboxResponse | None = None,
) -> None:
    allowed = {
        "mcp.executor_disabled",
        "mcp.authorization_revoked",
        "mcp.server_unavailable",
        "mcp.state_conflict",
        "mcp.unavailable",
        "mcp.sandbox_unavailable",
    }
    code = error_code if error_code in allowed else "mcp.unavailable"
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, None)
        timestamp = _db_time(current)
        invocation = _require_invocation_claim(
            connection,
            claim,
            status="probing",
            timestamp=timestamp,
        )
        if network is not None:
            if (
                network_response is not None
                and network_response.status == "succeeded"
                and network.phase != "call_preflight"
            ):
                _complete_network_success(
                    connection,
                    network,
                    response=network_response,
                    current=current,
                )
            else:
                _complete_network_failure(
                    connection,
                    network,
                    error_code=code,
                    availability_reason=_network_failure_reason(
                        network_response,
                        error_code=code,
                    ),
                    current=current,
                )
        attempt = _load_processing_attempt(connection, invocation, kind="probe")
        _finish_attempt(
            connection,
            attempt,
            status="failed",
            error_code=code,
            request_bytes=attempt["request_bytes"],
            response_bytes=attempt["response_bytes"],
            timestamp=timestamp,
            duration_ms=_duration_ms(attempt["started_at"], current),
        )
        if (
            code in {"mcp.server_unavailable", "mcp.sandbox_unavailable"}
            and claim.attempt_number < _MAX_PROBE_ATTEMPTS
        ):
            next_at = current + timedelta(seconds=_probe_backoff(claim.attempt_number))
            changed = connection.execute(
                """
                UPDATE mcp_invocations
                SET status='queued',state_revision=state_revision+1,
                    lease_generation=lease_generation+1,lease_owner_id=NULL,lease_token=NULL,
                    lease_expires_at=NULL,next_attempt_at=?,updated_at=?
                WHERE tenant_id=? AND mcp_invocation_id=? AND status='probing'
                  AND state_revision=? AND lease_generation=?
                  AND lease_owner_id=? AND lease_token=?
                """,
                (
                    _db_time(next_at),
                    timestamp,
                    claim.tenant_id,
                    claim.invocation_id,
                    invocation["state_revision"],
                    claim.lease_generation,
                    claim.lease_owner_id,
                    claim.lease_token,
                ),
            ).rowcount
            if changed != 1:
                raise ApiError("mcp.state_conflict")
        else:
            _terminalize_without_call(
                connection,
                invocation,
                status="failed",
                error_code=code,
                timestamp=timestamp,
                duration_ms=_duration_ms(invocation["started_at"], current),
                input_dlp=input_dlp,
            )
        connection.commit()
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, TypeError, ValueError) as exc:
        _rollback(connection)
        raise ApiError("mcp.unavailable") from exc
    finally:
        connection.close()


def _begin_call_after_probe(
    config: AppConfig,
    material: _InvocationMaterial,
    probe: McpProbeResult,
) -> _CallMaterial | None:
    claim = material.claim
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, None)
        timestamp = _db_time(current)
        invocation = _require_invocation_claim(
            connection,
            claim,
            status="probing",
            timestamp=timestamp,
        )
        tool = connection.execute(
            """
            SELECT tool.*,server.status AS server_status,
              server.state_revision AS current_server_revision,
              server.active_credential_revision AS current_credential_revision,
              server.protocol_version AS current_protocol_version,
              server.capabilities_sha256 AS current_capabilities_sha256,
              credential.status AS credential_status
            FROM mcp_tools tool
            JOIN mcp_servers server ON server.tenant_id=tool.tenant_id
             AND server.mcp_server_id=tool.mcp_server_id
            LEFT JOIN mcp_server_credentials credential ON credential.tenant_id=server.tenant_id
             AND credential.mcp_server_id=server.mcp_server_id
             AND credential.credential_revision=server.active_credential_revision
            WHERE tool.tenant_id=? AND tool.mcp_tool_id=?
            """,
            (claim.tenant_id, claim.tool_id),
        ).fetchone()
        failure = _current_authorization_failure(connection, config, invocation, tool)
        if failure is None:
            required = tuple(_json_string_list(tool["required_permission_codes_json"]))
            try:
                require_actor_transaction_permissions(
                    connection,
                    tenant_id=claim.tenant_id,
                    user_id=_text(invocation["requested_by"]),
                    permission_codes=tuple(dict.fromkeys(("mcp.invoke", *required))),
                    unavailable_error_code="mcp.unavailable",
                )
            except ApiError as exc:
                failure = (
                    "mcp.authorization_revoked"
                    if exc.code == "auth.permission_denied"
                    else "mcp.unavailable"
                )
        if failure is None:
            dlp = _scan_dlp(
                connection,
                config,
                tenant_id=claim.tenant_id,
                invocation_id=claim.invocation_id,
                value=material.arguments,
                scope="input",
            )
            if dlp.status == "blocked":
                _close_probe_and_terminalize(
                    connection,
                    invocation,
                    claim,
                    status="blocked",
                    error_code="mcp.arguments_blocked",
                    attempt_error_code="mcp.arguments_blocked",
                    current=current,
                    request_bytes=probe.request_bytes,
                    response_bytes=probe.response_bytes,
                    input_dlp=dlp,
                )
                connection.commit()
                return None
            if dlp.status == "unavailable":
                _close_probe_and_terminalize(
                    connection,
                    invocation,
                    claim,
                    status="failed",
                    error_code="mcp.unavailable",
                    attempt_error_code="mcp.unavailable",
                    current=current,
                    request_bytes=probe.request_bytes,
                    response_bytes=probe.response_bytes,
                    input_dlp=dlp,
                )
                connection.commit()
                return None
        if failure is not None:
            _close_probe_and_terminalize(
                connection,
                invocation,
                claim,
                status="failed",
                error_code=failure,
                attempt_error_code=failure,
                current=current,
                request_bytes=probe.request_bytes,
                response_bytes=probe.response_bytes,
                input_dlp=material.input_dlp,
            )
            connection.commit()
            return None

        drifted = _probe_has_drift(
            tool=tool,
            probe=probe,
        )
        if drifted:
            _close_probe_and_terminalize(
                connection,
                invocation,
                claim,
                status="failed",
                error_code="mcp.state_conflict",
                attempt_error_code="mcp.state_conflict",
                current=current,
                request_bytes=probe.request_bytes,
                response_bytes=probe.response_bytes,
                input_dlp=dlp,
            )
            connection.commit()
            return None

        attempt = _load_processing_attempt(connection, invocation, kind="probe")
        _finish_attempt(
            connection,
            attempt,
            status="succeeded",
            error_code=None,
            request_bytes=_bounded_metric(probe.request_bytes),
            response_bytes=_bounded_metric(probe.response_bytes),
            timestamp=timestamp,
            duration_ms=_duration_ms(attempt["started_at"], current),
        )
        call_token = secrets.token_hex(32)
        call_expires = current + timedelta(seconds=_invocation_lease_seconds(config))
        changed = connection.execute(
            """
            UPDATE mcp_invocations
            SET status='calling',state_revision=state_revision+1,call_attempt_count=1,
                call_started_at=?,execution_status='unknown',tool_call_count=NULL,
                cost_status='unknown',cost_micros=NULL,
                input_dlp_status=?,input_dlp_match_count=?,input_dlp_rule_id=?,
                input_dlp_ruleset_hmac=?,input_dlp_ruleset_hmac_key_version=?,
                input_dlp_scanned_at=?,
                lease_generation=lease_generation+1,lease_owner_id=?,lease_token=?,
                lease_expires_at=?,next_attempt_at=NULL,updated_at=?
            WHERE tenant_id=? AND mcp_invocation_id=? AND status='probing'
              AND state_revision=? AND lease_generation=?
              AND lease_owner_id=? AND lease_token=?
            """,
            (
                timestamp,
                dlp.status,
                dlp.match_count,
                dlp.rule_id,
                dlp.ruleset_hmac,
                dlp.ruleset_hmac_key_version,
                timestamp,
                claim.lease_owner_id,
                call_token,
                _db_time(call_expires),
                timestamp,
                claim.tenant_id,
                claim.invocation_id,
                invocation["state_revision"],
                claim.lease_generation,
                claim.lease_owner_id,
                claim.lease_token,
            ),
        ).rowcount
        if changed != 1:
            raise ApiError("mcp.state_conflict")
        call_generation = claim.lease_generation + 1
        call_attempt_id = f"mcpa_{uuid4().hex}"
        connection.execute(
            """
            INSERT INTO mcp_invocation_attempts(
              mcp_invocation_attempt_id,tenant_id,mcp_invocation_id,attempt_kind,
              attempt_number,status,lease_generation,error_code,request_bytes,response_bytes,
              started_at,call_started_at,completed_at,duration_ms
            ) VALUES(?,?,?,'call',1,'processing',?,NULL,?,NULL,?,?,NULL,NULL)
            """,
            (
                call_attempt_id,
                claim.tenant_id,
                claim.invocation_id,
                call_generation,
                int(invocation["arguments_bytes"]),
                timestamp,
                timestamp,
            ),
        )
        connection.commit()
        return _CallMaterial(
            tenant_id=claim.tenant_id,
            invocation_id=claim.invocation_id,
            attempt_id=call_attempt_id,
            lease_generation=call_generation,
            lease_owner_id=claim.lease_owner_id,
            lease_token=call_token,
            connection=material.connection,
            arguments=material.arguments,
            remote_name=material.remote_name,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, McpContractError, TypeError, ValueError) as exc:
        _rollback(connection)
        raise ApiError("mcp.unavailable") from exc
    finally:
        connection.close()


def _probe_has_drift(
    *,
    tool: sqlite3.Row,
    probe: McpProbeResult,
) -> bool:
    remote = probe.tool
    input_schema = remote.schema_bundle.input
    output_schema = remote.schema_bundle.output
    expected = (
        tool["description"],
        tool["input_schema_original_json"],
        tool["input_schema_original_sha256"],
        tool["input_schema_effective_json"],
        tool["input_schema_effective_sha256"],
        tool["output_schema_original_json"],
        tool["output_schema_original_sha256"],
        tool["output_schema_effective_json"],
        tool["output_schema_effective_sha256"],
        tool["annotations_json"],
        tool["annotations_sha256"],
    )
    observed = (
        remote.description,
        input_schema.original_json,
        input_schema.original_sha256,
        input_schema.effective_json,
        input_schema.effective_sha256,
        output_schema.original_json if output_schema is not None else None,
        output_schema.original_sha256 if output_schema is not None else None,
        output_schema.effective_json if output_schema is not None else None,
        output_schema.effective_sha256 if output_schema is not None else None,
        remote.annotations.canonical_json,
        remote.annotations.sha256,
    )
    return expected != observed or (
        probe.protocol_version != tool["current_protocol_version"]
        or probe.capabilities_sha256 != tool["current_capabilities_sha256"]
    )


def _finalize_call_result(
    config: AppConfig,
    material: _CallMaterial,
    result: McpCallResult,
    *,
    network: _NetworkFence | None = None,
    network_response: McpSandboxResponse | None = None,
) -> None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, None)
        timestamp = _db_time(current)
        invocation, attempt = _require_call_claim(connection, material, timestamp=timestamp)
        if network is not None:
            if network_response is None or network_response.status != "succeeded":
                raise ApiError("mcp.state_conflict")
            _complete_network_call_success(connection, network, current=current)
        tool = connection.execute(
            """
            SELECT tool.*,server.status AS server_status,
              server.state_revision AS current_server_revision,
              server.active_credential_revision AS current_credential_revision,
              credential.status AS credential_status
            FROM mcp_tools tool JOIN mcp_servers server
              ON server.tenant_id=tool.tenant_id AND server.mcp_server_id=tool.mcp_server_id
            LEFT JOIN mcp_server_credentials credential ON credential.tenant_id=server.tenant_id
              AND credential.mcp_server_id=server.mcp_server_id
              AND credential.credential_revision=server.active_credential_revision
            WHERE tool.tenant_id=? AND tool.mcp_tool_id=?
            """,
            (material.tenant_id, invocation["mcp_tool_id"]),
        ).fetchone()
        if _current_authorization_failure(connection, config, invocation, tool) is not None:
            _finalize_known_call_in_transaction(
                connection,
                invocation,
                attempt,
                status="failed",
                error_code="mcp.unavailable",
                timestamp=timestamp,
                current=current,
                request_bytes=_bounded_metric(result.request_bytes),
                response_bytes=_bounded_metric(result.response_bytes),
            )
            connection.commit()
            return
        try:
            if not result.is_error and tool["output_schema_effective_json"] is not None:
                if result.structured_content is None:
                    raise McpContractError("mcp.result_invalid")
                validate_result(
                    result.structured_content,
                    json.loads(_text(tool["output_schema_effective_json"])),
                    limits=_schema_limits(config),
                )
            projected = project_mcp_result(
                result.content,
                result.structured_content,
                _json_string_list(invocation["result_redaction_pointers_json"]),
                limits=_schema_limits(config),
            )
        except (McpContractError, json.JSONDecodeError, TypeError, ValueError):
            _finalize_known_call_in_transaction(
                connection,
                invocation,
                attempt,
                status="failed",
                error_code="mcp.result_invalid",
                timestamp=timestamp,
                current=current,
                request_bytes=_bounded_metric(result.request_bytes),
                response_bytes=_bounded_metric(result.response_bytes),
            )
            connection.commit()
            return
        output_dlp = _scan_dlp(
            connection,
            config,
            tenant_id=material.tenant_id,
            invocation_id=material.invocation_id,
            value=projected.value,
            scope="output",
        )
        if output_dlp.status == "blocked":
            terminal_status = "blocked"
            error_code: str | None = "mcp.result_blocked"
        elif output_dlp.status == "unavailable":
            terminal_status = "failed"
            error_code = "mcp.output_dlp_unavailable"
        elif result.is_error:
            terminal_status = "failed"
            error_code = "mcp.tool_failed"
        else:
            terminal_status = "succeeded"
            error_code = None
        result_fields: tuple[object, ...] = (
            None,
            None,
            None,
            None,
            None,
            projected.redaction_count,
        )
        if terminal_status == "succeeded":
            encryption_version, encryption_secret = _current_encryption(config)
            hmac_version, hmac_secret = _current_hmac(config)
            ciphertext = encrypt_mcp_value(
                projected.canonical_json,
                secret=encryption_secret,
                owner=McpCryptoOwner(
                    material.tenant_id,
                    material.invocation_id,
                    "result",
                    encryption_version,
                ),
            )
            digest = mcp_hmac(
                projected.canonical_json,
                secret=hmac_secret,
                owner=McpHmacOwner(
                    material.tenant_id,
                    material.invocation_id,
                    "result",
                    hmac_version,
                ),
            )
            result_fields = (
                ciphertext,
                encryption_version,
                digest,
                hmac_version,
                projected.byte_count,
                projected.redaction_count,
            )
        duration = _duration_ms(invocation["started_at"], current)
        _finish_attempt(
            connection,
            attempt,
            status="succeeded" if terminal_status == "succeeded" else "failed",
            error_code=error_code,
            request_bytes=_bounded_metric(result.request_bytes),
            response_bytes=_bounded_metric(result.response_bytes),
            timestamp=timestamp,
            duration_ms=_duration_ms(attempt["started_at"], current),
        )
        changed = connection.execute(
            """
            UPDATE mcp_invocations
            SET status=?,state_revision=state_revision+1,
                output_dlp_status=?,output_dlp_match_count=?,output_dlp_rule_id=?,
                output_dlp_ruleset_hmac=?,output_dlp_ruleset_hmac_key_version=?,
                output_dlp_scanned_at=?,result_ciphertext=?,result_encryption_key_version=?,
                result_hmac=?,result_hmac_key_version=?,result_bytes=?,
                result_redaction_count=?,request_bytes=?,response_bytes=?,duration_ms=?,
                execution_status='called',tool_call_count=1,cost_status='not_reported',
                cost_micros=NULL,error_code=?,lease_generation=lease_generation+1,
                lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
                next_attempt_at=NULL,completed_at=?,updated_at=?
            WHERE tenant_id=? AND mcp_invocation_id=? AND status='calling'
              AND state_revision=? AND lease_generation=?
              AND lease_owner_id=? AND lease_token=?
            """,
            (
                terminal_status,
                output_dlp.status,
                output_dlp.match_count,
                output_dlp.rule_id,
                output_dlp.ruleset_hmac,
                output_dlp.ruleset_hmac_key_version,
                timestamp,
                *result_fields,
                _bounded_metric(result.request_bytes),
                _bounded_metric(result.response_bytes),
                duration,
                error_code,
                timestamp,
                timestamp,
                material.tenant_id,
                material.invocation_id,
                invocation["state_revision"],
                material.lease_generation,
                material.lease_owner_id,
                material.lease_token,
            ),
        ).rowcount
        if changed != 1:
            raise ApiError("mcp.state_conflict")
        terminal = _load_invocation_row(connection, material.tenant_id, material.invocation_id)
        create_mcp_invocation_event_in_transaction(connection, terminal)
        connection.commit()
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, McpContractError, TypeError, ValueError) as exc:
        _rollback(connection)
        raise ApiError("mcp.unavailable") from exc
    finally:
        connection.close()


def _finalize_call_failure(
    config: AppConfig,
    material: _CallMaterial,
    *,
    error_code: str,
    network: _NetworkFence | None = None,
    network_response: McpSandboxResponse | None = None,
) -> None:
    code = (
        error_code if error_code in {"mcp.result_invalid", "mcp.unavailable"} else "mcp.unavailable"
    )
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, None)
        timestamp = _db_time(current)
        invocation, attempt = _require_call_claim(connection, material, timestamp=timestamp)
        if network is not None:
            if network_response is None or network_response.status != "succeeded":
                raise ApiError("mcp.state_conflict")
            _complete_network_call_success(connection, network, current=current)
        _finalize_known_call_in_transaction(
            connection,
            invocation,
            attempt,
            status="failed",
            error_code=code,
            timestamp=timestamp,
            current=current,
            request_bytes=(
                network_response.request_bytes
                if network_response is not None
                else int(attempt["request_bytes"])
            ),
            response_bytes=(network_response.response_bytes if network_response is not None else 0),
        )
        connection.commit()
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, TypeError, ValueError) as exc:
        _rollback(connection)
        raise ApiError("mcp.unavailable") from exc
    finally:
        connection.close()


def _finalize_known_call_in_transaction(
    connection: sqlite3.Connection,
    invocation: sqlite3.Row,
    attempt: sqlite3.Row,
    *,
    status: str,
    error_code: str,
    timestamp: str,
    current: datetime,
    request_bytes: int,
    response_bytes: int,
) -> None:
    _finish_attempt(
        connection,
        attempt,
        status="failed",
        error_code=error_code,
        request_bytes=request_bytes,
        response_bytes=response_bytes,
        timestamp=timestamp,
        duration_ms=_duration_ms(attempt["started_at"], current),
    )
    duration = _duration_ms(invocation["started_at"], current)
    changed = connection.execute(
        """
        UPDATE mcp_invocations
        SET status=?,state_revision=state_revision+1,request_bytes=?,response_bytes=?,
            duration_ms=?,execution_status='called',tool_call_count=1,
            cost_status='not_reported',cost_micros=NULL,error_code=?,
            lease_generation=lease_generation+1,lease_owner_id=NULL,lease_token=NULL,
            lease_expires_at=NULL,next_attempt_at=NULL,completed_at=?,updated_at=?
        WHERE tenant_id=? AND mcp_invocation_id=? AND status='calling'
          AND state_revision=? AND lease_generation=?
          AND lease_owner_id=? AND lease_token=?
        """,
        (
            status,
            request_bytes,
            response_bytes,
            duration,
            error_code,
            timestamp,
            timestamp,
            invocation["tenant_id"],
            invocation["mcp_invocation_id"],
            invocation["state_revision"],
            invocation["lease_generation"],
            invocation["lease_owner_id"],
            invocation["lease_token"],
        ),
    ).rowcount
    if changed != 1:
        raise ApiError("mcp.state_conflict")
    terminal = _load_invocation_row(
        connection, _text(invocation["tenant_id"]), _text(invocation["mcp_invocation_id"])
    )
    create_mcp_invocation_event_in_transaction(connection, terminal)


def _finalize_call_unknown(
    config: AppConfig,
    material: _CallMaterial,
    *,
    network: _NetworkFence | None = None,
    network_response: McpSandboxResponse | None = None,
) -> None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, None)
        timestamp = _db_time(current)
        invocation, attempt = _require_call_claim(connection, material, timestamp=timestamp)
        if network is not None:
            _complete_network_call_failure(
                connection,
                network,
                network_response=network_response,
                current=current,
            )
        _finish_attempt(
            connection,
            attempt,
            status="outcome_unknown",
            error_code="mcp.outcome_unknown",
            request_bytes=attempt["request_bytes"],
            response_bytes=0,
            timestamp=timestamp,
            duration_ms=_duration_ms(attempt["started_at"], current),
        )
        duration = _duration_ms(invocation["started_at"], current)
        changed = connection.execute(
            """
            UPDATE mcp_invocations
            SET status='outcome_unknown',state_revision=state_revision+1,
                response_bytes=0,duration_ms=?,execution_status='unknown',
                tool_call_count=NULL,cost_status='unknown',cost_micros=NULL,
                error_code='mcp.outcome_unknown',lease_generation=lease_generation+1,
                lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
                next_attempt_at=NULL,completed_at=?,updated_at=?
            WHERE tenant_id=? AND mcp_invocation_id=? AND status='calling'
              AND state_revision=? AND lease_generation=?
              AND lease_owner_id=? AND lease_token=?
            """,
            (
                duration,
                timestamp,
                timestamp,
                material.tenant_id,
                material.invocation_id,
                invocation["state_revision"],
                material.lease_generation,
                material.lease_owner_id,
                material.lease_token,
            ),
        ).rowcount
        if changed != 1:
            raise ApiError("mcp.state_conflict")
        terminal = _load_invocation_row(connection, material.tenant_id, material.invocation_id)
        create_mcp_invocation_event_in_transaction(connection, terminal)
        connection.commit()
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, TypeError, ValueError) as exc:
        _rollback(connection)
        raise ApiError("mcp.unavailable") from exc
    finally:
        connection.close()


def _close_probe_and_terminalize(
    connection: sqlite3.Connection,
    invocation: sqlite3.Row,
    claim: McpInvocationClaim,
    *,
    status: str,
    error_code: str,
    attempt_error_code: str,
    current: datetime,
    request_bytes: int | None = None,
    response_bytes: int | None = None,
    input_dlp: DlpDecision | None = None,
) -> None:
    timestamp = _db_time(current)
    attempt = _load_processing_attempt(connection, invocation, kind="probe")
    _finish_attempt(
        connection,
        attempt,
        status="failed",
        error_code=attempt_error_code,
        request_bytes=(
            _bounded_metric(request_bytes)
            if request_bytes is not None
            else attempt["request_bytes"]
        ),
        response_bytes=(
            _bounded_metric(response_bytes)
            if response_bytes is not None
            else attempt["response_bytes"]
        ),
        timestamp=timestamp,
        duration_ms=_duration_ms(attempt["started_at"], current),
    )
    _terminalize_without_call(
        connection,
        invocation,
        status=status,
        error_code=error_code,
        timestamp=timestamp,
        duration_ms=_duration_ms(invocation["started_at"], current),
        input_dlp=input_dlp,
    )


def _terminalize_without_call(
    connection: sqlite3.Connection,
    invocation: sqlite3.Row,
    *,
    status: str,
    error_code: str,
    timestamp: str,
    duration_ms: int,
    input_dlp: DlpDecision | None = None,
) -> None:
    if input_dlp is None:
        changed = connection.execute(
            """
            UPDATE mcp_invocations
            SET status=?,state_revision=state_revision+1,duration_ms=?,
                execution_status='not_called',tool_call_count=0,cost_status='not_applicable',
                cost_micros=NULL,error_code=?,lease_generation=lease_generation+1,
                lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
                next_attempt_at=NULL,completed_at=?,updated_at=?
            WHERE tenant_id=? AND mcp_invocation_id=? AND status='probing'
              AND state_revision=? AND lease_generation=?
              AND lease_owner_id=? AND lease_token=?
            """,
            (
                status,
                duration_ms,
                error_code,
                timestamp,
                timestamp,
                invocation["tenant_id"],
                invocation["mcp_invocation_id"],
                invocation["state_revision"],
                invocation["lease_generation"],
                invocation["lease_owner_id"],
                invocation["lease_token"],
            ),
        ).rowcount
    else:
        changed = connection.execute(
            """
            UPDATE mcp_invocations
            SET status=?,state_revision=state_revision+1,duration_ms=?,
                input_dlp_status=?,input_dlp_match_count=?,input_dlp_rule_id=?,
                input_dlp_ruleset_hmac=?,input_dlp_ruleset_hmac_key_version=?,
                input_dlp_scanned_at=?,execution_status='not_called',tool_call_count=0,
                cost_status='not_applicable',cost_micros=NULL,error_code=?,
                lease_generation=lease_generation+1,lease_owner_id=NULL,lease_token=NULL,
                lease_expires_at=NULL,next_attempt_at=NULL,completed_at=?,updated_at=?
            WHERE tenant_id=? AND mcp_invocation_id=? AND status='probing'
              AND state_revision=? AND lease_generation=?
              AND lease_owner_id=? AND lease_token=?
            """,
            (
                status,
                duration_ms,
                input_dlp.status,
                input_dlp.match_count,
                input_dlp.rule_id,
                input_dlp.ruleset_hmac,
                input_dlp.ruleset_hmac_key_version,
                timestamp,
                error_code,
                timestamp,
                timestamp,
                invocation["tenant_id"],
                invocation["mcp_invocation_id"],
                invocation["state_revision"],
                invocation["lease_generation"],
                invocation["lease_owner_id"],
                invocation["lease_token"],
            ),
        ).rowcount
    if changed != 1:
        raise ApiError("mcp.state_conflict")


def _finish_attempt(
    connection: sqlite3.Connection,
    attempt: sqlite3.Row,
    *,
    status: str,
    error_code: str | None,
    request_bytes: object,
    response_bytes: object,
    timestamp: str,
    duration_ms: int,
) -> None:
    changed = connection.execute(
        """
        UPDATE mcp_invocation_attempts
        SET status=?,error_code=?,request_bytes=?,response_bytes=?,
            completed_at=?,duration_ms=?
        WHERE tenant_id=? AND mcp_invocation_attempt_id=? AND status='processing'
          AND lease_generation=?
        """,
        (
            status,
            error_code,
            request_bytes,
            response_bytes,
            timestamp,
            duration_ms,
            attempt["tenant_id"],
            attempt["mcp_invocation_attempt_id"],
            attempt["lease_generation"],
        ),
    ).rowcount
    if changed != 1:
        raise ApiError("mcp.state_conflict")


def _require_sync_claim(
    connection: sqlite3.Connection,
    claim: McpSyncJobClaim,
    *,
    timestamp: str,
    allow_expired_deadline: bool = False,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT * FROM mcp_server_sync_jobs
        WHERE tenant_id=? AND mcp_server_sync_job_id=? AND status='processing'
          AND lease_generation=? AND lease_owner_id=? AND lease_token=?
          AND unixepoch(lease_expires_at)>unixepoch(?)
          AND (?=1 OR unixepoch(deadline_at)>unixepoch(?))
        """,
        (
            claim.tenant_id,
            claim.job_id,
            claim.lease_generation,
            claim.lease_owner_id,
            claim.lease_token,
            timestamp,
            int(allow_expired_deadline),
            timestamp,
        ),
    ).fetchone()
    if row is None:
        raise ApiError("mcp.state_conflict")
    return row


def _require_invocation_claim(
    connection: sqlite3.Connection,
    claim: McpInvocationClaim,
    *,
    status: str,
    timestamp: str,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT * FROM mcp_invocations
        WHERE tenant_id=? AND mcp_invocation_id=? AND status=?
          AND lease_generation=? AND lease_owner_id=? AND lease_token=?
          AND unixepoch(lease_expires_at)>unixepoch(?)
        """,
        (
            claim.tenant_id,
            claim.invocation_id,
            status,
            claim.lease_generation,
            claim.lease_owner_id,
            claim.lease_token,
            timestamp,
        ),
    ).fetchone()
    if row is None:
        raise ApiError("mcp.state_conflict")
    return row


def _require_call_claim(
    connection: sqlite3.Connection,
    material: _CallMaterial,
    *,
    timestamp: str,
) -> tuple[sqlite3.Row, sqlite3.Row]:
    invocation = connection.execute(
        """
        SELECT * FROM mcp_invocations
        WHERE tenant_id=? AND mcp_invocation_id=? AND status='calling'
          AND lease_generation=? AND lease_owner_id=? AND lease_token=?
          AND unixepoch(lease_expires_at)>unixepoch(?)
        """,
        (
            material.tenant_id,
            material.invocation_id,
            material.lease_generation,
            material.lease_owner_id,
            material.lease_token,
            timestamp,
        ),
    ).fetchone()
    attempt = connection.execute(
        """
        SELECT * FROM mcp_invocation_attempts
        WHERE tenant_id=? AND mcp_invocation_attempt_id=? AND status='processing'
          AND attempt_kind='call' AND attempt_number=1 AND lease_generation=?
        """,
        (
            material.tenant_id,
            material.attempt_id,
            material.lease_generation,
        ),
    ).fetchone()
    if invocation is None or attempt is None:
        raise ApiError("mcp.state_conflict")
    return invocation, attempt


def _load_processing_attempt(
    connection: sqlite3.Connection,
    invocation: sqlite3.Row,
    *,
    kind: str,
) -> sqlite3.Row:
    count_column = "probe_attempt_count" if kind == "probe" else "call_attempt_count"
    row = connection.execute(
        """
        SELECT * FROM mcp_invocation_attempts
        WHERE tenant_id=? AND mcp_invocation_id=? AND attempt_kind=?
          AND attempt_number=? AND lease_generation=? AND status='processing'
        """,
        (
            invocation["tenant_id"],
            invocation["mcp_invocation_id"],
            kind,
            invocation[count_column],
            invocation["lease_generation"],
        ),
    ).fetchone()
    if row is None:
        raise ApiError("mcp.state_conflict")
    return row


def _load_invocation_row(
    connection: sqlite3.Connection, tenant_id: str, invocation_id: str
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM mcp_invocations WHERE tenant_id=? AND mcp_invocation_id=?",
        (tenant_id, invocation_id),
    ).fetchone()
    if row is None:
        raise ApiError("mcp.unavailable")
    return row


def _require_tenant_lease(
    connection: sqlite3.Connection,
    lease: McpSchedulerTenantLease,
    timestamp: str,
) -> None:
    row = connection.execute(
        """
        SELECT 1 FROM mcp_scheduler_tenants
        WHERE tenant_id=? AND status='leased' AND lease_generation=?
          AND lease_owner_id=? AND lease_token=?
          AND unixepoch(lease_expires_at)>unixepoch(?)
        """,
        (
            lease.tenant_id,
            lease.lease_generation,
            lease.lease_owner_id,
            lease.lease_token,
            timestamp,
        ),
    ).fetchone()
    if row is None:
        raise ApiError("mcp.state_conflict")


async def _scheduler_loop(config: AppConfig, stop_event: threading.Event) -> None:
    concurrency = max(1, min(int(config.mcp_global_concurrency), 32))
    batch_size = max(1, min(int(config.mcp_scheduler_batch_size), 100))
    while not stop_event.is_set():
        worked = False
        remaining = batch_size
        while remaining > 0 and not stop_event.is_set():
            count = min(concurrency, remaining)
            results = await asyncio.gather(
                *(
                    asyncio.to_thread(
                        run_mcp_scheduler_once,
                        config,
                        stop_event=stop_event,
                    )
                    for _ in range(count)
                )
            )
            worked = worked or any(results)
            remaining -= count
            if not any(results):
                break
            await asyncio.sleep(0)
        await _wait(stop_event, 0.05 if worked else _poll_seconds(config))


async def _outbox_loop(config: AppConfig, stop_event: threading.Event) -> None:
    batch_size = max(1, min(int(config.mcp_scheduler_batch_size), 100))
    while not stop_event.is_set():
        worked = False
        for _ in range(batch_size):
            if stop_event.is_set():
                break
            current = await asyncio.to_thread(run_mcp_outbox_once, config, stop_event=stop_event)
            worked = worked or current
            if not current:
                break
            await asyncio.sleep(0)
        await _wait(stop_event, 0.05 if worked else _poll_seconds(config))


async def _wait(stop_event: threading.Event, delay: float) -> None:
    if stop_event.is_set():
        return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + delay
    while not stop_event.is_set() and loop.time() < deadline:
        await asyncio.sleep(min(0.05, max(0.0, deadline - loop.time())))


def _network_policy_from_row(row: sqlite3.Row, *, config: AppConfig) -> McpNetworkPolicy:
    try:
        policy = validate_mcp_network_policy(
            {
                "expected_revision": int(row["current_revision"]),
                "status": _text(row["network_status"]),
                "egress_mode": _text(row["egress_mode"]),
                "connect_timeout_seconds": int(row["connect_timeout_seconds"]),
                "probe_timeout_seconds": int(row["probe_timeout_seconds"]),
                "call_timeout_seconds": int(row["call_timeout_seconds"]),
                "failure_threshold": int(row["failure_threshold"]),
                "open_seconds": int(row["open_seconds"]),
            },
            caps=config.mcp_network_policy_caps(),
        )
    except (McpContractError, TypeError, ValueError):
        raise ApiError("mcp.unavailable") from None
    if not hmac.compare_digest(policy.fingerprint, _text(row["current_fingerprint"])):
        raise ApiError("mcp.state_conflict")
    return policy


def _start_network_fence(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    server_id: str,
    policy_row: sqlite3.Row,
    phase: str,
    owner_type: str,
    owner_id: str,
    lease_generation: int,
    lease_owner_id: str,
    lease_token: str,
    lease_expires_at: datetime,
    attempt_number: int,
    request_id: str,
    trace_id: str,
    current: datetime,
    allow_disabled: bool = False,
) -> _NetworkFence:
    policy_id = _text(policy_row["mcp_network_policy_id"])
    policy_revision = int(policy_row["current_revision"])
    policy_fingerprint = _text(policy_row["current_fingerprint"])
    policy_status = _text(policy_row["network_status"])
    if policy_status != "active" and not (allow_disabled and policy_status == "disabled"):
        raise ApiError("mcp.state_conflict")
    policy = connection.execute(
        """
        SELECT 1 FROM mcp_network_policies current
        JOIN mcp_network_policy_revisions revision
          ON revision.tenant_id=current.tenant_id
         AND revision.mcp_network_policy_id=current.mcp_network_policy_id
         AND revision.policy_revision=current.current_revision
        WHERE current.tenant_id=? AND current.mcp_server_id=?
          AND current.mcp_network_policy_id=? AND current.current_revision=?
          AND current.current_fingerprint=? AND current.status=?
          AND revision.policy_fingerprint=current.current_fingerprint
        """,
        (
            tenant_id,
            server_id,
            policy_id,
            policy_revision,
            policy_fingerprint,
            policy_status,
        ),
    ).fetchone()
    if policy is None:
        raise ApiError("mcp.state_conflict")
    timestamp = _db_time(current)
    circuit = connection.execute(
        """
        SELECT * FROM mcp_network_circuits
        WHERE tenant_id=? AND mcp_server_id=? AND policy_revision=?
        """,
        (tenant_id, server_id, policy_revision),
    ).fetchone()
    if circuit is None:
        connection.execute(
            """
            INSERT INTO mcp_network_circuits(
              tenant_id,mcp_server_id,mcp_network_policy_id,policy_revision,
              state,failure_count,opened_until,half_open_owner_type,
              half_open_owner_id,half_open_lease_generation,half_open_lease_owner_id,
              half_open_lease_token,half_open_lease_expires_at,last_failure_code,
              state_revision,created_at,updated_at
            ) VALUES(?,?,?,?,'closed',0,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,1,?,?)
            """,
            (tenant_id, server_id, policy_id, policy_revision, timestamp, timestamp),
        )
        circuit = connection.execute(
            """
            SELECT * FROM mcp_network_circuits
            WHERE tenant_id=? AND mcp_server_id=? AND policy_revision=?
            """,
            (tenant_id, server_id, policy_revision),
        ).fetchone()
    if circuit is None:
        raise ApiError("mcp.state_conflict")
    if circuit["state"] == "open":
        opened_until = _parse_db_time(circuit["opened_until"])
        if opened_until > current:
            raise ApiError("mcp.server_unavailable")
        changed = connection.execute(
            """
            UPDATE mcp_network_circuits
            SET state='half_open',failure_count=failure_count,opened_until=NULL,
                half_open_owner_type=?,half_open_owner_id=?,
                half_open_lease_generation=?,half_open_lease_owner_id=?,
                half_open_lease_token=?,half_open_lease_expires_at=?,
                last_failure_code=last_failure_code,state_revision=state_revision+1,
                updated_at=?
            WHERE tenant_id=? AND mcp_server_id=? AND policy_revision=?
              AND state='open' AND state_revision=?
              AND unixepoch(opened_until)<=unixepoch(?)
            """,
            (
                owner_type,
                owner_id,
                lease_generation,
                lease_owner_id,
                lease_token,
                _db_time(lease_expires_at),
                timestamp,
                tenant_id,
                server_id,
                policy_revision,
                circuit["state_revision"],
                timestamp,
            ),
        ).rowcount
        if changed != 1:
            raise ApiError("mcp.state_conflict")
        circuit = connection.execute(
            """
            SELECT * FROM mcp_network_circuits
            WHERE tenant_id=? AND mcp_server_id=? AND policy_revision=?
            """,
            (tenant_id, server_id, policy_revision),
        ).fetchone()
    elif circuit["state"] == "half_open":
        same_owner = (
            circuit["half_open_owner_type"] == owner_type
            and circuit["half_open_owner_id"] == owner_id
            and int(circuit["half_open_lease_generation"]) == lease_generation
            and circuit["half_open_lease_owner_id"] == lease_owner_id
            and hmac.compare_digest(_text(circuit["half_open_lease_token"]), lease_token)
        )
        if not same_owner:
            if _parse_db_time(circuit["half_open_lease_expires_at"]) > current:
                raise ApiError("mcp.server_unavailable")
            reclaimed = connection.execute(
                """
                UPDATE mcp_network_circuits
                SET state='open',opened_until=?,half_open_owner_type=NULL,
                    half_open_owner_id=NULL,half_open_lease_generation=NULL,
                    half_open_lease_owner_id=NULL,half_open_lease_token=NULL,
                    half_open_lease_expires_at=NULL,state_revision=state_revision+1,
                    updated_at=?
                WHERE tenant_id=? AND mcp_server_id=? AND policy_revision=?
                  AND state='half_open' AND state_revision=?
                  AND half_open_owner_type=? AND half_open_owner_id=?
                  AND half_open_lease_generation=? AND half_open_lease_owner_id=?
                  AND half_open_lease_token=? AND half_open_lease_expires_at=?
                  AND unixepoch(half_open_lease_expires_at)<=unixepoch(?)
                """,
                (
                    timestamp,
                    timestamp,
                    tenant_id,
                    server_id,
                    policy_revision,
                    circuit["state_revision"],
                    circuit["half_open_owner_type"],
                    circuit["half_open_owner_id"],
                    circuit["half_open_lease_generation"],
                    circuit["half_open_lease_owner_id"],
                    circuit["half_open_lease_token"],
                    circuit["half_open_lease_expires_at"],
                    timestamp,
                ),
            ).rowcount
            if reclaimed != 1:
                raise ApiError("mcp.state_conflict")
            circuit = connection.execute(
                """
                SELECT * FROM mcp_network_circuits
                WHERE tenant_id=? AND mcp_server_id=? AND policy_revision=?
                """,
                (tenant_id, server_id, policy_revision),
            ).fetchone()
            if circuit is None or circuit["state"] != "open":
                raise ApiError("mcp.state_conflict")
            claimed = connection.execute(
                """
                UPDATE mcp_network_circuits
                SET state='half_open',opened_until=NULL,half_open_owner_type=?,
                    half_open_owner_id=?,half_open_lease_generation=?,
                    half_open_lease_owner_id=?,half_open_lease_token=?,
                    half_open_lease_expires_at=?,state_revision=state_revision+1,
                    updated_at=?
                WHERE tenant_id=? AND mcp_server_id=? AND policy_revision=?
                  AND state='open' AND state_revision=?
                  AND unixepoch(opened_until)<=unixepoch(?)
                """,
                (
                    owner_type,
                    owner_id,
                    lease_generation,
                    lease_owner_id,
                    lease_token,
                    _db_time(lease_expires_at),
                    timestamp,
                    tenant_id,
                    server_id,
                    policy_revision,
                    circuit["state_revision"],
                    timestamp,
                ),
            ).rowcount
            if claimed != 1:
                raise ApiError("mcp.state_conflict")
            circuit = connection.execute(
                """
                SELECT * FROM mcp_network_circuits
                WHERE tenant_id=? AND mcp_server_id=? AND policy_revision=?
                """,
                (tenant_id, server_id, policy_revision),
            ).fetchone()
    assert circuit is not None
    return _NetworkFence(
        tenant_id=tenant_id,
        server_id=server_id,
        policy_id=policy_id,
        policy_revision=policy_revision,
        policy_fingerprint=policy_fingerprint,
        policy_status=policy_status,
        failure_threshold=int(policy_row["failure_threshold"]),
        open_seconds=int(policy_row["open_seconds"]),
        phase=phase,
        owner_type=owner_type,
        owner_id=owner_id,
        lease_generation=lease_generation,
        lease_owner_id=lease_owner_id,
        lease_token=lease_token,
        attempt_number=attempt_number,
        circuit_revision=int(circuit["state_revision"]),
        half_open=circuit["state"] == "half_open",
        request_id=request_id,
        trace_id=trace_id,
    )


def _require_network_fence(
    connection: sqlite3.Connection,
    fence: _NetworkFence,
    *,
    require_current_policy: bool = True,
) -> sqlite3.Row:
    if require_current_policy:
        row = connection.execute(
            """
            SELECT circuit.* FROM mcp_network_circuits circuit
            JOIN mcp_network_policies policy ON policy.tenant_id=circuit.tenant_id
             AND policy.mcp_server_id=circuit.mcp_server_id
             AND policy.mcp_network_policy_id=circuit.mcp_network_policy_id
             AND policy.current_revision=circuit.policy_revision
            WHERE circuit.tenant_id=? AND circuit.mcp_server_id=?
              AND circuit.mcp_network_policy_id=? AND circuit.policy_revision=?
              AND policy.current_fingerprint=? AND policy.status=?
              AND circuit.state_revision=?
            """,
            (
                fence.tenant_id,
                fence.server_id,
                fence.policy_id,
                fence.policy_revision,
                fence.policy_fingerprint,
                fence.policy_status,
                fence.circuit_revision,
            ),
        ).fetchone()
    else:
        row = connection.execute(
            """
            SELECT * FROM mcp_network_circuits
            WHERE tenant_id=? AND mcp_server_id=? AND mcp_network_policy_id=?
              AND policy_revision=? AND state_revision=?
            """,
            (
                fence.tenant_id,
                fence.server_id,
                fence.policy_id,
                fence.policy_revision,
                fence.circuit_revision,
            ),
        ).fetchone()
    if row is None and not fence.half_open:
        if require_current_policy:
            row = connection.execute(
                """
                SELECT circuit.* FROM mcp_network_circuits circuit
                JOIN mcp_network_policies policy ON policy.tenant_id=circuit.tenant_id
                 AND policy.mcp_server_id=circuit.mcp_server_id
                 AND policy.mcp_network_policy_id=circuit.mcp_network_policy_id
                 AND policy.current_revision=circuit.policy_revision
                WHERE circuit.tenant_id=? AND circuit.mcp_server_id=?
                  AND circuit.mcp_network_policy_id=? AND circuit.policy_revision=?
                  AND policy.current_fingerprint=? AND policy.status=?
                """,
                (
                    fence.tenant_id,
                    fence.server_id,
                    fence.policy_id,
                    fence.policy_revision,
                    fence.policy_fingerprint,
                    fence.policy_status,
                ),
            ).fetchone()
        else:
            row = connection.execute(
                """
                SELECT * FROM mcp_network_circuits
                WHERE tenant_id=? AND mcp_server_id=? AND mcp_network_policy_id=?
                  AND policy_revision=?
                """,
                (
                    fence.tenant_id,
                    fence.server_id,
                    fence.policy_id,
                    fence.policy_revision,
                ),
            ).fetchone()
    if row is None or (not fence.half_open and row["state"] == "half_open"):
        raise ApiError("mcp.state_conflict")
    if fence.half_open and not (
        row["state"] == "half_open"
        and row["half_open_owner_type"] == fence.owner_type
        and row["half_open_owner_id"] == fence.owner_id
        and int(row["half_open_lease_generation"]) == fence.lease_generation
        and row["half_open_lease_owner_id"] == fence.lease_owner_id
        and hmac.compare_digest(_text(row["half_open_lease_token"]), fence.lease_token)
    ):
        raise ApiError("mcp.state_conflict")
    return row


def _complete_network_success(
    connection: sqlite3.Connection,
    fence: _NetworkFence,
    *,
    response: McpSandboxResponse,
    current: datetime,
) -> None:
    circuit = _require_network_fence(connection, fence)
    _insert_network_decision(
        connection,
        fence,
        response=response,
        result="allowed",
        reason_code="allowed",
        permit_digest=None,
        observed_circuit_revision=int(circuit["state_revision"]),
        current=current,
    )
    _close_network_circuit(connection, fence, circuit=circuit, current=current)


def _record_network_block(
    connection: sqlite3.Connection,
    fence: _NetworkFence,
    *,
    response: McpSandboxResponse,
    current: datetime,
) -> str:
    circuit = _require_network_fence(connection, fence)
    decision_id = _insert_network_decision(
        connection,
        fence,
        response=response,
        result="blocked",
        reason_code=_network_block_reason(response),
        permit_digest=None,
        observed_circuit_revision=int(circuit["state_revision"]),
        current=current,
    )
    _close_network_circuit(connection, fence, circuit=circuit, current=current)
    return decision_id


def _complete_network_failure(
    connection: sqlite3.Connection,
    fence: _NetworkFence,
    *,
    error_code: str,
    availability_reason: str | None = None,
    current: datetime,
    require_current_policy: bool = True,
) -> None:
    circuit = _require_network_fence(
        connection,
        fence,
        require_current_policy=require_current_policy,
    )
    failure_count = int(circuit["failure_count"]) + 1
    should_open = (
        fence.half_open or circuit["state"] == "open" or failure_count >= fence.failure_threshold
    )
    state = "open" if should_open else "closed"
    opened_until = (
        _db_time(current + timedelta(seconds=fence.open_seconds)) if should_open else None
    )
    last_failure = availability_reason or (
        "mcp.sandbox_unavailable"
        if error_code in {"mcp.sandbox_unavailable", "mcp.unavailable"}
        else "overall_timeout"
    )
    changed = connection.execute(
        """
        UPDATE mcp_network_circuits
        SET state=?,failure_count=?,opened_until=?,half_open_owner_type=NULL,
            half_open_owner_id=NULL,half_open_lease_generation=NULL,
            half_open_lease_owner_id=NULL,half_open_lease_token=NULL,
            half_open_lease_expires_at=NULL,last_failure_code=?,
            state_revision=state_revision+1,updated_at=?
        WHERE tenant_id=? AND mcp_server_id=? AND policy_revision=?
          AND state_revision=?
        """,
        (
            state,
            failure_count,
            opened_until,
            last_failure,
            _db_time(current),
            fence.tenant_id,
            fence.server_id,
            fence.policy_revision,
            circuit["state_revision"],
        ),
    ).rowcount
    if changed != 1:
        raise ApiError("mcp.state_conflict")


def _complete_network_call_success(
    connection: sqlite3.Connection,
    fence: _NetworkFence,
    *,
    current: datetime,
) -> None:
    circuit = _require_network_fence(
        connection,
        fence,
        require_current_policy=False,
    )
    _close_network_circuit(connection, fence, circuit=circuit, current=current)


def _complete_network_call_failure(
    connection: sqlite3.Connection,
    fence: _NetworkFence,
    *,
    network_response: McpSandboxResponse | None,
    current: datetime,
) -> None:
    _complete_network_failure(
        connection,
        fence,
        error_code="mcp.sandbox_unavailable",
        availability_reason=_network_failure_reason(
            network_response,
            error_code="mcp.sandbox_unavailable",
        ),
        current=current,
        require_current_policy=False,
    )


def _close_network_circuit(
    connection: sqlite3.Connection,
    fence: _NetworkFence,
    *,
    circuit: sqlite3.Row,
    current: datetime,
) -> int:
    changed = connection.execute(
        """
        UPDATE mcp_network_circuits
        SET state='closed',failure_count=0,opened_until=NULL,
            half_open_owner_type=NULL,half_open_owner_id=NULL,
            half_open_lease_generation=NULL,half_open_lease_owner_id=NULL,
            half_open_lease_token=NULL,half_open_lease_expires_at=NULL,
            last_failure_code=NULL,state_revision=state_revision+1,updated_at=?
        WHERE tenant_id=? AND mcp_server_id=? AND policy_revision=?
          AND state_revision=? AND state=?
        """,
        (
            _db_time(current),
            fence.tenant_id,
            fence.server_id,
            fence.policy_revision,
            circuit["state_revision"],
            circuit["state"],
        ),
    ).rowcount
    if changed != 1:
        raise ApiError("mcp.state_conflict")
    return int(circuit["state_revision"]) + 1


def _insert_network_decision(
    connection: sqlite3.Connection,
    fence: _NetworkFence,
    *,
    decision_id: str | None = None,
    response: McpSandboxResponse,
    result: str,
    reason_code: str,
    permit_digest: str | None,
    observed_circuit_revision: int | None = None,
    current: datetime,
) -> str:
    if decision_id is None:
        decision_id = f"mcpnd_{uuid4().hex}"
    connection.execute(
        """
        INSERT INTO mcp_network_decisions(
          mcp_network_decision_id,tenant_id,mcp_server_id,mcp_server_operation_id,
          mcp_invocation_id,mcp_network_policy_id,phase,result,reason_code,
          policy_revision,policy_fingerprint,address_count,duration_ms,
          attempt_number,dispatch_id,permit_digest,observed_circuit_revision,
          half_open_owner_type,half_open_owner_id,half_open_lease_generation,
          request_id,trace_id,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            decision_id,
            fence.tenant_id,
            fence.server_id,
            fence.owner_id if fence.owner_type == "operation" else None,
            fence.owner_id if fence.owner_type == "invocation" else None,
            fence.policy_id,
            fence.phase,
            result,
            reason_code,
            fence.policy_revision,
            fence.policy_fingerprint,
            response.address_count,
            response.duration_ms,
            fence.attempt_number,
            response.dispatch_id,
            permit_digest,
            (
                observed_circuit_revision
                if observed_circuit_revision is not None
                else fence.circuit_revision
            ),
            fence.owner_type if fence.half_open else None,
            fence.owner_id if fence.half_open else None,
            fence.lease_generation if fence.half_open else None,
            fence.request_id,
            fence.trace_id,
            _db_time(current),
        ),
    )
    return decision_id


def _create_network_event_outbox(
    connection: sqlite3.Connection,
    fence: _NetworkFence,
    *,
    decision_id: str,
    reason_code: str,
    address_count: int,
    attempt_number: int,
    current: datetime,
) -> None:
    timestamp = _db_time(current)
    event_id = f"mcpne_{uuid4().hex}"
    connection.execute(
        """
        INSERT INTO mcp_network_events(
          mcp_network_event_id,mcp_network_decision_id,tenant_id,mcp_server_id,
          mcp_server_operation_id,mcp_invocation_id,phase,reason_code,
          policy_revision,address_count,attempt_number,request_id,trace_id,occurred_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id,
            decision_id,
            fence.tenant_id,
            fence.server_id,
            fence.owner_id if fence.owner_type == "operation" else None,
            fence.owner_id if fence.owner_type == "invocation" else None,
            fence.phase,
            reason_code,
            fence.policy_revision,
            address_count,
            attempt_number,
            fence.request_id,
            fence.trace_id,
            timestamp,
        ),
    )
    event = _load_network_webhook_event(
        connection,
        tenant_id=fence.tenant_id,
        event_id=event_id,
    )
    from metahub_control.webhooks.service import freeze_webhook_event_delivery_snapshot

    freeze_webhook_event_delivery_snapshot(connection, event, frozen_at=timestamp)
    snapshot_json = _network_canonical_json(_network_delivery_ids(connection, event))
    connection.execute(
        """
        INSERT INTO mcp_network_event_outbox(
          mcp_network_event_outbox_id,tenant_id,mcp_network_event_id,status,
          attempt_count,lease_generation,lease_owner_id,lease_token,lease_expires_at,
          next_attempt_at,subscription_snapshot_json,snapshot_frozen_at,
          error_code,delivered_at,created_at,updated_at
        ) VALUES(?,?,?,'pending',0,0,NULL,NULL,NULL,?,?,?,NULL,NULL,?,?)
        """,
        (
            f"mcpno_{uuid4().hex}",
            fence.tenant_id,
            event_id,
            timestamp,
            snapshot_json,
            timestamp,
            timestamp,
            timestamp,
        ),
    )


def _network_block_reason(response: McpSandboxResponse | None) -> str:
    if response is None or response.status != "blocked":
        raise ApiError("mcp.state_conflict")
    reason = response.payload.get("reason_code")
    if reason not in {
        "policy_disabled",
        "address_denied",
        "mixed_dns_answers",
        "port_denied",
        "redirect_denied",
    }:
        raise ApiError("mcp.unavailable")
    return str(reason)


def _network_failure_reason(
    response: McpSandboxResponse | None,
    *,
    error_code: str,
) -> str:
    reason = (
        response.payload.get("availability_reason")
        if response is not None and response.status == "failed"
        else None
    )
    if reason in {
        "dns_unavailable",
        "connect_unavailable",
        "tls_unavailable",
        "headers_timeout",
        "body_timeout",
        "overall_timeout",
        "mcp.sandbox_unavailable",
    }:
        return str(reason)
    return (
        "mcp.sandbox_unavailable"
        if error_code in {"mcp.sandbox_unavailable", "mcp.unavailable"}
        else "overall_timeout"
    )


def _sandbox_http(config: AppConfig) -> bool:
    return getattr(config, "mcp_executor_mode", None) == "sandbox_http"


def _sync_error_code(job_type: str, code: str) -> str:
    if code == "mcp.executor_disabled":
        return code
    if job_type == "credential_probe" and code in {
        "mcp.server_unavailable",
        "mcp.sync_failed",
    }:
        return "mcp.credential_invalid"
    if code in {"mcp.state_conflict", "mcp.unavailable"}:
        return code
    return "mcp.sync_failed"


def _probe_backoff(attempt: int) -> int:
    return {1: 5, 2: 30}.get(attempt, 300)


def _duration_ms(started_at: object, current: datetime) -> int:
    try:
        started = datetime.strptime(_text(started_at), _TIMESTAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        raise ApiError("mcp.unavailable") from None
    return max(0, min(300_000, int((current - started).total_seconds() * 1_000)))


def _bounded_metric(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 1_048_576:
        raise ApiError("mcp.unavailable")
    return value


def _json_string_list(value: object) -> list[str]:
    try:
        decoded = json.loads(_text(value))
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ApiError("mcp.unavailable") from None
    if type(decoded) is not list or any(type(item) is not str for item in decoded):
        raise ApiError("mcp.unavailable")
    return decoded


def _transaction_now(connection: sqlite3.Connection, override: datetime | None) -> datetime:
    if override is not None:
        if override.tzinfo is None:
            raise ApiError("mcp.unavailable")
        return override.astimezone(UTC).replace(microsecond=0)
    row = connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')").fetchone()
    if row is None:
        raise ApiError("mcp.unavailable")
    return datetime.strptime(_text(row[0]), _TIMESTAMP_FORMAT).replace(tzinfo=UTC)


def _db_time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ApiError("mcp.unavailable")
    return value.astimezone(UTC).replace(microsecond=0).strftime(_TIMESTAMP_FORMAT)


def _parse_db_time(value: object) -> datetime:
    try:
        return datetime.strptime(_text(value), _TIMESTAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        raise ApiError("mcp.unavailable") from None


def _remaining_deadline_seconds(
    deadline: datetime,
    *,
    lease_expires_at: datetime,
    configured: object,
    now: datetime | None = None,
) -> int | None:
    if deadline.tzinfo is None or lease_expires_at.tzinfo is None or type(configured) is not int:
        raise ApiError("mcp.unavailable")
    current = (now or datetime.now(UTC)).astimezone(UTC)
    effective_deadline = min(deadline.astimezone(UTC), lease_expires_at.astimezone(UTC))
    available = int((effective_deadline - current).total_seconds()) - _SYNC_FINALIZE_RESERVE_SECONDS
    if available < 1:
        return None
    return min(available, max(1, min(configured, 120)))


def _sandbox_sync_dispatch_deadline(
    claim: McpSyncJobClaim,
    *,
    configured: object,
    now: datetime | None = None,
) -> datetime | None:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    remaining = _remaining_deadline_seconds(
        claim.deadline_at,
        lease_expires_at=claim.lease_expires_at,
        configured=(
            configured + MCP_SANDBOX_RESPONSE_RESERVE_SECONDS
            if type(configured) is int
            else configured
        ),
        now=current,
    )
    return current + timedelta(seconds=remaining) if remaining is not None else None


def _poll_seconds(config: AppConfig) -> float:
    value = config.mcp_scheduler_poll_interval_seconds
    if type(value) not in {int, float} or isinstance(value, bool):
        return 2.0
    return min(max(float(value), 0.1), 60.0)


def _tenant_lease_seconds(config: AppConfig) -> int:
    return max(_operation_lease_seconds(config), _invocation_lease_seconds(config))


def _tenant_concurrency(config: AppConfig) -> int:
    return max(1, min(int(config.mcp_tenant_concurrency), 32))


def _operation_lease_seconds(config: AppConfig) -> int:
    return max(30, min(int(config.mcp_operation_lease_seconds), 3_600))


def _invocation_lease_seconds(config: AppConfig) -> int:
    return max(30, min(int(config.mcp_invocation_lease_seconds), 3_600))


def _worker_id(value: object) -> str:
    if type(value) is not str or _WORKER_ID.fullmatch(value) is None:
        raise ApiError("mcp.unavailable")
    return value


def _text(value: object) -> str:
    if type(value) is not str or not value:
        raise ApiError("mcp.unavailable")
    return value


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


def _warning(event: str, error_code: str) -> None:
    logger.warning(
        "mcp recovery iteration failed",
        extra={"event": event, "error_code": error_code},
    )


__all__ = [
    "McpInvocationClaim",
    "McpSchedulerTenantLease",
    "McpSyncJobClaim",
    "claim_next_scheduler_tenant",
    "install_mcp_scheduler",
    "run_mcp_outbox_once",
    "run_mcp_scheduler_once",
]
