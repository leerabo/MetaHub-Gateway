"""FinOps HTTP 路由、后端 RBAC 与严格 wire 解析。

GET/POST 都先执行管理员认证依赖，再解析查询或正文。这样 developer/viewer 即使发送
超限正文或畸形参数，也只能得到稳定权限错误，不能利用校验差异探测财务接口合同。
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from metahub_shared.runtime.errors import ApiError

from metahub_control.audit import build_audit_request_meta
from metahub_control.auth import AuthContext, require_finops_reader, require_finops_writer

from .models import (
    FinOpsAdjustmentRequest,
    FinOpsAdjustmentResponse,
    FinOpsLedgerListResponse,
)
from .service import create_finops_adjustment, list_finops_ledger

router = APIRouter(prefix="/finops", tags=["finops"])

# 查询和正文均采用明确白名单与硬上限，避免未知字段扩大 SQL、审计或内存消耗面。
ALLOWED_LEDGER_QUERY_FIELDS = frozenset(
    {
        "start_date",
        "end_date",
        "entry_type",
        "direction",
        "quota_type",
        "request_id",
        "price_snapshot_id",
        "related_entry_id",
        "limit",
        "cursor",
    }
)
MAX_ADJUSTMENT_BODY_BYTES = 8 * 1024
MAX_JSON_DEPTH = 8
MAX_JSON_NODES = 64
FINOPS_ADJUSTMENT_REQUEST_OPENAPI = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {
                "schema": FinOpsAdjustmentRequest.model_json_schema(),
            }
        },
    }
}


class _DuplicateJsonKey(ValueError):
    """标记任意层级重复 JSON key，防止不同解析器采用 first/last 不同语义。"""


@router.get("/ledger", response_model=FinOpsLedgerListResponse)
async def get_finops_ledger(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_finops_reader)],
) -> FinOpsLedgerListResponse:
    """按当前管理员租户读取不可变正式账本和同快照汇总。"""

    query, limit, cursor = _read_ledger_query(request)
    # 财务分录持续追加，浏览器和中间代理不得缓存旧快照或旧权限结果。
    response.headers["Cache-Control"] = "no-store"
    return list_finops_ledger(
        request.app.state.config,
        context,
        query=query,
        limit=limit,
        cursor=cursor,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post(
    "/adjustments",
    response_model=FinOpsAdjustmentResponse,
    status_code=201,
    openapi_extra=FINOPS_ADJUSTMENT_REQUEST_OPENAPI,
)
async def post_finops_adjustment(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_finops_writer)],
) -> FinOpsAdjustmentResponse:
    """创建人工调整或按租户幂等键恢复既有提交结果。"""

    idempotency_key = _read_idempotency_key(request)
    payload = await _read_adjustment_payload(request)
    result = create_finops_adjustment(
        request.app.state.config,
        context,
        payload,
        idempotency_key=idempotency_key,
        audit_request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    # 首次提交用 201；完全相同的幂等恢复没有创建新资源，固定返回 200。
    if result.idempotent_replay:
        response.status_code = 200
    response.headers["Cache-Control"] = "no-store"
    return result


def _read_ledger_query(request: Request) -> tuple[dict[str, str | None], int, str | None]:
    """严格读取白名单查询，拒绝未知、重复、空白和客户端 tenant_id。"""

    seen: set[str] = set()
    values: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        # 未知或重复参数会造成解析歧义，也不得让客户端注入租户筛选条件。
        if key not in ALLOWED_LEDGER_QUERY_FIELDS or key in seen:
            raise ApiError("finops.query_invalid")
        seen.add(key)
        values[key] = value
    limit = _query_integer(values.pop("limit", None), default=20, minimum=1, maximum=100)
    cursor = values.pop("cursor", None)
    if cursor is not None and (
        not cursor
        or len(cursor) > 2048
        or cursor != cursor.strip()
        or any(ord(character) < 33 or ord(character) > 126 for character in cursor)
    ):
        # 游标参与签名校验，只接受有界可见 ASCII，避免规范化差异绕过筛选绑定。
        raise ApiError("finops.query_invalid")
    # 业务字段的枚举、日期和资源标识仍由服务层统一归一，避免路由与内部调用产生双合同。
    query = {field: values.get(field) for field in ALLOWED_LEDGER_QUERY_FIELDS - {"limit", "cursor"}}
    return query, limit, cursor


def _query_integer(value: str | None, *, default: int, minimum: int, maximum: int) -> int:
    """解析 ASCII 无符号整数，不接受符号、空白、指数或 Unicode 数字。"""

    if value is None:
        return default
    # 数字查询参数只接受规范 ASCII 表示，避免不同解析器对符号或 Unicode 的理解不一致。
    if not value or any(character < "0" or character > "9" for character in value):
        raise ApiError("finops.query_invalid")
    parsed = int(value)
    # 上下界限制查询成本；越界值统一按无效查询处理，不能静默截断。
    if parsed < minimum or parsed > maximum:
        raise ApiError("finops.query_invalid")
    return parsed


def _read_idempotency_key(request: Request) -> str:
    """读取唯一可见 ASCII 幂等键，禁止代理合并重复 Header 后产生歧义。"""

    raw_values = [
        value.decode("latin-1")
        for key, value in request.scope.get("headers", [])
        if key.lower() == b"idempotency-key"
    ]
    # 幂等键必须恰好出现一次，防止代理合并重复头后改变去重语义。
    if len(raw_values) != 1:
        raise ApiError("finops.adjustment_invalid")
    value = raw_values[0]
    # 有界可见 ASCII 保证存储与比较语义稳定，也避免控制字符污染审计字段。
    if not 8 <= len(value) <= 128 or any(ord(character) < 33 or ord(character) > 126 for character in value):
        raise ApiError("finops.adjustment_invalid")
    return value


async def _read_adjustment_payload(request: Request) -> dict[str, Any]:
    """有界读取严格 UTF-8 JSON object，并拒绝重复键和递归资源攻击。"""

    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    # 财务调整只接受明确 JSON 媒体类型，避免表单或文本被中间件按不同规则解析。
    if content_type != "application/json":
        raise ApiError("finops.adjustment_invalid")
    declared = request.headers.get("content-length")
    if declared is not None:
        # Content-Length 必须可严格转为十进制整数；畸形值不能绕过正文上限。
        try:
            declared_length = int(declared)
        except ValueError as exc:
            # 对外收敛为统一业务错误，避免暴露底层整数解析器的异常细节。
            raise ApiError("finops.adjustment_invalid") from exc
        # 声明长度同样执行硬上限，尽早拒绝不可能合法的财务调整正文。
        if declared_length < 1 or declared_length > MAX_ADJUSTMENT_BODY_BYTES:
            raise ApiError("finops.adjustment_invalid")
    body = bytearray()
    async for chunk in request.stream():
        # 即使声明长度合法，也按实际流量限额，防止分块传输绕过内存保护。
        if len(body) + len(chunk) > MAX_ADJUSTMENT_BODY_BYTES:
            raise ApiError("finops.adjustment_invalid")
        body.extend(chunk)
    # 空正文无法表达合法调整，必须失败关闭而不能补入隐式默认字段。
    if not body:
        raise ApiError("finops.adjustment_invalid")
    try:
        parsed = json.loads(
            bytes(body).decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    # 编码、重复键或递归异常属于同一外部合同，不向客户端暴露解析器差异。
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ApiError("finops.adjustment_invalid") from exc
    # 顶层必须为对象且结构受限，避免列表合同歧义及深度或节点资源攻击。
    if not isinstance(parsed, dict) or _has_unsafe_json_structure(parsed):
        raise ApiError("finops.adjustment_invalid")
    return parsed


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """构造 JSON object，并在任意层发现重复字段时失败关闭。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _has_unsafe_json_structure(value: object) -> bool:
    """迭代限制 JSON 深度、节点数和字符串控制字符。"""

    stack: list[tuple[object, int]] = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if depth > MAX_JSON_DEPTH or nodes > MAX_JSON_NODES:
            return True
        if isinstance(current, str):
            if any(ord(character) < 32 for character in current):
                return True
            continue
        if isinstance(current, dict):
            for key, child in current.items():
                stack.append((key, depth + 1))
                stack.append((child, depth + 1))
            continue
        if isinstance(current, list):
            for child in current:
                stack.append((child, depth + 1))
    return False


def _request_id(request: Request) -> str:
    """读取统一中间件生成或透传的 request_id。"""

    return str(getattr(request.state, "request_id", ""))


def _trace_id(request: Request) -> str:
    """读取统一中间件生成或透传的 trace_id。"""

    return str(getattr(request.state, "trace_id", ""))


__all__ = ["router"]
