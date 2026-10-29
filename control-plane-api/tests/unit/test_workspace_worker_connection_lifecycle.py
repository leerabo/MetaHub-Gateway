"""Workspace workers reuse physical handles without retaining authorization state."""

import sqlite3
import threading
from contextlib import closing

import pytest
from metahub_control.workspaces import routes
from metahub_shared.db import connection as database


@pytest.fixture(autouse=True)
def clean_pool():
    database._close_idle_database_connections()
    yield
    database._close_idle_database_connections()


@pytest.mark.anyio
@pytest.mark.parametrize("fail", [False, True])
async def test_workspace_workers_release_fresh_leases_and_rollback_failure(tmp_path, fail):
    url = f"sqlite:///{(tmp_path / 'workspace-worker.sqlite3').as_posix()}"
    with closing(database.connect_database(url)) as connection:
        connection.execute("CREATE TABLE facts(value INTEGER NOT NULL)")
        connection.commit()
    caller_thread = threading.get_ident()
    handles = []

    def write(value):
        assert threading.get_ident() != caller_thread
        connection = database.connect_database(url)
        handles.append(connection)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT INTO facts VALUES(?)", (value,))
            if fail:
                raise RuntimeError("workspace worker failed")
            connection.commit()
            return value
        finally:
            connection.close()

    def read():
        assert threading.get_ident() != caller_thread
        connection = database.connect_database(url)
        handles.append(connection)
        try:
            assert not connection.in_transaction
            return connection.execute("SELECT count(*) FROM facts").fetchone()[0]
        finally:
            connection.close()

    if fail:
        with pytest.raises(RuntimeError, match="workspace worker failed"):
            await routes._run(write, 1)
    else:
        assert await routes._run(write, 1) == 1
    assert await routes._run(read) == (0 if fail else 1)
    assert handles[0] is not handles[1]
    assert handles[0]._connection is handles[1]._connection
    for handle in handles:
        with pytest.raises(sqlite3.ProgrammingError):
            handle.execute("SELECT 1")
    with closing(database.connect_database(url)) as outside:
        assert isinstance(outside, sqlite3.Connection)
