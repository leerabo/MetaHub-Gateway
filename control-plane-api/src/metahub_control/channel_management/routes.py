"""Strict Platform and Partner HTTP boundaries for PRD-139."""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from metahub_shared.runtime.errors import ApiError

from metahub_control.identity.service import (
    CSRF_COOKIE as GLOBAL_CSRF_COOKIE,
)
from metahub_control.identity.service import (
    SESSION_COOKIE as GLOBAL_SESSION_COOKIE,
)
from metahub_control.identity.service import (
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
from metahub_control.workspace_access.constants import (
    WORKSPACE_CSRF_COOKIE,
    WORKSPACE_SESSION_COOKIE,
)

from .models import (
    AttributionApprovalCommand,
    AttributionRequestCommand,
    AttributionResponse,
    ChannelPartnerListResponse,
    ChannelPartnerResponse,
    LeadCommand,
    LeadResponse,
    PartnerCommand,
    PartnerWorkspaceListResponse,
    StatementListResponse,
)
from .service import (
    apply_partner_command,
    approve_workspace_attribution,
    list_channel_partners,
    list_channel_statements,
    list_partner_workspaces,
    register_channel_lead,
    request_workspace_attribution,
)

router = APIRouter(tags=["channel-management"])
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_IF_MATCH = re.compile(r'"(0|[1-9][0-9]*)"')
_PLATFORM_PERMISSION_BY_ROUTE = {
    ("GET", "/platform/channel-partners"): "platform.channel.read",
    ("POST", "/platform/channel-partners"): "platform.channel.manage",
    ("POST", "/platform/channel-leads"): "platform.channel.lead.register",
    ("POST", "/platform/workspace-attributions"): (
        "platform.channel.attribution.request"
    ),
    ("POST", "/platform/workspace-attributions/{attribution_id}/approve"): (
        "platform.channel.attribution.approve"
    ),
    ("GET", "/platform/channel-statements"): "platform.channel.statement.read",
}


def require_platform_channel(request: Request) -> PlatformContext:
    """Authenticate the isolated Platform principal before request-body parsing."""

    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, WORKSPACE_SESSION_COOKIE)
        or _cookie_values(request, WORKSPACE_CSRF_COOKIE)
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
    route = request.scope.get("route")
    route_path = getattr(route, "path", request.url.path)
    permission = _PLATFORM_PERMISSION_BY_ROUTE.get((request.method, route_path))
    if permission is None or permission not in context.permissions:
        raise ApiError("platform.permission_denied")
    return context


