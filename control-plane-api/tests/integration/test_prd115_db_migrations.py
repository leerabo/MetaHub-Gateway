"""PRD-115 migration round-trip, ownership, and durable-fact guards."""

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
PRD115_TABLES = {
    "scim_tokens",
    "scim_user_links",
    "scim_pending_invitations",
    "scim_group_links",
    "scim_group_members",
    "scim_role_mappings",
    "scim_sync_runs",
    "scim_conflicts",
    "scim_commands",
    "scim_event_outbox",
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
def migrated_0106(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("prd115-migrated-0106")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 106:
            shutil.copy2(source, migrations / source.name)
    database = root / "prd115-0106.sqlite3"
    assert apply_migrations(_url(database), migrations_dir=migrations).applied[-1] == "0106"
    apply_seed_data(_url(database))
    for filename in (
        "0107_scim_directory_membership_lifecycle.sql",
        "0107_scim_directory_membership_lifecycle.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    return database, migrations


def test_0106_to_0107_pristine_roundtrip(migrated_0106: tuple[Path, Path], tmp_path: Path) -> None:
    source, migrations = migrated_0106
    database = tmp_path / "roundtrip.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)

    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0107"]
    connection = connect_database(database_url)
    try:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert PRD115_TABLES <= tables
        assert {
            row["name"]
            for row in connection.execute("SELECT name FROM pragma_table_info('scim_conflicts')")
        } >= {"directory_action_code", "directory_target_role_code"}
        assert tuple(
            connection.execute(
                "SELECT COUNT(*),MIN(catalog_revision),MAX(catalog_revision) "
                "FROM rbac_permission_templates"
            ).fetchone()
        ) == (106, 26, 26)
        grants = {
            row["code"]: row["total"]
            for row in connection.execute(
                """SELECT role.code,COUNT(*) total
                     FROM roles role
                     JOIN role_permissions relation
                       ON relation.tenant_id=role.tenant_id
                      AND relation.role_id=role.role_id
                     JOIN permissions permission
                       ON permission.tenant_id=relation.tenant_id
                      AND permission.permission_id=relation.permission_id
                    WHERE permission.code LIKE 'scim.%'
                    GROUP BY role.code"""
            )
        }
        assert grants == {"admin": 2, "security_admin": 2, "viewer": 1}
    finally:
        connection.close()
    _healthy(database_url)

    assert (
        rollback_last_migration(
            database_url,
            migrations_dir=migrations,
            expected_version="0107",
        ).rolled_back
        == "0107"
    )
    connection = connect_database(database_url)
    try:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert PRD115_TABLES.isdisjoint(tables)
        assert "lifecycle_source" not in {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM pragma_table_info('workspace_memberships')"
            )
        }
        assert tuple(
            connection.execute(
                "SELECT COUNT(*),MIN(catalog_revision),MAX(catalog_revision) "
                "FROM rbac_permission_templates"
            ).fetchone()
        ) == (104, 25, 25)
    finally:
        connection.close()
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0107"]
    _healthy(database_url)


def test_up_guard_refuses_unexpected_prd115_schema(
    migrated_0106: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0106
    database = tmp_path / "up-guard.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    connection = connect_database(database_url)
    try:
        connection.execute("CREATE TABLE unexpected_prd115(marker INTEGER NOT NULL)")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(DbError):
        apply_migrations(database_url, migrations_dir=migrations)
    connection = connect_database(database_url)
    try:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name='scim_tokens'"
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()
    _healthy(database_url)


def test_down_refuses_durable_scim_facts(migrated_0106: tuple[Path, Path], tmp_path: Path) -> None:
    source, migrations = migrated_0106
    database = tmp_path / "down-guard.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0107"]
    connection = connect_database(database_url)
    try:
        owner = connection.execute(
            """SELECT workspace_id,tenant_id,workspace_membership_id,account_id
                 FROM workspace_memberships
                WHERE role_code='owner' AND status='active'
                ORDER BY workspace_id LIMIT 1"""
        ).fetchone()
        assert owner is not None
        connection.execute(
            """INSERT INTO scim_tokens(
                 scim_token_id,workspace_id,tenant_id,token_hmac,token_prefix,scope_code,
                 status,revision,expires_at,grace_expires_at,last_used_at,
                 created_by_membership_id,created_by_account_id,created_at,updated_at)
               VALUES(?,?,?,?,?,'users_groups','active',1,
                 '2030-01-01T00:00:00Z',NULL,NULL,?,?,
                 '2026-08-25T00:00:00Z','2026-08-25T00:00:00Z')""",
            (
                "sct_" + "a" * 32,
                owner["workspace_id"],
                owner["tenant_id"],
                "b" * 64,
                "mhscim_test",
                owner["workspace_membership_id"],
                owner["account_id"],
            ),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(DbError):
        rollback_last_migration(
            database_url,
            migrations_dir=migrations,
            expected_version="0107",
        )
    _healthy(database_url)


def test_scim_conflicts_accept_only_directory_roles(
    migrated_0106: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0106
    database = tmp_path / "role-constraint.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0107"]
    allowed_roles = (
        "admin",
        "developer",
        "security_admin",
        "finance_admin",
        "legal_admin",
        "auditor",
        "viewer",
    )
    connection = connect_database(database_url)
    try:
        owner = connection.execute(
            """SELECT workspace_id,tenant_id FROM workspace_memberships
                 WHERE role_code='owner' AND status='active'
                 ORDER BY workspace_id LIMIT 1"""
        ).fetchone()
        assert owner is not None

        def insert_conflict(sequence: int, role_code: str) -> None:
            connection.execute(
                """INSERT INTO scim_conflicts(
                     scim_conflict_id,workspace_id,tenant_id,subject_type,subject_id,
                     reason_code,directory_action_code,directory_target_role_code,
                     current_revision_hash,directory_revision_hash,status,created_at,updated_at)
                   VALUES(?,?,?,'membership',?,'role_conflict','set_role',?,
                     ?,?,'open','2026-08-25T00:00:00Z','2026-08-25T00:00:00Z')""",
                (
                    f"scf_{sequence:032x}",
                    owner["workspace_id"],
                    owner["tenant_id"],
                    f"subject_{sequence:028x}",
                    role_code,
                    f"{sequence:064x}",
                    f"{sequence + 100:064x}",
                ),
            )

        for sequence, role_code in enumerate(allowed_roles, start=1):
            insert_conflict(sequence, role_code)
        with pytest.raises(sqlite3.IntegrityError):
            insert_conflict(len(allowed_roles) + 1, "ops_admin")
        connection.commit()
        stored_roles = {
            row["directory_target_role_code"]
            for row in connection.execute(
                "SELECT directory_target_role_code FROM scim_conflicts"
            )
        }
        assert stored_roles == set(allowed_roles)
    finally:
        connection.close()
    _healthy(database_url)


def test_fresh_schema_installs_current_head(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 108:
            shutil.copy2(source, migrations / source.name)
    database_url = _url(tmp_path / "fresh.sqlite3")
    assert apply_migrations(database_url, migrations_dir=migrations).applied[-1] == "0108"
    _healthy(database_url)
