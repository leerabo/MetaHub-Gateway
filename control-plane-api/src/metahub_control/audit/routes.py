"""模块职责：提供管理操作审计的只读 HTTP 路由和统一分页参数入口。

维护边界：租户只能来自认证上下文，后端 RBAC 必须限制为 admin，响应必须保留 request_id/trace_id。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response

from metahub_control.auth import AuthContext, require_audit_exporter, require_audit_log_reader

from .export_scheduler import wake_audit_export_scheduler
from .export_service import export_audit_logs
from .models import AuditLogListResponse
from .service import build_audit_request_meta, list_audit_logs

router = APIRouter(prefix="/audit-logs", tags=["audit-logs"])

# 导出只接受列表已有的七个精确筛选；tenant、分页、列选择和文件路径都不能由客户端指定。
AUDIT_EXPORT_QUERY_FIELDS = frozenset(
    {
        "actor_user_id",
        "action",
        "object_type",
        "object_id",
        "request_id",
        "created_from",
        "created_to",
    }
)


@router.get("/export")
async def get_audit_log_export(
    request: Request,
    context: Annotated[AuthContext, Depends(require_audit_exporter)],
    actor_user_id: str | None = None,
    action: str | None = None,
    object_type: str | None = None,
    object_id: str | None = None,
    request_id_filter: Annotated[str | None, Query(alias="request_id")] = None,
    created_from: str | None = None,
    created_to: str | None = None,
) -> Response:
    """同步导出当前租户审计 CSV，并返回不可变复核响应头。

    权限依赖在业务 query 处理前执行；developer/viewer 即使提交畸形筛选也只能得到统一 403。
    """

    keys = list(request.query_params.keys())
    if any(key not in AUDIT_EXPORT_QUERY_FIELDS for key in keys) or any(
        len(request.query_params.getlist(key)) != 1 for key in keys
    ):
        # FastAPI 默认忽略未知或重复 query，强审计导出必须显式失败关闭越权和歧义输入。
        from metahub_shared.runtime.errors import ApiError

        raise ApiError("audit_export.filter_invalid")
    result = export_audit_logs(
        request.app.state.config,
        context,
        actor_user_id=actor_user_id,
        action=action,
        object_type=object_type,
        object_id=object_id,
        request_id_filter=request_id_filter,
        created_from=created_from,
        created_to=created_to,
        request_id=getattr(request.state, "request_id", ""),
        trace_id=getattr(request.state, "trace_id", ""),
        audit_request_meta=build_audit_request_meta(request),
    )
    # 唤醒只提示后台 worker 立即扫描；路由本身从不取得 lease 或调用恢复模式。
    wake_audit_export_scheduler(request.app)
    return Response(
        content=result.content,
        status_code=200,
        headers={
            "Content-Type": "text/csv; charset=utf-8",
            "Content-Disposition": f'attachment; filename="{result.file_name}"',
            "Cache-Control": "no-store, private",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "ETag": f'"{result.content_sha256}"',
            "X-MetaHub-Audit-Export-ID": result.audit_export_id,
            "X-MetaHub-Audit-Export-Row-Count": str(result.row_count),
        },
    )


@router.get("", response_model=AuditLogListResponse)
async def get_audit_logs(
    request: Request,
    context: Annotated[AuthContext, Depends(require_audit_log_reader)],
    actor_user_id: str | None = None,
    action: str | None = None,
    object_type: str | None = None,
    object_id: str | None = None,
    request_id_filter: Annotated[str | None, Query(alias="request_id")] = None,
    created_from: str | None = None,
    created_to: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> AuditLogListResponse:
    """分页查询当前租户管理审计；客户端不能传 tenant_id 改变隔离边界。"""

    return list_audit_logs(
        request.app.state.config,
        context,
        actor_user_id=actor_user_id,
        action=action,
        object_type=object_type,
        object_id=object_id,
        request_id_filter=request_id_filter,
        created_from=created_from,
        created_to=created_to,
        limit=limit,
        offset=offset,
        request_id=getattr(request.state, "request_id", ""),
        trace_id=getattr(request.state, "trace_id", ""),
    )
