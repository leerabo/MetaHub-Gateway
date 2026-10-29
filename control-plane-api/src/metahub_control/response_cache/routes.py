"""Response Cache Control API 路由：认证先于正文，并统一严格 HTTP 边界。"""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from metahub_shared.runtime.errors import ApiError
from pydantic import BaseModel, ValidationError

from metahub_control.audit import build_audit_request_meta
from metahub_control.auth import (
    AuthContext,
    require_response_cache_invalidator,
    require_response_cache_reader,
    require_response_cache_writer,
)

from .models import (
    CacheInvalidationResponse,
    CachePolicyListResponse,
    CachePolicyPatchResponse,
    InvalidateResponseCacheRequest,
    PatchCachePolicyRequest,
)
from .service import invalidate_response_cache, list_cache_policies, patch_cache_policy

# 控制面路由必须以后端租户与权限结果为准，并用统一链路标识返回固定错误。
_PATCH_BODY_LIMIT = 16 * 1024
_INVALIDATE_BODY_LIMIT = 8 * 1024
_JSON_DEPTH_LIMIT = 8
_JSON_NODE_LIMIT = 256


class _DuplicateJsonKey(ValueError):
    """标记重复 JSON key，防止 Python 与上游采用不同的最后值语义。"""


class _InvalidJsonNumber(ValueError):
    """标记非整数 JSON number；本组管理请求没有合法浮点字段。"""


class ResponseCacheRoute(APIRoute):
    """把框架参数错误折叠为固定业务码，绝不回显 Header 或正文片段。"""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """包裹 FastAPI 处理器，保持 path/query 验证与手工 parser 使用同一错误码。"""

        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            """控制面路由必须以后端租户与权限结果为准，并用统一链路标识返回固定错误。 当前定义负责 handler 对应步骤。"""
            try:
                return await original(request)
            # 控制面路由必须以后端租户与权限结果为准，并用统一链路标识返回固定错误。
            except RequestValidationError:
                # 控制面路由必须以后端租户与权限结果为准，并用统一链路标识返回固定错误。
                raise ApiError("response_cache.input_invalid") from None

        return handler


router = APIRouter(tags=["response-cache"], route_class=ResponseCacheRoute)


@router.get("/cache-policies", response_model=CachePolicyListResponse)
async def get_cache_policies(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_response_cache_reader)],
) -> CachePolicyListResponse:
    """返回当前 Session tenant 的 active Alias 与可空 CachePolicy 安全摘要。"""

    _reject_repeated_safe_headers(request, include_idempotency=False)
    await _reject_get_body(request)
    query = _strict_query(request, {"limit", "cursor"})
    _no_store(response)
    return list_cache_policies(
        request.app.state.config,
        context,
        limit=query.get("limit"),
        cursor=query.get("cursor"),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.patch("/cache-policies", response_model=CachePolicyPatchResponse)
async def update_cache_policy(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_response_cache_writer)],
) -> CachePolicyPatchResponse:
    """以完整 CAS 快照创建或更新策略，并复用独立 Idempotency-Key owner。"""

    _reject_query(request)
    _reject_repeated_safe_headers(request, include_idempotency=True)
    idempotency_key = _idempotency_key(request)
    payload = await _parse_json(request, PatchCachePolicyRequest, maximum=_PATCH_BODY_LIMIT)
    _no_store(response)
    return patch_cache_policy(
        request.app.state.config,
        context,
        payload,
        idempotency_key=idempotency_key,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
        audit_request_meta=build_audit_request_meta(request),
    )


@router.post("/cache/invalidate", response_model=CacheInvalidationResponse)
async def invalidate_cache(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_response_cache_invalidator)],
) -> CacheInvalidationResponse:
    """失效当前 tenant 的全部缓存或同时匹配一组安全 selector 的缓存。"""

    _reject_query(request)
    _reject_repeated_safe_headers(request, include_idempotency=True)
    idempotency_key = _idempotency_key(request)
    payload = await _parse_json(
        request,
        InvalidateResponseCacheRequest,
        maximum=_INVALIDATE_BODY_LIMIT,
    )
    _no_store(response)
    return invalidate_response_cache(
        request.app.state.config,
        context,
        payload,
        idempotency_key=idempotency_key,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
        audit_request_meta=build_audit_request_meta(request),
    )


