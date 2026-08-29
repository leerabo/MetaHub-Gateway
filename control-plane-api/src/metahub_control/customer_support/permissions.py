"""Verified Workspace permission projection for PRD-132 customer support."""

from __future__ import annotations

import sqlite3

from metahub_shared.runtime.errors import ApiError

CUSTOMER_SUPPORT_ROLE_PERMISSIONS = {
    "owner": ("support.ticket.create", "support.ticket.manage", "support.ticket.read"),
    "admin": ("support.ticket.create", "support.ticket.manage", "support.ticket.read"),
    "developer": ("support.ticket.create", "support.ticket.read"),
    "security_admin": ("support.ticket.create", "support.ticket.read"),
    "finance_admin": ("support.ticket.create", "support.ticket.read"),
    "legal_admin": ("support.ticket.create", "support.ticket.read"),
    "auditor": ("support.ticket.read",),
    "viewer": ("support.ticket.read",),
}


def load_verified_workspace_customer_support_permissions(
    connection: sqlite3.Connection,
    *,
    role_code: str,
    unavailable_error_code: str,
) -> tuple[str, ...]:
    catalog_table = "workspace_customer_support_permission_extensions"
    assignment_table = "workspace_customer_support_role_permissions"
    try:
        catalog_available = _table_exists(connection, catalog_table)
        assignments_available = _table_exists(connection, assignment_table)
        if not catalog_available and not assignments_available:
            return ()
        if catalog_available != assignments_available:
            raise ApiError(unavailable_error_code)
        expected = {
            (assigned_role, permission)
            for assigned_role, permissions in CUSTOMER_SUPPORT_ROLE_PERMISSIONS.items()
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


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


__all__ = [
    "CUSTOMER_SUPPORT_ROLE_PERMISSIONS",
    "load_verified_workspace_customer_support_permissions",
]
