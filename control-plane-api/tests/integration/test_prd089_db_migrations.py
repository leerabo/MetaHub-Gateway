"""PRD-089 migration 0082 fresh-up, round-trip, and irreversible-fact guards."""

from __future__ import annotations

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


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _healthy(database_url: str) -> None:
    connection = connect_database(database_url)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def _version(database_url: str) -> str:
    connection = connect_database(database_url)
    try:
        return str(
            connection.execute(
                "SELECT version FROM schema_migrations WHERE status='applied' "
                "ORDER BY version DESC LIMIT 1"
            ).fetchone()[0]
        )
    finally:
        connection.close()


def test_0082_fresh_up_pristine_down_and_reapply(tmp_path: Path) -> None:
    database = tmp_path / "fresh-0082.sqlite3"
    database_url = _url(database)
    result = apply_migrations(database_url)
    assert result.applied == [f"{version:04d}" for version in range(1, 83)]
    seed = apply_seed_data(database_url)

    connection = connect_database(database_url)
    try:
        assert _version(database_url) == "0082"
        assert tuple(
            connection.execute(
                "SELECT (SELECT COUNT(*) FROM rbac_permission_templates),"
                "(SELECT COUNT(*) FROM rbac_role_permission_templates),"
                "(SELECT COUNT(*) FROM tenant_data_states WHERE tenant_id=?)",
                (seed.tenant_id,),
            ).fetchone()
        ) == (102, 259, 1)
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN "
                "('tenant_export_jobs','deletion_requests','tenant_data_states',"
                "'tenant_retention_archives','tenant_lifecycle_events',"
                "'tenant_lifecycle_event_outbox','tenant_deletion_table_manifest',"
                "'tenant_deletion_trigger_manifest')"
            ).fetchone()[0]
            == 8
        )
    finally:
        connection.close()
    _healthy(database_url)

    repeated = apply_migrations(database_url)
    assert repeated.applied == [] and repeated.skipped[-1] == "0082"
    assert rollback_last_migration(database_url, expected_version="0082").rolled_back == "0082"
    assert _version(database_url) == "0081"
    connection = connect_database(database_url)
    try:
        assert tuple(
            connection.execute(
                "SELECT (SELECT COUNT(*) FROM rbac_permission_templates),"
                "(SELECT COUNT(*) FROM rbac_role_permission_templates),"
                "(SELECT COUNT(*) FROM sqlite_schema WHERE name='tenant_data_states')"
            ).fetchone()
        ) == (98, 254, 0)
    finally:
        connection.close()
    _healthy(database_url)

    assert apply_migrations(database_url).applied == ["0082"]
    assert _version(database_url) == "0082"
    _healthy(database_url)


def test_0082_down_guard_keeps_export_fact_atomic(tmp_path: Path) -> None:
    database_url = _url(tmp_path / "guard-0082.sqlite3")
    apply_migrations(database_url)
    seed = apply_seed_data(database_url)
    connection = connect_database(database_url)
    try:
        user_id = str(
            connection.execute(
                "SELECT user_id FROM users WHERE tenant_id=? AND role='admin' LIMIT 1",
                (seed.tenant_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO tenant_export_jobs(
              tenant_export_job_id,tenant_id,requested_by_user_id,request_id,trace_id,
              idempotency_key_hash,request_fingerprint,status,schema_version,snapshot_at,
              usage_snapshot_sequence,audit_snapshot_sequence,attempt_count,generation,
              expires_at,download_count,created_at,updated_at)
            VALUES(?,?,?,'req_prd089_guard','trace_prd089_guard',?,?,'pending',
              'metahub.tenant-export/v1','2030-01-01T00:00:00Z',0,0,0,0,
              '2030-01-02T00:00:00Z',0,'2030-01-01T00:00:00Z','2030-01-01T00:00:00Z')
            """,
            (
                "tex_" + "a" * 32,
                seed.tenant_id,
                user_id,
                "b" * 64,
                "c" * 64,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(DbError):
        rollback_last_migration(database_url, expected_version="0082")
    assert _version(database_url) == "0082"
    connection = connect_database(database_url)
    try:
        assert connection.execute("SELECT COUNT(*) FROM tenant_export_jobs").fetchone()[0] == 1
    finally:
        connection.close()
    _healthy(database_url)
