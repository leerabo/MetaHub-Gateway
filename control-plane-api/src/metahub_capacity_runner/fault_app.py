"""Internal leased-fault API; deploy independently of Control and the load runner."""

from __future__ import annotations

import hmac
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .contracts import CapacityRunnerRequest, decode_signed
from .fault_contracts import FaultLeaseCommand, FaultLeaseInfo, FaultProxySettings
from .fault_proxy import FaultProxy
from .settings import read_secret


@asynccontextmanager
async def lifespan(service: FastAPI):
    try:
        with Path(os.environ["METAHUB_CAPACITY_FAULT_SETTINGS_FILE"]).open("rb") as source:
            payload = source.read(1024 * 1024 + 1)
        if len(payload) > 1024 * 1024:
            raise ValueError
        settings = FaultProxySettings.model_validate_json(payload)
        token, secret = (
            read_secret(settings.internal_token_file),
            read_secret(settings.permit_secret_file),
        )
        if hmac.compare_digest(token, secret):
            raise ValueError
    except (KeyError, OSError, ValueError):
        raise ValueError("fault.configuration_invalid") from None
    proxy = FaultProxy(settings)
    await proxy.start()
    service.state.proxy, service.state.credentials = proxy, (token, secret)
    try:
        yield
    finally:
        service.state.proxy, service.state.credentials = None, None
        await proxy.close()


app = FastAPI(
    title="MetaHub Fault Proxy", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
)


class FaultRequestLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        size = 0

        async def bounded_receive():
            nonlocal size
            message = await receive()
            if message["type"] == "http.request":
                size += len(message.get("body", b""))
                if size > 16384:
                    raise HTTPException(status_code=413, detail="fault.request_too_large")
            return message

        await self.app(scope, bounded_receive if scope["type"] == "http" else receive, send)


app.add_middleware(FaultRequestLimit)


@app.middleware("http")
async def prevent_caching(request: Request, call_next):
    request.state.request_id = "req_" + uuid4().hex
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Request-ID"] = request.state.request_id
    return response


@app.exception_handler(RequestValidationError)
async def invalid_request(request, _error):
    return JSONResponse(
        status_code=422,
        content={
            "detail": "fault.request_invalid",
            "request_id": request.state.request_id,
        },
    )


@app.exception_handler(HTTPException)
async def operation_error(request, error):
    return JSONResponse(
        status_code=error.status_code,
        content={
            "detail": error.detail,
            "request_id": request.state.request_id,
        },
    )


@app.get("/ready")
def ready():
    proxy = getattr(app.state, "proxy", None)
    if proxy is None or not proxy.running:
        raise HTTPException(status_code=503, detail="fault.proxy_unavailable")
    try:
        for binding in proxy.settings.bindings:
            proxy.store.active_fault(binding.binding_code)
    except (sqlite3.Error, ValueError):
        raise HTTPException(status_code=503, detail="fault.state_unavailable") from None
    return {"status": "ready", "service": "capacity-fault-proxy"}


@app.post("/internal/capacity/faults", response_model=FaultLeaseInfo)
def fault_command(request: CapacityRunnerRequest, authorization: str | None = Header(default=None)):
    proxy = getattr(app.state, "proxy", None)
    credentials = getattr(app.state, "credentials", None)
    if proxy is None or not proxy.running or credentials is None:
        raise HTTPException(status_code=503, detail="fault.proxy_unavailable")
    token, secret = credentials
    if authorization is None or not hmac.compare_digest(
        authorization.encode("utf-8"),
        f"Bearer {token}".encode("ascii"),
    ):
        raise HTTPException(status_code=401, detail="fault.unauthorized")
    try:
        command = decode_signed(
            request.permit, secret, domain="fault-lease", model_type=FaultLeaseCommand
        )
    except (ValueError, TypeError, HTTPException):
        raise HTTPException(status_code=401, detail="fault.permit_invalid") from None
    try:
        return proxy.command(command)
    except sqlite3.Error:
        raise HTTPException(status_code=503, detail="fault.state_unavailable") from None
    except ValueError as exc:
        allowed = {
            "fault.binding_unavailable": 503,
            "fault.command_invalid": 422,
            "fault.start_window_expired": 409,
            "fault.scope_conflict": 409,
            "fault.sequence_conflict": 409,
            "fault.already_applied": 409,
            "fault.execution_not_found": 404,
            "fault.binding_busy": 409,
            "fault.recovery_required": 503,
            "fault.proxy_unavailable": 503,
        }
        code = str(exc) if str(exc) in allowed else "fault.state_unavailable"
        raise HTTPException(status_code=allowed.get(code, 503), detail=code) from None
