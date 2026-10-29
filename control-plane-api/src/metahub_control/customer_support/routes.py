"""Credential-isolated HTTP boundary for PRD-132 customer support."""

from __future__ import annotations

import re
from dataclasses import dataclass

from fastapi import APIRouter, Request, Response, status
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import clear_workspace_cookies_on_error
from starlette.concurrency import run_in_threadpool

from metahub_control.identity.service import valid_opaque_value
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
    SUPPORT_SESSION_COOKIE,
)
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
from metahub_control.workspace_access.constants import (
    WORKSPACE_CSRF_COOKIE,
    WORKSPACE_SESSION_COOKIE,
)

from .models import (
    AttachmentCreateRequest,
    AttachmentMutationResponse,
    EscalationCreateRequest,
    EscalationMutationResponse,
    TicketCreateRequest,
    TicketListResponse,
    TicketMutationResponse,
    TicketPatchRequest,
)
from .service import (
    create_attachment,
    create_escalation,
    create_ticket,
    list_tickets,
    patch_ticket,
)

router = APIRouter(prefix="/support/tickets", tags=["customer-support"])
_TICKET_ID = re.compile(r"^tkt_[0-9a-f]{32}$")
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_WORKSPACE_ID = re.compile(r"^wsp_[0-9a-f]{32}$")
_STATUSES = frozenset(
    {"open", "triaged", "waiting_customer", "waiting_platform", "resolved", "closed"}
)
_SEVERITIES = frozenset({"p1", "p2", "p3", "p4"})


@dataclass(frozen=True, slots=True)
class _Credentials:
    domain: str
    workspace_token: str | None = None
    platform_token: str | None = None
    workspace_csrf: str | None = None
    platform_csrf: str | None = None


