"""PRD-064 Adapter 合同内部上报与当前租户只读 HTTP 路由。

内部身份依赖必须先于正文读取执行；公开入口必须先恢复 Session 并实时校验
``provider.health.read``。路由手工处理 query 和 JSON，确保重复字段、未知字段、非有限数字、
过大正文以及 tenant 注入都使用统一领域错误而不是框架自由文本。
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from metahub_shared.runtime.errors import ApiError
from pydantic import ValidationError

from metahub_control.auth import (
    AuthContext,
    require_internal_service_token,
    require_provider_health_reader,
)

from .models import (
    AdapterContractRunDetailResponse,
    AdapterContractRunIngestResponse,
    AdapterContractRunListResponse,
    AdapterContractRunReport,
)
from .service import (
    get_adapter_contract_run,
    ingest_adapter_contract_run,
    list_adapter_contract_runs,
)

router = APIRouter(tags=["adapter-contracts"])
internal_router = APIRouter(tags=["adapter-contracts-internal"])

# 512 KiB/2048 cases 是设计冻结上限；节点和深度还要独立限制，避免小字节深嵌套消耗递归资源。
MAX_REPORT_BODY_BYTES = 512 * 1024
MAX_JSON_DEPTH = 24
MAX_JSON_NODES = 20_000
_LIST_QUERY_FIELDS = frozenset({"status", "suite_kind", "mode", "limit", "cursor"})


class _DuplicateJsonKey(ValueError):
    """标记任意层重复 JSON key；异常不保存 key 或正文，避免诊断链泄露输入。"""


def _require_internal_report_access(
    request: Request,
    internal_token: Annotated[str | None, Header(alias="X-Internal-Token")] = None,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> None:
    """要求唯一内部 Token，并拒绝 Bearer 混用和重复敏感 Header。"""

    raw_tokens = [
        value
        for name, value in request.headers.raw
        if name.lower() == b"x-internal-token"
    ]
    # 内部上报没有身份时必须在读取正文前终止，避免未认证请求消耗 JSON 解析资源。
    if not raw_tokens:
        # 缺失身份必须在路由读取正文前返回 401，超大或畸形 body 不会先消耗解析资源。
        raise ApiError("auth.internal_token_required")
    if len(raw_tokens) != 1:
        # 多个 Token 的代理合并语义不一致，不能让任意一个值通过常量时间比较。
        raise ApiError("auth.internal_token_invalid")
    require_internal_service_token(request, internal_token)
    if authorization is not None:
        # 内部 runner 与用户 Session 是两条互斥身份链，不允许请求挑选更宽松的一条。
        raise ApiError("auth.internal_token_invalid")


@internal_router.post(
    "/internal/tenants/{tenant_id}/adapter-contract-runs",
    response_model=AdapterContractRunIngestResponse,
)
async def post_adapter_contract_run(
    tenant_id: str,
    request: Request,
    _authorized: Annotated[None, Depends(_require_internal_report_access)],
) -> AdapterContractRunIngestResponse:
    """保存一次严格合同报告；内部 Token 在读取第一个正文分块前完成校验。"""

    del _authorized
    if request.query_params:
        # tenant 和命令语义都由 path/body 固定，内部入口不接受任何 query 覆盖。
        raise ApiError("adapter.contract_report_invalid")
    payload = await _read_report(request)
    return ingest_adapter_contract_run(
        request.app.state.config,
        tenant_id,
        payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/adapter-contract-runs", response_model=AdapterContractRunListResponse)
async def get_adapter_contract_runs(
    request: Request,
    context: Annotated[AuthContext, Depends(require_provider_health_reader)],
) -> AdapterContractRunListResponse:
    """列出当前会话租户的合同运行；客户端不能提交 tenant_id。"""

    query = _read_unique_query(request, allowed=_LIST_QUERY_FIELDS)
    return list_adapter_contract_runs(
        request.app.state.config,
        context,
        status=query.get("status"),
        suite_kind=query.get("suite_kind"),
        mode=query.get("mode"),
        limit=_limit(query.get("limit")),
        cursor=query.get("cursor"),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get(
    "/adapter-contract-runs/{adapter_contract_run_id}",
    response_model=AdapterContractRunDetailResponse,
)
async def get_adapter_contract_run_detail(
    adapter_contract_run_id: str,
    request: Request,
    context: Annotated[AuthContext, Depends(require_provider_health_reader)],
) -> AdapterContractRunDetailResponse:
    """读取当前会话租户的一次 run 和全部有界 case；跨租户固定返回 404。"""

    if request.query_params:
        # 详情没有筛选参数；拒绝 tenant_id、重复参数和未来未知参数，避免缓存键语义漂移。
        raise ApiError("adapter.contract_report_invalid")
    return get_adapter_contract_run(
        request.app.state.config,
        context,
        adapter_contract_run_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


async def _read_report(request: Request) -> AdapterContractRunReport:
    """有界读取并严格解析报告 JSON，拒绝重复 key、非有限数和额外字段。"""

    media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media_type != "application/json":
        # 合同报告只接受明确 JSON，禁止浏览器表单或模糊媒体类型绕过严格解析边界。
        raise ApiError("adapter.contract_report_invalid")
    content_lengths = [
        value
        for name, value in request.headers.raw
        if name.lower() == b"content-length"
    ]
    if len(content_lengths) > 1:
        # 重复 Content-Length 可能让代理与应用采用不同正文边界，读取前直接失败关闭。
        raise ApiError("adapter.contract_report_invalid")
    if content_lengths:
        try:
            raw_length = content_lengths[0].decode("ascii")
        # 非 ASCII 长度无法形成唯一 HTTP 正文边界，必须在开始流式读取前失败关闭。
        except UnicodeDecodeError:
            raise ApiError("adapter.contract_report_invalid") from None
        if (
            not raw_length.isdecimal()
            or (len(raw_length) > 1 and raw_length.startswith("0"))
            or not 1 <= int(raw_length) <= MAX_REPORT_BODY_BYTES
        ):
            # 非规范、空或超限长度均使用统一错误，避免暴露解析器对畸形输入的差异。
            raise ApiError("adapter.contract_report_invalid")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_REPORT_BODY_BYTES:
            # 分块传输也受同一硬上限，不能只信任可伪造或缺失的 Content-Length。
            raise ApiError("adapter.contract_report_invalid")
        body.extend(chunk)
    if not body:
        # 空正文不是有效报告，且不交给框架生成可能不一致的自由文本错误。
        raise ApiError("adapter.contract_report_invalid")
    try:
        decoded = bytes(body).decode("utf-8", errors="strict")
        value = json.loads(
            decoded,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
        if type(value) is not dict:
            # 顶层必须是对象，避免数组或标量绕过 Pydantic 的字段级严格校验。
            raise ValueError
        _validate_json_shape(value)
        return AdapterContractRunReport.model_validate(value, strict=True)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateJsonKey,
        RecursionError,
        TypeError,
        ValueError,
        ValidationError,
    ):
        # 解析异常可能引用正文片段，切断异常链后只公开固定领域错误码。
        raise ApiError("adapter.contract_report_invalid") from None


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """构造 JSON 对象并在不记录字段原文的前提下拒绝任意层重复 key。"""

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            # 任意层重复 key 都具有多解析器歧义，必须在模型校验前直接拒绝。
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> object:
    """拒绝 NaN/Infinity 等非标准常量，保证 hash 与跨语言解析一致。"""

    # 非有限常量没有跨语言 canonical JSON 表示，必须在计算报告哈希前拒绝。
    raise ValueError


def _validate_json_shape(root: object) -> None:
    """以显式栈限制 JSON 深度和节点数，避免递归遍历放大恶意小正文。"""

    nodes = 0
    stack: list[tuple[object, int]] = [(root, 1)]
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            # 节点或深度超限可能放大解析资源，统一按无效报告终止。
            raise ValueError
        if isinstance(value, dict):
            stack.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            stack.extend((child, depth + 1) for child in value)


def _read_unique_query(request: Request, *, allowed: frozenset[str]) -> dict[str, str]:
    """读取无重复、无控制字符的 query 白名单，未知 tenant 字段整体拒绝。"""

    result: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key not in allowed or key in result or _has_control(key) or _has_control(value):
            # 未知、重复或控制字符参数会造成租户及缓存语义歧义，必须整体拒绝。
            raise ApiError("adapter.contract_report_invalid")
        result[key] = value
    return result


def _limit(value: str | None) -> int:
    """解析列表分页上限。

    参数：原始 query 字符串或缺省值；返回：1..100 的规范十进制整数。
    约束：拒绝前导零、非 ASCII 和越界输入，避免分页缓存键出现等价编码。
    """

    if value is None:
        return 50
    if (
        not value.isascii()
        or not value.isdecimal()
        or (len(value) > 1 and value.startswith("0"))
    ):
        # 非规范十进制会让同一分页值产生多个缓存表示，必须拒绝而不是宽松转换。
        raise ApiError("adapter.contract_report_invalid")
    parsed = int(value)
    if not 1 <= parsed <= 100:
        # 越界分页可能造成无界数据库读取，只返回固定领域错误。
        raise ApiError("adapter.contract_report_invalid")
    return parsed


def _has_control(value: str) -> bool:
    """判断 query 是否含 C0/C1 控制字符，防止日志与解析边界分歧。"""

    return any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value)


def _request_id(request: Request) -> str:
    """读取统一中间件已经脱敏并确定的 request_id。"""

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """读取统一中间件已经脱敏并确定的 trace_id。"""

    return getattr(request.state, "trace_id", "")


__all__ = ["internal_router", "router"]
