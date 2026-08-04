"""PRD-054 请求解释中心 Control API 路由。

后端 Session/RBAC 依赖在 path、query、header 和 body 业务校验之前执行。已授权的
admin/developer 才能进入严格 wire 解析；viewer 即使手写请求或提交畸形输入仍先得到统一
403，前端隐藏入口只是一层体验优化。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from metahub_control.auth import AuthContext, require_request_explanation_reader

from .models import RequestExplanationResponse
from .service import get_request_explanation
from .wire import validate_request_explanation_get

router = APIRouter(tags=["request-explanations"])


@router.get(
    "/request-explain/{source_request_id}",
    response_model=RequestExplanationResponse,
)
async def get_request_explanation_route(
    source_request_id: str,
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_request_explanation_reader)],
) -> RequestExplanationResponse:
    """解释当前租户的一条 Gateway 请求，并原子保存查看访问事实。"""

    canonical_id = await validate_request_explanation_get(request, source_request_id)
    # 解释包含治理、成本和错误证据，共享浏览器/代理绝不能在登出或换租户后复用。
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["Vary"] = "Authorization"
    return get_request_explanation(
        request.app.state.config,
        context,
        canonical_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


def _request_id(request: Request) -> str:
    """读取共享中间件生成的本次操作 request_id，禁止复用源 Gateway 标识。"""

    return str(getattr(request.state, "request_id", ""))


def _trace_id(request: Request) -> str:
    """读取共享中间件生成的本次操作 trace_id，供统一响应和访问审计关联。"""

    return str(getattr(request.state, "trace_id", ""))


__all__ = ["router"]
