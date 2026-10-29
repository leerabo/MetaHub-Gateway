"""Credential-isolated HTTP boundaries for PRD-117 data governance."""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import clear_workspace_cookies_on_error

from metahub_control.platform_access.constants import (
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
from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    authenticate_workspace_session_context,
)

from .models import (
    DataGovernanceResponse,
    DataGovernanceUpdateRequest,
    LegalHoldCreateRequest,
    LegalHoldReleaseRequest,
    LegalHoldResponse,
    ResidencyEvidenceListResponse,
)
from .service import (
    create_legal_hold,
    get_data_governance,
    list_residency_evidence,
    release_legal_hold,
    update_data_governance,
)

router = APIRouter(tags=["data-governance"])
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_COMPONENTS = frozenset(
    {
        "gateway",
        "database",
        "object_storage",
        "backup",
        "job_runtime",
        "disaster_recovery",
        "tenant_data",
    }
)
_OUTCOMES = frozenset({"compliant", "migration_required", "blocked", "violation"})


def require_workspace_governance(request: Request) -> WorkspaceSessionContext:
    clear_workspace_cookies_on_error(request)
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, PLATFORM_SESSION_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("workspace_session.credential_conflict")
    csrf = None
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        _require_origin(request, "data_governance.request_invalid")
        csrf = _required_csrf(
            request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid"
        )
    return authenticate_workspace_session_context(
        request.app.state.config,
        _required_cookie(request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"),
        csrf_token=csrf,
    )


def require_platform_evidence(request: Request) -> PlatformContext:
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, WORKSPACE_SESSION_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("platform.credential_conflict")
    context = authenticate_platform_session_context(
        request.app.state.config,
        _required_cookie(request, PLATFORM_SESSION_COOKIE, "platform.session_required"),
    )
    if "platform.data_residency.read" not in context.permissions:
        raise ApiError("platform.permission_denied")
    return context


@router.get(
    "/workspaces/{workspace_id}/data-governance",
    response_model=DataGovernanceResponse,
)
async def get_workspace_data_governance(
    workspace_id: str,
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_governance)],
) -> DataGovernanceResponse:
    _reject_query_and_body(request)
    result = get_data_governance(
        request.app.state.config,
        session_token=_workspace_cookie(request),
        workspace_id=workspace_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = _etag(result.policy.revision)
    _private(response)
    return result


@router.patch(
    "/workspaces/{workspace_id}/data-governance",
    response_model=DataGovernanceResponse,
)
async def patch_workspace_data_governance(
    workspace_id: str,
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_governance)],
) -> DataGovernanceResponse:
    _reject_query(request)
    payload = _model(
        await _strict_json(request, "data_governance.request_invalid"),
        DataGovernanceUpdateRequest,
    )
    result = update_data_governance(
        request.app.state.config,
        session_token=_workspace_cookie(request),
        csrf_token=_workspace_csrf(request),
        workspace_id=workspace_id,
        expected_revision=_if_match(request),
        idempotency_key=_idempotency_key(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = _etag(result.policy.revision)
    _private(response)
    return result


@router.post(
    "/workspaces/{workspace_id}/legal-holds",
    response_model=LegalHoldResponse,
    status_code=201,
)
async def post_workspace_legal_hold(
    workspace_id: str,
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_governance)],
) -> LegalHoldResponse:
    _reject_query(request)
    payload = _model(
        await _strict_json(request, "data_governance.request_invalid"),
        LegalHoldCreateRequest,
    )
    result = create_legal_hold(
        request.app.state.config,
        session_token=_workspace_cookie(request),
        csrf_token=_workspace_csrf(request),
        workspace_id=workspace_id,
        idempotency_key=_idempotency_key(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = 200
    response.headers["ETag"] = _etag(result.hold.revision)
    _private(response)
    return result


@router.delete(
    "/workspaces/{workspace_id}/legal-holds/{hold_id}",
    response_model=LegalHoldResponse,
)
async def delete_workspace_legal_hold(
    workspace_id: str,
    hold_id: str,
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_governance)],
) -> LegalHoldResponse:
    _reject_query(request)
    payload = _model(
        await _strict_json(request, "data_governance.request_invalid"),
        LegalHoldReleaseRequest,
    )
    result = release_legal_hold(
        request.app.state.config,
        session_token=_workspace_cookie(request),
        csrf_token=_workspace_csrf(request),
        workspace_id=workspace_id,
        hold_id=hold_id,
        expected_revision=_if_match(request),
        idempotency_key=_idempotency_key(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = _etag(result.hold.revision)
    _private(response)
    return result


@router.get(
    "/platform/data-residency/evidence",
    response_model=ResidencyEvidenceListResponse,
)
async def get_platform_residency_evidence(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_evidence)],
) -> ResidencyEvidenceListResponse:
    _reject_body(request)
    allowed = {"workspace_id", "component", "outcome", "cursor", "limit"}
    items = request.query_params.multi_items()
    if any(key not in allowed for key, _ in items) or len({key for key, _ in items}) != len(
        items
    ):
        raise ApiError("data_governance.request_invalid")
    workspace_id = request.query_params.get("workspace_id")
    component = request.query_params.get("component")
    outcome = request.query_params.get("outcome")
    cursor = request.query_params.get("cursor")
    if component is not None and component not in _COMPONENTS:
        raise ApiError("data_governance.request_invalid")
    if outcome is not None and outcome not in _OUTCOMES:
        raise ApiError("data_governance.request_invalid")
    raw_limit = request.query_params.get("limit", "50")
    if re.fullmatch(r"[1-9][0-9]{0,2}", raw_limit) is None:
        raise ApiError("data_governance.request_invalid")
    result = list_residency_evidence(
        request.app.state.config,
        session_token=_required_cookie(
            request, PLATFORM_SESSION_COOKIE, "platform.session_required"
        ),
        workspace_id=workspace_id,
        component=component,
        outcome=outcome,
        cursor=cursor,
        limit=int(raw_limit),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


def _model(payload: object, model_type):
    try:
        return model_type.model_validate(payload)
    except ValueError:
        raise ApiError("data_governance.request_invalid") from None


def _if_match(request: Request) -> int:
    values = _raw_headers(request, b"if-match")
    if len(values) != 1 or re.fullmatch(r'"[1-9][0-9]*"', values[0]) is None:
        raise ApiError("data_governance.request_invalid")
    return int(values[0][1:-1])


def _idempotency_key(request: Request) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY_RE.fullmatch(values[0]) is None:
        raise ApiError("data_governance.request_invalid")
    return values[0]


def _workspace_cookie(request: Request) -> str:
    return _required_cookie(request, WORKSPACE_SESSION_COOKIE, "workspace_session.required")


def _workspace_csrf(request: Request) -> str:
    return _required_csrf(
        request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid"
    )


def _reject_query(request: Request) -> None:
    if request.query_params.multi_items():
        raise ApiError("data_governance.request_invalid")


def _reject_body(request: Request) -> None:
    if _raw_headers(request, b"transfer-encoding") or _raw_headers(request, b"content-type"):
        raise ApiError("data_governance.request_invalid")
    lengths = _raw_headers(request, b"content-length")
    if len(lengths) > 1 or (lengths and lengths[0] != "0"):
        raise ApiError("data_governance.request_invalid")


def _reject_query_and_body(request: Request) -> None:
    _reject_query(request)
    _reject_body(request)


def _etag(revision: int) -> str:
    return f'"{revision}"'


__all__ = ["require_platform_evidence", "require_workspace_governance", "router"]
