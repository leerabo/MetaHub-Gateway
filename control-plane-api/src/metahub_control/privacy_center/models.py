"""Strict public DTOs for PRD-134 privacy preferences and subject requests."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ConsentPurpose = Literal["privacy_terms", "analytics", "product_updates", "model_improvement"]
ConsentDecision = Literal["granted", "denied", "withdrawn"]
RequestKind = Literal["export", "rectify", "erase"]
RequestStatus = Literal[
    "received",
    "identity_verified",
    "collecting",
    "reviewing",
    "ready",
    "completed",
    "partially_denied",
    "failed",
    "cancelled",
]
OwnerDomain = Literal[
    "identity", "membership", "audit", "billing", "support", "edge", "data_governance"
]


class PrivacyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PrivacyPreferencePatchRequest(PrivacyModel):
    analytics_enabled: bool | None = None
    product_updates_enabled: bool | None = None
    model_improvement_enabled: bool | None = None
    terms_version: str | None = Field(default=None, min_length=1, max_length=80)
    locale: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{1,19}$")

    @model_validator(mode="after")
    def useful_patch(self) -> PrivacyPreferencePatchRequest:
        if all(
            value is None
            for value in (
                self.analytics_enabled,
                self.product_updates_enabled,
                self.model_improvement_enabled,
                self.terms_version,
                self.locale,
            )
        ):
            raise ValueError("empty patch")
        return self


class ConsentRecordSummary(PrivacyModel):
    consent_record_id: str = Field(pattern=r"^con_[0-9a-f]{32}$")
    purpose: ConsentPurpose
    terms_version: str
    locale: str
    source_kind: Literal["registration", "privacy_center"]
    decision: ConsentDecision
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decided_at: datetime


class PrivacyPreferenceSummary(PrivacyModel):
    privacy_preference_id: str = Field(pattern=r"^prf_[0-9a-f]{32}$")
    workspace_id: str = Field(pattern=r"^wsp_[0-9a-f]{32}$")
    analytics_enabled: bool
    product_updates_enabled: bool
    model_improvement_enabled: bool
    terms_version: str
    locale: str
    revision: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime


class PrivacyPreferenceResponse(PrivacyModel):
    preference: PrivacyPreferenceSummary
    consent_records: tuple[ConsentRecordSummary, ...]
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class DataSubjectRequestCreate(PrivacyModel):
    request_kind: RequestKind
    step_up_grant: str = Field(min_length=43, max_length=128, repr=False)
    confirmed: Literal[True]


class DataSubjectRequestCancel(PrivacyModel):
    step_up_grant: str = Field(min_length=43, max_length=128, repr=False)
    confirmed: Literal[True]


class OwnerProgressSummary(PrivacyModel):
    owner_domain: OwnerDomain
    status: Literal["pending", "running", "completed", "failed", "cancelled"]
    outcome_code: (
        Literal["exported", "rectified", "erased", "anonymized", "retained", "not_found"] | None
    )
    checkpoint_code: str
    attempt_count: int = Field(ge=0, le=8)
    completed_at: datetime | None


class IdentityExportSummary(PrivacyModel):
    identity_export_id: str = Field(pattern=r"^pex_[0-9a-f]{32}$")
    schema_version: Literal[1]
    size_bytes: int = Field(ge=2, le=524_288)
    revision: int = Field(ge=1)
    status: Literal["ready", "downloaded", "expired"]
    receipt_status: Literal["none", "active", "consumed", "expired"]
    ready_at: datetime
    downloaded_at: datetime | None
    expires_at: datetime


class ErasureDecisionSummary(PrivacyModel):
    owner_domain: OwnerDomain
    outcome: Literal["erase", "anonymize", "retain", "not_found"]
    legal_code: Literal["subject_request", "legal_hold", "statutory_retention", "owner_not_found"]
    policy_generation: int = Field(ge=1)
    hold_generation: int = Field(ge=0)
    decided_at: datetime


class RetentionExceptionSummary(PrivacyModel):
    owner_domain: OwnerDomain
    classification_code: Literal["identity", "audit", "billing", "file", "prompt", "response"]
    legal_code: Literal["legal_hold", "statutory_retention"]
    explanation_code: Literal["active_legal_hold", "retention_period_active"]
    policy_generation: int = Field(ge=1)
    hold_generation: int = Field(ge=0)
    expires_at: datetime | None


class DataSubjectRequestSummary(PrivacyModel):
    data_subject_request_id: str = Field(pattern=r"^dsr_[0-9a-f]{32}$")
    request_code: str = Field(pattern=r"^prq_[0-9a-f]{16}$")
    workspace_id: str = Field(pattern=r"^wsp_[0-9a-f]{32}$")
    request_kind: RequestKind
    status: RequestStatus
    revision: int = Field(ge=1)
    deadline_at: datetime
    deadline_warning_at: datetime
    warning_sent: bool
    received_at: datetime
    updated_at: datetime
    terminal_reason_code: str | None
    terminal_at: datetime | None
    owner_progress: tuple[OwnerProgressSummary, ...]
    identity_export: IdentityExportSummary | None
    erasure_decisions: tuple[ErasureDecisionSummary, ...]
    retention_exceptions: tuple[RetentionExceptionSummary, ...]


class DataSubjectRequestResponse(PrivacyModel):
    request: DataSubjectRequestSummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class DataSubjectRequestListResponse(PrivacyModel):
    items: tuple[DataSubjectRequestSummary, ...]
    request_id: str
    trace_id: str


class ExportReceiptResponse(PrivacyModel):
    identity_export_id: str = Field(pattern=r"^pex_[0-9a-f]{32}$")
    receipt: str = Field(min_length=43, max_length=128, repr=False)
    expires_at: datetime
    request_id: str
    trace_id: str


class PlatformPrivacyQueueItem(PrivacyModel):
    request_code: str = Field(pattern=r"^prq_[0-9a-f]{16}$")
    request_kind: RequestKind
    status: RequestStatus
    deadline_at: datetime
    deadline_warning_at: datetime
    warning_sent: bool
    domain_progress: tuple[OwnerProgressSummary, ...]
    legal_outcomes: tuple[Literal["erase", "anonymize", "retain", "not_found"], ...]
    received_at: datetime
    updated_at: datetime


class PlatformPrivacyQueueResponse(PrivacyModel):
    items: tuple[PlatformPrivacyQueueItem, ...]
    request_id: str
    trace_id: str


__all__ = [
    "DataSubjectRequestCancel",
    "DataSubjectRequestCreate",
    "DataSubjectRequestListResponse",
    "DataSubjectRequestResponse",
    "ExportReceiptResponse",
    "PlatformPrivacyQueueResponse",
    "PrivacyPreferencePatchRequest",
    "PrivacyPreferenceResponse",
]
