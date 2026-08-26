"""Database-authoritative PRD-118 Service Account and OAuth machine identity service."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Never
from uuid import uuid4

from metahub_shared.db import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    authenticate_workspace_session_in_transaction,
    workspace_actor_authorization_probe_in_transaction,
    workspace_actor_permissions_in_transaction,
)

from .models import (
    OAuthClientCreateRequest,
    OAuthClientCredentialResponse,
    OAuthClientSummary,
    OAuthTokenResponse,
    ServiceAccountCreateRequest,
    ServiceAccountListResponse,
    ServiceAccountResponse,
    ServiceAccountSummary,
)

_SERVICE_ACCOUNT_RE = re.compile(r"^svc_[0-9a-f]{32}$")
_CLIENT_ID_RE = re.compile(r"^moc_[0-9a-f]{32}$")
_SECRET_RE = re.compile(r"^mcs_[A-Za-z0-9_-]{32,80}$")
_TOKEN_RE = re.compile(r"^mat_[A-Za-z0-9_-]{32,80}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,79}$")
_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
_MANAGE_ROLES = frozenset({"owner", "security_admin"})
_READ_ROLES = frozenset({"owner", "security_admin", "developer"})
_TEMPLATE_SCOPES = {
    "provider_reader": ("provider.read",),
    "observability_reader": ("request_log.read",),
}
_MACHINE_ROUTES = {
    ("GET", "/providers"): ("provider.read", "machine.provider_listed", "provider_collection"),
    ("GET", "/request-logs"): (
        "request_log.read",
        "machine.request_log_listed",
        "request_log_collection",
    ),
}
_AUDIENCE = "metahub-control"
_CLIENT_SECRET_LIFETIME = timedelta(days=365)
_ROTATION_GRACE = timedelta(seconds=30)
_TOKEN_FAILURE_WINDOW = timedelta(minutes=1)
_TOKEN_FAILURE_LOCK = timedelta(minutes=2)
_TOKEN_FAILURE_LIMIT = 5
_LAST_USED_TOUCH_INTERVAL = timedelta(seconds=60)


@dataclass(frozen=True, slots=True)
class MachineAuthContext:
    """Machine principal restored from one opaque grant without a personal Session."""

    subject_type: str
    service_account_id: str
    oauth_client_id: str
    access_token_grant_id: str
    workspace_id: str
    tenant_id: str
    audience: str
    permission_codes: tuple[str, ...]
    permission_revision: int
    authorization_fingerprint: str
    expires_at: datetime

    @property
    def user(self) -> SimpleNamespace:
        return SimpleNamespace(
            user_id=self.service_account_id,
            tenant_id=self.tenant_id,
            email="",
            display_name=self.service_account_id,
            role="service_account",
            permission_codes=self.permission_codes,
            permission_revision=self.permission_revision,
            authorization_fingerprint=self.authorization_fingerprint,
        )

    @property
    def session(self) -> SimpleNamespace:
        return SimpleNamespace(session_id=self.access_token_grant_id, expires_at=self.expires_at)


def list_service_accounts(
    config: AppConfig,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> ServiceAccountListResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = authenticate_workspace_session_in_transaction(
            connection, config, session_token=session_token, touch=True
        )
        _require_actor(actor, "machine_identity.read", _READ_ROLES)
        rows = connection.execute(
            """SELECT * FROM service_accounts
                 WHERE workspace_id=? AND tenant_id=?
                 ORDER BY created_at DESC,service_account_id DESC""",
            (actor.workspace_id, actor.tenant_id),
        ).fetchall()
        result = ServiceAccountListResponse(
            service_accounts=[_service_account_summary(connection, row) for row in rows],
            request_id=_request_identifier(request_id),
            trace_id=_request_identifier(trace_id),
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("machine_identity.unavailable") from None
    finally:
        connection.close()


def create_service_account(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    idempotency_key: str,
    payload: ServiceAccountCreateRequest,
    request_id: str,
    trace_id: str,
) -> ServiceAccountResponse:
    _valid_idempotency(idempotency_key)
    name = _normalized_name(payload.name)
    display_name = _display_name(payload.display_name)
    scopes = _normalized_scopes(payload.scope_template, payload.scopes)
    ip_allowlist = _normalized_networks(payload.ip_allowlist)
    mtls_fingerprint = _normalized_mtls(payload.mtls_fingerprint_sha256)
    constraint_sha256 = _constraint_digest(ip_allowlist, mtls_fingerprint)
    request_sha256 = _request_digest(
        {
            "name": name,
            "display_name": display_name,
            "scope_template": payload.scope_template,
            "scopes": scopes,
            "token_ttl_seconds": payload.token_ttl_seconds,
            "ip_allowlist": ip_allowlist,
            "mtls_fingerprint_sha256": mtls_fingerprint,
        }
    )
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
        _require_actor(actor, "machine_identity.manage", _MANAGE_ROLES)
        if not set(scopes) <= set(actor.permission_codes):
            raise ApiError("machine_identity.scope_denied")
        _verify_machine_catalog(connection)
        idempotency_sha256 = _command_idempotency(
            config, "service_account.create", actor.account_id, idempotency_key
        )
        replay = _command_replay(
            connection,
            workspace_id=actor.workspace_id,
            tenant_id=actor.tenant_id,
            subject_type="human",
            subject_id=actor.account_id,
            action_code="service_account.create",
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
        )
        if replay is not None:
            row = _service_account_row(
                connection,
                actor.workspace_id,
                actor.tenant_id,
                str(replay["resource_id"]),
            )
            result = ServiceAccountResponse(
                service_account=_service_account_summary(connection, row),
                request_id=_request_identifier(request_id),
                trace_id=_request_identifier(trace_id),
            )
            connection.commit()
            return result
        command_id = _id("mic_")
        service_account_id = _id("svc_")
        stamp = _db_time(_utcnow())
        connection.execute(
            """INSERT INTO service_accounts(
                 service_account_id,workspace_id,tenant_id,name,display_name,status,
                 scope_template,catalog_revision,constraint_sha256,constraint_generation,
                 ip_allowlist_json,mtls_fingerprint_sha256,token_ttl_seconds,revision,
                 created_by_account_id,updated_by_account_id,last_used_at,revoked_at,
                 created_at,updated_at)
               VALUES(?,?,?,?,?,'active',?,1,?,1,?,?,?,1,?,?,NULL,NULL,?,?)""",
            (
                service_account_id,
                actor.workspace_id,
                actor.tenant_id,
                name,
                display_name,
                payload.scope_template,
                constraint_sha256,
                _canonical_json(ip_allowlist),
                mtls_fingerprint,
                payload.token_ttl_seconds,
                actor.account_id,
                actor.account_id,
                stamp,
                stamp,
            ),
        )
        connection.executemany(
            "INSERT INTO service_account_scopes(service_account_id,permission_code,created_at) "
            "VALUES(?,?,?)",
            [(service_account_id, scope, stamp) for scope in scopes],
        )
        audit_id = _audit(
            connection,
            workspace_id=actor.workspace_id,
            tenant_id=actor.tenant_id,
            subject_type="human",
            subject_id=actor.account_id,
            action_code="service_account.created",
            resource_type="service_account",
            resource_id=service_account_id,
            outcome="success",
            permission_code="machine_identity.manage",
            resource_revision=1,
            request_id=request_id,
            trace_id=trace_id,
            stamp=stamp,
        )
        _outbox(
            connection,
            audit_id=audit_id,
            workspace_id=actor.workspace_id,
            tenant_id=actor.tenant_id,
            event_type="service_account.created",
            aggregate_id=service_account_id,
            aggregate_revision=1,
            payload={
                "service_account_id": service_account_id,
                "revision": 1,
                "status": "active",
                "constraint_generation": 1,
            },
            stamp=stamp,
        )
        _write_command(
            connection,
            command_id=command_id,
            workspace_id=actor.workspace_id,
            tenant_id=actor.tenant_id,
            subject_type="human",
            subject_id=actor.account_id,
            action_code="service_account.create",
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            resource_type="service_account",
            resource_id=service_account_id,
            result_revision=1,
            result_generation=0,
            request_id=request_id,
            trace_id=trace_id,
            stamp=stamp,
        )
        row = _service_account_row(
            connection,
            actor.workspace_id,
            actor.tenant_id,
            service_account_id,
        )
        result = ServiceAccountResponse(
            service_account=_service_account_summary(connection, row),
            request_id=_request_identifier(request_id),
            trace_id=_request_identifier(trace_id),
        )
        connection.commit()
        return result
    except sqlite3.IntegrityError as exc:
        _rollback(connection)
        if "service_accounts.workspace_id, service_accounts.name" in str(exc) or "UNIQUE" in str(
            exc
        ):
            raise ApiError("machine_identity.name_conflict") from None
        raise ApiError("machine_identity.request_invalid") from None
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("machine_identity.unavailable") from None
    finally:
        connection.close()


def create_oauth_client(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    idempotency_key: str,
    payload: OAuthClientCreateRequest,
    request_id: str,
    trace_id: str,
) -> OAuthClientCredentialResponse:
    _valid_idempotency(idempotency_key)
    _valid_service_account_id(payload.service_account_id)
    request_sha256 = _request_digest(
        {
            "service_account_id": payload.service_account_id,
            "audience": payload.audience,
        }
    )
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
        _require_actor(actor, "machine_identity.manage", _MANAGE_ROLES)
        account = _service_account_row(
            connection, actor.workspace_id, actor.tenant_id, payload.service_account_id
        )
        if account["status"] != "active":
            raise ApiError("machine_identity.conflict")
        scopes = _scope_codes(connection, payload.service_account_id)
        if not set(scopes) <= set(actor.permission_codes):
            raise ApiError("machine_identity.scope_denied")
        idempotency_sha256 = _command_idempotency(
            config, "oauth_client.create", actor.account_id, idempotency_key
        )
        replay = _command_replay(
            connection,
            workspace_id=actor.workspace_id,
            tenant_id=actor.tenant_id,
            subject_type="human",
            subject_id=actor.account_id,
            action_code="oauth_client.create",
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
        )
        if replay is not None:
            result = _credential_replay_response(
                connection,
                config,
                replay=replay,
                idempotency_key=idempotency_key,
                action_code="oauth_client.create",
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return result
        command_id = _id("mic_")
        oauth_client_id = _id("ocl_")
        client_id = _id("moc_")
        client_secret = _command_opaque(
            config, "oauth_client.create", idempotency_key, command_id, "mcs_"
        )
        stamp_time = _utcnow()
        stamp = _db_time(stamp_time)
        expires_at = _db_time(stamp_time + _CLIENT_SECRET_LIFETIME)
        credential_id = _id("occ_")
        connection.execute(
            """INSERT INTO oauth_clients(
                 oauth_client_id,client_id,service_account_id,workspace_id,tenant_id,
                 audience,status,credential_generation,revision,last_used_at,revoked_at,
                 created_at,updated_at)
               VALUES(?,?,?,?,?,?,'active',1,1,NULL,NULL,?,?)""",
            (
                oauth_client_id,
                client_id,
                payload.service_account_id,
                actor.workspace_id,
                actor.tenant_id,
                payload.audience,
                stamp,
                stamp,
            ),
        )
        connection.execute(
            """INSERT INTO client_credentials(
                 client_credential_id,oauth_client_id,workspace_id,tenant_id,generation,
                 secret_hash,secret_prefix,status,expires_at,grace_expires_at,last_used_at,
                 revoked_at,created_at,updated_at)
               VALUES(?,?,?,?,1,?,?,'active',?,NULL,NULL,NULL,?,?)""",
            (
                credential_id,
                oauth_client_id,
                actor.workspace_id,
                actor.tenant_id,
                _secret_digest(config, "client-secret", client_secret),
                client_secret[:12],
                expires_at,
                stamp,
                stamp,
            ),
        )
        _audit(
            connection,
            workspace_id=actor.workspace_id,
            tenant_id=actor.tenant_id,
            subject_type="human",
            subject_id=actor.account_id,
            action_code="oauth_client.created",
            resource_type="oauth_client",
            resource_id=oauth_client_id,
            outcome="success",
            permission_code="machine_identity.manage",
            resource_revision=1,
            request_id=request_id,
            trace_id=trace_id,
            stamp=stamp,
        )
        _write_command(
            connection,
            command_id=command_id,
            workspace_id=actor.workspace_id,
            tenant_id=actor.tenant_id,
            subject_type="human",
            subject_id=actor.account_id,
            action_code="oauth_client.create",
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            resource_type="oauth_client",
            resource_id=oauth_client_id,
            result_revision=1,
            result_generation=1,
            request_id=request_id,
            trace_id=trace_id,
            stamp=stamp,
        )
        account = _service_account_row(
            connection, actor.workspace_id, actor.tenant_id, payload.service_account_id
        )
        client = _oauth_client_row(connection, actor.workspace_id, actor.tenant_id, oauth_client_id)
        result = OAuthClientCredentialResponse(
            service_account=_service_account_summary(connection, account),
            oauth_client=_oauth_client_summary(connection, client),
            client_secret=client_secret,
            request_id=_request_identifier(request_id),
            trace_id=_request_identifier(trace_id),
        )
        connection.commit()
        return result
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("machine_identity.client_conflict") from None
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("machine_identity.unavailable") from None
    finally:
        connection.close()


def rotate_service_account_credential(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    service_account_id: str,
    expected_revision: int,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> OAuthClientCredentialResponse:
    _valid_idempotency(idempotency_key)
    _valid_service_account_id(service_account_id)
    if expected_revision < 1:
        raise ApiError("machine_identity.revision_conflict")
    request_sha256 = _request_digest(
        {
            "service_account_id": service_account_id,
            "expected_revision": expected_revision,
        }
    )
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
        _require_actor(actor, "machine_identity.manage", _MANAGE_ROLES)
        account = _service_account_row(
            connection, actor.workspace_id, actor.tenant_id, service_account_id
        )
        client = _oauth_client_for_account(
            connection,
            actor.workspace_id,
            actor.tenant_id,
            service_account_id,
        )
        idempotency_sha256 = _command_idempotency(
            config, "oauth_client.rotate", actor.account_id, idempotency_key
        )
        replay = _command_replay(
            connection,
            workspace_id=actor.workspace_id,
            tenant_id=actor.tenant_id,
            subject_type="human",
            subject_id=actor.account_id,
            action_code="oauth_client.rotate",
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
        )
        if replay is not None:
            result = _credential_replay_response(
                connection,
                config,
                replay=replay,
                idempotency_key=idempotency_key,
                action_code="oauth_client.rotate",
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return result
        if account["status"] != "active" or client["status"] != "active":
            raise ApiError("machine_identity.conflict")
        if int(client["revision"]) != expected_revision:
            raise ApiError("machine_identity.revision_conflict")
        command_id = _id("mic_")
        client_secret = _command_opaque(
            config, "oauth_client.rotate", idempotency_key, command_id, "mcs_"
        )
        now = _utcnow()
        stamp = _db_time(now)
        grace_expires_at = _db_time(now + _ROTATION_GRACE)
        expires_at = _db_time(now + _CLIENT_SECRET_LIFETIME)
        old_generation = int(client["credential_generation"])
        updated = connection.execute(
            """UPDATE client_credentials
                  SET status='grace',grace_expires_at=?,updated_at=?
                WHERE oauth_client_id=? AND generation=? AND status='active'""",
            (grace_expires_at, stamp, client["oauth_client_id"], old_generation),
        ).rowcount
        if updated != 1:
            raise ApiError("machine_identity.conflict")
        connection.execute(
            """UPDATE access_token_grants
                  SET expires_at=CASE WHEN expires_at>? THEN ? ELSE expires_at END,updated_at=?
                WHERE oauth_client_id=? AND credential_generation=? AND status='active'""",
            (
                grace_expires_at,
                grace_expires_at,
                stamp,
                client["oauth_client_id"],
                old_generation,
            ),
        )
        generation = old_generation + 1
        connection.execute(
            """INSERT INTO client_credentials(
                 client_credential_id,oauth_client_id,workspace_id,tenant_id,generation,
                 secret_hash,secret_prefix,status,expires_at,grace_expires_at,last_used_at,
                 revoked_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,'active',?,NULL,NULL,NULL,?,?)""",
            (
                _id("occ_"),
                client["oauth_client_id"],
                actor.workspace_id,
                actor.tenant_id,
                generation,
                _secret_digest(config, "client-secret", client_secret),
                client_secret[:12],
                expires_at,
                stamp,
                stamp,
            ),
        )
        revision = expected_revision + 1
        updated = connection.execute(
            """UPDATE oauth_clients
                  SET credential_generation=?,revision=?,updated_at=?
                WHERE oauth_client_id=? AND revision=? AND status='active'""",
            (generation, revision, stamp, client["oauth_client_id"], expected_revision),
        ).rowcount
        if updated != 1:
            raise ApiError("machine_identity.revision_conflict")
        audit_id = _audit(
            connection,
            workspace_id=actor.workspace_id,
            tenant_id=actor.tenant_id,
            subject_type="human",
            subject_id=actor.account_id,
            action_code="service_account.credential_rotated",
            resource_type="service_account",
            resource_id=service_account_id,
            outcome="success",
            permission_code="machine_identity.manage",
            resource_revision=revision,
            request_id=request_id,
            trace_id=trace_id,
            stamp=stamp,
        )
        _outbox(
            connection,
            audit_id=audit_id,
            workspace_id=actor.workspace_id,
            tenant_id=actor.tenant_id,
            event_type="service_account.credential_rotated",
            aggregate_id=service_account_id,
            aggregate_revision=revision,
            payload={
                "service_account_id": service_account_id,
                "credential_generation": generation,
                "revision": revision,
                "status": "active",
            },
            stamp=stamp,
        )
        _write_command(
            connection,
            command_id=command_id,
            workspace_id=actor.workspace_id,
            tenant_id=actor.tenant_id,
            subject_type="human",
            subject_id=actor.account_id,
            action_code="oauth_client.rotate",
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            resource_type="oauth_client",
            resource_id=str(client["oauth_client_id"]),
            result_revision=revision,
            result_generation=generation,
            request_id=request_id,
            trace_id=trace_id,
            stamp=stamp,
        )
        client = _oauth_client_for_account(
            connection, actor.workspace_id, actor.tenant_id, service_account_id
        )
        result = OAuthClientCredentialResponse(
            service_account=_service_account_summary(connection, account),
            oauth_client=_oauth_client_summary(connection, client),
            client_secret=client_secret,
            request_id=_request_identifier(request_id),
            trace_id=_request_identifier(trace_id),
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("machine_identity.unavailable") from None
    finally:
        connection.close()


def revoke_service_account(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    service_account_id: str,
    expected_revision: int,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> ServiceAccountResponse:
    _valid_idempotency(idempotency_key)
    _valid_service_account_id(service_account_id)
    if expected_revision < 1:
        raise ApiError("machine_identity.revision_conflict")
    request_sha256 = _request_digest(
        {
            "service_account_id": service_account_id,
            "expected_revision": expected_revision,
        }
    )
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
        _require_actor(actor, "machine_identity.manage", _MANAGE_ROLES)
        account = _service_account_row(
            connection, actor.workspace_id, actor.tenant_id, service_account_id
        )
        idempotency_sha256 = _command_idempotency(
            config, "service_account.revoke", actor.account_id, idempotency_key
        )
        replay = _command_replay(
            connection,
            workspace_id=actor.workspace_id,
            tenant_id=actor.tenant_id,
            subject_type="human",
            subject_id=actor.account_id,
            action_code="service_account.revoke",
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
        )
        if replay is not None:
            if (
                account["status"] != "revoked"
                or int(account["revision"]) != int(replay["result_revision"])
            ):
                raise ApiError("machine_identity.conflict")
            result = ServiceAccountResponse(
                service_account=_service_account_summary(connection, account),
                request_id=_request_identifier(request_id),
                trace_id=_request_identifier(trace_id),
            )
            connection.commit()
            return result
        if int(account["revision"]) != expected_revision:
            raise ApiError("machine_identity.revision_conflict")
        if account["status"] == "revoked":
            raise ApiError("machine_identity.conflict")
        command_id = _id("mic_")
        stamp = _db_time(_utcnow())
        next_revision = expected_revision + 1
        updated = connection.execute(
            """UPDATE service_accounts
                  SET status='revoked',constraint_generation=constraint_generation+1,
                      revision=?,updated_by_account_id=?,revoked_at=?,updated_at=?
                WHERE workspace_id=? AND tenant_id=? AND service_account_id=?
                  AND revision=? AND status IN('active','disabled')""",
            (
                next_revision,
                actor.account_id,
                stamp,
                stamp,
                actor.workspace_id,
                actor.tenant_id,
                service_account_id,
                expected_revision,
            ),
        ).rowcount
        if updated != 1:
            raise ApiError("machine_identity.revision_conflict")
        client = connection.execute(
            "SELECT * FROM oauth_clients WHERE service_account_id=?",
            (service_account_id,),
        ).fetchone()
        if client is not None:
            connection.execute(
                """UPDATE oauth_clients
                      SET status='revoked',revision=revision+1,revoked_at=?,updated_at=?
                    WHERE oauth_client_id=? AND status!='revoked'""",
                (stamp, stamp, client["oauth_client_id"]),
            )
            connection.execute(
                """UPDATE client_credentials
                      SET status='revoked',revoked_at=?,updated_at=?
                    WHERE oauth_client_id=? AND status!='revoked'""",
                (stamp, stamp, client["oauth_client_id"]),
            )
            connection.execute(
                """UPDATE access_token_grants
                      SET status='revoked',revoked_at=?,updated_at=?
                    WHERE oauth_client_id=? AND status='active'""",
                (stamp, stamp, client["oauth_client_id"]),
            )
        audit_id = _audit(
            connection,
            workspace_id=actor.workspace_id,
            tenant_id=actor.tenant_id,
            subject_type="human",
            subject_id=actor.account_id,
            action_code="service_account.revoked",
            resource_type="service_account",
            resource_id=service_account_id,
            outcome="success",
            permission_code="machine_identity.manage",
            resource_revision=next_revision,
            request_id=request_id,
            trace_id=trace_id,
            stamp=stamp,
        )
        _outbox(
            connection,
            audit_id=audit_id,
            workspace_id=actor.workspace_id,
            tenant_id=actor.tenant_id,
            event_type="service_account.revoked",
            aggregate_id=service_account_id,
            aggregate_revision=next_revision,
            payload={
                "service_account_id": service_account_id,
                "revision": next_revision,
                "status": "revoked",
            },
            stamp=stamp,
        )
        _write_command(
            connection,
            command_id=command_id,
            workspace_id=actor.workspace_id,
            tenant_id=actor.tenant_id,
            subject_type="human",
            subject_id=actor.account_id,
            action_code="service_account.revoke",
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            resource_type="service_account",
            resource_id=service_account_id,
            result_revision=next_revision,
            result_generation=0,
            request_id=request_id,
            trace_id=trace_id,
            stamp=stamp,
        )
        account = _service_account_row(
            connection, actor.workspace_id, actor.tenant_id, service_account_id
        )
        result = ServiceAccountResponse(
            service_account=_service_account_summary(connection, account),
            request_id=_request_identifier(request_id),
            trace_id=_request_identifier(trace_id),
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("machine_identity.unavailable") from None
    finally:
        connection.close()


def issue_access_token(
    config: AppConfig,
    *,
    client_id: str,
    client_secret: str,
    idempotency_key: str,
    audience: str,
    scopes: tuple[str, ...],
    peer_ip: str | None,
    verified_mtls_fingerprint: str | None,
    request_id: str,
    trace_id: str,
) -> OAuthTokenResponse:
    _valid_idempotency(idempotency_key, error_code="oauth.invalid_request")
    if _CLIENT_ID_RE.fullmatch(client_id) is None or _SECRET_RE.fullmatch(client_secret) is None:
        _uniform_client_failure(config, client_id)
    if audience != _AUDIENCE:
        _uniform_client_failure(config, client_id)
    requested_scopes = _token_scopes(scopes)
    request_sha256 = _request_digest(
        {
            "client_id": client_id,
            "audience": audience,
            "scopes": requested_scopes,
        }
    )
    now = _utcnow()
    stamp = _db_time(now)
    client_key = _secret_digest(config, "client-key", client_id)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_token_guard(connection, client_key, now)
        rows = connection.execute(
            """SELECT client.*,account.name account_name,account.status account_status,
                      account.scope_template,account.catalog_revision,
                      account.constraint_sha256,account.constraint_generation,
                      account.ip_allowlist_json,account.mtls_fingerprint_sha256,
                      account.token_ttl_seconds,account.created_by_account_id,
                      account.revision account_revision,
                      credential.client_credential_id,credential.generation,
                      credential.secret_hash,credential.status credential_status,
                      credential.expires_at credential_expires_at,
                      credential.grace_expires_at
                 FROM oauth_clients client
                 JOIN service_accounts account
                   ON account.service_account_id=client.service_account_id
                 JOIN client_credentials credential
                   ON credential.oauth_client_id=client.oauth_client_id
                WHERE client.client_id=? AND credential.status IN('active','grace')
                ORDER BY credential.generation DESC LIMIT 2""",
            (client_id,),
        ).fetchall()
        supplied_digest = _secret_digest(config, "client-secret", client_secret)
        matches = [
            row
            for row in rows
            if hmac.compare_digest(str(row["secret_hash"]), supplied_digest)
        ]
        if len(matches) != 1:
            _record_token_failure(connection, client_key, now)
            connection.commit()
            raise ApiError("oauth.invalid_client")
        row = matches[0]
        if (
            row["account_status"] != "active"
            or row["status"] != "active"
            or _expired(row["credential_expires_at"], now)
            or (
                row["credential_status"] == "grace"
                and (
                    row["grace_expires_at"] is None
                    or _expired(row["grace_expires_at"], now)
                )
            )
        ):
            _record_token_failure(connection, client_key, now)
            connection.commit()
            raise ApiError("oauth.invalid_client")
        _verify_machine_catalog(connection)
        account_scopes = _scope_codes(connection, str(row["service_account_id"]))
        if not set(requested_scopes) <= set(account_scopes):
            _record_token_failure(connection, client_key, now)
            connection.commit()
            raise ApiError("oauth.invalid_scope")
        try:
            operator = workspace_actor_permissions_in_transaction(
                connection,
                account_id=str(row["created_by_account_id"]),
                workspace_id=str(row["workspace_id"]),
                tenant_id=str(row["tenant_id"]),
            )
        except ApiError:
            _record_token_failure(connection, client_key, now)
            connection.commit()
            raise ApiError("oauth.invalid_client") from None
        if not set(requested_scopes) <= set(operator.permission_codes):
            _record_token_failure(connection, client_key, now)
            connection.commit()
            raise ApiError("oauth.invalid_client")
        if not _constraints_match(
            row,
            peer_ip=peer_ip,
            verified_mtls_fingerprint=verified_mtls_fingerprint,
        ):
            _record_token_failure(connection, client_key, now)
            connection.commit()
            raise ApiError("oauth.invalid_client")
        idempotency_sha256 = _command_idempotency(
            config,
            "oauth.token.issue",
            str(row["service_account_id"]),
            idempotency_key,
        )
        replay = _command_replay(
            connection,
            workspace_id=str(row["workspace_id"]),
            tenant_id=str(row["tenant_id"]),
            subject_type="service_account",
            subject_id=str(row["service_account_id"]),
            action_code="oauth.token.issue",
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            conflict_code="oauth.invalid_request",
        )
        if replay is not None:
            result = _token_replay_response(
                connection,
                config,
                replay=replay,
                source=row,
                operator=operator,
                requested_scopes=requested_scopes,
                idempotency_key=idempotency_key,
                now=now,
            )
            connection.commit()
            return result
        credential_deadline = _parse_time(
            row["grace_expires_at"]
            if row["credential_status"] == "grace"
            else row["credential_expires_at"]
        )
        expires_at = min(
            now + timedelta(seconds=int(row["token_ttl_seconds"])), credential_deadline
        )
        expires_in = int((expires_at - now).total_seconds())
        if expires_in < 1:
            _record_token_failure(connection, client_key, now)
            connection.commit()
            raise ApiError("oauth.invalid_client")
        command_id = _id("mic_")
        access_token = _command_opaque(
            config, "oauth.token.issue", idempotency_key, command_id, "mat_"
        )
        grant_id = _id("atg_")
        connection.execute(
            """INSERT INTO access_token_grants(
                 access_token_grant_id,token_hash,service_account_id,oauth_client_id,
                 workspace_id,tenant_id,scopes_json,audience,operator_permission_revision,
                 operator_authorization_fingerprint,catalog_revision,credential_generation,
                 constraint_generation,status,issued_at,expires_at,last_used_at,revoked_at,
                 created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?,'active',?,?,NULL,NULL,?,?)""",
            (
                grant_id,
                _secret_digest(config, "access-token", access_token),
                row["service_account_id"],
                row["oauth_client_id"],
                row["workspace_id"],
                row["tenant_id"],
                _canonical_json(requested_scopes),
                audience,
                operator.permission_revision,
                operator.authorization_fingerprint,
                row["generation"],
                row["constraint_generation"],
                stamp,
                _db_time(expires_at),
                stamp,
                stamp,
            ),
        )
        connection.execute(
            "UPDATE client_credentials SET last_used_at=?,updated_at=? "
            "WHERE client_credential_id=?",
            (stamp, stamp, row["client_credential_id"]),
        )
        connection.execute(
            "UPDATE oauth_clients SET last_used_at=?,updated_at=? WHERE oauth_client_id=?",
            (stamp, stamp, row["oauth_client_id"]),
        )
        connection.execute(
            "UPDATE service_accounts SET last_used_at=?,updated_at=? WHERE service_account_id=?",
            (stamp, stamp, row["service_account_id"]),
        )
        _clear_token_guard(connection, client_key)
        _audit(
            connection,
            workspace_id=str(row["workspace_id"]),
            tenant_id=str(row["tenant_id"]),
            subject_type="service_account",
            subject_id=str(row["service_account_id"]),
            action_code="oauth.token_issued",
            resource_type="access_token_grant",
            resource_id=grant_id,
            outcome="success",
            permission_code=requested_scopes[0],
            resource_revision=1,
            request_id=request_id,
            trace_id=trace_id,
            stamp=stamp,
        )
        _write_command(
            connection,
            command_id=command_id,
            workspace_id=str(row["workspace_id"]),
            tenant_id=str(row["tenant_id"]),
            subject_type="service_account",
            subject_id=str(row["service_account_id"]),
            action_code="oauth.token.issue",
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            resource_type="access_token_grant",
            resource_id=grant_id,
            result_revision=1,
            result_generation=int(row["generation"]),
            request_id=request_id,
            trace_id=trace_id,
            stamp=stamp,
        )
        connection.commit()
        return OAuthTokenResponse(
            access_token=access_token,
            expires_in=expires_in,
            scope=" ".join(requested_scopes),
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("oauth.temporarily_unavailable") from None
    finally:
        connection.close()


def authenticate_machine_request(
    config: AppConfig,
    *,
    access_token: str,
    method: str,
    path: str,
    peer_ip: str | None,
    verified_mtls_fingerprint: str | None,
    request_id: str,
    trace_id: str,
) -> MachineAuthContext:
    route = _MACHINE_ROUTES.get((method, path))
    if route is None or _TOKEN_RE.fullmatch(access_token) is None:
        raise ApiError("machine_auth.invalid")
    required_permission, action_code, resource_type = route
    token_hash = _secret_digest(config, "access-token", access_token)
    now = _utcnow()
    stamp = _db_time(now)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT grant.*,account.status account_status,
                      account.catalog_revision account_catalog_revision,
                      account.constraint_sha256,
                      account.constraint_generation account_constraint_generation,
                      account.ip_allowlist_json,account.mtls_fingerprint_sha256,
                      account.created_by_account_id,account.revision account_revision,
                      account.last_used_at account_last_used_at,
                      client.status client_status,
                      client.credential_generation current_credential_generation,
                      client.last_used_at client_last_used_at,
                      credential.status credential_status,
                      credential.expires_at credential_expires_at,
                      credential.grace_expires_at,
                      credential.last_used_at credential_last_used_at
                 FROM access_token_grants grant
                 JOIN service_accounts account
                   ON account.service_account_id=grant.service_account_id
                 JOIN oauth_clients client ON client.oauth_client_id=grant.oauth_client_id
                 JOIN client_credentials credential
                   ON credential.oauth_client_id=grant.oauth_client_id
                  AND credential.generation=grant.credential_generation
                WHERE grant.token_hash=?""",
            (token_hash,),
        ).fetchone()
        if row is None:
            raise ApiError("machine_auth.invalid")

        def denied() -> Never:
            _audit(
                connection,
                workspace_id=str(row["workspace_id"]),
                tenant_id=str(row["tenant_id"]),
                subject_type="service_account",
                subject_id=str(row["service_account_id"]),
                action_code=action_code,
                resource_type=resource_type,
                resource_id=path,
                outcome="denied",
                permission_code=required_permission,
                resource_revision=int(row["account_revision"]),
                request_id=request_id,
                trace_id=trace_id,
                stamp=stamp,
            )
            connection.commit()
            raise ApiError("machine_auth.invalid")

        if (
            row["status"] != "active"
            or row["account_status"] != "active"
            or row["client_status"] != "active"
            or _expired(row["expires_at"], now)
            or _expired(row["credential_expires_at"], now)
            or int(row["catalog_revision"]) != 1
            or int(row["account_catalog_revision"]) != 1
            or int(row["constraint_generation"]) != int(row["account_constraint_generation"])
            or (
                row["credential_status"] == "grace"
                and (
                    row["grace_expires_at"] is None
                    or _expired(row["grace_expires_at"], now)
                )
            )
            or row["credential_status"] not in {"active", "grace"}
        ):
            denied()
        _verify_machine_catalog(connection)
        scopes = _json_string_tuple(row["scopes_json"])
        account_scopes = _scope_codes(connection, str(row["service_account_id"]))
        if (
            required_permission not in scopes
            or not set(scopes) <= set(account_scopes)
            or row["audience"] != _AUDIENCE
        ):
            denied()
        try:
            operator = workspace_actor_authorization_probe_in_transaction(
                connection,
                account_id=str(row["created_by_account_id"]),
                workspace_id=str(row["workspace_id"]),
                tenant_id=str(row["tenant_id"]),
                permission_codes=scopes,
            )
        except ApiError:
            denied()
        if (
            operator.authorization_fingerprint != row["operator_authorization_fingerprint"]
            or operator.permission_revision != int(row["operator_permission_revision"])
            or not set(scopes) <= set(operator.permission_codes)
            or not _constraints_match(
                row,
                peer_ip=peer_ip,
                verified_mtls_fingerprint=verified_mtls_fingerprint,
            )
        ):
            denied()
        fingerprint = hashlib.sha256(
            _canonical_json(
                {
                    "grant_id": row["access_token_grant_id"],
                    "service_account_id": row["service_account_id"],
                    "workspace_id": row["workspace_id"],
                    "scopes": scopes,
                    "operator": operator.authorization_fingerprint,
                    "credential_generation": row["credential_generation"],
                    "constraint_generation": row["constraint_generation"],
                }
            ).encode("utf-8")
        ).hexdigest()
        if _last_used_touch_required(row["last_used_at"], now):
            connection.execute(
                "UPDATE access_token_grants SET last_used_at=?,updated_at=? "
                "WHERE access_token_grant_id=?",
                (stamp, stamp, row["access_token_grant_id"]),
            )
        if _last_used_touch_required(row["credential_last_used_at"], now):
            connection.execute(
                "UPDATE client_credentials SET last_used_at=?,updated_at=? "
                "WHERE oauth_client_id=? AND generation=?",
                (stamp, stamp, row["oauth_client_id"], row["credential_generation"]),
            )
        if _last_used_touch_required(row["client_last_used_at"], now):
            connection.execute(
                "UPDATE oauth_clients SET last_used_at=?,updated_at=? WHERE oauth_client_id=?",
                (stamp, stamp, row["oauth_client_id"]),
            )
        if _last_used_touch_required(row["account_last_used_at"], now):
            connection.execute(
                "UPDATE service_accounts SET last_used_at=?,updated_at=? "
                "WHERE service_account_id=?",
                (stamp, stamp, row["service_account_id"]),
            )
        _audit(
            connection,
            workspace_id=str(row["workspace_id"]),
            tenant_id=str(row["tenant_id"]),
            subject_type="service_account",
            subject_id=str(row["service_account_id"]),
            action_code=action_code,
            resource_type=resource_type,
            resource_id=path,
            outcome="success",
            permission_code=required_permission,
            resource_revision=int(row["account_revision"]),
            request_id=request_id,
            trace_id=trace_id,
            stamp=stamp,
        )
        context = MachineAuthContext(
            subject_type="service_account",
            service_account_id=str(row["service_account_id"]),
            oauth_client_id=str(row["oauth_client_id"]),
            access_token_grant_id=str(row["access_token_grant_id"]),
            workspace_id=str(row["workspace_id"]),
            tenant_id=str(row["tenant_id"]),
            audience=str(row["audience"]),
            permission_codes=scopes,
            permission_revision=int(row["operator_permission_revision"]),
            authorization_fingerprint=fingerprint,
            expires_at=_parse_time(row["expires_at"]),
        )
        connection.commit()
        return context
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("machine_auth.unavailable") from None
    finally:
        connection.close()


