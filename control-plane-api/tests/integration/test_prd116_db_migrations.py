"""PRD-116 migration round-trip, MFA expansion, and durable-fact guards."""

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
PRD116_TABLES = {
    "webauthn_credentials",
    "mfa_challenges",
    "recovery_code_sets",
    "recovery_codes",
    "trusted_devices",
    "conditional_access_policies",
    "conditional_access_simulations",
    "step_up_grants",
    "identity_security_events",
    "identity_security_event_outbox",
}


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _healthy(database_url: str) -> None:
    with connect_database(database_url) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.fixture(scope="module")
def migrated_0107(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("prd116-migrated-0107")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 107:
            shutil.copy2(source, migrations / source.name)
    database = root / "prd116-0107.sqlite3"
    assert apply_migrations(_url(database), migrations_dir=migrations).applied[-1] == "0107"
    apply_seed_data(_url(database))
    for filename in (
        "0108_mfa_conditional_access_device_session_security.sql",
        "0108_mfa_conditional_access_device_session_security.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    return database, migrations


def test_0107_to_0108_pristine_roundtrip(
    migrated_0107: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0107
    database = tmp_path / "roundtrip.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)

    with connect_database(database_url) as connection:
        before_factors = connection.execute(
            "SELECT * FROM account_mfa_factors ORDER BY factor_id"
        ).fetchall()
        before_guards = connection.execute(
            "SELECT * FROM account_mfa_attempt_guards ORDER BY account_id,factor_id,device_hmac"
        ).fetchall()
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0108"]
    with connect_database(database_url) as connection:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert PRD116_TABLES <= tables
        assert "ready_at" in {
            row["name"] for row in connection.execute("PRAGMA table_info(mfa_challenges)")
        }
        assert {
            "revoke_generation",
            "assurance_level",
            "auth_method",
            "trusted_device_id",
        } <= {row["name"] for row in connection.execute("PRAGMA table_info(global_sessions)")}
        assert {
            "revoke_generation",
            "assurance_level",
            "trusted_device_id",
            "conditional_access_revision",
        } <= {row["name"] for row in connection.execute("PRAGMA table_info(workspace_sessions)")}
        assert tuple(
            connection.execute(
                "SELECT COUNT(*),MIN(catalog_revision),MAX(catalog_revision) "
                "FROM rbac_permission_templates"
            ).fetchone()
        ) == (108, 27, 27)
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
                    WHERE permission.code LIKE 'conditional_access.%'
                    GROUP BY role.code"""
            )
        }
        assert grants == {"admin": 2, "security_admin": 2, "viewer": 1}
        assert [tuple(row) for row in before_factors] == [
            tuple(row)
            for row in connection.execute("SELECT * FROM account_mfa_factors ORDER BY factor_id")
        ]
        assert [tuple(row) for row in before_guards] == [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM account_mfa_attempt_guards ORDER BY account_id,factor_id,device_hmac"
            )
        ]
        account_id = connection.execute(
            "SELECT account_id FROM global_accounts ORDER BY created_at LIMIT 1"
        ).fetchone()[0]
        stamp = "2026-08-25T00:00:00Z"
        for suffix in ("1", "2"):
            connection.execute(
                """INSERT INTO account_mfa_factors(
                     factor_id,account_id,factor_type,secret_ciphertext,secret_version,status,
                     revision,last_accepted_counter,verified_at,disabled_at,created_at,updated_at)
                   VALUES(?,?,'webauthn',NULL,NULL,'active',2,NULL,?,NULL,?,?)""",
                ("mfa_" + suffix * 32, account_id, stamp, stamp, stamp),
            )
        connection.rollback()
    _healthy(database_url)
    assert (
        rollback_last_migration(
            database_url,
            migrations_dir=migrations,
            expected_version="0108",
        ).rolled_back
        == "0108"
    )
    with connect_database(database_url) as connection:
        assert tuple(
            connection.execute(
                "SELECT COUNT(*),MIN(catalog_revision),MAX(catalog_revision) "
                "FROM rbac_permission_templates"
            ).fetchone()
        ) == (106, 26, 26)
    _healthy(database_url)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0108"]
    _healthy(database_url)


def test_up_guard_refuses_unexpected_prd116_schema(
    migrated_0107: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0107
    database = tmp_path / "up-guard.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    with connect_database(database_url) as connection:
        connection.execute("CREATE TABLE unexpected_prd116(value TEXT)")
        connection.commit()
    with pytest.raises(DbError):
        apply_migrations(database_url, migrations_dir=migrations)
    with connect_database(database_url) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name='webauthn_credentials'"
        ).fetchone()[0] == 0


def test_down_refuses_durable_identity_security_facts(
    migrated_0107: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0107
    database = tmp_path / "down-guard.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0108"]
    with connect_database(database_url) as connection:
        owner = connection.execute(
            """SELECT member.workspace_id,member.tenant_id,member.account_id
                 FROM workspace_memberships member WHERE member.role_code='owner'
                 ORDER BY member.created_at LIMIT 1"""
        ).fetchone()
        assert owner is not None
        connection.execute(
            """INSERT INTO conditional_access_policies(
                 conditional_access_policy_id,workspace_id,tenant_id,mode,require_mfa,
                 require_step_up_for_high_risk,allow_trusted_devices,region_rule,network_rule,
                 allowed_cidrs_json,exception_membership_ids_json,revision,
                 simulation_watermark_at,updated_by_account_id,created_at,updated_at)
               VALUES(?,?,?,'audit',1,1,0,'any','any','[]','[]',1,NULL,?,?,?)""",
            (
                "cap_" + "1" * 32,
                owner["workspace_id"],
                owner["tenant_id"],
                owner["account_id"],
                "2026-08-25T00:00:00Z",
                "2026-08-25T00:00:00Z",
            ),
        )
        connection.commit()
    with pytest.raises(DbError):
        rollback_last_migration(
            database_url,
            migrations_dir=migrations,
            expected_version="0108",
        )
    with connect_database(database_url) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM conditional_access_policies"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
        ).fetchone()[0] == "0108"
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
