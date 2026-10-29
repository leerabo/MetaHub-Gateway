"""PRD-061 Provider 状态 HTTP 路由与后端双权限边界。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from metahub_shared.runtime.errors import ApiError

from metahub_control.auth import AuthContext, require_workspace_or_legacy_permissions

from .models import ProviderStatusListResponse
from .service import list_provider_statuses

router = APIRouter(tags=["provider-status"])

# 状态页只接受固定筛选和分页参数；tenant_id、统计窗口及任意上游字段都不能由客户端注入。
ALLOWED_QUERY_FIELDS = frozenset(
    {"provider_id", "provider_type", "health_status", "provider_status", "limit", "offset"}
)
PAGINATION_FIELDS = frozenset({"limit", "offset"})


@router.get("/status/providers", response_model=ProviderStatusListResponse)
async def get_provider_statuses(
    request: Request,
    response: Response,
    context: Annotated[
        AuthContext,
        Depends(require_workspace_or_legacy_permissions(
            "provider.health.read", "request_log.read",
        )),
    ],
) -> ProviderStatusListResponse:
    """返回当前租户 Provider 状态。

    参数不包含 tenant_id 或统计窗口；租户边界只来自认证上下文，窗口固定为过去 24 小时。
    """

    query = _read_query(request)
    limit = _query_integer(query.get("limit"), default=50, minimum=1, maximum=100)
    offset = _query_integer(query.get("offset"), default=0, minimum=0, maximum=10000)
    # 状态响应包含租户内故障与维护备注，浏览器和中间代理均不得缓存授权后的正文。
    response.headers["Cache-Control"] = "no-store"
    return list_provider_statuses(
        request.app.state.config,
        context,
        provider_id=query.get("provider_id"),
        provider_type=query.get("provider_type"),
        health_status=query.get("health_status"),
        provider_status=query.get("provider_status"),
        limit=limit,
        offset=offset,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


def _read_query(request: Request) -> dict[str, str]:
    """读取唯一白名单查询参数。

    参数：当前 HTTP 请求。返回：未经业务归一化的唯一字符串参数。
    关键约束：重复键在代理间可能具有不同 first/last 语义，未知字段也可能尝试注入租户或窗口，
    因此两类输入都必须在进入聚合服务前失败关闭，且错误响应不回显原值。
    """

    values: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        error_code = (
            "provider_status.pagination_invalid"
            if key in PAGINATION_FIELDS
            else "provider_status.filter_invalid"
        )
        if (
            key not in ALLOWED_QUERY_FIELDS
            or key in values
            or _has_control_character(key)
            or _has_control_character(value)
        ):
            # 不回显未知或恶意 query；分页键与业务筛选分别保持本域稳定错误码。
            raise ApiError(error_code)
        values[key] = value
    return values


def _query_integer(value: str | None, *, default: int, minimum: int, maximum: int) -> int:
    """解析规范 ASCII 十进制分页参数。

    参数：原始 query 值、默认值和闭区间边界。返回：已验证整数。
    关键约束：符号、空白、小数、指数和 Unicode 数字都不属于 wire 合同，不能交给框架宽松转换。
    """

    if value is None:
        return default
    if not value or any(character < "0" or character > "9" for character in value):
        # 禁止框架宽松接受符号、小数或 Unicode 数字，避免代理和服务产生不同分页语义。
        raise ApiError("provider_status.pagination_invalid")
    parsed = int(value)
    if parsed < minimum or parsed > maximum:
        # 明确上限保护租户内聚合成本；调用方不能通过极端 offset/limit 放大扫描范围。
        raise ApiError("provider_status.pagination_invalid")
    return parsed


def _has_control_character(value: str) -> bool:
    """判断 query 键值是否含 C0/C1 控制字符，避免解释差异和日志污染。"""

    return any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value)


def _request_id(request: Request) -> str:
    """读取统一中间件生成或透传的 request_id。"""

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """读取统一中间件生成或透传的 trace_id。"""

    return getattr(request.state, "trace_id", "")
