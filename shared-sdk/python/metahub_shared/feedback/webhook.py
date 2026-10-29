"""把 PRD-049 持久事件精确转换为统一 Webhook 信封。

调用方只能传 ``FeedbackEventRecord``；稳定 event/object/actor/trace/time 均来自已提交事件行，
不能在 Control 或 Gateway 中重新生成，从而保证重放和首次投递使用同一幂等事实。
"""

from __future__ import annotations

from metahub_shared.runtime.webhooks import WebhookEvent, build_webhook_event

from .contracts import FeedbackEventRecord


def feedback_event_to_webhook(event: FeedbackEventRecord) -> WebhookEvent:
    """构造并再次校验 ``feedback.created`` 精确信封和安全 payload。"""

    return build_webhook_event(
        tenant_id=event.tenant_id,
        event_type=event.event_type,
        payload=event.payload,
        actor_id=event.actor_id,
        object_id=event.request_feedback_id,
        trace_id=event.trace_id,
        event_id=event.feedback_event_id,
        timestamp=event.created_at,
    )
