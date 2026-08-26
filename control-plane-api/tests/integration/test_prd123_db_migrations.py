"""PRD-123 cloud-safe Secret reference schema and guarded rollback."""

from __future__ import annotations

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
PRD123_TABLES = {
    "workspace_edge_secret_permission_extensions",
    "workspace_edge_secret_role_permissions",
    "edge_secret_references",
    "edge_secret_sync_batches",
    "edge_secret_events",
    "edge_secret_sync_receipts",
    "edge_secret_event_outbox",
    "tenant_deletion_manifest_extensions_prd123",
}
TENANT_TABLES = {
    "edge_secret_references",
    "edge_secret_sync_batches",
    "edge_secret_events",
    "edge_secret_sync_receipts",
    "edge_secret_event_outbox",
}
FORBIDDEN_COLUMNS = {
    "secret",
    "value",
    "token",
    "ciphertext",
    "endpoint",
    "body",
    "headers",
    "fingerprint",
    "length",
    "message",
}
NOW = "2026-08-27T04:00:00.000Z"


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _healthy(database_url: str) -> None:
    with closing(connect_database(database_url)) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.fixture(scope="module")
def base_0114(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("prd123-0114-base")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 114:
            shutil.copy2(source, migrations / source.name)
    database = root / "base-0114.sqlite3"
    assert apply_migrations(_url(database), migrations_dir=migrations).applied[-1] == "0114"
    for filename in (
        "0115_edge_secret_reference_sync.sql",
        "0115_edge_secret_reference_sync.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    _healthy(_url(database))
    return database, migrations


@pytest.fixture
def migrated_0114(base_0114: tuple[Path, Path], tmp_path: Path) -> tuple[Path, Path]:
    base, migrations = base_0114
    database = tmp_path / "prd123-0114.sqlite3"
    shutil.copy2(base, database)
    return database, migrations


def _owner(connection: sqlite3.Connection) -> tuple[str, str, str]:
    row = connection.execute(
        """
        SELECT binding.workspace_id,binding.tenant_id,membership.account_id
        FROM workspace_tenant_bindings binding
        JOIN workspace_memberships membership
          ON membership.workspace_id=binding.workspace_id
         AND membership.tenant_id=binding.tenant_id
        WHERE binding.status='active' AND membership.status='active'
          AND membership.role_code='owner'
        ORDER BY binding.workspace_id LIMIT 1
        """
    ).fetchone()
    assert row is not None
    return str(row[0]), str(row[1]), str(row[2])


def _insert_deployment(connection: sqlite3.Connection) -> tuple[str, str, str]:
    workspace_id, tenant_id, account_id = _owner(connection)
    deployment_id = "edp_" + "1" * 32
    connection.execute(
        """
        INSERT INTO edge_deployments(
          edge_deployment_id,workspace_id,tenant_id,display_name,region_code,mode,status,
          revision,revocation_generation,agent_version,health_code,last_connected_at,
          certificate_expires_at,created_by_account_id,revoked_by_account_id,
          revoked_reason_code,created_at,updated_at,revoked_at)
        VALUES(?,?,?,'PRD-123 migration gateway','cn-east-1','connected','pending',
          1,1,NULL,'not_registered',NULL,NULL,?,NULL,NULL,?,?,NULL)
        """,
        (deployment_id, workspace_id, tenant_id, account_id, NOW, NOW),
    )
    connection.commit()
    return deployment_id, workspace_id, tenant_id


def _insert_reference(
    connection: sqlite3.Connection,
    *,
    deployment_id: str,
    workspace_id: str,
    tenant_id: str,
) -> str:
    reference_id = "lsr_" + "1" * 32
    connection.execute(
        """
        INSERT INTO edge_secret_references(
          reference_id,schema_version,workspace_id,tenant_id,edge_deployment_id,
          provider_connection_id,label,kind,status,revision,active_version,last_test_outcome,
          capabilities_json,rotated_at,updated_at,projection_sha256,source_event_id,received_at)
        VALUES(?,'1',?,?,?,'pcn_prd123','Primary credential','api_key','active',1,1,
          NULL,'[]',NULL,?,'a'||substr(hex(zeroblob(32)),2),'lse_'||lower(hex(zeroblob(16))),?)
        """,
        (reference_id, workspace_id, tenant_id, deployment_id, NOW, NOW),
    )
    connection.commit()
    return reference_id


def test_0114_to_0115_pristine_roundtrip_and_manifest(
    migrated_0114: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0114
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0115"]
    apply_seed_data(database_url)
    with closing(connect_database(database_url)) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            ).fetchall()
        }
        assert PRD123_TABLES <= tables
        assert {
            str(row[0])
            for row in connection.execute(
                "SELECT source_table FROM tenant_deletion_manifest_extensions_prd123"
            ).fetchall()
        } == TENANT_TABLES
        assert [
            str(row[0])
            for row in connection.execute(
                "SELECT permission_code FROM workspace_edge_secret_permission_extensions"
            ).fetchall()
        ] == ["edge.secret_reference.read"]
        assert [
            str(row[0])
            for row in connection.execute(
                "SELECT role_code FROM workspace_edge_secret_role_permissions ORDER BY role_code"
            ).fetchall()
        ] == ["admin", "auditor", "owner", "security_admin"]
    _healthy(database_url)

    assert rollback_last_migration(
        database_url, migrations_dir=migrations, expected_version="0115"
    ).rolled_back == "0115"
    with closing(connect_database(database_url)) as connection:
        assert not PRD123_TABLES.intersection(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            ).fetchall()
        )
    _healthy(database_url)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0115"]
    _healthy(database_url)


