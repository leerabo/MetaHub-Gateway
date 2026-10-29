"""PRD-078 审批 HTTP 边界的 exact parser 与私有缓存策略。"""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from metahub_shared.runtime.errors import ApiError
from pydantic import BaseModel, ValidationError

# 不同审批入口分别限制请求体大小，防止密码或内部载荷被无界读取并扩大敏感数据驻留。
MAX_INTERNAL_BODY_BYTES = 16 * 1024
MAX_STEP_UP_BODY_BYTES = 4 * 1024
MAX_DECISION_BODY_BYTES = 4 * 1024
MAX_JSON_DEPTH = 4
MAX_JSON_NODES = 64


class _DuplicateJsonKey(ValueError):
    """标记任意层重复 JSON key，避免不同解析器产生不同审批意图。"""


class ApprovalRoute(APIRoute):
    """把框架校验错误折叠为不回显输入的审批固定错误。"""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """``get_route_handler`` 约束审批运行时线协议，拒绝未知字段并避免敏感内容进入响应或日志。"""
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            """``handler`` 约束审批运行时线协议，拒绝未知字段并避免敏感内容进入响应或日志。"""
            try:
                return await original(request)
            # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
            except RequestValidationError:
                # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
                raise ApiError("approval.input_invalid") from None

        return handler


async def parse_exact_json[PayloadT: BaseModel](
    request: Request,
    model: type[PayloadT],
    *,
    maximum: int,
) -> PayloadT:
    """有界读取 UTF-8 JSON，拒绝重复 key、非有限数与 Pydantic 额外字段。"""

    _require_json_content_type(request)
    try:
        body = await read_bounded_body(request, maximum=maximum)
        value = json.loads(
            body.decode("utf-8", errors="strict"),
            object_pairs_hook=_without_duplicate_keys,
            parse_constant=_reject_non_finite_number,
        )
        if type(value) is not dict:
            # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
            raise ValueError("object required")
        _validate_json_shape(value)
        return model.model_validate(value)
    # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
    except ApiError:
        raise
    except (
        json.JSONDecodeError,
        UnicodeError,
        ValidationError,
        _DuplicateJsonKey,
        RecursionError,
        TypeError,
        ValueError,
    ):
        # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
        raise ApiError("approval.input_invalid") from None


async def read_bounded_body(request: Request, *, maximum: int) -> bytes:
    """同时约束 Content-Length 与分块后的实际字节数。"""

    declared = content_length(request)
    if declared is not None and (declared < 1 or declared > maximum):
        # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
        raise ApiError("approval.input_invalid")
    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > maximum:
                # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
                raise ApiError("approval.input_invalid")
            body.extend(chunk)
    # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
    except ApiError:
        raise
    except Exception:
        # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
        raise ApiError("approval.input_invalid") from None
    if not body or (declared is not None and declared != len(body)):
        # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
        raise ApiError("approval.input_invalid")
    return bytes(body)


def content_length(request: Request) -> int | None:
    """只接受零或一个规范十进制 Content-Length。"""

    values = raw_header_values(request, b"content-length")
    if not values:
        return None
    value = values[0]
    if (
        len(values) != 1
        or not value
        or len(value) > 12
        or any(character < "0" or character > "9" for character in value)
    ):
        # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
        raise ApiError("approval.input_invalid")
    return int(value)


def require_unique_header(request: Request, name: bytes, *, required_code: str) -> str:
    """读取唯一非空 Header；重复值绝不合并。"""

    values = raw_header_values(request, name)
    if len(values) != 1 or not values[0].strip() or "," in values[0]:
        # 错误路径必须保留统一错误码与 request_id，并清除敏感上下文后再进入日志或响应。
        raise ApiError(required_code)
    return values[0]


def raw_header_values(request: Request, name: bytes) -> list[str]:
    """从 ASGI 原始 Header 读取全部值，保留重复项检测能力。"""

    return [
        value.decode("latin-1")
        for key, value in request.scope.get("headers", ())
        if key.lower() == name
    ]


