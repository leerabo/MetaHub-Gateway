"""Credential-isolated HTTP boundaries for PRD-144 commercial accounting."""

from __future__ import annotations

import json
import re
import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import clear_workspace_cookies_on_error
from pydantic import ValidationError

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
    BillingDisputeCommandResponse,
    BillingDisputeCreateRequest,
    BillingDisputeDecisionRequest,
    PlatformPlanCommandRequest,
    PlatformPlanCommandResponse,
    PlatformPlanListResponse,
    PlatformSubscriptionListResponse,
    ProviderCostRateCommandRequest,
    ProviderCostRateCommandResponse,
    WorkspaceFxRateCommandRequest,
    WorkspaceFxRateCommandResponse,
    WorkspaceFxRateListResponse,
    ProviderReferenceRateCommandRequest,
    ProviderReferenceRateCommandResponse,
    WorkspaceProviderCostRateListResponse,
    WorkspaceSubscriptionResponse,
)
from .permissions import (
    PLATFORM_COMMERCIAL_PERMISSIONS,
    WORKSPACE_COMMERCIAL_PERMISSIONS,
)
from .service import (
    command_platform_plan,
    command_platform_provider_reference_rate,
    command_workspace_provider_cost_rate,
    create_workspace_billing_dispute,
    decide_platform_billing_dispute,
    get_workspace_subscription,
    list_platform_plans,
    list_platform_subscriptions,
    list_workspace_provider_cost_rates,
    list_workspace_fx_rates,
    command_workspace_fx_rate,
)

router = APIRouter(tags=["commercial-accounting"])
_MAX_BODY_BYTES = 8_192
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_IF_MATCH = re.compile(r'^"(0|[1-9][0-9]{0,9})"$')


def require_platform_commercial(request: Request) -> PlatformContext:
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, WORKSPACE_SESSION_COOKIE)
        or _cookie_values(request, WORKSPACE_CSRF_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("platform.credential_conflict")
    csrf = None
    if request.method == "POST":
        _require_origin(request, "platform.origin_invalid")
        csrf = _required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid")
    context = authenticate_platform_session_context(
        request.app.state.config,
        _required_cookie(request, PLATFORM_SESSION_COOKIE, "platform.session_required"),
        csrf_token=csrf,
    )
    required = _platform_permission(request)
    if required not in context.permissions or required not in PLATFORM_COMMERCIAL_PERMISSIONS:
        raise ApiError("platform.permission_denied")
    return context


def require_workspace_commercial(request: Request) -> WorkspaceSessionContext:
    clear_workspace_cookies_on_error(request)
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, PLATFORM_SESSION_COOKIE)
        or _cookie_values(request, PLATFORM_CSRF_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("workspace_session.credential_conflict")
    csrf = None
    if request.method in {"POST", "PATCH"}:
        _require_origin(request, "platform.origin_invalid")
        csrf = _required_csrf(
            request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid"
        )
    context = authenticate_workspace_session_context(
        request.app.state.config,
        _required_cookie(request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"),
        csrf_token=csrf,
    )
    required = _workspace_permission(request)
    if required not in context.permission_codes or required not in WORKSPACE_COMMERCIAL_PERMISSIONS:
        raise ApiError("workspace_session.permission_denied")
    return context


@router.get("/platform/plans", response_model=PlatformPlanListResponse)
async def get_platform_plans(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_commercial)],
) -> PlatformPlanListResponse:
    result = list_platform_plans(
        request.app.state.config,
        session_token=_platform_token(request),
        query=_unique_query(request, platform=True),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response, result.permission_fingerprint)
    return result


@router.post("/platform/plans", response_model=PlatformPlanCommandResponse)
async def post_platform_plan_command(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_commercial)],
) -> PlatformPlanCommandResponse:
    _reject_query(request, platform=True)
    command = await _model(request, PlatformPlanCommandRequest, "platform.request_invalid")
    result = command_platform_plan(
        request.app.state.config,
        session_token=_platform_token(request),
        csrf_token=_platform_csrf(request),
        step_up_grant=_step_up(request),
        idempotency_key=_idempotency_key(request, platform=True),
        expected_revision=_if_match(request, platform=True),
        command=command,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if command.action == "create_candidate" and not result.idempotent_replay:
        response.status_code = status.HTTP_201_CREATED
    response.headers["ETag"] = f'"{result.plan.revision}"'
    _private(response, result.permission_fingerprint)
    return result


@router.get("/platform/subscriptions", response_model=PlatformSubscriptionListResponse)
async def get_platform_subscriptions(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_commercial)],
) -> PlatformSubscriptionListResponse:
    result = list_platform_subscriptions(
        request.app.state.config,
        session_token=_platform_token(request),
        query=_unique_query(request, platform=True),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response, result.permission_fingerprint)
    return result


