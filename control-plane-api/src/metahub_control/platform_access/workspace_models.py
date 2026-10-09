"""Strict PRD-096 platform Workspace operations response models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from .models import PlatformResponseModel

OperationalStatus = Literal[
    "active",
    "suspended",
    "deletion_pending",
    "deleted",
    "migration_conflict",
]
RiskTag = Literal[
    "security_review",
    "payment_attention",
    "adoption_attention",
    "compliance_hold",
]
HealthRiskTag = Literal[
    "usage_declining",
    "high_error_rate",
    "cost_anomaly_open",
    "budget_missing",
    "team_inactive",
]


class WorkspaceHealthSummary(PlatformResponseModel):
    status: Literal["fresh", "stale", "not_ready", "unavailable"]
    score_band: Literal["healthy", "watch", "at_risk", "critical"] | None = None
    risk_level: Literal["healthy", "watch", "at_risk", "critical"] | None = None
    risk_tags: tuple[HealthRiskTag, ...] | None = None
    evaluated_at: datetime | None = None


class PlatformWorkspaceSummary(PlatformResponseModel):
    workspace_id: str
    kind: Literal["organization"]
    display_name: str
    slug: str | None
    created_at: datetime
    operational_status: OperationalStatus
    operational_revision: int = Field(ge=1)
    risk_revision: int = Field(ge=1)
    region_code: str | None
    residency_policy_version: str | None
    member_count: int = Field(ge=0)
    active_session_count: int = Field(ge=0)
    plan_status: Literal["not_available"] | None = None
    deployment_status: Literal["not_configured"] = "not_configured"
    data_lifecycle_status: Literal["active", "deletion_requested", "deleted", "conflict"]
    latest_export_status: Literal["pending", "processing", "completed", "failed", "expired"] | None
    latest_deletion_status: (
        Literal["requested", "quiescing", "deleting", "completed", "failed"] | None
    )
    gateway_status: Literal["not_applicable", "transition_pending"]
    health: WorkspaceHealthSummary
    platform_risk_tags: tuple[RiskTag, ...] | None = None
    owner_contact_status: Literal["available", "unavailable"] | None = None
    last_notification_status: Literal["pending", "sent", "failed"] | None = None
    updated_at: datetime
    private_gateway_enabled: bool = False
    private_gateway_revision: int = Field(ge=1)


class PlatformWorkspaceListResponse(PlatformResponseModel):
    items: tuple[PlatformWorkspaceSummary, ...]
    limit: int = Field(ge=1, le=100)
    has_more: bool
    next_cursor: str | None
    request_id: str
    trace_id: str


class PlatformActionSummary(PlatformResponseModel):
    action_intent_id: str
    action_type: Literal[
        "suspend", "restore", "risk_tag_add", "risk_tag_remove", "owner_notification"
    ]
    reason_code: str
    result_revision: int = Field(ge=1)
    created_at: datetime


class PlatformWorkspaceDetailResponse(PlatformResponseModel):
    workspace: PlatformWorkspaceSummary
    recent_actions: tuple[PlatformActionSummary, ...]
    request_id: str
    trace_id: str


class PlatformWorkspaceHealthResponse(PlatformResponseModel):
    workspace_id: str
    health: WorkspaceHealthSummary
    request_id: str
    trace_id: str


class PlatformWorkspaceMutationResponse(PlatformResponseModel):
    action_intent_id: str
    workspace: PlatformWorkspaceSummary
    request_id: str
    trace_id: str


class PlatformWorkspacePrivateGatewayResponse(PlatformResponseModel):
    workspace_id: str
    tenant_id: str
    enabled: bool
    revision: int = Field(ge=1)
    updated_at: datetime
    request_id: str
    trace_id: str


class OwnerNotificationResponse(PlatformResponseModel):
    action_intent_id: str
    workspace_id: str
    notification_status: Literal["pending", "sent", "failed"]
    request_id: str
    trace_id: str


class WorkspaceOperationalEventSummary(PlatformResponseModel):
    event_id: str
    event_type: Literal[
        "workspace.suspended",
        "workspace.restored",
        "workspace.risk_tag_added",
        "workspace.risk_tag_removed",
        "workspace.owner_notification_requested",
    ]
    reason_code: str
    operational_revision: int = Field(ge=1)
    risk_revision: int = Field(ge=1)
    occurred_at: datetime


class WorkspaceOperationalEventListResponse(PlatformResponseModel):
    events: tuple[WorkspaceOperationalEventSummary, ...]
    limit: int = Field(ge=1, le=100)
    has_more: bool
    request_id: str
    trace_id: str
