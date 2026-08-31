"""Strict Platform HTTP boundary for PRD-140 customer success."""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from metahub_shared.runtime.errors import ApiError

from metahub_control.identity.service import CSRF_COOKIE as GLOBAL_CSRF_COOKIE
from metahub_control.identity.service import SESSION_COOKIE as GLOBAL_SESSION_COOKIE
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
    CustomerAccountListResponse,
    CustomerHealthResponse,
    CustomerTaskCommand,
    CustomerTaskResponse,
    RenewalPipelineResponse,
    SuccessPlanCommand,
    SuccessPlanResponse,
)
from .permissions import (
    DIRECTORY,
    FINANCE_READ,
    PLAN_MANAGE,
    RENEWAL_READ,
    SUPPORT_READ,
    TASK_MANAGE,
)
from .service import (
    apply_customer_task_command,
    apply_success_plan_command,
    get_customer_health,
    list_customer_accounts,
    list_renewal_pipeline,
)

router = APIRouter(tags=["platform-customer-success"])
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_IF_MATCH = re.compile(r'"(0|[1-9][0-9]*)"')
_ACCOUNT_QUERY = {
    "limit",
    "cursor",
    "customer_account_id",
    "workspace_id",
    "slug",
    "kind",
    "region",
    "operational_status",
    "activation",
    "adoption",
    "health_risk",
    "renewal_status",
    "view",
}
_RENEWAL_QUERY = {
    "limit",
    "cursor",
    "status",
    "risk",
    "renewal_from",
    "renewal_to",
    "region",
    "customer_account_id",
}


