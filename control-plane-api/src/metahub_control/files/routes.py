"""PRD-072 文件上传、列表和详情 HTTP 路由。

所有入口先由 FastAPI 执行 Bearer Session 与实时数据库权限依赖，再解析 query、path 或
multipart 正文。上传路由不声明 ``UploadFile``，从框架装配层保证未授权请求不会触发正文
读取或明文临时 spool。
"""

from __future__ import annotations

from typing import Annotated

import anyio
from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from metahub_shared.runtime.errors import ApiError, build_error_response

from metahub_control.auth import AuthContext, require_file_reader, require_file_writer

from .models import FileListResponse, FileResponse
from .multipart import read_single_file_part
from .service import (
    DEFAULT_FILE_LIST_LIMIT,
    MAX_FILE_LIST_LIMIT,
    FileProcessingError,
    create_file,
    dispatch_file_events_safely,
    get_file,
    list_files,
)

router = APIRouter(prefix="/files", tags=["files"])

# 列表只接受状态、上限和签名游标三个字段；状态集合与数据库终态合同保持一致，禁止
# 客户端传 tenant、actor、排序列或任意过滤表达式影响租户隔离查询。
_LIST_QUERY_FIELDS = frozenset({"status", "limit", "cursor"})
_LIST_STATUSES = frozenset({"processing", "ready", "blocked", "failed"})


@router.post("", response_model=FileResponse, status_code=status.HTTP_201_CREATED)
async def post_file(
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    context: Annotated[AuthContext, Depends(require_file_writer)],
) -> FileResponse | Response:
    """上传、解析并安全扫描一个文件。

    参数：认证请求、响应、后台派发器和已具备 ``file.write`` 的上下文。返回：ready 文件
    安全投影；blocked/failed 通过统一错误响应返回，同时保留已提交 source event。
    """

    _reject_query(request)
    limiter = _processing_limiter(request)
    acquired = False
    try:
        try:
            # 容量必须在读取 multipart 正文前取得，防止并发请求无界占用 20 MiB 缓冲区。
            with anyio.fail_after(2.0):
                await limiter.acquire()
                acquired = True
        # 等待容量超时发生在正文读取前，必须映射为可重试忙碌而不是继续无界缓冲。
        except TimeoutError:
            # 固定错误码不暴露当前并发数或其他租户是否正在处理文件。
            raise ApiError("file.processing_busy") from None

        part = await read_single_file_part(
            request,
            maximum_file_bytes=request.app.state.config.file_max_upload_bytes,
        )
        request_id = _request_id(request)
        trace_id = _trace_id(request)
        try:
            result, events = await anyio.to_thread.run_sync(
                lambda: create_file(
                    request.app.state.config,
                    context,
                    part,
                    request_id=request_id,
                    trace_id=trace_id,
                )
            )
        # 该异常表示服务层已经提交安全失败终态，路由仍须带出 source event 派发任务。
        except FileProcessingError as exc:
            # 失败终态已提交时仍需投递 uploaded/scanned；后台任务只接收安全事件信封。
            if exc.events:
                background_tasks.add_task(
                    dispatch_file_events_safely,
                    request.app.state.config,
                    exc.events,
                    request_id=request_id,
                    trace_id=trace_id,
                )
            return _file_error_response(
                exc.error,
                request_id=request_id,
                trace_id=trace_id,
                background_tasks=background_tasks,
            )
        if events:
            background_tasks.add_task(
                dispatch_file_events_safely,
                request.app.state.config,
                events,
                request_id=request_id,
                trace_id=trace_id,
            )
        _no_store(response)
        return result
    finally:
        if acquired:
            # CapacityLimiter 要求由同一 ASGI task 释放；worker thread 只运行同步业务函数。
            limiter.release()


