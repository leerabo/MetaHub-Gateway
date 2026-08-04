"""配置导入导出 Control API 路由。

路由只负责后端权限、严格 wire 门禁和 request_id/trace_id 传递；租户身份始终来自
Bearer Session，文件中的来源租户和任何前端状态都不能改变服务层写入边界。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from metahub_shared.runtime.errors import ApiError

from metahub_control.audit import build_audit_request_meta
from metahub_control.auth import (
    AuthContext,
    require_config_transfer_reader,
    require_config_transfer_writer,
)

from .models import ConfigExportResponse, ConfigImportResponse
from .parser import read_strict_json_object
from .service import apply_import, export_config, validate_import

router = APIRouter(tags=["config-transfer"])


@router.post("/config/export", response_model=ConfigExportResponse)
async def post_config_export(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_config_transfer_reader)],
) -> ConfigExportResponse:
    """导出当前认证租户的严格白名单配置，并持久化安全导出事实。

    参数：已通过 ``config.transfer.read`` 的请求、响应头容器和数据库权限快照。
    返回：不含 Provider Credential、Virtual Key 凭据及数据库 UUID 的配置清单。
    约束：权限依赖先于正文读取；正文必须是空 JSON 对象，未知查询参数一律拒绝。
    """

    _reject_query_parameters(request)
    response.headers["Cache-Control"] = "no-store"
    payload = await read_strict_json_object(request)
    if payload:
        # export 没有可由客户端控制的选项，拒绝任意字段可避免未来参数被旧服务静默忽略。
        raise ApiError("config_transfer.body_invalid")
    return export_config(
        request.app.state.config,
        context,
        # User-Agent 完全由客户端控制，配置迁移端点不持久化该字段，避免其成为 Key/Token 审计旁路。
        audit_request_meta=build_audit_request_meta(request, include_user_agent=False),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post("/config/import/validate", response_model=ConfigImportResponse)
async def post_config_import_validation(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_config_transfer_writer)],
) -> ConfigImportResponse:
    """只读预检一份 ConfigExport，不生成 Key、审计或任何业务写入。

    参数：已通过 ``config.transfer.write`` 的请求及当前数据库权限快照。
    返回：``validated`` 或带有有界行错误的 ``invalid`` 报告。
    约束：目标 tenant 只取自 AuthContext；即使预检成功，apply 仍会在写事务内完整重检。
    """

    _reject_query_parameters(request)
    response.headers["Cache-Control"] = "no-store"
    document = await read_strict_json_object(request)
    return validate_import(
        request.app.state.config,
        context,
        document,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post(
    "/config/import",
    response_model=ConfigImportResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_config_import(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_config_transfer_writer)],
) -> ConfigImportResponse:
    """在单个写事务中 create-only 导入配置并一次性交付新 Virtual Key。

    参数：已通过专用 critical 权限的请求及当前租户认证上下文。
    返回：成功为 ``applied``，精确重试为 ``replayed``，业务预检失败为 ``invalid``。
    约束：replayed/invalid 使用 HTTP 200 且绝不重放 Key 明文；新成功批次使用 HTTP 201。
    """

    _reject_query_parameters(request)
    response.headers["Cache-Control"] = "no-store"
    document = await read_strict_json_object(request)
    result = apply_import(
        request.app.state.config,
        context,
        document,
        # 导入正文可能生成一次性 Key，审计来源只保留直连 IP，主动关闭可注入的 User-Agent。
        audit_request_meta=build_audit_request_meta(request, include_user_agent=False),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.report.status != "applied":
        response.status_code = status.HTTP_200_OK
    return result


def _reject_query_parameters(request: Request) -> None:
    """拒绝全部查询参数，且不回显攻击者构造的参数名或值。"""

    if request.query_params:
        # 迁移接口没有查询选项；拒绝全部参数可防止旧服务静默忽略未来安全开关。
        raise ApiError("config_transfer.query_invalid")


def _request_id(request: Request) -> str:
    """读取统一中间件生成或透传的 request_id。"""

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """读取统一中间件生成的 trace_id，供事实、审计、事件和响应共用。"""

    return getattr(request.state, "trace_id", "")
