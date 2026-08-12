"""PAGE-107 只读管理员助手的 Control HTTP 路由与私有缓存响应边界。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from metahub_shared.runtime.fastapi_app import ERROR_RESPONSE_BACKGROUND_TASKS_STATE

from metahub_control.auth import AuthContext, require_admin_assistant_authentication

from .service import execute_admin_assistant_query

router = APIRouter(tags=["admin-assistant"])


@router.post("/admin-assistant/query", response_model=dict[str, object])
async def query_admin_assistant_route(
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    context: Annotated[
        AuthContext,
        Depends(require_admin_assistant_authentication),
    ],
) -> dict[str, object]:
    """在仅认证依赖后执行后端 base/source RBAC，并返回无缓存 exact 回答。"""

    # 成功响应由 FastAPI 自动挂载该容器；服务提交失败终态后再抛 ApiError 时，共享 handler
    # 通过同一个专用 state 槽接回任务，确保通用审计不会在错误路径丢失或同步阻塞。
    setattr(request.state, ERROR_RESPONSE_BACKGROUND_TASKS_STATE, background_tasks)
    result = await execute_admin_assistant_query(request, context, background_tasks)
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["Vary"] = "Authorization"
    return result


__all__ = ["router"]
