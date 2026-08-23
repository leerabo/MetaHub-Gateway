"""PRD-107 feature rollout migration invariants and guarded rollback."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from metahub_shared.db import apply_migrations, connect_database, rollback_last_migration
from metahub_shared.db.errors import DbError

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
PRD107_TABLES = {
    "platform_feature_permission_extensions",
    "platform_feature_role_permissions",
    "feature_flags",
    "feature_flag_versions",
    "tenant_cohorts",
    "tenant_cohort_members",
    "rollout_health_summaries",
    "feature_rollouts",
    "feature_rollout_approvals",
    "flag_evaluation_snapshots",
    "feature_exposures",
    "feature_flag_commands",
    "feature_event_outbox",
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
def migrated_0098(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("prd107-migrated-0098")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 98:
            shutil.copy2(source, migrations / source.name)
    database = root / "prd107-0098.sqlite3"
    assert apply_migrations(_url(database), migrations_dir=migrations).applied[-1] == "0098"
    for filename in (
        "0099_feature_flags_progressive_rollout.sql",
        "0099_feature_flags_progressive_rollout.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    return database, migrations


def test_0098_to_0099_pristine_roundtrip(
    migrated_0098: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0098
    database = tmp_path / "roundtrip.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0099"]
    connection = connect_database(database_url)
    try:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert PRD107_TABLES <= tables
        assert tuple(
            connection.execute(
                "SELECT status,revision FROM platform_job_types "
                "WHERE type_code='feature.rollout.step'"
            ).fetchone()
        ) == ("active", 2)
        permissions = {
            row[0]
            for row in connection.execute(
                "SELECT permission_code FROM platform_feature_permission_extensions"
            ).fetchall()
        }
        assert permissions == {
            "platform.feature.read",
            "platform.feature.write",
            "platform.feature.approve",
            "platform.feature.kill",
        }
        role_counts = {
            row[0]: int(row[1])
            for row in connection.execute(
                "SELECT role_code,COUNT(*) FROM platform_feature_role_permissions "
                "GROUP BY role_code"
            ).fetchall()
        }
        assert role_counts == {
            "platform_ops": 2,
            "platform_owner": 4,
            "release_manager": 3,
        }
    finally:
        connection.close()
    _healthy(database_url)

    assert rollback_last_migration(
        database_url, migrations_dir=migrations, expected_version="0099"
    ).rolled_back == "0099"
    connection = connect_database(database_url)
    try:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert PRD107_TABLES.isdisjoint(tables)
        assert tuple(
            connection.execute(
                "SELECT status,revision FROM platform_job_types "
                "WHERE type_code='feature.rollout.step'"
            ).fetchone()
        ) == ("disabled", 1)
    finally:
        connection.close()
    _healthy(database_url)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0099"]
    _healthy(database_url)


def test_feature_facts_block_down(
    migrated_0098: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0098
    database = tmp_path / "guarded.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0099"]
    connection = connect_database(database_url)
    try:
        connection.execute(
            """
            INSERT INTO rollout_health_summaries(
              rollout_health_summary_id,service_code,region_code,source_watermark,
              health_status,error_rate_bps,latency_p95_ms,summary_sha256,
              observed_at,expires_at)
            VALUES('rhs_00000000000000000000000000000000','gateway','cn-north-1',1,
              'healthy',0,12,?,'2026-08-23T00:00:00Z','2026-08-23T00:05:00Z')
            """,
            ("a" * 64,),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(DbError) as blocked:
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0099"
        )
    assert blocked.value.code == "db.migration_failed"
    _healthy(database_url)


def test_fresh_schema_installs_current_head(tmp_path: Path) -> None:
    database_url = _url(tmp_path / "fresh.sqlite3")
    assert apply_migrations(database_url).applied[-1] == "0099"
    _healthy(database_url)
