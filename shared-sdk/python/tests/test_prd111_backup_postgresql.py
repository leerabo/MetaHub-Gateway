"""Native pg_dump/pg_restore in a newly created loopback-only cluster."""

from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
import test_prd111_backup_consistency as base
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    close_database_pools,
    rollback_last_migration,
)
from metahub_shared.infrastructure import backups as b
from psycopg import sql

config = base.config
store = base.store
ROOT = Path(__file__).resolve().parents[3]
PG = ROOT / ".tools/postgresql17/pgsql/bin"


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    executable = ".exe" if os.name == "nt" else ""
    if not (PG / ("initdb" + executable)).is_file():
        pytest.skip("Explicit local PostgreSQL tools are not installed.")
    work = tmp_path_factory.mktemp("s3-native-restore")
    data = work / "data"
    assert not data.exists()

    def run(*arguments):
        with (work / "lifecycle.log").open("ab") as output:
            subprocess.run(
                arguments,
                check=True,
                timeout=60,
                stdout=output,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    run(
        str(PG / ("initdb" + executable)),
        "-D",
        str(data),
        "-A",
        "trust",
        "-U",
        "s3_restore",
        "--no-locale",
        "-E",
        "UTF8",
        "--wal-segsize=1",
    )
    try:
        run(
            str(PG / ("pg_ctl" + executable)),
            "-D",
            str(data),
            "-l",
            str(work / "server.log"),
            "-w",
            "start",
            "-o",
            f"-h 127.0.0.1 -p {port} -c max_wal_size=128MB -c min_wal_size=16MB "
            "-c max_locks_per_transaction=1024",
        )
        yield f"postgresql://s3_restore@127.0.0.1:{port}/"
    finally:
        close_database_pools()
        if (data / "postmaster.pid").exists():
            run(str(PG / ("pg_ctl" + executable)), "-D", str(data), "-m", "fast", "-w", "stop")
        assert not (data / "postmaster.pid").exists()


@pytest.fixture
def native(config, cluster, monkeypatch):
    names = ["source_" + uuid4().hex, "target_" + uuid4().hex]
    with psycopg.connect(cluster + "postgres", autocommit=True) as connection:
        for name in names:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    source, target = (cluster + name for name in names)
    with psycopg.connect(source) as connection:
        connection.execute(base.SCHEMA)
    monkeypatch.setenv("PATH", str(PG) + os.pathsep + os.environ["PATH"])
    try:
        yield config.model_copy(
            update={"database_url": source, "backup_restore_database_url": target}
        )
    finally:
        close_database_pools()


def test_native_snapshot_dump_and_fingerprints_share_one_mvcc_snapshot(native, store, monkeypatch):
    real_run = b.subprocess.run
    calls = []

    def concurrent_write(arguments, **kwargs):
        calls.append(arguments)
        if arguments[0] == "pg_dump":
            assert any(argument.startswith("--snapshot=") for argument in arguments)
            with psycopg.connect(native.database_url) as writer:
                writer.execute("UPDATE config_snapshot_heads SET generation=9")
                writer.execute("UPDATE finops_ledger_entries SET amount_micros=999")
        return real_run(arguments, **kwargs)

    monkeypatch.setattr(b.subprocess, "run", concurrent_write)
    artifact = b.create_snapshot(native, backup_type="full")
    proof = base.restore(native, artifact)
    assert proof.consistency == artifact.consistency
    with psycopg.connect(native.backup_restore_database_url) as restored:
        assert restored.execute("SELECT generation FROM config_snapshot_heads").fetchone()[0] == 7
        assert (
            restored.execute("SELECT SUM(amount_micros) FROM finops_ledger_entries").fetchone()[0]
            == 400
        )
    assert len(calls) == 2
    assert "--clean" not in calls[-1]
    assert "--single-transaction" in calls[-1]


def test_native_restore_refuses_nonempty_target_without_invoking_restore(
    native, store, monkeypatch
):
    artifact = b.create_snapshot(native, backup_type="full")
    with psycopg.connect(native.backup_restore_database_url) as target:
        target.execute("CREATE TABLE existing_user_data(id INTEGER PRIMARY KEY)")
        target.execute("INSERT INTO existing_user_data VALUES(42)")
    monkeypatch.setattr(
        b.subprocess, "run", lambda *args, **kwargs: pytest.fail("Destructive restore")
    )
    with pytest.raises(b.BackupRuntimeError, match="^backup.restore_target_not_empty$"):
        base.restore(native, artifact)
    with psycopg.connect(native.backup_restore_database_url) as target:
        assert target.execute("SELECT id FROM existing_user_data").fetchone()[0] == 42


def test_native_restore_refuses_source_hostname_alias(native, store):
    artifact = b.create_snapshot(native, backup_type="full")
    target = native.database_url.replace("127.0.0.1", "localhost")
    unsafe = native.model_copy(update={"backup_restore_database_url": target})
    with pytest.raises(b.BackupRuntimeError, match="^backup.restore_target_unsafe$"):
        base.restore(unsafe, artifact)


def test_native_restore_target_lock_is_exclusive_and_released(native, store):
    artifact = b.create_snapshot(native, backup_type="full")
    with psycopg.connect(native.backup_restore_database_url, autocommit=True) as owner:
        owner.execute("SELECT pg_advisory_lock(111,100)")
        with pytest.raises(b.BackupRuntimeError, match="^backup.restore_target_busy$"):
            base.restore(native, artifact)
    assert base.restore(native, artifact).consistency == artifact.consistency
    with psycopg.connect(native.backup_restore_database_url) as check:
        assert check.execute("SELECT pg_try_advisory_lock(111,100)").fetchone()[0]


def test_native_failed_restore_keeps_target_empty_and_can_retry(native, store):
    with pytest.raises(b.BackupRuntimeError, match="^backup.restore_failed$"):
        b._verify_postgres_restore(native, b"invalid-dump-sensitive-value")
    with psycopg.connect(native.backup_restore_database_url) as target:
        assert (
            target.execute("SELECT COUNT(*) FROM pg_tables WHERE schemaname='public'").fetchone()[0]
            == 0
        )
    artifact = b.create_snapshot(native, backup_type="full")
    assert base.restore(native, artifact).consistency == artifact.consistency


def test_native_restore_detects_same_count_data_corruption(native, store, monkeypatch):
    artifact = b.create_snapshot(native, backup_type="full")
    real_run = b.subprocess.run

    def corrupt_target(arguments, **kwargs):
        result = real_run(arguments, **kwargs)
        if arguments[0] == "pg_restore":
            with psycopg.connect(native.backup_restore_database_url) as target:
                target.execute("UPDATE config_event_outbox SET payload='changed' ")
        return result

    monkeypatch.setattr(b.subprocess, "run", corrupt_target)
    with pytest.raises(b.BackupRuntimeError, match="^backup.restore_consistency_failed$"):
        base.restore(native, artifact)


def test_native_current_schema_real_backup_and_restore(native, cluster, store, monkeypatch):
    name = "fullschema_" + uuid4().hex
    with psycopg.connect(cluster + "postgres", autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    configured = native.model_copy(update={"database_url": cluster + name})
    result = apply_migrations(configured.database_url, migrations_dir=ROOT / "infra/migrations")
    assert result.applied[-1] == "0160"
    rollback_last_migration(
        configured.database_url, migrations_dir=ROOT / "infra/migrations", expected_version="0160"
    )
    with psycopg.connect(configured.database_url) as connection:
        assert (
            connection.execute(
                "SELECT proconfig FROM pg_proc "
                "WHERE oid='public.metahub_not_glob(text,text)'::regprocedure"
            ).fetchone()[0]
            is None
        )
        connection.execute(
            "ALTER FUNCTION public.metahub_not_glob(text,text) "
            "SET search_path TO pg_catalog, public"
        )
    apply_migrations(configured.database_url, migrations_dir=ROOT / "infra/migrations")
    rollback_last_migration(
        configured.database_url, migrations_dir=ROOT / "infra/migrations", expected_version="0160"
    )
    with psycopg.connect(configured.database_url) as connection:
        assert connection.execute(
            "SELECT proconfig FROM pg_proc "
            "WHERE oid='public.metahub_not_glob(text,text)'::regprocedure"
        ).fetchone()[0] == ["search_path=pg_catalog, public"]
    apply_migrations(configured.database_url, migrations_dir=ROOT / "infra/migrations")
    with psycopg.connect(configured.database_url) as connection:
        connection.execute("SET LOCAL search_path TO ''")
        assert connection.execute("SELECT public.metahub_not_glob('ok','*x*')").fetchone()[0]
        assert connection.execute("SELECT public.json_type('[1]','$.0')").fetchone()[0] == "integer"
    for key in ("ADMIN_PASSWORD", "VIRTUAL_KEY", "PROVIDER_API_KEY"):
        monkeypatch.setenv("METAHUB_SEED_" + key, "isolated-backup-seed-" + uuid4().hex)
    apply_seed_data(configured.database_url, seed_file=ROOT / "infra/seeds/default_seed.json")
    artifact = b.create_snapshot(configured, backup_type="full")
    proof = base.restore(configured, artifact)
    assert proof.schema_head == "0160"
    assert proof.consistency == artifact.consistency
    assert len(proof.consistency) > 50
    assert proof.tenant_owner_sample_count > 0


def test_native_restore_helper_down_preserves_backup_facts(native):
    script = (
        ROOT / "infra/migrations/postgresql/0160_backup_restore_function_search_path.down.sql"
    ).read_text()
    with psycopg.connect(native.database_url) as connection:
        connection.execute("CREATE TABLE public.backup_sets(id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO public.backup_sets VALUES(1)")
    with pytest.raises(
        psycopg.errors.RaiseException, match="backup_facts_prevent_restore_helper_down"
    ):
        with psycopg.connect(native.database_url) as connection:
            connection.execute(script)
    with psycopg.connect(native.database_url) as connection:
        assert connection.execute("SELECT id FROM public.backup_sets").fetchone()[0] == 1


def test_native_v2_restore_succeeds_with_source_database_offline(native, cluster, store):
    artifact = b.create_snapshot(native, backup_type="full")
    close_database_pools()
    source_name = native.database_url.rsplit("/", 1)[1]
    with psycopg.connect(cluster + "postgres", autocommit=True) as admin:
        admin.execute(
            sql.SQL("ALTER DATABASE {} WITH ALLOW_CONNECTIONS false").format(
                sql.Identifier(source_name)
            )
        )
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(native.database_url, connect_timeout=1)
    proof = base.restore(native, artifact)
    assert proof.consistency == artifact.consistency
