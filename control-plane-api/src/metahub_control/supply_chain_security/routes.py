"""Separated Developer Bearer and Platform Cookie routes for PRD-136."""

from __future__ import annotations

import re

from fastapi import APIRouter, Request, Response
from metahub_shared.runtime.errors import ApiError
from starlette.concurrency import run_in_threadpool

from metahub_control.auth import require_developer_portal_reader
from metahub_control.identity.service import valid_opaque_value
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
    SUPPORT_SESSION_COOKIE,
)
from metahub_control.platform_access.service import authenticate_platform_session_context
from metahub_control.saas_billing.routes import (
    _cookie_values,
    _private,
    _raw_headers,
    _request_id,
    _require_origin,
    _required_csrf,
    _strict_json,
    _trace_id,
)
from metahub_control.workspace_access.constants import WORKSPACE_SESSION_COOKIE

from .models import (
    BuildProvenanceResponse,
    ReleaseSecurityReportResponse,
    SecurityExceptionCommand,
    SecurityExceptionResponse,
)
from .service import apply_security_exception, get_release_provenance, get_release_security_report

router = APIRouter(tags=["supply-chain-security"])
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")


@router.get("/releases/{release_id}/security-report", response_model=ReleaseSecurityReportResponse)
async def release_security_report(
    release_id: str, request: Request, response: Response
) -> ReleaseSecurityReportResponse:
    _reject_query_and_body(request)
    reader = await _reader(request)
    result = await run_in_threadpool(
        get_release_security_report,
        request.app.state.config,
        release_id,
        reader,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.get("/releases/{release_id}/provenance", response_model=BuildProvenanceResponse)
async def release_provenance(
    release_id: str, request: Request, response: Response
) -> BuildProvenanceResponse:
    _reject_query_and_body(request)
    reader = await _reader(request)
    result = await run_in_threadpool(
        get_release_provenance,
        request.app.state.config,
        release_id,
        reader,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post(
    "/platform/vulnerabilities/{finding_id}/exceptions",
    response_model=SecurityExceptionResponse,
)
async def vulnerability_exception(
    finding_id: str, request: Request, response: Response
) -> SecurityExceptionResponse:
    token, csrf = _platform_credentials(request, write=True)
    _reject_query(request)
    try:
        command = SecurityExceptionCommand.model_validate(
            await _strict_json(request, "supply_chain.request_invalid")
        )
    except ValueError:
        raise ApiError("supply_chain.request_invalid") from None
    result = await run_in_threadpool(
        apply_security_exception,
        request.app.state.config,
        finding_id,
        command,
        platform_session_token=token,
        platform_csrf_token=csrf,
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


async def _reader(request: Request):
    authorization = _raw_headers(request, b"authorization")
    platform = _cookie_values(request, PLATFORM_SESSION_COOKIE)
    other_cookies = _cookie_values(request, WORKSPACE_SESSION_COOKIE) + _cookie_values(
        request, SUPPORT_SESSION_COOKIE
    )
    if len(authorization) == 1 and not platform and not other_cookies:
        return await run_in_threadpool(require_developer_portal_reader, request, authorization[0])
    if not authorization and len(platform) == 1 and not other_cookies:
        if not valid_opaque_value(platform[0]):
            raise ApiError("platform.session_required")
        return await run_in_threadpool(
            authenticate_platform_session_context, request.app.state.config, platform[0]
        )
    raise ApiError("supply_chain.credential_conflict")


def _platform_credentials(request: Request, *, write: bool) -> tuple[str, str]:
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, WORKSPACE_SESSION_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("platform.credential_conflict")
    values = _cookie_values(request, PLATFORM_SESSION_COOKIE)
    if len(values) != 1 or not valid_opaque_value(values[0]):
        raise ApiError("platform.session_required")
    if not write:
        return values[0], ""
    _require_origin(request, "platform.origin_invalid")
    return values[0], _required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid")


def _idempotency_key(request: Request) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY.fullmatch(values[0]) is None:
        raise ApiError("supply_chain.idempotency_invalid")
    return values[0]


def _reject_query(request: Request) -> None:
    if request.query_params.multi_items():
        raise ApiError("supply_chain.request_invalid")


def _reject_query_and_body(request: Request) -> None:
    _reject_query(request)
    if _raw_headers(request, b"transfer-encoding") or _raw_headers(request, b"content-type"):
        raise ApiError("supply_chain.request_invalid")
    lengths = _raw_headers(request, b"content-length")
    if len(lengths) > 1 or (lengths and lengths[0] != "0"):
        raise ApiError("supply_chain.request_invalid")
