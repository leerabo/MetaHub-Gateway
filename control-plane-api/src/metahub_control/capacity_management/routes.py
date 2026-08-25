"""Credential-isolated PlatformSession routes for PRD-113."""

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
    CapacityForecastRequest,
    CapacityForecastResponse,
    CapacityStatusResponse,
    ChaosCommandRequest,
    ChaosExperimentResponse,
    LoadTestRequest,
    LoadTestResponse,
    UnitEconomicsResponse,
)
from .service import (
    command_chaos_experiment,
    create_capacity_forecast,
    create_load_test,
    get_capacity_status,
    get_unit_economics,
)

router = APIRouter(tags=["capacity-management"])
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")


def require_platform_capacity(request: Request) -> PlatformContext:
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
    if request.url.path == "/platform/unit-economics":
        required = "platform.unit_economics.read"
    elif request.method == "GET":
        required = "platform.capacity.read"
    elif request.url.path == "/platform/chaos-experiments":
        required = "platform.capacity.chaos"
    else:
        required = "platform.capacity.test"
    if required not in context.permissions:
        raise ApiError("platform.permission_denied")
    return context


@router.get("/platform/capacity", response_model=CapacityStatusResponse)
async def capacity_status(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_capacity)],
) -> CapacityStatusResponse:
    _reject_query(request)
    result = get_capacity_status(
        request.app.state.config,
        session_token=_platform_cookie(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post(
    "/platform/capacity/forecasts",
    response_model=CapacityForecastResponse,
    status_code=201,
)
async def capacity_forecasts(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_capacity)],
) -> CapacityForecastResponse:
    _reject_query(request)
    result = create_capacity_forecast(
        request.app.state.config,
        session_token=_platform_cookie(request),
        csrf_token=_platform_csrf(request),
        expected_revision=_if_match(request),
        payload=await _model(request, CapacityForecastRequest),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = 200
    response.headers["ETag"] = f'"{result.forecast.revision}"'
    _private(response)
    return result


@router.post("/platform/load-tests", response_model=LoadTestResponse, status_code=201)
async def load_tests(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_capacity)],
) -> LoadTestResponse:
    _reject_query(request)
    _reject_if_match(request)
    result = create_load_test(
        request.app.state.config,
        session_token=_platform_cookie(request),
        csrf_token=_platform_csrf(request),
        payload=await _model(request, LoadTestRequest),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = 200
    response.headers["ETag"] = f'"{result.run.revision}"'
    _private(response)
    return result


@router.post(
    "/platform/chaos-experiments",
    response_model=ChaosExperimentResponse,
    status_code=201,
)
async def chaos_experiments(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_capacity)],
) -> ChaosExperimentResponse:
    _reject_query(request)
    payload = await _model(request, ChaosCommandRequest)
    expected_revision = _optional_if_match(request, required=payload.action != "create")
    result = command_chaos_experiment(
        request.app.state.config,
        session_token=_platform_cookie(request),
        csrf_token=_platform_csrf(request),
        expected_revision=expected_revision,
        payload=payload,
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if payload.action != "create" or result.idempotent_replay:
        response.status_code = 200
    response.headers["ETag"] = f'"{result.experiment.revision}"'
    _private(response)
    return result


@router.get("/platform/unit-economics", response_model=UnitEconomicsResponse)
async def unit_economics(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_capacity)],
) -> UnitEconomicsResponse:
    billing_period, currency = _economics_query(request)
    result = get_unit_economics(
        request.app.state.config,
        session_token=_platform_cookie(request),
        billing_period=billing_period,
        currency=currency,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


async def _model(request: Request, model_type):
    try:
        return model_type.model_validate(await _strict_json(request, "capacity.request_invalid"))
    except ValueError:
        raise ApiError("capacity.request_invalid") from None


def _if_match(request: Request) -> int:
    value = _optional_if_match(request, required=True)
    assert value is not None
    return value


def _optional_if_match(request: Request, *, required: bool) -> int | None:
    values = _raw_headers(request, b"if-match")
    if not values and not required:
        return None
    if len(values) != 1 or re.fullmatch(r'"[1-9][0-9]*"', values[0]) is None:
        raise ApiError("capacity.request_invalid")
    return int(values[0][1:-1])


def _reject_if_match(request: Request) -> None:
    if _raw_headers(request, b"if-match"):
        raise ApiError("capacity.request_invalid")


def _idempotency_key(request: Request) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY_RE.fullmatch(values[0]) is None:
        raise ApiError("capacity.request_invalid")
    return values[0]


def _economics_query(request: Request) -> tuple[str | None, str | None]:
    items = request.query_params.multi_items()
    if len(items) != len({key for key, _value in items}) or any(
        key not in {"billing_period", "currency"} for key, _value in items
    ):
        raise ApiError("capacity.request_invalid")
    period = request.query_params.get("billing_period")
    currency = request.query_params.get("currency")
    if period is not None and re.fullmatch(r"[0-9]{4}-(?:0[1-9]|1[0-2])", period) is None:
        raise ApiError("capacity.request_invalid")
    if currency not in {None, "CNY", "USD"}:
        raise ApiError("capacity.request_invalid")
    return period, currency


def _reject_query(request: Request) -> None:
    if request.query_params.multi_items():
        raise ApiError("capacity.request_invalid")


def _platform_cookie(request: Request) -> str:
    return _required_cookie(request, PLATFORM_SESSION_COOKIE, "platform.session_required")


def _platform_csrf(request: Request) -> str:
    return _required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid")


__all__ = ["require_platform_capacity", "router"]
