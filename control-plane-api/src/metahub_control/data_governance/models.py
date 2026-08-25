"""Strict public DTOs for PRD-117 data governance and low-sensitivity evidence."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ClassificationCode = Literal["prompt", "response", "file", "audit", "identity", "billing"]
DeletionMode = Literal["hard_delete", "archive_then_delete", "anonymize"]


class DataGovernanceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RetentionPolicyInput(DataGovernanceModel):
    classification_code: ClassificationCode
    retention_days: int = Field(ge=1, le=3650)
    deletion_mode: DeletionMode
    backup_grace_days: int = Field(ge=0, le=365)


class DataGovernanceUpdateRequest(DataGovernanceModel):
    allowed_residency_groups: list[str] = Field(min_length=1, max_length=16)
    primary_residency_group: str = Field(
        min_length=3, max_length=40, pattern=r"^[a-z0-9-]+$"
    )
    cross_region_mode: Literal["deny", "same_group"]
    retention_policies: list[RetentionPolicyInput] = Field(min_length=6, max_length=6)
    reason_code: Literal[
        "compliance_update",
        "regional_expansion",
        "regional_restriction",
        "retention_update",
    ]
    confirmed: bool

    @model_validator(mode="after")
    def complete_policy(self) -> DataGovernanceUpdateRequest:
        groups = self.allowed_residency_groups
        if groups != sorted(set(groups)) or self.primary_residency_group not in groups:
            raise ValueError("residency groups must be unique, sorted, and include primary")
        codes = [item.classification_code for item in self.retention_policies]
        if codes != sorted(codes) or set(codes) != {
            "prompt",
            "response",
            "file",
            "audit",
            "identity",
            "billing",
        }:
            raise ValueError("retention policies must contain the complete sorted catalog")
        return self


class LegalHoldCreateRequest(DataGovernanceModel):
    classification_code: ClassificationCode
    scope_kind: Literal["classification", "object_digest"]
    selector_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reason_code: Literal[
        "litigation",
        "regulatory_inquiry",
        "internal_investigation",
        "preservation_notice",
    ]
    expires_at: datetime | None = None
    confirmed: Literal[True]

    @model_validator(mode="after")
    def exact_scope(self) -> LegalHoldCreateRequest:
        if (self.scope_kind == "classification") != (self.selector_sha256 is None):
            raise ValueError("hold selector does not match scope")
        return self


class LegalHoldReleaseRequest(DataGovernanceModel):
    reason_code: Literal["legal_instruction", "matter_closed"]
    confirmed: Literal[True]


class DataClassificationSummary(DataGovernanceModel):
    data_classification_id: str = Field(pattern=r"^dcl_[0-9a-f]{32}$")
    code: ClassificationCode
    sensitivity_level: Literal["restricted", "confidential", "internal"]
    allowed_purposes: tuple[str, ...]
    default_retention_days: int = Field(ge=1, le=3650)


class ResidencyPolicySummary(DataGovernanceModel):
    residency_policy_id: str = Field(pattern=r"^rgp_[0-9a-f]{32}$")
    allowed_residency_groups: tuple[str, ...]
    primary_residency_group: str
    cross_region_mode: Literal["deny", "same_group"]
    revision: int = Field(ge=1)
    generation: int = Field(ge=1)
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    valid_until: datetime
    updated_at: datetime


class RetentionPolicySummary(DataGovernanceModel):
    retention_policy_id: str = Field(pattern=r"^rtp_[0-9a-f]{32}$")
    classification_code: ClassificationCode
    retention_days: int = Field(ge=1, le=3650)
    deletion_mode: DeletionMode
    backup_grace_days: int = Field(ge=0, le=365)
    revision: int = Field(ge=1)
    generation: int = Field(ge=1)


class LegalHoldSummary(DataGovernanceModel):
    legal_hold_id: str = Field(pattern=r"^lgh_[0-9a-f]{32}$")
    classification_code: ClassificationCode
    scope_kind: Literal["classification", "object_digest"]
    selector_configured: bool
    reason_code: Literal[
        "litigation",
        "regulatory_inquiry",
        "internal_investigation",
        "preservation_notice",
    ]
    status: Literal["active", "released", "expired"]
    revision: int = Field(ge=1)
    created_hold_generation: int = Field(ge=1)
    expires_at: datetime | None
    released_at: datetime | None
    created_at: datetime
    updated_at: datetime


class DeletionScheduleSummary(DataGovernanceModel):
    deletion_schedule_id: str = Field(pattern=r"^dgs_[0-9a-f]{32}$")
    classification_code: ClassificationCode
    due_at: datetime
    status: Literal["queued", "running", "blocked", "completed", "failed"]
    policy_generation: int = Field(ge=1)
    hold_generation: int = Field(ge=0)
    checkpoint_code: str
    deleted_count: int = Field(ge=0)
    retained_count: int = Field(ge=0)
    safe_outcome_code: str | None
    revision: int = Field(ge=1)
    completed_at: datetime | None
    updated_at: datetime


class GovernanceFenceSummary(DataGovernanceModel):
    policy_generation: int = Field(ge=1)
    hold_generation: int = Field(ge=0)
    deletion_generation: int = Field(ge=0)
    revision: int = Field(ge=1)


class GovernanceSimulationSummary(DataGovernanceModel):
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    next_generation: int = Field(ge=1)
    placement_compliant: bool
    migration_required_components: tuple[
        Literal["gateway", "backup", "disaster_recovery"], ...
    ]


class DataGovernanceResponse(DataGovernanceModel):
    workspace_id: str = Field(pattern=r"^wsp_[0-9a-f]{32}$")
    policy: ResidencyPolicySummary
    classifications: tuple[DataClassificationSummary, ...]
    retention_policies: tuple[RetentionPolicySummary, ...]
    legal_holds: tuple[LegalHoldSummary, ...]
    deletion_schedules: tuple[DeletionScheduleSummary, ...]
    fence: GovernanceFenceSummary
    simulation: GovernanceSimulationSummary | None = None
    applied: bool = False
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class LegalHoldResponse(DataGovernanceModel):
    hold: LegalHoldSummary
    fence: GovernanceFenceSummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class ResidencyEvidenceSummary(DataGovernanceModel):
    residency_evidence_id: str = Field(pattern=r"^rse_[0-9a-f]{32}$")
    workspace_id: str = Field(pattern=r"^wsp_[0-9a-f]{32}$")
    policy_generation: int = Field(ge=1)
    component_code: Literal[
        "gateway",
        "database",
        "object_storage",
        "backup",
        "job_runtime",
        "disaster_recovery",
        "tenant_data",
    ]
    region_code: str
    storage_class: Literal["routing", "database", "object", "backup", "job", "restore", "deletion"]
    outcome: Literal["compliant", "migration_required", "blocked", "violation"]
    violation_code: Literal[
        "region_not_allowed",
        "generation_stale",
        "policy_expired",
        "policy_unavailable",
        "hold_active",
    ] | None
    observed_watermark: int = Field(ge=0)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime


class ResidencyEvidenceListResponse(DataGovernanceModel):
    items: tuple[ResidencyEvidenceSummary, ...]
    next_cursor: str | None
    request_id: str
    trace_id: str


__all__ = [
    "DataGovernanceResponse",
    "DataGovernanceUpdateRequest",
    "DeletionScheduleSummary",
    "GovernanceSimulationSummary",
    "LegalHoldCreateRequest",
    "LegalHoldReleaseRequest",
    "LegalHoldResponse",
    "ResidencyEvidenceListResponse",
    "RetentionPolicyInput",
]
