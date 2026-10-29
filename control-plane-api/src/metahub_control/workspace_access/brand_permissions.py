"""Verified Workspace DeploymentBrand permissions for PRD-142 entry points."""

from __future__ import annotations

import sqlite3

from metahub_shared.db.schema import table_exists
from metahub_shared.runtime.errors import ApiError

WORKSPACE_BRAND_ROLE_PERMISSIONS = {
    "owner": ("deployment.branding.manage", "deployment.branding.read"),
    "admin": ("deployment.branding.manage", "deployment.branding.read"),
}

WORKSPACE_BRAND_PERMISSIONS = frozenset(
    permission
    for permissions in WORKSPACE_BRAND_ROLE_PERMISSIONS.values()
    for permission in permissions
)

_CATALOG_TABLE = "workspace_brand_permission_extensions"
_ASSIGNMENTS_TABLE = "workspace_brand_role_permissions"
_INTRODUCED_MIGRATION = "0134"


def load_verified_workspace_brand_permissions(
    connection: sqlite3.Connection,
    *,
    role_code: str,
    unavailable_error_code: str,
) -> tuple[str, ...]:
    """Return one role projection only after verifying the immutable 0134 matrix."""

    try:
        catalog_available = _table_exists(connection, _CATALOG_TABLE)
        assignments_available = _table_exists(connection, _ASSIGNMENTS_TABLE)
        migration_required = _migration_applied(connection, _INTRODUCED_MIGRATION)
        if catalog_available != assignments_available or (
            migration_required and not catalog_available
        ):
            raise ApiError(unavailable_error_code)
        if not catalog_available:
            return ()

        expected_assignments = {
            (assigned_role, permission_code)
            for assigned_role, permission_codes in WORKSPACE_BRAND_ROLE_PERMISSIONS.items()
            for permission_code in permission_codes
        }
        catalog = {
            str(row[0])
            for row in connection.execute(
                f"SELECT permission_code FROM {_CATALOG_TABLE}"
            ).fetchall()
        }
        assignments = {
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                f"SELECT role_code,permission_code FROM {_ASSIGNMENTS_TABLE}"
            ).fetchall()
        }
        if (
            catalog != {permission_code for _, permission_code in expected_assignments}
            or assignments != expected_assignments
        ):
            raise ApiError(unavailable_error_code)
        return tuple(
            sorted(
                permission_code
                for assigned_role, permission_code in expected_assignments
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
    return table_exists(connection, name)


__all__ = (
    "WORKSPACE_BRAND_PERMISSIONS",
    "WORKSPACE_BRAND_ROLE_PERMISSIONS",
    "load_verified_workspace_brand_permissions",
)