def _require_actor(
    actor: WorkspaceSessionContext,
    permission_code: str,
    roles: frozenset[str],
) -> None:
    if actor.membership_role not in roles or permission_code not in actor.permission_codes:
        raise ApiError("workspace_session.permission_denied")


def _verify_machine_catalog(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "SELECT permission_code,scope_template,http_method,route_template,catalog_revision,status "
        "FROM machine_permission_catalog ORDER BY permission_code"
    ).fetchall()
    actual = {
        (
            str(row["permission_code"]),
            str(row["scope_template"]),
            str(row["http_method"]),
            str(row["route_template"]),
            int(row["catalog_revision"]),
            str(row["status"]),
        )
        for row in rows
    }
    expected = {
        ("provider.read", "provider_reader", "GET", "/providers", 1, "active"),
        (
            "request_log.read",
            "observability_reader",
            "GET",
            "/request-logs",
            1,
            "active",
        ),
    }
    if actual != expected:
        raise ApiError("machine_identity.unavailable")


def _service_account_row(
    connection: sqlite3.Connection,
    workspace_id: str,
    tenant_id: str,
    service_account_id: str,
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM service_accounts WHERE workspace_id=? AND tenant_id=? "
        "AND service_account_id=?",
        (workspace_id, tenant_id, service_account_id),
    ).fetchone()
    if row is None:
        raise ApiError("machine_identity.not_found")
    return row


