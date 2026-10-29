"""Strict HTTP boundaries for PRD-142 brand ownership domains."""

from __future__ import annotations

import json
import re
import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import clear_workspace_cookies_on_error

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
    ChannelBrandCommand,
    ChannelBrandResponse,
    DeploymentBrandCommand,
    DeploymentBrandResponse,
    PlatformBrandCommand,
    PlatformBrandResponse,
    PublicBrandResponse,
)
from .service import (
    canonicalize_request_host,
    get_channel_brand,
    get_deployment_brand,
    get_platform_brand,
    get_public_asset,
    get_public_brand,
    mutate_channel_brand,
    mutate_deployment_brand,
    mutate_platform_brand,
)

router = APIRouter(tags=["brand-domains"])
_MAX_BODY_BYTES = 704_000
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_IF_MATCH = re.compile(r'^"([1-9][0-9]*)"$')


def require_platform_brand(request: Request) -> PlatformContext:
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, WORKSPACE_SESSION_COOKIE)
        or _cookie_values(request, WORKSPACE_CSRF_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("platform.credential_conflict")
    csrf = None
    if request.method == "PATCH":
        _require_origin(request, "platform.origin_invalid")
        csrf = _required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid")
    context = authenticate_platform_session_context(
        request.app.state.config,
        _required_cookie(request, PLATFORM_SESSION_COOKIE, "platform.session_required"),
        csrf_token=csrf,
    )
    path = getattr(request.scope.get("route"), "path", request.url.path)
    accepted = (
        {"platform.brand.read"} if path == "/platform/branding" else {"platform.channel_brand.read"}
    )
    if request.method == "PATCH":
        accepted |= (
            {"platform.brand.manage", "platform.brand.release"}
            if path == "/platform/branding"
            else {"platform.channel_brand.manage", "platform.channel_brand.release"}
        )
    if not accepted.intersection(context.permissions):
        raise ApiError("platform.permission_denied")
    return context


def require_deployment_brand(request: Request) -> WorkspaceSessionContext:
    clear_workspace_cookies_on_error(request)
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, PLATFORM_SESSION_COOKIE)
        or _cookie_values(request, PLATFORM_CSRF_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("workspace_session.credential_conflict")
    csrf = None
    if request.method == "PATCH":
        _require_origin(request, "branding.request_invalid")
        csrf = _required_csrf(request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid")
    context = authenticate_workspace_session_context(
        request.app.state.config,
        _required_cookie(request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"),
        csrf_token=csrf,
    )
    required = (
        "deployment.branding.manage" if request.method == "PATCH" else "deployment.branding.read"
    )
    if required not in context.permission_codes:
        raise ApiError("workspace_session.permission_denied")
    return context


@router.get("/platform/branding", response_model=PlatformBrandResponse)
async def get_platform_brand_route(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_brand)],
) -> PlatformBrandResponse:
    _reject_query_and_body(request)
    result = get_platform_brand(
        request.app.state.config,
        session_token=_platform_token(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    response.headers["ETag"] = _etag(result.branding.revision)
    return result


@router.patch("/platform/branding", response_model=PlatformBrandResponse)
async def patch_platform_brand_route(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_brand)],
) -> PlatformBrandResponse:
    _reject_query(request)
    command = _model(await _strict_json(request), PlatformBrandCommand)
    result = mutate_platform_brand(
        request.app.state.config,
        command,
        session_token=_platform_token(request),
        csrf_token=_platform_csrf(request),
        step_up_grant=_step_up(request),
        idempotency_key=_idempotency_key(request),
        expected_revision=_if_match(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    response.headers["ETag"] = _etag(result.branding.revision)
    return result


@router.get(
    "/platform/channel-partners/{channel_partner_id}/branding",
    response_model=ChannelBrandResponse,
)
async def get_channel_brand_route(
    channel_partner_id: str,
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_brand)],
) -> ChannelBrandResponse:
    _reject_query_and_body(request)
    result = get_channel_brand(
        request.app.state.config,
        session_token=_platform_token(request),
        channel_partner_id=channel_partner_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    response.headers["ETag"] = _etag(result.branding.revision)
    return result


@router.patch(
    "/platform/channel-partners/{channel_partner_id}/branding",
    response_model=ChannelBrandResponse,
)
async def patch_channel_brand_route(
    channel_partner_id: str,
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_brand)],
) -> ChannelBrandResponse:
    _reject_query(request)
    command = _model(await _strict_json(request), ChannelBrandCommand)
    result = mutate_channel_brand(
        request.app.state.config,
        command,
        session_token=_platform_token(request),
        csrf_token=_platform_csrf(request),
        step_up_grant=_step_up(request),
        idempotency_key=_idempotency_key(request),
        expected_revision=_if_match(request),
        channel_partner_id=channel_partner_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    response.headers["ETag"] = _etag(result.branding.revision)
    return result


@router.get("/deployments/{edge_deployment_id}/branding", response_model=DeploymentBrandResponse)
async def get_deployment_brand_route(
    edge_deployment_id: str,
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_deployment_brand)],
) -> DeploymentBrandResponse:
    _reject_query_and_body(request)
    result = get_deployment_brand(
        request.app.state.config,
        session_token=_workspace_token(request),
        edge_deployment_id=edge_deployment_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    response.headers["ETag"] = _etag(result.branding.revision)
    return result


@router.patch("/deployments/{edge_deployment_id}/branding", response_model=DeploymentBrandResponse)
async def patch_deployment_brand_route(
    edge_deployment_id: str,
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_deployment_brand)],
) -> DeploymentBrandResponse:
    _reject_query(request)
    command = _model(await _strict_json(request), DeploymentBrandCommand)
    result = mutate_deployment_brand(
        request.app.state.config,
        command,
        session_token=_workspace_token(request),
        csrf_token=_workspace_csrf(request),
        step_up_grant=_step_up(request),
        idempotency_key=_idempotency_key(request, platform=False),
        expected_revision=_if_match(request),
        edge_deployment_id=edge_deployment_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    response.headers["ETag"] = _etag(result.branding.revision)
    return result


@router.get("/branding/public", response_model=PublicBrandResponse)
async def get_public_brand_route(request: Request, response: Response) -> PublicBrandResponse:
    _reject_query_and_body(request)
    result = get_public_brand(
        request.app.state.config,
        canonical_host=_host(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["ETag"] = result.branding.public_etag
    return result


@router.get("/branding/public/assets/{asset_id}")
async def get_public_brand_asset_route(asset_id: str, request: Request) -> Response:
    _reject_query_and_body(request)
    asset = get_public_asset(
        request.app.state.config, canonical_host=_host(request), asset_id=asset_id
    )
    return Response(
        content=asset.body,
        media_type="image/png",
        headers={
            "Content-Length": str(len(asset.body)),
            "ETag": asset.etag,
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
        },
    )


async def _strict_json(request: Request) -> dict[str, Any]:
    content_types = _raw_headers(request, b"content-type")
    if len(content_types) != 1:
        raise ApiError("branding.request_invalid")
    media_type, separator, parameters = content_types[0].partition(";")
    if media_type.strip().lower() != "application/json" or (
        separator and parameters.strip().lower() not in {"charset=utf-8", 'charset="utf-8"'}
    ):
        raise ApiError("branding.request_invalid")
    if _raw_headers(request, b"transfer-encoding"):
        raise ApiError("branding.request_invalid")
    lengths = _raw_headers(request, b"content-length")
    if len(lengths) > 1:
        raise ApiError("branding.request_invalid")
    declared = None
    if lengths:
        if not lengths[0].isdigit() or (len(lengths[0]) > 1 and lengths[0].startswith("0")):
            raise ApiError("branding.request_invalid")
        declared = int(lengths[0])
        if not 2 <= declared <= _MAX_BODY_BYTES:
            raise ApiError("branding.request_invalid")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_BODY_BYTES:
            raise ApiError("branding.request_invalid")
    if not body or (declared is not None and declared != len(body)):
        raise ApiError("branding.request_invalid")

    def unique_object(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        payload = json.loads(bytes(body).decode("utf-8"), object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise ApiError("branding.request_invalid") from None
    if type(payload) is not dict:
        raise ApiError("branding.request_invalid")
    return payload


def _model(payload: dict[str, Any], model_type):
    try:
        return model_type.model_validate(payload)
    except ValueError:
        raise ApiError("branding.request_invalid") from None


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


def _if_match(request: Request) -> int:
    values = _raw_headers(request, b"if-match")
    match = _IF_MATCH.fullmatch(values[0]) if len(values) == 1 else None
    if match is None:
        raise ApiError("branding.revision_conflict")
    return int(match.group(1))


def _idempotency_key(request: Request, *, platform: bool = True) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY.fullmatch(values[0]) is None:
        raise ApiError(
            "platform.idempotency_conflict" if platform else "workspace.idempotency_conflict"
        )
    return values[0]


def _step_up(request: Request) -> str:
    values = _raw_headers(request, b"x-step-up-grant")
    if len(values) != 1 or not valid_opaque_value(values[0]):
        raise ApiError("step_up.invalid")
    return values[0]


def _platform_token(request: Request) -> str:
    return _required_cookie(request, PLATFORM_SESSION_COOKIE, "platform.session_required")


def _platform_csrf(request: Request) -> str:
    return _required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid")


def _workspace_token(request: Request) -> str:
    return _required_cookie(request, WORKSPACE_SESSION_COOKIE, "workspace_session.required")


def _workspace_csrf(request: Request) -> str:
    return _required_csrf(request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid")


def _host(request: Request) -> str:
    values = _raw_headers(request, b"host")
    if len(values) != 1:
        raise ApiError("branding.request_invalid")
    # Forwarded and X-Forwarded-Host are deliberately ignored here. A trusted proxy
    # must replace the canonical Host before the request reaches this application.
    return canonicalize_request_host(values[0])


def _reject_query(request: Request) -> None:
    if request.query_params.multi_items():
        raise ApiError("branding.request_invalid")


def _reject_query_and_body(request: Request) -> None:
    _reject_query(request)
    if _raw_headers(request, b"transfer-encoding") or _raw_headers(request, b"content-type"):
        raise ApiError("branding.request_invalid")
    lengths = _raw_headers(request, b"content-length")
    if len(lengths) > 1 or (lengths and lengths[0] != "0"):
        raise ApiError("branding.request_invalid")


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


def _private(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, private"


def _etag(revision: int) -> str:
    return f'"{revision}"'


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", "")


__all__ = ["router"]
