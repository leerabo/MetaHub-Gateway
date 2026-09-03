"""Credential-isolated HTTP routes for PRD-145 console experiences."""

from __future__ import annotations

import json
import re
import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import (
    clear_identity_cookies_on_error,
    clear_workspace_cookies_on_error,
)
from pydantic import ValidationError

from metahub_control.identity.service import (
    CSRF_COOKIE as IDENTITY_CSRF_COOKIE,
)
from metahub_control.identity.service import (
    SESSION_COOKIE as IDENTITY_SESSION_COOKIE,
)
from metahub_control.identity.service import (
    AuthenticatedIdentity,
    authenticate_global_session,
    valid_opaque_value,
)
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
    SUPPORT_SESSION_COOKIE,
)
from metahub_control.platform_access.service import (
    PlatformContext,
    authenticate_platform_session_context,
)
from metahub_control.workspace_access.constants import (
    WORKSPACE_CSRF_COOKIE,
    WORKSPACE_SESSION_COOKIE,
)
from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    authenticate_workspace_session_context,
)

from .models import (
    AccountPreferenceCommandResponse,
    AccountPreferencePatch,
    AccountPreferenceResponse,
    ApprovalQueueResponse,
    AssistantQueryResponse,
    AssistantQuestionRequest,
    DeploymentSummaryResponse,
    OverviewResponse,
)
from .permissions import PLATFORM_CONSOLE_PERMISSIONS, WORKSPACE_CONSOLE_PERMISSIONS
from .service import (
    change_account_preferences,
    get_account_preferences,
    get_platform_overview,
    get_workspace_deployment_summary,
    get_workspace_overview,
    list_platform_approvals,
    list_workspace_approvals,
    query_platform_assistant,
    query_workspace_assistant,
)

router = APIRouter(tags=["console-experience"])
_MAX_BODY_BYTES = 8_192
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_IF_MATCH = re.compile(r'^"([1-9][0-9]{0,9})"$')


def require_platform_console(request: Request) -> PlatformContext:
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, WORKSPACE_SESSION_COOKIE)
        or _cookie_values(request, WORKSPACE_CSRF_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("platform.credential_conflict")
    csrf = None
    if request.method == "POST":
        _require_origin(request, "platform.origin_invalid")
        csrf = _required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid")
    context = authenticate_platform_session_context(
        request.app.state.config,
        _required_cookie(request, PLATFORM_SESSION_COOKIE, "platform.session_required"),
        csrf_token=csrf,
    )
    permission = _platform_permission(request)
    if permission not in context.permissions or permission not in PLATFORM_CONSOLE_PERMISSIONS:
        raise ApiError("platform.permission_denied")
    return context


def require_workspace_console(request: Request) -> WorkspaceSessionContext:
    clear_workspace_cookies_on_error(request)
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, PLATFORM_SESSION_COOKIE)
        or _cookie_values(request, PLATFORM_CSRF_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("workspace_session.credential_conflict")
    csrf = None
    if request.method == "POST":
        _require_origin(request, "platform.origin_invalid")
        csrf = _required_csrf(
            request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid"
        )
    context = authenticate_workspace_session_context(
        request.app.state.config,
        _required_cookie(request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"),
        csrf_token=csrf,
    )
    permission = _workspace_permission(request, context)
    if (
        permission not in context.permission_codes
        or permission not in WORKSPACE_CONSOLE_PERMISSIONS
    ):
        raise ApiError("workspace_session.permission_denied")
    return context


def require_identity_preferences(request: Request) -> AuthenticatedIdentity:
    clear_identity_cookies_on_error(request)
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, PLATFORM_SESSION_COOKIE)
        or _cookie_values(request, PLATFORM_CSRF_COOKIE)
        or _cookie_values(request, WORKSPACE_SESSION_COOKIE)
        or _cookie_values(request, WORKSPACE_CSRF_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("identity.credential_invalid")
    if request.method == "PATCH":
        _require_origin(request, "identity.origin_invalid")
        _required_csrf(request, IDENTITY_CSRF_COOKIE, "identity.csrf_invalid")
    return authenticate_global_session(
        request.app.state.config,
        _required_cookie(request, IDENTITY_SESSION_COOKIE, "identity.session_required"),
    )


@router.get("/platform/overview", response_model=OverviewResponse)
async def platform_overview_route(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_console)],
) -> OverviewResponse:
    _reject_query(request, "platform.request_invalid")
    result = get_platform_overview(
        request.app.state.config,
        session_token=_platform_token(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response, result.permission_fingerprint)
    return result


@router.get("/workspace/overview", response_model=OverviewResponse)
async def workspace_overview_route(
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_console)],
) -> OverviewResponse:
    _reject_query(request, "workspace_session.request_invalid")
    result = get_workspace_overview(
        request.app.state.config,
        session_token=_workspace_token(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response, result.permission_fingerprint)
    return result


@router.post("/platform/assistant/query", response_model=AssistantQueryResponse)
async def platform_assistant_route(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_console)],
) -> AssistantQueryResponse:
    _reject_query(request, "platform.request_invalid")
    command = await _model(request, AssistantQuestionRequest, "platform.request_invalid")
    result = query_platform_assistant(
        request.app.state.config,
        session_token=_platform_token(request),
        question=command.question,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response, result.permission_fingerprint)
    return result


