"""PRD-129 backup, restore, drill schema and guarded rollback."""

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
PRD129_TABLES = {
    "workspace_edge_backup_permission_extensions",
    "workspace_edge_backup_role_permissions",
    "platform_edge_backup_permission_extensions",
    "platform_edge_backup_role_permissions",
    "edge_backup_reports",
    "edge_backup_statuses",
    "edge_restore_jobs",
    "edge_restore_verifications",
    "private_dr_drills",
    "edge_backup_events",
    "edge_backup_outbox",
    "edge_backup_action_commands",
    "tenant_deletion_manifest_extensions_prd129",
}
TENANT_TABLES = {
    "edge_backup_reports",
    "edge_backup_statuses",
    "edge_restore_jobs",
    "edge_restore_verifications",
    "private_dr_drills",
    "edge_backup_events",
    "edge_backup_outbox",
}
NOW = "2026-08-28T13:00:00.000Z"


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _healthy(database_url: str) -> None:
    with closing(connect_database(database_url)) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.fixture(scope="module")
def base_0120(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("prd129-0120-base")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 120:
            shutil.copy2(source, migrations / source.name)
    database = root / "base-0120.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied[-1] == "0120"
    apply_seed_data(database_url)
    for filename in (
        "0121_edge_backup_restore_dr_drill.sql",
        "0121_edge_backup_restore_dr_drill.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    _healthy(database_url)
    return database, migrations


@pytest.fixture
def migrated_0120(base_0120: tuple[Path, Path], tmp_path: Path) -> tuple[Path, Path]:
    base, migrations = base_0120
    database = tmp_path / "prd129-0120.sqlite3"
    shutil.copy2(base, database)
    return database, migrations


def test_0120_to_0121_permissions_manifest_and_roundtrip(
    migrated_0120: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0120
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0121"]
    with closing(connect_database(database_url)) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            ).fetchall()
        }
        assert PRD129_TABLES <= tables
        assert {
            str(row[0])
            for row in connection.execute(
                "SELECT source_table FROM tenant_deletion_manifest_extensions_prd129"
            ).fetchall()
        } == TENANT_TABLES
        assert {
            "edge.backup.create",
            "edge.backup.read",
            "edge.drill.manage",
            "edge.restore.approve",
        } <= set(
            load_verified_workspace_edge_permissions(
                connection,
                role_code="owner",
                unavailable_error_code="edge_backup.unavailable",
            )
        )
        assert {
            "platform.edge_backup.read",
            "platform.edge_drill.manage",
        } <= set(
            load_verified_platform_edge_permissions(
                connection,
                roles=("platform_ops",),
                unavailable_error_code="edge_backup.unavailable",
            )
        )
        assert "edge.restore.approve" not in load_verified_workspace_edge_permissions(
            connection,
            role_code="admin",
            unavailable_error_code="edge_backup.unavailable",
        )
        assert load_verified_platform_edge_permissions(
            connection,
            roles=("platform_support",),
            unavailable_error_code="edge_backup.unavailable",
        ) == ()
    _healthy(database_url)
    assert (
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0121"
        ).rolled_back
        == "0121"
    )
    _healthy(database_url)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0121"]
    _healthy(database_url)


def test_0121_down_guard_rejects_backup_action_fact(
    migrated_0120: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0120
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0121"]
    with closing(connect_database(database_url)) as connection:
        connection.execute(
            """
            INSERT INTO edge_backup_action_commands(
              edge_backup_action_command_id,actor_domain,actor_id,action_code,
              idempotency_sha256,request_sha256,result_object_id,created_at)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                "bac_" + "1" * 32,
                "agent",
                "edi_" + "2" * 32,
                "edge.backup.report",
                "3" * 64,
                "4" * 64,
                "ebk_" + "5" * 32,
                NOW,
            ),
        )
        connection.commit()
    with pytest.raises(DbError) as blocked:
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0121"
        )
    assert blocked.value.code == "db.migration_failed"
    _healthy(database_url)
