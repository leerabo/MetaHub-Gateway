"""Strict transport and credential domains for PRD-098 SaaS billing APIs."""

from __future__ import annotations

import json
import re
import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import clear_workspace_cookies_on_error
from starlette.convertors import Convertor, register_url_convertor

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
    SaasPlanCatalogResponse,
    SaasPlanCreateRequest,
    SubscriptionChangeRequest,
    SubscriptionCreateRequest,
    SubscriptionPatchRequest,
    SubscriptionResponse,
)
from .service import (
    cancel_subscription,
    change_subscription_plan,
    create_platform_plan,
    create_subscription,
    get_current_subscription,
    list_platform_plans,
    list_public_plans,
)


class _SubscriptionIdConvertor(Convertor[str]):
    regex = r"sub_[0-9a-f]{32}"

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        if re.fullmatch(self.regex, value) is None:
            raise ValueError("invalid subscription id")
        return value


register_url_convertor("saas_subscription_id", _SubscriptionIdConvertor())

router = APIRouter(tags=["saas-billing"])
_MAX_BODY_BYTES = 8 * 1024
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")


def require_workspace_finance(request: Request) -> WorkspaceSessionContext:
    """Authenticate Workspace finance authority before reading a request body."""

    clear_workspace_cookies_on_error(request)
    if _raw_headers(request, b"authorization") or _cookie_values(
        request, PLATFORM_SESSION_COOKIE
    ) or _cookie_values(request, SUPPORT_SESSION_COOKIE):
        raise ApiError("workspace_session.credential_conflict")
    csrf = None
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        _require_origin(request, "subscription.request_invalid")
        csrf = _required_csrf(request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid")
    context = authenticate_workspace_session_context(
        request.app.state.config,
        _required_cookie(request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"),
        csrf_token=csrf,
    )
    if context.membership_role not in {"owner", "finance_admin"}:
        raise ApiError("workspace_session.permission_denied")
    return context


def require_platform_catalog(request: Request) -> PlatformContext:
    """Authenticate Platform catalog authority before reading a request body."""

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
    required = "platform.catalog.write" if request.method == "POST" else "platform.catalog.read"
    if required not in context.permissions:
        raise ApiError("platform.permission_denied")
    return context


@router.get("/catalog/plans", response_model=SaasPlanCatalogResponse)
async def get_catalog_plans(request: Request, response: Response) -> SaasPlanCatalogResponse:
    _reject_query(request, "saas_catalog.request_invalid")
    response.headers["Cache-Control"] = "public, max-age=60"
    return list_public_plans(
        request.app.state.config,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/subscriptions/current", response_model=SubscriptionResponse)
async def get_subscription(
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_finance)],
) -> SubscriptionResponse:
    _reject_query(request, "subscription.request_invalid")
    _private(response)
    result = get_current_subscription(
        request.app.state.config,
        session_token=_required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.subscription is not None:
        response.headers["ETag"] = _etag(result.subscription.state_revision)
    return result


@router.post("/subscriptions", response_model=SubscriptionResponse, status_code=201)
async def post_subscription(
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_finance)],
) -> SubscriptionResponse:
    _reject_query(request, "subscription.request_invalid")
    payload = _model(
        await _strict_json(request, "subscription.request_invalid"),
        SubscriptionCreateRequest,
    )
    result = create_subscription(
        request.app.state.config,
        session_token=_required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        csrf_token=_required_csrf(
            request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid"
        ),
        payload=payload,
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = 200
    if result.subscription is not None:
        response.headers["ETag"] = _etag(result.subscription.state_revision)
    _private(response)
    return result


@router.post("/subscriptions/current/change-plan", response_model=SubscriptionResponse)
async def post_subscription_change(
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_finance)],
) -> SubscriptionResponse:
    _reject_query(request, "subscription.request_invalid")
    payload = _model(
        await _strict_json(request, "subscription.request_invalid"),
        SubscriptionChangeRequest,
    )
    result = change_subscription_plan(
        request.app.state.config,
        session_token=_required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        csrf_token=_required_csrf(
            request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid"
        ),
        payload=payload,
        expected_revision=_if_match(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.subscription is not None:
        response.headers["ETag"] = _etag(result.subscription.state_revision)
    _private(response)
    return result


@router.patch("/subscriptions/{subscription_id}", response_model=SubscriptionResponse)
async def patch_subscription(
    subscription_id: str,
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_finance)],
) -> SubscriptionResponse:
    """以强 ETag 和幂等键变更或取消指定的当前订阅。"""

    _reject_query(request, "subscription.request_invalid")
    payload = _model(
        await _strict_json(request, "subscription.request_invalid"),
        SubscriptionPatchRequest,
    )
    common = {
        "config": request.app.state.config,
        "session_token": _required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        "csrf_token": _required_csrf(
            request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid"
        ),
        "expected_revision": _if_match(request),
        "idempotency_key": _idempotency_key(request),
        "request_id": _request_id(request),
        "trace_id": _trace_id(request),
    }
    if payload.operation == "change_plan":
        result = change_subscription_plan(
            **common,
            payload=SubscriptionChangeRequest(plan_version_id=payload.plan_version_id),
            subscription_id=subscription_id,
        )
    else:
        result = cancel_subscription(**common, subscription_id=subscription_id)
    if result.subscription is not None:
        response.headers["ETag"] = _etag(result.subscription.state_revision)
    _private(response)
    return result