def _oauth_client_row(
    connection: sqlite3.Connection,
    workspace_id: str,
    tenant_id: str,
    oauth_client_id: str,
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM oauth_clients WHERE workspace_id=? AND tenant_id=? AND oauth_client_id=?",
        (workspace_id, tenant_id, oauth_client_id),
    ).fetchone()
    if row is None:
        raise ApiError("machine_identity.not_found")
    return row


def _oauth_client_for_account(
    connection: sqlite3.Connection,
    workspace_id: str,
    tenant_id: str,
    service_account_id: str,
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM oauth_clients WHERE workspace_id=? AND tenant_id=? AND service_account_id=?",
        (workspace_id, tenant_id, service_account_id),
    ).fetchone()
    if row is None:
        raise ApiError("machine_identity.client_not_found")
    return row


def _service_account_summary(
    connection: sqlite3.Connection, row: sqlite3.Row
) -> ServiceAccountSummary:
    client = connection.execute(
        "SELECT * FROM oauth_clients WHERE service_account_id=?",
        (row["service_account_id"],),
    ).fetchone()
    return ServiceAccountSummary(
        service_account_id=str(row["service_account_id"]),
        workspace_id=str(row["workspace_id"]),
        name=str(row["name"]),
        display_name=str(row["display_name"]),
        status=str(row["status"]),
        scope_template=str(row["scope_template"]),
        scopes=list(_scope_codes(connection, str(row["service_account_id"]))),
        token_ttl_seconds=int(row["token_ttl_seconds"]),
        ip_allowlist=list(_json_string_tuple(row["ip_allowlist_json"])),
        mtls_required=row["mtls_fingerprint_sha256"] is not None,
        constraint_generation=int(row["constraint_generation"]),
        revision=int(row["revision"]),
        last_used_at=(
            _parse_time(row["last_used_at"]) if row["last_used_at"] is not None else None
        ),
        revoked_at=(
            _parse_time(row["revoked_at"]) if row["revoked_at"] is not None else None
        ),
        created_at=_parse_time(row["created_at"]),
        updated_at=_parse_time(row["updated_at"]),
        oauth_client=_oauth_client_summary(connection, client) if client is not None else None,
    )


