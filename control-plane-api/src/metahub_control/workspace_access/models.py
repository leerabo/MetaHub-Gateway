"""Bounded public DTOs for PRD-094 Membership and Workspace switching."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

WorkspaceRole = Literal[
    "owner",
    "admin",
    "developer",
    "security_admin",
    "finance_admin",
    "legal_admin",
    "auditor",
    "viewer",
    "service_account",
]


class IdentityWorkspaceSummary(BaseModel):
    workspace_id: str
    kind: Literal["personal", "organization"]
    name: str
    slug: str | None
    role: WorkspaceRole


class IdentityWorkspaceListResponse(BaseModel):
    workspaces: tuple[IdentityWorkspaceSummary, ...]
    limit: int
    offset: int
    total: int
    request_id: str
    trace_id: str


class WorkspaceContextSummary(IdentityWorkspaceSummary):
    session_status: Literal["active"] = "active"
    expires_at: datetime


class WorkspaceSwitchResponse(BaseModel):
    context: WorkspaceContextSummary
    request_id: str
    trace_id: str


class PartnerHandoffResponse(BaseModel):
    """Confirmation that the browser left its Workspace credential domain."""

    handed_off: Literal[True] = True
    request_id: str
    trace_id: str


class WorkspaceAuthorizationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    permission_codes: tuple[str, ...] = Field(max_length=300)
    permission_revision: int = Field(ge=1)
    permission_fingerprint: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    membership_revision: int = Field(ge=1)


class WorkspaceSessionResponse(BaseModel):
    context: WorkspaceContextSummary
    authorization: WorkspaceAuthorizationSummary
    request_id: str
    trace_id: str


class MembershipSummary(BaseModel):
    membership_id: str
    display_name: str
    role: WorkspaceRole
    status: Literal["pending", "active", "disabled"]
    revision: int
    is_current: bool
    can_manage: bool


class MembershipListResponse(BaseModel):
    memberships: tuple[MembershipSummary, ...]
    limit: int
    offset: int
    total: int
    request_id: str
    trace_id: str


class MembershipResponse(BaseModel):
    membership: MembershipSummary
    request_id: str
    trace_id: str


class MembershipInvitationSummary(BaseModel):
    invitation_id: str
    role: WorkspaceRole
    status: Literal["pending"] = "pending"
    expires_at: datetime


class MembershipInvitationResponse(BaseModel):
    invitation: MembershipInvitationSummary
    accept_token: str
    request_id: str
    trace_id: str


class OwnerTransferResponse(BaseModel):
    transferred: Literal[True] = True
    source: MembershipSummary
    target: MembershipSummary
    request_id: str
    trace_id: str


class ConditionalAccessModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConditionalAccessPolicySummary(ConditionalAccessModel):
    policy_id: str
    workspace_id: str
    mode: Literal["audit", "enforced"]
    require_mfa: bool
    require_step_up_for_high_risk: bool
    allow_trusted_devices: bool
    region_rule: Literal["any", "domestic_only"]
    network_rule: Literal["any", "trusted_only"]
    allowed_cidrs: tuple[str, ...]
    exception_membership_ids: tuple[str, ...]
    revision: int = Field(ge=1)
    simulation_watermark_at: datetime | None
    updated_at: datetime


class ConditionalAccessSimulationSummary(ConditionalAccessModel):
    simulation_id: str
    policy_revision: int = Field(ge=1)
    outcome_code: Literal["simulation.ready", "simulation.owner_blocked"]
    affected_session_count: int = Field(ge=0)
    owner_ready: bool
    simulated_at: datetime


class ConditionalAccessResponse(ConditionalAccessModel):
    policy: ConditionalAccessPolicySummary | None
    latest_simulation: ConditionalAccessSimulationSummary | None
    request_id: str
    trace_id: str


class ConditionalAccessSimulationResponse(ConditionalAccessModel):
    policy: ConditionalAccessPolicySummary
    simulation: ConditionalAccessSimulationSummary
    request_id: str
    trace_id: str
