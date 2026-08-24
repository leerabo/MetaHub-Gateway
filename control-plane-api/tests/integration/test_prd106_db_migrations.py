"""PRD-106 platform job migration invariants and guarded rollback."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from metahub_shared.db import apply_migrations, connect_database, rollback_last_migration
from metahub_shared.db.errors import DbError

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
PRD106_TABLES = {
    "platform_job_permission_extensions",
    "platform_job_role_permissions",
    "platform_job_types",
    "platform_work_queues",
    "platform_jobs",
    "platform_job_attempts",
    "platform_scheduler_leases",
    "platform_job_checkpoints",
    "platform_job_effects",
    "platform_dead_letter_jobs",
    "platform_job_event_outbox",
    "platform_job_commands",
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
def migrated_0097(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("prd106-migrated-0097")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 97:
            shutil.copy2(source, migrations / source.name)
    database = root / "prd106-0097.sqlite3"
    assert apply_migrations(_url(database), migrations_dir=migrations).applied[-1] == "0097"
    for filename in (
        "0098_platform_job_runtime.sql",
        "0098_platform_job_runtime.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    return database, migrations


def test_0097_to_0098_pristine_roundtrip(
    migrated_0097: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0097
    database = tmp_path / "roundtrip.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0098"]
    connection = connect_database(database_url)
    try:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert PRD106_TABLES <= tables
        migration_sql = (MIGRATIONS_DIR / "0098_platform_job_runtime.sql").read_text(
            encoding="utf-8"
        )
        assert "batch_jobs" not in migration_sql
        assert "batch_tasks" not in migration_sql
        assert "tenant_id" not in migration_sql
        assert "workspace_id" not in migration_sql
        assert connection.execute("SELECT COUNT(*) FROM platform_job_types").fetchone()[0] == 8
        assert connection.execute("SELECT COUNT(*) FROM platform_work_queues").fetchone()[0] == 3
        permissions = {
            row[0]
            for row in connection.execute(
                "SELECT permission_code FROM platform_job_permission_extensions"
            ).fetchall()
        }
        assert permissions == {
            "platform.job.read",
            "platform.job.manage",
            "platform.job.type_manage",
        }
        roles = connection.execute(
            "SELECT role_code,COUNT(*) FROM platform_job_role_permissions GROUP BY role_code"
        ).fetchall()
        assert [tuple(row) for row in roles] == [
            ("platform_ops", 3),
            ("platform_owner", 3),
        ]
    finally:
        connection.close()
    _healthy(database_url)

    assert rollback_last_migration(
        database_url, migrations_dir=migrations, expected_version="0098"
    ).rolled_back == "0098"
    connection = connect_database(database_url)
    try:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert PRD106_TABLES.isdisjoint(tables)
    finally:
        connection.close()
    _healthy(database_url)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0098"]
    _healthy(database_url)


@pytest.mark.parametrize("fact", ["job", "catalog"])
def test_runtime_facts_and_catalog_changes_block_down(
    migrated_0097: tuple[Path, Path], tmp_path: Path, fact: str
) -> None:
    source, migrations = migrated_0097
    database = tmp_path / f"guarded-{fact}.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0098"]
    connection = connect_database(database_url)
    try:
        if fact == "catalog":
            connection.execute(
                """UPDATE platform_job_types SET status='active',revision=2,
                     updated_at='2026-08-23T00:00:01Z'
                   WHERE type_code='feature.rollout.step'"""
            )
        else:
            connection.execute(
                """INSERT INTO platform_jobs(
                  platform_job_id,type_code,queue_code,priority,status,revision,generation,
                  attempt_count,max_attempts,input_envelope,input_sha256,idempotency_sha256,
                  source_code,request_id,trace_id,safe_outcome_code,scheduled_at,started_at,
                  completed_at,created_at,updated_at)
                VALUES('pjob_00000000000000000000000000000000','platform.runtime.probe',
                  'standard',50,'queued',1,0,0,3,?, ?,?,'platform.runtime.test',
                  'request','trace',NULL,'2026-08-23T00:00:00Z',NULL,NULL,
                  '2026-08-23T00:00:00Z','2026-08-23T00:00:00Z')""",
                ("x" * 40, "1" * 64, "2" * 64),
            )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(DbError) as blocked:
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0098"
        )
    assert blocked.value.code == "db.migration_failed"
    _healthy(database_url)


def test_fresh_schema_installs_current_head(tmp_path: Path) -> None:
    database_url = _url(tmp_path / "fresh.sqlite3")
    result = apply_migrations(database_url)
    assert result.applied[-1] == "0104"
    _healthy(database_url)
