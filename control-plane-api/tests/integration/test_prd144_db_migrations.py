"""PRD-144 commercial accounting migration matrices and rollback guards."""

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


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _migration_subset(destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=False)
    source_dir = Path(__file__).resolve().parents[3] / "infra" / "migrations"
    for source in source_dir.glob("*.sql"):
        version = source.name[:4]
        if version.isdigit() and int(version) <= 136:
            shutil.copy2(source, destination / source.name)
    return destination


def test_prd144_fresh_matrix_and_pristine_roundtrip(tmp_path: Path) -> None:
    database_url = _url(tmp_path / "prd144-fresh.sqlite3")
    migrations = _migration_subset(tmp_path / "migrations-0136")
    result = apply_migrations(database_url, migrations_dir=migrations)
    assert result.applied[-1] == "0136"
    with closing(connect_database(database_url)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM platform_commercial_permission_extensions"
        ).fetchone()[0] == 8
        assert connection.execute(
            "SELECT count(*) FROM platform_commercial_role_permissions"
        ).fetchone()[0] == 13
        assert connection.execute(
            "SELECT count(*) FROM workspace_commercial_role_permissions"
        ).fetchone()[0] == 14
        assert connection.execute(
            "SELECT count(*) FROM commercial_accounting_scheduler_state"
        ).fetchone()[0] == 10
        assert connection.execute(
            "SELECT count(*) FROM commercial_accounting_migration_cursors"
        ).fetchone()[0] == 10
        source_scope_index = next(
            row[1]
            for row in connection.execute(
                "PRAGMA index_list(workspace_provider_cost_rate_versions)"
            ).fetchall()
            if [
                column[2]
                for column in connection.execute(
                    f"PRAGMA index_info({row[1]})"
                ).fetchall()
            ]
            == ["workspace_provider_cost_rate_id", "source_type", "source_id"]
        )
        assert source_scope_index
        reconciliation_index = next(
            row[1]
            for row in connection.execute(
                "PRAGMA index_list(reconciliation_differences)"
            ).fetchall()
            if row[1] == "idx_reconciliation_differences_billing_domain_prd144"
        )
        assert [
            column[2]
            for column in connection.execute(
                f"PRAGMA index_info({reconciliation_index})"
            ).fetchall()
        ] == [
            "reconciliation_run_id",
            "workspace_id",
            "tenant_id",
            "currency",
            "reconciliation_difference_id",
        ]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert rollback_last_migration(
        database_url, expected_version="0136", migrations_dir=migrations
    ).rolled_back == "0136"
    with closing(connect_database(database_url)) as connection:
        routing_candidates_sql = connection.execute(
            "SELECT sql FROM sqlite_schema "
            "WHERE type='table' AND name='routing_candidates'"
        ).fetchone()[0]
        assert "workspace_provider_cost_rate" not in routing_candidates_sql
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0136"]
    with closing(connect_database(database_url)) as connection:
        routing_candidates_sql = connection.execute(
            "SELECT sql FROM sqlite_schema "
            "WHERE type='table' AND name='routing_candidates'"
        ).fetchone()[0]
        assert "workspace_provider_cost_rate" in routing_candidates_sql


def test_prd144_down_refuses_target_domain_fact(tmp_path: Path) -> None:
    database_url = _url(tmp_path / "prd144-guard.sqlite3")
    migrations = _migration_subset(tmp_path / "migrations-0136")
    assert apply_migrations(database_url, migrations_dir=migrations).applied[-1] == "0136"
    with closing(connect_database(database_url)) as connection:
        connection.execute(
            """
            INSERT INTO commercial_accounting_commands(
              commercial_accounting_command_id,actor_domain,actor_id,action,object_kind,
              object_id,owner_sha256,idempotency_hmac,request_sha256,expected_revision,
              permission_revision,result_id,result_sha256,created_at)
            VALUES(?,'platform','operator-test','release_plan','plan_catalog',?,?,?,?,0,1,?,?,?)
            """,
            (
                "cacm_" + "1" * 32,
                "plan_" + "2" * 32,
                "3" * 64,
                "4" * 64,
                "5" * 64,
                "plan_release_" + "6" * 16,
                "7" * 64,
                "2026-09-03T00:00:00Z",
            ),
        )
        connection.commit()
    with pytest.raises(DbError):
        rollback_last_migration(
            database_url, expected_version="0136", migrations_dir=migrations
        )
