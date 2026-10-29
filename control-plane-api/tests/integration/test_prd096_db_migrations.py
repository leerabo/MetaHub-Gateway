"""PRD-096 migration backfill, guards, and pristine rollback contracts."""

from __future__ import annotations

import shutil
import sqlite3
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


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


@pytest.fixture(scope="module")
def migrated_0087(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("prd096-migrations")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 87:
            shutil.copy2(source, migrations / source.name)
    database = root / "platform-0087.sqlite3"
    result = apply_migrations(_url(database), migrations_dir=migrations)
    assert result.applied[-1] == "0087"
    return database


def test_0087_to_0088_backfill_pristine_round_trip_and_guards(
    monkeypatch: pytest.MonkeyPatch,
    migrated_0087: Path,
    tmp_path: Path,
) -> None:
    database = tmp_path / "platform-0088.sqlite3"
    shutil.copy2(migrated_0087, database)
    database_url = _url(database)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", "LocalAdmin!2026")
    apply_seed_data(database_url)
    migrations = tmp_path / "migrations"
    shutil.copytree(migrated_0087.parent / "migrations", migrations)
    shutil.copy2(
        MIGRATIONS_DIR / "0088_platform_workspace_operations.sql",
        migrations / "0088_platform_workspace_operations.sql",
    )
    shutil.copy2(
        MIGRATIONS_DIR / "0088_platform_workspace_operations.down.sql",
        migrations / "0088_platform_workspace_operations.down.sql",
    )

    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0088"]
    connection = connect_database(database_url)
    try:
        counts = connection.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM workspaces),
              (SELECT COUNT(*) FROM workspace_operational_states),
              (SELECT COUNT(*) FROM workspace_operational_states
                WHERE status='active' AND source='migration_backfill'
                  AND revision=1 AND risk_revision=1)
            """
        ).fetchone()
        assert tuple(counts) == (1, 1, 1)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        with pytest.raises(sqlite3.IntegrityError, match="workspace_operational_immutable"):
            connection.execute("DELETE FROM workspace_operational_states")
        connection.rollback()
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd096'"
            ).fetchone()[0]
            == 31
        )
    finally:
        connection.close()

    assert rollback_last_migration(database_url, expected_version="0088").rolled_back == "0088"
    connection = connect_database(database_url)
    try:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE name='workspace_operational_states'"
            ).fetchone()
            is None
        )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0088"]

    connection = connect_database(database_url)
    try:
        connection.execute(
            """
            UPDATE workspace_operational_states
            SET risk_revision=risk_revision+1,updated_at=updated_at
            """
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(DbError) as blocked:
        rollback_last_migration(
            database_url,
            migrations_dir=migrations,
            expected_version="0088",
        )
    assert blocked.value.code == "db.migration_failed"
    connection = connect_database(database_url)
    try:
        assert (
            connection.execute("SELECT risk_revision FROM workspace_operational_states").fetchone()[
                0
            ]
            == 2
        )
        assert (
            connection.execute(
                "SELECT status FROM schema_migrations WHERE version='0088'"
            ).fetchone()[0]
            == "applied"
        )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()