def _oauth_client_summary(
    connection: sqlite3.Connection, row: sqlite3.Row
) -> OAuthClientSummary:
    credential = connection.execute(
        "SELECT * FROM client_credentials WHERE oauth_client_id=? "
        "ORDER BY generation DESC LIMIT 1",
        (row["oauth_client_id"],),
    ).fetchone()
    return OAuthClientSummary(
        oauth_client_id=str(row["oauth_client_id"]),
        client_id=str(row["client_id"]),
        audience=str(row["audience"]),
        status=str(row["status"]),
        credential_generation=int(row["credential_generation"]),
        revision=int(row["revision"]),
        credential_prefix=(str(credential["secret_prefix"]) if credential is not None else None),
        credential_expires_at=(
            _parse_time(credential["expires_at"]) if credential is not None else None
        ),
        last_used_at=(
            _parse_time(row["last_used_at"]) if row["last_used_at"] is not None else None
        ),
        created_at=_parse_time(row["created_at"]),
        updated_at=_parse_time(row["updated_at"]),
    )


def _scope_codes(connection: sqlite3.Connection, service_account_id: str) -> tuple[str, ...]:
    values = tuple(
        str(row[0])
        for row in connection.execute(
            "SELECT permission_code FROM service_account_scopes "
            "WHERE service_account_id=? ORDER BY permission_code",
            (service_account_id,),
        ).fetchall()
    )
    if not values:
        raise ApiError("machine_identity.unavailable")
    return values


