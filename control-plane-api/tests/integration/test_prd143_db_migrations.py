"""PRD-143 Provider Engineering migration ownership and rollback guards."""

from __future__ import annotations

import shutil
from contextlib import closing
from pathlib import Path

import pytest
from metahub_shared.db import (
    apply_migrations,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _migration_subset(destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=False)
    for source in MIGRATIONS_DIR.glob("*.sql"):
        version = source.name[:4]
        if version.isdigit() and int(version) <= 135:
            shutil.copy2(source, destination / source.name)
    return destination


def test_prd143_fresh_matrix_and_pristine_roundtrip(tmp_path: Path) -> None:
    database_url = _url(tmp_path / "prd143-fresh.sqlite3")
    migrations = _migration_subset(tmp_path / "migrations-0135")
    result = apply_migrations(database_url, migrations_dir=migrations)
    assert result.applied[-1] == "0135"
    with closing(connect_database(database_url)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM platform_provider_engineering_permission_extensions"
        ).fetchone()[0] == 9
        assert connection.execute(
            "SELECT count(*) FROM platform_provider_engineering_role_permissions"
        ).fetchone()[0] == 17
        assert connection.execute(
            "SELECT count(*) FROM workspace_provider_engineering_role_permissions"
        ).fetchone()[0] == 7
        assert connection.execute(
            "SELECT count(*) FROM provider_engineering_scheduler_state"
        ).fetchone()[0] == 6
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert rollback_last_migration(
        database_url, expected_version="0135", migrations_dir=migrations
    ).rolled_back == "0135"
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0135"]


def test_prd143_down_refuses_target_domain_fact(tmp_path: Path) -> None:
    database_url = _url(tmp_path / "prd143-guard.sqlite3")
    migrations = _migration_subset(tmp_path / "migrations-0135")
    assert apply_migrations(database_url, migrations_dir=migrations).applied[-1] == "0135"
    with closing(connect_database(database_url)) as connection:
        connection.execute(
            """
            INSERT INTO provider_engineering_commands(
              provider_engineering_command_id,actor_domain,actor_id,action,object_kind,
              object_id,idempotency_hmac,request_sha256,result_revision,created_at)
            VALUES(?,'platform','operator-test','run_contract','adapter_release',?,?,?,1,?)
            """,
            (
                "pecm_" + "1" * 32,
                "parl_" + "2" * 32,
                "3" * 64,
                "4" * 64,
                "2026-09-01T00:00:00Z",
            ),
        )
        connection.commit()
    with pytest.raises(DbError):
        rollback_last_migration(
            database_url, expected_version="0135", migrations_dir=migrations
        )
