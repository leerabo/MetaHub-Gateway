"""PRD-082 tenant-qualified MCP governance and invocation persistence."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.mcp import (
    McpContractError,
    McpCryptoOwner,
    McpHmacOwner,
    McpSchemaLimits,
    McpVersionedSecret,
    canonical_json_bytes,
    canonical_mcp_permission_codes,
    canonical_redaction_pointers,
    decrypt_mcp_value_from_keyring,
    encrypt_mcp_value,
    mcp_hmac,
    mcp_hmac_candidates,
    validate_arguments,
    validate_mcp_endpoint_url,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.safe_regex import UnsafeRegexError, compile_safe_dlp_regex

from metahub_control.approvals.service import (
    create_mcp_approval_in_transaction,
    mcp_approval_target_coordinates,
)
from metahub_control.audit.service import (
    AuditRequestMeta,
    commit_audited_transaction,
    record_audit_log,
)
from metahub_control.auth import (
    AuthContext,
    refresh_transaction_authentication,
    require_permission,
)

from .models import (
    InvokeMcpToolRequest,
    McpDlpSummary,
    McpInvocationInfo,
    McpInvocationListResponse,
    McpInvocationResponse,
    McpInvocationResultResponse,
    McpServerInfo,
    McpServerListResponse,
    McpServerOperationInfo,
    McpServerOperationResponse,
    McpServerResponse,
    McpToolDetailInfo,
    McpToolInfo,
    McpToolListResponse,
    McpToolResponse,
    RefreshMcpServerRequest,
    RegisterMcpServerRequest,
    UpdateMcpServerRequest,
    UpdateMcpToolPolicyRequest,
)

_SERVER_STATUSES = {"pending_sync", "active", "sync_failed", "disabled"}
_TOOL_STATUSES = {"review_required", "active", "disabled"}
_INVOCATION_STATUSES = {
    "approval_pending",
    "queued",
    "probing",
    "calling",
    "succeeded",
    "failed",
    "blocked",
    "rejected",
    "outcome_unknown",
}
_TERMINAL_INVOCATION_STATUSES = {
    "succeeded",
    "failed",
    "blocked",
    "rejected",
    "outcome_unknown",
}
# SQLite can spend five seconds waiting for a writer; keep one more for CAS and audits.
_SYNC_FINALIZE_RESERVE_SECONDS = 6


@dataclass(frozen=True, slots=True)
class DlpDecision:
    status: str
    match_count: int
    rule_id: str | None
    ruleset_hmac: str
    ruleset_hmac_key_version: int


def register_server(
    config: AppConfig,
    context: AuthContext,
    payload: RegisterMcpServerRequest,
    *,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
    request_meta: AuditRequestMeta | None = None,
) -> McpServerResponse:
    """Persist registration intent before any executor discovery attempt."""

    _ensure_enabled(config)
    tenant_id = context.user.tenant_id
    name = _display_name(payload.name, maximum=80)
    endpoint = _endpoint(config, payload.endpoint_url)
    server_id = f"mcps_{uuid4().hex}"
    credential_id = f"mcpc_{uuid4().hex}"
    operation_id = f"mcpo_{uuid4().hex}"
    job_id = f"mcpj_{uuid4().hex}"
    now = _utcnow()
    timestamp = _to_db_time(now)
    intent_bytes = _canonical(
        {
            "operation": "register",
            "name": name,
            "endpoint_url": endpoint,
            "auth_type": payload.auth.type,
            "bearer_token": payload.auth.bearer_token,
        }
    )
    key_candidates = _hmac_candidates(
        config, idempotency_key, tenant_id, context.user.user_id, "idempotency"
    )
    intent_candidates = _hmac_candidates(
        config, intent_bytes, tenant_id, context.user.user_id, "intent"
    )
    current_key_version, current_key_hmac = key_candidates[0]
    current_intent_hmac = dict(intent_candidates).get(current_key_version)
    if current_intent_hmac is None:
        raise ApiError("mcp.unavailable")
    endpoint_key_version, endpoint_hmac = _hmac_candidates(
        config, endpoint, tenant_id, "endpoint-registry", "endpoint"
    )[0]
    encryption_version, encryption_secret = _current_encryption(config)
    hmac_version, hmac_secret = _current_hmac(config)
    endpoint_ciphertext = encrypt_mcp_value(
        endpoint,
        secret=encryption_secret,
        owner=McpCryptoOwner(tenant_id, server_id, "endpoint", encryption_version),
    )
    credential_ciphertext, credential_hmac = _credential_material(
        payload.auth.type,
        payload.auth.bearer_token,
        tenant_id=tenant_id,
        server_id=server_id,
        credential_id=credential_id,
        encryption_version=encryption_version,
        encryption_secret=encryption_secret,
        hmac_version=hmac_version,
        hmac_secret=hmac_secret,
    )
    parsed = urlsplit(endpoint)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        live = _transaction_permission(connection, context, "mcp.server.manage")
        replay = _find_operation(connection, tenant_id, live.user.user_id, key_candidates)
        if replay is not None:
            _require_replay_intent(replay, intent_candidates)
            server = _load_server(connection, tenant_id, str(replay["mcp_server_id"]))
            if server is None:
                raise ApiError("mcp.unavailable")
            response = _server_response(
                connection,
                server,
                replay,
                request_id=request_id,
                trace_id=trace_id,
                idempotent_replay=True,
            )
            connection.commit()
            return response
        endpoint_candidates = _hmac_candidates(
            config, endpoint, tenant_id, "endpoint-registry", "endpoint"
        )
        if _find_server_by_endpoint(connection, tenant_id, endpoint_candidates) is not None:
            raise ApiError("mcp.state_conflict")
        connection.execute(
            """
            INSERT INTO mcp_servers(
              mcp_server_id,tenant_id,name,transport,endpoint_ciphertext,
              endpoint_encryption_key_version,endpoint_hmac,endpoint_hmac_key_version,
              endpoint_scheme,endpoint_host_hint,endpoint_has_path,active_credential_revision,
              protocol_version,capabilities_sha256,status,state_revision,created_by,updated_by,
              created_at,updated_at
            ) VALUES(?,?,?,'streamable_http',?,?,?,?,?,?,?,NULL,NULL,NULL,
                     'pending_sync',1,?,?,?,?)
            """,
            (
                server_id,
                tenant_id,
                name,
                endpoint_ciphertext,
                encryption_version,
                endpoint_hmac,
                endpoint_key_version,
                parsed.scheme,
                _mask_host(parsed.hostname or ""),
                int(parsed.path not in {"", "/"}),
                live.user.user_id,
                live.user.user_id,
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO mcp_server_credentials(
              mcp_server_credential_id,tenant_id,mcp_server_id,credential_revision,
              auth_type,credential_ciphertext,credential_encryption_key_version,
              credential_hmac,credential_hmac_key_version,status,probe_operation_id,
              created_by,created_at,activated_at,rejected_at
            ) VALUES(?,?,?,1,?,?,?,?,?,'candidate',?, ?,?,NULL,NULL)
            """,
            (
                credential_id,
                tenant_id,
                server_id,
                payload.auth.type,
                credential_ciphertext,
                encryption_version if credential_ciphertext is not None else None,
                credential_hmac,
                hmac_version if credential_hmac is not None else None,
                operation_id,
                live.user.user_id,
                timestamp,
            ),
        )
        _insert_operation(
            connection,
            operation_id=operation_id,
            tenant_id=tenant_id,
            server_id=server_id,
            operation_type="register",
            key_version=current_key_version,
            key_hmac=current_key_hmac,
            intent_version=current_key_version,
            intent_hmac=current_intent_hmac,
            actor_id=live.user.user_id,
            start_server_revision=1,
            start_credential_revision=None,
            candidate_credential_revision=1,
            request_id=request_id,
            trace_id=trace_id,
            timestamp=timestamp,
        )
        _insert_sync_job(
            connection,
            config,
            job_id=job_id,
            tenant_id=tenant_id,
            server_id=server_id,
            operation_id=operation_id,
            candidate_credential_revision=1,
            job_type="discover",
            now=now,
        )
        _ensure_scheduler_tenant(connection, tenant_id, timestamp)
        server = _load_server(connection, tenant_id, server_id)
        operation = _load_operation(connection, tenant_id, operation_id)
        if server is None or operation is None:
            raise ApiError("mcp.unavailable")
        record_audit_log(
            connection,
            live,
            action="mcp.server_registered",
            object_type="mcp_server",
            object_id=server_id,
            object_name=None,
            before_summary={},
            after_summary=_server_audit_summary(server),
            request_meta=_audit_request_meta(request_meta),
            request_id=request_id,
            trace_id=trace_id,
            created_at=now,
        )
        response = _server_response(
            connection,
            server,
            operation,
            request_id=request_id,
            trace_id=trace_id,
            idempotent_replay=False,
        )
        commit_audited_transaction(connection)
        return response
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError as exc:
        _rollback(connection)
        raise ApiError("mcp.state_conflict") from exc
    except (sqlite3.Error, DbError, McpContractError, TypeError, ValueError) as exc:
        _rollback(connection)
        raise _mapped_error(exc) from exc
    finally:
        connection.close()


