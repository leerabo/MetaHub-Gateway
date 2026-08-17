"""PRD-085 产品激活漏斗的严格只读响应模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class _StrictModel(BaseModel):
    """拒绝未审计字段进入公开聚合响应。"""

    model_config = ConfigDict(extra="forbid")


class ProductAnalyticsWindow(_StrictModel):
    """固定公开漏斗使用全历史窗口和 UTC 时区，禁止客户端改变统计口径。"""

    kind: Literal["all_time"] = "all_time"
    timezone: Literal["UTC"] = "UTC"


class ProductAnalyticsStage(_StrictModel):
    """描述单个激活阶段的租户内事件计数、连续到达状态和首末发生时间。"""

    order: int
    event_type: str
    event_count: int
    reached: bool
    cumulative_reached: bool
    reached_tenant_count: int
    cumulative_tenant_count: int
    conversion_from_previous_bps: int | None
    first_occurred_at: str | None
    last_occurred_at: str | None


class ProductAnalyticsSummary(_StrictModel):
    """汇总七阶段的独立完成数、连续深度、整数完成率和最终激活状态。"""

    completed_stage_count: int
    cumulative_stage_count: int
    total_stage_count: Literal[7] = 7
    completion_bps: int
    activated: bool


class ProductAnalyticsFunnelResponse(_StrictModel):
    """返回当前认证租户的完整漏斗，并携带统一请求与追踪标识。"""

    tenant_id: str
    window: ProductAnalyticsWindow
    cohort_tenant_count: Literal[1] = 1
    stages: list[ProductAnalyticsStage]
    summary: ProductAnalyticsSummary
    generated_at: str
    request_id: str
    trace_id: str


__all__ = [
    "ProductAnalyticsFunnelResponse",
    "ProductAnalyticsStage",
    "ProductAnalyticsSummary",
    "ProductAnalyticsWindow",
]
