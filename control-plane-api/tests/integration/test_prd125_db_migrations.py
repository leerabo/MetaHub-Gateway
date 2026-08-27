"""PRD-125 validation, delivery acceptance, and support-bundle schema guards."""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from metahub_control.workspace_access.edge_permissions import (
    load_verified_workspace_edge_permissions,
)
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.errors import ApiError

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
PRD125_TABLES = {
    "workspace_edge_validation_permission_extensions",
    "workspace_edge_validation_role_permissions",
    "edge_validation_runs",
    "edge_validation_checks",
    "edge_delivery_acceptances",
    "edge_support_bundles",
    "edge_support_bundle_download_audits",
    "edge_delivery_events",
    "edge_delivery_outbox",
    "edge_validation_commands",
    "tenant_deletion_manifest_extensions_prd125",
}
TENANT_TABLES = {
    "edge_validation_runs",
    "edge_validation_checks",
    "edge_delivery_acceptances",
    "edge_support_bundles",
    "edge_support_bundle_download_audits",
    "edge_delivery_events",
    "edge_delivery_outbox",
    "edge_validation_commands",
}
RELEASE_ID = "irl_12400000000000000000000000000000"
DEPLOYMENT_ID = "edp_12500000000000000000000000000000"


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _healthy(database_url: str) -> None:
    with closing(connect_database(database_url)) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.fixture(scope="module")
def base_0116(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("prd125-0116-base")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 116:
            shutil.copy2(source, migrations / source.name)
    database = root / "base-0116.sqlite3"
    assert apply_migrations(_url(database), migrations_dir=migrations).applied[-1] == "0116"
    for filename in (
        "0117_edge_validation_delivery_support_bundles.sql",
        "0117_edge_validation_delivery_support_bundles.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    _healthy(_url(database))
    return database, migrations


@pytest.fixture
def migrated_0116(base_0116: tuple[Path, Path], tmp_path: Path) -> tuple[Path, Path]:
    base, migrations = base_0116
    database = tmp_path / "prd125-0116.sqlite3"
    shutil.copy2(base, database)
    return database, migrations


def test_0116_to_0117_pristine_roundtrip_and_permission_catalog(
    migrated_0116: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0116
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0117"]
    apply_seed_data(database_url)
    with closing(connect_database(database_url)) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            ).fetchall()
        }
        assert PRD125_TABLES <= tables
        assert {
            str(row[0])
            for row in connection.execute(
                "SELECT source_table FROM tenant_deletion_manifest_extensions_prd125"
            ).fetchall()
        } == TENANT_TABLES
        assert {
            "edge.validation.read",
            "edge.validation.manage",
            "edge.delivery.accept",
            "edge.support_bundle.read",
        } <= set(
            load_verified_workspace_edge_permissions(
                connection,
                role_code="owner",
                unavailable_error_code="edge.unavailable",
            )
        )
    _healthy(database_url)

    assert (
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0117"
        ).rolled_back
        == "0117"
    )
    with closing(connect_database(database_url)) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            ).fetchall()
        }
        assert PRD125_TABLES.isdisjoint(tables)
    _healthy(database_url)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0117"]
    _healthy(database_url)


