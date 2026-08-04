"""PRD-053 成本异常 API 路由与严格 raw wire 解析。

认证和 admin RBAC 依赖先于 query、header 和 body 解析执行。这样 anonymous、
developer 和 viewer 即使提交重复 Header、畸形 JSON 或超限正文，也只得到统一
401/403，不能利用解析差异探测财务治理合同。
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import ValidationError

from metahub_control.audit import build_audit_request_meta
from metahub_control.auth import (
    AuthContext,
    require_cost_anomaly_reader,
    require_cost_anomaly_writer,
)
from metahub_shared.runtime.errors import ApiError

from .models import (
    CostAnomalyActionRequest,
    CostAnomalyActionResponse,
    CostAnomalyListResponse,
)
from .service import apply_cost_anomaly_action, list_cost_anomalies

router = APIRouter(tags=["cost-anomalies"])

# 查询白名单、动作字段、幂等格式和正文复杂度共同限定公开 wire 的可接受边界。
ALLOWED_LIST_QUERY_FIELDS = frozenset(
    {
        "status",
        "rule_type",
        "severity",
        "environment_id",
        "virtual_key_id",
        "from_time",
        "to_time",
        "limit",
        "cursor",
    }
)
ACTION_BODY_FIELDS = frozenset(
    {
        "action",
        "dismiss_reason",
        "duration_minutes",
        "rpm_limit",
        "tpm_limit",
        "concurrency_limit",
    }
)
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
MAX_ACTION_BODY_BYTES = 8 * 1024
MAX_JSON_DEPTH = 8
MAX_JSON_NODES = 64
ACTION_OPENAPI = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {
                "schema": CostAnomalyActionRequest.model_json_schema(),
            }
        },
    },
    "parameters": [
        {
            "name": "Idempotency-Key",
            "in": "header",
            "required": True,
            "schema": {"type": "string", "minLength": 8, "maxLength": 128},
        }
    ],
}


class _DuplicateJsonKey(ValueError):
    """标记任意层级重复 JSON key，避免 first/last 解析器语义分裂。"""


class _RejectedJsonNumber(ValueError):
    """标记 float、指数和非有限数字；限流合同只接受 JSON integer。"""


@router.get("/cost-anomalies", response_model=CostAnomalyListResponse)
async def get_cost_anomalies_route(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_cost_anomaly_reader)],
) -> CostAnomalyListResponse:
    """读取当前管理员租户的成本异常、检测器汇总和 snapshot cursor。"""

    query = _read_query(request, ALLOWED_LIST_QUERY_FIELDS)
    response.headers["Cache-Control"] = "no-store"
    # 服务层仍按认证 tenant 构造查询，路由不会接受任何客户端自报租户。
    return list_cost_anomalies(
        request.app.state.config,
        context,
        status=query.get("status"),
        rule_type=query.get("rule_type"),
        severity=query.get("severity"),
        environment_id=query.get("environment_id"),
        virtual_key_id=query.get("virtual_key_id"),
        from_time=query.get("from_time"),
        to_time=query.get("to_time"),
        limit=query.get("limit"),
        cursor=query.get("cursor"),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.patch(
    "/cost-anomalies/{cost_anomaly_id}/action",
    response_model=CostAnomalyActionResponse,
    openapi_extra=ACTION_OPENAPI,
)
async def patch_cost_anomaly_action_route(
    cost_anomaly_id: str,
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_cost_anomaly_writer)],
) -> CostAnomalyActionResponse:
    """按严格幂等 wire 合同处置一条当前租户异常。"""

    # PATCH 不接受 query，避免相同 path/body 因未签名查询参数产生另一种意图。
    if request.query_params.multi_items():
        raise ApiError("cost_anomaly.action_invalid")
    content_type = _read_content_type(request)
    if content_type != "application/json":
        # 即使解析器未来扩展，成本治理写接口仍只接受明确的 JSON 媒体类型。
        raise ApiError("cost_anomaly.media_type_invalid")
    idempotency_key = _read_idempotency_key(request)
    payload = await _read_action_payload(request)
    response.headers["Cache-Control"] = "no-store"
    # 动作服务只接收认证上下文中的 tenant 与 actor，并负责事务级二次权限边界。
    return apply_cost_anomaly_action(
        request.app.state.config,
        context,
        cost_anomaly_id,
        payload,
        idempotency_key=idempotency_key,
        audit_request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


def _read_query(request: Request, allowed_fields: frozenset[str]) -> dict[str, str]:
    """读取唯一白名单 query，拒绝重复、未知字段和客户端 tenant_id。"""

    seen: set[str] = set()
    values: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key not in allowed_fields or key in seen:
            # 未知或重复参数可能改变代理与框架解释，必须按同一查询错误拒绝。
            raise ApiError("cost_anomaly.query_invalid")
        if _contains_control(key) or _contains_control(value):
            # 控制字符会污染日志和签名边界，不能进入筛选或 cursor 绑定摘要。
            raise ApiError("cost_anomaly.query_invalid")
        seen.add(key)
        values[key] = value
    return values


def _read_content_type(request: Request) -> str:
    """从 ASGI 原始请求头读取唯一 JSON 媒体类型，并严格限定为 UTF-8 字符集。"""

    values = _raw_header_values(request, b"content-type")
    if len(values) != 1:
        # 缺失或重复 Content-Type 在不同代理上语义不一致，必须统一拒绝。
        raise ApiError("cost_anomaly.media_type_invalid")
    try:
        raw = values[0].decode("ascii")
    # 媒体类型语法限定为 ASCII，解码异常不能作为底层错误穿透统一响应。
    except UnicodeDecodeError as exc:
        raise ApiError("cost_anomaly.media_type_invalid") from exc
    parts = [part.strip() for part in raw.split(";")]
    if not parts or parts[0].lower() != "application/json":
        # 写动作拒绝通配符、别名和非 JSON 类型，避免正文解释出现多套合同。
        raise ApiError("cost_anomaly.media_type_invalid")
    seen_charset = False
    for parameter in parts[1:]:
        if not parameter or "=" not in parameter:
            # 每个媒体参数必须是完整键值对，空段和裸参数都视为含糊输入。
            raise ApiError("cost_anomaly.media_type_invalid")
        name, value = (item.strip() for item in parameter.split("=", 1))
        if name.lower() != "charset" or seen_charset:
            # 仅允许出现一次 charset，拒绝扩展参数及重复参数的覆盖语义。
            raise ApiError("cost_anomaly.media_type_invalid")
        normalized = value.lower()
        if normalized not in {"utf-8", '"utf-8"'}:
            # 只接受裸值或完整成对 quoted-string；strip 引号会把缺失一侧的畸形语法
            # 错当成 UTF-8，从而让代理与服务端对正文边界产生不同解释。
            raise ApiError("cost_anomaly.media_type_invalid")
        seen_charset = True
    return "application/json"


def _read_idempotency_key(request: Request) -> str:
    """读取唯一 ASCII 幂等键；原始值只驻留请求内存，不写日志或数据库。"""

    values = _raw_header_values(request, b"idempotency-key")
    if len(values) != 1 or b"," in values[0]:
        # 幂等键必须是单一原始头，拒绝代理合并和多值覆盖造成的意图错配。
        raise ApiError("cost_anomaly.action_invalid")
    try:
        value = values[0].decode("ascii")
    # 键仅允许受限 ASCII，解码失败统一映射且绝不回显原始敏感值。
    except UnicodeDecodeError as exc:
        raise ApiError("cost_anomaly.action_invalid") from exc
    if not IDEMPOTENCY_KEY_PATTERN.fullmatch(value):
        # 长度或字符集不符合约束时拒绝，避免控制字符进入摘要与诊断链路。
        raise ApiError("cost_anomaly.action_invalid")
    return value


async def _read_action_payload(request: Request) -> CostAnomalyActionRequest:
    """有界流式读取严格 UTF-8 JSON object，并转换为 extra-forbid DTO。"""

    expected_length = _content_length(request)
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_ACTION_BODY_BYTES:
            # 流式累计在内存扩张前执行硬上限，Content-Length 不能替代实际字节校验。
            raise ApiError("cost_anomaly.action_invalid")
        chunks.append(chunk)
    if expected_length is not None and expected_length != size:
        # 声明长度与实际字节不一致可能表示截断或请求走私，必须失败关闭。
        raise ApiError("cost_anomaly.action_invalid")
    if size == 0:
        # 空正文无法表达明确治理意图，不能让模型默认值代替客户端选择。
        raise ApiError("cost_anomaly.action_invalid")
    try:
        text = b"".join(chunks).decode("utf-8", errors="strict")
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_float=_reject_json_number,
            parse_constant=_reject_json_number,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateJsonKey,
        _RejectedJsonNumber,
        RecursionError,
        ValueError,
    # 编码、重复键、数值和结构错误统一折叠，响应不得带回原始敏感正文。
    ) as exc:
        raise ApiError("cost_anomaly.action_invalid") from exc
    if not isinstance(parsed, dict) or not set(parsed).issubset(ACTION_BODY_FIELDS):
        # 顶层必须是白名单对象，数组和未知字段都不能进入幂等意图摘要。
        raise ApiError("cost_anomaly.action_invalid")
    _validate_json_tree(parsed)
    try:
        return CostAnomalyActionRequest.model_validate(parsed)
    # DTO 校验异常可能含原始值，只转换为统一动作错误而不向日志传播正文。
    except (ValidationError, TypeError, ValueError) as exc:
        raise ApiError("cost_anomaly.action_invalid") from exc


def _content_length(request: Request) -> int | None:
    """验证最多一个 canonical Content-Length，并在读取正文前拒绝越界。"""

    values = _raw_header_values(request, b"content-length")
    if len(values) > 1:
        # 重复长度头在代理链中可能产生请求走私歧义，因此不采用任一值。
        raise ApiError("cost_anomaly.action_invalid")
    if not values:
        return None
    try:
        raw = values[0].decode("ascii")
    # Content-Length 必须是 ASCII 十进制，解码失败统一视为非法动作。
    except UnicodeDecodeError as exc:
        raise ApiError("cost_anomaly.action_invalid") from exc
    if not raw.isdigit() or (len(raw) > 1 and raw.startswith("0")):
        # 只接受 canonical 非负十进制表示，拒绝符号、空白和前导零替代形式。
        raise ApiError("cost_anomaly.action_invalid")
    length = int(raw)
    if length > MAX_ACTION_BODY_BYTES:
        # 声明长度已越界时在读取请求体前拒绝，减少不必要的内存占用。
        raise ApiError("cost_anomaly.action_invalid")
    return length


def _raw_header_values(request: Request, name: bytes) -> list[bytes]:
    """直接读取 ASGI header 列表，保留重复项供安全校验。"""

    return [
        value
        for key, value in request.scope.get("headers", [])
        if key.lower() == name
    ]


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """拒绝任意层级重复 key，而不是接受解析器的 last-value-wins。"""

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _reject_json_number(value: str) -> object:
    """拒绝 float、指数、NaN 和 Infinity，不回显原始数字文本。"""

    raise _RejectedJsonNumber("non-integer JSON number")


def _validate_json_tree(value: object) -> None:
    """限制 JSON 深度、节点数和控制字符，避免异常正文扩大内存与审计面。"""

    nodes = 0

    def visit(item: object, depth: int) -> None:
        """递归计数已解析节点，并在进入子节点前执行深度与字符安全校验。"""

        nonlocal nodes
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            # 节点或深度越界会放大解析成本，必须在继续递归前立即终止。
            raise ApiError("cost_anomaly.action_invalid")
        if isinstance(item, dict):
            for key, child in item.items():
                if _contains_control(key):
                    # 对象键中的控制字符会污染审计和哈希语义，不能继续规范化。
                    raise ApiError("cost_anomaly.action_invalid")
                visit(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
        elif isinstance(item, str) and _contains_control(item):
            # 字符串值同样禁止控制字符，避免日志注入和不可见意图差异。
            raise ApiError("cost_anomaly.action_invalid")

    visit(value, 1)


def _contains_control(value: str) -> bool:
    """判断字符串是否含 Unicode 控制字符。"""

    return any(unicodedata.category(character).startswith("C") for character in value)


def _request_id(request: Request) -> str:
    """读取统一中间件生成的 request_id，供响应正文、响应头和审计记录保持一致。"""

    return str(getattr(request.state, "request_id", ""))


def _trace_id(request: Request) -> str:
    """读取统一中间件生成的 trace_id，确保处置、审计和事件投递可以关联追踪。"""

    return str(getattr(request.state, "trace_id", ""))


__all__ = ["router"]