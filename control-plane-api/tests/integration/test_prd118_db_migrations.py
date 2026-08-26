"""PRD-118 migration round-trip and fail-closed machine identity invariants."""

from __future__ import annotations

import hashlib
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
PRD118_TABLES = {
    "workspace_machine_permission_extensions",
    "workspace_machine_role_permissions",
    "machine_permission_catalog",
    "service_accounts",
    "service_account_scopes",
    "oauth_clients",
    "client_credentials",
    "access_token_grants",
    "machine_identity_audits",
    "machine_identity_event_outbox",
    "machine_identity_commands",
    "oauth_token_attempt_guards",
    "tenant_deletion_manifest_extensions_prd118",
}
TENANT_OWNED_TABLES = {
    "service_accounts",
    "oauth_clients",
    "client_credentials",
    "access_token_grants",
    "machine_identity_audits",
    "machine_identity_event_outbox",
    "machine_identity_commands",
}
STAMP = "2026-08-26T00:00:00.000Z"


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _healthy(database_url: str) -> None:
    with connect_database(database_url) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        registered = {
            row["source_table"]
            for row in connection.execute(
                "SELECT source_table FROM tenant_deletion_table_manifest"
            )
        }
        assert TENANT_OWNED_TABLES <= registered
        for table in TENANT_OWNED_TABLES:
            schema = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            assert schema is not None
            manifest = connection.execute(
                "SELECT * FROM tenant_deletion_table_manifest WHERE source_table=?",
                (table,),
            ).fetchone()
            assert manifest is not None
            assert manifest["table_sql_sha256"] == hashlib.sha256(
                schema["sql"].encode()
            ).hexdigest()


