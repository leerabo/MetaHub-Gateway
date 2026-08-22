"""Strict public DTOs for the SaaS commercial catalog and subscriptions."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

BillingPeriod = Literal["none", "monthly", "yearly"]
Currency = Literal["CNY", "USD"]
PlanVersionStatus = Literal["draft", "published", "retired"]
SubscriptionStatus = Literal["pending", "active", "trialing", "past_due", "canceled", "expired"]


class SaasResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EntitlementDefinition(SaasResponseModel):
    gateway_requests_per_month: int = Field(ge=0, le=1_000_000_000)
    workspace_seats: int = Field(ge=1, le=100_000)
    model_aliases: int = Field(ge=1, le=100_000)
    retention_days: int = Field(ge=1, le=3650)
    priority_support: bool


class SaasPlanVersionInfo(SaasResponseModel):
    plan_version_id: str
    version_number: int = Field(ge=1)
    status: PlanVersionStatus
    billing_period: BillingPeriod
    currency: Currency
    unit_price: str
    trial_days: int = Field(ge=0, le=90)
    entitlements: EntitlementDefinition
    entitlements_sha256: str
    published_at: datetime | None = None
    created_at: datetime


class SaasPlanCatalogInfo(SaasResponseModel):
    plan_catalog_id: str
    plan_code: str
    display_name: str
    status: Literal["draft", "active", "retired"]
    display_order: int = Field(ge=0, le=10000)
    revision: int = Field(ge=1)
    versions: tuple[SaasPlanVersionInfo, ...]


class SaasPlanCatalogResponse(SaasResponseModel):
    plans: tuple[SaasPlanCatalogInfo, ...]
    request_id: str
    trace_id: str


class SaasPlanCreateRequest(SaasResponseModel):
    plan_code: str
    display_name: str
    display_order: int = Field(ge=0, le=10000)
    billing_period: BillingPeriod
    currency: Currency
    unit_price: str
    trial_days: int = Field(ge=0, le=90)
    entitlements: EntitlementDefinition
    publish: bool


class SubscriptionCreateRequest(SaasResponseModel):
    plan_version_id: str
    start_trial: bool


class SubscriptionChangeRequest(SaasResponseModel):
    plan_version_id: str


class SubscriptionInfo(SaasResponseModel):
    subscription_id: str
    workspace_id: str
    plan_version_id: str
    pending_plan_version_id: str | None = None
    status: SubscriptionStatus
    current_period_started_at: datetime
    current_period_ends_at: datetime | None = None
    pending_effective_at: datetime | None = None
    state_revision: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime


class TrialInfo(SaasResponseModel):
    trial_id: str
    plan_version_id: str
    cycle: Literal[1] = 1
    status: Literal["active", "expired", "ended"]
    starts_at: datetime
    ends_at: datetime


class EntitlementSnapshotInfo(SaasResponseModel):
    entitlement_snapshot_id: str
    plan_version_id: str
    snapshot_version: int = Field(ge=1)
    entitlements: EntitlementDefinition
    entitlements_sha256: str
    effective_from: datetime
    effective_until: datetime | None = None


class SubscriptionOrderInfo(SaasResponseModel):
    order_record_id: str
    plan_version_id: str
    order_kind: Literal["create", "upgrade", "downgrade"]
    status: Literal["not_required", "pending_payment", "scheduled"]
    currency: Currency
    amount: str
    created_at: datetime


class SubscriptionResponse(SaasResponseModel):
    subscription: SubscriptionInfo | None = None
    trial: TrialInfo | None = None
    entitlement: EntitlementSnapshotInfo | None = None
    latest_order: SubscriptionOrderInfo | None = None
    idempotent_replay: bool = False
    request_id: str
    trace_id: str
