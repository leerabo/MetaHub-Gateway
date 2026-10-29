"""Strict public DTOs for PRD-145 console-experience domains."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ConsoleMetric(StrictModel):
    metric_code: str = Field(min_length=1, max_length=80)
    value: int | None = Field(default=None, ge=0)
    state_code: str = Field(min_length=1, max_length=80)
    action_code: str | None = Field(default=None, min_length=1, max_length=80)
    reason_code: str | None = Field(default=None, min_length=1, max_length=80)


class ConsoleSection(StrictModel):
    source_code: str = Field(min_length=1, max_length=80)
    freshness: Literal["fresh", "stale", "unavailable"]
    source_revision: int = Field(ge=0)
    source_watermark: str = Field(min_length=1, max_length=160)
    metrics: tuple[ConsoleMetric, ...] = Field(max_length=40)


class OverviewResponse(StrictModel):
    domain: Literal["platform", "workspace"]
    status: Literal["ready", "partial", "stale"]
    generation: int = Field(ge=1)
    navigation_epoch: int = Field(ge=1)
    observed_at: datetime
    sections: tuple[ConsoleSection, ...] = Field(max_length=16)
    permission_revision: int = Field(ge=1)
    permission_fingerprint: str = Field(min_length=64, max_length=64)
    request_id: str
    trace_id: str


class AssistantQuestionRequest(StrictModel):
    question: str = Field(min_length=1, max_length=2000)

    @field_validator("question")
    @classmethod
    def validate_question(cls, value: str) -> str:
        if not value.strip() or any(ord(character) == 0 for character in value):
            raise ValueError("invalid question")
        return value


class AssistantClaim(StrictModel):
    claim_code: str = Field(min_length=1, max_length=80)
    value: int | None = Field(default=None, ge=0)
    state_code: str = Field(min_length=1, max_length=80)


class AssistantCitation(StrictModel):
    source_type: str = Field(min_length=1, max_length=80)
    label_code: str = Field(min_length=1, max_length=80)
    navigation_id: str = Field(min_length=1, max_length=80)


class AssistantSourceStatus(StrictModel):
    source_type: str = Field(min_length=1, max_length=80)
    status: Literal["ready", "stale", "unavailable"]


class AssistantQueryResponse(StrictModel):
    query_id: str
    domain: Literal["platform", "workspace"]
    query_type: Literal[
        "operations_summary",
        "security_summary",
        "billing_summary",
        "provider_summary",
        "unknown",
    ]
    outcome: Literal["answered", "partial", "unsupported"]
    answer_code: str = Field(min_length=1, max_length=80)
    claims: tuple[AssistantClaim, ...] = Field(max_length=100)
    citations: tuple[AssistantCitation, ...] = Field(max_length=50)
    sources: tuple[AssistantSourceStatus, ...] = Field(max_length=50)
    navigation_epoch: int = Field(ge=1)
    permission_revision: int = Field(ge=1)
    permission_fingerprint: str = Field(min_length=64, max_length=64)
    request_id: str
    trace_id: str


class ApprovalQueueItem(StrictModel):
    projection_id: str
    source_domain: str = Field(min_length=1, max_length=80)
    source_type: str = Field(min_length=1, max_length=80)
    source_revision: int = Field(ge=1)
    status: Literal["pending", "approved", "rejected", "expired"]
    risk_band: Literal["standard", "high", "critical"]
    summary_code: str = Field(min_length=1, max_length=80)
    decision_contract_code: str = Field(min_length=1, max_length=80)
    deadline_at: datetime
    can_open: bool


class ApprovalQueueResponse(StrictModel):
    domain: Literal["platform", "workspace"]
    items: tuple[ApprovalQueueItem, ...] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, min_length=43, max_length=512)
    navigation_epoch: int = Field(ge=1)
    permission_revision: int = Field(ge=1)
    permission_fingerprint: str = Field(min_length=64, max_length=64)
    request_id: str
    trace_id: str


class AccountPreferenceResponse(StrictModel):
    preference_id: str
    locale: Literal["zh-CN", "en-US"]
    available_locales: tuple[Literal["zh-CN", "en-US"], ...]
    revision: int = Field(ge=1)
    confirmation_state: Literal["confirmed", "needs_confirmation"]
    updated_at: datetime
    request_id: str
    trace_id: str


class AccountPreferencePatch(StrictModel):
    locale: Literal["zh-CN", "en-US"]


class AccountPreferenceCommandResponse(AccountPreferenceResponse):
    changed: bool
    idempotent_replay: bool


class DeploymentDiagnostic(StrictModel):
    deployment_ref: str = Field(min_length=64, max_length=64)
    deployment_kind: Literal["connected", "isolated"] | None = None
    region_code: str | None = Field(default=None, min_length=2, max_length=40)
    release_band: Literal["current", "behind", "unknown"]
    compatibility_band: Literal["compatible", "attention", "blocked", "unknown"]
    desired_generation: int | None = Field(default=None, ge=0)
    observed_generation: int | None = Field(default=None, ge=0)
    health_state: Literal["healthy", "degraded", "offline", "revoked", "unknown"]
    reason_code: str = Field(min_length=1, max_length=80)
    action_code: str | None = Field(default=None, min_length=1, max_length=80)
    evidence_digest: str = Field(min_length=64, max_length=64)
    observed_at: datetime


class DeploymentSummaryResponse(StrictModel):
    scope: Literal["operator", "audit"]
    items: tuple[DeploymentDiagnostic, ...] = Field(max_length=100)
    projection_generation: int = Field(ge=0)
    navigation_epoch: int = Field(ge=1)
    permission_revision: int = Field(ge=1)
    permission_fingerprint: str = Field(min_length=64, max_length=64)
    request_id: str
    trace_id: str
