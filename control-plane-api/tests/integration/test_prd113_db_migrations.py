"""PRD-113 migration, ownership, immutability, and rollback contracts."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
from metahub_shared.db import (
    apply_migrations,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
PRD113_TABLES = {
    "platform_capacity_permission_extensions",
    "platform_capacity_role_permissions",
    "capacity_service_states",
    "capacity_scenarios",
    "performance_baselines",
    "capacity_forecasts",
    "load_test_runs",
    "chaos_fault_catalog",
    "chaos_experiments",
    "chaos_approvals",
    "platform_cost_allocations",
    "unit_economics_snapshots",
    "performance_rollout_blocks",
    "capacity_commands",
    "capacity_event_outbox",
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
def migrated_0104(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("prd113-migrated-0104")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 104:
            shutil.copy2(source, migrations / source.name)
    database = root / "prd113-0104.sqlite3"
    assert apply_migrations(_url(database), migrations_dir=migrations).applied[-1] == "0104"
    for filename in (
        "0105_capacity_performance_chaos_economics.sql",
        "0105_capacity_performance_chaos_economics.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    return database, migrations


def test_0104_to_0105_pristine_roundtrip(
    migrated_0104: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0104
    database = tmp_path / "roundtrip.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)

    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0105"]
    connection = connect_database(database_url)
    try:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert PRD113_TABLES <= tables
        assert connection.execute("SELECT COUNT(*) FROM capacity_scenarios").fetchone()[0] == 4
        assert connection.execute("SELECT COUNT(*) FROM chaos_fault_catalog").fetchone()[0] == 7
        job_type = connection.execute(
            "SELECT status,revision FROM platform_job_types WHERE type_code='capacity.run'"
        ).fetchone()
        assert job_type is not None
        assert (job_type["status"], job_type["revision"]) == ("active", 2)
    finally:
        connection.close()
    _healthy(database_url)

    assert rollback_last_migration(
        database_url, migrations_dir=migrations, expected_version="0105"
    ).rolled_back == "0105"
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0105"]
    _healthy(database_url)


def test_catalog_relationships_and_immutability(
    migrated_0104: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0104
    database = tmp_path / "contracts.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0105"]
    connection = connect_database(database_url)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                """INSERT INTO capacity_service_states VALUES(
                     'css_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','gateway','invalid-region',
                     1,2,8000,2000,1,datetime('now'),datetime('now'))"""
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="capacity_scenario_immutable"):
            connection.execute(
                "UPDATE capacity_scenarios SET status='disabled' "
                "WHERE scenario_code='gateway-release'"
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            connection.execute(
                """INSERT INTO platform_cost_allocations VALUES(
                     'pca_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb','2026-08','gateway','cn-north-1',
                     'compute',1,'CNY',?,1,datetime('now'))""",
                ("Z" * 64,),
            )
        connection.rollback()
    finally:
        connection.close()
    _healthy(database_url)


def test_down_refuses_durable_capacity_facts(
    migrated_0104: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0104
    database = tmp_path / "down-guard.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0105"]
    connection = connect_database(database_url)
    try:
        connection.execute(
            """INSERT INTO platform_cost_allocations VALUES(
                 'pca_cccccccccccccccccccccccccccccccc','2026-08','gateway','cn-north-1',
                 'compute',1000000,'CNY',?,1,datetime('now'))""",
            ("c" * 64,),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(DbError):
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0105"
        )
    _healthy(database_url)


def test_fresh_schema_installs_current_head(tmp_path: Path) -> None:
    database_url = _url(tmp_path / "fresh.sqlite3")
    assert apply_migrations(database_url).applied[-1] == "0105"
    _healthy(database_url)