def require_platform_customer_success(request: Request) -> PlatformContext:
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, WORKSPACE_SESSION_COOKIE)
        or _cookie_values(request, WORKSPACE_CSRF_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
        or _cookie_values(request, GLOBAL_SESSION_COOKIE)
        or _cookie_values(request, GLOBAL_CSRF_COOKIE)
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
    route = request.scope.get("route")
    path = getattr(route, "path", request.url.path)
    accepted = {
        ("GET", "/platform/customer-accounts"): {DIRECTORY, SUPPORT_READ, FINANCE_READ},
        ("GET", "/platform/customer-accounts/{customer_account_id}/health"): {
            DIRECTORY,
            SUPPORT_READ,
        },
        ("POST", "/platform/customer-success-plans"): {PLAN_MANAGE},
        ("POST", "/platform/customer-tasks"): {TASK_MANAGE},
        ("GET", "/platform/renewal-pipeline"): {RENEWAL_READ},
    }.get((request.method, path))
    if accepted is None or not accepted.intersection(context.permissions):
        raise ApiError("platform.permission_denied")
    return context


@router.get("/platform/customer-accounts", response_model=CustomerAccountListResponse)
async def get_customer_accounts(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_customer_success)],
) -> CustomerAccountListResponse:
    query = _query(request, _ACCOUNT_QUERY)
    _reject_body(request)
    limit = _limit(query)
    filters = {key: value for key, value in query.items() if key not in {"limit", "cursor"}}
    result = list_customer_accounts(
        request.app.state.config,
        session_token=_platform_token(request),
        limit=limit,
        cursor=query.get("cursor"),
        filters=filters,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.get(
    "/platform/customer-accounts/{customer_account_id}/health",
    response_model=CustomerHealthResponse,
)
async def get_customer_account_health(
    customer_account_id: str,
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_customer_success)],
) -> CustomerHealthResponse:
    _query(request, set())
    _reject_body(request)
    result = get_customer_health(
        request.app.state.config,
        session_token=_platform_token(request),
        customer_account_id=customer_account_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post("/platform/customer-success-plans", response_model=SuccessPlanResponse)
async def post_customer_success_plan(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_customer_success)],
) -> SuccessPlanResponse:
    _query(request, set())
    command = _model(await _strict_json(request, "platform.request_invalid"), SuccessPlanCommand)
    if _if_match(request) != command.expected_revision:
        raise ApiError("platform.revision_conflict")
    result = apply_success_plan_command(
        request.app.state.config,
        command,
        session_token=_platform_token(request),
        csrf_token=_platform_csrf(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = _etag(result.plan.revision)
    if command.operation == "create" and not result.idempotent_replay:
        response.status_code = 201
    _private(response)
    return result


@router.post("/platform/customer-tasks", response_model=CustomerTaskResponse)
async def post_customer_task(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_customer_success)],
) -> CustomerTaskResponse:
    _query(request, set())
    command = _model(await _strict_json(request, "platform.request_invalid"), CustomerTaskCommand)
    if _if_match(request) != command.expected_revision:
        raise ApiError("platform.revision_conflict")
    result = apply_customer_task_command(
        request.app.state.config,
        command,
        session_token=_platform_token(request),
        csrf_token=_platform_csrf(request),
        step_up_grant=_optional_step_up(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    revision = result.task.revision if result.task is not None else result.batch.revision
    response.headers["ETag"] = _etag(revision)
    if command.operation in {"create_one", "prepare_batch"} and not result.idempotent_replay:
        response.status_code = 201
    _private(response)
    return result


@router.get("/platform/renewal-pipeline", response_model=RenewalPipelineResponse)
async def get_renewal_pipeline(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_customer_success)],
) -> RenewalPipelineResponse:
    query = _query(request, _RENEWAL_QUERY)
    _reject_body(request)
    result = list_renewal_pipeline(
        request.app.state.config,
        session_token=_platform_token(request),
        limit=_limit(query),
        cursor=query.get("cursor"),
        filters={key: value for key, value in query.items() if key not in {"limit", "cursor"}},
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


def _model(payload: dict[str, Any], model_type):
    try:
        return model_type.model_validate(payload)
    except ValueError:
        raise ApiError("platform.request_invalid") from None


def _platform_token(request: Request) -> str:
    return _required_cookie(request, PLATFORM_SESSION_COOKIE, "platform.session_required")


def _platform_csrf(request: Request) -> str:
    return _required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid")


def _if_match(request: Request) -> int:
    values = _raw_headers(request, b"if-match")
    if len(values) != 1 or _IF_MATCH.fullmatch(values[0]) is None:
        raise ApiError("platform.revision_conflict")
    return int(values[0][1:-1])


def _idempotency_key(request: Request) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY.fullmatch(values[0]) is None:
        raise ApiError("platform.idempotency_conflict")
    return values[0]


def _optional_step_up(request: Request) -> str | None:
    values = _raw_headers(request, b"x-step-up-grant")
    if not values:
        return None
    if len(values) != 1 or not valid_opaque_value(values[0]):
        raise ApiError("platform.step_up_required")
    return values[0]


def _query(request: Request, allowed: set[str]) -> dict[str, str]:
    items = request.query_params.multi_items()
    keys = [key for key, _value in items]
    if (
        any(key not in allowed for key in keys)
        or len(keys) != len(set(keys))
        or any(value == "" for _key, value in items)
    ):
        raise ApiError("platform.request_invalid")
    return dict(items)


def _limit(query: dict[str, str]) -> int:
    raw = query.get("limit", "50")
    try:
        value = int(raw)
    except ValueError:
        raise ApiError("platform.request_invalid") from None
    if str(value) != raw or not 1 <= value <= 100:
        raise ApiError("platform.request_invalid")
    return value


def _reject_body(request: Request) -> None:
    if _raw_headers(request, b"transfer-encoding") or _raw_headers(request, b"content-type"):
        raise ApiError("platform.request_invalid")
    lengths = _raw_headers(request, b"content-length")
    if len(lengths) > 1 or (lengths and lengths[0] != "0"):
        raise ApiError("platform.request_invalid")


def _etag(revision: int) -> str:
    return f'"{revision}"'


__all__ = ["router"]
