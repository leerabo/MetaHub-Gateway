"""Credential-isolated HTTP boundaries for PRD-112 status communication."""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import clear_workspace_cookies_on_error

from metahub_control.identity.service import SESSION_COOKIE
from metahub_control.notifications.routes import (
    _cookie_values,
    _raw_headers,
    _request_id,
    _required_cookie,
    _required_csrf,
    _strict_json,
    _trace_id,
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
from metahub_control.workspace_access.constants import WORKSPACE_SESSION_COOKIE
from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    authenticate_workspace_session_context,
)

from .models import (
    IncidentApprovalRequest,
    IncidentCreateRequest,
    IncidentResponse,
    IncidentUpdateRequest,
    MaintenanceCreateRequest,
    MaintenanceResponse,
    PostmortemPublishRequest,
    StatusSubscriptionListResponse,
    StatusSubscriptionMutationRequest,
    StatusSubscriptionMutationResponse,
    StatusWorkspaceResponse,
)
from .service import (
    approve_security_incident,
    create_incident,
    create_maintenance_window,
    list_platform_status,
    list_status_subscriptions,
    list_workspace_incidents,
    mutate_status_subscription,
    publish_postmortem,
    update_incident,
)

router = APIRouter(tags=["public-status"])
_ID = re.compile(r"^inc_[0-9a-f]{32}$")
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")


def require_workspace_status(request: Request) -> WorkspaceSessionContext:
    clear_workspace_cookies_on_error(request)
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, PLATFORM_SESSION_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
        or _cookie_values(request, SESSION_COOKIE)
    ):
        raise ApiError("workspace_session.credential_conflict")
    return authenticate_workspace_session_context(
        request.app.state.config,
        _required_cookie(request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"),
    )


def require_platform_status(request: Request) -> PlatformContext:
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, WORKSPACE_SESSION_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
        or _cookie_values(request, SESSION_COOKIE)
    ):
        raise ApiError("platform.credential_conflict")
    csrf = None
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        _require_origin(request)
        csrf = _required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid")
    context = authenticate_platform_session_context(
        request.app.state.config,
        _required_cookie(request, PLATFORM_SESSION_COOKIE, "platform.session_required"),
        csrf_token=csrf,
    )
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        permission = "platform.status.read"
    elif request.url.path.endswith("/security-approval"):
        permission = "platform.status.security_approve"
    else:
        permission = "platform.status.write"
    if permission not in context.permissions:
        raise ApiError("platform.permission_denied")
    return context


