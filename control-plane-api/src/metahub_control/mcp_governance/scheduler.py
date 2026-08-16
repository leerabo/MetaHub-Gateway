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
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi import FastAPI
from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.mcp import (
    McpContractError,
    McpCryptoOwner,
    McpHmacOwner,
    decrypt_mcp_value_from_keyring,
    encrypt_mcp_value,
    mcp_hmac,
    project_mcp_result,
    validate_result,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

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
    """Recover one persisted MCP source event without logging its payload."""

    try:
        return drain_mcp_invocation_event_once(
            config,
            worker_id=worker_id,
            should_stop=stop_event.is_set if stop_event is not None else None,
        )
    except ApiError as exc:
        _warning("mcp.outbox_failed", exc.code)
        return False
    except Exception:
        _warning("mcp.outbox_failed", "mcp.unavailable")
        return False


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
        "error_code": (
            _text(row["error_code"]) if row["error_code"] is not None else None
        ),
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
) -> None:
    allowed = {
        "mcp.executor_disabled",
        "mcp.sync_failed",
        "mcp.credential_invalid",
        "mcp.state_conflict",
        "mcp.unavailable",
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
                    code
                    if code in {"mcp.result_invalid", "mcp.unavailable"}
                    else "mcp.unavailable"
                ),
            )
        return
    except Exception:
        _finalize_call_unknown(config, call_material)
        return
    _finalize_call_result(config, call_material, result)


def _prepare_invocation(
    config: AppConfig, claim: McpInvocationClaim
) -> _InvocationMaterial | None:
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
                    if failure in {
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
        or tool["result_redaction_pointers_json"]
        != invocation["result_redaction_pointers_json"]
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
        return (
            None
            if invocation["approval_task_id"] is None
            else "mcp.authorization_revoked"
        )
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
            input_schema_original_sha256=_text(
                invocation["input_schema_original_sha256"]
            ),
            input_schema_effective_sha256=_text(
                invocation["input_schema_effective_sha256"]
            ),
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
            required_permissions_sha256=_text(
                invocation["required_permissions_sha256"]
            ),
            grant_sha256=_text(invocation["grant_sha256"]),
            arguments_hmac=_text(invocation["arguments_hmac"]),
            arguments_hmac_key_version=int(
                invocation["arguments_hmac_key_version"]
            ),
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
) -> None:
    allowed = {
        "mcp.executor_disabled",
        "mcp.authorization_revoked",
        "mcp.server_unavailable",
        "mcp.state_conflict",
        "mcp.unavailable",
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
        if code == "mcp.server_unavailable" and claim.attempt_number < _MAX_PROBE_ATTEMPTS:
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
                lease_expires_at=?,next_attempt_at=NULL,request_bytes=?,updated_at=?
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
                int(invocation["arguments_bytes"]),
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
) -> None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, None)
        timestamp = _db_time(current)
        invocation, attempt = _require_call_claim(
            connection, material, timestamp=timestamp
        )
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
        terminal = _load_invocation_row(
            connection, material.tenant_id, material.invocation_id
        )
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
) -> None:
    code = (
        error_code
        if error_code in {"mcp.result_invalid", "mcp.unavailable"}
        else "mcp.unavailable"
    )
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, None)
        timestamp = _db_time(current)
        invocation, attempt = _require_call_claim(
            connection, material, timestamp=timestamp
        )
        _finalize_known_call_in_transaction(
            connection,
            invocation,
            attempt,
            status="failed",
            error_code=code,
            timestamp=timestamp,
            current=current,
            request_bytes=int(attempt["request_bytes"]),
            response_bytes=0,
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


def _finalize_call_unknown(config: AppConfig, material: _CallMaterial) -> None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, None)
        timestamp = _db_time(current)
        invocation, attempt = _require_call_claim(
            connection, material, timestamp=timestamp
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
        terminal = _load_invocation_row(
            connection, material.tenant_id, material.invocation_id
        )
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
            current = await asyncio.to_thread(
                run_mcp_outbox_once, config, stop_event=stop_event
            )
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


def _transaction_now(
    connection: sqlite3.Connection, override: datetime | None
) -> datetime:
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
    if (
        deadline.tzinfo is None
        or lease_expires_at.tzinfo is None
        or type(configured) is not int
    ):
        raise ApiError("mcp.unavailable")
    current = (now or datetime.now(UTC)).astimezone(UTC)
    effective_deadline = min(
        deadline.astimezone(UTC), lease_expires_at.astimezone(UTC)
    )
    available = (
        int((effective_deadline - current).total_seconds())
        - _SYNC_FINALIZE_RESERVE_SECONDS
    )
    if available < 1:
        return None
    return min(available, max(1, min(configured, 120)))


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
