"""Control Session 反馈领域接入、公开 DTO 映射与提交后 Webhook 投递。

评分、标签、幂等、租户/actor 绑定、资格归因、事务和 summary 单快照均委托
``metahub_shared.feedback``。本模块只把可信 Session 恢复为 actor，并裁剪公开响应。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

from metahub_shared.feedback import (
    FeedbackActor,
    FeedbackCreateResult,
    FeedbackIntent,
    create_request_feedback,
    feedback_event_to_webhook,
    query_feedback_summary,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import WebhookEvent

from metahub_control.auth import AuthContext
from metahub_control.auth.resource_transactions import ResourceWriteTransaction
from metahub_control.webhooks.service import dispatch_webhook_event

from .models import (
    FeedbackModelSummaryInfo,
    FeedbackMutationResponse,
    FeedbackSummaryResponse,
    FeedbackSummaryTotalsInfo,
    FeedbackTagCount,
    RequestFeedbackInfo,
)

logger = logging.getLogger("metahub.control.feedback")


def create_session_feedback(
    config: AppConfig,
    context: AuthContext,
    *,
    operation_request_id: str,
    trace_id: str,
    intent: FeedbackIntent,
) -> FeedbackMutationResponse:
    """以可信 Session actor 创建反馈并在提交后尽力投递持久事件。

    参数：应用配置、已经 RBAC 校验的 Session 上下文、本次操作 request_id/trace_id 与
    shared 规范化意图。返回不含租户、actor ID、Provider 或指纹的公开响应。数据库提交
    先于 Webhook；接收端失败只能写安全日志，不能回滚已形成的反馈事实。
    """

    actor = FeedbackActor(
        tenant_id=context.user.tenant_id,
        actor_type="session_user",
        actor_user_id=context.user.user_id,
    )
    result = create_request_feedback(
        config.database_url,
        actor=actor,
        operation_request_id=operation_request_id,
        trace_id=trace_id,
        intent=intent,
        authorize=_feedback_authorizer(config, context),
    )
    _dispatch_feedback_event(
        config,
        result,
        operation_request_id=operation_request_id,
        response_trace_id=trace_id,
    )
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


def summarize_session_feedback(
    config: AppConfig,
    context: AuthContext,
    *,
    model_id: str | None,
    from_time: datetime,
    to_time: datetime,
    limit: int,
    offset: int,
    request_id: str,
    trace_id: str,
    monotonic_deadline: float,
) -> FeedbackSummaryResponse:
    """查询当前 Session 租户的窗口反馈报表并映射为控制面响应。

    tenant_id 只取自认证上下文；``monotonic_deadline`` 由认证后的路由入口冻结，shared
    helper 在该 endpoint-wide deadline 内使用单连接只读事务形成一致快照。返回总计覆盖
    完整窗口，模型行才应用 limit/offset；DTO 映射也不能越过同一预算。
    """

    if time.monotonic() >= monotonic_deadline:
        # query 解析或资源排队已经耗尽预算时禁止再打开 SQLite 或刷新查询预算。
        raise ApiError("feedback.unavailable")
    result = query_feedback_summary(
        config.database_url,
        tenant_id=context.user.tenant_id,
        from_time=from_time,
        to_time=to_time,
        model_id=model_id,
        limit=limit,
        offset=offset,
        monotonic_deadline=monotonic_deadline,
        authorize=_feedback_authorizer(config, context),
    )
    response = FeedbackSummaryResponse(
        scope_type=result.scope_type,
        from_time=result.from_time,
        to_time=result.to_time,
        summary=FeedbackSummaryTotalsInfo(
            feedback_count=result.summary.feedback_count,
            rating_sum=result.summary.rating_sum,
            average_rating_milli=result.summary.average_rating_milli,
            accepted_count=result.summary.accepted_count,
            adoption_rate_bps=result.summary.adoption_rate_bps,
            rating_distribution=dict(result.summary.rating_distribution),
            tag_counts=[
                FeedbackTagCount(tag=item.tag, count=item.count)
                for item in result.summary.tag_counts
            ],
        ),
        models=[
            FeedbackModelSummaryInfo(
                model_id=item.model_id,
                internal_model_id=item.internal_model_id,
                display_name=item.display_name,
                feedback_count=item.feedback_count,
                average_rating_milli=item.average_rating_milli,
                accepted_count=item.accepted_count,
                adoption_rate_bps=item.adoption_rate_bps,
                rating_distribution=dict(item.rating_distribution),
            )
            for item in result.models
        ],
        total=result.total,
        limit=result.limit,
        offset=result.offset,
        request_id=request_id,
        trace_id=trace_id,
    )
    if time.monotonic() >= monotonic_deadline:
        # 最多 100 个模型 DTO 的 Python 映射也属于 endpoint 预算，不能只限制 SQL statement。
        raise ApiError("feedback.unavailable")
    return response


def _feedback_authorizer(config: AppConfig, context: AuthContext):
    def authorize(connection, read_only):
        try:
            ResourceWriteTransaction(
                connection, config, context, "feedback.use", read_only=read_only,
            ).verify()
        except ApiError as exc:
            if exc.code == "workspace_session.unavailable":
                raise ApiError("feedback.unavailable") from exc
            raise
    return authorize


def _mutation_response(
    result: FeedbackCreateResult,
    *,
    operation_request_id: str,
    response_trace_id: str,
) -> FeedbackMutationResponse:
    """把内部创建结果裁剪成不暴露归属、指纹和事件内部字段的响应。"""

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
    """委托 shared 把首次持久事件转换成经精确 payload 校验的 Webhook 信封。"""

    return feedback_event_to_webhook(result.event)


def _dispatch_feedback_event(
    config: AppConfig,
    result: FeedbackCreateResult,
    *,
    operation_request_id: str,
    response_trace_id: str,
) -> None:
    """提交后使用控制面幂等投递器发送事件，任何外部失败均不改变成功响应。"""

    try:
        # 信封转换也属于提交后副作用；注册/校验异常不能把已提交反馈改写成 HTTP 500。
        event = _persistent_webhook_event(result)
        dispatch_webhook_event(
            config,
            event.tenant_id,
            event,
            request_id=operation_request_id,
            trace_id=response_trace_id,
        )
    except Exception:
        # 异常可能携带 Endpoint、网络或签名细节；日志只保留稳定事件 ID 和固定分类。
        logger.warning(
            "feedback.webhook_dispatch_failed",
            extra={
                "event": "feedback.webhook_dispatch_failed",
                "tenant_id": result.event.tenant_id,
                "object_id": result.event.feedback_event_id,
                "request_id": operation_request_id,
                "trace_id": response_trace_id,
                "error_code": "webhook.delivery_unavailable",
            },
        )


def _log_feedback_created(
    result: FeedbackCreateResult,
    *,
    operation_request_id: str,
    response_trace_id: str,
) -> None:
    """记录不含 rating、accepted、tags、Token 或正文的安全反馈操作摘要。"""

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
                "actor_type": result.feedback.actor_type,
                "idempotent_replay": result.idempotent_replay,
            },
        },
    )


__all__ = ["create_session_feedback", "summarize_session_feedback"]
