"""PRD-146 Workspace navigation migration matrix and rollback guards."""

from __future__ import annotations

import shutil
import sqlite3
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


def _migration_subset(destination: Path, maximum_version: int) -> Path:
    destination.mkdir(parents=True, exist_ok=False)
    for source in MIGRATIONS_DIR.glob("*.sql"):
        version = source.name[:4]
        if version.isdigit() and int(version) <= maximum_version:
            shutil.copy2(source, destination / source.name)
    return destination


def test_prd146_fresh_matrix_and_pristine_roundtrip(tmp_path: Path) -> None:
    database_url = _url(tmp_path / "prd146-fresh.sqlite3")
    migrations_0138 = _migration_subset(tmp_path / "migrations-0138", 138)
    result = apply_migrations(database_url, migrations_dir=migrations_0138)
    assert result.applied[-1] == "0138"
    with closing(connect_database(database_url)) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM workspace_navigation_permission_extensions"
            ).fetchone()[0]
            == 4
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM workspace_navigation_role_permissions"
            ).fetchone()[0]
            == 26
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM workspace_navigation_scheduler_state"
            ).fetchone()[0]
            == 5
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM workspace_navigation_migration_cursors"
            ).fetchone()[0]
            == 5
        )
        assert (
            connection.execute(
                """SELECT count(*) FROM workspace_navigation_migration_cursors
               WHERE source_kind='catalog' AND source_row_count=63
                 AND source_watermark='0137' AND state='inventory'"""
            ).fetchone()[0]
            == 1
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """UPDATE workspace_navigation_permission_extensions
                   SET introduced_in_migration='0138'
                   WHERE permission_code='workspace.navigation.read'"""
            )
    assert (
        rollback_last_migration(
            database_url,
            expected_version="0138",
            migrations_dir=migrations_0138,
        ).rolled_back
        == "0138"
    )
    with closing(connect_database(database_url)) as connection:
        assert (
            connection.execute(
                """SELECT count(*) FROM sqlite_schema
               WHERE name LIKE 'workspace_navigation_%'
                  OR name LIKE 'workspace_role_task_home_%'
                  OR name LIKE 'workspace_advanced_tool_%'"""
            ).fetchone()[0]
            == 0
        )
    assert apply_migrations(database_url, migrations_dir=migrations_0138).applied == ["0138"]


def test_prd146_upgrades_0137_and_refuses_non_pristine_down(tmp_path: Path) -> None:
    migrations_0137 = _migration_subset(tmp_path / "migrations-0137", 137)
    migrations_0138 = _migration_subset(tmp_path / "migrations-0138", 138)
    database_url = _url(tmp_path / "prd146-upgrade.sqlite3")
    assert (
        apply_migrations(
            database_url,
            migrations_dir=migrations_0137,
        ).applied[-1]
        == "0137"
    )
    assert apply_migrations(database_url, migrations_dir=migrations_0138).applied == ["0138"]
    with closing(connect_database(database_url)) as connection:
        connection.execute(
            """UPDATE workspace_navigation_scheduler_state
               SET fence_generation=2
               WHERE queue_code='navigation_reconciliation'"""
        )
        connection.commit()
    with pytest.raises(DbError, match="Rollback failed"):
        rollback_last_migration(
            database_url,
            expected_version="0138",
            migrations_dir=migrations_0138,
        )
