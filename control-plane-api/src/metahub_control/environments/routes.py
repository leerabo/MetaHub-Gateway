"""模块职责：提供租户级 Environment 管理 HTTP 接口。

维护边界：路由只负责认证、参数收口和链路 ID 透传；租户隔离与状态校验必须由服务层执行。
来源文件：control-plane-api/src/metahub_control/environments/routes.py
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from metahub_control.auth import AuthContext, require_workspace_or_legacy_permissions

from .models import (
    CreateEnvironmentRequest,
    EnvironmentListResponse,
    EnvironmentResponse,
    UpdateEnvironmentRequest,
)
from .service import create_environment, list_environments, update_environment

router = APIRouter(tags=["environments"])


@router.get("/environments", response_model=EnvironmentListResponse)
async def get_environments(
    request: Request,
    context: Annotated[
        AuthContext,
        Depends(require_workspace_or_legacy_permissions("environment.read", touch=False)),
    ],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    code: str | None = None,
) -> EnvironmentListResponse:
    """读取当前租户 Environment 列表，viewer 由后端权限依赖拒绝。"""

    return list_environments(
        request.app.state.config,
        context,
        status=status_filter,
        code=code,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post(
    "/environments",
    response_model=EnvironmentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_environment(
    payload: CreateEnvironmentRequest,
    request: Request,
    context: Annotated[
        AuthContext,
        Depends(require_workspace_or_legacy_permissions("environment.write")),
    ],
) -> EnvironmentResponse:
    """创建当前租户标准环境，并记录 environment.created。"""

    return create_environment(
        request.app.state.config,
        context,
        payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.patch("/environments/{environment_id}", response_model=EnvironmentResponse)
async def patch_environment(
    environment_id: str,
    payload: UpdateEnvironmentRequest,
    request: Request,
    context: Annotated[
        AuthContext,
        Depends(require_workspace_or_legacy_permissions("environment.write")),
    ],
) -> EnvironmentResponse:
    """更新 Environment 名称、说明或状态，代码不可修改。"""

    return update_environment(
        request.app.state.config,
        context,
        environment_id,
        payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


def _request_id(request: Request) -> str:
    """从中间件状态读取统一请求标识，供环境响应和审计事件关联。"""

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """从中间件状态读取统一追踪标识，供环境响应和审计事件关联。"""

    return getattr(request.state, "trace_id", "")
