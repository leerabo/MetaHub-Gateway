"""Middleware binding contracts; actual authenticated persistence is tested in Control."""

import hashlib
import hmac
import secrets
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from metahub_shared.observability import (
    bind_authenticated_telemetry_tenant,
    install_telemetry_runtime,
)
from starlette.requests import Request as StarletteRequest

TENANT = "ten_" + "1" * 32
FOREIGN = "ten_" + "2" * 32


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.parametrize("invalid", ["", "private owner", "x" * 129, None, False, "../owner"])
def test_invalid_or_conflicting_owner_cannot_be_rebound(invalid):
    request = StarletteRequest({"type": "http", "state": {}})
    bind_authenticated_telemetry_tenant(request, invalid)
    bind_authenticated_telemetry_tenant(request, TENANT)
    assert request.state._telemetry_tenant_owner is False
    other = StarletteRequest({"type": "http", "state": {}})
    bind_authenticated_telemetry_tenant(other, TENANT)
    bind_authenticated_telemetry_tenant(other, TENANT)
    assert other.state._telemetry_tenant_owner == TENANT
    bind_authenticated_telemetry_tenant(other, FOREIGN)
    bind_authenticated_telemetry_tenant(other, TENANT)
    assert other.state._telemetry_tenant_owner is False


@pytest.mark.anyio
@pytest.mark.parametrize("backend_fails", [False, True])
async def test_middleware_uses_only_request_local_trusted_binding(monkeypatch, backend_fails):
    secret = secrets.token_urlsafe(48)
    app = FastAPI()
    app.state.config = SimpleNamespace(platform_session_secret=secret, object_store_region=None)
    observed = []

    @app.get("/authorized")
    async def authorized(request: Request):
        bind_authenticated_telemetry_tenant(request, TENANT)
        return {"ok": True}

    @app.get("/anonymous")
    async def anonymous():
        return {"ok": True}

    @app.get("/conflict")
    async def conflict(request: Request):
        bind_authenticated_telemetry_tenant(request, TENANT)
        bind_authenticated_telemetry_tenant(request, FOREIGN)
        return {"ok": True}

    install_telemetry_runtime(app, service_code="control")
    runtime = app.state.telemetry_runtime

    def persist(facts, dropped):
        assert dropped == 0
        if backend_fails:
            raise sqlite3.OperationalError("private-backend-diagnostic")
        observed.extend(facts)

    monkeypatch.setattr(runtime, "_persist_batch", persist)
    async with AsyncClient(
        transport=ASGITransport(app), base_url="https://telemetry.test"
    ) as client:
        for path in ("/authorized", "/anonymous", "/conflict", "/authorized"):
            response = await client.get(
                path,
                headers={
                    "X-Tenant-ID": FOREIGN,
                    "X-Telemetry-Operation": "private-label-canary",
                },
            )
            assert response.status_code == 200
    assert runtime.snapshot().accepted == runtime.snapshot().queued == 4
    assert runtime.flush_once() is (not backend_fails)
    if backend_fails:
        assert runtime.snapshot().dropped == 4
        assert runtime.snapshot().backend_failures == 1
    else:
        pseudonym = hmac.new(
            secret.encode(),
            f"prd109/telemetry-tenant/v1\0{TENANT}".encode(),
            hashlib.sha256,
        ).hexdigest()
        assert [fact.tenant_pseudonym for fact in observed] == [
            pseudonym,
            "none",
            "none",
            pseudonym,
        ]
        assert all(fact.operation_code == "other" for fact in observed)
        assert runtime.snapshot().persisted == 4
        assert TENANT not in repr(observed) and secret not in repr(observed)
