"""Verified human permission projections for PRD-120 private gateways."""

from __future__ import annotations

import sqlite3

from metahub_shared.runtime.errors import ApiError

EDGE_WORKSPACE_ROLE_PERMISSIONS = {
    "owner": ("edge.deployment.manage", "edge.deployment.read"),
    "admin": ("edge.deployment.manage", "edge.deployment.read"),
    "developer": ("edge.deployment.read",),
    "security_admin": ("edge.deployment.read",),
}
EDGE_TELEMETRY_WORKSPACE_ROLE_PERMISSIONS = {
    "owner": ("edge.telemetry.manage", "edge.telemetry.read", "edge.usage.read"),
    "admin": ("edge.telemetry.manage", "edge.telemetry.read", "edge.usage.read"),
    "security_admin": ("edge.telemetry.manage", "edge.telemetry.read"),
    "finance_admin": ("edge.usage.read",),
}
EDGE_SECRET_WORKSPACE_ROLE_PERMISSIONS = {
    "owner": ("edge.secret_reference.read",),
    "admin": ("edge.secret_reference.read",),
    "security_admin": ("edge.secret_reference.read",),
    "auditor": ("edge.secret_reference.read",),
}
EDGE_INSTALLER_WORKSPACE_ROLE_PERMISSIONS = {
    "owner": ("edge.installation.manage", "edge.installer.read"),
}
EDGE_VALIDATION_WORKSPACE_ROLE_PERMISSIONS = {
    "owner": (
        "edge.delivery.accept",
        "edge.support_bundle.read",
        "edge.validation.manage",
        "edge.validation.read",
    ),
    "admin": (
        "edge.delivery.accept",
        "edge.support_bundle.read",
        "edge.validation.manage",
        "edge.validation.read",
    ),
    "security_admin": ("edge.support_bundle.read", "edge.validation.read"),
    "auditor": ("edge.validation.read",),
}
EDGE_DIAGNOSTIC_WORKSPACE_ROLE_PERMISSIONS = {
    "owner": ("edge.diagnostic.approve", "edge.diagnostic.read", "edge.health.read"),
    "admin": ("edge.diagnostic.approve", "edge.diagnostic.read", "edge.health.read"),
    "developer": ("edge.health.read",),
    "security_admin": (
        "edge.diagnostic.approve",
        "edge.diagnostic.read",
        "edge.health.read",
    ),
    "auditor": ("edge.diagnostic.read", "edge.health.read"),
}
EDGE_PLATFORM_ROLE_PERMISSIONS = {
    "platform_owner": ("platform.edge_fleet.manage", "platform.edge_fleet.read"),
    "platform_ops": ("platform.edge_fleet.manage", "platform.edge_fleet.read"),
    "platform_security": ("platform.edge_fleet.read",),
}
EDGE_INSTALLER_PLATFORM_ROLE_PERMISSIONS = {
    "platform_owner": ("platform.edge_installer.read",),
    "platform_security": ("platform.edge_installer.read",),
    "release_manager": (
        "platform.edge_installer.approve",
        "platform.edge_installer.manage",
        "platform.edge_installer.read",
    ),
}

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


def resolve_verified_workspace_edge_role(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    legacy_user_id: str,
    legacy_role_code: str,
    unavailable_error_code: str,
) -> str:
    """Resolve Edge extensions from the active Workspace role, not the legacy role."""

    required_tables = (
        "workspace_memberships",
        "workspaces",
        "workspace_tenant_bindings",
    )
    try:
        available = tuple(_table_exists(connection, table) for table in required_tables)
        if not any(available):
            return legacy_role_code
        if not all(available):
            raise ApiError(unavailable_error_code)
        rows = connection.execute(
            """
            SELECT member.role_code
            FROM workspace_memberships member
            JOIN workspaces workspace ON workspace.workspace_id=member.workspace_id
            JOIN workspace_tenant_bindings binding
              ON binding.workspace_id=member.workspace_id
             AND binding.tenant_id=member.tenant_id
            WHERE member.tenant_id=? AND member.legacy_user_id=?
              AND member.status='active' AND member.role_code!='service_account'
              AND workspace.status='active' AND binding.status='active'
            """,
            (tenant_id, legacy_user_id),
        ).fetchall()
        if not rows:
            return ""
        if len(rows) != 1:
            raise ApiError(unavailable_error_code)
        workspace_role = str(rows[0][0])
        if _LEGACY_ROLE_BY_WORKSPACE_ROLE.get(workspace_role) != legacy_role_code:
            raise ApiError(unavailable_error_code)
        return workspace_role
    except sqlite3.Error as exc:
        raise ApiError(unavailable_error_code) from exc


