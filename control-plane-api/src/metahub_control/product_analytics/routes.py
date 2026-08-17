"""产品激活漏斗路由只接受认证租户上下文，并拒绝客户端指定查询范围。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from metahub_shared.runtime.errors import ApiError

from metahub_control.auth import AuthContext, require_product_analytics_reader

from .models import ProductAnalyticsFunnelResponse
from .service import get_product_analytics_funnel

router = APIRouter(prefix="/product-analytics", tags=["product-analytics"])


@router.get("/funnel", response_model=ProductAnalyticsFunnelResponse)
async def get_product_analytics_funnel_route(
    request: Request,
    context: Annotated[AuthContext, Depends(require_product_analytics_reader)],
) -> ProductAnalyticsFunnelResponse:
    """认证与实时授权后拒绝全部 query，再读取当前 tenant 聚合。"""

    # 租户身份和统计窗口只能来自服务端认证上下文，任何 query 都可能形成越权旁路。
    if request.query_params.multi_items():
        raise ApiError("product_analytics.request_invalid")
    return get_product_analytics_funnel(
        request.app.state.config,
        context,
        request_id=getattr(request.state, "request_id", ""),
        trace_id=getattr(request.state, "trace_id", ""),
    )


__all__ = ["router"]
