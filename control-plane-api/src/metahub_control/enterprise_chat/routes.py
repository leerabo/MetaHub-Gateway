"""PRD-066 企业 Chat 控制面 HTTP 路由。

所有公开接口先执行数据库实时 ``chat.use`` 权限依赖，再解析资源和正文。前端导航隐藏只改善
体验，不能替代这里的后端 RBAC、tenant、用户和登录 Session owner 校验。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from metahub_shared.runtime.errors import ApiError, build_error_response
from pydantic import BaseModel, ValidationError

from metahub_control.auth import AuthContext, require_enterprise_chat_user

from .models import (
    ChatMessageResponse,
    ChatOptionsResponse,
    ChatSessionResponse,
    ChatTemplateMessageResponse,
    CreateChatSessionRequest,
    SendChatMessageRequest,
)
from .service import (
    ChatDispatchError,
    clear_chat_session,
    create_chat_session,
    dispatch_chat_event_safely,
    list_chat_options,
    send_chat_message,
)

# 请求模型使用泛型返回值，调用端可继续获得具体 DTO 类型而不需要不安全的强制转换。
# 512 KiB 覆盖 64 KiB 合法正文在最坏 ``\\uXXXX`` 转义下的 wire 体积，同时阻断无界请求缓存。
MAX_CHAT_WIRE_BODY_BYTES = 512 * 1024


class _DuplicateJsonKey(ValueError):
    """表示企业 Chat JSON 在任意对象层出现重复键，公开响应不得携带具体键名。"""


class EnterpriseChatRoute(APIRoute):
    """把本域的框架请求校验错误收敛到 PRD-066 固定错误码。

    全局校验处理器服务所有既有 API，不能为单个 M2 页面修改其稳定合同。本路由包装器只
    捕获 ``/chat/*`` 的 body/path 解析错误；认证、权限和业务 ``ApiError`` 保持原优先级。
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """返回带 Chat 校验错误转换的原 FastAPI 路由处理器。"""

        original_handler = super().get_route_handler()

        async def chat_route_handler(request: Request) -> Response:
            """执行原处理器，并禁止框架级字段详情穿透企业 Chat 边界。"""

            try:
                return await original_handler(request)
            # 框架校验详情可能夹带正文或注入字段，必须在统一异常响应前固定收敛。
            except RequestValidationError:
                # 详细 loc/input 可能包含正文片段或客户端注入字段；公开响应只给固定错误码。
                raise ApiError("chat.request_invalid") from None

        return chat_route_handler


router = APIRouter(
    prefix="/chat",
    tags=["enterprise-chat"],
    route_class=EnterpriseChatRoute,
)


@router.get("/options", response_model=ChatOptionsResponse)
async def get_chat_options(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_enterprise_chat_user)],
) -> ChatOptionsResponse:
    """返回当前租户的企业 Chat 安全选项目录。"""

    _reject_query(request)
    response.headers["Cache-Control"] = "no-store"
    return list_chat_options(
        request.app.state.config,
        context,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post(
    "/sessions",
    response_model=ChatSessionResponse,
    status_code=status.HTTP_201_CREATED,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": CreateChatSessionRequest.model_json_schema()
                }
            },
        }
    },
)
async def post_chat_session(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_enterprise_chat_user)],
) -> ChatSessionResponse:
    """为当前 tenant、用户和登录 Session 创建短生命周期 ChatSession。"""

    _reject_query(request)
    payload = await _parse_json_body(request, CreateChatSessionRequest)
    response.headers["Cache-Control"] = "no-store"
    # 只有实时权限和严格正文均通过后才解析租户资源，防止未授权请求探测业务对象。
    return create_chat_session(
        request.app.state.config,
        context,
        payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post(
    "/sessions/{chat_session_id}/messages",
    # 模板响应必须保留 rendered_user；普通响应仍使用原 exact schema，不新增 null 字段。
    response_model=ChatTemplateMessageResponse | ChatMessageResponse,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": SendChatMessageRequest.model_json_schema()
                }
            },
        }
    },
)
async def post_chat_message(
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    chat_session_id: str,
    context: Annotated[AuthContext, Depends(require_enterprise_chat_user)],
) -> ChatTemplateMessageResponse | ChatMessageResponse | Response:
    """发送一段有界纯文本 history，并返回本次 assistant 文本和治理摘要。"""

    _reject_query(request)
    payload = await _parse_json_body(request, SendChatMessageRequest)
    response.headers["Cache-Control"] = "no-store"
    request_id = _request_id(request)
    trace_id = _trace_id(request)
    try:
        result, event = await send_chat_message(
            request.app.state.config,
            context,
            chat_session_id,
            payload,
            request_id=request_id,
            trace_id=trace_id,
        )
    # Gateway 业务失败已经带有持久终态，路由只能派发安全事件并传播冻结错误码。
    except ChatDispatchError as exc:
        # 抛 ApiError 会让全局处理器新建一个不带本路由 BackgroundTasks 的响应，因此这里
        # 直接返回同一统一错误模型，并把安全派发器附到该错误响应。Starlette 会在线程池中
        # 执行同步任务，Webhook Endpoint 延迟不会阻塞 ASGI 事件循环或覆盖已提交失败终态。
        background_tasks.add_task(
            dispatch_chat_event_safely,
            request.app.state.config,
            exc.event,
            request_id=request_id,
            trace_id=trace_id,
        )
        # 禁止把内部异常对象或事件信封序列化，客户端只能接收统一目录中的固定错误字段。
        return _chat_error_response(
            exc.error,
            request_id=request_id,
            trace_id=trace_id,
            background_tasks=background_tasks,
        )
    background_tasks.add_task(
        dispatch_chat_event_safely,
        request.app.state.config,
        event,
        request_id=request_id,
        trace_id=trace_id,
    )
    return result


def _chat_error_response(
    error: ApiError,
    *,
    request_id: str,
    trace_id: str,
    background_tasks: BackgroundTasks,
) -> JSONResponse:
    """构造带失败事件后台任务的统一 Chat 错误响应。

    参数：目录化错误、当前链路标识和只含安全派发器的任务集合。返回：与全局 ApiError
    handler 相同的错误体和 HTTP 状态。关键约束：响应只序列化 ``build_error_response`` 的
    白名单字段；Webhook 事件、Endpoint、异常正文和模型内容均不得进入浏览器响应。
    """

    body = build_error_response(error, request_id, trace_id)
    return JSONResponse(
        status_code=error.http_status,
        content=body.model_dump(mode="json"),
        headers={"Cache-Control": "no-store"},
        background=background_tasks,
    )


@router.delete("/sessions/{chat_session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_chat_session(
    request: Request,
    chat_session_id: str,
    context: Annotated[AuthContext, Depends(require_enterprise_chat_user)],
) -> Response:
    """清空并终止当前 owner 的企业 Chat 会话。"""

    _reject_query(request)
    # DELETE 合同没有正文；逐块失败关闭可避免先缓存超大无效 body，再判断其非空。
    await _read_bounded_body(request, max_bytes=0, allow_empty=True)
    clear_chat_session(request.app.state.config, context, chat_session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers={"Cache-Control": "no-store"})


def _reject_query(request: Request) -> None:
    """拒绝全部 query 参数，尤其是 tenant、Key、Provider 和模型文本注入。"""

    if request.query_params.multi_items():
        # Chat 身份和资源只来自登录上下文与严格正文，query 注入一律使用固定错误拒绝。
        raise ApiError("chat.request_invalid")


async def _parse_json_body[RequestPayloadT: BaseModel](
    request: Request,
    model_type: type[RequestPayloadT],
) -> RequestPayloadT:
    """在认证和权限依赖完成后严格解析一份企业 Chat JSON 正文。

    参数：当前请求和允许的 Pydantic 请求模型。返回：通过 exact-set 与严格类型校验的 DTO。
    关键约束：不能把模型重新声明为 FastAPI body 参数，否则框架会在认证前解码畸形 JSON，
    让匿名或已撤权调用者通过 422 响应探测接口合同；校验详情也不得携带正文返回客户端。
    """

    media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media_type != "application/json":
        # 只接受明确 JSON，避免表单、文本或浏览器隐式编码绕过严格字段集合。
        raise ApiError("chat.request_invalid")
    try:
        body = await _read_bounded_body(
            request,
            max_bytes=MAX_CHAT_WIRE_BODY_BYTES,
            allow_empty=False,
        )
        decoded = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_object_without_duplicates,
        )
        return model_type.model_validate(decoded)
    # JSON 语法与模型字段错误统一脱敏，不能让解析器位置或字段值进入响应和日志。
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        ValidationError,
        _DuplicateJsonKey,
        RecursionError,
        TypeError,
    ):
        # 解析位置、非法值和正文片段都属于敏感输入，对外只能返回固定错误码。
        raise ApiError("chat.request_invalid") from None


async def _read_bounded_body(
    request: Request,
    *,
    max_bytes: int,
    allow_empty: bool,
) -> bytes:
    """按声明和实际流量有界读取正文，异常统一折叠为 Chat 请求错误。

    参数：当前请求、允许的最大 wire 字节数及空正文策略。返回：严格受限的原始 bytes。
    关键约束：认证/RBAC 已在调用前完成；不能只信任 Content-Length，也不能把断连异常链接到响应。
    """

    declared = _content_length(request)
    if declared is not None and (
        declared > max_bytes or (declared == 0 and not allow_empty)
    ):
        # 声明已越界时在分配正文缓冲区前拒绝，避免无效 Chat 请求占用大块内存。
        raise ApiError("chat.request_invalid")
    body = bytearray()
    try:
        async for chunk in request.stream():
            # 实际流独立累计，省略或伪造 Content-Length 也不能绕过 wire 上限。
            if len(body) + len(chunk) > max_bytes:
                raise ApiError("chat.request_invalid")
            body.extend(chunk)
    # 主动触发的统一 Chat 错误已经完成脱敏，必须保留其固定码而不能再包装或链接正文异常。
    except ApiError:
        raise
    # 断连或 ASGI receive 异常可能含网络细节，统一切断异常链且不记录已收正文。
    except Exception:
        raise ApiError("chat.request_invalid") from None
    if (not body and not allow_empty) or (
        declared is not None and declared != len(body)
    ):
        # 空正文、截断和拼接流都没有唯一请求语义，必须固定失败关闭。
        raise ApiError("chat.request_invalid")
    return bytes(body)


def _content_length(request: Request) -> int | None:
    """读取唯一规范 Content-Length；重复、空白、符号或 Unicode 数字一律拒绝。"""

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
        # 代理对重复或非规范长度可能采用不同解释，Chat 入口不能替调用方猜测。
        raise ApiError("chat.request_invalid")
    return int(value)


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """构造 JSON object，并在任意嵌套层发现重复键时以固定内部异常中止。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            # 不把冲突键写入异常文本，避免敏感或攻击者构造字段进入 traceback。
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _request_id(request: Request) -> str:
    """读取统一中间件生成或安全透传的 request_id。"""

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """读取统一中间件生成或安全透传的 trace_id。"""

    return getattr(request.state, "trace_id", "")
