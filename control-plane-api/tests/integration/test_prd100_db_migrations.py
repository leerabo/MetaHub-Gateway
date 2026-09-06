"""PRD-100 production infrastructure migration and guarded rollback."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from metahub_shared.db import (
    apply_migrations,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db import (
    migrate as migration_runtime,
)
from metahub_shared.db.errors import DbError

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
PRD100_TABLES = {
    "platform_infrastructure_permission_extensions",
    "platform_infrastructure_role_permissions",
    "infrastructure_resources",
    "migration_leases",
    "backup_sets",
    "backup_restore_verifications",
    "infrastructure_event_outbox",
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
def migrated_0091(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("prd100-migrated-0091")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 91:
            shutil.copy2(source, migrations / source.name)
    database = root / "prd100-0091.sqlite3"
    assert apply_migrations(_url(database), migrations_dir=migrations).applied[-1] == "0091"
    for filename in (
        "0092_production_infrastructure.sql",
        "0092_production_infrastructure.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    return database, migrations


def test_0091_to_0092_pristine_roundtrip(
    migrated_0091: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0091
    database = tmp_path / "roundtrip.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0092"]
    connection = connect_database(database_url)
    try:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert PRD100_TABLES <= tables
        assert connection.execute(
            "SELECT COUNT(*) FROM infrastructure_resources"
        ).fetchone()[0] == 4
        assert connection.execute(
            "SELECT COUNT(*) FROM platform_infrastructure_role_permissions"
        ).fetchone()[0] == 9
    finally:
        connection.close()
    _healthy(database_url)
    assert rollback_last_migration(
        database_url, migrations_dir=migrations, expected_version="0092"
    ).rolled_back == "0092"
    connection = connect_database(database_url)
    try:
        remaining = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert PRD100_TABLES.isdisjoint(remaining)
    finally:
        connection.close()
    _healthy(database_url)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0092"]
    _healthy(database_url)


def test_backup_fact_blocks_0092_down(
    migrated_0091: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0091
    database = tmp_path / "non-pristine.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0092"]
    connection = connect_database(database_url)
    try:
        connection.execute(
            """
            INSERT INTO migration_leases(
              migration_lease_id,backend_code,lease_name,owner_id,lease_token_sha256,
              generation,status,expires_at,acquired_at,released_at)
            VALUES('mgl_11111111111111111111111111111111','sqlite','schema-migration',
              'prd100-worker','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
              1,'active',datetime('now','+5 minutes'),datetime('now'),NULL)
            """
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(DbError) as blocked:
        rollback_last_migration(
            database_url,
            migrations_dir=migrations,
            expected_version="0092",
        )
    assert blocked.value.code == "db.migration_failed"
    _healthy(database_url)


def test_fresh_schema_installs_current_head(tmp_path: Path) -> None:
    database_url = _url(tmp_path / "fresh.sqlite3")
    result = apply_migrations(database_url)
    assert result.applied[-1] == "0139"
    _healthy(database_url)


def test_postgresql_migrations_dispatch_to_native_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []

    class Connection:
        def close(self) -> None:
            calls.append(("close", self))

    connection = Connection()

    def connect(_database_url: str) -> Connection:
        return connection

    def apply_native(active_connection, **_kwargs):
        calls.append(("apply", active_connection))
        return migration_runtime.MigrationResult(
            run_id="dbm_test",
            applied=["0139"],
            skipped=[],
        )

    def rollback_native(active_connection, **_kwargs):
        calls.append(("rollback", active_connection))
        return migration_runtime.MigrationResult(
            run_id="dbr_test",
            applied=[],
            skipped=[],
            rolled_back="0140",
        )

    monkeypatch.setenv("METAHUB_ENV", "test")
    monkeypatch.setattr(migration_runtime, "connect_database", connect)
    monkeypatch.setattr(migration_runtime, "_apply_postgresql_migrations", apply_native)
    monkeypatch.setattr(migration_runtime, "_rollback_postgresql_migration", rollback_native)
    database_url = "postgresql://database.test/metahub?sslmode=require"

    assert apply_migrations(database_url).applied == ["0139"]
    assert rollback_last_migration(database_url).rolled_back == "0140"
    assert [name for name, _connection in calls] == [
        "apply",
        "close",
        "rollback",
        "close",
    ]
    assert all(active is connection for _name, active in calls)
