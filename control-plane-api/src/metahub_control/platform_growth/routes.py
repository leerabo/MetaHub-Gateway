"""Strict Platform HTTP boundary for PRD-141 growth operations."""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from metahub_shared.runtime.errors import ApiError

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
    AttributionResponse,
    CampaignCommand,
    CampaignListResponse,
    CampaignResponse,
    CohortResponse,
    ExperimentCommand,
    ExperimentListResponse,
    ExperimentResponse,
    FunnelResponse,
    GrowthExportApprovalCommand,
    GrowthExportReceiptCommand,
    GrowthExportReceiptResponse,
    GrowthExportRequest,
    GrowthExportResponse,
)
from .permissions import (
    ADOPTION_READ,
    CAMPAIGN_MANAGE,
    CHANNEL_ATTRIBUTION_READ,
    EXPERIMENT_MANAGE,
    EXPORT_APPROVE,
    EXPORT_DOWNLOAD,
    EXPORT_REQUEST,
    READ,
)
from .service import (
    apply_campaign_command,
    apply_experiment_command,
    approve_export,
    download_export,
    get_export,
    issue_export_receipt,
    list_attribution,
    list_campaigns,
    list_cohorts,
    list_experiments,
    list_funnels,
    request_export,
)

router = APIRouter(tags=["platform-growth"])
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_IF_MATCH = re.compile(r'"(0|[1-9][0-9]*)"')
_FUNNEL_QUERY = {
    "view",
    "from",
    "to",
    "bucket",
    "dimension",
    "dimension_value",
    "formula_version",
    "cursor",
    "limit",
}
_COHORT_QUERY = {
    "view",
    "cohort_kind",
    "from",
    "to",
    "age_buckets",
    "dimension",
    "dimension_value",
    "formula_version",
    "cursor",
    "limit",
}
_CAMPAIGN_QUERY = {"status", "source", "channel", "from", "to", "cursor", "limit"}
_EXPERIMENT_QUERY = {"status", "surface", "metric", "from", "to", "cursor", "limit"}
_ATTRIBUTION_QUERY = {
    "view",
    "from",
    "to",
    "stage",
    "formula_version",
    "source",
    "channel",
    "campaign",
    "cursor",
    "limit",
}


def require_platform_growth(request: Request) -> PlatformContext:
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, WORKSPACE_SESSION_COOKIE)
        or _cookie_values(request, WORKSPACE_CSRF_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("platform.growth_credential_conflict")
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
        ("GET", "/platform/growth/funnels"): {READ, ADOPTION_READ},
        ("GET", "/platform/growth/cohorts"): {READ, ADOPTION_READ},
        ("GET", "/platform/growth/campaigns"): {READ},
        ("POST", "/platform/growth/campaigns"): {CAMPAIGN_MANAGE},
        ("GET", "/platform/growth/experiments"): {READ},
        ("POST", "/platform/growth/experiments"): {EXPERIMENT_MANAGE},
        ("GET", "/platform/growth/attribution"): {READ, CHANNEL_ATTRIBUTION_READ},
        ("POST", "/platform/growth/exports"): {EXPORT_REQUEST},
        ("GET", "/platform/growth/exports/{export_id}"): {
            EXPORT_REQUEST,
            EXPORT_APPROVE,
            EXPORT_DOWNLOAD,
        },
        ("POST", "/platform/growth/exports/{export_id}/approvals"): {EXPORT_APPROVE},
        ("POST", "/platform/growth/exports/{export_id}/receipts"): {EXPORT_DOWNLOAD},
        ("GET", "/platform/growth/exports/{export_id}/download"): {EXPORT_DOWNLOAD},
    }.get((request.method, path))
    if accepted is None or not accepted.intersection(context.permissions):
        raise ApiError("platform.permission_denied")
    return context


