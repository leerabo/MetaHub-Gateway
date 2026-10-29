"""Native per-execution databases and server-confirmed cancellation/restart fences."""

from __future__ import annotations

import subprocess
import threading
import time

import psycopg
import pytest
import test_prd111_backup_postgresql as native_tests
import test_prd111_restore_execution as base
from metahub_shared.infrastructure import restore_execution as e
from metahub_shared.infrastructure.restore_target import PostgresRestoreTarget
from psycopg import sql

config = native_tests.config
store = native_tests.store
cluster = native_tests.cluster
native = native_tests.native


def inspect_target(cluster, name):
    with psycopg.connect(cluster + "postgres", autocommit=True) as admin:
        return admin.execute(
            "SELECT datallowconn,(SELECT COUNT(*) FROM pg_stat_activity WHERE datid=d.oid) "
            "FROM pg_database d WHERE datname=%s",
            (name,),
        ).fetchone()


def test_native_durable_restore_uses_fresh_database_and_closes_admission(
    native, cluster, store, tmp_path
):
    request, artifact = base.prepare(native, store)
    executor = e.RestoreExecutor(tmp_path / "restore-state")
    result = executor.execute(native, request, postgres_admin_url=cluster + "postgres")
    assert result.status == "verified" and result.stop_confirmed
    assert result.proof.consistency == artifact.consistency
    name = "mh_restore_" + request.execution_id[4:]
    assert inspect_target(cluster, name) == (False, 0)
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(cluster + name, connect_timeout=1)
    assert executor.execute(native, request, postgres_admin_url=cluster + "postgres") == result
    # No restoration occurred into the old configured fixed target.
    with psycopg.connect(native.backup_restore_database_url) as configured_target:
        assert (
            configured_target.execute(
                "SELECT COUNT(*) FROM pg_tables WHERE schemaname='public'"
            ).fetchone()[0]
            == 0
        )


def test_native_restart_fences_orphan_backend_without_touching_source(
    native, cluster, store, tmp_path, monkeypatch
):
    request, _ = base.prepare(native, store)
    executor = e.RestoreExecutor(tmp_path / "restore-state")
    retained = []

    def interrupted_restore(config, **kwargs):
        connection = psycopg.connect(config.backup_restore_database_url, autocommit=True)
        connection.execute("CREATE TABLE partial_restore(id INTEGER PRIMARY KEY)")
        retained.append(connection)
        raise SystemExit("simulated-runner-crash")

    monkeypatch.setattr(e, "restore_and_verify", interrupted_restore)
    with psycopg.connect(native.database_url) as source:
        try:
            with pytest.raises(SystemExit):
                executor.execute(native, request, postgres_admin_url=cluster + "postgres")
            name = "mh_restore_" + request.execution_id[4:]
            assert inspect_target(cluster, name) == (True, 1)
            monkeypatch.setattr(
                e,
                "restore_and_verify",
                lambda *args, **kwargs: pytest.fail("Interrupted restore replayed"),
            )
            result = e.RestoreExecutor(executor.directory).execute(
                native, request, postgres_admin_url=cluster + "postgres"
            )
            assert result.status == "interrupted" and result.stop_confirmed and result.proof is None
            assert inspect_target(cluster, name) == (False, 0)
            assert source.execute("SELECT COUNT(*) FROM finops_ledger_entries").fetchone()[0] == 2
            with pytest.raises(psycopg.OperationalError):
                retained[0].execute("SELECT 1")
        finally:
            for connection in retained:
                connection.close()


def test_native_uncertain_stop_stays_pending_until_server_confirms(
    native, cluster, store, tmp_path, monkeypatch
):
    request, _ = base.prepare(native, store)
    executor = e.RestoreExecutor(tmp_path / "restore-state")
    real_fence = PostgresRestoreTarget.fence
    monkeypatch.setattr(PostgresRestoreTarget, "fence", lambda self: False)
    result = executor.execute(native, request, postgres_admin_url=cluster + "postgres")
    assert result.status == "stop_pending" and not result.stop_confirmed and result.proof is None
    monkeypatch.setattr(PostgresRestoreTarget, "fence", real_fence)
    monkeypatch.setattr(
        e, "restore_and_verify", lambda *args, **kwargs: pytest.fail("Restore replay")
    )
    result = executor.execute(native, request, postgres_admin_url=cluster + "postgres")
    assert result.status == "interrupted" and result.stop_confirmed
    assert inspect_target(cluster, "mh_restore_" + request.execution_id[4:]) == (False, 0)


