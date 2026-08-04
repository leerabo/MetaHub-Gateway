"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/debug_console/routes.py
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from metahub_control.auth import AuthContext, require_debug_console_user

from .models import DebugChatCompletionRequest, DebugResultResponse
from .service import run_debug_chat_completion

router = APIRouter(prefix="/debug", tags=["debug-console"])


@router.post("/chat-completions", response_model=DebugResultResponse)
async def post_debug_chat_completions(
    payload: DebugChatCompletionRequest,
    request: Request,
    context: Annotated[AuthContext, Depends(require_debug_console_user)],
) -> DebugResultResponse:
    """执行一次控制台内置 Chat Completions 调试请求。

    路由层只恢复登录态和 request_id/trace_id；租户、权限、Virtual Key 状态、模型路由和敏感字段过滤均交由
    service 层统一处理，避免前端隐藏按钮被误当成真实权限边界。
    """

    return await run_debug_chat_completion(
        request.app.state.config,
        context,
        payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


def _request_id(request: Request) -> str:
    """从统一中间件写入的 request.state 中读取 request_id，供调试事件和调用日志串联。"""

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """从统一中间件写入的 request.state 中读取 trace_id，供跨服务排查链路串联。"""

    return getattr(request.state, "trace_id", "")