def _credential_replay_response(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    replay: sqlite3.Row,
    idempotency_key: str,
    action_code: str,
    request_id: str,
    trace_id: str,
) -> OAuthClientCredentialResponse:
    client = _oauth_client_row(
        connection,
        str(replay["workspace_id"]),
        str(replay["tenant_id"]),
        str(replay["resource_id"]),
    )
    generation = int(replay["result_generation"])
    if (
        client["status"] != "active"
        or int(client["revision"]) != int(replay["result_revision"])
        or int(client["credential_generation"]) != generation
    ):
        raise ApiError("machine_identity.conflict")
    credential = connection.execute(
        "SELECT * FROM client_credentials WHERE oauth_client_id=? AND generation=?",
        (client["oauth_client_id"], generation),
    ).fetchone()
    if credential is None or credential["status"] != "active":
        raise ApiError("machine_identity.conflict")
    client_secret = _command_opaque(
        config,
        action_code,
        idempotency_key,
        str(replay["machine_identity_command_id"]),
        "mcs_",
    )
    if not hmac.compare_digest(
        str(credential["secret_hash"]),
        _secret_digest(config, "client-secret", client_secret),
    ):
        raise ApiError("machine_identity.unavailable")
    account = _service_account_row(
        connection,
        str(replay["workspace_id"]),
        str(replay["tenant_id"]),
        str(client["service_account_id"]),
    )
    return OAuthClientCredentialResponse(
        service_account=_service_account_summary(connection, account),
        oauth_client=_oauth_client_summary(connection, client),
        client_secret=client_secret,
        request_id=_request_identifier(request_id),
        trace_id=_request_identifier(trace_id),
    )