def test_validation_attempts_are_scoped_to_the_input_contract(
    migrated_0116: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0116
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0117"]
    apply_seed_data(database_url)
    with closing(connect_database(database_url)) as connection:
        owner = connection.execute(
            """
            SELECT binding.workspace_id,binding.tenant_id,account.account_id
            FROM workspace_tenant_bindings binding
            JOIN workspace_memberships membership
              ON membership.workspace_id=binding.workspace_id
             AND membership.tenant_id=binding.tenant_id
             AND membership.role_code='owner' AND membership.status='active'
            JOIN global_accounts account ON account.account_id=membership.account_id
            ORDER BY binding.workspace_id LIMIT 1
            """
        ).fetchone()
        assert owner is not None
        workspace_id, tenant_id, account_id = map(str, owner)
        now = "2026-08-27T00:00:00.000Z"
        connection.execute(
            """
            INSERT INTO edge_deployments(
              edge_deployment_id,workspace_id,tenant_id,display_name,region_code,mode,
              status,revision,revocation_generation,agent_version,health_code,
              last_connected_at,certificate_expires_at,created_by_account_id,
              revoked_by_account_id,revoked_reason_code,created_at,updated_at,revoked_at)
            VALUES(?,?,?,'PRD-125 migration','cn-test','connected','active',1,1,
              '1.0.0','healthy',NULL,NULL,?,NULL,NULL,?,?,NULL)
            """,
            (DEPLOYMENT_ID, workspace_id, tenant_id, account_id, now, now),
        )

        def insert_run(run_id: str, contract: str, attempt: int, source: str | None) -> None:
            connection.execute(
                """
                INSERT INTO edge_validation_runs(
                  validation_run_id,workspace_id,tenant_id,edge_deployment_id,
                  source_validation_run_id,attempt,plan_version,deployment_revision,
                  installer_release_id,desired_generation,applied_generation,lkg_generation,
                  input_contract_sha256,requested_check_codes_json,status,outcome,duration_ms,
                  requested_by_account_id,idempotency_sha256,request_sha256,started_at,
                  completed_at,created_at,updated_at)
                VALUES(?,?,?,?,?,?,'edge-delivery-v1',1,?,0,0,0,?,
                  '["device_certificate"]','requested',NULL,NULL,?,?,?,NULL,NULL,?,?)
                """,
                (
                    run_id,
                    workspace_id,
                    tenant_id,
                    DEPLOYMENT_ID,
                    source,
                    attempt,
                    RELEASE_ID,
                    contract,
                    account_id,
                    run_id[-1] * 64,
                    run_id[-2] * 64,
                    now,
                    now,
                ),
            )

        first = "dvr_12500000000000000000000000000001"
        second_contract = "dvr_12500000000000000000000000000002"
        retry = "dvr_12500000000000000000000000000003"
        insert_run(first, "a" * 64, 1, None)
        insert_run(second_contract, "b" * 64, 1, None)
        insert_run(retry, "a" * 64, 2, first)
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
            insert_run("dvr_12500000000000000000000000000004", "a" * 64, 2, first)
        connection.rollback()
    _healthy(database_url)


def test_permission_drift_and_delivery_facts_block_rollback(
    migrated_0116: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0116
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0117"]
    apply_seed_data(database_url)
    with closing(connect_database(database_url)) as connection:
        connection.execute("BEGIN")
        connection.execute("DROP TRIGGER trg_workspace_edge_validation_roles_delete_prd125")
        connection.execute(
            "DELETE FROM workspace_edge_validation_role_permissions "
            "WHERE role_code='admin' AND permission_code='edge.validation.manage'"
        )
        with pytest.raises(ApiError) as drift:
            load_verified_workspace_edge_permissions(
                connection,
                role_code="admin",
                unavailable_error_code="edge.unavailable",
            )
        assert drift.value.code == "edge.unavailable"
        connection.rollback()

        connection.execute(
            """
            INSERT INTO edge_validation_commands(
              edge_validation_command_id,workspace_id,tenant_id,actor_account_id,
              action_code,idempotency_sha256,request_sha256,result_object_id,created_at)
            SELECT 'evc_12500000000000000000000000000001',workspace_id,tenant_id,
              account_id,'edge.validation.start',?,?,
              'dvr_12500000000000000000000000000001','2026-08-27T00:00:00.000Z'
            FROM workspace_memberships WHERE role_code='owner' AND status='active'
            ORDER BY workspace_id LIMIT 1
            """,
            ("c" * 64, "d" * 64),
        )
        connection.commit()

    with pytest.raises(DbError) as blocked:
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0117"
        )
    assert blocked.value.code == "db.migration_failed"
    with closing(connect_database(database_url)) as connection:
        assert connection.execute(
            "SELECT version FROM schema_migrations WHERE status='applied' "
            "ORDER BY version DESC LIMIT 1"
        ).fetchone()[0] == "0117"
        assert (
            connection.execute("SELECT COUNT(*) FROM edge_validation_commands").fetchone()[0]
            == 1
        )
    _healthy(database_url)
