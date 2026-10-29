"""Verified Workspace permission projection for PRD-131 migration operations."""

from __future__ import annotations

import sqlite3

from metahub_shared.db.schema import table_exists
from metahub_shared.runtime.errors import ApiError

MIGRATION_ROLE_PERMISSIONS = {
    "owner": ("migration.manage", "migration.read"),
    "admin": ("migration.manage", "migration.read"),
    "developer": ("migration.read",),
}


def load_verified_workspace_migration_permissions(
    connection: sqlite3.Connection,
    *,
    role_code: str,
    unavailable_error_code: str,
) -> tuple[str, ...]:
    """Load the exact immutable 0123 catalog and fail closed on any drift."""

    catalog_table = "workspace_migration_permission_extensions"
    assignment_table = "workspace_migration_role_permissions"
    try:
        catalog_available = table_exists(connection, catalog_table)
        assignments_available = table_exists(connection, assignment_table)
        if not catalog_available and not assignments_available:
            return ()
        if catalog_available != assignments_available:
            raise ApiError(unavailable_error_code)
        expected = {
            (assigned_role, permission)
            for assigned_role, permissions in MIGRATION_ROLE_PERMISSIONS.items()
            for permission in permissions
        }
        catalog = {
            str(row[0])
            for row in connection.execute(f"SELECT permission_code FROM {catalog_table}").fetchall()
        }
        assignments = {
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                f"SELECT role_code,permission_code FROM {assignment_table}"
            ).fetchall()
        }
        if catalog != {permission for _, permission in expected} or assignments != expected:
            raise ApiError(unavailable_error_code)
        return tuple(
            str(row[0])
            for row in connection.execute(
                f"SELECT permission_code FROM {assignment_table} "
                "WHERE role_code=? ORDER BY permission_code",
                (role_code,),
            ).fetchall()
        )
    except ApiError:
        raise
    except sqlite3.Error as exc:
        raise ApiError(unavailable_error_code) from exc


__all__ = ["MIGRATION_ROLE_PERMISSIONS", "load_verified_workspace_migration_permissions"]
