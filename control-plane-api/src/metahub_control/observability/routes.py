"""Strict PlatformSession HTTP boundary for platform observability."""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from metahub_shared.runtime.errors import ApiError

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
    AlertAcknowledgementRequest,
    AlertAcknowledgementResponse,
    ErrorBudgetListResponse,
    ServiceCatalogResponse,
    ServiceLevelObjectiveListResponse,
    ServiceLevelObjectiveRequest,
    ServiceLevelObjectiveResponse,
)
from .service import (
    acknowledge_alert,
    create_slo,
    list_error_budgets,
    list_services,
    list_slos,
)

router = APIRouter(tags=["platform-observability"])
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,63}$")


def require_platform_observability(request: Request) -> PlatformContext:
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
    required = (
        {"platform.observability.read", "platform.observability.security_read"}
        if request.method == "GET"
        else {
            "platform.observability.alert_ack"
            if request.url.path.endswith("/ack")
            else "platform.observability.manage"
        }
    )
    if required.isdisjoint(context.permissions):
        raise ApiError("platform.permission_denied")
    return context


@router.get("/platform/observability/services", response_model=ServiceCatalogResponse)
async def platform_observability_services(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_observability)],
) -> ServiceCatalogResponse:
    _reject_query(request)
    result = list_services(
        request.app.state.config,
        session_token=_platform_cookie(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.get("/platform/slos", response_model=ServiceLevelObjectiveListResponse)
async def platform_slos(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_observability)],
) -> ServiceLevelObjectiveListResponse:
    service_code, signal_class, current_only, limit, offset = _slo_query(request)
    result = list_slos(
        request.app.state.config,
        session_token=_platform_cookie(request),
        service_code=service_code,
        signal_class=signal_class,
        current_only=current_only,
        limit=limit,
        offset=offset,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post("/platform/slos", response_model=ServiceLevelObjectiveResponse, status_code=201)
async def post_platform_slo(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_observability)],
) -> ServiceLevelObjectiveResponse:
    _reject_query(request)
    payload = await _model(request, ServiceLevelObjectiveRequest)
    result = create_slo(
        request.app.state.config,
        session_token=_platform_cookie(request),
        csrf_token=_platform_csrf(request),
        expected_revision=_if_match(request),
        idempotency_key=_idempotency_key(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = 200
    response.headers["ETag"] = f'"{result.slo.version_number}"'
    _private(response)
    return result


@router.get("/platform/error-budgets", response_model=ErrorBudgetListResponse)
async def platform_error_budgets(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_observability)],
) -> ErrorBudgetListResponse:
    service_code, window_minutes, limit, cursor = _budget_query(request)
    result = list_error_budgets(
        request.app.state.config,
        session_token=_platform_cookie(request),
        service_code=service_code,
        window_minutes=window_minutes,
        limit=limit,
        cursor=cursor,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post(
    "/platform/alerts/{alert_id}/ack",
    response_model=AlertAcknowledgementResponse,
    status_code=201,
)
async def post_platform_alert_ack(
    alert_id: str,
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_observability)],
) -> AlertAcknowledgementResponse:
    _reject_query(request)
    payload = await _model(request, AlertAcknowledgementRequest)
    result = acknowledge_alert(
        request.app.state.config,
        session_token=_platform_cookie(request),
        csrf_token=_platform_csrf(request),
        alert_id=alert_id,
        idempotency_key=_idempotency_key(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = 200
    _private(response)
    return result


async def _model(request: Request, model_type):
    try:
        return model_type.model_validate(
            await _strict_json(request, "observability.request_invalid")
        )
    except ValueError:
        raise ApiError("observability.request_invalid") from None


def _slo_query(request: Request) -> tuple[str | None, str | None, bool, int, int]:
    _unique_allowed(request, {"service_code", "signal_class", "current_only", "limit", "offset"})
    service_code = request.query_params.get("service_code")
    if service_code is not None and _CODE_RE.fullmatch(service_code) is None:
        raise ApiError("observability.request_invalid")
    signal_class = request.query_params.get("signal_class")
    if signal_class not in {None, "reliability", "security"}:
        raise ApiError("observability.request_invalid")
    current_raw = request.query_params.get("current_only", "true")
    if current_raw not in {"true", "false"}:
        raise ApiError("observability.request_invalid")
    return (
        service_code,
        signal_class,
        current_raw == "true",
        _integer_query(request, "limit", 50, 1, 200),
        _integer_query(request, "offset", 0, 0, 1000000),
    )


def _budget_query(request: Request) -> tuple[str | None, int | None, int, str | None]:
    _unique_allowed(request, {"service_code", "window_minutes", "limit", "cursor"})
    service_code = request.query_params.get("service_code")
    if service_code is not None and _CODE_RE.fullmatch(service_code) is None:
        raise ApiError("observability.request_invalid")
    window_raw = request.query_params.get("window_minutes")
    window = None
    if window_raw is not None:
        if re.fullmatch(r"[1-9][0-9]{0,4}", window_raw) is None or int(window_raw) > 43200:
            raise ApiError("observability.request_invalid")
        window = int(window_raw)
    cursor = request.query_params.get("cursor")
    if cursor is not None and not 20 <= len(cursor) <= 2048:
        raise ApiError("observability.cursor_invalid")
    return service_code, window, _integer_query(request, "limit", 50, 1, 200), cursor


def _unique_allowed(request: Request, allowed: set[str]) -> None:
    items = request.query_params.multi_items()
    if len(items) != len({key for key, _value in items}) or any(
        key not in allowed for key, _value in items
    ):
        raise ApiError("observability.request_invalid")


def _integer_query(request: Request, name: str, default: int, minimum: int, maximum: int) -> int:
    raw = request.query_params.get(name, str(default))
    if re.fullmatch(r"0|[1-9][0-9]{0,6}", raw) is None:
        raise ApiError("observability.request_invalid")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise ApiError("observability.request_invalid")
    return value


def _if_match(request: Request) -> int:
    values = _raw_headers(request, b"if-match")
    if len(values) != 1 or re.fullmatch(r'"(?:0|[1-9][0-9]*)"', values[0]) is None:
        raise ApiError("observability.request_invalid")
    return int(values[0][1:-1])


def _idempotency_key(request: Request) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY_RE.fullmatch(values[0]) is None:
        raise ApiError("observability.request_invalid")
    return values[0]


def _reject_query(request: Request) -> None:
    if request.query_params.multi_items():
        raise ApiError("observability.request_invalid")


def _platform_cookie(request: Request) -> str:
    return _required_cookie(request, PLATFORM_SESSION_COOKIE, "platform.session_required")


def _platform_csrf(request: Request) -> str:
    return _required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid")


__all__ = ["require_platform_observability", "router"]
