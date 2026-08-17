"""PRD-087 伙伴门户路由、严格 wire 解析与私有响应边界。"""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from metahub_shared.runtime.errors import ApiError
from pydantic import BaseModel, ValidationError

from metahub_control.auth import (
    AuthContext,
    require_partner_branding_manager,
    require_partner_customer_manager,
    require_partner_delivery_downloader,
    require_partner_portal_configurer,
    require_partner_portal_reader,
)

from .commission import export_partner_commission_csv, get_partner_commission
from .models import (
    CreatePartnerCustomerRequest,
    CreatePartnerRequest,
    PartnerCommissionResponse,
    PartnerCustomerResponse,
    PartnerCustomersResponse,
    PartnerProjectsResponse,
    PartnerResponse,
    UpdateBrandingRequest,
    UpdatePartnerCustomerRequest,
)
from .service import (
    DEFAULT_PAGE_SIZE,
    create_partner,
    create_partner_customer,
    generate_delivery_package,
    get_partner_profile,
    list_partner_customers,
    list_partner_projects,
    normalize_idempotency_key,
    normalize_revision,
    parse_pagination,
    parse_window,
    update_partner_branding,
    update_partner_customer,
)

MAX_BODY_BYTES = 8 * 1024
MAX_JSON_DEPTH = 5
MAX_JSON_NODES = 64


class PartnerPortalRoute(APIRoute):
    """把框架校验和未知异常收敛为稳定伙伴领域错误。"""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError:
                raise ApiError("partner.request_invalid") from None
            except ApiError:
                raise
            except Exception:
                raise ApiError("partner.unavailable") from None

        return handler


router = APIRouter(prefix="/partners", tags=["partner-portal"], route_class=PartnerPortalRoute)


