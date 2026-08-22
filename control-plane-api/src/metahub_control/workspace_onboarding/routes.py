"""Strict WorkspaceSession transport for PRD-097 onboarding APIs."""

from __future__ import annotations

import json
import re
import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import clear_workspace_cookies_on_error

from metahub_control.identity.service import valid_opaque_value
from metahub_control.platform_access.constants import (
    PLATFORM_SESSION_COOKIE,
    SUPPORT_SESSION_COOKIE,
)
from metahub_control.workspace_access.constants import (
    WORKSPACE_CSRF_COOKIE,
    WORKSPACE_SESSION_COOKIE,
)
from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    authenticate_workspace_session_context,
)

from .models import WorkspaceOnboardingMutationRequest, WorkspaceOnboardingResponse
from .service import (
    get_workspace_onboarding,
    mutate_workspace_onboarding_step,
    recheck_workspace_onboarding,
)

router = APIRouter(prefix="/workspaces/current/onboarding", tags=["workspace-onboarding"])
_MAX_BODY_BYTES = 1024
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")


def require_onboarding_session(request: Request) -> WorkspaceSessionContext:
    """Authenticate the selected Workspace before a mutation body is parsed."""

    clear_workspace_cookies_on_error(request)
    if _raw_headers(request, b"authorization") or _foreign_session_present(request):
        raise ApiError("workspace_session.credential_conflict")
    csrf_token = None
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        _require_write_origin(request)
        csrf_token = _required_csrf(request)
    return authenticate_workspace_session_context(
        request.app.state.config,
        _required_workspace_cookie(request),
        csrf_token=csrf_token,
    )


@router.get("", response_model=WorkspaceOnboardingResponse)
async def get_current_onboarding(
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_onboarding_session)],
) -> WorkspaceOnboardingResponse:
    _reject_query(request)
    result = get_workspace_onboarding(
        request.app.state.config,
        session_token=_required_workspace_cookie(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = _etag(result.revision)
    return result


@router.patch("/steps/{step_id}", response_model=WorkspaceOnboardingResponse)
async def patch_current_onboarding_step(
    step_id: str,
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_onboarding_session)],
) -> WorkspaceOnboardingResponse:
    _reject_query(request)
    expected_revision = _if_match(request)
    idempotency_key = _idempotency_key(request)
    raw = await _strict_json(request)
    try:
        payload = WorkspaceOnboardingMutationRequest.model_validate(raw)
    except ValueError:
        raise ApiError("onboarding.request_invalid") from None
    result = mutate_workspace_onboarding_step(
        request.app.state.config,
        session_token=_required_workspace_cookie(request),
        csrf_token=_required_csrf(request),
        step_id=step_id,
        action=payload.action,
        reason_code=payload.reason_code,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = _etag(result.revision)
    return result


@router.post("/recheck", response_model=WorkspaceOnboardingResponse)
async def post_current_onboarding_recheck(
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_onboarding_session)],
) -> WorkspaceOnboardingResponse:
    _reject_query(request)
    _reject_body(request)
    result = recheck_workspace_onboarding(
        request.app.state.config,
        session_token=_required_workspace_cookie(request),
        csrf_token=_required_csrf(request),
        expected_revision=_if_match(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = _etag(result.revision)
    return result


async def _strict_json(request: Request) -> dict[str, Any]:
    content_types = _raw_headers(request, b"content-type")
    if len(content_types) != 1:
        raise ApiError("onboarding.request_invalid")
    media_type, separator, parameters = content_types[0].partition(";")
    if media_type.strip().lower() != "application/json" or (
        separator and parameters.strip().lower() not in {"charset=utf-8", 'charset="utf-8"'}
    ):
        raise ApiError("onboarding.request_invalid")
    if _raw_headers(request, b"transfer-encoding"):
        raise ApiError("onboarding.request_invalid")
    lengths = _raw_headers(request, b"content-length")
    if len(lengths) > 1:
        raise ApiError("onboarding.request_invalid")
    declared: int | None = None
    if lengths:
        if not lengths[0].isdigit() or (len(lengths[0]) > 1 and lengths[0].startswith("0")):
            raise ApiError("onboarding.request_invalid")
        declared = int(lengths[0])
        if declared < 2 or declared > _MAX_BODY_BYTES:
            raise ApiError("onboarding.request_invalid")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_BODY_BYTES:
            raise ApiError("onboarding.request_invalid")
    if not body or (declared is not None and declared != len(body)):
        raise ApiError("onboarding.request_invalid")

    def unique_object(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    try:
        payload = json.loads(bytes(body).decode("utf-8"), object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ApiError("onboarding.request_invalid") from None
    if type(payload) is not dict:
        raise ApiError("onboarding.request_invalid")
    return payload


def _required_workspace_cookie(request: Request) -> str:
    values = _cookie_values(request, WORKSPACE_SESSION_COOKIE)
    if len(values) != 1 or not valid_opaque_value(values[0]):
        raise ApiError("workspace_session.required" if not values else "workspace_session.invalid")
    return values[0]


def _required_csrf(request: Request) -> str:
    cookies = _cookie_values(request, WORKSPACE_CSRF_COOKIE)
    headers = _raw_headers(request, b"x-csrf-token")
    if (
        len(cookies) != 1
        or len(headers) != 1
        or not valid_opaque_value(cookies[0])
        or not valid_opaque_value(headers[0])
        or not secrets.compare_digest(cookies[0], headers[0])
    ):
        raise ApiError("workspace_session.csrf_invalid")
    return cookies[0]


def _require_write_origin(request: Request) -> None:
    origins = _raw_headers(request, b"origin")
    sites = _raw_headers(request, b"sec-fetch-site")
    if (
        len(origins) != 1
        or "," in origins[0]
        or origins[0] not in request.app.state.config.allowed_origins
        or len(sites) > 1
        or (sites and sites[0].lower() == "cross-site")
    ):
        raise ApiError("onboarding.origin_invalid")


def _foreign_session_present(request: Request) -> bool:
    return bool(
        _cookie_values(request, PLATFORM_SESSION_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    )


def _cookie_values(request: Request, cookie_name: str) -> list[str]:
    values: list[str] = []
    for raw_cookie in _raw_headers(request, b"cookie"):
        for segment in raw_cookie.split(";"):
            name, separator, value = segment.strip().partition("=")
            if separator and name == cookie_name:
                values.append(value)
    return values


def _if_match(request: Request) -> int:
    values = _raw_headers(request, b"if-match")
    if len(values) != 1 or re.fullmatch(r'"[1-9][0-9]*"', values[0]) is None:
        raise ApiError("onboarding.revision_required")
    return int(values[0][1:-1])


def _idempotency_key(request: Request) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY_RE.fullmatch(values[0]) is None:
        raise ApiError("onboarding.request_invalid")
    return values[0]


def _reject_query(request: Request) -> None:
    if request.query_params.multi_items():
        raise ApiError("onboarding.request_invalid")


def _reject_body(request: Request) -> None:
    if _raw_headers(request, b"transfer-encoding") or _raw_headers(request, b"content-type"):
        raise ApiError("onboarding.request_invalid")
    lengths = _raw_headers(request, b"content-length")
    if len(lengths) > 1 or (lengths and lengths[0] != "0"):
        raise ApiError("onboarding.request_invalid")


def _raw_headers(request: Request, name: bytes) -> list[str]:
    return [
        raw_value.decode("latin-1")
        for raw_name, raw_value in request.scope.get("headers", ())
        if raw_name.lower() == name
    ]


def _etag(revision: int) -> str:
    return f'"{revision}"'


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", "")
