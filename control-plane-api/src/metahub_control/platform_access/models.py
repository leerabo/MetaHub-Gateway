"""Bounded response models for PRD-095 Platform IAM."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PlatformRole = Literal[
    "platform_owner",
    "platform_ops",
    "platform_security",
    "platform_billing",
    "platform_support",
    "platform_verification",
    "platform_compliance",
    "platform_privacy",
    "release_manager",
    "platform_provider_engineer",
    "platform_channel",
    "platform_customer_success",
    "platform_growth",
]


class PlatformResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlatformOperatorSummary(PlatformResponseModel):
    operator_id: str
    display_name: str
    status: Literal["active", "disabled"]
    roles: tuple[PlatformRole, ...]
    revision: int = Field(ge=1)


class PlatformSessionContext(PlatformResponseModel):
    operator: PlatformOperatorSummary
    permissions: tuple[str, ...]
    expires_at: datetime


class PlatformSessionResponse(PlatformResponseModel):
    context: PlatformSessionContext
    request_id: str
    trace_id: str


class PlatformBootstrapResponse(PlatformResponseModel):
    completed: Literal[True] = True
    operator: PlatformOperatorSummary
    request_id: str
    trace_id: str


class PlatformOperatorResponse(PlatformResponseModel):
    operator: PlatformOperatorSummary
    request_id: str
    trace_id: str


class PlatformOperatorListResponse(PlatformResponseModel):
    operators: tuple[PlatformOperatorSummary, ...]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)
    request_id: str
    trace_id: str


class PlatformNavigationItem(PlatformResponseModel):
    id: str
    label: str
    href: str


class PlatformNavigationResponse(PlatformResponseModel):
    items: tuple[PlatformNavigationItem, ...]
    request_id: str
    trace_id: str


class SupportGrantSummary(PlatformResponseModel):
    grant_id: str
    workspace_id: str
    scope: Literal["workspace.diagnostics.read"] = "workspace.diagnostics.read"
    status: Literal["pending_approvals", "active", "revoked", "expired"]
    missing_approvals: tuple[Literal["workspace", "platform"], ...]
    expires_at: datetime


class SupportGrantResponse(PlatformResponseModel):
    grant: SupportGrantSummary
    request_id: str
    trace_id: str


class SupportGrantListResponse(PlatformResponseModel):
    grants: tuple[SupportGrantSummary, ...]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)
    request_id: str
    trace_id: str


class SupportSessionSummary(PlatformResponseModel):
    source_kind: Literal["grant", "break_glass"]
    source_id: str
    workspace_id: str
    scope: Literal["workspace.diagnostics.read"] = "workspace.diagnostics.read"
    expires_at: datetime


class SupportSessionResponse(PlatformResponseModel):
    session: SupportSessionSummary
    request_id: str
    trace_id: str


class BreakGlassReviewResponse(PlatformResponseModel):
    break_glass_id: str
    status: Literal["active", "review_pending", "reviewed", "expired"]
    missing_reviews: tuple[Literal["workspace", "platform"], ...]
    review_due_at: datetime
    request_id: str
    trace_id: str


class BreakGlassSessionResponse(PlatformResponseModel):
    break_glass_id: str
    session: SupportSessionSummary
    review_due_at: datetime
    request_id: str
    trace_id: str


class SupportDiagnosticsResponse(PlatformResponseModel):
    workspace_id: str
    operational_status: Literal["active", "restricted"]
    region_code: str | None
    provisioning_status: Literal["ready", "attention_required"]
    health_band: Literal["healthy", "watch", "at_risk", "critical"] | None
    active_memberships: int = Field(ge=0)
    request_id: str
    trace_id: str


class PlatformAuditSummary(PlatformResponseModel):
    audit_id: str
    action: str
    object_type: str
    object_id: str
    reason_code: str
    occurred_at: datetime


class PlatformAuditListResponse(PlatformResponseModel):
    entries: tuple[PlatformAuditSummary, ...]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    next_cursor: str | None
    request_id: str
    trace_id: str
