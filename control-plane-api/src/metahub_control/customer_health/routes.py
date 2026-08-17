"""PRD-086 客户健康纯读路由，只返回后端授权租户的最新可信评分。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from metahub_shared.runtime.errors import ApiError

from metahub_control.auth import AuthContext, require_customer_health_reader

from .models import CustomerHealthResponse
from .service import get_customer_health

router = APIRouter(tags=["customer-health"])


@router.get("/customer-health", response_model=CustomerHealthResponse)
async def get_customer_health_route(
    request: Request,
    context: Annotated[AuthContext, Depends(require_customer_health_reader)],
) -> CustomerHealthResponse:
    """认证后拒绝全部 query，只返回 Session tenant 的最新可信快照。"""

    # 端点不接受任何查询参数，避免调用方影响固定租户与窗口选择。
    if request.query_params.multi_items():
        raise ApiError("customer_health.request_invalid")
    return get_customer_health(
        request.app.state.config,
        context,
        request_id=getattr(request.state, "request_id", ""),
        trace_id=getattr(request.state, "trace_id", ""),
    )


__all__ = ["router"]
