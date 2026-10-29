"""Verified human permission projections for PRD-120 private gateways."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from metahub_shared.db.schema import table_exists
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
EDGE_UPGRADE_WORKSPACE_ROLE_PERMISSIONS = {
    "owner": ("edge.rollback.manage", "edge.upgrade.manage", "edge.upgrade.read"),
    "admin": ("edge.rollback.manage", "edge.upgrade.manage", "edge.upgrade.read"),
    "security_admin": ("edge.upgrade.read",),
    "auditor": ("edge.upgrade.read",),
}
EDGE_LICENSE_WORKSPACE_ROLE_PERMISSIONS = {
    "owner": ("edge.license.import", "edge.license.read", "edge.license.refresh"),
    "admin": ("edge.license.read", "edge.license.refresh"),
    "finance_admin": ("edge.license.read",),
    "auditor": ("edge.license.read",),
}
EDGE_BACKUP_WORKSPACE_ROLE_PERMISSIONS = {
    "owner": (
        "edge.backup.create",
        "edge.backup.read",
        "edge.drill.manage",
        "edge.restore.approve",
    ),
    "admin": ("edge.backup.create", "edge.backup.read", "edge.drill.manage"),
    "security_admin": (
        "edge.backup.read",
        "edge.drill.manage",
        "edge.restore.approve",
    ),
    "auditor": ("edge.backup.read",),
}
EDGE_OFFLINE_WORKSPACE_ROLE_PERMISSIONS = {
    "owner": (
        "edge.offline_bundle.read",
        "edge.offline_bundle.transfer",
        "edge.offline_conflict.resolve",
    ),
    "admin": ("edge.offline_bundle.read", "edge.offline_bundle.transfer"),
    "security_admin": (
        "edge.offline_bundle.read",
        "edge.offline_bundle.transfer",
        "edge.offline_conflict.resolve",
    ),
    "auditor": ("edge.offline_bundle.read",),
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
EDGE_UPGRADE_PLATFORM_ROLE_PERMISSIONS = {
    "platform_owner": (
        "platform.edge_release.approve",
        "platform.edge_release.read",
        "platform.edge_rollout.manage",
        "platform.edge_rollout.read",
    ),
    "platform_ops": (
        "platform.edge_release.read",
        "platform.edge_rollout.manage",
        "platform.edge_rollout.read",
    ),
    "platform_security": (
        "platform.edge_release.read",
        "platform.edge_rollout.read",
    ),
    "release_manager": (
        "platform.edge_release.approve",
        "platform.edge_release.manage",
        "platform.edge_release.read",
        "platform.edge_rollout.read",
    ),
}
EDGE_LICENSE_PLATFORM_ROLE_PERMISSIONS = {
    "platform_owner": (
        "platform.edge_license.issue",
        "platform.edge_license.read",
        "platform.edge_license.revoke",
    ),
    "platform_ops": ("platform.edge_license.read",),
    "platform_security": (
        "platform.edge_license.read",
        "platform.edge_license.revoke",
    ),
    "platform_billing": (
        "platform.edge_license.issue",
        "platform.edge_license.read",
        "platform.edge_license.revoke",
    ),
}
EDGE_BACKUP_PLATFORM_ROLE_PERMISSIONS = {
    "platform_owner": ("platform.edge_backup.read", "platform.edge_drill.manage"),
    "platform_ops": ("platform.edge_backup.read", "platform.edge_drill.manage"),
    "platform_security": ("platform.edge_backup.read",),
}
EDGE_OFFLINE_PLATFORM_ROLE_PERMISSIONS = {
    "platform_owner": (
        "platform.edge_offline_bundle.manage",
        "platform.edge_offline_bundle.read",
    ),
    "platform_ops": (
        "platform.edge_offline_bundle.manage",
        "platform.edge_offline_bundle.read",
    ),
    "platform_security": (
        "platform.edge_offline_bundle.manage",
        "platform.edge_offline_bundle.read",
    ),
    "release_manager": (
        "platform.edge_offline_bundle.manage",
        "platform.edge_offline_bundle.read",
    ),
}

_WORKSPACE_ROLES_BY_LEGACY_ROLE = {
    "admin": frozenset(("owner", "admin")),
    "ops_admin": frozenset(("admin",)),
    "developer": frozenset(("developer",)),
    "security_admin": frozenset(("security_admin",)),
    "finance_admin": frozenset(("finance_admin",)),
    "viewer": frozenset(("legal_admin", "auditor", "viewer")),
}

_PLATFORM_EDGE_PERMISSION_CACHE: dict[tuple[str, int, int], dict[str, tuple[str, ...]]] = {}


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
        if workspace_role not in _WORKSPACE_ROLES_BY_LEGACY_ROLE.get(
            legacy_role_code, frozenset()
        ):
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
    permissions.update(
        _load_verified_permissions(
            connection,
            role_code=role_code,
            expected_by_role=EDGE_UPGRADE_WORKSPACE_ROLE_PERMISSIONS,
            catalog_table="workspace_edge_upgrade_permission_extensions",
            assignments_table="workspace_edge_upgrade_role_permissions",
            introduced_migration="0119",
            unavailable_error_code=unavailable_error_code,
        )
    )
    permissions.update(
        _load_verified_permissions(
            connection,
            role_code=role_code,
            expected_by_role=EDGE_LICENSE_WORKSPACE_ROLE_PERMISSIONS,
            catalog_table="workspace_edge_license_permission_extensions",
            assignments_table="workspace_edge_license_role_permissions",
            introduced_migration="0120",
            unavailable_error_code=unavailable_error_code,
        )
    )
    permissions.update(
        _load_verified_permissions(
            connection,
            role_code=role_code,
            expected_by_role=EDGE_BACKUP_WORKSPACE_ROLE_PERMISSIONS,
            catalog_table="workspace_edge_backup_permission_extensions",
            assignments_table="workspace_edge_backup_role_permissions",
            introduced_migration="0121",
            unavailable_error_code=unavailable_error_code,
        )
    )
    permissions.update(
        _load_verified_permissions(
            connection,
            role_code=role_code,
            expected_by_role=EDGE_OFFLINE_WORKSPACE_ROLE_PERMISSIONS,
            catalog_table="workspace_edge_offline_permission_extensions",
            assignments_table="workspace_edge_offline_role_permissions",
            introduced_migration="0122",
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
    cache_key = _platform_edge_cache_key(connection)
    if cache_key is not None:
        cached = _PLATFORM_EDGE_PERMISSION_CACHE.get(cache_key)
        if cached is not None:
            return tuple(
                sorted({permission for role in roles for permission in cached.get(role, ())})
            )
    permissions: set[str] = set()
    # All six platform edge catalogs are verified against the same schema in one
    # authorization check. Keep discovery results local to this connection so
    # the hot session read does not repeat identical sqlite_schema and migration
    # lookups, while preserving fail-closed behavior for every catalog/query.
    table_cache: dict[str, bool] = _platform_extension_table_cache(connection)
    migration_cache: dict[str, bool] = _platform_extension_migration_cache(
        connection, table_cache
    )
    projection: dict[str, tuple[str, ...]] = {}
    for role_code in roles:
        role_permissions: set[str] = set()
        role_permissions.update(
            _load_verified_permissions(
                connection,
                role_code=role_code,
                expected_by_role=EDGE_OFFLINE_PLATFORM_ROLE_PERMISSIONS,
                catalog_table="platform_edge_offline_permission_extensions",
                assignments_table="platform_edge_offline_role_permissions",
                introduced_migration="0122",
                unavailable_error_code=unavailable_error_code,
                table_cache=table_cache,
                migration_cache=migration_cache,
            )
        )
        role_permissions.update(
            _load_verified_permissions(
                connection,
                role_code=role_code,
                expected_by_role=EDGE_PLATFORM_ROLE_PERMISSIONS,
                catalog_table="platform_edge_permission_extensions",
                assignments_table="platform_edge_role_permissions",
                introduced_migration="0112",
                unavailable_error_code=unavailable_error_code,
                table_cache=table_cache,
                migration_cache=migration_cache,
            )
        )
        role_permissions.update(
            _load_verified_permissions(
                connection,
                role_code=role_code,
                expected_by_role=EDGE_BACKUP_PLATFORM_ROLE_PERMISSIONS,
                catalog_table="platform_edge_backup_permission_extensions",
                assignments_table="platform_edge_backup_role_permissions",
                introduced_migration="0121",
                unavailable_error_code=unavailable_error_code,
                table_cache=table_cache,
                migration_cache=migration_cache,
            )
        )
        role_permissions.update(
            _load_verified_permissions(
                connection,
                role_code=role_code,
                expected_by_role=EDGE_LICENSE_PLATFORM_ROLE_PERMISSIONS,
                catalog_table="platform_edge_license_permission_extensions",
                assignments_table="platform_edge_license_role_permissions",
                introduced_migration="0120",
                unavailable_error_code=unavailable_error_code,
                table_cache=table_cache,
                migration_cache=migration_cache,
            )
        )
        role_permissions.update(
            _load_verified_permissions(
                connection,
                role_code=role_code,
                expected_by_role=EDGE_UPGRADE_PLATFORM_ROLE_PERMISSIONS,
                catalog_table="platform_edge_upgrade_permission_extensions",
                assignments_table="platform_edge_upgrade_role_permissions",
                introduced_migration="0119",
                unavailable_error_code=unavailable_error_code,
                table_cache=table_cache,
                migration_cache=migration_cache,
            )
        )
        role_permissions.update(
            _load_verified_permissions(
                connection,
                role_code=role_code,
                expected_by_role=EDGE_INSTALLER_PLATFORM_ROLE_PERMISSIONS,
                catalog_table="platform_edge_installer_permission_extensions",
                assignments_table="platform_edge_installer_role_permissions",
                introduced_migration="0116",
                unavailable_error_code=unavailable_error_code,
                table_cache=table_cache,
                migration_cache=migration_cache,
            )
        )
        projection[role_code] = tuple(sorted(role_permissions))
        permissions.update(role_permissions)
    if cache_key is not None:
        _PLATFORM_EDGE_PERMISSION_CACHE[cache_key] = projection
    return tuple(sorted(permissions))


def _platform_edge_cache_key(connection: sqlite3.Connection) -> tuple[str, int, int] | None:
    """Use file metadata to invalidate the immutable extension projection after writes."""

    if getattr(connection, "backend", "sqlite") == "postgresql":
        return None
    try:
        database = connection.execute("PRAGMA database_list").fetchone()
        path = str(database[2]) if database is not None else ""
        if not path or path == ":memory:":
            return None
        stat = Path(path).stat()
        return (os.path.abspath(path), int(stat.st_mtime_ns), int(stat.st_size))
    except (OSError, sqlite3.Error):
        return None


def _platform_extension_table_cache(connection: sqlite3.Connection) -> dict[str, bool]:
    """Discover the fixed extension schema with one catalog query per request."""

    names = {
        name
        for pair in (
            (
                "platform_edge_offline_permission_extensions",
                "platform_edge_offline_role_permissions",
            ),
            ("platform_edge_permission_extensions", "platform_edge_role_permissions"),
            (
                "platform_edge_backup_permission_extensions",
                "platform_edge_backup_role_permissions",
            ),
            (
                "platform_edge_license_permission_extensions",
                "platform_edge_license_role_permissions",
            ),
            (
                "platform_edge_upgrade_permission_extensions",
                "platform_edge_upgrade_role_permissions",
            ),
            (
                "platform_edge_installer_permission_extensions",
                "platform_edge_installer_role_permissions",
            ),
        )
        for name in pair
    }
    try:
        if getattr(connection, "backend", "sqlite") == "postgresql":
            rows = connection.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema=current_schema() AND table_type='BASE TABLE' "
                "AND table_name = ANY(?)",
                (list(names),),
            ).fetchall()
        else:
            placeholders = ",".join("?" for _ in names)
            rows = connection.execute(
                f"SELECT name FROM sqlite_schema WHERE type='table' AND name IN ({placeholders})",
                tuple(names),
            ).fetchall()
        available = {str(row[0]) for row in rows}
        return {name: name in available for name in names}
    except sqlite3.Error as exc:
        raise ApiError("platform.unavailable") from exc


def _platform_extension_migration_cache(
    connection: sqlite3.Connection, table_cache: dict[str, bool]
) -> dict[str, bool]:
    versions = ("0116", "0119", "0120", "0121", "0122")
    try:
        if not table_cache.get("schema_migrations", False):
            table_cache["schema_migrations"] = _table_exists(connection, "schema_migrations")
        if not table_cache["schema_migrations"]:
            return {version: False for version in versions}
        placeholders = ",".join("?" for _ in versions)
        rows = connection.execute(
            "SELECT version FROM schema_migrations "
            f"WHERE status='applied' AND version IN ({placeholders})",
            versions,
        ).fetchall()
        applied = {str(row[0]) for row in rows}
        return {version: version in applied for version in versions}
    except sqlite3.Error as exc:
        raise ApiError("platform.unavailable") from exc


def _load_verified_permissions(
    connection: sqlite3.Connection,
    *,
    role_code: str,
    expected_by_role: dict[str, tuple[str, ...]],
    catalog_table: str,
    assignments_table: str,
    introduced_migration: str,
    unavailable_error_code: str,
    table_cache: dict[str, bool] | None = None,
    migration_cache: dict[str, bool] | None = None,
) -> tuple[str, ...]:
    """Verify the complete immutable matrix before returning one role projection."""

    try:
        if table_cache is None:
            table_cache = {}
        if migration_cache is None:
            migration_cache = {}
        if catalog_table not in table_cache:
            table_cache[catalog_table] = _table_exists(connection, catalog_table)
        if assignments_table not in table_cache:
            table_cache[assignments_table] = _table_exists(connection, assignments_table)
        if introduced_migration not in migration_cache:
            migration_cache[introduced_migration] = _migration_applied(
                connection, introduced_migration
            )
        catalog_available = table_cache[catalog_table]
        assignments_available = table_cache[assignments_table]
        migration_required = migration_cache[introduced_migration]
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
    return table_exists(connection, name)


__all__ = (
    "EDGE_BACKUP_PLATFORM_ROLE_PERMISSIONS",
    "EDGE_BACKUP_WORKSPACE_ROLE_PERMISSIONS",
    "EDGE_DIAGNOSTIC_WORKSPACE_ROLE_PERMISSIONS",
    "EDGE_INSTALLER_PLATFORM_ROLE_PERMISSIONS",
    "EDGE_INSTALLER_WORKSPACE_ROLE_PERMISSIONS",
    "EDGE_LICENSE_PLATFORM_ROLE_PERMISSIONS",
    "EDGE_LICENSE_WORKSPACE_ROLE_PERMISSIONS",
    "EDGE_OFFLINE_WORKSPACE_ROLE_PERMISSIONS",
    "EDGE_PLATFORM_ROLE_PERMISSIONS",
    "EDGE_SECRET_WORKSPACE_ROLE_PERMISSIONS",
    "EDGE_TELEMETRY_WORKSPACE_ROLE_PERMISSIONS",
    "EDGE_UPGRADE_PLATFORM_ROLE_PERMISSIONS",
    "EDGE_UPGRADE_WORKSPACE_ROLE_PERMISSIONS",
    "EDGE_VALIDATION_WORKSPACE_ROLE_PERMISSIONS",
    "EDGE_WORKSPACE_ROLE_PERMISSIONS",
    "load_verified_platform_edge_permissions",
    "load_verified_workspace_edge_permissions",
    "resolve_verified_workspace_edge_role",
)