@router.post("", response_model=PartnerResponse, status_code=status.HTTP_201_CREATED)
async def post_partner(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_partner_portal_configurer)],
) -> PartnerResponse:
    """创建当前租户唯一 Partner，认证依赖先于正文和幂等键解析。"""

    _reject_query(request)
    key = _idempotency_key(request)
    payload = await _parse_exact_json(request, CreatePartnerRequest)
    _private(response)
    return create_partner(
        request.app.state.config,
        context,
        payload,
        idempotency_key=key,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/profile", response_model=PartnerResponse)
async def get_partner_profile_route(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_partner_portal_reader)],
) -> PartnerResponse:
    """读取认证租户的 Partner 与安全白标摘要。"""

    _reject_query(request)
    _private(response)
    return get_partner_profile(
        request.app.state.config,
        context,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.patch("/branding", response_model=PartnerResponse)
async def patch_partner_branding(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_partner_branding_manager)],
) -> PartnerResponse:
    """按 If-Match revision CAS 更新纯文本白标字段。"""

    _reject_query(request)
    revision = _if_match(request)
    payload = await _parse_exact_json(request, UpdateBrandingRequest)
    _private(response)
    return update_partner_branding(
        request.app.state.config,
        context,
        payload,
        revision=revision,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/projects", response_model=PartnerProjectsResponse)
async def get_partner_projects_route(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_partner_customer_manager)],
) -> PartnerProjectsResponse:
    """列出当前租户 active 且尚未建立伙伴客户映射的 Project。"""

    _reject_query(request)
    _private(response)
    return list_partner_projects(
        request.app.state.config,
        context,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/customers", response_model=PartnerCustomersResponse)
async def get_partner_customers_route(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_partner_portal_reader)],
) -> PartnerCustomersResponse:
    """分页返回客户映射、Project 历史用量与分润物化新鲜度。"""

    query = _strict_query(request, {"start_date", "end_date", "status", "q", "limit", "offset"})
    window = parse_window(query.get("start_date"), query.get("end_date"))
    limit, offset = parse_pagination(
        _query_integer(query, "limit", DEFAULT_PAGE_SIZE), _query_integer(query, "offset", 0)
    )
    _private(response)
    return list_partner_customers(
        request.app.state.config,
        context,
        window=window,
        status=query.get("status"),
        query=query.get("q"),
        limit=limit,
        offset=offset,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post(
    "/customers", response_model=PartnerCustomerResponse, status_code=status.HTTP_201_CREATED
)
async def post_partner_customer(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_partner_customer_manager)],
) -> PartnerCustomerResponse:
    """原子创建 Project 映射、分润游标、EVT-099 source 与操作摘要。"""

    _reject_query(request)
    key = _idempotency_key(request)
    payload = await _parse_exact_json(request, CreatePartnerCustomerRequest)
    _private(response)
    return create_partner_customer(
        request.app.state.config,
        context,
        payload,
        idempotency_key=key,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.patch("/customers/{partner_customer_id}", response_model=PartnerCustomerResponse)
async def patch_partner_customer(
    request: Request,
    response: Response,
    partner_customer_id: str,
    context: Annotated[AuthContext, Depends(require_partner_customer_manager)],
) -> PartnerCustomerResponse:
    """按 revision 修改展示名或冻结 active 客户的分润 cutoff。"""

    _reject_query(request)
    revision = _if_match(request)
    payload = await _parse_exact_json(request, UpdatePartnerCustomerRequest)
    _private(response)
    return update_partner_customer(
        request.app.state.config,
        context,
        partner_customer_id,
        payload,
        revision=revision,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/commission", response_model=PartnerCommissionResponse)
async def get_partner_commission_route(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_partner_portal_reader)],
) -> PartnerCommissionResponse | Response:
    """返回 JSON 分润报表；CSV 模式在服务内再次要求独立导出权限。"""

    query = _strict_query(
        request, {"start_date", "end_date", "customer_id", "format", "limit", "offset"}
    )
    window = parse_window(query.get("start_date"), query.get("end_date"))
    output_format = query.get("format", "json")
    if output_format not in {"json", "csv"}:
        raise ApiError("partner.request_invalid")
    if output_format == "csv":
        if "limit" in query or "offset" in query:
            raise ApiError("partner.request_invalid")
        attachment = export_partner_commission_csv(
            request.app.state.config,
            context,
            window=window,
            customer_id=query.get("customer_id"),
            request_id=_request_id(request),
            trace_id=_trace_id(request),
        )
        return _attachment_response(attachment)
    limit, offset = parse_pagination(
        _query_integer(query, "limit", DEFAULT_PAGE_SIZE), _query_integer(query, "offset", 0)
    )
    _private(response)
    return get_partner_commission(
        request.app.state.config,
        context,
        window=window,
        customer_id=query.get("customer_id"),
        limit=limit,
        offset=offset,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/delivery-package")
async def get_partner_delivery_package_route(
    request: Request,
    context: Annotated[AuthContext, Depends(require_partner_delivery_downloader)],
) -> Response:
    """生成不含 Project、凭据、内部 Endpoint 或业务明细的安全 JSON 附件。"""

    _reject_query(request)
    attachment = generate_delivery_package(
        request.app.state.config,
        context,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    return _attachment_response(attachment)


def _attachment_response(attachment) -> Response:
    """构造 nosniff 私有附件响应，文件名只允许服务端稳定 ASCII。"""

    return Response(
        content=attachment.body,
        headers={
            "Content-Type": attachment.content_type,
            "Content-Disposition": f'attachment; filename="{attachment.filename}"',
            "Cache-Control": "no-store, private",
            "X-Request-ID": attachment.request_id,
            "X-Trace-ID": attachment.trace_id,
            "X-Content-Type-Options": "nosniff",
        },
    )


def _strict_query(request: Request, allowed: set[str]) -> dict[str, str]:
    """拒绝未知、重复、空值和控制字符查询参数。"""

    result: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if (
            key not in allowed
            or key in result
            or not value
            or any(ord(char) < 32 for char in value)
        ):
            raise ApiError("partner.request_invalid")
        result[key] = value
    return result


def _reject_query(request: Request) -> None:
    """无查询参数端点拒绝 tenant_id 及任何未来含糊输入。"""

    if request.query_params.multi_items():
        raise ApiError("partner.request_invalid")


def _query_integer(query: dict[str, str], key: str, default: int) -> int:
    """读取十进制非负查询整数，不接受符号、空格或 bool 兼容值。"""

    value = query.get(key)
    if value is None:
        return default
    if not value.isascii() or not value.isdigit():
        raise ApiError("partner.request_invalid")
    return int(value)


def _idempotency_key(request: Request) -> str:
    """从原始 Header 取得唯一幂等键，避免框架合并重复值。"""

    values = [
        value.decode("latin-1")
        for key, value in request.scope.get("headers", [])
        if key.lower() == b"idempotency-key"
    ]
    if len(values) != 1:
        raise ApiError("partner.request_invalid")
    return normalize_idempotency_key(values[0])


def _if_match(request: Request) -> int:
    """从原始 Header 取得唯一强 revision。"""

    values = [
        value.decode("latin-1")
        for key, value in request.scope.get("headers", [])
        if key.lower() == b"if-match"
    ]
    if len(values) != 1:
        raise ApiError("partner.request_invalid")
    return normalize_revision(values[0])


async def _parse_exact_json[ModelT: BaseModel](request: Request, model: type[ModelT]) -> ModelT:
    """有界读取 UTF-8 object，拒绝重复键、未知字段、控制字符和结构攻击。"""

    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise ApiError("partner.request_invalid")
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            size = int(declared)
        except ValueError:
            raise ApiError("partner.request_invalid") from None
        if not 1 <= size <= MAX_BODY_BYTES:
            raise ApiError("partner.request_invalid")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise ApiError("partner.request_invalid")
        body.extend(chunk)
    if not body:
        raise ApiError("partner.request_invalid")
    try:
        parsed = json.loads(
            bytes(body).decode("utf-8", errors="strict"), object_pairs_hook=_reject_duplicates
        )
        if type(parsed) is not dict or _unsafe_json(parsed):
            raise ValueError("unsafe_json")
        return model.model_validate(parsed)
    except (UnicodeDecodeError, ValueError, ValidationError, RecursionError):
        raise ApiError("partner.request_invalid") from None


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """在 JSON 任意 object 层发现重复键时立即拒绝。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _unsafe_json(value: object) -> bool:
    """限制 JSON 深度、节点数和字符串控制字符。"""

    stack: list[tuple[object, int]] = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if depth > MAX_JSON_DEPTH or nodes > MAX_JSON_NODES:
            return True
        if type(current) is str and any(ord(char) < 32 for char in current):
            return True
        if type(current) is dict:
            stack.extend((item, depth + 1) for pair in current.items() for item in pair)
        elif type(current) is list:
            stack.extend((item, depth + 1) for item in current)
    return False


def _private(response: Response) -> None:
    """为 JSON 成功响应设置私有且禁止缓存策略。"""

    response.headers["Cache-Control"] = "no-store, private"


def _request_id(request: Request) -> str:
    """读取统一中间件生成的 request_id。"""

    return str(getattr(request.state, "request_id", ""))


def _trace_id(request: Request) -> str:
    """读取统一中间件生成的 trace_id。"""

    return str(getattr(request.state, "trace_id", ""))


__all__ = ["router"]
