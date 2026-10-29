"""PRD-080 账单争议 HTTP 路由与严格 wire 解析。

路由只接受有限大小、有限深度且没有重复键的 UTF-8 JSON；租户和权限始终由后端依赖解析，
写请求的幂等键不会进入日志，所有响应均使用私有且禁止缓存的策略。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from metahub_shared.runtime.errors import ApiError
from pydantic import BaseModel, ValidationError

from metahub_control.audit import build_audit_request_meta
from metahub_control.auth import (
    AuthContext,
    require_billing_dispute_creator,
    require_billing_dispute_mutator,
    require_billing_dispute_reader,
)

from .models import (
    BillingDisputeCreateRequest,
    BillingDisputeDetailResponse,
    BillingDisputeListResponse,
    BillingDisputeMutationResponse,
    BillingDisputePatchRequest,
)
from .service import (
    create_billing_dispute,
    get_billing_dispute,
    list_billing_disputes,
    transition_billing_dispute,
)

MAX_BODY_BYTES = 32 * 1024
MAX_JSON_DEPTH = 5
MAX_JSON_NODES = 512


class BillingDisputeRoute(APIRoute):
    """把框架路径或响应校验失败收敛为稳定领域错误，保留统一 request_id。"""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError:
                raise ApiError("billing_dispute.request_invalid") from None
            except ApiError:
                raise
            except Exception:
                # This boundary may hold authorized reason text in request/response objects.
                # Convert unknown failures before ServerErrorMiddleware can re-raise them to
                # the process logger with an exception message or traceback.
                raise ApiError("billing_dispute.unavailable") from None

        return handler


router = APIRouter(
    prefix="/billing-disputes",
    tags=["billing-disputes"],
    route_class=BillingDisputeRoute,
)


@router.get("", response_model=BillingDisputeListResponse)
async def get_billing_disputes_route(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_billing_dispute_reader)],
) -> BillingDisputeListResponse:
    """读取当前认证租户的快照列表；实时 read 权限由后端依赖和事务再次校验。"""

    query = _strict_query(
        request,
        {"status", "scope_type", "created_from", "created_to", "limit", "cursor"},
    )
    _private(response)
    return list_billing_disputes(
        request.app.state.config,
        context,
        query=query,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/{billing_dispute_id}", response_model=BillingDisputeDetailResponse)
async def get_billing_dispute_route(
    request: Request,
    response: Response,
    billing_dispute_id: str,
    context: Annotated[AuthContext, Depends(require_billing_dispute_reader)],
) -> BillingDisputeDetailResponse:
    """读取单个同租户争议；跨租户标识与不存在资源统一返回 not_found。"""

    _reject_query(request)
    _private(response)
    return get_billing_dispute(
        request.app.state.config,
        context,
        billing_dispute_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post(
    "",
    response_model=BillingDisputeMutationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_billing_dispute_route(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_billing_dispute_creator)],
) -> BillingDisputeMutationResponse:
    """创建争议并冻结费用、hold、事件和审计事实；幂等重放返回既有资源。"""

    _reject_query(request)
    idempotency_key = _idempotency_key(request)
    payload = await _parse_exact_json(request, BillingDisputeCreateRequest)
    result = create_billing_dispute(
        request.app.state.config,
        context,
        payload,
        idempotency_key=idempotency_key,
        audit_request_meta=build_audit_request_meta(
            request,
            sensitive_values=(idempotency_key, payload.reason),
        ),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = status.HTTP_200_OK
    _private(response)
    return result


@router.patch("/{billing_dispute_id}", response_model=BillingDisputeMutationResponse)
async def patch_billing_dispute_route(
    request: Request,
    response: Response,
    billing_dispute_id: str,
    context: Annotated[AuthContext, Depends(require_billing_dispute_mutator)],
) -> BillingDisputeMutationResponse:
    """执行后端授权的 CAS 状态迁移，前端按钮可见性不参与权限判定。"""

    _reject_query(request)
    idempotency_key = _idempotency_key(request)
    payload = await _parse_exact_json(request, BillingDisputePatchRequest)
    result = transition_billing_dispute(
        request.app.state.config,
        context,
        billing_dispute_id,
        payload,
        idempotency_key=idempotency_key,
        audit_request_meta=build_audit_request_meta(
            request,
            sensitive_values=(
                (idempotency_key, payload.decision_reason)
                if payload.decision_reason is not None
                else (idempotency_key,)
            ),
        ),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    _private(response)
    return result


async def _parse_exact_json[PayloadT: BaseModel](
    request: Request, model: type[PayloadT]
) -> PayloadT:
    """严格解析单个 JSON 对象，拒绝重复键、非有限数字、超深或超大请求。"""

    _require_json_content_type(request)
    body = bytearray()
    declared = _content_length(request)
    if declared is not None and (declared < 1 or declared > MAX_BODY_BYTES):
        raise ApiError("billing_dispute.request_invalid")
    try:
        # 流式累计同时核对声明长度与实际字节数，避免框架预解析绕过请求体大小上限。
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_BODY_BYTES:
                raise ApiError("billing_dispute.request_invalid")
            body.extend(chunk)
        if not body or (declared is not None and declared != len(body)):
            raise ApiError("billing_dispute.request_invalid")
        parsed = json.loads(
            bytes(body).decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite")),
        )
        if type(parsed) is not dict:
            raise ValueError("object required")
        _validate_shape(parsed)
        return model.model_validate(parsed)
    except ApiError:
        raise
    except (UnicodeError, ValueError, ValidationError, json.JSONDecodeError, RecursionError):
        raise ApiError("billing_dispute.request_invalid") from None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _validate_shape(value: object) -> None:
    stack = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if depth > MAX_JSON_DEPTH or nodes > MAX_JSON_NODES:
            raise ValueError("json shape")
        if type(current) is dict:
            stack.extend((item, depth + 1) for item in current.values())
        elif type(current) is list:
            stack.extend((item, depth + 1) for item in current)


def _require_json_content_type(request: Request) -> None:
    values = _raw_header_values(request, b"content-type")
    if len(values) != 1 or "," in values[0]:
        raise ApiError("billing_dispute.request_invalid")
    parts = values[0].split(";")
    if parts[0].strip().lower() != "application/json" or len(parts) > 2:
        raise ApiError("billing_dispute.request_invalid")
    if len(parts) == 2:
        name, separator, value = parts[1].partition("=")
        if (
            separator != "="
            or name.strip().lower() != "charset"
            or value.strip().lower()
            not in {
                "utf-8",
                '"utf-8"',
            }
        ):
            raise ApiError("billing_dispute.request_invalid")


def _content_length(request: Request) -> int | None:
    values = _raw_header_values(request, b"content-length")
    if not values:
        return None
    if len(values) != 1 or not values[0].isdigit() or len(values[0]) > 12:
        raise ApiError("billing_dispute.request_invalid")
    return int(values[0])


def _idempotency_key(request: Request) -> str:
    values = _raw_header_values(request, b"idempotency-key")
    if len(values) != 1:
        raise ApiError("billing_dispute.request_invalid")
    value = values[0]
    if not 8 <= len(value) <= 128 or any(not 33 <= ord(char) <= 126 for char in value):
        raise ApiError("billing_dispute.request_invalid")
    return value


def _raw_header_values(request: Request, name: bytes) -> list[str]:
    """从 ASGI 原始头读取全部值，使重复 Header 无法被合并后伪装成唯一输入。"""

    # 必须遍历原始列表；框架映射可能折叠重复 Header，导致幂等键或媒体类型歧义。
    result: list[str] = []
    for key, value in request.scope.get("headers", ()):
        if key.lower() == name:
            try:
                result.append(value.decode("ascii", errors="strict"))
            except UnicodeDecodeError:
                raise ApiError("billing_dispute.request_invalid") from None
    return result


def _strict_query(request: Request, allowed: set[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key not in allowed or key in result or not value:
            raise ApiError("billing_dispute.query_invalid")
        result[key] = value
    return result


def _reject_query(request: Request) -> None:
    if request.query_params.multi_items():
        raise ApiError("billing_dispute.query_invalid")


def _private(response: Response) -> None:
    """争议响应含租户账务信息，任何成功或重放响应都禁止共享缓存。"""

    response.headers["Cache-Control"] = "no-store, private"


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", ""))


def _trace_id(request: Request) -> str:
    return str(getattr(request.state, "trace_id", ""))


__all__ = ["router"]
