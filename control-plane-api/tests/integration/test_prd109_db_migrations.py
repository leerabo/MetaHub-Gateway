"""PRD-109 observability migration, constraints, and rollback guards."""

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
PRD109_TABLES = {
    "platform_observability_permission_extensions",
    "platform_observability_role_permissions",
    "service_catalog_entries",
    "service_level_objectives",
    "service_level_objective_heads",
    "telemetry_ingest_head",
    "sli_time_buckets",
    "error_budgets",
    "platform_alerts",
    "platform_alert_transitions",
    "alert_acknowledgements",
    "on_call_routes",
    "telemetry_policies",
    "observability_commands",
    "observability_event_outbox",
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
def migrated_0100(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("prd109-migrated-0100")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 100:
            shutil.copy2(source, migrations / source.name)
    database = root / "prd109-0100.sqlite3"
    assert apply_migrations(_url(database), migrations_dir=migrations).applied[-1] == "0100"
    for filename in (
        "0101_saas_observability_slo_error_budget.sql",
        "0101_saas_observability_slo_error_budget.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    return database, migrations


def test_0100_to_0101_pristine_roundtrip(
    migrated_0100: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0100
    database = tmp_path / "roundtrip.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)

    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0101"]
    connection = connect_database(database_url)
    try:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert PRD109_TABLES <= tables
        permissions = {
            row[0]
            for row in connection.execute(
                "SELECT permission_code FROM platform_observability_permission_extensions"
            ).fetchall()
        }
        assert permissions == {
            "platform.observability.read",
            "platform.observability.manage",
            "platform.observability.security_read",
            "platform.observability.alert_ack",
        }
        roles = [
            tuple(row)
            for row in connection.execute(
                "SELECT role_code,permission_code FROM platform_observability_role_permissions "
                "ORDER BY role_code,permission_code"
            ).fetchall()
        ]
        assert roles == [
            ("platform_ops", "platform.observability.alert_ack"),
            ("platform_ops", "platform.observability.manage"),
            ("platform_ops", "platform.observability.read"),
            ("platform_owner", "platform.observability.alert_ack"),
            ("platform_owner", "platform.observability.manage"),
            ("platform_owner", "platform.observability.read"),
            ("platform_security", "platform.observability.security_read"),
        ]
        assert tuple(
            connection.execute(
                "SELECT status,revision FROM platform_job_types "
                "WHERE type_code='observability.aggregate'"
            ).fetchone()
        ) == ("active", 2)
        assert connection.execute("SELECT COUNT(*) FROM service_catalog_entries").fetchone()[0] == 2
    finally:
        connection.close()
    _healthy(database_url)

    assert rollback_last_migration(
        database_url, migrations_dir=migrations, expected_version="0101"
    ).rolled_back == "0101"
    connection = connect_database(database_url)
    try:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert PRD109_TABLES.isdisjoint(tables)
        assert tuple(
            connection.execute(
                "SELECT status,revision FROM platform_job_types "
                "WHERE type_code='observability.aggregate'"
            ).fetchone()
        ) == ("disabled", 1)
    finally:
        connection.close()
    _healthy(database_url)

    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0101"]
    _healthy(database_url)


def test_observability_fact_blocks_down(
    migrated_0100: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0100
    database = tmp_path / "guarded.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0101"]
    connection = connect_database(database_url)
    try:
        connection.execute(
            """
            UPDATE telemetry_ingest_head SET source_watermark=1,revision=revision+1,
              updated_at=datetime(updated_at,'+1 second')
            """,
        )
        connection.execute(
            """
            INSERT INTO sli_time_buckets(
              sli_time_bucket_id,source_watermark,bucket_started_at,service_code,
              operation_code,region_code,status_class,duration_bucket,tenant_pseudonym,
              good_count,total_count,source_sha256,created_at)
            VALUES('slb_11111111111111111111111111111111',1,'2026-08-23T00:00:00Z',
              'gateway','chat.completions','global','2xx','le_100ms','none',1,1,?,
              '2026-08-23T00:00:01Z')
            """,
            ("a" * 64,),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(DbError) as blocked:
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0101"
        )
    assert blocked.value.code == "db.migration_failed"
    _healthy(database_url)


def test_fresh_schema_installs_current_head(tmp_path: Path) -> None:
    database_url = _url(tmp_path / "fresh.sqlite3")
    assert apply_migrations(database_url).applied[-1] == "0101"
    _healthy(database_url)
