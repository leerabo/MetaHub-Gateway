"""PRD-136 supply-chain security schema and guarded rollback."""

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
PRD136_TABLES = {
    "platform_supply_chain_permission_extensions",
    "platform_supply_chain_role_permissions",
    "supply_chain_feed_registry",
    "release_sbom_reports",
    "release_build_provenance",
    "supply_chain_signing_keys",
    "release_artifact_signatures",
    "vulnerability_findings",
    "supply_chain_security_exceptions",
    "patch_releases",
    "supply_chain_events",
    "supply_chain_event_outbox",
    "supply_chain_action_commands",
    "supply_chain_scheduler_state",
    "tenant_deletion_manifest_extensions_prd136",
}


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _healthy(database_url: str) -> None:
    with closing(connect_database(database_url)) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.fixture(scope="module")
def base_0127(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("prd136-0127-base")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 127:
            shutil.copy2(source, migrations / source.name)
    database = root / "base-0127.sqlite3"
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied[-1] == "0127"
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", secrets.token_urlsafe(32))
        apply_seed_data(database_url)
    for filename in (
        "0128_supply_chain_security_provenance.sql",
        "0128_supply_chain_security_provenance.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    return database, migrations


@pytest.fixture
def migrated_0127(base_0127: tuple[Path, Path], tmp_path: Path) -> tuple[Path, Path]:
    base, migrations = base_0127
    database = tmp_path / "prd136-0127.sqlite3"
    shutil.copy2(base, database)
    return database, migrations


def test_0127_to_0128_catalog_manifest_and_roundtrip(
    migrated_0127: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0127
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0128"]
    with closing(connect_database(database_url)) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            ).fetchall()
        }
        assert PRD136_TABLES <= tables
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM platform_supply_chain_permission_extensions"
            ).fetchone()[0]
            == 4
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM platform_supply_chain_role_permissions"
            ).fetchone()[0]
            == 10
        )
        assert (
            connection.execute(
                """SELECT COUNT(*) FROM platform_developer_contract_role_permissions
                     WHERE role_code IN('platform_security','platform_compliance')
                       AND permission_code='platform.developer_contract.read'"""
            ).fetchone()[0]
            == 2
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM supply_chain_feed_registry").fetchone()[0] == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM tenant_deletion_manifest_extensions_prd136"
            ).fetchone()[0]
            == 2
        )
    _healthy(database_url)
    assert (
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0128"
        ).rolled_back
        == "0128"
    )
    _healthy(database_url)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0128"]
    _healthy(database_url)


def test_0128_down_guard_rejects_release_security_fact(
    migrated_0127: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0127
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0128"]
    with closing(connect_database(database_url)) as connection:
        connection.execute(
            """INSERT INTO supply_chain_signing_keys(
                 key_id,purpose,algorithm,public_key_b64,public_key_sha256,status,
                 not_before,not_after,created_at)
               VALUES('prd136-down-guard','metahub.artifact.attestation/v1','ed25519',
                 ?,?,'active','2026-08-31T00:00:00.000Z',
                 '2027-08-31T00:00:00.000Z','2026-08-31T00:00:00.000Z')""",
            ("a" * 43, "b" * 64),
        )
        connection.commit()
    with pytest.raises(DbError) as blocked:
        rollback_last_migration(database_url, migrations_dir=migrations, expected_version="0128")
    assert blocked.value.code == "db.migration_failed"
    _healthy(database_url)


def test_fresh_schema_installs_prd136_head(tmp_path: Path) -> None:
    database_url = _url(tmp_path / "fresh.sqlite3")
    assert apply_migrations(database_url).applied[-1] == "0128"
    _healthy(database_url)
