"""Focused DR binding contracts on real engines, with a minimal dependency schema."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
import test_prd111_backup_postgresql as postgres
from metahub_control.disaster_recovery import recovery_binding as binding
from metahub_control.disaster_recovery import runtime, service
from metahub_shared.db import apply_migrations, connect_database, rollback_last_migration
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.errors import ApiError
from psycopg import sql

config = postgres.config
cluster = postgres.cluster
ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 9, 10, 10, tzinfo=UTC)
RUN = "drr_" + "1" * 32
JOB = "pjob_" + "1" * 32
PLAN = "a" * 64


@contextmanager
def rejected(code="dr.recovery_point_unavailable"):
    with pytest.raises(ApiError) as error:
        yield
    assert error.value.code == code


DEPENDENCIES = """
CREATE TABLE platform_regions(region_id TEXT PRIMARY KEY);
INSERT INTO platform_regions VALUES('north'),('east');
CREATE TABLE platform_jobs(platform_job_id TEXT PRIMARY KEY,type_code TEXT,status TEXT);
INSERT INTO platform_jobs VALUES('pjob_11111111111111111111111111111111','dr.run','queued');
CREATE TABLE disaster_recovery_runs(
  disaster_recovery_run_id TEXT PRIMARY KEY,platform_job_id TEXT,status TEXT,
  plan_manifest_sha256 TEXT,source_region_id TEXT,target_region_id TEXT,
  expected_epoch INTEGER,fencing_epoch INTEGER,started_at TEXT,rpo_seconds INTEGER);
CREATE TABLE backup_sets(
  backup_id TEXT PRIMARY KEY,scope_kind TEXT,workspace_id TEXT,tenant_id TEXT,
  backup_type TEXT,source_backend TEXT,status TEXT,schema_head TEXT,
  manifest_sha256 TEXT,content_sha256 TEXT,object_key TEXT,kms_key_version TEXT,
  source_watermark TEXT,created_at TEXT,completed_at TEXT);
CREATE TABLE backup_restore_verifications(
  backup_id TEXT PRIMARY KEY REFERENCES backup_sets(backup_id),status TEXT,
  integrity_status TEXT,manifest_sha256 TEXT,schema_head TEXT);
CREATE TABLE recovery_points(
  recovery_point_id TEXT PRIMARY KEY,backup_id TEXT REFERENCES backup_sets(backup_id),
  scope_kind TEXT,workspace_id TEXT,tenant_id TEXT,source_watermark_sha256 TEXT,
  manifest_sha256 TEXT,kms_key_version_sha256 TEXT,schema_head TEXT,integrity_status TEXT,
  source_created_at TEXT,created_at TEXT);
