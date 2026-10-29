"""PRD-103 客户、平台和渠道签名三种凭据域的严格 HTTP 边界。"""

from __future__ import annotations

import json
import re
import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.fastapi_app import clear_workspace_cookies_on_error
from starlette.concurrency import run_in_threadpool

from metahub_control.identity.service import valid_opaque_value
from metahub_control.platform_access.constants import (
    PLATFORM_CSRF_COOKIE,
    PLATFORM_SESSION_COOKIE,
    SUPPORT_SESSION_COOKIE,
)
from metahub_control.platform_access.service import (
    PlatformContext,
    authenticate_platform_session_context,
)
from metahub_control.workspace_access.constants import (
    WORKSPACE_CSRF_COOKIE,
    WORKSPACE_SESSION_COOKIE,
)
from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    authenticate_workspace_session_context,
)

from .models import (
    BillingOrdersResponse,
    PaymentIntentCreateRequest,
    PaymentIntentResponse,
    PaymentWebhookReceiptResponse,
    PlatformRefundDecisionRequest,
    PlatformRefundQueueResponse,
    RefundCreateRequest,
    RefundResponse,
)
from .service import (
    create_payment_intent,
    list_billing_orders,
    list_platform_refunds,
    persist_webhook_receipt,
    request_refund,
    review_refund,
)

router = APIRouter(tags=["payment-commerce"])
_MAX_COMMAND_BYTES = 8 * 1024
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")


