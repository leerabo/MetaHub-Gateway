"""Internal-only capacity runner with fixed catalogs and aggregate-only output."""

from __future__ import annotations

import hmac
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request, Response

from .contracts import (
    CapacityExecutionInfo,
    CapacityKillClaims,
    CapacityKillRequest,
    CapacityKillResponse,
    CapacityPermitClaims,
    CapacityRunnerRequest,
    CapacityRunnerResult,
    decode_signed,
)
from .engine import validate_capacity_scope
from .settings import load_settings
from .supervisor import CapacityExecutor


@asynccontextmanager
async def lifespan(service: FastAPI):
    configuration = os.getenv("METAHUB_CAPACITY_RUNNER_SETTINGS_FILE")
    executor = CapacityExecutor(load_settings(Path(configuration))) if configuration else None
    service.state.executor = executor
    if executor is not None:
        executor.start()
    try:
        yield
    finally:
        if executor is not None:
            executor.close()
        service.state.executor = None


app = FastAPI(
    title="MetaHub Capacity Runner",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


@app.middleware("http")
async def prevent_caching(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "capacity-runner"}


@app.get("/ready")
def ready() -> dict[str, str]:
    executor = _executor()
    if executor.worker is None or not executor.worker.is_alive() or executor.stopped.is_set():
        raise HTTPException(status_code=503, detail="runner.executor_unavailable")
    return {"status": "ready", "service": "capacity-runner"}


@app.post("/internal/capacity/runs", response_model=CapacityRunnerResult | CapacityExecutionInfo)
def run_capacity(
    request: CapacityRunnerRequest,
    response: Response,
    authorization: str | None = Header(default=None),
) -> CapacityRunnerResult | CapacityExecutionInfo:
    claims = _claims(request, authorization)
    executor = getattr(app.state, "executor", None)
    if executor is not None:
        _operation(lambda: executor.store.require_not_killed(claims))
    validate_capacity_scope(claims)
    if executor is None:
        raise HTTPException(status_code=503, detail="runner.executor_unavailable")
    status = _operation(lambda: executor.submit(claims))
    response.headers["Cache-Control"] = "no-store"
    if status.result is not None:
        return status.result
    response.status_code = 202 if status.state in {"queued", "running", "stopping"} else 200
    return status


def _claims(request: CapacityRunnerRequest, authorization: str | None):
    token, secret = _credentials()
    _authorize(authorization, token)
    try:
        claims = decode_signed(
            request.permit, secret, domain="run-permit", model_type=CapacityPermitClaims
        )
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="runner.permit_invalid") from None
    return claims


@app.post("/internal/capacity/runs/{object_id}/status", response_model=CapacityExecutionInfo)
def execution_status(
    object_id: str,
    request: CapacityRunnerRequest,
    response: Response,
    authorization: str | None = Header(default=None),
):
    claims = _claims(request, authorization)
    if claims.object_id != object_id:
        raise HTTPException(status_code=409, detail="runner.scope_conflict")
    response.headers["Cache-Control"] = "no-store"
    return _operation(lambda: _executor().store.status(claims))


@app.post("/internal/capacity/runs/{object_id}/cancel", response_model=CapacityExecutionInfo)
def cancel_execution(
    object_id: str,
    request: CapacityRunnerRequest,
    response: Response,
    authorization: str | None = Header(default=None),
):
    claims = _claims(request, authorization)
    if claims.object_id != object_id:
        raise HTTPException(status_code=409, detail="runner.scope_conflict")
    response.headers["Cache-Control"] = "no-store"
    return _operation(lambda: _executor().store.cancel(claims))


@app.post(
    "/internal/capacity/runs/{experiment_id}/kill",
    response_model=CapacityKillResponse,
)
def kill_capacity(
    experiment_id: str,
    request: CapacityKillRequest,
    authorization: str | None = Header(default=None),
) -> CapacityKillResponse:
    token, secret = _credentials()
    _authorize(authorization, token)
    try:
        claims = decode_signed(
            request.permit, secret, domain="kill-permit", model_type=CapacityKillClaims
        )
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="runner.permit_invalid") from None
    if not hmac.compare_digest(claims.experiment_id, experiment_id):
        raise HTTPException(status_code=409, detail="runner.scope_conflict")
    generation = _operation(lambda: _executor().store.kill(experiment_id, claims.kill_generation))
    return CapacityKillResponse(
        accepted=True,
        experiment_id=experiment_id,
        kill_generation=generation,
    )


def _executor():
    executor = getattr(app.state, "executor", None)
    if executor is None:
        raise HTTPException(status_code=503, detail="runner.executor_unavailable")
    return executor


def _operation(action):
    try:
        return action()
    except ValueError as exc:
        code = str(exc)
        allowed = {
            "runner.scope_conflict": 409,
            "runner.killed": 409,
            "runner.execution_not_found": 404,
            "runner.queue_full": 429,
            "runner.target_unavailable": 503,
            "runner.target_capacity_exceeded": 422,
            "runner.identities_unavailable": 503,
            "runner.identities_invalid": 503,
            "runner.credentials_unavailable": 503,
            "runner.fault_adapter_unavailable": 503,
            "runner.executor_unavailable": 503,
        }
        raise HTTPException(
            status_code=allowed.get(code, 503),
            detail=code if code in allowed else "runner.state_unavailable",
        ) from None
    except sqlite3.Error:
        raise HTTPException(status_code=503, detail="runner.state_unavailable") from None


def _credentials() -> tuple[str, str]:
    token = os.getenv("METAHUB_CAPACITY_RUNNER_INTERNAL_TOKEN", "")
    secret = os.getenv("METAHUB_CAPACITY_RUNNER_PERMIT_SECRET", "")
    try:
        encoded = (token.encode("ascii"), secret.encode("ascii"))
    except UnicodeEncodeError:
        encoded = ()
    if (
        len(encoded) != 2
        or any(
            not 32 <= len(value) <= 512
            or any(character.isspace() or ord(character) < 32 for character in credential)
            for credential, value in zip((token, secret), encoded, strict=True)
        )
        or hmac.compare_digest(token, secret)
    ):
        raise HTTPException(status_code=503, detail="runner.not_configured")
    return token, secret


def _authorize(value: str | None, token: str) -> None:
    prefix = "Bearer "
    if (
        value is None
        or not value.startswith(prefix)
        or not hmac.compare_digest(value[len(prefix) :].encode("utf-8"), token.encode("ascii"))
    ):
        raise HTTPException(status_code=401, detail="runner.unauthorized")


__all__ = ["app"]
