"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/dashboard/routes.py
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from metahub_control.auth import AuthContext, require_dashboard_reader
from metahub_shared.runtime.errors import ApiError

from .catalog import ROLE_DASHBOARD_VIEW_CODES
from .models import DashboardOverviewResponse, RoleDashboardResponse
from .role_service import get_role_dashboard
from .service import get_dashboard_overview

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/overview", response_model=DashboardOverviewResponse)
async def get_dashboard_overview_route(
    request: Request,
    context: Annotated[AuthContext, Depends(require_dashboard_reader)],
) -> DashboardOverviewResponse:
    """读取当前租户的 Dashboard 总览指标。

    后端从 AuthContext 恢复 tenant_id 和角色，前端不能通过任何查询参数指定租户或扩大可见范围。
    """

    return get_dashboard_overview(
        request.app.state.config,
        context,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/role-view", response_model=RoleDashboardResponse)
async def get_role_dashboard_route(
    request: Request,
    context: Annotated[AuthContext, Depends(require_dashboard_reader)],
) -> RoleDashboardResponse:
    """读取当前租户、当前角色的 PAGE-092 首页。

    路由不声明 FastAPI query DTO，而是在认证依赖成功后手工解析原始 multi_items；这样匿名或缺权调用者即使提交畸形 query，
    也只能先得到统一 401/403，不能利用 422 差异探测视图目录。
    """

    return get_role_dashboard(
        request.app.state.config,
        context,
        requested_view=_role_view_query(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


def _role_view_query(request: Request) -> str | None:
    """严格解析 role-view query，只允许零个或一个固定小写 ``view``。

    重复参数、空值、大小写变体、未知 view，以及 tenant_id/role/user_id 等额外字段都统一拒绝，客户端不能选择身份边界。
    """

    pairs = list(request.query_params.multi_items())
    if not pairs:
        return None
    if (
        len(pairs) != 1
        or pairs[0][0] != "view"
        or pairs[0][1] not in ROLE_DASHBOARD_VIEW_CODES
    ):
        # 未知、重复或身份类 query 统一拒绝，不能让客户端借参数选择 tenant、角色或授权视图。
        raise ApiError("dashboard.query_invalid")
    return pairs[0][1]


def _request_id(request: Request) -> str:
    """从请求上下文读取统一 request_id，保证 Dashboard 响应和日志可串联。"""

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """从请求上下文读取统一 trace_id，保证 Dashboard 排障可跨模块串联。"""

    return getattr(request.state, "trace_id", "")
