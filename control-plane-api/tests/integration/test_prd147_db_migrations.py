"""PRD-147 console-domain migration schema, manifests, and rollback guards."""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from metahub_control.console_migration.manifest import SCHEDULER_QUEUE_MANIFEST
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


def test_prd147_fresh_matrix_and_pristine_roundtrip(tmp_path: Path) -> None:
    database_url = _url(tmp_path / "prd147-fresh.sqlite3")
    result = apply_migrations(database_url)
    assert result.applied[-1] == "0139"

    with closing(connect_database(database_url)) as connection:
        counts = {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "legacy_route_catalog",
                "scheduler_registry_entries",
                "scheduler_baseline_mappings",
                "scheduler_queue_contracts",
                "runtime_surface_manifest",
                "platform_console_migration_batch_items",
            )
        }
        assert counts == {
            "legacy_route_catalog": 46,
            "scheduler_registry_entries": 37,
            "scheduler_baseline_mappings": 41,
            "scheduler_queue_contracts": 64,
            "runtime_surface_manifest": 3,
            "platform_console_migration_batch_items": 0,
        }
        assert connection.execute(
            """SELECT count(*) FROM scheduler_registry_entries
               WHERE scheduler_code='platform-job-runtime'
                 AND execution_class='cluster_singleton'"""
        ).fetchone()[0] == 1
        assert connection.execute(
            """SELECT count(DISTINCT final_scheduler_code)
               FROM scheduler_baseline_mappings"""
        ).fetchone()[0] == 35
        queue_contracts = connection.execute(
            """SELECT scheduler_code,queue_code,owner_domain,partition_policy_code,
                      manifest_digest
               FROM scheduler_queue_contracts ORDER BY queue_code"""
        ).fetchall()
        assert [tuple(row) for row in queue_contracts] == sorted(
            (
                queue.scheduler_code,
                queue.code,
                queue.owner_domain,
                queue.partition_policy_code,
                queue.manifest_digest,
            )
            for queue in SCHEDULER_QUEUE_MANIFEST
        )
        assert connection.execute(
            """SELECT count(*) FROM legacy_route_catalog
               WHERE (method='GET' AND normalized_path='/branding/public')
                  OR normalized_path LIKE '/platform/%'"""
        ).fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

        with pytest.raises(sqlite3.IntegrityError, match="legacy_route_catalog_immutable"):
            connection.execute(
                """UPDATE legacy_route_catalog SET status='planned'
                   WHERE route_code='console-navigation'"""
            )
        with pytest.raises(sqlite3.IntegrityError, match="scheduler_registry_manifest_immutable"):
            connection.execute(
                """UPDATE scheduler_registry_entries SET revision=2
                   WHERE scheduler_code='platform-job-runtime'"""
            )
        with pytest.raises(sqlite3.IntegrityError, match="scheduler_fence_invalid"):
            connection.execute(
                """INSERT INTO scheduler_watermarks VALUES(
                   'platform-job-runtime','global','jobs',?,0,1,1,?)""",
                ("0" * 64, "2026-09-04T00:00:00Z"),
            )

    assert apply_migrations(database_url).applied == []
    assert rollback_last_migration(
        database_url, expected_version="0139"
    ).rolled_back == "0139"
    with closing(connect_database(database_url)) as connection:
        assert connection.execute(
            "SELECT max(version) FROM schema_migrations WHERE status='applied'"
        ).fetchone()[0] == "0138"
        assert connection.execute(
            """SELECT count(*) FROM sqlite_schema
               WHERE name IN('legacy_route_catalog','scheduler_registry_entries',
                 'console_domain_migrations','console_ingress_contract_heads',
                 'platform_console_migration_batch_items')"""
        ).fetchone()[0] == 0
    assert apply_migrations(database_url).applied == ["0139"]


def test_prd147_upgrades_0138_and_refuses_fact_loss(tmp_path: Path) -> None:
    migrations_0138 = _migration_subset(tmp_path / "migrations-0138", 138)
    database_url = _url(tmp_path / "prd147-upgrade.sqlite3")
    assert apply_migrations(
        database_url, migrations_dir=migrations_0138
    ).applied[-1] == "0138"
    assert apply_migrations(database_url).applied == ["0139"]

    with closing(connect_database(database_url)) as connection:
        connection.execute(
            """INSERT INTO platform_console_migration_audits(
               platform_console_migration_audit_id,operator_pseudonym,
               operator_revision,factor_revision,session_epoch_hmac,action_code,owner_ref,
               old_state,new_state,quoted_revision,idempotency_key_hmac,
               permission_fingerprint,result_code,request_id,trace_id,created_at)
               VALUES(?,?,1,1,?,'backfill.prepare',NULL,'inventoried','backfill_queued',
                 0,?,?,'accepted','request-prd147','trace-prd147',?)""",
            (
                "cau_" + "1" * 32,
                "2" * 64,
                "3" * 64,
                "4" * 64,
                "5" * 64,
                "2026-09-04T00:00:00Z",
            ),
        )
        connection.commit()

    with pytest.raises(DbError, match="Rollback failed"):
        rollback_last_migration(database_url, expected_version="0139")
    with closing(connect_database(database_url)) as connection:
        assert connection.execute(
            "SELECT status FROM schema_migrations WHERE version='0139'"
        ).fetchone()[0] == "applied"
        assert connection.execute(
            "SELECT count(*) FROM platform_console_migration_audits"
        ).fetchone()[0] == 1
