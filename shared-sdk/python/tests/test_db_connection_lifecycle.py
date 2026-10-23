"""Request-scoped SQLite handle reuse and physical-close lifecycle tests."""

from __future__ import annotations

import sqlite3
import time
from contextlib import ExitStack
from contextvars import Context

import anyio
import pytest
from metahub_shared.db import connection as database_connections
from metahub_shared.db.connection import (
    _close_idle_database_connections,
    connect_database,
    database_commit_guard,
    database_request_scope,
    release_idle_request_connections,
)


def test_closed_connection_cannot_be_reused_by_a_prior_owner(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'lifecycle.db'}"

    first = connect_database(database_url)
    first.execute("CREATE TABLE probe (value TEXT NOT NULL)")
    first.commit()
    first.close()

    second = connect_database(database_url)
    second.execute("INSERT INTO probe(value) VALUES ('ok')")
    second.commit()
    assert second.execute("SELECT value FROM probe").fetchone()[0] == "ok"
    with pytest.raises(sqlite3.ProgrammingError):
        first.execute("SELECT value FROM probe")
    second.close()


def test_new_connection_restores_required_sqlite_pragmas(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'pragmas.db'}"

    connection = connect_database(database_url)
    connection.execute("CREATE TABLE probe (value TEXT NOT NULL)")
    connection.commit()
    connection.close()

    reopened = connect_database(database_url)
    assert reopened.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert reopened.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    reopened.close()


def test_deadline_connection_does_not_wait_for_wal_promotion_lock(tmp_path):
    database = str(tmp_path / "wal-promotion.db")
    seed = sqlite3.connect(database)
    seed.execute("CREATE TABLE probe (value TEXT)")
    seed.commit()
    seed.close()

    database_connections._SQLITE_WAL_DATABASES.discard(database)
    assert database_connections._SQLITE_WAL_LOCK.acquire()
    try:
        started = time.monotonic()
        with database_connections.database_operation_deadline(time.monotonic() + 0.020):
            connection = connect_database(f"sqlite:///{database}")
        elapsed = time.monotonic() - started
        connection.close()
    finally:
        database_connections._SQLITE_WAL_LOCK.release()

    assert elapsed < 0.20


def test_schema_initialization_finishes_without_holding_a_writer(tmp_path, monkeypatch):
    database = str(tmp_path / "schema-before-writer.db")
    observer = sqlite3.connect(database, timeout=0.05)
    observer.execute("CREATE TABLE probe(value INTEGER)")
    observer.commit()
    original = database_connections._SqliteConnection.execute
    checked = []

    def check_schema_read(connection, sql, parameters=()):
        if "sqlite_schema" in sql:
            # A slow cold-schema preparation must leave the business writer free.
            observer.execute("BEGIN IMMEDIATE")
            observer.execute("INSERT INTO probe VALUES(1)")
            observer.commit()
            checked.append(True)
        return original(connection, sql, parameters)

    monkeypatch.setattr(database_connections._SqliteConnection, "execute", check_schema_read)
    try:
        connection = connect_database(f"sqlite:///{database}")
        try:
            assert checked
            assert not connection.in_transaction
            connection.execute("BEGIN IMMEDIATE")
            assert connection.execute("SELECT count(*) FROM probe").fetchone()[0] == 1
            connection.rollback()
        finally:
            connection.close()
    finally:
        observer.close()


def test_schema_initialization_failure_releases_native_connection(tmp_path, monkeypatch):
    original = database_connections._SqliteConnection.execute
    opened = []

    def fail_schema_read(connection, sql, parameters=()):
        if "sqlite_schema" in sql:
            opened.append(connection)
            raise sqlite3.OperationalError("controlled schema read failure")
        return original(connection, sql, parameters)

    monkeypatch.setattr(database_connections._SqliteConnection, "execute", fail_schema_read)
    with pytest.raises(database_connections.DbError):
        connect_database(f"sqlite:///{tmp_path / 'schema-error.db'}")
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")


def test_request_scope_reuses_one_handle_until_the_request_ends(
    tmp_path,
    monkeypatch,
):
    database_url = f"sqlite:///{tmp_path / 'request.db'}"
    real_connect = sqlite3.connect
    opened = 0

    def counted_connect(*args, **kwargs):
        nonlocal opened
        opened += 1
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", counted_connect)
    with database_request_scope():
        authentication = connect_database(database_url)
        authentication.execute("CREATE TABLE probe (value TEXT NOT NULL)")
        authentication.commit()
        authentication.close()

        business = connect_database(database_url)
        business.execute("INSERT INTO probe(value) VALUES ('ok')")
        business.commit()
        assert business.execute("SELECT value FROM probe").fetchone()[0] == "ok"
        assert opened == 1

    with pytest.raises(sqlite3.ProgrammingError):
        business.execute("SELECT value FROM probe")