@router.get("/platform/growth/funnels", response_model=FunnelResponse)
async def get_growth_funnels(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_growth)],
) -> FunnelResponse:
    query = _query(request, _FUNNEL_QUERY)
    _reject_body(request)
    result = list_funnels(
        request.app.state.config,
        session_token=_platform_token(request),
        filters={key: value for key, value in query.items() if key not in {"limit", "cursor"}},
        limit=_limit(query, 200),
        cursor=query.get("cursor"),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.get("/platform/growth/cohorts", response_model=CohortResponse)
async def get_growth_cohorts(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_growth)],
) -> CohortResponse:
    query = _query(request, _COHORT_QUERY)
    _reject_body(request)
    result = list_cohorts(
        request.app.state.config,
        session_token=_platform_token(request),
        filters={key: value for key, value in query.items() if key not in {"limit", "cursor"}},
        limit=_limit(query, 200),
        cursor=query.get("cursor"),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.get("/platform/growth/campaigns", response_model=CampaignListResponse)
async def get_growth_campaigns(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_growth)],
) -> CampaignListResponse:
    query = _query(request, _CAMPAIGN_QUERY)
    _reject_body(request)
    result = list_campaigns(
        request.app.state.config,
        session_token=_platform_token(request),
        filters={key: value for key, value in query.items() if key not in {"limit", "cursor"}},
        limit=_limit(query, 100),
        cursor=query.get("cursor"),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post("/platform/growth/campaigns", response_model=CampaignResponse)
async def post_growth_campaign(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_growth)],
) -> CampaignResponse:
    _query(request, set())
    command = _model(await _strict_json(request, "platform.request_invalid"), CampaignCommand)
    if _if_match(request) != command.expected_revision:
        raise ApiError("platform.growth_state_conflict")
    result = apply_campaign_command(
        request.app.state.config,
        command,
        session_token=_platform_token(request),
        csrf_token=_platform_csrf(request),
        step_up_grant=_step_up(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = _etag(result.campaign.revision)
    if command.operation == "create" and not result.idempotent_replay:
        response.status_code = 201
    _private(response)
    return result


@router.get("/platform/growth/experiments", response_model=ExperimentListResponse)
async def get_growth_experiments(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_growth)],
) -> ExperimentListResponse:
    query = _query(request, _EXPERIMENT_QUERY)
    _reject_body(request)
    result = list_experiments(
        request.app.state.config,
        session_token=_platform_token(request),
        filters={key: value for key, value in query.items() if key not in {"limit", "cursor"}},
        limit=_limit(query, 100),
        cursor=query.get("cursor"),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post("/platform/growth/experiments", response_model=ExperimentResponse)
async def post_growth_experiment(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_growth)],
) -> ExperimentResponse:
    _query(request, set())
    command = _model(await _strict_json(request, "platform.request_invalid"), ExperimentCommand)
    if _if_match(request) != command.expected_revision:
        raise ApiError("platform.growth_state_conflict")
    result = apply_experiment_command(
        request.app.state.config,
        command,
        session_token=_platform_token(request),
        csrf_token=_platform_csrf(request),
        step_up_grant=_step_up(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = _etag(result.experiment.revision)
    if command.operation == "create" and not result.idempotent_replay:
        response.status_code = 201
    _private(response)
    return result


@router.get("/platform/growth/attribution", response_model=AttributionResponse)
async def get_growth_attribution(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_growth)],
) -> AttributionResponse:
    query = _query(request, _ATTRIBUTION_QUERY)
    _reject_body(request)
    result = list_attribution(
        request.app.state.config,
        session_token=_platform_token(request),
        filters={key: value for key, value in query.items() if key not in {"limit", "cursor"}},
        limit=_limit(query, 200),
        cursor=query.get("cursor"),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post("/platform/growth/exports", response_model=GrowthExportResponse, status_code=202)
async def post_growth_export(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_growth)],
) -> GrowthExportResponse:
    _query(request, set())
    manifest = _model(await _strict_json(request, "platform.request_invalid"), GrowthExportRequest)
    result = request_export(
        request.app.state.config,
        manifest,
        session_token=_platform_token(request),
        csrf_token=_platform_csrf(request),
        step_up_grant=_step_up(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.get("/platform/growth/exports/{export_id}", response_model=GrowthExportResponse)
async def get_growth_export(
    export_id: str,
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_growth)],
) -> GrowthExportResponse:
    _query(request, set())
    _reject_body(request)
    result = get_export(
        request.app.state.config,
        session_token=_platform_token(request),
        export_id=export_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post(
    "/platform/growth/exports/{export_id}/approvals",
    response_model=GrowthExportResponse,
)
async def post_growth_export_approval(
    export_id: str,
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_growth)],
) -> GrowthExportResponse:
    _query(request, set())
    command = _model(
        await _strict_json(request, "platform.request_invalid"),
        GrowthExportApprovalCommand,
    )
    if _if_match(request) != command.expected_revision:
        raise ApiError("platform.growth_state_conflict")
    result = approve_export(
        request.app.state.config,
        command,
        session_token=_platform_token(request),
        csrf_token=_platform_csrf(request),
        step_up_grant=_step_up(request),
        idempotency_key=_idempotency_key(request),
        export_id=export_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = _etag(result.export.revision)
    _private(response)
    return result


@router.post(
    "/platform/growth/exports/{export_id}/receipts",
    response_model=GrowthExportReceiptResponse,
)
async def post_growth_export_receipt(
    export_id: str,
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_growth)],
) -> GrowthExportReceiptResponse:
    _query(request, set())
    _model(await _strict_json(request, "platform.request_invalid"), GrowthExportReceiptCommand)
    result = issue_export_receipt(
        request.app.state.config,
        session_token=_platform_token(request),
        csrf_token=_platform_csrf(request),
        step_up_grant=_step_up(request),
        export_id=export_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.get("/platform/growth/exports/{export_id}/download")
async def get_growth_export_download(
    export_id: str,
    request: Request,
    _context: Annotated[PlatformContext, Depends(require_platform_growth)],
) -> Response:
    _query(request, set())
    _reject_body(request)
    values = _raw_headers(request, b"x-growth-export-receipt")
    if len(values) != 1 or not valid_opaque_value(values[0]):
        raise ApiError("platform.growth_export_unavailable")
    content, filename = download_export(
        request.app.state.config,
        session_token=_platform_token(request),
        export_id=export_id,
        receipt=values[0],
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store, max-age=0",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
        },
    )


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
        raise ApiError("platform.growth_state_conflict")
    return int(values[0][1:-1])


def _idempotency_key(request: Request) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY.fullmatch(values[0]) is None:
        raise ApiError("platform.idempotency_conflict")
    return values[0]


def _step_up(request: Request) -> str:
    values = _raw_headers(request, b"x-step-up-grant")
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


def _limit(query: dict[str, str], maximum: int) -> int:
    raw = query.get("limit", "50")
    try:
        value = int(raw)
    except ValueError:
        raise ApiError("platform.request_invalid") from None
    if str(value) != raw or not 1 <= value <= maximum:
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