@router.post(
    "/platform/billing-disputes/{dispute_id}/decisions",
    response_model=BillingDisputeCommandResponse,
)
async def post_platform_billing_dispute_decision(
    dispute_id: str,
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_commercial)],
) -> BillingDisputeCommandResponse:
    _reject_query(request, platform=True)
    command = await _model(
        request, BillingDisputeDecisionRequest, "platform.request_invalid"
    )
    result = decide_platform_billing_dispute(
        request.app.state.config,
        session_token=_platform_token(request),
        csrf_token=_platform_csrf(request),
        step_up_grant=_step_up(request),
        idempotency_key=_idempotency_key(request, platform=True),
        expected_case_revision=_if_match(request, platform=True),
        dispute_id=dispute_id,
        command=command,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.dispute.state_revision}"'
    _private(response, result.permission_fingerprint)
    return result


@router.get("/workspace/subscription", response_model=WorkspaceSubscriptionResponse)
async def get_workspace_subscription_route(
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_commercial)],
) -> WorkspaceSubscriptionResponse:
    result = get_workspace_subscription(
        request.app.state.config,
        session_token=_workspace_token(request),
        query=_unique_query(request, platform=False),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.subscription is not None:
        response.headers["ETag"] = f'"{result.subscription.revision}"'
    _private(response, result.permission_fingerprint)
    return result


@router.post(
    "/workspace/billing-disputes",
    response_model=BillingDisputeCommandResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_workspace_billing_dispute(
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_commercial)],
) -> BillingDisputeCommandResponse:
    _reject_query(request, platform=False)
    command = await _model(
        request, BillingDisputeCreateRequest, "billing_dispute.request_invalid"
    )
    result = create_workspace_billing_dispute(
        request.app.state.config,
        session_token=_workspace_token(request),
        csrf_token=_workspace_csrf(request),
        step_up_grant=_step_up(request),
        idempotency_key=_idempotency_key(request, platform=False),
        expected_source_revision=_if_match(request, platform=False),
        command=command,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = status.HTTP_200_OK
    response.headers["ETag"] = f'"{result.dispute.state_revision}"'
    _private(response, result.permission_fingerprint)
    return result


@router.get(
    "/workspace/provider-cost-rates",
    response_model=WorkspaceProviderCostRateListResponse,
)
async def get_workspace_provider_cost_rates(
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_commercial)],
) -> WorkspaceProviderCostRateListResponse:
    result = list_workspace_provider_cost_rates(
        request.app.state.config,
        session_token=_workspace_token(request),
        query=_unique_query(request, platform=False),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response, result.permission_fingerprint)
    return result


@router.patch(
    "/workspace/provider-cost-rates",
    response_model=ProviderCostRateCommandResponse,
)
async def patch_workspace_provider_cost_rates(
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_commercial)],
) -> ProviderCostRateCommandResponse:
    _reject_query(request, platform=False)
    command = await _model(
        request, ProviderCostRateCommandRequest, "model_price.query_invalid"
    )
    result = command_workspace_provider_cost_rate(
        request.app.state.config,
        session_token=_workspace_token(request),
        csrf_token=_workspace_csrf(request),
        step_up_grant=_step_up(request),
        idempotency_key=_idempotency_key(request, platform=False),
        expected_head_revision=_if_match(request, platform=False),
        command=command,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.rate.head_revision}"'
    _private(response, result.permission_fingerprint)
    return result


@router.get("/workspace/routing-fx-rates", response_model=WorkspaceFxRateListResponse)
async def get_workspace_routing_fx_rates(
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_commercial)],
) -> WorkspaceFxRateListResponse:
    result = list_workspace_fx_rates(
        request.app.state.config,
        session_token=_workspace_token(request),
        query=_unique_query(request, platform=False),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response, result.permission_fingerprint)
    return result


@router.patch("/workspace/routing-fx-rates", response_model=WorkspaceFxRateCommandResponse)
async def patch_workspace_routing_fx_rates(
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_commercial)],
) -> WorkspaceFxRateCommandResponse:
    _reject_query(request, platform=False)
    command = await _model(request, WorkspaceFxRateCommandRequest, "model_price.query_invalid")
    result = command_workspace_fx_rate(
        request.app.state.config,
        session_token=_workspace_token(request),
        csrf_token=_workspace_csrf(request),
        step_up_grant=_step_up(request),
        idempotency_key=_idempotency_key(request, platform=False),
        expected_head_revision=_if_match(request, platform=False),
        command=command,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.rate.revision}"'
    _private(response, result.permission_fingerprint)
    return result


