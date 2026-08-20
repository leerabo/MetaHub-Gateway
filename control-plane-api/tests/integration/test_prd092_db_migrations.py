"""PRD-092 migration 0084 round-trip and irreversible-fact guards."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from metahub_shared.db import (
    apply_migrations,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
SELF_SERVICE_TABLES = {
    "registration_intents",
    "password_reset_intents",
    "verification_challenges",
    "account_verifications",
    "consent_records",
    "identity_abuse_buckets",
    "invitation_identity_bindings",
    "identity_lifecycle_events",
    "identity_lifecycle_outbox",
}


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _version(connection) -> str:
    return str(
        connection.execute(
            "SELECT version FROM schema_migrations WHERE status='applied' "
            "ORDER BY version DESC LIMIT 1"
        ).fetchone()[0]
    )


def _tables(connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table'"
        )
        if str(row[0]) in SELF_SERVICE_TABLES
    }


def _healthy(connection) -> None:
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.fixture(scope="module")
def migrations_0084(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("prd092-migrations-0084")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 84:
            shutil.copy2(source, migrations / source.name)
    return migrations


def test_0084_fresh_up_pristine_down_and_reapply(
    tmp_path: Path,
    migrations_0084: Path,
) -> None:
    database_url = _url(tmp_path / "fresh-0084.sqlite3")
    applied = apply_migrations(database_url, migrations_dir=migrations_0084)
    assert applied.applied == [f"{version:04d}" for version in range(1, 85)]

    connection = connect_database(database_url)
    try:
        assert _version(connection) == "0084"
        assert _tables(connection) == SELF_SERVICE_TABLES
        registration_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(registration_intents)")
        }
        assert "email_ciphertext" in registration_columns
        assert "email_normalized" not in registration_columns
        assert {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(password_reset_intents)")
        } >= {"target_account_id", "target_credential_revision", "generation"}
        event_columns = {
            str(row[1]): int(row[3])
            for row in connection.execute("PRAGMA table_info(identity_lifecycle_events)")
        }
        assert event_columns["account_id"] == 0
        _healthy(connection)
    finally:
        connection.close()

    repeated = apply_migrations(database_url, migrations_dir=migrations_0084)
    assert repeated.applied == [] and repeated.skipped[-1] == "0084"
    rolled_back = rollback_last_migration(
        database_url,
        expected_version="0084",
        migrations_dir=migrations_0084,
    )
    assert rolled_back.rolled_back == "0084"

    connection = connect_database(database_url)
    try:
        assert _version(connection) == "0083"
        assert _tables(connection) == set()
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA legacy_alter_table").fetchone()[0] == 0
        _healthy(connection)
    finally:
        connection.close()

    assert apply_migrations(database_url, migrations_dir=migrations_0084).applied == ["0084"]
    connection = connect_database(database_url)
    try:
        assert _version(connection) == "0084"
        assert _tables(connection) == SELF_SERVICE_TABLES
        _healthy(connection)
    finally:
        connection.close()


def test_0084_irreversible_identity_fact_blocks_down_atomically(
    tmp_path: Path,
    migrations_0084: Path,
) -> None:
    database_url = _url(tmp_path / "guard-0084.sqlite3")
    apply_migrations(database_url, migrations_dir=migrations_0084)
    connection = connect_database(database_url)
    try:
        connection.execute(
            """
            INSERT INTO identity_abuse_buckets(
              identity_abuse_bucket_id,scope,bucket_hmac,window_started_at,
              attempt_count,blocked_until,expires_at,updated_at)
            VALUES(?, 'registration_global', ?, '2030-01-01T00:00:00Z',
              1, NULL, '2030-01-01T00:15:00Z', '2030-01-01T00:00:00Z')
            """,
            ("iab_" + "a" * 32, "b" * 64),
        )
        connection.commit()
        _healthy(connection)
    finally:
        connection.close()

    with pytest.raises(DbError):
        rollback_last_migration(
            database_url,
            expected_version="0084",
            migrations_dir=migrations_0084,
        )

    connection = connect_database(database_url)
    try:
        assert _version(connection) == "0084"
        assert _tables(connection) == SELF_SERVICE_TABLES
        assert connection.execute(
            "SELECT COUNT(*) FROM identity_abuse_buckets"
        ).fetchone()[0] == 1
        _healthy(connection)
    finally:
        connection.close()
