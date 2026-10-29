"""PRD-138 launch-governance schema and guarded rollback."""

from __future__ import annotations

import secrets
import shutil
import sqlite3
from contextlib import closing
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
PRD138_TABLES = {
    "platform_launch_permission_extensions",
    "platform_launch_role_permissions",
    "launch_gate_policies",
    "launch_candidates",
    "launch_candidate_artifacts",
    "launch_evidence",
    "launch_go_no_go_decisions",
    "launch_waves",
    "launch_rollbacks",
    "post_launch_reviews",
    "launch_events",
    "launch_event_outbox",
    "launch_action_commands",
    "launch_scheduler_state",
    "tenant_deletion_manifest_extensions_prd138",
}
STAMP = "2026-08-31T00:00:00.000Z"


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _healthy(database_url: str) -> None:
    with closing(connect_database(database_url)) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def _platform_operator(connection) -> str:
    existing = connection.execute(
        "SELECT operator_id FROM platform_operators WHERE status='active' LIMIT 1"
    ).fetchone()
    if existing is not None:
        return str(existing["operator_id"])
    account = connection.execute(
        "SELECT account_id FROM global_accounts WHERE status='active' LIMIT 1"
    ).fetchone()
    assert account is not None
    operator_id = "pto_" + "a" * 32
    connection.execute(
        """INSERT INTO platform_operators(
             operator_id,account_id,status,revision,created_by_operator_id,created_at,updated_at)
           VALUES(?,?,'active',1,NULL,?,?)""",
        (operator_id, account["account_id"], STAMP, STAMP),
    )
    return operator_id


def _insert_candidate(connection, operator_id: str, candidate_id: str) -> None:
    connection.execute(
        """INSERT INTO launch_candidates(
             launch_candidate_id,candidate_sha256,code_tree_sha256,migration_set_sha256,
             contract_set_sha256,artifact_set_sha256,test_manifest_sha256,
             gate_policy_set_sha256,generation,status,requested_by_operator_id,
             requested_permission_revision,revision,created_at,updated_at,completed_at)
           VALUES(?,?,?,?,?,?,?,?,1,'collecting',?,1,1,?,?,NULL)""",
        (
            candidate_id,
            "0" * 64,
            "1" * 64,
            "2" * 64,
            "3" * 64,
            "4" * 64,
            "5" * 64,
            "6" * 64,
            operator_id,
            STAMP,
            STAMP,
        ),
    )


@pytest.fixture(scope="module")
def base_0129(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("prd138-0129-base")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 129:
            shutil.copy2(source, migrations / source.name)
    database = root / "base-0129.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied[-1] == "0129"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", secrets.token_urlsafe(32))
        apply_seed_data(database_url)
    for filename in (
        "0130_saas_release_candidate_launch_gates.sql",
        "0130_saas_release_candidate_launch_gates.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    return database, migrations


@pytest.fixture
def migrated_0129(base_0129: tuple[Path, Path], tmp_path: Path) -> tuple[Path, Path]:
    base, migrations = base_0129
    database = tmp_path / "prd138-0129.sqlite3"
    shutil.copy2(base, database)
    return database, migrations


def test_0129_to_0130_catalog_manifest_and_roundtrip(
    migrated_0129: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0129
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0130"]
    with closing(connect_database(database_url)) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            ).fetchall()
        }
        assert PRD138_TABLES <= tables
        assert connection.execute(
            "SELECT COUNT(*) FROM platform_launch_permission_extensions"
        ).fetchone()[0] == 11
        assert connection.execute(
            "SELECT COUNT(*) FROM platform_launch_role_permissions"
        ).fetchone()[0] == 17
        assert connection.execute("SELECT COUNT(*) FROM launch_gate_policies").fetchone()[0] == 20
        assert connection.execute(
            "SELECT COUNT(*) FROM tenant_deletion_manifest_extensions_prd138"
        ).fetchone()[0] == 8
    _healthy(database_url)
    assert rollback_last_migration(
        database_url, migrations_dir=migrations, expected_version="0130"
    ).rolled_back == "0130"
    _healthy(database_url)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0130"]
    _healthy(database_url)