def _require_json_content_type(request: Request) -> None:
    """只接受唯一 JSON media type 和至多一个明确的 UTF-8 参数。"""

    raw_values = [
        value for key, value in request.scope.get("headers", ()) if key.lower() == b"content-type"
    ]
    if len(raw_values) != 1 or not raw_values[0] or b"," in raw_values[0]:
        # 缺失、重复和代理逗号折叠都会造成解析语义歧义，统一失败关闭。
        raise ApiError("approval.input_invalid")
    try:
        content_type = raw_values[0].decode("ascii", errors="strict")
    # Content-Type 非 ASCII 时不能进入 media type parser，也不能把原始字节带入异常链。
    except UnicodeDecodeError:
        # 非 ASCII media type 统一映射为固定输入错误，响应不得回显原始 Header。
        raise ApiError("approval.input_invalid") from None

    parts = content_type.split(";")
    # 审批写请求只接受 JSON，其他媒体类型不得进入正文解析或 DTO 校验阶段。
    if parts[0].strip(" \t").lower() != "application/json":
        raise ApiError("approval.input_invalid")
    if len(parts) == 1:
        return
    if len(parts) != 2:
        # 只允许唯一 charset 参数，其他参数及重复 charset 均拒绝。
        raise ApiError("approval.input_invalid")
    name, separator, raw_value = parts[1].partition("=")
    charset = raw_value.strip(" \t")
    if (
        separator != "="
        or name.strip(" \t").lower() != "charset"
        or charset.lower() not in {"utf-8", '"utf-8"'}
    ):
        # 非 UTF-8 或形状不唯一的 charset 会产生代理与应用解析差异，必须固定失败关闭。
        raise ApiError("approval.input_invalid")


def _validate_json_shape(value: object) -> None:
    """以根深度一遍历 JSON，限制深度和节点，阻断窄深或宽幅载荷。"""

    nodes = 0
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if depth > MAX_JSON_DEPTH or nodes > MAX_JSON_NODES:
            # DTO 构造前即终止，避免未知嵌套字段放大 CPU 与内存。
            raise ApiError("approval.input_invalid")
        if type(current) is dict:
            stack.extend((item, depth + 1) for item in current.values())
        elif type(current) is list:
            stack.extend((item, depth + 1) for item in current)


def strict_query(request: Request, allowed: set[str]) -> dict[str, str]:
    """拒绝未知、重复和空 query 参数。
    ``strict_query`` 约束审批运行时线协议，拒绝未知字段并避免敏感内容进入响应或日志。
    """

    result: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key not in allowed or key in result or not value:
            # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
            raise ApiError("approval.input_invalid")
        result[key] = value
    return result


def reject_query(request: Request) -> None:
    """动作端点不接受任何 query 参数。
    ``reject_query`` 约束审批运行时线协议，拒绝未知字段并避免敏感内容进入响应或日志。
    """

    if request.query_params.multi_items():
        # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
        raise ApiError("approval.input_invalid")


def private_no_store(response: Response) -> None:
    """审批响应不得被浏览器、代理或历史页面缓存。"""

    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["Vary"] = "Authorization"


def request_ids(request: Request) -> tuple[str, str]:
    """读取统一 middleware 生成或敏感 POST 主动轮换后的链路 ID。"""

    return (
        str(getattr(request.state, "request_id", "")),
        str(getattr(request.state, "trace_id", "")),
    )


def _without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝任意层重复 key，避免解析器对审批意图产生分歧。"""
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKey
        value[key] = item
    return value


def _reject_non_finite_number(_value: str) -> object:
    """拒绝 JSON 非有限数，保持跨组件审批意图一致。"""
    # 外部或持久化输入必须满足精确类型、长度和枚举约束；非法内容统一映射为固定错误。
    raise ValueError("non-finite number")


__all__ = [
    "ApprovalRoute",
    "MAX_DECISION_BODY_BYTES",
    "MAX_INTERNAL_BODY_BYTES",
    "MAX_STEP_UP_BODY_BYTES",
    "parse_exact_json",
    "private_no_store",
    "raw_header_values",
    "reject_query",
    "request_ids",
    "require_unique_header",
    "strict_query",
]