async def _parse_json[PayloadT: BaseModel](
    request: Request,
    model: type[PayloadT],
    *,
    maximum: int,
) -> PayloadT:
    """严格读取 UTF-8 JSON object，并拒绝重复键、浮点、超深或未知字段。"""

    if _single_content_type(request) != "application/json":
        # 控制面路由必须以后端租户与权限结果为准，并用统一链路标识返回固定错误。
        raise ApiError("response_cache.input_invalid")
    try:
        body = await _read_body(request, maximum=maximum, allow_empty=False)
        value = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_object_without_duplicates,
            parse_float=_reject_json_number,
            parse_constant=_reject_json_number,
        )
        if type(value) is not dict:
            # 控制面路由必须以后端租户与权限结果为准，并用统一链路标识返回固定错误。
            raise ValueError
        _validate_json_shape(value)
        return model.model_validate(value)
    except (
        json.JSONDecodeError,
        UnicodeError,
        ValidationError,
        _DuplicateJsonKey,
        _InvalidJsonNumber,
        RecursionError,
        TypeError,
        ValueError,
    ):
        # 控制面路由必须以后端租户与权限结果为准，并用统一链路标识返回固定错误。
        raise ApiError("response_cache.input_invalid") from None


async def _reject_get_body(request: Request) -> None:
    """GET 即使使用 chunked body 也必须为空，避免代理与应用解释分叉。"""

    await _read_body(request, maximum=0, allow_empty=True)


async def _read_body(request: Request, *, maximum: int, allow_empty: bool) -> bytes:
    """同时验证声明长度与实际流长度，不能只信任 Content-Length。"""

    declared = _content_length(request)
    if declared is not None and (declared > maximum or (declared == 0 and not allow_empty)):
        # 控制面路由必须以后端租户与权限结果为准，并用统一链路标识返回固定错误。
        raise ApiError("response_cache.input_invalid")
    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > maximum:
                # 控制面路由必须以后端租户与权限结果为准，并用统一链路标识返回固定错误。
                raise ApiError("response_cache.input_invalid")
            body.extend(chunk)
    # 控制面路由必须以后端租户与权限结果为准，并用统一链路标识返回固定错误。
    except ApiError:
        raise
    except Exception:
        # 控制面路由必须以后端租户与权限结果为准，并用统一链路标识返回固定错误。
        raise ApiError("response_cache.input_invalid") from None
    if (not body and not allow_empty) or (declared is not None and declared != len(body)):
        # 控制面路由必须以后端租户与权限结果为准，并用统一链路标识返回固定错误。
        raise ApiError("response_cache.input_invalid")
    return bytes(body)


def _content_length(request: Request) -> int | None:
    """只接受单个短十进制 Content-Length，拒绝折叠值和符号。"""

    values = _raw_headers(request, b"content-length")
    if not values:
        return None
    if len(values) != 1:
        # 控制面路由必须以后端租户与权限结果为准，并用统一链路标识返回固定错误。
        raise ApiError("response_cache.input_invalid")
    try:
        value = values[0].decode("ascii", errors="strict")
    # 控制面路由必须以后端租户与权限结果为准，并用统一链路标识返回固定错误。
    except UnicodeError:
        # 控制面路由必须以后端租户与权限结果为准，并用统一链路标识返回固定错误。
        raise ApiError("response_cache.input_invalid") from None
    if not value or len(value) > 12 or not value.isdigit():
        # 控制面路由必须以后端租户与权限结果为准，并用统一链路标识返回固定错误。
        raise ApiError("response_cache.input_invalid")
    return int(value)


def _single_content_type(request: Request) -> str:
    """管理写接口仅接受一个 exact application/json，不猜测 charset 或多值。"""

    values = _raw_headers(request, b"content-type")
    if len(values) != 1:
        return ""
    try:
        return values[0].decode("ascii", errors="strict")
    # 控制面路由必须以后端租户与权限结果为准，并用统一链路标识返回固定错误。
    except UnicodeError:
        return ""


def _idempotency_key(request: Request) -> str:
    """提取只存在于本次私有 operation ticket 的稳定 intent owner。"""

    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1:
        # 控制面路由必须以后端租户与权限结果为准，并用统一链路标识返回固定错误。
        raise ApiError("response_cache.input_invalid")
    try:
        value = values[0].decode("ascii", errors="strict")
    # 控制面路由必须以后端租户与权限结果为准，并用统一链路标识返回固定错误。
    except UnicodeError:
        # 控制面路由必须以后端租户与权限结果为准，并用统一链路标识返回固定错误。
        raise ApiError("response_cache.input_invalid") from None
    if (
        not 16 <= len(value) <= 128
        or any(not 0x21 <= ord(character) <= 0x7E for character in value)
        or "," in value
        or request.headers.get("idempotency-key") != value
    ):
        # 控制面路由必须以后端租户与权限结果为准，并用统一链路标识返回固定错误。
        raise ApiError("response_cache.input_invalid")
    return value


