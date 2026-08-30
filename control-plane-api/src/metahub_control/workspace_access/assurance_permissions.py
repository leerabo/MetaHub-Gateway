"""Verified PRD-137 Workspace security-assurance permission extension."""

from __future__ import annotations

import sqlite3

from metahub_shared.runtime.errors import ApiError

ASSURANCE_ROLE_PERMISSIONS = {
    "owner": ("security_assurance.accept", "security_assurance.read"),
    "security_admin": ("security_assurance.accept", "security_assurance.read"),
    "auditor": ("security_assurance.read",),
}

_CATALOG_TABLE = "workspace_security_assurance_permission_extensions"
_ASSIGNMENTS_TABLE = "workspace_security_assurance_role_permissions"
_INTRODUCED_MIGRATION = "0129"


def load_verified_workspace_assurance_permissions(
    connection: sqlite3.Connection,
    *,
    role_code: str,
    unavailable_error_code: str,
) -> tuple[str, ...]:
    """Return grants only when the complete immutable PRD-137 matrix matches."""

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
        expected = {
            (assigned_role, permission_code)
            for assigned_role, permission_codes in ASSURANCE_ROLE_PERMISSIONS.items()
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
    return (
        _table_exists(connection, "schema_migrations")
        and connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version=? AND status='applied'", (version,)
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


__all__ = ["ASSURANCE_ROLE_PERMISSIONS", "load_verified_workspace_assurance_permissions"]
