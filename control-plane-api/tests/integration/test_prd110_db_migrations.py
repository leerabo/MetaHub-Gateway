"""PRD-110 risk-control migration, ownership, and rollback contracts."""

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
PRD110_TABLES = {
    "platform_risk_permission_extensions",
    "platform_risk_role_permissions",
    "abuse_counters",
    "abuse_counter_facts",
    "risk_cases",
    "risk_case_transitions",
    "risk_signals",
    "risk_decisions",
    "risk_restrictions",
    "risk_challenges",
    "risk_challenge_attempts",
    "risk_challenge_commands",
    "risk_appeals",
    "risk_commands",
    "risk_event_outbox",
    "risk_degraded_counters",
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
def migrated_0101(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("prd110-migrated-0101")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 101:
            shutil.copy2(source, migrations / source.name)
    database = root / "prd110-0101.sqlite3"
    assert apply_migrations(_url(database), migrations_dir=migrations).applied[-1] == "0101"
    apply_seed_data(_url(database))
    for filename in (
        "0102_abuse_risk_controls.sql",
        "0102_abuse_risk_controls.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    return database, migrations


def test_0101_to_0102_pristine_roundtrip(migrated_0101: tuple[Path, Path], tmp_path: Path) -> None:
    source, migrations = migrated_0101
    database = tmp_path / "roundtrip.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)

    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0102"]
    connection = connect_database(database_url)
    try:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert PRD110_TABLES <= tables
        roles = [
            tuple(row)
            for row in connection.execute(
                "SELECT role_code,permission_code FROM platform_risk_role_permissions "
                "ORDER BY role_code,permission_code"
            ).fetchall()
        ]
        assert roles == [
            ("platform_owner", "platform.risk.decide"),
            ("platform_owner", "platform.risk.read"),
            ("platform_security", "platform.risk.decide"),
            ("platform_security", "platform.risk.read"),
            ("platform_support", "platform.risk.support_read"),
        ]
        assert tuple(
            connection.execute(
                "SELECT status,revision FROM platform_job_types "
                "WHERE type_code='risk.restriction.expire'"
            ).fetchone()
        ) == ("active", 2)
    finally:
        connection.close()
    _healthy(database_url)

    assert (
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0102"
        ).rolled_back
        == "0102"
    )
    connection = connect_database(database_url)
    try:
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        }
        assert PRD110_TABLES.isdisjoint(tables)
        assert tuple(
            connection.execute(
                "SELECT status,revision FROM platform_job_types "
                "WHERE type_code='risk.restriction.expire'"
            ).fetchone()
        ) == ("disabled", 1)
    finally:
        connection.close()
    _healthy(database_url)

    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0102"]
    _healthy(database_url)


def test_owner_mismatch_and_business_facts_block_down(
    migrated_0101: tuple[Path, Path], tmp_path: Path
) -> None:
    source, migrations = migrated_0101
    database = tmp_path / "guarded.sqlite3"
    shutil.copy2(source, database)
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0102"]
    connection = connect_database(database_url)
    try:
        connection.execute(
            """INSERT INTO tenants VALUES(
                 'prd110-owner-mismatch-tenant','prd110-owner-mismatch',
                 'PRD-110 owner mismatch','internal_only','active',
                 '2026-08-24T00:00:00Z','2026-08-24T00:00:00Z')"""
        )
        owners = connection.execute(
            "SELECT workspace_id,tenant_id FROM workspace_tenant_bindings "
            "WHERE status='active' ORDER BY workspace_id LIMIT 2"
        ).fetchall()
        assert len(owners) >= 2
        case_id = "rca_" + "1" * 32
        connection.execute(
            """INSERT INTO risk_cases(
                 risk_case_id,workspace_id,tenant_id,case_kind,safe_reason_family,score_band,
                 status,source_watermark,revision,opened_at,updated_at,closed_at)
               VALUES(?,?,?,'api_abuse','unusual_volume','high','open',0,1,
                 '2026-08-24T00:00:00Z','2026-08-24T00:00:00Z',NULL)""",
            (case_id, owners[0]["workspace_id"], owners[0]["tenant_id"]),
        )
        with pytest.raises(Exception, match="FOREIGN KEY constraint failed"):
            connection.execute(
                """INSERT INTO risk_signals VALUES(
                     'rsg_11111111111111111111111111111111',?,?,?,?,
                     'warning','2026-08-24T00:00:00Z',?,?,?, ?,1,
                     '2026-08-24T00:00:00Z')""",
                (
                    case_id,
                    owners[1]["workspace_id"],
                    owners[1]["tenant_id"],
                    "request_velocity",
                    "a" * 64,
                    None,
                    None,
                    "b" * 64,
                ),
            )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(DbError) as blocked:
        rollback_last_migration(database_url, migrations_dir=migrations, expected_version="0102")
    assert blocked.value.code == "db.migration_failed"
    _healthy(database_url)


def test_fresh_schema_installs_current_head(tmp_path: Path) -> None:
    database_url = _url(tmp_path / "fresh.sqlite3")
    assert apply_migrations(database_url).applied[-1] == "0102"
    _healthy(database_url)
