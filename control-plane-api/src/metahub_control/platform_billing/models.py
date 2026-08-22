"""Strict public DTOs for PRD-099 platform metering and billing summaries."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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