def test_native_unowned_database_is_neither_restored_nor_fenced(native, cluster, store, tmp_path):
    request, _ = base.prepare(native, store)
    name = "mh_restore_" + request.execution_id[4:]
    with psycopg.connect(cluster + "postgres", autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    with psycopg.connect(cluster + name, autocommit=True) as existing:
        existing.execute("CREATE TABLE retained_data(id INTEGER PRIMARY KEY)")
        existing.execute("INSERT INTO retained_data VALUES(42)")
        result = e.RestoreExecutor(tmp_path / "restore-state").execute(
            native, request, postgres_admin_url=cluster + "postgres"
        )
        assert result.status == "stop_pending" and not result.stop_confirmed
        assert existing.execute("SELECT id FROM retained_data").fetchone()[0] == 42
        assert inspect_target(cluster, name) == (True, 1)


def test_native_server_identity_change_cannot_confirm_old_execution_stopped(
    native, cluster, store, tmp_path, monkeypatch
):
    request, _ = base.prepare(native, store)
    executor = e.RestoreExecutor(tmp_path / "restore-state")
    real_fence = PostgresRestoreTarget.fence
    monkeypatch.setattr(PostgresRestoreTarget, "fence", lambda self: False)
    assert (
        executor.execute(native, request, postgres_admin_url=cluster + "postgres").status
        == "stop_pending"
    )
    monkeypatch.setattr(PostgresRestoreTarget, "fence", real_fence)
    real_enter = PostgresRestoreTarget.__enter__

    def different_server(self):
        result = real_enter(self)
        self.server_sha256 = "0" * 64
        return result

    with monkeypatch.context() as altered:
        altered.setattr(PostgresRestoreTarget, "__enter__", different_server)
        result = executor.execute(native, request, postgres_admin_url=cluster + "postgres")
        assert result.status == "stop_pending" and not result.stop_confirmed
        assert inspect_target(cluster, "mh_restore_" + request.execution_id[4:])[0] is True
    assert (
        executor.execute(native, request, postgres_admin_url=cluster + "postgres").status
        == "interrupted"
    )


def test_native_cancel_during_pg_restore_stops_child_and_database_backend(
    native, cluster, store, tmp_path, monkeypatch
):
    with psycopg.connect(native.database_url) as source:
        # Validated checks are restored before COPY; NOT VALID checks are post-data.
        source.execute("""
            CREATE FUNCTION restore_delay() RETURNS boolean LANGUAGE plpgsql AS
              $$ BEGIN
                IF current_database() ~ '^mh_restore_[0-9a-f]{32}$' THEN
                  PERFORM pg_sleep(20);
                END IF;
                RETURN true;
              END $$;
            ALTER TABLE finops_ledger_entries ADD CONSTRAINT restore_delay_check
              CHECK(restore_delay());
        """)
    request, _ = base.prepare(native, store)
    executor = e.RestoreExecutor(tmp_path / "restore-state")
    children, errors = [], []
    observed = threading.Event()
    real_popen = subprocess.Popen

    def track(arguments, **kwargs):
        child = real_popen(arguments, **kwargs)
        if arguments[0] == "pg_restore":
            children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", track)

    def cancel_active_copy():
        try:
            with psycopg.connect(cluster + "postgres", autocommit=True) as observer:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    active = observer.execute(
                        "SELECT COUNT(*) FROM pg_stat_activity WHERE datname=%s "
                        "AND state='active' AND wait_event='PgSleep' "
                        "AND query LIKE 'COPY %%finops_ledger_entries%%'",
                        ("mh_restore_" + request.execution_id[4:],),
                    ).fetchone()[0]
                    if active:
                        observed.set()
                        executor.cancel(request)
                        return
                    time.sleep(0.02)
                errors.append("no-active-restore")
        except Exception as exc:
            errors.append(type(exc).__name__)

    canceller = threading.Thread(target=cancel_active_copy)
    canceller.start()
    try:
        result = executor.execute(native, request, postgres_admin_url=cluster + "postgres")
    finally:
        canceller.join(timeout=20)
    assert not canceller.is_alive() and not errors and observed.is_set()
    assert result.status == "cancelled" and result.stop_confirmed and result.proof is None
    assert len(children) == 1 and children[0].poll() is not None
    assert inspect_target(cluster, "mh_restore_" + request.execution_id[4:]) == (False, 0)


def test_native_registration_creates_no_database_until_dispatch(native, cluster, store, tmp_path):
    request, _ = base.prepare(native, store)
    executor = e.RestoreExecutor(tmp_path / "registered")
    assert executor.register(native, request, postgres_admin_url=cluster + "postgres") is None
    assert inspect_target(cluster, "mh_restore_" + request.execution_id[4:]) is None
    restarted = e.RestoreExecutor(executor.directory)
    result = restarted.execute(native, request, postgres_admin_url=cluster + "postgres")
    assert result.status == "verified"
    assert inspect_target(cluster, "mh_restore_" + request.execution_id[4:]) == (False, 0)


def test_native_cancelled_registration_never_connects_to_target(
    native, cluster, store, tmp_path, monkeypatch
):
    request, _ = base.prepare(native, store)
    executor = e.RestoreExecutor(tmp_path / "registered")
    unreachable = "postgresql://s3_restore@127.0.0.1:1/postgres"
    monkeypatch.setattr(
        PostgresRestoreTarget, "__enter__", lambda *_: pytest.fail("Cancelled target connection")
    )
    executor.register(native, request, postgres_admin_url=unreachable, cancel_requested=True)
    restarted = e.RestoreExecutor(executor.directory)
    result = restarted.execute(native, request, postgres_admin_url=unreachable)
    assert result.status == "cancelled" and result.stop_confirmed and result.proof is None
    assert inspect_target(cluster, "mh_restore_" + request.execution_id[4:]) is None
