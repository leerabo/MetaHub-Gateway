"""Bounded public DTOs for PRD-094 Membership and Workspace switching."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

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


class WorkspaceSessionResponse(BaseModel):
    context: WorkspaceContextSummary
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
