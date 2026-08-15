"""FinOps 正式账本 API 的只读与调整数据合同。

本模块只描述可向当前租户管理员公开的财务字段。金额始终使用六位小数文本，避免
JSON ``float`` 破坏微元精度；Prompt、Response、密钥、原始 Usage 和内部哈希均不得
进入这些 DTO。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CurrencyCode = Literal["CNY", "USD"]
DirectionCode = Literal["debit", "credit"]
NormalizationStatus = Literal["normalized", "unavailable"]
PriceEvidenceStatus = Literal[
    "priced", "unpriced", "legacy_amount_only", "not_applicable"
]

# 正式分录类型包含来源 Usage 和五种只追加的管理员调整。
EntryTypeCode = Literal[
    "usage_charge",
    "manual_debit",
    "manual_credit",
    "reversal",
    "gift_credit",
    "trial_credit",
]
# 管理员调整不接受 usage_charge，来源用量只能由计费链路入账。
AdjustmentTypeCode = Literal[
    "manual_debit",
    "manual_credit",
    "reversal",
    "gift_credit",
    "trial_credit",
]


class FinOpsAdjustmentRequest(BaseModel):
    """OpenAPI 展示用调整请求；运行时仍由认证后的严格解析器处理正文。"""

    model_config = ConfigDict(extra="forbid")

    adjustment_type: AdjustmentTypeCode = Field(description="调整类型；冲正使用 reversal。")
    amount: str = Field(description="正数普通十进制金额字符串，最多六位小数。")
    reversal_of_entry_id: str | None = Field(
        default=None,
        description="冲正目标正式分录 ID；仅 reversal 必填。",
    )
    related_request_id: str | None = Field(
        default=None,
        description="可选的同租户已计费 request_id；reversal 禁止提交。",
    )
    reason: str = Field(description="调整原因，规范化后为 1 至 500 个 UTF-8 字节。")


class FinOpsActorInfo(BaseModel):
    """当前租户内可展示的最小操作者身份，不包含 Session 或认证信息。"""

    user_id: str
    display_name: str | None
    email: str


class FinOpsOutputSafetyDisputeInfo(BaseModel):
    """FinOps 分录关联的输出安全复核徽标。

    FinanceAdmin 只需要知道哪次扫描触发待复核以及是否具备人工冲正资格；规则、
    扫描模式、暴露计数和任何正文都不属于财务读权限的公开合同。
    """

    output_safety_scan_id: str
    review_status: Literal["dispute_pending"]
    reversal_eligible: bool


class FinOpsBillingDisputeHoldInfo(BaseModel):
    """Minimal active settlement hold overlay for a formal charge."""

    billing_settlement_hold_id: str
    billing_dispute_id: str
    billing_dispute_item_id: str
    status: Literal["active"]


class FinOpsLedgerEntryInfo(BaseModel):
    """一条不可变正式分录的安全展示对象。"""

    finops_ledger_entry_id: str
    tenant_id: str
    entry_type: EntryTypeCode
    direction: DirectionCode
    amount: str
    currency: CurrencyCode
    normalized_cny: str | None
    cny_normalization_status: NormalizationStatus
    price_evidence_status: PriceEvidenceStatus
    price_snapshot_source: str | None
    posting_source: Literal["direct", "spool_replay", "legacy_backfill"]
    quota_type: Literal["paid", "free", "trial", "gift"]
    quota_allocation_class: Literal[
        "paid", "free", "trial", "gift", "mixed", "unallocated", "not_applicable"
    ]
    quota_allocation_count: int
    usage_ledger_id: str | None
    finops_adjustment_id: str | None
    source_request_id: str | None
    source_trace_id: str | None
    operation_request_id: str
    operation_trace_id: str
    price_snapshot_id: str | None
    price_schema_version: str | None
    reversal_of_entry_id: str | None
    actor: FinOpsActorInfo | None
    reason: str | None
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_tokens: int
    model_id: str | None
    provider_id: str | None
    virtual_key_id: str | None
    environment_id: str | None
    project_id: str | None
    application_id: str | None
    estimated: bool
    status: Literal["posted"]
    created_at: datetime
    output_safety_dispute: FinOpsOutputSafetyDisputeInfo | None = None
    billing_dispute_hold: FinOpsBillingDisputeHoldInfo | None = None


class FinOpsCurrencyTotals(BaseModel):
    """单一原币下的借、贷和净额汇总，全部保持精确十进制文本。"""

    debit: str
    credit: str
    net: str


class FinOpsLedgerTotals(BaseModel):
    """与列表相同快照和筛选条件下的账本汇总。"""

    by_currency: dict[str, FinOpsCurrencyTotals]
    net_cost_cny: str
    unnormalized_entry_count: int
    entry_count: int


class FinOpsLedgerListResponse(BaseModel):
    """签名快照游标分页响应，并统一携带请求链路标识。"""

    ledger_entries: list[FinOpsLedgerEntryInfo]
    totals: FinOpsLedgerTotals
    total: int
    limit: int
    next_cursor: str | None
    has_more: bool
    request_id: str
    trace_id: str


class FinOpsAdjustmentInfo(BaseModel):
    """人工调整的不可变业务事实；内部幂等摘要和原因摘要不对外返回。"""

    finops_adjustment_id: str
    tenant_id: str
    adjustment_type: AdjustmentTypeCode
    amount: str
    currency: CurrencyCode
    reversal_of_entry_id: str | None
    related_request_id: str | None
    reason: str
    actor_user_id: str
    operation_request_id: str
    operation_trace_id: str
    status: Literal["posted"]
    created_at: datetime


class FinOpsAdjustmentResponse(BaseModel):
    """调整创建或幂等恢复响应；重放不会再次产生审计或 Webhook。"""

    adjustment: FinOpsAdjustmentInfo
    ledger_entry: FinOpsLedgerEntryInfo
    idempotent_replay: bool
    request_id: str
    trace_id: str


__all__ = [
    "AdjustmentTypeCode",
    "CurrencyCode",
    "DirectionCode",
    "EntryTypeCode",
    "FinOpsActorInfo",
    "FinOpsAdjustmentInfo",
    "FinOpsAdjustmentRequest",
    "FinOpsAdjustmentResponse",
    "FinOpsBillingDisputeHoldInfo",
    "FinOpsCurrencyTotals",
    "FinOpsLedgerEntryInfo",
    "FinOpsLedgerListResponse",
    "FinOpsLedgerTotals",
    "FinOpsOutputSafetyDisputeInfo",
    "NormalizationStatus",
    "PriceEvidenceStatus",
]
