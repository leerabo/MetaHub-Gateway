"""Gateway Virtual Key 的逐请求反馈写路由。

该模块只注册 ``POST /feedback``。汇总查询属于控制面，不能在数据面增加聚合扫描而
与模型热路径争用 SQLite/CPU。鉴权依赖必须先于 raw header 和正文解析执行。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response
from metahub_shared.feedback import (
    MAX_FEEDBACK_BODY_BYTES,
    parse_feedback_json_bytes,
    resolve_feedback_operation_request_id,
)
from metahub_shared.runtime.errors import ApiError

from metahub_gateway.auth.virtual_key import GatewayAuthContext, authenticate_virtual_key

from .models import FeedbackMutationResponse
from .service import create_virtual_key_feedback

router = APIRouter(prefix="/feedback", tags=["feedback"])


def require_feedback_virtual_key(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> GatewayAuthContext:
    """恢复 active Virtual Key 的可信 tenant/key 上下文。

    参数 ``request`` 提供 Gateway 配置，``authorization`` 是 OpenAI-compatible Bearer
    Token；返回只含脱敏 Key 元数据的上下文。认证失败沿用既有 gateway auth 错误，
    且发生在读取反馈正文之前，明文 Token 只在认证函数内存中参与哈希匹配。
    """

    # 统一认证函数同时检查 tenant/Key active 状态并只返回可信归属，禁止从正文恢复身份。
    return authenticate_virtual_key(request.app.state.config, authorization)


@router.post("", response_model=FeedbackMutationResponse, status_code=201)
async def post_feedback(
    request: Request,
    response: Response,
    context: Annotated[GatewayAuthContext, Depends(require_feedback_virtual_key)],
) -> FeedbackMutationResponse:
    """使用产生源调用的 Virtual Key 提交一条不可变反馈。

    shared 领域事务会同时绑定 tenant 与 ``virtual_key_id``；跨 Key、跨租户和未知
    request_id 统一返回 404 防枚举。首次创建为 201，同操作同意图恢复为 200。
    """

    operation_request_id = resolve_feedback_operation_request_id(
        request.scope.get("headers", ()),
        _request_id(request),
    )
    body = await _read_feedback_body(request)
    intent = parse_feedback_json_bytes(
        body,
        content_type=request.headers.get("content-type"),
        content_length=request.headers.get("content-length"),
    )
    result = create_virtual_key_feedback(
        request.app.state.config,
        request.app.state,
        context,
        operation_request_id=operation_request_id,
        trace_id=_trace_id(request),
        intent=intent,
    )
    response.status_code = 200 if result.idempotent_replay else 201
    response.headers["Cache-Control"] = "no-store, private"
    return result


async def _read_feedback_body(request: Request) -> bytes:
    """在内存中有界收集正文，实际流超过 4096 字节即统一拒绝且不记录内容。"""

    body = bytearray()
    async for chunk in request.stream():
        # Content-Length 可能缺失或伪造，因此必须对实际收到的每个 chunk 累计校验。
        if len(body) + len(chunk) > MAX_FEEDBACK_BODY_BYTES:
            raise ApiError("feedback.input_invalid")
        body.extend(chunk)
    return bytes(body)


def _request_id(request: Request) -> str:
    """读取统一中间件为当前 Gateway 操作确定的 request_id。"""

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """读取当前 HTTP 操作 trace_id，幂等重放不得改写持久事件首次 trace。"""

    return getattr(request.state, "trace_id", "")


__all__ = ["require_feedback_virtual_key", "router"]
