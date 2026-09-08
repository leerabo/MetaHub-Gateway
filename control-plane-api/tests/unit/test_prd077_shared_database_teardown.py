"""A leased subsystem owns its handles, not the shared database journal."""

from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from metahub_control.admin_assistant import service


@pytest.mark.anyio
@pytest.mark.parametrize("active_reader", [False, True])
async def test_teardown_preserves_shared_wal_and_allows_runtime_restart(tmp_path, active_reader):
    database = tmp_path / "shared.sqlite3"
    observer = sqlite3.connect(database)
    observer.execute("PRAGMA journal_mode=WAL")
    observer.execute("CREATE TABLE facts(value INTEGER)")
    observer.execute("INSERT INTO facts VALUES(1)")
    observer.commit()
    app = FastAPI()
    app.state.config = SimpleNamespace(database_url=f"sqlite:///{database.as_posix()}")
    runtime = await service.initialize_admin_assistant_execution_runtime(app)
    handles = (runtime.read_connection, runtime.write_connection)
    runtime.write_connection.execute("PRAGMA busy_timeout=10")
    if active_reader:
        observer.execute("BEGIN")
        assert observer.execute("SELECT count(*) FROM facts").fetchone()[0] == 1
    runtime.write_connection.execute("INSERT INTO facts VALUES(2)")
    runtime.write_connection.commit()
    runtime.write_connection.execute("INSERT INTO facts VALUES(3)")
    try:
        await service.clear_admin_assistant_execution_runtime(app, asyncio.get_running_loop())
        assert runtime.closed
        for handle in handles:
            with pytest.raises(sqlite3.ProgrammingError):
                handle.execute("SELECT 1")
        if active_reader:
            assert observer.execute("SELECT count(*) FROM facts").fetchone()[0] == 1
            observer.rollback()
        assert observer.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert observer.execute("SELECT value FROM facts ORDER BY value").fetchall() == [(1,), (2,)]
        observer.execute("INSERT INTO facts VALUES(4)")
        observer.commit()
        replacement = await service.initialize_admin_assistant_execution_runtime(app)
        assert replacement is not runtime
        await service.clear_admin_assistant_execution_runtime(app, asyncio.get_running_loop())
        assert replacement.closed
    finally:
        observer.close()
        for handle in handles:
            try:
                handle.close()
            except sqlite3.Error:
                pass
