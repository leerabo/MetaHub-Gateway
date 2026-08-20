"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/tenants/routes.py
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from metahub_control.auth import AuthContext, require_current_admin, require_tenant_settings_reader

from .models import TenantCurrentResponse, TenantUpdateRequest
from .onboarding import TenantOnboardingSummaryResponse, get_onboarding_summary
from .service import get_current_tenant, update_current_tenant

router = APIRouter(prefix="/tenant", tags=["tenant"])


@router.get("/onboarding/summary", response_model=TenantOnboardingSummaryResponse)
async def onboarding_summary(
    request: Request,
    context: Annotated[AuthContext, Depends(require_current_admin)],
) -> TenantOnboardingSummaryResponse:
    """Return backend-authoritative first-use progress for the current tenant."""

    return get_onboarding_summary(
        request.app.state.config,
        context,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/current", response_model=TenantCurrentResponse)
async def current_tenant(
    request: Request,
    context: Annotated[AuthContext, Depends(require_tenant_settings_reader)],
) -> TenantCurrentResponse:
    """读取当前登录上下文的租户信息。"""

    # 租户判断决定数据隔离边界，不能跨租户复用或返回对象。
    return get_current_tenant(
        request.app.state.config,
        context,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.patch("/current", response_model=TenantCurrentResponse)
async def patch_current_tenant(
    payload: TenantUpdateRequest,
    request: Request,
    context: Annotated[AuthContext, Depends(require_tenant_settings_reader)],
) -> TenantCurrentResponse:
    """处理 控制面租户资料 的写入型 HTTP 入口，统一传递 request_id/trace_id 给服务层。
    
    安全：路由依赖按数据库权限快照校验 ``tenant.settings.read``，服务层只使用可信上下文的
    ``tenant_id`` 更新当前租户；前端隐藏按钮不构成授权边界。
    """

    # 租户判断决定数据隔离边界，不能跨租户复用或返回对象。
    return update_current_tenant(
        request.app.state.config,
        context,
        payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


def _request_id(request: Request) -> str:
    """在 控制面租户资料 内部复用 request id 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """在 控制面租户资料 内部复用 trace id 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    return getattr(request.state, "trace_id", "")