"""


@pytest.fixture(scope="module")
def native_contract_connection(cluster):
    name = "dr_binding_" + uuid4().hex
    with psycopg.connect(cluster + "postgres", connect_timeout=5, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    connection = connect_database(cluster + name + "?connect_timeout=5")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture(params=["sqlite", "postgresql"])
def database(request, config):
    is_pg = request.param == "postgresql"
    connection = (
        request.getfixturevalue("native_contract_connection")
        if is_pg
        else connect_database(config.database_url)
    )
    if is_pg:
        # This database was created above for this module only; no retained/user DB is reset.
        connection.rollback()
        connection.execute("DROP SCHEMA public CASCADE")
        connection.execute("CREATE SCHEMA public")
        connection.commit()
        connection.executescript(postgres.base.SCHEMA)
        # Fixture timestamp helpers only; full-schema compatibility is tested separately.
        connection.execute("""CREATE FUNCTION unixepoch(text) RETURNS bigint LANGUAGE SQL AS
          $$ SELECT floor(extract(epoch FROM $1::timestamptz))::bigint $$""")
        connection.execute("""CREATE FUNCTION julianday(text)
          RETURNS double precision LANGUAGE SQL AS
          $$ SELECT extract(epoch FROM $1::timestamptz)::double precision / 86400 $$""")
    connection.commit()
    connection.executescript(DEPENDENCIES)
    head = "0161" if is_pg else "0153"
    connection.execute("UPDATE schema_migrations SET version=?", (head,))
    connection.commit()
    script = (
        ROOT
        / "infra/migrations"
        / (
            "postgresql/0161_dr_run_recovery_binding.sql"
            if is_pg
            else "0153_dr_run_recovery_binding.sql"
        )
    )
    if is_pg:
        connection.execute(script.read_text(encoding="utf-8"))
    else:
        connection.executescript(script.read_text(encoding="utf-8"))
    connection.execute(
        "INSERT INTO disaster_recovery_runs VALUES(?,NULL,'planned',?,'north','east',0,1,NULL,900)",
        (RUN, PLAN),
    )
    connection.commit()
    try:
        yield connection
    finally:
        connection.rollback()
        if not is_pg:
            connection.close()


def backup(connection, *, marker="1", age=30, **updates):
    value = {
        "backup_id": "bkp_" + marker * 32,
        "scope_kind": "platform",
        "workspace_id": None,
        "tenant_id": None,
        "backup_type": "full",
        "source_backend": getattr(connection, "backend", "sqlite"),
        "status": "ready",
        "schema_head": binding.schema_head(connection),
        "manifest_sha256": marker * 64,
        "content_sha256": "c" * 64,
        "object_key": "backups/isolated-object-" + marker,
        "kms_key_version": "isolated-kms-version",
        "source_watermark": "snapshot:1",
        "created_at": (NOW - timedelta(seconds=age)).isoformat(),
        "completed_at": NOW.isoformat(),
        **updates,
    }
    connection.execute(
        f"INSERT INTO backup_sets({','.join(value)}) VALUES({','.join('?' for _ in value)})",
        tuple(value.values()),
    )
    connection.execute(
        "INSERT INTO backup_restore_verifications VALUES(?,'passed','ok',?,?)",
        (
            value["backup_id"],
            value["manifest_sha256"],
            value["schema_head"],
        ),
    )
    return value


def run(connection):
    return connection.execute(
        "SELECT * FROM disaster_recovery_runs WHERE disaster_recovery_run_id=?", (RUN,)
    ).fetchone()


def bind(connection):
    recovery = service._persist_latest_recovery_point(connection, {"rpo_seconds": 900}, NOW)
    binding.bind_recovery_in_transaction(
        connection, run=run(connection), recovery=recovery, job_id=JOB, now=NOW
    )
    connection.execute(
        "UPDATE disaster_recovery_runs SET status='approved',platform_job_id=?", (JOB,)
    )
    return recovery


def test_binding_keeps_original_recovery_when_newer_backup_appears(database):
    first = backup(database)
    recovery = bind(database)
    backup(database, marker="2", age=5)
    latest = service._persist_latest_recovery_point(database, {"rpo_seconds": 900}, NOW)
    assert latest["backup_id"] != first["backup_id"]
    restored = binding.bound_recovery_in_transaction(database, run=run(database), job_id=JOB)
    assert restored["recovery_point_id"] == recovery["recovery_point_id"]
    assert restored["backup_id"] == first["backup_id"]


@pytest.mark.parametrize(
    "change",
    [
        {"backup_type": "incremental"},
        {"scope_kind": "workspace", "workspace_id": "wsp_test", "tenant_id": "ten_test"},
        {"schema_head": "0103"},
        {"source_backend": "other"},
        {"age": 901},
        {"age": -1},
    ],
)
def test_platform_selection_rejects_ineligible_backups(database, change):
    backup(database, **change)
    with rejected():
        service._persist_latest_recovery_point(database, {"rpo_seconds": 900}, NOW)


def test_selection_skips_newer_workspace_backup(database):
    selected = backup(database)
    backup(
        database,
        marker="2",
        age=1,
        scope_kind="workspace",
        workspace_id="wsp_test",
        tenant_id="ten_test",
    )
    assert binding.latest_platform_backup(database)["backup_id"] == selected["backup_id"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("manifest_sha256", "d" * 64),
        ("content_sha256", "d" * 64),
        ("object_key", "backups/another-object"),
        ("kms_key_version", "another-version"),
        ("source_watermark", "snapshot:2"),
        ("schema_head", "0103"),
        ("created_at", "2026-09-10T09:59:45+00:00"),
        ("status", "failed"),
    ],
)
def test_bound_backup_metadata_drift_fails_closed(database, field, value):
    backup(database)
    bind(database)
    database.execute(f"UPDATE backup_sets SET {field}=?", (value,))
    with rejected():
        binding.bound_recovery_in_transaction(database, run=run(database))


def test_actual_schema_drift_and_failed_migration_reject_execution(database):
    backup(database)
    bind(database)
    database.execute("INSERT INTO schema_migrations VALUES('0999','applied')")
    with rejected():
        binding.bound_recovery_in_transaction(database, run=run(database))
    database.execute("UPDATE schema_migrations SET status='failed' WHERE version='0999'")
    with rejected():
        binding.bound_recovery_in_transaction(database, run=run(database))


def test_old_completed_timestamp_cannot_refresh_stale_data(database):
    backup(database, age=1800)
    assert service._backup_age(binding.latest_platform_backup(database), NOW) == 1800
    with rejected():
        bind(database)


def test_job_owner_must_match_before_any_execution(database):
    backup(database)
    bind(database)
    with rejected("dr.run_conflict"):
        binding.bound_recovery_in_transaction(database, run=run(database), job_id="other-job")


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE dr_run_recovery_bindings SET content_sha256='" + "d" * 64 + "'",
        "DELETE FROM dr_run_recovery_bindings",
        "UPDATE disaster_recovery_runs SET platform_job_id=NULL",
    ],
)
def test_database_blocks_binding_mutation_and_job_replacement(database, statement):
    backup(database)
    bind(database)
    database.commit()
    with pytest.raises((sqlite3.DatabaseError, DbError)):
        database.execute(statement)
    database.rollback()
    assert binding.bound_recovery_in_transaction(database, run=run(database))["backup_id"]


def test_unbound_historical_run_cannot_advance_but_can_fail(database):
    with pytest.raises((sqlite3.DatabaseError, DbError)):
        database.execute(
            "UPDATE disaster_recovery_runs SET status='approved',platform_job_id=?", (JOB,)
        )
    database.rollback()
    database.execute("UPDATE disaster_recovery_runs SET status='failed'")
    assert run(database)["status"] == "failed"


def test_binding_and_approval_rollback_together(database):
    backup(database)
    database.commit()
    bind(database)
    database.rollback()
    assert database.execute("SELECT COUNT(*) FROM dr_run_recovery_bindings").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM recovery_points").fetchone()[0] == 0
    assert run(database)["status"] == "planned"


def test_missing_binding_never_falls_back_to_global_latest(database):
    backup(database)
    service._persist_latest_recovery_point(database, {"rpo_seconds": 900}, NOW)
    with rejected():
        binding.bound_recovery_in_transaction(database, run=run(database))


def test_restore_duration_does_not_change_rpo_snapshot_age(database):
    backup(database, age=899)
    bind(database)
    database.execute("UPDATE disaster_recovery_runs SET started_at=?", (NOW.isoformat(),))
    assert binding.bound_recovery_in_transaction(database, run=run(database))
    delayed = dict(run(database))
    delayed["started_at"] = (NOW + timedelta(seconds=2)).isoformat()
    with rejected():
        binding.bound_recovery_in_transaction(database, run=delayed)
    database.commit()
    with pytest.raises((sqlite3.DatabaseError, DbError)):
        database.execute("UPDATE disaster_recovery_runs SET started_at=NULL")
    database.rollback()


def test_runtime_refuses_another_jobs_effect_including_failure(database):
    backup(database)
    bind(database)
    with rejected("dr.run_conflict"):
        runtime._owned_effect(database, "other-job", RUN, lambda _: pytest.fail("wrong owner"))
    assert runtime._owned_effect(database, JOB, RUN, lambda _: "owned") == "owned"


@pytest.mark.parametrize(
    "field,value",
    [
        ("manifest_sha256", "f" * 64),
        ("content_sha256", "f" * 64),
        ("schema_head", "0103"),
        ("expected_epoch", 2),
        ("fencing_epoch", 3),
        ("source_region_id", "east"),
        ("plan_manifest_sha256", "f" * 64),
        ("platform_job_id", "unknown-job"),
    ],
)
def test_database_rejects_forged_binding_fields(database, field, value):
    backup(database)
    bind(database)
    source = dict(database.execute("SELECT * FROM dr_run_recovery_bindings").fetchone())
    database.rollback()
    backup(database)
    service._persist_latest_recovery_point(database, {"rpo_seconds": 900}, NOW)
    source[field] = value
    with pytest.raises((sqlite3.DatabaseError, DbError)):
        database.execute(
            f"INSERT INTO dr_run_recovery_bindings({','.join(source)}) "
            f"VALUES({','.join('?' for _ in source)})",
            tuple(source.values()),
        )
    database.rollback()


def test_old_recovery_point_with_completion_timestamp_is_not_silently_rebound(database):
    backup(database)
    recovery = service._persist_latest_recovery_point(database, {"rpo_seconds": 900}, NOW)
    database.execute("UPDATE recovery_points SET source_created_at=?", (NOW.isoformat(),))
    with rejected():
        binding.bind_recovery_in_transaction(
            database,
            run=run(database),
            recovery=database.execute(
                "SELECT * FROM recovery_points WHERE recovery_point_id=?",
                (recovery["recovery_point_id"],),
            ).fetchone(),
            job_id=JOB,
            now=NOW,
        )


def test_new_migration_down_up_and_fact_preservation(database):
    is_pg = getattr(database, "backend", "sqlite") == "postgresql"
    path = (
        ROOT
        / "infra/migrations"
        / ("postgresql/0161_dr_run_recovery_binding" if is_pg else "0153_dr_run_recovery_binding")
    )

    def execute(suffix):
        sql = Path(str(path) + suffix).read_text(encoding="utf-8")
        database.execute(sql) if is_pg else database.executescript(sql)

    database.commit()
    execute(".down.sql")
    execute(".sql")
    backup(database)
    bind(database)
    database.commit()
    with pytest.raises((sqlite3.DatabaseError, DbError)):
        execute(".down.sql")
    database.rollback()
    assert binding.bound_recovery_in_transaction(database, run=run(database))


def test_source_age_rounds_up_at_rpo_boundary():
    assert binding.source_age(NOW - timedelta(seconds=900), NOW) == 900
    assert binding.source_age(NOW - timedelta(seconds=900.001), NOW) == 901


@pytest.mark.parametrize("offset", [-30.001, 0.001])
def test_subsecond_invalid_completion_time_is_rejected(database, offset):
    backup(database, age=30, completed_at=(NOW + timedelta(seconds=offset)).isoformat())
    with rejected():
        bind(database)


def test_full_sqlite_binding_migration_roundtrip(config, tmp_path):
    url = f"sqlite:///{(tmp_path / 'full-schema.db').as_posix()}"
    migrations = ROOT / "infra/migrations"
    assert apply_migrations(url, migrations_dir=migrations).applied[-1] == "0153"
    assert not apply_migrations(url, migrations_dir=migrations).applied
    rollback_last_migration(url, migrations_dir=migrations, expected_version="0153")
    assert apply_migrations(url, migrations_dir=migrations).applied == ["0153"]
    connection = connect_database(url)
    try:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert binding.schema_head(connection) == "0153"
    finally:
        connection.close()
