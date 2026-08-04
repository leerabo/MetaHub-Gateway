"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/users/routes.py
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from metahub_control.auth import AuthContext, require_current_admin

from .models import CreateUserRequest, UpdateUserRoleRequest, UserListResponse, UserResponse
from .service import create_user, list_users, update_user_role

router = APIRouter(prefix="/users", tags=["users"])


@router.get("", response_model=UserListResponse)
async def get_users(
    request: Request,
    context: Annotated[AuthContext, Depends(require_current_admin)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> UserListResponse:
    """处理 控制面用户管理 的查询型 HTTP 入口，把认证上下文中的 tenant_id 交给服务层。
    
    安全：前端参数不能伪造租户边界，后端仍按会话角色和租户过滤。
    """

    return list_users(
        request.app.state.config,
        context,
        status=status_filter,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def post_user(
    payload: CreateUserRequest,
    request: Request,
    context: Annotated[AuthContext, Depends(require_current_admin)],
) -> UserResponse:
    """处理 控制面用户管理 的写入型 HTTP 入口，统一传递 request_id/trace_id 给服务层。
    
    安全：按钮隐藏只影响体验，实际写权限必须在依赖和服务层再次校验。
    """

    return create_user(
        request.app.state.config,
        context,
        payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.patch("/{user_id}/role", response_model=UserResponse)
async def patch_user_role(
    user_id: str,
    payload: UpdateUserRoleRequest,
    request: Request,
    context: Annotated[AuthContext, Depends(require_current_admin)],
) -> UserResponse:
    """处理 控制面用户管理 的写入型 HTTP 入口，统一传递 request_id/trace_id 给服务层。
    
    安全：按钮隐藏只影响体验，实际写权限必须在依赖和服务层再次校验。
    """

    return update_user_role(
        request.app.state.config,
        context,
        user_id,
        payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


def _request_id(request: Request) -> str:
    """在 控制面用户管理 内部复用 request id 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """在 控制面用户管理 内部复用 trace id 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    return getattr(request.state, "trace_id", "")
