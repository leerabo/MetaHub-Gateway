"""Workspace and Platform HTTP boundaries for the PRD-119 provider catalog."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from metahub_shared.db.connection import database_request_scope
from metahub_shared.runtime.errors import ApiError

from metahub_control.auth import AuthContext, require_workspace_or_legacy_permissions
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
    CatalogProviderCreateRequest,
    CatalogProviderCreateResponse,
    CompatibilityResponse,
    PlatformCatalogPublishRequest,
    PlatformCatalogPublishResponse,
    PlatformCatalogResponse,
    ProviderCatalogResponse,
    ProviderPreflightRequest,
    ProviderPreflightResponse,
)
from .service import (
    create_catalog_provider,
    get_catalog_compatibility,
    get_platform_catalog,
    list_provider_catalog,
    preflight_catalog_provider,
    publish_platform_catalog,
)


async def provider_catalog_database_scope() -> AsyncIterator[None]:
    """Reuse connections only for operations without an upstream network phase."""

    with database_request_scope(reuse_connections=True):
        yield


router = APIRouter(tags=["provider-catalog"])
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")


@router.get(
    "/provider-catalog", response_model=ProviderCatalogResponse,
    dependencies=[Depends(provider_catalog_database_scope)],
)
async def get_provider_catalog(
    request: Request,
    response: Response,
    context: Annotated[
        AuthContext,
        Depends(require_workspace_or_legacy_permissions("provider_catalog.read")),
    ],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    vendor_code: str | None = None,
    capability_code: str | None = None,
    region_code: str | None = None,
) -> ProviderCatalogResponse:
    result = list_provider_catalog(
        request.app.state.config,
        context,
        status=status_filter,
        vendor_code=vendor_code,
        capability_code=capability_code,
        region_code=region_code,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post(
    "/providers/from-catalog",
    response_model=CatalogProviderCreateResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(provider_catalog_database_scope)],
)
async def post_provider_from_catalog(
    payload: Annotated[CatalogProviderCreateRequest, Body()],
    request: Request,
    response: Response,
    context: Annotated[
        AuthContext,
        Depends(require_workspace_or_legacy_permissions("provider.write")),
    ],
) -> CatalogProviderCreateResponse:
    result = create_catalog_provider(
        request.app.state.config,
        context,
        payload,
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.instance.revision}"'
    _private(response)
    return result


@router.post(
    "/providers/{provider_id}/preflight",
    response_model=ProviderPreflightResponse,
)
async def post_provider_preflight(
    provider_id: str,
    payload: Annotated[ProviderPreflightRequest, Body()],
    request: Request,
    response: Response,
    context: Annotated[
        AuthContext,
        Depends(require_workspace_or_legacy_permissions("provider.write")),
    ],
) -> ProviderPreflightResponse:
    result = await run_in_threadpool(
        preflight_catalog_provider,
        request.app.state.config,
        context,
        provider_id,
        payload,
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.get(
    "/provider-catalog/{entry_code}/compatibility",
    response_model=CompatibilityResponse,
    dependencies=[Depends(provider_catalog_database_scope)],
)
async def get_provider_compatibility(
    entry_code: str,
    request: Request,
    response: Response,
    context: Annotated[
        AuthContext,
        Depends(require_workspace_or_legacy_permissions("provider_catalog.read")),
    ],
) -> CompatibilityResponse:
    _reject_query(request)
    result = get_catalog_compatibility(
        request.app.state.config,
        context,
        entry_code,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


def require_platform_provider_catalog(request: Request) -> PlatformContext:
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
    permitted = (
        "platform.provider_catalog.manage" in context.permissions
        if request.method not in {"GET", "HEAD", "OPTIONS"}
        else bool(
            {
                "platform.provider_catalog.manage",
                "platform.provider_catalog.security_read",
            }.intersection(context.permissions)
        )
    )
    if not permitted:
        raise ApiError("platform.permission_denied")
    return context


@router.get(
    "/platform/provider-catalog", response_model=PlatformCatalogResponse,
    dependencies=[Depends(provider_catalog_database_scope)],
)
async def get_platform_provider_catalog(
    request: Request,
    response: Response,
    context: Annotated[PlatformContext, Depends(require_platform_provider_catalog)],
) -> PlatformCatalogResponse:
    _reject_query(request)
    result = get_platform_catalog(
        request.app.state.config,
        context,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.current.generation}"'
    _private(response)
    return result


@router.post(
    "/platform/provider-catalog/versions",
    response_model=PlatformCatalogPublishResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(provider_catalog_database_scope)],
)
async def post_platform_provider_catalog_version(
    request: Request,
    response: Response,
    context: Annotated[PlatformContext, Depends(require_platform_provider_catalog)],
) -> PlatformCatalogPublishResponse:
    _reject_query(request)
    payload = _model(
        await _strict_json(request, "provider_catalog.request_invalid"),
        PlatformCatalogPublishRequest,
    )
    result = publish_platform_catalog(
        request.app.state.config,
        context,
        payload,
        expected_revision=_if_match(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.catalog.generation}"'
    _private(response)
    return result


def _model(payload: dict[str, Any], model_type):
    try:
        return model_type.model_validate(payload)
    except ValueError:
        raise ApiError("provider_catalog.request_invalid") from None


def _if_match(request: Request) -> int:
    values = _raw_headers(request, b"if-match")
    if len(values) != 1 or re.fullmatch(r'"[1-9][0-9]*"', values[0]) is None:
        raise ApiError("provider_catalog.request_invalid")
    return int(values[0][1:-1])


def _idempotency_key(request: Request) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY_RE.fullmatch(values[0]) is None:
        raise ApiError("provider_catalog.request_invalid")
    return values[0]


def _reject_query(request: Request) -> None:
    if request.query_params.multi_items():
        raise ApiError("provider_catalog.request_invalid")


__all__ = ["router"]
