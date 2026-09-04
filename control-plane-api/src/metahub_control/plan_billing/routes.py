"""套餐与额度 HTTP 路由、后端 RBAC 和有界 exact JSON 解析。"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from metahub_shared.runtime.errors import ApiError

from metahub_control.audit import build_audit_request_meta
from metahub_control.auth import (
    AuthContext,
    require_plan_reader,
    require_plan_writer,
    require_quota_reader,
    require_quota_writer,
)

from .models import (
    PlanPatchRequest,
    PlanResponse,
    QuotaAdjustmentRequest,
    QuotaAdjustmentResponse,
    QuotaBucketsResponse,
)
from .service import create_quota_adjustment, get_plan, get_quota_buckets, patch_plan

router = APIRouter(tags=["plan-billing"])
# 请求体上限与结构深度共同限制解析成本，且不允许客户端扩大服务端资源边界。
MAX_BODY_BYTES = 8 * 1024
MAX_JSON_DEPTH = 8
MAX_JSON_NODES = 64


@router.get("/plans", response_model=PlanResponse)
async def read_plan(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_plan_reader)],
) -> PlanResponse:
    """读取当前认证租户唯一 Plan；客户端不能提交 tenant 参数。"""

    _reject_query(request, "plan.request_invalid")
    _private_no_store(response)
    return get_plan(
        request.app.state.config,
        context,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.patch(
    "/plans",
    response_model=PlanResponse,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": PlanPatchRequest.model_json_schema()}},
        }
    },
)
async def update_plan(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_plan_writer)],
) -> PlanResponse:
    """创建或 CAS 修改 Plan；运行时正文严格拒绝未知、重复和 owner 字段。"""

    _reject_query(request, "plan.request_invalid")
    payload = await _read_json_object(request, error_code="plan.request_invalid")
    _private_no_store(response)
    return patch_plan(
        request.app.state.config,
        context,
        payload,
        audit_request_meta=build_audit_request_meta(request),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/quota-buckets", response_model=QuotaBucketsResponse)
async def read_quota_buckets(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_quota_reader)],
) -> QuotaBucketsResponse:
    """读取四类额度、Allocation 分类报表和独立 overage 摘要。"""

    _reject_query(request, "quota.request_invalid")
    _private_no_store(response)
    return get_quota_buckets(
        request.app.state.config,
        context,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post(
    "/quota-adjustments",
    response_model=QuotaAdjustmentResponse,
    status_code=201,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": QuotaAdjustmentRequest.model_json_schema()}
            },
        }
    },
)
async def post_quota_adjustment(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_quota_writer)],
) -> QuotaAdjustmentResponse:
    """创建额度 credit/debit，或恢复完全相同的租户内幂等事实。"""

    _reject_query(request, "quota.request_invalid")
    idempotency_key = _idempotency_key(request)
    payload = await _read_json_object(request, error_code="quota.request_invalid")
    result = create_quota_adjustment(
        request.app.state.config,
        context,
        payload,
        idempotency_key=idempotency_key,
        audit_request_meta=build_audit_request_meta(
            request,
            sensitive_values=(idempotency_key,),
        ),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = 200
    _private_no_store(response)
    return result


runtime_router = APIRouter()
runtime_router.routes.extend(
    route for route in router.routes if getattr(route, "path", None) != "/plans"
)


def _reject_query(request: Request, error_code: str) -> None:
    """四个接口均无查询参数；拒绝 tenant_id 和任何未来含糊输入。"""

    # 公开路由不接受任何查询参数，避免 tenant 或未来字段绕过认证上下文。
    if request.query_params.multi_items():
        raise ApiError(error_code)


def _idempotency_key(request: Request) -> str:
    """读取唯一可见 ASCII 幂等键；明文只驻留当前请求内存。"""

    values = [
        value.decode("latin-1")
        for key, value in request.scope.get("headers", [])
        if key.lower() == b"idempotency-key"
    ]
    # 幂等键必须唯一，缺失或重复时都不能进入额度写事务。
    if len(values) != 1:
        raise ApiError("quota.request_invalid")
    value = values[0]
    # 只允许有界可见 ASCII，防止控制字符进入摘要、审计元数据或访问日志。
    if not 8 <= len(value) <= 128 or any(
        ord(character) < 33 or ord(character) > 126 for character in value
    ):
        # 非法幂等键不能进入摘要、审计元数据或任何额度写事务。
        raise ApiError("quota.request_invalid")
    return value


async def _read_json_object(request: Request, *, error_code: str) -> dict[str, Any]:
    """有界读取 UTF-8 JSON object，并拒绝任意层重复键、深度和节点攻击。"""

    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    # 内容类型不精确时拒绝读取正文，避免不同解析器产生合同歧义。
    if content_type != "application/json":
        raise ApiError(error_code)
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            size = int(declared)
        # 非法长度统一映射输入错误，不回显原始 Header 内容。
        except ValueError as exc:
            raise ApiError(error_code) from exc
        # 声明长度必须落在有界范围，实际流长度仍会在下方独立复核。
        if size < 1 or size > MAX_BODY_BYTES:
            raise ApiError(error_code)
    body = bytearray()
    async for chunk in request.stream():
        # 分块正文按实际累计长度再次限流，不能信任客户端 Content-Length。
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise ApiError(error_code)
        body.extend(chunk)
    # 空正文不具有可解释的写入意图，必须在服务层事务之前拒绝。
    if not body:
        raise ApiError(error_code)
    try:
        parsed = json.loads(
            bytes(body).decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    # 编码、重复键或递归结构错误均收敛为同一公开输入错误。
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ApiError(error_code) from exc
    # 顶层必须是对象且结构受限，数组或深层节点不能进入业务归一化。
    if type(parsed) is not dict or _unsafe_json(parsed):
        raise ApiError(error_code)
    return parsed


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """把 JSON 键值对还原为对象，并在任意层发现重复键时立即失败。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        # 重复键可能让不同解析器看到不同写入意图，因此不能以后值覆盖前值。
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _unsafe_json(value: object) -> bool:
    """限制递归结构并拒绝字符串控制字符。"""

    stack: list[tuple[object, int]] = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if depth > MAX_JSON_DEPTH or nodes > MAX_JSON_NODES:
            return True
        if type(current) is str:
            if any(ord(character) < 32 for character in current):
                return True
        elif type(current) is dict:
            stack.extend((item, depth + 1) for pair in current.items() for item in pair)
        elif type(current) is list:
            stack.extend((item, depth + 1) for item in current)
    return False


def _private_no_store(response: Response) -> None:
    """为套餐与额度响应设置私有禁缓存边界，防止共享缓存保存租户数据。"""

    response.headers["Cache-Control"] = "no-store, private"


def _request_id(request: Request) -> str:
    """读取中间件生成的请求标识，不接受客户端业务正文覆盖链路归属。"""

    return str(getattr(request.state, "request_id", ""))


def _trace_id(request: Request) -> str:
    """读取中间件生成的追踪标识，用于响应和审计事实的服务端关联。"""

    return str(getattr(request.state, "trace_id", ""))


__all__ = ["router"]
