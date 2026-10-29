"""A denied commit aborts its transaction instead of retaining the write lock."""

from contextlib import nullcontext

import pytest
from metahub_shared.db.connection import (
    _close_idle_database_connections,
    _require_commit_authority,
    connect_database,
    database_commit_guard,
)


@pytest.mark.parametrize("method", ["commit", "sql", "context"])
@pytest.mark.parametrize("guard_error", [RuntimeError, KeyboardInterrupt])
def test_denied_commit_aborts_and_releases_sqlite_writer(tmp_path, method, guard_error):
    url = f"sqlite:///{(tmp_path / 'fenced.sqlite3').as_posix()}"
    connection = connect_database(url)
    connection.execute("CREATE TABLE probe(value INTEGER)")
    connection.commit()
    failure = guard_error("controlled denial")

    def reject(_connection):
        raise failure

    try:
        with database_commit_guard(reject), pytest.raises(guard_error) as caught:
            with connection if method == "context" else nullcontext():
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("INSERT INTO probe VALUES(1)")
                if method == "commit":
                    connection.commit()
                elif method == "sql":
                    connection.execute("COMMIT")
        assert caught.value is failure
        assert not connection.in_transaction
        observer = connect_database(url)
        try:
            observer.execute("PRAGMA busy_timeout=20")
            observer.execute("BEGIN IMMEDIATE")
            assert observer.execute("SELECT count(*) FROM probe").fetchone()[0] == 0
            observer.execute("INSERT INTO probe VALUES(2)")
            observer.commit()
        finally:
            observer.close()
    finally:
        connection.close()


def test_denied_commit_closes_if_rollback_fails_and_preserves_the_denial():
    closed = []
    failure = RuntimeError("controlled denial")

    class Connection:
        in_transaction = True

        def rollback(self):
            raise ValueError("controlled rollback failure")

        def close(self):
            closed.append(True)

    def reject(_connection):
        raise failure

    with database_commit_guard(reject), pytest.raises(RuntimeError) as caught:
        _require_commit_authority(Connection())
    assert caught.value is failure
    assert closed == [True]


def test_guarded_workers_reuse_closed_handles_but_never_share_active_connections(tmp_path):
    url = f"sqlite:///{(tmp_path / 'workers.sqlite3').as_posix()}"
    try:
        with database_commit_guard(lambda _: None):
            first = connect_database(url)
            first.execute("CREATE TABLE probe(value INTEGER)")
            first.commit()
            second = connect_database(url)
            assert first._connection is not second._connection
            second.close()
            physical = first._connection
            first.execute("BEGIN IMMEDIATE")
            first.execute("INSERT INTO probe VALUES(1)")
            first.execute("PRAGMA query_only=ON")
            first.close()
            first.close()
            with pytest.raises(Exception, match="closed"):
                first.execute("SELECT 1")

        def rejected(_connection):
            raise RuntimeError("replacement authority rejected")

        with database_commit_guard(rejected):
            replacement = connect_database(url)
            try:
                assert replacement._connection is physical
                assert not replacement.in_transaction
                assert replacement.execute("PRAGMA query_only").fetchone()[0] == 0
                assert replacement.execute("SELECT count(*) FROM probe").fetchone()[0] == 0
                replacement.execute("INSERT INTO probe VALUES(2)")
                with pytest.raises(RuntimeError, match="replacement authority rejected"):
                    replacement.commit()
                assert not replacement.in_transaction
            finally:
                replacement.close()
    finally:
        _close_idle_database_connections()
