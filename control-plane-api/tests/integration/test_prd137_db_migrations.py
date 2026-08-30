"""PRD-137 tenant-isolation assurance schema and guarded rollback."""

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
PRD137_TABLES = {
    "platform_security_assurance_permission_extensions",
    "platform_security_assurance_role_permissions",
    "workspace_security_assurance_permission_extensions",
    "workspace_security_assurance_role_permissions",
    "security_assurance_profiles",
    "security_assurance_scenarios",
    "security_assessments",
    "assurance_runner_leases",
    "isolation_test_runs",
    "pen_test_findings",
    "security_remediations",
    "security_risk_acceptances",
    "assurance_cleanup_journal",
    "security_assurance_events",
    "security_assurance_event_outbox",
    "security_assurance_action_commands",
    "security_assurance_scheduler_state",
    "tenant_deletion_manifest_extensions_prd137",
}


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
        (
            operator_id,
            account["account_id"],
            "2026-08-31T00:00:00.000Z",
            "2026-08-31T00:00:00.000Z",
        ),
    )
    return operator_id


def _insert_assessment(connection, owner, operator_id: str, assessment_id: str) -> None:
    profile = connection.execute(
        """SELECT * FROM security_assurance_profiles
            WHERE profile_code='incremental_release' AND profile_version=1"""
    ).fetchone()
    assert profile is not None
    stamp = "2026-08-31T00:00:00.000Z"
    connection.execute(
        """INSERT INTO security_assessments(
             security_assessment_id,workspace_id,tenant_id,profile_code,profile_version,
             profile_sha256,profile_signature_sha256,code_tree_sha256,
             migration_set_sha256,contract_set_sha256,artifact_sha256,scope_sha256,
             status,generation,requester_operator_id,environment_receipt_sha256,
             summary_sha256,revision,requested_at,started_at,finished_at,updated_at)
           VALUES(?,?,?,'incremental_release',1,?,?,?,?,?,?,?,'failed',1,?,?,?,3,?,?,?,?)""",
        (
            assessment_id,
            owner["workspace_id"],
            owner["tenant_id"],
            profile["profile_sha256"],
            profile["signature_sha256"],
            "1" * 64,
            "2" * 64,
            "3" * 64,
            "4" * 64,
            "5" * 64,
            operator_id,
            "6" * 64,
            "7" * 64,
            stamp,
            stamp,
            stamp,
            stamp,
        ),
    )


def _insert_run(connection, owner, assessment_id: str, run_id: str) -> None:
    stamp = "2026-08-31T00:00:00.000Z"
    connection.execute(
        """INSERT INTO isolation_test_runs(
             isolation_test_run_id,security_assessment_id,workspace_id,tenant_id,
             scenario_code,source_subject_sha256,target_subject_sha256,
             owner_registry_version,runner_image_sha256,seed_receipt_sha256,
             cleanup_receipt_sha256,outcome,duration_ms,evidence_sha256,
             fencing_generation,started_at,finished_at)
           VALUES(?,?,?,?,'api_idor',?,?,1,?,?,?,'failed',1,?,1,?,?)""",
        (
            run_id,
            assessment_id,
            owner["workspace_id"],
            owner["tenant_id"],
            "8" * 64,
            "9" * 64,
            "a" * 64,
            "b" * 64,
            "c" * 64,
            "d" * 64,
            stamp,
            stamp,
        ),
    )


def _insert_finding(connection, owner, assessment_id: str, run_id: str, finding_id: str) -> None:
    stamp = "2026-08-31T00:00:00.000Z"
    connection.execute(
        """INSERT INTO pen_test_findings(
             pen_test_finding_id,security_assessment_id,isolation_test_run_id,
             workspace_id,tenant_id,owner_kind,contract_code,severity,weakness_code,
             status,sla_deadline_at,evidence_sha256,revision,first_seen_at,last_seen_at,closed_at)
           VALUES(?,?,?, ?,?,'workspace_resource','owner.api','p0','idor_read',
             'open','2026-09-01T00:00:00.000Z',?,1,?,?,NULL)""",
        (
            finding_id,
            assessment_id,
            run_id,
            owner["workspace_id"],
            owner["tenant_id"],
            "d" * 64,
            stamp,
            stamp,
        ),
    )


