"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/dlp/routes.py
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from metahub_control.audit import build_audit_request_meta
from metahub_control.auth import AuthContext, require_dlp_reader, require_dlp_writer

from .models import CreateDlpRuleRequest, DlpRuleListResponse, DlpRuleResponse, UpdateDlpRuleRequest
from .service import create_dlp_rule, list_dlp_rules, update_dlp_rule

router = APIRouter(tags=["dlp"])


@router.get("/dlp/rules", response_model=DlpRuleListResponse)
async def list_dlp_rules_route(
    request: Request,
    context: Annotated[AuthContext, Depends(require_dlp_reader)],
    status: Annotated[str | None, Query()] = None,
    rule_type: Annotated[str | None, Query()] = None,
    action: Annotated[str | None, Query()] = None,
    scan_scope: Annotated[str | None, Query()] = None,
) -> DlpRuleListResponse:
    """读取当前租户 DLP 规则列表，并在后端校验权限、租户和检测方向。"""

    return list_dlp_rules(
        request.app.state.config,
        context,
        status=status,
        rule_type=rule_type,
        action=action,
        scan_scope=scan_scope,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post("/dlp/rules", response_model=DlpRuleResponse)
async def create_dlp_rule_route(
    request: Request,
    payload: CreateDlpRuleRequest,
    context: Annotated[AuthContext, Depends(require_dlp_writer)],
) -> DlpRuleResponse:
    """创建当前租户 DLP 规则，pattern 仅在 admin 管理接口中出现。"""

    return create_dlp_rule(
        request.app.state.config,
        context,
        payload,
        audit_request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.patch("/dlp/rules/{rule_id}", response_model=DlpRuleResponse)
async def patch_dlp_rule_route(
    rule_id: str,
    request: Request,
    payload: UpdateDlpRuleRequest,
    context: Annotated[AuthContext, Depends(require_dlp_writer)],
) -> DlpRuleResponse:
    """更新当前租户 DLP 规则，路径 ID 必须在服务层再次按 tenant_id 校验。"""

    return update_dlp_rule(
        request.app.state.config,
        context,
        rule_id,
        payload,
        audit_request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


def _request_id(request: Request) -> str:
    """从请求上下文读取统一 request_id，保证 DLP API 响应和日志可串联。"""

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """从请求上下文读取统一 trace_id，保证跨服务排障链路可串联。"""

    return getattr(request.state, "trace_id", "")