@router.post("/workspace/assistant/query", response_model=AssistantQueryResponse)
async def workspace_assistant_route(
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_console)],
) -> AssistantQueryResponse:
    _reject_query(request, "workspace_session.request_invalid")
    command = await _model(
        request, AssistantQuestionRequest, "workspace_session.request_invalid"
    )
    result = query_workspace_assistant(
        request.app.state.config,
        session_token=_workspace_token(request),
        question=command.question,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response, result.permission_fingerprint)
    return result


@router.get("/platform/approvals", response_model=ApprovalQueueResponse)
async def platform_approvals_route(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_console)],
) -> ApprovalQueueResponse:
    query = _approval_query(request, "platform.request_invalid")
    result = list_platform_approvals(
        request.app.state.config,
        session_token=_platform_token(request),
        **query,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response, result.permission_fingerprint)
    return result


@router.get("/workspace/approvals", response_model=ApprovalQueueResponse)
async def workspace_approvals_route(
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_console)],
) -> ApprovalQueueResponse:
    query = _approval_query(request, "workspace_session.request_invalid")
    result = list_workspace_approvals(
        request.app.state.config,
        session_token=_workspace_token(request),
        **query,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response, result.permission_fingerprint)
    return result


@router.get("/identity/preferences", response_model=AccountPreferenceResponse)
async def account_preferences_route(
    request: Request,
    response: Response,
    _identity: Annotated[AuthenticatedIdentity, Depends(require_identity_preferences)],
) -> AccountPreferenceResponse:
    _reject_query(request, "identity.request_invalid")
    result = get_account_preferences(
        request.app.state.config,
        session_token=_identity_token(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.revision}"'
    _private(response)
    return result


@router.patch("/identity/preferences", response_model=AccountPreferenceCommandResponse)
async def patch_account_preferences_route(
    request: Request,
    response: Response,
    _identity: Annotated[AuthenticatedIdentity, Depends(require_identity_preferences)],
) -> AccountPreferenceCommandResponse:
    _reject_query(request, "identity.request_invalid")
    command = await _model(request, AccountPreferencePatch, "identity.request_invalid")
    result = change_account_preferences(
        request.app.state.config,
        session_token=_identity_token(request),
        csrf_token=_identity_csrf(request),
        expected_revision=_if_match(request),
        idempotency_key=_idempotency_key(request),
        patch=command,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.revision}"'
    _private(response)
    return result


@router.get("/workspace/deployment-summary", response_model=DeploymentSummaryResponse)
async def workspace_deployment_summary_route(
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_console)],
) -> DeploymentSummaryResponse:
    _reject_query(request, "workspace_session.request_invalid")
    result = get_workspace_deployment_summary(
        request.app.state.config,
        session_token=_workspace_token(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response, result.permission_fingerprint)
    return result


def _platform_permission(request: Request) -> str:
    key = (request.method, request.url.path)
    permissions = {
        ("GET", "/platform/overview"): "platform.console.overview.read",
        ("POST", "/platform/assistant/query"): "platform.console.assistant.query",
        ("GET", "/platform/approvals"): "platform.console.approval.read",
    }
    permission = permissions.get(key)
    if permission is None:
        raise ApiError("platform.permission_denied")
    return permission


def _workspace_permission(
    request: Request, context: WorkspaceSessionContext
) -> str:
    key = (request.method, request.url.path)
    permissions = {
        ("GET", "/workspace/overview"): "workspace.console.overview.read",
        ("POST", "/workspace/assistant/query"): "workspace.console.assistant.query",
        ("GET", "/workspace/approvals"): "workspace.console.approval.read",
    }
    permission = permissions.get(key)
    if permission is not None:
        return permission
    if key == ("GET", "/workspace/deployment-summary"):
        return (
            "workspace.deployment_diagnostic.read"
            if "workspace.deployment_diagnostic.read" in context.permission_codes
            else "workspace.deployment_diagnostic.audit_read"
        )
    raise ApiError("workspace_session.permission_denied")


def _approval_query(request: Request, error_code: str) -> dict[str, object]:
    allowed = {"status", "risk", "source_domain", "cursor", "limit"}
    values: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key not in allowed or key in values or _control(key) or _control(value):
            raise ApiError(error_code)
        values[key] = value
    raw_limit = values.get("limit", "50")
    if (
        not raw_limit.isascii()
        or not raw_limit.isdigit()
        or (len(raw_limit) > 1 and raw_limit.startswith("0"))
    ):
        raise ApiError(error_code)
    cursor = values.get("cursor")
    if cursor == "":
        raise ApiError(error_code)
    return {
        "status_filter": values.get("status"),
        "risk_filter": values.get("risk"),
        "source_domain": values.get("source_domain"),
        "cursor": cursor,
        "limit": int(raw_limit),
    }


async def _model(request: Request, model_type, error_code: str):
    payload = await _strict_json(request, error_code)
    try:
        return model_type.model_validate_json(
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")), strict=True
        )
    except ValidationError:
        raise ApiError(error_code) from None


async def _strict_json(request: Request, error_code: str) -> dict[str, Any]:
    content_types = _raw_headers(request, b"content-type")
    if len(content_types) != 1:
        raise ApiError(error_code)
    media_type, separator, parameters = content_types[0].partition(";")
    if media_type.strip().lower() != "application/json" or (
        separator and parameters.strip().lower() not in {"charset=utf-8", 'charset="utf-8"'}
    ):
        raise ApiError(error_code)
    if _raw_headers(request, b"transfer-encoding"):
        raise ApiError(error_code)
    lengths = _raw_headers(request, b"content-length")
    if len(lengths) > 1:
        raise ApiError(error_code)
    declared = None
    if lengths:
        if not lengths[0].isdigit() or (len(lengths[0]) > 1 and lengths[0].startswith("0")):
            raise ApiError(error_code)
        declared = int(lengths[0])
        if not 2 <= declared <= _MAX_BODY_BYTES:
            raise ApiError(error_code)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_BODY_BYTES:
            raise ApiError(error_code)
    if not body or (declared is not None and declared != len(body)):
        raise ApiError(error_code)

    def unique_object(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        payload = json.loads(
            bytes(body).decode("utf-8", errors="strict"),
            object_pairs_hook=unique_object,
            parse_float=lambda _: (_ for _ in ()).throw(ValueError("float")),
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise ApiError(error_code) from None
    if type(payload) is not dict:
        raise ApiError(error_code)
    return payload


def _reject_query(request: Request, error_code: str) -> None:
    if request.query_params.multi_items():
        raise ApiError(error_code)


def _required_cookie(request: Request, name: str, missing_code: str) -> str:
    values = _cookie_values(request, name)
    if len(values) != 1 or not valid_opaque_value(values[0]):
        raise ApiError(missing_code)
    return values[0]


def _required_csrf(request: Request, cookie_name: str, error_code: str) -> str:
    cookies = _cookie_values(request, cookie_name)
    headers = _raw_headers(request, b"x-csrf-token")
    if (
        len(cookies) != 1
        or len(headers) != 1
        or not valid_opaque_value(cookies[0])
        or not valid_opaque_value(headers[0])
        or not secrets.compare_digest(cookies[0], headers[0])
    ):
        raise ApiError(error_code)
    return cookies[0]


def _require_origin(request: Request, error_code: str) -> None:
    origins = _raw_headers(request, b"origin")
    sites = _raw_headers(request, b"sec-fetch-site")
    if (
        len(origins) != 1
        or "," in origins[0]
        or origins[0] not in request.app.state.config.allowed_origins
        or len(sites) > 1
        or (sites and sites[0].lower() == "cross-site")
    ):
        raise ApiError(error_code)


def _if_match(request: Request) -> int:
    values = _raw_headers(request, b"if-match")
    match = _IF_MATCH.fullmatch(values[0]) if len(values) == 1 else None
    if match is None:
        raise ApiError("identity.revision_required")
    return int(match.group(1))


def _idempotency_key(request: Request) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY.fullmatch(values[0]) is None:
        raise ApiError("identity.idempotency_invalid")
    return values[0]


def _platform_token(request: Request) -> str:
    return _required_cookie(request, PLATFORM_SESSION_COOKIE, "platform.session_required")


def _workspace_token(request: Request) -> str:
    return _required_cookie(
        request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
    )


def _identity_token(request: Request) -> str:
    return _required_cookie(request, IDENTITY_SESSION_COOKIE, "identity.session_required")


def _identity_csrf(request: Request) -> str:
    return _required_csrf(request, IDENTITY_CSRF_COOKIE, "identity.csrf_invalid")


def _cookie_values(request: Request, name: str) -> list[str]:
    values: list[str] = []
    for raw in _raw_headers(request, b"cookie"):
        for segment in raw.split(";"):
            key, separator, value = segment.strip().partition("=")
            if separator and key == name:
                values.append(value)
    return values


def _raw_headers(request: Request, name: bytes) -> list[str]:
    return [
        value.decode("latin-1")
        for key, value in request.scope.get("headers", ())
        if key.lower() == name
    ]


def _private(response: Response, permission_fingerprint: str | None = None) -> None:
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["Vary"] = "Cookie"
    if permission_fingerprint is not None:
        response.headers["X-Permission-Fingerprint"] = permission_fingerprint


def _control(value: str) -> bool:
    return any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value)


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", "")


__all__ = ["router"]
