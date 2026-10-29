"""PRD-097 onboarding migration backfill and pristine rollback contracts."""

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
PRD097_TABLES = {
    "onboarding_progress",
    "onboarding_step_facts",
    "onboarding_acknowledgements",
    "activation_checkpoints",
    "onboarding_events",
    "onboarding_event_outbox",
}


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _healthy(connection: sqlite3.Connection) -> None:
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _version(connection: sqlite3.Connection) -> str:
    return str(
        connection.execute(
            "SELECT version FROM schema_migrations WHERE status='applied' "
            "ORDER BY version DESC LIMIT 1"
        ).fetchone()[0]
    )


@pytest.fixture(scope="module")
def seeded_0088(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("prd097-seeded-0088")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 88:
            shutil.copy2(source, migrations / source.name)
    database = root / "seeded-0088.sqlite3"
    result = apply_migrations(_url(database), migrations_dir=migrations)
    assert result.applied[-1] == "0088"
    apply_seed_data(_url(database))
    shutil.copy2(
        MIGRATIONS_DIR / "0089_workspace_onboarding.sql",
        migrations / "0089_workspace_onboarding.sql",
    )
    shutil.copy2(
        MIGRATIONS_DIR / "0089_workspace_onboarding.down.sql",
        migrations / "0089_workspace_onboarding.down.sql",
    )
    return database


def test_0088_to_0089_backfill_pristine_down_up(
    seeded_0088: Path,
    tmp_path: Path,
) -> None:
    database = tmp_path / "onboarding-roundtrip.sqlite3"
    shutil.copy2(seeded_0088, database)
    database_url = _url(database)
    migrations = seeded_0088.parent / "migrations"

    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0089"]
    connection = connect_database(database_url)
    try:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            )
        }
        assert PRD097_TABLES <= tables
        workspace_count = connection.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0]
        progress_count = connection.execute(
            "SELECT COUNT(*) FROM onboarding_progress"
        ).fetchone()[0]
        fact_count = connection.execute("SELECT COUNT(*) FROM onboarding_step_facts").fetchone()[0]
        assert workspace_count == progress_count
        assert fact_count == workspace_count * 8
        assert connection.execute(
            """
            SELECT COUNT(*) FROM onboarding_progress
            WHERE completed_steps!=0 OR status!='incomplete' OR generation!=0 OR revision!=1
            """
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM onboarding_step_facts WHERE source='legacy_seed'"
        ).fetchone()[0] > 0
        _healthy(connection)
    finally:
        connection.close()

    result = rollback_last_migration(
        database_url,
        migrations_dir=migrations,
        expected_version="0089",
    )
    assert result.rolled_back == "0089"
    connection = connect_database(database_url)
    try:
        assert _version(connection) == "0088"
        remaining = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            )
        }
        assert PRD097_TABLES.isdisjoint(remaining)
        _healthy(connection)
    finally:
        connection.close()

    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0089"]
    connection = connect_database(database_url)
    try:
        assert _version(connection) == "0089"
        _healthy(connection)
    finally:
        connection.close()


def test_0089_rollback_rejects_changed_projection(
    seeded_0088: Path,
    tmp_path: Path,
) -> None:
    database = tmp_path / "onboarding-non-pristine.sqlite3"
    shutil.copy2(seeded_0088, database)
    database_url = _url(database)
    migrations = seeded_0088.parent / "migrations"
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0089"]

    connection = connect_database(database_url)
    try:
        connection.execute(
            """
            UPDATE onboarding_progress
            SET generation=1,revision=2,updated_at=updated_at
            WHERE workspace_id=(SELECT workspace_id FROM onboarding_progress LIMIT 1)
            """
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(DbError) as blocked:
        rollback_last_migration(
            database_url,
            migrations_dir=migrations,
            expected_version="0089",
        )
    assert blocked.value.code == "db.migration_failed"
    connection = connect_database(database_url)
    try:
        assert _version(connection) == "0089"
        assert connection.execute(
            "SELECT generation FROM onboarding_progress LIMIT 1"
        ).fetchone()[0] == 1
        _healthy(connection)
    finally:
        connection.close()


def test_fresh_schema_installs_0089(tmp_path: Path) -> None:
    database_url = _url(tmp_path / "onboarding-fresh.sqlite3")
    result = apply_migrations(database_url)
    assert result.applied[-1] == "0089"
    assert len(result.applied) == 89
    connection = connect_database(database_url)
    try:
        assert _version(connection) == "0089"
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            )
        }
        assert PRD097_TABLES <= tables
        _healthy(connection)
    finally:
        connection.close()
