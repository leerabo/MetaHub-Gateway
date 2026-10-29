"""PRD-135 compliance schema, machine catalog extension, and guarded rollback."""

from __future__ import annotations

import secrets
import shutil
from contextlib import closing
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
PRD135_TABLES = {
    "workspace_compliance_permission_extensions",
    "workspace_compliance_role_permissions",
    "platform_compliance_permission_extensions",
    "platform_compliance_role_permissions",
    "compliance_controls",
    "compliance_control_mappings",
    "compliance_evidence",
    "compliance_exceptions",
    "compliance_remediation_tasks",
    "compliance_reports",
    "compliance_report_items",
    "compliance_events",
    "compliance_event_outbox",
    "compliance_action_commands",
    "compliance_collector_replay_guards",
    "compliance_scheduler_state",
    "tenant_deletion_manifest_extensions_prd135",
}


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _healthy(database_url: str) -> None:
    with closing(connect_database(database_url)) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.fixture(scope="module")
def base_0126(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("prd135-0126-base")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 126:
            shutil.copy2(source, migrations / source.name)
    database = root / "base-0126.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied[-1] == "0126"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", secrets.token_urlsafe(32))
        apply_seed_data(database_url)
    for filename in (
        "0127_compliance_evidence_center.sql",
        "0127_compliance_evidence_center.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    return database, migrations


@pytest.fixture
def migrated_0126(base_0126: tuple[Path, Path], tmp_path: Path) -> tuple[Path, Path]:
    base, migrations = base_0126
    database = tmp_path / "prd135-0126.sqlite3"
    shutil.copy2(base, database)
    return database, migrations


def test_0126_to_0127_catalog_manifest_and_roundtrip(
    migrated_0126: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0126
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0127"]
    with closing(connect_database(database_url)) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            ).fetchall()
        }
        assert PRD135_TABLES <= tables
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM workspace_compliance_role_permissions"
            ).fetchone()[0]
            == 3
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM platform_compliance_role_permissions"
            ).fetchone()[0]
            == 4
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM machine_permission_catalog").fetchone()[0] == 3
        )
        assert (
            connection.execute(
                "SELECT scope_template FROM machine_permission_catalog "
                "WHERE permission_code='compliance.collect'"
            ).fetchone()[0]
            == "compliance_collector"
        )
        audit_sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name='machine_identity_audits'"
        ).fetchone()[0]
        assert "compliance.collect" in audit_sql
        assert "machine.compliance_evidence_submitted" in audit_sql
        assert connection.execute("SELECT COUNT(*) FROM compliance_controls").fetchone()[0] == 3
        assert (
            connection.execute("SELECT COUNT(*) FROM compliance_control_mappings").fetchone()[0]
            == 6
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM tenant_deletion_manifest_extensions_prd135"
            ).fetchone()[0]
            == 9
        )
    _healthy(database_url)
    assert (
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0127"
        ).rolled_back
        == "0127"
    )
    _healthy(database_url)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0127"]
    _healthy(database_url)


def test_0127_down_guard_rejects_non_seed_control(
    migrated_0126: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0126
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0127"]
    with closing(connect_database(database_url)) as connection:
        connection.execute(
            """INSERT INTO compliance_controls VALUES(
                 'ctl_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','test.custom_control',1,'test.1',
                 'Test custom control','security','workspace','access_review',1,86400,
                 'medium','sample_failure_v1',metahub_sha256('sample_failure_v1'),'active',0,
                 '2026-08-30T00:00:00.000Z')"""
        )
        connection.commit()
    with pytest.raises(DbError) as blocked:
        rollback_last_migration(database_url, migrations_dir=migrations, expected_version="0127")
    assert blocked.value.code == "db.migration_failed"
    _healthy(database_url)


def test_fresh_schema_installs_prd135_head(tmp_path: Path) -> None:
    database_url = _url(tmp_path / "fresh.sqlite3")
    assert apply_migrations(database_url).applied[-1] == "0127"
    _healthy(database_url)
