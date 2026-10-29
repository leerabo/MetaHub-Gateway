"""提供隔离 PlatformSession 凭据域的 Gateway fleet 管理路由，边界是不接受其他会话域。"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from metahub_shared.runtime.errors import ApiError

from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
    SUPPORT_SESSION_COOKIE,
)
from metahub_control.platform_access.service import (
    PlatformContext,
    authenticate_platform_session_context,
)
from metahub_control.saas_billing.routes import (
    _cookie_values,
    _private,
    _raw_headers,
    _request_id,
    _require_origin,
    _required_cookie,
    _required_csrf,
    _strict_json,
    _trace_id,
)
from metahub_control.workspace_access.constants import WORKSPACE_SESSION_COOKIE

from .models import (
    GatewayCellListResponse,
    GatewayPlacementResponse,
    GatewayPlacementUpdateRequest,
)
from .service import list_gateway_cells, update_workspace_placement

router = APIRouter(tags=["platform-gateway"])
# 幂等键只允许固定可打印字符与有界长度，错误响应绝不回显原始键值。
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")


def require_platform_gateway(request: Request) -> PlatformContext:
    """拒绝 Bearer/Workspace/Support 混用并按读写方法复核平台权限与 CSRF。"""

    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, WORKSPACE_SESSION_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        # 平台路由不能混用 Bearer、Workspace 或 Support 凭据，避免会话域权限串联。
        raise ApiError("platform.credential_conflict")
    csrf = None
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        _require_origin(request, "platform.origin_invalid")
        csrf = _required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid")
    context = authenticate_platform_session_context(
        request.app.state.config,
        _required_cookie(request, PLATFORM_SESSION_COOKIE, "platform.session_required"),
        csrf_token=csrf,
    )
    permission = "platform.gateway.read" if request.method == "GET" else "platform.gateway.manage"
    # 写权限与读权限独立复核，不能仅凭导航可见性放行 placement 变更。
    if permission not in context.permissions:
        raise ApiError("platform.permission_denied")
    return context


@router.get("/platform/gateway-cells", response_model=GatewayCellListResponse)
async def gateway_cells(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_gateway)],
) -> GatewayCellListResponse:
    """返回无内部地址、Provider 或客户信息的 Cell 安全摘要。"""

    _reject_query(request)
    result = list_gateway_cells(
        request.app.state.config,
        session_token=_required_cookie(
            request, PLATFORM_SESSION_COOKIE, "platform.session_required"
        ),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


@router.patch(
    "/platform/workspaces/{workspace_id}/gateway-placement",
    response_model=GatewayPlacementResponse,
)
async def patch_gateway_placement(
    workspace_id: str,
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_gateway)],
) -> GatewayPlacementResponse:
    """解析严格 JSON 与并发头后，在服务层原子提交 placement 和审计/outbox。"""

    _reject_query(request)
    try:
        payload = GatewayPlacementUpdateRequest.model_validate(
            await _strict_json(request, "platform_gateway.request_invalid")
        )
    # Pydantic 或有界正文解析失败统一映射固定错误，不返回具体字段和值。
    except ValueError:
        raise ApiError("platform_gateway.request_invalid") from None
    result = update_workspace_placement(
        request.app.state.config,
        session_token=_required_cookie(
            request, PLATFORM_SESSION_COOKIE, "platform.session_required"
        ),
        csrf_token=_required_csrf(
            request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid"
        ),
        workspace_id=workspace_id,
        expected_revision=_if_match(request),
        idempotency_key=_idempotency_key(request),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = f'"{result.placement.revision}"'
    _private(response)
    return result


def _if_match(request: Request) -> int:
    """只接受一个强十进制 ETag，拒绝弱 ETag、列表和非规范数字。"""

    values = _raw_headers(request, b"if-match")
    # 只承认单个强 ETag，防止弱比较或列表语义绕过 revision CAS。
    if len(values) != 1 or re.fullmatch(r'"(?:0|[1-9][0-9]*)"', values[0]) is None:
        raise ApiError("platform_gateway.request_invalid")
    return int(values[0][1:-1])


def _idempotency_key(request: Request) -> str:
    """读取单一有界幂等键，错误不回显原值。"""

    values = _raw_headers(request, b"idempotency-key")
    # 缺失、重复或越界幂等键都必须在服务事务前失败关闭。
    if len(values) != 1 or _IDEMPOTENCY_RE.fullmatch(values[0]) is None:
        raise ApiError("platform_gateway.request_invalid")
    return values[0]


def _reject_query(request: Request) -> None:
    """拒绝所有查询参数，防止客户端通过 query 覆盖 owner、Region 或 placement 事实。"""

    # Owner 和区域只能由路径、会话与数据库恢复，query 出现任何字段都拒绝。
    if request.query_params.multi_items():
        raise ApiError("platform_gateway.request_invalid")


__all__ = ["router"]
