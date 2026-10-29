"""Bounded public DTOs for PRD-093 Workspace creation."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class WorkspaceEnvironmentSummary(BaseModel):
    code: Literal["dev"]
    status: Literal["active", "disabled"]
    origin: Literal["system_created", "legacy_existing", "user_created"]


class WorkspaceProvisioningSummary(BaseModel):
    status: Literal["core_committed", "ready", "failed"]


class WorkspaceSummary(BaseModel):
    workspace_id: str
    kind: Literal["organization"]
    status: Literal["active", "migration_conflict"]
    profile_state: Literal["draft", "confirmed", "migration_conflict"]
    name: str
    slug: str | None
    region: str
    service_mode: Literal["internal_only", "public_service"]
    environment: WorkspaceEnvironmentSummary
    provisioning: WorkspaceProvisioningSummary
    revision: int


class WorkspaceResponse(BaseModel):
    workspace: WorkspaceSummary
    request_id: str
    trace_id: str


class WorkspaceListResponse(BaseModel):
    workspaces: tuple[WorkspaceSummary, ...]
    limit: int
    offset: int
    total: int
    request_id: str
    trace_id: str
