"""Fleet health projections and controlled Edge diagnostics for PRD-126."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta

from metahub_shared.private_gateway import (
    DIAGNOSTIC_COMMAND_SCHEMA_VERSION,
    DIAGNOSTIC_SIGNATURE_PURPOSE,
    MAX_DIAGNOSTIC_RESULT_BYTES,
    DiagnosticCommandEnvelope,
    DiagnosticExecutionReport,
    DiagnosticGrantContract,
    DiagnosticInvocation,
    DiagnosticResult,
    DiagnosticUnsignedCommand,
    canonical_diagnostic_bytes,
    diagnostic_grant_sha256,
    diagnostic_invocation_sha256,
    reject_forbidden_diagnostic_fields,
    sign_diagnostic_command,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from pydantic import TypeAdapter

from metahub_control.identity.security_service import (
    consume_global_step_up_grant_in_transaction,
    consume_step_up_grant_in_transaction,
)
from metahub_control.platform_access.service import PlatformContext
from metahub_control.workspace_access.edge_permissions import (
    load_verified_platform_edge_permissions,
    load_verified_workspace_edge_permissions,
)
from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    authenticate_workspace_session_in_transaction,
)

from .models import (
    EdgeDiagnosticCommandMutationRequest,
    EdgeDiagnosticCommandResponse,
    EdgeDiagnosticCommandSummary,
    EdgeDiagnosticGrantMutationRequest,
    EdgeDiagnosticGrantResponse,
    EdgeDiagnosticGrantSummary,
    EdgeDiagnosticLeaseRequest,
    EdgeDiagnosticLeaseResponse,
    EdgeDiagnosticPendingResponse,
    EdgeDiagnosticResultResponse,
    EdgeDiagnosticResultSummary,
    EdgeHealthResponse,
    EdgeHealthSnapshotSummary,
    PlatformFleetDeployment,
    PlatformFleetPageResponse,
)
from .service import (
    _connect,
    _datetime,
    _db_time,
    _digest,
    _id,
    _identifier,
    _owned_deployment,
    _rollback,
    _secret_hmac,
    _utcnow,
)

_INVOCATION = TypeAdapter(DiagnosticInvocation)
_RESULT = TypeAdapter(DiagnosticResult)
_REGION_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")
_FLEET_STATUSES = frozenset({"pending", "active", "revoked", "retired"})
_HEALTH_CODES = frozenset({"healthy", "degraded", "critical", "offline", "revoked", "unknown"})
_FLEET_CURSOR_DOMAIN = b"prd126/platform-fleet-cursor/v1\0"
_LEASE_SECONDS = 30
_CLOCK_SKEW = timedelta(minutes=2)


def list_platform_fleet(
    config: AppConfig,
    context: PlatformContext,
    *,
    status_filter: str | None,
    region_filter: str | None,
    health_filter: str | None,
    cursor: str | None,
    limit: int,
    request_id: str,
    trace_id: str,
) -> PlatformFleetPageResponse:
    """Read only the field-trimmed 0118 index using a filter-bound keyset."""

    _require_platform(context, "platform.edge_fleet.read")
    if (
        status_filter is not None
        and status_filter not in _FLEET_STATUSES
        or region_filter is not None
        and _REGION_RE.fullmatch(region_filter) is None
        or health_filter is not None
        and health_filter not in _HEALTH_CODES
        or not 1 <= limit <= 200
    ):
        raise ApiError("platform.edge_fleet_request_invalid")
    filters = (status_filter, region_filter, health_filter)
    after = _decode_cursor(config, cursor, filters) if cursor is not None else None
    conditions: list[str] = []
    parameters: list[object] = []
    if status_filter is not None:
        conditions.append("status=?")
        parameters.append(status_filter)
    if region_filter is not None:
        conditions.append("region_code=?")
        parameters.append(region_filter)
    if health_filter is not None:
        conditions.append("health_code=?")
        parameters.append(health_filter)
    if after is not None:
        conditions.append(
            "(status,region_code,health_code,edge_deployment_id)>(?,?,?,?)"
        )
        parameters.extend(after)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    connection = _connect(config)
    try:
        rows = connection.execute(
            f"""
            SELECT edge_deployment_id,region_code,mode,status,health_code,health_score,
              alert_code,maintenance_mode,agent_version,desired_generation,
              applied_generation,lkg_generation,telemetry_lag_seconds,
              certificate_signal,source_health_generation,index_revision,updated_at
            FROM edge_fleet_deployment_index
            {where}
            ORDER BY status,region_code,health_code,edge_deployment_id
            LIMIT ?
            """,
            (*parameters, limit + 1),
        ).fetchall()
        visible = rows[:limit]
        next_cursor = None
        if len(rows) > limit and visible:
            last = visible[-1]
            next_cursor = _encode_cursor(
                config,
                filters,
                (
                    str(last["status"]),
                    str(last["region_code"]),
                    str(last["health_code"]),
                    str(last["edge_deployment_id"]),
                ),
            )
        deployments = tuple(_fleet_summary(row) for row in visible)
        return PlatformFleetPageResponse(
            deployments=deployments,
            next_cursor=next_cursor,
            count=len(deployments),
            request_id=_identifier(request_id),
            trace_id=_identifier(trace_id),
        )
    except ApiError:
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        raise ApiError("platform.edge_fleet_unavailable") from None
    finally:
        connection.close()


def get_workspace_edge_health(
    config: AppConfig,
    deployment_id: str,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> EdgeHealthResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = _workspace_context(
            connection,
            config,
            session_token=session_token,
            csrf_token=None,
            permission="edge.health.read",
        )
        deployment = _owned_deployment(connection, context, deployment_id)
        snapshot = refresh_edge_health_projection_in_transaction(
            connection,
            deployment_id,
            request_id=request_id,
            trace_id=trace_id,
            now=_utcnow(),
        )
        grants: tuple[sqlite3.Row, ...] | list[sqlite3.Row] = ()
        results: tuple[sqlite3.Row, ...] | list[sqlite3.Row] = ()
        if "edge.diagnostic.read" in context.permission_codes:
            grants = connection.execute(
                """
                SELECT * FROM edge_diagnostic_grants
                WHERE workspace_id=? AND tenant_id=? AND edge_deployment_id=?
                ORDER BY updated_at DESC,diagnostic_grant_id DESC LIMIT 20
                """,
                (
                    context.workspace_id,
                    context.tenant_id,
                    deployment["edge_deployment_id"],
                ),
            ).fetchall()
            results = connection.execute(
                """
                SELECT * FROM edge_diagnostic_results
                WHERE workspace_id=? AND tenant_id=? AND edge_deployment_id=?
                ORDER BY completed_at DESC,diagnostic_result_id DESC LIMIT 20
                """,
                (
                    context.workspace_id,
                    context.tenant_id,
                    deployment["edge_deployment_id"],
                ),
            ).fetchall()
        response = EdgeHealthResponse(
            health=_health_summary(snapshot),
            diagnostic_grants=tuple(_grant_summary(row) for row in grants),
            diagnostic_results=tuple(_result_summary(row) for row in results),
            request_id=_identifier(request_id),
            trace_id=_identifier(trace_id),
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("edge.unavailable") from None
    finally:
        connection.close()


def request_platform_diagnostic_grant(
    config: AppConfig,
    context: PlatformContext,
    deployment_id: str,
    payload: EdgeDiagnosticGrantMutationRequest,
    *,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> EdgeDiagnosticGrantResponse:
    _require_platform(context, "platform.edge_fleet.manage")
    if payload.action != "request" or payload.invocation is None or payload.max_runs is None:
        raise ApiError("edge.diagnostic_request_invalid")
    invocation = payload.invocation
    parameters_sha256 = diagnostic_invocation_sha256(invocation)
    request_sha256 = _digest(payload.model_dump(mode="json", exclude_none=True))
    idempotency_sha256 = _platform_idempotency(
        config, context.operator_id, "edge.diagnostic.request_grant", idempotency_key
    )
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        replay = _action_command(
            connection,
            actor_domain="platform",
            actor_id=context.operator_id,
            action="edge.diagnostic.request_grant",
            idempotency_sha256=idempotency_sha256,
        )
        if replay is not None:
            _assert_replay(replay, request_sha256)
            row = _diagnostic_grant(connection, deployment_id, str(replay["result_object_id"]))
            result = _grant_response(row, request_id, trace_id, replay=True)
            connection.commit()
            return result
        deployment = _deployment(connection, deployment_id)
        if str(deployment["status"]) != "active":
            raise ApiError("edge.diagnostic_state_conflict")
        _platform_authority_current(connection, context)
        now = _db_time(_utcnow())
        grant_id = _id("dgr_")
        invocation_bytes = canonical_diagnostic_bytes(
            invocation.model_dump(mode="json"), maximum=4096
        )
        connection.execute(
            """
            INSERT INTO edge_diagnostic_grants(
              diagnostic_grant_id,workspace_id,tenant_id,edge_deployment_id,status,
              command_code,invocation_rfc8785,parameters_sha256,reason_code,
              requested_by_platform_operator_id,requested_by_platform_account_id,
              platform_authority_revision,approved_by_account_id,approved_by_membership_id,
              workspace_authority_revision,deployment_revision,revocation_generation,
              max_runs,run_count,revision,request_idempotency_sha256,request_sha256,
              requested_at,approved_at,expires_at,revoked_at,updated_at)
            VALUES(?,?,?,?,'pending_customer',?,?,?,?,?,?,?,NULL,NULL,NULL,?,?,?,0,1,?,?,?,
              NULL,NULL,NULL,?)
            """,
            (
                grant_id,
                deployment["workspace_id"],
                deployment["tenant_id"],
                deployment_id,
                invocation.command_code,
                invocation_bytes.decode("utf-8"),
                parameters_sha256,
                payload.reason_code,
                context.operator_id,
                context.account_id,
                context.operator_revision,
                deployment["revision"],
                deployment["revocation_generation"],
                payload.max_runs,
                idempotency_sha256,
                request_sha256,
                now,
                now,
            ),
        )
        _insert_action_command(
            connection,
            workspace_id=str(deployment["workspace_id"]),
            tenant_id=str(deployment["tenant_id"]),
            actor_domain="platform",
            actor_id=context.operator_id,
            action="edge.diagnostic.request_grant",
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            result_object_id=grant_id,
            now=now,
        )
        _insert_event(
            connection,
            deployment,
            grant_id=grant_id,
            command_id=None,
            event_type="edge.diagnostic_requested",
            actor_domain="platform",
            actor_id=context.operator_id,
            reason=payload.reason_code,
            outcome="requested",
            summary={"command_code": invocation.command_code, "max_runs": payload.max_runs},
            request_id=request_id,
            trace_id=trace_id,
            now=now,
            domains=("workspace", "fleet", "security"),
        )
        row = _diagnostic_grant(connection, deployment_id, grant_id)
        result = _grant_response(row, request_id, trace_id, replay=False)
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge.diagnostic_unavailable") from None
    finally:
        connection.close()


def decide_workspace_diagnostic_grant(
    config: AppConfig,
    deployment_id: str,
    payload: EdgeDiagnosticGrantMutationRequest,
    *,
    session_token: str,
    csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> EdgeDiagnosticGrantResponse:
    if payload.action not in {"approve", "revoke"} or payload.diagnostic_grant_id is None:
        raise ApiError("edge.diagnostic_request_invalid")
    action = f"edge.diagnostic.{payload.action}_grant"
    request_sha256 = _digest(payload.model_dump(mode="json", exclude_none=True))
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = _workspace_context(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            permission="edge.diagnostic.approve",
        )
        deployment = _owned_deployment(connection, context, deployment_id)
        idempotency_sha256 = _workspace_idempotency(
            config, context, action, idempotency_key
        )
        replay = _action_command(
            connection,
            actor_domain="workspace",
            actor_id=context.membership_id,
            action=action,
            idempotency_sha256=idempotency_sha256,
        )
        if replay is not None:
            _assert_replay(replay, request_sha256)
            row = _diagnostic_grant(
                connection, deployment_id, str(replay["result_object_id"])
            )
            result = _grant_response(row, request_id, trace_id, replay=True)
            connection.commit()
            return result
        grant = _diagnostic_grant(connection, deployment_id, payload.diagnostic_grant_id)
        if (
            str(grant["workspace_id"]) != context.workspace_id
            or str(grant["tenant_id"]) != context.tenant_id
        ):
            raise ApiError("edge.diagnostic_grant_not_found")
        now_value = _utcnow()
        consume_step_up_grant_in_transaction(
            connection,
            config,
            context=context,
            grant=step_up_grant,
            action_code=action,
            resource_type="edge_diagnostic_grant",
            resource_id=payload.diagnostic_grant_id,
            now=now_value,
        )
        now = _strictly_after(now_value, str(grant["updated_at"]))
        if payload.action == "approve":
            if str(grant["status"]) != "pending_customer":
                raise ApiError("edge.diagnostic_state_conflict")
            if (
                context.account_id == str(grant["requested_by_platform_account_id"])
                or str(deployment["status"]) != "active"
                or int(deployment["revision"]) != int(grant["deployment_revision"])
                or int(deployment["revocation_generation"])
                != int(grant["revocation_generation"])
            ):
                raise ApiError("edge.diagnostic_authority_changed")
            membership = _active_membership(connection, context.membership_id)
            expires = _db_time(now_value + timedelta(seconds=int(payload.ttl_seconds or 0)))
            updated = connection.execute(
                """
                UPDATE edge_diagnostic_grants
                SET status='active',approved_by_account_id=?,approved_by_membership_id=?,
                  workspace_authority_revision=?,approved_at=?,expires_at=?,revision=revision+1,
                  updated_at=?
                WHERE diagnostic_grant_id=? AND status='pending_customer' AND revision=?
                """,
                (
                    context.account_id,
                    context.membership_id,
                    membership["revision"],
                    _db_time(now_value),
                    expires,
                    now,
                    payload.diagnostic_grant_id,
                    grant["revision"],
                ),
            ).rowcount
            outcome = "approved"
        else:
            if str(grant["status"]) not in {"pending_customer", "active"}:
                raise ApiError("edge.diagnostic_state_conflict")
            updated = connection.execute(
                """
                UPDATE edge_diagnostic_grants
                SET status='revoked',revoked_at=?,revision=revision+1,updated_at=?
                WHERE diagnostic_grant_id=? AND status IN('pending_customer','active')
                  AND revision=?
                """,
                (
                    _db_time(now_value),
                    now,
                    payload.diagnostic_grant_id,
                    grant["revision"],
                ),
            ).rowcount
            _cancel_grant_commands(
                connection, payload.diagnostic_grant_id, now_value, completed_at=_db_time(now_value)
            )
            outcome = "cancelled"
        if updated != 1:
            raise ApiError("edge.diagnostic_state_conflict")
        _insert_action_command(
            connection,
            workspace_id=context.workspace_id,
            tenant_id=context.tenant_id,
            actor_domain="workspace",
            actor_id=context.membership_id,
            action=action,
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            result_object_id=payload.diagnostic_grant_id,
            now=now,
        )
        _insert_event(
            connection,
            deployment,
            grant_id=payload.diagnostic_grant_id,
            command_id=None,
            event_type="edge.diagnostic_requested",
            actor_domain="workspace",
            actor_id=context.membership_id,
            reason=payload.reason_code,
            outcome=outcome,
            summary={"action": payload.action},
            request_id=request_id,
            trace_id=trace_id,
            now=now,
            domains=("workspace", "fleet", "security"),
        )
        row = _diagnostic_grant(connection, deployment_id, payload.diagnostic_grant_id)
        result = _grant_response(row, request_id, trace_id, replay=False)
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge.diagnostic_unavailable") from None
    finally:
        connection.close()


def mutate_platform_diagnostic_command(
    config: AppConfig,
    context: PlatformContext,
    deployment_id: str,
    payload: EdgeDiagnosticCommandMutationRequest,
    *,
    step_up_grant: str | None,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> EdgeDiagnosticCommandResponse:
    _require_platform(context, "platform.edge_fleet.manage")
    action = f"edge.diagnostic.{payload.action}"
    request_sha256 = _digest(payload.model_dump(mode="json", exclude_none=True))
    idempotency_sha256 = _platform_idempotency(
        config, context.operator_id, action, idempotency_key
    )
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        replay = _action_command(
            connection,
            actor_domain="platform",
            actor_id=context.operator_id,
            action=action,
            idempotency_sha256=idempotency_sha256,
        )
        if replay is not None:
            _assert_replay(replay, request_sha256)
            command = _diagnostic_command(
                connection, deployment_id, str(replay["result_object_id"])
            )
            result = _command_response(command, request_id, trace_id, replay=True)
            connection.commit()
            return result
        deployment = _deployment(connection, deployment_id)
        _platform_authority_current(connection, context)
        now_value = _utcnow()
        if payload.action == "run":
            if (
                payload.diagnostic_grant_id is None
                or payload.deadline_seconds is None
                or step_up_grant is None
            ):
                raise ApiError("edge.diagnostic_request_invalid")
            grant = _diagnostic_grant(connection, deployment_id, payload.diagnostic_grant_id)
            _require_current_grant(connection, grant, deployment, now_value)
            if int(grant["run_count"]) >= int(grant["max_runs"]):
                raise ApiError("edge.diagnostic_grant_exhausted")
            consume_global_step_up_grant_in_transaction(
                connection,
                config,
                account_id=context.account_id,
                global_session_id=context.global_session_id,
                grant=step_up_grant,
                action_code=action,
                resource_type="edge_deployment",
                resource_id=deployment_id,
                now=now_value,
            )
            command = _insert_signed_command(
                connection,
                config,
                context,
                deployment,
                grant,
                deadline_seconds=payload.deadline_seconds,
                step_up_grant=step_up_grant,
                idempotency_sha256=idempotency_sha256,
                request_sha256=request_sha256,
                now=now_value,
            )
            reason = payload.reason_code
            outcome = "requested"
        else:
            if payload.diagnostic_command_id is None:
                raise ApiError("edge.diagnostic_request_invalid")
            command = _diagnostic_command(
                connection, deployment_id, payload.diagnostic_command_id
            )
            if str(command["status"]) not in {"authorized", "leased", "running"}:
                raise ApiError("edge.diagnostic_state_conflict")
            changed = _strictly_after(now_value, str(command["updated_at"]))
            updated = connection.execute(
                """
                UPDATE edge_diagnostic_commands
                SET status='cancelled',lease_owner_session_id=NULL,lease_nonce_sha256=NULL,
                  lease_expires_at=NULL,completed_at=?,revision=revision+1,updated_at=?
                WHERE diagnostic_command_id=? AND status IN('authorized','leased','running')
                  AND revision=?
                """,
                (
                    _db_time(now_value),
                    changed,
                    payload.diagnostic_command_id,
                    command["revision"],
                ),
            ).rowcount
            if updated != 1:
                raise ApiError("edge.diagnostic_state_conflict")
            command = _diagnostic_command(
                connection, deployment_id, payload.diagnostic_command_id
            )
            _finish_grant_if_needed(
                connection, str(command["diagnostic_grant_id"]), now_value
            )
            reason = payload.reason_code
            outcome = "cancelled"
        _insert_action_command(
            connection,
            workspace_id=str(deployment["workspace_id"]),
            tenant_id=str(deployment["tenant_id"]),
            actor_domain="platform",
            actor_id=context.operator_id,
            action=action,
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            result_object_id=str(command["diagnostic_command_id"]),
            now=_strictly_after(now_value, str(command["updated_at"])),
        )
        _insert_event(
            connection,
            deployment,
            grant_id=str(command["diagnostic_grant_id"]),
            command_id=str(command["diagnostic_command_id"]),
            event_type=(
                "edge.diagnostic_requested"
                if payload.action == "run"
                else "edge.diagnostic_completed"
            ),
            actor_domain="platform",
            actor_id=context.operator_id,
            reason=reason,
            outcome=outcome,
            summary={
                "command_code": str(command["command_code"]),
                "generation": int(command["generation"]),
            },
            request_id=request_id,
            trace_id=trace_id,
            now=_strictly_after(now_value, str(command["updated_at"])),
            domains=("workspace", "fleet", "security"),
        )
        current = _diagnostic_command(
            connection, deployment_id, str(command["diagnostic_command_id"])
        )
        result = _command_response(current, request_id, trace_id, replay=False)
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("edge.diagnostic_unavailable") from None
    finally:
        connection.close()


def pull_agent_diagnostic(
    config: AppConfig,
    deployment_id: str,
    *,
    verified_fingerprint: str,
    edge_session_token: str,
    request_id: str,
    trace_id: str,
) -> EdgeDiagnosticPendingResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        agent = _authenticated_agent(
            connection,
            config,
            deployment_id,
            verified_fingerprint,
            edge_session_token,
        )
        _require_diagnostics_capability(agent)
        now = _utcnow()
        _recover_commands(connection, deployment_id, now)
        command = connection.execute(
            """
            SELECT * FROM edge_diagnostic_commands
            WHERE edge_deployment_id=? AND status='authorized'
            ORDER BY generation,diagnostic_command_id LIMIT 1
            """,
            (deployment_id,),
        ).fetchone()
        while command is not None:
            grant = _diagnostic_grant(
                connection, deployment_id, str(command["diagnostic_grant_id"])
            )
            deployment = _deployment(connection, deployment_id)
            try:
                _require_current_grant(connection, grant, deployment, now)
                break
            except ApiError:
                _cancel_command(connection, command, now, expired=True)
                command = connection.execute(
                    """
                    SELECT * FROM edge_diagnostic_commands
                    WHERE edge_deployment_id=? AND status='authorized'
                    ORDER BY generation,diagnostic_command_id LIMIT 1
                    """,
                    (deployment_id,),
                ).fetchone()
        cancelled = tuple(
            str(row[0])
            for row in connection.execute(
                """
                SELECT diagnostic_command_id FROM edge_diagnostic_commands
                WHERE edge_deployment_id=? AND status IN('cancelled','expired')
                ORDER BY updated_at DESC,diagnostic_command_id DESC LIMIT 20
                """,
                (deployment_id,),
            ).fetchall()
        )
        envelope = None
        revision = None
        if command is not None:
            envelope = DiagnosticCommandEnvelope.model_validate(
                json.loads(str(command["envelope_rfc8785"]))
            )
            revision = int(command["revision"])
        result = EdgeDiagnosticPendingResponse(
            deployment_id=deployment_id,
            command=envelope,
            command_revision=revision,
            cancelled_command_ids=cancelled,
            request_id=_identifier(request_id),
            trace_id=_identifier(trace_id),
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("edge.diagnostic_unavailable") from None
    finally:
        connection.close()


def advance_agent_diagnostic(
    config: AppConfig,
    deployment_id: str,
    command_id: str,
    payload: EdgeDiagnosticLeaseRequest,
    *,
    verified_fingerprint: str,
    edge_session_token: str,
    request_id: str,
    trace_id: str,
) -> EdgeDiagnosticLeaseResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        agent = _authenticated_agent(
            connection,
            config,
            deployment_id,
            verified_fingerprint,
            edge_session_token,
        )
        _require_diagnostics_capability(agent)
        now = _utcnow()
        _recover_commands(connection, deployment_id, now)
        command = _diagnostic_command(connection, deployment_id, command_id)
        if int(command["revision"]) != payload.expected_revision:
            raise ApiError("edge.diagnostic_revision_conflict")
        grant = _diagnostic_grant(
            connection, deployment_id, str(command["diagnostic_grant_id"])
        )
        deployment = _deployment(connection, deployment_id)
        _require_current_grant(connection, grant, deployment, now)
        nonce_sha256 = _secret_hmac(
            config, f"diagnostic-lease\0{command_id}", payload.lease_nonce
        )
        changed = _strictly_after(now, str(command["updated_at"]))
        if payload.action == "lease":
            if str(command["status"]) != "authorized":
                raise ApiError("edge.diagnostic_state_conflict")
            lease_expires = min(
                now + timedelta(seconds=_LEASE_SECONDS), _datetime(command["deadline_at"])
            )
            if lease_expires <= now:
                raise ApiError("edge.diagnostic_expired")
            updated = connection.execute(
                """
                UPDATE edge_diagnostic_commands
                SET status='leased',lease_owner_session_id=?,lease_nonce_sha256=?,
                  lease_expires_at=?,revision=revision+1,updated_at=?
                WHERE diagnostic_command_id=? AND status='authorized' AND revision=?
                """,
                (
                    agent["edge_session_id"],
                    nonce_sha256,
                    _db_time(lease_expires),
                    changed,
                    command_id,
                    payload.expected_revision,
                ),
            ).rowcount
        else:
            if (
                str(command["status"]) != "leased"
                or str(command["lease_owner_session_id"]) != str(agent["edge_session_id"])
                or command["lease_nonce_sha256"] is None
                or not hmac.compare_digest(str(command["lease_nonce_sha256"]), nonce_sha256)
                or command["lease_expires_at"] is None
                or _datetime(command["lease_expires_at"]) <= now
            ):
                raise ApiError("edge.diagnostic_lease_invalid")
            updated = connection.execute(
                """
                UPDATE edge_diagnostic_commands
                SET status='running',started_at=?,lease_expires_at=deadline_at,
                  revision=revision+1,updated_at=?
                WHERE diagnostic_command_id=? AND status='leased' AND revision=?
                  AND lease_owner_session_id=? AND lease_nonce_sha256=?
                """,
                (
                    _db_time(now),
                    changed,
                    command_id,
                    payload.expected_revision,
                    agent["edge_session_id"],
                    nonce_sha256,
                ),
            ).rowcount
        if updated != 1:
            raise ApiError("edge.diagnostic_state_conflict")
        current = _diagnostic_command(connection, deployment_id, command_id)
        result = EdgeDiagnosticLeaseResponse(
            command=_command_summary(current),
            request_id=_identifier(request_id),
            trace_id=_identifier(trace_id),
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge.diagnostic_unavailable") from None
    finally:
        connection.close()


def record_agent_diagnostic_result(
    config: AppConfig,
    deployment_id: str,
    command_id: str,
    report: DiagnosticExecutionReport,
    *,
    verified_fingerprint: str,
    edge_session_token: str,
    request_id: str,
    trace_id: str,
) -> EdgeDiagnosticResultResponse:
    payload = report.model_dump(mode="json")
    reject_forbidden_diagnostic_fields(payload)
    report_bytes = canonical_diagnostic_bytes(payload, maximum=MAX_DIAGNOSTIC_RESULT_BYTES)
    report_sha256 = hashlib.sha256(report_bytes).hexdigest()
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        agent = _authenticated_agent(
            connection,
            config,
            deployment_id,
            verified_fingerprint,
            edge_session_token,
        )
        _require_diagnostics_capability(agent)
        if report.deployment_id != deployment_id or report.command_id != command_id:
            raise ApiError("edge.diagnostic_result_invalid")
        existing = connection.execute(
            "SELECT * FROM edge_diagnostic_results WHERE diagnostic_command_id=?",
            (command_id,),
        ).fetchone()
        if existing is not None:
            if not hmac.compare_digest(str(existing["report_sha256"]), report_sha256):
                raise ApiError("edge.diagnostic_idempotency_conflict")
            result = _result_response(existing, request_id, trace_id, replay=True)
            connection.commit()
            return result
        command = _diagnostic_command(connection, deployment_id, command_id)
        grant = _diagnostic_grant(
            connection, deployment_id, str(command["diagnostic_grant_id"])
        )
        if (
            report.grant_id != str(command["diagnostic_grant_id"])
            or report.generation != int(command["generation"])
            or report.result is not None
            and report.result.command_code != str(command["command_code"])
        ):
            raise ApiError("edge.diagnostic_result_invalid")
        command_status = str(command["status"])
        terminal_receipt = (
            command_status == "cancelled" and report.status == "cancelled"
            or command_status == "expired" and report.status in {"cancelled", "timed_out"}
        )
        if not terminal_receipt:
            if (
                command_status != "running"
                or str(command["lease_owner_session_id"]) != str(agent["edge_session_id"])
            ):
                raise ApiError("edge.diagnostic_state_conflict")
            deployment = _deployment(connection, deployment_id)
            _require_current_grant(connection, grant, deployment, _utcnow())
        started = _datetime(report.started_at)
        completed = _datetime(report.completed_at)
        if (
            started < _datetime(command["issued_at"]) - _CLOCK_SKEW
            or completed > _datetime(command["expires_at"]) + _CLOCK_SKEW
            or report.status == "completed"
            and completed > _datetime(command["deadline_at"])
        ):
            raise ApiError("edge.diagnostic_result_invalid")
        now_value = _utcnow()
        result_id = _id("drs_")
        connection.execute(
            """
            INSERT INTO edge_diagnostic_results(
              diagnostic_result_id,workspace_id,tenant_id,edge_deployment_id,
              diagnostic_grant_id,diagnostic_command_id,generation,status,reason_code,
              report_rfc8785,report_sha256,result_sha256,duration_ms,started_at,
              completed_at,received_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                result_id,
                command["workspace_id"],
                command["tenant_id"],
                deployment_id,
                command["diagnostic_grant_id"],
                command_id,
                report.generation,
                report.status,
                report.reason_code,
                report_bytes.decode("utf-8"),
                report_sha256,
                report.result_sha256,
                report.duration_ms,
                report.started_at,
                report.completed_at,
                _db_time(now_value),
            ),
        )
        if command_status == "running":
            terminal = {
                "completed": "completed",
                "failed": "failed",
                "cancelled": "cancelled",
                "timed_out": "expired",
            }[report.status]
            changed = _strictly_after(now_value, str(command["updated_at"]))
            updated = connection.execute(
                """
                UPDATE edge_diagnostic_commands
                SET status=?,lease_owner_session_id=NULL,lease_nonce_sha256=NULL,
                  lease_expires_at=NULL,completed_at=?,revision=revision+1,updated_at=?
                WHERE diagnostic_command_id=? AND status='running' AND revision=?
                """,
                (
                    terminal,
                    report.completed_at,
                    changed,
                    command_id,
                    command["revision"],
                ),
            ).rowcount
            if updated != 1:
                raise ApiError("edge.diagnostic_state_conflict")
        _finish_grant_if_needed(connection, str(command["diagnostic_grant_id"]), now_value)
        deployment = _deployment(connection, deployment_id)
        outcome = (
            "completed"
            if report.status == "completed"
            else "cancelled"
            if report.status in {"cancelled", "timed_out"}
            else "failed"
        )
        _insert_event(
            connection,
            deployment,
            grant_id=str(command["diagnostic_grant_id"]),
            command_id=command_id,
            event_type="edge.diagnostic_completed",
            actor_domain="device",
            actor_id=str(agent["edge_device_identity_id"]),
            reason=report.reason_code,
            outcome=outcome,
            summary={
                "duration_ms": report.duration_ms,
                "generation": report.generation,
                "status": report.status,
            },
            request_id=request_id,
            trace_id=trace_id,
            now=_db_time(now_value),
            domains=("workspace", "fleet", "security"),
        )
        row = connection.execute(
            "SELECT * FROM edge_diagnostic_results WHERE diagnostic_result_id=?",
            (result_id,),
        ).fetchone()
        if row is None:
            raise ApiError("edge.diagnostic_unavailable")
        result = _result_response(row, request_id, trace_id, replay=False)
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("edge.diagnostic_unavailable") from None
    finally:
        connection.close()


