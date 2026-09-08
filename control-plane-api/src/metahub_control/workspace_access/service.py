"""Database-authoritative WorkspaceSession, Membership and invitation services."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.db.connection import connect_database, database_operation_deadline
from metahub_shared.db.errors import DbError
from metahub_shared.db.schema import table_exists
from metahub_shared.db.security import hash_token, verify_password
from metahub_shared.rbac import PERMISSION_BY_CODE, authorization_fingerprint
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.rbac_snapshot import require_actor_runtime_permissions

from metahub_control.auth.models import AuthSession, AuthTenant, AuthUser
from metahub_control.auth.service import AuthContext, authentication_database_deadline
from metahub_control.customer_support.permissions import (
    CUSTOMER_SUPPORT_ROLE_PERMISSIONS,
)
from metahub_control.identity.service import (
    authenticate_global_session_in_transaction,
    normalize_global_email,
    valid_opaque_value,
)
from metahub_control.migration_operations.permissions import (
    MIGRATION_ROLE_PERMISSIONS,
)

from .assurance_permissions import ASSURANCE_ROLE_PERMISSIONS
from .brand_permissions import WORKSPACE_BRAND_ROLE_PERMISSIONS
from .commercial_permissions import WORKSPACE_COMMERCIAL_ROLE_PERMISSIONS
from .compliance_permissions import COMPLIANCE_ROLE_PERMISSIONS
from .console_permissions import WORKSPACE_CONSOLE_ROLE_PERMISSIONS
from .edge_permissions import (
    EDGE_BACKUP_WORKSPACE_ROLE_PERMISSIONS,
    EDGE_DIAGNOSTIC_WORKSPACE_ROLE_PERMISSIONS,
    EDGE_INSTALLER_WORKSPACE_ROLE_PERMISSIONS,
    EDGE_LICENSE_WORKSPACE_ROLE_PERMISSIONS,
    EDGE_OFFLINE_WORKSPACE_ROLE_PERMISSIONS,
    EDGE_SECRET_WORKSPACE_ROLE_PERMISSIONS,
    EDGE_TELEMETRY_WORKSPACE_ROLE_PERMISSIONS,
    EDGE_UPGRADE_WORKSPACE_ROLE_PERMISSIONS,
    EDGE_VALIDATION_WORKSPACE_ROLE_PERMISSIONS,
    EDGE_WORKSPACE_ROLE_PERMISSIONS,
)
from .models import (
    IdentityWorkspaceListResponse,
    IdentityWorkspaceSummary,
    MembershipInvitationResponse,
    MembershipInvitationSummary,
    MembershipListResponse,
    MembershipResponse,
    MembershipSummary,
    OwnerTransferResponse,
    WorkspaceAuthorizationSummary,
    WorkspaceContextSummary,
    WorkspaceSessionResponse,
    WorkspaceSwitchResponse,
)
from .navigation_permissions import WORKSPACE_NAVIGATION_ROLE_PERMISSIONS
from .privacy_permissions import PRIVACY_ROLE_PERMISSIONS
from .provider_catalog_permissions import PROVIDER_CATALOG_ROLE_PERMISSIONS
from .provider_engineering_permissions import (
    WORKSPACE_PROVIDER_ENGINEERING_ROLE_PERMISSIONS,
)

logger = logging.getLogger("metahub.control.workspace_access")

_WORKSPACE_ID_RE = re.compile(r"^wsp_[0-9a-f]{32}$")
_MEMBERSHIP_ID_RE = re.compile(r"^wmb_[0-9a-f]{32}$")
_HUMAN_ROLES = frozenset(
    {
        "owner",
        "admin",
        "developer",
        "security_admin",
        "finance_admin",
        "legal_admin",
        "auditor",
        "viewer",
    }
)
_DELEGABLE_ROLES = _HUMAN_ROLES - {"owner"}
_MANAGER_ROLES = frozenset({"owner", "admin"})
_LEGACY_ROLE_BY_WORKSPACE_ROLE = {
    "owner": "admin",
    "admin": "admin",
    "developer": "developer",
    "security_admin": "security_admin",
    "finance_admin": "finance_admin",
    "legal_admin": "viewer",
    "auditor": "viewer",
    "viewer": "viewer",
}
_WORKSPACE_ROLE_BY_LEGACY_ROLE = {
    "admin": "admin",
    "ops_admin": "admin",
    "developer": "developer",
    "security_admin": "security_admin",
    "finance_admin": "finance_admin",
    "viewer": "viewer",
}
_WORKSPACE_ROLES_BY_LEGACY_ROLE = {
    "admin": frozenset(("owner", "admin")),
    "ops_admin": frozenset(("admin",)),
    "developer": frozenset(("developer",)),
    "security_admin": frozenset(("security_admin",)),
    "finance_admin": frozenset(("finance_admin",)),
    "viewer": frozenset(("legal_admin", "auditor", "viewer")),
}
_GOVERNANCE_ROLE_PERMISSIONS = {
    "owner": (
        "data_governance.manage",
        "data_governance.read",
        "legal_hold.manage",
        "legal_hold.read",
    ),
    "security_admin": (
        "data_governance.manage",
        "data_governance.read",
        "legal_hold.read",
    ),
    "legal_admin": (
        "data_governance.read",
        "legal_hold.manage",
        "legal_hold.read",
    ),
    "auditor": ("data_governance.read", "legal_hold.read"),
}
_MACHINE_IDENTITY_ROLE_PERMISSIONS = {
    "owner": ("machine_identity.manage", "machine_identity.read"),
    "security_admin": ("machine_identity.manage", "machine_identity.read"),
    "developer": ("machine_identity.read",),
}


@dataclass(frozen=True, slots=True)
class _WorkspaceExtensionFamily:
    group: str
    catalog_table: str
    assignments_table: str
    introduced_migration: str
    expected_by_role: dict[str, tuple[str, ...]]
    required_for_workspace_session: bool = False


_WORKSPACE_EXTENSION_GROUPS = (
    "governance",
    "machine_identity",
    "provider_catalog",
    "edge",
    "migration",
    "customer_support",
    "privacy",
    "compliance",
    "assurance",
    "branding",
    "provider_engineering",
    "commercial",
    "console",
    "navigation",
)
_WORKSPACE_EXTENSION_FAMILIES = (
    _WorkspaceExtensionFamily(
        "governance",
        "workspace_governance_permission_extensions",
        "workspace_governance_role_permissions",
        "0109",
        _GOVERNANCE_ROLE_PERMISSIONS,
        True,
    ),
    _WorkspaceExtensionFamily(
        "machine_identity",
        "workspace_machine_permission_extensions",
        "workspace_machine_role_permissions",
        "0110",
        _MACHINE_IDENTITY_ROLE_PERMISSIONS,
        True,
    ),
    _WorkspaceExtensionFamily(
        "provider_catalog",
        "workspace_provider_catalog_permission_extensions",
        "workspace_provider_catalog_role_permissions",
        "0111",
        PROVIDER_CATALOG_ROLE_PERMISSIONS,
        True,
    ),
    _WorkspaceExtensionFamily(
        "edge",
        "workspace_edge_permission_extensions",
        "workspace_edge_role_permissions",
        "0112",
        EDGE_WORKSPACE_ROLE_PERMISSIONS,
    ),
    _WorkspaceExtensionFamily(
        "edge",
        "workspace_edge_telemetry_permission_extensions",
        "workspace_edge_telemetry_role_permissions",
        "0114",
        EDGE_TELEMETRY_WORKSPACE_ROLE_PERMISSIONS,
    ),
    _WorkspaceExtensionFamily(
        "edge",
        "workspace_edge_secret_permission_extensions",
        "workspace_edge_secret_role_permissions",
        "0115",
        EDGE_SECRET_WORKSPACE_ROLE_PERMISSIONS,
    ),
    _WorkspaceExtensionFamily(
        "edge",
        "workspace_edge_installer_permission_extensions",
        "workspace_edge_installer_role_permissions",
        "0116",
        EDGE_INSTALLER_WORKSPACE_ROLE_PERMISSIONS,
    ),
    _WorkspaceExtensionFamily(
        "edge",
        "workspace_edge_validation_permission_extensions",
        "workspace_edge_validation_role_permissions",
        "0117",
        EDGE_VALIDATION_WORKSPACE_ROLE_PERMISSIONS,
    ),
    _WorkspaceExtensionFamily(
        "edge",
        "workspace_edge_diagnostic_permission_extensions",
        "workspace_edge_diagnostic_role_permissions",
        "0118",
        EDGE_DIAGNOSTIC_WORKSPACE_ROLE_PERMISSIONS,
    ),
    _WorkspaceExtensionFamily(
        "edge",
        "workspace_edge_upgrade_permission_extensions",
        "workspace_edge_upgrade_role_permissions",
        "0119",
        EDGE_UPGRADE_WORKSPACE_ROLE_PERMISSIONS,
    ),
    _WorkspaceExtensionFamily(
        "edge",
        "workspace_edge_license_permission_extensions",
        "workspace_edge_license_role_permissions",
        "0120",
        EDGE_LICENSE_WORKSPACE_ROLE_PERMISSIONS,
    ),
    _WorkspaceExtensionFamily(
        "edge",
        "workspace_edge_backup_permission_extensions",
        "workspace_edge_backup_role_permissions",
        "0121",
        EDGE_BACKUP_WORKSPACE_ROLE_PERMISSIONS,
    ),
    _WorkspaceExtensionFamily(
        "edge",
        "workspace_edge_offline_permission_extensions",
        "workspace_edge_offline_role_permissions",
        "0122",
        EDGE_OFFLINE_WORKSPACE_ROLE_PERMISSIONS,
    ),
    _WorkspaceExtensionFamily(
        "migration",
        "workspace_migration_permission_extensions",
        "workspace_migration_role_permissions",
        "0123",
        MIGRATION_ROLE_PERMISSIONS,
    ),
    _WorkspaceExtensionFamily(
        "customer_support",
        "workspace_customer_support_permission_extensions",
        "workspace_customer_support_role_permissions",
        "0124",
        CUSTOMER_SUPPORT_ROLE_PERMISSIONS,
    ),
    _WorkspaceExtensionFamily(
        "privacy",
        "workspace_privacy_permission_extensions",
        "workspace_privacy_role_permissions",
        "0126",
        PRIVACY_ROLE_PERMISSIONS,
    ),
    _WorkspaceExtensionFamily(
        "compliance",
        "workspace_compliance_permission_extensions",
        "workspace_compliance_role_permissions",
        "0127",
        COMPLIANCE_ROLE_PERMISSIONS,
    ),
    _WorkspaceExtensionFamily(
        "assurance",
        "workspace_security_assurance_permission_extensions",
        "workspace_security_assurance_role_permissions",
        "0129",
        ASSURANCE_ROLE_PERMISSIONS,
    ),
    _WorkspaceExtensionFamily(
        "branding",
        "workspace_brand_permission_extensions",
        "workspace_brand_role_permissions",
        "0134",
        WORKSPACE_BRAND_ROLE_PERMISSIONS,
    ),
    _WorkspaceExtensionFamily(
        "provider_engineering",
        "workspace_provider_engineering_permission_extensions",
        "workspace_provider_engineering_role_permissions",
        "0135",
        WORKSPACE_PROVIDER_ENGINEERING_ROLE_PERMISSIONS,
    ),
    _WorkspaceExtensionFamily(
        "commercial",
        "workspace_commercial_permission_extensions",
        "workspace_commercial_role_permissions",
        "0136",
        WORKSPACE_COMMERCIAL_ROLE_PERMISSIONS,
    ),
    _WorkspaceExtensionFamily(
        "console",
        "workspace_console_permission_extensions",
        "workspace_console_role_permissions",
        "0137",
        WORKSPACE_CONSOLE_ROLE_PERMISSIONS,
    ),
    _WorkspaceExtensionFamily(
        "navigation",
        "workspace_navigation_permission_extensions",
        "workspace_navigation_role_permissions",
        "0138",
        WORKSPACE_NAVIGATION_ROLE_PERMISSIONS,
    ),
)
WORKSPACE_EXTENSION_PERMISSION_CODES = frozenset(
    permission_code
    for family in _WORKSPACE_EXTENSION_FAMILIES
    for permission_codes in family.expected_by_role.values()
    for permission_code in permission_codes
)


@dataclass(frozen=True, slots=True)
class _WorkspacePermissionSnapshot:
    permission_codes: tuple[str, ...]
    permission_revision: int
    matrix_sha256: str
    authorization_fingerprint: str
    legacy_authorization_fingerprint: str


@dataclass(frozen=True, slots=True)
class WorkspaceActorPermissionSnapshot:
    """Current human Workspace authority used by delegated machine principals."""

    account_id: str
    workspace_id: str
    tenant_id: str
    membership_role: str
    permission_codes: tuple[str, ...]
    permission_revision: int
    authorization_fingerprint: str


@dataclass(frozen=True, slots=True)
class WorkspaceSessionContext:
    workspace_session_id: str
    global_session_id: str
    account_id: str
    workspace_id: str
    tenant_id: str
    membership_id: str
    legacy_user_id: str
    membership_role: str
    legacy_role: str
    account_email: str
    display_name: str
    tenant_slug: str
    tenant_name: str
    permission_codes: tuple[str, ...]
    permission_revision: int
    authorization_fingerprint: str
    expires_at: datetime
    token: str
    legacy_authorization_fingerprint: str | None = None

    def as_auth_context(self) -> AuthContext:
        legacy_permissions = tuple(
            code for code in self.permission_codes if code in PERMISSION_BY_CODE
        )
        extension_permissions = tuple(
            code for code in self.permission_codes if code not in PERMISSION_BY_CODE
        )
        return AuthContext(
            user=AuthUser(
                user_id=self.legacy_user_id,
                tenant_id=self.tenant_id,
                email=self.account_email,
                display_name=self.display_name,
                role=self.legacy_role,
                permission_codes=legacy_permissions,
                permission_revision=self.permission_revision,
                authorization_fingerprint=(
                    self.legacy_authorization_fingerprint or self.authorization_fingerprint
                ),
            ),
            tenant=AuthTenant(
                tenant_id=self.tenant_id,
                slug=self.tenant_slug,
                name=self.tenant_name,
            ),
            session=AuthSession(
                session_id=self.workspace_session_id,
                expires_at=self.expires_at,
            ),
            token=self.token,
            token_jti=self.workspace_session_id,
            extension_permission_codes=extension_permissions,
            extension_authorization_fingerprint=(
                self.authorization_fingerprint if extension_permissions else None
            ),
        )


def list_identity_workspaces(
    config: AppConfig,
    *,
    session_token: str,
    limit: int,
    offset: int,
    request_id: str,
    trace_id: str,
) -> IdentityWorkspaceListResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        identity, _now = authenticate_global_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            touch=True,
        )
        operational_filter = (
            "AND EXISTS(SELECT 1 FROM workspace_operational_states operational "
            "WHERE operational.workspace_id=member.workspace_id "
            "AND operational.tenant_id=member.tenant_id AND operational.status='active')"
            if _table_exists(connection, "workspace_operational_states")
            else ""
        )
        where = (
            """
          member.account_id=? AND member.status='active'
          AND member.role_code!='service_account'
          AND workspace.status='active' AND binding.status='active'
        """
            + operational_filter
        )
        total = int(
            connection.execute(
                f"""
                SELECT COUNT(*)
                FROM workspace_memberships member
                JOIN workspaces workspace ON workspace.workspace_id=member.workspace_id
                JOIN workspace_tenant_bindings binding
                  ON binding.workspace_id=member.workspace_id AND binding.tenant_id=member.tenant_id
                WHERE {where}
                """,
                (identity.account_id,),
            ).fetchone()[0]
        )
        rows = connection.execute(
            f"""
            SELECT workspace.workspace_id,workspace.kind,
              COALESCE(organization.name,personal.display_label) display_name,
              organization.slug_normalized,member.role_code
            FROM workspace_memberships member
            JOIN workspaces workspace ON workspace.workspace_id=member.workspace_id
            JOIN workspace_tenant_bindings binding
              ON binding.workspace_id=member.workspace_id AND binding.tenant_id=member.tenant_id
            LEFT JOIN organization_profiles organization
              ON organization.workspace_id=workspace.workspace_id
            LEFT JOIN personal_profiles personal
              ON personal.workspace_id=workspace.workspace_id
            WHERE {where}
            ORDER BY workspace.created_at,workspace.workspace_id
            LIMIT ? OFFSET ?
            """,
            (identity.account_id, limit, offset),
        ).fetchall()
        connection.commit()
        return IdentityWorkspaceListResponse(
            workspaces=tuple(_identity_workspace(row) for row in rows),
            limit=limit,
            offset=offset,
            total=total,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError as exc:
        _finish_global_error(connection, exc)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("workspace_session.unavailable") from None
    finally:
        connection.close()


def switch_workspace(
    config: AppConfig,
    *,
    global_session_token: str,
    global_csrf_token: str,
    workspace_id: str,
    request_id: str,
    trace_id: str,
    client_host: str | None = None,
) -> tuple[WorkspaceSwitchResponse, str, str]:
    if not _WORKSPACE_ID_RE.fullmatch(workspace_id):
        raise ApiError("workspace.not_found")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        identity, now = authenticate_global_session_in_transaction(
            connection,
            config,
            session_token=global_session_token,
            csrf_token=global_csrf_token,
            touch=True,
        )
        row = _switch_target(connection, identity.account_id, workspace_id)
        if row is None:
            raise ApiError("workspace.not_found")
        from .conditional_access import enforce_conditional_access_in_transaction

        conditional_access_revision = enforce_conditional_access_in_transaction(
            connection,
            account_id=identity.account_id,
            global_session_id=identity.session_id,
            workspace_id=row["workspace_id"],
            membership_id=row["workspace_membership_id"],
            client_host=client_host,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        snapshot = _permission_snapshot(connection, row)
        session_id = _id("wss")
        enterprise_grant = _recovery_grant_for_switch(
            connection,
            global_session_id=identity.session_id,
            account_id=identity.account_id,
            workspace_id=row["workspace_id"],
            tenant_id=row["tenant_id"],
            now=now,
        )
        stamp = _db_time(now)
        connection.execute(
            """
            UPDATE workspace_sessions
            SET status='revoked',revoked_at=?,revoked_reason='workspace_switched',updated_at=?
            WHERE global_session_id=? AND status='active'
            """,
            (stamp, stamp, identity.session_id),
        )
        raw_token = secrets.token_urlsafe(32)
        raw_csrf = secrets.token_urlsafe(32)
        absolute_expires = min(
            now + timedelta(seconds=config.workspace_session_ttl_seconds),
            identity.current_session.expires_at,
        )
        if enterprise_grant is not None:
            absolute_expires = min(absolute_expires, _parse_time(enterprise_grant["expires_at"]))
        connection.execute(
            """
            INSERT INTO workspace_sessions(
              workspace_session_id,global_session_id,account_id,workspace_id,tenant_id,
              membership_id,token_hmac,csrf_hmac,status,membership_revision,
              workspace_revision,account_session_revision,legacy_permission_revision,
              matrix_fingerprint,created_at,last_seen_at,absolute_expires_at,
              idle_expires_at,revoked_at,revoked_reason,updated_at)
            VALUES(?,?,?,?,?,?,?,?, 'active',?,?,?,?,?,?,?,?,?,NULL,NULL,?)
            """,
            (
                session_id,
                identity.session_id,
                identity.account_id,
                row["workspace_id"],
                row["tenant_id"],
                row["workspace_membership_id"],
                _session_hmac(config, "token", raw_token),
                _session_hmac(config, "csrf", raw_csrf),
                row["membership_revision"],
                row["workspace_revision"],
                row["account_session_revision"],
                snapshot.permission_revision,
                snapshot.authorization_fingerprint,
                stamp,
                stamp,
                _db_time(absolute_expires),
                _db_time(absolute_expires),
                stamp,
            ),
        )
        connection.execute(
            """UPDATE workspace_sessions SET conditional_access_revision=?,
                 assurance_level=?,trusted_device_id=?,updated_at=?
               WHERE workspace_session_id=?""",
            (
                conditional_access_revision,
                identity.current_session.assurance_level,
                identity.current_session.trusted_device_id,
                stamp,
                session_id,
            ),
        )
        if enterprise_grant is not None:
            grant_stamp = _db_time(
                max(now, _parse_time(enterprise_grant["updated_at"]) + timedelta(seconds=1))
            )
            consumed = connection.execute(
                """UPDATE enterprise_identity_session_grants
                     SET workspace_session_id=?,status='consumed',revision=2,
                         consumed_at=?,updated_at=?
                     WHERE enterprise_identity_session_grant_id=?
                       AND status='active' AND revision=1""",
                (
                    session_id,
                    grant_stamp,
                    grant_stamp,
                    enterprise_grant["enterprise_identity_session_grant_id"],
                ),
            ).rowcount
            if consumed != 1:
                raise ApiError("sso.login_required")
        _write_event(
            connection,
            workspace_id=row["workspace_id"],
            tenant_id=row["tenant_id"],
            actor_account_id=identity.account_id,
            subject_membership_id=row["workspace_membership_id"],
            event_type="workspace.switched",
            reason_code="user_selected",
            request_id=request_id,
            trace_id=trace_id,
            payload={
                "schema_version": 1,
                "role_code": row["role_code"],
                "membership_revision": row["membership_revision"],
            },
            now=now,
        )
        connection.commit()
        logger.info(
            "workspace.switched",
            extra={
                "event": "workspace.switched",
                "workspace_id": row["workspace_id"],
                "request_id": request_id,
                "trace_id": trace_id,
                "role_code": row["role_code"],
            },
        )
        response = WorkspaceSwitchResponse(
            context=_context_summary(row, absolute_expires),
            request_id=request_id,
            trace_id=trace_id,
        )
        return response, raw_token, raw_csrf
    except ApiError as exc:
        _finish_global_error(connection, exc)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("workspace_session.unavailable") from None
    finally:
        connection.close()


def get_workspace_session(
    config: AppConfig,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> WorkspaceSessionResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context, row = _authenticate_workspace(
            connection,
            config,
            session_token=session_token,
            csrf_token=None,
            touch=True,
        )
        connection.commit()
        return WorkspaceSessionResponse(
            context=_context_summary(row, context.expires_at),
            authorization=WorkspaceAuthorizationSummary(
                permission_codes=context.permission_codes,
                permission_revision=context.permission_revision,
                permission_fingerprint=context.authorization_fingerprint,
                membership_revision=row["current_membership_revision"],
            ),
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError as exc:
        _finish_workspace_error(connection, exc)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("workspace_session.unavailable") from None
    finally:
        connection.close()


def authenticate_workspace_session_as_auth_context(
    config: AppConfig,
    session_token: str,
    *,
    csrf_token: str | None = None,
    monotonic_deadline: float | None = None,
    deadline_error_code: str = "workspace_session.unavailable",
) -> AuthContext:
    return authenticate_workspace_session_context(
        config,
        session_token,
        csrf_token=csrf_token,
        monotonic_deadline=monotonic_deadline,
        deadline_error_code=deadline_error_code,
    ).as_auth_context()


def authenticate_workspace_session_context(
    config: AppConfig,
    session_token: str,
    *,
    csrf_token: str | None = None,
    monotonic_deadline: float | None = None,
    deadline_error_code: str = "workspace_session.unavailable",
) -> WorkspaceSessionContext:
    """Restore the full Workspace principal with optional CSRF verification."""

    try:
        with database_operation_deadline(monotonic_deadline):
            connection = _connect(config)
    except (ApiError, sqlite3.Error) as exc:
        if monotonic_deadline is not None:
            raise ApiError(deadline_error_code) from exc
        raise
    try:
        with authentication_database_deadline(
            connection, monotonic_deadline=monotonic_deadline, error_code=deadline_error_code,
        ):
            connection.execute("BEGIN IMMEDIATE")
            context, _row = _authenticate_workspace(
                connection,
                config,
                session_token=session_token,
                csrf_token=csrf_token,
                touch=True,
            )
            connection.commit()
            return context
    except ApiError as exc:
        _finish_workspace_error(connection, exc)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        if connection.in_transaction:
            connection.rollback()
        raise ApiError(
            deadline_error_code
            if monotonic_deadline is not None else "workspace_session.unavailable"
        ) from None
    finally:
        connection.close()


def authenticate_workspace_session_in_transaction(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str | None = None,
    touch: bool = False,
    read_only: bool = False,
) -> WorkspaceSessionContext:
    """Authenticate one WorkspaceSession inside a caller-owned transaction."""

    context, _row = _authenticate_workspace(
        connection,
        config,
        session_token=session_token,
        csrf_token=csrf_token,
        touch=touch and not read_only,
        read_only=read_only,
    )
    return context


def workspace_actor_permissions_in_transaction(
    connection: sqlite3.Connection,
    *,
    account_id: str,
    workspace_id: str,
    tenant_id: str,
) -> WorkspaceActorPermissionSnapshot:
    """Reload one active human Workspace actor without creating or touching a Session."""

    row = _switch_target(connection, account_id, workspace_id)
    if row is None or row["tenant_id"] != tenant_id:
        raise ApiError("workspace_session.permission_denied")
    snapshot = _permission_snapshot(connection, row)
    return WorkspaceActorPermissionSnapshot(
        account_id=str(row["account_id"]),
        workspace_id=str(row["workspace_id"]),
        tenant_id=str(row["tenant_id"]),
        membership_role=str(row["role_code"]),
        permission_codes=snapshot.permission_codes,
        permission_revision=snapshot.permission_revision,
        authorization_fingerprint=snapshot.authorization_fingerprint,
    )


def workspace_actor_authorization_probe_in_transaction(
    connection: sqlite3.Connection,
    *,
    account_id: str,
    workspace_id: str,
    tenant_id: str,
    permission_codes: tuple[str, ...],
) -> WorkspaceActorPermissionSnapshot:
    """Revalidate one delegated actor without rebuilding the full RBAC catalog."""

    if (
        not permission_codes
        or len(permission_codes) != len(set(permission_codes))
        or any(code not in PERMISSION_BY_CODE for code in permission_codes)
    ):
        raise ApiError("workspace_session.unavailable")
    row = _switch_target(
        connection,
        account_id,
        workspace_id,
        require_operational_state=True,
    )
    if row is None or row["tenant_id"] != tenant_id:
        raise ApiError("workspace_session.permission_denied")
    placeholders = ",".join("?" for _code in permission_codes)
    role_rows = connection.execute(
        f"""SELECT role.role_id,role.tenant_id,role.code,role.status,
                   role.permission_revision,role.matrix_sha256,
                   permission.code granted_permission_code
              FROM roles role
              JOIN role_permissions assignment
                ON assignment.tenant_id=role.tenant_id
               AND assignment.role_id=role.role_id
              JOIN permissions permission
                ON permission.tenant_id=assignment.tenant_id
               AND permission.permission_id=assignment.permission_id
             WHERE role.tenant_id=? AND role.code=?
               AND permission.status='active'
               AND permission.code IN ({placeholders})""",
        (tenant_id, row["legacy_role"], *permission_codes),
    ).fetchall()
    if not role_rows:
        raise ApiError("workspace_session.permission_denied")
    role = role_rows[0]
    revision = role["permission_revision"]
    matrix_sha256 = role["matrix_sha256"]
    if (
        any(
            item["role_id"] != role["role_id"]
            or item["tenant_id"] != role["tenant_id"]
            or item["code"] != role["code"]
            or item["status"] != role["status"]
            or item["permission_revision"] != revision
            or item["matrix_sha256"] != matrix_sha256
            for item in role_rows
        )
        or role["tenant_id"] != tenant_id
        or role["code"] != row["legacy_role"]
        or role["status"] != "active"
        or type(revision) is not int
        or revision < 1
        or type(matrix_sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", matrix_sha256) is None
    ):
        raise ApiError("workspace_session.unavailable")
    granted = {str(item["granted_permission_code"]) for item in role_rows}
    if granted != set(permission_codes):
        raise ApiError("workspace_session.permission_denied")
    (
        governance,
        machine_identity,
        provider_catalog,
        edge,
        migration,
        customer_support,
        privacy,
        compliance,
        assurance,
        branding,
        provider_engineering,
        commercial,
        console,
        navigation,
    ) = _workspace_extension_permissions(
        connection,
        role_code=str(row["role_code"]),
        require_catalogs=True,
    )
    legacy_fingerprint = authorization_fingerprint(
        tenant_id=tenant_id,
        user_id=str(row["legacy_user_id"]),
        role=str(row["legacy_role"]),
        permission_revision=revision,
        matrix_sha256=matrix_sha256,
    )
    return WorkspaceActorPermissionSnapshot(
        account_id=str(row["account_id"]),
        workspace_id=str(row["workspace_id"]),
        tenant_id=str(row["tenant_id"]),
        membership_role=str(row["role_code"]),
        permission_codes=tuple(sorted(permission_codes)),
        permission_revision=revision,
        authorization_fingerprint=_workspace_authorization_fingerprint(
            legacy_fingerprint=legacy_fingerprint,
            membership_role=str(row["role_code"]),
            governance_permissions=governance,
            machine_identity_permissions=machine_identity,
            provider_catalog_permissions=provider_catalog,
            edge_permissions=edge,
            migration_permissions=migration,
            customer_support_permissions=customer_support,
            privacy_permissions=privacy,
            compliance_permissions=compliance,
            assurance_permissions=assurance,
            branding_permissions=branding,
            provider_engineering_permissions=provider_engineering,
            commercial_permissions=commercial,
            console_permissions=console,
            navigation_permissions=navigation,
        ),
    )


def issue_federated_workspace_session_in_transaction(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    global_session_id: str,
    global_expires_at: datetime,
    account_id: str,
    workspace_id: str,
    sso_connection_id: str,
    now: datetime,
    request_id: str,
    trace_id: str,
) -> tuple[str, str, str]:
    """Issue a WorkspaceSession after a verified federation callback in the caller transaction."""

    if not connection.in_transaction:
        raise ApiError("workspace_session.unavailable")
    row = _switch_target(connection, account_id, workspace_id)
    if row is None:
        raise ApiError("sso.login_failed")
    policy = connection.execute(
        "SELECT mode,active_connection_id FROM sso_policies WHERE workspace_id=? AND tenant_id=?",
        (row["workspace_id"], row["tenant_id"]),
    ).fetchone()
    if (
        policy is not None
        and policy["mode"] == "enforced"
        and policy["active_connection_id"] != sso_connection_id
    ):
        raise ApiError("sso.login_failed")
    snapshot = _permission_snapshot(connection, row)
    raw_token = secrets.token_urlsafe(32)
    raw_csrf = secrets.token_urlsafe(32)
    session_id = _id("wss")
    absolute_expires = min(
        now + timedelta(seconds=config.workspace_session_ttl_seconds),
        global_expires_at,
    )
    stamp = _db_time(now)
    connection.execute(
        """
        INSERT INTO workspace_sessions(
          workspace_session_id,global_session_id,account_id,workspace_id,tenant_id,
          membership_id,token_hmac,csrf_hmac,status,membership_revision,
          workspace_revision,account_session_revision,legacy_permission_revision,
          matrix_fingerprint,created_at,last_seen_at,absolute_expires_at,
          idle_expires_at,revoked_at,revoked_reason,updated_at)
        VALUES(?,?,?,?,?,?,?,?, 'active',?,?,?,?,?,?,?,?,?,NULL,NULL,?)
        """,
        (
            session_id,
            global_session_id,
            account_id,
            row["workspace_id"],
            row["tenant_id"],
            row["workspace_membership_id"],
            _session_hmac(config, "token", raw_token),
            _session_hmac(config, "csrf", raw_csrf),
            row["membership_revision"],
            row["workspace_revision"],
            row["account_session_revision"],
            snapshot.permission_revision,
            snapshot.authorization_fingerprint,
            stamp,
            stamp,
            _db_time(absolute_expires),
            _db_time(absolute_expires),
            stamp,
        ),
    )
    _write_event(
        connection,
        workspace_id=row["workspace_id"],
        tenant_id=row["tenant_id"],
        actor_account_id=account_id,
        subject_membership_id=row["workspace_membership_id"],
        event_type="workspace.switched",
        reason_code="federated_identity",
        request_id=request_id,
        trace_id=trace_id,
        payload={
            "schema_version": 1,
            "role_code": row["role_code"],
            "membership_revision": row["membership_revision"],
        },
        now=now,
    )
    return raw_token, raw_csrf, session_id


def list_memberships(
    config: AppConfig,
    *,
    session_token: str,
    limit: int,
    offset: int,
    request_id: str,
    trace_id: str,
) -> MembershipListResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        context, _session = _authenticate_workspace(
            connection,
            config,
            session_token=session_token,
            csrf_token=None,
            touch=True,
        )
        manager = context.membership_role in _MANAGER_ROLES
        owner_filter = (
            "member.workspace_id=?"
            if manager
            else ("member.workspace_id=? AND member.workspace_membership_id=?")
        )
        owner_values: tuple[object, ...] = (
            (context.workspace_id,) if manager else (context.workspace_id, context.membership_id)
        )
        total = int(
            connection.execute(
                f"SELECT COUNT(*) FROM workspace_memberships member WHERE {owner_filter}",
                owner_values,
            ).fetchone()[0]
        )
        rows = connection.execute(
            f"""
            SELECT member.*,account.display_name
            FROM workspace_memberships member
            JOIN global_accounts account ON account.account_id=member.account_id
            WHERE {owner_filter}
            ORDER BY CASE member.role_code WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END,
              account.display_name,member.workspace_membership_id
            LIMIT ? OFFSET ?
            """,
            (*owner_values, limit, offset),
        ).fetchall()
        connection.commit()
        return MembershipListResponse(
            memberships=tuple(_membership_summary(row, context) for row in rows),
            limit=limit,
            offset=offset,
            total=total,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError as exc:
        _finish_workspace_error(connection, exc)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("membership.unavailable") from None
    finally:
        connection.close()


def update_membership(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    membership_id: str,
    expected_revision: int,
    role: str | None,
    status: str | None,
    request_id: str,
    trace_id: str,
) -> MembershipResponse:
    if not _MEMBERSHIP_ID_RE.fullmatch(membership_id):
        raise ApiError("membership.not_found")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context, _session = _authenticate_workspace(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=False,
        )
        _require_manager(context)
        target = _membership_row(connection, context.workspace_id, membership_id)
        if target is None:
            raise ApiError("membership.not_found")
        if int(target["revision"]) != expected_revision:
            raise ApiError("membership.revision_conflict")
        if target["role_code"] == "owner" or target["role_code"] == "service_account":
            raise ApiError("membership.update_invalid")
        next_role = target["role_code"] if role is None else _delegable_role(role)
        next_status = target["status"] if status is None else _membership_status(status)
        if context.membership_role == "admin" and next_role not in _DELEGABLE_ROLES:
            raise ApiError("workspace_session.permission_denied")
        if next_role == target["role_code"] and next_status == target["status"]:
            raise ApiError("membership.update_invalid")
        now = _utcnow()
        stamp = _db_time(now)
        legacy_role = _LEGACY_ROLE_BY_WORKSPACE_ROLE[next_role]
        connection.execute(
            """
            UPDATE users SET role=?,status=?,updated_at=?
            WHERE tenant_id=? AND user_id=?
            """,
            (
                legacy_role,
                "active" if next_status == "active" else "disabled",
                stamp,
                context.tenant_id,
                target["legacy_user_id"],
            ),
        )
        changed = connection.execute(
            """
            UPDATE workspace_memberships
            SET role_code=?,status=?,revision=revision+1,updated_by_account_id=?,updated_at=?
            WHERE workspace_id=? AND workspace_membership_id=? AND revision=?
            """,
            (
                next_role,
                next_status,
                context.account_id,
                stamp,
                context.workspace_id,
                membership_id,
                expected_revision,
            ),
        ).rowcount
        if changed != 1:
            raise ApiError("membership.revision_conflict")
        event_type = "membership.disabled" if next_status == "disabled" else "membership.updated"
        _write_event(
            connection,
            workspace_id=context.workspace_id,
            tenant_id=context.tenant_id,
            actor_account_id=context.account_id,
            subject_membership_id=membership_id,
            event_type=event_type,
            reason_code="member_admin_update",
            request_id=request_id,
            trace_id=trace_id,
            payload={
                "schema_version": 1,
                "role_code": next_role,
                "status": next_status,
                "membership_revision": expected_revision + 1,
            },
            now=now,
        )
        updated = _membership_row(connection, context.workspace_id, membership_id)
        if updated is None:
            raise ApiError("membership.unavailable")
        response = MembershipResponse(
            membership=_membership_summary(updated, context),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError as exc:
        _finish_workspace_error(connection, exc)
        raise
    except sqlite3.IntegrityError:
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("membership.update_invalid") from None
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("membership.unavailable") from None
    finally:
        connection.close()


def create_membership_invitation(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    email: str,
    role: str,
    expires_in_hours: int,
    request_id: str,
    trace_id: str,
) -> MembershipInvitationResponse:
    normalized_email = normalize_global_email(email)
    if normalized_email is None:
        raise ApiError("membership.invitation_invalid")
    target_role = _delegable_role(role, error_code="membership.invitation_invalid")
    if type(expires_in_hours) is not int or not 1 <= expires_in_hours <= 168:
        raise ApiError("membership.invitation_invalid")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context, _session = _authenticate_workspace(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=False,
        )
        _require_manager(context)
        if context.membership_role == "admin" and target_role not in _DELEGABLE_ROLES:
            raise ApiError("workspace_session.permission_denied")
        now = _utcnow()
        stamp = _db_time(now)
        expires_at = now + timedelta(hours=expires_in_hours)
        expires_text = _db_time(expires_at)
        email_hmac = _membership_email_hmac(config, normalized_email)
        connection.execute(
            """
            UPDATE membership_invitations SET status='expired',updated_at=?
            WHERE workspace_id=? AND target_email_hmac=? AND status='pending'
              AND unixepoch(expires_at)<=unixepoch(?)
            """,
            (stamp, context.workspace_id, email_hmac, stamp),
        )
        if (
            connection.execute(
                """
            SELECT 1 FROM membership_invitations
            WHERE workspace_id=? AND target_email_hmac=? AND status='pending'
            """,
                (context.workspace_id, email_hmac),
            ).fetchone()
            is not None
        ):
            raise ApiError("membership.invitation_conflict")
        if (
            connection.execute(
                """
            SELECT 1 FROM global_accounts account
            JOIN workspace_memberships member ON member.account_id=account.account_id
            WHERE member.workspace_id=? AND lower(account.email_normalized)=?
            """,
                (context.workspace_id, normalized_email),
            ).fetchone()
            is not None
        ):
            raise ApiError("membership.invitation_conflict")
        raw_token = secrets.token_urlsafe(32)
        token_digest = hash_token(raw_token)
        legacy_invitation_id = _id("inv")
        invitation_id = _id("min")
        legacy_role = _LEGACY_ROLE_BY_WORKSPACE_ROLE[target_role]
        connection.execute(
            """
            INSERT INTO invitations(
              invitation_id,tenant_id,email,email_fingerprint,role_code,token_hash,status,
              invited_by,accepted_user_id,expires_at,accepted_at,created_at,updated_at)
            VALUES(?,?,?,?,?,?,'pending',?,NULL,?,NULL,?,?)
            """,
            (
                legacy_invitation_id,
                context.tenant_id,
                normalized_email,
                _legacy_email_fingerprint(legacy_invitation_id, normalized_email),
                legacy_role,
                token_digest,
                context.legacy_user_id,
                expires_text,
                stamp,
                stamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO membership_invitations(
              membership_invitation_id,workspace_id,tenant_id,legacy_invitation_id,
              target_email_hmac,target_role_code,inviter_membership_id,
              inviter_membership_revision,token_hash,status,target_account_id,
              accepted_membership_id,expires_at,accepted_at,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,'pending',NULL,NULL,?,NULL,?,?)
            """,
            (
                invitation_id,
                context.workspace_id,
                context.tenant_id,
                legacy_invitation_id,
                email_hmac,
                target_role,
                context.membership_id,
                _current_membership_revision(connection, context.membership_id),
                token_digest,
                expires_text,
                stamp,
                stamp,
            ),
        )
        _write_event(
            connection,
            workspace_id=context.workspace_id,
            tenant_id=context.tenant_id,
            actor_account_id=context.account_id,
            subject_membership_id=None,
            event_type="membership.invited",
            reason_code="workspace_invitation_created",
            request_id=request_id,
            trace_id=trace_id,
            payload={"schema_version": 1, "role_code": target_role},
            now=now,
        )
        connection.commit()
        return MembershipInvitationResponse(
            invitation=MembershipInvitationSummary(
                invitation_id=invitation_id,
                role=target_role,
                expires_at=expires_at,
            ),
            accept_token=raw_token,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError as exc:
        _finish_workspace_error(connection, exc)
        raise
    except sqlite3.IntegrityError:
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("membership.invitation_conflict") from None
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("membership.unavailable") from None
    finally:
        connection.close()


def transfer_owner(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    target_membership_id: str,
    current_password: str,
    request_id: str,
    trace_id: str,
) -> OwnerTransferResponse:
    if not _MEMBERSHIP_ID_RE.fullmatch(target_membership_id) or not current_password:
        raise ApiError("owner_transfer.request_invalid")
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        initial, _row = _authenticate_workspace(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=False,
        )
        if initial.membership_role != "owner":
            raise ApiError("workspace_session.permission_denied")
        credential = connection.execute(
            """
            SELECT secret_hash FROM account_credentials
            WHERE account_id=? AND credential_type='password' AND status='active'
            """,
            (initial.account_id,),
        ).fetchone()
        if credential is None:
            raise ApiError("owner_transfer.reauthentication_failed")
        expected_hash = str(credential["secret_hash"])
        connection.rollback()
        if not verify_password(current_password, expected_hash):
            raise ApiError("owner_transfer.reauthentication_failed")

        connection.execute("BEGIN IMMEDIATE")
        context, _row = _authenticate_workspace(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=False,
        )
        if context.membership_role != "owner":
            raise ApiError("owner_transfer.conflict")
        account = connection.execute(
            """
            SELECT account.mfa_state,credential.secret_hash
            FROM global_accounts account
            JOIN account_credentials credential ON credential.account_id=account.account_id
              AND credential.credential_type='password' AND credential.status='active'
            WHERE account.account_id=?
            """,
            (context.account_id,),
        ).fetchone()
        if account is None or not hmac.compare_digest(str(account["secret_hash"]), expected_hash):
            raise ApiError("owner_transfer.conflict")
        if account["mfa_state"] != "not_enrolled":
            raise ApiError("owner_transfer.mfa_required")
        source = _membership_row(connection, context.workspace_id, context.membership_id)
        target = _membership_row(connection, context.workspace_id, target_membership_id)
        if (
            source is None
            or target is None
            or source["role_code"] != "owner"
            or source["status"] != "active"
            or target["status"] != "active"
            or target["role_code"] in {"owner", "service_account"}
        ):
            raise ApiError("owner_transfer.conflict")
        workspace = connection.execute(
            "SELECT revision FROM workspaces WHERE workspace_id=? AND status='active'",
            (context.workspace_id,),
        ).fetchone()
        if workspace is None:
            raise ApiError("owner_transfer.conflict")
        now = _utcnow()
        stamp = _db_time(now)
        transfer_id = _id("owt")
        connection.execute(
            """
            INSERT INTO owner_transfers(
              owner_transfer_id,workspace_id,tenant_id,source_membership_id,
              target_membership_id,source_membership_revision,target_membership_revision,
              workspace_revision,status,reauthenticated_at,request_id,reason_code,
              completed_at,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?, 'pending',?,?,?,NULL,?,?)
            """,
            (
                transfer_id,
                context.workspace_id,
                context.tenant_id,
                context.membership_id,
                target_membership_id,
                source["revision"],
                target["revision"],
                workspace["revision"],
                stamp,
                request_id,
                "owner_reauthenticated",
                stamp,
                stamp,
            ),
        )
        target_changed = connection.execute(
            """
            UPDATE workspace_memberships
            SET role_code='owner',revision=revision+1,updated_by_account_id=?,updated_at=?
            WHERE workspace_membership_id=? AND workspace_id=? AND revision=?
              AND status='active' AND role_code!='owner' AND role_code!='service_account'
            """,
            (
                context.account_id,
                stamp,
                target_membership_id,
                context.workspace_id,
                target["revision"],
            ),
        ).rowcount
        if target_changed != 1:
            raise ApiError("owner_transfer.conflict")
        legacy_target_changed = connection.execute(
            """
            UPDATE users SET role='admin',updated_at=?
            WHERE tenant_id=? AND user_id=? AND status='active'
            """,
            (stamp, context.tenant_id, target["legacy_user_id"]),
        ).rowcount
        if legacy_target_changed != 1:
            raise ApiError("owner_transfer.conflict")
        source_changed = connection.execute(
            """
            UPDATE workspace_memberships
            SET role_code='admin',revision=revision+1,updated_by_account_id=?,updated_at=?
            WHERE workspace_membership_id=? AND workspace_id=? AND revision=?
              AND status='active' AND role_code='owner'
            """,
            (
                context.account_id,
                stamp,
                context.membership_id,
                context.workspace_id,
                source["revision"],
            ),
        ).rowcount
        if source_changed != 1:
            raise ApiError("owner_transfer.conflict")
        workspace_changed = connection.execute(
            """
            UPDATE workspaces SET revision=revision+1,updated_at=?
            WHERE workspace_id=? AND revision=? AND status='active'
            """,
            (stamp, context.workspace_id, workspace["revision"]),
        ).rowcount
        if workspace_changed != 1:
            raise ApiError("owner_transfer.conflict")
        completed = connection.execute(
            """
            UPDATE owner_transfers SET status='completed',completed_at=?,updated_at=?
            WHERE owner_transfer_id=? AND status='pending'
            """,
            (stamp, stamp, transfer_id),
        ).rowcount
        if completed != 1:
            raise ApiError("owner_transfer.conflict")
        _write_event(
            connection,
            workspace_id=context.workspace_id,
            tenant_id=context.tenant_id,
            actor_account_id=context.account_id,
            subject_membership_id=target_membership_id,
            event_type="workspace.owner_transferred",
            reason_code="owner_reauthenticated",
            request_id=request_id,
            trace_id=trace_id,
            payload={
                "schema_version": 1,
                "source_revision": int(source["revision"]) + 1,
                "target_revision": int(target["revision"]) + 1,
            },
            now=now,
        )
        source_updated = _membership_row(connection, context.workspace_id, context.membership_id)
        target_updated = _membership_row(connection, context.workspace_id, target_membership_id)
        if source_updated is None or target_updated is None:
            raise ApiError("owner_transfer.unavailable")
        response = OwnerTransferResponse(
            source=_membership_summary(source_updated, context),
            target=_membership_summary(target_updated, context),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError as exc:
        _finish_workspace_error(connection, exc)
        raise
    except sqlite3.IntegrityError:
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("owner_transfer.conflict") from None
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("owner_transfer.unavailable") from None
    finally:
        connection.close()


def bridge_legacy_invitation_in_transaction(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    legacy_invitation_id: str,
    tenant_id: str,
    inviter_user_id: str,
    target_email: str,
    legacy_role: str,
    token_hash: str,
    expires_at: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> bool:
    if not _table_exists(connection, "membership_invitations"):
        return False
    inviter = connection.execute(
        """
        SELECT member.* FROM workspace_memberships member
        JOIN workspaces workspace ON workspace.workspace_id=member.workspace_id
        WHERE member.tenant_id=? AND member.legacy_user_id=?
          AND member.status='active' AND member.role_code IN('owner','admin')
          AND workspace.status='active'
        """,
        (tenant_id, inviter_user_id),
    ).fetchone()
    target_role = _WORKSPACE_ROLE_BY_LEGACY_ROLE.get(legacy_role)
    if inviter is None or target_role not in _DELEGABLE_ROLES:
        raise ApiError("invitation.create_invalid")
    normalized_email = normalize_global_email(target_email)
    if normalized_email is None:
        raise ApiError("invitation.create_invalid")
    connection.execute(
        """
        INSERT INTO membership_invitations(
          membership_invitation_id,workspace_id,tenant_id,legacy_invitation_id,
          target_email_hmac,target_role_code,inviter_membership_id,
          inviter_membership_revision,token_hash,status,target_account_id,
          accepted_membership_id,expires_at,accepted_at,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,'pending',NULL,NULL,?,NULL,?,?)
        """,
        (
            _id("min"),
            inviter["workspace_id"],
            tenant_id,
            legacy_invitation_id,
            _membership_email_hmac(config, normalized_email),
            target_role,
            inviter["workspace_membership_id"],
            inviter["revision"],
            token_hash,
            expires_at,
            _db_time(now),
            _db_time(now),
        ),
    )
    _write_event(
        connection,
        workspace_id=inviter["workspace_id"],
        tenant_id=tenant_id,
        actor_account_id=inviter["account_id"],
        subject_membership_id=None,
        event_type="membership.invited",
        reason_code="legacy_invitation_bridge",
        request_id=request_id,
        trace_id=trace_id,
        payload={"schema_version": 1, "role_code": target_role},
        now=now,
    )
    return True


def workspace_access_schema_available_in_transaction(connection: sqlite3.Connection) -> bool:
    """Report whether PRD-094 tables are available to a versioned legacy transaction."""

    return _table_exists(connection, "membership_invitations")


def consume_verified_invitation_in_transaction(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    legacy_invitation_id: str,
    account_id: str,
    email: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> bool:
    return _consume_invitation(
        connection,
        config,
        legacy_invitation_id=legacy_invitation_id,
        account_id=account_id,
        email=email,
        existing_user_id=None,
        require_identity_binding=True,
        request_id=request_id,
        trace_id=trace_id,
        now=now,
    )


def consume_legacy_invitation_in_transaction(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    legacy_invitation_id: str,
    account_id: str,
    email: str,
    existing_user_id: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> bool:
    return _consume_invitation(
        connection,
        config,
        legacy_invitation_id=legacy_invitation_id,
        account_id=account_id,
        email=email,
        existing_user_id=existing_user_id,
        require_identity_binding=False,
        request_id=request_id,
        trace_id=trace_id,
        now=now,
    )


def _consume_invitation(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    legacy_invitation_id: str,
    account_id: str,
    email: str,
    existing_user_id: str | None,
    require_identity_binding: bool,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> bool:
    if not _table_exists(connection, "membership_invitations"):
        return False
    normalized_email = normalize_global_email(email)
    if normalized_email is None:
        return False
    invitation = connection.execute(
        """
        SELECT membership_invitation.*,legacy.status legacy_status,
          inviter.status inviter_status,inviter.role_code inviter_role,
          inviter.revision inviter_current_revision,inviter.account_id inviter_account_id,
          workspace.status workspace_status
        FROM membership_invitations membership_invitation
        JOIN invitations legacy
          ON legacy.tenant_id=membership_invitation.tenant_id
         AND legacy.invitation_id=membership_invitation.legacy_invitation_id
        JOIN workspace_memberships inviter
          ON inviter.workspace_membership_id=membership_invitation.inviter_membership_id
        JOIN workspaces workspace ON workspace.workspace_id=membership_invitation.workspace_id
        WHERE membership_invitation.legacy_invitation_id=?
          AND membership_invitation.status='pending'
        """,
        (legacy_invitation_id,),
    ).fetchone()
    if (
        invitation is None
        or invitation["workspace_status"] != "active"
        or invitation["inviter_status"] != "active"
        or invitation["inviter_role"] not in _MANAGER_ROLES
        or invitation["inviter_current_revision"] != invitation["inviter_membership_revision"]
        or _parse_time(invitation["expires_at"]) <= now
        or not hmac.compare_digest(
            invitation["target_email_hmac"],
            _membership_email_hmac(config, normalized_email),
        )
    ):
        return False
    if require_identity_binding:
        binding = connection.execute(
            """
            SELECT * FROM invitation_identity_bindings
            WHERE invitation_id=? AND account_id=?
              AND status='verified_pending_membership'
            """,
            (legacy_invitation_id, account_id),
        ).fetchone()
        if binding is None or invitation["legacy_status"] != "pending":
            return False
    else:
        binding = None
        if invitation["legacy_status"] != "accepted":
            return False
    if (
        connection.execute(
            "SELECT 1 FROM workspace_memberships WHERE workspace_id=? AND account_id=?",
            (invitation["workspace_id"], account_id),
        ).fetchone()
        is not None
    ):
        return False
    stamp = _db_time(now)
    user_id = existing_user_id
    if user_id is None:
        account = connection.execute(
            """
            SELECT account.account_id,account.email_normalized,account.display_name,
              account.credential_revision,credential.secret_hash
            FROM global_accounts account
            JOIN account_credentials credential ON credential.account_id=account.account_id
              AND credential.credential_type='password' AND credential.status='active'
            WHERE account.account_id=? AND account.status='active' AND account.login_enabled=1
            """,
            (account_id,),
        ).fetchone()
        if account is None or not hmac.compare_digest(
            account["email_normalized"], normalized_email
        ):
            return False
        user_id = _id("usr")
        claim_id = _id("wpc")
        connection.execute(
            """
            INSERT INTO workspace_user_projection_claims(
              workspace_user_projection_claim_id,workspace_id,tenant_id,account_id,
              legacy_user_id,credential_revision,email_normalized,status,
              expires_at,consumed_at,created_at)
            VALUES(?,?,?,?,?,?,?,'pending',?,NULL,?)
            """,
            (
                claim_id,
                invitation["workspace_id"],
                invitation["tenant_id"],
                account_id,
                user_id,
                account["credential_revision"],
                normalized_email,
                _db_time(now + timedelta(minutes=5)),
                stamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,
              created_at,updated_at,failed_login_count,locked_until,last_login_at,
              password_changed_at)
            VALUES(?,?,?,?,?,?,'active',?,?,0,NULL,NULL,?)
            """,
            (
                user_id,
                invitation["tenant_id"],
                normalized_email,
                account["display_name"],
                account["secret_hash"],
                _LEGACY_ROLE_BY_WORKSPACE_ROLE[invitation["target_role_code"]],
                stamp,
                stamp,
                stamp,
            ),
        )
        claim = connection.execute(
            """
            SELECT status FROM workspace_user_projection_claims
            WHERE workspace_user_projection_claim_id=?
            """,
            (claim_id,),
        ).fetchone()
        if claim is None or claim["status"] != "consumed":
            raise sqlite3.IntegrityError("membership_projection_claim_not_consumed")
    link = connection.execute(
        """
        SELECT 1 FROM account_identity_links
        WHERE account_id=? AND legacy_tenant_id=? AND legacy_user_id=? AND status='active'
        """,
        (account_id, invitation["tenant_id"], user_id),
    ).fetchone()
    if link is None:
        return False
    membership_id = _id("wmb")
    connection.execute(
        """
        INSERT INTO workspace_memberships(
          workspace_membership_id,workspace_id,tenant_id,account_id,legacy_user_id,
          role_code,status,source,revision,created_by_account_id,updated_by_account_id,
          created_at,updated_at)
        VALUES(?,?,?,?,?,?,'active','membership_invitation',1,?,?,?,?)
        """,
        (
            membership_id,
            invitation["workspace_id"],
            invitation["tenant_id"],
            account_id,
            user_id,
            invitation["target_role_code"],
            invitation["inviter_account_id"],
            invitation["inviter_account_id"],
            stamp,
            stamp,
        ),
    )
    connection.execute(
        """
        UPDATE membership_invitations
        SET status='accepted',target_account_id=?,accepted_membership_id=?,
          accepted_at=?,updated_at=?
        WHERE membership_invitation_id=? AND status='pending'
        """,
        (account_id, membership_id, stamp, stamp, invitation["membership_invitation_id"]),
    )
    if require_identity_binding and binding is not None:
        connection.execute(
            """
            UPDATE invitation_identity_bindings
            SET status='consumed',revision=2,consumed_at=?
            WHERE invitation_identity_binding_id=?
              AND status='verified_pending_membership' AND revision=1
            """,
            (stamp, binding["invitation_identity_binding_id"]),
        )
        connection.execute(
            """
            UPDATE invitations
            SET status='accepted',accepted_user_id=?,accepted_at=?,updated_at=?
            WHERE tenant_id=? AND invitation_id=? AND status='pending'
            """,
            (user_id, stamp, stamp, invitation["tenant_id"], legacy_invitation_id),
        )
    _write_event(
        connection,
        workspace_id=invitation["workspace_id"],
        tenant_id=invitation["tenant_id"],
        actor_account_id=account_id,
        subject_membership_id=membership_id,
        event_type="invitation.accepted",
        reason_code="verified_identity_bound",
        request_id=request_id,
        trace_id=trace_id,
        payload={
            "schema_version": 1,
            "role_code": invitation["target_role_code"],
            "membership_revision": 1,
        },
        now=now,
    )
    return True


def _recovery_grant_for_switch(
    connection: sqlite3.Connection,
    *,
    global_session_id: str,
    account_id: str,
    workspace_id: str,
    tenant_id: str,
    now: datetime,
) -> sqlite3.Row | None:
    if not _table_exists(connection, "enterprise_identity_session_grants"):
        return None
    policy = connection.execute(
        "SELECT * FROM sso_policies WHERE workspace_id=? AND tenant_id=?",
        (workspace_id, tenant_id),
    ).fetchone()
    if policy is None or policy["mode"] != "enforced":
        return None
    grant = connection.execute(
        """SELECT * FROM enterprise_identity_session_grants
             WHERE global_session_id=? AND workspace_id=? AND tenant_id=? AND account_id=?
               AND grant_type='recovery' AND status='active'
               AND reason_code IN('idp_unavailable','identity_recovery')""",
        (global_session_id, workspace_id, tenant_id, account_id),
    ).fetchone()
    if (
        grant is None
        or policy["break_glass_account_id"] != account_id
        or _parse_time(grant["expires_at"]) <= now
    ):
        raise ApiError("sso.login_required")
    return grant


def _enterprise_workspace_session_authorized(
    connection: sqlite3.Connection, row: sqlite3.Row, now: datetime
) -> bool:
    if not _table_exists(connection, "enterprise_identity_session_grants"):
        return True
    policy = connection.execute(
        "SELECT * FROM sso_policies WHERE workspace_id=? AND tenant_id=?",
        (row["workspace_id"], row["tenant_id"]),
    ).fetchone()
    if policy is None or policy["mode"] != "enforced":
        return True
    grant = connection.execute(
        """SELECT 1 FROM enterprise_identity_session_grants grant_record
             WHERE grant_record.workspace_session_id=?
               AND grant_record.global_session_id=?
               AND grant_record.workspace_id=? AND grant_record.tenant_id=?
               AND grant_record.account_id=? AND grant_record.status='consumed'
               AND grant_record.expires_at>?
               AND ((grant_record.grant_type='federated'
                     AND grant_record.sso_connection_id=?)
                 OR (grant_record.grant_type='recovery'
                     AND grant_record.account_id=?))""",
        (
            row["workspace_session_id"],
            row["global_session_id"],
            row["workspace_id"],
            row["tenant_id"],
            row["account_id"],
            _db_time(now),
            policy["active_connection_id"],
            policy["break_glass_account_id"],
        ),
    ).fetchone()
    return grant is not None


def _authenticate_workspace(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str | None,
    touch: bool,
    read_only: bool = False,
) -> tuple[WorkspaceSessionContext, sqlite3.Row]:
    if not connection.in_transaction:
        raise ApiError("workspace_session.unavailable")
    if not valid_opaque_value(session_token):
        raise ApiError("workspace_session.invalid")
    now = _utcnow()
    if _table_exists(connection, "workspace_operational_states"):
        operational_select = "operational.status operational_status,"
        operational_join = (
            "LEFT JOIN workspace_operational_states operational "
            "ON operational.workspace_id=member.workspace_id "
            "AND operational.tenant_id=member.tenant_id"
        )
    else:
        operational_select = "'active' operational_status,"
        operational_join = ""
    row = connection.execute(
        f"""
        SELECT session.*,
          member.revision current_membership_revision,member.role_code,
          member.status membership_status,member.account_id membership_account_id,
          member.workspace_id membership_workspace_id,member.tenant_id membership_tenant_id,
          member.legacy_user_id,legacy_user.status user_status,legacy_user.role legacy_role,
          legacy_user.email,legacy_user.display_name,workspace.status workspace_status,
          workspace.revision current_workspace_revision,workspace.kind,
          COALESCE(organization.name,personal.display_label) workspace_name,
          organization.slug_normalized,binding.status binding_status,
          account.status account_status,account.login_enabled,
          account.session_revision current_account_session_revision,
          global_session.status global_session_status,
          global_session.account_id global_session_account_id,
          global_session.expires_at global_expires_at,
          global_session.idle_expires_at global_idle_expires_at,
          tenant.status tenant_status,tenant.slug tenant_slug,tenant.name tenant_name,
          {operational_select}
          1 operational_projection_version
        FROM workspace_sessions session
        JOIN workspace_memberships member ON member.workspace_membership_id=session.membership_id
        JOIN users legacy_user ON legacy_user.tenant_id=member.tenant_id
          AND legacy_user.user_id=member.legacy_user_id
        JOIN workspaces workspace ON workspace.workspace_id=member.workspace_id
        JOIN workspace_tenant_bindings binding
          ON binding.workspace_id=member.workspace_id AND binding.tenant_id=member.tenant_id
        JOIN global_accounts account ON account.account_id=member.account_id
        JOIN global_sessions global_session ON global_session.session_id=session.global_session_id
        JOIN tenants tenant ON tenant.tenant_id=member.tenant_id
        {operational_join}
        LEFT JOIN organization_profiles organization
          ON organization.workspace_id=workspace.workspace_id
        LEFT JOIN personal_profiles personal ON personal.workspace_id=workspace.workspace_id
        WHERE session.token_hmac=?
        """,
        (_session_hmac(config, "token", session_token),),
    ).fetchone()
    if row is None:
        raise ApiError("workspace_session.invalid")
    if row["status"] == "revoked":
        raise ApiError("workspace_session.revoked")
    if row["status"] == "expired":
        raise ApiError("workspace_session.expired")
    if (
        _parse_time(row["absolute_expires_at"]) <= now
        or _parse_time(row["idle_expires_at"]) <= now
        or _parse_time(row["global_expires_at"]) <= now
        or _parse_time(row["global_idle_expires_at"]) <= now
    ):
        if not read_only:
            connection.execute(
                """
                UPDATE workspace_sessions SET status='expired',updated_at=?
                WHERE workspace_session_id=?
                """,
                (_db_time(now), row["workspace_session_id"]),
            )
        raise ApiError("workspace_session.expired")
    compatible_workspace_roles = _WORKSPACE_ROLES_BY_LEGACY_ROLE.get(
        row["legacy_role"], frozenset()
    )
    enterprise_authorized = _enterprise_workspace_session_authorized(connection, row, now)
    from .conditional_access import session_satisfies_conditional_access_in_transaction

    conditional_access_authorized = session_satisfies_conditional_access_in_transaction(
        connection, row, now
    )
    stale = (
        row["membership_status"] != "active"
        or row["role_code"] == "service_account"
        or row["membership_account_id"] != row["account_id"]
        or row["membership_workspace_id"] != row["workspace_id"]
        or row["membership_tenant_id"] != row["tenant_id"]
        or row["workspace_status"] != "active"
        or row["binding_status"] != "active"
        or row["tenant_status"] != "active"
        or row["operational_status"] != "active"
        or row["account_status"] != "active"
        or row["login_enabled"] != 1
        or row["global_session_status"] != "active"
        or row["global_session_account_id"] != row["account_id"]
        or row["user_status"] != "active"
        or row["role_code"] not in compatible_workspace_roles
        or row["membership_revision"] != row["current_membership_revision"]
        or row["workspace_revision"] != row["current_workspace_revision"]
        or row["account_session_revision"] != row["current_account_session_revision"]
        or not enterprise_authorized
        or not conditional_access_authorized
    )
    if stale:
        if not read_only:
            _revoke_workspace_session(connection, row["workspace_session_id"], now, "authority_changed")
        raise ApiError("workspace_session.revoked")
    try:
        snapshot = _permission_snapshot(connection, row)
    except ApiError:
        if not read_only:
            _revoke_workspace_session(
                connection, row["workspace_session_id"], now, "permission_changed"
            )
        raise ApiError("workspace_session.revoked") from None
    if row["legacy_permission_revision"] != snapshot.permission_revision or not hmac.compare_digest(
        row["matrix_fingerprint"], snapshot.authorization_fingerprint
    ):
        if not read_only:
            _revoke_workspace_session(
                connection, row["workspace_session_id"], now, "permission_changed"
            )
        raise ApiError("workspace_session.revoked")
    if csrf_token is not None:
        if not valid_opaque_value(csrf_token) or not hmac.compare_digest(
            row["csrf_hmac"], _session_hmac(config, "csrf", csrf_token)
        ):
            raise ApiError("workspace_session.csrf_invalid")
    if touch and _parse_time(row["last_seen_at"]) <= now - timedelta(seconds=60):
        connection.execute(
            """
            UPDATE workspace_sessions SET last_seen_at=?,updated_at=?
            WHERE workspace_session_id=?
            """,
            (_db_time(now), _db_time(now), row["workspace_session_id"]),
        )
    context = WorkspaceSessionContext(
        workspace_session_id=row["workspace_session_id"],
        global_session_id=row["global_session_id"],
        account_id=row["account_id"],
        workspace_id=row["workspace_id"],
        tenant_id=row["tenant_id"],
        membership_id=row["membership_id"],
        legacy_user_id=row["legacy_user_id"],
        membership_role=row["role_code"],
        legacy_role=row["legacy_role"],
        account_email=row["email"],
        display_name=row["display_name"],
        tenant_slug=row["tenant_slug"],
        tenant_name=row["tenant_name"],
        permission_codes=snapshot.permission_codes,
        permission_revision=snapshot.permission_revision,
        authorization_fingerprint=snapshot.authorization_fingerprint,
        expires_at=_parse_time(row["absolute_expires_at"]),
        token=session_token,
        legacy_authorization_fingerprint=getattr(
            snapshot,
            "legacy_authorization_fingerprint",
            snapshot.authorization_fingerprint,
        ),
    )
    return context, row


def _switch_target(
    connection: sqlite3.Connection,
    account_id: str,
    workspace_id: str,
    *,
    require_operational_state: bool = False,
) -> sqlite3.Row | None:
    operational_filter = (
        "AND EXISTS(SELECT 1 FROM workspace_operational_states operational "
        "WHERE operational.workspace_id=member.workspace_id "
        "AND operational.tenant_id=member.tenant_id AND operational.status='active')"
        if require_operational_state or _table_exists(connection, "workspace_operational_states")
        else ""
    )
    row = connection.execute(
        f"""
        SELECT member.workspace_membership_id,member.workspace_id,member.tenant_id,
          member.account_id,member.legacy_user_id,member.role_code,
          member.revision membership_revision,workspace.revision workspace_revision,
          workspace.kind,COALESCE(organization.name,personal.display_label) workspace_name,
          organization.slug_normalized,account.session_revision account_session_revision,
          legacy_user.role legacy_role,legacy_user.status user_status,tenant.status tenant_status
        FROM workspace_memberships member
        JOIN workspaces workspace ON workspace.workspace_id=member.workspace_id
        JOIN workspace_tenant_bindings binding
          ON binding.workspace_id=member.workspace_id AND binding.tenant_id=member.tenant_id
        JOIN global_accounts account ON account.account_id=member.account_id
        JOIN users legacy_user ON legacy_user.tenant_id=member.tenant_id
          AND legacy_user.user_id=member.legacy_user_id
        JOIN tenants tenant ON tenant.tenant_id=member.tenant_id
        LEFT JOIN organization_profiles organization
          ON organization.workspace_id=workspace.workspace_id
        LEFT JOIN personal_profiles personal ON personal.workspace_id=workspace.workspace_id
        WHERE member.account_id=? AND member.workspace_id=? AND member.status='active'
          AND member.role_code!='service_account' AND workspace.status='active'
          AND binding.status='active' AND account.status='active' AND account.login_enabled=1
          AND legacy_user.status='active' AND tenant.status='active'
          {operational_filter}
        """,
        (account_id, workspace_id),
    ).fetchone()
    if row is None:
        return None
    if row["role_code"] not in _WORKSPACE_ROLES_BY_LEGACY_ROLE.get(
        row["legacy_role"], frozenset()
    ):
        return None
    return row


def _permission_snapshot(connection: sqlite3.Connection, row: sqlite3.Row):
    legacy = require_actor_runtime_permissions(
        connection,
        tenant_id=row["tenant_id"],
        user_id=row["legacy_user_id"],
        permission_codes=("console.basic.read",),
        unavailable_error_code="workspace_session.unavailable",
    )
    (
        governance,
        machine_identity,
        provider_catalog,
        edge,
        migration,
        customer_support,
        privacy,
        compliance,
        assurance,
        branding,
        provider_engineering,
        commercial,
        console,
        navigation,
    ) = _workspace_extension_permissions(
        connection,
        role_code=str(row["role_code"]),
    )
    if not any(
        (
            governance,
            machine_identity,
            provider_catalog,
            edge,
            migration,
            customer_support,
            privacy,
            compliance,
            assurance,
            branding,
            provider_engineering,
            commercial,
            console,
            navigation,
        )
    ):
        return legacy
    permissions = tuple(
        sorted(
            {
                *legacy.permission_codes,
                *governance,
                *machine_identity,
                *provider_catalog,
                *edge,
                *migration,
                *customer_support,
                *privacy,
                *compliance,
                *assurance,
                *branding,
                *provider_engineering,
                *commercial,
                *console,
                *navigation,
            }
        )
    )
    fingerprint = _workspace_authorization_fingerprint(
        legacy_fingerprint=legacy.authorization_fingerprint,
        membership_role=str(row["role_code"]),
        governance_permissions=governance,
        machine_identity_permissions=machine_identity,
        provider_catalog_permissions=provider_catalog,
        edge_permissions=edge,
        migration_permissions=migration,
        customer_support_permissions=customer_support,
        privacy_permissions=privacy,
        compliance_permissions=compliance,
        assurance_permissions=assurance,
        branding_permissions=branding,
        provider_engineering_permissions=provider_engineering,
        commercial_permissions=commercial,
        console_permissions=console,
        navigation_permissions=navigation,
    )
    return _WorkspacePermissionSnapshot(
        permission_codes=permissions,
        permission_revision=legacy.permission_revision,
        matrix_sha256=legacy.matrix_sha256,
        authorization_fingerprint=fingerprint,
        legacy_authorization_fingerprint=legacy.authorization_fingerprint,
    )


def _workspace_extension_permissions(
    connection: sqlite3.Connection,
    *,
    role_code: str,
    require_catalogs: bool = False,
) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    table_names = {
        table
        for family in _WORKSPACE_EXTENSION_FAMILIES
        for table in (family.catalog_table, family.assignments_table)
    }
    table_names.add("schema_migrations")
    placeholders = ",".join("?" for _ in table_names)
    try:
        table_catalog = (
            "SELECT relname FROM pg_catalog.pg_class "
            "WHERE relnamespace=current_schema()::regnamespace AND relkind IN ('r','p') "
            "AND relname IN "
            if getattr(connection, "backend", "sqlite") == "postgresql"
            else "SELECT name FROM sqlite_schema WHERE type='table' AND name IN "
        )
        existing_tables = {
            str(row[0])
            for row in connection.execute(
                f"{table_catalog}({placeholders})",
                tuple(sorted(table_names)),
            ).fetchall()
        }
        migration_versions = tuple(
            sorted({family.introduced_migration for family in _WORKSPACE_EXTENSION_FAMILIES})
        )
        migration_placeholders = ",".join("?" for _ in migration_versions)
        applied_migrations = (
            {
                str(row[0])
                for row in connection.execute(
                    "SELECT version FROM schema_migrations WHERE status='applied' "
                    f"AND version IN ({migration_placeholders})",
                    migration_versions,
                ).fetchall()
            }
            if "schema_migrations" in existing_tables
            else set()
        )

        available: list[_WorkspaceExtensionFamily] = []
        for family in _WORKSPACE_EXTENSION_FAMILIES:
            has_catalog = family.catalog_table in existing_tables
            has_assignments = family.assignments_table in existing_tables
            required = family.introduced_migration in applied_migrations or (
                require_catalogs and family.required_for_workspace_session
            )
            if has_catalog != has_assignments or (required and not has_catalog):
                raise ApiError("workspace_session.unavailable")
            if has_catalog:
                available.append(family)

        selects: list[str] = []
        family_by_key: dict[str, _WorkspaceExtensionFamily] = {}
        for index, family in enumerate(available):
            key = f"family_{index}"
            family_by_key[key] = family
            selects.extend(
                (
                    f"SELECT '{key}' family_key,'catalog' row_kind,'' role_code,"
                    f"permission_code FROM {family.catalog_table}",
                    f"SELECT '{key}' family_key,'assignment' row_kind,role_code,"
                    f"permission_code FROM {family.assignments_table}",
                )
            )
        rows = connection.execute(" UNION ALL ".join(selects)).fetchall() if selects else []
    except sqlite3.Error as exc:
        raise ApiError("workspace_session.unavailable") from exc

    catalogs = {key: set() for key in family_by_key}
    assignments = {key: set() for key in family_by_key}
    for row in rows:
        key = str(row["family_key"])
        if key not in family_by_key:
            raise ApiError("workspace_session.unavailable")
        permission_code = str(row["permission_code"])
        if row["row_kind"] == "catalog":
            catalogs[key].add(permission_code)
        elif row["row_kind"] == "assignment":
            assignments[key].add((str(row["role_code"]), permission_code))
        else:
            raise ApiError("workspace_session.unavailable")

    grants = {group: set() for group in _WORKSPACE_EXTENSION_GROUPS}
    for key, family in family_by_key.items():
        expected = {
            (assigned_role, permission_code)
            for assigned_role, permission_codes in family.expected_by_role.items()
            for permission_code in permission_codes
        }
        if catalogs[key] != {code for _, code in expected} or assignments[key] != expected:
            raise ApiError("workspace_session.unavailable")
        grants[family.group].update(
            code for assigned_role, code in expected if assigned_role == role_code
        )
    return tuple(tuple(sorted(grants[group])) for group in _WORKSPACE_EXTENSION_GROUPS)


def _workspace_authorization_fingerprint(
    *,
    legacy_fingerprint: str,
    membership_role: str,
    governance_permissions: tuple[str, ...],
    machine_identity_permissions: tuple[str, ...],
    provider_catalog_permissions: tuple[str, ...],
    edge_permissions: tuple[str, ...],
    migration_permissions: tuple[str, ...],
    customer_support_permissions: tuple[str, ...],
    privacy_permissions: tuple[str, ...],
    compliance_permissions: tuple[str, ...],
    assurance_permissions: tuple[str, ...],
    branding_permissions: tuple[str, ...],
    provider_engineering_permissions: tuple[str, ...],
    commercial_permissions: tuple[str, ...],
    console_permissions: tuple[str, ...],
    navigation_permissions: tuple[str, ...],
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "legacy": legacy_fingerprint,
                "membership_role": membership_role,
                "governance_permissions": governance_permissions,
                "machine_identity_permissions": machine_identity_permissions,
                "provider_catalog_permissions": provider_catalog_permissions,
                "edge_permissions": edge_permissions,
                "migration_permissions": migration_permissions,
                "customer_support_permissions": customer_support_permissions,
                "privacy_permissions": privacy_permissions,
                "compliance_permissions": compliance_permissions,
                "assurance_permissions": assurance_permissions,
                "branding_permissions": branding_permissions,
                "provider_engineering_permissions": provider_engineering_permissions,
                "commercial_permissions": commercial_permissions,
                "console_permissions": console_permissions,
                "navigation_permissions": navigation_permissions,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()


def _identity_workspace(row: sqlite3.Row) -> IdentityWorkspaceSummary:
    return IdentityWorkspaceSummary(
        workspace_id=row["workspace_id"],
        kind=row["kind"],
        name=row["display_name"],
        slug=row["slug_normalized"],
        role=row["role_code"],
    )


def _context_summary(row: sqlite3.Row, expires_at: datetime) -> WorkspaceContextSummary:
    return WorkspaceContextSummary(
        workspace_id=row["workspace_id"],
        kind=row["kind"],
        name=row["workspace_name"],
        slug=row["slug_normalized"],
        role=row["role_code"],
        expires_at=expires_at,
    )


def _membership_row(
    connection: sqlite3.Connection,
    workspace_id: str,
    membership_id: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT member.*,account.display_name
        FROM workspace_memberships member
        JOIN global_accounts account ON account.account_id=member.account_id
        WHERE member.workspace_id=? AND member.workspace_membership_id=?
        """,
        (workspace_id, membership_id),
    ).fetchone()


def _membership_summary(
    row: sqlite3.Row,
    context: WorkspaceSessionContext,
) -> MembershipSummary:
    can_manage = context.membership_role in _MANAGER_ROLES and row["role_code"] not in {
        "owner",
        "service_account",
    }
    return MembershipSummary(
        membership_id=row["workspace_membership_id"],
        display_name=row["display_name"],
        role=row["role_code"],
        status=row["status"],
        revision=int(row["revision"]),
        is_current=row["workspace_membership_id"] == context.membership_id,
        can_manage=can_manage,
    )


def _current_membership_revision(connection: sqlite3.Connection, membership_id: str) -> int:
    row = connection.execute(
        "SELECT revision FROM workspace_memberships WHERE workspace_membership_id=?",
        (membership_id,),
    ).fetchone()
    if row is None:
        raise ApiError("workspace_session.revoked")
    return int(row["revision"])


def _require_manager(context: WorkspaceSessionContext) -> None:
    if context.membership_role not in _MANAGER_ROLES:
        raise ApiError("workspace_session.permission_denied")


def _delegable_role(value: str, *, error_code: str = "membership.update_invalid") -> str:
    if type(value) is not str:
        raise ApiError(error_code)
    normalized = value.strip().lower()
    if normalized not in _DELEGABLE_ROLES:
        raise ApiError(error_code)
    return normalized


def _membership_status(value: str) -> str:
    if type(value) is not str:
        raise ApiError("membership.update_invalid")
    normalized = value.strip().lower()
    if normalized not in {"active", "disabled"}:
        raise ApiError("membership.update_invalid")
    return normalized


def _write_event(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    tenant_id: str,
    actor_account_id: str,
    subject_membership_id: str | None,
    event_type: str,
    reason_code: str,
    request_id: str,
    trace_id: str,
    payload: dict[str, object],
    now: datetime,
) -> None:
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    event_id = _id("mev")
    stamp = _db_time(now)
    connection.execute(
        """
        INSERT INTO membership_events(
          membership_event_id,workspace_id,tenant_id,actor_account_id,
          subject_membership_id,event_type,reason_code,request_id,trace_id,
          payload_json,payload_sha256,occurred_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id,
            workspace_id,
            tenant_id,
            actor_account_id,
            subject_membership_id,
            event_type,
            reason_code,
            request_id,
            trace_id,
            payload_json,
            payload_hash,
            stamp,
        ),
    )
    connection.execute(
        """
        INSERT INTO membership_event_outbox(
          membership_event_outbox_id,membership_event_id,status,attempt_count,
          next_attempt_at,delivered_at,created_at,updated_at)
        VALUES(?,?,'pending',0,?,NULL,?,?)
        """,
        (_id("mob"), event_id, stamp, stamp, stamp),
    )


def _revoke_workspace_session(
    connection: sqlite3.Connection,
    session_id: str,
    now: datetime,
    reason: str,
) -> None:
    stamp = _db_time(now)
    connection.execute(
        """
        UPDATE workspace_sessions
        SET status='revoked',revoked_at=?,revoked_reason=?,updated_at=?
        WHERE workspace_session_id=? AND status='active'
        """,
        (stamp, reason, stamp, session_id),
    )


def _session_hmac(config: AppConfig, purpose: str, value: str) -> str:
    payload = f"metahub-workspace-session-v1\x00{purpose}\x00{value}".encode("ascii")
    return hmac.new(
        config.workspace_session_secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()


def _membership_email_hmac(config: AppConfig, email: str) -> str:
    payload = f"metahub-membership-email-v1\x00{email}".encode()
    return hmac.new(
        config.workspace_session_secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()


def _legacy_email_fingerprint(invitation_id: str, email: str) -> str:
    return hashlib.sha256(f"{invitation_id}\x00{email}".encode()).hexdigest()


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return table_exists(connection, name)


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("workspace_session.unavailable") from None


def _finish_global_error(connection: sqlite3.Connection, error: ApiError) -> None:
    if not connection.in_transaction:
        return
    if error.code == "identity.session_expired":
        connection.commit()
    else:
        connection.rollback()


def _finish_workspace_error(connection: sqlite3.Connection, error: ApiError) -> None:
    if not connection.in_transaction:
        return
    if error.code in {"workspace_session.expired", "workspace_session.revoked"}:
        connection.commit()
    else:
        connection.rollback()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_time(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("invalid database time")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
