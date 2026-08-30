"""Separated browser and machine HTTP boundaries for PRD-135 compliance."""

from __future__ import annotations

import re

from fastapi import APIRouter, Request, Response, status
from metahub_shared.runtime.errors import ApiError
from starlette.concurrency import run_in_threadpool

from metahub_control.identity.service import valid_opaque_value
from metahub_control.machine_identity.service import authenticate_machine_request
from metahub_control.machine_identity.transport import (
    direct_peer_ip,
    optional_machine_bearer,
    reject_machine_cookies,
    verified_mtls_fingerprint,
)
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
    SUPPORT_SESSION_COOKIE,
)
from metahub_control.saas_billing.routes import (
    _cookie_values,
    _private,
    _raw_headers,
    _request_id,
    _require_origin,
    _required_csrf,
    _strict_json,
    _trace_id,
)
from metahub_control.workspace_access.constants import WORKSPACE_SESSION_COOKIE

from .models import (
    ComplianceEvidenceResponse,
    ComplianceEvidenceSubmitRequest,
    ComplianceExceptionCommand,
    ComplianceExceptionResponse,
    ComplianceReportListResponse,
    PlatformComplianceControlsResponse,
)
from .service import (
    apply_exception_command,
    get_platform_controls,
    list_workspace_reports,
    submit_evidence,
)

router = APIRouter(tags=["compliance"])
_WORKSPACE_ID = re.compile(r"^wsp_[0-9a-f]{32}$")
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")


@router.get("/platform/compliance/controls", response_model=PlatformComplianceControlsResponse)
async def get_controls(request: Request, response: Response) -> PlatformComplianceControlsResponse:
    token, _csrf = _platform_credentials(request, write=False)
    workspace_id = _optional_workspace_query(request)
    result = await run_in_threadpool(
        get_platform_controls,
        request.app.state.config,
        platform_session_token=token,
        workspace_id=workspace_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post(
    "/platform/compliance/evidence",
    response_model=ComplianceEvidenceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_evidence(request: Request, response: Response) -> ComplianceEvidenceResponse:
    _reject_query(request)
    reject_machine_cookies(request)
    access_token = optional_machine_bearer(request)
    if access_token is None:
        raise ApiError("machine_auth.invalid")
    payload = _model(
        await _strict_json(request, "compliance.request_invalid"),
        ComplianceEvidenceSubmitRequest,
    )
    request_id = _request_id(request)
    trace_id = _trace_id(request)
    machine = await run_in_threadpool(
        authenticate_machine_request,
        request.app.state.config,
        access_token=access_token,
        method="POST",
        path="/platform/compliance/evidence",
        peer_ip=direct_peer_ip(request),
        verified_mtls_fingerprint=verified_mtls_fingerprint(request),
        request_id=request_id,
        trace_id=trace_id,
    )
    result = await run_in_threadpool(
        submit_evidence,
        request.app.state.config,
        payload,
        machine=machine,
        access_token=access_token,
        idempotency_key=_idempotency_key(request),
        request_id=request_id,
        trace_id=trace_id,
    )
    if result.idempotent_replay:
        response.status_code = status.HTTP_200_OK
    _private(response)
    response.headers["Vary"] = "Authorization"
    return result


@router.get(
    "/workspaces/{workspace_id}/compliance-reports",
    response_model=ComplianceReportListResponse,
)
async def get_workspace_reports(
    workspace_id: str, request: Request, response: Response
) -> ComplianceReportListResponse:
    checked_id = _workspace_id(workspace_id)
    _reject_query_and_body(request)
    token = _workspace_credentials(request)
    result = await run_in_threadpool(
        list_workspace_reports,
        request.app.state.config,
        checked_id,
        workspace_session_token=token,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post("/platform/compliance/exceptions", response_model=ComplianceExceptionResponse)
async def post_exception(request: Request, response: Response) -> ComplianceExceptionResponse:
    token, csrf = _platform_credentials(request, write=True)
    _reject_query(request)
    payload = _model(
        await _strict_json(request, "compliance.request_invalid"),
        ComplianceExceptionCommand,
    )
    result = await run_in_threadpool(
        apply_exception_command,
        request.app.state.config,
        payload,
        platform_session_token=token,
        platform_csrf_token=csrf,
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


def _platform_credentials(request: Request, *, write: bool) -> tuple[str, str]:
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, WORKSPACE_SESSION_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("platform.credential_conflict")
    values = _cookie_values(request, PLATFORM_SESSION_COOKIE)
    if len(values) != 1 or not valid_opaque_value(values[0]):
        raise ApiError("platform.session_required")
    if not write:
        return values[0], ""
    _require_origin(request, "platform.origin_invalid")
    return values[0], _required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid")


def _workspace_credentials(request: Request) -> str:
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, PLATFORM_SESSION_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("compliance.credential_conflict")
    values = _cookie_values(request, WORKSPACE_SESSION_COOKIE)
    if len(values) != 1 or not valid_opaque_value(values[0]):
        raise ApiError("workspace_session.required")
    return values[0]


def _optional_workspace_query(request: Request) -> str | None:
    items = request.query_params.multi_items()
    if any(key != "workspace_id" for key, _value in items) or len(items) > 1:
        raise ApiError("compliance.request_invalid")
    value = request.query_params.get("workspace_id")
    if value is None:
        return None
    return _workspace_id(value)


def _model(payload: dict[str, object], model_type):
    try:
        return model_type.model_validate(payload)
    except ValueError:
        raise ApiError("compliance.request_invalid") from None


def _workspace_id(value: str) -> str:
    if _WORKSPACE_ID.fullmatch(value) is None:
        raise ApiError("compliance.report_not_found")
    return value


def _idempotency_key(request: Request) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY.fullmatch(values[0]) is None:
        raise ApiError("compliance.idempotency_invalid")
    return values[0]


def _reject_query(request: Request) -> None:
    if request.query_params.multi_items():
        raise ApiError("compliance.request_invalid")


def _reject_body(request: Request) -> None:
    if _raw_headers(request, b"transfer-encoding") or _raw_headers(request, b"content-type"):
        raise ApiError("compliance.request_invalid")
    lengths = _raw_headers(request, b"content-length")
    if len(lengths) > 1 or (lengths and lengths[0] != "0"):
        raise ApiError("compliance.request_invalid")


def _reject_query_and_body(request: Request) -> None:
    _reject_query(request)
    _reject_body(request)


__all__ = ["router"]