@router.get("", response_model=TicketListResponse)
async def get_support_tickets(request: Request, response: Response) -> TicketListResponse:
    credentials = _credentials(request, write=False)
    _reject_body(request)
    query = _list_query(request)
    result = await run_in_threadpool(
        list_tickets,
        request.app.state.config,
        workspace_session_token=credentials.workspace_token,
        platform_session_token=credentials.platform_token,
        limit=query["limit"],
        status_filter=query["status"],
        severity_filter=query["severity"],
        workspace_filter=query["workspace_id"],
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post("", response_model=TicketMutationResponse, status_code=status.HTTP_201_CREATED)
async def post_support_ticket(request: Request, response: Response) -> TicketMutationResponse:
    credentials = _credentials(request, write=True)
    if credentials.domain != "workspace":
        raise ApiError("support.permission_denied")
    _reject_query(request)
    payload = _model(await _strict_json(request, "support.request_invalid"), TicketCreateRequest)
    result = await run_in_threadpool(
        create_ticket,
        request.app.state.config,
        payload,
        session_token=credentials.workspace_token,
        csrf_token=credentials.workspace_csrf,
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = status.HTTP_200_OK
    response.headers["ETag"] = f'"{result.ticket.revision}"'
    _private(response)
    return result


@router.patch("/{ticket_id}", response_model=TicketMutationResponse)
async def patch_support_ticket(
    ticket_id: str, request: Request, response: Response
) -> TicketMutationResponse:
    checked_id = _ticket_id(ticket_id)
    credentials = _credentials(request, write=True)
    _reject_query(request)
    payload = _model(await _strict_json(request, "support.request_invalid"), TicketPatchRequest)
    result = await run_in_threadpool(
        patch_ticket,
        request.app.state.config,
        checked_id,
        payload,
        workspace_session_token=credentials.workspace_token,
        platform_session_token=credentials.platform_token,
        workspace_csrf_token=credentials.workspace_csrf,
        platform_csrf_token=credentials.platform_csrf,
        expected_revision=_if_match(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.ticket.revision}"'
    _private(response)
    return result


@router.post(
    "/{ticket_id}/attachments",
    response_model=AttachmentMutationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_support_attachment(
    ticket_id: str, request: Request, response: Response
) -> AttachmentMutationResponse:
    checked_id = _ticket_id(ticket_id)
    credentials = _credentials(request, write=True)
    if credentials.domain != "workspace":
        raise ApiError("support.permission_denied")
    _reject_query(request)
    payload = _model(
        await _strict_json(request, "support.request_invalid"), AttachmentCreateRequest
    )
    result = await run_in_threadpool(
        create_attachment,
        request.app.state.config,
        checked_id,
        payload,
        session_token=credentials.workspace_token,
        csrf_token=credentials.workspace_csrf,
        expected_revision=_if_match(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = status.HTTP_200_OK
    response.headers["ETag"] = f'"{result.ticket_revision}"'
    _private(response)
    return result


@router.post(
    "/{ticket_id}/escalations",
    response_model=EscalationMutationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_support_escalation(
    ticket_id: str, request: Request, response: Response
) -> EscalationMutationResponse:
    checked_id = _ticket_id(ticket_id)
    credentials = _credentials(request, write=True)
    _reject_query(request)
    payload = _model(
        await _strict_json(request, "support.request_invalid"), EscalationCreateRequest
    )
    result = await run_in_threadpool(
        create_escalation,
        request.app.state.config,
        checked_id,
        payload,
        workspace_session_token=credentials.workspace_token,
        platform_session_token=credentials.platform_token,
        workspace_csrf_token=credentials.workspace_csrf,
        platform_csrf_token=credentials.platform_csrf,
        expected_revision=_if_match(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = status.HTTP_200_OK
    response.headers["ETag"] = f'"{result.ticket_revision}"'
    _private(response)
    return result


def _credentials(request: Request, *, write: bool) -> _Credentials:
    clear_workspace_cookies_on_error(request)
    if _raw_headers(request, b"authorization") or _cookie_values(request, SUPPORT_SESSION_COOKIE):
        raise ApiError("workspace_session.credential_conflict")
    workspace = _cookie_values(request, WORKSPACE_SESSION_COOKIE)
    platform = _cookie_values(request, PLATFORM_SESSION_COOKIE)
    if len(workspace) + len(platform) != 1:
        raise ApiError(
            "workspace_session.required"
            if not workspace and not platform
            else "workspace_session.credential_conflict"
        )
    domain = "workspace" if workspace else "platform"
    token = workspace[0] if workspace else platform[0]
    if not valid_opaque_value(token):
        raise ApiError("workspace_session.invalid" if workspace else "platform.session_invalid")
    if not write:
        return _Credentials(
            domain=domain,
            workspace_token=token if workspace else None,
            platform_token=token if platform else None,
        )
    _require_origin(request, "support.origin_invalid")
    if workspace:
        csrf = _required_csrf(request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid")
        return _Credentials(domain=domain, workspace_token=token, workspace_csrf=csrf)
    csrf = _required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid")
    return _Credentials(domain=domain, platform_token=token, platform_csrf=csrf)


def _list_query(request: Request) -> dict[str, object]:
    allowed = {"limit", "status", "severity", "workspace_id"}
    items = request.query_params.multi_items()
    if any(key not in allowed for key, _value in items) or len({key for key, _ in items}) != len(
        items
    ):
        raise ApiError("support.request_invalid")
    try:
        limit = int(request.query_params.get("limit", "50"))
    except ValueError:
        raise ApiError("support.request_invalid") from None
    status_filter = request.query_params.get("status")
    severity_filter = request.query_params.get("severity")
    workspace_filter = request.query_params.get("workspace_id")
    if (
        not 1 <= limit <= 100
        or (status_filter is not None and status_filter not in _STATUSES)
        or (severity_filter is not None and severity_filter not in _SEVERITIES)
        or (workspace_filter is not None and _WORKSPACE_ID.fullmatch(workspace_filter) is None)
    ):
        raise ApiError("support.request_invalid")
    return {
        "limit": limit,
        "status": status_filter,
        "severity": severity_filter,
        "workspace_id": workspace_filter,
    }


def _ticket_id(value: str) -> str:
    if _TICKET_ID.fullmatch(value) is None:
        raise ApiError("support.not_found")
    return value


def _model(payload: object, model_type):
    try:
        return model_type.model_validate(payload)
    except ValueError:
        raise ApiError("support.request_invalid") from None


def _idempotency_key(request: Request) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY.fullmatch(values[0]) is None:
        raise ApiError("support.request_invalid")
    return values[0]


def _if_match(request: Request) -> int:
    values = _raw_headers(request, b"if-match")
    if len(values) != 1 or re.fullmatch(r'"[1-9][0-9]*"', values[0]) is None:
        raise ApiError("support.revision_required")
    return int(values[0][1:-1])


def _reject_query(request: Request) -> None:
    if request.query_params.multi_items():
        raise ApiError("support.request_invalid")


def _reject_body(request: Request) -> None:
    if _raw_headers(request, b"transfer-encoding") or _raw_headers(request, b"content-type"):
        raise ApiError("support.request_invalid")
    lengths = _raw_headers(request, b"content-length")
    if len(lengths) > 1 or (lengths and lengths[0] != "0"):
        raise ApiError("support.request_invalid")


__all__ = ["router"]