def test_safe_projection_constraints_owner_and_permissions(
    migrated_0114: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0114
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0115"]
    apply_seed_data(database_url)
    with closing(connect_database(database_url)) as connection:
        deployment_id, workspace_id, tenant_id = _insert_deployment(connection)
        reference_id = _insert_reference(
            connection,
            deployment_id=deployment_id,
            workspace_id=workspace_id,
            tenant_id=tenant_id,
        )
        columns = {
            str(row[1]).lower()
            for table in TENANT_TABLES
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        assert not FORBIDDEN_COLUMNS.intersection(columns)

        with pytest.raises(sqlite3.IntegrityError, match="transition_invalid"):
            connection.execute(
                "UPDATE edge_secret_references SET tenant_id=?,revision=2 WHERE reference_id=?",
                ("ten_cross_owner", reference_id),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE edge_secret_references SET revision=2,capabilities_json=? "
                "WHERE reference_id=?",
                ('["models","chat_completions"]', reference_id),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="secret_permission_immutable"):
            connection.execute(
                "DELETE FROM workspace_edge_secret_role_permissions WHERE role_code='owner'"
            )
        connection.rollback()
    _healthy(database_url)


def test_reference_fact_blocks_down(migrated_0114: tuple[Path, Path]) -> None:
    database, migrations = migrated_0114
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0115"]
    apply_seed_data(database_url)
    with closing(connect_database(database_url)) as connection:
        deployment_id, workspace_id, tenant_id = _insert_deployment(connection)
        _insert_reference(
            connection,
            deployment_id=deployment_id,
            workspace_id=workspace_id,
            tenant_id=tenant_id,
        )
    with pytest.raises(DbError) as blocked:
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0115"
        )
    assert blocked.value.code == "db.migration_failed"
    with closing(connect_database(database_url)) as connection:
        assert connection.execute(
            "SELECT version FROM schema_migrations WHERE status='applied' "
            "ORDER BY version DESC LIMIT 1"
        ).fetchone()[0] == "0115"
        assert connection.execute("SELECT COUNT(*) FROM edge_secret_references").fetchone()[
            0
        ] == 1
    _healthy(database_url)


def test_fresh_schema_installs_current_head(tmp_path: Path) -> None:
    database_url = _url(tmp_path / "fresh.sqlite3")
    assert apply_migrations(database_url).applied[-1] == "0115"
    _healthy(database_url)
