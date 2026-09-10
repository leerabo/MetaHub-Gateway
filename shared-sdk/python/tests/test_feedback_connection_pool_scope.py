"""Opt-in pooling retains independent write connection ownership."""

import sqlite3
from contextlib import closing

import pytest
from metahub_shared.db import connection as database


@pytest.fixture(autouse=True)
def clean_pool():
    database._close_idle_database_connections()
    yield
    database._close_idle_database_connections()


def test_independent_pool_reuses_only_closed_handles_and_resets_state(tmp_path):
    url = f"sqlite:///{(tmp_path / 'independent.sqlite3').as_posix()}"
    with database.database_connection_pool_scope():
        first = database.connect_database(url)
        second = database.connect_database(url)
        assert first is not second
        assert first._connection is not second._connection
        physical = first._connection
        first.execute("CREATE TABLE probe(value TEXT)")
        first.commit()
        first.execute("INSERT INTO probe VALUES ('uncommitted')")
        first.execute("PRAGMA query_only=ON")
        first.close()
        first.close()
        with pytest.raises(sqlite3.ProgrammingError):
            first.execute("SELECT 1")
        third = database.connect_database(url)
        assert third is not first and third._connection is physical
        assert third.execute("PRAGMA query_only").fetchone()[0] == 0
        assert third.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 0
        assert second.execute("SELECT 1").fetchone()[0] == 1
        second.close()
        third.close()


def test_pool_scope_does_not_take_over_outer_transaction_or_escape(tmp_path):
    url = f"sqlite:///{(tmp_path / 'outer.sqlite3').as_posix()}"
    with database.database_request_scope(reuse_connections=True):
        outer = database.connect_database(url)
        outer.execute("CREATE TABLE probe(value TEXT)")
        outer.commit()
        outer.execute("INSERT INTO probe VALUES ('outer')")
        with pytest.raises(RuntimeError), database.database_connection_pool_scope():
            with closing(database.connect_database(url)) as independent:
                assert independent._connection is not outer._connection
                assert independent.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 0
            raise RuntimeError("test boundary")
        assert database.connect_database(url) is outer
        assert outer.in_transaction
        outer.rollback()
    with closing(database.connect_database(url)) as unpooled:
        assert isinstance(unpooled, sqlite3.Connection)


def test_independent_pool_keeps_commit_guard_and_discards_rejected_work(tmp_path):
    url = f"sqlite:///{(tmp_path / 'guard.sqlite3').as_posix()}"
    with closing(database.connect_database(url)) as setup:
        setup.execute("CREATE TABLE probe(value TEXT)")
        setup.commit()

    def deny(_connection):
        raise RuntimeError("revoked")

    with database.database_connection_pool_scope():
        with (
            database.database_commit_guard(deny),
            closing(database.connect_database(url)) as writer,
        ):
            writer.execute("INSERT INTO probe VALUES ('rejected')")
            with pytest.raises(RuntimeError, match="revoked"):
                writer.commit()
        with closing(database.connect_database(url)) as verification:
            assert verification.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 0


def test_pool_scope_preserves_independent_in_memory_databases():
    with database.database_connection_pool_scope():
        with closing(database.connect_database("sqlite:///:memory:")) as first:
            with closing(database.connect_database("sqlite:///:memory:")) as second:
                first.execute("CREATE TABLE probe(value TEXT)")
                assert isinstance(first, sqlite3.Connection)
                result = second.execute("SELECT name FROM sqlite_schema WHERE name='probe'")
                assert result.fetchone() is None
