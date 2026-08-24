"""PRD-108 configuration distribution migration and rollback invariants."""

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
PRD108_TABLES = {
    "platform_config_permission_extensions",
    "platform_config_role_permissions",
    "config_snapshots",
    "config_snapshot_heads",
    "config_deltas",
    "config_acks",
    "cache_watermarks",
    "config_rebuild_jobs",
    "config_event_outbox",
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
def migrated_0099(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("prd108-migrated-0099")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 99:
            shutil.copy2(source, migrations / source.name)
    database = root / "prd108-0099.sqlite3"
    assert apply_migrations(_url(database), migrations_dir=migrations).applied[-1] == "0099"
    for filename in (
        "0100_tenant_config_distribution.sql",
        "0100_tenant_config_distribution.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    return database, migrations


def test_0099_to_0100_pristine_roundtrip(
    migrated_0099: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0099
    database = tmp_path / "roundtrip.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)

    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0100"]
    connection = connect_database(database_url)
    try:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert PRD108_TABLES <= tables
        permissions = {
            row[0]
            for row in connection.execute(
                "SELECT permission_code FROM platform_config_permission_extensions"
            ).fetchall()
        }
        assert permissions == {
            "platform.config_distribution.read",
            "platform.config_distribution.rebuild",
        }
        roles = [
            tuple(row)
            for row in connection.execute(
                "SELECT role_code,permission_code FROM platform_config_role_permissions "
                "ORDER BY role_code,permission_code"
            ).fetchall()
        ]
        assert roles == [
            ("platform_ops", "platform.config_distribution.read"),
            ("platform_ops", "platform.config_distribution.rebuild"),
            ("platform_owner", "platform.config_distribution.read"),
            ("platform_owner", "platform.config_distribution.rebuild"),
            ("platform_security", "platform.config_distribution.read"),
        ]
        assert tuple(
            connection.execute(
                "SELECT status,revision FROM platform_job_types "
                "WHERE type_code='config.snapshot.rebuild'"
            ).fetchone()
        ) == ("active", 2)
    finally:
        connection.close()
    _healthy(database_url)

    assert rollback_last_migration(
        database_url, migrations_dir=migrations, expected_version="0100"
    ).rolled_back == "0100"
    connection = connect_database(database_url)
    try:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert PRD108_TABLES.isdisjoint(tables)
        assert tuple(
            connection.execute(
                "SELECT status,revision FROM platform_job_types "
                "WHERE type_code='config.snapshot.rebuild'"
            ).fetchone()
        ) == ("disabled", 1)
    finally:
        connection.close()
    _healthy(database_url)

    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0100"]
    _healthy(database_url)


def test_distribution_fact_blocks_down(
    migrated_0099: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0099
    database = tmp_path / "guarded.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0100"]
    apply_seed_data(database_url)

    connection = connect_database(database_url)
    try:
        owner = connection.execute(
            "SELECT workspace_id,tenant_id FROM workspace_tenant_bindings "
            "WHERE status='active' ORDER BY workspace_id LIMIT 1"
        ).fetchone()
        assert owner is not None
        connection.execute(
            """
            INSERT INTO config_snapshots(
              config_snapshot_id,workspace_id,tenant_id,generation,manifest_envelope,
              manifest_sha256,source_sha256,kms_key_version,signature_algorithm,
              signature_b64,revocation_watermark,published_at)
            VALUES('csn_11111111111111111111111111111111',?,?,1,?,?,?,'test-v1',
              'ed25519',?,0,'2026-08-23T00:00:00Z')
            """,
            (owner["workspace_id"], owner["tenant_id"], "x" * 64, "a" * 64, "b" * 64, "c" * 86),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(DbError) as blocked:
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0100"
        )
    assert blocked.value.code == "db.migration_failed"
    _healthy(database_url)


def test_fresh_schema_installs_current_head(tmp_path: Path) -> None:
    database_url = _url(tmp_path / "fresh.sqlite3")
    assert apply_migrations(database_url).applied[-1] == "0102"
    _healthy(database_url)
