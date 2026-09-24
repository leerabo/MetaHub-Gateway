"""A contended KEY request must leave the event loop free to release a writer."""

import asyncio
import sqlite3
import threading

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from metahub_control.auth.resource_transactions import ResourceWriteTransaction
from metahub_control.virtual_keys import routes
from metahub_shared.runtime.errors import ApiError


@pytest.mark.anyio
@pytest.mark.parametrize("method,path,service", [
    ("GET", "/virtual-keys", "list_virtual_keys"),
    ("DELETE", "/virtual-keys/test-key", "disable_virtual_key"),
])
async def test_key_database_wait_does_not_block_other_requests(tmp_path, monkeypatch, method, path, service):
    database_path = str(tmp_path / "contention.db")
    writer = sqlite3.connect(database_path)
    writer.execute("CREATE TABLE changes (value INTEGER)")
    writer.commit()
    writer.execute("BEGIN IMMEDIATE")
    entered = threading.Event()
    owner_thread = threading.get_ident()
    worker_threads = []

    def blocked_service(*args, **kwargs):
        worker_threads.append(threading.get_ident())
        connection = sqlite3.connect(database_path, timeout=0.5)
        try:
            entered.set()
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT INTO changes VALUES (1)")
            connection.commit()
            return JSONResponse({"completed": True})
        finally:
            connection.close()

    monkeypatch.setattr(routes, service, blocked_service)
    app = FastAPI()
    app.state.config = object()
    # Register the real handlers directly so their evaluated permission
    # dependencies are stable even with FastAPI's lazy included routers.
    for route in routes.router.routes:
        app.add_api_route(
            route.path, route.endpoint, methods=route.methods,
            response_model=route.response_model, status_code=route.status_code,
        )
        for dependency in app.routes[-1].dependant.dependencies:
            app.dependency_overrides[dependency.call] = lambda: object()

    @app.get("/responsive")
    async def responsive():
        return {"ok": True}

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            request = asyncio.create_task(client.request(method, path))
            try:
                entered_service = await asyncio.to_thread(entered.wait, 1)
                if not entered_service and request.done():
                    response = await request
                    pytest.fail(f"KEY service was not reached: HTTP {response.status_code}")
                assert entered_service
                assert not request.done(), "database wait blocked the event loop until timeout"
                assert (await client.get("/responsive")).status_code == 200
                writer.commit()
                response = await request
                assert response.status_code == 200
                assert response.json() == {"completed": True}
                assert worker_threads and owner_thread not in worker_threads
            finally:
                writer.rollback()
                await request
    finally:
        writer.close()


def test_contended_resource_transaction_returns_retryable_error_and_closes(tmp_path):
    database_path = str(tmp_path / "busy.db")
    writer = sqlite3.connect(database_path)
    connection = sqlite3.connect(database_path, timeout=0)
    try:
        writer.execute("CREATE TABLE changes (value INTEGER)")
        writer.commit()
        writer.execute("BEGIN IMMEDIATE")
        with pytest.raises(ApiError) as caught:
            with ResourceWriteTransaction(connection, object(), object(), "virtual_key.write"):
                pytest.fail("contended transaction must not perform business work")
        assert caught.value.code == "infra.service_unavailable"
        assert caught.value.http_status == 503
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")
    finally:
        connection.close()
        writer.rollback()
        writer.close()