@router.get("/status/subscriptions", response_model=StatusSubscriptionListResponse)
async def get_status_subscriptions(
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_status)],
) -> StatusSubscriptionListResponse:
    _reject_query(request)
    result = list_status_subscriptions(
        request.app.state.config,
        session_token=_required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post(
    "/status/subscriptions",
    response_model=StatusSubscriptionMutationResponse,
    status_code=202,
)
async def post_status_subscription(
    request: Request,
    response: Response,
) -> StatusSubscriptionMutationResponse:
    _reject_query(request)
    if _raw_headers(request, b"authorization"):
        raise ApiError("status.credential_conflict")
    payload = _model(await _strict_json(request), StatusSubscriptionMutationRequest)
    result = mutate_status_subscription(
        request.app.state.config,
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.get("/incidents", response_model=StatusWorkspaceResponse)
async def get_incidents(
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_status)],
) -> StatusWorkspaceResponse:
    _reject_query(request)
    result = list_workspace_incidents(
        request.app.state.config,
        session_token=_required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.get("/platform/status-incidents", response_model=StatusWorkspaceResponse)
async def get_platform_status_incidents(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_status)],
) -> StatusWorkspaceResponse:
    _reject_query(request)
    result = list_platform_status(
        request.app.state.config,
        session_token=_platform_cookie(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post("/platform/incidents", response_model=IncidentResponse, status_code=201)
async def post_platform_incident(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_status)],
) -> IncidentResponse:
    _reject_query(request)
    payload = _model(await _strict_json(request), IncidentCreateRequest)
    result = create_incident(
        request.app.state.config,
        session_token=_platform_cookie(request),
        idempotency_key=_idempotency_key(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _write_response(response, result.idempotent_replay, result.incident.revision)
    return result


@router.post(
    "/platform/incidents/{incident_id}/security-approval",
    response_model=IncidentResponse,
    status_code=201,
)
async def post_platform_incident_security_approval(
    incident_id: str,
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_status)],
) -> IncidentResponse:
    _incident_id(incident_id)
    _reject_query(request)
    payload = _model(await _strict_json(request), IncidentApprovalRequest)
    _require_if_match(request, payload.revision)
    result = approve_security_incident(
        request.app.state.config,
        session_token=_platform_cookie(request),
        incident_id=incident_id,
        idempotency_key=_idempotency_key(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _write_response(response, result.idempotent_replay, result.incident.revision)
    return result


@router.post(
    "/platform/incidents/{incident_id}/updates",
    response_model=IncidentResponse,
    status_code=201,
)
async def post_platform_incident_update(
    incident_id: str,
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_status)],
) -> IncidentResponse:
    _incident_id(incident_id)
    _reject_query(request)
    payload = _model(await _strict_json(request), IncidentUpdateRequest)
    _require_if_match(request, payload.revision)
    result = update_incident(
        request.app.state.config,
        session_token=_platform_cookie(request),
        incident_id=incident_id,
        idempotency_key=_idempotency_key(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _write_response(response, result.idempotent_replay, result.incident.revision)
    return result


@router.post(
    "/platform/incidents/{incident_id}/postmortem",
    response_model=IncidentResponse,
    status_code=201,
)
async def post_platform_incident_postmortem(
    incident_id: str,
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_status)],
) -> IncidentResponse:
    _incident_id(incident_id)
    _reject_query(request)
    payload = _model(await _strict_json(request), PostmortemPublishRequest)
    _require_if_match(request, payload.revision)
    result = publish_postmortem(
        request.app.state.config,
        session_token=_platform_cookie(request),
        incident_id=incident_id,
        idempotency_key=_idempotency_key(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _write_response(response, result.idempotent_replay, result.incident.revision)
    return result


@router.post(
    "/platform/maintenance-windows",
    response_model=MaintenanceResponse,
    status_code=201,
)
async def post_platform_maintenance_window(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_status)],
) -> MaintenanceResponse:
    _reject_query(request)
    payload = _model(await _strict_json(request), MaintenanceCreateRequest)
    result = create_maintenance_window(
        request.app.state.config,
        session_token=_platform_cookie(request),
        idempotency_key=_idempotency_key(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _write_response(
        response, result.idempotent_replay, result.maintenance_window.revision
    )
    return result


def _model(payload: object, model_type):
    try:
        return model_type.model_validate(payload)
    except ValueError:
        raise ApiError("status.request_invalid") from None


def _idempotency_key(request: Request) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY.fullmatch(values[0]) is None:
        raise ApiError("status.request_invalid")
    return values[0]


def _require_if_match(request: Request, revision: int) -> None:
    values = _raw_headers(request, b"if-match")
    if len(values) != 1 or values[0] != f'"{revision}"':
        raise ApiError("status.revision_conflict")


def _require_origin(request: Request) -> None:
    origins = _raw_headers(request, b"origin")
    sites = _raw_headers(request, b"sec-fetch-site")
    if (
        len(origins) != 1
        or "," in origins[0]
        or origins[0] not in request.app.state.config.allowed_origins
        or len(sites) > 1
        or (sites and sites[0].lower() == "cross-site")
    ):
        raise ApiError("platform.origin_invalid")


def _platform_cookie(request: Request) -> str:
    return _required_cookie(request, PLATFORM_SESSION_COOKIE, "platform.session_required")


def _incident_id(value: str) -> None:
    if _ID.fullmatch(value) is None:
        raise ApiError("status.request_invalid")


def _reject_query(request: Request) -> None:
    if request.query_params.multi_items():
        raise ApiError("status.request_invalid")


def _private(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"


def _write_response(response: Response, replay: bool, revision: int) -> None:
    if replay:
        response.status_code = 200
    response.headers["ETag"] = f'"{revision}"'
    _private(response)


__all__ = ["require_platform_status", "require_workspace_status", "router"]
