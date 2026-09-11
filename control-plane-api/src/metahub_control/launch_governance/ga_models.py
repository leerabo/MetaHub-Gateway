"""Strict DTOs for the final PRD-148 GA release boundary."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import CandidateSummary, GateCode, RequestTimestamp


class GaModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class GaGateSummary(GaModel):
    ga_gate_id: str = Field(pattern=r"^gag_[0-9a-f]{32}$")
    gate_code: GateCode
    policy_version: Literal[1]
    verifier_code: str
    verifier_version: Literal[1]
    freshness_seconds: int = Field(ge=300, le=604800)
    blocking_severity: Literal["p0"]
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class GaEvidenceCommand(GaModel):
    launch_candidate_id: str = Field(pattern=r"^lvc_[0-9a-f]{32}$")
    expected_revision: int = Field(ge=1)
    workspace_id: str = Field(pattern=r"^wsp_[0-9a-f]{32}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verifier_code: str = Field(pattern=r"^[a-z][a-z0-9_-]{3,47}$")
    verifier_version: Literal[1]
    outcome: Literal["passed", "failed"]
    metric_value: float | None = None
    metric_unit: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_./%-]{0,31}$")
    environment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    started_at: RequestTimestamp
    finished_at: RequestTimestamp
    signature: str = Field(pattern=r"^[A-Za-z0-9_-]{43,512}$")
    confirmed: Literal[True]

    @model_validator(mode="after")
    def evidence_shape(self) -> GaEvidenceCommand:
        if self.finished_at <= self.started_at:
            raise ValueError("invalid evidence interval")
        if (self.metric_value is None) != (self.metric_unit is None):
            raise ValueError("metric value and unit must be paired")
        return self


class GaLaunchCommand(GaModel):
    operation: Literal["launch"]
    launch_candidate_id: str = Field(pattern=r"^lvc_[0-9a-f]{32}$")
    expected_revision: int = Field(ge=1)
    confirmed: Literal[True]


class GaRollbackCommand(GaModel):
    operation: Literal["request", "verify"]
    expected_revision: int = Field(ge=1)
    ga_rollback_id: str | None = Field(default=None, pattern=r"^gar_[0-9a-f]{32}$")
    trigger_code: (
        Literal[
            "gate_failed", "health_slo", "payment_failure", "security_finding", "operator_request"
        ]
        | None
    ) = None
    trigger_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    checkpoint_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    job_receipt_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    outcome: Literal["passed", "failed"] | None = None
    verification_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    confirmed: Literal[True]

    @model_validator(mode="after")
    def operation_shape(self) -> GaRollbackCommand:
        request_fields = (
            self.trigger_code,
            self.trigger_sha256,
            self.checkpoint_sha256,
            self.job_receipt_sha256,
        )
        if self.operation == "request":
            if (
                self.ga_rollback_id is not None
                or self.outcome is not None
                or self.verification_sha256 is not None
            ):
                raise ValueError("verification fields forbidden")
            if any(value is None for value in request_fields):
                raise ValueError("rollback request fields required")
        else:
            if (
                any(value is not None for value in request_fields)
                or self.ga_rollback_id is None
                or self.outcome is None
            ):
                raise ValueError("rollback verification required")
            if self.verification_sha256 is None:
                raise ValueError("rollback verification digest required")
        return self


class GaReviewCommand(GaModel):
    expected_revision: int = Field(ge=1)
    summary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    action_items_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    conclusion: Literal["ga_launched", "ga_rolled_back"]
    confirmed: Literal[True]


class GaEvidenceSummary(GaModel):
    ga_evidence_id: str = Field(pattern=r"^gae_[0-9a-f]{32}$")
    launch_candidate_id: str = Field(pattern=r"^lvc_[0-9a-f]{32}$")
    ga_gate_id: str = Field(pattern=r"^gag_[0-9a-f]{32}$")
    source_evidence_id: str = Field(pattern=r"^lve_[0-9a-f]{32}$")
    outcome: Literal["passed"]
    finished_at: datetime
    expires_at: datetime
    created_at: datetime


class GaDecisionSummary(GaModel):
    ga_decision_id: str = Field(pattern=r"^gad_[0-9a-f]{32}$")
    ga_launch_id: str = Field(pattern=r"^gal_[0-9a-f]{32}$")
    actor_kind: Literal["product", "sre", "security", "test", "finance", "legal"]
    operator_id: str = Field(pattern=r"^pto_[0-9a-f]{32}$")
    source_decision_id: str = Field(pattern=r"^lgd_[0-9a-f]{32}$")
    decided_at: datetime


class GaLaunchSummary(GaModel):
    ga_launch_id: str = Field(pattern=r"^gal_[0-9a-f]{32}$")
    launch_candidate_id: str = Field(pattern=r"^lvc_[0-9a-f]{32}$")
    workspace_id: str = Field(pattern=r"^wsp_[0-9a-f]{32}$")
    status: Literal["approved", "launched", "rollback_pending", "rolled_back", "failed"]
    candidate_generation: int = Field(ge=1)
    revision: int = Field(ge=1)
    approved_at: datetime
    launched_at: datetime | None
    created_at: datetime
    updated_at: datetime
    evidence: tuple[GaEvidenceSummary, ...]
    decisions: tuple[GaDecisionSummary, ...]
    rollbacks: tuple[GaRollbackSummary, ...]
    review: PostGaReviewSummary | None


class GaRollbackSummary(GaModel):
    ga_rollback_id: str = Field(pattern=r"^gar_[0-9a-f]{32}$")
    ga_launch_id: str = Field(pattern=r"^gal_[0-9a-f]{32}$")
    from_status: Literal["approved", "launched", "rollback_pending"]
    trigger_code: str
    trigger_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    checkpoint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    job_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verification_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    status: Literal["pending", "passed", "failed"]
    revision: int = Field(ge=1)
    requested_at: datetime
    verified_at: datetime | None


class PostGaReviewSummary(GaModel):
    post_ga_review_id: str = Field(pattern=r"^pgr_[0-9a-f]{32}$")
    ga_launch_id: str = Field(pattern=r"^gal_[0-9a-f]{32}$")
    summary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    action_items_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    conclusion: Literal["ga_launched", "ga_rolled_back"]
    reviewed_at: datetime


class GaGateListResponse(GaModel):
    gates: tuple[GaGateSummary, ...]
    candidate: CandidateSummary | None
    launch: GaLaunchSummary | None
    request_id: str
    trace_id: str


class GaEvidenceResponse(GaModel):
    evidence: GaEvidenceSummary
    request_id: str
    trace_id: str


class GaLaunchResponse(GaModel):
    launch: GaLaunchSummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class GaRollbackResponse(GaModel):
    rollback: GaRollbackSummary
    launch: GaLaunchSummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class GaReviewResponse(GaModel):
    review: PostGaReviewSummary
    launch: GaLaunchSummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


GaLaunchSummary.model_rebuild()
GaReviewResponse.model_rebuild()


__all__ = [
    "GaEvidenceCommand",
    "GaEvidenceResponse",
    "GaEvidenceSummary",
    "GaGateListResponse",
    "GaGateSummary",
    "GaLaunchCommand",
    "GaLaunchResponse",
    "GaLaunchSummary",
    "GaRollbackCommand",
    "GaRollbackResponse",
    "GaRollbackSummary",
    "GaDecisionSummary",
    "GaReviewCommand",
    "GaReviewResponse",
    "PostGaReviewSummary",
]
