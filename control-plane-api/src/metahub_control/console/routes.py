"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息
脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/console/routes.py
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from metahub_control.auth import AuthContext, require_console_user, require_current_user

from .models import (
    ConsoleEventRequest,
    ConsoleEventResponse,
    ConsoleNavigationResponse,
    UpdateUserPreferenceRequest,
    UserPreferenceResponse,
)
from .service import (
    get_console_navigation,
    get_user_preferences,
    record_console_event,
    update_user_preferences,
)

router = APIRouter(prefix="/console", tags=["console"])


@router.get("/navigation", response_model=ConsoleNavigationResponse)
async def get_console_navigation_route(
    request: Request,
    context: Annotated[AuthContext, Depends(require_console_user)],
    active_item_id: Annotated[str | None, Query()] = None,
) -> ConsoleNavigationResponse:
    """读取当前登录用户可见的控制台导航项。

    后端按 AuthContext 计算角色可见性，前端传入的 active_item_id 只作为候选当前页，不参与权限判定。
    """

    return get_console_navigation(
        request.app.state.config,
        context,
        requested_active_item_id=active_item_id,
        workspace_scoped=getattr(request.state, "workspace_session_authenticated", False) is True,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/preferences", response_model=UserPreferenceResponse)
async def get_user_preferences_route(
    request: Request,
    context: Annotated[AuthContext, Depends(require_current_user)],
) -> UserPreferenceResponse:
    """读取当前登录用户的控制台 UI 偏好。"""

    return get_user_preferences(
        request.app.state.config,
        context,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.patch("/preferences", response_model=UserPreferenceResponse)
async def update_user_preferences_route(
    payload: UpdateUserPreferenceRequest,
    request: Request,
    context: Annotated[AuthContext, Depends(require_current_user)],
) -> UserPreferenceResponse:
    """更新当前登录用户的控制台 UI 偏好。

    请求体不允许传 tenant_id/user_id；服务层始终用登录态中的租户和用户写入。
    """

    return update_user_preferences(
        request.app.state.config,
        context,
        payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post("/events", response_model=ConsoleEventResponse)
async def record_console_event_route(
    payload: ConsoleEventRequest,
    request: Request,
    context: Annotated[AuthContext, Depends(require_current_user)],
) -> ConsoleEventResponse:
    """记录控制台页面访问事件，用于后续审计和开发者体验分析。"""

    return record_console_event(
        request.app.state.config,
        context,
        payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


def _request_id(request: Request) -> str:
    """从统一中间件写入的 request.state 中读取 request_id。"""

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """从统一中间件写入的 request.state 中读取 trace_id。"""

    return getattr(request.state, "trace_id", "")