@pytest.fixture(scope="module")
def base_0128(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("prd137-0128-base")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 128:
            shutil.copy2(source, migrations / source.name)
    database = root / "base-0128.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied[-1] == "0128"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", secrets.token_urlsafe(32))
        apply_seed_data(database_url)
    for filename in (
        "0129_tenant_isolation_security_assurance.sql",
        "0129_tenant_isolation_security_assurance.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    return database, migrations


@pytest.fixture
def migrated_0128(base_0128: tuple[Path, Path], tmp_path: Path) -> tuple[Path, Path]:
    base, migrations = base_0128
    database = tmp_path / "prd137-0128.sqlite3"
    shutil.copy2(base, database)
    return database, migrations


def test_0128_to_0129_catalog_manifest_and_roundtrip(
    migrated_0128: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0128
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0129"]
    with closing(connect_database(database_url)) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            ).fetchall()
        }
        assert PRD137_TABLES <= tables
        assert connection.execute(
            "SELECT COUNT(*) FROM platform_security_assurance_role_permissions"
        ).fetchone()[0] == 8
        assert connection.execute(
            "SELECT COUNT(*) FROM workspace_security_assurance_role_permissions"
        ).fetchone()[0] == 5
        assert connection.execute(
            "SELECT COUNT(*) FROM security_assurance_profiles"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM security_assurance_scenarios"
        ).fetchone()[0] == 10
        assert connection.execute(
            "SELECT COUNT(*) FROM tenant_deletion_manifest_extensions_prd137"
        ).fetchone()[0] == 10
    _healthy(database_url)
    assert rollback_last_migration(
        database_url, migrations_dir=migrations, expected_version="0129"
    ).rolled_back == "0129"
    _healthy(database_url)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0129"]
    _healthy(database_url)


