"""Credential-isolated HTTP boundaries for PRD-131 migration operations."""

from __future__ import annotations

import re

from fastapi import APIRouter, Request, Response, status
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import clear_workspace_cookies_on_error
from starlette.concurrency import run_in_threadpool

from metahub_control.identity.service import valid_opaque_value
from metahub_control.platform_access.constants import (
    PLATFORM_SESSION_COOKIE,
    SUPPORT_SESSION_COOKIE,
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
    MigrationAssessmentCreateRequest,
    MigrationAssessmentResponse,
    MigrationDifferenceListResponse,
    MigrationRunCreateRequest,
    MigrationRunResponse,
    MigrationTransitionRequest,
)
from .service import (
    create_migration_assessment,
    create_migration_run,
    execute_migration_cutover,
    execute_migration_rollback,
    list_migration_differences,
)

router = APIRouter(prefix="/migrations", tags=["migration-operations"])
_ID_RE = re.compile(r"^(?:mas|mrn)_[0-9a-f]{32}$")
_RUN_ID_RE = re.compile(r"^mrn_[0-9a-f]{32}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")


@router.post(
    "/assessments",
    response_model=MigrationAssessmentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_migration_assessment(
    request: Request, response: Response
) -> MigrationAssessmentResponse:
    _prepare_workspace(request, write=True)
    _reject_query(request)
    payload = _model(
        await _strict_json(request, "migration.request_invalid"),
        MigrationAssessmentCreateRequest,
    )
    result = await run_in_threadpool(
        create_migration_assessment,
        request.app.state.config,
        payload,
        session_token=_workspace_cookie(request),
        csrf_token=_workspace_csrf(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = status.HTTP_200_OK
    response.headers["ETag"] = f'"{result.assessment.revision}"'
    _private(response)
    return result


@router.post("/runs", response_model=MigrationRunResponse, status_code=status.HTTP_201_CREATED)
async def post_migration_run(request: Request, response: Response) -> MigrationRunResponse:
    _prepare_workspace(request, write=True)
    _reject_query(request)
    payload = _model(
        await _strict_json(request, "migration.request_invalid"),
        MigrationRunCreateRequest,
    )
    if _if_match(request) != payload.expected_assessment_revision:
        raise ApiError("migration.revision_conflict")
    result = await run_in_threadpool(
        create_migration_run,
        request.app.state.config,
        payload,
        session_token=_workspace_cookie(request),
        csrf_token=_workspace_csrf(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = status.HTTP_200_OK
    response.headers["ETag"] = f'"{result.run.revision}"'
    _private(response)
    return result


@router.get("/{migration_id}/differences", response_model=MigrationDifferenceListResponse)
async def get_migration_differences(
    migration_id: str, request: Request, response: Response
) -> MigrationDifferenceListResponse:
    if _ID_RE.fullmatch(migration_id) is None:
        raise ApiError("migration.not_found")
    _prepare_difference_read(request)
    _reject_query_and_body(request)
    workspace_tokens = _cookie_values(request, WORKSPACE_SESSION_COOKIE)
    support_tokens = _cookie_values(request, SUPPORT_SESSION_COOKIE)
    result = await run_in_threadpool(
        list_migration_differences,
        request.app.state.config,
        migration_id,
        workspace_session_token=workspace_tokens[0] if workspace_tokens else None,
        support_session_token=support_tokens[0] if support_tokens else None,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.post("/{run_id}/cutover", response_model=MigrationRunResponse)
async def post_migration_cutover(
    run_id: str, request: Request, response: Response
) -> MigrationRunResponse:
    return await _post_transition(
        run_id,
        request,
        response,
        operation="cutover",
    )


@router.post("/{run_id}/rollback", response_model=MigrationRunResponse)
async def post_migration_rollback(
    run_id: str, request: Request, response: Response
) -> MigrationRunResponse:
    return await _post_transition(
        run_id,
        request,
        response,
        operation="rollback",
    )


async def _post_transition(
    run_id: str,
    request: Request,
    response: Response,
    *,
    operation: str,
) -> MigrationRunResponse:
    if _RUN_ID_RE.fullmatch(run_id) is None:
        raise ApiError("migration.not_found")
    _prepare_workspace(request, write=True)
    _reject_query(request)
    payload = _model(
        await _strict_json(request, "migration.request_invalid"),
        MigrationTransitionRequest,
    )
    if _if_match(request) != payload.expected_revision:
        raise ApiError("migration.revision_conflict")
    service = execute_migration_cutover if operation == "cutover" else execute_migration_rollback
    result = await run_in_threadpool(
        service,
        request.app.state.config,
        run_id,
        payload,
        session_token=_workspace_cookie(request),
        csrf_token=_workspace_csrf(request),
        step_up_grant=_step_up_grant(request),
        idempotency_key=_idempotency_key(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.run.revision}"'
    _private(response)
    return result


def _prepare_workspace(request: Request, *, write: bool) -> None:
    clear_workspace_cookies_on_error(request)
    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, PLATFORM_SESSION_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("workspace_session.credential_conflict")
    if write:
        _require_origin(request, "migration.origin_invalid")
        _workspace_csrf(request)
    _workspace_cookie(request)


def _prepare_difference_read(request: Request) -> None:
    clear_workspace_cookies_on_error(request)
    if _raw_headers(request, b"authorization") or _cookie_values(request, PLATFORM_SESSION_COOKIE):
        raise ApiError("workspace_session.credential_conflict")
    workspace = _cookie_values(request, WORKSPACE_SESSION_COOKIE)
    support = _cookie_values(request, SUPPORT_SESSION_COOKIE)
    if len(workspace) + len(support) != 1:
        raise ApiError(
            "workspace_session.required"
            if not workspace and not support
            else "workspace_session.credential_conflict"
        )
    token = workspace[0] if workspace else support[0]
    if not valid_opaque_value(token):
        raise ApiError("workspace_session.invalid" if workspace else "support.session_invalid")


def _model(payload: object, model_type):
    try:
        return model_type.model_validate(payload)
    except ValueError:
        raise ApiError("migration.request_invalid") from None


def _workspace_cookie(request: Request) -> str:
    return _required_cookie(request, WORKSPACE_SESSION_COOKIE, "workspace_session.required")


def _workspace_csrf(request: Request) -> str:
    return _required_csrf(request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid")


def _idempotency_key(request: Request) -> str:
    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY_RE.fullmatch(values[0]) is None:
        raise ApiError("migration.request_invalid")
    return values[0]


def _step_up_grant(request: Request) -> str:
    values = _raw_headers(request, b"x-step-up-grant")
    if len(values) != 1 or not valid_opaque_value(values[0]):
        raise ApiError("step_up.required")
    return values[0]


def _if_match(request: Request) -> int:
    values = _raw_headers(request, b"if-match")
    if len(values) != 1 or re.fullmatch(r'"[1-9][0-9]*"', values[0]) is None:
        raise ApiError("migration.revision_required")
    return int(values[0][1:-1])


def _reject_query(request: Request) -> None:
    if request.query_params.multi_items():
        raise ApiError("migration.request_invalid")


def _reject_body(request: Request) -> None:
    if _raw_headers(request, b"transfer-encoding") or _raw_headers(request, b"content-type"):
        raise ApiError("migration.request_invalid")
    lengths = _raw_headers(request, b"content-length")
    if len(lengths) > 1 or (lengths and lengths[0] != "0"):
        raise ApiError("migration.request_invalid")


def _reject_query_and_body(request: Request) -> None:
    _reject_query(request)
    _reject_body(request)


__all__ = ["router"]
