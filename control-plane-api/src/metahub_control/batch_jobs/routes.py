"""PRD-074 批任务控制面模块：负责租户内调度、领取、续租、取消、计量和可恢复事件投递。

定义：模块职责与安全边界。
所有调用路径均维持租户隔离、脱敏和失败追踪。

定义：模块职责与安全边界。
所有调用路径均维持租户隔离、脱敏和失败追踪。

Strict authenticated HTTP routes for PRD-074 Batch jobs.
"""

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

# 该常量组统一批任务状态、限额和错误语义，变更时同步契约与测试。
MAX_BATCH_CREATE_BODY_BYTES = 256 * 1024
MAX_BATCH_COMMAND_BODY_BYTES = 16 * 1024


class _DuplicateJsonKey(ValueError):
    """处理批任务接口边界。

    定义：_DuplicateJsonKey。
    后端统一校验租户权限并返回可追踪错误。
    """
    pass


class BatchRoute(APIRoute):
    """处理批任务接口边界。

    定义：BatchRoute。
    后端统一校验租户权限并返回可追踪错误。

    Collapse framework validation details before they can echo sensitive inputs.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """处理批任务接口边界。

        定义：get_route_handler。
        后端统一校验租户权限并返回可追踪错误。
        """
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            """处理批任务接口边界。

            定义：handler。
            后端统一校验租户权限并返回可追踪错误。
            """
            try:
                return await original(request)
            # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
            except RequestValidationError:
                # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
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
    """处理批任务接口边界。

    定义：post_batch_job。
    后端统一校验租户权限并返回可追踪错误。
    """
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
    """处理批任务接口边界。

    定义：get_batch_jobs。
    后端统一校验租户权限并返回可追踪错误。
    """
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
    """处理批任务接口边界。

    定义：get_batch_job_route。
    后端统一校验租户权限并返回可追踪错误。
    """
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
    """处理批任务接口边界。

    定义：get_batch_tasks。
    后端统一校验租户权限并返回可追踪错误。
    """
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
    """处理批任务接口边界。

    定义：get_batch_result。
    后端统一校验租户权限并返回可追踪错误。
    """
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
    """处理批任务接口边界。

    定义：post_batch_cancel。
    后端统一校验租户权限并返回可追踪错误。
    """
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
    """处理批任务接口边界。

    定义：post_batch_reconciliation。
    后端统一校验租户权限并返回可追踪错误。
    """
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
    """处理批任务接口边界。

    定义：_parse_json。
    后端统一校验租户权限并返回可追踪错误。
    """
    media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media_type != "application/json":
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
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
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.request_invalid") from None


async def _read_body(request: Request, *, maximum: int, allow_empty: bool) -> bytes:
    """处理批任务接口边界。

    定义：_read_body。
    后端统一校验租户权限并返回可追踪错误。
    """
    declared = _content_length(request)
    if declared is not None and (declared > maximum or (declared == 0 and not allow_empty)):
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.request_invalid")
    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > maximum:
                # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
                raise ApiError("batch.request_invalid")
            body.extend(chunk)
    # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
    except ApiError:
        raise
    except Exception:
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.request_invalid") from None
    if (not body and not allow_empty) or (declared is not None and declared != len(body)):
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.request_invalid")
    return bytes(body)


def _content_length(request: Request) -> int | None:
    """处理批任务接口边界。

    定义：_content_length。
    后端统一校验租户权限并返回可追踪错误。
    """
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
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.request_invalid")
    return int(value)


def _unique_header(request: Request, name: bytes) -> str | None:
    """处理批任务接口边界。

    定义：_unique_header。
    后端统一校验租户权限并返回可追踪错误。
    """
    values = [
        value.decode("latin-1")
        for key, value in request.scope.get("headers", ())
        if key.lower() == name
    ]
    if len(values) != 1:
        return None
    return values[0]


def _strict_query(request: Request, allowed: set[str]) -> dict[str, str]:
    """处理批任务接口边界。

    定义：_strict_query。
    后端统一校验租户权限并返回可追踪错误。
    """
    result: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key not in allowed or key in result or not value:
            # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
            raise ApiError("batch.request_invalid")
        result[key] = value
    return result


def _reject_query(request: Request) -> None:
    """处理批任务接口边界。

    定义：_reject_query。
    后端统一校验租户权限并返回可追踪错误。
    """
    if request.query_params.multi_items():
        # 异常统一为稳定错误并保留请求标识；不得泄露堆栈和敏感配置。
        raise ApiError("batch.request_invalid")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """处理批任务接口边界。

    定义：_object_without_duplicates。
    后端统一校验租户权限并返回可追踪错误。
    """
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKey
        value[key] = item
    return value


def _no_store(response: Response) -> None:
    """处理批任务接口边界。

    定义：_no_store。
    后端统一校验租户权限并返回可追踪错误。
    """
    response.headers["Cache-Control"] = "no-store, private"


def _request_id(request: Request) -> str:
    """处理批任务接口边界。

    定义：_request_id。
    后端统一校验租户权限并返回可追踪错误。
    """
    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """处理批任务接口边界。

    定义：_trace_id。
    后端统一校验租户权限并返回可追踪错误。
    """
    return getattr(request.state, "trace_id", "")


__all__ = ["router"]