def _reject_repeated_safe_headers(request: Request, *, include_idempotency: bool) -> None:
    """在 raw ASGI 层拒绝会被框架折叠的安全 Header，多值不能进入业务 parser。"""

    names = (b"authorization", b"x-request-id", b"x-trace-id", b"content-length")
    if include_idempotency:
        names += (b"idempotency-key", b"content-type")
    for name in names:
        if len(_raw_headers(request, name)) > 1:
            # 控制面路由必须以后端租户与权限结果为准，并用统一链路标识返回固定错误。
            raise ApiError("response_cache.input_invalid")


def _raw_headers(request: Request, name: bytes) -> list[bytes]:
    """读取未经 Starlette 合并的 Header 列表。

    控制面路由必须以后端租户与权限结果为准，并用统一链路标识返回固定错误。 当前定义负责 _raw_headers 对应步骤。"""

    return [value for key, value in request.scope.get("headers", ()) if key.lower() == name]


def _strict_query(request: Request, allowed: set[str]) -> dict[str, str]:
    """GET 只接受无编码歧义、无重复且在白名单内的 limit/cursor。"""

    raw = request.scope.get("query_string", b"")
    try:
        text = raw.decode("ascii", errors="strict")
    # 控制面路由必须以后端租户与权限结果为准，并用统一链路标识返回固定错误。
    except UnicodeError:
        # 控制面路由必须以后端租户与权限结果为准，并用统一链路标识返回固定错误。
        raise ApiError("response_cache.query_invalid") from None
    if "%" in text or "+" in text:
        # 控制面路由必须以后端租户与权限结果为准，并用统一链路标识返回固定错误。
        raise ApiError("response_cache.query_invalid")
    result: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key not in allowed or key in result or not value:
            # 控制面路由必须以后端租户与权限结果为准，并用统一链路标识返回固定错误。
            raise ApiError("response_cache.query_invalid")
        result[key] = value
    if text and "&".join(f"{key}={value}" for key, value in result.items()) != text:
        # 控制面路由必须以后端租户与权限结果为准，并用统一链路标识返回固定错误。
        raise ApiError("response_cache.query_invalid")
    return result


def _reject_query(request: Request) -> None:
    """写接口的全部意图都在 strict JSON 中，禁止 query 覆盖。"""

    if request.scope.get("query_string", b""):
        # 控制面路由必须以后端租户与权限结果为准，并用统一链路标识返回固定错误。
        raise ApiError("response_cache.query_invalid")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """构造 JSON object 时即时拒绝重复 key。

    控制面路由必须以后端租户与权限结果为准，并用统一链路标识返回固定错误。 当前定义负责 _object_without_duplicates 对应步骤。"""

    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKey
        value[key] = item
    return value


def _reject_json_number(_: str) -> None:
    """本合同不接受浮点与 NaN/Infinity。

    控制面路由必须以后端租户与权限结果为准，并用统一链路标识返回固定错误。 当前定义负责 _reject_json_number 对应步骤。"""

    raise _InvalidJsonNumber


def _validate_json_shape(value: object) -> None:
    """独立限制节点和深度，避免小字节恶意 JSON 消耗递归资源。"""

    nodes = 0

    def visit(item: object, depth: int) -> None:
        """控制面路由必须以后端租户与权限结果为准，并用统一链路标识返回固定错误。 当前定义负责 visit 对应步骤。"""
        nonlocal nodes
        nodes += 1
        if nodes > _JSON_NODE_LIMIT or depth > _JSON_DEPTH_LIMIT:
            # 控制面路由必须以后端租户与权限结果为准，并用统一链路标识返回固定错误。
            raise ValueError
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    # 控制面路由必须以后端租户与权限结果为准，并用统一链路标识返回固定错误。
                    raise ValueError
                visit(child, depth + 1)
        elif type(item) is list:
            for child in item:
                visit(child, depth + 1)
        elif item is not None and type(item) not in {str, int, bool}:
            # 控制面路由必须以后端租户与权限结果为准，并用统一链路标识返回固定错误。
            raise ValueError

    visit(value, 1)


def _no_store(response: Response) -> None:
    """成功响应与外层错误中间件共用同一租户私有缓存边界。"""

    response.headers["Cache-Control"] = "no-store, private"


def _request_id(request: Request) -> str:
    """读取统一中间件已生成的本次 attempt request ID。"""

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """读取统一中间件已生成的本次 attempt trace ID。"""

    return getattr(request.state, "trace_id", "")


__all__ = ["router"]
