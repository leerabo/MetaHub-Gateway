"""PRD-078 审批中心、移动卡片和内部生产者 HTTP 路由。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from metahub_control.auth import (
    AuthContext,
    require_approval_decider,
    require_approval_internal_producer,
    require_approval_reader,
)

from .models import (
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    ApprovalDetailResponse,
    ApprovalListResponse,
    ApprovalStepUpRequest,
    ApprovalStepUpResponse,
    CreateApprovalRequest,
    CreateApprovalResponse,
)
from .service import (
    create_approval,
    decide_approval,
    get_approval,
    issue_step_up,
    list_approvals,
)
from .wire import (
    MAX_DECISION_BODY_BYTES,
    MAX_INTERNAL_BODY_BYTES,
    MAX_STEP_UP_BODY_BYTES,
    ApprovalRoute,
    parse_exact_json,
    private_no_store,
    reject_query,
    request_ids,
    require_unique_header,
    strict_query,
)

router = APIRouter(tags=["approvals"], route_class=ApprovalRoute)


@router.get("/approvals", response_model=ApprovalListResponse)
async def get_approvals_route(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_approval_reader)],
) -> ApprovalListResponse:
    """列出当前租户且目标域仍可读的审批任务。"""

    query = strict_query(request, {"status", "target_type", "limit", "cursor"})
    private_no_store(response)
    request_id, trace_id = request_ids(request)
    return list_approvals(
        request.app.state.config,
        context,
        status=query.get("status"),
        target_type=query.get("target_type"),
        limit=query.get("limit"),
        cursor=query.get("cursor"),
        request_id=request_id,
        trace_id=trace_id,
    )


@router.get("/approvals/{approval_id}", response_model=ApprovalDetailResponse)
async def get_approval_route(
    request: Request,
    response: Response,
    approval_id: str,
    context: Annotated[AuthContext, Depends(require_approval_reader)],
) -> ApprovalDetailResponse:
    """读取单任务安全摘要与 append-only 决策时间线。"""

    reject_query(request)
    private_no_store(response)
    request_id, trace_id = request_ids(request)
    return get_approval(
        request.app.state.config,
        context,
        approval_id,
        request_id=request_id,
        trace_id=trace_id,
    )


@router.post("/approvals/{approval_id}/step-up", response_model=ApprovalStepUpResponse)
async def post_approval_step_up_route(
    request: Request,
    response: Response,
    approval_id: str,
    context: Annotated[AuthContext, Depends(require_approval_decider)],
) -> ApprovalStepUpResponse:
    """验证当前用户密码并签发绑定任务、动作和 Session 的一次性票据。"""

    reject_query(request)
    request_id, trace_id = request_ids(request)
    payload = await parse_exact_json(
        request,
        ApprovalStepUpRequest,
        maximum=MAX_STEP_UP_BODY_BYTES,
    )
    private_no_store(response)
    return issue_step_up(
        request.app.state.config,
        context,
        approval_id,
        payload,
        request_id=request_id,
        trace_id=trace_id,
    )


async def _decide(
    request: Request,
    response: Response,
    approval_id: str,
    context: AuthContext,
    *,
    action: str,
) -> ApprovalDecisionResponse:
    """共享两个真实动作端点的 exact 解析，但不创建带竖线的伪路由。"""

    reject_query(request)
    request_id, trace_id = request_ids(request)
    idempotency_key = require_unique_header(
        request,
        b"idempotency-key",
        required_code="approval.idempotency_required",
    )
    payload = await parse_exact_json(
        request,
        ApprovalDecisionRequest,
        maximum=MAX_DECISION_BODY_BYTES,
    )
    private_no_store(response)
    return decide_approval(
        request.app.state.config,
        context,
        approval_id,
        action=action,
        payload=payload,
        idempotency_key=idempotency_key,
        request_id=request_id,
        trace_id=trace_id,
    )


@router.post("/approvals/{approval_id}/approve", response_model=ApprovalDecisionResponse)
async def post_approval_approve_route(
    request: Request,
    response: Response,
    approval_id: str,
    context: Annotated[AuthContext, Depends(require_approval_decider)],
) -> ApprovalDecisionResponse:
    """批准当前审批任务。
    认证、RBAC 与 tenant_id 均由后端可信上下文提供。
    """

    return await _decide(request, response, approval_id, context, action="approve")


@router.post("/approvals/{approval_id}/reject", response_model=ApprovalDecisionResponse)
async def post_approval_reject_route(
    request: Request,
    response: Response,
    approval_id: str,
    context: Annotated[AuthContext, Depends(require_approval_decider)],
) -> ApprovalDecisionResponse:
    """拒绝当前审批任务。
    认证、RBAC 与 tenant_id 均由后端可信上下文提供。
    """

    return await _decide(request, response, approval_id, context, action="reject")


@router.post(
    "/internal/tenants/{tenant_id}/approvals",
    response_model=CreateApprovalResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_internal_approval_route(
    request: Request,
    response: Response,
    tenant_id: str,
    _: Annotated[None, Depends(require_approval_internal_producer)],
) -> CreateApprovalResponse:
    """由受信服务身份为路径租户创建固定目标审批任务。"""

    reject_query(request)
    request_id, trace_id = request_ids(request)
    payload = await parse_exact_json(
        request,
        CreateApprovalRequest,
        maximum=MAX_INTERNAL_BODY_BYTES,
    )
    private_no_store(response)
    return create_approval(
        request.app.state.config,
        tenant_id,
        payload,
        request_id=request_id,
        trace_id=trace_id,
    )


runtime_router = APIRouter()
runtime_router.routes.extend(
    route
    for route in router.routes
    if getattr(route, "path", None) == "/internal/tenants/{tenant_id}/approvals"
)


__all__ = ["router", "runtime_router"]
