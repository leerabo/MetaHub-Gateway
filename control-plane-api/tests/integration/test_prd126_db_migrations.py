"""PRD-126 Fleet health and controlled diagnostic schema guards."""

from __future__ import annotations

import shutil
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
PRD126_TABLES = {
    "workspace_edge_diagnostic_permission_extensions",
    "workspace_edge_diagnostic_role_permissions",
    "edge_health_snapshots",
    "edge_health_heads",
    "edge_fleet_deployment_index",
    "edge_diagnostic_grants",
    "edge_diagnostic_commands",
    "edge_diagnostic_results",
    "edge_diagnostic_events",
    "edge_diagnostic_outbox",
    "edge_diagnostic_action_commands",
    "tenant_deletion_manifest_extensions_prd126",
}
TENANT_TABLES = {
    "edge_health_snapshots",
    "edge_health_heads",
    "edge_fleet_deployment_index",
    "edge_diagnostic_grants",
    "edge_diagnostic_commands",
    "edge_diagnostic_results",
    "edge_diagnostic_events",
    "edge_diagnostic_outbox",
    "edge_diagnostic_action_commands",
}
DEPLOYMENT_ID = "edp_12600000000000000000000000000000"
NOW = "2026-08-27T12:00:00.000Z"


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _healthy(database_url: str) -> None:
    with closing(connect_database(database_url)) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.fixture(scope="module")
def base_0117(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("prd126-0117-base")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 117:
            shutil.copy2(source, migrations / source.name)
    database = root / "base-0117.sqlite3"
    assert apply_migrations(_url(database), migrations_dir=migrations).applied[-1] == "0117"
    for filename in (
        "0118_edge_fleet_health_controlled_diagnostics.sql",
        "0118_edge_fleet_health_controlled_diagnostics.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    _healthy(_url(database))
    return database, migrations


@pytest.fixture
def migrated_0117(base_0117: tuple[Path, Path], tmp_path: Path) -> tuple[Path, Path]:
    base, migrations = base_0117
    database = tmp_path / "prd126-0117.sqlite3"
    shutil.copy2(base, database)
    return database, migrations


def _seed_deployment(database_url: str) -> tuple[str, str, str]:
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
        connection.execute(
            """
            INSERT INTO edge_deployments(
              edge_deployment_id,workspace_id,tenant_id,display_name,region_code,mode,
              status,revision,revocation_generation,agent_version,health_code,
              last_connected_at,certificate_expires_at,created_by_account_id,
              revoked_by_account_id,revoked_reason_code,created_at,updated_at,revoked_at)
            VALUES(?,?,?,'PRD-126 migration','cn-test','connected','active',1,1,
              '1.2.3','healthy',?,?,?,NULL,NULL,?,?,NULL)
            """,
            (
                DEPLOYMENT_ID,
                workspace_id,
                tenant_id,
                NOW,
                "2026-09-27T12:00:00.000Z",
                account_id,
                NOW,
                NOW,
            ),
        )
        connection.commit()
    return workspace_id, tenant_id, account_id


def test_0117_to_0118_backfill_roundtrip_and_permission_catalog(
    migrated_0117: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0117
    database_url = _url(database)
    workspace_id, tenant_id, _account_id = _seed_deployment(database_url)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0118"]
    with closing(connect_database(database_url)) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            ).fetchall()
        }
        assert PRD126_TABLES <= tables
        assert {
            str(row[0])
            for row in connection.execute(
                "SELECT source_table FROM tenant_deletion_manifest_extensions_prd126"
            ).fetchall()
        } == TENANT_TABLES
        assert {
            "edge.health.read",
            "edge.diagnostic.read",
            "edge.diagnostic.approve",
        } <= set(
            load_verified_workspace_edge_permissions(
                connection,
                role_code="owner",
                unavailable_error_code="edge.unavailable",
            )
        )
        snapshot = connection.execute(
            "SELECT * FROM edge_health_snapshots WHERE edge_deployment_id=?",
            (DEPLOYMENT_ID,),
        ).fetchone()
        assert snapshot is not None
        assert snapshot["source_code"] == "migration_backfill"
        assert snapshot["workspace_id"] == workspace_id
        assert snapshot["tenant_id"] == tenant_id
        assert connection.execute(
            "SELECT COUNT(*) FROM edge_health_heads WHERE edge_deployment_id=?",
            (DEPLOYMENT_ID,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM edge_fleet_deployment_index WHERE edge_deployment_id=?",
            (DEPLOYMENT_ID,),
        ).fetchone()[0] == 1
    _healthy(database_url)

    assert (
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0118"
        ).rolled_back
        == "0118"
    )
    with closing(connect_database(database_url)) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            ).fetchall()
        }
        assert PRD126_TABLES.isdisjoint(tables)
    _healthy(database_url)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0118"]
    _healthy(database_url)


def test_permission_drift_and_runtime_health_fact_block_rollback(
    migrated_0117: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0117
    database_url = _url(database)
    workspace_id, tenant_id, _account_id = _seed_deployment(database_url)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0118"]
    with closing(connect_database(database_url)) as connection:
        connection.execute("BEGIN")
        connection.execute("DROP TRIGGER trg_workspace_edge_diagnostic_roles_delete_prd126")
        connection.execute(
            "DELETE FROM workspace_edge_diagnostic_role_permissions "
            "WHERE role_code='admin' AND permission_code='edge.diagnostic.approve'"
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
            INSERT INTO edge_health_snapshots(
              edge_health_snapshot_id,workspace_id,tenant_id,edge_deployment_id,generation,
              source_code,source_revision,score_model_version,health_score,health_code,
              certificate_signal,version_signal,capacity_signal,config_signal,telemetry_signal,
              validation_signal,desired_generation,applied_generation,lkg_generation,
              telemetry_lag_seconds,certificate_expires_at,agent_version,snapshot_sha256,
              observed_at,created_at)
            VALUES('ehs_12600000000000000000000000000001',?,?,?,2,'runtime',1,
              'edge-health-v1',80,'healthy','healthy','current','healthy','converged',
              'current','passed',0,0,0,0,'2026-09-27T12:00:00.000Z','1.2.3',?, ?,?)
            """,
            (workspace_id, tenant_id, DEPLOYMENT_ID, "f" * 64, NOW, NOW),
        )
        connection.commit()

    with pytest.raises(DbError) as blocked:
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0118"
        )
    assert blocked.value.code == "db.migration_failed"
    with closing(connect_database(database_url)) as connection:
        assert connection.execute(
            "SELECT version FROM schema_migrations WHERE status='applied' "
            "ORDER BY version DESC LIMIT 1"
        ).fetchone()[0] == "0118"
        assert connection.execute(
            "SELECT COUNT(*) FROM edge_health_snapshots WHERE source_code='runtime'"
        ).fetchone()[0] == 1
    _healthy(database_url)
