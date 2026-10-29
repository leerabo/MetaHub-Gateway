"""Closed PRD-110 risk-control request and response contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class RiskModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RiskRestrictionInfo(RiskModel):
    risk_restriction_id: str = Field(pattern=r"^rst_[0-9a-f]{32}$")
    restriction_kind: Literal["api_calls", "resource_creation", "workspace_access"]
    status: Literal["active", "cleared", "expired"]
    starts_at: datetime
    expires_at: datetime
    cleared_at: datetime | None
    revision: int = Field(ge=1)


class RiskAppealInfo(RiskModel):
    risk_appeal_id: str = Field(pattern=r"^rap_[0-9a-f]{32}$")
    category: Literal["not_my_activity", "expected_usage", "restriction_error", "other"]
    status: Literal["pending", "approved", "rejected"]
    resolution_reason: (
        Literal["verified_false_positive", "restriction_confirmed", "insufficient_evidence"] | None
    )
    revision: int = Field(ge=1)
    created_at: datetime
    resolved_at: datetime | None


class RiskCaseInfo(RiskModel):
    risk_case_id: str = Field(pattern=r"^rca_[0-9a-f]{32}$")
    workspace_id: str = Field(pattern=r"^wsp_[0-9a-f]{32}$")
    case_kind: Literal["credential_stuffing", "resource_abuse", "api_abuse", "account_takeover"]
    safe_reason_family: Literal[
        "unusual_volume",
        "repeated_failures",
        "automation_detected",
        "resource_spike",
        "manual_review",
    ]
    score_band: Literal["low", "medium", "high", "critical"] | None
    status: Literal["open", "under_review", "restricted", "cleared", "closed"]
    signal_families: tuple[str, ...]
    restriction: RiskRestrictionInfo | None
    appeals: tuple[RiskAppealInfo, ...]
    revision: int = Field(ge=1)
    opened_at: datetime
    updated_at: datetime


class RiskCaseListResponse(RiskModel):
    items: tuple[RiskCaseInfo, ...]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    next_cursor: str | None
    projection: Literal["security", "support"]
    request_id: str
    trace_id: str


class RiskCaseDecisionRequest(RiskModel):
    decision: Literal["required_review", "restricted", "cleared"]
    reason_code: Literal[
        "manual_review", "automation_confirmed", "false_positive", "appeal_approved"
    ]
    restriction_kind: Literal["api_calls", "resource_creation", "workspace_access"] | None = None
    restriction_minutes: int | None = Field(default=None, ge=5, le=10080)
    confirmed: Literal[True]

    @model_validator(mode="after")
    def validate_decision_shape(self) -> RiskCaseDecisionRequest:
        restricted = self.decision == "restricted"
        if restricted != (self.restriction_kind is not None):
            raise ValueError("restriction_kind mismatch")
        if restricted != (self.restriction_minutes is not None):
            raise ValueError("restriction_minutes mismatch")
        if self.decision == "required_review" and self.reason_code != "manual_review":
            raise ValueError("review reason mismatch")
        if self.decision == "restricted" and self.reason_code != "automation_confirmed":
            raise ValueError("restriction reason mismatch")
        if self.decision == "cleared" and self.reason_code not in {
            "false_positive",
            "appeal_approved",
        }:
            raise ValueError("clear reason mismatch")
        return self


class RiskDecisionInfo(RiskModel):
    risk_decision_id: str = Field(pattern=r"^rdc_[0-9a-f]{32}$")
    risk_case_id: str = Field(pattern=r"^rca_[0-9a-f]{32}$")
    decision: Literal["allow", "challenge", "required_review", "restricted", "cleared"]
    safe_reason_family: str = Field(min_length=3, max_length=64)
    policy_version: int = Field(ge=1)
    expires_at: datetime | None
    decided_at: datetime


class RiskCaseDecisionResponse(RiskModel):
    case: RiskCaseInfo
    decision: RiskDecisionInfo
    idempotent_replay: bool
    request_id: str
    trace_id: str


class RiskChallengeRequest(RiskModel):
    challenge_id: str = Field(pattern=r"^rch_[0-9a-f]{32}$")
    challenge_token: str = Field(min_length=32, max_length=256, repr=False)
    answer: str = Field(min_length=6, max_length=128, repr=False)
    revision: int = Field(ge=1)
    confirmed: Literal[True]


class RiskChallengeInfo(RiskModel):
    risk_challenge_id: str = Field(pattern=r"^rch_[0-9a-f]{32}$")
    challenge_kind: Literal["email_otp", "totp", "proof_of_control"]
    status: Literal["pending", "consumed", "failed", "expired"]
    attempts: int = Field(ge=0, le=5)
    max_attempts: int = Field(ge=1, le=5)
    expires_at: datetime
    revision: int = Field(ge=1)


class RiskChallengeResponse(RiskModel):
    challenge: RiskChallengeInfo
    completed: Literal[True]
    idempotent_replay: bool
    request_id: str
    trace_id: str


class RiskAppealRequest(RiskModel):
    restriction_id: str = Field(pattern=r"^rst_[0-9a-f]{32}$")
    category: Literal["not_my_activity", "expected_usage", "restriction_error", "other"]
    confirmed: Literal[True]


class RiskAppealResponse(RiskModel):
    appeal: RiskAppealInfo
    idempotent_replay: bool
    request_id: str
    trace_id: str


class IssuedRiskChallenge(RiskModel):
    challenge: RiskChallengeInfo
    challenge_token: SecretStr = Field(repr=False)
    answer: SecretStr = Field(repr=False)
