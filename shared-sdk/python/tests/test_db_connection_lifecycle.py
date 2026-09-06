"""Request-scoped SQLite handle reuse and physical-close lifecycle tests."""

from __future__ import annotations

import sqlite3

import anyio
import pytest
from metahub_shared.db.connection import (
    _close_idle_database_connections,
    connect_database,
    database_request_scope,
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