def update_server(
    config: AppConfig,
    context: AuthContext,
    server_id: str,
    payload: UpdateMcpServerRequest,
    *,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
    request_meta: AuditRequestMeta | None = None,
) -> McpServerResponse:
    """Update safe metadata immediately or persist a two-phase credential rotation."""

    _ensure_enabled(config)
    normalized_id = _resource_id(server_id, "mcps_")
    if payload.auth is not None and payload.model_fields_set.intersection({"name", "status"}):
        raise ApiError("mcp.request_invalid")
    operation_type = (
        "rotate"
        if payload.auth is not None
        else (
            "disable"
            if payload.status == "disabled"
            else ("enable" if payload.status == "active" else "update")
        )
    )
    intent_bytes = _canonical(
        {
            "operation": operation_type,
            "server_id": normalized_id,
            "expected_revision": payload.expected_revision,
            "name": payload.name,
            "status": payload.status,
            "auth_type": payload.auth.type if payload.auth is not None else None,
            "bearer_token": payload.auth.bearer_token if payload.auth is not None else None,
        }
    )
    tenant_id = context.user.tenant_id
    key_candidates = _hmac_candidates(
        config, idempotency_key, tenant_id, context.user.user_id, "idempotency"
    )
    intent_candidates = _hmac_candidates(
        config, intent_bytes, tenant_id, context.user.user_id, "intent"
    )
    current_key_version, current_key_hmac = key_candidates[0]
    current_intent_hmac = dict(intent_candidates).get(current_key_version)
    if current_intent_hmac is None:
        raise ApiError("mcp.unavailable")
    now = _utcnow()
    timestamp = _to_db_time(now)
    operation_id = f"mcpo_{uuid4().hex}"
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        live = _transaction_permission(connection, context, "mcp.server.manage")
        replay = _find_operation(connection, tenant_id, live.user.user_id, key_candidates)
        if replay is not None:
            _require_replay_intent(replay, intent_candidates)
            server = _load_server(connection, tenant_id, normalized_id)
            if server is None or replay["mcp_server_id"] != normalized_id:
                raise ApiError("mcp.idempotency_conflict")
            response = _server_response(
                connection,
                server,
                replay,
                request_id=request_id,
                trace_id=trace_id,
                idempotent_replay=True,
            )
            connection.commit()
            return response
        server = _load_server(connection, tenant_id, normalized_id)
        if server is None:
            raise ApiError("mcp.server_not_found")
        if int(server["state_revision"]) != payload.expected_revision:
            raise ApiError("mcp.state_conflict")
        before_server = _server_audit_summary(server)
        start_credential = server["active_credential_revision"]
        candidate_revision: int | None = None
        if payload.auth is not None:
            candidate_revision = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(credential_revision),0)+1 AS value
                    FROM mcp_server_credentials WHERE tenant_id=? AND mcp_server_id=?
                    """,
                    (tenant_id, normalized_id),
                ).fetchone()["value"]
            )
            credential_id = f"mcpc_{uuid4().hex}"
            encryption_version, encryption_secret = _current_encryption(config)
            hmac_version, hmac_secret = _current_hmac(config)
            ciphertext, credential_hmac = _credential_material(
                payload.auth.type,
                payload.auth.bearer_token,
                tenant_id=tenant_id,
                server_id=normalized_id,
                credential_id=credential_id,
                encryption_version=encryption_version,
                encryption_secret=encryption_secret,
                hmac_version=hmac_version,
                hmac_secret=hmac_secret,
            )
            connection.execute(
                """
                INSERT INTO mcp_server_credentials(
                  mcp_server_credential_id,tenant_id,mcp_server_id,credential_revision,
                  auth_type,credential_ciphertext,credential_encryption_key_version,
                  credential_hmac,credential_hmac_key_version,status,probe_operation_id,
                  created_by,created_at,activated_at,rejected_at
                ) VALUES(?,?,?,?,?,?,?,?,?,'candidate',?,?,?,NULL,NULL)
                """,
                (
                    credential_id,
                    tenant_id,
                    normalized_id,
                    candidate_revision,
                    payload.auth.type,
                    ciphertext,
                    encryption_version if ciphertext is not None else None,
                    credential_hmac,
                    hmac_version if credential_hmac is not None else None,
                    operation_id,
                    live.user.user_id,
                    timestamp,
                ),
            )
            _insert_operation(
                connection,
                operation_id=operation_id,
                tenant_id=tenant_id,
                server_id=normalized_id,
                operation_type="rotate",
                key_version=current_key_version,
                key_hmac=current_key_hmac,
                intent_version=current_key_version,
                intent_hmac=current_intent_hmac,
                actor_id=live.user.user_id,
                start_server_revision=payload.expected_revision,
                start_credential_revision=(
                    int(start_credential) if start_credential is not None else None
                ),
                candidate_credential_revision=candidate_revision,
                request_id=request_id,
                trace_id=trace_id,
                timestamp=timestamp,
            )
            _insert_sync_job(
                connection,
                config,
                job_id=f"mcpj_{uuid4().hex}",
                tenant_id=tenant_id,
                server_id=normalized_id,
                operation_id=operation_id,
                candidate_credential_revision=candidate_revision,
                job_type="credential_probe",
                now=now,
            )
            _ensure_scheduler_tenant(connection, tenant_id, timestamp)
        else:
            updated_name = (
                _display_name(payload.name, maximum=80)
                if payload.name is not None
                else server["name"]
            )
            updated_status = payload.status if payload.status is not None else server["status"]
            if updated_status == "active" and server["active_credential_revision"] is None:
                raise ApiError("mcp.state_conflict")
            _insert_operation(
                connection,
                operation_id=operation_id,
                tenant_id=tenant_id,
                server_id=normalized_id,
                operation_type=operation_type,
                key_version=current_key_version,
                key_hmac=current_key_hmac,
                intent_version=current_key_version,
                intent_hmac=current_intent_hmac,
                actor_id=live.user.user_id,
                start_server_revision=payload.expected_revision,
                start_credential_revision=(
                    int(start_credential) if start_credential is not None else None
                ),
                candidate_credential_revision=None,
                request_id=request_id,
                trace_id=trace_id,
                timestamp=timestamp,
            )
            changed = connection.execute(
                """
                UPDATE mcp_servers
                SET name=?,status=?,state_revision=state_revision+1,updated_by=?,updated_at=?
                WHERE tenant_id=? AND mcp_server_id=? AND state_revision=?
                """,
                (
                    updated_name,
                    updated_status,
                    live.user.user_id,
                    timestamp,
                    tenant_id,
                    normalized_id,
                    payload.expected_revision,
                ),
            ).rowcount
            if changed != 1:
                raise ApiError("mcp.state_conflict")
            completed = connection.execute(
                """
                UPDATE mcp_server_operations
                SET status='succeeded',completed_at=?,updated_at=?
                WHERE tenant_id=? AND mcp_server_operation_id=? AND status='queued'
                """,
                (timestamp, timestamp, tenant_id, operation_id),
            ).rowcount
            if completed != 1:
                raise ApiError("mcp.unavailable")
        refreshed = _load_server(connection, tenant_id, normalized_id)
        operation = _load_operation(connection, tenant_id, operation_id)
        if refreshed is None or operation is None:
            raise ApiError("mcp.unavailable")
        if payload.auth is None:
            audit_action = {
                "disable": "mcp.server_disabled",
                "enable": "mcp.server_enabled",
                "update": "mcp.server_updated",
            }[operation_type]
            record_audit_log(
                connection,
                live,
                action=audit_action,
                object_type="mcp_server",
                object_id=normalized_id,
                object_name=None,
                before_summary=before_server,
                after_summary=_server_audit_summary(refreshed),
                request_meta=_audit_request_meta(request_meta),
                request_id=request_id,
                trace_id=trace_id,
                created_at=now,
            )
        response = _server_response(
            connection,
            refreshed,
            operation,
            request_id=request_id,
            trace_id=trace_id,
            idempotent_replay=False,
        )
        if payload.auth is None:
            commit_audited_transaction(connection)
        else:
            connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError as exc:
        _rollback(connection)
        raise ApiError("mcp.state_conflict") from exc
    except (sqlite3.Error, DbError, McpContractError, TypeError, ValueError) as exc:
        _rollback(connection)
        raise _mapped_error(exc) from exc
    finally:
        connection.close()


def refresh_server_tools(
    config: AppConfig,
    context: AuthContext,
    server_id: str,
    payload: RefreshMcpServerRequest,
    *,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
    request_meta: AuditRequestMeta | None = None,
) -> McpServerResponse:
    """Persist a recoverable tools/list operation without synchronous network access."""

    _ensure_enabled(config)
    normalized_id = _resource_id(server_id, "mcps_")
    tenant_id = context.user.tenant_id
    intent_bytes = _canonical(
        {
            "operation": "refresh",
            "server_id": normalized_id,
            "expected_revision": payload.expected_revision,
        }
    )
    keys = _hmac_candidates(config, idempotency_key, tenant_id, context.user.user_id, "idempotency")
    intents = _hmac_candidates(config, intent_bytes, tenant_id, context.user.user_id, "intent")
    key_version, key_hmac = keys[0]
    intent_hmac = dict(intents).get(key_version)
    if intent_hmac is None:
        raise ApiError("mcp.unavailable")
    now = _utcnow()
    timestamp = _to_db_time(now)
    operation_id = f"mcpo_{uuid4().hex}"
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        live = refresh_transaction_authentication(
            connection, context, unavailable_error_code="mcp.unavailable"
        )
        require_permission(live, "mcp.server.manage")
        require_permission(live, "mcp.policy.manage")
        replay = _find_operation(connection, tenant_id, live.user.user_id, keys)
        if replay is not None:
            _require_replay_intent(replay, intents)
            if replay["mcp_server_id"] != normalized_id:
                raise ApiError("mcp.idempotency_conflict")
            server = _load_server(connection, tenant_id, normalized_id)
            if server is None:
                raise ApiError("mcp.unavailable")
            response = _server_response(
                connection,
                server,
                replay,
                request_id=request_id,
                trace_id=trace_id,
                idempotent_replay=True,
            )
            connection.commit()
            return response
        server = _load_server(connection, tenant_id, normalized_id)
        if server is None:
            raise ApiError("mcp.server_not_found")
        if (
            int(server["state_revision"]) != payload.expected_revision
            or server["status"] == "disabled"
            or server["active_credential_revision"] is None
        ):
            raise ApiError("mcp.state_conflict")
        _insert_operation(
            connection,
            operation_id=operation_id,
            tenant_id=tenant_id,
            server_id=normalized_id,
            operation_type="refresh",
            key_version=key_version,
            key_hmac=key_hmac,
            intent_version=key_version,
            intent_hmac=intent_hmac,
            actor_id=live.user.user_id,
            start_server_revision=payload.expected_revision,
            start_credential_revision=int(server["active_credential_revision"]),
            candidate_credential_revision=None,
            request_id=request_id,
            trace_id=trace_id,
            timestamp=timestamp,
        )
        _insert_sync_job(
            connection,
            config,
            job_id=f"mcpj_{uuid4().hex}",
            tenant_id=tenant_id,
            server_id=normalized_id,
            operation_id=operation_id,
            candidate_credential_revision=None,
            job_type="discover",
            now=now,
        )
        _ensure_scheduler_tenant(connection, tenant_id, timestamp)
        operation = _load_operation(connection, tenant_id, operation_id)
        if operation is None:
            raise ApiError("mcp.unavailable")
        response = _server_response(
            connection,
            server,
            operation,
            request_id=request_id,
            trace_id=trace_id,
            idempotent_replay=False,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError as exc:
        _rollback(connection)
        raise ApiError("mcp.state_conflict") from exc
    except (sqlite3.Error, DbError, McpContractError, TypeError, ValueError) as exc:
        _rollback(connection)
        raise _mapped_error(exc) from exc
    finally:
        connection.close()


def list_servers(
    config: AppConfig,
    context: AuthContext,
    *,
    status: str | None,
    limit: str | None,
    cursor: str | None,
    request_id: str,
    trace_id: str,
) -> McpServerListResponse:
    _ensure_enabled(config)
    normalized_status = _optional_enum(status, _SERVER_STATUSES)
    page_size = _page_limit(limit)
    if cursor is not None:
        _resource_id(cursor, "mcps_")
    clauses = ["server.tenant_id=?"]
    params: list[object] = [context.user.tenant_id]
    if normalized_status is not None:
        clauses.append("server.status=?")
        params.append(normalized_status)
    if cursor is not None:
        clauses.append("server.mcp_server_id>?")
        params.append(cursor)
    params.append(page_size + 1)
    connection = _connect(config)
    try:
        rows = connection.execute(
            f"""
            SELECT server.*,
              (SELECT COUNT(*) FROM mcp_tools tool
               WHERE tool.tenant_id=server.tenant_id
                 AND tool.mcp_server_id=server.mcp_server_id) AS tool_count,
              (SELECT COUNT(*) FROM mcp_tools tool
               WHERE tool.tenant_id=server.tenant_id
                 AND tool.mcp_server_id=server.mcp_server_id
                 AND tool.status='active') AS active_tool_count
            FROM mcp_servers server
            WHERE {" AND ".join(clauses)}
            ORDER BY server.mcp_server_id LIMIT ?
            """,
            params,
        ).fetchall()
        visible = rows[:page_size]
        return McpServerListResponse(
            servers=[_server_info(row) for row in visible],
            next_cursor=(str(visible[-1]["mcp_server_id"]) if len(rows) > page_size else None),
            request_id=request_id,
            trace_id=trace_id,
        )
    except (sqlite3.Error, DbError, TypeError, ValueError) as exc:
        raise ApiError("mcp.unavailable") from exc
    finally:
        connection.close()


def get_server_operation(
    config: AppConfig,
    context: AuthContext,
    operation_id: str,
    *,
    request_id: str,
    trace_id: str,
) -> McpServerOperationResponse:
    _ensure_enabled(config)
    normalized_id = _resource_id(operation_id, "mcpo_")
    connection = _connect(config)
    try:
        row = _load_operation(connection, context.user.tenant_id, normalized_id)
        if row is None:
            raise ApiError("mcp.server_not_found")
        return McpServerOperationResponse(
            operation=_operation_info(row), request_id=request_id, trace_id=trace_id
        )
    finally:
        connection.close()


def list_tools(
    config: AppConfig,
    context: AuthContext,
    *,
    server_id: str | None,
    status: str | None,
    risk: str | None,
    approval_required: str | None,
    limit: str | None,
    cursor: str | None,
    request_id: str,
    trace_id: str,
) -> McpToolListResponse:
    _ensure_enabled(config)
    normalized_status = _optional_enum(status, _TOOL_STATUSES)
    normalized_risk = _optional_enum(risk, {"standard", "high"})
    normalized_server = _resource_id(server_id, "mcps_") if server_id is not None else None
    required_approval = _optional_boolean_query(approval_required)
    page_size = _page_limit(limit)
    if cursor is not None:
        _resource_id(cursor, "mcpt_")
    clauses = ["tool.tenant_id=?"]
    params: list[object] = [context.user.tenant_id]
    for column, value in (
        ("tool.mcp_server_id", normalized_server),
        ("tool.status", normalized_status),
        ("tool.risk_level", normalized_risk),
        ("tool.approval_required", required_approval),
    ):
        if value is not None:
            clauses.append(f"{column}=?")
            params.append(value)
    if cursor is not None:
        clauses.append("tool.mcp_tool_id>?")
        params.append(cursor)
    params.append(page_size + 1)
    connection = _connect(config)
    try:
        rows = connection.execute(
            f"""
            SELECT tool.*,
              (SELECT COUNT(*) FROM mcp_tool_role_grants grant_row
               WHERE grant_row.tenant_id=tool.tenant_id
                 AND grant_row.mcp_tool_id=tool.mcp_tool_id) AS allowed_role_count,
              (SELECT COUNT(*) FROM mcp_tool_user_grants grant_row
               WHERE grant_row.tenant_id=tool.tenant_id
                 AND grant_row.mcp_tool_id=tool.mcp_tool_id) AS allowed_user_count
            FROM mcp_tools tool
            WHERE {" AND ".join(clauses)}
            ORDER BY tool.mcp_tool_id LIMIT ?
            """,
            params,
        ).fetchall()
        visible = rows[:page_size]
        return McpToolListResponse(
            tools=[_tool_info(row) for row in visible],
            next_cursor=(str(visible[-1]["mcp_tool_id"]) if len(rows) > page_size else None),
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        raise
    except (sqlite3.Error, DbError, TypeError, ValueError) as exc:
        raise ApiError("mcp.unavailable") from exc
    finally:
        connection.close()


def get_tool(
    config: AppConfig,
    context: AuthContext,
    tool_id: str,
    *,
    request_id: str,
    trace_id: str,
) -> McpToolResponse:
    _ensure_enabled(config)
    normalized_id = _resource_id(tool_id, "mcpt_")
    connection = _connect(config)
    try:
        row = _load_tool_with_counts(connection, context.user.tenant_id, normalized_id)
        if row is None:
            raise ApiError("mcp.tool_not_found")
        return McpToolResponse(
            tool=_tool_detail(connection, row), request_id=request_id, trace_id=trace_id
        )
    finally:
        connection.close()


def update_tool_policy(
    config: AppConfig,
    context: AuthContext,
    tool_id: str,
    payload: UpdateMcpToolPolicyRequest,
    *,
    request_id: str,
    trace_id: str,
    request_meta: AuditRequestMeta | None = None,
) -> McpToolResponse:
    """Apply policy and per-tool grants under one policy-revision CAS."""

    _ensure_enabled(config)
    normalized_id = _resource_id(tool_id, "mcpt_")
    tenant_id = context.user.tenant_id
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        live = _transaction_permission(connection, context, "mcp.policy.manage")
        current = _load_tool_with_counts(connection, tenant_id, normalized_id)
        if current is None:
            raise ApiError("mcp.tool_not_found")
        if int(current["policy_revision"]) != payload.expected_policy_revision:
            raise ApiError("mcp.state_conflict")
        before_policy = _tool_audit_summary(current, include_grants=False)
        before_grants = _tool_audit_summary(current, include_policy=False)
        permissions = (
            canonical_mcp_permission_codes(payload.required_permission_codes)
            if payload.required_permission_codes is not None
            else tuple(_json_string_list(current["required_permission_codes_json"]))
        )
        _validate_permission_catalog(connection, permissions)
        pointers = (
            canonical_redaction_pointers(payload.result_redaction_pointers)
            if payload.result_redaction_pointers is not None
            else tuple(_json_string_list(current["result_redaction_pointers_json"]))
        )
        current_role_ids = _tool_role_ids(connection, tenant_id, normalized_id)
        current_user_ids = _tool_user_ids(connection, tenant_id, normalized_id)
        role_ids = (
            tuple(sorted(payload.allowed_role_ids))
            if payload.allowed_role_ids is not None
            else current_role_ids
        )
        user_ids = (
            tuple(sorted(payload.allowed_user_ids))
            if payload.allowed_user_ids is not None
            else current_user_ids
        )
        _validate_grant_owners(connection, tenant_id, role_ids, user_ids)
        risk = payload.risk_level if payload.risk_level is not None else str(current["risk_level"])
        approval_required = (
            bool(payload.approval_required)
            if payload.approval_required is not None
            else bool(current["approval_required"])
        )
        if risk == "high" and not approval_required:
            raise ApiError("mcp.request_invalid")
        desired_status = payload.status if payload.status is not None else str(current["status"])
        if desired_status == "active" and not role_ids and not user_ids:
            raise ApiError("mcp.request_invalid")
        expires = (
            int(payload.approval_expires_seconds)
            if payload.approval_expires_seconds is not None
            else int(current["approval_expires_seconds"])
        )
        now = _utcnow()
        timestamp = _to_db_time(now)
        added_role_ids = tuple(sorted(set(role_ids) - set(current_role_ids)))
        added_user_ids = tuple(sorted(set(user_ids) - set(current_user_ids)))
        removed_role_ids = tuple(sorted(set(current_role_ids) - set(role_ids)))
        removed_user_ids = tuple(sorted(set(current_user_ids) - set(user_ids)))
        for role_id in added_role_ids:
            connection.execute(
                """
                INSERT INTO mcp_tool_role_grants(
                  mcp_tool_role_grant_id,tenant_id,mcp_tool_id,role_id,created_by,created_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    f"mcprg_{uuid4().hex}",
                    tenant_id,
                    normalized_id,
                    role_id,
                    live.user.user_id,
                    timestamp,
                ),
            )
        for user_id in added_user_ids:
            connection.execute(
                """
                INSERT INTO mcp_tool_user_grants(
                  mcp_tool_user_grant_id,tenant_id,mcp_tool_id,user_id,created_by,created_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    f"mcpug_{uuid4().hex}",
                    tenant_id,
                    normalized_id,
                    user_id,
                    live.user.user_id,
                    timestamp,
                ),
            )
        changed = connection.execute(
            """
            UPDATE mcp_tools
            SET status=?,risk_level=?,approval_required=?,approval_expires_seconds=?,
                required_permission_codes_json=?,result_redaction_pointers_json=?,
                policy_revision=policy_revision+1,updated_by=?,updated_at=?
            WHERE tenant_id=? AND mcp_tool_id=? AND policy_revision=?
            """,
            (
                desired_status,
                risk,
                int(approval_required),
                expires,
                _canonical_json_text(list(permissions)),
                _canonical_json_text(list(pointers)),
                live.user.user_id,
                timestamp,
                tenant_id,
                normalized_id,
                payload.expected_policy_revision,
            ),
        ).rowcount
        if changed != 1:
            raise ApiError("mcp.state_conflict")
        for role_id in removed_role_ids:
            connection.execute(
                """
                DELETE FROM mcp_tool_role_grants
                WHERE tenant_id=? AND mcp_tool_id=? AND role_id=?
                """,
                (tenant_id, normalized_id, role_id),
            )
        for user_id in removed_user_ids:
            connection.execute(
                """
                DELETE FROM mcp_tool_user_grants
                WHERE tenant_id=? AND mcp_tool_id=? AND user_id=?
                """,
                (tenant_id, normalized_id, user_id),
            )
        row = _load_tool_with_counts(connection, tenant_id, normalized_id)
        if row is None:
            raise ApiError("mcp.unavailable")
        record_audit_log(
            connection,
            live,
            action="mcp.tool_policy_updated",
            object_type="mcp_tool",
            object_id=normalized_id,
            object_name=None,
            before_summary=before_policy,
            after_summary=_tool_audit_summary(row, include_grants=False),
            request_meta=_audit_request_meta(request_meta),
            request_id=request_id,
            trace_id=trace_id,
            created_at=now,
        )
        if removed_role_ids or removed_user_ids or added_role_ids or added_user_ids:
            record_audit_log(
                connection,
                live,
                action="mcp.tool_grants_updated",
                object_type="mcp_tool",
                object_id=normalized_id,
                object_name=None,
                before_summary=before_grants,
                after_summary=_tool_audit_summary(row, include_policy=False),
                request_meta=_audit_request_meta(request_meta),
                request_id=request_id,
                trace_id=trace_id,
                created_at=now,
            )
        response = McpToolResponse(
            tool=_tool_detail(connection, row), request_id=request_id, trace_id=trace_id
        )
        commit_audited_transaction(connection)
        return response
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError as exc:
        _rollback(connection)
        raise ApiError("mcp.state_conflict") from exc
    except (sqlite3.Error, DbError, McpContractError, TypeError, ValueError) as exc:
        _rollback(connection)
        raise _mapped_error(exc) from exc
    finally:
        connection.close()


def invoke_tool(
    config: AppConfig,
    context: AuthContext,
    tool_id: str,
    payload: InvokeMcpToolRequest,
    *,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> McpInvocationResponse:
    """Validate and persist one invocation; no MCP network is allowed in this transaction."""

    _ensure_enabled(config)
    normalized_id = _resource_id(tool_id, "mcpt_")
    tenant_id = context.user.tenant_id
    key_candidates = _hmac_candidates(
        config, idempotency_key, tenant_id, context.user.user_id, "idempotency"
    )
    intent_bytes = _canonical(
        {"operation": "invoke", "tool_id": normalized_id, "arguments": payload.arguments}
    )
    intent_candidates = _hmac_candidates(
        config, intent_bytes, tenant_id, context.user.user_id, "intent"
    )
    current_key_version, current_key_hmac = key_candidates[0]
    current_intent_hmac = dict(intent_candidates).get(current_key_version)
    if current_intent_hmac is None:
        raise ApiError("mcp.unavailable")
    invocation_id = f"mcpi_{uuid4().hex}"
    approval_id = f"apr_{uuid4().hex}"
    now = _utcnow()
    timestamp = _to_db_time(now)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        live = _transaction_permission(connection, context, "mcp.invoke")
        replay = _find_invocation_replay(connection, tenant_id, live.user.user_id, key_candidates)
        if replay is not None:
            _require_replay_intent(replay, intent_candidates)
            response = McpInvocationResponse(
                invocation=_invocation_info(replay),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            _raise_invocation_error(replay)
            return response
        tool = _load_invocable_tool(connection, tenant_id, normalized_id)
        if tool is None:
            raise ApiError("mcp.tool_not_found")
        if tool["status"] != "active" or tool["server_status"] != "active":
            raise ApiError("mcp.state_conflict")
        if tool["active_credential_revision"] is None:
            raise ApiError("mcp.server_unavailable")
        required_permissions = tuple(_json_string_list(tool["required_permission_codes_json"]))
        for permission in required_permissions:
            require_permission(live, permission)
        role_ids, user_ids = _require_tool_grant(connection, tenant_id, normalized_id, live)
        effective_schema = _json_object(tool["input_schema_effective_json"])
        arguments_bytes = validate_arguments(
            payload.arguments,
            effective_schema,
            limits=_schema_limits(config),
        )
        dlp = _scan_dlp(
            connection,
            config,
            tenant_id=tenant_id,
            invocation_id=invocation_id,
            value=payload.arguments,
            scope="input",
        )
        encryption_version, encryption_secret = _current_encryption(config)
        hmac_version, hmac_secret = _current_hmac(config)
        arguments_ciphertext = encrypt_mcp_value(
            arguments_bytes,
            secret=encryption_secret,
            owner=McpCryptoOwner(tenant_id, invocation_id, "arguments", encryption_version),
        )
        arguments_hmac = mcp_hmac(
            arguments_bytes,
            secret=hmac_secret,
            owner=McpHmacOwner(tenant_id, invocation_id, "arguments", hmac_version),
        )
        approval_required = bool(tool["approval_required"])
        approval_deadline = now + timedelta(seconds=int(tool["approval_expires_seconds"]))
        if dlp.status == "blocked":
            invocation_status = "blocked"
            error_code = "mcp.arguments_blocked"
        elif dlp.status == "unavailable":
            invocation_status = "failed"
            error_code = "mcp.unavailable"
        elif approval_required:
            invocation_status = "approval_pending"
            error_code = None
        else:
            invocation_status = "queued"
            error_code = None
        terminal = invocation_status in _TERMINAL_INVOCATION_STATUSES
        required_permissions_sha256 = _sha256(list(required_permissions))
        grant_sha256 = _sha256([list(role_ids), list(user_ids)])
        approval_deadline_text = _to_db_time(approval_deadline)
        if approval_required:
            _target_version, policy_fingerprint = mcp_approval_target_coordinates(
                config,
                invocation_id=invocation_id,
                tenant_id=tenant_id,
                server_id=str(tool["mcp_server_id"]),
                tool_id=normalized_id,
                requested_by=live.user.user_id,
                server_state_revision=int(tool["server_state_revision"]),
                credential_revision=int(tool["active_credential_revision"]),
                schema_revision=int(tool["schema_revision"]),
                policy_revision=int(tool["policy_revision"]),
                input_schema_original_sha256=str(tool["input_schema_original_sha256"]),
                input_schema_effective_sha256=str(tool["input_schema_effective_sha256"]),
                output_schema_original_sha256=(
                    str(tool["output_schema_original_sha256"])
                    if tool["output_schema_original_sha256"] is not None
                    else None
                ),
                output_schema_effective_sha256=(
                    str(tool["output_schema_effective_sha256"])
                    if tool["output_schema_effective_sha256"] is not None
                    else None
                ),
                required_permissions_sha256=required_permissions_sha256,
                grant_sha256=grant_sha256,
                arguments_hmac=arguments_hmac,
                arguments_hmac_key_version=hmac_version,
                approval_deadline_at=approval_deadline_text,
            )
        else:
            policy_fingerprint = _sha256(
                [
                    int(tool["server_state_revision"]),
                    int(tool["active_credential_revision"]),
                    int(tool["schema_revision"]),
                    int(tool["policy_revision"]),
                    str(tool["input_schema_original_sha256"]),
                    str(tool["input_schema_effective_sha256"]),
                    tool["output_schema_original_sha256"],
                    tool["output_schema_effective_sha256"],
                    str(tool["risk_level"]),
                    False,
                    required_permissions_sha256,
                    grant_sha256,
                    tool["result_redaction_pointers_json"],
                ]
            )
        values = {
            "mcp_invocation_id": invocation_id,
            "tenant_id": tenant_id,
            "mcp_server_id": tool["mcp_server_id"],
            "mcp_tool_id": normalized_id,
            "requested_by": live.user.user_id,
            "request_id": request_id,
            "trace_id": trace_id,
            "idempotency_key_hmac": current_key_hmac,
            "idempotency_key_hmac_version": current_key_version,
            "intent_hmac": current_intent_hmac,
            "intent_hmac_version": current_key_version,
            "server_state_revision": int(tool["server_state_revision"]),
            "credential_revision": int(tool["active_credential_revision"]),
            "schema_revision": int(tool["schema_revision"]),
            "policy_revision": int(tool["policy_revision"]),
            "policy_fingerprint": policy_fingerprint,
            "input_schema_original_sha256": tool["input_schema_original_sha256"],
            "input_schema_effective_sha256": tool["input_schema_effective_sha256"],
            "output_schema_original_sha256": tool["output_schema_original_sha256"],
            "output_schema_effective_sha256": tool["output_schema_effective_sha256"],
            "risk_level": tool["risk_level"],
            "approval_required": int(approval_required),
            "required_permissions_sha256": required_permissions_sha256,
            "grant_sha256": grant_sha256,
            "result_redaction_pointers_json": tool["result_redaction_pointers_json"],
            "arguments_ciphertext": arguments_ciphertext,
            "arguments_encryption_key_version": encryption_version,
            "arguments_bytes": len(arguments_bytes),
            "arguments_hmac": arguments_hmac,
            "arguments_hmac_key_version": hmac_version,
            "approval_task_id": approval_id if approval_required and not terminal else None,
            "approval_deadline_at": (
                approval_deadline_text if approval_required and not terminal else None
            ),
            "status": invocation_status,
            "state_revision": 1,
            "input_dlp_status": dlp.status,
            "input_dlp_match_count": dlp.match_count,
            "input_dlp_rule_id": dlp.rule_id,
            "input_dlp_ruleset_hmac": dlp.ruleset_hmac,
            "input_dlp_ruleset_hmac_key_version": dlp.ruleset_hmac_key_version,
            "input_dlp_scanned_at": timestamp,
            "output_dlp_status": None,
            "output_dlp_match_count": None,
            "output_dlp_rule_id": None,
            "output_dlp_ruleset_hmac": None,
            "output_dlp_ruleset_hmac_key_version": None,
            "output_dlp_scanned_at": None,
            "result_ciphertext": None,
            "result_encryption_key_version": None,
            "result_hmac": None,
            "result_hmac_key_version": None,
            "result_bytes": None,
            "result_redaction_count": None,
            "probe_attempt_count": 0,
            "call_attempt_count": 0,
            "request_bytes": len(arguments_bytes),
            "response_bytes": 0,
            "duration_ms": 0 if terminal else None,
            "execution_status": "not_called",
            "invocation_count": 1,
            "tool_call_count": 0,
            "cost_status": "not_applicable",
            "cost_micros": None,
            "error_code": error_code,
            "lease_generation": 0,
            "lease_owner_id": None,
            "lease_token": None,
            "lease_expires_at": None,
            "next_attempt_at": timestamp if invocation_status == "queued" else None,
            "created_at": timestamp,
            "queued_at": timestamp if invocation_status == "queued" else None,
            "started_at": None,
            "call_started_at": None,
            "completed_at": timestamp if terminal else None,
            "updated_at": timestamp,
        }
        _insert_mapping(connection, "mcp_invocations", values)
        if invocation_status == "approval_pending":
            create_mcp_approval_in_transaction(
                connection,
                config,
                tenant_id=tenant_id,
                invocation_id=invocation_id,
                approval_id=approval_id,
                created_by_id=live.user.user_id,
                expires_at=approval_deadline,
                request_id=request_id,
                trace_id=trace_id,
                now=now,
            )
        _ensure_scheduler_tenant(connection, tenant_id, timestamp)
        row = _load_invocation(connection, tenant_id, invocation_id)
        if row is None:
            raise ApiError("mcp.unavailable")
        response = McpInvocationResponse(
            invocation=_invocation_info(row),
            idempotent_replay=False,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        _raise_invocation_error(row)
        return response
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError as exc:
        _rollback(connection)
        raise ApiError("mcp.state_conflict") from exc
    except (sqlite3.Error, DbError, McpContractError, TypeError, ValueError) as exc:
        _rollback(connection)
        raise _mapped_error(exc) from exc
    finally:
        connection.close()


def list_invocations(
    config: AppConfig,
    context: AuthContext,
    *,
    status: str | None,
    tool_id: str | None,
    server_id: str | None,
    limit: str | None,
    cursor: str | None,
    request_id: str,
    trace_id: str,
) -> McpInvocationListResponse:
    _ensure_enabled(config)
    normalized_status = _optional_enum(status, _INVOCATION_STATUSES)
    normalized_tool = _resource_id(tool_id, "mcpt_") if tool_id is not None else None
    normalized_server = _resource_id(server_id, "mcps_") if server_id is not None else None
    if cursor is not None:
        _resource_id(cursor, "mcpi_")
    page_size = _page_limit(limit)
    clauses = ["tenant_id=?"]
    params: list[object] = [context.user.tenant_id]
    if not {"mcp.server.manage", "mcp.policy.manage"}.intersection(context.permission_codes):
        clauses.append("requested_by=?")
        params.append(context.user.user_id)
    for column, value in (
        ("status", normalized_status),
        ("mcp_tool_id", normalized_tool),
        ("mcp_server_id", normalized_server),
    ):
        if value is not None:
            clauses.append(f"{column}=?")
            params.append(value)
    if cursor is not None:
        clauses.append("mcp_invocation_id>?")
        params.append(cursor)
    params.append(page_size + 1)
    connection = _connect(config)
    try:
        rows = connection.execute(
            f"""
            SELECT * FROM mcp_invocations
            WHERE {" AND ".join(clauses)}
            ORDER BY mcp_invocation_id LIMIT ?
            """,
            params,
        ).fetchall()
        visible = rows[:page_size]
        return McpInvocationListResponse(
            invocations=[_invocation_info(row) for row in visible],
            next_cursor=(str(visible[-1]["mcp_invocation_id"]) if len(rows) > page_size else None),
            request_id=request_id,
            trace_id=trace_id,
        )
    finally:
        connection.close()


def get_invocation(
    config: AppConfig,
    context: AuthContext,
    invocation_id: str,
    *,
    request_id: str,
    trace_id: str,
) -> McpInvocationResponse:
    _ensure_enabled(config)
    normalized_id = _resource_id(invocation_id, "mcpi_")
    connection = _connect(config)
    try:
        row = _load_invocation(connection, context.user.tenant_id, normalized_id)
        if row is None or (
            row["requested_by"] != context.user.user_id
            and not {"mcp.server.manage", "mcp.policy.manage"}.intersection(
                context.permission_codes
            )
        ):
            raise ApiError("mcp.invocation_not_found")
        return McpInvocationResponse(
            invocation=_invocation_info(row),
            idempotent_replay=False,
            request_id=request_id,
            trace_id=trace_id,
        )
    finally:
        connection.close()


def get_invocation_result(
    config: AppConfig,
    context: AuthContext,
    invocation_id: str,
    *,
    request_id: str,
    trace_id: str,
) -> McpInvocationResultResponse:
    _ensure_enabled(config)
    normalized_id = _resource_id(invocation_id, "mcpi_")
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        live = _transaction_permission(connection, context, "mcp.invoke")
        row = _load_invocation(connection, live.user.tenant_id, normalized_id)
        if row is None or row["requested_by"] != live.user.user_id:
            raise ApiError("mcp.invocation_not_found")
        if row["status"] != "succeeded" or row["result_ciphertext"] is None:
            raise ApiError("mcp.state_conflict")
        tool = _load_invocable_tool(connection, live.user.tenant_id, str(row["mcp_tool_id"]))
        if tool is None:
            raise ApiError("mcp.authorization_revoked")
        _require_result_authorization(connection, row, tool, live)
        plaintext, _version = decrypt_mcp_value_from_keyring(
            row["result_ciphertext"],
            keyring=_encryption_keyring(config),
            tenant_id=live.user.tenant_id,
            owner_id=normalized_id,
            kind="result",
        )
        try:
            result = json.loads(plaintext.decode("utf-8", errors="strict"))
        except (json.JSONDecodeError, UnicodeError):
            raise ApiError("mcp.unavailable") from None
        connection.commit()
        return McpInvocationResultResponse(
            mcp_invocation_id=normalized_id,
            result=result,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (McpContractError, TypeError, ValueError) as exc:
        _rollback(connection)
        raise ApiError("mcp.unavailable") from exc
    finally:
        connection.close()


def _insert_operation(
    connection: sqlite3.Connection,
    *,
    operation_id: str,
    tenant_id: str,
    server_id: str,
    operation_type: str,
    key_version: int,
    key_hmac: str,
    intent_version: int,
    intent_hmac: str,
    actor_id: str,
    start_server_revision: int,
    start_credential_revision: int | None,
    candidate_credential_revision: int | None,
    request_id: str,
    trace_id: str,
    timestamp: str,
) -> None:
    connection.execute(
        """
        INSERT INTO mcp_server_operations(
          mcp_server_operation_id,tenant_id,mcp_server_id,operation_type,
          idempotency_key_hmac,idempotency_key_hmac_version,intent_hmac,
          intent_hmac_version,actor_user_id,start_server_revision,
          start_credential_revision,candidate_credential_revision,status,error_code,
          request_id,trace_id,created_at,completed_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?,?)
        """,
        (
            operation_id,
            tenant_id,
            server_id,
            operation_type,
            key_hmac,
            key_version,
            intent_hmac,
            intent_version,
            actor_id,
            start_server_revision,
            start_credential_revision,
            candidate_credential_revision,
            "queued",
            request_id,
            trace_id,
            timestamp,
            None,
            timestamp,
        ),
    )


def _insert_sync_job(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    job_id: str,
    tenant_id: str,
    server_id: str,
    operation_id: str,
    candidate_credential_revision: int | None,
    job_type: str,
    now: datetime,
) -> None:
    timestamp = _to_db_time(now)
    deadline_seconds = int(getattr(config, "mcp_probe_deadline_seconds", 10))
    deadline_seconds = min(max(deadline_seconds, 1), 120)
    connection.execute(
        """
        INSERT INTO mcp_server_sync_jobs(
          mcp_server_sync_job_id,tenant_id,mcp_server_id,mcp_server_operation_id,
          candidate_credential_revision,job_type,status,attempt_count,lease_generation,
          lease_owner_id,lease_token,lease_expires_at,next_attempt_at,deadline_at,error_code,
          created_at,started_at,completed_at,updated_at
        ) VALUES(?,?,?,?,?,?,'pending',0,0,NULL,NULL,NULL,?,?,NULL,?,NULL,NULL,?)
        """,
        (
            job_id,
            tenant_id,
            server_id,
            operation_id,
            candidate_credential_revision,
            job_type,
            timestamp,
            _to_db_time(
                now
                + timedelta(
                    seconds=deadline_seconds + _SYNC_FINALIZE_RESERVE_SECONDS
                )
            ),
            timestamp,
            timestamp,
        ),
    )


def _ensure_scheduler_tenant(
    connection: sqlite3.Connection, tenant_id: str, timestamp: str
) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO mcp_scheduler_tenants(
          tenant_id,status,last_claimed_at,last_claim_sequence,lease_generation,
          lease_owner_id,lease_token,lease_expires_at,next_scan_at,created_at,updated_at
        ) VALUES(?,'idle',NULL,0,0,NULL,NULL,NULL,?,?,?)
        """,
        (tenant_id, timestamp, timestamp, timestamp),
    )


