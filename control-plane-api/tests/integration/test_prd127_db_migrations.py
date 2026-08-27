"""PRD-127 Fleet staged-upgrade schema, rollback, and permission guards."""

from __future__ import annotations

import shutil
from contextlib import closing
from pathlib import Path

import pytest
from metahub_control.platform_jobs.catalog import ARTIFACTS
from metahub_control.workspace_access.edge_permissions import (
    load_verified_platform_edge_permissions,
    load_verified_workspace_edge_permissions,
)
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
PRD127_TABLES = {
    "workspace_edge_upgrade_permission_extensions",
    "workspace_edge_upgrade_role_permissions",
    "platform_edge_upgrade_permission_extensions",
    "platform_edge_upgrade_role_permissions",
    "edge_gateway_releases",
    "edge_gateway_release_artifacts",
    "edge_release_channels",
    "edge_maintenance_windows",
    "edge_deployment_release_heads",
    "edge_fleet_rollouts",
    "edge_rollout_batches",
    "edge_rollout_targets",
    "edge_upgrade_commands",
    "edge_upgrade_receipts",
    "edge_rollback_attempts",
    "edge_fleet_upgrade_events",
    "edge_fleet_upgrade_outbox",
    "edge_upgrade_action_commands",
    "tenant_deletion_manifest_extensions_prd127",
}
TENANT_TABLES = {
    "edge_maintenance_windows",
    "edge_deployment_release_heads",
    "edge_rollout_targets",
    "edge_upgrade_commands",
    "edge_upgrade_receipts",
    "edge_rollback_attempts",
    "edge_fleet_upgrade_events",
    "edge_fleet_upgrade_outbox",
}
EXISTING_DEPLOYMENT_ID = "edp_12700000000000000000000000000001"
NEW_DEPLOYMENT_ID = "edp_12700000000000000000000000000002"
NOW = "2026-08-28T12:00:00.000Z"


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _healthy(database_url: str) -> None:
    with closing(connect_database(database_url)) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.fixture(scope="module")
def base_0118(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("prd127-0118-base")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 118:
            shutil.copy2(source, migrations / source.name)
    database = root / "base-0118.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied[-1] == "0118"
    apply_seed_data(database_url)
    for filename in (
        "0119_edge_fleet_staged_upgrade_rollback.sql",
        "0119_edge_fleet_staged_upgrade_rollback.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    _healthy(database_url)
    return database, migrations


@pytest.fixture
def migrated_0118(base_0118: tuple[Path, Path], tmp_path: Path) -> tuple[Path, Path]:
    base, migrations = base_0118
    database = tmp_path / "prd127-0118.sqlite3"
    shutil.copy2(base, database)
    return database, migrations


def _owner(connection) -> tuple[str, str, str]:
    row = connection.execute(
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
    assert row is not None
    return tuple(map(str, row))


def _insert_deployment(
    connection, deployment_id: str, *, agent_version: str | None
) -> tuple[str, str, str]:
    workspace_id, tenant_id, account_id = _owner(connection)
    connection.execute(
        """
        INSERT INTO edge_deployments(
          edge_deployment_id,workspace_id,tenant_id,display_name,region_code,mode,
          status,revision,revocation_generation,agent_version,health_code,
          last_connected_at,certificate_expires_at,created_by_account_id,
          revoked_by_account_id,revoked_reason_code,created_at,updated_at,revoked_at)
        VALUES(?,?,?,'PRD-127 migration','cn-test','connected','active',1,1,?,
          'healthy',?,?,?,NULL,NULL,?,?,NULL)
        """,
        (
            deployment_id,
            workspace_id,
            tenant_id,
            agent_version,
            NOW,
            "2026-09-28T12:00:00.000Z",
            account_id,
            NOW,
            NOW,
        ),
    )
    connection.commit()
    return workspace_id, tenant_id, account_id


def test_0118_to_0119_backfill_auto_head_and_roundtrip(
    migrated_0118: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0118
    database_url = _url(database)
    with closing(connect_database(database_url)) as connection:
        workspace_id, tenant_id, _account_id = _insert_deployment(
            connection, EXISTING_DEPLOYMENT_ID, agent_version="1.0.0"
        )

    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0119"]
    with closing(connect_database(database_url)) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            ).fetchall()
        }
        assert PRD127_TABLES <= tables
        assert {
            str(row[0])
            for row in connection.execute(
                "SELECT source_table FROM tenant_deletion_manifest_extensions_prd127"
            ).fetchall()
        } == TENANT_TABLES
        head = connection.execute(
            "SELECT * FROM edge_deployment_release_heads WHERE edge_deployment_id=?",
            (EXISTING_DEPLOYMENT_ID,),
        ).fetchone()
        assert head is not None
        assert head["workspace_id"] == workspace_id
        assert head["tenant_id"] == tenant_id
        assert head["current_version"] == "1.0.0"
        assert head["lkg_version"] == "1.0.0"
        assert head["desired_generation"] == 0
        assert {
            "edge.rollback.manage",
            "edge.upgrade.manage",
            "edge.upgrade.read",
        } <= set(
            load_verified_workspace_edge_permissions(
                connection,
                role_code="owner",
                unavailable_error_code="edge_upgrade.unavailable",
            )
        )
        assert {
            "platform.edge_release.read",
            "platform.edge_rollout.manage",
            "platform.edge_rollout.read",
        } <= set(
            load_verified_platform_edge_permissions(
                connection,
                roles=("platform_ops",),
                unavailable_error_code="fleet.rollout_unavailable",
            )
        )
        assert connection.execute(
            "SELECT artifact_sha256 FROM platform_job_types "
            "WHERE type_code='fleet.rollout.advance'"
        ).fetchone()[0] == ARTIFACTS["fleet.rollout.advance"]

        _insert_deployment(connection, NEW_DEPLOYMENT_ID, agent_version=None)
        new_head = connection.execute(
            "SELECT * FROM edge_deployment_release_heads WHERE edge_deployment_id=?",
            (NEW_DEPLOYMENT_ID,),
        ).fetchone()
        assert new_head is not None
        assert new_head["current_version"] == "0.0.0"
        assert new_head["lkg_version"] == "0.0.0"
    _healthy(database_url)

    assert (
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0119"
        ).rolled_back
        == "0119"
    )
    _healthy(database_url)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0119"]
    with closing(connect_database(database_url)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM edge_deployment_release_heads"
        ).fetchone()[0] == connection.execute(
            "SELECT COUNT(*) FROM edge_deployments"
        ).fetchone()[0]
    _healthy(database_url)


def test_0119_down_guard_rejects_runtime_generation(
    migrated_0118: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0118
    database_url = _url(database)
    with closing(connect_database(database_url)) as connection:
        _insert_deployment(connection, EXISTING_DEPLOYMENT_ID, agent_version="1.0.0")
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0119"]
    with closing(connect_database(database_url)) as connection:
        connection.execute(
            """
            UPDATE edge_deployment_release_heads SET desired_generation=1,
              revision=revision+1,updated_at='2026-08-28T12:00:01.000Z'
            WHERE edge_deployment_id=?
            """,
            (EXISTING_DEPLOYMENT_ID,),
        )
        connection.commit()
    with pytest.raises(DbError) as blocked:
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0119"
        )
    assert blocked.value.code == "db.migration_failed"
    _healthy(database_url)
