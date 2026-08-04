"""模型质量评分 HTTP 路由、严格查询和有界 JSON wire 解析。

PATCH 刻意不声明 Pydantic Body：FastAPI 必须先执行 admin 权限依赖，再读取不可信正文，
避免未授权调用者利用大正文、重复键或解析差异消耗资源。tenant_id 始终来自认证上下文。
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from metahub_shared.runtime.errors import ApiError

from metahub_control.audit import build_audit_request_meta
from metahub_control.auth import (
    AuthContext,
    require_model_quality_score_reader,
    require_model_quality_score_writer,
)

from .models import ModelQualityScoreListResponse, ModelQualityScoreMutationResponse
from .service import list_model_quality_scores, update_manual_quality_score

router = APIRouter(prefix="/model-quality-scores", tags=["model-quality-scores"])

# 人工评分只需要三个标量；8 KiB、16 层和 64 节点同时限制正文、深度和宽度。
MAX_SCORE_BODY_BYTES = 8 * 1024
MAX_SCORE_JSON_DEPTH = 16
MAX_SCORE_JSON_NODES = 64
ALLOWED_QUERY_FIELDS = frozenset({"model_id", "limit", "offset"})


class _DuplicateJsonKey(ValueError):
    """标记任意层级重复 JSON key，防止后值覆盖已经检查的安全意图。"""


@router.get("", response_model=ModelQualityScoreListResponse)
async def get_model_quality_scores(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_model_quality_score_reader)],
) -> ModelQualityScoreListResponse:
    """查询当前认证租户的模型质量统计，viewer 由后端依赖拒绝。"""

    model_id, limit, offset = _read_score_query(request)
    # 评分和路由先验会随调用/管理员更新，浏览器或代理不得缓存旧治理事实。
    response.headers["Cache-Control"] = "no-store"
    return list_model_quality_scores(
        request.app.state.config,
        context,
        model_id=model_id,
        limit=limit,
        offset=offset,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.patch("", response_model=ModelQualityScoreMutationResponse)
async def patch_model_quality_score(
    request: Request,
    context: Annotated[AuthContext, Depends(require_model_quality_score_writer)],
) -> ModelQualityScoreMutationResponse:
    """认证后读取严格小 JSON，并以独立人工版本 CAS 修改评分。"""

    payload = await _read_score_payload(request)
    return update_manual_quality_score(
        request.app.state.config,
        context,
        payload,
        audit_request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


def _read_score_query(request: Request) -> tuple[str | None, int, int]:
    """严格解析白名单查询参数，拒绝未知键、重复键和客户端 tenant_id。"""

    pairs = list(request.query_params.multi_items())
    seen: set[str] = set()
    values: dict[str, str] = {}
    for key, value in pairs:
        # 重复参数在代理/框架间可能采用 first/last 不同语义，必须整体拒绝。
        if key not in ALLOWED_QUERY_FIELDS or key in seen:
            raise ApiError("model_quality_score.query_invalid")
        seen.add(key)
        values[key] = value
    model_id = values.get("model_id")
    # 模型标识必须按原样校验，避免裁剪后探测其他租户资源；非法值统一返回查询错误。
    if model_id is not None and not _safe_identifier(model_id):
        raise ApiError("model_quality_score.query_invalid")
    limit = _query_integer(values.get("limit"), default=100, minimum=1, maximum=200)
    offset = _query_integer(
        values.get("offset"),
        default=0,
        minimum=0,
        maximum=1_000_000,
    )
    return model_id, limit, offset


def _query_integer(value: str | None, *, default: int, minimum: int, maximum: int) -> int:
    """解析无符号十进制查询整数，不接受正号、空格、指数或 Unicode 数字。"""

    if value is None:
        return default
    # API wire 固定 ASCII 十进制，避免全角/其他 Unicode digit 在不同组件中归一化不同。
    if not value or any(character < "0" or character > "9" for character in value):
        raise ApiError("model_quality_score.query_invalid")
    parsed = int(value)
    # 分页边界限制单次租户查询的扫描成本，防止极端参数耗尽控制面数据库资源。
    if parsed < minimum or parsed > maximum:
        raise ApiError("model_quality_score.query_invalid")
    return parsed


async def _read_score_payload(request: Request) -> dict[str, Any]:
    """有界读取 UTF-8 JSON object，拒绝重复键、控制字符和资源越界。"""

    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    # 权限校验已在依赖层完成，此处固定媒体类型以消除网关与框架之间的正文解析歧义。
    if content_type != "application/json":
        raise ApiError("model_quality_score.input_invalid")
    declared = request.headers.get("content-length")
    if declared:
        try:
            declared_length = int(declared)
        # 声明长度不是可信边界，格式非法时统一折叠为输入错误且不回显原始请求头。
        except ValueError as exc:
            raise ApiError("model_quality_score.input_invalid") from exc
        # 先校验声明长度可以及早拒绝空正文或超限正文，但后续仍会检查实际流长度。
        if declared_length < 1 or declared_length > MAX_SCORE_BODY_BYTES:
            raise ApiError("model_quality_score.input_invalid")
    body = bytearray()
    async for chunk in request.stream():
        # 代理可能缺失或伪造 Content-Length，因此必须对实际流累计值再次执行硬上限。
        if len(body) + len(chunk) > MAX_SCORE_BODY_BYTES:
            raise ApiError("model_quality_score.input_invalid")
        body.extend(chunk)
    # 空正文不具有合法 PATCH 语义，统一进入公开输入错误而不是交给服务层猜测默认值。
    if not body:
        raise ApiError("model_quality_score.input_invalid")
    try:
        parsed = json.loads(
            bytes(body).decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    # 编码、重复键、递归深度等解析异常不得携带正文或底层异常信息返回给调用方。
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ApiError("model_quality_score.input_invalid") from exc
    # 根对象和深宽约束在进入服务层前失败关闭，避免未知结构消耗资源或绕过字段白名单。
    if not isinstance(parsed, dict) or _has_unsafe_json_structure(parsed):
        raise ApiError("model_quality_score.input_invalid")
    return parsed


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """把 pairs 转为字典，并在任意对象层级发现重复 key 时中止。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _has_unsafe_json_structure(value: object) -> bool:
    """迭代限制 JSON 深度/节点并拒绝字符串控制字符，避免递归资源攻击。"""

    stack: list[tuple[object, int]] = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if depth > MAX_SCORE_JSON_DEPTH or nodes > MAX_SCORE_JSON_NODES:
            return True
        if isinstance(current, str):
            if any(ord(character) < 32 for character in current):
                return True
            continue
        if isinstance(current, dict):
            for key, child in reversed(list(current.items())):
                stack.append((child, depth + 1))
                stack.append((key, depth + 1))
            continue
        if isinstance(current, list):
            for child in reversed(current):
                stack.append((child, depth + 1))
    return False


def _safe_identifier(value: object) -> bool:
    """验证模型 ID 为有界、无空白/控制字符的原样文本。"""

    return (
        type(value) is str
        and 1 <= len(value) <= 128
        and value == value.strip()
        and not any(character.isspace() or ord(character) < 32 for character in value)
    )


def _request_id(request: Request) -> str:
    """读取统一 request_id，供响应、幂等、审计和事件共同关联。"""

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """读取统一 trace_id，供事务提交后的 Webhook 与日志关联。"""

    return getattr(request.state, "trace_id", "")
