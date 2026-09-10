"""Strict Workspace and Platform HTTP boundaries for PRD-105 notifications."""

from __future__ import annotations

import json
import re
import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import clear_workspace_cookies_on_error
from starlette.concurrency import run_in_threadpool

from metahub_control.identity.service import valid_opaque_value
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
    NotificationListResponse,
    NotificationPreferencesPatchRequest,
    NotificationPreferencesResponse,
    NotificationReadResponse,
    NotificationTemplateCreateRequest,
    NotificationTemplateListResponse,
    NotificationTemplateResponse,
)
from .service import (
    create_notification_template,
    get_notification_preferences,
    list_notification_templates,
    list_notifications,
    mark_notification_read,
    update_notification_preferences,
)

router = APIRouter(tags=["notifications"])
_MAX_BODY_BYTES = 12 * 1024
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")


def require_workspace_notification(request: Request) -> WorkspaceSessionContext:
    clear_workspace_cookies_on_error(request)
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, PLATFORM_SESSION_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("workspace_session.credential_conflict")
    csrf = None
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        _require_origin(request, "notification.request_invalid")
        csrf = _required_csrf(request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid")
    return authenticate_workspace_session_context(
        request.app.state.config,
        _required_cookie(request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"),
        csrf_token=csrf,
    )


def require_platform_notification(request: Request) -> PlatformContext:
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
    permission = (
        "platform.notification_template.write"
        if request.method not in {"GET", "HEAD", "OPTIONS"}
        else "platform.notification_template.read"
    )
    if permission not in context.permissions:
        raise ApiError("platform.permission_denied")
    return context


@router.get("/notifications", response_model=NotificationListResponse)
async def get_notifications(
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_notification)],
    unread_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    cursor: str | None = None,
) -> NotificationListResponse:
    _exact_query(request, {"unread_only", "limit", "cursor"})
    _private(response)
    return await run_in_threadpool(
        list_notifications,
        request.app.state.config,
        session_token=_required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        unread_only=unread_only,
        limit=limit,
        cursor=cursor,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.patch("/notifications/{notification_id}/read", response_model=NotificationReadResponse)
async def patch_notification_read(
    notification_id: str,
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_notification)],
) -> NotificationReadResponse:
    _reject_query(request)
    if await _strict_json(request):
        raise ApiError("notification.request_invalid")
    result = await run_in_threadpool(
        mark_notification_read,
        request.app.state.config,
        session_token=_required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        csrf_token=_required_csrf(request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid"),
        notification_id=notification_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.get("/notification-preferences", response_model=NotificationPreferencesResponse)
async def get_preferences(
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_notification)],
) -> NotificationPreferencesResponse:
    _reject_query(request)
    result = await run_in_threadpool(
        get_notification_preferences,
        request.app.state.config,
        session_token=_required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = _etag(result.revision)
    _private(response)
    return result


@router.patch("/notification-preferences", response_model=NotificationPreferencesResponse)
async def patch_preferences(
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_notification)],
) -> NotificationPreferencesResponse:
    _reject_query(request)
    payload = _model(await _strict_json(request), NotificationPreferencesPatchRequest)
    result = await run_in_threadpool(
        update_notification_preferences,
        request.app.state.config,
        session_token=_required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        csrf_token=_required_csrf(request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid"),
        expected_revision=_if_match(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = _etag(result.revision)
    _private(response)
    return result


@router.get(
    "/platform/notification-templates", response_model=NotificationTemplateListResponse
)
async def get_platform_notification_templates(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_notification)],
) -> NotificationTemplateListResponse:
    _reject_query(request)
    result = await run_in_threadpool(
        list_notification_templates,
        request.app.state.config,
        platform_session_token=_required_cookie(
            request, PLATFORM_SESSION_COOKIE, "platform.session_required"
        ),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = _etag(result.catalog_revision)
    _private(response)
    return result


@router.post(
    "/platform/notification-templates",
    response_model=NotificationTemplateResponse,
    status_code=201,
)
async def post_platform_notification_template(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_notification)],
) -> NotificationTemplateResponse:
    _reject_query(request)
    payload = _model(await _strict_json(request), NotificationTemplateCreateRequest)
    result = await run_in_threadpool(
        create_notification_template,
        request.app.state.config,
        platform_session_token=_required_cookie(
            request, PLATFORM_SESSION_COOKIE, "platform.session_required"
        ),
        csrf_token=_required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid"),
        expected_revision=_if_match(request),
        idempotency_key=_idempotency_key(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = 200
    response.headers["ETag"] = _etag(result.catalog_revision)
    _private(response)
    return result


async def _strict_json(request: Request) -> dict[str, Any]:
    content_types = _raw_headers(request, b"content-type")
    if len(content_types) != 1:
        raise ApiError("notification.request_invalid")
    media_type, separator, parameters = content_types[0].partition(";")
    if media_type.strip().lower() != "application/json" or (
        separator and parameters.strip().lower() not in {"charset=utf-8", 'charset="utf-8"'}
    ):
        raise ApiError("notification.request_invalid")
    if _raw_headers(request, b"transfer-encoding"):
        raise ApiError("notification.request_invalid")
    lengths = _raw_headers(request, b"content-length")
    if len(lengths) > 1:
        raise ApiError("notification.request_invalid")
    declared = None
    if lengths:
        if not lengths[0].isdigit() or (len(lengths[0]) > 1 and lengths[0].startswith("0")):
            raise ApiError("notification.request_invalid")
        declared = int(lengths[0])
        if declared < 2 or declared > _MAX_BODY_BYTES:
            raise ApiError("notification.request_invalid")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_BODY_BYTES:
            raise ApiError("notification.request_invalid")
    if not body or (declared is not None and declared != len(body)):
        raise ApiError("notification.request_invalid")

    def unique(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    try:
        value = json.loads(bytes(body).decode("utf-8"), object_pairs_hook=unique)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise ApiError("notification.request_invalid") from None
    if type(value) is not dict:
        raise ApiError("notification.request_invalid")
    return value


def _model(payload: dict[str, Any], model_type):
    try:
        return model_type.model_validate(payload)
    except ValueError:
        raise ApiError("notification.request_invalid") from None


def _if_match(request: Request) -> int:
    values = _raw_headers(request, b"if-match")
    if len(values) != 1 or re.fullmatch(r'"[1-9][0-9]{0,9}"', values[0]) is None:
        raise ApiError("notification.revision_required")
    return int(values[0][1:-1])


def _idempotency_key(request: Request) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY_RE.fullmatch(values[0]) is None:
        raise ApiError("notification.request_invalid")
    return values[0]


def _required_cookie(request: Request, name: str, missing_code: str) -> str:
    values = _cookie_values(request, name)
    if len(values) != 1 or not valid_opaque_value(values[0]):
        if values and name == PLATFORM_SESSION_COOKIE:
            raise ApiError("platform.session_invalid")
        if values and name == WORKSPACE_SESSION_COOKIE:
            raise ApiError("workspace_session.invalid")
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


def _reject_query(request: Request) -> None:
    if request.query_params.multi_items():
        raise ApiError("notification.request_invalid")


def _exact_query(request: Request, allowed: set[str]) -> None:
    keys = [key for key, _value in request.query_params.multi_items()]
    if any(key not in allowed for key in keys) or len(keys) != len(set(keys)):
        raise ApiError("notification.request_invalid")


def _private(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["X-Content-Type-Options"] = "nosniff"


def _etag(revision: int) -> str:
    return f'"{revision}"'


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", "")


__all__ = ["router"]
