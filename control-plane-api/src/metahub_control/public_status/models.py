"""Closed PRD-112 public status, incident, maintenance, and subscription contracts."""

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


class StatusModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class StatusComponentInfo(StatusModel):
    code: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,47}$")
    name: str = Field(min_length=2, max_length=80)
    group: Literal["gateway", "control", "identity", "billing", "integrations"]
    status: Literal[
        "operational", "degraded_performance", "partial_outage", "major_outage", "maintenance"
    ]
    revision: int = Field(ge=1)


class IncidentUpdateInfo(StatusModel):
    incident_update_id: str = Field(pattern=r"^iup_[0-9a-f]{32}$")
    status: Literal["published", "monitoring", "resolved"]
    message: str = Field(min_length=10, max_length=2000)
    affected_components: tuple[str, ...] = Field(max_length=50)
    affected_regions: tuple[str, ...] = Field(max_length=50)
    published_at: datetime


class PostmortemInfo(StatusModel):
    summary: str = Field(min_length=20, max_length=4000)
    timeline: tuple[str, ...] = Field(min_length=1, max_length=100)
    cause_category: Literal[
        "capacity", "dependency", "deployment", "configuration", "software", "network", "other"
    ]
    corrective_actions: tuple[str, ...] = Field(min_length=1, max_length=100)
    published_at: datetime


class IncidentInfo(StatusModel):
    incident_id: str = Field(pattern=r"^inc_[0-9a-f]{32}$")
    reference: str = Field(min_length=8, max_length=32)
    title: str = Field(min_length=5, max_length=160)
    classification: Literal["reliability", "security"]
    visibility: Literal["public", "workspace"]
    severity: Literal["minor", "major", "critical"]
    status: Literal[
        "draft",
        "pending_security_review",
        "published",
        "monitoring",
        "resolved",
        "postmortem_published",
    ]
    updates: tuple[IncidentUpdateInfo, ...] = Field(max_length=500)
    postmortem: PostmortemInfo | None = None
    security_approval_available: bool
    revision: int = Field(ge=1)
    acknowledged_at: datetime
    updated_at: datetime


class MaintenanceWindowInfo(StatusModel):
    maintenance_window_id: str = Field(pattern=r"^mtn_[0-9a-f]{32}$")
    reference: str = Field(min_length=8, max_length=32)
    title: str = Field(min_length=5, max_length=160)
    message: str = Field(min_length=10, max_length=2000)
    visibility: Literal["public", "workspace"]
    status: Literal["planned", "in_progress", "completed", "cancelled"]
    affected_components: tuple[str, ...] = Field(max_length=50)
    affected_regions: tuple[str, ...] = Field(max_length=50)
    starts_at: datetime
    ends_at: datetime
    revision: int = Field(ge=1)


class StatusWorkspaceResponse(StatusModel):
    components: tuple[StatusComponentInfo, ...]
    incidents: tuple[IncidentInfo, ...]
    maintenance_windows: tuple[MaintenanceWindowInfo, ...]
    bundle_generation: int | None = Field(default=None, ge=1)
    request_id: str
    trace_id: str


class IncidentCreateRequest(StatusModel):
    owner_workspace_id: str = Field(pattern=r"^wsp_[0-9a-f]{32}$")
    title: str = Field(min_length=5, max_length=160)
    classification: Literal["reliability", "security"]
    visibility: Literal["public", "workspace"]
    severity: Literal["minor", "major", "critical"]
    acknowledged_at: RequestTimestamp
    initial_update: str | None = Field(default=None, min_length=10, max_length=2000)
    affected_components: list[str] = Field(default_factory=list, max_length=50)
    affected_regions: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def validate_initial_update(self) -> IncidentCreateRequest:
        if (self.classification == "reliability") != (self.initial_update is not None):
            raise ValueError("reliability incidents require exactly one initial update")
        return self


class IncidentApprovalRequest(StatusModel):
    revision: int = Field(ge=1)
    public_message: str = Field(min_length=10, max_length=2000)
    affected_components: list[str] = Field(default_factory=list, max_length=50)
    affected_regions: list[str] = Field(default_factory=list, max_length=50)


class IncidentUpdateRequest(StatusModel):
    revision: int = Field(ge=1)
    status: Literal["monitoring", "resolved"]
    public_message: str = Field(min_length=10, max_length=2000)
    affected_components: list[str] = Field(default_factory=list, max_length=50)
    affected_regions: list[str] = Field(default_factory=list, max_length=50)


class MaintenanceCreateRequest(StatusModel):
    owner_workspace_id: str = Field(pattern=r"^wsp_[0-9a-f]{32}$")
    title: str = Field(min_length=5, max_length=160)
    public_message: str = Field(min_length=10, max_length=2000)
    visibility: Literal["public", "workspace"]
    affected_components: list[str] = Field(default_factory=list, max_length=50)
    affected_regions: list[str] = Field(default_factory=list, max_length=50)
    starts_at: RequestTimestamp
    ends_at: RequestTimestamp

    @model_validator(mode="after")
    def validate_window(self) -> MaintenanceCreateRequest:
        if self.ends_at <= self.starts_at:
            raise ValueError("maintenance end must follow start")
        return self


class PostmortemPublishRequest(StatusModel):
    revision: int = Field(ge=1)
    summary: str = Field(min_length=20, max_length=4000)
    timeline: list[str] = Field(min_length=1, max_length=100)
    cause_category: Literal[
        "capacity", "dependency", "deployment", "configuration", "software", "network", "other"
    ]
    corrective_actions: list[str] = Field(min_length=1, max_length=100)


class IncidentResponse(StatusModel):
    incident: IncidentInfo
    idempotent_replay: bool
    request_id: str
    trace_id: str


class MaintenanceResponse(StatusModel):
    maintenance_window: MaintenanceWindowInfo
    idempotent_replay: bool
    request_id: str
    trace_id: str


class StatusSubscriptionInfo(StatusModel):
    status_subscription_id: str = Field(pattern=r"^sts_[0-9a-f]{32}$")
    channel: Literal["email"]
    scope: dict[str, object]
    status: Literal["pending", "active", "unsubscribed", "expired"]
    revision: int = Field(ge=1)
    created_at: datetime


class StatusSubscriptionListResponse(StatusModel):
    subscriptions: tuple[StatusSubscriptionInfo, ...]
    create_intent: str = Field(min_length=40, max_length=4096)
    request_id: str
    trace_id: str


class StatusSubscriptionMutationRequest(StatusModel):
    operation: Literal["create", "verify", "unsubscribe"]
    create_intent: str | None = Field(default=None, min_length=40, max_length=4096)
    channel: Literal["email"] | None = None
    contact: str | None = Field(default=None, min_length=3, max_length=320)
    scope: dict[str, object] | None = None
    token: str | None = Field(default=None, min_length=32, max_length=256)

    @model_validator(mode="after")
    def validate_operation(self) -> StatusSubscriptionMutationRequest:
        create_fields = (
            self.create_intent is not None or self.contact is not None or self.scope is not None
        )
        if self.operation == "create":
            if self.create_intent is None or self.channel != "email" or self.contact is None:
                raise ValueError("create fields required")
            if self.token is not None:
                raise ValueError("token not accepted for create")
        elif create_fields or self.channel is not None or self.token is None:
            raise ValueError("only token accepted for token operation")
        return self


class StatusSubscriptionMutationResponse(StatusModel):
    accepted: Literal[True]
    request_id: str
    trace_id: str


__all__ = [name for name in globals() if name.endswith(("Info", "Request", "Response"))]