@router.get("/platform/channel-partners", response_model=ChannelPartnerListResponse)
async def get_channel_partners(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_channel)],
) -> ChannelPartnerListResponse:
    query = _query(request, {"status", "tier_code", "region_scope", "limit", "offset"})
    _reject_body(request)
    limit, offset = _pagination(query)
    result = list_channel_partners(
        request.app.state.config,
        platform_session_token=_platform_token(request),
        status=query.get("status"),
        tier_code=query.get("tier_code"),
        region_scope=query.get("region_scope"),
        limit=limit,
        offset=offset,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post("/platform/channel-partners", response_model=ChannelPartnerResponse)
async def post_channel_partner(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_channel)],
) -> ChannelPartnerResponse:
    _query(request, set())
    command = _model(await _strict_json(request, "channel.request_invalid"), PartnerCommand)
    expected = _if_match(request)
    command_revision = command.expected_revision or 0
    if expected != command_revision:
        raise ApiError("channel.revision_conflict")
    result = apply_partner_command(
        request.app.state.config,
        command,
        platform_session_token=_platform_token(request),
        platform_csrf_token=_platform_csrf(request),
        step_up_grant=_step_up_grant(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if command.operation == "create" and not result.idempotent_replay:
        response.status_code = 201
    response.headers["ETag"] = _etag(result.partner.revision)
    _private(response)
    return result


@router.post("/platform/channel-leads", response_model=LeadResponse, status_code=201)
async def post_channel_lead(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_channel)],
) -> LeadResponse:
    _query(request, set())
    command = _model(await _strict_json(request, "channel.request_invalid"), LeadCommand)
    if _if_match(request) != command.expected_revision:
        raise ApiError("channel.revision_conflict")
    result = register_channel_lead(
        request.app.state.config,
        command,
        platform_session_token=_platform_token(request),
        platform_csrf_token=_platform_csrf(request),
        step_up_grant=_step_up_grant(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = 200
    response.headers["ETag"] = _etag(result.lead.revision)
    _private(response)
    return result


@router.post(
    "/platform/workspace-attributions",
    response_model=AttributionResponse,
    status_code=201,
)
async def post_workspace_attribution(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_channel)],
) -> AttributionResponse:
    _query(request, set())
    command = _model(
        await _strict_json(request, "channel.request_invalid"),
        AttributionRequestCommand,
    )
    if _if_match(request) != command.expected_attribution_revision:
        raise ApiError("channel.revision_conflict")
    result = request_workspace_attribution(
        request.app.state.config,
        command,
        platform_session_token=_platform_token(request),
        platform_csrf_token=_platform_csrf(request),
        step_up_grant=_step_up_grant(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = 200
    response.headers["ETag"] = _etag(result.attribution.revision)
    _private(response)
    return result


@router.post(
    "/platform/workspace-attributions/{attribution_id}/approve",
    response_model=AttributionResponse,
)
async def post_workspace_attribution_approval(
    attribution_id: str,
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_channel)],
) -> AttributionResponse:
    _query(request, set())
    command = _model(
        await _strict_json(request, "channel.request_invalid"),
        AttributionApprovalCommand,
    )
    if _if_match(request) != command.expected_revision:
        raise ApiError("channel.revision_conflict")
    result = approve_workspace_attribution(
        request.app.state.config,
        attribution_id,
        command,
        platform_session_token=_platform_token(request),
        platform_csrf_token=_platform_csrf(request),
        step_up_grant=_step_up_grant(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = _etag(result.attribution.revision)
    _private(response)
    return result


@router.get("/platform/channel-statements", response_model=StatementListResponse)
async def get_channel_statements(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_channel)],
) -> StatementListResponse:
    query = _query(request, {"channel_partner_id", "currency", "limit", "offset"})
    _reject_body(request)
    limit, offset = _pagination(query)
    result = list_channel_statements(
        request.app.state.config,
        platform_session_token=_platform_token(request),
        channel_partner_id=query.get("channel_partner_id"),
        currency=query.get("currency"),
        limit=limit,
        offset=offset,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.get("/partner/workspaces", response_model=PartnerWorkspaceListResponse)
async def get_partner_workspaces(
    request: Request,
    response: Response,
) -> PartnerWorkspaceListResponse:
    query = _query(request, {"limit", "offset"})
    _reject_body(request)
    limit, offset = _pagination(query)
    result = list_partner_workspaces(
        request.app.state.config,
        global_session_token=_partner_token(request),
        limit=limit,
        offset=offset,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


def _model(payload: dict[str, Any], model_type):
    try:
        return model_type.model_validate(payload)
    except ValueError:
        raise ApiError("channel.request_invalid") from None


def _platform_token(request: Request) -> str:
    return _required_cookie(request, PLATFORM_SESSION_COOKIE, "platform.session_required")


def _platform_csrf(request: Request) -> str:
    return _required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid")


def _partner_token(request: Request) -> str:
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, PLATFORM_SESSION_COOKIE)
        or _cookie_values(request, PLATFORM_CSRF_COOKIE)
        or _cookie_values(request, WORKSPACE_SESSION_COOKIE)
        or _cookie_values(request, WORKSPACE_CSRF_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("partner.credential_conflict")
    values = _cookie_values(request, GLOBAL_SESSION_COOKIE)
    if len(values) != 1:
        raise ApiError("partner.session_required")
    if not valid_opaque_value(values[0]):
        raise ApiError("partner.session_invalid")
    csrf_values = _cookie_values(request, GLOBAL_CSRF_COOKIE)
    if len(csrf_values) > 1:
        raise ApiError("partner.credential_conflict")
    return values[0]


def _if_match(request: Request) -> int:
    values = _raw_headers(request, b"if-match")
    if len(values) != 1 or _IF_MATCH.fullmatch(values[0]) is None:
        raise ApiError("channel.revision_required")
    return int(values[0][1:-1])


def _idempotency_key(request: Request) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY.fullmatch(values[0]) is None:
        raise ApiError("channel.idempotency_invalid")
    return values[0]


def _step_up_grant(request: Request) -> str:
    values = _raw_headers(request, b"x-step-up-grant")
    if len(values) != 1 or not valid_opaque_value(values[0]):
        raise ApiError("step_up.required")
    return values[0]


def _query(request: Request, allowed: set[str]) -> dict[str, str]:
    items = request.query_params.multi_items()
    keys = [key for key, _value in items]
    if any(key not in allowed for key in keys) or len(keys) != len(set(keys)):
        raise ApiError("channel.request_invalid")
    return dict(items)


def _pagination(query: dict[str, str]) -> tuple[int, int]:
    try:
        limit = int(query.get("limit", "20"))
        offset = int(query.get("offset", "0"))
    except ValueError:
        raise ApiError("channel.request_invalid") from None
    if str(limit) != query.get("limit", str(limit)) or not 1 <= limit <= 100:
        raise ApiError("channel.request_invalid")
    if str(offset) != query.get("offset", str(offset)) or not 0 <= offset <= 1_000_000:
        raise ApiError("channel.request_invalid")
    return limit, offset


def _reject_body(request: Request) -> None:
    if _raw_headers(request, b"transfer-encoding") or _raw_headers(request, b"content-type"):
        raise ApiError("channel.request_invalid")
    lengths = _raw_headers(request, b"content-length")
    if len(lengths) > 1 or (lengths and lengths[0] != "0"):
        raise ApiError("channel.request_invalid")


def _etag(revision: int) -> str:
    return f'"{revision}"'


__all__ = ["router"]
