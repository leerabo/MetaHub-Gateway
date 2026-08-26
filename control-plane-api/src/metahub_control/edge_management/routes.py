"""Credential-separated HTTP boundaries for PRD-120 Edge identity."""

from __future__ import annotations

import re
from functools import partial
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Depends, Request, Response, status
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import clear_workspace_cookies_on_error

from metahub_control.machine_identity.transport import verified_mtls_fingerprint
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

from .models import (
    EdgeAgentRegistrationRequest,
    EdgeCertificateResponse,
    EdgeCertificateRotationRequest,
    EdgeDeploymentListResponse,
    EdgeDeploymentRevokeRequest,
    EdgeDeploymentRevokeResponse,
    EdgeSessionCreateRequest,
    EdgeSessionResponse,
    PlatformEdgeFleetResponse,
    WorkspaceRegistrationCreateRequest,
    WorkspaceRegistrationResponse,
)
from .service import (
    create_edge_session,
    create_workspace_registration,
    list_platform_edge_fleet,
    list_workspace_deployments,
    register_edge_agent,
    revoke_edge_deployment,
    rotate_edge_certificate,
)

router = APIRouter(tags=["edge-management"])
_WORK_LIMITER = anyio.CapacityLimiter(4)
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_UNTRUSTED_CERT_HEADERS = frozenset(
    {
        b"ssl-client-cert",
        b"x-client-cert",
        b"x-client-cert-sha256",
        b"x-forwarded-client-cert",
        b"x-mtls-client-cert",
        b"x-ssl-client-cert",
    }
)


