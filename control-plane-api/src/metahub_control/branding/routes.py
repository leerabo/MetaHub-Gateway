"""PRD-088 BrandingConfig 受保护 API、Logo 下载和固定形状公共投影。"""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from metahub_shared.runtime.errors import ApiError
from pydantic import ValidationError

from metahub_control.audit import build_audit_request_meta
from metahub_control.auth import (
    AuthContext,
    require_branding_manager,
    require_branding_reader,
)

from .models import BrandingPatchRequest, BrandingResponse, PublicBrandingResponse
from .service import (
    get_branding,
    get_branding_logo,
    get_public_branding,
    get_public_branding_logo,
    update_branding,
)

MAX_BODY_BYTES = 256 * 1024
MAX_JSON_DEPTH = 5
MAX_JSON_NODES = 96


class BrandingRoute(APIRoute):
    """将框架校验和未知内部异常收敛为 Branding 固定错误码。"""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError:
                raise ApiError("branding.request_invalid") from None
            except ApiError:
                raise
            except Exception:
                raise ApiError("branding.unavailable") from None

        return handler


router = APIRouter(tags=["branding"], route_class=BrandingRoute)


@router.get("/branding", response_model=BrandingResponse)
async def get_branding_route(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_branding_reader)],
) -> BrandingResponse:
    """读取认证租户 BrandingConfig，不接受任何客户端租户参数。"""

    _reject_query(request)
    _private(response)
    return get_branding(
        request.app.state.config,
        context,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/branding/logo")
async def get_branding_logo_route(
    request: Request,
    context: Annotated[AuthContext, Depends(require_branding_reader)],
) -> Response:
    """返回认证租户的 PNG Logo；正文只存在本次 HTTP 响应内存。"""

    _reject_query(request)
    logo = get_branding_logo(
        request.app.state.config,
        context,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    return _logo_response(logo.body, logo.etag, logo.request_id, logo.trace_id)


@router.patch("/branding", response_model=BrandingResponse)
async def patch_branding_route(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_branding_manager)],
) -> BrandingResponse:
    """按 If-Match revision CAS 更新全局白标并追加审计/EVT-100 source。"""

    _reject_query(request)
    revision = _if_match(request)
    payload = await _parse_exact_json(request)
    _private(response)
    return update_branding(
        request.app.state.config,
        context,
        payload,
        revision=revision,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
        audit_request_meta=build_audit_request_meta(request),
    )


@router.get("/branding/public", response_model=PublicBrandingResponse)
async def get_public_branding_route(request: Request, response: Response) -> PublicBrandingResponse:
    """返回登录页固定形状品牌投影，非法/不存在 slug 仍为默认 200。"""

    slug = _public_slug(request)
    _private(response)
    return get_public_branding(
        request.app.state.config,
        tenant_slug=slug,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/branding/public/logo")
async def get_public_branding_logo_route(request: Request) -> Response:
    """公共 Logo 下载不区分未知租户与未配置 Logo。"""

    slug = _public_slug(request)
    logo = get_public_branding_logo(request.app.state.config, tenant_slug=slug)
    return _logo_response(logo.body, logo.etag, _request_id(request), _trace_id(request))


async def _parse_exact_json(request: Request) -> BrandingPatchRequest:
    """有界读取 JSON object，拒绝重复键、未知字段和结构资源攻击。"""

    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise ApiError("branding.request_invalid")
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            size = int(declared)
        except ValueError:
            raise ApiError("branding.request_invalid") from None
        if not 1 <= size <= MAX_BODY_BYTES:
            code = (
                "branding.logo_too_large"
                if size > MAX_BODY_BYTES
                else "branding.request_invalid"
            )
            raise ApiError(code)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise ApiError("branding.logo_too_large")
        body.extend(chunk)
    try:
        parsed = json.loads(bytes(body).decode("utf-8", errors="strict"), object_pairs_hook=_unique)
        if type(parsed) is not dict or _unsafe_json(parsed):
            raise ValueError("unsafe_json")
        return BrandingPatchRequest.model_validate(parsed)
    except ValidationError as exc:
        fields = {
            str(error["loc"][0])
            for error in exc.errors()
            if error.get("loc")
        }
        if "custom_domain" in fields:
            raise ApiError("branding.custom_domain_invalid") from None
        if any(
            error.get("loc") and error["loc"][0] == "logo_png_base64"
            and error.get("type") == "string_too_long"
            for error in exc.errors()
        ):
            raise ApiError("branding.logo_too_large") from None
        raise ApiError("branding.request_invalid") from None
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ApiError("branding.request_invalid") from None


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝对象内重复键，避免签名/幂等正文歧义。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _unsafe_json(value: object) -> bool:
    """限制 JSON 深度/节点和所有字符串控制字符。"""

    stack: list[tuple[object, int]] = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            return True
        if isinstance(current, str) and any(ord(char) < 32 for char in current):
            return True
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
    return False


def _if_match(request: Request) -> int:
    """读取唯一规范十进制 If-Match revision。"""

    values = [
        value.decode("latin-1")
        for key, value in request.scope.get("headers", [])
        if key.lower() == b"if-match"
    ]
    if len(values) != 1 or not values[0].isascii() or not values[0].isdigit():
        raise ApiError("branding.request_invalid")
    revision = int(values[0])
    if not 1 <= revision <= 2_147_483_647:
        raise ApiError("branding.request_invalid")
    return revision


def _public_slug(request: Request) -> str:
    """严格读取登录页 tenant slug，不把非法值交给数据库模糊匹配。"""

    items = request.query_params.multi_items()
    if len(items) != 1 or items[0][0] != "tenant_slug":
        raise ApiError("branding.request_invalid")
    slug = items[0][1]
    slug_body = slug.replace("-", "").replace("_", "")
    if not slug or len(slug) > 80 or not slug.isascii() or not slug_body.isalnum():
        # 公共投影对格式非法仍按默认品牌返回，避免 query 错误成为租户枚举侧信道。
        return "__invalid__"
    return slug.lower()


def _reject_query(request: Request) -> None:
    """受保护端点不接受 tenant_id 或任何未设计 query。"""

    if request.query_params.multi_items():
        raise ApiError("branding.request_invalid")


def _private(response: Response) -> None:
    """统一受保护和公共 JSON 的缓存边界。"""

    response.headers["Cache-Control"] = "no-store, private"


def _logo_response(body: bytes, etag: str, request_id: str, trace_id: str) -> Response:
    """固定 PNG 下载头，禁止浏览器 MIME 猜测和跨请求缓存。"""

    return Response(
        content=body,
        media_type="image/png",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Length": str(len(body)),
            "ETag": etag,
            "X-Request-ID": request_id,
            "X-Trace-ID": trace_id,
            "X-Content-Type-Options": "nosniff",
        },
    )


def _request_id(request: Request) -> str:
    """读取 middleware 已生成的请求 ID。"""

    return str(getattr(request.state, "request_id", ""))


def _trace_id(request: Request) -> str:
    """读取 middleware 已生成的 trace ID。"""

    return str(getattr(request.state, "trace_id", ""))


__all__ = ["router"]