def _token_replay_response(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    replay: sqlite3.Row,
    source: sqlite3.Row,
    operator: object,
    requested_scopes: tuple[str, ...],
    idempotency_key: str,
    now: datetime,
) -> OAuthTokenResponse:
    grant = connection.execute(
        "SELECT * FROM access_token_grants WHERE workspace_id=? AND tenant_id=? "
        "AND access_token_grant_id=?",
        (replay["workspace_id"], replay["tenant_id"], replay["resource_id"]),
    ).fetchone()
    if (
        grant is None
        or grant["status"] != "active"
        or _expired(grant["expires_at"], now)
        or str(grant["service_account_id"]) != str(source["service_account_id"])
        or str(grant["oauth_client_id"]) != str(source["oauth_client_id"])
        or int(grant["credential_generation"]) != int(source["generation"])
        or int(grant["constraint_generation"]) != int(source["constraint_generation"])
        or int(grant["operator_permission_revision"]) != int(operator.permission_revision)
        or not hmac.compare_digest(
            str(grant["operator_authorization_fingerprint"]),
            str(operator.authorization_fingerprint),
        )
        or _json_string_tuple(grant["scopes_json"]) != requested_scopes
        or grant["audience"] != _AUDIENCE
        or int(replay["result_revision"]) != 1
        or int(replay["result_generation"]) != int(source["generation"])
    ):
        raise ApiError("oauth.invalid_request")
    access_token = _command_opaque(
        config,
        "oauth.token.issue",
        idempotency_key,
        str(replay["machine_identity_command_id"]),
        "mat_",
    )
    if not hmac.compare_digest(
        str(grant["token_hash"]),
        _secret_digest(config, "access-token", access_token),
    ):
        raise ApiError("oauth.temporarily_unavailable")
    expires_in = int((_parse_time(grant["expires_at"]) - now).total_seconds())
    if expires_in < 1:
        raise ApiError("oauth.invalid_request")
    return OAuthTokenResponse(
        access_token=access_token,
        expires_in=expires_in,
        scope=" ".join(requested_scopes),
    )


