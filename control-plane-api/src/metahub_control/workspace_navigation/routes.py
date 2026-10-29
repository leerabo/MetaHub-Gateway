"""Strict transport boundary for PRD-146 Workspace navigation."""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import (
    clear_identity_cookies_on_auth_error,
    clear_workspace_cookies_on_error,
)
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

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
from metahub_control.workspace_access.constants import (
    WORKSPACE_CSRF_COOKIE,
    WORKSPACE_SESSION_COOKIE,
)
from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    authenticate_workspace_session_context,
)

from .catalog import ADVANCED_TOOL_SPECS
from .models import (
    AdvancedToolOpenResponse,
    AdvancedToolsResponse,
    GettingStartedResponse,
    NavigationPreferencePatch,
    NavigationPreferenceResponse,
    WorkspaceNavigationResponse,
    WorkspacePageVisitRequest,
    WorkspacePageVisitResponse,
)
from .permissions import WORKSPACE_NAVIGATION_PERMISSIONS
from .service import (
    change_navigation_preference,
    get_workspace_advanced_tools,
    get_workspace_getting_started,
    get_workspace_navigation,
    open_workspace_advanced_tool,
    record_workspace_page_visit,
)

router = APIRouter(tags=["workspace-navigation"])
_MAX_BODY_BYTES = 4_096
_NAVIGATION_AUTH_DEADLINE_SECONDS = 2.0
_TASK_HOME_RETRY_DELAYS_SECONDS = (0.075, 0.15)
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_IF_MATCH = re.compile(r'^"(0|[1-9][0-9]{0,9})"$')
_TOOL_CODE = re.compile(r"^[a-z][a-z0-9_-]{2,79}$")
_KNOWN_TOOL_CODES = frozenset(spec.tool_code for spec in ADVANCED_TOOL_SPECS)


def require_workspace_navigation(request: Request) -> WorkspaceSessionContext:
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
            request,
            WORKSPACE_CSRF_COOKIE,
            "workspace_session.csrf_invalid",
        )
    context = authenticate_workspace_session_context(
        request.app.state.config,
        _required_cookie(
            request,
            WORKSPACE_SESSION_COOKIE,
            "workspace_session.required",
        ),
        csrf_token=csrf,
        # Navigation is a read-heavy boundary used during logout and account
        # transitions. A stale page-view writer must not make auth wait for
        # SQLite's process-wide 30-second lock timeout.
        monotonic_deadline=time.monotonic() + _NAVIGATION_AUTH_DEADLINE_SECONDS,
        deadline_error_code="workspace_session.unavailable",
        # Navigation is read-heavy and immediately followed by its own
        # audited transaction for page visits. Avoid touching last_seen here,
        # which would promote every concurrent tab load to BEGIN IMMEDIATE.
        touch=False,
    )
    permission = _workspace_permission(request)
    if (
        permission not in context.permission_codes
        or permission not in WORKSPACE_NAVIGATION_PERMISSIONS
    ):
        raise ApiError("workspace_session.permission_denied")
    return context


