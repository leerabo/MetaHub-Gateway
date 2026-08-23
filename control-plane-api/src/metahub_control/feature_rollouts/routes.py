"""Strict PlatformSession and WorkspaceSession HTTP boundaries for feature flags."""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import clear_workspace_cookies_on_error

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
from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    authenticate_workspace_session_context,
)

from .models import (
    FeatureFlagCreateRequest,
    FeatureFlagListResponse,
    FeatureFlagResponse,
    FeatureKillRequest,
    FeatureRolloutCommandRequest,
    FeatureRolloutResponse,
    WorkspaceCapabilitiesResponse,
)
from .service import (
    command_feature_rollout,
    create_feature_flag,
    get_workspace_capabilities,
    kill_feature_flag,
    list_feature_flags,
)

router = APIRouter(tags=["feature-rollouts"])
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")


def require_platform_feature(request: Request) -> PlatformContext:
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
    if request.method == "GET":
        allowed_permissions = {"platform.feature.read"}
    elif request.url.path.endswith("/kill"):
        allowed_permissions = {"platform.feature.kill"}
    elif request.url.path.endswith("/rollouts"):
        allowed_permissions = {"platform.feature.write", "platform.feature.approve"}
    else:
        allowed_permissions = {"platform.feature.write"}
    if allowed_permissions.isdisjoint(context.permissions):
        raise ApiError("platform.permission_denied")
    return context


def require_workspace_feature(request: Request) -> WorkspaceSessionContext:
    clear_workspace_cookies_on_error(request)
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, PLATFORM_SESSION_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("workspace_session.credential_conflict")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        _required_csrf(request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid")
    return authenticate_workspace_session_context(
        request.app.state.config,
        _required_cookie(request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"),
    )


@router.get("/platform/feature-flags", response_model=FeatureFlagListResponse)
async def feature_flags(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_feature)],
) -> FeatureFlagListResponse:
    status, risk_level, limit = _list_query(request)
    result = list_feature_flags(
        request.app.state.config,
        session_token=_platform_cookie(request),
        status=status,
        risk_level=risk_level,
        limit=limit,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post("/platform/feature-flags", response_model=FeatureFlagResponse)
async def post_feature_flag(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_feature)],
) -> FeatureFlagResponse:
    _reject_query(request)
    payload = await _model(request, FeatureFlagCreateRequest)
    result = create_feature_flag(
        request.app.state.config,
        session_token=_platform_cookie(request),
        csrf_token=_platform_csrf(request),
        expected_revision=_optional_if_match(
            request, required=payload.action == "version"
        ),
        idempotency_key=_idempotency_key(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.flag.revision}"'
    _private(response)
    return result


@router.post(
    "/platform/feature-flags/{flag_id}/rollouts",
    response_model=FeatureRolloutResponse,
)
async def post_feature_rollout(
    flag_id: str,
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_feature)],
) -> FeatureRolloutResponse:
    _reject_query(request)
    payload = await _model(request, FeatureRolloutCommandRequest)
    result = command_feature_rollout(
        request.app.state.config,
        session_token=_platform_cookie(request),
        csrf_token=_platform_csrf(request),
        flag_id=flag_id,
        expected_revision=_if_match(request),
        idempotency_key=_idempotency_key(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.rollout.revision}"'
    _private(response)
    return result


@router.post("/platform/feature-flags/{flag_id}/kill", response_model=FeatureFlagResponse)
async def post_feature_kill(
    flag_id: str,
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_feature)],
) -> FeatureFlagResponse:
    _reject_query(request)
    payload = await _model(request, FeatureKillRequest)
    result = kill_feature_flag(
        request.app.state.config,
        session_token=_platform_cookie(request),
        csrf_token=_platform_csrf(request),
        flag_id=flag_id,
        expected_revision=_if_match(request),
        idempotency_key=_idempotency_key(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.flag.revision}"'
    _private(response)
    return result


@router.get(
    "/workspaces/current/capabilities",
    response_model=WorkspaceCapabilitiesResponse,
)
async def workspace_capabilities(
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_feature)],
) -> WorkspaceCapabilitiesResponse:
    _reject_query(request)
    result = get_workspace_capabilities(
        request.app.state.config,
        session_token=_required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


async def _model(request: Request, model_type):
    try:
        return model_type.model_validate(
            await _strict_json(request, "feature_flag.request_invalid")
        )
    except ValueError:
        raise ApiError("feature_flag.request_invalid") from None


def _list_query(request: Request) -> tuple[str | None, str | None, int]:
    items = request.query_params.multi_items()
    if len(items) != len({key for key, _value in items}):
        raise ApiError("feature_flag.request_invalid")
    if any(key not in {"status", "risk_level", "limit"} for key, _value in items):
        raise ApiError("feature_flag.request_invalid")
    status = request.query_params.get("status")
    risk_level = request.query_params.get("risk_level")
    if status not in {None, "active", "killed", "disabled"}:
        raise ApiError("feature_flag.request_invalid")
    if risk_level not in {None, "standard", "p0"}:
        raise ApiError("feature_flag.request_invalid")
    raw_limit = request.query_params.get("limit", "50")
    if re.fullmatch(r"[1-9][0-9]{0,2}", raw_limit) is None or int(raw_limit) > 100:
        raise ApiError("feature_flag.request_invalid")
    return status, risk_level, int(raw_limit)


def _if_match(request: Request) -> int:
    values = _raw_headers(request, b"if-match")
    if len(values) != 1 or re.fullmatch(r'"[1-9][0-9]*"', values[0]) is None:
        raise ApiError("feature_flag.request_invalid")
    return int(values[0][1:-1])


def _optional_if_match(request: Request, *, required: bool) -> int | None:
    values = _raw_headers(request, b"if-match")
    if not values and not required:
        return None
    if len(values) != 1 or re.fullmatch(r'"[1-9][0-9]*"', values[0]) is None:
        raise ApiError("feature_flag.request_invalid")
    return int(values[0][1:-1])


def _idempotency_key(request: Request) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY_RE.fullmatch(values[0]) is None:
        raise ApiError("feature_flag.request_invalid")
    return values[0]


def _reject_query(request: Request) -> None:
    if request.query_params.multi_items():
        raise ApiError("feature_flag.request_invalid")


def _platform_cookie(request: Request) -> str:
    return _required_cookie(request, PLATFORM_SESSION_COOKIE, "platform.session_required")


def _platform_csrf(request: Request) -> str:
    return _required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid")


__all__ = ["require_platform_feature", "require_workspace_feature", "router"]
