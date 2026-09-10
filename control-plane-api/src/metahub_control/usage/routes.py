"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/usage/routes.py
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response

from metahub_control.audit import build_audit_request_meta
from metahub_control.auth import AuthContext, require_workspace_or_legacy_permissions

from .export_service import download_usage_export, get_usage_export_status, request_usage_export
from .models import UsageExportStatusResponse, UsageRecordListResponse, UsageSummaryResponse
from .service import DEFAULT_LIMIT, get_usage_summary, list_usage_records

router = APIRouter(prefix="/usage", tags=["usage"])
UsageReader = Annotated[
    AuthContext, Depends(require_workspace_or_legacy_permissions("billing.usage.read"))
]
UsageExporter = Annotated[
    AuthContext, Depends(require_workspace_or_legacy_permissions("billing.usage.export"))
]
UsageExportWriter = Annotated[
    AuthContext,
    Depends(require_workspace_or_legacy_permissions("billing.usage.export", csrf_required=True)),
]

# GET 创建任务是 Excel 固定合同，因此只接受明确白名单参数，禁止代理或前端夹带 tenant/path/format。
USAGE_EXPORT_QUERY_FIELDS = frozenset(
    {
        "start_date",
        "end_date",
        "model_id",
        "virtual_key_id",
        "consumer_id",
        "environment_id",
        "project_id",
        "application_id",
        "provider_id",
        "estimated",
        "request_id",
        "trace_id",
    }
)


@router.get("/summary", response_model=UsageSummaryResponse)
def get_usage_summary_route(
    request: Request,
    context: UsageReader,
    start_date: str | None = None,
    end_date: str | None = None,
    model_id: str | None = None,
    virtual_key_id: str | None = None,
    consumer_id: str | None = None,
    environment_id: str | None = None,
    project_id: str | None = None,
    application_id: str | None = None,
    provider_id: str | None = None,
    estimated: Annotated[str | None, Query()] = None,
    request_id_filter: Annotated[str | None, Query(alias="request_id")] = None,
    trace_id_filter: Annotated[str | None, Query(alias="trace_id")] = None,
) -> UsageSummaryResponse:
    """读取当前租户的用量概览，按天、模型和 Virtual Key 返回聚合统计。"""

    return get_usage_summary(
        request.app.state.config,
        context,
        start_date=start_date,
        end_date=end_date,
        model_id=model_id,
        virtual_key_id=virtual_key_id,
        consumer_id=consumer_id,
        environment_id=environment_id,
        project_id=project_id,
        application_id=application_id,
        provider_id=provider_id,
        estimated=estimated,
        request_id_filter=request_id_filter,
        trace_id_filter=trace_id_filter,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/records", response_model=UsageRecordListResponse)
def get_usage_records_route(
    request: Request,
    context: UsageReader,
    start_date: str | None = None,
    end_date: str | None = None,
    model_id: str | None = None,
    virtual_key_id: str | None = None,
    consumer_id: str | None = None,
    environment_id: str | None = None,
    project_id: str | None = None,
    application_id: str | None = None,
    provider_id: str | None = None,
    estimated: Annotated[str | None, Query()] = None,
    request_id_filter: Annotated[str | None, Query(alias="request_id")] = None,
    trace_id_filter: Annotated[str | None, Query(alias="trace_id")] = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> UsageRecordListResponse:
    """分页读取当前租户的用量明细，支持调用详情跳转和成本排查。"""

    return list_usage_records(
        request.app.state.config,
        context,
        start_date=start_date,
        end_date=end_date,
        model_id=model_id,
        virtual_key_id=virtual_key_id,
        consumer_id=consumer_id,
        environment_id=environment_id,
        project_id=project_id,
        application_id=application_id,
        provider_id=provider_id,
        estimated=estimated,
        request_id_filter=request_id_filter,
        trace_id_filter=trace_id_filter,
        limit=limit,
        offset=offset,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/export.csv")
def export_usage_csv_route(
    request: Request,
    context: UsageExportWriter,
    start_date: str | None = None,
    end_date: str | None = None,
    model_id: str | None = None,
    virtual_key_id: str | None = None,
    consumer_id: str | None = None,
    environment_id: str | None = None,
    project_id: str | None = None,
    application_id: str | None = None,
    provider_id: str | None = None,
    estimated: Annotated[str | None, Query()] = None,
    request_id_filter: Annotated[str | None, Query(alias="request_id")] = None,
    trace_id_filter: Annotated[str | None, Query(alias="trace_id")] = None,
) -> Response:
    """按当前 admin 和租户创建同步/异步 CSV 导出，未知 query 一律拒绝。"""

    # FastAPI 默认忽略未知 query；批量导出必须显式拒绝 tenant_id、limit、path 等夹带字段。
    if any(key not in USAGE_EXPORT_QUERY_FIELDS for key in request.query_params):
        from metahub_shared.runtime.errors import ApiError

        # 未知字段可能夹带 tenant_id 等越权意图，必须统一按非法导出筛选拒绝。
        raise ApiError("usage.export_filter_invalid")
    result = request_usage_export(
        request.app.state.config,
        context,
        start_date=start_date,
        end_date=end_date,
        model_id=model_id,
        virtual_key_id=virtual_key_id,
        consumer_id=consumer_id,
        environment_id=environment_id,
        project_id=project_id,
        application_id=application_id,
        provider_id=provider_id,
        estimated=estimated,
        request_id_filter=request_id_filter,
        trace_id_filter=trace_id_filter,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
        audit_request_meta=build_audit_request_meta(request),
    )
    if result.content is None:
        return JSONResponse(
            status_code=202,
            content=result.response.model_dump(mode="json"),
            headers={"Cache-Control": "no-store, private"},
        )
    return _csv_response(result.content, result.response)


@router.get("/exports/{usage_export_job_id}", response_model=UsageExportStatusResponse)
def get_usage_export_status_route(
    usage_export_job_id: str,
    request: Request,
    context: UsageExporter,
) -> UsageExportStatusResponse:
    """读取当前租户异步任务状态，不返回 BLOB、lease 或对象路径。"""

    return get_usage_export_status(
        request.app.state.config,
        context,
        usage_export_job_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/exports/{usage_export_job_id}/download")
def download_usage_export_route(
    usage_export_job_id: str,
    request: Request,
    context: UsageExportWriter,
) -> Response:
    """下载当前租户 completed CSV，并在释放正文前原子记录下载审计。"""

    result = download_usage_export(
        request.app.state.config,
        context,
        usage_export_job_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
        audit_request_meta=build_audit_request_meta(request),
    )
    response = UsageExportStatusResponse(
        export_job=result.job,
        status_url=f"/usage/exports/{result.job.usage_export_job_id}",
        download_url=f"/usage/exports/{result.job.usage_export_job_id}/download",
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    return _csv_response(result.content, response)


def _csv_response(content: bytes, status: UsageExportStatusResponse) -> Response:
    """构造禁止缓存和嗅探的 CSV 下载响应，文件名只取服务端已校验任务字段。"""

    return Response(
        content=content,
        status_code=200,
        headers={
            "Content-Type": "text/csv; charset=utf-8",
            "Content-Disposition": f'attachment; filename="{status.export_job.file_name}"',
            "Cache-Control": "no-store, private",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _request_id(request: Request) -> str:
    """从 FastAPI request.state 读取统一 request_id，保证响应与日志可关联。"""

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """从 FastAPI request.state 读取统一 trace_id，保证跨服务排障链路可关联。"""

    return getattr(request.state, "trace_id", "")
