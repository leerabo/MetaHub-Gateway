"""Strict DTOs for PRD-138 release-candidate launch governance."""

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

GateCode = Literal[
    "wb",
    "api",
    "bb",
    "gr",
    "perf",
    "web",
    "ui",
    "ucd",
    "comp",
    "sec",
    "flow",
    "migration",
    "disaster_recovery",
    "payment",
    "edge",
    "privacy",
    "compliance",
    "supply_chain",
    "tenant_isolation",
    "long_stability",
]
WaveKind = Literal["internal", "personal_pilot", "enterprise_pilot", "managed_private_pilot"]
ActorKind = Literal["product", "sre", "security", "test", "finance", "legal"]


class LaunchModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class GateArtifactCommand(LaunchModel):
    gate_code: GateCode
    workspace_id: str = Field(pattern=r"^wsp_[0-9a-f]{32}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class WavePlanCommand(LaunchModel):
    wave_kind: WaveKind
    workspace_id: str = Field(pattern=r"^wsp_[0-9a-f]{32}$")
    cohort_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    feature_flag_generation: int = Field(ge=1)
    migration_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rollback_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation_seconds: int = Field(ge=60, le=604_800)


class LaunchCommand(LaunchModel):
    operation: Literal[
        "create",
        "decide",
        "start_wave",
        "pause_wave",
        "resume_wave",
        "complete_wave",
        "review",
    ]
    launch_candidate_id: str | None = Field(default=None, pattern=r"^lvc_[0-9a-f]{32}$")
    expected_revision: int | None = Field(default=None, ge=1)
    code_tree_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    migration_set_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    contract_set_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    artifact_set_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    test_manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    artifacts: list[GateArtifactCommand] | None = Field(default=None, min_length=20, max_length=20)
    waves: list[WavePlanCommand] | None = Field(default=None, min_length=4, max_length=4)
    actor_kind: ActorKind | None = None
    approved: bool | None = None
    reason_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{2,79}$")
    wave_kind: WaveKind | None = None
    health_watermark_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    summary_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    action_items_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    conclusion: Literal["release_candidate"] | None = None
    confirmed: Literal[True]

    @model_validator(mode="after")
    def operation_shape(self) -> LaunchCommand:
        create_fields = (
            self.code_tree_sha256,
            self.migration_set_sha256,
            self.contract_set_sha256,
            self.artifact_set_sha256,
            self.test_manifest_sha256,
            self.artifacts,
            self.waves,
        )
        if self.operation == "create":
            if self.launch_candidate_id is not None or self.expected_revision is not None:
                raise ValueError("create identifiers forbidden")
            if any(value is None for value in create_fields):
                raise ValueError("candidate inputs required")
            assert self.artifacts is not None and self.waves is not None
            if len({item.gate_code for item in self.artifacts}) != 20:
                raise ValueError("all gates required")
            if {item.wave_kind for item in self.waves} != {
                "internal",
                "personal_pilot",
                "enterprise_pilot",
                "managed_private_pilot",
            }:
                raise ValueError("all waves required")
        else:
            if any(value is not None for value in create_fields):
                raise ValueError("candidate inputs forbidden")
            if self.launch_candidate_id is None or self.expected_revision is None:
                raise ValueError("candidate and revision required")
        if self.operation == "decide":
            if self.actor_kind is None or self.approved is None or self.reason_code is None:
                raise ValueError("decision fields required")
        elif any(value is not None for value in (self.actor_kind, self.approved, self.reason_code)):
            raise ValueError("decision fields forbidden")
        if self.operation in {"start_wave", "pause_wave", "resume_wave", "complete_wave"}:
            if self.wave_kind is None:
                raise ValueError("wave required")
            if self.operation == "complete_wave" and self.health_watermark_sha256 is None:
                raise ValueError("health watermark required")
            if self.operation != "complete_wave" and self.health_watermark_sha256 is not None:
                raise ValueError("health watermark forbidden")
        elif self.wave_kind is not None or self.health_watermark_sha256 is not None:
            raise ValueError("wave fields forbidden")
        review_fields = (self.summary_sha256, self.action_items_sha256, self.conclusion)
        if self.operation == "review":
            if any(value is None for value in review_fields):
                raise ValueError("review fields required")
        elif any(value is not None for value in review_fields):
            raise ValueError("review fields forbidden")
        return self


class EvidenceCommand(LaunchModel):
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
    def evidence_shape(self) -> EvidenceCommand:
        if self.finished_at <= self.started_at:
            raise ValueError("invalid evidence interval")
        if (self.metric_value is None) != (self.metric_unit is None):
            raise ValueError("metric value and unit must be paired")
        return self


class RollbackCommand(LaunchModel):
    operation: Literal["request", "verify"]
    expected_revision: int = Field(ge=1)
    launch_rollback_id: str | None = Field(default=None, pattern=r"^lrb_[0-9a-f]{32}$")
    from_wave_kind: WaveKind | None = None
    to_wave_kind: Literal["pre_launch", "internal", "personal_pilot", "enterprise_pilot"] | None = (
        None
    )
    trigger_code: (
        Literal[
            "gate_failed",
            "health_slo",
            "payment_failure",
            "security_finding",
            "operator_request",
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
    def operation_shape(self) -> RollbackCommand:
        request_fields = (
            self.from_wave_kind,
            self.to_wave_kind,
            self.trigger_code,
            self.trigger_sha256,
            self.checkpoint_sha256,
            self.job_receipt_sha256,
        )
        if self.operation == "request":
            if self.launch_rollback_id is not None or any(
                value is not None for value in (self.outcome, self.verification_sha256)
            ):
                raise ValueError("verification fields forbidden")
            if any(value is None for value in request_fields):
                raise ValueError("rollback request fields required")
        else:
            if any(value is not None for value in request_fields):
                raise ValueError("rollback request fields forbidden")
            if self.launch_rollback_id is None or self.outcome is None:
                raise ValueError("rollback verification required")
            if self.verification_sha256 is None:
                raise ValueError("rollback verification digest required")
        return self


class LaunchGateSummary(LaunchModel):
    launch_gate_id: str = Field(pattern=r"^lgt_[0-9a-f]{32}$")
    gate_code: GateCode
    policy_version: Literal[1]
    verifier_code: str
    verifier_version: Literal[1]
    threshold_rule: Literal["exact_pass"]
    freshness_seconds: int
    blocking_severity: Literal["p0"]
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class LaunchEvidenceSummary(LaunchModel):
    launch_evidence_id: str = Field(pattern=r"^lve_[0-9a-f]{32}$")
    launch_gate_id: str = Field(pattern=r"^lgt_[0-9a-f]{32}$")
    gate_code: GateCode
    workspace_id: str = Field(pattern=r"^wsp_[0-9a-f]{32}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verifier_code: str
    verifier_version: Literal[1]
    envelope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome: Literal["passed", "failed"]
    metric_value: float | None
    metric_unit: str | None
    environment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    started_at: datetime
    finished_at: datetime
    expires_at: datetime
    created_at: datetime


class DecisionSummary(LaunchModel):
    go_no_go_decision_id: str = Field(pattern=r"^lgd_[0-9a-f]{32}$")
    actor_kind: ActorKind
    operator_id: str = Field(pattern=r"^pto_[0-9a-f]{32}$")
    operator_revision: int = Field(ge=1)
    decision: Literal["approved", "rejected"]
    reason_code: str
    decision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    signature_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decided_at: datetime


class WaveSummary(LaunchModel):
    launch_wave_id: str = Field(pattern=r"^lvw_[0-9a-f]{32}$")
    wave_kind: WaveKind
    wave_sequence: int = Field(ge=1, le=4)
    workspace_id: str = Field(pattern=r"^wsp_[0-9a-f]{32}$")
    cohort_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    feature_flag_generation: int = Field(ge=1)
    migration_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rollback_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation_seconds: int
    status: Literal["pending", "running", "paused", "completed", "rolled_back"]
    observation_deadline_at: datetime | None
    health_watermark_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    revision: int = Field(ge=1)
    started_at: datetime | None
    completed_at: datetime | None


class RollbackSummary(LaunchModel):
    launch_rollback_id: str = Field(pattern=r"^lrb_[0-9a-f]{32}$")
    workspace_id: str = Field(pattern=r"^wsp_[0-9a-f]{32}$")
    from_wave_kind: WaveKind
    to_wave_kind: Literal["pre_launch", "internal", "personal_pilot", "enterprise_pilot"]
    trigger_code: str
    trigger_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    checkpoint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    job_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verification_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    status: Literal["pending", "passed", "failed"]
    revision: int = Field(ge=1)
    requested_at: datetime
    verified_at: datetime | None


class ReviewSummary(LaunchModel):
    post_launch_review_id: str = Field(pattern=r"^lpr_[0-9a-f]{32}$")
    workspace_id: str = Field(pattern=r"^wsp_[0-9a-f]{32}$")
    summary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    action_items_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    conclusion: Literal["release_candidate"]
    reviewed_at: datetime


class CandidateSummary(LaunchModel):
    launch_candidate_id: str = Field(pattern=r"^lvc_[0-9a-f]{32}$")
    candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    code_tree_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    migration_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    contract_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    test_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    gate_policy_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    generation: int = Field(ge=1)
    status: Literal[
        "collecting",
        "signing",
        "ready",
        "launching",
        "paused",
        "review_pending",
        "release_candidate",
        "rollback_pending",
        "rolled_back",
        "failed",
        "stale",
    ]
    revision: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    evidence: tuple[LaunchEvidenceSummary, ...]
    decisions: tuple[DecisionSummary, ...]
    waves: tuple[WaveSummary, ...]
    rollbacks: tuple[RollbackSummary, ...]
    review: ReviewSummary | None


class GateListResponse(LaunchModel):
    gates: tuple[LaunchGateSummary, ...]
    candidate: CandidateSummary | None
    request_id: str
    trace_id: str


class LaunchResponse(LaunchModel):
    candidate: CandidateSummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class EvidenceResponse(LaunchModel):
    evidence: LaunchEvidenceSummary
    candidate: CandidateSummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class RollbackResponse(LaunchModel):
    rollback: RollbackSummary
    candidate: CandidateSummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str
