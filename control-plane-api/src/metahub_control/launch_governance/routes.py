"""Strict four-route Platform boundary for release-candidate governance."""

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

from .ga_models import (
    GaEvidenceCommand,
    GaEvidenceResponse,
    GaGateListResponse,
    GaLaunchCommand,
    GaLaunchResponse,
    GaReviewCommand,
    GaReviewResponse,
    GaRollbackCommand,
    GaRollbackResponse,
)
from .ga_service import (
    apply_ga_rollback,
    list_ga_gates,
    promote_ga,
    record_ga_review,
    submit_ga_evidence,
)
from .models import (
    EvidenceCommand,
    EvidenceResponse,
    GateListResponse,
    LaunchCommand,
    LaunchResponse,
    RollbackCommand,
    RollbackResponse,
)
from .service import (
    apply_launch_command,
    apply_launch_rollback,
    list_launch_gates,
    submit_launch_evidence,
)

router = APIRouter(tags=["launch-governance"])
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")


@router.get("/platform/launch-gates", response_model=GateListResponse)
async def get_launch_gates(request: Request, response: Response) -> GateListResponse:
    token, _csrf = _platform_credentials(request, write=False)
    _reject_query_and_body(request)
    result = await run_in_threadpool(
        list_launch_gates,
        request.app.state.config,
        platform_session_token=token,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.candidate is not None:
        response.headers["ETag"] = f'"{result.candidate.revision}"'
    _private(response)
    return result


@router.post(
    "/platform/launch-gates/{launch_gate_id}/evidence",
    response_model=EvidenceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_launch_evidence(
    launch_gate_id: str, request: Request, response: Response
) -> EvidenceResponse:
    token, csrf = _platform_credentials(request, write=True)
    _reject_query(request)
    command = _model(await _strict_json(request, "launch.request_invalid"), EvidenceCommand)
    if _if_match(request) != command.expected_revision:
        raise ApiError("launch.revision_conflict")
    result = await run_in_threadpool(
        submit_launch_evidence,
        request.app.state.config,
        launch_gate_id,
        command,
        platform_session_token=token,
        platform_csrf_token=csrf,
        step_up_grant=_step_up_grant(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
        verifier=getattr(request.app.state, "launch_evidence_verifier", None),
    )
    if result.idempotent_replay:
        response.status_code = status.HTTP_200_OK
    response.headers["ETag"] = f'"{result.candidate.revision}"'
    _private(response)
    return result


@router.post(
    "/platform/launches",
    response_model=LaunchResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_launch(request: Request, response: Response) -> LaunchResponse:
    token, csrf = _platform_credentials(request, write=True)
    _reject_query(request)
    command = _model(await _strict_json(request, "launch.request_invalid"), LaunchCommand)
    revision = _if_match(request)
    if (command.operation == "create" and revision != 1) or (
        command.operation != "create" and revision != command.expected_revision
    ):
        raise ApiError("launch.revision_conflict")
    result = await run_in_threadpool(
        apply_launch_command,
        request.app.state.config,
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
    response.headers["ETag"] = f'"{result.candidate.revision}"'
    _private(response)
    return result


@router.post(
    "/platform/launches/{launch_candidate_id}/rollback",
    response_model=RollbackResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_launch_rollback(
    launch_candidate_id: str, request: Request, response: Response
) -> RollbackResponse:
    token, csrf = _platform_credentials(request, write=True)
    _reject_query(request)
    command = _model(await _strict_json(request, "launch.request_invalid"), RollbackCommand)
    if _if_match(request) != command.expected_revision:
        raise ApiError("launch.revision_conflict")
    result = await run_in_threadpool(
        apply_launch_rollback,
        request.app.state.config,
        launch_candidate_id,
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
    response.headers["ETag"] = f'"{result.candidate.revision}"'
    _private(response)
    return result


@router.get("/platform/ga-gates", response_model=GaGateListResponse)
async def get_ga_gates(request: Request, response: Response) -> GaGateListResponse:
    token, _csrf = _platform_credentials(request, write=False)
    _reject_query_and_body(request)
    result = await run_in_threadpool(
        list_ga_gates,
        request.app.state.config,
        platform_session_token=token,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.candidate is not None:
        response.headers["ETag"] = f'"{result.candidate.revision}"'
    _private(response)
    return result


@router.post(
    "/platform/ga-gates/{ga_gate_id}/evidence",
    response_model=GaEvidenceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_ga_evidence(
    ga_gate_id: str, request: Request, response: Response
) -> GaEvidenceResponse:
    token, csrf = _platform_credentials(request, write=True)
    _reject_query(request)
    command = _model(await _strict_json(request, "launch.request_invalid"), GaEvidenceCommand)
    if _if_match(request) != command.expected_revision:
        raise ApiError("launch.revision_conflict")
    result = await run_in_threadpool(
        submit_ga_evidence,
        request.app.state.config,
        ga_gate_id,
        command,
        platform_session_token=token,
        platform_csrf_token=csrf,
        step_up_grant=_step_up_grant(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{command.expected_revision}"'
    _private(response)
    return result


@router.post(
    "/platform/ga-launches",
    response_model=GaLaunchResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_ga_launch(request: Request, response: Response) -> GaLaunchResponse:
    token, csrf = _platform_credentials(request, write=True)
    _reject_query(request)
    command = _model(await _strict_json(request, "launch.request_invalid"), GaLaunchCommand)
    if _if_match(request) != command.expected_revision:
        raise ApiError("launch.revision_conflict")
    result = await run_in_threadpool(
        promote_ga,
        request.app.state.config,
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
    response.headers["ETag"] = f'"{result.launch.revision}"'
    _private(response)
    return result


@router.post(
    "/platform/ga-launches/{ga_launch_id}/rollback",
    response_model=GaRollbackResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_ga_rollback(
    ga_launch_id: str, request: Request, response: Response
) -> GaRollbackResponse:
    token, csrf = _platform_credentials(request, write=True)
    _reject_query(request)
    command = _model(await _strict_json(request, "launch.request_invalid"), GaRollbackCommand)
    if _if_match(request) != command.expected_revision:
        raise ApiError("launch.revision_conflict")
    result = await run_in_threadpool(
        apply_ga_rollback,
        request.app.state.config,
        ga_launch_id,
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
    response.headers["ETag"] = f'"{result.launch.revision}"'
    _private(response)
    return result


@router.post(
    "/platform/ga-launches/{ga_launch_id}/review",
    response_model=GaReviewResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_ga_review(
    ga_launch_id: str, request: Request, response: Response
) -> GaReviewResponse:
    token, csrf = _platform_credentials(request, write=True)
    _reject_query(request)
    command = _model(await _strict_json(request, "launch.request_invalid"), GaReviewCommand)
    if _if_match(request) != command.expected_revision:
        raise ApiError("launch.revision_conflict")
    result = await run_in_threadpool(
        record_ga_review,
        request.app.state.config,
        ga_launch_id,
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
    response.headers["ETag"] = f'"{result.launch.revision}"'
    _private(response)
    return result


def _platform_credentials(request: Request, *, write: bool) -> tuple[str, str]:
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, WORKSPACE_SESSION_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("launch.credential_conflict")
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
        raise ApiError("launch.request_invalid") from None


def _idempotency_key(request: Request) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY.fullmatch(values[0]) is None:
        raise ApiError("launch.idempotency_invalid")
    return values[0]


def _if_match(request: Request) -> int:
    values = _raw_headers(request, b"if-match")
    if len(values) != 1 or re.fullmatch(r'"[1-9][0-9]*"', values[0]) is None:
        raise ApiError("launch.revision_required")
    return int(values[0][1:-1])


def _step_up_grant(request: Request) -> str:
    values = _raw_headers(request, b"x-step-up-grant")
    if len(values) != 1 or not valid_opaque_value(values[0]):
        raise ApiError("step_up.required")
    return values[0]


def _reject_query(request: Request) -> None:
    if request.query_params.multi_items():
        raise ApiError("launch.request_invalid")


def _reject_query_and_body(request: Request) -> None:
    _reject_query(request)
    if _raw_headers(request, b"transfer-encoding") or _raw_headers(request, b"content-type"):
        raise ApiError("launch.request_invalid")
    lengths = _raw_headers(request, b"content-length")
    if len(lengths) > 1 or (lengths and lengths[0] != "0"):
        raise ApiError("launch.request_invalid")


__all__ = ["router"]
