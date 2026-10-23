"""模块职责：提供 Project/Application 管理 HTTP 入口。

维护边界：路由层只负责认证依赖、request_id/trace_id 透传和参数收口，租户隔离与业务校验
必须交给服务层重复执行。
来源文件：control-plane-api/src/metahub_control/projects/routes.py
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status

from metahub_control.audit import build_audit_request_meta
from metahub_control.auth import AuthContext, require_workspace_or_legacy_permissions

from .models import (
    ApplicationListResponse,
    ApplicationMembersResponse,
    ApplicationResponse,
    CreateApplicationRequest,
    CreateProjectRequest,
    ProjectListResponse,
    ProjectOwnerCandidatesResponse,
    ProjectResponse,
    UpdateApplicationRequest,
    UpdateApplicationMembersRequest,
    UpdateProjectRequest,
)
from .service import (
    create_application,
    create_project,
    delete_application,
    delete_project,
    list_applications,
    list_projects,
    list_project_owner_candidates,
    update_application,
    list_application_members,
    update_application_members,
    update_project,
)

router = APIRouter(tags=["projects"])


@router.get("/projects/owner-candidates", response_model=ProjectOwnerCandidatesResponse)
async def get_project_owner_candidates(
    request: Request,
    context: Annotated[
        AuthContext, Depends(require_workspace_or_legacy_permissions("project.write", touch=False)),
    ],
    search: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> ProjectOwnerCandidatesResponse:
    """项目编辑器的组织内负责人选择器。"""

    return list_project_owner_candidates(
        request.app.state.config, context, search=search, limit=limit,
        request_id=_request_id(request), trace_id=_trace_id(request),
    )


@router.get("/projects", response_model=ProjectListResponse)
async def get_projects(
    request: Request,
    context: Annotated[
        AuthContext,
        Depends(require_workspace_or_legacy_permissions("project.read", touch=False)),
    ],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    search: str | None = None,
) -> ProjectListResponse:
    """读取当前租户项目列表，后端依赖负责拦截未授权角色。"""

    return list_projects(
        request.app.state.config,
        context,
        status=status_filter,
        search=search,
        personal_only="project.write" not in context.permission_codes,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post("/projects", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def post_project(
    payload: CreateProjectRequest,
    request: Request,
    context: Annotated[
        AuthContext,
        Depends(require_workspace_or_legacy_permissions("project.write")),
    ],
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
    context: Annotated[
        AuthContext,
        Depends(require_workspace_or_legacy_permissions("project.write")),
    ],
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


@router.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_project(
    project_id: str,
    request: Request,
    context: Annotated[AuthContext, Depends(require_workspace_or_legacy_permissions("project.write"))],
) -> Response:
    delete_project(
        request.app.state.config,
        context,
        project_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/applications", response_model=ApplicationListResponse)
async def get_applications(
    request: Request,
    context: Annotated[
        AuthContext,
        Depends(require_workspace_or_legacy_permissions("project.read")),
    ],
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
        personal_only="project.write" not in context.permission_codes,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post(
    "/applications",
    response_model=ApplicationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_application(
    payload: CreateApplicationRequest,
    request: Request,
    context: Annotated[
        AuthContext,
        Depends(require_workspace_or_legacy_permissions("project.write")),
    ],
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
    context: Annotated[
        AuthContext,
        Depends(require_workspace_or_legacy_permissions("project.write")),
    ],
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


@router.delete("/applications/{application_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_application(
    application_id: str,
    request: Request,
    context: Annotated[AuthContext, Depends(require_workspace_or_legacy_permissions("project.write"))],
) -> Response:
    delete_application(
        request.app.state.config,
        context,
        application_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/applications/{application_id}/members", response_model=ApplicationMembersResponse)
async def get_application_members(
    application_id: str,
    request: Request,
    context: Annotated[
        AuthContext,
        Depends(require_workspace_or_legacy_permissions("project.read")),
    ],
    search: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
    selected_only: bool = False,
) -> ApplicationMembersResponse:
    """读取应用成员选择器的分页候选。"""

    return list_application_members(
        request.app.state.config,
        context,
        application_id,
        search=search,
        limit=limit,
        offset=offset,
        selected_only=selected_only,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.put("/applications/{application_id}/members", response_model=ApplicationMembersResponse)
async def put_application_members(
    application_id: str,
    payload: UpdateApplicationMembersRequest,
    request: Request,
    context: Annotated[
        AuthContext,
        Depends(require_workspace_or_legacy_permissions("project.write")),
    ],
) -> ApplicationMembersResponse:
    """保存应用的完整成员选择集合。"""

    return update_application_members(
        request.app.state.config,
        context,
        application_id,
        payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


def _request_id(request: Request) -> str:
    """从请求状态读取统一 request_id，缺失时返回空字符串兼容测试应用。"""

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """从请求状态读取统一 trace_id，缺失时返回空字符串兼容测试应用。"""

    return getattr(request.state, "trace_id", "")
