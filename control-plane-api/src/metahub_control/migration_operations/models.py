"""Strict public DTOs for PRD-131 customer migration and cutover."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MigrationDomain = Literal[
    "identity",
    "workspace",
    "provider_reference",
    "policy",
    "ledger",
    "audit",
    "edge",
]
SourceKind = Literal["legacy_tenant", "direct_provider", "private_deployment"]


class MigrationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class MigrationAssessmentCreateRequest(MigrationModel):
    source_kind: SourceKind
    manifest_version: str = Field(min_length=3, max_length=40, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    domains: list[MigrationDomain] = Field(min_length=1, max_length=7)
    reason_code: Literal["initial_assessment", "reassessment", "cutover_precheck"]
    confirmed: Literal[True]

    @model_validator(mode="after")
    def canonical_domains(self) -> MigrationAssessmentCreateRequest:
        if sorted(set(self.domains)) != self.domains:
            raise ValueError("migration domains must be unique and sorted")
        return self


class MigrationRunCreateRequest(MigrationModel):
    assessment_id: str = Field(pattern=r"^mas_[0-9a-f]{32}$")
    expected_assessment_revision: int = Field(ge=1, le=2_147_483_647)
    reason_code: Literal["customer_approved", "scheduled_window", "recovery_retry"]
    confirmed: Literal[True]


class MigrationTransitionRequest(MigrationModel):
    expected_revision: int = Field(ge=1, le=2_147_483_647)
    reason_code: Literal[
        "customer_approved",
        "scheduled_window",
        "validation_recovered",
        "customer_requested_rollback",
        "validation_failed",
        "observation_failed",
    ]
    confirmed: Literal[True]


class MigrationDomainSnapshot(MigrationModel):
    domain_code: MigrationDomain
    adapter_version: Literal["migration-adapter-v1"]
    source_count: int = Field(ge=0)
    target_count: int = Field(ge=0)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_sequence: int = Field(ge=0)
    target_sequence: int = Field(ge=0)
    source_watermark: int = Field(ge=0)
    target_watermark: int = Field(ge=0)
    matches: bool


class MigrationPlanSummary(MigrationModel):
    plan_id: str = Field(pattern=r"^mpl_[0-9a-f]{32}$")
    assessment_id: str = Field(pattern=r"^mas_[0-9a-f]{32}$")
    generation: int = Field(ge=1)
    status: Literal["ready", "blocked"]
    phases: tuple[
        Literal["assessment", "backfill", "dual_read", "freeze", "cutover", "observe"],
        ...,
    ]
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    planned_downtime_seconds: int = Field(ge=0, le=7200)
    rollback_deadline_seconds: int = Field(ge=1, le=7200)
    revision: int = Field(ge=1)
    created_at: datetime


class MigrationAssessmentSummary(MigrationModel):
    assessment_id: str = Field(pattern=r"^mas_[0-9a-f]{32}$")
    workspace_id: str = Field(pattern=r"^wsp_[0-9a-f]{32}$")
    source_kind: SourceKind
    manifest_version: str
    source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["ready", "validation_failed"]
    object_count: int = Field(ge=0)
    blocking_difference_count: int = Field(ge=0)
    write_watermark_before: int = Field(ge=0)
    write_watermark_after: int = Field(ge=0)
    credential_reentry_required_count: int = Field(ge=0)
    permission_expansion_count: int = Field(ge=0)
    revision: int = Field(ge=1)
    created_at: datetime
    completed_at: datetime
    snapshots: tuple[MigrationDomainSnapshot, ...]
    plan: MigrationPlanSummary


class MigrationDifferenceSummary(MigrationModel):
    difference_id: str = Field(pattern=r"^mdf_[0-9a-f]{32}$")
    assessment_id: str = Field(pattern=r"^mas_[0-9a-f]{32}$")
    run_id: str | None = Field(default=None, pattern=r"^mrn_[0-9a-f]{32}$")
    domain_code: MigrationDomain
    difference_code: Literal[
        "count_mismatch",
        "hash_mismatch",
        "sequence_mismatch",
        "watermark_mismatch",
        "permission_expansion",
    ]
    severity: Literal["blocking"]
    source_count: int = Field(ge=0)
    target_count: int = Field(ge=0)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_sequence: int = Field(ge=0)
    target_sequence: int = Field(ge=0)
    remediation_code: Literal[
        "rebuild_target_projection",
        "replay_missing_sequence",
        "wait_for_watermark",
        "reassess_source",
        "review_role_mapping",
    ]
    status: Literal["open"]
    detected_at: datetime


class CutoverCheckpointSummary(MigrationModel):
    checkpoint_id: str = Field(pattern=r"^mcp_[0-9a-f]{32}$")
    run_id: str = Field(pattern=r"^mrn_[0-9a-f]{32}$")
    phase_code: Literal["assessment", "backfill", "dual_read", "freeze", "cutover", "rollback"]
    phase_sequence: int = Field(ge=1, le=6)
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_watermark: int = Field(ge=0)
    target_watermark: int = Field(ge=0)
    freeze_generation: int = Field(ge=1)
    status: Literal["verified", "failed"]
    created_at: datetime


class RollbackPlanSummary(MigrationModel):
    rollback_plan_id: str = Field(pattern=r"^mrp_[0-9a-f]{32}$")
    run_id: str = Field(pattern=r"^mrn_[0-9a-f]{32}$")
    previous_run_id: str | None = Field(default=None, pattern=r"^mrn_[0-9a-f]{32}$")
    last_safe_checkpoint_id: str = Field(pattern=r"^mcp_[0-9a-f]{32}$")
    compensation_order: tuple[
        Literal["freeze_writes", "restore_head", "replay_receipts", "verify_owner"],
        ...,
    ]
    rollback_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["ready", "completed", "failed"]
    revision: int = Field(ge=1)
    completed_at: datetime | None


class MigrationRunSummary(MigrationModel):
    run_id: str = Field(pattern=r"^mrn_[0-9a-f]{32}$")
    assessment_id: str = Field(pattern=r"^mas_[0-9a-f]{32}$")
    plan_id: str = Field(pattern=r"^mpl_[0-9a-f]{32}$")
    status: Literal["observing", "completed", "failed", "rolling_back", "rolled_back"]
    generation: int = Field(ge=1)
    freeze_generation: int = Field(ge=1)
    blocking_difference_count: int = Field(ge=0)
    revision: int = Field(ge=1)
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    checkpoints: tuple[CutoverCheckpointSummary, ...]
    rollback_plan: RollbackPlanSummary | None


class MigrationAssessmentResponse(MigrationModel):
    assessment: MigrationAssessmentSummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class MigrationRunResponse(MigrationModel):
    run: MigrationRunSummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class MigrationDifferenceListResponse(MigrationModel):
    assessment_id: str = Field(pattern=r"^mas_[0-9a-f]{32}$")
    run_id: str | None = Field(default=None, pattern=r"^mrn_[0-9a-f]{32}$")
    items: tuple[MigrationDifferenceSummary, ...]
    support_scoped: bool = False
    request_id: str
    trace_id: str


__all__ = [
    "MigrationAssessmentCreateRequest",
    "MigrationAssessmentResponse",
    "MigrationDifferenceListResponse",
    "MigrationRunCreateRequest",
    "MigrationRunResponse",
    "MigrationTransitionRequest",
]