def _command_replay(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    tenant_id: str,
    subject_type: str,
    subject_id: str,
    action_code: str,
    idempotency_sha256: str,
    request_sha256: str,
    conflict_code: str = "machine_identity.idempotency_conflict",
) -> sqlite3.Row | None:
    row = connection.execute(
        "SELECT * FROM machine_identity_commands WHERE workspace_id=? AND tenant_id=? "
        "AND subject_type=? AND subject_id=? AND action_code=? AND idempotency_sha256=?",
        (
            workspace_id,
            tenant_id,
            subject_type,
            subject_id,
            action_code,
            idempotency_sha256,
        ),
    ).fetchone()
    if row is not None and not hmac.compare_digest(str(row["request_sha256"]), request_sha256):
        raise ApiError(conflict_code)
    return row


def _write_command(
    connection: sqlite3.Connection,
    *,
    command_id: str,
    workspace_id: str,
    tenant_id: str,
    subject_type: str,
    subject_id: str,
    action_code: str,
    idempotency_sha256: str,
    request_sha256: str,
    resource_type: str,
    resource_id: str,
    result_revision: int,
    result_generation: int,
    request_id: str,
    trace_id: str,
    stamp: str,
) -> None:
    connection.execute(
        """INSERT INTO machine_identity_commands(
             machine_identity_command_id,workspace_id,tenant_id,subject_type,subject_id,
             action_code,idempotency_sha256,request_sha256,resource_type,resource_id,
             result_revision,result_generation,request_id,trace_id,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            command_id,
            workspace_id,
            tenant_id,
            subject_type,
            subject_id,
            action_code,
            idempotency_sha256,
            request_sha256,
            resource_type,
            resource_id,
            result_revision,
            result_generation,
            _request_identifier(request_id),
            _request_identifier(trace_id),
            stamp,
        ),
    )


def _audit(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    tenant_id: str,
    subject_type: str,
    subject_id: str,
    action_code: str,
    resource_type: str,
    resource_id: str,
    outcome: str,
    permission_code: str | None,
    resource_revision: int,
    request_id: str,
    trace_id: str,
    stamp: str,
) -> str:
    audit_id = _id("mia_")
    connection.execute(
        """INSERT INTO machine_identity_audits(
             machine_identity_audit_id,workspace_id,tenant_id,subject_type,subject_id,
             action_code,resource_type,resource_id,outcome,permission_code,
             resource_revision,request_id,trace_id,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            audit_id,
            workspace_id,
            tenant_id,
            subject_type,
            subject_id,
            action_code,
            resource_type,
            resource_id,
            outcome,
            permission_code,
            resource_revision,
            _request_identifier(request_id),
            _request_identifier(trace_id),
            stamp,
        ),
    )
    return audit_id


def _outbox(
    connection: sqlite3.Connection,
    *,
    audit_id: str,
    workspace_id: str,
    tenant_id: str,
    event_type: str,
    aggregate_id: str,
    aggregate_revision: int,
    payload: dict[str, object],
    stamp: str,
) -> None:
    connection.execute(
        """INSERT INTO machine_identity_event_outbox(
             machine_identity_outbox_id,machine_identity_audit_id,workspace_id,tenant_id,
             event_type,aggregate_id,aggregate_revision,payload_json,status,attempt_count,
             next_attempt_at,lease_owner_id,lease_token,lease_expires_at,delivered_at,
             created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,'pending',0,?,NULL,NULL,NULL,NULL,?,?)""",
        (
            _id("mio_"),
            audit_id,
            workspace_id,
            tenant_id,
            event_type,
            aggregate_id,
            aggregate_revision,
            _canonical_json(payload),
            stamp,
            stamp,
            stamp,
        ),
    )


def _require_token_guard(
    connection: sqlite3.Connection, client_key: str, now: datetime
) -> None:
    row = connection.execute(
        "SELECT blocked_until FROM oauth_token_attempt_guards WHERE client_key_sha256=?",
        (client_key,),
    ).fetchone()
    if row is not None and row["blocked_until"] is not None and not _expired(
        row["blocked_until"], now
    ):
        raise ApiError("oauth.rate_limited")


