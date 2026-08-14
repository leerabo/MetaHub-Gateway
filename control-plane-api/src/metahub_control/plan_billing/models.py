"""套餐、额度、分类消费与调整的公开 DTO。

公开金额全部使用固定六位 CNY 文本；幂等摘要、Reservation owner、Webhook delivery
快照、SQL 和调整原因正文不进入列表响应。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

QuotaType = Literal["paid", "free", "trial", "gift"]


class PlanPatchRequest(BaseModel):
    """OpenAPI 展示合同；运行时正文仍由路由 exact parser 校验。"""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    display_name: str = Field(min_length=1, max_length=80)
    base_tier: Literal["free", "paid"]
    status: Literal["active", "suspended"]
    trial_ends_at: datetime | None = None


class QuotaAdjustmentRequest(BaseModel):
    """OpenAPI 展示合同；金额必须为文本，不能由 JSON number 隐式转换。"""

    model_config = ConfigDict(extra="forbid")

    quota_type: QuotaType
    direction: Literal["credit", "debit"]
    amount: str
    reason: str = Field(min_length=1, max_length=500)
    expected_bucket_revision: int = Field(ge=1)


class QuotaBucketInfo(BaseModel):
    """当前租户一个固定类型 Bucket 的安全计数。"""

    quota_bucket_id: str
    tenant_id: str
    plan_id: str
    quota_type: QuotaType
    granted: str
    consumed: str
    reserved: str
    available: str
    status: Literal["active", "exhausted", "expired", "suspended"]
    expires_at: datetime | None
    state_revision: int
    allocation_consumed: str
    allocation_count: int
    created_at: datetime
    updated_at: datetime


class PlanInfo(BaseModel):
    """返回当前认证租户唯一套餐及服务端推导的试用生命周期状态。"""

    plan_id: str
    tenant_id: str
    display_name: str
    base_tier: Literal["free", "paid"]
    effective_tier: Literal["free", "paid", "trial"]
    status: Literal["active", "suspended"]
    trial_status: Literal["not_started", "active", "expired"]
    trial_started_at: datetime | None
    trial_ends_at: datetime | None
    trial_cycle: int
    state_revision: int
    quota_generation: int
    created_at: datetime
    updated_at: datetime


class PlanResponse(BaseModel):
    """返回套餐读取或版本化更新结果，并携带同租户固定四类额度桶。"""

    plan: PlanInfo | None
    quota_buckets: list[QuotaBucketInfo]
    trial_cycle_changed: bool = False
    request_id: str
    trace_id: str


class QuotaAdjustmentSummary(BaseModel):
    """额度页最近调整摘要；reason 正文只保存在权威事实表，不在轮询列表复制。"""

    quota_adjustment_id: str
    tenant_id: str
    plan_id: str
    quota_type: QuotaType
    direction: Literal["credit", "debit"]
    amount: str
    actor_user_id: str
    operation_request_id: str
    resulting_bucket_revision: int
    created_at: datetime


class QuotaOverageSummary(BaseModel):
    """未分摊成本单独展示，绝不塞入 paid 分类。"""

    amount: str
    settlement_count: int


class QuotaBucketsResponse(BaseModel):
    """PAGE-111 四分类额度与最近调整响应。"""

    plan: PlanInfo | None
    quota_buckets: list[QuotaBucketInfo]
    overage: QuotaOverageSummary
    recent_adjustments: list[QuotaAdjustmentSummary]
    request_id: str
    trace_id: str


class QuotaAdjustmentResponse(BaseModel):
    """首次创建或幂等恢复的调整响应。"""

    adjustment: QuotaAdjustmentSummary
    quota_bucket: QuotaBucketInfo
    plan: PlanInfo
    idempotent_replay: bool
    request_id: str
    trace_id: str


__all__ = [
    "PlanInfo",
    "PlanPatchRequest",
    "PlanResponse",
    "QuotaAdjustmentRequest",
    "QuotaAdjustmentResponse",
    "QuotaAdjustmentSummary",
    "QuotaBucketInfo",
    "QuotaBucketsResponse",
    "QuotaOverageSummary",
    "QuotaType",
]
