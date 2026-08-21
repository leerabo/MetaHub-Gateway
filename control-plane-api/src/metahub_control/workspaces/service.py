"""Atomic Workspace/Tenant/owner provisioning from a verified GlobalSession."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import sqlite3
import unicodedata
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.identity.service import authenticate_global_session_in_transaction

from .models import (
    WorkspaceEnvironmentSummary,
    WorkspaceListResponse,
    WorkspaceProvisioningSummary,
    WorkspaceResponse,
    WorkspaceSummary,
)

logger = logging.getLogger("metahub.control.workspaces")

_WORKSPACE_ID_RE = re.compile(r"^wsp_[0-9a-f]{32}$")
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,46}[a-z0-9])$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._~-]{16,128}$")
_RESERVED_SLUGS = frozenset(
    {
        "admin",
        "api",
        "app",
        "billing",
        "console",
        "default",
        "identity",
        "internal",
        "metahub",
        "platform",
        "security",
        "support",
        "system",
        "workspace",
        "workspaces",
    }
)


def create_workspace(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    idempotency_key: str,
    kind: str,
    name: str | None,
    slug: str | None,
    region: str,
    service_mode: str,
    request_id: str,
    trace_id: str,
) -> WorkspaceResponse:
    normalized = _normalize_create_input(
        config,
        kind=kind,
        name=name,
        slug=slug,
        region=region,
        service_mode=service_mode,
    )
    if not _IDEMPOTENCY_RE.fullmatch(idempotency_key):
        raise ApiError("workspace.idempotency_invalid")
    request_sha256 = _request_sha256(normalized)
    key_hmac = _idempotency_hmac(config, idempotency_key)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        identity, now = authenticate_global_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=False,
        )
        _require_verified_account(connection, identity.account_id)
        existing = connection.execute(
            """
            SELECT request_sha256,workspace_id FROM workspace_creation_requests
            WHERE account_id=? AND idempotency_key_hmac=?
            """,
            (identity.account_id, key_hmac),
        ).fetchone()
        if existing is not None:
            if not hmac.compare_digest(str(existing["request_sha256"]), request_sha256):
                raise ApiError("workspace.idempotency_conflict")
            summary = _load_workspace(connection, identity.account_id, existing["workspace_id"])
            connection.commit()
            return WorkspaceResponse(
                workspace=summary,
                request_id=request_id,
                trace_id=trace_id,
            )

        account = connection.execute(
            """
            SELECT account_id,email_normalized,display_name,credential_revision
            FROM global_accounts
            WHERE account_id=? AND status='active' AND login_enabled=1
            """,
            (identity.account_id,),
        ).fetchone()
        credential = connection.execute(
            """
            SELECT secret_hash FROM account_credentials
            WHERE account_id=? AND credential_type='password' AND status='active'
            """,
            (identity.account_id,),
        ).fetchone()
        if account is None or credential is None:
            raise ApiError("identity.session_revoked")

        stamp = _db_time(now)
        expires_at = _db_time(now + timedelta(minutes=5))
        workspace_id = _id("wsp")
        tenant_id = _id("ten")
        legacy_user_id = _id("usr")
        tenant_slug = (
            str(normalized["slug"])
            if normalized["kind"] == "organization"
            else f"personal-{workspace_id[4:20]}"
        )
        profile_name = (
            str(normalized["name"])
            if normalized["kind"] == "organization"
            else _personal_label(str(account["display_name"]))
        )
        profile_state = "draft" if normalized["kind"] == "organization" else "confirmed"

        connection.execute(
            """
            INSERT INTO workspaces(
              workspace_id,kind,status,profile_state,creation_source,revision,
              created_by_account_id,created_at,updated_at)
            VALUES(?,?,'active',?,'user_created',1,?,?,?)
            """,
            (
                workspace_id,
                normalized["kind"],
                profile_state,
                identity.account_id,
                stamp,
                stamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO workspace_tenant_creation_claims(
              workspace_tenant_creation_claim_id,workspace_id,tenant_id,account_id,
              status,expires_at,consumed_at,created_at)
            VALUES(?,?,?,?,'pending',?,NULL,?)
            """,
            (_id("wtc"), workspace_id, tenant_id, identity.account_id, expires_at, stamp),
        )
        connection.execute(
            """
            INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
            VALUES(?,?,?,?, 'active',?,?)
            """,
            (
                tenant_id,
                tenant_slug,
                profile_name,
                normalized["service_mode"],
                stamp,
                stamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO workspace_tenant_bindings(
              workspace_tenant_binding_id,workspace_id,tenant_id,status,created_at)
            VALUES(?,?,?,'active',?)
            """,
            (_id("wtb"), workspace_id, tenant_id, stamp),
        )
        if _table_exists(connection, "workspace_operational_states"):
            connection.execute(
                """
                INSERT OR IGNORE INTO workspace_operational_states(
                  workspace_operational_state_id,workspace_id,tenant_id,status,
                  revision,risk_revision,source,suspended_reason_code,
                  suspended_by_operator_id,suspended_at,created_at,updated_at)
                VALUES(?,?,?,'active',1,1,'workspace_creation',NULL,NULL,NULL,?,?)
                """,
                (_id("wos"), workspace_id, tenant_id, stamp, stamp),
            )
        connection.execute(
            """
            INSERT INTO workspace_regions(
              workspace_region_id,workspace_id,tenant_id,region_code,
              residency_policy_version,status,created_at)
            VALUES(?,?,?,?,?,'active',?)
            """,
            (
                _id("wrg"),
                workspace_id,
                tenant_id,
                normalized["region"],
                config.workspace_residency_policy_version,
                stamp,
            ),
        )
        if normalized["kind"] == "organization":
            connection.execute(
                """
                INSERT INTO organization_profiles(
                  organization_profile_id,workspace_id,name,slug_normalized,
                  profile_state,revision,created_at,updated_at)
                VALUES(?,?,?,?,?,1,?,?)
                """,
                (
                    _id("org"),
                    workspace_id,
                    profile_name,
                    tenant_slug,
                    profile_state,
                    stamp,
                    stamp,
                ),
            )
        else:
            connection.execute(
                """
                INSERT INTO personal_profiles(
                  personal_profile_id,workspace_id,account_id,display_label,
                  revision,created_at,updated_at)
                VALUES(?,?,?,?,1,?,?)
                """,
                (_id("per"), workspace_id, identity.account_id, profile_name, stamp, stamp),
            )

        connection.execute(
            """
            INSERT INTO workspace_user_projection_claims(
              workspace_user_projection_claim_id,workspace_id,tenant_id,account_id,
              legacy_user_id,credential_revision,email_normalized,status,
              expires_at,consumed_at,created_at)
            VALUES(?,?,?,?,?,?,?,'pending',?,NULL,?)
            """,
            (
                _id("wpc"),
                workspace_id,
                tenant_id,
                identity.account_id,
                legacy_user_id,
                account["credential_revision"],
                account["email_normalized"],
                expires_at,
                stamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,
              created_at,updated_at,failed_login_count,locked_until,last_login_at,
              password_changed_at)
            VALUES(?,?,?,?,?,'admin','active',?,?,0,NULL,NULL,?)
            """,
            (
                legacy_user_id,
                tenant_id,
                account["email_normalized"],
                account["display_name"],
                credential["secret_hash"],
                stamp,
                stamp,
                stamp,
            ),
        )
        _verify_projection_claim_consumed(connection, tenant_id, legacy_user_id)
        connection.execute(
            """
            INSERT INTO workspace_memberships(
              workspace_membership_id,workspace_id,tenant_id,account_id,legacy_user_id,
              role_code,status,source,revision,created_by_account_id,updated_by_account_id,
              created_at,updated_at)
            VALUES(?,?,?,?,?,'owner','active','workspace_creator',1,?,?,?,?)
            """,
            (
                _id("wmb"),
                workspace_id,
                tenant_id,
                identity.account_id,
                legacy_user_id,
                identity.account_id,
                identity.account_id,
                stamp,
                stamp,
            ),
        )
        environment = connection.execute(
            """
            SELECT environment_id,status FROM environments
            WHERE tenant_id=? AND code='dev'
            """,
            (tenant_id,),
        ).fetchone()
        if environment is None or environment["status"] != "active":
            raise sqlite3.IntegrityError("workspace_default_environment_missing")
        connection.execute(
            """
            INSERT INTO workspace_environment_origins(
              workspace_environment_origin_id,workspace_id,tenant_id,
              environment_id,origin,created_at)
            VALUES(?,?,?,?,'system_created',?)
            """,
            (_id("weo"), workspace_id, tenant_id, environment["environment_id"], stamp),
        )
        connection.execute(
            """
            INSERT INTO workspace_provisioning_jobs(
              workspace_provisioning_job_id,workspace_id,tenant_id,generation,status,
              attempt_count,error_code,next_attempt_at,created_at,updated_at)
            VALUES(?,?,?,1,'ready',0,NULL,NULL,?,?)
            """,
            (_id("wpj"), workspace_id, tenant_id, stamp, stamp),
        )
        _write_event(
            connection,
            workspace_id=workspace_id,
            tenant_id=tenant_id,
            account_id=identity.account_id,
            event_type="workspace.created",
            reason_code="core_committed",
            request_id=request_id,
            trace_id=trace_id,
            payload={
                "schema_version": 1,
                "kind": normalized["kind"],
                "profile_state": profile_state,
                "region_code": normalized["region"],
                "environment_origin": "system_created",
            },
            now=now,
        )
        connection.execute(
            """
            INSERT INTO workspace_creation_requests(
              workspace_creation_request_id,account_id,idempotency_key_hmac,
              request_sha256,workspace_id,status,created_at)
            VALUES(?,?,?,?,?,'completed',?)
            """,
            (_id("wcr"), identity.account_id, key_hmac, request_sha256, workspace_id, stamp),
        )
        updated = connection.execute(
            """
            UPDATE workspace_tenant_creation_claims
            SET status='consumed',consumed_at=?
            WHERE workspace_id=? AND tenant_id=? AND account_id=? AND status='pending'
            """,
            (stamp, workspace_id, tenant_id, identity.account_id),
        ).rowcount
        if updated != 1:
            raise sqlite3.IntegrityError("workspace_tenant_claim_not_consumed")
        _verify_core_projection(connection, workspace_id, tenant_id, identity.account_id)
        summary = _load_workspace(connection, identity.account_id, workspace_id)
        connection.commit()
        logger.info(
            "workspace.created",
            extra={
                "event": "workspace.created",
                "request_id": request_id,
                "trace_id": trace_id,
                "workspace_id": workspace_id,
                "tenant_id": tenant_id,
                "kind": normalized["kind"],
                "status": "ready",
            },
        )
        return WorkspaceResponse(workspace=summary, request_id=request_id, trace_id=trace_id)
    except ApiError as exc:
        _finish_identity_error(connection, exc)
        raise
    except sqlite3.IntegrityError as exc:
        if connection.in_transaction:
            connection.rollback()
        if _is_create_conflict(exc):
            raise ApiError("workspace.create_conflict") from None
        raise ApiError("workspace.unavailable") from None
    except (sqlite3.Error, DbError, ValueError, TypeError):
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("workspace.unavailable") from None
    finally:
        connection.close()


def list_workspaces(
    config: AppConfig,
    *,
    session_token: str,
    limit: int,
    offset: int,
    request_id: str,
    trace_id: str,
) -> WorkspaceListResponse:
    connection = _connect(config)
    try:
        identity, _now = authenticate_global_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            touch=True,
        )
        total = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM workspace_memberships
                WHERE account_id=? AND status='active'
                """,
                (identity.account_id,),
            ).fetchone()[0]
        )
        rows = connection.execute(
            """
            SELECT workspace_id FROM workspace_memberships
            WHERE account_id=? AND status='active'
            ORDER BY created_at,workspace_id LIMIT ? OFFSET ?
            """,
            (identity.account_id, limit, offset),
        ).fetchall()
        workspaces = tuple(
            _load_workspace(connection, identity.account_id, row["workspace_id"]) for row in rows
        )
        connection.commit()
        return WorkspaceListResponse(
            workspaces=workspaces,
            limit=limit,
            offset=offset,
            total=total,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError as exc:
        _finish_identity_error(connection, exc)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError):
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("workspace.unavailable") from None
    finally:
        connection.close()


def get_workspace(
    config: AppConfig,
    *,
    session_token: str,
    workspace_id: str,
    request_id: str,
    trace_id: str,
) -> WorkspaceResponse:
    normalized_id = _normalize_workspace_id(workspace_id)
    connection = _connect(config)
    try:
        identity, _now = authenticate_global_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            touch=True,
        )
        summary = _load_workspace(connection, identity.account_id, normalized_id)
        connection.commit()
        return WorkspaceResponse(workspace=summary, request_id=request_id, trace_id=trace_id)
    except ApiError as exc:
        _finish_identity_error(connection, exc)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError):
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("workspace.unavailable") from None
    finally:
        connection.close()


def update_workspace(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    workspace_id: str,
    expected_revision: int,
    name: str | None,
    display_label: str | None,
    profile_state: str | None,
    supplied_fields: frozenset[str],
    request_id: str,
    trace_id: str,
) -> WorkspaceResponse:
    normalized_id = _normalize_workspace_id(workspace_id)
    if expected_revision < 1:
        raise ApiError("workspace.revision_invalid")
    if not supplied_fields:
        raise ApiError("workspace.update_empty")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        identity, now = authenticate_global_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=False,
        )
        row = connection.execute(
            """
            SELECT w.kind,w.profile_state,w.revision,b.tenant_id
            FROM workspaces w
            JOIN workspace_tenant_bindings b ON b.workspace_id=w.workspace_id
            JOIN workspace_memberships m ON m.workspace_id=w.workspace_id
              AND m.tenant_id=b.tenant_id
            WHERE w.workspace_id=? AND m.account_id=? AND m.role_code='owner'
              AND m.status='active'
            """,
            (normalized_id, identity.account_id),
        ).fetchone()
        if row is None:
            raise ApiError("workspace.not_found")
        if int(row["revision"]) != expected_revision:
            raise ApiError("workspace.revision_conflict")
        stamp = _db_time(now)
        next_revision = expected_revision + 1
        event_reason: str | None = None
        if row["kind"] == "organization":
            if supplied_fields - {"name", "profile_state"}:
                raise ApiError("workspace.update_invalid")
            normalized_name = _normalize_name(name) if "name" in supplied_fields else None
            if profile_state is not None:
                if profile_state != "confirmed" or row["profile_state"] != "draft":
                    raise ApiError("workspace.profile_transition_invalid")
                event_reason = "profile_confirmed"
            elif row["profile_state"] == "confirmed" and normalized_name is not None:
                event_reason = "profile_updated"
            assignments: list[str] = ["revision=?", "updated_at=?"]
            values: list[object] = [next_revision, stamp]
            if normalized_name is not None:
                assignments.append("name=?")
                values.append(normalized_name)
            if profile_state is not None:
                assignments.append("profile_state='confirmed'")
            values.append(normalized_id)
            updated = connection.execute(
                f"UPDATE organization_profiles SET {','.join(assignments)} WHERE workspace_id=?",
                values,
            ).rowcount
        else:
            if supplied_fields != {"display_label"}:
                raise ApiError("workspace.update_invalid")
            normalized_label = _normalize_name(display_label, maximum=120)
            updated = connection.execute(
                """
                UPDATE personal_profiles SET display_label=?,revision=?,updated_at=?
                WHERE workspace_id=?
                """,
                (normalized_label, next_revision, stamp, normalized_id),
            ).rowcount
        if updated != 1:
            raise sqlite3.IntegrityError("workspace_profile_missing")
        next_profile_state = "confirmed" if profile_state is not None else row["profile_state"]
        connection.execute(
            """
            UPDATE workspaces SET profile_state=?,revision=?,updated_at=?
            WHERE workspace_id=? AND revision=?
            """,
            (next_profile_state, next_revision, stamp, normalized_id, expected_revision),
        )
        if event_reason is not None:
            _write_event(
                connection,
                workspace_id=normalized_id,
                tenant_id=row["tenant_id"],
                account_id=identity.account_id,
                event_type="workspace.profile_confirmed",
                reason_code=event_reason,
                request_id=request_id,
                trace_id=trace_id,
                payload={
                    "schema_version": 1,
                    "profile_state": next_profile_state,
                    "revision": next_revision,
                },
                now=now,
            )
        summary = _load_workspace(connection, identity.account_id, normalized_id)
        connection.commit()
        return WorkspaceResponse(workspace=summary, request_id=request_id, trace_id=trace_id)
    except ApiError as exc:
        _finish_identity_error(connection, exc)
        raise
    except sqlite3.IntegrityError:
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("workspace.update_invalid") from None
    except (sqlite3.Error, DbError, ValueError, TypeError):
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("workspace.unavailable") from None
    finally:
        connection.close()


def _normalize_create_input(
    config: AppConfig,
    *,
    kind: str,
    name: str | None,
    slug: str | None,
    region: str,
    service_mode: str,
) -> dict[str, str | None]:
    normalized_kind = kind.strip().lower()
    if normalized_kind not in {"personal", "organization"}:
        raise ApiError("workspace.kind_invalid")
    normalized_region = region.strip().lower()
    if normalized_region not in config.workspace_allowed_regions:
        raise ApiError("workspace.region_invalid")
    normalized_mode = service_mode.strip().lower()
    if normalized_mode not in {"internal_only", "public_service"}:
        raise ApiError("workspace.service_mode_invalid")
    if normalized_kind == "organization":
        normalized_name = _normalize_name(name)
        normalized_slug = _normalize_slug(slug)
    else:
        if name is not None or slug is not None:
            raise ApiError("workspace.request_invalid")
        normalized_name = None
        normalized_slug = None
    return {
        "kind": normalized_kind,
        "name": normalized_name,
        "slug": normalized_slug,
        "region": normalized_region,
        "service_mode": normalized_mode,
    }


def _normalize_name(value: str | None, *, maximum: int = 160) -> str:
    if type(value) is not str:
        raise ApiError("workspace.profile_invalid")
    normalized = unicodedata.normalize("NFC", value).strip()
    if (
        not normalized
        or len(normalized) > maximum
        or len(normalized.encode("utf-8")) > maximum * 4
        or any(unicodedata.category(character).startswith("C") for character in normalized)
    ):
        raise ApiError("workspace.profile_invalid")
    return normalized


def _normalize_slug(value: str | None) -> str:
    if type(value) is not str:
        raise ApiError("workspace.slug_invalid")
    normalized = value.strip().lower()
    if not _SLUG_RE.fullmatch(normalized) or normalized in _RESERVED_SLUGS:
        raise ApiError("workspace.slug_invalid")
    return normalized


def _normalize_workspace_id(value: str) -> str:
    if not _WORKSPACE_ID_RE.fullmatch(value):
        raise ApiError("workspace.not_found")
    return value


def _personal_label(display_name: str) -> str:
    label = _normalize_name(display_name, maximum=100)
    suffix = " Personal Workspace"
    return f"{label[: 120 - len(suffix)]}{suffix}"


def _request_sha256(normalized: dict[str, str | None]) -> str:
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _idempotency_hmac(config: AppConfig, value: str) -> str:
    payload = b"metahub-workspace-idempotency-v1\x00" + value.encode("ascii")
    return hmac.new(
        config.identity_session_secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()


def _require_verified_account(connection: sqlite3.Connection, account_id: str) -> None:
    if (
        connection.execute(
            "SELECT 1 FROM account_verifications WHERE account_id=? LIMIT 1",
            (account_id,),
        ).fetchone()
        is None
    ):
        raise ApiError("workspace.account_unverified")


def _verify_projection_claim_consumed(
    connection: sqlite3.Connection,
    tenant_id: str,
    user_id: str,
) -> None:
    row = connection.execute(
        """
        SELECT status,consumed_at FROM workspace_user_projection_claims
        WHERE tenant_id=? AND legacy_user_id=?
        """,
        (tenant_id, user_id),
    ).fetchone()
    if row is None or row["status"] != "consumed" or row["consumed_at"] is None:
        raise sqlite3.IntegrityError("workspace_projection_claim_not_consumed")


def _verify_core_projection(
    connection: sqlite3.Connection,
    workspace_id: str,
    tenant_id: str,
    account_id: str,
) -> None:
    template_counts = tuple(
        connection.execute(
            """
            SELECT
              (SELECT COUNT(DISTINCT role_code) FROM rbac_role_permission_templates),
              (SELECT COUNT(*) FROM rbac_permission_templates),
              (SELECT COUNT(*) FROM rbac_role_permission_templates)
            """
        ).fetchone()
    )
    actual_counts = tuple(
        connection.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM roles WHERE tenant_id=?),
              (SELECT COUNT(*) FROM permissions WHERE tenant_id=?),
              (SELECT COUNT(*) FROM role_permissions WHERE tenant_id=?)
            """,
            (tenant_id, tenant_id, tenant_id),
        ).fetchone()
    )
    authority = tuple(
        connection.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM workspace_memberships
                WHERE workspace_id=? AND tenant_id=? AND account_id=?
                  AND role_code='owner' AND status='active'),
              (SELECT COUNT(*) FROM account_identity_links
                WHERE account_id=? AND legacy_tenant_id=? AND status='active'),
              (SELECT COUNT(*) FROM environments WHERE tenant_id=? AND code='dev'),
              (SELECT COUNT(*) FROM workspace_environment_origins
                WHERE workspace_id=? AND tenant_id=? AND origin='system_created'),
              (SELECT COUNT(*) FROM providers WHERE tenant_id=?),
              (SELECT COUNT(*) FROM provider_credentials WHERE tenant_id=?),
              (SELECT COUNT(*) FROM models WHERE tenant_id=?),
              (SELECT COUNT(*) FROM virtual_keys WHERE tenant_id=?),
              (SELECT COUNT(*) FROM projects WHERE tenant_id=?)
            """,
            (
                workspace_id,
                tenant_id,
                account_id,
                account_id,
                tenant_id,
                tenant_id,
                workspace_id,
                tenant_id,
                tenant_id,
                tenant_id,
                tenant_id,
                tenant_id,
                tenant_id,
            ),
        ).fetchone()
    )
    if template_counts != actual_counts or authority != (1, 1, 1, 1, 0, 0, 0, 0, 0):
        raise sqlite3.IntegrityError("workspace_core_projection_invalid")


def _write_event(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    tenant_id: str,
    account_id: str,
    event_type: str,
    reason_code: str,
    request_id: str,
    trace_id: str,
    payload: dict[str, object],
    now: datetime,
) -> None:
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    event_id = _id("wev")
    outbox_id = _id("wob")
    stamp = _db_time(now)
    connection.execute(
        """
        INSERT INTO workspace_events(
          workspace_event_id,workspace_id,tenant_id,account_id,event_type,
          reason_code,request_id,trace_id,payload_json,payload_sha256,occurred_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id,
            workspace_id,
            tenant_id,
            account_id,
            event_type,
            reason_code,
            request_id,
            trace_id,
            payload_json,
            hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
            stamp,
        ),
    )
    connection.execute(
        """
        INSERT INTO workspace_event_outbox(
          workspace_event_outbox_id,workspace_event_id,status,attempt_count,generation,
          next_attempt_at,delivered_at,created_at,updated_at)
        VALUES(?,?,'pending',0,0,?,NULL,?,?)
        """,
        (outbox_id, event_id, stamp, stamp, stamp),
    )


