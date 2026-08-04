"""Gateway Virtual Key 反馈接入与非阻塞持久事件入队。

领域校验、tenant/key 归属、事务和幂等全部复用 shared 包。本模块仅恢复可信 actor、
裁剪公开响应，并把提交后的稳定事件放入既有有界 worker；队列不可用不能反压模型热路径。
"""

from __future__ import annotations

import logging

from metahub_shared.feedback import (
    FeedbackActor,
    FeedbackCreateResult,
    FeedbackIntent,
    create_request_feedback,
    feedback_event_to_webhook,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.webhooks import WebhookEvent

from metahub_gateway.auth.virtual_key import GatewayAuthContext
from metahub_gateway.events import enqueue_runtime_event

from .models import FeedbackMutationResponse, RequestFeedbackInfo

logger = logging.getLogger("metahub.gateway.feedback")


def create_virtual_key_feedback(
    config: AppConfig,
    app_state: object,
    context: GatewayAuthContext,
    *,
    operation_request_id: str,
    trace_id: str,
    intent: FeedbackIntent,
) -> FeedbackMutationResponse:
    """以可信 Virtual Key actor 创建反馈并把持久事件尽力放入后台队列。

    参数 ``context`` 来自 Key 哈希认证，tenant/key 不接受客户端覆盖；``app_state``
    仅用于访问既有有界事件队列。返回安全公开 DTO；worker 不可用或队列满仍返回成功，
    因为数据库反馈事实已经提交且将来可用相同操作 request_id 重放入队。
    """

    actor = FeedbackActor(
        tenant_id=context.tenant_id,
        actor_type="virtual_key",
        actor_virtual_key_id=context.virtual_key_id,
    )
    result = create_request_feedback(
        config.database_url,
        actor=actor,
        operation_request_id=operation_request_id,
        trace_id=trace_id,
        intent=intent,
    )
    _enqueue_feedback_event(app_state, result)
    _log_feedback_created(
        result,
        operation_request_id=operation_request_id,
        response_trace_id=trace_id,
    )
    return _mutation_response(
        result,
        operation_request_id=operation_request_id,
        response_trace_id=trace_id,
    )


def _mutation_response(
    result: FeedbackCreateResult,
    *,
    operation_request_id: str,
    response_trace_id: str,
) -> FeedbackMutationResponse:
    """把内部 actor/tenant/event 事实裁剪成 Gateway 的公开反馈响应。"""

    feedback = result.feedback
    return FeedbackMutationResponse(
        feedback=RequestFeedbackInfo(
            request_feedback_id=feedback.request_feedback_id,
            request_id=feedback.source_request_id,
            model_id=feedback.model_id,
            rating=feedback.rating,
            accepted=feedback.accepted,
            tags=list(feedback.tags),
            actor_type=feedback.actor_type,
            status=feedback.status,
            created_at=feedback.created_at,
        ),
        idempotent_replay=result.idempotent_replay,
        request_id=operation_request_id,
        trace_id=response_trace_id,
    )


def _persistent_webhook_event(result: FeedbackCreateResult) -> WebhookEvent:
    """委托 shared 从首次持久 Event 恢复并精确校验统一 Webhook 信封。"""

    return feedback_event_to_webhook(result.event)


def _enqueue_feedback_event(app_state: object, result: FeedbackCreateResult) -> None:
    """提交后转换并非阻塞入队持久事件，任何异常只记录固定安全分类。

    ``feedback_event_to_webhook`` 的注册/payload 校验和 ``put_nowait`` 都发生在数据库
    commit 之后；内部契约或队列故障不能把已经形成的 Feedback/Event 改写成 HTTP 500。
    """

    try:
        # dispatcher 自身会记录 worker_unavailable/queue_full；这里兜住转换或未知队列实现异常。
        enqueue_runtime_event(app_state, _persistent_webhook_event(result))
    except Exception:
        logger.warning(
            "feedback.runtime_event_enqueue_failed",
            extra={
                "event": "feedback.runtime_event_enqueue_failed",
                "tenant_id": result.event.tenant_id,
                "object_id": result.event.feedback_event_id,
                "request_id": result.event.operation_request_id,
                "trace_id": result.event.trace_id,
                "error_code": "webhook.delivery_unavailable",
            },
        )


def _log_feedback_created(
    result: FeedbackCreateResult,
    *,
    operation_request_id: str,
    response_trace_id: str,
) -> None:
    """写入不含 Key、rating、accepted、tags 或正文的安全数据面操作日志。"""

    logger.info(
        "feedback.recorded",
        extra={
            "event": "feedback.recorded",
            "tenant_id": result.event.tenant_id,
            "object_id": result.feedback.request_feedback_id,
            "request_id": operation_request_id,
            "trace_id": response_trace_id,
            "payload": {
                "source_request_id": result.feedback.source_request_id,
                "actor_type": "virtual_key",
                "idempotent_replay": result.idempotent_replay,
            },
        },
    )


__all__ = ["create_virtual_key_feedback"]
