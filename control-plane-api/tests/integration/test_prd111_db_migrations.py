"""PRD-111 migration, rollback, fencing, and owner contracts."""

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
PRD111_TABLES = {
    "platform_dr_permission_extensions",
    "platform_dr_role_permissions",
    "platform_regions",
    "failover_plans",
    "disaster_recovery_runs",
    "failover_approvals",
    "recovery_points",
    "restore_evidence",
    "region_writer_leases",
    "dr_commands",
    "dr_event_outbox",
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
def migrated_0102(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("prd111-migrated-0102")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 102:
            shutil.copy2(source, migrations / source.name)
    database = root / "prd111-0102.sqlite3"
    assert apply_migrations(_url(database), migrations_dir=migrations).applied[-1] == "0102"
    apply_seed_data(_url(database))
    for filename in (
        "0103_multi_region_disaster_recovery.sql",
        "0103_multi_region_disaster_recovery.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    return database, migrations


def test_0102_to_0103_pristine_roundtrip(migrated_0102: tuple[Path, Path], tmp_path: Path) -> None:
    source, migrations = migrated_0102
    database = tmp_path / "roundtrip.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)

    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0103"]
    connection = connect_database(database_url)
    try:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert PRD111_TABLES <= tables
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT region_code,residency_group,writer_status,failover_epoch,revision "
                "FROM platform_regions ORDER BY region_code"
            ).fetchall()
        ] == [
            ("cn-east-1", "cn-main", "standby", 0, 1),
            ("cn-north-1", "cn-main", "active", 0, 1),
        ]
        assert tuple(
            connection.execute(
                "SELECT status,revision FROM platform_job_types WHERE type_code='dr.run'"
            ).fetchone()
        ) == ("active", 2)
        assert (
            connection.execute("SELECT COUNT(*) FROM platform_dr_role_permissions").fetchone()[0]
            == 9
        )
    finally:
        connection.close()
    _healthy(database_url)

    assert (
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0103"
        ).rolled_back
        == "0103"
    )
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0103"]
    _healthy(database_url)


def test_region_change_and_restore_owner_facts_block_or_fence(
    migrated_0102: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0102
    database = tmp_path / "facts.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0103"]
    connection = connect_database(database_url)
    try:
        now = connection.execute("SELECT datetime('now','+1 second')").fetchone()[0]
        connection.execute(
            """UPDATE platform_regions SET health_status='degraded',revision=revision+1,
                 updated_at=? WHERE region_code='cn-east-1'""",
            (now,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """UPDATE platform_regions SET residency_group='cn-other',revision=revision+1,
                     updated_at=datetime('now','+2 seconds') WHERE region_code='cn-east-1'"""
            )
        source_account = dict(
            connection.execute("SELECT * FROM global_accounts ORDER BY account_id LIMIT 1")
            .fetchone()
        )
        source_account.update(
            {
                "account_id": "gac_" + "d112" * 8,
                "email_normalized": "prd111-owner-test@example.invalid",
                "phone_e164": None,
                "display_name": "PRD111 Owner Test",
                "status": "active",
                "login_enabled": 1,
                "failed_login_count": 0,
                "locked_until": None,
                "last_login_at": None,
            }
        )
        account_columns = tuple(source_account)
        connection.execute(
            f"INSERT INTO global_accounts({','.join(account_columns)}) "
            f"VALUES({','.join('?' for _ in account_columns)})",
            tuple(source_account[column] for column in account_columns),
        )
        operator_id = "pto_" + "d112" * 8
        connection.execute(
            """INSERT INTO platform_operators(
                 operator_id,account_id,status,revision,created_by_operator_id,
                 created_at,updated_at)
               VALUES(?,?,'active',1,NULL,datetime('now'),datetime('now'))""",
            (operator_id, source_account["account_id"]),
        )
        source_tenant = dict(
            connection.execute("SELECT * FROM tenants ORDER BY tenant_id LIMIT 1").fetchone()
        )
        alternate_tenant_id = "ten_" + "d112" * 8
        source_tenant.update(
            {
                "tenant_id": alternate_tenant_id,
                "slug": "prd111-owner-mismatch",
                "name": "PRD111 Owner Mismatch",
                "status": "active",
                "updated_at": now,
            }
        )
        tenant_columns = tuple(source_tenant)
        connection.execute(
            f"INSERT INTO tenants({','.join(tenant_columns)}) "
            f"VALUES({','.join('?' for _ in tenant_columns)})",
            tuple(source_tenant[column] for column in tenant_columns),
        )
        binding = connection.execute(
            "SELECT workspace_id,tenant_id FROM workspace_tenant_bindings "
            "WHERE tenant_id<>? ORDER BY workspace_id LIMIT 1",
            (alternate_tenant_id,),
        ).fetchone()
        plan = connection.execute(
            "SELECT * FROM failover_plans WHERE plan_code='control-north-to-east'"
        ).fetchone()
        assert binding is not None and plan is not None
        run_id = "drr_" + "d112" * 8
        connection.execute(
            """INSERT INTO disaster_recovery_runs(
                 disaster_recovery_run_id,run_kind,failover_plan_id,plan_version,
                 plan_manifest_sha256,source_region_id,target_region_id,expected_epoch,
                 fencing_epoch,reason_code,status,platform_job_id,initiated_by_operator_id,
                 request_id,trace_id,safe_outcome_code,revision,started_at,completed_at,
                 created_at,updated_at)
               VALUES(?,'drill',?,?,?, ?,?,0,1,'quarterly_drill','planned',NULL,?,
                 'request-prd111-owner','trace-prd111-owner',NULL,1,NULL,NULL,?,?)""",
            (
                run_id,
                plan["failover_plan_id"],
                plan["version"],
                plan["manifest_sha256"],
                plan["source_region_id"],
                plan["target_region_id"],
                operator_id,
                now,
                now,
            ),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                """INSERT INTO restore_evidence(
                     restore_evidence_id,disaster_recovery_run_id,workspace_id,tenant_id,
                     component_code,before_watermark_sha256,after_watermark_sha256,
                     integrity_status,rpo_outcome,rto_outcome,duration_seconds,
                     manifest_sha256,created_at)
                   VALUES(?,?,?,?, 'database',?,?,'verified','met','met',1,?,?)""",
                (
                    "rev_" + "d112" * 8,
                    run_id,
                    binding["workspace_id"],
                    alternate_tenant_id,
                    "a" * 64,
                    "b" * 64,
                    "c" * 64,
                    now,
                ),
            )
        connection.rollback()
    finally:
        connection.close()
    with pytest.raises(DbError):
        rollback_last_migration(database_url, migrations_dir=migrations, expected_version="0103")
    _healthy(database_url)


def test_fresh_schema_installs_current_head(tmp_path: Path) -> None:
    database_url = _url(tmp_path / "fresh.sqlite3")
    assert apply_migrations(database_url).applied[-1] == "0103"
    _healthy(database_url)
