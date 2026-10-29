"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/user_preferences/routes.py
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from metahub_control.auth import AuthContext, require_current_user

from .models import UpdateUserPreferenceRequest, UserPreferenceResponse
from .service import get_user_preferences, update_user_preferences

router = APIRouter(prefix="/user/preferences", tags=["user-preferences"])


@router.get("", response_model=UserPreferenceResponse)
async def get_user_preferences_route(
    request: Request,
    context: Annotated[AuthContext, Depends(require_current_user)],
) -> UserPreferenceResponse:
    """读取当前登录用户的语言偏好。"""

    return get_user_preferences(
        request.app.state.config,
        context,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.patch("", response_model=UserPreferenceResponse)
async def update_user_preferences_route(
    payload: UpdateUserPreferenceRequest,
    request: Request,
    context: Annotated[AuthContext, Depends(require_current_user)],
) -> UserPreferenceResponse:
    """更新当前登录用户的语言偏好。

    请求体不允许传 tenant_id/user_id；服务层始终使用认证上下文写入当前用户偏好。
    """

    return update_user_preferences(
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
