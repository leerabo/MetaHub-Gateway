"""PRD-133 public API and SDK lifecycle schema and guarded rollback."""

from __future__ import annotations

import shutil
from contextlib import closing
from pathlib import Path

import pytest
from metahub_shared.db import apply_migrations, connect_database, rollback_last_migration
from metahub_shared.db.errors import DbError

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
PRD133_TABLES = {
    "platform_developer_contract_permission_extensions",
    "platform_developer_contract_role_permissions",
    "developer_contract_signing_keys",
    "api_versions",
    "api_contract_snapshots",
    "sdk_releases",
    "deprecation_notices",
    "webhook_schema_versions",
    "developer_contract_download_audits",
    "developer_contract_events",
    "developer_contract_event_outbox",
    "developer_contract_action_commands",
    "developer_contract_scheduler_state",
}


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _healthy(database_url: str) -> None:
    with closing(connect_database(database_url)) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.fixture(scope="module")
def base_0124(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("prd133-0124-base")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 124:
            shutil.copy2(source, migrations / source.name)
    database = root / "base-0124.sqlite3"
    assert apply_migrations(_url(database), migrations_dir=migrations).applied[-1] == "0124"
    for filename in (
        "0125_public_api_sdk_lifecycle.sql",
        "0125_public_api_sdk_lifecycle.down.sql",
    ):
        shutil.copy2(MIGRATIONS_DIR / filename, migrations / filename)
    return database, migrations


@pytest.fixture
def migrated_0124(base_0124: tuple[Path, Path], tmp_path: Path) -> tuple[Path, Path]:
    base, migrations = base_0124
    database = tmp_path / "prd133-0124.sqlite3"
    shutil.copy2(base, database)
    return database, migrations


def test_0124_to_0125_catalog_permissions_and_roundtrip(
    migrated_0124: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0124
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0125"]
    with closing(connect_database(database_url)) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            ).fetchall()
        }
        assert PRD133_TABLES <= tables
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM platform_developer_contract_permission_extensions"
            ).fetchone()[0]
            == 3
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM platform_developer_contract_role_permissions"
            ).fetchone()[0]
            == 6
        )
        assert (
            connection.execute(
                "SELECT fencing_generation FROM developer_contract_scheduler_state"
            ).fetchone()[0]
            == 0
        )
    _healthy(database_url)
    assert (
        rollback_last_migration(
            database_url, migrations_dir=migrations, expected_version="0125"
        ).rolled_back
        == "0125"
    )
    _healthy(database_url)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0125"]
    _healthy(database_url)


def test_0125_down_guard_rejects_signing_key_fact(
    migrated_0124: tuple[Path, Path],
) -> None:
    database, migrations = migrated_0124
    database_url = _url(database)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0125"]
    with closing(connect_database(database_url)) as connection:
        connection.execute(
            """INSERT INTO developer_contract_signing_keys(
                 key_id,purpose,algorithm,public_key_b64,public_key_sha256,status,
                 not_before,not_after,created_at)
               VALUES(?,'metahub.public-contract.release/v1','ed25519',?,?,'active',?,?,?)""",
            (
                "release-test-v1",
                "A" * 43,
                "b" * 64,
                "2026-08-29T00:00:00.000Z",
                "2027-08-29T00:00:00.000Z",
                "2026-08-29T00:00:00.000Z",
            ),
        )
        connection.commit()
    with pytest.raises(DbError) as blocked:
        rollback_last_migration(database_url, migrations_dir=migrations, expected_version="0125")
    assert blocked.value.code == "db.migration_failed"
    _healthy(database_url)


def test_fresh_schema_installs_prd133_head(tmp_path: Path) -> None:
    database_url = _url(tmp_path / "fresh.sqlite3")
    assert apply_migrations(database_url).applied[-1] == "0125"
    _healthy(database_url)
