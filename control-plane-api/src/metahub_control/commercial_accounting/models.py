"""Strict Platform and Workspace commercial accounting DTOs for PRD-144."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from metahub_control.saas_billing.models import EntitlementDefinition


class CommercialAccountingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


Currency = Literal["CNY", "USD"]
BillingPeriod = Literal["none", "monthly", "yearly"]
ProjectionState = Literal[
    "current", "partial", "stale", "reconciliation_required", "unknown"
]


class ProviderReferenceRateInfo(CommercialAccountingModel):
    provider_reference_rate_release_id: str
    provider_id: str
    model_id: str
    currency: Currency
    input_rate_micros_per_1k: int = Field(ge=0)
    output_rate_micros_per_1k: int = Field(ge=0)
    cache_rate_micros_per_1k: int = Field(ge=0)
    effective_from: datetime
    effective_until: datetime | None = None
    source_version: str
    content_sha256: str
    status: Literal["candidate", "released", "retired"]
    revision: int = Field(ge=1)
    release_generation: int = Field(ge=0)
    head_revision: int | None = Field(default=None, ge=1)
    is_head: bool
    actions: tuple[str, ...]
    created_at: datetime
    updated_at: datetime
    released_at: datetime | None = None


class PlatformPlanReleaseInfo(CommercialAccountingModel):
    platform_plan_catalog_release_id: str
    plan_catalog_id: str
    plan_version_id: str
    plan_code: str
    display_name: str
    billing_period: BillingPeriod
    currency: Currency
    unit_price_minor: int = Field(ge=0)
    entitlements_sha256: str
    contract_sha256: str
    status: Literal[
        "legacy_reconciled", "candidate", "review_required", "released", "retired"
    ]
    revision: int = Field(ge=1)
    release_generation: int = Field(ge=0)
    head_revision: int | None = Field(default=None, ge=1)
    is_head: bool
    readiness: Literal["legacy", "candidate", "review", "released", "retired"]
    actions: tuple[str, ...]
    created_at: datetime
    updated_at: datetime
    released_at: datetime | None = None


class PlatformPlanListResponse(CommercialAccountingModel):
    plans: tuple[PlatformPlanReleaseInfo, ...]
    reference_rates: tuple[ProviderReferenceRateInfo, ...]
    next_cursor: str | None = None
    limit: int = Field(ge=1, le=100)
    permission_fingerprint: str
    request_id: str
    trace_id: str


class PlatformPlanCommandRequest(CommercialAccountingModel):
    action: Literal["create_candidate", "submit_review", "release", "retire"]
    plan_code: str | None = Field(
        default=None, min_length=2, max_length=32, pattern=r"^[a-z][a-z0-9-]+$"
    )
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    display_order: StrictInt | None = Field(default=None, ge=0, le=10_000)
    billing_period: BillingPeriod | None = None
    currency: Currency | None = None
    unit_price_minor: StrictInt | None = Field(
        default=None, ge=0, le=1_000_000_000_000
    )
    trial_days: StrictInt | None = Field(default=None, ge=0, le=90)
    entitlements: EntitlementDefinition | None = None
    plan_version_id: str | None = None
    governance_approval_id: str | None = None

    @model_validator(mode="after")
    def exact_action_shape(self) -> PlatformPlanCommandRequest:
        supplied = self.model_fields_set - {"action"}
        if self.action == "create_candidate" and supplied == {
            "plan_code",
            "display_name",
            "display_order",
            "billing_period",
            "currency",
            "unit_price_minor",
            "trial_days",
            "entitlements",
        } and all(
            value is not None
            for value in (
                self.plan_code,
                self.display_name,
                self.display_order,
                self.billing_period,
                self.currency,
                self.unit_price_minor,
                self.trial_days,
                self.entitlements,
            )
        ):
            if self.display_name != self.display_name.strip():
                raise ValueError("invalid display name")
            return self
        if (
            self.action in {"submit_review", "retire"}
            and supplied == {"plan_version_id"}
            and self.plan_version_id is not None
        ):
            return self
        if self.action == "release" and supplied == {
            "plan_version_id",
            "governance_approval_id",
        } and self.plan_version_id is not None and self.governance_approval_id is not None:
            return self
        raise ValueError("invalid platform plan command shape")


class PlatformPlanCommandResponse(CommercialAccountingModel):
    plan: PlatformPlanReleaseInfo
    command_id: str
    idempotent_replay: bool = False
    permission_fingerprint: str
    request_id: str
    trace_id: str


class PlatformInvoiceInfo(CommercialAccountingModel):
    platform_invoice_id: str
    currency: Currency
    subtotal_minor: int
    tax_rate_basis_points: int = Field(ge=0, le=10_000)
    tax_minor: int
    total_minor: int
    status: Literal[
        "requested",
        "processing",
        "issued",
        "failed",
        "reversal_pending",
        "reversed",
        "replaced",
    ]
    invoice_number_masked: str | None = None
    artifact_manifest_sha256: str | None = None
    source_revision: int = Field(ge=1)
    generation: int = Field(ge=1)
    updated_at: datetime


class BillingDisputeInfo(CommercialAccountingModel):
    billing_dispute_case_id: str
    source_kind: Literal["statement", "invoice", "statement_line"]
    source_ref: str
    currency: Currency
    disputed_amount_minor: int = Field(gt=0)
    reason_code: Literal[
        "amount_mismatch", "duplicate_charge", "service_not_delivered", "tax_mismatch"
    ]
    status: Literal["submitted", "under_review", "approved", "rejected", "withdrawn"]
    source_revision: int = Field(ge=1)
    state_revision: int = Field(ge=1)
    decision_code: Literal[
        "facts_confirmed", "duplicate_confirmed", "service_credit", "claim_not_supported"
    ] | None = None
    credited_amount_minor: int = Field(default=0, ge=0)
    actions: tuple[str, ...]
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None


class SubscriptionProjectionInfo(CommercialAccountingModel):
    workspace_customer_ref: str
    subscription_id: str
    plan_catalog_id: str
    plan_version_id: str
    platform_plan_catalog_release_id: str | None = None
    plan_code: str
    display_name: str
    subscription_status: str
    current_period_started_at: datetime
    current_period_ends_at: datetime | None = None
    entitlement_sha256: str
    currency: Currency
    current_due_minor: int | None = None
    paid_minor: int | None = None
    refunded_minor: int | None = None
    invoiced_minor: int | None = None
    order_status: str
    payment_status: str
    invoice_status: str
    dispute_status: str
    projection_state: ProjectionState
    source_watermark: int = Field(ge=0)
    source_sha256: str
    revision: int = Field(ge=1)
    projection_generation: int = Field(ge=1)
    invoices: tuple[PlatformInvoiceInfo, ...] = ()
    disputes: tuple[BillingDisputeInfo, ...] = ()
    actions: tuple[str, ...] = ()
    projected_at: datetime


class PlatformSubscriptionListResponse(CommercialAccountingModel):
    subscriptions: tuple[SubscriptionProjectionInfo, ...]
    next_cursor: str | None = None
    limit: int = Field(ge=1, le=100)
    permission_fingerprint: str
    request_id: str
    trace_id: str


class WorkspaceSubscriptionResponse(CommercialAccountingModel):
    subscription: SubscriptionProjectionInfo | None = None
    permission_revision: int = Field(ge=1)
    permission_fingerprint: str
    request_id: str
    trace_id: str


class BillingDisputeCreateRequest(CommercialAccountingModel):
    source_kind: Literal["statement", "invoice", "statement_line"]
    source_id: str
    source_line_id: str | None = None
    reason_code: Literal[
        "amount_mismatch", "duplicate_charge", "service_not_delivered", "tax_mismatch"
    ]

    @model_validator(mode="after")
    def exact_source_shape(self) -> BillingDisputeCreateRequest:
        has_line = "source_line_id" in self.model_fields_set
        if self.source_kind == "statement_line" and has_line and self.source_line_id:
            return self
        if self.source_kind != "statement_line" and not has_line:
            return self
        raise ValueError("invalid dispute source shape")


class BillingDisputeDecisionRequest(CommercialAccountingModel):
    action: Literal["start_review", "approve_credit", "reject"]
    decision_code: Literal[
        "facts_confirmed", "duplicate_confirmed", "service_credit", "claim_not_supported"
    ] | None = None
    expected_source_revision: StrictInt = Field(ge=1)

    @model_validator(mode="after")
    def exact_decision_shape(self) -> BillingDisputeDecisionRequest:
        supplied = self.model_fields_set
        if self.action == "start_review" and supplied == {
            "action",
            "expected_source_revision",
        }:
            return self
        if self.action == "approve_credit" and self.decision_code in {
            "facts_confirmed",
            "duplicate_confirmed",
            "service_credit",
        }:
            return self
        if self.action == "reject" and self.decision_code == "claim_not_supported":
            return self
        raise ValueError("invalid dispute decision shape")


class BillingDisputeCommandResponse(CommercialAccountingModel):
    dispute: BillingDisputeInfo
    command_id: str
    adjustment_id: str | None = None
    idempotent_replay: bool = False
    permission_revision: int = Field(ge=1)
    permission_fingerprint: str
    request_id: str
    trace_id: str


class ProviderCostRateInfo(CommercialAccountingModel):
    workspace_provider_cost_rate_id: str
    workspace_provider_cost_rate_version_id: str
    provider_id: str
    provider_code: str
    provider_name: str
    model_id: str
    model_alias: str
    currency: Currency
    version_number: int = Field(ge=1)
    input_rate_micros_per_1k: int = Field(ge=0)
    output_rate_micros_per_1k: int = Field(ge=0)
    cache_rate_micros_per_1k: int = Field(ge=0)
    effective_from: datetime
    effective_until: datetime | None = None
    source_type: Literal[
        "manual", "platform_reference", "legacy_model_price", "legacy_model"
    ]
    source_ref: str
    status: Literal["active", "superseded", "disabled"]
    revision: int = Field(ge=1)
    head_revision: int = Field(ge=1)
    adoption_state: Literal["active", "disabled"]
    content_sha256: str
    actions: tuple[str, ...]
    created_at: datetime


class WorkspaceProviderCostRateListResponse(CommercialAccountingModel):
    rates: tuple[ProviderCostRateInfo, ...]
    next_cursor: str | None = None
    limit: int = Field(ge=1, le=100)
    permission_revision: int = Field(ge=1)
    permission_fingerprint: str
    request_id: str
    trace_id: str


class ProviderCostRateCommandRequest(CommercialAccountingModel):
    action: Literal["create_version", "adopt_reference", "disable"]
    provider_id: str | None = None
    model_id: str | None = None
    currency: Currency | None = None
    input_rate_micros_per_1k: StrictInt | None = Field(default=None, ge=0)
    output_rate_micros_per_1k: StrictInt | None = Field(default=None, ge=0)
    cache_rate_micros_per_1k: StrictInt | None = Field(default=None, ge=0)
    effective_from: datetime | None = None
    effective_until: datetime | None = None
    provider_reference_rate_release_id: str | None = None
    workspace_provider_cost_rate_id: str | None = None

    @model_validator(mode="after")
    def exact_action_shape(self) -> ProviderCostRateCommandRequest:
        supplied = self.model_fields_set - {"action"}
        interval = {"effective_until"} if "effective_until" in supplied else set()
        if self.action == "create_version" and supplied == {
            "provider_id",
            "model_id",
            "currency",
            "input_rate_micros_per_1k",
            "output_rate_micros_per_1k",
            "cache_rate_micros_per_1k",
            "effective_from",
            *interval,
        } and all(
            value is not None
            for value in (
                self.provider_id,
                self.model_id,
                self.currency,
                self.input_rate_micros_per_1k,
                self.output_rate_micros_per_1k,
                self.cache_rate_micros_per_1k,
                self.effective_from,
            )
        ):
            return self._valid_interval()
        if self.action == "adopt_reference" and supplied == {
            "provider_id",
            "model_id",
            "currency",
            "provider_reference_rate_release_id",
        } and all(
            value is not None
            for value in (
                self.provider_id,
                self.model_id,
                self.currency,
                self.provider_reference_rate_release_id,
            )
        ):
            return self
        if (
            self.action == "disable"
            and supplied == {"workspace_provider_cost_rate_id"}
            and self.workspace_provider_cost_rate_id is not None
        ):
            return self
        raise ValueError("invalid provider cost command shape")

    def _valid_interval(self) -> ProviderCostRateCommandRequest:
        if self.effective_until is not None and self.effective_from is not None:
            if self.effective_until <= self.effective_from:
                raise ValueError("invalid effective interval")
        return self


class ProviderCostRateCommandResponse(CommercialAccountingModel):
    rate: ProviderCostRateInfo
    command_id: str
    idempotent_replay: bool = False
    permission_revision: int = Field(ge=1)
    permission_fingerprint: str
    request_id: str
    trace_id: str


class WorkspaceFxRateInfo(CommercialAccountingModel):
    workspace_fx_rate_version_id: str
    workspace_id: str
    tenant_id: str
    base_currency: Literal["USD"]
    quote_currency: Literal["CNY"]
    usd_to_cny: str
    version_number: int = Field(ge=1)
    effective_from: datetime
    status: Literal["active", "superseded"]
    revision: int = Field(ge=1)
    actions: tuple[str, ...]
    created_at: datetime


class WorkspaceFxRateListResponse(CommercialAccountingModel):
    rates: tuple[WorkspaceFxRateInfo, ...]
    next_cursor: str | None = None
    limit: int = Field(ge=1, le=100)
    permission_revision: int = Field(ge=1)
    permission_fingerprint: str
    request_id: str
    trace_id: str


class WorkspaceFxRateCommandRequest(CommercialAccountingModel):
    action: Literal["create_version"]
    usd_to_cny: str = Field(min_length=1, max_length=24, pattern=r"^[0-9]+(\.[0-9]+)?$")
    effective_from: datetime


class WorkspaceFxRateCommandResponse(CommercialAccountingModel):
    rate: WorkspaceFxRateInfo
    command_id: str
    idempotent_replay: bool = False
    permission_revision: int = Field(ge=1)
    permission_fingerprint: str
    request_id: str
    trace_id: str


class ProviderReferenceRateCommandRequest(CommercialAccountingModel):
    action: Literal["create_candidate", "release", "retire"]
    model_id: str | None = None
    currency: Currency | None = None
    input_rate_micros_per_1k: StrictInt | None = Field(default=None, ge=0)
    output_rate_micros_per_1k: StrictInt | None = Field(default=None, ge=0)
    cache_rate_micros_per_1k: StrictInt | None = Field(default=None, ge=0)
    effective_from: datetime | None = None
    effective_until: datetime | None = None
    source_version: str | None = None
    provider_reference_rate_release_id: str | None = None
    governance_approval_id: str | None = None

    @model_validator(mode="after")
    def exact_action_shape(self) -> ProviderReferenceRateCommandRequest:
        supplied = self.model_fields_set - {"action"}
        interval = {"effective_until"} if "effective_until" in supplied else set()
        if self.action == "create_candidate" and supplied == {
            "model_id",
            "currency",
            "input_rate_micros_per_1k",
            "output_rate_micros_per_1k",
            "cache_rate_micros_per_1k",
            "effective_from",
            "source_version",
            *interval,
        } and all(
            value is not None
            for value in (
                self.model_id,
                self.currency,
                self.input_rate_micros_per_1k,
                self.output_rate_micros_per_1k,
                self.cache_rate_micros_per_1k,
                self.effective_from,
                self.source_version,
            )
        ):
            if self.effective_until is not None and self.effective_from is not None:
                if self.effective_until <= self.effective_from:
                    raise ValueError("invalid effective interval")
            return self
        if self.action == "release" and supplied == {
            "provider_reference_rate_release_id",
            "governance_approval_id",
        } and all(
            value is not None
            for value in (
                self.provider_reference_rate_release_id,
                self.governance_approval_id,
            )
        ):
            return self
        if self.action == "retire" and supplied == {
            "provider_reference_rate_release_id"
        } and self.provider_reference_rate_release_id is not None:
            return self
        raise ValueError("invalid provider reference command shape")


class ProviderReferenceRateCommandResponse(CommercialAccountingModel):
    rate: ProviderReferenceRateInfo
    command_id: str
    idempotent_replay: bool = False
    permission_fingerprint: str
    request_id: str
    trace_id: str


__all__ = [name for name in globals() if not name.startswith("_")]
