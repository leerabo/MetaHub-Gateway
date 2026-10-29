"""PRD-094 migration, Membership backfill, and rollback guards."""

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
PRD094_TABLES = {
    "membership_event_outbox",
    "membership_events",
    "membership_invitations",
    "owner_transfers",
    "workspace_sessions",
}


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _version(connection) -> str:
    return str(
        connection.execute(
            "SELECT version FROM schema_migrations WHERE status='applied' "
            "ORDER BY version DESC LIMIT 1"
        ).fetchone()[0]
    )


def _healthy(connection) -> None:
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.fixture(scope="module")
def seeded_0085(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("prd094-seeded-0085")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 85:
            shutil.copy2(source, migrations / source.name)
    database = root / "seeded.sqlite3"
    result = apply_migrations(_url(database), migrations_dir=migrations)
    assert result.applied[-1] == "0085"
    apply_seed_data(_url(database))
    for source in MIGRATIONS_DIR.glob("0086_*.sql"):
        shutil.copy2(source, migrations / source.name)
    return database


def _apply_0086(database_url: str, seeded_0085: Path):
    return apply_migrations(database_url, migrations_dir=seeded_0085.parent / "migrations")


def test_0086_backfills_all_legacy_roles_and_pristine_round_trip(
    seeded_0085: Path,
    tmp_path: Path,
) -> None:
    database = tmp_path / "roundtrip.sqlite3"
    shutil.copy2(seeded_0085, database)
    database_url = _url(database)
    connection = connect_database(database_url)
    try:
        seed = connection.execute(
            "SELECT tenant_id,password_hash FROM users ORDER BY created_at LIMIT 1"
        ).fetchone()
        assert seed is not None
        for index, role in enumerate(
            ("developer", "security_admin", "finance_admin", "ops_admin", "viewer"),
            start=1,
        ):
            connection.execute(
                """
                INSERT INTO users(
                  user_id,tenant_id,email,display_name,password_hash,role,status,
                  created_at,updated_at,failed_login_count)
                VALUES(?,?,?,?,?,?,'active',?,?,0)
                """,
                (
                    f"legacy_role_{index}",
                    seed["tenant_id"],
                    f"role-{index}@example.com",
                    f"Role {index}",
                    seed["password_hash"],
                    role,
                    f"2030-01-0{index}T00:00:00Z",
                    f"2030-01-0{index}T00:00:00Z",
                ),
            )
        connection.commit()
    finally:
        connection.close()

    assert _apply_0086(database_url, seeded_0085).applied == ["0086"]
    connection = connect_database(database_url)
    try:
        assert _version(connection) == "0086"
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert PRD094_TABLES <= tables
        roles = {
            row["legacy_user_id"]: row["role_code"]
            for row in connection.execute(
                "SELECT legacy_user_id,role_code FROM workspace_memberships"
            )
        }
        assert roles["legacy_role_1"] == "developer"
        assert roles["legacy_role_2"] == "security_admin"
        assert roles["legacy_role_3"] == "finance_admin"
        assert roles["legacy_role_4"] == "admin"
        assert roles["legacy_role_5"] == "viewer"
        assert connection.execute(
            "SELECT COUNT(*) FROM workspace_memberships WHERE role_code='owner' AND status='active'"
        ).fetchone()[0] == 1
        assert connection.execute(
            """
            SELECT COUNT(*) FROM workspace_memberships
            WHERE revision=1 AND created_by_account_id=account_id
              AND updated_by_account_id=account_id
            """
        ).fetchone()[0] == 6
        _healthy(connection)
    finally:
        connection.close()

    assert rollback_last_migration(database_url, expected_version="0086").rolled_back == "0086"
    connection = connect_database(database_url)
    try:
        assert _version(connection) == "0085"
        assert connection.execute(
            "SELECT COUNT(*) FROM workspace_memberships"
        ).fetchone()[0] == 1
        _healthy(connection)
    finally:
        connection.close()
    assert _apply_0086(database_url, seeded_0085).applied == ["0086"]


def test_0086_committed_membership_fact_blocks_down_atomically(
    seeded_0085: Path,
    tmp_path: Path,
) -> None:
    database = tmp_path / "down-guard.sqlite3"
    shutil.copy2(seeded_0085, database)
    database_url = _url(database)
    _apply_0086(database_url, seeded_0085)
    connection = connect_database(database_url)
    try:
        row = connection.execute(
            """
            SELECT workspace_id,tenant_id,account_id,workspace_membership_id
            FROM workspace_memberships LIMIT 1
            """
        ).fetchone()
        assert row is not None
        connection.execute(
            """
            INSERT INTO membership_events(
              membership_event_id,workspace_id,tenant_id,actor_account_id,
              subject_membership_id,event_type,reason_code,request_id,trace_id,
              payload_json,payload_sha256,occurred_at)
            VALUES('mev_00000000000000000000000000000000',?,?,?,?,
              'membership.updated','test_fact','req_test','trace_test','{}',
              '44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a',
              '2030-01-01T00:00:00Z')
            """,
            (
                row["workspace_id"],
                row["tenant_id"],
                row["account_id"],
                row["workspace_membership_id"],
            ),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(DbError):
        rollback_last_migration(database_url, expected_version="0086")
    connection = connect_database(database_url)
    try:
        assert _version(connection) == "0086"
        assert connection.execute("SELECT COUNT(*) FROM membership_events").fetchone()[0] == 1
        _healthy(connection)
    finally:
        connection.close()


def test_0086_reserves_pending_service_subject_without_browser_projection(
    seeded_0085: Path,
    tmp_path: Path,
) -> None:
    database = tmp_path / "service-subject.sqlite3"
    shutil.copy2(seeded_0085, database)
    database_url = _url(database)
    _apply_0086(database_url, seeded_0085)
    connection = connect_database(database_url)
    try:
        owner = connection.execute(
            """
            SELECT workspace_id,tenant_id,account_id
            FROM workspace_memberships WHERE role_code='owner'
            """
        ).fetchone()
        assert owner is not None
        service_account_id = "gac_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        connection.execute(
            """
            INSERT INTO global_accounts(
              account_id,email_normalized,phone_e164,display_name,status,login_enabled,
              credential_revision,session_revision,mfa_state,failed_login_count,
              locked_until,last_login_at,created_at,updated_at)
            SELECT ?,?,NULL,'Automation Runner','disabled',0,1,1,'not_enrolled',0,
              NULL,NULL,'2030-01-01T00:00:00Z','2030-01-01T00:00:00Z'
            FROM global_accounts LIMIT 1
            """,
            (service_account_id, "automation-subject@example.invalid"),
        )
        connection.execute(
            """
            INSERT INTO workspace_memberships(
              workspace_membership_id,workspace_id,tenant_id,account_id,legacy_user_id,
              role_code,status,source,revision,created_by_account_id,
              updated_by_account_id,created_at,updated_at)
            VALUES('wmb_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',?,?,?,NULL,
              'service_account','pending','membership_invitation',1,?,?,
              '2030-01-01T00:00:00Z','2030-01-01T00:00:00Z')
            """,
            (
                owner["workspace_id"],
                owner["tenant_id"],
                service_account_id,
                owner["account_id"],
                owner["account_id"],
            ),
        )
        connection.commit()
        row = connection.execute(
            """
            SELECT role_code,status,legacy_user_id
            FROM workspace_memberships WHERE account_id=?
            """,
            (service_account_id,),
        ).fetchone()
        assert tuple(row) == ("service_account", "pending", None)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE workspace_memberships
                SET status='active',revision=revision+1,updated_at='2030-01-02T00:00:00Z'
                WHERE account_id=?
                """,
                (service_account_id,),
            )
        connection.rollback()
        assert connection.execute(
            "SELECT COUNT(*) FROM workspace_sessions WHERE account_id=?",
            (service_account_id,),
        ).fetchone()[0] == 0
        _healthy(connection)
    finally:
        connection.close()


def test_0086_new_legacy_user_backfills_membership_without_replacing_owner(
    seeded_0085: Path,
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy-user-after-up.sqlite3"
    shutil.copy2(seeded_0085, database)
    database_url = _url(database)
    _apply_0086(database_url, seeded_0085)
    connection = connect_database(database_url)
    try:
        owner = connection.execute(
            """
            SELECT workspace.workspace_id,workspace.created_by_account_id,
              member.tenant_id,user.password_hash
            FROM workspaces workspace
            JOIN workspace_memberships member
              ON member.workspace_id=workspace.workspace_id
             AND member.role_code='owner' AND member.status='active'
            JOIN users user ON user.tenant_id=member.tenant_id
             AND user.user_id=member.legacy_user_id
            """
        ).fetchone()
        assert owner is not None
        connection.execute(
            """
            INSERT INTO users(
              user_id,tenant_id,email,display_name,password_hash,role,status,
              created_at,updated_at,failed_login_count)
            VALUES('legacy_after_prd094',?,'legacy-after-prd094@example.com',
              'Legacy After PRD-094',?,'developer','active',
              '2030-01-01T00:00:00Z','2030-01-01T00:00:00Z',0)
            """,
            (owner["tenant_id"], owner["password_hash"]),
        )
        connection.commit()
        workspace = connection.execute(
            "SELECT created_by_account_id FROM workspaces WHERE workspace_id=?",
            (owner["workspace_id"],),
        ).fetchone()
        membership = connection.execute(
            """
            SELECT role_code,status,source,revision
            FROM workspace_memberships WHERE legacy_user_id='legacy_after_prd094'
            """
        ).fetchone()
        assert workspace["created_by_account_id"] == owner["created_by_account_id"]
        assert tuple(membership) == ("developer", "active", "legacy_backfill", 1)
        _healthy(connection)
    finally:
        connection.close()
