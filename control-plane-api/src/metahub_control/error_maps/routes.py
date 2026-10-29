"""PRD-056 Provider 错误归一规则的公共与内部 HTTP 路由。

认证/RBAC 依赖先于 path、query、header 和 body 的业务解析执行。公共写接口只把原始且未被
中间件改写的 X-Request-ID 作为幂等身份，避免代理拼接、截断或清洗后的值产生意图碰撞。
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import Response as FastAPIResponse
from metahub_shared.runtime.errors import ApiError
from pydantic import ValidationError

from metahub_control.audit import build_audit_request_meta
from metahub_control.auth import (
    AuthContext,
    require_error_map_reader,
    require_error_map_writer,
    require_internal_service_token,
)

from .models import (
    CreateProviderErrorMapRequest,
    ProviderErrorMapListResponse,
    ProviderErrorMapMutationResponse,
    UpdateProviderErrorMapRequest,
)
from .service import (
    build_internal_provider_error_map_response,
    create_provider_error_map,
    list_provider_error_maps,
    update_provider_error_map,
)

router = APIRouter(tags=["error-maps"])
internal_router = APIRouter(tags=["error-maps-internal"])

# Wire 白名单与资源上限必须在任何持久化和意图指纹计算之前执行。
LIST_QUERY_FIELDS = frozenset(
    {
        "error_map_id",
        "provider_id",
        "source",
        "status",
        "output_error_code",
        "limit",
        "offset",
    }
)
EDITABLE_FIELDS = frozenset(
    {
        "provider_id",
        "priority",
        "http_statuses",
        "http_status_class",
        "error_field",
        "field_operator",
        "field_pattern",
        "message_operator",
        "message_pattern",
        "output_error_code",
        "status",
    }
)
CREATE_FIELDS = EDITABLE_FIELDS
PATCH_FIELDS = EDITABLE_FIELDS | {"expected_version"}
MAX_BODY_BYTES = 16 * 1024
MAX_JSON_DEPTH = 16
MAX_JSON_NODES = 128
MAX_CONTENT_LENGTH_DIGITS = 10
SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


class _DuplicateJsonKey(ValueError):
    """标记任意层级重复 JSON key，不保存具体 key 或正文。"""


class _RejectedJsonNumber(ValueError):
    """标记浮点、指数或非标准常量，规则 wire 只接受整数。"""


@router.get("/error-maps", response_model=ProviderErrorMapListResponse)
async def get_provider_error_maps_route(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_error_map_reader)],
) -> ProviderErrorMapListResponse:
    """读取当前认证租户的规则、Provider 选项和配置 head。"""

    query = _read_unique_query(request, LIST_QUERY_FIELDS)
    result, etag = list_provider_error_maps(
        request.app.state.config,
        context,
        query=query,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _set_no_store(response)
    response.headers["ETag"] = etag
    return result


@router.post(
    "/error-maps",
    response_model=ProviderErrorMapMutationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_provider_error_map_route(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_error_map_writer)],
) -> ProviderErrorMapMutationResponse:
    """创建一条租户覆盖规则，并原子推进版本、head、事件与 Audit。"""

    operation_request_id = _raw_mutation_request_id(request)
    payload = await _read_json_model(
        request,
        CreateProviderErrorMapRequest,
        allowed_fields=CREATE_FIELDS,
        required_fields=CREATE_FIELDS,
    )
    result = create_provider_error_map(
        request.app.state.config,
        context,
        payload,
        audit_request_meta=build_audit_request_meta(request),
        request_id=operation_request_id,
        trace_id=_trace_id(request),
    )
    _set_no_store(response)
    response.headers["ETag"] = _head_etag(result)
    return result


@router.patch(
    "/error-maps/{error_map_id}",
    response_model=ProviderErrorMapMutationResponse,
)
async def patch_provider_error_map_route(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_error_map_writer)],
) -> ProviderErrorMapMutationResponse:
    """按稳定资源 ID 和 expected_version CAS 更新当前租户规则。"""

    operation_request_id = _raw_mutation_request_id(request)
    error_map_id = _raw_path_value(request, "error_map_id")
    payload = await _read_json_model(
        request,
        UpdateProviderErrorMapRequest,
        allowed_fields=PATCH_FIELDS,
        required_fields=frozenset({"expected_version"}),
    )
    if payload.model_fields_set <= {"expected_version"}:
        # 空 PATCH 没有可执行意图，但仍在 RBAC 与严格 wire 门禁之后返回统一领域错误。
        raise ApiError("error_map.update_empty")
    result = update_provider_error_map(
        request.app.state.config,
        context,
        error_map_id,
        payload,
        audit_request_meta=build_audit_request_meta(request),
        request_id=operation_request_id,
        trace_id=_trace_id(request),
    )
    _set_no_store(response)
    response.headers["ETag"] = _head_etag(result)
    return result


@internal_router.get("/internal/error-maps/{tenant_id}/current")
async def get_internal_provider_error_map_route(
    request: Request,
    _: Annotated[None, Depends(require_internal_service_token)],
) -> FastAPIResponse:
    """向已认证 Gateway 返回当前租户 active overlay，支持严格强 ETag 304。"""

    tenant_id = _raw_path_value(request, "tenant_id")
    # 内部快照不能混用用户 Bearer 身份，也不接受任何查询分支。
    if request.query_params.multi_items() or _raw_header_values(request, b"authorization"):
        raise ApiError("error_map.snapshot_unavailable")
    if_none_match = _single_optional_ascii_header(
        request,
        b"if-none-match",
        error_code="error_map.snapshot_unavailable",
        maximum=70,
    )
    return build_internal_provider_error_map_response(
        request.app.state.config,
        tenant_id,
        if_none_match=if_none_match,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


def _read_unique_query(request: Request, allowed: frozenset[str]) -> dict[str, str]:
    """读取唯一白名单 query，拒绝重复、未知、控制字符和 tenant_id。"""

    values: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        # query 只接受唯一白名单键且禁止控制字符，tenant 永远来自认证上下文而非参数。
        if (
            key not in allowed
            or key in values
            or _contains_control(key)
            or _contains_control(value)
        ):
            # 拒绝分支不回显 query 原值，避免控制字符或租户探测内容进入错误响应。
            raise ApiError("error_map.input_invalid")
        values[key] = value
    # 精确冲突重取与分页筛选互斥，避免目标资源再次被旧查询条件隐藏。
    if "error_map_id" in values and set(values) != {"error_map_id"}:
        # 精确冲突重取必须与筛选和分页互斥，避免目标资源被旧条件再次隐藏。
        raise ApiError("error_map.input_invalid")
    return values


async def _read_json_model(
    request: Request,
    model: type[CreateProviderErrorMapRequest] | type[UpdateProviderErrorMapRequest],
    *,
    allowed_fields: frozenset[str],
    required_fields: frozenset[str],
) -> CreateProviderErrorMapRequest | UpdateProviderErrorMapRequest:
    """有界读取 strict UTF-8 JSON object，并执行重复 key、深度和节点门禁。"""

    _require_json_content_type(request)
    declared_length = _content_length(request)
    # 已声明超限时在读取正文前拒绝，避免攻击者让控制面缓冲敏感 pattern 大正文。
    if declared_length is not None and declared_length > MAX_BODY_BYTES:
        raise ApiError("error_map.input_invalid")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            # 超限后立即停止消费，避免攻击者让控制面无界缓冲任意 pattern 正文。
            raise ApiError("error_map.input_invalid")
        body.extend(chunk)
    # 声明长度与实际流不一致说明请求被截断或拼接，不能继续解析部分规则。
    if declared_length is not None and declared_length != len(body):
        raise ApiError("error_map.input_invalid")
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
        # 解码与 JSON 异常可能携带 pattern 原文，只返回固定输入错误且不回显底层信息。
        raise ApiError("error_map.input_invalid") from None
    if (
        type(parsed) is not dict
        or not required_fields.issubset(parsed)
        or not set(parsed).issubset(allowed_fields)
    ):
        # 顶层必须满足 endpoint 的 exact 字段合同，未知字段不能静默进入未来业务语义。
        raise ApiError("error_map.input_invalid")
    _validate_json_shape(parsed)
    try:
        return model.model_validate(parsed, strict=True)
    # Pydantic 错误可能携带 pattern 或原始 input，公开层只能返回固定领域错误码。
    except ValidationError:
        # Pydantic 错误可能携带 pattern 或原始 input，公开层只能返回固定领域错误码。
        raise ApiError("error_map.input_invalid") from None


def _raw_mutation_request_id(request: Request) -> str:
    """恢复未清洗 X-Request-ID，并验证它与中间件可见值逐字一致。"""

    values = _raw_header_values(request, b"x-request-id")
    state_value = _request_id(request)
    if not values:
        # 缺省头只能使用中间件生成且符合安全字符集的 request_id。
        if SAFE_REQUEST_ID.fullmatch(state_value) is None:
            raise ApiError("error_map.input_invalid")
        return state_value
    # mutation 幂等键禁止重复 Header，代理拼接值不能被任选一个继续处理。
    if len(values) != 1:
        raise ApiError("error_map.input_invalid")
    try:
        value = values[0].decode("ascii", errors="strict")
    # 非 ASCII 链路标识可能造成日志或存储层解释差异，必须在业务写入前拒绝。
    except UnicodeDecodeError:
        raise ApiError("error_map.input_invalid") from None
    # raw 与中间件状态必须逐字一致，防止替换、截断或代理规范化导致幂等碰撞。
    if SAFE_REQUEST_ID.fullmatch(value) is None or value != state_value:
        # strip、替换、截断、凭证碰撞和代理拼接都会导致 raw/state 不一致，必须失败关闭。
        raise ApiError("error_map.input_invalid")
    return value


def _require_json_content_type(request: Request) -> None:
    """从原始 Header 要求唯一 application/json 与可选唯一 UTF-8 charset，拒绝代理解释分歧。"""

    values = _raw_header_values(request, b"content-type")
    # Content-Type 必须唯一，重复值不能由应用任选一个解释。
    if len(values) != 1:
        raise ApiError("error_map.input_invalid")
    try:
        text = values[0].decode("ascii", errors="strict")
    # 媒体类型合同仅使用 ASCII，异常字节不进入错误正文与日志。
    except UnicodeDecodeError:
        raise ApiError("error_map.input_invalid") from None
    parts = [part.strip() for part in text.split(";")]
    # 规则 mutation 只接受 JSON，form/text 等类型不能触发正文解析。
    if not parts or parts[0].lower() != "application/json":
        raise ApiError("error_map.input_invalid")
    seen_charset = False
    for parameter in parts[1:]:
        # 参数必须是完整 name=value，空片段与无等号形式均存在代理解释差异。
        if not parameter or "=" not in parameter:
            raise ApiError("error_map.input_invalid")
        name, value = (part.strip() for part in parameter.split("=", 1))
        # 只允许一次 UTF-8 charset，其他编码或重复声明会破坏字节级幂等指纹。
        if name.lower() != "charset" or seen_charset or value.lower() not in {"utf-8", '"utf-8"'}:
            raise ApiError("error_map.input_invalid")
        seen_charset = True


def _content_length(request: Request) -> int | None:
    """严格读取唯一 Content-Length；缺失时由流式累计上限负责。"""

    values = _raw_header_values(request, b"content-length")
    if not values:
        return None
    # 重复 Content-Length 在代理间解释不一，不能选择任意值继续读取。
    if len(values) != 1:
        raise ApiError("error_map.input_invalid")
    try:
        text = values[0].decode("ascii", errors="strict")
    # 长度 Header 仅允许 ASCII 十进制，异常字节不进入整数解析或错误回显。
    except UnicodeDecodeError:
        raise ApiError("error_map.input_invalid") from None
    # 先限制文本长度再转整数，避免超长十进制 Header 触发大整数解析放大。
    if not text or len(text) > MAX_CONTENT_LENGTH_DIGITS or not text.isdecimal():
        raise ApiError("error_map.input_invalid")
    return int(text)


def _validate_json_shape(value: object) -> None:
    """以迭代方式限制 JSON 深度和节点数，避免递归或宽对象放大 CPU。"""

    nodes = 0
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        # 节点和深度双上限阻断宽对象及深层容器放大 CPU/内存。
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise ApiError("error_map.input_invalid")
        if type(current) is dict:
            stack.extend((item, depth + 1) for item in current.values())
        elif type(current) is list:
            stack.extend((item, depth + 1) for item in current)


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """构造 JSON object；任意层级重复 key 均失败关闭。"""

    result: dict[str, object] = {}
    for key, value in pairs:
        # 任意层重复键可能让校验器和执行器选择不同值，必须拒绝完整请求。
        if key in result:
            raise _DuplicateJsonKey()
        result[key] = value
    return result


def _reject_number(_: str) -> Any:
    """拒绝 float、指数、NaN 和 Infinity，避免跨运行时数值解释漂移。"""

    raise _RejectedJsonNumber()


def _raw_path_value(request: Request, name: str) -> str:
    """在认证依赖通过后读取未经 typed converter 处理的 path 参数。"""

    value = request.path_params.get(name)
    # path 值必须是非空无控制字符字符串，租户和资源 ID 的细化校验由服务层继续执行。
    if type(value) is not str or not value or _contains_control(value):
        raise ApiError("error_map.input_invalid")
    return value


def _single_optional_ascii_header(
    request: Request,
    name: bytes,
    *,
    error_code: str,
    maximum: int,
) -> str | None:
    """读取一个可空原始 ASCII Header，拒绝重复、控制字符和超长值。"""

    values = _raw_header_values(request, name)
    if not values:
        return None
    # 可选 Header 一旦出现也必须唯一，重复值不能由内部接口猜测合并规则。
    if len(values) != 1:
        raise ApiError(error_code)
    try:
        value = values[0].decode("ascii", errors="strict")
    # 内部条件 Header 必须是 ASCII，坏字节统一为调用方指定的固定错误码。
    except UnicodeDecodeError:
        raise ApiError(error_code) from None
    # 空值、超长、控制字符和逗号合并形式均可能造成代理与应用解释分歧。
    if not value or len(value) > maximum or _contains_control(value) or "," in value:
        raise ApiError(error_code)
    return value


def _raw_header_values(request: Request, name: bytes) -> list[bytes]:
    """从 ASGI scope 逐项读取 Header，保留重复项和原始字节。"""

    return [value for key, value in request.scope.get("headers", ()) if key.lower() == name]


def _contains_control(value: str) -> bool:
    """判断文本是否含 C0/C1 控制字符或 DEL。"""

    return any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value)


def _set_no_store(response: Response) -> None:
    """禁止浏览器、代理或共享缓存保存规则 pattern 与管理状态。"""

    response.headers["Cache-Control"] = "no-store"


def _head_etag(result: ProviderErrorMapMutationResponse) -> str:
    """使用服务层返回的内容 hash 作为写响应的可读强 ETag。"""

    return f'"{result.content_hash}"'


def _request_id(request: Request) -> str:
    """读取统一中间件确定的 request_id，供响应、操作账本与 Audit 共享同一链路标识。"""

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """读取统一中间件确定的 trace_id，确保 Source Outbox 与 Audit 可关联本次请求链路。"""

    return getattr(request.state, "trace_id", "")


__all__ = ["internal_router", "router"]