@router.post(
    "/platform/provider-reference-rates/{provider_id}/commands",
    response_model=ProviderReferenceRateCommandResponse,
)
async def post_platform_provider_reference_rate_command(
    provider_id: str,
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_commercial)],
) -> ProviderReferenceRateCommandResponse:
    _reject_query(request, platform=True)
    command = await _model(
        request, ProviderReferenceRateCommandRequest, "platform.request_invalid"
    )
    result = command_platform_provider_reference_rate(
        request.app.state.config,
        session_token=_platform_token(request),
        csrf_token=_platform_csrf(request),
        step_up_grant=_step_up(request),
        idempotency_key=_idempotency_key(request, platform=True),
        expected_revision=_if_match(request, platform=True),
        provider_id=provider_id,
        command=command,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if command.action == "create_candidate" and not result.idempotent_replay:
        response.status_code = status.HTTP_201_CREATED
    response.headers["ETag"] = f'"{result.rate.revision}"'
    _private(response, result.permission_fingerprint)
    return result


def _platform_permission(request: Request) -> str:
    path = request.url.path
    if path == "/platform/plans":
        return (
            "platform.commercial.plan.manage"
            if request.method == "POST"
            else "platform.commercial.plan.read"
        )
    if path == "/platform/subscriptions" and request.method == "GET":
        return "platform.commercial.subscription.read"
    if path.startswith("/platform/billing-disputes/") and request.method == "POST":
        return "platform.commercial.dispute.decide"
    if path.startswith("/platform/provider-reference-rates/") and request.method == "POST":
        return "platform.commercial.plan.manage"
    raise ApiError("platform.permission_denied")


def _workspace_permission(request: Request) -> str:
    path = request.url.path
    if path == "/workspace/subscription" and request.method == "GET":
        return "workspace.commercial.subscription.read"
    if path == "/workspace/billing-disputes" and request.method == "POST":
        return "workspace.commercial.dispute.submit"
    if path == "/workspace/provider-cost-rates":
        return (
            "workspace.provider_cost.manage"
            if request.method == "PATCH"
            else "workspace.provider_cost.read"
        )
    if path == "/workspace/routing-fx-rates":
        return (
            "workspace.provider_cost.manage"
            if request.method == "PATCH"
            else "workspace.provider_cost.read"
        )
    raise ApiError("workspace_session.permission_denied")


async def _model(request: Request, model_type, error_code: str):
    payload = await _strict_json(request, error_code)
    try:
        return model_type.model_validate_json(
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")), strict=True
        )
    except ValidationError:
        raise ApiError(error_code) from None


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
    declared = None
    if lengths:
        if not lengths[0].isdigit() or (len(lengths[0]) > 1 and lengths[0].startswith("0")):
            raise ApiError(error_code)
        declared = int(lengths[0])
        if not 2 <= declared <= _MAX_BODY_BYTES:
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
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        payload = json.loads(
            bytes(body).decode("utf-8", errors="strict"),
            object_pairs_hook=unique_object,
            parse_float=lambda _: (_ for _ in ()).throw(ValueError("float")),
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise ApiError(error_code) from None
    if type(payload) is not dict:
        raise ApiError(error_code)
    return payload


def _unique_query(request: Request, *, platform: bool) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key in result or _control(key) or _control(value):
            raise ApiError("platform.request_invalid" if platform else "model_price.query_invalid")
        result[key] = value
    return result


def _reject_query(request: Request, *, platform: bool) -> None:
    if request.query_params.multi_items():
        raise ApiError("platform.request_invalid" if platform else "model_price.query_invalid")


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


def _if_match(request: Request, *, platform: bool) -> int:
    values = _raw_headers(request, b"if-match")
    match = _IF_MATCH.fullmatch(values[0]) if len(values) == 1 else None
    if match is None:
        raise ApiError("platform.revision_conflict" if platform else "model_price.version_conflict")
    return int(match.group(1))


def _idempotency_key(request: Request, *, platform: bool) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY.fullmatch(values[0]) is None:
        raise ApiError(
            "platform.idempotency_conflict"
            if platform
            else "billing_dispute.idempotency_conflict"
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


def _private(response: Response, permission_fingerprint: str) -> None:
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["X-Permission-Fingerprint"] = permission_fingerprint


def _control(value: str) -> bool:
    return any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value)


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", "")


__all__ = ["router"]
