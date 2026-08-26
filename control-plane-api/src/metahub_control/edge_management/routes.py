"""Credential-separated HTTP boundaries for PRD-120 Edge identity."""

from __future__ import annotations

import json
import re
import zlib
from functools import partial
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Depends, Request, Response, status
from metahub_shared.private_gateway import (
    EdgeTelemetryBatch,
    EdgeTelemetryError,
    EdgeTelemetryReceiptWire,
    canonical_edge_json_bytes,
    reject_forbidden_telemetry_fields,
)
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

from .desired_state_service import (
    acknowledge_agent_desired_state,
    get_workspace_desired_state,
    pull_agent_desired_state,
    rollback_workspace_desired_state,
    write_workspace_desired_state,
)
from .models import (
    EdgeAckCreateRequest,
    EdgeAckResponse,
    EdgeAgentRegistrationRequest,
    EdgeAgentTelemetryWatermarkResponse,
    EdgeCertificateResponse,
    EdgeCertificateRotationRequest,
    EdgeDeploymentListResponse,
    EdgeDeploymentRevokeRequest,
    EdgeDeploymentRevokeResponse,
    EdgeDesiredStateApprovalRequest,
    EdgeDesiredStatePublishRequest,
    EdgeDesiredStatePullResponse,
    EdgeDesiredStateResponse,
    EdgeDesiredStateRollbackRequest,
    EdgeSessionCreateRequest,
    EdgeSessionResponse,
    EdgeTelemetryPolicyPatchRequest,
    EdgeTelemetryPolicyResponse,
    EdgeTelemetryWatermarkResponse,
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
from .telemetry_service import (
    get_agent_telemetry_watermark,
    get_workspace_telemetry_watermark,
    patch_workspace_telemetry_policy,
    receive_agent_telemetry,
)

router = APIRouter(tags=["edge-management"])
_WORK_LIMITER = anyio.CapacityLimiter(4)
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_MAX_TELEMETRY_COMPRESSED_BYTES = 262_144
_MAX_TELEMETRY_BYTES = 1_048_576
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
async def post_edge_certificate(request: Request, response: Response) -> EdgeCertificateResponse:
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
    payload = _model(await _strict_json(request, "edge.request_invalid"), EdgeSessionCreateRequest)
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


@router.get(
    "/edge/deployments/{deployment_id}/desired-state",
    response_model=EdgeDesiredStateResponse | EdgeDesiredStatePullResponse,
)
async def get_edge_desired_state(
    deployment_id: str, request: Request, response: Response
) -> EdgeDesiredStateResponse | EdgeDesiredStatePullResponse | Response:
    _reject_query(request)
    if _cookie_values(request, WORKSPACE_SESSION_COOKIE):
        _prepare_workspace(request, write=False)
        result = await _run(
            get_workspace_desired_state,
            request.app.state.config,
            deployment_id,
            session_token=_workspace_cookie(request),
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
        response.headers["ETag"] = _desired_etag(result)
    else:
        fingerprint = _prepare_agent(request, require_mtls=True)
        result = await _run(
            pull_agent_desired_state,
            request.app.state.config,
            deployment_id,
            verified_fingerprint=fingerprint,
            edge_session_token=_edge_session(request),
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
        response = Response(
            content=canonical_edge_json_bytes(result.model_dump(mode="json"), maximum=786_432),
            media_type="application/json",
        )
        response.headers["ETag"] = f'"{result.envelope_sha256}"'
        _edge_private(response)
        return response
    _edge_private(response)
    return result


@router.post(
    "/edge/deployments/{deployment_id}/desired-state",
    response_model=EdgeDesiredStateResponse,
)
async def post_edge_desired_state(
    deployment_id: str, request: Request, response: Response
) -> EdgeDesiredStateResponse:
    _prepare_workspace(request, write=True)
    _reject_query(request)
    raw = await _strict_json(request, "edge.request_invalid")
    if raw.get("action") == "publish":
        payload = _model(raw, EdgeDesiredStatePublishRequest)
    elif raw.get("action") in {"approve", "reject"}:
        payload = _model(raw, EdgeDesiredStateApprovalRequest)
    else:
        raise ApiError("edge.request_invalid")
    result = await _run(
        write_workspace_desired_state,
        request.app.state.config,
        deployment_id,
        payload,
        session_token=_workspace_cookie(request),
        csrf_token=_workspace_csrf(request),
        expected_revision=_desired_if_match(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = _desired_etag(result)
    _edge_private(response)
    return result


@router.post(
    "/edge/deployments/{deployment_id}/acks",
    response_model=EdgeAckResponse,
)
async def post_edge_ack(
    deployment_id: str, request: Request, response: Response
) -> EdgeAckResponse:
    _reject_query(request)
    fingerprint = _prepare_agent(request, require_mtls=True)
    payload = _model(await _strict_json(request, "edge.request_invalid"), EdgeAckCreateRequest)
    result = await _run(
        acknowledge_agent_desired_state,
        request.app.state.config,
        deployment_id,
        payload,
        verified_fingerprint=fingerprint,
        edge_session_token=_edge_session(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _edge_private(response)
    return result


@router.post(
    "/edge/deployments/{deployment_id}/rollbacks",
    response_model=EdgeDesiredStateResponse,
)
async def post_edge_rollback(
    deployment_id: str, request: Request, response: Response
) -> EdgeDesiredStateResponse:
    _prepare_workspace(request, write=True)
    _reject_query(request)
    payload = _model(
        await _strict_json(request, "edge.request_invalid"),
        EdgeDesiredStateRollbackRequest,
    )
    result = await _run(
        rollback_workspace_desired_state,
        request.app.state.config,
        deployment_id,
        payload,
        session_token=_workspace_cookie(request),
        csrf_token=_workspace_csrf(request),
        expected_revision=_desired_if_match(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = _desired_etag(result)
    _edge_private(response)
    return result


@router.post(
    "/edge/telemetry/batches",
    response_model=EdgeTelemetryReceiptWire,
)
async def post_edge_telemetry_batch(
    request: Request, response: Response
) -> EdgeTelemetryReceiptWire:
    _reject_query(request)
    fingerprint = _prepare_agent(request, require_mtls=True)
    payload = _model(await _strict_telemetry_json(request), EdgeTelemetryBatch)
    result = await _run(
        receive_agent_telemetry,
        request.app.state.config,
        payload,
        verified_fingerprint=fingerprint,
        edge_session_token=_edge_session(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _edge_private(response)
    return result


@router.get(
    "/edge/deployments/{deployment_id}/telemetry-watermark",
    response_model=EdgeTelemetryWatermarkResponse | EdgeAgentTelemetryWatermarkResponse,
)
async def get_edge_telemetry_watermark(
    deployment_id: str, request: Request, response: Response
) -> EdgeTelemetryWatermarkResponse | EdgeAgentTelemetryWatermarkResponse:
    _reject_query(request)
    if _cookie_values(request, WORKSPACE_SESSION_COOKIE):
        _prepare_workspace(request, write=False)
        result = await _run(
            get_workspace_telemetry_watermark,
            request.app.state.config,
            deployment_id,
            session_token=_workspace_cookie(request),
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
    else:
        fingerprint = _prepare_agent(request, require_mtls=True)
        result = await _run(
            get_agent_telemetry_watermark,
            request.app.state.config,
            deployment_id,
            verified_fingerprint=fingerprint,
            edge_session_token=_edge_session(request),
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
    response.headers["ETag"] = f'"{result.policy.revision if result.policy else 0}"'
    _edge_private(response)
    return result


@router.patch(
    "/edge/deployments/{deployment_id}/telemetry-policy",
    response_model=EdgeTelemetryPolicyResponse,
)
async def patch_edge_telemetry_policy(
    deployment_id: str, request: Request, response: Response
) -> EdgeTelemetryPolicyResponse:
    _prepare_workspace(request, write=True)
    _reject_query(request)
    payload = _model(
        await _strict_json(request, "edge.request_invalid"), EdgeTelemetryPolicyPatchRequest
    )
    result = await _run(
        patch_workspace_telemetry_policy,
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
    response.headers["ETag"] = f'"{result.policy.revision}"'
    _edge_private(response)
    return result


def require_platform_edge_fleet(request: Request) -> PlatformContext:
    if (
        _raw_headers(request, b"authorization")
        or _raw_headers(request, b"x-edge-session")
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
        or _raw_headers(request, b"x-edge-session")
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
        name.lower() in _UNTRUSTED_CERT_HEADERS for name, _value in request.scope.get("headers", ())
    ):
        raise ApiError("edge.authentication_failed")
    fingerprint = verified_mtls_fingerprint(request)
    if require_mtls and fingerprint is None:
        raise ApiError("edge.authentication_failed")
    return fingerprint or ""


def _workspace_cookie(request: Request) -> str:
    return _required_cookie(request, WORKSPACE_SESSION_COOKIE, "workspace_session.required")


def _workspace_csrf(request: Request) -> str:
    return _required_csrf(request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid")


def _if_match(request: Request) -> int:
    values = _raw_headers(request, b"if-match")
    if len(values) != 1 or re.fullmatch(r'"[1-9][0-9]*"', values[0]) is None:
        raise ApiError("edge.request_invalid")
    return int(values[0][1:-1])


def _desired_if_match(request: Request) -> int:
    values = _raw_headers(request, b"if-match")
    if len(values) != 1 or re.fullmatch(r'"(?:0|[1-9][0-9]*)"', values[0]) is None:
        raise ApiError("edge.request_invalid")
    return int(values[0][1:-1])


def _edge_session(request: Request) -> str:
    values = _raw_headers(request, b"x-edge-session")
    if len(values) != 1:
        raise ApiError("edge.authentication_failed")
    return values[0]


def _desired_etag(result: EdgeDesiredStateResponse) -> str:
    if result.state is None:
        return '"0"'
    revision = (
        result.state.revision
        if result.state.status == "approval_pending"
        else result.state.head_revision
    )
    return f'"{revision}"'


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


async def _strict_telemetry_json(request: Request) -> dict[str, Any]:
    content_types = _raw_headers(request, b"content-type")
    if len(content_types) != 1 or re.fullmatch(
        r"application/json(?:;\s*charset=utf-8)?", content_types[0], re.IGNORECASE
    ) is None:
        raise ApiError("edge.request_invalid")
    encodings = _raw_headers(request, b"content-encoding")
    if len(encodings) > 1:
        raise ApiError("edge.request_invalid")
    encoding = encodings[0].lower() if encodings else "identity"
    if encoding not in {"gzip", "identity"}:
        raise ApiError("edge.request_invalid")
    maximum_body = (
        _MAX_TELEMETRY_COMPRESSED_BYTES if encoding == "gzip" else _MAX_TELEMETRY_BYTES
    )
    body = await _bounded_body(request, maximum_body)
    try:
        if encoding == "gzip":
            if not body:
                raise ApiError("edge.request_invalid")
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            decoded = decoder.decompress(body, _MAX_TELEMETRY_BYTES + 1)
            if (
                len(decoded) > _MAX_TELEMETRY_BYTES
                or decoder.unconsumed_tail
                or decoder.unused_data
                or not decoder.eof
                or (len(decoded) > 65_536 and len(decoded) > len(body) * 64)
            ):
                raise ApiError("edge.request_invalid")
        elif encoding == "identity":
            if not body:
                raise ApiError("edge.request_invalid")
            decoded = body
        def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in items:
                if key in result:
                    raise ValueError("duplicate")
                result[key] = value
            return result

        def invalid_constant(_value: str) -> object:
            raise ValueError("constant")

        parsed = json.loads(
            decoded.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=invalid_constant,
        )
        if type(parsed) is not dict:
            raise ValueError("object")
        reject_forbidden_telemetry_fields(parsed)
        return parsed
    except ApiError:
        raise
    except (EdgeTelemetryError, RecursionError, UnicodeError, ValueError, zlib.error):
        raise ApiError("edge.request_invalid") from None


async def _bounded_body(request: Request, maximum: int) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > maximum:
            raise ApiError("edge.request_invalid")
        body.extend(chunk)
    return bytes(body)


def _reject_query(request: Request) -> None:
    if request.query_params.multi_items():
        raise ApiError("edge.request_invalid")


def _edge_private(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"


async def _run(function, /, *args, **kwargs):
    return await anyio.to_thread.run_sync(partial(function, *args, **kwargs), limiter=_WORK_LIMITER)


__all__ = ["router"]