def require_workspace_page_visit(request: Request) -> None:
    """Validate transport credentials before the single page-visit transaction.

    Page-view telemetry performs its authoritative Workspace/session and
    permission checks inside ``record_workspace_page_visit``.  Repeating the
    full dependency authentication here opens a second SQLite transaction for
    every beacon and can make logout compete with stale telemetry.
    """
    clear_workspace_cookies_on_error(request)
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, PLATFORM_SESSION_COOKIE)
        or _cookie_values(request, PLATFORM_CSRF_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("workspace_session.credential_conflict")
    _require_origin(request, "platform.origin_invalid")
    _required_csrf(request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid")
    _required_cookie(request, WORKSPACE_SESSION_COOKIE, "workspace_session.required")


def require_identity_navigation(request: Request) -> AuthenticatedIdentity:
    clear_identity_cookies_on_auth_error(request)
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, PLATFORM_SESSION_COOKIE)
        or _cookie_values(request, PLATFORM_CSRF_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("identity.credential_invalid")
    # Global and Workspace cookies share the host and therefore coexist in browsers.
    # This route authenticates only the GlobalSession; Workspace cookies are ignored.
    _require_origin(request, "identity.origin_invalid")
    _required_csrf(request, IDENTITY_CSRF_COOKIE, "identity.csrf_invalid")
    return authenticate_global_session(
        request.app.state.config,
        _required_cookie(
            request,
            IDENTITY_SESSION_COOKIE,
            "identity.session_required",
        ),
    )


@router.get("/workspace/navigation", response_model=WorkspaceNavigationResponse)
async def workspace_navigation_route(
    request: Request,
    response: Response,
    _context: Annotated[
        WorkspaceSessionContext,
        Depends(require_workspace_navigation),
    ],
) -> WorkspaceNavigationResponse:
    _reject_query(request, "workspace_session.request_invalid")
    result = await run_in_threadpool(
        get_workspace_navigation,
        request.app.state.config,
        session_token=_workspace_token(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = _etag(
        result.contract_generation,
        result.projection_generation,
        result.navigation_epoch,
        result.permission_revision,
    )
    _private(response, result.permission_fingerprint)
    return result


@router.get("/workspace/getting-started", response_model=GettingStartedResponse)
async def workspace_getting_started_route(
    request: Request,
    response: Response,
    _context: Annotated[
        WorkspaceSessionContext,
        Depends(require_workspace_navigation),
    ],
) -> GettingStartedResponse:
    _reject_query(request, "workspace_session.request_invalid")
    # Task-home hydration and page-view telemetry both persist audit facts in
    # SQLite. During a real route transition the telemetry writer can briefly
    # hold the writer lock; retry only this transient availability error so a
    # valid session does not surface a false 503 to the user. Authorization,
    # validation and all other errors remain fail-closed and are not retried.
    result = None
    for attempt, delay in enumerate((*_TASK_HOME_RETRY_DELAYS_SECONDS, None)):
        try:
            result = await run_in_threadpool(
                get_workspace_getting_started,
                request.app.state.config,
                session_token=_workspace_token(request),
                request_id=_request_id(request),
                trace_id=_trace_id(request),
            )
            break
        except ApiError as exc:
            if exc.code != "workspace_session.unavailable" or delay is None:
                raise
            await asyncio.sleep(delay)
    assert result is not None
    response.headers["ETag"] = _etag(
        result.contract_generation,
        result.task_generation,
        result.navigation_epoch,
        result.permission_revision,
    )
    _private(response, result.permission_fingerprint)
    return result


@router.patch(
    "/identity/preferences/navigation",
    response_model=NavigationPreferenceResponse,
)
async def patch_navigation_preference_route(
    request: Request,
    response: Response,
    _identity: Annotated[
        AuthenticatedIdentity,
        Depends(require_identity_navigation),
    ],
) -> NavigationPreferenceResponse:
    _reject_query(request, "identity.request_invalid")
    patch = await _model(
        request,
        NavigationPreferencePatch,
        "identity.request_invalid",
    )
    result = await run_in_threadpool(
        change_navigation_preference,
        request.app.state.config,
        session_token=_identity_token(request),
        csrf_token=_identity_csrf(request),
        expected_revision=_if_match(request, minimum=0, error_code="identity.revision_required"),
        idempotency_key=_idempotency_key(request, "identity.idempotency_invalid"),
        patch=patch,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = _etag(result.revision)
    _private(response, result.permission_fingerprint)
    return result


@router.get("/workspace/advanced-tools", response_model=AdvancedToolsResponse)
async def workspace_advanced_tools_route(
    request: Request,
    response: Response,
    _context: Annotated[
        WorkspaceSessionContext,
        Depends(require_workspace_navigation),
    ],
) -> AdvancedToolsResponse:
    _reject_query(request, "workspace_session.request_invalid")
    result = await run_in_threadpool(
        get_workspace_advanced_tools,
        request.app.state.config,
        session_token=_workspace_token(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = _etag(
        result.contract_generation,
        result.entitlement_generation,
        result.navigation_epoch,
        result.permission_revision,
    )
    _private(response, result.permission_fingerprint)
    return result


@router.post(
    "/workspace/advanced-tools/{tool_code}/open",
    response_model=AdvancedToolOpenResponse,
)
async def open_workspace_advanced_tool_route(
    tool_code: str,
    request: Request,
    response: Response,
    _context: Annotated[
        WorkspaceSessionContext,
        Depends(require_workspace_navigation),
    ],
) -> AdvancedToolOpenResponse:
    _reject_query(request, "workspace_session.request_invalid")
    await _reject_body(request, "workspace_session.request_invalid")
    if _TOOL_CODE.fullmatch(tool_code) is None or tool_code not in _KNOWN_TOOL_CODES:
        raise ApiError("workspace_session.request_invalid")
    result = await run_in_threadpool(
        open_workspace_advanced_tool,
        request.app.state.config,
        session_token=_workspace_token(request),
        tool_code=tool_code,
        expected_revision=_if_match(
            request,
            minimum=1,
            error_code="workspace_session.request_invalid",
        ),
        idempotency_key=_idempotency_key(
            request,
            "workspace_session.request_invalid",
        ),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = _etag(result.entitlement_revision)
    _private(response, result.permission_fingerprint)
    return result


@router.post("/workspace/navigation/page-views", response_model=WorkspacePageVisitResponse)
async def workspace_page_visit_route(
    request: Request,
    response: Response,
    _context: Annotated[None, Depends(require_workspace_page_visit)],
) -> WorkspacePageVisitResponse:
    _reject_query(request, "workspace_session.request_invalid")
    payload = await _model(request, WorkspacePageVisitRequest, "workspace_session.request_invalid")
    result = await run_in_threadpool(
        record_workspace_page_visit,
        request.app.state.config,
        session_token=_workspace_token(request),
        payload=payload,
        idempotency_key=_idempotency_key(request, "workspace_session.request_invalid"),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response, payload.permission_fingerprint)
    response.headers["ETag"] = f'"{result.event_id}"'
    return result


def _workspace_permission(request: Request) -> str:
    key = (request.method, request.url.path)
    permissions = {
        ("GET", "/workspace/navigation"): "workspace.navigation.read",
        ("GET", "/workspace/getting-started"): "workspace.task_home.read",
        ("GET", "/workspace/advanced-tools"): "workspace.advanced_tool.read",
        ("POST", "/workspace/navigation/page-views"): "workspace.navigation.read",
    }
    permission = permissions.get(key)
    if permission is not None:
        return permission
    if request.method == "POST" and re.fullmatch(
        r"/workspace/advanced-tools/[a-z][a-z0-9_-]{2,79}/open",
        request.url.path,
    ):
        return "workspace.advanced_tool.open"
    raise ApiError("workspace_session.permission_denied")


async def _model(request: Request, model_type, error_code: str):
    payload = await _strict_json(request, error_code)
    try:
        return model_type.model_validate_json(
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
            strict=True,
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
    try:
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > _MAX_BODY_BYTES:
                raise ApiError(error_code)
    except ClientDisconnect:
        raise ApiError(error_code) from None
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


async def _reject_body(request: Request, error_code: str) -> None:
    if _raw_headers(request, b"transfer-encoding"):
        raise ApiError(error_code)
    lengths = _raw_headers(request, b"content-length")
    if len(lengths) > 1 or (
        lengths and (not lengths[0].isascii() or not lengths[0].isdigit() or int(lengths[0]) != 0)
    ):
        raise ApiError(error_code)
    try:
        body = await request.body()
    except ClientDisconnect:
        raise ApiError(error_code) from None
    if body:
        raise ApiError(error_code)


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


def _if_match(request: Request, *, minimum: int, error_code: str) -> int:
    values = _raw_headers(request, b"if-match")
    match = _IF_MATCH.fullmatch(values[0]) if len(values) == 1 else None
    if match is None:
        raise ApiError(error_code)
    revision = int(match.group(1))
    if revision < minimum:
        raise ApiError(error_code)
    return revision


def _idempotency_key(request: Request, error_code: str) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY.fullmatch(values[0]) is None:
        raise ApiError(error_code)
    return values[0]


def _workspace_token(request: Request) -> str:
    return _required_cookie(
        request,
        WORKSPACE_SESSION_COOKIE,
        "workspace_session.required",
    )


def _identity_token(request: Request) -> str:
    return _required_cookie(
        request,
        IDENTITY_SESSION_COOKIE,
        "identity.session_required",
    )


def _identity_csrf(request: Request) -> str:
    return _required_csrf(
        request,
        IDENTITY_CSRF_COOKIE,
        "identity.csrf_invalid",
    )


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


def _private(response: Response, permission_fingerprint: str) -> None:
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["Vary"] = "Cookie"
    response.headers["X-Permission-Fingerprint"] = permission_fingerprint


def _etag(*parts: int) -> str:
    return '"' + ".".join(str(part) for part in parts) + '"'


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", "")


__all__ = ["router"]