@router.get("", response_model=FileListResponse)
async def get_files(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_file_reader)],
) -> FileListResponse:
    """按稳定游标列出当前租户的文件处理安全摘要。"""

    await _require_empty_body(request)
    status_filter, limit, cursor = _parse_list_query(request)
    result = await anyio.to_thread.run_sync(
        lambda: list_files(
            request.app.state.config,
            context,
            status=status_filter,
            limit=limit,
            cursor=cursor,
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
    )
    _no_store(response)
    return result


@router.get("/{file_id}", response_model=FileResponse)
async def get_file_detail(
    file_id: str,
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_file_reader)],
) -> FileResponse:
    """读取当前租户的一个文件安全摘要；跨租户 ID 与不存在统一返回 404。"""

    _reject_query(request)
    await _require_empty_body(request)
    result = await anyio.to_thread.run_sync(
        lambda: get_file(
            request.app.state.config,
            context,
            file_id,
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
    )
    _no_store(response)
    return result


def _processing_limiter(request: Request) -> anyio.CapacityLimiter:
    """为当前应用实例惰性创建固定容量的同步解析门禁。"""

    configured = request.app.state.config.file_parse_concurrency
    existing = getattr(request.app.state, "file_processing_limiter", None)
    if existing is None:
        # 同一 event loop task 在首次赋值前没有 await，因而不会创建两个并发 limiter。
        existing = anyio.CapacityLimiter(configured)
        request.app.state.file_processing_limiter = existing
    if not isinstance(existing, anyio.CapacityLimiter) or existing.total_tokens != configured:
        # 运行中替换部署容量会拆分并发预算，必须失败关闭并由重启应用生效。
        raise ApiError("file.storage_unavailable")
    return existing


def _parse_list_query(request: Request) -> tuple[str | None, int, str | None]:
    """严格解析列表 exact-set query，拒绝重复键和客户端 owner/sort 注入。"""

    pairs = request.query_params.multi_items()
    if any(key not in _LIST_QUERY_FIELDS for key, _ in pairs):
        # 未知键可能试图注入 tenant、owner 或排序规则，必须按 exact-set 拒绝。
        raise ApiError("infra.validation_failed")
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        # 重复 query 在代理和框架中可能取首值或末值，禁止产生授权语义歧义。
        raise ApiError("infra.validation_failed")
    values = dict(pairs)
    status_filter = values.get("status")
    if status_filter is not None and status_filter not in _LIST_STATUSES:
        # 未知状态不能被静默当作空列表，否则会掩盖客户端合同漂移。
        raise ApiError("infra.validation_failed")
    limit = _parse_limit(values.get("limit"))
    cursor = values.get("cursor")
    if cursor is not None and (not cursor or len(cursor) > 2_048 or not cursor.isascii()):
        # 游标先做有界 ASCII 门禁，再由服务层验证租户和授权指纹签名。
        raise ApiError("infra.validation_failed")
    return status_filter, limit, cursor


def _parse_limit(value: str | None) -> int:
    """解析文件列表的规范十进制分页上限。

    参数为可选 query 字符串，返回部署冻结范围内的整数；空值使用安全默认值，前导零、
    非 ASCII 数字和越界值都拒绝，避免游标意图在不同客户端产生不同规范化结果。
    """

    if value is None:
        return DEFAULT_FILE_LIST_LIMIT
    if (
        not value
        or any(character not in "0123456789" for character in value)
        or (len(value) > 1 and value.startswith("0"))
    ):
        # 只接受无前导零的 ASCII 十进制，避免签名意图出现多个等价表示。
        raise ApiError("infra.validation_failed")
    parsed = int(value, 10)
    if not 1 <= parsed <= MAX_FILE_LIST_LIMIT:
        # 上限超出 1..100 会放大数据库和解密开销，不能自动截断后继续。
        raise ApiError("infra.validation_failed")
    return parsed


def _reject_query(request: Request) -> None:
    """拒绝上传/详情路由的全部 query，tenant 和 actor 只能来自 AuthContext。"""

    if request.query_params.multi_items():
        # 上传和详情没有 query 合同，尤其不能接受客户端 tenant 或 actor 覆盖。
        raise ApiError("infra.validation_failed")


async def _require_empty_body(request: Request) -> None:
    """拒绝 GET 正文，并在首个非空 chunk 到达时立即停止。"""

    try:
        async for chunk in request.stream():
            if type(chunk) is not bytes or chunk:
                # GET 正文既无业务语义又可能夹带敏感输入，首个非空块即失败关闭。
                raise ApiError("infra.validation_failed")
    # 已规范化校验错误保持原码，不让宽泛流异常处理改变客户端诊断。
    except ApiError:
        # 原异常不含正文，直接传播以保留统一 request_id 和错误码处理。
        raise
    # 断流或 ASGI 类型异常不得回显底层对象，也不能被当作空正文继续查询。
    except Exception:
        # 固定校验错误关闭未知流状态，避免异常消息包含客户输入或运行路径。
        raise ApiError("infra.validation_failed") from None


def _file_error_response(
    error: ApiError,
    *,
    request_id: str,
    trace_id: str,
    background_tasks: BackgroundTasks,
) -> JSONResponse:
    """构造与全局处理器一致且携带安全后台派发任务的文件错误响应。"""

    body = build_error_response(error, request_id, trace_id)
    return JSONResponse(
        status_code=error.http_status,
        content=body.model_dump(mode="json"),
        headers={"Cache-Control": "no-store, private"},
        background=background_tasks,
    )


def _no_store(response: Response) -> None:
    """禁止浏览器和共享代理持久缓存文件名与租户安全状态。"""

    response.headers["Cache-Control"] = "no-store, private"


def _request_id(request: Request) -> str:
    """读取统一中间件生成的 request_id，不接受 multipart/query 覆盖。"""

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """读取统一中间件生成的 trace_id，供响应和 source event 共用。"""

    return getattr(request.state, "trace_id", "")
