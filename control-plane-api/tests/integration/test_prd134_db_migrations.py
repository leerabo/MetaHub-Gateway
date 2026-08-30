"""PRD-134 consent expansion, privacy schema, manifest, and guarded rollback."""

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
PRD134_TABLES = {
    "workspace_privacy_permission_extensions",
    "workspace_privacy_role_permissions",
    "privacy_preferences",
    "data_subject_requests",
    "privacy_owner_adapter_versions",
    "data_subject_request_owner_jobs",
    "identity_exports",
    "erasure_decisions",
    "retention_exceptions",
    "privacy_events",
    "privacy_event_outbox",
    "privacy_action_commands",
    "privacy_scheduler_state",
    "tenant_deletion_manifest_extensions_prd134",
}


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _healthy(database_url: str) -> None:
    with closing(connect_database(database_url)) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.fixture(scope="module")
def base_0125(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("prd134-0125-base")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 125:
            shutil.copy2(source, migrations / source.name)
    database = root / "base-0125.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied[-1] == "0125"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", secrets.token_urlsafe(32))
        apply_seed_data(database_url)
    for filename in (
        "0126_global_account_privacy_dsr.sql",
        "0126_global_account_privacy_dsr.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    return database, migrations


@pytest.fixture
def migrated_0125(base_0125: tuple[Path, Path], tmp_path: Path) -> tuple[Path, Path]:
    base, migrations = base_0125
    database = tmp_path / "prd134-0125.sqlite3"
    shutil.copy2(base, database)
    return database, migrations


def test_0125_to_0126_catalog_consent_manifest_and_roundtrip(
    migrated_0125: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0125
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0126"]
    with closing(connect_database(database_url)) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            ).fetchall()
        }
        assert PRD134_TABLES <= tables
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM workspace_privacy_permission_extensions"
            ).fetchone()[0]
            == 5
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM workspace_privacy_role_permissions"
            ).fetchone()[0]
            == 40
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM privacy_owner_adapter_versions").fetchone()[0]
            == 7
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM tenant_deletion_manifest_extensions_prd134"
            ).fetchone()[0]
            == 9
        )
        registration = connection.execute(
            "SELECT source_kind,decision,locale FROM consent_records LIMIT 1"
        ).fetchone()
        columns = {
            str(row["name"]): row["dflt_value"]
            for row in connection.execute("PRAGMA table_info(consent_records)").fetchall()
        }
        assert columns["locale"] == "'und'"
        assert columns["source_kind"] == "'registration'"
        assert columns["decision"] == "'granted'"
        if registration is not None:
            assert tuple(registration) == ("registration", "granted", "und")
    _healthy(database_url)
    assert (
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0126"
        ).rolled_back
        == "0126"
    )
    _healthy(database_url)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0126"]
    _healthy(database_url)


def test_0126_down_guard_rejects_privacy_business_fact(
    migrated_0125: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0125
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0126"]
    with closing(connect_database(database_url)) as connection:
        owner = connection.execute(
            """SELECT member.workspace_id,member.tenant_id,member.account_id
                 FROM workspace_memberships member
                 WHERE member.status='active' AND member.role_code='owner' LIMIT 1"""
        ).fetchone()
        connection.execute(
            """INSERT INTO privacy_preferences(
                 privacy_preference_id,workspace_id,tenant_id,account_id,analytics_enabled,
                 product_updates_enabled,model_improvement_enabled,terms_version,locale,
                 revision,created_at,updated_at)
               VALUES(?,?,?, ?,0,0,0,'privacy-2026-08','zh-cn',1,
                 '2026-08-30T00:00:00.000Z','2026-08-30T00:00:00.000Z')""",
            ("prf_" + "a" * 32, owner["workspace_id"], owner["tenant_id"], owner["account_id"]),
        )
        connection.commit()
    with pytest.raises(DbError) as blocked:
        rollback_last_migration(database_url, migrations_dir=migrations, expected_version="0126")
    assert blocked.value.code == "db.migration_failed"
    _healthy(database_url)


def test_fresh_schema_installs_prd134_head(tmp_path: Path) -> None:
    database_url = _url(tmp_path / "fresh.sqlite3")
    assert apply_migrations(database_url).applied[-1] == "0126"
    _healthy(database_url)
