"""PRD-114 migration, ownership, replay, and rollback contracts."""

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
PRD114_TABLES = {
    "verified_domains",
    "sso_connections",
    "sso_certificates",
    "sso_policies",
    "enterprise_identity_session_grants",
    "account_federation_links",
    "sso_login_states",
    "sso_replay_guards",
    "enterprise_identity_commands",
    "enterprise_identity_event_outbox",
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


def _insert_domain(
    connection: sqlite3.Connection,
    *,
    suffix: str,
    workspace_id: str,
    tenant_id: str,
    account_id: str,
    domain: str = "enterprise.example.invalid",
    status: str = "pending",
) -> str:
    domain_id = "vdm_" + suffix * 32
    revision = 2 if status == "verified" else 1
    verified_at = "2026-08-25T00:00:01Z" if status == "verified" else None
    connection.execute(
        """INSERT INTO verified_domains(
             verified_domain_id,workspace_id,tenant_id,domain_normalized,domain_hmac,
             challenge_hash,status,revision,challenge_expires_at,verified_at,suspended_at,
             created_by_account_id,updated_by_account_id,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,'2026-08-25T01:00:00Z',?,NULL,?,?,
             '2026-08-25T00:00:00Z','2026-08-25T00:00:01Z')""",
        (
            domain_id,
            workspace_id,
            tenant_id,
            domain,
            suffix * 64,
            chr(ord(suffix) + 1) * 64,
            status,
            revision,
            verified_at,
            account_id,
            account_id,
        ),
    )
    return domain_id


def _insert_connection(
    connection: sqlite3.Connection,
    *,
    suffix: str,
    workspace_id: str,
    tenant_id: str,
    domain_id: str,
    account_id: str,
) -> str:
    connection_id = "ssc_" + suffix * 32
    connection.execute(
        """INSERT INTO sso_connections(
             sso_connection_id,workspace_id,tenant_id,verified_domain_id,
             connection_code,display_name,protocol,issuer,public_config_json,
             client_secret_ciphertext,secret_version,metadata_sha256,status,revision,
             tested_at,disabled_at,created_by_account_id,updated_by_account_id,
             created_at,updated_at)
           VALUES(?,?,?,?,?,'Enterprise OIDC','oidc','https://idp.example.invalid',
             '{}',?,1,?,'draft',1,NULL,NULL,?,?,
             '2026-08-25T00:00:00Z','2026-08-25T00:00:01Z')""",
        (
            connection_id,
            workspace_id,
            tenant_id,
            domain_id,
            f"oidc-{suffix}",
            suffix * 80,
            suffix * 64,
            account_id,
            account_id,
        ),
    )
    return connection_id


@pytest.fixture(scope="module")
def migrated_0105(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("prd114-migrated-0105")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 105:
            shutil.copy2(source, migrations / source.name)
    database = root / "prd114-0105.sqlite3"
    assert apply_migrations(_url(database), migrations_dir=migrations).applied[-1] == "0105"
    apply_seed_data(_url(database))
    for filename in (
        "0106_enterprise_domain_sso.sql",
        "0106_enterprise_domain_sso.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    return database, migrations


def test_0105_to_0106_pristine_roundtrip(
    migrated_0105: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0105
    database = tmp_path / "roundtrip.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)

    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0106"]
    connection = connect_database(database_url)
    try:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert PRD114_TABLES <= tables
        assert tuple(
            connection.execute(
                "SELECT COUNT(*),MIN(catalog_revision),MAX(catalog_revision) "
                "FROM rbac_permission_templates"
            ).fetchone()
        ) == (104, 25, 25)
        tenant_id = connection.execute(
            "SELECT tenant_id FROM tenants ORDER BY tenant_id LIMIT 1"
        ).fetchone()[0]
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
                    WHERE role.tenant_id=? AND permission.code LIKE 'enterprise_sso.%'
                    GROUP BY role.code""",
                (tenant_id,),
            )
        }
        assert grants == {"admin": 2, "security_admin": 2, "viewer": 1}
    finally:
        connection.close()
    _healthy(database_url)

    assert (
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0106"
        ).rolled_back
        == "0106"
    )
    connection = connect_database(database_url)
    try:
        assert tuple(
            connection.execute(
                "SELECT COUNT(*),MIN(catalog_revision),MAX(catalog_revision) "
                "FROM rbac_permission_templates"
            ).fetchone()
        ) == (102, 24, 24)
        assert connection.execute(
            "SELECT COUNT(*) FROM permissions WHERE code LIKE 'enterprise_sso.%'"
        ).fetchone()[0] == 0
    finally:
        connection.close()
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0106"]
    _healthy(database_url)


def test_owner_state_membership_and_replay_contracts(
    migrated_0105: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0105
    database = tmp_path / "contracts.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0106"]
    connection = connect_database(database_url)
    try:
        owners = connection.execute(
            """SELECT binding.workspace_id,binding.tenant_id,member.account_id,
                      member.workspace_membership_id
                 FROM workspace_tenant_bindings binding
                 JOIN workspace_memberships member
                   ON member.workspace_id=binding.workspace_id
                  AND member.tenant_id=binding.tenant_id
                WHERE binding.status='active' AND member.status='active'
                ORDER BY binding.workspace_id,member.workspace_membership_id"""
        ).fetchall()
        assert owners
        owner = owners[0]
        domain_id = _insert_domain(
            connection,
            suffix="a",
            workspace_id=owner["workspace_id"],
            tenant_id=owner["tenant_id"],
            account_id=owner["account_id"],
        )
        connection_id = _insert_connection(
            connection,
            suffix="b",
            workspace_id=owner["workspace_id"],
            tenant_id=owner["tenant_id"],
            domain_id=domain_id,
            account_id=owner["account_id"],
        )
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            _insert_connection(
                connection,
                suffix="c",
                workspace_id=owner["workspace_id"],
                tenant_id="tenant_prd114_owner_mismatch",
                domain_id=domain_id,
                account_id=owner["account_id"],
            )
        connection.rollback()

        with pytest.raises(sqlite3.IntegrityError, match="verified_domain_transition_invalid"):
            connection.execute(
                """UPDATE verified_domains SET status='verified',verified_at=?,updated_at=?
                     WHERE verified_domain_id=?""",
                ("2026-08-25T00:00:02Z", "2026-08-25T00:00:02Z", domain_id),
            )
        connection.rollback()

        with pytest.raises(sqlite3.IntegrityError, match="federation_link_owner_invalid"):
            connection.execute(
                """INSERT INTO account_federation_links(
                     account_federation_link_id,workspace_id,tenant_id,sso_connection_id,
                     account_id,membership_id,external_subject_hmac,verified_email_hmac,
                     status,revision,linked_at,disabled_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,'active',1,
                     '2026-08-25T00:00:00Z',NULL,'2026-08-25T00:00:00Z')""",
                (
                    "afl_" + "d" * 32,
                    owner["workspace_id"],
                    owner["tenant_id"],
                    connection_id,
                    owner["account_id"],
                    "wmb_" + "d" * 32,
                    "d" * 64,
                    "e" * 64,
                ),
            )
        connection.rollback()

        replay_values = (
            owner["workspace_id"],
            owner["tenant_id"],
            connection_id,
            "f" * 64,
        )
        connection.execute(
            """INSERT INTO sso_replay_guards VALUES(
                 ?,?,?,?,'oidc',?,'2026-08-25T01:00:00Z','2026-08-25T00:00:00Z')""",
            ("srg_" + "f" * 32, *replay_values),
        )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(
                """INSERT INTO sso_replay_guards VALUES(
                     ?,?,?,?,'oidc',?,'2026-08-25T01:00:00Z','2026-08-25T00:00:00Z')""",
                ("srg_" + "e" * 32, *replay_values),
            )
        connection.rollback()
    finally:
        connection.close()
    _healthy(database_url)


def test_down_refuses_durable_enterprise_identity_facts(
    migrated_0105: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0105
    database = tmp_path / "down-guard.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0106"]
    connection = connect_database(database_url)
    try:
        owner = connection.execute(
            """SELECT binding.workspace_id,binding.tenant_id,member.account_id
                 FROM workspace_tenant_bindings binding
                 JOIN workspace_memberships member
                   ON member.workspace_id=binding.workspace_id
                  AND member.tenant_id=binding.tenant_id
                WHERE binding.status='active' AND member.status='active'
                ORDER BY binding.workspace_id LIMIT 1"""
        ).fetchone()
        assert owner is not None
        _insert_domain(
            connection,
            suffix="d",
            workspace_id=owner["workspace_id"],
            tenant_id=owner["tenant_id"],
            account_id=owner["account_id"],
            domain="durable.example.invalid",
            status="verified",
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(DbError):
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0106"
        )
    _healthy(database_url)


def test_fresh_schema_installs_current_head(tmp_path: Path) -> None:
    database_url = _url(tmp_path / "fresh.sqlite3")
    assert apply_migrations(database_url).applied[-1] == "0112"
    _healthy(database_url)
