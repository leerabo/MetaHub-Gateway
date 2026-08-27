"""PRD-128 license capacity schema, permission, manifest, and rollback guards."""

from __future__ import annotations

import shutil
from contextlib import closing
from pathlib import Path

import pytest
from metahub_control.workspace_access.edge_permissions import (
    load_verified_platform_edge_permissions,
    load_verified_workspace_edge_permissions,
)
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
PRD128_TABLES = {
    "workspace_edge_license_permission_extensions",
    "workspace_edge_license_role_permissions",
    "platform_edge_license_permission_extensions",
    "platform_edge_license_role_permissions",
    "edge_licenses",
    "edge_signed_entitlements",
    "edge_capacity_grants",
    "edge_license_refresh_requests",
    "edge_license_acknowledgements",
    "edge_license_lease_reports",
    "edge_license_audits",
    "edge_license_outbox",
    "edge_license_action_commands",
    "tenant_deletion_manifest_extensions_prd128",
}
TENANT_TABLES = {
    "edge_licenses",
    "edge_signed_entitlements",
    "edge_capacity_grants",
    "edge_license_refresh_requests",
    "edge_license_acknowledgements",
    "edge_license_lease_reports",
    "edge_license_audits",
    "edge_license_outbox",
}
NOW = "2026-08-28T12:00:00.000Z"


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _healthy(database_url: str) -> None:
    with closing(connect_database(database_url)) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.fixture(scope="module")
def base_0119(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("prd128-0119-base")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 119:
            shutil.copy2(source, migrations / source.name)
    database = root / "base-0119.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied[-1] == "0119"
    apply_seed_data(database_url)
    for filename in (
        "0120_edge_license_capacity_offline_grace.sql",
        "0120_edge_license_capacity_offline_grace.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    _healthy(database_url)
    return database, migrations


@pytest.fixture
def migrated_0119(base_0119: tuple[Path, Path], tmp_path: Path) -> tuple[Path, Path]:
    base, migrations = base_0119
    database = tmp_path / "prd128-0119.sqlite3"
    shutil.copy2(base, database)
    return database, migrations


def test_0119_to_0120_permissions_manifest_and_roundtrip(
    migrated_0119: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0119
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0120"]

    with closing(connect_database(database_url)) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            ).fetchall()
        }
        assert PRD128_TABLES <= tables
        assert {
            str(row[0])
            for row in connection.execute(
                "SELECT source_table FROM tenant_deletion_manifest_extensions_prd128"
            ).fetchall()
        } == TENANT_TABLES
        assert {
            "edge.license.import",
            "edge.license.read",
            "edge.license.refresh",
        } <= set(
            load_verified_workspace_edge_permissions(
                connection,
                role_code="owner",
                unavailable_error_code="edge_license.unavailable",
            )
        )
        assert {
            "platform.edge_license.issue",
            "platform.edge_license.read",
            "platform.edge_license.revoke",
        } <= set(
            load_verified_platform_edge_permissions(
                connection,
                roles=("platform_billing",),
                unavailable_error_code="edge_license.unavailable",
            )
        )
        assert "edge.license.import" not in load_verified_workspace_edge_permissions(
            connection,
            role_code="admin",
            unavailable_error_code="edge_license.unavailable",
        )
        assert load_verified_platform_edge_permissions(
            connection,
            roles=("platform_support",),
            unavailable_error_code="edge_license.unavailable",
        ) == ()
    _healthy(database_url)

    assert (
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0120"
        ).rolled_back
        == "0120"
    )
    _healthy(database_url)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0120"]
    _healthy(database_url)


def test_0120_down_guard_rejects_license_action_fact(
    migrated_0119: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0119
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0120"]
    with closing(connect_database(database_url)) as connection:
        connection.execute(
            """
            INSERT INTO edge_license_action_commands(
              license_action_command_id,actor_domain,actor_id,action_code,
              idempotency_sha256,request_sha256,result_object_id,created_at)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                "lac_" + "1" * 32,
                "platform",
                "pto_" + "2" * 32,
                "edge.license.issue",
                "3" * 64,
                "4" * 64,
                "lic_" + "5" * 32,
                NOW,
            ),
        )
        connection.commit()
    with pytest.raises(DbError) as blocked:
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0120"
        )
    assert blocked.value.code == "db.migration_failed"
    _healthy(database_url)
