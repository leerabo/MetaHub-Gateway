"""Strict public DTOs for the PRD-135 compliance evidence center."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator


def _request_timestamp(value: object) -> datetime:
    if type(value) is not str or not 20 <= len(value) <= 40:
        raise ValueError("timestamp must be an ISO-8601 string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include an offset")
    return parsed


RequestTimestamp = Annotated[datetime, BeforeValidator(_request_timestamp)]

EvidenceKind = Literal["access_review", "encryption_posture", "privacy_request_process"]
Classification = Literal["internal", "confidential", "restricted"]
EvidenceOutcome = Literal["passed", "failed", "inconclusive", "not_applicable"]
ReportOutcome = Literal[
    "implemented",
    "partially_implemented",
    "not_implemented",
    "not_applicable",
    "exception_active",
    "evidence_stale",
]


class ComplianceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ControlMappingSummary(ComplianceModel):
    framework_code: Literal["djcp_mlps", "pipl", "dsl"]
    framework_version: str
    clause_code: str
    statement_template_code: Literal["control_fact_only_v1", "privacy_process_fact_v1"]


class ComplianceControlSummary(ComplianceModel):
    compliance_control_id: str = Field(pattern=r"^ctl_[0-9a-f]{32}$")
    control_code: str
    control_version: int = Field(ge=1)
    control_set_version: str
    title: str
    owner_code: Literal["security", "privacy", "platform"]
    responsibility: Literal["platform", "workspace", "shared"]
    evidence_kind: EvidenceKind
    collector_schema_version: Literal[1]
    freshness_seconds: int = Field(ge=300, le=2_592_000)
    criticality: Literal["critical", "high", "medium"]
    evaluation_rule_code: Literal["sample_failure_v1"]
    evaluation_rule_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mappings: tuple[ControlMappingSummary, ...]


class EvidenceSummary(ComplianceModel):
    compliance_evidence_id: str = Field(pattern=r"^evd_[0-9a-f]{32}$")
    workspace_id: str = Field(pattern=r"^wsp_[0-9a-f]{32}$")
    compliance_control_id: str = Field(pattern=r"^ctl_[0-9a-f]{32}$")
    evidence_sequence: int = Field(ge=1)
    evidence_generation: int = Field(ge=1)
    collector_service_account_id: str = Field(pattern=r"^svc_[0-9a-f]{32}$")
    collector_version: str
    evidence_kind: EvidenceKind
    schema_version: Literal[1]
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    classification: Classification
    scope_state: Literal["present", "absent"]
    observed_state: Literal["effective", "ineffective", "unknown"]
    sample_count: int = Field(ge=0, le=1_000_000)
    failed_count: int = Field(ge=0, le=1_000_000)
    outcome: EvidenceOutcome
    outcome_reason_code: Literal[
        "all_samples_effective",
        "sample_failure_observed",
        "collector_inconclusive",
        "scope_not_present",
    ]
    immutable_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    captured_at: datetime
    valid_until: datetime
    submitted_at: datetime
    freshness: Literal["current", "expiring", "expired"]


class ComplianceExceptionSummary(ComplianceModel):
    compliance_exception_id: str = Field(pattern=r"^cex_[0-9a-f]{32}$")
    workspace_id: str = Field(pattern=r"^wsp_[0-9a-f]{32}$")
    compliance_control_id: str = Field(pattern=r"^ctl_[0-9a-f]{32}$")
    evidence_generation: int = Field(ge=1)
    reason_code: Literal["risk_acceptance", "vendor_dependency", "planned_remediation"]
    compensating_controls: tuple[
        Literal["enhanced_monitoring", "manual_review", "access_restriction"], ...
    ]
    status: Literal["pending", "active", "expired"]
    review_generation: int = Field(ge=1)
    revision: int = Field(ge=1)
    starts_at: datetime
    expires_at: datetime
    approved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class RemediationTaskSummary(ComplianceModel):
    remediation_task_id: str = Field(pattern=r"^rem_[0-9a-f]{32}$")
    compliance_control_id: str = Field(pattern=r"^ctl_[0-9a-f]{32}$")
    failed_evidence_generation: int = Field(ge=1)
    status: Literal["open", "in_progress", "blocked", "verified", "closed"]
    owner_code: Literal["security", "privacy", "platform"]
    due_at: datetime
    revision: int = Field(ge=1)


class ComplianceControlState(ComplianceModel):
    control: ComplianceControlSummary
    report_outcome: ReportOutcome | None
    explanation_code: str | None
    evidence: EvidenceSummary | None
    exception: ComplianceExceptionSummary | None
    remediation: RemediationTaskSummary | None


class PlatformComplianceControlsResponse(ComplianceModel):
    control_set_version: str
    workspace_id: str | None
    certification_claimed: Literal[False] = False
    claim_template_code: Literal["control_fact_only_v1"] = "control_fact_only_v1"
    controls: tuple[ComplianceControlState, ...]
    request_id: str
    trace_id: str


class ComplianceEvidenceSubmitRequest(ComplianceModel):
    audience: Literal["metahub-control"]
    route: Literal["/platform/compliance/evidence"]
    workspace_id: str = Field(pattern=r"^wsp_[0-9a-f]{32}$")
    compliance_control_id: str = Field(pattern=r"^ctl_[0-9a-f]{32}$")
    collector_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{2,39}$")
    evidence_kind: EvidenceKind
    schema_version: Literal[1]
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    classification: Classification
    scope_state: Literal["present", "absent"]
    observed_state: Literal["effective", "ineffective", "unknown"]
    sample_count: int = Field(ge=0, le=1_000_000)
    failed_count: int = Field(ge=0, le=1_000_000)
    captured_at: RequestTimestamp
    valid_until: RequestTimestamp
    issued_at: RequestTimestamp
    nonce: str = Field(pattern=r"^[A-Za-z0-9_-]{24,96}$", repr=False)
    signature_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", repr=False)

    @model_validator(mode="after")
    def fixed_fact_shape(self) -> ComplianceEvidenceSubmitRequest:
        if (
            self.captured_at.tzinfo is None
            or self.valid_until.tzinfo is None
            or self.issued_at.tzinfo is None
        ):
            raise ValueError("timestamps require offsets")
        if self.valid_until <= self.captured_at or self.failed_count > self.sample_count:
            raise ValueError("invalid evidence interval or count")
        if self.scope_state == "absent":
            if self.observed_state != "unknown" or self.sample_count != 0 or self.failed_count != 0:
                raise ValueError("absent scope must have no samples")
        elif self.sample_count < 1:
            raise ValueError("present scope requires samples")
        if self.observed_state == "effective" and self.failed_count != 0:
            raise ValueError("effective evidence cannot contain failures")
        if self.observed_state == "ineffective" and self.failed_count < 1:
            raise ValueError("ineffective evidence requires a failure")
        return self


class ComplianceEvidenceResponse(ComplianceModel):
    evidence: EvidenceSummary
    remediation: RemediationTaskSummary | None
    report_id: str = Field(pattern=r"^crp_[0-9a-f]{32}$")
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class ComplianceExceptionCommand(ComplianceModel):
    operation: Literal["request", "approve"]
    workspace_id: str | None = Field(default=None, pattern=r"^wsp_[0-9a-f]{32}$")
    compliance_control_id: str | None = Field(default=None, pattern=r"^ctl_[0-9a-f]{32}$")
    evidence_generation: int | None = Field(default=None, ge=1)
    reason_code: Literal["risk_acceptance", "vendor_dependency", "planned_remediation"] | None = (
        None
    )
    compensating_controls: (
        list[Literal["enhanced_monitoring", "manual_review", "access_restriction"]] | None
    ) = Field(default=None, min_length=1, max_length=3)
    starts_at: RequestTimestamp | None = None
    expires_at: RequestTimestamp | None = None
    compliance_exception_id: str | None = Field(default=None, pattern=r"^cex_[0-9a-f]{32}$")
    review_generation: int | None = Field(default=None, ge=1)
    confirmed: Literal[True]

    @model_validator(mode="after")
    def operation_shape(self) -> ComplianceExceptionCommand:
        request_fields = (
            self.workspace_id,
            self.compliance_control_id,
            self.evidence_generation,
            self.reason_code,
            self.compensating_controls,
            self.starts_at,
            self.expires_at,
        )
        if self.operation == "request":
            if any(value is None for value in request_fields):
                raise ValueError("request fields are required")
            if self.compliance_exception_id is not None or self.review_generation is not None:
                raise ValueError("approval fields are forbidden")
            if self.compensating_controls != sorted(set(self.compensating_controls or [])):
                raise ValueError("compensating controls must be canonical")
            if self.starts_at and self.expires_at and self.expires_at <= self.starts_at:
                raise ValueError("invalid exception interval")
        else:
            if self.compliance_exception_id is None or self.review_generation is None:
                raise ValueError("approval fields are required")
            if any(value is not None for value in request_fields):
                raise ValueError("request fields are forbidden")
        return self


class ComplianceExceptionResponse(ComplianceModel):
    exception: ComplianceExceptionSummary
    report_id: str = Field(pattern=r"^crp_[0-9a-f]{32}$")
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class ComplianceReportItemSummary(ComplianceModel):
    compliance_control_id: str = Field(pattern=r"^ctl_[0-9a-f]{32}$")
    control_code: str
    title: str
    report_outcome: ReportOutcome
    explanation_code: str
    evidence_generation: int | None
    evidence_artifact_sha256: str | None
    evidence_classification: Classification | None
    evidence_valid_until: datetime | None
    compliance_exception_id: str | None
    remediation_task_id: str | None


class ComplianceReportSummary(ComplianceModel):
    compliance_report_id: str = Field(pattern=r"^crp_[0-9a-f]{32}$")
    workspace_id: str = Field(pattern=r"^wsp_[0-9a-f]{32}$")
    report_version: int = Field(ge=1)
    control_set_version: str
    evidence_high_watermark: int = Field(ge=0)
    generator_version: Literal["prd135-v1"]
    summary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    implemented_count: int = Field(ge=0)
    gap_count: int = Field(ge=0)
    stale_count: int = Field(ge=0)
    stale: bool
    expires_at: datetime
    generated_at: datetime
    items: tuple[ComplianceReportItemSummary, ...]


class ComplianceReportListResponse(ComplianceModel):
    current: ComplianceReportSummary | None
    recent: tuple[ComplianceReportSummary, ...]
    certification_claimed: Literal[False] = False
    request_id: str
    trace_id: str


__all__ = [
    "ComplianceEvidenceResponse",
    "ComplianceEvidenceSubmitRequest",
    "ComplianceExceptionCommand",
    "ComplianceExceptionResponse",
    "ComplianceReportListResponse",
    "PlatformComplianceControlsResponse",
]
