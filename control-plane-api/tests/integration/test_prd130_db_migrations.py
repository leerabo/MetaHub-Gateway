"""PRD-130 isolated bundle schema and guarded rollback."""

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
PRD130_TABLES = {
    "workspace_edge_offline_permission_extensions",
    "workspace_edge_offline_role_permissions",
    "platform_edge_offline_permission_extensions",
    "platform_edge_offline_role_permissions",
    "edge_offline_bundle_requests",
    "edge_offline_bundles",
    "edge_offline_bundle_transfers",
    "edge_offline_sync_cursors",
    "edge_offline_conflicts",
    "edge_offline_conflict_resolutions",
    "edge_offline_bundle_events",
    "edge_offline_bundle_outbox",
    "edge_offline_bundle_action_commands",
    "tenant_deletion_manifest_extensions_prd130",
}
TENANT_TABLES = {
    "edge_offline_bundle_requests",
    "edge_offline_bundles",
    "edge_offline_bundle_transfers",
    "edge_offline_sync_cursors",
    "edge_offline_conflicts",
    "edge_offline_conflict_resolutions",
    "edge_offline_bundle_events",
    "edge_offline_bundle_outbox",
}
NOW = "2026-08-28T13:00:00.000Z"


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _healthy(database_url: str) -> None:
    with closing(connect_database(database_url)) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.fixture(scope="module")
def base_0121(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("prd130-0121-base")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 121:
            shutil.copy2(source, migrations / source.name)
    database = root / "base-0121.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied[-1] == "0121"
    apply_seed_data(database_url)
    for filename in (
        "0122_edge_offline_bundle_conflict_reconciliation.sql",
        "0122_edge_offline_bundle_conflict_reconciliation.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    _healthy(database_url)
    return database, migrations


@pytest.fixture
def migrated_0121(base_0121: tuple[Path, Path], tmp_path: Path) -> tuple[Path, Path]:
    base, migrations = base_0121
    database = tmp_path / "prd130-0121.sqlite3"
    shutil.copy2(base, database)
    return database, migrations


def test_0121_to_0122_permissions_manifest_and_roundtrip(
    migrated_0121: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0121
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0122"]
    with closing(connect_database(database_url)) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            ).fetchall()
        }
        assert PRD130_TABLES <= tables
        assert {
            str(row[0])
            for row in connection.execute(
                "SELECT source_table FROM tenant_deletion_manifest_extensions_prd130"
            ).fetchall()
        } == TENANT_TABLES
        assert {
            "edge.offline_bundle.read",
            "edge.offline_bundle.transfer",
            "edge.offline_conflict.resolve",
        } <= set(
            load_verified_workspace_edge_permissions(
                connection,
                role_code="owner",
                unavailable_error_code="edge_offline_bundle.unavailable",
            )
        )
        assert "edge.offline_conflict.resolve" not in load_verified_workspace_edge_permissions(
            connection,
            role_code="admin",
            unavailable_error_code="edge_offline_bundle.unavailable",
        )
        assert {
            "platform.edge_offline_bundle.read",
            "platform.edge_offline_bundle.manage",
        } <= set(
            load_verified_platform_edge_permissions(
                connection,
                roles=("platform_ops",),
                unavailable_error_code="edge_offline_bundle.unavailable",
            )
        )
        assert (
            load_verified_platform_edge_permissions(
                connection,
                roles=("platform_support",),
                unavailable_error_code="edge_offline_bundle.unavailable",
            )
            == ()
        )
    _healthy(database_url)
    assert (
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0122"
        ).rolled_back
        == "0122"
    )
    _healthy(database_url)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0122"]
    _healthy(database_url)


def test_0122_down_guard_rejects_offline_action_fact(
    migrated_0121: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0121
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0122"]
    with closing(connect_database(database_url)) as connection:
        connection.execute(
            """INSERT INTO edge_offline_bundle_action_commands(
                 edge_offline_bundle_action_command_id,actor_domain,actor_id,action_code,
                 idempotency_sha256,request_sha256,result_object_id,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                "oac_" + "1" * 32,
                "workspace",
                "acc_test",
                "edge.offline.export.request",
                "2" * 64,
                "3" * 64,
                "obr_" + "4" * 32,
                NOW,
            ),
        )
        connection.commit()
    with pytest.raises(DbError) as blocked:
        rollback_last_migration(database_url, migrations_dir=migrations, expected_version="0122")
    assert blocked.value.code == "db.migration_failed"
    _healthy(database_url)
