"""Usage responses stay private even when failures bypass business routes."""

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from metahub_shared.runtime.fastapi_app import create_service_app

USAGE_PATHS = (
    "/usage/summary",
    "/usage/records",
    "/usage/export.csv",
    "/usage/exports/job",
    "/usage/exports/job/download",
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _app():
    app = create_service_app("control-plane-api", "Usage cache boundary")

    async def response_probe(mode: str = "ok", limit: int = 20):
        if mode == "unauthorized":
            raise HTTPException(401)
        if mode == "forbidden":
            raise HTTPException(403)
        if mode == "missing":
            raise HTTPException(404)
        if mode == "crash":
            raise RuntimeError("usage cache probe")
        return JSONResponse({"limit": limit}, headers={"Cache-Control": "public, max-age=60"})

    for path in (*USAGE_PATHS, "/usage-other"):
        app.add_api_route(path, response_probe, methods=["GET"])
    return app


@pytest.mark.anyio
@pytest.mark.parametrize("path", USAGE_PATHS)
@pytest.mark.parametrize(
    ("query", "status"),
    [
        ({}, 200),
        ({"mode": "unauthorized"}, 401),
        ({"mode": "forbidden"}, 403),
        ({"mode": "missing"}, 404),
        ({"limit": "invalid"}, 422),
        ({"mode": "crash"}, 500),
    ],
)
async def test_usage_cache_boundary_covers_success_and_all_error_exits(path, query, status):
    async with AsyncClient(
        transport=ASGITransport(app=_app(), raise_app_exceptions=False),
        base_url="http://usage-test",
    ) as client:
        response = await client.get(path, params=query)
    assert response.status_code == status
    assert response.headers["Cache-Control"] == "no-store, private"
    assert response.headers["X-Request-ID"]
    assert response.headers["X-Trace-ID"]


@pytest.mark.anyio
async def test_usage_cache_boundary_covers_unknown_paths_without_matching_similar_prefix():
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://usage-test"
    ) as client:
        for path in ("/usage", "/usage/unknown"):
            response = await client.get(path)
            assert response.status_code == 404
            assert response.headers["Cache-Control"] == "no-store, private"
        other = await client.get("/usage-other")
    assert other.headers["Cache-Control"] == "public, max-age=60"