def _load_server(
    connection: sqlite3.Connection, tenant_id: str, server_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT server.*,
          (SELECT COUNT(*) FROM mcp_tools tool
           WHERE tool.tenant_id=server.tenant_id
             AND tool.mcp_server_id=server.mcp_server_id) AS tool_count,
          (SELECT COUNT(*) FROM mcp_tools tool
           WHERE tool.tenant_id=server.tenant_id
             AND tool.mcp_server_id=server.mcp_server_id
             AND tool.status='active') AS active_tool_count
        FROM mcp_servers server
        WHERE server.tenant_id=? AND server.mcp_server_id=?
        """,
        (tenant_id, server_id),
    ).fetchone()


def _load_operation(
    connection: sqlite3.Connection, tenant_id: str, operation_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT * FROM mcp_server_operations
        WHERE tenant_id=? AND mcp_server_operation_id=?
        """,
        (tenant_id, operation_id),
    ).fetchone()


def _load_tool_with_counts(
    connection: sqlite3.Connection, tenant_id: str, tool_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT tool.*,
          (SELECT COUNT(*) FROM mcp_tool_role_grants grant_row
           WHERE grant_row.tenant_id=tool.tenant_id
             AND grant_row.mcp_tool_id=tool.mcp_tool_id) AS allowed_role_count,
          (SELECT COUNT(*) FROM mcp_tool_user_grants grant_row
           WHERE grant_row.tenant_id=tool.tenant_id
             AND grant_row.mcp_tool_id=tool.mcp_tool_id) AS allowed_user_count
        FROM mcp_tools tool
        WHERE tool.tenant_id=? AND tool.mcp_tool_id=?
        """,
        (tenant_id, tool_id),
    ).fetchone()


def _load_invocable_tool(
    connection: sqlite3.Connection, tenant_id: str, tool_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT tool.*,server.status AS server_status,
               server.state_revision AS server_state_revision,
               server.active_credential_revision
        FROM mcp_tools tool
        JOIN mcp_servers server
          ON server.tenant_id=tool.tenant_id
         AND server.mcp_server_id=tool.mcp_server_id
        WHERE tool.tenant_id=? AND tool.mcp_tool_id=?
        """,
        (tenant_id, tool_id),
    ).fetchone()