@router.get("/edge/deployments", response_model=EdgeDeploymentListResponse)
async def get_edge_deployments(request: Request, response: Response) -> EdgeDeploymentListResponse:
    _prepare_workspace(request, write=False)
    _reject_query(request)
    result = await _run(
        list_workspace_deployments,
        request.app.state.config,
        session_token=_workspace_cookie(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post(
    "/edge/registrations",
    response_model=WorkspaceRegistrationResponse | EdgeCertificateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_edge_registration(
    request: Request, response: Response
) -> WorkspaceRegistrationResponse | EdgeCertificateResponse:
    _reject_query(request)
    raw = await _strict_json(request, "edge.request_invalid")
    if _cookie_values(request, WORKSPACE_SESSION_COOKIE):
        _prepare_workspace(request, write=True)
        payload = _model(raw, WorkspaceRegistrationCreateRequest)
        result = await _run(
            create_workspace_registration,
            request.app.state.config,
            session_token=_workspace_cookie(request),
            csrf_token=_workspace_csrf(request),
            idempotency_key=_idempotency_key(request),
            payload=payload,
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
        response.headers["ETag"] = f'"{result.deployment.revision}"'
    else:
        _prepare_agent(request, require_mtls=False)
        payload = _model(raw, EdgeAgentRegistrationRequest)
        result = await _run(
            register_edge_agent,
            request.app.state.config,
            payload,
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
    _edge_private(response)
    return result


@router.post("/edge/certificates", response_model=EdgeCertificateResponse)
async def post_edge_certificate(
    request: Request, response: Response
) -> EdgeCertificateResponse:
    _reject_query(request)
    fingerprint = _prepare_agent(request, require_mtls=True)
    payload = _model(
        await _strict_json(request, "edge.request_invalid"),
        EdgeCertificateRotationRequest,
    )
    result = await _run(
        rotate_edge_certificate,
        request.app.state.config,
        payload,
        verified_fingerprint=fingerprint,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _edge_private(response)
    return result


@router.post(
    "/edge/sessions",
    response_model=EdgeSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_edge_session(request: Request, response: Response) -> EdgeSessionResponse:
    _reject_query(request)
    fingerprint = _prepare_agent(request, require_mtls=True)
    payload = _model(
        await _strict_json(request, "edge.request_invalid"), EdgeSessionCreateRequest
    )
    result = await _run(
        create_edge_session,
        request.app.state.config,
        payload,
        verified_fingerprint=fingerprint,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _edge_private(response)
    return result


@router.post(
    "/edge/deployments/{deployment_id}/revoke",
    response_model=EdgeDeploymentRevokeResponse,
)
async def post_edge_deployment_revoke(
    deployment_id: str, request: Request, response: Response
) -> EdgeDeploymentRevokeResponse:
    _prepare_workspace(request, write=True)
    _reject_query(request)
    payload = _model(
        await _strict_json(request, "edge.request_invalid"), EdgeDeploymentRevokeRequest
    )
    result = await _run(
        revoke_edge_deployment,
        request.app.state.config,
        deployment_id,
        payload,
        session_token=_workspace_cookie(request),
        csrf_token=_workspace_csrf(request),
        expected_revision=_if_match(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.deployment.revision}"'
    _edge_private(response)
    return result


def require_platform_edge_fleet(request: Request) -> PlatformContext:
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, WORKSPACE_SESSION_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("platform.credential_conflict")
    context = authenticate_platform_session_context(
        request.app.state.config,
        _required_cookie(request, PLATFORM_SESSION_COOKIE, "platform.session_required"),
        csrf_token=None,
    )
    if not {
        "platform.edge_fleet.read",
        "platform.edge_fleet.manage",
    }.intersection(context.permissions):
        raise ApiError("platform.permission_denied")
    return context


@router.get(
    "/platform/fleet/edge-deployments",
    response_model=PlatformEdgeFleetResponse,
)
async def get_platform_edge_fleet(
    request: Request,
    response: Response,
    context: Annotated[PlatformContext, Depends(require_platform_edge_fleet)],
) -> PlatformEdgeFleetResponse:
    _reject_query(request)
    result = await _run(
        list_platform_edge_fleet,
        request.app.state.config,
        context,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


def _prepare_workspace(request: Request, *, write: bool) -> None:
    clear_workspace_cookies_on_error(request)
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, PLATFORM_SESSION_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("workspace_session.credential_conflict")
    _workspace_cookie(request)
    if write:
        _require_origin(request, "edge.request_invalid")
        _workspace_csrf(request)


def _prepare_agent(request: Request, *, require_mtls: bool) -> str:
    if _raw_headers(request, b"authorization") or _raw_headers(request, b"cookie"):
        raise ApiError("edge.authentication_failed")
    if any(
        name.lower() in _UNTRUSTED_CERT_HEADERS
        for name, _value in request.scope.get("headers", ())
    ):
        raise ApiError("edge.authentication_failed")
    fingerprint = verified_mtls_fingerprint(request)
    if require_mtls and fingerprint is None:
        raise ApiError("edge.authentication_failed")
    return fingerprint or ""


def _workspace_cookie(request: Request) -> str:
    return _required_cookie(request, WORKSPACE_SESSION_COOKIE, "workspace_session.required")


def _workspace_csrf(request: Request) -> str:
    return _required_csrf(
        request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid"
    )


def _if_match(request: Request) -> int:
    values = _raw_headers(request, b"if-match")
    if len(values) != 1 or re.fullmatch(r'"[1-9][0-9]*"', values[0]) is None:
        raise ApiError("edge.request_invalid")
    return int(values[0][1:-1])


def _idempotency_key(request: Request) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY_RE.fullmatch(values[0]) is None:
        raise ApiError("edge.request_invalid")
    return values[0]


def _model(payload: dict[str, Any], model_type):
    try:
        return model_type.model_validate(payload)
    except ValueError:
        raise ApiError("edge.request_invalid") from None


def _reject_query(request: Request) -> None:
    if request.query_params.multi_items():
        raise ApiError("edge.request_invalid")


def _edge_private(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"


async def _run(function, /, *args, **kwargs):
    return await anyio.to_thread.run_sync(
        partial(function, *args, **kwargs), limiter=_WORK_LIMITER
    )


__all__ = ["router"]