def _record_token_failure(
    connection: sqlite3.Connection, client_key: str, now: datetime
) -> None:
    row = connection.execute(
        "SELECT * FROM oauth_token_attempt_guards WHERE client_key_sha256=?",
        (client_key,),
    ).fetchone()
    stamp = _db_time(now)
    if row is None or now - _parse_time(row["window_started_at"]) >= _TOKEN_FAILURE_WINDOW:
        count = 1
        window = stamp
    else:
        count = min(20, int(row["failure_count"]) + 1)
        window = str(row["window_started_at"])
    blocked_until = _db_time(now + _TOKEN_FAILURE_LOCK) if count >= _TOKEN_FAILURE_LIMIT else None
    connection.execute(
        """INSERT INTO oauth_token_attempt_guards(
             client_key_sha256,failure_count,window_started_at,blocked_until,updated_at)
           VALUES(?,?,?,?,?)
           ON CONFLICT(client_key_sha256) DO UPDATE SET
             failure_count=excluded.failure_count,
             window_started_at=excluded.window_started_at,
             blocked_until=excluded.blocked_until,
             updated_at=excluded.updated_at""",
        (client_key, count, window, blocked_until, stamp),
    )


def _clear_token_guard(connection: sqlite3.Connection, client_key: str) -> None:
    connection.execute(
        "DELETE FROM oauth_token_attempt_guards WHERE client_key_sha256=?", (client_key,)
    )


def _uniform_client_failure(config: AppConfig, client_id: str) -> None:
    # A bounded digest keeps malformed identifiers out of logs and durable storage.
    hashlib.sha256(
        (config.auth_token_secret + "\x00invalid-client\x00" + client_id[:128]).encode("utf-8")
    ).digest()
    raise ApiError("oauth.invalid_client")


def _constraints_match(
    row: sqlite3.Row,
    *,
    peer_ip: str | None,
    verified_mtls_fingerprint: str | None,
) -> bool:
    try:
        networks = _json_string_tuple(row["ip_allowlist_json"])
        if networks:
            if peer_ip is None:
                return False
            address = ipaddress.ip_address(peer_ip)
            if not any(address in ipaddress.ip_network(network) for network in networks):
                return False
        expected_mtls = row["mtls_fingerprint_sha256"]
        if expected_mtls is not None and (
            verified_mtls_fingerprint is None
            or not hmac.compare_digest(str(expected_mtls), verified_mtls_fingerprint)
        ):
            return False
        return _constraint_digest(networks, expected_mtls) == row["constraint_sha256"]
    except (ValueError, TypeError, json.JSONDecodeError):
        return False


def _normalized_name(value: str) -> str:
    normalized = value.strip().lower()
    if _NAME_RE.fullmatch(normalized) is None:
        raise ApiError("machine_identity.request_invalid")
    return normalized


def _display_name(value: str) -> str:
    normalized = value.strip()
    if (
        not 2 <= len(normalized) <= 120
        or "\x00" in normalized
        or any(ord(character) < 32 for character in normalized)
    ):
        raise ApiError("machine_identity.request_invalid")
    return normalized


def _normalized_scopes(template: str, values: list[str]) -> tuple[str, ...]:
    expected = _TEMPLATE_SCOPES.get(template)
    if expected is None or any(type(value) is not str for value in values):
        raise ApiError("machine_identity.request_invalid")
    normalized = tuple(sorted(values))
    if (
        len(normalized) != len(set(normalized))
        or not normalized
        or not set(normalized) <= set(expected)
    ):
        raise ApiError("machine_identity.request_invalid")
    return normalized


def _token_scopes(values: tuple[str, ...]) -> tuple[str, ...]:
    if not values or len(values) > 8 or len(values) != len(set(values)):
        raise ApiError("oauth.invalid_scope")
    normalized = tuple(sorted(values))
    if any(value not in {"provider.read", "request_log.read"} for value in normalized):
        raise ApiError("oauth.invalid_scope")
    return normalized


def _normalized_networks(values: list[str]) -> tuple[str, ...]:
    if len(values) > 16:
        raise ApiError("machine_identity.request_invalid")
    try:
        normalized = tuple(
            sorted(str(ipaddress.ip_network(value, strict=False)) for value in values)
        )
    except (TypeError, ValueError):
        raise ApiError("machine_identity.request_invalid") from None
    if len(normalized) != len(set(normalized)):
        raise ApiError("machine_identity.request_invalid")
    return normalized


def _normalized_mtls(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if _HEX_64_RE.fullmatch(normalized) is None:
        raise ApiError("machine_identity.request_invalid")
    return normalized


def _constraint_digest(networks: tuple[str, ...], mtls_fingerprint: object) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "ip_allowlist": networks,
                "mtls_fingerprint_sha256": mtls_fingerprint,
            }
        ).encode("utf-8")
    ).hexdigest()


def _secret_digest(config: AppConfig, domain: str, value: str) -> str:
    return hmac.new(
        config.auth_token_secret.encode("utf-8"),
        f"metahub.machine-identity.v1\x00{domain}\x00{value}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _command_idempotency(
    config: AppConfig, action_code: str, subject_id: str, idempotency_key: str
) -> str:
    return _secret_digest(
        config,
        f"idempotency:{action_code}:{subject_id}",
        idempotency_key,
    )


def _command_opaque(
    config: AppConfig,
    action_code: str,
    idempotency_key: str,
    command_id: str,
    prefix: str,
) -> str:
    digest = hmac.new(
        config.auth_token_secret.encode("utf-8"),
        (
            "metahub.machine-identity.command-secret.v1\x00"
            f"{action_code}\x00{idempotency_key}\x00{command_id}"
        ).encode("ascii"),
        hashlib.sha256,
    ).digest()
    return prefix + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _request_digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("ascii")).hexdigest()


def _valid_idempotency(
    value: str, *, error_code: str = "machine_identity.request_invalid"
) -> None:
    if _IDEMPOTENCY_RE.fullmatch(value or "") is None:
        raise ApiError(error_code)


def _json_string_tuple(value: object) -> tuple[str, ...]:
    parsed = json.loads(str(value))
    if (
        type(parsed) is not list
        or len(parsed) > 32
        or any(type(item) is not str or not item for item in parsed)
        or len(parsed) != len(set(parsed))
    ):
        raise ValueError("invalid string array")
    return tuple(parsed)


def _valid_service_account_id(value: str) -> None:
    if _SERVICE_ACCOUNT_RE.fullmatch(value) is None:
        raise ApiError("machine_identity.not_found")


def _request_identifier(value: str) -> str:
    if _SAFE_ID_RE.fullmatch(value) is None:
        raise ApiError("machine_identity.unavailable")
    return value


def _expired(value: object, now: datetime) -> bool:
    return _parse_time(value) <= now


def _last_used_touch_required(value: object | None, now: datetime) -> bool:
    return value is None or _parse_time(value) <= now - _LAST_USED_TOUCH_INTERVAL


def _parse_time(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


def _opaque(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _connect(config: AppConfig) -> sqlite3.Connection:
    return connect_database(config.database_url)


def _rollback(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        connection.rollback()