def load_verified_workspace_edge_permissions(
    connection: sqlite3.Connection,
    *,
    role_code: str,
    unavailable_error_code: str,
) -> tuple[str, ...]:
    permissions = set(
        _load_verified_permissions(
            connection,
            role_code=role_code,
            expected_by_role=EDGE_WORKSPACE_ROLE_PERMISSIONS,
            catalog_table="workspace_edge_permission_extensions",
            assignments_table="workspace_edge_role_permissions",
            introduced_migration="0112",
            unavailable_error_code=unavailable_error_code,
        )
    )
    permissions.update(
        _load_verified_permissions(
            connection,
            role_code=role_code,
            expected_by_role=EDGE_TELEMETRY_WORKSPACE_ROLE_PERMISSIONS,
            catalog_table="workspace_edge_telemetry_permission_extensions",
            assignments_table="workspace_edge_telemetry_role_permissions",
            introduced_migration="0114",
            unavailable_error_code=unavailable_error_code,
        )
    )
    permissions.update(
        _load_verified_permissions(
            connection,
            role_code=role_code,
            expected_by_role=EDGE_INSTALLER_WORKSPACE_ROLE_PERMISSIONS,
            catalog_table="workspace_edge_installer_permission_extensions",
            assignments_table="workspace_edge_installer_role_permissions",
            introduced_migration="0116",
            unavailable_error_code=unavailable_error_code,
        )
    )
    permissions.update(
        _load_verified_permissions(
            connection,
            role_code=role_code,
            expected_by_role=EDGE_SECRET_WORKSPACE_ROLE_PERMISSIONS,
            catalog_table="workspace_edge_secret_permission_extensions",
            assignments_table="workspace_edge_secret_role_permissions",
            introduced_migration="0115",
            unavailable_error_code=unavailable_error_code,
        )
    )
    permissions.update(
        _load_verified_permissions(
            connection,
            role_code=role_code,
            expected_by_role=EDGE_VALIDATION_WORKSPACE_ROLE_PERMISSIONS,
            catalog_table="workspace_edge_validation_permission_extensions",
            assignments_table="workspace_edge_validation_role_permissions",
            introduced_migration="0117",
            unavailable_error_code=unavailable_error_code,
        )
    )
    permissions.update(
        _load_verified_permissions(
            connection,
            role_code=role_code,
            expected_by_role=EDGE_DIAGNOSTIC_WORKSPACE_ROLE_PERMISSIONS,
            catalog_table="workspace_edge_diagnostic_permission_extensions",
            assignments_table="workspace_edge_diagnostic_role_permissions",
            introduced_migration="0118",
            unavailable_error_code=unavailable_error_code,
        )
    )
    return tuple(sorted(permissions))


def load_verified_platform_edge_permissions(
    connection: sqlite3.Connection,
    *,
    roles: tuple[str, ...],
    unavailable_error_code: str,
) -> tuple[str, ...]:
    permissions: set[str] = set()
    for role_code in roles:
        permissions.update(
            _load_verified_permissions(
                connection,
                role_code=role_code,
                expected_by_role=EDGE_PLATFORM_ROLE_PERMISSIONS,
                catalog_table="platform_edge_permission_extensions",
                assignments_table="platform_edge_role_permissions",
                introduced_migration="0112",
                unavailable_error_code=unavailable_error_code,
            )
        )
        permissions.update(
            _load_verified_permissions(
                connection,
                role_code=role_code,
                expected_by_role=EDGE_INSTALLER_PLATFORM_ROLE_PERMISSIONS,
                catalog_table="platform_edge_installer_permission_extensions",
                assignments_table="platform_edge_installer_role_permissions",
                introduced_migration="0116",
                unavailable_error_code=unavailable_error_code,
            )
        )
    return tuple(sorted(permissions))


def _load_verified_permissions(
    connection: sqlite3.Connection,
    *,
    role_code: str,
    expected_by_role: dict[str, tuple[str, ...]],
    catalog_table: str,
    assignments_table: str,
    introduced_migration: str,
    unavailable_error_code: str,
) -> tuple[str, ...]:
    """Verify the complete immutable matrix before returning one role projection."""

    try:
        catalog_available = _table_exists(connection, catalog_table)
        assignments_available = _table_exists(connection, assignments_table)
        migration_required = _migration_applied(connection, introduced_migration)
        if catalog_available != assignments_available or (
            migration_required and not catalog_available
        ):
            raise ApiError(unavailable_error_code)
        if not catalog_available:
            return ()
        expected = {
            (assigned_role, permission_code)
            for assigned_role, permission_codes in expected_by_role.items()
            for permission_code in permission_codes
        }
        catalog = {
            str(row[0])
            for row in connection.execute(
                f"SELECT permission_code FROM {catalog_table}"
            ).fetchall()
        }
        assignments = {
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                f"SELECT role_code,permission_code FROM {assignments_table}"
            ).fetchall()
        }
        if catalog != {permission for _, permission in expected} or assignments != expected:
            raise ApiError(unavailable_error_code)
        return tuple(
            sorted(
                permission
                for assigned_role, permission in expected
                if assigned_role == role_code
            )
        )
    except sqlite3.Error as exc:
        raise ApiError(unavailable_error_code) from exc


def _migration_applied(connection: sqlite3.Connection, version: str) -> bool:
    if not _table_exists(connection, "schema_migrations"):
        return False
    return (
        connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version=? AND status='applied'",
            (version,),
        ).fetchone()
        is not None
    )


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


__all__ = (
    "EDGE_DIAGNOSTIC_WORKSPACE_ROLE_PERMISSIONS",
    "EDGE_INSTALLER_PLATFORM_ROLE_PERMISSIONS",
    "EDGE_INSTALLER_WORKSPACE_ROLE_PERMISSIONS",
    "EDGE_PLATFORM_ROLE_PERMISSIONS",
    "EDGE_SECRET_WORKSPACE_ROLE_PERMISSIONS",
    "EDGE_TELEMETRY_WORKSPACE_ROLE_PERMISSIONS",
    "EDGE_VALIDATION_WORKSPACE_ROLE_PERMISSIONS",
    "EDGE_WORKSPACE_ROLE_PERMISSIONS",
    "load_verified_platform_edge_permissions",
    "load_verified_workspace_edge_permissions",
    "resolve_verified_workspace_edge_role",
)
