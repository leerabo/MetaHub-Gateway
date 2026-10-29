"""Real PostgreSQL baseline and native-delta migration acceptance."""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest
from metahub_shared.db import (
    DbError,
    apply_migrations,
    close_database_pools,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.migrate import (
    MigrationFile,
    _record_failed_postgresql_migration,
)

try:
    import psycopg
    from psycopg import sql
except ImportError:
    psycopg = None
    sql = None


ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = ROOT / "infra" / "migrations"
ADMIN_URL = os.getenv("METAHUB_TEST_POSTGRESQL_ADMIN_URL")

pytestmark = pytest.mark.skipif(
    not ADMIN_URL or psycopg is None,
    reason="METAHUB_TEST_POSTGRESQL_ADMIN_URL and psycopg are required",
)


def _database_url(admin_url: str, database_name: str) -> str:
    parsed = urlsplit(admin_url)
    return urlunsplit(
        (parsed.scheme, parsed.netloc, f"/{database_name}", parsed.query, parsed.fragment)
    )


@pytest.fixture
def postgresql_database() -> Iterator[str]:
    assert ADMIN_URL is not None
    assert psycopg is not None
    assert sql is not None
    database_name = f"metahub_test_{uuid4().hex}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    database_url = _database_url(ADMIN_URL, database_name)
    try:
        yield database_url
    finally:
        close_database_pools()
        with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                    sql.Identifier(database_name)
                )
            )


@pytest.fixture
def native_migrations(tmp_path: Path) -> Path:
    root = tmp_path / "migrations"
    native = root / "postgresql"
    native.mkdir(parents=True)
    for source in MIGRATIONS.glob("*.sql"):
        if source.name[:4].isdigit() and source.name[:4] <= "0139":
            shutil.copy2(source, root / source.name)
    shutil.copy2(
        MIGRATIONS / "postgresql" / "0139_postgresql_baseline.sql",
        native / "0139_postgresql_baseline.sql",
    )
    (native / "0140_postgresql_test_delta.sql").write_text(
        """
CREATE TABLE postgresql_migration_probe(
  probe_id TEXT PRIMARY KEY,
  revision BIGINT NOT NULL CHECK(revision>=1)
);
INSERT INTO postgresql_migration_probe(probe_id,revision) VALUES('probe',1);
""".lstrip(),
        encoding="utf-8",
    )
    (native / "0140_postgresql_test_delta.down.sql").write_text(
        "DROP TABLE postgresql_migration_probe;\n",
        encoding="utf-8",
    )
    return root


def test_postgresql_fresh_repeat_delta_rollback_and_failure_atomicity(
    postgresql_database: str,
    native_migrations: Path,
) -> None:
    fresh = apply_migrations(postgresql_database, migrations_dir=native_migrations)
    assert len(fresh.applied) == 140
    assert fresh.applied[-1] == "0140"

    repeat = apply_migrations(postgresql_database, migrations_dir=native_migrations)
    assert repeat.applied == []
    assert len(repeat.skipped) == 140
    assert repeat.skipped[-1] == "0140"

    connection = connect_database(postgresql_database)
    try:
        state = connection.execute(
            "SELECT status,generator_revision,application_table_count,check_constraint_count,"
            "foreign_key_count,trigger_count,seed_row_count "
            "FROM metahub_bootstrap.baseline_state"
        ).fetchone()
        assert tuple(state) == ("ready", 4, 1074, 10241, 2019, 2486, 4225)
        assert connection.execute(
            "SELECT revision FROM postgresql_migration_probe WHERE probe_id='probe'"
        ).fetchone()[0] == 1
    finally:
        connection.close()

    assert rollback_last_migration(
        postgresql_database,
        migrations_dir=native_migrations,
        expected_version="0140",
    ).rolled_back == "0140"
    connection = connect_database(postgresql_database)
    try:
        assert connection.execute(
            "SELECT to_regclass('public.postgresql_migration_probe')"
        ).fetchone()[0] is None
    finally:
        connection.close()

    assert apply_migrations(
        postgresql_database, migrations_dir=native_migrations
    ).applied == ["0140"]
    broken = native_migrations / "postgresql" / "0141_postgresql_broken_delta.sql"
    broken.write_text(
        "CREATE TABLE postgresql_failed_probe(probe_id TEXT PRIMARY KEY);\n"
        "SELECT * FROM postgresql_missing_relation;\n",
        encoding="utf-8",
    )
    with pytest.raises(DbError) as failed:
        apply_migrations(postgresql_database, migrations_dir=native_migrations)
    assert failed.value.code == "db.migration_failed"

    connection = connect_database(postgresql_database)
    try:
        assert connection.execute(
            "SELECT to_regclass('public.postgresql_failed_probe')"
        ).fetchone()[0] is None
        assert connection.execute(
            "SELECT status FROM schema_migrations WHERE version='0141'"
        ).fetchone()[0] == "failed"
        connection.rollback()
        # A delayed failure writer must not corrupt a concurrently completed retry.
        _record_failed_postgresql_migration(
            connection,
            MigrationFile("0140", "stale_attempt", broken, "stale", "SELECT 1;"),
            time.perf_counter(),
        )
        assert tuple(connection.execute(
            "SELECT name,status FROM schema_migrations WHERE version='0140'"
        ).fetchone()) == ("postgresql_test_delta", "applied")
        connection.execute(
            "UPDATE metahub_bootstrap.baseline_state "
            "SET source_manifest_sha256=repeat('0',64)"
        ).close()
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(DbError) as drifted:
        apply_migrations(postgresql_database, migrations_dir=native_migrations)
    assert drifted.value.code == "db.migration_failed"


def test_postgresql_partial_baseline_fails_closed(
    postgresql_database: str,
) -> None:
    assert psycopg is not None
    baseline = (
        MIGRATIONS / "postgresql" / "0139_postgresql_baseline.sql"
    ).read_text(encoding="utf-8")
    bootstrap = baseline.split("BEGIN;", 1)[0]
    with psycopg.connect(postgresql_database, autocommit=True) as connection:
        connection.execute(bootstrap)

    with pytest.raises(DbError) as partial:
        apply_migrations(postgresql_database)
    assert partial.value.code == "db.migration_failed"
    connection = connect_database(postgresql_database)
    try:
        assert connection.execute(
            "SELECT status FROM metahub_bootstrap.baseline_state"
        ).fetchone()[0] == "installing"
        assert connection.execute(
            "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
        ).fetchone()[0] == 0
    finally:
        connection.close()