def require_workspace_payment(request: Request) -> WorkspaceSessionContext:
    """在读取客户命令正文前恢复 WorkspaceSession，并拒绝其他凭据域混入。"""

    clear_workspace_cookies_on_error(request)
    if _raw_headers(request, b"authorization") or _cookie_values(
        request, PLATFORM_SESSION_COOKIE
    ) or _cookie_values(request, SUPPORT_SESSION_COOKIE):
        raise ApiError("workspace_session.credential_conflict")
    csrf = None
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        _require_origin(request, "payment.request_invalid")
        csrf = _required_csrf(request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid")
    context = authenticate_workspace_session_context(
        request.app.state.config,
        _required_cookie(request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"),
        csrf_token=csrf,
    )
    if context.membership_role not in {"owner", "finance_admin"}:
        raise ApiError("workspace_session.permission_denied")
    return context


def require_platform_refund(request: Request) -> PlatformContext:
    """平台退款审核使用独立 Platform Session，并动态检查 billing adjust 权限。"""

    if (
        _raw_headers(request, b"authorization")
        or _cookie_values(request, WORKSPACE_SESSION_COOKIE)
        or _cookie_values(request, SUPPORT_SESSION_COOKIE)
    ):
        raise ApiError("platform.credential_conflict")
    csrf = None
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        _require_origin(request, "platform.origin_invalid")
        csrf = _required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid")
    context = authenticate_platform_session_context(
        request.app.state.config,
        _required_cookie(request, PLATFORM_SESSION_COOKIE, "platform.session_required"),
        csrf_token=csrf,
    )
    required = (
        "platform.billing.adjust"
        if request.method not in {"GET", "HEAD", "OPTIONS"}
        else "platform.billing.read"
    )
    if required not in context.permissions:
        raise ApiError("platform.permission_denied")
    return context


@router.get("/billing/orders", response_model=BillingOrdersResponse)
async def get_billing_orders(
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_payment)],
) -> BillingOrdersResponse:
    """返回当前 Workspace 的订单、支付、退款和催收安全摘要。"""

    _reject_query(request, "payment.request_invalid")
    _private(response)
    return await run_in_threadpool(
        list_billing_orders,
        request.app.state.config,
        session_token=_required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post("/payments/intents", response_model=PaymentIntentResponse, status_code=201)
async def post_payment_intent(
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_payment)],
) -> PaymentIntentResponse:
    """创建一次性支付意图；幂等重放不会再次返回 client token。"""

    _reject_query(request, "payment.request_invalid")
    payload = _model(
        await _strict_json(request, "payment.request_invalid", _MAX_COMMAND_BYTES),
        PaymentIntentCreateRequest,
        "payment.request_invalid",
    )
    result = await run_in_threadpool(
        create_payment_intent,
        request.app.state.config,
        session_token=_required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        csrf_token=_required_csrf(
            request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid"
        ),
        payload=payload,
        idempotency_key=_idempotency_key(request, "payment.request_invalid"),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = 200
    response.headers["ETag"] = _etag(result.payment_intent.state_revision)
    _private(response)
    return result


@router.post("/refunds", response_model=RefundResponse, status_code=201)
async def post_refund(
    request: Request,
    response: Response,
    _context: Annotated[WorkspaceSessionContext, Depends(require_workspace_payment)],
) -> RefundResponse:
    """幂等创建退款申请，客户不能写入 provider 引用或 captured 权威字段。"""

    _reject_query(request, "refund.request_invalid")
    payload = _model(
        await _strict_json(request, "refund.request_invalid", _MAX_COMMAND_BYTES),
        RefundCreateRequest,
        "refund.request_invalid",
    )
    result = await run_in_threadpool(
        request_refund,
        request.app.state.config,
        session_token=_required_cookie(
            request, WORKSPACE_SESSION_COOKIE, "workspace_session.required"
        ),
        csrf_token=_required_csrf(
            request, WORKSPACE_CSRF_COOKIE, "workspace_session.csrf_invalid"
        ),
        payload=payload,
        idempotency_key=_idempotency_key(request, "refund.request_invalid"),
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = 200
    response.headers["ETag"] = _etag(result.refund.state_revision)
    _private(response)
    return result


@router.post(
    "/payments/webhooks/{provider}",
    response_model=PaymentWebhookReceiptResponse,
    status_code=202,
)
async def post_payment_webhook(
    provider: str, request: Request, response: Response
) -> PaymentWebhookReceiptResponse:
    """回调只接受 provider 签名身份，验签并持久 receipt 后立即应答。"""

    _reject_query(request, "payment.webhook_invalid")
    _reject_webhook_browser_credentials(request)
    body = await _raw_body(
        request,
        "payment.webhook_invalid",
        request.app.state.config.payment_webhook_max_body_bytes,
    )
    timestamp = _single_header(request, b"x-metahub-payment-timestamp")
    signature = _single_header(request, b"x-metahub-payment-signature")
    version_text = _single_header(request, b"x-metahub-payment-key-version")
    if timestamp is None or signature is None or version_text is None or not version_text.isdigit():
        raise ApiError("payment.webhook_invalid")
    result = await run_in_threadpool(
        persist_webhook_receipt,
        request.app.state.config,
        provider=provider,
        timestamp=timestamp,
        signature=signature,
        declared_key_version=int(version_text),
        body=body,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    if result.idempotent_replay:
        response.status_code = 200
    _private(response)
    return result


@router.post("/platform/refunds/{refund_id}/review", response_model=RefundResponse)
async def post_platform_refund_review(
    refund_id: str,
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_refund)],
) -> RefundResponse:
    """平台 billing 审核推进退款到 processing 或 rejected。"""

    _reject_query(request, "refund.request_invalid")
    payload = _model(
        await _strict_json(request, "refund.request_invalid", _MAX_COMMAND_BYTES),
        PlatformRefundDecisionRequest,
        "refund.request_invalid",
    )
    result = await run_in_threadpool(
        review_refund,
        request.app.state.config,
        platform_session_token=_required_cookie(
            request, PLATFORM_SESSION_COOKIE, "platform.session_required"
        ),
        csrf_token=_required_csrf(request, PLATFORM_CSRF_COOKIE, "platform.csrf_invalid"),
        refund_id=refund_id,
        expected_revision=_if_match(request, "refund.request_invalid"),
        payload=payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )
    response.headers["ETag"] = _etag(result.refund.state_revision)
    _private(response)
    return result


@router.get("/platform/refunds", response_model=PlatformRefundQueueResponse)
async def get_platform_refunds(
    request: Request,
    response: Response,
    _context: Annotated[PlatformContext, Depends(require_platform_refund)],
) -> PlatformRefundQueueResponse:
    """返回固定状态的最多一百条平台退款审核摘要。"""

    items = request.query_params.multi_items()
    if len(items) != 1 or items[0][0] != "status":
        raise ApiError("refund.request_invalid")
    _private(response)
    return await run_in_threadpool(
        list_platform_refunds,
        request.app.state.config,
        platform_session_token=_required_cookie(
            request, PLATFORM_SESSION_COOKIE, "platform.session_required"
        ),
        status=items[0][1],
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


async def _strict_json(request: Request, error_code: str, maximum: int) -> dict[str, Any]:
    """读取有界 JSON 并拒绝重复键、错误 charset、分块传输和长度漂移。"""

    body = await _raw_body(request, error_code, maximum)

    def unique(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    try:
        payload = json.loads(body.decode("utf-8"), object_pairs_hook=unique)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise ApiError(error_code) from None
    if type(payload) is not dict:
        raise ApiError(error_code)
    return payload


async def _raw_body(request: Request, error_code: str, maximum: int) -> bytes:
    """在内存中有界读取原始正文，Webhook 验签使用返回的原始 bytes。"""

    content_types = _raw_headers(request, b"content-type")
    if len(content_types) != 1:
        raise ApiError(error_code)
    media_type, separator, parameters = content_types[0].partition(";")
    if media_type.strip().lower() != "application/json" or (
        separator and parameters.strip().lower() not in {"charset=utf-8", 'charset="utf-8"'}
    ):
        raise ApiError(error_code)
    if _raw_headers(request, b"transfer-encoding"):
        raise ApiError(error_code)
    lengths = _raw_headers(request, b"content-length")
    if len(lengths) > 1:
        raise ApiError(error_code)
    declared: int | None = None
    if lengths:
        if not lengths[0].isdigit() or (len(lengths[0]) > 1 and lengths[0].startswith("0")):
            raise ApiError(error_code)
        declared = int(lengths[0])
        if declared < 2 or declared > maximum:
            raise ApiError(error_code)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > maximum:
            raise ApiError(error_code)
    if not body or (declared is not None and declared != len(body)):
        raise ApiError(error_code)
    return bytes(body)


def _model(payload: dict[str, Any], model_type, error_code: str):
    """把 Pydantic 严格错误折叠为稳定业务码。"""

    try:
        return model_type.model_validate(payload)
    except ValueError:
        raise ApiError(error_code) from None


def _reject_webhook_browser_credentials(request: Request) -> None:
    """渠道路由拒绝 Cookie、Bearer、Origin 和浏览器 fetch 元数据，消除 credential confusion。"""

    if (
        _raw_headers(request, b"authorization")
        or _raw_headers(request, b"cookie")
        or _raw_headers(request, b"origin")
        or _raw_headers(request, b"sec-fetch-site")
        or _raw_headers(request, b"x-csrf-token")
    ):
        raise ApiError("payment.webhook_invalid")


def _required_cookie(request: Request, name: str, missing_code: str) -> str:
    """要求唯一合法 Cookie，重复值不采用框架的最后值语义。"""

    values = _cookie_values(request, name)
    if len(values) != 1 or not valid_opaque_value(values[0]):
        if values and name == PLATFORM_SESSION_COOKIE:
            raise ApiError("platform.session_invalid")
        if values and name == WORKSPACE_SESSION_COOKIE:
            raise ApiError("workspace_session.invalid")
        raise ApiError(missing_code)
    return values[0]


def _required_csrf(request: Request, cookie_name: str, error_code: str) -> str:
    """双提交 CSRF 要求唯一 Cookie/Header 且常量时间相等。"""

    cookies = _cookie_values(request, cookie_name)
    headers = _raw_headers(request, b"x-csrf-token")
    if (
        len(cookies) != 1
        or len(headers) != 1
        or not valid_opaque_value(cookies[0])
        or not valid_opaque_value(headers[0])
        or not secrets.compare_digest(cookies[0], headers[0])
    ):
        raise ApiError(error_code)
    return cookies[0]


def _require_origin(request: Request, error_code: str) -> None:
    """客户和平台写命令只接受部署 allowlist 的同站 Origin。"""

    origins = _raw_headers(request, b"origin")
    sites = _raw_headers(request, b"sec-fetch-site")
    if (
        len(origins) != 1
        or "," in origins[0]
        or origins[0] not in request.app.state.config.allowed_origins
        or len(sites) > 1
        or (sites and sites[0].lower() == "cross-site")
    ):
        raise ApiError(error_code)


def _idempotency_key(request: Request, error_code: str) -> str:
    """命令幂等键必须唯一、有界并使用可审计字符集。"""

    values = _raw_headers(request, b"idempotency-key")
    if len(values) != 1 or _IDEMPOTENCY_RE.fullmatch(values[0]) is None:
        raise ApiError(error_code)
    return values[0]


def _if_match(request: Request, error_code: str) -> int:
    """解析强 ETag revision，拒绝弱验证器和多值。"""

    values = _raw_headers(request, b"if-match")
    if len(values) != 1 or re.fullmatch(r'"[1-9][0-9]*"', values[0]) is None:
        raise ApiError(error_code)
    return int(values[0][1:-1])


def _cookie_values(request: Request, name: str) -> list[str]:
    """从所有 Cookie header 收集目标值，保留重复项用于失败关闭。"""

    values: list[str] = []
    for raw in _raw_headers(request, b"cookie"):
        for segment in raw.split(";"):
            key, separator, value = segment.strip().partition("=")
            if separator and key == name:
                values.append(value)
    return values


def _raw_headers(request: Request, name: bytes) -> list[str]:
    """读取原始 header 列表，避免框架合并隐藏重复安全字段。"""

    return [
        value.decode("latin-1")
        for key, value in request.scope.get("headers", ())
        if key.lower() == name
    ]


def _single_header(request: Request, name: bytes) -> str | None:
    """签名字段必须恰好出现一次。"""

    values = _raw_headers(request, name)
    return values[0] if len(values) == 1 else None


def _reject_query(request: Request, error_code: str) -> None:
    """固定路径合同不允许 query 携带 owner 或凭据。"""

    if request.query_params.multi_items():
        raise ApiError(error_code)


def _private(response: Response) -> None:
    """商业响应禁止浏览器和中间代理缓存。"""

    response.headers["Cache-Control"] = "no-store, private"


def _etag(revision: int) -> str:
    """状态 revision 使用强 ETag。"""

    return f'"{revision}"'


def _request_id(request: Request) -> str:
    """从统一中间件读取公开 request id。"""

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """从统一中间件读取公开 trace id。"""

    return getattr(request.state, "trace_id", "")
