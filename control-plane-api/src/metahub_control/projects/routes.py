"""模块职责：提供 Project/Application 管理 HTTP 入口。

维护边界：路由层只负责认证依赖、request_id/trace_id 透传和参数收口，租户隔离与业务校验必须交给服务层重复执行。
来源文件：control-plane-api/src/metahub_control/projects/routes.py
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from metahub_control.audit import build_audit_request_meta
from metahub_control.auth import AuthContext, require_project_admin, require_project_reader

from .models import (
    ApplicationListResponse,
    ApplicationResponse,
    CreateApplicationRequest,
    CreateProjectRequest,
    ProjectListResponse,
    ProjectResponse,
    UpdateApplicationRequest,
    UpdateProjectRequest,
)
from .service import (
    create_application,
    create_project,
    list_applications,
    list_projects,
    update_application,
    update_project,
)

router = APIRouter(tags=["projects"])


@router.get("/projects", response_model=ProjectListResponse)
async def get_projects(
    request: Request,
    context: Annotated[AuthContext, Depends(require_project_reader)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    search: str | None = None,
) -> ProjectListResponse:
    """读取当前租户项目列表，后端依赖负责拦截未授权角色。"""

    return list_projects(
        request.app.state.config,
        context,
        status=status_filter,
        search=search,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post("/projects", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def post_project(
    payload: CreateProjectRequest,
    request: Request,
    context: Annotated[AuthContext, Depends(require_project_admin)],
) -> ProjectResponse:
    """创建当前租户内的项目，并透传 request_id 写入 project.created 审计事件。"""

    return create_project(
        request.app.state.config,
        context,
        payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.patch("/projects/{project_id}", response_model=ProjectResponse)
async def patch_project(
    project_id: str,
    payload: UpdateProjectRequest,
    request: Request,
    context: Annotated[AuthContext, Depends(require_project_admin)],
) -> ProjectResponse:
    """更新项目治理元数据，不允许修改 project_code。"""

    return update_project(
        request.app.state.config,
        context,
        project_id,
        payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/applications", response_model=ApplicationListResponse)
async def get_applications(
    request: Request,
    context: Annotated[AuthContext, Depends(require_project_reader)],
    project_id: str | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    search: str | None = None,
) -> ApplicationListResponse:
    """读取当前租户应用列表，可按项目筛选。"""

    return list_applications(
        request.app.state.config,
        context,
        project_id=project_id,
        status=status_filter,
        search=search,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post("/applications", response_model=ApplicationResponse, status_code=status.HTTP_201_CREATED)
async def post_application(
    payload: CreateApplicationRequest,
    request: Request,
    context: Annotated[AuthContext, Depends(require_project_admin)],
) -> ApplicationResponse:
    """在当前租户的项目下创建应用。"""

    return create_application(
        request.app.state.config,
        context,
        payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.patch("/applications/{application_id}", response_model=ApplicationResponse)
async def patch_application(
    application_id: str,
    payload: UpdateApplicationRequest,
    request: Request,
    context: Annotated[AuthContext, Depends(require_project_admin)],
) -> ApplicationResponse:
    """更新应用治理元数据，迁移项目时同步 Key 归属。"""

    return update_application(
        request.app.state.config,
        context,
        application_id,
        payload,
        audit_request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


def _request_id(request: Request) -> str:
    """从请求状态读取统一 request_id，缺失时返回空字符串兼容测试应用。"""

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """从请求状态读取统一 trace_id，缺失时返回空字符串兼容测试应用。"""

    return getattr(request.state, "trace_id", "")