def _load_invocation(
    connection: sqlite3.Connection, tenant_id: str, invocation_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT * FROM mcp_invocations
        WHERE tenant_id=? AND mcp_invocation_id=?
        """,
        (tenant_id, invocation_id),
    ).fetchone()


def _server_response(
    connection: sqlite3.Connection,
    server: sqlite3.Row,
    operation: sqlite3.Row,
    *,
    request_id: str,
    trace_id: str,
    idempotent_replay: bool,
) -> McpServerResponse:
    del connection
    return McpServerResponse(
        server=_server_info(server),
        operation=_operation_info(operation),
        idempotent_replay=idempotent_replay,
        request_id=request_id,
        trace_id=trace_id,
    )


def _server_info(row: sqlite3.Row) -> McpServerInfo:
    return McpServerInfo(
        mcp_server_id=str(row["mcp_server_id"]),
        name=str(row["name"]),
        endpoint_hint=f"{row['endpoint_scheme']}://{row['endpoint_host_hint']}",
        endpoint_has_path=bool(row["endpoint_has_path"]),
        transport="streamable_http",
        status=str(row["status"]),  # type: ignore[arg-type]
        state_revision=int(row["state_revision"]),
        active_credential_revision=(
            int(row["active_credential_revision"])
            if row["active_credential_revision"] is not None
            else None
        ),
        protocol_version=(str(row["protocol_version"]) if row["protocol_version"] else None),
        tool_count=int(row["tool_count"]),
        active_tool_count=int(row["active_tool_count"]),
        created_at=_db_datetime(row["created_at"]),
        updated_at=_db_datetime(row["updated_at"]),
    )


def _operation_info(row: sqlite3.Row) -> McpServerOperationInfo:
    return McpServerOperationInfo(
        mcp_server_operation_id=str(row["mcp_server_operation_id"]),
        mcp_server_id=str(row["mcp_server_id"]),
        operation_type=str(row["operation_type"]),  # type: ignore[arg-type]
        status=str(row["status"]),  # type: ignore[arg-type]
        error_code=(str(row["error_code"]) if row["error_code"] is not None else None),
        created_at=_db_datetime(row["created_at"]),
        completed_at=(
            _db_datetime(row["completed_at"]) if row["completed_at"] is not None else None
        ),
        updated_at=_db_datetime(row["updated_at"]),
    )


def _tool_info(row: sqlite3.Row) -> McpToolInfo:
    return McpToolInfo(
        mcp_tool_id=str(row["mcp_tool_id"]),
        mcp_server_id=str(row["mcp_server_id"]),
        remote_name=str(row["remote_name"]),
        title=(str(row["title"]) if row["title"] is not None else None),
        schema_revision=int(row["schema_revision"]),
        policy_revision=int(row["policy_revision"]),
        input_schema_sha256=str(row["input_schema_effective_sha256"]),
        output_schema_sha256=(
            str(row["output_schema_effective_sha256"])
            if row["output_schema_effective_sha256"] is not None
            else None
        ),
        risk_level=str(row["risk_level"]),  # type: ignore[arg-type]
        approval_required=bool(row["approval_required"]),
        approval_expires_seconds=int(row["approval_expires_seconds"]),
        required_permission_codes=_json_string_list(row["required_permission_codes_json"]),
        allowed_role_count=int(row["allowed_role_count"]),
        allowed_user_count=int(row["allowed_user_count"]),
        result_redaction_count=len(_json_string_list(row["result_redaction_pointers_json"])),
        status=str(row["status"]),  # type: ignore[arg-type]
        created_at=_db_datetime(row["created_at"]),
        updated_at=_db_datetime(row["updated_at"]),
    )


def _tool_detail(connection: sqlite3.Connection, row: sqlite3.Row) -> McpToolDetailInfo:
    info = _tool_info(row).model_dump(mode="python")
    return McpToolDetailInfo(
        **info,
        description=(str(row["description"]) if row["description"] is not None else None),
        input_schema_original=_json_object(row["input_schema_original_json"]),
        input_schema_effective=_json_object(row["input_schema_effective_json"]),
        output_schema_original=(
            _json_object(row["output_schema_original_json"])
            if row["output_schema_original_json"] is not None
            else None
        ),
        output_schema_effective=(
            _json_object(row["output_schema_effective_json"])
            if row["output_schema_effective_json"] is not None
            else None
        ),
        allowed_role_ids=list(
            _tool_role_ids(connection, str(row["tenant_id"]), str(row["mcp_tool_id"]))
        ),
        allowed_user_ids=list(
            _tool_user_ids(connection, str(row["tenant_id"]), str(row["mcp_tool_id"]))
        ),
        result_redaction_pointers=_json_string_list(row["result_redaction_pointers_json"]),
    )


def _invocation_info(row: sqlite3.Row) -> McpInvocationInfo:
    return McpInvocationInfo(
        mcp_invocation_id=str(row["mcp_invocation_id"]),
        mcp_server_id=str(row["mcp_server_id"]),
        mcp_tool_id=str(row["mcp_tool_id"]),
        status=str(row["status"]),  # type: ignore[arg-type]
        risk_level=str(row["risk_level"]),  # type: ignore[arg-type]
        approval_required=bool(row["approval_required"]),
        approval_task_id=(
            str(row["approval_task_id"]) if row["approval_task_id"] is not None else None
        ),
        input_dlp=McpDlpSummary(
            status=str(row["input_dlp_status"]),  # type: ignore[arg-type]
            match_count=int(row["input_dlp_match_count"]),
            rule_id=(str(row["input_dlp_rule_id"]) if row["input_dlp_rule_id"] else None),
        ),
        output_dlp=McpDlpSummary(
            status=(str(row["output_dlp_status"]) if row["output_dlp_status"] else None),  # type: ignore[arg-type]
            match_count=(
                int(row["output_dlp_match_count"])
                if row["output_dlp_match_count"] is not None
                else None
            ),
            rule_id=(str(row["output_dlp_rule_id"]) if row["output_dlp_rule_id"] else None),
        ),
        result_redaction_count=(
            int(row["result_redaction_count"])
            if row["result_redaction_count"] is not None
            else 0
        ),
        probe_attempt_count=int(row["probe_attempt_count"]),
        call_attempt_count=int(row["call_attempt_count"]),
        request_bytes=int(row["request_bytes"]),
        response_bytes=int(row["response_bytes"]),
        duration_ms=(int(row["duration_ms"]) if row["duration_ms"] is not None else None),
        execution_status=str(row["execution_status"]),  # type: ignore[arg-type]
        invocation_count=1,
        tool_call_count=(
            int(row["tool_call_count"]) if row["tool_call_count"] is not None else None
        ),
        cost_status=str(row["cost_status"]),  # type: ignore[arg-type]
        error_code=(str(row["error_code"]) if row["error_code"] is not None else None),
        created_at=_db_datetime(row["created_at"]),
        queued_at=_optional_db_datetime(row["queued_at"]),
        started_at=_optional_db_datetime(row["started_at"]),
        completed_at=_optional_db_datetime(row["completed_at"]),
        updated_at=_db_datetime(row["updated_at"]),
    )


def _find_operation(
    connection: sqlite3.Connection,
    tenant_id: str,
    actor_id: str,
    candidates: tuple[tuple[int, str], ...],
) -> sqlite3.Row | None:
    for version, digest in candidates:
        row = connection.execute(
            """
            SELECT * FROM mcp_server_operations
            WHERE tenant_id=? AND actor_user_id=?
              AND idempotency_key_hmac_version=? AND idempotency_key_hmac=?
            """,
            (tenant_id, actor_id, version, digest),
        ).fetchone()
        if row is not None:
            return row
    return None


def _find_invocation_replay(
    connection: sqlite3.Connection,
    tenant_id: str,
    actor_id: str,
    candidates: tuple[tuple[int, str], ...],
) -> sqlite3.Row | None:
    for version, digest in candidates:
        row = connection.execute(
            """
            SELECT * FROM mcp_invocations
            WHERE tenant_id=? AND requested_by=?
              AND idempotency_key_hmac_version=? AND idempotency_key_hmac=?
            """,
            (tenant_id, actor_id, version, digest),
        ).fetchone()
        if row is not None:
            return row
    return None


def _find_server_by_endpoint(
    connection: sqlite3.Connection,
    tenant_id: str,
    candidates: tuple[tuple[int, str], ...],
) -> sqlite3.Row | None:
    for version, digest in candidates:
        row = connection.execute(
            """
            SELECT * FROM mcp_servers
            WHERE tenant_id=? AND endpoint_hmac_key_version=? AND endpoint_hmac=?
            """,
            (tenant_id, version, digest),
        ).fetchone()
        if row is not None:
            return row
    return None


def _require_replay_intent(row: sqlite3.Row, candidates: tuple[tuple[int, str], ...]) -> None:
    expected = dict(candidates).get(int(row["intent_hmac_version"]))
    if expected is None or not hmac.compare_digest(expected, str(row["intent_hmac"])):
        raise ApiError("mcp.idempotency_conflict")


def _require_tool_grant(
    connection: sqlite3.Connection,
    tenant_id: str,
    tool_id: str,
    context: AuthContext,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    role_ids = _tool_role_ids(connection, tenant_id, tool_id)
    user_ids = _tool_user_ids(connection, tenant_id, tool_id)
    if not role_ids and not user_ids:
        raise ApiError("auth.permission_denied")
    role = connection.execute(
        "SELECT role_id FROM roles WHERE tenant_id=? AND code=? AND status='active'",
        (tenant_id, context.user.role),
    ).fetchone()
    role_id = str(role["role_id"]) if role is not None else None
    if context.user.user_id not in user_ids and role_id not in role_ids:
        raise ApiError("auth.permission_denied")
    return role_ids, user_ids


def _require_result_authorization(
    connection: sqlite3.Connection,
    invocation: sqlite3.Row,
    tool: sqlite3.Row,
    context: AuthContext,
) -> None:
    """Recheck the frozen permission/grant contract before releasing a result."""

    tenant_id = context.user.tenant_id
    tool_id = str(invocation["mcp_tool_id"])
    permissions = tuple(_json_string_list(tool["required_permission_codes_json"]))
    role_ids = _tool_role_ids(connection, tenant_id, tool_id)
    user_ids = _tool_user_ids(connection, tenant_id, tool_id)
    if (
        not hmac.compare_digest(
            _sha256(list(permissions)), str(invocation["required_permissions_sha256"])
        )
        or not hmac.compare_digest(
            _sha256([list(role_ids), list(user_ids)]), str(invocation["grant_sha256"])
        )
        or any(permission not in context.permission_codes for permission in permissions)
    ):
        raise ApiError("mcp.authorization_revoked")
    role = connection.execute(
        "SELECT role_id FROM roles WHERE tenant_id=? AND code=? AND status='active'",
        (tenant_id, context.user.role),
    ).fetchone()
    role_id = str(role["role_id"]) if role is not None else None
    if context.user.user_id not in user_ids and role_id not in role_ids:
        raise ApiError("mcp.authorization_revoked")


def _tool_role_ids(connection: sqlite3.Connection, tenant_id: str, tool_id: str) -> tuple[str, ...]:
    return tuple(
        str(row["role_id"])
        for row in connection.execute(
            """
            SELECT role_id FROM mcp_tool_role_grants
            WHERE tenant_id=? AND mcp_tool_id=? ORDER BY role_id
            """,
            (tenant_id, tool_id),
        ).fetchall()
    )


def _tool_user_ids(connection: sqlite3.Connection, tenant_id: str, tool_id: str) -> tuple[str, ...]:
    return tuple(
        str(row["user_id"])
        for row in connection.execute(
            """
            SELECT user_id FROM mcp_tool_user_grants
            WHERE tenant_id=? AND mcp_tool_id=? ORDER BY user_id
            """,
            (tenant_id, tool_id),
        ).fetchall()
    )


def _validate_grant_owners(
    connection: sqlite3.Connection,
    tenant_id: str,
    role_ids: tuple[str, ...],
    user_ids: tuple[str, ...],
) -> None:
    for role_id in role_ids:
        row = connection.execute(
            "SELECT 1 FROM roles WHERE tenant_id=? AND role_id=? AND status='active'",
            (tenant_id, role_id),
        ).fetchone()
        if row is None:
            raise ApiError("mcp.request_invalid")
    for user_id in user_ids:
        row = connection.execute(
            "SELECT 1 FROM users WHERE tenant_id=? AND user_id=? AND status='active'",
            (tenant_id, user_id),
        ).fetchone()
        if row is None:
            raise ApiError("mcp.request_invalid")


def _validate_permission_catalog(
    connection: sqlite3.Connection, permissions: tuple[str, ...]
) -> None:
    for permission in permissions:
        row = connection.execute(
            "SELECT 1 FROM rbac_permission_templates WHERE permission_code=?",
            (permission,),
        ).fetchone()
        if row is None:
            raise ApiError("mcp.request_invalid")


def _scan_dlp(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    tenant_id: str,
    invocation_id: str,
    value: object,
    scope: str,
) -> DlpDecision:
    rows = connection.execute(
        """
        SELECT dlp_rule_id,rule_type,pattern,pattern_hash,action,scan_scope,priority
        FROM dlp_rules
        WHERE tenant_id=? AND status='active' AND scan_scope IN(?, 'both')
        ORDER BY priority ASC,updated_at DESC,dlp_rule_id ASC LIMIT 100
        """,
        (tenant_id, scope),
    ).fetchall()
    safe_rules = [
        [
            row["dlp_rule_id"],
            row["rule_type"],
            row["pattern_hash"],
            row["action"],
            row["scan_scope"],
            int(row["priority"]),
        ]
        for row in rows
    ]
    hmac_version, hmac_secret = _current_hmac(config)
    ruleset_hmac = mcp_hmac(
        _canonical(safe_rules),
        secret=hmac_secret,
        owner=McpHmacOwner(tenant_id, invocation_id, f"{scope}_ruleset", hmac_version),  # type: ignore[arg-type]
    )
    texts = _json_strings(value)
    matches: list[sqlite3.Row] = []
    try:
        for row in rows:
            matched = False
            if row["rule_type"] == "keyword":
                matched = any(str(row["pattern"]) in text for text in texts)
            elif row["rule_type"] == "regex":
                compiled = compile_safe_dlp_regex(str(row["pattern"])).compiled
                matched = any(compiled.search(text) is not None for text in texts)
            else:
                return DlpDecision("unavailable", 0, None, ruleset_hmac, hmac_version)
            if matched:
                matches.append(row)
    except (UnsafeRegexError, TypeError, ValueError, RecursionError):
        return DlpDecision("unavailable", 0, None, ruleset_hmac, hmac_version)
    blocking = next((row for row in matches if row["action"] == "block"), None)
    logging_match = next((row for row in matches if row["action"] == "log_only"), None)
    if blocking is not None:
        return DlpDecision(
            "blocked", len(matches), str(blocking["dlp_rule_id"]), ruleset_hmac, hmac_version
        )
    if logging_match is not None:
        return DlpDecision(
            "log_only",
            len(matches),
            str(logging_match["dlp_rule_id"]),
            ruleset_hmac,
            hmac_version,
        )
    return DlpDecision("clean", len(matches), None, ruleset_hmac, hmac_version)


def _json_strings(value: object) -> tuple[str, ...]:
    result: list[str] = []
    stack = [value]
    while stack:
        current = stack.pop()
        if type(current) is str:
            result.append(current)
        elif type(current) is dict:
            stack.extend(current.values())
        elif type(current) is list:
            stack.extend(current)
    return tuple(result)


def _credential_material(
    auth_type: str,
    token: str | None,
    *,
    tenant_id: str,
    server_id: str,
    credential_id: str,
    encryption_version: int,
    encryption_secret: str,
    hmac_version: int,
    hmac_secret: str,
) -> tuple[str | None, str | None]:
    if auth_type == "none":
        return None, None
    if token is None:
        raise ApiError("mcp.request_invalid")
    ciphertext = encrypt_mcp_value(
        token,
        secret=encryption_secret,
        owner=McpCryptoOwner(tenant_id, credential_id, "bearer", encryption_version),
    )
    digest = mcp_hmac(
        token,
        secret=hmac_secret,
        owner=McpHmacOwner(tenant_id, server_id, "credential", hmac_version),
    )
    return ciphertext, digest


def _hmac_candidates(
    config: AppConfig,
    value: object,
    tenant_id: str,
    owner_id: str,
    kind: str,
) -> tuple[tuple[int, str], ...]:
    try:
        return mcp_hmac_candidates(
            value,
            keyring=_hmac_keyring(config),
            tenant_id=tenant_id,
            owner_id=owner_id,
            kind=kind,  # type: ignore[arg-type]
        )
    except McpContractError as exc:
        raise ApiError(exc.code) from exc


def _current_encryption(config: AppConfig) -> tuple[int, str]:
    keyring = _encryption_keyring(config)
    return keyring[0].version, keyring[0].secret


def _current_hmac(config: AppConfig) -> tuple[int, str]:
    keyring = _hmac_keyring(config)
    return keyring[0].version, keyring[0].secret


def _encryption_keyring(config: AppConfig) -> tuple[McpVersionedSecret, ...]:
    return _keyring(config, "encryption")


def _hmac_keyring(config: AppConfig) -> tuple[McpVersionedSecret, ...]:
    return _keyring(config, "hmac")


def _keyring(config: AppConfig, kind: str) -> tuple[McpVersionedSecret, ...]:
    version = getattr(config, f"mcp_{kind}_version", None)
    secret = getattr(config, f"mcp_{kind}_secret", None)
    if type(version) is not int or type(secret) is not str:
        raise ApiError("mcp.feature_disabled")
    result = [McpVersionedSecret(version, secret)]
    previous_version = getattr(config, f"mcp_{kind}_previous_version", None)
    previous_secret = getattr(config, f"mcp_{kind}_previous_secret", None)
    if previous_version is not None or previous_secret is not None:
        if type(previous_version) is not int or type(previous_secret) is not str:
            raise ApiError("mcp.feature_disabled")
        result.append(McpVersionedSecret(previous_version, previous_secret))
    return tuple(result)


def _schema_limits(config: AppConfig) -> McpSchemaLimits:
    return config.mcp_schema_limits


def _server_audit_summary(row: sqlite3.Row) -> dict[str, object]:
    return {
        "mcp_server_id": str(row["mcp_server_id"]),
        "status": str(row["status"]),
        "state_revision": int(row["state_revision"]),
        "active_credential_revision": (
            int(row["active_credential_revision"])
            if row["active_credential_revision"] is not None
            else None
        ),
        "tool_count": int(row["tool_count"]),
        "active_tool_count": int(row["active_tool_count"]),
    }


def _tool_audit_summary(
    row: sqlite3.Row,
    *,
    include_policy: bool = True,
    include_grants: bool = True,
) -> dict[str, object]:
    summary: dict[str, object] = {
        "mcp_tool_id": str(row["mcp_tool_id"]),
        "mcp_server_id": str(row["mcp_server_id"]),
        "policy_revision": int(row["policy_revision"]),
    }
    if include_policy:
        summary.update(
            {
                "status": str(row["status"]),
                "schema_revision": int(row["schema_revision"]),
                "risk_level": str(row["risk_level"]),
                "approval_required": bool(row["approval_required"]),
                "required_permission_count": len(
                    _json_string_list(row["required_permission_codes_json"])
                ),
                "redaction_count": len(
                    _json_string_list(row["result_redaction_pointers_json"])
                ),
            }
        )
    if include_grants:
        summary.update(
            {
                "role_grant_count": int(row["allowed_role_count"]),
                "user_grant_count": int(row["allowed_user_count"]),
            }
        )
    return summary


def _audit_request_meta(value: AuditRequestMeta | None) -> AuditRequestMeta:
    return value if value is not None else AuditRequestMeta(client_ip=None, user_agent=None)


def _transaction_permission(
    connection: sqlite3.Connection, context: AuthContext, permission: str
) -> AuthContext:
    live = refresh_transaction_authentication(
        connection, context, unavailable_error_code="mcp.unavailable"
    )
    return require_permission(live, permission)


def _endpoint(config: AppConfig, value: str) -> str:
    try:
        return validate_mcp_endpoint_url(
            value,
            production=getattr(config, "environment", "dev") == "prod",
            allow_test_loopback=bool(getattr(config, "mcp_test_loopback_enabled", False)),
        )
    except McpContractError as exc:
        raise ApiError(exc.code) from exc


def _ensure_enabled(config: AppConfig) -> None:
    if getattr(config, "mcp_enabled", False) is not True:
        raise ApiError("mcp.feature_disabled")


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except DbError as exc:
        raise ApiError("mcp.unavailable") from exc


def _insert_mapping(connection: sqlite3.Connection, table: str, values: dict[str, object]) -> None:
    allowed = {"mcp_invocations"}
    if table not in allowed or not values:
        raise ApiError("mcp.unavailable")
    columns = tuple(values)
    placeholders = ",".join("?" for _ in columns)
    connection.execute(
        f"INSERT INTO {table}({','.join(columns)}) VALUES({placeholders})",
        tuple(values[column] for column in columns),
    )


def _mapped_error(exc: BaseException) -> ApiError:
    if isinstance(exc, McpContractError):
        return ApiError(exc.code)
    return ApiError("mcp.unavailable")


def _raise_invocation_error(row: sqlite3.Row) -> None:
    if row["status"] in {"blocked", "failed"} and row["error_code"] in {
        "mcp.arguments_blocked",
        "mcp.unavailable",
    }:
        raise ApiError(str(row["error_code"]))


def _rollback(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        connection.rollback()


def _resource_id(value: object, prefix: str) -> str:
    if (
        type(value) is not str
        or len(value) != len(prefix) + 32
        or not value.startswith(prefix)
        or any(character not in "0123456789abcdef" for character in value[len(prefix) :])
    ):
        raise ApiError("mcp.request_invalid")
    return value


def _display_name(value: object, *, maximum: int) -> str:
    if type(value) is not str:
        raise ApiError("mcp.request_invalid")
    normalized = unicodedata.normalize("NFC", value).strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(unicodedata.category(character).startswith("C") for character in normalized)
    ):
        raise ApiError("mcp.request_invalid")
    return normalized


def _mask_host(host: str) -> str:
    if not host:
        raise ApiError("mcp.request_invalid")
    if ":" in host:
        return "[::***]"
    if host.replace(".", "").isdigit():
        parts = host.split(".")
        return f"{parts[0]}.***.***.{parts[-1]}" if len(parts) == 4 else "ip-***"
    labels = host.split(".")
    suffix = labels[-1]
    return f"{labels[0][:1]}***.{suffix}" if len(labels) > 1 else f"{host[:1]}***"


def _page_limit(value: str | None) -> int:
    if value is None:
        return 50
    if not value.isascii() or not value.isdigit():
        raise ApiError("mcp.request_invalid")
    parsed = int(value)
    if not 1 <= parsed <= 100:
        raise ApiError("mcp.request_invalid")
    return parsed


def _optional_enum(value: str | None, allowed: set[str]) -> str | None:
    if value is None:
        return None
    if value not in allowed:
        raise ApiError("mcp.request_invalid")
    return value


def _optional_boolean_query(value: str | None) -> int | None:
    if value is None:
        return None
    if value == "true":
        return 1
    if value == "false":
        return 0
    raise ApiError("mcp.request_invalid")


def _json_string_list(value: object) -> list[str]:
    try:
        parsed = json.loads(str(value))
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ApiError("mcp.unavailable") from None
    if type(parsed) is not list or any(type(item) is not str for item in parsed):
        raise ApiError("mcp.unavailable")
    return parsed


def _json_object(value: object) -> dict[str, object]:
    try:
        parsed = json.loads(str(value))
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ApiError("mcp.unavailable") from None
    if type(parsed) is not dict or any(type(key) is not str for key in parsed):
        raise ApiError("mcp.unavailable")
    return parsed


def _canonical(value: object) -> bytes:
    try:
        return canonical_json_bytes(value, maximum=1_048_576, code="mcp.request_invalid")
    except McpContractError as exc:
        raise ApiError(exc.code) from exc


def _canonical_json_text(value: object) -> str:
    return _canonical(value).decode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _to_db_time(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _db_datetime(value: object) -> datetime:
    if type(value) is not str:
        raise ApiError("mcp.unavailable")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ApiError("mcp.unavailable") from None
    if parsed.tzinfo is None:
        raise ApiError("mcp.unavailable")
    return parsed.astimezone(UTC)


def _optional_db_datetime(value: object) -> datetime | None:
    return None if value is None else _db_datetime(value)


__all__ = [
    "get_invocation",
    "get_invocation_result",
    "get_server_operation",
    "get_tool",
    "invoke_tool",
    "list_invocations",
    "list_servers",
    "list_tools",
    "refresh_server_tools",
    "register_server",
    "update_server",
    "update_tool_policy",
]
