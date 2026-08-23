"""Strict PlatformSession HTTP boundary for the platform job runtime."""

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

from .models import (
    PlatformJobActionRequest,
    PlatformJobActionResponse,
    PlatformJobDetailResponse,
    PlatformJobQueueResponse,
    PlatformJobTypeCommandRequest,
    PlatformJobTypeResponse,
)
from .service import (
    get_platform_job,
    list_platform_job_queues,
    update_platform_job,
    update_platform_job_type,
)

router = APIRouter(tags=["platform-jobs"])
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_STATUSES = frozenset(
    {
        "queued",
        "leased",
        "running",
        "cancel_requested",
        "succeeded",
        "failed",
        "cancelled",
        "dead_lettered",
    }
)


def require_platform_job(request: Request) -> PlatformContext:
    """Reject every non-platform principal and revalidate live permission state."""

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
    if request.method == "GET":
        permission = "platform.job.read"
    elif request.url.path == "/platform/job-types":
        permission = "platform.job.type_manage"
    else:
        permission = "platform.job.manage"
    if permission not in context.permissions:
        raise ApiError("platform.permission_denied")
    return context


@router.get("/platform/job-queues", response_model=PlatformJobQueueResponse)
async def job_queues(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_job)],
) -> PlatformJobQueueResponse:
    status, limit = _queue_query(request)
    result = list_platform_job_queues(
        request.app.state.config,
        session_token=_platform_cookie(request),
        status=status,
        limit=limit,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.get("/platform/jobs/{job_id}", response_model=PlatformJobDetailResponse)
async def job_detail(
    job_id: str,
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_job)],
) -> PlatformJobDetailResponse:
    _reject_query(request)
    result = get_platform_job(
        request.app.state.config,
        session_token=_platform_cookie(request),
        job_id=job_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.job.revision}"'
    _private(response)
    return result


@router.post("/platform/jobs/{job_id}/retry", response_model=PlatformJobActionResponse)
async def retry_job(
    job_id: str,
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_job)],
) -> PlatformJobActionResponse:
    return await _job_action(job_id, "retry", request, response)


@router.post("/platform/jobs/{job_id}/cancel", response_model=PlatformJobActionResponse)
async def cancel_job(
    job_id: str,
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_job)],
) -> PlatformJobActionResponse:
    return await _job_action(job_id, "cancel", request, response)


@router.post("/platform/job-types", response_model=PlatformJobTypeResponse)
async def command_job_type(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_job)],
) -> PlatformJobTypeResponse:
    _reject_query(request)
    try:
        payload = PlatformJobTypeCommandRequest.model_validate(
            await _strict_json(request, "platform_job.request_invalid")
        )
    except ValueError:
        raise ApiError("platform_job.request_invalid") from None
    result = update_platform_job_type(
        request.app.state.config,
        session_token=_platform_cookie(request),
        csrf_token=_platform_csrf(request),
        expected_revision=_if_match(request),
        idempotency_key=_idempotency_key(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.job_type.revision}"'
    _private(response)
    return result


async def _job_action(
    job_id: str, action: str, request: Request, response: Response
) -> PlatformJobActionResponse:
    _reject_query(request)
    try:
        payload = PlatformJobActionRequest.model_validate(
            await _strict_json(request, "platform_job.request_invalid")
        )
    except ValueError:
        raise ApiError("platform_job.request_invalid") from None
    result = update_platform_job(
        request.app.state.config,
        session_token=_platform_cookie(request),
        csrf_token=_platform_csrf(request),
        job_id=job_id,
        action=action,
        expected_revision=_if_match(request),
        idempotency_key=_idempotency_key(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.job.revision}"'
    _private(response)
    return result


def _queue_query(request: Request) -> tuple[str | None, int]:
    items = request.query_params.multi_items()
    if len(items) != len({key for key, _value in items}):
        raise ApiError("platform_job.request_invalid")
    if any(key not in {"status", "limit"} for key, _value in items):
        raise ApiError("platform_job.request_invalid")
    status = request.query_params.get("status")
    if status is not None and status not in _STATUSES:
        raise ApiError("platform_job.request_invalid")
    raw_limit = request.query_params.get("limit", "50")
    if re.fullmatch(r"[1-9][0-9]{0,2}", raw_limit) is None:
        raise ApiError("platform_job.request_invalid")
    limit = int(raw_limit)
    if limit > 100:
        raise ApiError("platform_job.request_invalid")
    return status, limit


def _if_match(request: Request) -> int:
    values = _raw_headers(request, b"if-match")
    if len(values) != 1 or re.fullmatch(r'"[1-9][0-9]*"', values[0]) is None:
        raise ApiError("platform_job.request_invalid")
    return int(values[0][1:-1])


def _idempotency_key(request: Request) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY_RE.fullmatch(values[0]) is None:
        raise ApiError("platform_job.request_invalid")
    return values[0]


def _reject_query(request: Request) -> None:
    if request.query_params.multi_items():
        raise ApiError("platform_job.request_invalid")


def _platform_cookie(request: Request) -> str:
    return _required_cookie(request, PLATFORM_SESSION_COOKIE, "platform.session_required")


def _platform_csrf(request: Request) -> str:
    return _required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid")


__all__ = ["require_platform_job", "router"]
