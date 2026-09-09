"""Strict Workspace and Platform transport boundaries for PRD-099 billing APIs."""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from starlette.concurrency import run_in_threadpool
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
    require_workspace_finance,
)
from metahub_control.workspace_access.constants import (
    WORKSPACE_SESSION_COOKIE,
)
from metahub_control.workspace_access.service import WorkspaceSessionContext

from .models import (
    BillingAdjustmentRequest,
    BillingAdjustmentResponse,
    MeteringStateResponse,
    MeterRecoveryRequest,
    MeterRecoveryResponse,
    PlatformBillingListResponse,
    WorkspaceBillingResponse,
)
from .recovery import get_metering_state, recover_meter_fact
from .service import (
    create_adjustment,
    export_workspace_reconciliation_csv,
    get_workspace_billing,
    list_platform_billing,
)

router = APIRouter(tags=["platform-billing"])
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")


def require_platform_billing(request: Request) -> PlatformContext:
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
        "platform.billing.adjust" if request.method == "POST" else "platform.billing.read"
    )
    if permission not in context.permissions:
        raise ApiError("platform.permission_denied")
    return context


@router.get("/billing/summary", response_model=WorkspaceBillingResponse)
async def get_billing_summary(
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_finance)],
    cycle_id: str | None = None,
) -> WorkspaceBillingResponse:
    _exact_query(request, {"cycle_id"})
    _private(response)
    return get_workspace_billing(
        request.app.state.config,
        session_token=_required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        cycle_id=cycle_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/billing/usage", response_model=WorkspaceBillingResponse)
async def get_billing_usage(
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_finance)],
    cycle_id: str | None = None,
) -> WorkspaceBillingResponse:
    _exact_query(request, {"cycle_id"})
    _private(response)
    return get_workspace_billing(
        request.app.state.config,
        session_token=_required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        cycle_id=cycle_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/billing/reconciliation-export")
async def get_billing_reconciliation_export(
    request: Request,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_finance)],
    cycle_id: str,
) -> Response:
    _exact_query(request, {"cycle_id"})
    content, filename = export_workspace_reconciliation_csv(
        request.app.state.config,
        session_token=_required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        cycle_id=cycle_id,
    )
    return Response(
        content=content,
        headers={
            "Content-Type": "text/csv; charset=utf-8",
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store, private",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/platform/billing/summary", response_model=PlatformBillingListResponse)
async def get_platform_billing_summary(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_billing)],
    status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> PlatformBillingListResponse:
    _exact_query(request, {"status", "limit", "cursor"})
    _private(response)
    return list_platform_billing(
        request.app.state.config,
        session_token=_required_cookie(
            request, PLATFORM_SESSION_COOKIE, "platform.session_required"
        ),
        status=status,
        limit=limit,
        cursor=cursor,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post("/platform/billing/adjustments", response_model=BillingAdjustmentResponse)
async def post_platform_billing_adjustment(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_billing)],
) -> BillingAdjustmentResponse:
    _exact_query(request, set())
    try:
        payload = BillingAdjustmentRequest.model_validate(
            await _strict_json(request, "platform_billing.request_invalid")
        )
    except ValueError:
        raise ApiError("platform_billing.request_invalid") from None
    result = create_adjustment(
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
    _private(response)
    return result


def _idempotency_key(request: Request) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY_RE.fullmatch(values[0]) is None:
        raise ApiError("platform_billing.request_invalid")
    return values[0]


@router.get("/platform/billing/metering-state", response_model=MeteringStateResponse)
def get_platform_metering_state(
    request: Request, response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_billing)],
) -> MeteringStateResponse:
    _exact_query(request, set())
    result = get_metering_state(
        request.app.state.config,
        session_token=_required_cookie(request, PLATFORM_SESSION_COOKIE, "platform.session_required"),
        request_id=_request_id(request), trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post("/platform/billing/meter-recoveries", response_model=MeterRecoveryResponse)
async def post_platform_meter_recovery(
    request: Request, response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_billing)],
) -> MeterRecoveryResponse:
    _exact_query(request, set())
    try:
        payload = MeterRecoveryRequest.model_validate(await _strict_json(request, "platform_billing.request_invalid"))
    except ValueError:
        raise ApiError("platform_billing.request_invalid") from None
    result = await run_in_threadpool(
        recover_meter_fact, request.app.state.config,
        session_token=_required_cookie(request, PLATFORM_SESSION_COOKIE, "platform.session_required"),
        csrf_token=_required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid"),
        payload=payload, idempotency_key=_idempotency_key(request),
        request_id=_request_id(request), trace_id=_trace_id(request),
    )
    response.status_code = 200 if result.idempotent_replay else 201
    _private(response)
    return result


def _exact_query(request: Request, allowed: set[str]) -> None:
    items = request.query_params.multi_items()
    keys = [key for key, _value in items]
    if any(key not in allowed for key in keys) or len(keys) != len(set(keys)):
        raise ApiError("platform_billing.request_invalid")


__all__ = ["router"]
