"""Strict credential-domain HTTP boundaries for Platform and Support routes."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any, Literal

import anyio
from fastapi import APIRouter, Request, Response
from metahub_shared.db.connection import database_request_scope
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.identity.service import CSRF_COOKIE as GLOBAL_CSRF_COOKIE
from metahub_control.identity.service import SESSION_COOKIE as GLOBAL_SESSION_COOKIE
from metahub_control.identity.service import valid_opaque_value
from metahub_control.workspace_access.constants import (
    WORKSPACE_CSRF_COOKIE,
    WORKSPACE_SESSION_COOKIE,
)

from .constants import PLATFORM_CSRF_COOKIE, PLATFORM_SESSION_COOKIE, SUPPORT_SESSION_COOKIE
from .models import (
    BreakGlassReviewResponse,
    BreakGlassSessionResponse,
    PlatformAuditListResponse,
    PlatformBootstrapResponse,
    PlatformNavigationResponse,
    PlatformOperatorListResponse,
    PlatformOperatorResponse,
    PlatformSessionResponse,
    SupportDiagnosticsResponse,
    SupportGrantListResponse,
    SupportGrantResponse,
    SupportSessionResponse,
)
from .service import (
    approve_support_grant_platform,
    approve_support_grant_workspace,
    bootstrap_platform_owner,
    create_break_glass_session,
    create_platform_operator,
    create_support_grant,
    get_platform_navigation,
    get_platform_session,
    get_support_diagnostics,
    get_support_session,
    issue_platform_session,
    issue_support_session,
    list_platform_audit,
    list_platform_operators,
    list_support_grants,
    review_break_glass_platform,
    review_break_glass_workspace,
    revoke_support_grant_platform,
    revoke_support_grant_workspace,
    update_platform_operator,
)
from .workspace_models import (
    OwnerNotificationResponse,
    PlatformWorkspaceDetailResponse,
    PlatformWorkspaceHealthResponse,
    PlatformWorkspaceListResponse,
    PlatformWorkspaceMutationResponse,
    WorkspaceOperationalEventListResponse,
)
from .workspace_service import (
    add_workspace_risk_tag,
    authorize_workspace_write,
    get_platform_workspace,
    get_platform_workspace_health,
    list_platform_workspaces,
    list_workspace_events,
    notify_workspace_owner,
    remove_workspace_risk_tag,
    restore_workspace,
    suspend_workspace,
)

router = APIRouter(tags=["platform"])
_WORK_LIMITER = anyio.CapacityLimiter(4)
_MAX_BODY_BYTES = 8192
_REASON_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,79}$")
_BOOTSTRAP_REASONS = frozenset({"initial_platform_owner"})
_SUPPORT_REQUEST_REASONS = frozenset({"workspace_diagnostics", "incident_response"})
_SUPPORT_REVOKE_REASONS = frozenset(
    {"operator_revoked", "diagnostics_complete", "customer_revoked", "security_revoked"}
)
_BREAK_GLASS_REASONS = frozenset({"incident_containment", "service_recovery"})
_WORKSPACE_FILTERS = frozenset(
    {
        "search",
        "kind",
        "operational_status",
        "region",
        "risk_tag",
        "health_status",
        "health_risk",
        "plan_status",
        "deployment_status",
    }
)
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")


@router.post("/platform/bootstrap", response_model=PlatformBootstrapResponse)
async def bootstrap(request: Request, response: Response) -> PlatformBootstrapResponse:
    _reject_authorization(request)
    _require_write_origin(request)
    payload = await _strict_json(
        request,
        required_fields=(
            "current_password",
            "totp_code",
            "recovery_secret",
            "reason_code",
            "confirmed",
        ),
    )
    if payload["confirmed"] is not True:
        raise ApiError("platform.request_invalid")
    result = await _run(
        bootstrap_platform_owner,
        _config(request),
        global_session_token=_required_cookie(
            request, GLOBAL_SESSION_COOKIE, "platform.session_required"
        ),
        global_csrf_token=_required_csrf(request, GLOBAL_CSRF_COOKIE, "identity.csrf_invalid"),
        current_password=_string(payload, "current_password", 1, 1024),
        totp_code=_totp(payload),
        recovery_secret=_string(payload, "recovery_secret", 32, 512),
        reason_code=_reason(payload, _BOOTSTRAP_REASONS),
        device_fingerprint=_device_fingerprint(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post("/platform/sessions", response_model=PlatformSessionResponse)
async def create_session(request: Request, response: Response) -> PlatformSessionResponse:
    _reject_authorization(request)
    _require_write_origin(request)
    payload = await _strict_json(request, required_fields=("totp_code",))
    issued = await _run(
        issue_platform_session,
        _config(request),
        global_session_token=_required_cookie(
            request, GLOBAL_SESSION_COOKIE, "platform.session_required"
        ),
        global_csrf_token=_required_csrf(request, GLOBAL_CSRF_COOKIE, "identity.csrf_invalid"),
        totp_code=_totp(payload),
        device_fingerprint=_device_fingerprint(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _set_platform_cookies(response, _config(request), issued.session_token, issued.csrf_token)
    _clear_workspace_and_support(response)
    _private(response)
    return issued.response


@router.get("/platform/session", response_model=PlatformSessionResponse)
async def current_session(request: Request, response: Response) -> PlatformSessionResponse:
    _platform_read_boundary(request)
    result = await _run(
        get_platform_session,
        _config(request),
        session_token=_required_platform_cookie(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.get("/platform/operators", response_model=PlatformOperatorListResponse)
async def operators(request: Request, response: Response) -> PlatformOperatorListResponse:
    _platform_read_boundary(request, allowed_query=("limit", "offset"))
    limit, offset = _pagination(request)
    result = await _run(
        list_platform_operators,
        _config(request),
        session_token=_required_platform_cookie(request),
        limit=limit,
        offset=offset,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post("/platform/operators", response_model=PlatformOperatorResponse, status_code=201)
async def create_operator(request: Request, response: Response) -> PlatformOperatorResponse:
    _platform_write_boundary(request)
    payload = await _strict_json(request, required_fields=("account_id", "roles"))
    result = await _run(
        create_platform_operator,
        _config(request),
        session_token=_required_platform_cookie(request),
        csrf_token=_required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid"),
        account_id=_string(payload, "account_id", 36, 36),
        roles=_roles(payload),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.patch("/platform/operators/{operator_id}", response_model=PlatformOperatorResponse)
async def update_operator(
    operator_id: str, request: Request, response: Response
) -> PlatformOperatorResponse:
    _platform_write_boundary(request)
    payload = await _strict_json(request, required_fields=("status", "roles"))
    result = await _run(
        update_platform_operator,
        _config(request),
        session_token=_required_platform_cookie(request),
        csrf_token=_required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid"),
        operator_id=operator_id,
        expected_revision=_if_match(request),
        status=_string(payload, "status", 6, 8),
        roles=_roles(payload),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.get("/platform/navigation", response_model=PlatformNavigationResponse)
async def navigation(request: Request, response: Response) -> PlatformNavigationResponse:
    _platform_read_boundary(request)
    result = await _run(
        get_platform_navigation,
        _config(request),
        session_token=_required_platform_cookie(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.get(
    "/platform/workspaces",
    response_model=PlatformWorkspaceListResponse,
    response_model_exclude_none=True,
)
async def platform_workspaces(
    request: Request, response: Response
) -> PlatformWorkspaceListResponse:
    _platform_read_boundary(request, allowed_query=("limit", "cursor", *_WORKSPACE_FILTERS))
    limit, cursor, filters = _workspace_pagination(request)
    result = await _run(
        list_platform_workspaces,
        _config(request),
        session_token=_required_platform_cookie(request),
        limit=limit,
        cursor=cursor,
        filters=filters,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.get(
    "/platform/workspaces/{workspace_id}",
    response_model=PlatformWorkspaceDetailResponse,
    response_model_exclude_none=True,
)
async def platform_workspace_detail(
    workspace_id: str, request: Request, response: Response
) -> PlatformWorkspaceDetailResponse:
    _platform_read_boundary(request)
    result = await _run(
        get_platform_workspace,
        _config(request),
        session_token=_required_platform_cookie(request),
        workspace_id=workspace_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = _etag(result.workspace.operational_revision)
    _private(response)
    return result


@router.get(
    "/platform/workspaces/{workspace_id}/health",
    response_model=PlatformWorkspaceHealthResponse,
    response_model_exclude_none=True,
)
async def platform_workspace_health(
    workspace_id: str, request: Request, response: Response
) -> PlatformWorkspaceHealthResponse:
    _platform_read_boundary(request)
    result = await _run(
        get_platform_workspace_health,
        _config(request),
        session_token=_required_platform_cookie(request),
        workspace_id=workspace_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post(
    "/platform/workspaces/{workspace_id}/suspend",
    response_model=PlatformWorkspaceMutationResponse,
    response_model_exclude_none=True,
)
async def suspend_platform_workspace(
    workspace_id: str, request: Request, response: Response
) -> PlatformWorkspaceMutationResponse:
    credentials = await _authorize_workspace_write(request, "lifecycle")
    expected_revision = _if_match(request)
    idempotency_key = _idempotency_key(request)
    payload = await _strict_json(request, required_fields=("reason_code", "confirmed"))
    if payload["confirmed"] is not True:
        raise ApiError("platform.request_invalid")
    result = await _run(
        suspend_workspace,
        _config(request),
        session_token=credentials[0],
        csrf_token=credentials[1],
        workspace_id=workspace_id,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        reason_code=_string(payload, "reason_code", 2, 80),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = _etag(result.workspace.operational_revision)
    _private(response)
    return result


@router.post(
    "/platform/workspaces/{workspace_id}/restore",
    response_model=PlatformWorkspaceMutationResponse,
    response_model_exclude_none=True,
)
async def restore_platform_workspace(
    workspace_id: str, request: Request, response: Response
) -> PlatformWorkspaceMutationResponse:
    credentials = await _authorize_workspace_write(request, "lifecycle")
    expected_revision = _if_match(request)
    idempotency_key = _idempotency_key(request)
    payload = await _strict_json(request, required_fields=("reason_code", "confirmed"))
    if payload["confirmed"] is not True:
        raise ApiError("platform.request_invalid")
    result = await _run(
        restore_workspace,
        _config(request),
        session_token=credentials[0],
        csrf_token=credentials[1],
        workspace_id=workspace_id,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        reason_code=_string(payload, "reason_code", 2, 80),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = _etag(result.workspace.operational_revision)
    _private(response)
    return result


@router.post(
    "/platform/workspaces/{workspace_id}/risk-tags",
    response_model=PlatformWorkspaceMutationResponse,
    response_model_exclude_none=True,
)
async def add_platform_workspace_risk_tag(
    workspace_id: str, request: Request, response: Response
) -> PlatformWorkspaceMutationResponse:
    credentials = await _authorize_workspace_write(request, "risk")
    expected_revision = _if_match(request)
    idempotency_key = _idempotency_key(request)
    payload = await _strict_json(request, required_fields=("tag_code", "reason_code"))
    result = await _run(
        add_workspace_risk_tag,
        _config(request),
        session_token=credentials[0],
        csrf_token=credentials[1],
        workspace_id=workspace_id,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        tag_code=_string(payload, "tag_code", 2, 40),
        reason_code=_string(payload, "reason_code", 2, 80),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = _etag(result.workspace.risk_revision)
    _private(response)
    return result


@router.delete(
    "/platform/workspaces/{workspace_id}/risk-tags/{tag_code}",
    response_model=PlatformWorkspaceMutationResponse,
    response_model_exclude_none=True,
)
async def remove_platform_workspace_risk_tag(
    workspace_id: str, tag_code: str, request: Request, response: Response
) -> PlatformWorkspaceMutationResponse:
    credentials = await _authorize_workspace_write(request, "risk")
    expected_revision = _if_match(request)
    idempotency_key = _idempotency_key(request)
    payload = await _strict_json(request, required_fields=("reason_code",))
    result = await _run(
        remove_workspace_risk_tag,
        _config(request),
        session_token=credentials[0],
        csrf_token=credentials[1],
        workspace_id=workspace_id,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        tag_code=tag_code,
        reason_code=_string(payload, "reason_code", 2, 80),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = _etag(result.workspace.risk_revision)
    _private(response)
    return result


@router.post(
    "/platform/workspaces/{workspace_id}/owner-notifications",
    response_model=OwnerNotificationResponse,
)
async def create_workspace_owner_notification(
    workspace_id: str, request: Request, response: Response
) -> OwnerNotificationResponse:
    credentials = await _authorize_workspace_write(request, "contact")
    expected_revision = _if_match(request)
    idempotency_key = _idempotency_key(request)
    payload = await _strict_json(request, required_fields=("template_code", "reason_code"))
    result = await _run(
        notify_workspace_owner,
        _config(request),
        session_token=credentials[0],
        csrf_token=credentials[1],
        workspace_id=workspace_id,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        template_code=_string(payload, "template_code", 2, 80),
        reason_code=_string(payload, "reason_code", 2, 80),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.get(
    "/platform/workspaces/{workspace_id}/events",
    response_model=WorkspaceOperationalEventListResponse,
)
async def platform_workspace_events(
    workspace_id: str, request: Request, response: Response
) -> WorkspaceOperationalEventListResponse:
    _platform_read_boundary(request, allowed_query=("limit",))
    result = await _run(
        list_workspace_events,
        _config(request),
        session_token=_required_platform_cookie(request),
        workspace_id=workspace_id,
        limit=_limit_only(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post("/platform/support-grants", response_model=SupportGrantResponse, status_code=201)
async def request_support_grant(request: Request, response: Response) -> SupportGrantResponse:
    _platform_write_boundary(request)
    payload = await _strict_json(
        request, required_fields=("workspace_id", "reason_code", "duration_minutes")
    )
    duration = payload["duration_minutes"]
    if type(duration) is not int:
        raise ApiError("platform.request_invalid")
    result = await _run(
        create_support_grant,
        _config(request),
        session_token=_required_platform_cookie(request),
        csrf_token=_required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid"),
        workspace_id=_string(payload, "workspace_id", 36, 36),
        reason_code=_reason(payload, _SUPPORT_REQUEST_REASONS),
        duration_minutes=duration,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.get("/platform/support-grants", response_model=SupportGrantListResponse)
async def support_grants(request: Request, response: Response) -> SupportGrantListResponse:
    _platform_read_boundary(request, allowed_query=("limit", "offset"))
    limit, offset = _pagination(request)
    result = await _run(
        list_support_grants,
        _config(request),
        session_token=_required_platform_cookie(request),
        limit=limit,
        offset=offset,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post("/platform/support-grants/{grant_id}/approve", response_model=SupportGrantResponse)
async def approve_grant_platform(
    grant_id: str, request: Request, response: Response
) -> SupportGrantResponse:
    _platform_write_boundary(request)
    await _strict_json(request, required_fields=())
    result = await _run(
        approve_support_grant_platform,
        _config(request),
        session_token=_required_platform_cookie(request),
        csrf_token=_required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid"),
        grant_id=grant_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post("/workspace/support-grants/{grant_id}/approve", response_model=SupportGrantResponse)
async def approve_grant_workspace(
    grant_id: str, request: Request, response: Response
) -> SupportGrantResponse:
    _workspace_write_boundary(request)
    await _strict_json(request, required_fields=())
    result = await _run(
        approve_support_grant_workspace,
        _config(request),
        workspace_session_token=_required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        workspace_csrf_token=_required_csrf(
            request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid"
        ),
        grant_id=grant_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post("/platform/support-grants/{grant_id}/revoke", response_model=SupportGrantResponse)
async def revoke_grant_platform(
    grant_id: str, request: Request, response: Response
) -> SupportGrantResponse:
    _platform_write_boundary(request)
    payload = await _strict_json(request, required_fields=("reason_code",))
    result = await _run(
        revoke_support_grant_platform,
        _config(request),
        session_token=_required_platform_cookie(request),
        csrf_token=_required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid"),
        grant_id=grant_id,
        reason_code=_reason(payload, _SUPPORT_REVOKE_REASONS),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post("/workspace/support-grants/{grant_id}/revoke", response_model=SupportGrantResponse)
async def revoke_grant_workspace(
    grant_id: str, request: Request, response: Response
) -> SupportGrantResponse:
    _workspace_write_boundary(request)
    payload = await _strict_json(request, required_fields=("reason_code",))
    result = await _run(
        revoke_support_grant_workspace,
        _config(request),
        workspace_session_token=_required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        workspace_csrf_token=_required_csrf(
            request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid"
        ),
        grant_id=grant_id,
        reason_code=_reason(payload, _SUPPORT_REVOKE_REASONS),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post("/platform/support-grants/{grant_id}/sessions", response_model=SupportSessionResponse)
async def create_support_session(
    grant_id: str, request: Request, response: Response
) -> SupportSessionResponse:
    _platform_write_boundary(request)
    await _strict_json(request, required_fields=())
    issued = await _run(
        issue_support_session,
        _config(request),
        session_token=_required_platform_cookie(request),
        csrf_token=_required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid"),
        grant_id=grant_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _set_support_cookie(response, _config(request), issued.session_token)
    _private(response)
    return issued.response


@router.post(
    "/platform/break-glass-sessions", response_model=BreakGlassSessionResponse, status_code=201
)
async def create_break_glass(request: Request, response: Response) -> BreakGlassSessionResponse:
    _platform_write_boundary(request)
    payload = await _strict_json(
        request, required_fields=("workspace_id", "reason_code", "totp_code", "confirmed")
    )
    if payload["confirmed"] is not True:
        raise ApiError("platform.request_invalid")
    issued = await _run(
        create_break_glass_session,
        _config(request),
        session_token=_required_platform_cookie(request),
        csrf_token=_required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid"),
        workspace_id=_string(payload, "workspace_id", 36, 36),
        reason_code=_reason(payload, _BREAK_GLASS_REASONS),
        totp_code=_totp(payload),
        device_fingerprint=_device_fingerprint(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _set_support_cookie(response, _config(request), issued.session_token)
    _private(response)
    return issued.response


@router.post(
    "/platform/break-glass-sessions/{break_glass_id}/reviews",
    response_model=BreakGlassReviewResponse,
)
async def review_break_glass_as_platform(
    break_glass_id: str, request: Request, response: Response
) -> BreakGlassReviewResponse:
    _platform_write_boundary(request)
    await _strict_json(request, required_fields=())
    result = await _run(
        review_break_glass_platform,
        _config(request),
        session_token=_required_platform_cookie(request),
        csrf_token=_required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid"),
        break_glass_id=break_glass_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post(
    "/workspace/break-glass-sessions/{break_glass_id}/reviews",
    response_model=BreakGlassReviewResponse,
)
async def review_break_glass_as_workspace(
    break_glass_id: str, request: Request, response: Response
) -> BreakGlassReviewResponse:
    _workspace_write_boundary(request)
    await _strict_json(request, required_fields=())
    result = await _run(
        review_break_glass_workspace,
        _config(request),
        workspace_session_token=_required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        workspace_csrf_token=_required_csrf(
            request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid"
        ),
        break_glass_id=break_glass_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.get("/support/session", response_model=SupportSessionResponse)
async def support_session(request: Request, response: Response) -> SupportSessionResponse:
    _support_read_boundary(request)
    result = await _run(
        get_support_session,
        _config(request),
        session_token=_required_support_cookie(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.get("/support/workspace/diagnostics", response_model=SupportDiagnosticsResponse)
async def support_diagnostics(request: Request, response: Response) -> SupportDiagnosticsResponse:
    _support_read_boundary(request)
    result = await _run(
        get_support_diagnostics,
        _config(request),
        session_token=_required_support_cookie(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.get("/platform/audit-logs", response_model=PlatformAuditListResponse)
async def audit_logs(request: Request, response: Response) -> PlatformAuditListResponse:
    _platform_read_boundary(request, allowed_query=("limit", "cursor"))
    limit, cursor = _cursor_pagination(request)
    result = await _run(
        list_platform_audit,
        _config(request),
        session_token=_required_platform_cookie(request),
        limit=limit,
        cursor=cursor,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


async def _run(function, *args, **kwargs):
    def run_in_scope():
        with database_request_scope(reuse_connections=True):
            return function(*args, **kwargs)

    return await anyio.to_thread.run_sync(run_in_scope, limiter=_WORK_LIMITER)


async def _authorize_workspace_write(
    request: Request,
    capability: Literal["lifecycle", "risk", "contact"],
) -> tuple[str, str]:
    _platform_write_boundary(request)
    session_token = _required_platform_cookie(request)
    csrf_token = _required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid")
    await _run(
        authorize_workspace_write,
        _config(request),
        session_token=session_token,
        csrf_token=csrf_token,
        capability=capability,
    )
    return session_token, csrf_token


async def _strict_json(
    request: Request,
    *,
    required_fields: tuple[str, ...],
    optional_fields: tuple[str, ...] = (),
) -> dict[str, Any]:
    content_types = _raw_headers(request, b"content-type")
    if len(content_types) != 1 or content_types[0].strip().lower() != "application/json":
        raise ApiError("platform.request_invalid")
    lengths = _raw_headers(request, b"content-length")
    if len(lengths) > 1:
        raise ApiError("platform.request_invalid")
    if lengths:
        try:
            if not 0 <= int(lengths[0]) <= _MAX_BODY_BYTES:
                raise ApiError("platform.request_invalid")
        except ValueError:
            raise ApiError("platform.request_invalid") from None
    body = await request.body()
    if not body or len(body) > _MAX_BODY_BYTES:
        raise ApiError("platform.request_invalid")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError
            value[key] = item
        return value

    try:
        payload = json.loads(body.decode("utf-8"), object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ApiError("platform.request_invalid") from None
    required = set(required_fields)
    allowed = required | set(optional_fields)
    actual = set(payload) if type(payload) is dict else set()
    if type(payload) is not dict or not required.issubset(actual) or not actual <= allowed:
        raise ApiError("platform.request_invalid")
    return payload


def _platform_read_boundary(request: Request, allowed_query: Iterable[str] = ()) -> None:
    _reject_authorization(request)
    _reject_query(request, allowed_query)


def _platform_write_boundary(request: Request) -> None:
    _reject_authorization(request)
    _require_write_origin(request)
    _reject_query(request, ())


def _workspace_write_boundary(request: Request) -> None:
    _reject_authorization(request)
    _require_write_origin(request)
    _reject_query(request, ())


def _support_read_boundary(request: Request) -> None:
    _reject_authorization(request)
    _reject_query(request, ())


def _reject_authorization(request: Request) -> None:
    if _raw_headers(request, b"authorization"):
        raise ApiError("platform.credential_conflict")


def _require_write_origin(request: Request) -> None:
    origins = _raw_headers(request, b"origin")
    if len(origins) != 1 or origins[0] not in _config(request).allowed_origins or "," in origins[0]:
        raise ApiError("platform.origin_invalid")
    sites = _raw_headers(request, b"sec-fetch-site")
    if len(sites) > 1 or (sites and sites[0].lower() == "cross-site"):
        raise ApiError("platform.origin_invalid")


def _required_platform_cookie(request: Request) -> str:
    return _required_cookie(request, PLATFORM_SESSION_COOKIE, "platform.session_required")


def _required_support_cookie(request: Request) -> str:
    return _required_cookie(request, SUPPORT_SESSION_COOKIE, "support.session_required")


def _required_cookie(request: Request, name: str, missing_code: str) -> str:
    values = _cookie_values(request, name)
    if not values:
        raise ApiError(missing_code)
    if len(values) != 1 or not valid_opaque_value(values[0]):
        raise ApiError(
            "support.session_invalid"
            if name == SUPPORT_SESSION_COOKIE
            else "platform.session_invalid"
            if name == PLATFORM_SESSION_COOKIE
            else missing_code
        )
    return values[0]


def _required_csrf(request: Request, cookie_name: str, error_code: str) -> str:
    cookies = _cookie_values(request, cookie_name)
    headers = _raw_headers(request, b"x-csrf-token")
    if (
        len(cookies) != 1
        or len(headers) != 1
        or not valid_opaque_value(cookies[0])
        or not valid_opaque_value(headers[0])
        or not hmac_compare(cookies[0], headers[0])
    ):
        raise ApiError(error_code)
    return cookies[0]


def _cookie_values(request: Request, name: str) -> list[str]:
    values: list[str] = []
    for raw in _raw_headers(request, b"cookie"):
        for segment in raw.split(";"):
            key, separator, value = segment.strip().partition("=")
            if separator and key == name:
                values.append(value)
    return values


def _string(payload: dict[str, Any], field: str, minimum: int, maximum: int) -> str:
    value = payload.get(field)
    if type(value) is not str:
        raise ApiError("platform.request_invalid")
    try:
        length = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ApiError("platform.request_invalid") from None
    if length < minimum or length > maximum:
        raise ApiError("platform.request_invalid")
    return value


def _totp(payload: dict[str, Any]) -> str:
    value = _string(payload, "totp_code", 6, 6)
    if not value.isascii() or not value.isdigit():
        raise ApiError("platform.request_invalid")
    return value


def _reason(payload: dict[str, Any], allowed: frozenset[str]) -> str:
    value = _string(payload, "reason_code", 2, 80)
    if _REASON_RE.fullmatch(value) is None or value not in allowed:
        raise ApiError("platform.request_invalid")
    return value


def _roles(payload: dict[str, Any]) -> tuple[str, ...]:
    value = payload.get("roles")
    if (
        type(value) is not list
        or not 1 <= len(value) <= 13
        or any(type(role) is not str for role in value)
    ):
        raise ApiError("platform.request_invalid")
    return tuple(value)


def _if_match(request: Request) -> int:
    values = _raw_headers(request, b"if-match")
    if len(values) != 1 or re.fullmatch(r'"[1-9][0-9]*"', values[0]) is None:
        raise ApiError("platform.revision_invalid")
    return int(values[0][1:-1])


def _pagination(request: Request) -> tuple[int, int]:
    items = list(request.query_params.multi_items())
    if any(key not in {"limit", "offset"} for key, _ in items):
        raise ApiError("platform.request_invalid")
    if sum(key == "limit" for key, _ in items) > 1 or sum(key == "offset" for key, _ in items) > 1:
        raise ApiError("platform.request_invalid")
    try:
        limit = int(request.query_params.get("limit", "20"))
        offset = int(request.query_params.get("offset", "0"))
    except ValueError:
        raise ApiError("platform.request_invalid") from None
    if not 1 <= limit <= 100 or offset < 0:
        raise ApiError("platform.request_invalid")
    return limit, offset


def _cursor_pagination(request: Request) -> tuple[int, str | None]:
    items = list(request.query_params.multi_items())
    if any(key not in {"limit", "cursor"} for key, _ in items):
        raise ApiError("platform.request_invalid")
    if sum(key == "limit" for key, _ in items) > 1 or sum(key == "cursor" for key, _ in items) > 1:
        raise ApiError("platform.request_invalid")
    try:
        limit = int(request.query_params.get("limit", "20"))
    except ValueError:
        raise ApiError("platform.request_invalid") from None
    cursor = request.query_params.get("cursor")
    if not 1 <= limit <= 100 or cursor == "" or (cursor is not None and len(cursor) > 2048):
        raise ApiError("platform.request_invalid")
    return limit, cursor


def _workspace_pagination(request: Request) -> tuple[int, str | None, dict[str, str]]:
    items = list(request.query_params.multi_items())
    if any(sum(item_key == key for item_key, _ in items) > 1 for key in request.query_params):
        raise ApiError("platform.request_invalid")
    try:
        limit = int(request.query_params.get("limit", "20"))
    except ValueError:
        raise ApiError("platform.request_invalid") from None
    cursor = request.query_params.get("cursor")
    if not 1 <= limit <= 100 or cursor == "" or (cursor is not None and len(cursor) > 2048):
        raise ApiError("platform.request_invalid")
    filters = {
        key: value
        for key, value in items
        if key not in {"limit", "cursor"} and key in _WORKSPACE_FILTERS
    }
    return limit, cursor, filters


def _limit_only(request: Request) -> int:
    items = list(request.query_params.multi_items())
    if sum(key == "limit" for key, _ in items) > 1:
        raise ApiError("platform.request_invalid")
    try:
        limit = int(request.query_params.get("limit", "20"))
    except ValueError:
        raise ApiError("platform.request_invalid") from None
    if not 1 <= limit <= 100:
        raise ApiError("platform.request_invalid")
    return limit


def _idempotency_key(request: Request) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY_RE.fullmatch(values[0]) is None:
        raise ApiError("platform.request_invalid")
    return values[0]


def _etag(revision: int) -> str:
    return f'"{revision}"'


def _reject_query(request: Request, allowed: Iterable[str]) -> None:
    allowed_set = set(allowed)
    if any(key not in allowed_set for key, _ in request.query_params.multi_items()):
        raise ApiError("platform.request_invalid")


def _set_platform_cookies(
    response: Response, config: AppConfig, session_token: str, csrf_token: str
) -> None:
    response.set_cookie(
        PLATFORM_SESSION_COOKIE,
        session_token,
        max_age=config.platform_session_ttl_seconds,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    response.set_cookie(
        PLATFORM_CSRF_COOKIE,
        csrf_token,
        max_age=config.platform_session_ttl_seconds,
        path="/",
        secure=True,
        httponly=False,
        samesite="strict",
    )


def _set_support_cookie(response: Response, config: AppConfig, session_token: str) -> None:
    response.set_cookie(
        SUPPORT_SESSION_COOKIE,
        session_token,
        max_age=config.support_session_ttl_seconds,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def _clear_workspace_and_support(response: Response) -> None:
    response.delete_cookie(
        WORKSPACE_SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax"
    )
    response.delete_cookie(
        WORKSPACE_CSRF_COOKIE, path="/", secure=True, httponly=False, samesite="strict"
    )
    response.delete_cookie(
        SUPPORT_SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="lax"
    )


def _private(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, private"


def _device_fingerprint(request: Request) -> str:
    return (
        (request.headers.get("User-Agent") or "unknown")
        + "|"
        + (request.client.host if request.client else "unknown")
    )


def _raw_headers(request: Request, name: bytes) -> list[str]:
    return [
        value.decode("latin-1")
        for header_name, value in request.scope.get("headers", ())
        if header_name.lower() == name
    ]


def hmac_compare(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left, right)


def _config(request: Request) -> AppConfig:
    return request.app.state.config


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", "")
