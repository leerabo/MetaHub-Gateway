"""PRD-049 Gateway 反馈写入口的最小公开响应模型。

数据面只允许 Virtual Key 提交反馈，不提供汇总查询。响应不包含租户、Key ID/前缀、
Provider、指纹或事件 payload，避免高频入口扩大敏感身份与基础设施元数据的暴露面。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class RequestFeedbackInfo(BaseModel):
    """Virtual Key 可见的不可变反馈安全摘要。"""

    request_feedback_id: str
    request_id: str
    model_id: str
    rating: int
    accepted: bool
    tags: list[str]
    actor_type: str
    status: str
    created_at: datetime


class FeedbackMutationResponse(BaseModel):
    """Gateway 首次创建或幂等重放反馈的统一响应。"""

    feedback: RequestFeedbackInfo
    idempotent_replay: bool
    request_id: str
    trace_id: str


__all__ = ["FeedbackMutationResponse", "RequestFeedbackInfo"]