def test_request_scope_rolls_back_uncommitted_work(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'rollback.db'}"
    setup = connect_database(database_url)
    setup.execute("CREATE TABLE probe (value TEXT NOT NULL)")
    setup.commit()
    setup.close()

    with database_request_scope():
        connection = connect_database(database_url)
        connection.execute("INSERT INTO probe(value) VALUES ('not-committed')")
        connection.close()

    verification = connect_database(database_url)
    assert verification.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 0
    verification.close()


@pytest.mark.anyio
async def test_request_scope_reuses_handle_across_worker_context(
    tmp_path,
    monkeypatch,
):
    database_url = f"sqlite:///{tmp_path / 'worker.db'}"
    real_connect = sqlite3.connect
    opened = 0

    def counted_connect(*args, **kwargs):
        nonlocal opened
        opened += 1
        return real_connect(*args, **kwargs)

    def authenticate_in_worker():
        connection = connect_database(database_url)
        connection.execute("CREATE TABLE probe (value TEXT NOT NULL)")
        connection.commit()
        connection.close()
        return connection

    monkeypatch.setattr(sqlite3, "connect", counted_connect)
    with database_request_scope():
        authentication = await anyio.to_thread.run_sync(authenticate_in_worker)
        business = connect_database(database_url)
        business.execute("INSERT INTO probe(value) VALUES ('shared')")
        business.commit()
        assert business.execute("SELECT value FROM probe").fetchone()[0] == "shared"
        assert authentication is business
        assert opened == 1

    with pytest.raises(sqlite3.ProgrammingError):
        business.execute("SELECT value FROM probe")


def test_reusable_request_scope_keeps_schema_handle_warm_and_resets_connection_state(
    tmp_path,
    monkeypatch,
):
    database_url = f"sqlite:///{tmp_path / 'pooled-request.db'}"
    real_connect = sqlite3.connect
    opened = 0

    def counted_connect(*args, **kwargs):
        nonlocal opened
        opened += 1
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", counted_connect)
    try:
        with database_request_scope(reuse_connections=True):
            first = connect_database(database_url)
            first.execute("CREATE TABLE probe (value TEXT NOT NULL)")
            first.commit()
            first.execute("PRAGMA query_only=ON")

        with pytest.raises(sqlite3.ProgrammingError):
            first.execute("SELECT 1")

        with database_request_scope(reuse_connections=True):
            second = connect_database(database_url)
            assert second.execute("PRAGMA query_only").fetchone()[0] == 0
            second.execute("INSERT INTO probe(value) VALUES ('warm')")
            second.commit()
            assert second.execute("SELECT value FROM probe").fetchone()[0] == "warm"

        assert opened == 1
    finally:
        _close_idle_database_connections()


def test_absolute_sqlite_url_uses_the_requested_filesystem_path(tmp_path):
    database = tmp_path / "absolute-path.db"
    database_url = f"sqlite:///{database.as_posix()}"

    connection = connect_database(database_url)
    try:
        connection.execute("CREATE TABLE path_probe(value TEXT NOT NULL)")
        connection.commit()
    finally:
        connection.close()

    assert database.is_file()


def test_warm_queries_are_not_recompiled_after_every_clean_pool_loan(tmp_path):
    pool = database_connections._IdleConnectionPool()
    database = str(tmp_path / "statement-reuse.db")
    connection = pool.acquire(database)
    connection.execute("CREATE TABLE probe(value INTEGER)")
    connection.execute("INSERT INTO probe VALUES(1)")
    connection.commit()
    preparations = []

    def authorize(action, table, _column, *_rest):
        if action == sqlite3.SQLITE_READ and table == "probe":
            preparations.append(True)
        return sqlite3.SQLITE_OK

    connection.set_authorizer(authorize)
    try:
        for _ in range(20):
            assert connection.execute("SELECT value FROM probe").fetchall()[0][0] == 1
            pool.release(database, connection)
            connection = pool.acquire(database)
        assert len(preparations) == 1
        # Reusing compiled SQL still reads current data; it is not an authority cache.
        connection.execute("UPDATE probe SET value=2")
        connection.commit()
        assert connection.execute("SELECT value FROM probe").fetchall()[0][0] == 2
    finally:
        pool.release(database, connection)
        pool.close_idle()


@pytest.mark.parametrize("pragma,dirty,expected", [
    ("query_only", 1, 0), ("read_uncommitted", 1, 0),
    ("recursive_triggers", 1, 0), ("foreign_keys", 0, 1),
])
def test_dirty_pool_loan_restores_each_security_pragma(tmp_path, pragma, dirty, expected):
    pool = database_connections._IdleConnectionPool()
    database = str(tmp_path / "dirty-state.db")
    connection = pool.acquire(database)
    connection.execute(f"PRAGMA {pragma}={dirty}")
    pool.release(database, connection)
    restored = pool.acquire(database)
    try:
        assert restored.execute(f"PRAGMA {pragma}").fetchone()[0] == expected
    finally:
        pool.release(database, restored)
        pool.close_idle()


