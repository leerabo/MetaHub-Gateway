"""Platform Cookie boundary for fixed-profile tenant-isolation assurance."""

from __future__ import annotations

import re

from fastapi import APIRouter, Request, Response, status
from metahub_shared.runtime.errors import ApiError
from starlette.concurrency import run_in_threadpool

from metahub_control.identity.service import valid_opaque_value
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
    AssessmentCreateCommand,
    AssessmentResponse,
    RemediationCommand,
    RemediationResponse,
)
from .service import apply_remediation, create_security_assessment, get_security_assessment

router = APIRouter(tags=["security-assurance"])
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")


@router.post(
    "/platform/security/assessments",
    response_model=AssessmentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_assessment(request: Request, response: Response) -> AssessmentResponse:
    token, csrf = _platform_credentials(request, write=True)
    _reject_query(request)
    command = _model(
        await _strict_json(request, "security_assurance.request_invalid"),
        AssessmentCreateCommand,
    )
    if _if_match(request) != command.profile_version:
        raise ApiError("security_assurance.revision_conflict")
    result = await run_in_threadpool(
        create_security_assessment,
        request.app.state.config,
        command,
        platform_session_token=token,
        platform_csrf_token=csrf,
        step_up_grant=_step_up_grant(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
        runner=getattr(request.app.state, "security_assurance_runner", None),
    )
    if result.idempotent_replay:
        response.status_code = status.HTTP_200_OK
    _assessment_headers(response, result)
    return result


@router.get(
    "/platform/security/assessments/{assessment_id}",
    response_model=AssessmentResponse,
)
async def get_assessment(
    assessment_id: str, request: Request, response: Response
) -> AssessmentResponse:
    token, _csrf = _platform_credentials(request, write=False)
    _reject_query_and_body(request)
    result = await run_in_threadpool(
        get_security_assessment,
        request.app.state.config,
        assessment_id,
        platform_session_token=token,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _assessment_headers(response, result)
    return result


@router.post(
    "/platform/security/findings/{finding_id}/remediations",
    response_model=RemediationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_remediation(
    finding_id: str, request: Request, response: Response
) -> RemediationResponse:
    token, csrf = _platform_credentials(request, write=True)
    _reject_query(request)
    command = _model(
        await _strict_json(request, "security_assurance.request_invalid"),
        RemediationCommand,
    )
    if _if_match(request) != command.expected_revision:
        raise ApiError("security_assurance.revision_conflict")
    result = await run_in_threadpool(
        apply_remediation,
        request.app.state.config,
        finding_id,
        command,
        platform_session_token=token,
        platform_csrf_token=csrf,
        step_up_grant=_step_up_grant(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = status.HTTP_200_OK
    response.headers["ETag"] = f'"{result.finding.revision}"'
    _private(response)
    return result


def _platform_credentials(request: Request, *, write: bool) -> tuple[str, str]:
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, WORKSPACE_SESSION_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("security_assurance.credential_conflict")
    values = _cookie_values(request, PLATFORM_SESSION_COOKIE)
    if len(values) != 1 or not valid_opaque_value(values[0]):
        raise ApiError("platform.session_required")
    if not write:
        return values[0], ""
    _require_origin(request, "platform.origin_invalid")
    return values[0], _required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid")


def _model(payload: dict[str, object], model_type):
    try:
        return model_type.model_validate(payload)
    except ValueError:
        raise ApiError("security_assurance.request_invalid") from None


def _idempotency_key(request: Request) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY.fullmatch(values[0]) is None:
        raise ApiError("security_assurance.idempotency_invalid")
    return values[0]


def _if_match(request: Request) -> int:
    values = _raw_headers(request, b"if-match")
    if len(values) != 1 or re.fullmatch(r'"[1-9][0-9]*"', values[0]) is None:
        raise ApiError("security_assurance.revision_required")
    return int(values[0][1:-1])


def _step_up_grant(request: Request) -> str:
    values = _raw_headers(request, b"x-step-up-grant")
    if len(values) != 1 or not valid_opaque_value(values[0]):
        raise ApiError("step_up.required")
    return values[0]


def _reject_query(request: Request) -> None:
    if request.query_params.multi_items():
        raise ApiError("security_assurance.request_invalid")


def _reject_query_and_body(request: Request) -> None:
    _reject_query(request)
    if _raw_headers(request, b"transfer-encoding") or _raw_headers(request, b"content-type"):
        raise ApiError("security_assurance.request_invalid")
    lengths = _raw_headers(request, b"content-length")
    if len(lengths) > 1 or (lengths and lengths[0] != "0"):
        raise ApiError("security_assurance.request_invalid")


def _assessment_headers(response: Response, result: AssessmentResponse) -> None:
    response.headers["ETag"] = f'"{result.assessment.revision}"'
    _private(response)


__all__ = ["router"]
