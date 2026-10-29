"""Strict DTOs for PRD-137 tenant-isolation security assurance."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AssuranceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AssessmentCreateCommand(AssuranceModel):
    workspace_id: str = Field(pattern=r"^wsp_[0-9a-f]{32}$")
    profile_code: Literal["incremental_release", "quarterly_full"]
    profile_version: Literal[1]
    code_tree_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    migration_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    contract_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed: Literal[True]


class IsolationRunSummary(AssuranceModel):
    isolation_test_run_id: str = Field(pattern=r"^itr_[0-9a-f]{32}$")
    scenario_code: Literal[
        "api_idor",
        "write_authorization",
        "cache_owner",
        "queue_owner",
        "object_prefix",
        "metrics_labels",
        "dynamic_revocation",
        "replay_fencing",
        "late_response",
        "input_fuzz",
    ]
    target_kind: Literal[
        "public_contract", "cache", "queue", "object_store", "metrics", "authorization"
    ]
    owner_kind: Literal[
        "workspace_resource",
        "asynchronous_job",
        "cached_projection",
        "object_artifact",
        "metric_series",
        "workspace_session",
    ]
    contract_code: str
    weakness_code: str
    owner_registry_version: Literal[1]
    runner_image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome: Literal["passed", "failed", "error"]
    duration_ms: int = Field(ge=0, le=1_800_000)
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cleanup_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    started_at: datetime
    finished_at: datetime


class RemediationSummary(AssuranceModel):
    remediation_id: str = Field(pattern=r"^rem_[0-9a-f]{32}$")
    change_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verification_assessment_id: str | None = Field(
        default=None, pattern=r"^sas_[0-9a-f]{32}$"
    )
    outcome: Literal["proposed", "verification_failed", "verified_passed"]
    finding_revision: int = Field(ge=1)
    created_at: datetime
    verified_at: datetime | None


class RiskAcceptanceSummary(AssuranceModel):
    risk_acceptance_id: str = Field(pattern=r"^rsk_[0-9a-f]{32}$")
    scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    compensating_controls: tuple[
        Literal["network_isolation", "runtime_detection", "feature_disablement", "rate_limit"],
        ...,
    ]
    status: Literal[
        "pending_owner",
        "pending_security",
        "pending_platform",
        "active",
        "expired",
        "revoked",
    ]
    revision: int = Field(ge=1)
    expires_at: datetime
    approved_at: datetime | None
    updated_at: datetime


class PenTestFindingSummary(AssuranceModel):
    pen_test_finding_id: str = Field(pattern=r"^ptf_[0-9a-f]{32}$")
    isolation_test_run_id: str = Field(pattern=r"^itr_[0-9a-f]{32}$")
    owner_kind: str
    contract_code: str
    severity: Literal["p0", "p1", "p2", "p3"]
    weakness_code: str
    status: Literal["open", "remediation_pending", "closed", "risk_accepted"]
    sla_deadline_at: datetime
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: int = Field(ge=1)
    first_seen_at: datetime
    last_seen_at: datetime
    closed_at: datetime | None
    remediations: tuple[RemediationSummary, ...]
    risk_acceptances: tuple[RiskAcceptanceSummary, ...]


class SecurityAssessmentSummary(AssuranceModel):
    security_assessment_id: str = Field(pattern=r"^sas_[0-9a-f]{32}$")
    workspace_id: str = Field(pattern=r"^wsp_[0-9a-f]{32}$")
    profile_code: Literal["incremental_release", "quarterly_full"]
    profile_version: Literal[1]
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile_signature_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    code_tree_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    migration_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    contract_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["queued", "running", "passed", "failed"]
    generation: int = Field(ge=1)
    environment_receipt_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    summary_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    revision: int = Field(ge=1)
    requested_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    runs: tuple[IsolationRunSummary, ...]
    findings: tuple[PenTestFindingSummary, ...]


class AssessmentResponse(AssuranceModel):
    assessment: SecurityAssessmentSummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class RemediationCommand(AssuranceModel):
    operation: Literal["propose", "verify"]
    expected_revision: int = Field(ge=1)
    change_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    remediation_id: str | None = Field(default=None, pattern=r"^rem_[0-9a-f]{32}$")
    verification_assessment_id: str | None = Field(
        default=None, pattern=r"^sas_[0-9a-f]{32}$"
    )
    confirmed: Literal[True]

    @model_validator(mode="after")
    def operation_shape(self) -> RemediationCommand:
        if self.operation == "propose":
            if self.change_sha256 is None:
                raise ValueError("change digest required")
            if self.remediation_id is not None or self.verification_assessment_id is not None:
                raise ValueError("verification fields forbidden")
        else:
            if self.change_sha256 is not None:
                raise ValueError("change digest forbidden")
            if self.remediation_id is None or self.verification_assessment_id is None:
                raise ValueError("verification fields required")
        return self


class RemediationResponse(AssuranceModel):
    remediation: RemediationSummary
    finding: PenTestFindingSummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class ScenarioExecution(AssuranceModel):
    scenario_code: str
    outcome: Literal["passed", "failed", "error"]
    severity: Literal["p0", "p1", "p2", "p3"] | None = None
    duration_ms: int = Field(ge=0, le=1_800_000)
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def severity_shape(self) -> ScenarioExecution:
        if self.outcome == "passed" and self.severity is not None:
            raise ValueError("passed result cannot carry severity")
        if self.outcome != "passed" and self.severity is None:
            raise ValueError("failed result requires severity")
        return self


class RiskAcceptanceCommand(AssuranceModel):
    operation: Literal["request", "approve", "review"]
    expected_revision: int | None = Field(default=None, ge=1)
    risk_acceptance_id: str | None = Field(default=None, pattern=r"^rsk_[0-9a-f]{32}$")
    scope_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    compensating_controls: list[
        Literal["network_isolation", "runtime_detection", "feature_disablement", "rate_limit"]
    ] | None = Field(default=None, min_length=1, max_length=4)
    expires_at: datetime | None = None
    approved: bool | None = None
    confirmed: Literal[True]
