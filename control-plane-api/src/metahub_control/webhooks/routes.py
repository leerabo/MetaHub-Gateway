"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/webhooks/routes.py
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from metahub_control.audit import build_audit_request_meta
from metahub_control.auth import AuthContext, require_webhook_manager

from .models import (
    CreateWebhookEndpointRequest,
    ReplayWebhookRequest,
    WebhookDeadLetterArchiveResponse,
    WebhookDeadLetterListResponse,
    WebhookDeliveryListResponse,
    WebhookDeliveryReplayResponse,
    WebhookEndpointCreateResponse,
    WebhookEndpointListResponse,
    WebhookReplayResponse,
    WebhookSecretRotateResponse,
    WebhookTestResponse,
)
from .service import (
    archive_webhook_dead_letter,
    create_webhook,
    list_webhook_dead_letters,
    list_webhook_deliveries,
    list_webhooks,
    replay_webhook,
    replay_webhook_delivery,
    rotate_webhook_secret,
    test_webhook,
)
from .wire import REPLAY_DELIVERY_OPENAPI, read_replay_delivery_request

router = APIRouter(tags=["webhooks"])


@router.get("/webhook-dead-letters", response_model=WebhookDeadLetterListResponse)
async def list_webhook_dead_letters_route(
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_webhook_manager)],
    status: Annotated[str | None, Query()] = "all",
    event_type: Annotated[str | None, Query()] = None,
    webhook_endpoint_id: Annotated[str | None, Query()] = None,
    limit: Annotated[str | None, Query()] = "50",
    cursor: Annotated[str | None, Query()] = None,
) -> WebhookDeadLetterListResponse:
    """读取当前租户 Webhook 死信；viewer 与匿名请求由后端依赖统一拒绝。"""

    _set_no_store(response)
    return list_webhook_dead_letters(
        request.app.state.config,
        context,
        status=status,
        event_type=event_type,
        webhook_endpoint_id=webhook_endpoint_id,
        limit=limit,
        cursor=cursor,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post(
    "/webhook-deliveries/{webhook_delivery_id}/replay",
    response_model=WebhookDeliveryReplayResponse,
    openapi_extra=REPLAY_DELIVERY_OPENAPI,
)
async def replay_webhook_delivery_route(
    webhook_delivery_id: str,
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_webhook_manager)],
) -> WebhookDeliveryReplayResponse:
    """按来源 delivery 创建持久 replay intent，正文只允许幂等键。"""

    _set_no_store(response)
    payload = await read_replay_delivery_request(request)
    return replay_webhook_delivery(
        request.app.state.config,
        context,
        webhook_delivery_id,
        payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
        audit_request_meta=build_audit_request_meta(request),
    )


@router.post(
    "/webhook-dead-letters/{dead_letter_event_id}/archive",
    response_model=WebhookDeadLetterArchiveResponse,
)
async def archive_webhook_dead_letter_route(
    dead_letter_event_id: str,
    request: Request,
    response: Response,
    context: Annotated[AuthContext, Depends(require_webhook_manager)],
) -> WebhookDeadLetterArchiveResponse:
    """逻辑归档当前租户死信，投递与 attempt 证据保持可追踪。"""

    _set_no_store(response)
    return archive_webhook_dead_letter(
        request.app.state.config,
        context,
        dead_letter_event_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
        audit_request_meta=build_audit_request_meta(request),
    )


@router.get("/webhooks", response_model=WebhookEndpointListResponse)
async def list_webhooks_route(
    request: Request,
    context: Annotated[AuthContext, Depends(require_webhook_manager)],
    status: Annotated[str | None, Query()] = None,
    event_type: Annotated[str | None, Query()] = None,
    include_deliveries: Annotated[bool, Query()] = True,
) -> WebhookEndpointListResponse:
    """读取当前租户 Webhook Endpoint 和发送记录，后端按 tenant_id 与角色做边界控制。"""

    return list_webhooks(
        request.app.state.config,
        context,
        status=status,
        event_type=event_type,
        include_deliveries=include_deliveries,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post("/webhooks", response_model=WebhookEndpointCreateResponse)
async def create_webhook_route(
    request: Request,
    payload: CreateWebhookEndpointRequest,
    context: Annotated[AuthContext, Depends(require_webhook_manager)],
) -> WebhookEndpointCreateResponse:
    """创建当前租户 Webhook Endpoint，signing_secret 只在本次响应中返回一次。"""

    return create_webhook(
        request.app.state.config,
        context,
        payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post("/webhooks/{webhook_endpoint_id}/test", response_model=WebhookTestResponse)
async def test_webhook_route(
    webhook_endpoint_id: str,
    request: Request,
    context: Annotated[AuthContext, Depends(require_webhook_manager)],
) -> WebhookTestResponse:
    """向当前租户某个 Endpoint 发送测试事件，并返回逐次 attempt 摘要。"""

    return test_webhook(
        request.app.state.config,
        context,
        webhook_endpoint_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.get("/webhooks/{webhook_endpoint_id}/deliveries", response_model=WebhookDeliveryListResponse)
async def list_webhook_deliveries_route(
    webhook_endpoint_id: str,
    request: Request,
    context: Annotated[AuthContext, Depends(require_webhook_manager)],
    event_type: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = "all",
    limit: Annotated[str | None, Query()] = "100",
) -> WebhookDeliveryListResponse:
    """读取当前租户指定 Endpoint 最近 100 条以内的投递记录和安全 attempt 摘要。"""

    return list_webhook_deliveries(
        request.app.state.config,
        context,
        webhook_endpoint_id,
        event_type=event_type,
        status=status,
        limit=limit,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
    )


@router.post("/webhooks/{webhook_endpoint_id}/replay", response_model=WebhookReplayResponse)
async def replay_webhook_route(
    webhook_endpoint_id: str,
    payload: ReplayWebhookRequest,
    request: Request,
    context: Annotated[AuthContext, Depends(require_webhook_manager)],
) -> WebhookReplayResponse:
    """重放当前租户指定 Endpoint 的失败 delivery，相同幂等键不重复出网。"""

    return replay_webhook(
        request.app.state.config,
        context,
        webhook_endpoint_id,
        payload,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
        audit_request_meta=build_audit_request_meta(request),
    )


@router.post("/webhooks/{webhook_endpoint_id}/rotate-secret", response_model=WebhookSecretRotateResponse)
async def rotate_webhook_secret_route(
    webhook_endpoint_id: str,
    request: Request,
    context: Annotated[AuthContext, Depends(require_webhook_manager)],
) -> WebhookSecretRotateResponse:
    """原子轮换当前租户 Endpoint 的 signing secret，新明文只在本次响应返回。"""

    # 路由只传可信认证上下文和请求元数据，Secret 生成与租户归属全部留在服务层。
    return rotate_webhook_secret(
        request.app.state.config,
        context,
        webhook_endpoint_id,
        request_id=_request_id(request),
        trace_id=_trace_id(request),
        audit_request_meta=build_audit_request_meta(request),
    )


def _request_id(request: Request) -> str:
    """从请求上下文读取统一 request_id，保证 Webhook API 响应和日志可串联。"""

    return getattr(request.state, "request_id", "")


def _trace_id(request: Request) -> str:
    """从请求上下文读取统一 trace_id，保证 Webhook 投递和控制台排障可串联。"""

    return getattr(request.state, "trace_id", "")


def _set_no_store(response: Response) -> None:
    """禁止浏览器或中间代理缓存死信治理响应，避免退出后仍可回读事件标识。"""

    response.headers["Cache-Control"] = "no-store"