@router.get("/platform/plans", response_model=SaasPlanCatalogResponse)
async def get_platform_plans(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_catalog)],
) -> SaasPlanCatalogResponse:
    _reject_query(request, "saas_catalog.request_invalid")
    _private(response)
    return list_platform_plans(
        request.app.state.config,
        session_token=_required_cookie(
            request, PLATFORM_SESSION_COOKIE, "platform.session_required"
        ),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post("/platform/plans", response_model=SaasPlanCatalogResponse, status_code=201)
async def post_platform_plan(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_catalog)],
) -> SaasPlanCatalogResponse:
    _reject_query(request, "saas_catalog.request_invalid")
    payload = _model(
        await _strict_json(request, "saas_catalog.request_invalid"),
        SaasPlanCreateRequest,
    )
    _private(response)
    return create_platform_plan(
        request.app.state.config,
        session_token=_required_cookie(
            request, PLATFORM_SESSION_COOKIE, "platform.session_required"
        ),
        csrf_token=_required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid"),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


async def _strict_json(request: Request, error_code: str) -> dict[str, Any]:
    content_types = _raw_headers(request, b"content-type")
    if len(content_types) != 1:
        raise ApiError(error_code)
    media_type, separator, parameters = content_types[0].partition(";")
    if media_type.strip().lower() != "application/json" or (
        separator and parameters.strip().lower() not in {"charset=utf-8", 'charset="utf-8"'}
    ):
        raise ApiError(error_code)
    if _raw_headers(request, b"transfer-encoding"):
        raise ApiError(error_code)
    lengths = _raw_headers(request, b"content-length")
    if len(lengths) > 1:
        raise ApiError(error_code)
    declared: int | None = None
    if lengths:
        if not lengths[0].isdigit() or (len(lengths[0]) > 1 and lengths[0].startswith("0")):
            raise ApiError(error_code)
        declared = int(lengths[0])
        if declared < 2 or declared > _MAX_BODY_BYTES:
            raise ApiError(error_code)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_BODY_BYTES:
            raise ApiError(error_code)
    if not body or (declared is not None and declared != len(body)):
        raise ApiError(error_code)

    def unique_object(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    try:
        payload = json.loads(bytes(body).decode("utf-8"), object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise ApiError(error_code) from None
    if type(payload) is not dict:
        raise ApiError(error_code)
    return payload


def _model(payload: dict[str, Any], model_type):
    try:
        return model_type.model_validate(payload)
    except ValueError:
        code = (
            "saas_catalog.request_invalid"
            if model_type is SaasPlanCreateRequest
            else "subscription.request_invalid"
        )
        raise ApiError(code) from None


def _required_cookie(request: Request, name: str, missing_code: str) -> str:
    values = _cookie_values(request, name)
    if len(values) != 1 or not valid_opaque_value(values[0]):
        if values and name == PLATFORM_SESSION_COOKIE:
            raise ApiError("platform.session_invalid")
        if values and name == WORKSPACE_SESSION_COOKIE:
            raise ApiError("workspace_session.invalid")
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


def _idempotency_key(request: Request) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY_RE.fullmatch(values[0]) is None:
        raise ApiError("subscription.request_invalid")
    return values[0]


def _if_match(request: Request) -> int:
    values = _raw_headers(request, b"if-match")
    if len(values) != 1 or re.fullmatch(r'"[1-9][0-9]*"', values[0]) is None:
        raise ApiError("subscription.request_invalid")
    return int(values[0][1:-1])


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


def _reject_query(request: Request, error_code: str) -> None:
    if request.query_params.multi_items():
        raise ApiError(error_code)


def _private(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, private"


def _etag(revision: int) -> str:
    return f'"{revision}"'


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", "")


# The PRD-144 runtime owns both Platform plan methods. Keep the full router for
# isolated PRD-098 compatibility tests until PRD-147 removes the legacy surface.
runtime_router = APIRouter()
for route in router.routes:
    if (
        (
            getattr(route, "path", None) == "/platform/plans"
            and bool(getattr(route, "methods", set()) & {"GET", "POST"})
        )
        or getattr(route, "path", None)
        in {
            "/subscriptions/current",
            "/subscriptions/current/change-plan",
            "/subscriptions/{subscription_id}",
        }
    ):
        continue
    runtime_router.routes.append(route)

# The production router must not let retired literal aliases such as `current` fall
# through to the surviving object endpoint. The compatibility router keeps its original
# unconstrained path for the isolated PRD-098 contract suite.
runtime_router.patch(
    "/subscriptions/{subscription_id:saas_subscription_id}",
    response_model=SubscriptionResponse,
    tags=["saas-billing"],
)(patch_subscription)
