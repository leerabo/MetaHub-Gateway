"""PRD-063 Adapter 规范用户只读与内部激活 HTTP 路由。"""

from __future__ import annotations

import json
import re
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request

from metahub_control.auth import (
    AuthContext,
    require_internal_service_token,
    require_provider_health_reader,
)
from metahub_shared.runtime.errors import ApiError

from .models import AdapterSpecActivationResponse, AdapterSpecListResponse
from .service import activate_adapter_spec_catalog, list_adapter_specs

router = APIRouter(tags=["adapter-specs"])

# 查询白名单、幂等标识格式和正文上限共同限制公开与内部接口的输入攻击面。
_PUBLIC_QUERY_FIELDS = frozenset({"classification", "status"})
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_MAX_ACTIVATION_BODY_BYTES = 1024


def _require_internal_activation_access(
    request: Request,
    internal_token: Annotated[str | None, Header(alias="X-Internal-Token")] = None,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> None:
    """要求唯一的内部 Token 身份，并拒绝与用户 Bearer 会话混用。"""

    # 先完成常量时间 Token 校验；任何失败都发生在 tenant path 与数据库事实读取之前。
    require_internal_service_token(request, internal_token)
    if authorization is not None:
        # 内部发布与用户权限是两套身份边界，双 Header 请求不允许选择其中更宽松的一套。
        raise ApiError("auth.internal_token_invalid")


@router.get("/adapter-specs", response_model=AdapterSpecListResponse)
async def get_adapter_specs(
    request: Request,
    context: Annotated[AuthContext, Depends(require_provider_health_reader)],
) -> AdapterSpecListResponse:
    """返回当前会话租户目录；query 不能选择或覆盖 tenant_id。"""

    query = _read_unique_query(request, allowed=_PUBLIC_QUERY_FIELDS)
    return list_adapter_specs(
        request.app.state.config,
        context,
        classification=query.get("classification"),
        status=query.get("status"),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post(
    "/internal/adapter-specs/{tenant_id}/activate",
    response_model=AdapterSpecActivationResponse,
)
async def post_adapter_spec_activation(
    tenant_id: str,
    request: Request,
    _authorized: Annotated[None, Depends(_require_internal_activation_access)],
) -> AdapterSpecActivationResponse:
    """由内部发布服务激活当前目录。

    身份依赖先于手工读取正文执行，因此缺 Token 时不会解析 tenant 或 JSON。正文使用严格解析器，
    让额外字段、重复键和错误类型统一映射为本领域错误而非框架自由校验信息。
    """

    del _authorized
    # 内部 tenant 只能来自 path，任何 query 都可能制造代理与服务端选择不一致，故整体拒绝。
    if request.query_params:
        raise ApiError("adapter.spec_activation_invalid")
    raw_request_id = _strict_request_id(request)
    payload = await _read_activation_body(request)
    return activate_adapter_spec_catalog(
        request.app.state.config,
        tenant_id,
        catalog_version=payload["catalog_version"],
        catalog_hash=payload["catalog_hash"],
        request_id=raw_request_id,
        trace_id=_trace_id(request),
    )


def _read_unique_query(request: Request, *, allowed: frozenset[str]) -> dict[str, str]:
    """读取无重复、无控制字符的 query 白名单，未知 tenant_id 等字段统一拒绝。"""

    values: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if (
            key not in allowed
            or key in values
            or _has_control(key)
            or _has_control(value)
        ):
            # 未知、重复或含控制字符的筛选不能宽松忽略，否则可能掩盖 tenant 注入尝试。
            raise ApiError("adapter.spec_filter_invalid")
        values[key] = value
    return values


async def _read_activation_body(request: Request) -> dict[str, str]:
    """分块读取两字段 JSON，拒绝重复键、过大正文和非 JSON Content-Type。"""

    media_type = request.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        # 激活正文必须使用唯一 JSON 解释方式，禁止表单或文本类型触发内容嗅探。
        raise ApiError("adapter.spec_activation_invalid")
    content_lengths = [
        value
        for key, value in request.headers.raw
        if key.lower() == b"content-length"
    ]
    if len(content_lengths) > 1:
        # 重复 Content-Length 可能让代理和应用采用不同边界，不能进入 JSON 解析。
        raise ApiError("adapter.spec_activation_invalid")
    if content_lengths:
        try:
            declared_length = int(content_lengths[0].decode("ascii"))
        # 非 ASCII 或非十进制长度会导致不同 HTTP 实现产生分歧，统一按非法激活处理。
        except (UnicodeDecodeError, ValueError):
            raise ApiError("adapter.spec_activation_invalid") from None
        if declared_length < 1 or declared_length > _MAX_ACTIVATION_BODY_BYTES:
            # 声明长度在读取前先失败关闭，避免已知超限正文进入 ASGI 消息循环。
            raise ApiError("adapter.spec_activation_invalid")
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > _MAX_ACTIVATION_BODY_BYTES:
            # 在累计超过上限的首个分块立即停止，避免先把任意大正文完整载入进程内存。
            raise ApiError("adapter.spec_activation_invalid")
        if chunk:
            chunks.append(chunk)
    body = b"".join(chunks)
    if not body:
        # 空正文无法绑定 catalog 身份，不能被解释为当前目录的隐式激活。
        raise ApiError("adapter.spec_activation_invalid")
    try:
        decoded = body.decode("utf-8")
        value = json.loads(decoded, object_pairs_hook=_unique_json_object)
    # 编码、JSON 语法和重复键都可能产生跨解析器差异，统一收敛为领域错误码。
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ApiError("adapter.spec_activation_invalid") from None
    if (
        type(value) is not dict
        or set(value) != {"catalog_version", "catalog_hash"}
        or type(value["catalog_version"]) is not str
        or type(value["catalog_hash"]) is not str
    ):
        # 内部调用方只能提交精确两字段身份，额外字段不得演变成可执行 spec 注入通道。
        raise ApiError("adapter.spec_activation_invalid")
    return value


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """构造 JSON 对象并拒绝重复键，避免代理与服务使用不同的 first/last 语义。"""

    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            # 重复键的 first/last 语义不统一，必须在对象构造阶段直接拒绝。
            raise ValueError("duplicate json key")
        value[key] = item
    return value


def _strict_request_id(request: Request) -> str:
    """读取未经中间件替换的幂等 Header；缺失或被规范化的值均拒绝。"""

    raw = request.headers.get("X-Request-ID")
    if (
        raw is None
        or not _REQUEST_ID.fullmatch(raw)
        or raw != getattr(request.state, "request_id", None)
    ):
        # 缺失、非法或被中间件改写的 request_id 都不能承担激活幂等键职责。
        raise ApiError("adapter.spec_activation_invalid")
    return raw


def _has_control(value: str) -> bool:
    """判断 query 是否含 C0/C1 控制字符，防止解析与日志边界分歧。"""

    return any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value)


def _request_id(request: Request) -> str:
    """读取共享中间件确定的 request_id，供成功响应和统一错误链路关联。"""

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """读取共享中间件确定的 trace_id，禁止由业务正文覆盖链路身份。"""

    return getattr(request.state, "trace_id", "")
