"""PRD-093 migration 0085 backfill, owner election, and rollback guards."""

from __future__ import annotations

import shutil
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
WORKSPACE_TABLES = {
    "organization_profiles",
    "personal_profiles",
    "workspace_creation_requests",
    "workspace_environment_origins",
    "workspace_event_outbox",
    "workspace_events",
    "workspace_memberships",
    "workspace_provisioning_jobs",
    "workspace_regions",
    "workspace_tenant_bindings",
    "workspace_tenant_creation_claims",
    "workspace_user_projection_claims",
    "workspaces",
}


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _healthy(connection) -> None:
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _version(connection) -> str:
    return str(
        connection.execute(
            "SELECT version FROM schema_migrations WHERE status='applied' "
            "ORDER BY version DESC LIMIT 1"
        ).fetchone()[0]
    )


@pytest.fixture(scope="module")
def seeded_0084(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("prd093-seeded-0084")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 84:
            shutil.copy2(source, migrations / source.name)
    database = root / "seeded.sqlite3"
    result = apply_migrations(_url(database), migrations_dir=migrations)
    assert result.applied[-1] == "0084"
    apply_seed_data(_url(database))
    return database


@pytest.fixture(scope="module")
def migrations_0085(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("prd093-migrations-0085")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 85:
            shutil.copy2(source, migrations / source.name)
    return migrations


def test_0085_backfills_owner_foundation_and_pristine_round_trip(
    seeded_0084: Path,
    migrations_0085: Path,
    tmp_path: Path,
) -> None:
    database = tmp_path / "roundtrip.sqlite3"
    shutil.copy2(seeded_0084, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations_0085).applied == ["0085"]
    connection = connect_database(database_url)
    try:
        assert _version(connection) == "0085"
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert WORKSPACE_TABLES <= tables
        assert tuple(
            connection.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM tenants),
                  (SELECT COUNT(*) FROM workspaces),
                  (SELECT COUNT(*) FROM workspace_tenant_bindings),
                  (SELECT COUNT(*) FROM workspace_memberships),
                  (SELECT COUNT(*) FROM workspace_environment_origins)
                """
            ).fetchone()
        ) == (1, 1, 1, 1, 1)
        workspace = connection.execute(
            """
            SELECT w.status,w.profile_state,w.creation_source,m.role_code,m.source,
                   origin.origin,job.status
            FROM workspaces w
            JOIN workspace_memberships m ON m.workspace_id=w.workspace_id
            JOIN workspace_environment_origins origin ON origin.workspace_id=w.workspace_id
            JOIN workspace_provisioning_jobs job ON job.workspace_id=w.workspace_id
            """
        ).fetchone()
        assert tuple(workspace) == (
            "active",
            "confirmed",
            "legacy_bootstrap",
            "owner",
            "legacy_backfill",
            "legacy_existing",
            "ready",
        )
        _healthy(connection)
    finally:
        connection.close()

    assert (
        rollback_last_migration(
            database_url,
            expected_version="0085",
            migrations_dir=migrations_0085,
        ).rolled_back
        == "0085"
    )
    connection = connect_database(database_url)
    try:
        assert _version(connection) == "0084"
        assert not (
            WORKSPACE_TABLES
            & {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table'"
                )
            }
        )
        _healthy(connection)
    finally:
        connection.close()
    assert apply_migrations(database_url, migrations_dir=migrations_0085).applied == ["0085"]


def test_0085_owner_election_is_deterministic_and_missing_admin_conflicts(
    seeded_0084: Path,
    migrations_0085: Path,
    tmp_path: Path,
) -> None:
    database = tmp_path / "owner-election.sqlite3"
    shutil.copy2(seeded_0084, database)
    database_url = _url(database)
    connection = connect_database(database_url)
    try:
        seed = connection.execute(
            "SELECT tenant_id,user_id,password_hash,created_at "
            "FROM users ORDER BY created_at LIMIT 1"
        ).fetchone()
        assert seed is not None
        connection.execute(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,
              created_at,updated_at,failed_login_count)
            VALUES('user_second_admin',?,'second.admin@example.com','Second Admin',?,
              'admin','active','2030-01-01T00:00:00Z','2030-01-01T00:00:00Z',0)
            """,
            (seed["tenant_id"], seed["password_hash"]),
        )
        connection.execute(
            """
            INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
            VALUES('tenant_no_admin','no-admin','No Admin','internal_only','active',
              '2030-01-01T00:00:00Z','2030-01-01T00:00:00Z')
            """
        )
        connection.execute(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,
              created_at,updated_at,failed_login_count)
            VALUES('user_viewer','tenant_no_admin','viewer@example.com','Viewer',?,
              'viewer','active','2030-01-01T00:00:00Z','2030-01-01T00:00:00Z',0)
            """,
            (seed["password_hash"],),
        )
        connection.commit()
    finally:
        connection.close()

    assert apply_migrations(database_url, migrations_dir=migrations_0085).applied == ["0085"]
    connection = connect_database(database_url)
    try:
        elected = connection.execute(
            """
            SELECT m.legacy_user_id FROM workspace_memberships m
            JOIN workspace_tenant_bindings b ON b.workspace_id=m.workspace_id
            WHERE b.tenant_id=?
            """,
            (seed["tenant_id"],),
        ).fetchone()
        assert elected is not None and elected["legacy_user_id"] == seed["user_id"]
        conflict = connection.execute(
            """
            SELECT w.status,w.profile_state,job.status,job.error_code
            FROM workspace_tenant_bindings b
            JOIN workspaces w ON w.workspace_id=b.workspace_id
            JOIN workspace_provisioning_jobs job ON job.workspace_id=w.workspace_id
            WHERE b.tenant_id='tenant_no_admin'
            """
        ).fetchone()
        assert tuple(conflict) == (
            "migration_conflict",
            "migration_conflict",
            "failed",
            "workspace.migration_conflict",
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM workspace_memberships WHERE tenant_id='tenant_no_admin'"
        ).fetchone()[0] == 0
        _healthy(connection)
    finally:
        connection.close()


def test_0085_profile_fact_blocks_down_atomically(
    seeded_0084: Path,
    migrations_0085: Path,
    tmp_path: Path,
) -> None:
    database = tmp_path / "down-guard.sqlite3"
    shutil.copy2(seeded_0084, database)
    database_url = _url(database)
    apply_migrations(database_url, migrations_dir=migrations_0085)
    connection = connect_database(database_url)
    try:
        connection.execute(
            "UPDATE organization_profiles SET name='Changed',revision=2,updated_at=?",
            ("2030-01-01T00:00:00Z",),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(DbError):
        rollback_last_migration(
            database_url,
            expected_version="0085",
            migrations_dir=migrations_0085,
        )
    connection = connect_database(database_url)
    try:
        assert _version(connection) == "0085"
        assert connection.execute(
            "SELECT revision FROM organization_profiles"
        ).fetchone()[0] == 2
        _healthy(connection)
    finally:
        connection.close()
