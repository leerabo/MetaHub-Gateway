"""Request pooling preserves independent owners through streaming and cancellation."""

import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from metahub_gateway.database_runtime import GatewayDatabasePoolMiddleware
from metahub_shared.db import connection as database


@pytest.fixture(autouse=True)
def clean_pool():
    database._close_idle_database_connections()
    yield
    database._close_idle_database_connections()


async def noop(*_args):
    return {}


def test_full_gateway_installs_pool_boundary():
    source = Path(__file__).resolve().parents[2] / "src/metahub_gateway/app.py"
    assert "app.add_middleware(GatewayDatabasePoolMiddleware)" in source.read_text("utf-8")


@pytest.mark.anyio
async def test_stream_and_background_work_reuse_closed_handles_without_sharing_transactions(
    tmp_path,
):
    url = f"sqlite:///{(tmp_path / 'stream.sqlite3').as_posix()}"
    handles = []
    messages = []

    async def send(message):
        messages.append(message["type"])
        await asyncio.sleep(0)

    async def application(_scope, _receive, send):
        with closing(database.connect_database(url)) as setup:
            handles.append(setup._connection)
            setup.execute("CREATE TABLE probe(value TEXT)")
            setup.commit()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        for _ in range(2):
            with closing(database.connect_database(url)) as chunk:
                handles.append(chunk._connection)
                assert chunk.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 0
                chunk.execute("INSERT INTO probe VALUES ('not committed')")
            await send({"type": "http.response.body", "body": b"chunk", "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})
        with closing(database.connect_database(url)) as background:
            handles.append(background._connection)
            assert background.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 0

    await GatewayDatabasePoolMiddleware(application)({"type": "http"}, noop, send)
    assert len(handles) == 4 and all(handle is handles[0] for handle in handles)
    assert messages == ["http.response.start"] + ["http.response.body"] * 3
    with closing(database.connect_database(url)) as outside:
        assert isinstance(outside, sqlite3.Connection)


@pytest.mark.anyio
async def test_concurrent_requests_never_share_an_active_handle(tmp_path):
    url = f"sqlite:///{(tmp_path / 'concurrent.sqlite3').as_posix()}"
    entered, release = asyncio.Event(), asyncio.Event()
    handles = []

    async def application(_scope, _receive, _send):
        with closing(database.connect_database(url)) as owned:
            handles.append(owned._connection)
            if len(handles) == 2:
                entered.set()
            await release.wait()
            assert owned.execute("SELECT 1").fetchone()[0] == 1

    wrapper = GatewayDatabasePoolMiddleware(application)
    tasks = [asyncio.create_task(wrapper({"type": "http"}, noop, noop)) for _ in range(2)]
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert handles[0] is not handles[1]
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 1)


@pytest.mark.anyio
@pytest.mark.parametrize("outcome", ["error", "cancel"])
async def test_aborted_request_resets_scope_and_rolls_back(tmp_path, outcome):
    url = f"sqlite:///{(tmp_path / 'abort.sqlite3').as_posix()}"

    async def application(_scope, _receive, _send):
        with closing(database.connect_database(url)) as owned:
            owned.execute("CREATE TABLE probe(value TEXT)")
            owned.commit()
            owned.execute("INSERT INTO probe VALUES ('unfinished')")
            if outcome == "cancel":
                asyncio.current_task().cancel()
                await asyncio.sleep(0)
            raise RuntimeError("synthetic failure")

    async def run_request():
        expected = asyncio.CancelledError if outcome == "cancel" else RuntimeError
        with pytest.raises(expected):
            await GatewayDatabasePoolMiddleware(application)({"type": "http"}, noop, noop)
        with closing(database.connect_database(url)) as outside:
            assert isinstance(outside, sqlite3.Connection)
            assert outside.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 0

    await asyncio.create_task(run_request())


@pytest.mark.anyio
@pytest.mark.parametrize("scope_type", ["lifespan", "websocket"])
async def test_non_http_scope_remains_unpooled(tmp_path, scope_type):
    url = f"sqlite:///{(tmp_path / 'non-http.sqlite3').as_posix()}"
    called = False

    async def application(scope, _receive, _send):
        nonlocal called
        called = True
        assert scope["type"] == scope_type
        with closing(database.connect_database(url)) as connection:
            assert isinstance(connection, sqlite3.Connection)

    await GatewayDatabasePoolMiddleware(application)({"type": scope_type}, noop, noop)
    assert called
