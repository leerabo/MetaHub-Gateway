"""PRD-131 migration operations schema, catalog, and guarded rollback."""

from __future__ import annotations

import secrets
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from metahub_control.migration_operations.permissions import (
    load_verified_workspace_migration_permissions,
)
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
PRD131_TABLES = {
    "workspace_migration_permission_extensions",
    "workspace_migration_role_permissions",
    "migration_source_adapters",
    "migration_assessments",
    "migration_plans",
    "migration_assessment_snapshots",
    "migration_runs",
    "migration_differences",
    "migration_cutover_checkpoints",
    "migration_rollback_plans",
    "migration_cutover_heads",
    "migration_events",
    "migration_event_outbox",
    "migration_action_commands",
    "migration_support_access_audits",
    "tenant_deletion_manifest_extensions_prd131",
}
TENANT_TABLES = {
    "migration_assessments",
    "migration_plans",
    "migration_assessment_snapshots",
    "migration_runs",
    "migration_differences",
    "migration_cutover_checkpoints",
    "migration_rollback_plans",
    "migration_cutover_heads",
    "migration_events",
    "migration_event_outbox",
    "migration_support_access_audits",
}
NOW = "2026-08-29T10:00:00.000Z"
_TEST_PASSWORD = f"Prd131!Aa9{secrets.token_urlsafe(18)}"


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _healthy(database_url: str) -> None:
    with closing(connect_database(database_url)) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.fixture(scope="module")
def base_0122(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("prd131-0122-base")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 122:
            shutil.copy2(source, migrations / source.name)
    database = root / "base-0122.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied[-1] == "0122"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", _TEST_PASSWORD)
        apply_seed_data(database_url)
    for filename in (
        "0123_customer_migration_cutover_operations.sql",
        "0123_customer_migration_cutover_operations.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    _healthy(database_url)
    return database, migrations


@pytest.fixture
def migrated_0122(base_0122: tuple[Path, Path], tmp_path: Path) -> tuple[Path, Path]:
    base, migrations = base_0122
    database = tmp_path / "prd131-0122.sqlite3"
    shutil.copy2(base, database)
    return database, migrations


def test_0122_to_0123_catalog_manifest_constraints_and_roundtrip(
    migrated_0122: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0122
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0123"]
    with closing(connect_database(database_url)) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            ).fetchall()
        }
        assert PRD131_TABLES <= tables
        assert {
            str(row[0])
            for row in connection.execute(
                "SELECT source_table FROM tenant_deletion_manifest_extensions_prd131"
            ).fetchall()
        } == TENANT_TABLES
        assert load_verified_workspace_migration_permissions(
            connection,
            role_code="owner",
            unavailable_error_code="migration.unavailable",
        ) == ("migration.manage", "migration.read")
        assert load_verified_workspace_migration_permissions(
            connection,
            role_code="developer",
            unavailable_error_code="migration.unavailable",
        ) == ("migration.read",)
        assert (
            connection.execute("SELECT COUNT(*) FROM migration_source_adapters").fetchone()[0] == 7
        )
        with pytest.raises(sqlite3.IntegrityError, match="migration_adapter_immutable"):
            connection.execute(
                "UPDATE migration_source_adapters SET adapter_version='tampered' "
                "WHERE domain_code='identity'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                """INSERT INTO migration_action_commands(
                     migration_action_command_id,actor_account_id,action_code,
                     idempotency_sha256,request_sha256,result_object_id,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    "mac_" + "1" * 32,
                    "gac_" + "f" * 32,
                    "migration.assessment.create",
                    "2" * 64,
                    "3" * 64,
                    "mas_" + "4" * 32,
                    NOW,
                ),
            )
    _healthy(database_url)
    assert (
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0123"
        ).rolled_back
        == "0123"
    )
    _healthy(database_url)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0123"]
    _healthy(database_url)


def test_0123_down_guard_rejects_migration_business_fact(
    migrated_0122: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0122
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0123"]
    with closing(connect_database(database_url)) as connection:
        account_id = str(
            connection.execute(
                "SELECT account_id FROM global_accounts ORDER BY account_id LIMIT 1"
            ).fetchone()[0]
        )
        connection.execute(
            """INSERT INTO migration_action_commands(
                 migration_action_command_id,actor_account_id,action_code,
                 idempotency_sha256,request_sha256,result_object_id,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (
                "mac_" + "5" * 32,
                account_id,
                "migration.assessment.create",
                "6" * 64,
                "7" * 64,
                "mas_" + "8" * 32,
                NOW,
            ),
        )
        connection.commit()
    with pytest.raises(DbError) as blocked:
        rollback_last_migration(database_url, migrations_dir=migrations, expected_version="0123")
    assert blocked.value.code == "db.migration_failed"
    _healthy(database_url)


def test_fresh_schema_installs_prd131_head(tmp_path: Path) -> None:
    database_url = _url(tmp_path / "fresh.sqlite3")
    assert apply_migrations(database_url).applied[-1] == "0123"
    _healthy(database_url)
