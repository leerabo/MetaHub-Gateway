"""PRD-120 schema ownership, permissions, deletion manifest, and rollback guards."""

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
PRD120_TABLES = {
    "workspace_edge_permission_extensions",
    "workspace_edge_role_permissions",
    "platform_edge_permission_extensions",
    "platform_edge_role_permissions",
    "edge_deployments",
    "edge_registration_intents",
    "edge_device_identities",
    "edge_device_certificates",
    "edge_sessions",
    "workspace_edge_commands",
    "edge_machine_commands",
    "edge_identity_events",
    "edge_identity_event_outbox",
    "tenant_deletion_manifest_extensions_prd120",
}


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


@pytest.fixture(scope="module")
def migrated_0111(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("prd120-migrated-0111")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 111:
            shutil.copy2(source, migrations / source.name)
    database = root / "prd120-0111.sqlite3"
    assert apply_migrations(_url(database), migrations_dir=migrations).applied[-1] == "0111"
    for filename in (
        "0112_edge_agent_device_identity_mtls.sql",
        "0112_edge_agent_device_identity_mtls.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    return database, migrations


def test_0111_to_0112_pristine_roundtrip(
    migrated_0111: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0111
    database = tmp_path / "roundtrip.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0112"]
    with connect_database(database_url) as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            )
        }
        assert PRD120_TABLES <= tables
        assert {
            tuple(row)
            for row in connection.execute(
                "SELECT role_code,permission_code FROM workspace_edge_role_permissions"
            )
        } == {
            ("owner", "edge.deployment.read"),
            ("owner", "edge.deployment.manage"),
            ("admin", "edge.deployment.read"),
            ("admin", "edge.deployment.manage"),
            ("developer", "edge.deployment.read"),
            ("security_admin", "edge.deployment.read"),
        }
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert (
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0112"
        ).rolled_back
        == "0112"
    )
    with connect_database(database_url) as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            )
        }
        assert not PRD120_TABLES & tables
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_permissions_are_immutable_and_down_guard_preserves_business_facts(
    migrated_0111: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0111
    database = tmp_path / "guard.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0112"]
    with connect_database(database_url) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM workspace_edge_role_permissions WHERE role_code='developer'"
            )
        connection.rollback()
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            """
            INSERT INTO edge_deployments(
              edge_deployment_id,workspace_id,tenant_id,display_name,region_code,mode,status,
              revision,revocation_generation,agent_version,health_code,last_connected_at,
              certificate_expires_at,created_by_account_id,revoked_by_account_id,
              revoked_reason_code,created_at,updated_at,revoked_at)
            VALUES(?,?,?,'Guard deployment','cn-east-1','connected','pending',1,1,NULL,
              'not_registered',NULL,NULL,?,NULL,NULL,?,?,NULL)
            """,
            (
                "edp_" + "1" * 32,
                "wsp_" + "1" * 32,
                "ten_" + "1" * 32,
                "acc_" + "1" * 32,
                "2026-08-26T12:00:00.000Z",
                "2026-08-26T12:00:00.000Z",
            ),
        )
        connection.commit()
    with pytest.raises(DbError):
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0112"
        )
    with connect_database(database_url) as connection:
        assert connection.execute(
            "SELECT version FROM schema_migrations WHERE status='applied' "
            "ORDER BY version DESC LIMIT 1"
        ).fetchone()[0] == "0112"
        assert PRD120_TABLES <= {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            )
        }


def test_tenant_deletion_manifest_covers_all_edge_owner_tables(
    migrated_0111: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0111
    database = tmp_path / "manifest.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0112"]
    with connect_database(database_url) as connection:
        sources = {
            row[0]
            for row in connection.execute(
                "SELECT source_table FROM tenant_deletion_manifest_extensions_prd120"
            )
        }
        assert sources == {
            "edge_deployments",
            "edge_registration_intents",
            "edge_device_identities",
            "edge_device_certificates",
            "edge_sessions",
            "workspace_edge_commands",
            "edge_machine_commands",
            "edge_identity_events",
        }
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
