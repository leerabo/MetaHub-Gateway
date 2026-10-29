"""Credential-isolated PlatformSession boundaries for PRD-111."""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from metahub_shared.runtime.errors import ApiError

from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
    SUPPORT_SESSION_COOKIE,
)
from metahub_control.platform_access.service import (
    PlatformContext,
    authenticate_platform_session_context,
)
from metahub_control.saas_billing.routes import (
    _cookie_values,
    _private,
    _raw_headers,
    _request_id,
    _require_origin,
    _required_cookie,
    _required_csrf,
    _strict_json,
    _trace_id,
)
from metahub_control.workspace_access.constants import WORKSPACE_SESSION_COOKIE

from .models import BackupListResponse, DRCommandRequest, DRRunResponse, DRStatusResponse
from .service import cast_dr_vote, get_dr_status, list_recovery_points

router = APIRouter(tags=["disaster-recovery"])
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_REGION_ID_RE = re.compile(r"^reg_[0-9a-f]{32}$")


def require_platform_dr(request: Request) -> PlatformContext:
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, WORKSPACE_SESSION_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("platform.credential_conflict")
    csrf = None
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        _require_origin(request, "platform.origin_invalid")
        csrf = _required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid")
    context = authenticate_platform_session_context(
        request.app.state.config,
        _required_cookie(request, PLATFORM_SESSION_COOKIE, "platform.session_required"),
        csrf_token=csrf,
    )
    required = (
        {"platform.dr.read"}
        if request.method == "GET"
        else {
            "platform.dr.execute",
            "platform.dr.approve",
        }
    )
    if required.isdisjoint(context.permissions):
        raise ApiError("platform.permission_denied")
    return context


@router.get("/platform/dr/status", response_model=DRStatusResponse)
async def dr_status(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_dr)],
) -> DRStatusResponse:
    _reject_query(request)
    result = get_dr_status(
        request.app.state.config,
        session_token=_required_cookie(
            request, PLATFORM_SESSION_COOKIE, "platform.session_required"
        ),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.get("/platform/backups", response_model=BackupListResponse)
async def backups(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_dr)],
) -> BackupListResponse:
    _reject_query(request)
    result = list_recovery_points(
        request.app.state.config,
        session_token=_required_cookie(
            request, PLATFORM_SESSION_COOKIE, "platform.session_required"
        ),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post("/platform/dr/drills", response_model=DRRunResponse, status_code=201)
async def drills(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_dr)],
) -> DRRunResponse:
    _reject_query(request)
    payload = await _model(request)
    result = cast_dr_vote(
        request.app.state.config,
        session_token=_required_cookie(
            request, PLATFORM_SESSION_COOKIE, "platform.session_required"
        ),
        csrf_token=_required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid"),
        run_kind="drill",
        region_id=None,
        payload=payload,
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = 200
    _private(response)
    return result


@router.post(
    "/platform/regions/{region_id}/failovers",
    response_model=DRRunResponse,
    status_code=201,
)
async def failovers(
    region_id: str,
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_dr)],
) -> DRRunResponse:
    _reject_query(request)
    if _REGION_ID_RE.fullmatch(region_id) is None:
        raise ApiError("dr.request_invalid")
    payload = await _model(request)
    result = cast_dr_vote(
        request.app.state.config,
        session_token=_required_cookie(
            request, PLATFORM_SESSION_COOKIE, "platform.session_required"
        ),
        csrf_token=_required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid"),
        run_kind="failover",
        region_id=region_id,
        payload=payload,
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = 200
    _private(response)
    return result


async def _model(request: Request) -> DRCommandRequest:
    try:
        return DRCommandRequest.model_validate(await _strict_json(request, "dr.request_invalid"))
    except ValueError:
        raise ApiError("dr.request_invalid") from None


def _idempotency_key(request: Request) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY_RE.fullmatch(values[0]) is None:
        raise ApiError("dr.request_invalid")
    return values[0]


def _reject_query(request: Request) -> None:
    if request.query_params.multi_items():
        raise ApiError("dr.request_invalid")


__all__ = ["require_platform_dr", "router"]