def refresh_edge_health_projection_in_transaction(
    connection: sqlite3.Connection,
    deployment_id: str,
    *,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> sqlite3.Row:
    """Append one deterministic health snapshot and atomically refresh the Fleet index."""

    if not connection.in_transaction:
        raise ApiError("edge.unavailable")
    deployment = _deployment(connection, deployment_id)
    desired = connection.execute(
        "SELECT * FROM edge_desired_state_heads WHERE edge_deployment_id=?",
        (deployment_id,),
    ).fetchone()
    telemetry = connection.execute(
        "SELECT * FROM edge_fleet_telemetry_projections WHERE edge_deployment_id=?",
        (deployment_id,),
    ).fetchone()
    validation = connection.execute(
        """
        SELECT * FROM edge_validation_runs WHERE edge_deployment_id=?
        ORDER BY created_at DESC,validation_run_id DESC LIMIT 1
        """,
        (deployment_id,),
    ).fetchone()
    released = connection.execute(
        """
        SELECT version FROM installer_releases WHERE status='released'
        ORDER BY approved_at DESC,installer_release_id DESC LIMIT 1
        """
    ).fetchone()
    signals = _health_signals(deployment, desired, telemetry, validation, released, now)
    desired_generation = int(desired["generation"]) if desired is not None else 0
    applied_generation = int(desired["applied_generation"]) if desired is not None else 0
    lkg_generation = int(desired["lkg_generation"]) if desired is not None else 0
    source_revision = max(
        int(deployment["revision"]),
        int(desired["revision"]) if desired is not None else 0,
        int(telemetry["revision"]) if telemetry is not None else 0,
        int(validation["attempt"]) if validation is not None else 0,
    )
    facts = {
        "agent_version": deployment["agent_version"],
        "applied_generation": applied_generation,
        "capacity_signal": signals["capacity_signal"],
        "certificate_expires_at": deployment["certificate_expires_at"],
        "certificate_signal": signals["certificate_signal"],
        "config_signal": signals["config_signal"],
        "deployment_id": deployment_id,
        "desired_generation": desired_generation,
        "health_code": signals["health_code"],
        "health_score": signals["health_score"],
        "lkg_generation": lkg_generation,
        "score_model_version": "edge-health-v1",
        "source_revision": source_revision,
        "telemetry_lag_seconds": (
            int(telemetry["lag_seconds"])
            if telemetry is not None and telemetry["lag_seconds"] is not None
            else None
        ),
        "telemetry_signal": signals["telemetry_signal"],
        "validation_signal": signals["validation_signal"],
        "version_signal": signals["version_signal"],
    }
    snapshot_sha256 = _digest(facts)
    head = connection.execute(
        "SELECT * FROM edge_health_heads WHERE edge_deployment_id=?", (deployment_id,)
    ).fetchone()
    if head is not None and hmac.compare_digest(
        str(head["snapshot_sha256"]), snapshot_sha256
    ):
        row = _health_snapshot(connection, deployment_id, int(head["generation"]))
        _refresh_fleet_index(connection, deployment, row, signals, now)
        return row
    generation = int(head["generation"]) + 1 if head is not None else 1
    prior_updated = str(head["updated_at"]) if head is not None else None
    now_db = _strictly_after(now, prior_updated)
    snapshot_id = _id("ehs_")
    connection.execute(
        """
        INSERT INTO edge_health_snapshots(
          edge_health_snapshot_id,workspace_id,tenant_id,edge_deployment_id,generation,
          source_code,source_revision,score_model_version,health_score,health_code,
          certificate_signal,version_signal,capacity_signal,config_signal,telemetry_signal,
          validation_signal,desired_generation,applied_generation,lkg_generation,
          telemetry_lag_seconds,certificate_expires_at,agent_version,snapshot_sha256,
          observed_at,created_at)
        VALUES(?,?,?,?,?,'runtime',?,'edge-health-v1',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            snapshot_id,
            deployment["workspace_id"],
            deployment["tenant_id"],
            deployment_id,
            generation,
            source_revision,
            signals["health_score"],
            signals["health_code"],
            signals["certificate_signal"],
            signals["version_signal"],
            signals["capacity_signal"],
            signals["config_signal"],
            signals["telemetry_signal"],
            signals["validation_signal"],
            desired_generation,
            applied_generation,
            lkg_generation,
            facts["telemetry_lag_seconds"],
            deployment["certificate_expires_at"],
            deployment["agent_version"],
            snapshot_sha256,
            now_db,
            now_db,
        ),
    )
    if head is None:
        connection.execute(
            """
            INSERT INTO edge_health_heads(
              edge_deployment_id,workspace_id,tenant_id,generation,
              edge_health_snapshot_id,snapshot_sha256,revision,updated_at)
            VALUES(?,?,?,?,?,?,1,?)
            """,
            (
                deployment_id,
                deployment["workspace_id"],
                deployment["tenant_id"],
                generation,
                snapshot_id,
                snapshot_sha256,
                now_db,
            ),
        )
    else:
        updated = connection.execute(
            """
            UPDATE edge_health_heads
            SET generation=?,edge_health_snapshot_id=?,snapshot_sha256=?,
              revision=revision+1,updated_at=?
            WHERE edge_deployment_id=? AND generation=? AND revision=?
            """,
            (
                generation,
                snapshot_id,
                snapshot_sha256,
                now_db,
                deployment_id,
                head["generation"],
                head["revision"],
            ),
        ).rowcount
        if updated != 1:
            raise ApiError("edge.health_state_conflict")
    row = _health_snapshot(connection, deployment_id, generation)
    _refresh_fleet_index(connection, deployment, row, signals, now)
    old_code = None
    if head is not None:
        old = _health_snapshot(connection, deployment_id, int(head["generation"]))
        old_code = str(old["health_code"])
    if old_code != signals["health_code"]:
        _insert_event(
            connection,
            deployment,
            grant_id=None,
            command_id=None,
            event_type="edge.health_changed",
            actor_domain="system",
            actor_id="health-projector",
            reason="health_projection_changed",
            outcome="observed",
            summary={
                "generation": generation,
                "health_code": signals["health_code"],
                "health_score": signals["health_score"],
                "previous_health_code": old_code or "unknown",
            },
            request_id=request_id,
            trace_id=trace_id,
            now=now_db,
            domains=(
                ("workspace", "fleet", "security")
                if signals["health_code"] in {"critical", "offline", "revoked"}
                else ("workspace", "fleet")
            ),
        )
    return row


def _health_signals(
    deployment: sqlite3.Row,
    desired: sqlite3.Row | None,
    telemetry: sqlite3.Row | None,
    validation: sqlite3.Row | None,
    released: sqlite3.Row | None,
    now: datetime,
) -> dict[str, object]:
    status = str(deployment["status"])
    score = 100
    expires = (
        _datetime(deployment["certificate_expires_at"])
        if deployment["certificate_expires_at"] is not None
        else None
    )
    if status == "revoked":
        certificate_signal = "revoked"
        score = 0
    elif expires is None:
        certificate_signal = "missing"
        score -= 25
    elif expires <= now:
        certificate_signal = "expired"
        score -= 35
    elif expires <= now + timedelta(days=7):
        certificate_signal = "expiring"
        score -= 15
    else:
        certificate_signal = "healthy"
    agent_version = deployment["agent_version"]
    current_version = released["version"] if released is not None else None
    if agent_version is None or current_version is None:
        version_signal = "unknown"
        score -= 10
    elif str(agent_version) == str(current_version):
        version_signal = "current"
    else:
        version_signal = "outdated"
        score -= 10
    if telemetry is None:
        capacity_signal = "unknown"
        score -= 10
    else:
        buckets = {str(telemetry["cpu_bucket"]), str(telemetry["memory_bucket"])}
        if "critical" in buckets:
            capacity_signal = "critical"
            score -= 20
        elif "high" in buckets:
            capacity_signal = "warning"
            score -= 10
        else:
            capacity_signal = "healthy"
    if desired is None or int(desired["generation"]) == 0:
        config_signal = "not_published"
        score -= 5
    elif int(desired["generation"]) == int(desired["applied_generation"]):
        config_signal = "converged"
    else:
        config_signal = "drifted"
        score -= 20
    lag = (
        int(telemetry["lag_seconds"])
        if telemetry is not None and telemetry["lag_seconds"] is not None
        else None
    )
    if telemetry is None or telemetry["last_receipt_at"] is None:
        telemetry_signal = "missing"
        score -= 20
    elif lag is None or lag > 600:
        telemetry_signal = "stale"
        score -= 25
    elif lag > 120:
        telemetry_signal = "lagging"
        score -= 15
    else:
        telemetry_signal = "current"
    if validation is None:
        validation_signal = "missing"
        score -= 5
    elif str(validation["status"]) != "completed":
        validation_signal = "pending"
        score -= 10
    elif str(validation["outcome"]) == "passed":
        validation_signal = "passed"
    else:
        validation_signal = "failed"
        score -= 20
    score = max(0, min(100, score))
    last_connected = (
        _datetime(deployment["last_connected_at"])
        if deployment["last_connected_at"] is not None
        else None
    )
    offline = status != "active" or (
        (last_connected is None or last_connected <= now - timedelta(minutes=10))
        and telemetry_signal in {"missing", "stale"}
    )
    if status == "revoked":
        health_code = "revoked"
    elif offline:
        health_code = "offline"
        score = min(score, 20)
    elif score >= 80:
        health_code = "healthy"
    elif score >= 50:
        health_code = "degraded"
    else:
        health_code = "critical"
    return {
        "capacity_signal": capacity_signal,
        "certificate_signal": certificate_signal,
        "config_signal": config_signal,
        "health_code": health_code,
        "health_score": score,
        "telemetry_signal": telemetry_signal,
        "validation_signal": validation_signal,
        "version_signal": version_signal,
    }


def _refresh_fleet_index(
    connection: sqlite3.Connection,
    deployment: sqlite3.Row,
    snapshot: sqlite3.Row,
    signals: dict[str, object],
    now: datetime,
) -> None:
    existing = connection.execute(
        "SELECT * FROM edge_fleet_deployment_index WHERE edge_deployment_id=?",
        (deployment["edge_deployment_id"],),
    ).fetchone()
    alert = _alert_code(str(snapshot["health_code"]), signals)
    if existing is None:
        connection.execute(
            """
            INSERT INTO edge_fleet_deployment_index(
              edge_deployment_id,workspace_id,tenant_id,region_code,mode,status,
              health_code,health_score,alert_code,maintenance_mode,agent_version,
              desired_generation,applied_generation,lkg_generation,telemetry_lag_seconds,
              certificate_signal,source_health_generation,index_revision,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,?,1,?)
            """,
            (
                deployment["edge_deployment_id"],
                deployment["workspace_id"],
                deployment["tenant_id"],
                deployment["region_code"],
                deployment["mode"],
                deployment["status"],
                snapshot["health_code"],
                snapshot["health_score"],
                alert,
                snapshot["agent_version"],
                snapshot["desired_generation"],
                snapshot["applied_generation"],
                snapshot["lkg_generation"],
                snapshot["telemetry_lag_seconds"],
                snapshot["certificate_signal"],
                snapshot["generation"],
                _db_time(now),
            ),
        )
        return
    projected = (
        str(deployment["region_code"]),
        str(deployment["mode"]),
        str(deployment["status"]),
        str(snapshot["health_code"]),
        int(snapshot["health_score"]),
        alert,
        snapshot["agent_version"],
        int(snapshot["desired_generation"]),
        int(snapshot["applied_generation"]),
        int(snapshot["lkg_generation"]),
        snapshot["telemetry_lag_seconds"],
        str(snapshot["certificate_signal"]),
        int(snapshot["generation"]),
    )
    current = (
        str(existing["region_code"]),
        str(existing["mode"]),
        str(existing["status"]),
        str(existing["health_code"]),
        int(existing["health_score"]),
        str(existing["alert_code"]),
        existing["agent_version"],
        int(existing["desired_generation"]),
        int(existing["applied_generation"]),
        int(existing["lkg_generation"]),
        existing["telemetry_lag_seconds"],
        str(existing["certificate_signal"]),
        int(existing["source_health_generation"]),
    )
    if projected == current:
        return
    changed = _strictly_after(now, str(existing["updated_at"]))
    updated = connection.execute(
        """
        UPDATE edge_fleet_deployment_index
        SET region_code=?,mode=?,status=?,health_code=?,health_score=?,alert_code=?,
          agent_version=?,desired_generation=?,applied_generation=?,lkg_generation=?,
          telemetry_lag_seconds=?,certificate_signal=?,source_health_generation=?,
          index_revision=index_revision+1,updated_at=?
        WHERE edge_deployment_id=? AND index_revision=?
        """,
        (
            *projected,
            changed,
            deployment["edge_deployment_id"],
            existing["index_revision"],
        ),
    ).rowcount
    if updated != 1:
        raise ApiError("edge.health_state_conflict")


def _alert_code(health_code: str, signals: dict[str, object]) -> str:
    if health_code == "revoked":
        return "revoked"
    if health_code == "offline":
        return "offline"
    for signal, code, healthy in (
        ("certificate_signal", "certificate", {"healthy"}),
        ("version_signal", "version", {"current"}),
        ("capacity_signal", "capacity", {"healthy"}),
        ("config_signal", "configuration", {"converged", "not_published"}),
        ("telemetry_signal", "telemetry", {"current"}),
        ("validation_signal", "validation", {"passed", "missing"}),
    ):
        if signals[signal] not in healthy:
            return code
    return "none"


def _insert_signed_command(
    connection: sqlite3.Connection,
    config: AppConfig,
    context: PlatformContext,
    deployment: sqlite3.Row,
    grant: sqlite3.Row,
    *,
    deadline_seconds: int,
    step_up_grant: str,
    idempotency_sha256: str,
    request_sha256: str,
    now: datetime,
) -> sqlite3.Row:
    from .desired_state_service import _signing_material

    invocation = _parse_invocation(str(grant["invocation_rfc8785"]))
    expires = _datetime(grant["expires_at"])
    deadline = now + timedelta(seconds=deadline_seconds)
    if deadline > expires:
        raise ApiError("edge.diagnostic_grant_expired")
    generation_row = connection.execute(
        """
        SELECT coalesce(max(generation),0)+1 generation
        FROM edge_diagnostic_commands WHERE edge_deployment_id=?
        """,
        (deployment["edge_deployment_id"],),
    ).fetchone()
    if generation_row is None:
        raise ApiError("edge.diagnostic_unavailable")
    generation = int(generation_row["generation"])
    grant_contract = DiagnosticGrantContract(
        grant_id=str(grant["diagnostic_grant_id"]),
        deployment_id=str(deployment["edge_deployment_id"]),
        invocation=invocation,
        parameters_sha256=str(grant["parameters_sha256"]),
        max_runs=int(grant["max_runs"]),
        deployment_revision=int(grant["deployment_revision"]),
        revocation_generation=int(grant["revocation_generation"]),
        workspace_authority_revision=int(grant["workspace_authority_revision"]),
        platform_authority_revision=int(grant["platform_authority_revision"]),
        approved_at=str(grant["approved_at"]),
        expires_at=str(grant["expires_at"]),
    )
    private_key, kid, _manifest_id = _signing_material(connection, config, now=now)
    command_id = _id("dcm_")
    unsigned = DiagnosticUnsignedCommand(
        schema_version=DIAGNOSTIC_COMMAND_SCHEMA_VERSION,
        signature_purpose=DIAGNOSTIC_SIGNATURE_PURPOSE,
        command_id=command_id,
        grant_id=str(grant["diagnostic_grant_id"]),
        deployment_id=str(deployment["edge_deployment_id"]),
        generation=generation,
        revocation_generation=int(deployment["revocation_generation"]),
        grant_sha256=diagnostic_grant_sha256(grant_contract),
        invocation=invocation,
        parameters_sha256=str(grant["parameters_sha256"]),
        issued_at=_db_time(now),
        not_before=_db_time(now),
        deadline_at=_db_time(deadline),
        expires_at=str(grant["expires_at"]),
        maximum_result_bytes=MAX_DIAGNOSTIC_RESULT_BYTES,
        algorithm="Ed25519",
        kid=kid,
    )
    envelope = sign_diagnostic_command(unsigned, private_key)
    envelope_bytes = canonical_diagnostic_bytes(envelope.model_dump(mode="json"), maximum=16384)
    envelope_sha256 = hashlib.sha256(envelope_bytes).hexdigest()
    changed = _strictly_after(now, str(grant["updated_at"]))
    updated = connection.execute(
        """
        UPDATE edge_diagnostic_grants
        SET run_count=run_count+1,revision=revision+1,updated_at=?
        WHERE diagnostic_grant_id=? AND status='active' AND revision=?
          AND run_count<max_runs
        """,
        (changed, grant["diagnostic_grant_id"], grant["revision"]),
    ).rowcount
    if updated != 1:
        raise ApiError("edge.diagnostic_grant_exhausted")
    connection.execute(
        """
        INSERT INTO edge_diagnostic_commands(
          diagnostic_command_id,workspace_id,tenant_id,edge_deployment_id,
          diagnostic_grant_id,generation,status,command_code,parameters_sha256,
          grant_sha256,envelope_rfc8785,envelope_sha256,
          requested_by_platform_operator_id,requested_by_platform_account_id,
          platform_authority_revision,step_up_grant_sha256,idempotency_sha256,
          request_sha256,revision,lease_owner_session_id,lease_nonce_sha256,
          lease_expires_at,issued_at,deadline_at,expires_at,started_at,completed_at,
          updated_at)
        VALUES(?,?,?,?,?,?,'authorized',?,?,?,?,?,?,?,?,?,?,?,1,NULL,NULL,NULL,?,?,?,NULL,NULL,?)
        """,
        (
            command_id,
            deployment["workspace_id"],
            deployment["tenant_id"],
            deployment["edge_deployment_id"],
            grant["diagnostic_grant_id"],
            generation,
            grant["command_code"],
            grant["parameters_sha256"],
            unsigned.grant_sha256,
            envelope_bytes.decode("utf-8"),
            envelope_sha256,
            context.operator_id,
            context.account_id,
            context.operator_revision,
            _secret_hmac(config, "diagnostic-command-step-up", step_up_grant),
            idempotency_sha256,
            request_sha256,
            unsigned.issued_at,
            unsigned.deadline_at,
            unsigned.expires_at,
            changed,
        ),
    )
    return _diagnostic_command(connection, str(deployment["edge_deployment_id"]), command_id)


def _require_current_grant(
    connection: sqlite3.Connection,
    grant: sqlite3.Row,
    deployment: sqlite3.Row,
    now: datetime,
) -> None:
    if (
        str(grant["status"]) != "active"
        or grant["expires_at"] is None
        or _datetime(grant["expires_at"]) <= now
        or str(deployment["status"]) != "active"
        or int(deployment["revision"]) != int(grant["deployment_revision"])
        or int(deployment["revocation_generation"]) != int(grant["revocation_generation"])
    ):
        raise ApiError("edge.diagnostic_authority_changed")
    platform = connection.execute(
        """
        SELECT operator.*,account.status account_status,account.login_enabled
        FROM platform_operators operator
        JOIN global_accounts account ON account.account_id=operator.account_id
        WHERE operator.operator_id=?
        """,
        (grant["requested_by_platform_operator_id"],),
    ).fetchone()
    if (
        platform is None
        or str(platform["status"]) != "active"
        or str(platform["account_status"]) != "active"
        or int(platform["login_enabled"]) != 1
        or int(platform["revision"]) != int(grant["platform_authority_revision"])
        or str(platform["account_id"]) != str(grant["requested_by_platform_account_id"])
    ):
        raise ApiError("edge.diagnostic_authority_changed")
    roles = tuple(
        str(row[0])
        for row in connection.execute(
            """
            SELECT role_code FROM platform_operator_role_bindings
            WHERE operator_id=? AND status='active' ORDER BY role_code
            """,
            (grant["requested_by_platform_operator_id"],),
        ).fetchall()
    )
    permissions = load_verified_platform_edge_permissions(
        connection, roles=roles, unavailable_error_code="edge.diagnostic_unavailable"
    )
    if "platform.edge_fleet.manage" not in permissions:
        raise ApiError("edge.diagnostic_authority_changed")
    membership = connection.execute(
        """
        SELECT * FROM workspace_memberships
        WHERE workspace_membership_id=? AND workspace_id=? AND tenant_id=?
          AND account_id=? AND status='active'
        """,
        (
            grant["approved_by_membership_id"],
            grant["workspace_id"],
            grant["tenant_id"],
            grant["approved_by_account_id"],
        ),
    ).fetchone()
    if (
        membership is None
        or int(membership["revision"]) != int(grant["workspace_authority_revision"])
    ):
        raise ApiError("edge.diagnostic_authority_changed")
    workspace_permissions = load_verified_workspace_edge_permissions(
        connection,
        role_code=str(membership["role_code"]),
        unavailable_error_code="edge.diagnostic_unavailable",
    )
    if "edge.diagnostic.approve" not in workspace_permissions:
        raise ApiError("edge.diagnostic_authority_changed")


def _recover_commands(
    connection: sqlite3.Connection, deployment_id: str, now: datetime
) -> None:
    rows = connection.execute(
        """
        SELECT * FROM edge_diagnostic_commands
        WHERE edge_deployment_id=? AND status IN('authorized','leased','running')
        ORDER BY generation
        """,
        (deployment_id,),
    ).fetchall()
    for row in rows:
        if _datetime(row["deadline_at"]) <= now:
            _cancel_command(connection, row, now, expired=True)
        elif (
            str(row["status"]) == "leased"
            and row["lease_expires_at"] is not None
            and _datetime(row["lease_expires_at"]) <= now
        ):
            changed = _strictly_after(now, str(row["updated_at"]))
            connection.execute(
                """
                UPDATE edge_diagnostic_commands
                SET status='authorized',lease_owner_session_id=NULL,lease_nonce_sha256=NULL,
                  lease_expires_at=NULL,revision=revision+1,updated_at=?
                WHERE diagnostic_command_id=? AND status='leased' AND revision=?
                """,
                (changed, row["diagnostic_command_id"], row["revision"]),
            )
        elif (
            str(row["status"]) == "running"
            and row["lease_expires_at"] is not None
            and _datetime(row["lease_expires_at"]) <= now
        ):
            _cancel_command(connection, row, now, expired=True)


def _cancel_grant_commands(
    connection: sqlite3.Connection,
    grant_id: str,
    now: datetime,
    *,
    completed_at: str,
) -> None:
    rows = connection.execute(
        """
        SELECT * FROM edge_diagnostic_commands
        WHERE diagnostic_grant_id=? AND status IN('authorized','leased','running')
        """,
        (grant_id,),
    ).fetchall()
    for row in rows:
        changed = _strictly_after(now, str(row["updated_at"]))
        connection.execute(
            """
            UPDATE edge_diagnostic_commands
            SET status='cancelled',lease_owner_session_id=NULL,lease_nonce_sha256=NULL,
              lease_expires_at=NULL,completed_at=?,revision=revision+1,updated_at=?
            WHERE diagnostic_command_id=? AND revision=?
              AND status IN('authorized','leased','running')
            """,
            (completed_at, changed, row["diagnostic_command_id"], row["revision"]),
        )


def _cancel_command(
    connection: sqlite3.Connection, command: sqlite3.Row, now: datetime, *, expired: bool
) -> None:
    changed = _strictly_after(now, str(command["updated_at"]))
    connection.execute(
        """
        UPDATE edge_diagnostic_commands
        SET status=?,lease_owner_session_id=NULL,lease_nonce_sha256=NULL,
          lease_expires_at=NULL,completed_at=?,revision=revision+1,updated_at=?
        WHERE diagnostic_command_id=? AND revision=?
          AND status IN('authorized','leased','running')
        """,
        (
            "expired" if expired else "cancelled",
            _db_time(now),
            changed,
            command["diagnostic_command_id"],
            command["revision"],
        ),
    )


def _finish_grant_if_needed(
    connection: sqlite3.Connection, grant_id: str, now: datetime
) -> None:
    grant = connection.execute(
        "SELECT * FROM edge_diagnostic_grants WHERE diagnostic_grant_id=?", (grant_id,)
    ).fetchone()
    if grant is None or str(grant["status"]) != "active":
        return
    terminal = connection.execute(
        """
        SELECT COUNT(*) FROM edge_diagnostic_commands
        WHERE diagnostic_grant_id=? AND status IN('completed','failed','cancelled','expired')
        """,
        (grant_id,),
    ).fetchone()
    status = None
    if grant["expires_at"] is not None and _datetime(grant["expires_at"]) <= now:
        status = "expired"
    elif terminal is not None and int(terminal[0]) >= int(grant["max_runs"]):
        status = "exhausted"
    if status is not None:
        connection.execute(
            """
            UPDATE edge_diagnostic_grants
            SET status=?,revision=revision+1,updated_at=?
            WHERE diagnostic_grant_id=? AND status='active' AND revision=?
            """,
            (
                status,
                _strictly_after(now, str(grant["updated_at"])),
                grant_id,
                grant["revision"],
            ),
        )


def _authenticated_agent(
    connection: sqlite3.Connection,
    config: AppConfig,
    deployment_id: str,
    verified_fingerprint: str,
    edge_session_token: str,
) -> sqlite3.Row:
    from .desired_state_service import _authenticated_agent as authenticate

    return authenticate(
        connection,
        config,
        deployment_id=deployment_id,
        verified_fingerprint=verified_fingerprint,
        edge_session_token=edge_session_token,
    )


def _require_diagnostics_capability(agent: sqlite3.Row) -> None:
    try:
        capabilities = json.loads(str(agent["capability_codes_json"]))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        raise ApiError("edge.authentication_failed") from None
    if (
        type(capabilities) is not list
        or any(type(item) is not str for item in capabilities)
        or "diagnostics" not in capabilities
    ):
        raise ApiError("edge.authentication_failed")


def _workspace_context(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str | None,
    permission: str,
) -> WorkspaceSessionContext:
    context = authenticate_workspace_session_in_transaction(
        connection,
        config,
        session_token=session_token,
        csrf_token=csrf_token,
        touch=csrf_token is not None,
    )
    if permission not in context.permission_codes:
        raise ApiError("workspace_session.permission_denied")
    return context


def _active_membership(connection: sqlite3.Connection, membership_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM workspace_memberships WHERE workspace_membership_id=? AND status='active'",
        (membership_id,),
    ).fetchone()
    if row is None:
        raise ApiError("workspace_session.permission_denied")
    return row


def _platform_authority_current(
    connection: sqlite3.Connection, context: PlatformContext
) -> None:
    row = connection.execute(
        "SELECT * FROM platform_operators WHERE operator_id=?", (context.operator_id,)
    ).fetchone()
    if (
        row is None
        or str(row["status"]) != "active"
        or int(row["revision"]) != context.operator_revision
        or str(row["account_id"]) != context.account_id
    ):
        raise ApiError("platform.session_revoked")


def _require_platform(context: PlatformContext, permission: str) -> None:
    if permission not in context.permissions:
        raise ApiError("platform.permission_denied")


def _deployment(connection: sqlite3.Connection, deployment_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM edge_deployments WHERE edge_deployment_id=?", (deployment_id,)
    ).fetchone()
    if row is None:
        raise ApiError("edge.deployment_not_found")
    return row


def _diagnostic_grant(
    connection: sqlite3.Connection, deployment_id: str, grant_id: str
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT * FROM edge_diagnostic_grants
        WHERE edge_deployment_id=? AND diagnostic_grant_id=?
        """,
        (deployment_id, grant_id),
    ).fetchone()
    if row is None:
        raise ApiError("edge.diagnostic_grant_not_found")
    return row


def _diagnostic_command(
    connection: sqlite3.Connection, deployment_id: str, command_id: str
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT * FROM edge_diagnostic_commands
        WHERE edge_deployment_id=? AND diagnostic_command_id=?
        """,
        (deployment_id, command_id),
    ).fetchone()
    if row is None:
        raise ApiError("edge.diagnostic_command_not_found")
    return row


def _health_snapshot(
    connection: sqlite3.Connection, deployment_id: str, generation: int
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT * FROM edge_health_snapshots
        WHERE edge_deployment_id=? AND generation=?
        """,
        (deployment_id, generation),
    ).fetchone()
    if row is None:
        raise ApiError("edge.unavailable")
    return row


def _action_command(
    connection: sqlite3.Connection,
    *,
    actor_domain: str,
    actor_id: str,
    action: str,
    idempotency_sha256: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT * FROM edge_diagnostic_action_commands
        WHERE actor_domain=? AND actor_id=? AND action_code=? AND idempotency_sha256=?
        """,
        (actor_domain, actor_id, action, idempotency_sha256),
    ).fetchone()


def _insert_action_command(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    tenant_id: str,
    actor_domain: str,
    actor_id: str,
    action: str,
    idempotency_sha256: str,
    request_sha256: str,
    result_object_id: str,
    now: str,
) -> None:
    connection.execute(
        """
        INSERT INTO edge_diagnostic_action_commands(
          edge_diagnostic_action_command_id,workspace_id,tenant_id,actor_domain,
          actor_id,action_code,idempotency_sha256,request_sha256,result_object_id,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        """,
        (
            _id("dac_"),
            workspace_id,
            tenant_id,
            actor_domain,
            actor_id,
            action,
            idempotency_sha256,
            request_sha256,
            result_object_id,
            now,
        ),
    )


def _assert_replay(row: sqlite3.Row, request_sha256: str) -> None:
    if not hmac.compare_digest(str(row["request_sha256"]), request_sha256):
        raise ApiError("edge.diagnostic_idempotency_conflict")


def _insert_event(
    connection: sqlite3.Connection,
    deployment: sqlite3.Row,
    *,
    grant_id: str | None,
    command_id: str | None,
    event_type: str,
    actor_domain: str,
    actor_id: str,
    reason: str,
    outcome: str,
    summary: dict[str, object],
    request_id: str,
    trace_id: str,
    now: str,
    domains: tuple[str, ...],
) -> None:
    event_id = _id("dge_")
    summary_json = json.dumps(summary, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    connection.execute(
        """
        INSERT INTO edge_diagnostic_events(
          edge_diagnostic_event_id,workspace_id,tenant_id,edge_deployment_id,
          diagnostic_grant_id,diagnostic_command_id,event_type,actor_domain,actor_id,
          reason_code,outcome_code,summary_json,request_id,trace_id,occurred_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id,
            deployment["workspace_id"],
            deployment["tenant_id"],
            deployment["edge_deployment_id"],
            grant_id,
            command_id,
            event_type,
            actor_domain,
            actor_id,
            reason,
            outcome,
            summary_json,
            _identifier(request_id),
            _identifier(trace_id),
            now,
        ),
    )
    for domain in domains:
        connection.execute(
            """
            INSERT INTO edge_diagnostic_outbox(
              edge_diagnostic_outbox_id,workspace_id,tenant_id,edge_diagnostic_event_id,
              delivery_domain,status,attempt_count,available_at,delivered_at,
              last_error_code,created_at,updated_at)
            VALUES(?,?,?,?,?,'pending',0,?,NULL,NULL,?,?)
            """,
            (
                _id("dgo_"),
                deployment["workspace_id"],
                deployment["tenant_id"],
                event_id,
                domain,
                now,
                now,
                now,
            ),
        )


def _fleet_summary(row: sqlite3.Row) -> PlatformFleetDeployment:
    return PlatformFleetDeployment(
        deployment_id=str(row["edge_deployment_id"]),
        region_code=str(row["region_code"]),
        mode=str(row["mode"]),
        status=str(row["status"]),
        health_code=str(row["health_code"]),
        health_score=int(row["health_score"]),
        alert_code=str(row["alert_code"]),
        maintenance_mode=bool(row["maintenance_mode"]),
        agent_version=row["agent_version"],
        desired_generation=int(row["desired_generation"]),
        applied_generation=int(row["applied_generation"]),
        lkg_generation=int(row["lkg_generation"]),
        telemetry_lag_seconds=(
            int(row["telemetry_lag_seconds"])
            if row["telemetry_lag_seconds"] is not None
            else None
        ),
        certificate_signal=str(row["certificate_signal"]),
        source_health_generation=int(row["source_health_generation"]),
        index_revision=int(row["index_revision"]),
        updated_at=_datetime(row["updated_at"]),
    )


def _health_summary(row: sqlite3.Row) -> EdgeHealthSnapshotSummary:
    return EdgeHealthSnapshotSummary(
        edge_health_snapshot_id=str(row["edge_health_snapshot_id"]),
        deployment_id=str(row["edge_deployment_id"]),
        generation=int(row["generation"]),
        score_model_version=str(row["score_model_version"]),
        health_score=int(row["health_score"]),
        health_code=str(row["health_code"]),
        certificate_signal=str(row["certificate_signal"]),
        version_signal=str(row["version_signal"]),
        capacity_signal=str(row["capacity_signal"]),
        config_signal=str(row["config_signal"]),
        telemetry_signal=str(row["telemetry_signal"]),
        validation_signal=str(row["validation_signal"]),
        desired_generation=int(row["desired_generation"]),
        applied_generation=int(row["applied_generation"]),
        lkg_generation=int(row["lkg_generation"]),
        telemetry_lag_seconds=(
            int(row["telemetry_lag_seconds"])
            if row["telemetry_lag_seconds"] is not None
            else None
        ),
        certificate_expires_at=(
            _datetime(row["certificate_expires_at"])
            if row["certificate_expires_at"] is not None
            else None
        ),
        agent_version=row["agent_version"],
        observed_at=_datetime(row["observed_at"]),
        created_at=_datetime(row["created_at"]),
    )


def _grant_summary(row: sqlite3.Row) -> EdgeDiagnosticGrantSummary:
    return EdgeDiagnosticGrantSummary(
        diagnostic_grant_id=str(row["diagnostic_grant_id"]),
        deployment_id=str(row["edge_deployment_id"]),
        status=str(row["status"]),
        command_code=str(row["command_code"]),
        invocation=_parse_invocation(str(row["invocation_rfc8785"])),
        parameters_sha256=str(row["parameters_sha256"]),
        reason_code=str(row["reason_code"]),
        max_runs=int(row["max_runs"]),
        run_count=int(row["run_count"]),
        revision=int(row["revision"]),
        requested_at=_datetime(row["requested_at"]),
        approved_at=_datetime(row["approved_at"]) if row["approved_at"] is not None else None,
        expires_at=_datetime(row["expires_at"]) if row["expires_at"] is not None else None,
        revoked_at=_datetime(row["revoked_at"]) if row["revoked_at"] is not None else None,
        updated_at=_datetime(row["updated_at"]),
    )


def _command_summary(row: sqlite3.Row) -> EdgeDiagnosticCommandSummary:
    return EdgeDiagnosticCommandSummary(
        diagnostic_command_id=str(row["diagnostic_command_id"]),
        diagnostic_grant_id=str(row["diagnostic_grant_id"]),
        deployment_id=str(row["edge_deployment_id"]),
        generation=int(row["generation"]),
        status=str(row["status"]),
        command_code=str(row["command_code"]),
        envelope_sha256=str(row["envelope_sha256"]),
        revision=int(row["revision"]),
        issued_at=_datetime(row["issued_at"]),
        deadline_at=_datetime(row["deadline_at"]),
        expires_at=_datetime(row["expires_at"]),
        lease_expires_at=(
            _datetime(row["lease_expires_at"])
            if row["lease_expires_at"] is not None
            else None
        ),
        started_at=_datetime(row["started_at"]) if row["started_at"] is not None else None,
        completed_at=(
            _datetime(row["completed_at"]) if row["completed_at"] is not None else None
        ),
        updated_at=_datetime(row["updated_at"]),
    )


def _result_summary(row: sqlite3.Row) -> EdgeDiagnosticResultSummary:
    report = DiagnosticExecutionReport.model_validate(json.loads(str(row["report_rfc8785"])))
    return EdgeDiagnosticResultSummary(
        diagnostic_result_id=str(row["diagnostic_result_id"]),
        diagnostic_grant_id=str(row["diagnostic_grant_id"]),
        diagnostic_command_id=str(row["diagnostic_command_id"]),
        deployment_id=str(row["edge_deployment_id"]),
        generation=int(row["generation"]),
        status=str(row["status"]),
        reason_code=str(row["reason_code"]),
        result=report.result,
        report_sha256=str(row["report_sha256"]),
        result_sha256=row["result_sha256"],
        duration_ms=int(row["duration_ms"]),
        started_at=_datetime(row["started_at"]),
        completed_at=_datetime(row["completed_at"]),
        received_at=_datetime(row["received_at"]),
    )


def _grant_response(
    row: sqlite3.Row, request_id: str, trace_id: str, *, replay: bool
) -> EdgeDiagnosticGrantResponse:
    return EdgeDiagnosticGrantResponse(
        grant=_grant_summary(row),
        idempotent_replay=replay,
        request_id=_identifier(request_id),
        trace_id=_identifier(trace_id),
    )


def _command_response(
    row: sqlite3.Row, request_id: str, trace_id: str, *, replay: bool
) -> EdgeDiagnosticCommandResponse:
    return EdgeDiagnosticCommandResponse(
        command=_command_summary(row),
        idempotent_replay=replay,
        request_id=_identifier(request_id),
        trace_id=_identifier(trace_id),
    )


def _result_response(
    row: sqlite3.Row, request_id: str, trace_id: str, *, replay: bool
) -> EdgeDiagnosticResultResponse:
    return EdgeDiagnosticResultResponse(
        result=_result_summary(row),
        idempotent_replay=replay,
        request_id=_identifier(request_id),
        trace_id=_identifier(trace_id),
    )


def _parse_invocation(value: str) -> DiagnosticInvocation:
    return _INVOCATION.validate_python(json.loads(value), strict=True)


def _parse_result(value: object) -> DiagnosticResult:
    return _RESULT.validate_python(value, strict=True)


def _platform_idempotency(
    config: AppConfig, operator_id: str, action: str, value: str
) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{15,127}", value) is None:
        raise ApiError("edge.diagnostic_request_invalid")
    return hmac.new(
        config.workspace_session_secret.encode("utf-8"),
        f"prd126/platform-idempotency\0{operator_id}\0{action}\0{value}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _workspace_idempotency(
    config: AppConfig,
    context: WorkspaceSessionContext,
    action: str,
    value: str,
) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{15,127}", value) is None:
        raise ApiError("edge.diagnostic_request_invalid")
    return hmac.new(
        config.workspace_session_secret.encode("utf-8"),
        (
            f"prd126/workspace-idempotency\0{context.workspace_id}\0"
            f"{context.membership_id}\0{action}\0{value}"
        ).encode(),
        hashlib.sha256,
    ).hexdigest()


def _encode_cursor(
    config: AppConfig,
    filters: tuple[str | None, str | None, str | None],
    after: tuple[str, str, str, str],
) -> str:
    raw = json.dumps(
        {"after": list(after), "filters": list(filters), "v": 1},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    signature = hmac.new(
        config.workspace_session_secret.encode("utf-8"),
        _FLEET_CURSOR_DOMAIN + raw,
        hashlib.sha256,
    ).digest()
    return f"{_b64(raw)}.{_b64(signature)}"


def _decode_cursor(
    config: AppConfig,
    value: str,
    filters: tuple[str | None, str | None, str | None],
) -> tuple[str, str, str, str]:
    try:
        encoded, signature_value = value.split(".", 1)
        raw = _unb64(encoded, maximum=1024)
        signature = _unb64(signature_value, maximum=32)
        expected = hmac.new(
            config.workspace_session_secret.encode("utf-8"),
            _FLEET_CURSOR_DOMAIN + raw,
            hashlib.sha256,
        ).digest()
        document = json.loads(raw)
        after = document.get("after")
        if (
            len(signature) != 32
            or not hmac.compare_digest(signature, expected)
            or document.get("v") != 1
            or document.get("filters") != list(filters)
            or type(after) is not list
            or len(after) != 4
            or any(type(item) is not str for item in after)
            or after[0] not in _FLEET_STATUSES
            or _REGION_RE.fullmatch(after[1]) is None
            or after[2] not in _HEALTH_CODES
            or re.fullmatch(r"edp_[0-9a-f]{32}", after[3]) is None
        ):
            raise ValueError("invalid cursor")
        return after[0], after[1], after[2], after[3]
    except (AttributeError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise ApiError("platform.edge_fleet_cursor_invalid") from None


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str, *, maximum: int) -> bytes:
    if not 1 <= len(value) <= 2048 or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise ValueError("invalid encoding")
    decoded = base64.b64decode(
        value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
    )
    if not 1 <= len(decoded) <= maximum or _b64(decoded) != value:
        raise ValueError("invalid encoding")
    return decoded


def _strictly_after(now: datetime, prior: str | None) -> str:
    candidate = now.astimezone(UTC)
    if prior is not None:
        prior_time = _datetime(prior)
        if candidate <= prior_time:
            candidate = prior_time + timedelta(milliseconds=1)
    return _db_time(candidate)


__all__ = (
    "advance_agent_diagnostic",
    "decide_workspace_diagnostic_grant",
    "get_workspace_edge_health",
    "list_platform_fleet",
    "mutate_platform_diagnostic_command",
    "pull_agent_diagnostic",
    "record_agent_diagnostic_result",
    "refresh_edge_health_projection_in_transaction",
    "request_platform_diagnostic_grant",
)
