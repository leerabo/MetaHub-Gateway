"""Internal-only capacity runner with fixed catalogs and aggregate-only output."""

from __future__ import annotations

import hmac
import os
import threading

from fastapi import FastAPI, Header, HTTPException

from .contracts import (
    CapacityKillClaims,
    CapacityKillRequest,
    CapacityKillResponse,
    CapacityPermitClaims,
    CapacityRunnerRequest,
    CapacityRunnerResult,
    decode_signed,
)
from .engine import validate_capacity_scope

_KILL_FENCES: dict[str, int] = {}
_LOCK = threading.RLock()

app = FastAPI(
    title="MetaHub Capacity Runner",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "capacity-runner"}


@app.post("/internal/capacity/runs", response_model=CapacityRunnerResult)
def run_capacity(
    request: CapacityRunnerRequest,
    authorization: str | None = Header(default=None),
) -> CapacityRunnerResult:
    token, secret = _credentials()
    _authorize(authorization, token)
    try:
        claims = decode_signed(
            request.permit, secret, domain="run-permit", model_type=CapacityPermitClaims
        )
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="runner.permit_invalid") from None
    with _LOCK:
        if _KILL_FENCES.get(claims.object_id, 0) > claims.kill_generation:
            raise HTTPException(status_code=409, detail="runner.killed")
    validate_capacity_scope(claims)
    # Until an actual isolated executor is connected, never manufacture measurements.
    raise HTTPException(status_code=503, detail="runner.executor_unavailable")


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
    with _LOCK:
        _KILL_FENCES[experiment_id] = max(
            claims.kill_generation, _KILL_FENCES.get(experiment_id, 0)
        )
    return CapacityKillResponse(
        accepted=True,
        experiment_id=experiment_id,
        kill_generation=claims.kill_generation,
    )


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
    if value is None or not value.startswith(prefix) or not hmac.compare_digest(
        value[len(prefix) :], token
    ):
        raise HTTPException(status_code=401, detail="runner.unauthorized")
__all__ = ["app"]
