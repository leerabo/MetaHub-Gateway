"""PRD-080 账单争议公开 DTO。

所有公开金额均使用固定六位小数字符串；租户和操作者来自认证上下文，不接受客户端注入。
幂等摘要、租约坐标、Webhook Secret 与原始 Usage 证据均属于敏感内部事实，不进入响应模型。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

BillingDisputeStatus = Literal["submitted", "under_review", "approved", "rejected", "cancelled"]
BillingDisputeAction = Literal["start_review", "approve", "reject", "cancel"]
BillingDisputeScope = Literal["request_ids", "time_range"]
CurrencyCode = Literal["CNY", "USD"]

# DTO 只投影财务人员完成工作流所需的最小字段；金额、租户和敏感投递事实不得混入同一模型。


class BillingDisputeAdjustmentRequest(BaseModel):
    """单个批准项及其精确冲正金额；币种只能由服务端冻结账本项推导。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    billing_dispute_item_id: StrictStr = Field(min_length=37, max_length=37)
    amount: StrictStr = Field(min_length=1, max_length=32)


class BillingDisputeCreateRequest(BaseModel):
    """创建意图；认证租户和操作者不是客户端字段，范围最多冻结一百条费用。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    scope_type: BillingDisputeScope
    request_ids: list[StrictStr] | None = Field(default=None, min_length=1, max_length=100)
    period_start: StrictStr | None = Field(default=None, max_length=32)
    period_end: StrictStr | None = Field(default=None, max_length=32)
    reason: StrictStr = Field(min_length=1, max_length=2000)


class BillingDisputePatchRequest(BaseModel):
    """带期望修订号的 CAS 迁移意图；状态、租户、操作者和币种均由后端推导。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    action: BillingDisputeAction
    expected_revision: StrictInt = Field(ge=1)
    decision_reason: StrictStr | None = Field(default=None, min_length=1, max_length=2000)
    adjustments: list[BillingDisputeAdjustmentRequest] | None = Field(
        default=None, min_length=1, max_length=100
    )


class BillingDisputeActorInfo(BaseModel):
    """争议界面可见的最小同租户用户身份。"""

    user_id: str
    display_name: str | None
    email: str


class BillingDisputeSummary(BaseModel):
    """绑定双水位快照的列表投影，避免翻页期间状态变化造成重复或漏项。"""

    billing_dispute_id: str
    tenant_id: str
    scope_type: BillingDisputeScope
    period_start: datetime | None
    period_end: datetime | None
    status: BillingDisputeStatus
    state_revision: int
    item_count: int
    reason_sha256: str
    created_by: BillingDisputeActorInfo
    reviewer: BillingDisputeActorInfo | None
    disputed_currency_totals: dict[str, str]
    approved_currency_totals: dict[str, str]
    active_hold_count: int
    adjustment_count: int
    event_delivery_status: Literal["pending", "processing", "succeeded", "failed"]
    review_started_at: datetime | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class BillingSettlementHoldInfo(BaseModel):
    """单个冻结费用项的当前或历史结算暂缓层；修订号独立于争议状态修订号。"""

    billing_settlement_hold_id: str
    billing_dispute_item_id: str
    finops_ledger_entry_id: str
    status: Literal["active", "released"]
    release_reason: Literal["approved", "rejected", "cancelled"] | None
    state_revision: int
    created_transition_id: str
    released_transition_id: str | None
    created_at: datetime
    released_at: datetime | None
    updated_at: datetime


class BillingDisputeAdjustmentLinkInfo(BaseModel):
    """批准项与 FinOps 冲正事实之间的不可变关联。"""

    billing_dispute_adjustment_link_id: str
    billing_dispute_item_id: str
    finops_adjustment_id: str
    reversal_finops_ledger_entry_id: str
    amount: str
    currency: CurrencyCode
    billing_dispute_transition_id: str
    created_at: datetime


class BillingDisputeItemInfo(BaseModel):
    """面向财务读取者的冻结费用与定价证据，不暴露原始 Usage 正文。"""

    billing_dispute_item_id: str
    finops_ledger_entry_id: str
    usage_ledger_id: str
    source_request_id: str
    source_trace_id: str
    ledger_sequence: int
    currency: CurrencyCode
    amount: str
    disputable_amount: str
    current_disputable_amount: str
    price_snapshot_id: str | None
    price_schema_version: str | None
    price_snapshot_source: str | None
    price_evidence_status: str
    created_at: datetime
    hold: BillingSettlementHoldInfo
    adjustment_link: BillingDisputeAdjustmentLinkInfo | None


class BillingDisputeTransitionInfo(BaseModel):
    """仅追加的状态迁移事实；决定理由只投影摘要，不返回审计明文。"""

    billing_dispute_transition_id: str
    action: Literal["create", "start_review", "approve", "reject", "cancel"]
    from_status: BillingDisputeStatus | None
    to_status: BillingDisputeStatus
    expected_revision: int
    new_revision: int
    actor_user_id: str
    decision_reason_sha256: str | None
    approved_item_count: int
    approved_totals_sha256: str | None
    operation_request_id: str
    operation_trace_id: str
    created_at: datetime


class BillingDisputeEventInfo(BaseModel):
    """安全的 source/outbox 状态；payload、租约和投递目标保持私有。"""

    billing_dispute_event_id: str
    event_type: Literal["billing_dispute.created"]
    delivery_status: Literal["pending", "processing", "succeeded", "failed"]
    attempt_count: int
    last_error_code: str | None
    occurred_at: datetime
    succeeded_at: datetime | None


class BillingDisputeDetail(BaseModel):
    """通过后端权限和租户校验后的完整争议视图。"""

    summary: BillingDisputeSummary
    reason: str
    decision_reason: str | None
    create_request_id: str
    create_trace_id: str
    items: list[BillingDisputeItemInfo]
    holds: list[BillingSettlementHoldInfo]
    adjustment_links: list[BillingDisputeAdjustmentLinkInfo]
    transitions: list[BillingDisputeTransitionInfo]
    event: BillingDisputeEventInfo


class BillingDisputeListResponse(BaseModel):
    """使用租户绑定签名游标的双水位列表响应。"""

    billing_disputes: list[BillingDisputeSummary]
    limit: int
    next_cursor: str | None
    has_more: bool
    request_id: str
    trace_id: str


class BillingDisputeDetailResponse(BaseModel):
    """当前争议详情响应，并携带统一 request_id 与 trace_id。"""

    dispute: BillingDisputeDetail
    request_id: str
    trace_id: str


class BillingDisputeMutationResponse(BaseModel):
    """创建或迁移结果，包含本次精确迁移及幂等重放标记。"""

    dispute: BillingDisputeDetail
    transition: BillingDisputeTransitionInfo
    adjustment_links: list[BillingDisputeAdjustmentLinkInfo]
    idempotent_replay: bool
    request_id: str
    trace_id: str


__all__ = [
    "BillingDisputeAction",
    "BillingDisputeAdjustmentLinkInfo",
    "BillingDisputeAdjustmentRequest",
    "BillingDisputeActorInfo",
    "BillingDisputeCreateRequest",
    "BillingDisputeDetail",
    "BillingDisputeDetailResponse",
    "BillingDisputeEventInfo",
    "BillingDisputeItemInfo",
    "BillingDisputeListResponse",
    "BillingDisputeMutationResponse",
    "BillingDisputePatchRequest",
    "BillingDisputeScope",
    "BillingDisputeStatus",
    "BillingDisputeSummary",
    "BillingDisputeTransitionInfo",
    "BillingSettlementHoldInfo",
]
