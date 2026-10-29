"""PRD-098 SaaS catalog migration and guarded rollback contracts."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
PRD098_TABLES = {
    "platform_permission_extensions",
    "platform_role_permission_extensions",
    "saas_plan_catalogs",
    "saas_plan_versions",
    "workspace_subscriptions",
    "subscription_trials",
    "subscription_order_records",
    "entitlement_snapshots",
    "saas_subscription_events",
}


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _healthy(database_url: str) -> None:
    connection = connect_database(database_url)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


@pytest.fixture(scope="module")
def migrated_0089(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("prd098-migrated-0089")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 89:
            shutil.copy2(source, migrations / source.name)
    database = root / "prd098-0089.sqlite3"
    result = apply_migrations(_url(database), migrations_dir=migrations)
    assert result.applied[-1] == "0089"
    apply_seed_data(_url(database))
    shutil.copy2(
        MIGRATIONS_DIR / "0090_saas_plan_subscription_entitlement.sql",
        migrations / "0090_saas_plan_subscription_entitlement.sql",
    )
    shutil.copy2(
        MIGRATIONS_DIR / "0090_saas_plan_subscription_entitlement.down.sql",
        migrations / "0090_saas_plan_subscription_entitlement.down.sql",
    )
    return database, migrations


def test_0089_to_0090_pristine_roundtrip(
    migrated_0089: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0089
    database = tmp_path / "roundtrip.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0090"]
    connection = connect_database(database_url)
    try:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert PRD098_TABLES <= tables
        assert connection.execute(
            "SELECT COUNT(*) FROM platform_permission_extensions"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM platform_role_permission_extensions"
        ).fetchone()[0] == 4
        assert connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='index' "
            "AND name='idx_subscription_trials_due_prd098'"
        ).fetchone()
    finally:
        connection.close()
    _healthy(database_url)

    rolled = rollback_last_migration(
        database_url, migrations_dir=migrations, expected_version="0090"
    )
    assert rolled.rolled_back == "0090"
    connection = connect_database(database_url)
    try:
        remaining = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert PRD098_TABLES.isdisjoint(remaining)
    finally:
        connection.close()
    _healthy(database_url)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0090"]
    _healthy(database_url)


def test_0090_non_pristine_catalog_blocks_down(
    migrated_0089: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0089
    database = tmp_path / "non-pristine.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0090"]
    connection = connect_database(database_url)
    try:
        account_id = connection.execute(
            "SELECT account_id FROM global_accounts ORDER BY created_at LIMIT 1"
        ).fetchone()[0]
        operator_id = "pto_" + "9" * 32
        connection.execute(
            """
            INSERT INTO platform_operators(
              operator_id,account_id,status,revision,created_by_operator_id,created_at,updated_at)
            VALUES(?,?,'active',1,NULL,datetime('now'),datetime('now'))
            """,
            (operator_id, account_id),
        )
        connection.execute(
            """
            INSERT INTO saas_plan_catalogs(
              plan_catalog_id,plan_code,display_name,status,display_order,revision,
              created_by_operator_id,created_at,updated_at)
            VALUES(?,'free','Free','draft',0,1,?,datetime('now'),datetime('now'))
            """,
            ("spc_" + "8" * 32, operator_id),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(DbError) as blocked:
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0090"
        )
    assert blocked.value.code == "db.migration_failed"
    _healthy(database_url)


def test_fresh_schema_installs_0090(tmp_path: Path) -> None:
    database_url = _url(tmp_path / "fresh.sqlite3")
    result = apply_migrations(database_url)
    assert result.applied[-1] == "0090"
    assert "0090" in result.applied
    _healthy(database_url)
