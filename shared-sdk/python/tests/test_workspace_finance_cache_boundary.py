"""Budget and internal ledger responses remain private across framework errors."""

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from metahub_shared.runtime.fastapi_app import create_service_app

PATHS = (
    "/budgets",
    "/budgets/rule/mode",
    "/budget-status",
    "/finops/ledger",
    "/finops/adjustments",
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _app():
    app = create_service_app("control-plane-api", "Finance response boundary")

    async def probe(mode: str = "ok", limit: int = 20):
        if mode == "crash":
            raise RuntimeError("finance cache probe")
        if mode != "ok":
            raise HTTPException(int(mode))
        return JSONResponse({"limit": limit}, headers={"Cache-Control": "public, max-age=60"})

    for path in (*PATHS, "/budgets-other", "/budget-status-other"):
        app.add_api_route(path, probe, methods=["GET", "POST"])
    return app


@pytest.mark.anyio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize(
    "query,status",
    [
        ({}, 200),
        ({"mode": "401"}, 401),
        ({"mode": "403"}, 403),
        ({"mode": "404"}, 404),
        ({"limit": "invalid"}, 422),
        ({"mode": "crash"}, 500),
    ],
)
async def test_finance_response_boundary_on_every_exit(path, query, status):
    async with AsyncClient(
        transport=ASGITransport(app=_app(), raise_app_exceptions=False),
        base_url="http://finance-test",
    ) as client:
        response = await client.get(path, params=query)
    assert response.status_code == status
    assert response.headers["Cache-Control"] == "no-store, private"
    assert response.headers["X-Request-ID"] and response.headers["X-Trace-ID"]


@pytest.mark.anyio
async def test_finance_cache_boundary_matches_exact_prefixes_and_unknown_subpaths():
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://finance-test"
    ) as client:
        for path in ("/budgets/unknown", "/finops/unknown"):
            response = await client.get(path)
            assert response.status_code == 404
            assert response.headers["Cache-Control"] == "no-store, private"
        for path in ("/budgets-other", "/budget-status-other"):
            response = await client.get(path)
            assert response.headers["Cache-Control"] == "public, max-age=60"
