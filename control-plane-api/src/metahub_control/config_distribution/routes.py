"""Credential-isolated platform and internal transports for configuration distribution."""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response
from metahub_shared.runtime.errors import ApiError

from metahub_control.auth import require_internal_service_token
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
from metahub_control.workspace_access.constants import WORKSPACE_SESSION_COOKIE

from .models import (
    ConfigAckRequest,
    ConfigAckResponse,
    ConfigDistributionResponse,
    ConfigRebuildRequest,
    ConfigRebuildResponse,
)
from .service import (
    acknowledge_internal_config,
    build_internal_config_response,
    create_config_rebuild,
    list_config_distribution,
)

router = APIRouter(tags=["config-distribution"])
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_STRONG_ETAG_RE = re.compile(r'^"[0-9a-f]{64}"$')


def require_platform_config_distribution(request: Request) -> PlatformContext:
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
        "platform.config_distribution.read"
        if request.method == "GET"
        else "platform.config_distribution.rebuild"
    )
    if permission not in context.permissions:
        raise ApiError("platform.permission_denied")
    return context


def require_gateway_transport(
    request: Request,
    _: Annotated[None, Depends(require_internal_service_token)],
    gateway_identity: Annotated[str | None, Header(alias="X-Gateway-Identity")] = None,
) -> str:
    if (
        _raw_headers(request, b"authorization")
        or _raw_headers(request, b"cookie")
        or _cookie_values(request, PLATFORM_SESSION_COOKIE)
        or _cookie_values(request, WORKSPACE_SESSION_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
        or gateway_identity is None
    ):
        raise ApiError("config_distribution.gateway_identity_invalid")
    return gateway_identity


@router.get("/platform/config-distribution", response_model=ConfigDistributionResponse)
async def platform_config_distribution(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_config_distribution)],
) -> ConfigDistributionResponse:
    if request.query_params.multi_items():
        raise ApiError("config_distribution.request_invalid")
    result = list_config_distribution(
        request.app.state.config,
        session_token=_required_cookie(
            request, PLATFORM_SESSION_COOKIE, "platform.session_required"
        ),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post("/platform/config-rebuilds", response_model=ConfigRebuildResponse, status_code=201)
async def platform_config_rebuild(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_config_distribution)],
) -> ConfigRebuildResponse:
    if request.query_params.multi_items():
        raise ApiError("config_distribution.request_invalid")
    try:
        payload = ConfigRebuildRequest.model_validate(
            await _strict_json(request, "config_distribution.request_invalid")
        )
    except ValueError:
        raise ApiError("config_distribution.request_invalid") from None
    result = create_config_rebuild(
        request.app.state.config,
        session_token=_required_cookie(
            request, PLATFORM_SESSION_COOKIE, "platform.session_required"
        ),
        csrf_token=_required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid"),
        payload=payload,
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = 200
    _private(response)
    return result


@router.get("/internal/config-snapshots/{workspace_id}")
async def internal_config_snapshot(
    workspace_id: str,
    request: Request,
    gateway_identity: Annotated[str, Depends(require_gateway_transport)],
) -> Response:
    return build_internal_config_response(
        request.app.state.config,
        workspace_id=workspace_id,
        gateway_identity=gateway_identity,
        after_generation=_after_generation(request),
        if_none_match=_if_none_match(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post(
    "/internal/config-snapshots/{snapshot_id}/acks",
    response_model=ConfigAckResponse,
)
async def internal_config_ack(
    snapshot_id: str,
    request: Request,
    response: Response,
    gateway_identity: Annotated[str, Depends(require_gateway_transport)],
) -> ConfigAckResponse:
    if request.query_params.multi_items():
        raise ApiError("config_distribution.request_invalid")
    try:
        payload = ConfigAckRequest.model_validate(
            await _strict_json(request, "config_distribution.request_invalid")
        )
    except ValueError:
        raise ApiError("config_distribution.request_invalid") from None
    result = acknowledge_internal_config(
        request.app.state.config,
        snapshot_id=snapshot_id,
        gateway_identity=gateway_identity,
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


def _idempotency_key(request: Request) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY_RE.fullmatch(values[0]) is None:
        raise ApiError("config_distribution.request_invalid")
    return values[0]


def _after_generation(request: Request) -> int | None:
    values = request.query_params.getlist("after_generation")
    if any(key != "after_generation" for key, _ in request.query_params.multi_items()):
        raise ApiError("config_distribution.request_invalid")
    if not values:
        return None
    if len(values) != 1 or re.fullmatch(r"0|[1-9][0-9]{0,18}", values[0]) is None:
        raise ApiError("config_distribution.request_invalid")
    return int(values[0])


def _if_none_match(request: Request) -> str | None:
    values = _raw_headers(request, b"if-none-match")
    if not values:
        return None
    if len(values) != 1 or _STRONG_ETAG_RE.fullmatch(values[0]) is None:
        raise ApiError("config_distribution.request_invalid")
    return values[0]


__all__ = ["router"]