@pytest.fixture(scope="module")
def migrated_0109(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("prd118-migrated-0109")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 109:
            shutil.copy2(source, migrations / source.name)
    database = root / "prd118-0109.sqlite3"
    assert apply_migrations(_url(database), migrations_dir=migrations).applied[-1] == "0109"
    apply_seed_data(_url(database))
    for filename in (
        "0110_service_account_oauth_machine_identity.sql",
        "0110_service_account_oauth_machine_identity.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    return database, migrations


def test_0109_to_0110_pristine_roundtrip(
    migrated_0109: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0109
    database = tmp_path / "roundtrip.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)

    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0110"]
    with connect_database(database_url) as connection:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert PRD118_TABLES <= tables
        assert {
            tuple(row)
            for row in connection.execute(
                "SELECT role_code,permission_code "
                "FROM workspace_machine_role_permissions"
            )
        } == {
            ("owner", "machine_identity.read"),
            ("owner", "machine_identity.manage"),
            ("security_admin", "machine_identity.read"),
            ("security_admin", "machine_identity.manage"),
            ("developer", "machine_identity.read"),
        }
        assert {
            tuple(row)
            for row in connection.execute(
                "SELECT permission_code,http_method,route_template "
                "FROM machine_permission_catalog"
            )
        } == {
            ("provider.read", "GET", "/providers"),
            ("request_log.read", "GET", "/request-logs"),
        }
        assert connection.execute(
            "SELECT COUNT(*) FROM workspace_memberships "
            "WHERE role_code='service_account' AND status='active'"
        ).fetchone()[0] == 0
    _healthy(database_url)

    assert (
        rollback_last_migration(
            database_url,
            migrations_dir=migrations,
            expected_version="0110",
        ).rolled_back
        == "0110"
    )
    with connect_database(database_url) as connection:
        remaining = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert not PRD118_TABLES & remaining
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0110"]
    _healthy(database_url)


def test_up_guard_rejects_unexpected_prd118_schema(
    migrated_0109: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0109
    database = tmp_path / "up-guard.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    with connect_database(database_url) as connection:
        connection.execute("CREATE TABLE unexpected_prd118(value TEXT)")
        connection.commit()
    with pytest.raises(DbError):
        apply_migrations(database_url, migrations_dir=migrations)
    with connect_database(database_url) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema "
            "WHERE type='table' AND name='service_accounts'"
        ).fetchone()[0] == 0


def test_durable_fact_blocks_down_and_preserves_schema(
    migrated_0109: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0109
    database = tmp_path / "down-guard.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0110"]
    with connect_database(database_url) as connection:
        owner = _owner(connection)
        _insert_service_account(connection, owner)
        connection.commit()
    with pytest.raises(DbError):
        rollback_last_migration(
            database_url,
            migrations_dir=migrations,
            expected_version="0110",
        )
    with connect_database(database_url) as connection:
        assert connection.execute("SELECT COUNT(*) FROM service_accounts").fetchone()[0] == 1
        assert connection.execute(
            "SELECT version FROM schema_migrations WHERE status='applied' "
            "ORDER BY version DESC LIMIT 1"
        ).fetchone()[0] == "0110"
    _healthy(database_url)


def test_owner_scope_audit_and_outbox_transitions_fail_closed(
    migrated_0109: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0109
    database = tmp_path / "invariants.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0110"]

    with connect_database(database_url) as connection:
        owner = _owner(connection)
        _insert_service_account(connection, owner)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO service_account_scopes VALUES(?,?,?)",
                ("svc_" + "1" * 32, "request_log.read", STAMP),
            )
        connection.rollback()
        _insert_service_account(connection, owner)

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE service_accounts SET status='disabled' "
                "WHERE service_account_id=?",
                ("svc_" + "1" * 32,),
            )
        connection.rollback()
        _insert_service_account(connection, owner)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE machine_permission_catalog SET status='active' "
                "WHERE permission_code='provider.read'"
            )
        connection.rollback()
        _insert_service_account(connection, owner)

        with pytest.raises(sqlite3.IntegrityError):
            _insert_audit(
                connection,
                owner,
                audit_id="mia_" + "f" * 32,
                subject_id="gac_" + "f" * 32,
            )
        connection.rollback()
        _insert_service_account(connection, owner)
        _insert_audit(connection, owner, audit_id="mia_" + "1" * 32)
        _insert_outbox(connection, owner)

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """UPDATE machine_identity_event_outbox
                      SET status='delivered',delivered_at=?,updated_at=?
                    WHERE machine_identity_outbox_id=?""",
                (
                    "2026-08-26T00:00:00.100Z",
                    "2026-08-26T00:00:00.100Z",
                    "mio_" + "1" * 32,
                ),
            )
        connection.rollback()
        _insert_service_account(connection, owner)
        _insert_audit(connection, owner, audit_id="mia_" + "1" * 32)
        _insert_outbox(connection, owner)

        connection.execute(
            """UPDATE machine_identity_event_outbox
                  SET status='delivering',attempt_count=1,lease_owner_id='worker-1',
                      lease_token='lease-1',lease_expires_at=?,updated_at=?
                WHERE machine_identity_outbox_id=?""",
            (
                "2026-08-26T00:01:00.000Z",
                "2026-08-26T00:00:00.100Z",
                "mio_" + "1" * 32,
            ),
        )
        connection.execute(
            """UPDATE machine_identity_event_outbox
                  SET status='delivered',lease_owner_id=NULL,lease_token=NULL,
                      lease_expires_at=NULL,delivered_at=?,updated_at=?
                WHERE machine_identity_outbox_id=?""",
            (
                "2026-08-26T00:00:00.200Z",
                "2026-08-26T00:00:00.200Z",
                "mio_" + "1" * 32,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE machine_identity_event_outbox SET updated_at=? "
                "WHERE machine_identity_outbox_id=?",
                ("2026-08-26T00:00:00.300Z", "mio_" + "1" * 32),
            )
        connection.commit()

    _healthy(database_url)


def _owner(connection):
    row = connection.execute(
        """SELECT member.workspace_id,member.tenant_id,member.account_id
             FROM workspace_memberships member
            WHERE member.role_code='owner' AND member.status='active'
            ORDER BY member.created_at LIMIT 1"""
    ).fetchone()
    assert row is not None
    return row


def _insert_service_account(connection, owner) -> None:
    connection.execute(
        """INSERT INTO service_accounts(
             service_account_id,workspace_id,tenant_id,name,display_name,status,
             scope_template,catalog_revision,constraint_sha256,constraint_generation,
             ip_allowlist_json,mtls_fingerprint_sha256,token_ttl_seconds,revision,
             created_by_account_id,updated_by_account_id,last_used_at,revoked_at,
             created_at,updated_at)
           VALUES(?,?,?,?,?,'active','provider_reader',1,?,1,'[]',NULL,300,1,
             ?,?,NULL,NULL,?,?)""",
        (
            "svc_" + "1" * 32,
            owner["workspace_id"],
            owner["tenant_id"],
            "migration-probe",
            "Migration Probe",
            "a" * 64,
            owner["account_id"],
            owner["account_id"],
            STAMP,
            STAMP,
        ),
    )
    connection.execute(
        "INSERT INTO service_account_scopes VALUES(?,?,?)",
        ("svc_" + "1" * 32, "provider.read", STAMP),
    )


def _insert_audit(connection, owner, *, audit_id: str, subject_id: str | None = None) -> None:
    connection.execute(
        """INSERT INTO machine_identity_audits(
             machine_identity_audit_id,workspace_id,tenant_id,subject_type,subject_id,
             action_code,resource_type,resource_id,outcome,permission_code,
             resource_revision,request_id,trace_id,created_at)
           VALUES(?,?,?,'human',?,'service_account.created','service_account',
             ?,'success','machine_identity.manage',1,'req_migration','trace_migration',?)""",
        (
            audit_id,
            owner["workspace_id"],
            owner["tenant_id"],
            owner["account_id"] if subject_id is None else subject_id,
            "svc_" + "1" * 32,
            STAMP,
        ),
    )


def _insert_outbox(connection, owner) -> None:
    connection.execute(
        """INSERT INTO machine_identity_event_outbox(
             machine_identity_outbox_id,machine_identity_audit_id,workspace_id,tenant_id,
             event_type,aggregate_id,aggregate_revision,payload_json,status,attempt_count,
             next_attempt_at,lease_owner_id,lease_token,lease_expires_at,delivered_at,
             created_at,updated_at)
           VALUES(?,?,?,?, 'service_account.created',?,1,?,'pending',0,?,
             NULL,NULL,NULL,NULL,?,?)""",
        (
            "mio_" + "1" * 32,
            "mia_" + "1" * 32,
            owner["workspace_id"],
            owner["tenant_id"],
            "svc_" + "1" * 32,
            '{"constraint_generation":1,"revision":1,'
            '"service_account_id":"svc_11111111111111111111111111111111",'
            '"status":"active"}',
            STAMP,
            STAMP,
            STAMP,
        ),
    )
