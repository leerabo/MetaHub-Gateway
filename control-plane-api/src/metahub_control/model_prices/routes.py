"""模块职责：提供模型有效期价格计划的查询和创建 HTTP 接口。

维护边界：路由只收口参数并透传认证上下文，租户、模型与金额校验必须在服务层完成。
来源文件：control-plane-api/src/metahub_control/model_prices/routes.py
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from metahub_control.auth import AuthContext, require_model_price_reader, require_model_price_writer

from .models import (
    BatchCreateModelPricePlansRequest,
    BatchCreateModelPricePlansResponse,
    CreateModelPricePlanRequest,
    ModelPricePlanListResponse,
    ModelPricePlanResponse,
)
from .service import (
    create_model_price_plan,
    create_model_price_plans_batch,
    list_model_price_plans,
)

router = APIRouter(prefix="/model-prices", tags=["model-prices"])


@router.get("", response_model=ModelPricePlanListResponse)
async def get_model_prices(
    request: Request,
    context: Annotated[AuthContext, Depends(require_model_price_reader)],
    model_id: str | None = None,
    effective_status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ModelPricePlanListResponse:
    """查询当前租户价格计划，viewer 由后端权限依赖直接拒绝。"""

    return list_model_price_plans(
        request.app.state.config,
        context,
        model_id=model_id,
        effective_status=effective_status,
        limit=limit,
        offset=offset,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post("", response_model=ModelPricePlanResponse, status_code=status.HTTP_201_CREATED)
async def post_model_price(
    payload: CreateModelPricePlanRequest,
    request: Request,
    context: Annotated[AuthContext, Depends(require_model_price_writer)],
) -> ModelPricePlanResponse:
    """创建不可变价格计划，developer 和 viewer 均不能绕过后端写权限。"""

    return create_model_price_plan(
        request.app.state.config,
        context,
        payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post(
    "/batch",
    response_model=BatchCreateModelPricePlansResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_model_prices_batch(
    payload: BatchCreateModelPricePlansRequest,
    request: Request,
    context: Annotated[AuthContext, Depends(require_model_price_writer)],
) -> BatchCreateModelPricePlansResponse:
    """原子补录多个历史、当前或未来价格时间段。"""

    return create_model_price_plans_batch(
        request.app.state.config,
        context,
        payload.plans,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


def _request_id(request: Request) -> str:
    """读取统一请求标识，供价格响应、事件和结构化日志关联。"""

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """读取统一追踪标识，供价格响应、事件和 Webhook 链路关联。"""

    return getattr(request.state, "trace_id", "")
