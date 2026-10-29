"""Strict PRD-141 Platform Growth API contracts."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

HexId = Annotated[str, Field(pattern=r"^[a-z]{3}_[0-9a-f]{32}$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
DateText = Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")]
TimestampText = Annotated[
    str,
    Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{3,6})?Z$"),
]
Stage = Literal["registered", "activated", "trial_started", "paid", "retained_30d"]
Dimension = Literal["none", "source", "channel", "region", "plan_band"]
FreshnessStatus = Literal["fresh", "stale", "partial"]


class GrowthModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SourceFreshness(GrowthModel):
    status: FreshnessStatus
    watermark: str | None
    stale_seconds: int = Field(ge=0)
    coverage_bps: int = Field(ge=0, le=10_000)


class FunnelStage(GrowthModel):
    code: Stage
    subject_count: int | None = Field(default=None, ge=0)
    event_count: int | None = Field(default=None, ge=0)
    conversion_from_previous_bps: int | None = Field(default=None, ge=0, le=10_000)


class FunnelBucket(GrowthModel):
    snapshot_id: HexId
    bucket_start: DateText
    bucket: Literal["day", "week", "month"]
    view: Literal["overview", "adoption", "retention", "legacy_operational_v1"]
    dimension: Dimension
    dimension_value: str | None
    stages: tuple[FunnelStage, FunnelStage, FunnelStage, FunnelStage, FunnelStage]
    coverage_bps: int = Field(ge=0, le=10_000)
    suppressed: bool
    suppression_code: Literal["k_anonymity", "complementary", "privacy_budget"] | None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    revenue_minor: int | None = Field(default=None, ge=0)
    cost_minor: int | None = Field(default=None, ge=0)
    contribution_minor: int | None
    generation: int = Field(ge=1)
    source_watermark: str | None


class FunnelResponse(GrowthModel):
    items: tuple[FunnelBucket, ...]
    limit: int = Field(ge=1, le=200)
    has_more: bool
    next_cursor: str | None
    formula_version: str
    schema_version: Literal["platform-growth-funnel-v1"] = "platform-growth-funnel-v1"
    source_freshness: SourceFreshness
    generated_at: TimestampText
    request_id: str
    trace_id: str


class CohortBucket(GrowthModel):
    snapshot_id: HexId
    cohort_kind: Literal["signup_week", "activation_week", "first_paid_month", "campaign"]
    cohort_bucket: DateText
    age_bucket: int = Field(ge=0, le=23)
    view: Literal["adoption", "retention"]
    dimension: Dimension
    dimension_value: str | None
    eligible_count: int | None = Field(default=None, ge=0)
    retained_count: int | None = Field(default=None, ge=0)
    churned_count: int | None = Field(default=None, ge=0)
    retention_bps: int | None = Field(default=None, ge=0, le=10_000)
    coverage_bps: int = Field(ge=0, le=10_000)
    suppressed: bool
    suppression_code: Literal["k_anonymity", "complementary", "privacy_budget"] | None
    generation: int = Field(ge=1)
    supersedes_snapshot_id: HexId | None
    source_watermark: str | None


class CohortResponse(GrowthModel):
    items: tuple[CohortBucket, ...]
    limit: int = Field(ge=1, le=200)
    has_more: bool
    next_cursor: str | None
    formula_version: str
    schema_version: Literal["platform-growth-cohort-v1"] = "platform-growth-cohort-v1"
    source_freshness: SourceFreshness
    generated_at: TimestampText
    request_id: str
    trace_id: str


class CampaignSummary(GrowthModel):
    campaign_id: HexId
    display_name: str
    acquisition_source_id: HexId
    channel_code: str
    target_stage: Stage
    starts_at: TimestampText
    ends_at: TimestampText
    budget_minor: int = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    status: Literal["draft", "scheduled", "active", "paused", "completed", "archived"]
    revision: int = Field(ge=1)
    conversion_count: int = Field(ge=0)
    spend_minor: int = Field(ge=0)
    outcome_bps: int | None = Field(default=None, ge=0, le=10_000)
    action_capabilities: tuple[str, ...]
    updated_at: TimestampText


class CampaignListResponse(GrowthModel):
    items: tuple[CampaignSummary, ...]
    limit: int = Field(ge=1, le=100)
    has_more: bool
    next_cursor: str | None
    schema_version: Literal["platform-growth-campaign-v1"] = "platform-growth-campaign-v1"
    source_freshness: SourceFreshness
    generated_at: TimestampText
    request_id: str
    trace_id: str


class CampaignCommand(GrowthModel):
    operation: Literal["create", "update", "schedule", "pause", "resume", "complete", "archive"]
    campaign_id: HexId | None = None
    expected_revision: int = Field(ge=0)
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    acquisition_source_id: HexId | None = None
    channel_code: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9._-]{0,39}$")
    target_stage: Stage | None = None
    starts_at: TimestampText | None = None
    ends_at: TimestampText | None = None
    budget_minor: int | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    confirmed: bool

    @model_validator(mode="after")
    def exact_operation(self) -> CampaignCommand:
        config_values = (
            self.display_name,
            self.acquisition_source_id,
            self.channel_code,
            self.target_stage,
            self.starts_at,
            self.ends_at,
            self.budget_minor,
            self.currency,
        )
        if not self.confirmed:
            raise ValueError("confirmation required")
        if self.operation == "create":
            if self.campaign_id is not None or self.expected_revision != 0 or any(
                value is None for value in config_values
            ):
                raise ValueError("invalid create")
        elif self.operation == "update":
            if self.campaign_id is None or self.expected_revision < 1 or any(
                value is None for value in config_values
            ):
                raise ValueError("invalid update")
        elif (
            self.campaign_id is None
            or self.expected_revision < 1
            or any(value is not None for value in config_values)
        ):
            raise ValueError("invalid transition")
        return self


class CampaignResponse(GrowthModel):
    campaign: CampaignSummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class ExperimentSummary(GrowthModel):
    experiment_id: HexId
    hypothesis_code: str
    surface_code: str
    primary_metric: str
    control_code: str
    variant_code: str
    control_allocation_bps: int = Field(ge=1, le=9999)
    variant_allocation_bps: int = Field(ge=1, le=9999)
    eligibility_policy_version: str
    consent_version: str
    sample_cap: int = Field(ge=20)
    starts_at: TimestampText
    ends_at: TimestampText
    guardrail_code: str
    decision_policy_version: str
    status: Literal[
        "draft", "scheduled", "running", "paused", "decided", "stopped", "canceled", "archived"
    ]
    decision: Literal[
        "winner_control", "winner_variant", "inconclusive", "stopped_guardrail", "canceled"
    ] | None
    assigned_count: int = Field(ge=0)
    control_count: int = Field(ge=0)
    variant_count: int = Field(ge=0)
    revision: int = Field(ge=1)
    action_capabilities: tuple[str, ...]
    updated_at: TimestampText


class ExperimentListResponse(GrowthModel):
    items: tuple[ExperimentSummary, ...]
    limit: int = Field(ge=1, le=100)
    has_more: bool
    next_cursor: str | None
    schema_version: Literal["platform-growth-experiment-v1"] = "platform-growth-experiment-v1"
    source_freshness: SourceFreshness
    generated_at: TimestampText
    request_id: str
    trace_id: str


class ExperimentCommand(GrowthModel):
    operation: Literal[
        "create", "update", "schedule", "start", "pause", "resume", "stop", "decide", "archive"
    ]
    experiment_id: HexId | None = None
    expected_revision: int = Field(ge=0)
    hypothesis_code: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9._-]{2,79}$")
    surface_code: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9._-]{2,79}$")
    primary_metric: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9._-]{2,79}$")
    control_code: str | None = Field(default=None, min_length=1, max_length=40)
    variant_code: str | None = Field(default=None, min_length=1, max_length=40)
    control_allocation_bps: int | None = Field(default=None, ge=1, le=9999)
    variant_allocation_bps: int | None = Field(default=None, ge=1, le=9999)
    eligibility_policy_version: str | None = Field(default=None, min_length=1, max_length=80)
    consent_version: str | None = Field(default=None, min_length=1, max_length=80)
    sample_cap: int | None = Field(default=None, ge=20, le=10_000_000)
    starts_at: TimestampText | None = None
    ends_at: TimestampText | None = None
    guardrail_code: str | None = Field(default=None, min_length=3, max_length=80)
    decision_policy_version: str | None = Field(default=None, min_length=1, max_length=80)
    source_watermark: str | None = Field(default=None, min_length=1, max_length=128)
    confirmed: bool

    @model_validator(mode="after")
    def exact_operation(self) -> ExperimentCommand:
        config_values = (
            self.hypothesis_code,
            self.surface_code,
            self.primary_metric,
            self.control_code,
            self.variant_code,
            self.control_allocation_bps,
            self.variant_allocation_bps,
            self.eligibility_policy_version,
            self.consent_version,
            self.sample_cap,
            self.starts_at,
            self.ends_at,
            self.guardrail_code,
            self.decision_policy_version,
        )
        if not self.confirmed:
            raise ValueError("confirmation required")
        if self.operation == "create":
            if self.experiment_id is not None or self.expected_revision != 0 or any(
                value is None for value in config_values
            ):
                raise ValueError("invalid create")
        elif self.operation == "update":
            if self.experiment_id is None or self.expected_revision < 1 or any(
                value is None for value in config_values
            ):
                raise ValueError("invalid update")
        elif (
            self.experiment_id is None
            or self.expected_revision < 1
            or any(value is not None for value in config_values)
        ):
            raise ValueError("invalid transition")
        if self.operation in {"start", "decide"} and self.source_watermark is None:
            raise ValueError("source watermark required")
        if self.operation not in {"start", "decide"} and self.source_watermark is not None:
            raise ValueError("unexpected source watermark")
        return self


class ExperimentResponse(GrowthModel):
    experiment: ExperimentSummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class AttributionSummary(GrowthModel):
    safe_id: str
    source_id: HexId | None
    campaign_id: HexId | None
    channel_partner_id: HexId | None
    conversion_stage: Stage
    conversion_count: int | None = Field(default=None, ge=0)
    value_minor: int | None = Field(default=None, ge=0)
    cost_minor: int | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    conversion_bps: int | None = Field(default=None, ge=0, le=10_000)
    coverage_bps: int = Field(ge=0, le=10_000)
    suppressed: bool
    suppression_code: Literal["k_anonymity", "complementary", "privacy_budget"] | None
    source_watermark: str | None


class AttributionResponse(GrowthModel):
    view: Literal["summary", "campaign", "channel", "channel_summary"]
    items: tuple[AttributionSummary, ...]
    limit: int = Field(ge=1, le=200)
    has_more: bool
    next_cursor: str | None
    formula_version: Literal["last-eligible-touch-30d-v1"] = "last-eligible-touch-30d-v1"
    schema_version: Literal["platform-growth-attribution-v1"] = "platform-growth-attribution-v1"
    source_freshness: SourceFreshness
    generated_at: TimestampText
    request_id: str
    trace_id: str


class GrowthExportRequest(GrowthModel):
    query_kind: Literal["funnels", "cohorts", "campaigns", "experiments", "attribution"]
    view: Literal[
        "overview", "adoption", "retention", "summary", "campaign", "channel", "channel_summary"
    ]
    from_date: DateText
    to_date: DateText
    bucket: Literal["day", "week", "month"] | None = None
    dimension: Dimension = "none"
    formula_version: str = Field(min_length=1, max_length=80)
    sensitivity: Literal["aggregate", "high_cardinality"]
    confirmed: bool

    @model_validator(mode="after")
    def check_manifest(self) -> GrowthExportRequest:
        if not self.confirmed or self.from_date > self.to_date:
            raise ValueError("invalid export manifest")
        if self.query_kind == "funnels" and self.bucket is None:
            raise ValueError("funnel bucket required")
        if self.query_kind != "funnels" and self.bucket is not None:
            raise ValueError("unexpected bucket")
        return self


class GrowthExportApprovalCommand(GrowthModel):
    operation: Literal["approve", "reject"]
    expected_revision: int = Field(ge=1)
    reason_code: Literal["policy_approved", "policy_rejected"]
    confirmed: bool

    @model_validator(mode="after")
    def check_decision(self) -> GrowthExportApprovalCommand:
        expected = {"approve": "policy_approved", "reject": "policy_rejected"}[self.operation]
        if not self.confirmed or self.reason_code != expected:
            raise ValueError("invalid approval")
        return self


class GrowthExportSummary(GrowthModel):
    export_id: HexId
    query_kind: str
    sensitivity: Literal["aggregate", "high_cardinality"]
    status: Literal[
        "requested", "policy_review", "approved", "rejected", "building", "ready",
        "downloaded", "expired", "revoked", "failed"
    ]
    revision: int = Field(ge=1)
    row_count: int | None = Field(default=None, ge=0)
    cell_count: int | None = Field(default=None, ge=0)
    expires_at: TimestampText | None
    download_count: int = Field(ge=0, le=1)
    created_at: TimestampText
    updated_at: TimestampText


class GrowthExportResponse(GrowthModel):
    export: GrowthExportSummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class GrowthExportReceiptResponse(GrowthModel):
    export_id: HexId
    receipt: str = Field(min_length=32, max_length=256, repr=False)
    expires_at: TimestampText
    request_id: str
    trace_id: str


class GrowthExportReceiptCommand(GrowthModel):
    confirmed: Literal[True]


__all__ = [name for name in globals() if not name.startswith("_")]
