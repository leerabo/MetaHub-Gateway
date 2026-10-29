"""PRD-132 customer-support schema, permissions, manifests, and rollback guards."""

from __future__ import annotations

import secrets
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from metahub_control.customer_support.permissions import (
    load_verified_workspace_customer_support_permissions,
)
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
PRD132_TABLES = {
    "workspace_customer_support_permission_extensions",
    "workspace_customer_support_role_permissions",
    "platform_customer_support_permission_extensions",
    "platform_customer_support_role_permissions",
    "support_sla_policies",
    "support_tickets",
    "support_ticket_messages",
    "support_ticket_attachments",
    "support_attachment_scan_jobs",
    "support_sla_clocks",
    "support_sla_clock_events",
    "support_escalations",
    "support_satisfaction_surveys",
    "support_ticket_events",
    "support_event_outbox",
    "support_action_commands",
    "support_scheduler_state",
    "tenant_deletion_manifest_extensions_prd132",
}
TENANT_TABLES = {
    "support_tickets",
    "support_ticket_messages",
    "support_ticket_attachments",
    "support_attachment_scan_jobs",
    "support_sla_clocks",
    "support_sla_clock_events",
    "support_escalations",
    "support_satisfaction_surveys",
    "support_ticket_events",
    "support_event_outbox",
    "support_action_commands",
}
_TEST_PASSWORD = f"Prd132!Aa9{secrets.token_urlsafe(18)}"


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _healthy(database_url: str) -> None:
    with closing(connect_database(database_url)) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.fixture(scope="module")
def base_0123(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("prd132-0123-base")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 123:
            shutil.copy2(source, migrations / source.name)
    database = root / "base-0123.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied[-1] == "0123"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", _TEST_PASSWORD)
        apply_seed_data(database_url)
    for filename in (
        "0124_customer_support_ticket_sla_diagnostics.sql",
        "0124_customer_support_ticket_sla_diagnostics.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    _healthy(database_url)
    return database, migrations


@pytest.fixture
def migrated_0123(base_0123: tuple[Path, Path], tmp_path: Path) -> tuple[Path, Path]:
    base, migrations = base_0123
    database = tmp_path / "prd132-0123.sqlite3"
    shutil.copy2(base, database)
    return database, migrations


def test_0123_to_0124_catalog_manifest_and_roundtrip(
    migrated_0123: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0123
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0124"]
    with closing(connect_database(database_url)) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            ).fetchall()
        }
        assert PRD132_TABLES <= tables
        assert {
            str(row[0])
            for row in connection.execute(
                "SELECT source_table FROM tenant_deletion_manifest_extensions_prd132"
            ).fetchall()
        } == TENANT_TABLES
        assert load_verified_workspace_customer_support_permissions(
            connection,
            role_code="owner",
            unavailable_error_code="support.unavailable",
        ) == ("support.ticket.create", "support.ticket.manage", "support.ticket.read")
        assert load_verified_workspace_customer_support_permissions(
            connection,
            role_code="viewer",
            unavailable_error_code="support.unavailable",
        ) == ("support.ticket.read",)
        assert connection.execute("SELECT COUNT(*) FROM support_sla_policies").fetchone()[0] == 4
        with pytest.raises(sqlite3.IntegrityError, match="support_sla_policy_immutable"):
            connection.execute(
                "UPDATE support_sla_policies SET first_response_seconds=600 WHERE severity='p1'"
            )
    _healthy(database_url)
    assert (
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0124"
        ).rolled_back
        == "0124"
    )
    _healthy(database_url)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0124"]
    _healthy(database_url)


def test_0124_down_guard_rejects_support_business_fact(
    migrated_0123: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0123
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0124"]
    with closing(connect_database(database_url)) as connection:
        owner = connection.execute(
            """SELECT membership.workspace_id,membership.tenant_id,membership.account_id
                 FROM workspace_memberships membership
                WHERE membership.role_code='owner' AND membership.status='active'
                ORDER BY membership.workspace_id LIMIT 1"""
        ).fetchone()
        assert owner is not None
        connection.execute(
            """INSERT INTO support_action_commands(
                 support_action_command_id,workspace_id,tenant_id,actor_domain,actor_identity,
                 actor_account_id,actor_operator_id,action_code,idempotency_sha256,
                 request_sha256,result_object_id,created_at)
               VALUES(?,?,?,'customer',?,?,NULL,'support.ticket.create',?,?,?,?)""",
            (
                "sac_" + "1" * 32,
                owner["workspace_id"],
                owner["tenant_id"],
                owner["account_id"],
                owner["account_id"],
                "2" * 64,
                "3" * 64,
                "tkt_" + "4" * 32,
                "2026-08-29T10:00:00.000Z",
            ),
        )
        connection.commit()
    with pytest.raises(DbError) as blocked:
        rollback_last_migration(database_url, migrations_dir=migrations, expected_version="0124")
    assert blocked.value.code == "db.migration_failed"
    _healthy(database_url)


def test_fresh_schema_installs_prd132_head(tmp_path: Path) -> None:
    database_url = _url(tmp_path / "fresh.sqlite3")
    assert apply_migrations(database_url).applied[-1] == "0124"
    _healthy(database_url)
