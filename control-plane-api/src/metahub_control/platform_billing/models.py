"""Strict public DTOs for PRD-099 platform metering and billing summaries."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class BillingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class MeterSummaryInfo(BillingModel):
    meter_code: Literal["api_requests", "input_tokens", "output_tokens", "total_tokens"]
    unit: Literal["request", "token"]
    billing_mode: Literal["subscription_base", "informational"]
    usage_quantity: int = Field(ge=0)
    allocated_quantity: int = Field(ge=0)
    overage_quantity: int = Field(ge=0)
    adjustment_quantity: int
    currency: Literal["CNY", "USD"]
    amount: str
    adjustment_amount: str
    effective_amount: str
    summary_revision: int | None = Field(default=None, ge=1)
    summary_sha256: str | None = None


class BillingCycleInfo(BillingModel):
    billing_cycle_id: str
    workspace_id: str
    status: Literal["open", "closed", "reconciliation_required"]
    plan_version_id: str
    period_started_at: datetime
    period_ends_at: datetime
    currency: Literal["CNY", "USD"]
    source_watermark: int = Field(ge=0)
    revision: int = Field(ge=1)
    meters: tuple[MeterSummaryInfo, ...]


class WorkspaceBillingResponse(BillingModel):
    cycle: BillingCycleInfo | None = None
    available_cycles: tuple[str, ...] = ()
    request_id: str
    trace_id: str


class PlatformBillingCycleInfo(BillingCycleInfo):
    updated_at: datetime


class PlatformBillingListResponse(BillingModel):
    cycles: tuple[PlatformBillingCycleInfo, ...]
    next_cursor: str | None = None
    request_id: str
    trace_id: str


class BillingAdjustmentRequest(BillingModel):
    workspace_id: str
    billing_cycle_id: str
    meter_code: Literal["api_requests", "input_tokens", "output_tokens", "total_tokens"]
    reason_code: Literal["manual_credit", "manual_debit"]
    quantity_delta: int = Field(ge=-1_000_000_000_000, le=1_000_000_000_000)
    amount_delta: str
    expected_summary_sha256: str


class BillingAdjustmentInfo(BillingModel):
    billing_adjustment_id: str
    workspace_id: str
    billing_cycle_id: str
    meter_code: str
    reason_code: Literal["manual_credit", "manual_debit", "late_usage"]
    quantity_delta: int
    amount_delta: str
    currency: Literal["CNY", "USD"]
    created_at: datetime


class BillingAdjustmentResponse(BillingModel):
    adjustment: BillingAdjustmentInfo
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class MeterRecoveryRequest(BillingModel):
    meter_outbox_id: str = Field(pattern=r"^pmo_[0-9a-f]{32}$")
    source_sequence: int = Field(ge=1, le=9_007_199_254_740_991)
    expected_attempt_count: int = Field(ge=1, le=2_147_483_647)
    expected_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    action: Literal["retry", "internal_cost_only"]
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def require_explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("confirmation must be true")
        return value


class MeterRecoveryInfo(BillingModel):
    recovery_id: str
    source_sequence: int
    workspace_id: str
    action: Literal["retry", "internal_cost_only"]
    created_at: datetime


class MeterRecoveryResponse(BillingModel):
    recovery: MeterRecoveryInfo
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class MeterBlockedFact(BillingModel):
    meter_outbox_id: str
    source_sequence: int
    attempt_count: int
    source_sha256: str
    workspace_id: str | None
    occurred_at: datetime
    error_code: str
    can_archive_internal_cost: bool


class MeteringStateResponse(BillingModel):
    state: Literal["idle", "pending", "blocked"]
    checkpoint: int
    pending_count: int
    blocked_fact: MeterBlockedFact | None = None
    recent_recoveries: tuple[MeterRecoveryInfo, ...] = ()
    request_id: str
    trace_id: str
