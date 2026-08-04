"""提供 PRD-059 租户级权限目录读取与角色矩阵替换接口。

PATCH 正文由路由在认证依赖成功后有界读取，确保匿名或失权请求不能通过 JSON、长度或
字段校验差异探测权限目录合同。
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from metahub_shared.runtime.errors import ApiError
from pydantic import ValidationError

from metahub_control.audit import build_audit_request_meta
from metahub_control.auth import AuthContext, require_permission_matrix_manager

from .models import (
    PermissionMatrixResponse,
    ReplaceRolePermissionsRequest,
    RolePermissionUpdateResponse,
)
from .service import list_permission_matrix, replace_role_permissions

router = APIRouter(tags=["permissions"])

# 以下正文、结构和字段上限共同阻断资源放大与代理解析歧义，不能由客户端覆盖。
MAX_PERMISSION_BODY_BYTES = 32 * 1024
MAX_JSON_DEPTH = 4
MAX_JSON_NODES = 128
MAX_CONTENT_LENGTH_DIGITS = 6
PATCH_FIELDS = frozenset({"expected_revision", "permission_codes"})
PATCH_OPENAPI = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {
                "schema": ReplaceRolePermissionsRequest.model_json_schema(),
            }
        },
    }
}


class _DuplicateJsonKey(ValueError):
    """标记任意层级的重复 JSON key，异常对象不保存原始字段或正文。"""


class _RejectedJsonNumber(ValueError):
    """标记权限请求中不受支持的浮点、指数或非有限数值。"""


@router.get("/permissions", response_model=PermissionMatrixResponse)
async def get_permissions(
    request: Request,
    context: Annotated[AuthContext, Depends(require_permission_matrix_manager)],
) -> PermissionMatrixResponse:
    """读取当前认证租户的完整权限目录与六角色矩阵。"""

    # 权限请求正文必须严格、唯一且有界；异常统一拒绝，不能形成解析差异侧信道。
    return list_permission_matrix(
        request.app.state.config,
        context,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.patch(
    "/roles/{role_id}/permissions",
    response_model=RolePermissionUpdateResponse,
    openapi_extra=PATCH_OPENAPI,
)
async def patch_role_permissions(
    role_id: str,
    request: Request,
    context: Annotated[AuthContext, Depends(require_permission_matrix_manager)],
) -> RolePermissionUpdateResponse:
    """以 revision CAS 完整替换当前租户内一个非 admin 固定角色的权限。"""

    # FastAPI 依赖先完成逐请求数据库授权；到达此处后才读取正文，避免 wire oracle。
    payload = await _read_replace_payload(request)
    return replace_role_permissions(
        request.app.state.config,
        context,
        role_id,
        payload,
        request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


async def _read_replace_payload(request: Request) -> ReplaceRolePermissionsRequest:
    """有界读取严格 UTF-8 JSON，并拒绝重复键、未知字段和代理解释歧义。"""

    _require_json_content_type(request)
    declared_length = _content_length(request)
    if declared_length is not None and (
        declared_length < 1 or declared_length > MAX_PERMISSION_BODY_BYTES
    ):
        # 声明已越界时在分配正文缓冲区前失败，避免无界内存消耗。
        raise ApiError("rbac.payload_invalid")
    body = bytearray()
    async for chunk in request.stream():
        # 权限请求正文必须严格、唯一且有界；异常统一拒绝，不能形成解析差异侧信道。
        if len(body) + len(chunk) > MAX_PERMISSION_BODY_BYTES:
            # 实际流量独立受限，不能只相信客户端 Content-Length。
            raise ApiError("rbac.payload_invalid")
        body.extend(chunk)
    if not body or (declared_length is not None and declared_length != len(body)):
        # 空正文、截断或拼接正文都没有唯一矩阵意图，必须失败关闭。
        raise ApiError("rbac.payload_invalid")
    try:
        parsed = json.loads(
            bytes(body).decode("utf-8", errors="strict"),
            object_pairs_hook=_object_without_duplicates,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateJsonKey,
        _RejectedJsonNumber,
        RecursionError,
        ValueError,
    ):
        # 解析异常可能携带原始权限正文，只返回固定错误码且不链接异常文本。
        raise ApiError("rbac.payload_invalid") from None
    if type(parsed) is not dict or set(parsed) != PATCH_FIELDS:
        # PATCH 是完整替换合同，字段缺失或未来未知字段都不能被静默忽略。
        raise ApiError("rbac.payload_invalid")
    _validate_json_shape(parsed)
    try:
        # 权限请求正文必须严格、唯一且有界；异常统一拒绝，不能形成解析差异侧信道。
        return ReplaceRolePermissionsRequest.model_validate(parsed, strict=True)
    # 权限请求正文必须严格、唯一且有界；异常统一拒绝，不能形成解析差异侧信道。
    except ValidationError:
        # Pydantic 错误可能保存原始输入，公开错误只保留稳定领域码。
        raise ApiError("rbac.payload_invalid") from None


def _require_json_content_type(request: Request) -> None:
    """要求唯一 JSON 媒体类型与 UTF-8 字符集，阻断代理链对正文产生不同解释。"""

    values = _raw_header_values(request, b"content-type")
    if len(values) != 1:
        # 权限请求正文必须严格、唯一且有界；异常统一拒绝，不能形成解析差异侧信道。
        raise ApiError("rbac.payload_invalid")
    try:
        text = values[0].decode("ascii", errors="strict")
    # 权限请求正文必须严格、唯一且有界；异常统一拒绝，不能形成解析差异侧信道。
    except UnicodeDecodeError:
        # 权限请求正文必须严格、唯一且有界；异常统一拒绝，不能形成解析差异侧信道。
        raise ApiError("rbac.payload_invalid") from None
    parts = [part.strip() for part in text.split(";")]
    if not parts or parts[0].lower() != "application/json":
        # 权限请求正文必须严格、唯一且有界；异常统一拒绝，不能形成解析差异侧信道。
        raise ApiError("rbac.payload_invalid")
    seen_charset = False
    for parameter in parts[1:]:
        if not parameter or "=" not in parameter:
            # 权限请求正文必须严格、唯一且有界；异常统一拒绝，不能形成解析差异侧信道。
            raise ApiError("rbac.payload_invalid")
        name, value = (part.strip() for part in parameter.split("=", 1))
        if name.lower() != "charset" or seen_charset or value.lower() not in {
            "utf-8",
            '"utf-8"',
        }:
            # 权限请求正文必须严格、唯一且有界；异常统一拒绝，不能形成解析差异侧信道。
            raise ApiError("rbac.payload_invalid")
        seen_charset = True


def _content_length(request: Request) -> int | None:
    """读取唯一规范 Content-Length；缺失时由流式字节上限保护。"""

    values = _raw_header_values(request, b"content-length")
    if not values:
        return None
    if len(values) != 1:
        # 多个长度头可能在代理链上得到不同解释，不能选择其中任意一个。
        raise ApiError("rbac.payload_invalid")
    try:
        text = values[0].decode("ascii", errors="strict")
    # 权限请求正文必须严格、唯一且有界；异常统一拒绝，不能形成解析差异侧信道。
    except UnicodeDecodeError:
        # 权限请求正文必须严格、唯一且有界；异常统一拒绝，不能形成解析差异侧信道。
        raise ApiError("rbac.payload_invalid") from None
    if (
        not text
        or len(text) > MAX_CONTENT_LENGTH_DIGITS
        or any(character < "0" or character > "9" for character in text)
        or (len(text) > 1 and text.startswith("0"))
    ):
        # 权限请求正文必须严格、唯一且有界；异常统一拒绝，不能形成解析差异侧信道。
        raise ApiError("rbac.payload_invalid")
    return int(text)


def _raw_header_values(request: Request, name: bytes) -> list[bytes]:
    """直接读取 ASGI Header 列表，保留重复项供安全校验。"""

    return [
        value
        for raw_name, value in request.scope.get("headers", [])
        if raw_name.lower() == name
    ]


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """构造 JSON object，并拒绝任意层级的重复键。"""

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey()
        result[key] = value
    return result


def _reject_number(_: str) -> Any:
    """拒绝 float、指数、NaN 与 Infinity，保持 revision 字节语义唯一。"""

    raise _RejectedJsonNumber()


def _validate_json_shape(value: object) -> None:
    """以迭代方式限制 JSON 深度和节点数，阻断宽列表或深容器放大。"""

    nodes = 0
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            # 权限请求正文必须严格、唯一且有界；异常统一拒绝，不能形成解析差异侧信道。
            raise ApiError("rbac.payload_invalid")
        if type(current) is dict:
            stack.extend((item, depth + 1) for item in current.values())
        elif type(current) is list:
            stack.extend((item, depth + 1) for item in current)


def _request_id(request: Request) -> str:
    """读取中间件生成的统一请求标识，供响应、审计和事件关联。"""

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """读取中间件生成的统一追踪标识，供响应、审计和事件关联。"""

    return getattr(request.state, "trace_id", "")
