"""Strict authenticated HTTP routes for PRD-074 Batch jobs."""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from metahub_shared.runtime.errors import ApiError
from pydantic import BaseModel, ValidationError

from metahub_control.auth import (
    AuthContext,
    require_batch_canceller,
    require_batch_creator,
    require_batch_reader,
    require_batch_reconciler,
    require_batch_task_reader,
)

from .models import (
    BatchJobListResponse,
    BatchJobResponse,
    BatchReconciliationResponse,
    BatchTaskListResponse,
    BatchTaskResultResponse,
    CreateBatchJobRequest,
    ReconcileBatchTaskRequest,
)
from .service import (
    cancel_batch_job,
    create_batch_job,
    get_batch_job,
    get_batch_task_result,
    list_batch_jobs,
    list_batch_tasks,
    reconcile_batch_task,
)

MAX_BATCH_CREATE_BODY_BYTES = 256 * 1024
MAX_BATCH_COMMAND_BODY_BYTES = 16 * 1024


class _DuplicateJsonKey(ValueError):
    pass


class BatchRoute(APIRoute):
    """Collapse framework validation details before they can echo sensitive inputs."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError:
                raise ApiError("batch.request_invalid") from None

        return handler


router = APIRouter(prefix="/batch-jobs", tags=["batch-jobs"], route_class=BatchRoute)


@router.post(
    "",
    response_model=BatchJobResponse,
    status_code=status.HTTP_201_CREATED,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": CreateBatchJobRequest.model_json_schema()}},
        }
    },
)
async def post_batch_job(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_batch_creator)],
) -> BatchJobResponse:
    _reject_query(request)
    idempotency_key = _unique_header(request, b"idempotency-key")
    payload = await _parse_json(request, CreateBatchJobRequest, MAX_BATCH_CREATE_BODY_BYTES)
    _no_store(response)
    return create_batch_job(
        request.app.state.config,
        context,
        payload,
        idempotency_key=idempotency_key,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("", response_model=BatchJobListResponse)
async def get_batch_jobs(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_batch_reader)],
) -> BatchJobListResponse:
    query = _strict_query(request, {"status", "limit", "cursor"})
    _no_store(response)
    return list_batch_jobs(
        request.app.state.config,
        context,
        status=query.get("status"),
        limit=query.get("limit"),
        cursor=query.get("cursor"),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/{batch_job_id}", response_model=BatchJobResponse)
async def get_batch_job_route(
    request: Request,
    response: Response,
    batch_job_id: str,
    context: Annotated[AuthContext, Depends(require_batch_reader)],
) -> BatchJobResponse:
    _reject_query(request)
    _no_store(response)
    return get_batch_job(
        request.app.state.config,
        context,
        batch_job_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/{batch_job_id}/tasks", response_model=BatchTaskListResponse)
async def get_batch_tasks(
    request: Request,
    response: Response,
    batch_job_id: str,
    context: Annotated[AuthContext, Depends(require_batch_task_reader)],
) -> BatchTaskListResponse:
    query = _strict_query(request, {"status", "limit", "cursor"})
    _no_store(response)
    return list_batch_tasks(
        request.app.state.config,
        context,
        batch_job_id,
        status=query.get("status"),
        limit=query.get("limit"),
        cursor=query.get("cursor"),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get(
    "/{batch_job_id}/tasks/{batch_task_id}/result",
    response_model=BatchTaskResultResponse,
)
async def get_batch_result(
    request: Request,
    response: Response,
    batch_job_id: str,
    batch_task_id: str,
    context: Annotated[AuthContext, Depends(require_batch_task_reader)],
) -> BatchTaskResultResponse:
    _reject_query(request)
    _no_store(response)
    return get_batch_task_result(
        request.app.state.config,
        context,
        batch_job_id,
        batch_task_id,
    )


@router.post("/{batch_job_id}/cancel", response_model=BatchJobResponse)
async def post_batch_cancel(
    request: Request,
    response: Response,
    batch_job_id: str,
    context: Annotated[AuthContext, Depends(require_batch_canceller)],
) -> BatchJobResponse:
    _reject_query(request)
    await _read_body(request, maximum=0, allow_empty=True)
    _no_store(response)
    return cancel_batch_job(
        request.app.state.config,
        context,
        batch_job_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post(
    "/{batch_job_id}/tasks/{batch_task_id}/reconciliation",
    response_model=BatchReconciliationResponse,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": ReconcileBatchTaskRequest.model_json_schema()}
            },
        }
    },
)
async def post_batch_reconciliation(
    request: Request,
    response: Response,
    batch_job_id: str,
    batch_task_id: str,
    context: Annotated[AuthContext, Depends(require_batch_reconciler)],
) -> BatchReconciliationResponse:
    _reject_query(request)
    idempotency_key = _unique_header(request, b"idempotency-key")
    payload = await _parse_json(
        request,
        ReconcileBatchTaskRequest,
        MAX_BATCH_COMMAND_BODY_BYTES,
    )
    _no_store(response)
    return reconcile_batch_task(
        request.app.state.config,
        context,
        batch_job_id,
        batch_task_id,
        payload,
        idempotency_key=idempotency_key,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


async def _parse_json[PayloadT: BaseModel](
    request: Request,
    model: type[PayloadT],
    maximum: int,
) -> PayloadT:
    media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media_type != "application/json":
        raise ApiError("batch.request_invalid")
    try:
        body = await _read_body(request, maximum=maximum, allow_empty=False)
        value = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_object_without_duplicates,
        )
        return model.model_validate(value)
    except (
        json.JSONDecodeError,
        UnicodeError,
        ValidationError,
        _DuplicateJsonKey,
        RecursionError,
        TypeError,
    ):
        raise ApiError("batch.request_invalid") from None


async def _read_body(request: Request, *, maximum: int, allow_empty: bool) -> bytes:
    declared = _content_length(request)
    if declared is not None and (declared > maximum or (declared == 0 and not allow_empty)):
        raise ApiError("batch.request_invalid")
    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > maximum:
                raise ApiError("batch.request_invalid")
            body.extend(chunk)
    except ApiError:
        raise
    except Exception:
        raise ApiError("batch.request_invalid") from None
    if (not body and not allow_empty) or (declared is not None and declared != len(body)):
        raise ApiError("batch.request_invalid")
    return bytes(body)


def _content_length(request: Request) -> int | None:
    values = [
        value.decode("latin-1")
        for name, value in request.scope.get("headers", ())
        if name.lower() == b"content-length"
    ]
    if not values:
        return None
    value = values[0]
    if (
        len(values) != 1
        or not value
        or len(value) > 12
        or any(character < "0" or character > "9" for character in value)
    ):
        raise ApiError("batch.request_invalid")
    return int(value)


def _unique_header(request: Request, name: bytes) -> str | None:
    values = [
        value.decode("latin-1")
        for key, value in request.scope.get("headers", ())
        if key.lower() == name
    ]
    if len(values) != 1:
        return None
    return values[0]


def _strict_query(request: Request, allowed: set[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key not in allowed or key in result or not value:
            raise ApiError("batch.request_invalid")
        result[key] = value
    return result


def _reject_query(request: Request) -> None:
    if request.query_params.multi_items():
        raise ApiError("batch.request_invalid")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKey
        value[key] = item
    return value


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, private"


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", "")


__all__ = ["router"]