def test_0130_rejects_cross_owner_child_facts(
    migrated_0129: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0129
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0130"]
    with closing(connect_database(database_url)) as connection:
        owner = connection.execute(
            "SELECT workspace_id,tenant_id FROM workspace_tenant_bindings LIMIT 1"
        ).fetchone()
        assert owner is not None
        connection.execute(
            """INSERT INTO tenants(
                 tenant_id,slug,name,service_mode,status,created_at,updated_at)
               VALUES(?,'prd138-cross-owner','PRD-138 Cross Owner','internal_only',
                 'active',?,?)""",
            ("ten_" + "9" * 32, STAMP, STAMP),
        )
        other = connection.execute(
            """SELECT workspace_id,tenant_id FROM workspace_tenant_bindings
                WHERE tenant_id=?""",
            ("ten_" + "9" * 32,),
        ).fetchone()
        assert other is not None and other["workspace_id"] != owner["workspace_id"]
        operator_id = _platform_operator(connection)
        candidate_id = "lvc_" + "1" * 32
        _insert_candidate(connection, operator_id, candidate_id)
        gate = connection.execute(
            "SELECT launch_gate_id,verifier_code FROM launch_gate_policies LIMIT 1"
        ).fetchone()
        assert gate is not None
        artifact_id = "lca_" + "2" * 32
        artifact_sha = "7" * 64
        connection.execute(
            """INSERT INTO launch_candidate_artifacts(
                 launch_candidate_artifact_id,launch_candidate_id,launch_gate_id,
                 workspace_id,tenant_id,artifact_sha256,registered_at)
               VALUES(?,?,?,?,?,?,?)""",
            (
                artifact_id,
                candidate_id,
                gate["launch_gate_id"],
                owner["workspace_id"],
                owner["tenant_id"],
                artifact_sha,
                STAMP,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO launch_evidence(
                     launch_evidence_id,launch_candidate_id,launch_gate_id,workspace_id,
                     tenant_id,artifact_sha256,verifier_code,verifier_version,
                     envelope_sha256,signature_sha256,outcome,metric_value,metric_unit,
                     environment_sha256,provenance_sha256,started_at,finished_at,
                     expires_at,submitted_by_operator_id,submitted_permission_revision,created_at)
                   VALUES(?,?,?,?,?,?,?,1,?,?,'passed',1.0,'pass_ratio',?,?,?,?,?,?,1,?)""",
                (
                    "lve_" + "3" * 32,
                    candidate_id,
                    gate["launch_gate_id"],
                    other["workspace_id"],
                    other["tenant_id"],
                    artifact_sha,
                    gate["verifier_code"],
                    "8" * 64,
                    "9" * 64,
                    "a" * 64,
                    "b" * 64,
                    STAMP,
                    "2026-08-31T00:01:00.000Z",
                    "2026-08-31T01:00:00.000Z",
                    operator_id,
                    STAMP,
                ),
            )
        wave_id = "lvw_" + "4" * 32
        connection.execute(
            """INSERT INTO launch_waves(
                 launch_wave_id,launch_candidate_id,wave_kind,wave_sequence,workspace_id,
                 tenant_id,cohort_manifest_sha256,feature_flag_generation,
                 migration_plan_sha256,rollback_plan_sha256,observation_seconds,status,
                 observation_deadline_at,health_watermark_sha256,revision,started_at,
                 completed_at,updated_at)
               VALUES(?,?,'internal',1,?,?,?,1,?,?,60,'pending',NULL,NULL,1,NULL,NULL,?)""",
            (
                wave_id,
                candidate_id,
                owner["workspace_id"],
                owner["tenant_id"],
                "c" * 64,
                "d" * 64,
                "e" * 64,
                STAMP,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO launch_rollbacks(
                     launch_rollback_id,launch_candidate_id,workspace_id,tenant_id,
                     from_wave_kind,to_wave_kind,trigger_code,trigger_sha256,
                     checkpoint_sha256,job_receipt_sha256,verification_sha256,status,
                     requested_by_operator_id,requested_permission_revision,revision,
                     requested_at,verified_at,updated_at)
                   VALUES(?,?,?,?,'internal','pre_launch','operator_request',?,?,?,NULL,
                     'pending',?,1,1,?,NULL,?)""",
                (
                    "lrb_" + "5" * 32,
                    candidate_id,
                    other["workspace_id"],
                    other["tenant_id"],
                    "f" * 64,
                    "a" * 64,
                    "b" * 64,
                    operator_id,
                    STAMP,
                    STAMP,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO launch_events(
                     launch_event_id,launch_candidate_id,workspace_id,tenant_id,event_type,
                     object_type,object_id,payload_sha256,occurred_at)
                   VALUES(?,?,?,?, 'launch.wave_started','wave',?,?,?)""",
                (
                    "lev_" + "6" * 32,
                    candidate_id,
                    other["workspace_id"],
                    other["tenant_id"],
                    wave_id,
                    "c" * 64,
                    STAMP,
                ),
            )
        connection.rollback()
    _healthy(database_url)


def test_0130_down_guard_rejects_candidate_fact(
    migrated_0129: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0129
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0130"]
    with closing(connect_database(database_url)) as connection:
        _insert_candidate(
            connection,
            _platform_operator(connection),
            "lvc_" + "7" * 32,
        )
        connection.commit()
    with pytest.raises(DbError) as blocked:
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0130"
        )
    assert blocked.value.code == "db.migration_failed"
    _healthy(database_url)


def test_fresh_schema_installs_prd138_head(tmp_path: Path) -> None:
    database_url = _url(tmp_path / "fresh.sqlite3")
    # The full schema now includes the GA launch layer added after PRD-138.
    assert apply_migrations(database_url).applied[-1] == "0156"
    _healthy(database_url)
