"""Verify PRD-105 migration, catalog invariants and rollback guards."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from metahub_shared.db import apply_migrations, connect_database, rollback_last_migration
from metahub_shared.db.errors import DbError

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
PRD105_TABLES = {
    "notification_templates",
    "notification_template_catalog_state",
    "notifications",
    "notification_preferences",
    "delivery_attempts",
    "channel_suppressions",
    "notification_outbox",
    "platform_notification_permission_extensions",
    "platform_notification_role_permissions",
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
def migrated_0096(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("prd105-migrated-0096")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 96:
            shutil.copy2(source, migrations / source.name)
    database = root / "prd105-0096.sqlite3"
    assert apply_migrations(_url(database), migrations_dir=migrations).applied[-1] == "0096"
    for filename in ("0097_notification_center.sql", "0097_notification_center.down.sql"):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    return database, migrations


def test_0096_to_0097_pristine_roundtrip(
    migrated_0096: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0096
    database = tmp_path / "roundtrip.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0097"]
    connection = connect_database(database_url)
    try:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert PRD105_TABLES <= tables
        assert connection.execute("SELECT COUNT(*) FROM notification_templates").fetchone()[0] == 8
        assert connection.execute(
            "SELECT catalog_revision FROM platform_roles WHERE role_code='platform_communications'"
        ).fetchone()[0] == 7
        foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(platform_operator_role_bindings)"
        ).fetchall()
        assert any(row["table"] == "platform_roles" for row in foreign_keys)
    finally:
        connection.close()
    _healthy(database_url)

    assert rollback_last_migration(
        database_url, migrations_dir=migrations, expected_version="0097"
    ).rolled_back == "0097"
    connection = connect_database(database_url)
    try:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert PRD105_TABLES.isdisjoint(tables)
        assert connection.execute("SELECT COUNT(*) FROM platform_roles").fetchone()[0] == 13
        foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(platform_operator_role_bindings)"
        ).fetchall()
        assert any(row["table"] == "platform_roles" for row in foreign_keys)
    finally:
        connection.close()
    _healthy(database_url)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0097"]
    _healthy(database_url)


def test_notification_facts_and_communications_binding_block_down(
    migrated_0096: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0096
    database = tmp_path / "guarded.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0097"]
    connection = connect_database(database_url)
    try:
        account_id = "gac_" + "1" * 32
        operator_id = "pto_" + "1" * 32
        connection.execute(
            """
            INSERT INTO global_accounts(
              account_id,email_normalized,phone_e164,display_name,status,login_enabled,
              credential_revision,session_revision,mfa_state,failed_login_count,locked_until,
              last_login_at,created_at,updated_at)
            VALUES(?, 'prd105-migration@example.test',NULL,'PRD-105 Migration','active',0,
              1,1,'not_enrolled',0,NULL,NULL,'2026-08-23T00:00:00Z','2026-08-23T00:00:00Z')
            """,
            (account_id,),
        )
        connection.execute(
            """INSERT INTO platform_operators(
              operator_id,account_id,status,revision,created_by_operator_id,created_at,updated_at)
              VALUES(?,?,'active',1,NULL,'2026-08-23T00:00:00Z','2026-08-23T00:00:00Z')""",
            (operator_id, account_id),
        )
        connection.execute(
            """INSERT INTO platform_operator_role_bindings(
              operator_id,role_code,status,granted_by_operator_id,granted_at,revoked_at)
              VALUES(?,'platform_communications','active',NULL,'2026-08-23T00:00:00Z',NULL)""",
            (operator_id,),
        )
        connection.commit()
    finally:
        connection.close()

    try:
        rollback_last_migration(database_url, migrations_dir=migrations, expected_version="0097")
    except DbError as exc:
        assert exc.code == "db.migration_failed"
    else:
        raise AssertionError("0097 rollback accepted a live communications binding")
    _healthy(database_url)


def test_fresh_schema_installs_current_head(tmp_path: Path) -> None:
    database_url = _url(tmp_path / "fresh.sqlite3")
    result = apply_migrations(database_url)
    assert result.applied[-1] == "0100"
    _healthy(database_url)
