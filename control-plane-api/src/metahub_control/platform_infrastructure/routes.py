"""Credential-isolated Platform transport boundaries for PRD-100."""

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

from .models import BackupCreateRequest, BackupResponse, InfrastructureStatusResponse
from .service import create_backup, get_backup, get_infrastructure_status

router = APIRouter(tags=["platform-infrastructure"])
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_BACKUP_ID_RE = re.compile(r"^bkp_[0-9a-f]{32}$")


def require_platform_infrastructure(request: Request) -> PlatformContext:
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
        "platform.backup.create"
        if request.method == "POST"
        else "platform.infrastructure.read"
    )
    if required not in context.permissions:
        raise ApiError("platform.permission_denied")
    return context


@router.get(
    "/platform/infrastructure/status", response_model=InfrastructureStatusResponse
)
async def infrastructure_status(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_infrastructure)],
) -> InfrastructureStatusResponse:
    _reject_query(request)
    result = get_infrastructure_status(
        request.app.state.config,
        session_token=_required_cookie(
            request, PLATFORM_SESSION_COOKIE, "platform.session_required"
        ),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post("/platform/backups", response_model=BackupResponse, status_code=201)
async def post_backup(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_infrastructure)],
) -> BackupResponse:
    _reject_query(request)
    try:
        payload = BackupCreateRequest.model_validate(
            await _strict_json(request, "infrastructure.request_invalid")
        )
    except ValueError:
        raise ApiError("infrastructure.request_invalid") from None
    result = create_backup(
        request.app.state.config,
        session_token=_required_cookie(
            request, PLATFORM_SESSION_COOKIE, "platform.session_required"
        ),
        csrf_token=_required_csrf(
            request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid"
        ),
        payload=payload,
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = 200
    _private(response)
    return result


@router.get("/platform/backups/{backup_id}", response_model=BackupResponse)
async def backup_detail(
    backup_id: str,
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_infrastructure)],
) -> BackupResponse:
    _reject_query(request)
    if _BACKUP_ID_RE.fullmatch(backup_id) is None:
        raise ApiError("infrastructure.request_invalid")
    result = get_backup(
        request.app.state.config,
        session_token=_required_cookie(
            request, PLATFORM_SESSION_COOKIE, "platform.session_required"
        ),
        backup_id=backup_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


def _idempotency_key(request: Request) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY_RE.fullmatch(values[0]) is None:
        raise ApiError("infrastructure.request_invalid")
    return values[0]


def _reject_query(request: Request) -> None:
    if request.query_params.multi_items():
        raise ApiError("infrastructure.request_invalid")


__all__ = ["router"]
