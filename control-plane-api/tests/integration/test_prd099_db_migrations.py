"""PRD-099 metering and billing migration with guarded rollback contracts."""

from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

import pytest
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
PRD099_TABLES = {
    "platform_billing_permission_extensions",
    "platform_billing_role_permissions",
    "platform_meter_definitions",
    "platform_billing_cycles",
    "platform_quota_allocations",
    "platform_meter_readings",
    "platform_billing_summaries",
    "platform_billing_adjustments",
    "platform_meter_outbox",
    "platform_meter_checkpoints",
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
def migrated_0090(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("prd099-migrated-0090")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 90:
            shutil.copy2(source, migrations / source.name)
    database = root / "prd099-0090.sqlite3"
    result = apply_migrations(_url(database), migrations_dir=migrations)
    assert result.applied[-1] == "0090"
    apply_seed_data(_url(database))
    for filename in (
        "0091_platform_metering_billing_summary.sql",
        "0091_platform_metering_billing_summary.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    return database, migrations


def test_0090_to_0091_pristine_roundtrip(
    migrated_0090: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0090
    database = tmp_path / "roundtrip.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0091"]
    connection = connect_database(database_url)
    try:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert PRD099_TABLES <= tables
        assert (
            connection.execute("SELECT COUNT(*) FROM platform_meter_definitions").fetchone()[0]
            == 4
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM platform_billing_role_permissions"
        ).fetchone()[0] == 2
        checkpoint = connection.execute(
            "SELECT source_sequence,source_fact_id FROM platform_meter_checkpoints"
        ).fetchone()
        assert tuple(checkpoint) == (0, None)
    finally:
        connection.close()
    _healthy(database_url)

    rolled = rollback_last_migration(
        database_url, migrations_dir=migrations, expected_version="0091"
    )
    assert rolled.rolled_back == "0091"
    connection = connect_database(database_url)
    try:
        remaining = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert PRD099_TABLES.isdisjoint(remaining)
    finally:
        connection.close()
    _healthy(database_url)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0091"]
    _healthy(database_url)


def test_0091_usage_fact_blocks_down(
    migrated_0090: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0090
    database = tmp_path / "non-pristine.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0091"]
    connection = connect_database(database_url)
    try:
        tenant_id = str(connection.execute("SELECT tenant_id FROM tenants LIMIT 1").fetchone()[0])
        request_id = "req_prd099_" + uuid4().hex
        trace_id = "trc_prd099_" + uuid4().hex
        usage_id = "usg_" + uuid4().hex
        connection.execute(
            """
            INSERT INTO gateway_request_claims(
              tenant_id,request_id,trace_id,virtual_key_id,snapshot_version,
              policy_snapshot_status,claimed_at)
            VALUES(?,?,?,NULL,NULL,'disabled',datetime('now'))
            """,
            (tenant_id, request_id, trace_id),
        )
        connection.execute(
            """
            INSERT INTO request_logs(request_id,tenant_id,trace_id,status,created_at,updated_at)
            VALUES(?,?,?,'success',datetime('now'),datetime('now'))
            """,
            (request_id, tenant_id, trace_id),
        )
        connection.execute(
            """
            INSERT INTO usage_ledger(
              usage_ledger_id,tenant_id,request_id,raw_usage,normalized_usage,cost_amount,
              cost_cny,cost_usd,currency,created_at,updated_at)
            VALUES(?,?,?,'{}','{}',0,0,0,'CNY',datetime('now'),datetime('now'))
            """,
            (usage_id, tenant_id, request_id),
        )
        connection.commit()
        assert connection.execute("SELECT COUNT(*) FROM platform_meter_outbox").fetchone()[0] == 1
    finally:
        connection.close()

    with pytest.raises(DbError) as blocked:
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0091"
        )
    assert blocked.value.code == "db.migration_failed"
    _healthy(database_url)


def test_fresh_schema_installs_0091(tmp_path: Path) -> None:
    database_url = _url(tmp_path / "fresh.sqlite3")
    result = apply_migrations(database_url)
    assert result.applied[-1] == "0091"
    assert "0091" in result.applied
    _healthy(database_url)