def test_0129_rejects_cross_owner_child_facts(
    migrated_0128: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0128
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0129"]
    with closing(connect_database(database_url)) as connection:
        owner = connection.execute(
            "SELECT workspace_id,tenant_id FROM workspace_tenant_bindings LIMIT 1"
        ).fetchone()
        assert owner is not None
        operator_id = _platform_operator(connection)
        connection.execute(
            """INSERT INTO tenants(
                 tenant_id,slug,name,service_mode,status,created_at,updated_at)
               VALUES(?,'prd137-cross-owner','PRD-137 Cross Owner','internal_only',
                 'active','2026-08-31T00:00:00.000Z','2026-08-31T00:00:00.000Z')""",
            ("ten_" + "9" * 32,),
        )
        other = connection.execute(
            """SELECT workspace_id,tenant_id FROM workspace_tenant_bindings
                WHERE tenant_id=?""",
            ("ten_" + "9" * 32,),
        ).fetchone()
        assert other is not None and other["workspace_id"] != owner["workspace_id"]

        assessment_id = "sas_" + "1" * 32
        run_id = "itr_" + "2" * 32
        finding_id = "ptf_" + "3" * 32
        event_id = "ase_" + "4" * 32
        _insert_assessment(connection, owner, operator_id, assessment_id)

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO assurance_runner_leases(
                     security_assessment_id,workspace_id,tenant_id,worker_id,
                     lease_token_sha256,fencing_generation,lease_expires_at,
                     permission_revision,created_at,updated_at)
                   VALUES(?,?,?,'prd137-worker',?,1,'2026-09-01T00:00:00.000Z',1,?,?)""",
                (
                    assessment_id,
                    other["workspace_id"],
                    other["tenant_id"],
                    "a" * 64,
                    "2026-08-31T00:00:00.000Z",
                    "2026-08-31T00:00:00.000Z",
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_run(connection, other, assessment_id, "itr_" + "9" * 32)
        _insert_run(connection, owner, assessment_id, run_id)

        with pytest.raises(sqlite3.IntegrityError):
            _insert_finding(
                connection, other, assessment_id, run_id, "ptf_" + "9" * 32
            )
        _insert_finding(connection, owner, assessment_id, run_id, finding_id)

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO security_remediations(
                     remediation_id,pen_test_finding_id,workspace_id,tenant_id,
                     change_sha256,verification_assessment_id,outcome,
                     requester_operator_id,approver_operator_id,finding_revision,
                     created_at,verified_at)
                   VALUES(?,?,?,?,?,NULL,'proposed',?,NULL,1,?,NULL)""",
                (
                    "rem_" + "9" * 32,
                    finding_id,
                    other["workspace_id"],
                    other["tenant_id"],
                    "e" * 64,
                    operator_id,
                    "2026-08-31T00:00:00.000Z",
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO security_risk_acceptances(
                     risk_acceptance_id,pen_test_finding_id,workspace_id,tenant_id,
                     scope_sha256,compensating_controls_json,status,owner_membership_id,
                     owner_account_id,security_membership_id,security_account_id,
                     platform_reviewer_operator_id,expires_at,revision,requested_at,
                     approved_at,updated_at)
                   VALUES(?,?,?,?,?,'["network_isolation"]','pending_owner',
                     NULL,NULL,NULL,NULL,NULL,'2026-09-01T00:00:00.000Z',1,?,NULL,?)""",
                (
                    "rsk_" + "9" * 32,
                    finding_id,
                    other["workspace_id"],
                    other["tenant_id"],
                    "f" * 64,
                    "2026-08-31T00:00:00.000Z",
                    "2026-08-31T00:00:00.000Z",
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO assurance_cleanup_journal(
                     cleanup_journal_id,isolation_test_run_id,workspace_id,tenant_id,
                     fixture_code,fixture_id_sha256,environment_receipt_sha256,status,
                     cleanup_receipt_sha256,created_at,updated_at)
                   VALUES(?,?,?,?, 'source_subject',?,?,'deleted',?,?,?)""",
                (
                    "acj_" + "9" * 32,
                    run_id,
                    other["workspace_id"],
                    other["tenant_id"],
                    "1" * 64,
                    "2" * 64,
                    "3" * 64,
                    "2026-08-31T00:00:00.000Z",
                    "2026-08-31T00:00:00.000Z",
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO security_assurance_events(
                     security_assurance_event_id,event_type,workspace_id,tenant_id,
                     object_type,object_id,object_revision,payload_json,payload_sha256,
                     request_id,trace_id,occurred_at)
                   VALUES(?,'security.finding_created',?,?,'finding',?,1,'{}',?,
                     'req-cross-owner','trc-cross-owner',?)""",
                (
                    "ase_" + "9" * 32,
                    other["workspace_id"],
                    other["tenant_id"],
                    finding_id,
                    "4" * 64,
                    "2026-08-31T00:00:00.000Z",
                ),
            )
        connection.execute(
            """INSERT INTO security_assurance_events(
                 security_assurance_event_id,event_type,workspace_id,tenant_id,
                 object_type,object_id,object_revision,payload_json,payload_sha256,
                 request_id,trace_id,occurred_at)
               VALUES(?,'security.finding_created',?,?,'finding',?,1,'{}',?,
                 'req-owner','trc-owner',?)""",
            (
                event_id,
                owner["workspace_id"],
                owner["tenant_id"],
                finding_id,
                "4" * 64,
                "2026-08-31T00:00:00.000Z",
            ),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO security_assurance_event_outbox(
                     security_assurance_event_outbox_id,security_assurance_event_id,
                     workspace_id,tenant_id,status,attempt_count,next_attempt_at,
                     delivered_at,created_at,updated_at)
                   VALUES(?,?,?,?,'pending',0,?,NULL,?,?)""",
                (
                    "aso_" + "9" * 32,
                    event_id,
                    other["workspace_id"],
                    other["tenant_id"],
                    "2026-08-31T00:00:00.000Z",
                    "2026-08-31T00:00:00.000Z",
                    "2026-08-31T00:00:00.000Z",
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO security_assurance_action_commands(
                     security_assurance_action_command_id,actor_kind,actor_id,
                     workspace_id,tenant_id,action_code,idempotency_key_sha256,
                     request_sha256,result_object_id,result_revision,created_at)
                   VALUES(?,'platform_operator',?,?,?,'assessment.create',?,?,?,3,?)""",
                (
                    "asc_" + "9" * 32,
                    operator_id,
                    other["workspace_id"],
                    other["tenant_id"],
                    "5" * 64,
                    "6" * 64,
                    assessment_id,
                    "2026-08-31T00:00:00.000Z",
                ),
            )
        connection.rollback()
    _healthy(database_url)


def test_0129_down_guard_rejects_assurance_fact(
    migrated_0128: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0128
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0129"]
    with closing(connect_database(database_url)) as connection:
        owner = connection.execute(
            "SELECT workspace_id,tenant_id FROM workspace_tenant_bindings LIMIT 1"
        ).fetchone()
        assert owner is not None
        operator_id = _platform_operator(connection)
        _insert_assessment(
            connection, owner, operator_id, "sas_" + "3" * 32
        )
        connection.commit()
    with pytest.raises(DbError) as blocked:
        rollback_last_migration(database_url, migrations_dir=migrations, expected_version="0129")
    assert blocked.value.code == "db.migration_failed"
    _healthy(database_url)


def test_fresh_schema_installs_prd137_head(tmp_path: Path) -> None:
    database_url = _url(tmp_path / "fresh.sqlite3")
    assert apply_migrations(database_url).applied[-1] == "0129"
    _healthy(database_url)