def test_idle_stage_release_reuses_handle_with_independent_lifetime(tmp_path):
    url = f"sqlite:///{tmp_path / 'stage-lifetime.db'}"
    worker_context = Context()
    worker_scope = ExitStack()
    try:
        try:
            with database_request_scope(reuse_connections=True):
                authentication = connect_database(url)
                authentication.execute("CREATE TABLE probe(value TEXT)")
                authentication.commit()
                physical = authentication._connection
                release_idle_request_connections()
                release_idle_request_connections()
                with pytest.raises(sqlite3.ProgrammingError):
                    authentication.execute("SELECT 1")
                worker_context.run(
                    worker_scope.enter_context, database_request_scope(reuse_connections=True),
                )
                worker = worker_context.run(connect_database, url)
                assert worker._connection is physical
            assert worker.execute("SELECT 1").fetchone()[0] == 1
        finally:
            worker_context.run(worker_scope.close)
        with pytest.raises(sqlite3.ProgrammingError):
            worker.execute("SELECT 1")
    finally:
        _close_idle_database_connections()


def test_idle_stage_release_leaves_active_transaction_untouched(tmp_path):
    url = f"sqlite:///{tmp_path / 'stage-active.db'}"
    release_idle_request_connections()
    try:
        with database_request_scope(reuse_connections=True):
            connection = connect_database(url)
            connection.execute("CREATE TABLE probe(value TEXT)")
            connection.commit()
            connection.execute("INSERT INTO probe VALUES ('uncommitted')")
            release_idle_request_connections()
            assert connect_database(url) is connection
            assert connection.in_transaction
            assert connection.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 1
        with database_request_scope(reuse_connections=True):
            assert connect_database(url).execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 0
    finally:
        _close_idle_database_connections()


@pytest.mark.parametrize("owner", ["request", "worker"])
def test_full_profile_burst_keeps_exclusive_owners_and_reuses_bounded_idle_handles(tmp_path, owner):
    url = f"sqlite:///{(tmp_path / 'burst.db').as_posix()}"
    batches = []
    try:
        for _ in range(3):
            with ExitStack() as scopes:
                if owner == "worker":
                    scopes.enter_context(database_commit_guard(lambda _: None))
                handles = []
                for _ in range(16):
                    if owner == "request":
                        scopes.enter_context(database_request_scope(reuse_connections=True))
                    connection = connect_database(url)
                    if owner == "worker":
                        scopes.callback(connection.close)
                    connection.execute("SELECT 1").fetchone()
                    handles.append(connection)
                physical = {handle._connection for handle in handles}
                assert len(physical) == 16
                batches.append(physical)
            for handle in handles:
                with pytest.raises(sqlite3.ProgrammingError):
                    handle.execute("SELECT 1")
        # Active callers have exclusive handles, but a burst must not retain
        # sixteen schema caches after it finishes. Only the bounded idle set
        # survives into the next burst; overflow is closed by its owner.
        capacity = min(16, database_connections._MAX_IDLE_CONNECTIONS_PER_DATABASE)
        for before, after in zip(batches, batches[1:]):
            assert len(before & after) == capacity
    finally:
        _close_idle_database_connections()


def test_idle_pool_bounds_overflow_and_closes_all_retained_handles(tmp_path):
    pool = database_connections._IdleConnectionPool()
    database = str(tmp_path / "bounded.db")
    capacity = database_connections._MAX_IDLE_CONNECTIONS_PER_DATABASE
    handles = [pool.acquire(database) for _ in range(capacity + 1)]
    try:
        assert len(set(handles)) == capacity + 1
        for handle in handles:
            pool.release(database, handle)
        assert len(pool._connections[database]) == capacity
        with pytest.raises(sqlite3.ProgrammingError):
            handles[-1].execute("SELECT 1")
    finally:
        pool.close_idle()
    assert not pool._connections
    for handle in handles:
        with pytest.raises(sqlite3.ProgrammingError):
            handle.execute("SELECT 1")


def test_idle_pool_evicts_oldest_database_without_closing_active_owner(tmp_path):
    pool = database_connections._IdleConnectionPool()
    first_database = str(tmp_path / "first.db")
    active = pool.acquire(first_database)
    idle = pool.acquire(first_database)
    try:
        pool.release(first_database, idle)
        for index in range(database_connections._MAX_IDLE_DATABASES):
            database = str(tmp_path / f"other-{index}.db")
            pool.release(database, pool.acquire(database))
        assert len(pool._connections) == database_connections._MAX_IDLE_DATABASES
        assert first_database not in pool._connections
        with pytest.raises(sqlite3.ProgrammingError):
            idle.execute("SELECT 1")
        assert active.execute("SELECT 1").fetchone()[0] == 1
    finally:
        active.close()
        pool.close_idle()