def _load_workspace(
    connection: sqlite3.Connection,
    account_id: str,
    workspace_id: str,
) -> WorkspaceSummary:
    row = connection.execute(
        """
        SELECT w.workspace_id,w.kind,w.status,w.profile_state,w.revision,
          COALESCE(op.name,pp.display_label) display_name,op.slug_normalized,
          region.region_code,tenant.service_mode,environment.code environment_code,
          environment.status environment_status,origin.origin environment_origin,
          job.status provisioning_status
        FROM workspaces w
        JOIN workspace_tenant_bindings binding ON binding.workspace_id=w.workspace_id
        JOIN workspace_memberships membership
          ON membership.workspace_id=w.workspace_id AND membership.tenant_id=binding.tenant_id
        JOIN tenants tenant ON tenant.tenant_id=binding.tenant_id
        JOIN workspace_regions region
          ON region.workspace_id=w.workspace_id AND region.tenant_id=binding.tenant_id
        JOIN workspace_provisioning_jobs job
          ON job.workspace_id=w.workspace_id AND job.tenant_id=binding.tenant_id
        JOIN workspace_environment_origins origin
          ON origin.workspace_id=w.workspace_id AND origin.tenant_id=binding.tenant_id
        JOIN environments environment
          ON environment.environment_id=origin.environment_id
         AND environment.tenant_id=binding.tenant_id AND environment.code='dev'
        LEFT JOIN organization_profiles op ON op.workspace_id=w.workspace_id
        LEFT JOIN personal_profiles pp ON pp.workspace_id=w.workspace_id
        WHERE w.workspace_id=? AND membership.account_id=?
          AND membership.status='active'
        """,
        (workspace_id, account_id),
    ).fetchone()
    if row is None:
        raise ApiError("workspace.not_found")
    return WorkspaceSummary(
        workspace_id=row["workspace_id"],
        kind=row["kind"],
        status=row["status"],
        profile_state=row["profile_state"],
        name=row["display_name"],
        slug=row["slug_normalized"],
        region=row["region_code"],
        service_mode=row["service_mode"],
        environment=WorkspaceEnvironmentSummary(
            code=row["environment_code"],
            status=row["environment_status"],
            origin=row["environment_origin"],
        ),
        provisioning=WorkspaceProvisioningSummary(status=row["provisioning_status"]),
        revision=int(row["revision"]),
    )


def _finish_identity_error(connection: sqlite3.Connection, error: ApiError) -> None:
    if not connection.in_transaction:
        return
    if error.code == "identity.session_expired":
        connection.commit()
    else:
        connection.rollback()


def _is_create_conflict(error: sqlite3.IntegrityError) -> bool:
    text = str(error).lower()
    return "unique constraint failed" in text and (
        "slug" in text
        or "workspace_creation_requests.account_id" in text
        or "workspaces.created_by_account_id" in text
    )


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        is not None
    )


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("workspace.unavailable") from None


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
