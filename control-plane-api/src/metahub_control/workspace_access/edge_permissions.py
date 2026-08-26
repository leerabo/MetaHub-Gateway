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
EDGE_PLATFORM_ROLE_PERMISSIONS = {
    "platform_owner": ("platform.edge_fleet.manage", "platform.edge_fleet.read"),
    "platform_ops": ("platform.edge_fleet.manage", "platform.edge_fleet.read"),
    "platform_security": ("platform.edge_fleet.read",),
}


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
    "EDGE_PLATFORM_ROLE_PERMISSIONS",
    "EDGE_TELEMETRY_WORKSPACE_ROLE_PERMISSIONS",
    "EDGE_WORKSPACE_ROLE_PERMISSIONS",
    "load_verified_platform_edge_permissions",
    "load_verified_workspace_edge_permissions",
)
