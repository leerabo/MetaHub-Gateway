"""PRD-112 migration, ownership, immutability, and rollback contracts."""

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
PRD112_TABLES = {
    "platform_status_permission_extensions",
    "platform_status_role_permissions",
    "status_components",
    "incidents",
    "incident_workspace_scopes",
    "incident_updates",
    "maintenance_windows",
    "status_subscriptions",
    "postmortems",
    "status_publication_bundles",
    "status_commands",
    "status_event_outbox",
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


def _insert_operator(connection: sqlite3.Connection) -> str:
    source = connection.execute(
        "SELECT * FROM global_accounts ORDER BY account_id LIMIT 1"
    ).fetchone()
    assert source is not None
    account = dict(source)
    account_id = "gac_" + "d112" * 8
    operator_id = "pto_" + "d112" * 8
    account.update(
        {
            "account_id": account_id,
            "email_normalized": "prd112-migration@example.invalid",
            "phone_e164": None,
            "display_name": "PRD112 migration operator",
            "status": "active",
            "login_enabled": 1,
            "failed_login_count": 0,
            "locked_until": None,
            "last_login_at": None,
        }
    )
    columns = tuple(account)
    connection.execute(
        f"INSERT INTO global_accounts({','.join(columns)}) "
        f"VALUES({','.join('?' for _ in columns)})",
        tuple(account[column] for column in columns),
    )
    connection.execute(
        """INSERT INTO platform_operators(
             operator_id,account_id,status,revision,created_by_operator_id,
             created_at,updated_at)
           VALUES(?,?,'active',1,NULL,datetime('now'),datetime('now'))""",
        (operator_id, account_id),
    )
    return operator_id


@pytest.fixture(scope="module")
def migrated_0103(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("prd112-migrated-0103")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 103:
            shutil.copy2(source, migrations / source.name)
    database = root / "prd112-0103.sqlite3"
    assert apply_migrations(_url(database), migrations_dir=migrations).applied[-1] == "0103"
    apply_seed_data(_url(database))
    for filename in (
        "0104_public_status_incidents.sql",
        "0104_public_status_incidents.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    return database, migrations


def test_0103_to_0104_pristine_roundtrip(
    migrated_0103: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0103
    database = tmp_path / "roundtrip.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)

    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0104"]
    connection = connect_database(database_url)
    try:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert PRD112_TABLES <= tables
        assert connection.execute("SELECT COUNT(*) FROM status_components").fetchone()[0] == 5
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM platform_status_role_permissions"
            ).fetchone()[0]
            == 9
        )
    finally:
        connection.close()
    _healthy(database_url)

    assert (
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0104"
        ).rolled_back
        == "0104"
    )
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0104"]
    _healthy(database_url)


def test_composite_owner_and_immutable_fact_guards(
    migrated_0103: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0103
    database = tmp_path / "ownership.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0104"]
    connection = connect_database(database_url)
    try:
        operator_id = _insert_operator(connection)
        binding = connection.execute(
            """SELECT workspace_id,tenant_id FROM workspace_tenant_bindings
               ORDER BY workspace_id LIMIT 1"""
        ).fetchone()
        assert binding is not None
        tenant = dict(
            connection.execute(
                "SELECT * FROM tenants WHERE tenant_id=?", (binding["tenant_id"],)
            ).fetchone()
        )
        tenant.update(
            {
                "tenant_id": "ten_" + "e112" * 8,
                "slug": "prd112-owner-mismatch",
                "name": "PRD112 Owner Mismatch",
                "updated_at": connection.execute("SELECT datetime('now')").fetchone()[0],
            }
        )
        tenant_columns = tuple(tenant)
        connection.execute(
            f"INSERT INTO tenants({','.join(tenant_columns)}) "
            f"VALUES({','.join('?' for _ in tenant_columns)})",
            tuple(tenant[column] for column in tenant_columns),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                """INSERT INTO maintenance_windows(
                     maintenance_window_id,workspace_id,tenant_id,public_reference,title,
                     public_message,visibility,status,affected_components_json,
                     affected_regions_json,starts_at,ends_at,created_by_operator_id,
                     revision,created_at,updated_at)
                   VALUES(?,?,?,'MW-20260825-BAD001','Owner mismatch',
                     'This must not cross an owner boundary.','workspace','planned','[]','[]',
                     datetime('now','+1 hour'),datetime('now','+2 hours'),?,1,
                     datetime('now'),datetime('now'))""",
                (
                    "mtn_" + "a112" * 8,
                    binding["workspace_id"],
                    tenant["tenant_id"],
                    operator_id,
                ),
            )
        connection.rollback()

        connection.execute(
            """INSERT INTO incidents(
                 incident_id,public_reference,title,classification,visibility,severity,status,
                 acknowledged_at,first_update_at,created_by_operator_id,
                 security_approved_by_operator_id,security_approved_at,revision,
                 created_at,updated_at,resolved_at)
               VALUES(?,'INC-20260825-IMM001','Immutable incident','reliability','public',
                 'minor','published',datetime('now'),datetime('now'),?,NULL,NULL,1,
                 datetime('now'),datetime('now'),NULL)""",
            ("inc_" + "b112" * 8, operator_id),
        )
        connection.execute(
            """INSERT INTO incident_updates(
                 incident_update_id,incident_id,status,public_message,
                 affected_components_json,affected_regions_json,operator_id,published_at,
                 payload_sha256)
               VALUES(?,?,'published','The service is being investigated.','[]','[]',?,
                 datetime('now'),?)""",
            (
                "iup_" + "c112" * 8,
                "inc_" + "b112" * 8,
                operator_id,
                "a" * 64,
            ),
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="incident_update_immutable"):
            connection.execute(
                "UPDATE incident_updates SET public_message=? WHERE incident_update_id=?",
                ("A changed public message is forbidden.", "iup_" + "c112" * 8),
            )
        connection.rollback()
    finally:
        connection.close()

    with pytest.raises(DbError):
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0104"
        )
    _healthy(database_url)


def test_fresh_schema_installs_current_head(tmp_path: Path) -> None:
    database_url = _url(tmp_path / "fresh.sqlite3")
    assert apply_migrations(database_url).applied[-1] == "0105"
    _healthy(database_url)
