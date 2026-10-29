"""PRD-147 production router, OpenAPI, and ordinary-404 retirement contracts."""

from __future__ import annotations

import re

import pytest
from httpx import ASGITransport, AsyncClient
from metahub_control.app import app
from metahub_control.console_migration.manifest import LEGACY_ROUTE_MANIFEST


def _concrete_path(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "prd147-retired-object", path)


def _error_shape(response) -> dict[str, object]:
    body = response.json()
    for field in ("request_id", "trace_id"):
        body.pop(field, None)
    return body


def test_production_openapi_has_no_retired_console_route() -> None:
    schema = app.openapi()
    openapi_routes = {
        (method.upper(), path)
        for path, item in schema["paths"].items()
        for method in item
        if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}
    }
    assert not (set(LEGACY_ROUTE_MANIFEST) & openapi_routes)


@pytest.mark.anyio
async def test_retired_console_routes_are_ordinary_not_found() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://control"
    ) as client:
        unknown = await client.get("/prd147-ordinary-unknown-route")
        assert unknown.status_code == 404
        assert unknown.headers["cache-control"] == "no-store"
        expected_body = _error_shape(unknown)
        expected_content_type = unknown.headers.get("content-type")
        expected_cache_control = unknown.headers.get("cache-control")
        for method, path in LEGACY_ROUTE_MANIFEST:
            response = await client.request(method, _concrete_path(path))
            assert response.status_code == 404, (method, path, response.text)
            assert _error_shape(response) == expected_body, (method, path)
            assert response.headers.get("content-type") == expected_content_type
            assert response.headers.get("cache-control") == expected_cache_control, (
                method,
                path,
                response.headers.get("cache-control"),
                expected_cache_control,
            )
