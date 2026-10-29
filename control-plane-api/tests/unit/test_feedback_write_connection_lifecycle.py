"""Feedback opt-in pooling preserves the existing dependency and worker boundaries."""

import asyncio
import json
import sqlite3
import threading
from contextlib import closing
from types import SimpleNamespace

import pytest
from metahub_control.feedback import routes
from metahub_shared.db import connection as database
from starlette.requests import Request
from starlette.responses import Response


@pytest.fixture(autouse=True)
def clean_pool():
    database._close_idle_database_connections()
    yield
    database._close_idle_database_connections()


@pytest.mark.parametrize("deny", [False, True])
def test_writer_guard_keeps_existing_authorization_and_independent_close(
    tmp_path, monkeypatch, deny,
):
    url = f"sqlite:///{(tmp_path / 'guard.sqlite3').as_posix()}"
    request, context = object(), object()
    handles = []

    def existing_guard(actual, authorization):
        assert actual is request and authorization == "test-authorization"
        with closing(database.connect_database(url)) as connection:
            handles.append(connection)
            connection.execute("SELECT 1").fetchone()
        if deny:
            raise RuntimeError("permission denied")
        return context

    monkeypatch.setattr(routes, "_FEEDBACK_WRITE_PERMISSION", existing_guard)
    for _ in range(2):
        if deny:
            with pytest.raises(RuntimeError, match="permission denied"):
                routes._require_feedback_writer(request, "test-authorization")
        else:
            assert routes._require_feedback_writer(request, "test-authorization") is context
    assert handles[0] is not handles[1]
    assert handles[0]._connection is handles[1]._connection
    for connection in handles:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
    with closing(database.connect_database(url)) as default:
        assert isinstance(default, sqlite3.Connection)


@pytest.mark.anyio
@pytest.mark.parametrize("fail", [False, True])
async def test_feedback_worker_pools_independent_handles_and_preserves_guard(
    tmp_path, monkeypatch, fail,
):
    config = SimpleNamespace(database_url=f"sqlite:///{(tmp_path / 'worker.sqlite3').as_posix()}")
    context = object()
    handles, guarded = [], []
    body = json.dumps({
        "request_id": "req_existing", "rating": 5, "accepted": True, "tags": [],
    }).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request({
        "type": "http", "method": "POST", "path": "/feedback",
        "headers": [(b"content-type", b"application/json"), (b"x-request-id", b"test-replay")],
        "app": SimpleNamespace(state=SimpleNamespace(config=config)),
        "state": {"request_id": "test-replay", "trace_id": "test-trace"},
    }, receive)

    def create(actual_config, actual_context, **kwargs):
        assert actual_config is config and actual_context is context
        assert kwargs["operation_request_id"] == "test-replay"
        for _ in range(2):
            with closing(database.connect_database(config.database_url)) as connection:
                handles.append(connection)
                connection.execute("SELECT 1").fetchone()
                connection.execute("BEGIN IMMEDIATE")
                connection.commit()
        if fail:
            raise RuntimeError("worker failed")
        return SimpleNamespace(idempotent_replay=True)

    monkeypatch.setattr(routes, "create_session_feedback", create)
    response = Response()
    with database.database_commit_guard(lambda connection: guarded.append(connection)):
        if fail:
            with pytest.raises(RuntimeError, match="worker failed"):
                await routes.post_feedback(request, response, context)
        else:
            result = await routes.post_feedback(request, response, context)
            assert result.idempotent_replay and response.status_code == 200
            assert response.headers["cache-control"] == "no-store, private"
    assert len(guarded) == 2
    assert handles[0] is not handles[1]
    assert handles[0]._connection is handles[1]._connection
    for connection in handles:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


@pytest.mark.anyio
async def test_cancelled_feedback_worker_returns_connection_only_after_exit(tmp_path, monkeypatch):
    config = SimpleNamespace(database_url=f"sqlite:///{(tmp_path / 'cancel.sqlite3').as_posix()}")
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    handles, outcomes = [], []

    async def receive():
        return {
            "type": "http.request", "more_body": False,
            "body": b'{"request_id":"req_existing","rating":5,"accepted":true,"tags":[]}',
        }

    request = Request({
        "type": "http", "method": "POST", "path": "/feedback",
        "headers": [(b"content-type", b"application/json")],
        "app": SimpleNamespace(state=SimpleNamespace(config=config)),
        "state": {"request_id": "test-cancel", "trace_id": "test-cancel"},
    }, receive)

    def create(*_args, **_kwargs):
        try:
            with closing(database.connect_database(config.database_url)) as connection:
                handles.append(connection)
                connection.execute("CREATE TABLE probe(value TEXT)")
                connection.commit()
                connection.execute("INSERT INTO probe VALUES ('uncommitted')")
                started.set()
                if not release.wait(5):
                    raise RuntimeError("worker was not released")
                outcomes.append(connection.execute("SELECT COUNT(*) FROM probe").fetchone()[0])
                return SimpleNamespace(idempotent_replay=True)
        finally:
            finished.set()

    monkeypatch.setattr(routes, "create_session_feedback", create)
    task = asyncio.create_task(routes.post_feedback(request, Response(), object()))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not finished.is_set()
        with database.database_connection_pool_scope():
            with closing(database.connect_database(config.database_url)) as observer:
                assert observer._connection is not handles[0]._connection
                assert observer.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 0
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 3)
        if not task.done():
            await task
    assert outcomes == [1]
    with pytest.raises(sqlite3.ProgrammingError):
        handles[0].execute("SELECT 1")
    with database.database_connection_pool_scope():
        with closing(database.connect_database(config.database_url)) as reused:
            assert reused._connection is handles[0]._connection
            assert reused.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 0
