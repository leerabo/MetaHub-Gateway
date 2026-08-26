"""Verified Workspace permission extension used by PRD-119 entry points."""

from __future__ import annotations

import sqlite3

from metahub_shared.runtime.errors import ApiError

PROVIDER_CATALOG_PERMISSION_CODE = "provider_catalog.read"
PROVIDER_CATALOG_ROLE_PERMISSIONS = {
    "owner": (PROVIDER_CATALOG_PERMISSION_CODE,),
    "admin": (PROVIDER_CATALOG_PERMISSION_CODE,),
    "developer": (PROVIDER_CATALOG_PERMISSION_CODE,),
}

_CATALOG_TABLE = "workspace_provider_catalog_permission_extensions"
_ASSIGNMENTS_TABLE = "workspace_provider_catalog_role_permissions"
_INTRODUCED_MIGRATION = "0111"


def load_verified_provider_catalog_permissions(
    connection: sqlite3.Connection,
    *,
    role_code: str,
    unavailable_error_code: str,
) -> tuple[str, ...]:
    """Return a role grant only after verifying the complete immutable extension matrix."""

    try:
        catalog_available = _table_exists(connection, _CATALOG_TABLE)
        assignments_available = _table_exists(connection, _ASSIGNMENTS_TABLE)
        migration_required = _migration_applied(connection, _INTRODUCED_MIGRATION)
        if catalog_available != assignments_available or (
            migration_required and not catalog_available
        ):
            raise ApiError(unavailable_error_code)
        # Older migration fixtures legitimately have neither PRD-119 table. They grant nothing.
        if not catalog_available:
            return ()

        expected_assignments = {
            (assigned_role, permission_code)
            for assigned_role, permission_codes in PROVIDER_CATALOG_ROLE_PERMISSIONS.items()
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
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        is not None
    )


__all__ = (
    "PROVIDER_CATALOG_PERMISSION_CODE",
    "PROVIDER_CATALOG_ROLE_PERMISSIONS",
    "load_verified_provider_catalog_permissions",
)
