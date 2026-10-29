"""PRD-095 migration catalog, rollback, and immutable audit guards."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
from metahub_shared.db import apply_migrations, connect_database, rollback_last_migration
from metahub_shared.db.errors import DbError

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


@pytest.fixture(scope="module")
def migrated_0087(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("prd095-migrations")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 87:
            shutil.copy2(source, migrations / source.name)
    database = root / "platform-0087.sqlite3"
    result = apply_migrations(_url(database), migrations_dir=migrations)
    assert result.applied[-1] == "0087"
    return database


def test_0087_catalog_and_pristine_round_trip(migrated_0087: Path, tmp_path: Path) -> None:
    database = tmp_path / "roundtrip.sqlite3"
    shutil.copy2(migrated_0087, database)
    database_url = _url(database)
    connection = connect_database(database_url)
    try:
        assert connection.execute("SELECT COUNT(*) FROM platform_roles").fetchone()[0] == 13
        assert connection.execute("SELECT COUNT(*) FROM platform_permissions").fetchone()[0] == 18
        assert (
            connection.execute(
                "SELECT status FROM platform_bootstrap_state WHERE singleton_id=1"
            ).fetchone()[0]
            == "open"
        )
        owner_permissions = connection.execute(
            """
            SELECT COUNT(*) FROM platform_role_permissions
            WHERE role_code='platform_owner'
            """
        ).fetchone()[0]
        assert owner_permissions == 18
        assert (
            connection.execute(
                """
            SELECT COUNT(*) FROM platform_role_permissions
            WHERE role_code='platform_support' AND permission_code='platform.audit.read'
            """
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT catalog_sha256 FROM platform_catalog_state WHERE singleton_id=1"
            ).fetchone()[0]
            == "cded0f381ab07e93ca91b40bd7d95bcc1bf10ca47b71a51e2fd0f3b6043e7afc"
        )
        with pytest.raises(sqlite3.IntegrityError, match="platform_catalog_immutable"):
            connection.execute(
                """
                DELETE FROM platform_role_permissions
                WHERE role_code='platform_support' AND permission_code='platform.audit.read'
                """
            )
        connection.rollback()
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()

    assert rollback_last_migration(database_url, expected_version="0087").rolled_back == "0087"
    connection = connect_database(database_url)
    try:
        assert (
            connection.execute(
                """
            SELECT version FROM schema_migrations
            WHERE status='applied'
            ORDER BY version DESC LIMIT 1
            """
            ).fetchone()[0]
            == "0086"
        )
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE name='platform_operators'"
            ).fetchone()
            is None
        )
    finally:
        connection.close()
    assert apply_migrations(
        database_url, migrations_dir=migrated_0087.parent / "migrations"
    ).applied == ["0087"]


def test_0087_security_fact_blocks_down_atomically(migrated_0087: Path, tmp_path: Path) -> None:
    database = tmp_path / "down-guard.sqlite3"
    shutil.copy2(migrated_0087, database)
    database_url = _url(database)
    connection = connect_database(database_url)
    try:
        connection.execute(
            """
            INSERT INTO platform_audit_logs(
              audit_id,actor_domain,actor_id,action_code,object_type,object_id,reason_code,
              request_id,trace_id,before_sha256,after_sha256,occurred_at)
            VALUES('pau_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','global','gac_test',
              'platform.test','platform_test','object_test','test_fact',
              'request_test','trace_test',NULL,NULL,'2030-01-01T00:00:00Z')
            """
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(DbError):
        rollback_last_migration(database_url, expected_version="0087")
    connection = connect_database(database_url)
    try:
        assert connection.execute("SELECT COUNT(*) FROM platform_audit_logs").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="platform_audit_immutable"):
            connection.execute("DELETE FROM platform_audit_logs")
        connection.rollback()
    finally:
        connection.close()
