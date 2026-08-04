"""PRD-049 控制面反馈 API 的公开响应模型。

响应模型刻意排除 tenant_id、actor ID、Provider ID、Virtual Key ID、请求指纹和
持久事件 payload；这些字段只用于后端权限、归因和审计，不能借控制台接口扩散。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class RequestFeedbackInfo(BaseModel):
    """一条可向当前授权用户展示的不可变调用反馈。

    字段只覆盖对象标识、被评价调用、最终模型、评分、采纳、白名单标签、actor 类型
    和创建时间。actor 类型用于解释反馈来源，但 actor 身份值不进入公开响应。
    """

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
    """反馈首次创建或幂等重放的统一响应。

    ``request_id`` 与 ``trace_id`` 始终属于本次 HTTP 操作；持久事件仍保留首次创建
    链路。``idempotent_replay`` 让客户端区分 201 首建和 200 恢复，而不暴露指纹。
    """

    feedback: RequestFeedbackInfo
    idempotent_replay: bool
    request_id: str
    trace_id: str


class FeedbackTagCount(BaseModel):
    """一个固定白名单标签在完整筛选窗口内的出现次数。"""

    tag: str
    count: int


class FeedbackSummaryTotalsInfo(BaseModel):
    """当前租户与时间窗口的权威总计，不受模型分页影响。"""

    feedback_count: int
    rating_sum: int
    average_rating_milli: int
    accepted_count: int
    adoption_rate_bps: int
    rating_distribution: dict[str, int]
    tag_counts: list[FeedbackTagCount]


class FeedbackModelSummaryInfo(BaseModel):
    """反馈报表中一条模型级窗口统计。"""

    model_id: str
    internal_model_id: str
    display_name: str | None
    feedback_count: int
    average_rating_milli: int
    accepted_count: int
    adoption_rate_bps: int
    rating_distribution: dict[str, int]


class FeedbackSummaryResponse(BaseModel):
    """控制面租户级反馈汇总响应及其确定窗口、分页和链路信息。"""

    scope_type: str
    from_time: datetime
    to_time: datetime
    summary: FeedbackSummaryTotalsInfo
    models: list[FeedbackModelSummaryInfo]
    total: int
    limit: int
    offset: int
    request_id: str
    trace_id: str


__all__ = [
    "FeedbackModelSummaryInfo",
    "FeedbackMutationResponse",
    "FeedbackSummaryResponse",
    "FeedbackSummaryTotalsInfo",
    "FeedbackTagCount",
    "RequestFeedbackInfo",
]
