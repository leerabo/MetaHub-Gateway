"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/auth/routes.py
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from .dependencies import require_current_user
from .models import LoginRequest, LoginResponse, LogoutResponse, MeResponse
from .service import AuthContext, current_user_response, login_admin, logout_current_session

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
async def login(payload: LoginRequest, request: Request) -> LoginResponse:
    """处理控制台登录请求。

    路由层只负责提取请求上下文；密码校验、租户解析和会话创建全部交给 service 层。
    """

    client_host = request.client.host if request.client else None
    return login_admin(
        request.app.state.config,
        tenant_slug=payload.tenant_slug,
        email=payload.email,
        password=payload.password,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
        user_agent=request.headers.get("User-Agent"),
        client_host=client_host,
    )


@router.get("/me", response_model=MeResponse)
async def me(
    request: Request,
    context: Annotated[AuthContext, Depends(require_current_user)],
) -> MeResponse:
    """返回当前有效会话的用户、租户和 Session 摘要。"""

    return current_user_response(context, request_id=_request_id(request), trace_id=_trace_id(request))


@router.post("/logout", response_model=LogoutResponse)
async def logout(
    request: Request,
    context: Annotated[AuthContext, Depends(require_current_user)],
) -> LogoutResponse:
    """撤销当前 控制面认证与会话 会话，把 Session 标记为不可继续使用。
    
    边界：仅更新会话状态，不记录明文 Token，也不影响其他有效会话。
    """

    logout_current_session(request.app.state.config, context)
    return LogoutResponse(logged_out=True, request_id=_request_id(request), trace_id=_trace_id(request))


def _request_id(request: Request) -> str:
    """从统一中间件写入的 request.state 中读取 request_id。"""

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """从统一中间件写入的 request.state 中读取 trace_id。"""

    return getattr(request.state, "trace_id", "")
