"""Strict non-secret DTOs for Workspace SCIM administration."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ScimAdminModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ScimTokenCreateRequest(ScimAdminModel):
    expires_in_days: int = Field(ge=1, le=365)


class ScimTokenSummary(ScimAdminModel):
    scim_token_id: str = Field(pattern=r"^sct_[0-9a-f]{32}$")
    token_prefix: str = Field(min_length=8, max_length=20)
    status: Literal["active", "rotating", "revoked"]
    revision: int = Field(ge=1)
    expires_at: datetime
    grace_expires_at: datetime | None
    last_used_at: datetime | None


class ScimTokenResponse(ScimAdminModel):
    token: ScimTokenSummary
    bearer_token: str | None = Field(default=None, min_length=32, max_length=256)
    idempotent_replay: bool
    request_id: str = Field(min_length=8, max_length=160)
    trace_id: str = Field(min_length=8, max_length=160)


class ScimRoleMappingRequest(ScimAdminModel):
    scim_group_link_id: str = Field(pattern=r"^sgl_[0-9a-f]{32}$")
    role_code: Literal[
        "admin",
        "developer",
        "security_admin",
        "finance_admin",
        "legal_admin",
        "auditor",
        "viewer",
    ]


class ScimGroupSummary(ScimAdminModel):
    scim_group_link_id: str = Field(pattern=r"^sgl_[0-9a-f]{32}$")
    display_name: str = Field(min_length=1, max_length=120)
    state: Literal["active", "disabled"]
    revision: int = Field(ge=1)
    updated_at: datetime


class ScimRoleMappingSummary(ScimAdminModel):
    scim_role_mapping_id: str = Field(pattern=r"^srm_[0-9a-f]{32}$")
    scim_group_link_id: str = Field(pattern=r"^sgl_[0-9a-f]{32}$")
    group_display_name: str = Field(min_length=1, max_length=120)
    role_code: str = Field(min_length=2, max_length=40)
    status: Literal["active", "disabled"]
    revision: int = Field(ge=1)
    updated_at: datetime


class ScimRoleMappingResponse(ScimAdminModel):
    mapping: ScimRoleMappingSummary
    idempotent_replay: bool
    request_id: str = Field(min_length=8, max_length=160)
    trace_id: str = Field(min_length=8, max_length=160)


class ScimSyncSummary(ScimAdminModel):
    scim_sync_run_id: str = Field(pattern=r"^ssr_[0-9a-f]{32}$")
    status: Literal["running", "completed", "failed", "interrupted"]
    created_count: int = Field(ge=0)
    updated_count: int = Field(ge=0)
    disabled_count: int = Field(ge=0)
    conflict_count: int = Field(ge=0)
    safe_outcome_code: str = Field(pattern=r"^[a-z0-9][a-z0-9._]{1,79}$")
    started_at: datetime
    completed_at: datetime | None


class ScimConflictSummary(ScimAdminModel):
    scim_conflict_id: str = Field(pattern=r"^scf_[0-9a-f]{32}$")
    subject_type: Literal["user", "group", "membership"]
    reason_code: Literal[
        "owner_protected",
        "human_membership",
        "revision_stale",
        "duplicate_external_id",
        "account_unverified",
        "role_conflict",
    ]
    status: Literal["open", "resolved", "ignored"]
    revision: int = Field(ge=1)
    created_at: datetime
    resolved_at: datetime | None


class ScimConflictResolveRequest(ScimAdminModel):
    resolution_code: Literal["keep_local", "accept_directory", "ignore_directory"]


class ScimConflictResponse(ScimAdminModel):
    conflict: ScimConflictSummary
    idempotent_replay: bool
    request_id: str = Field(min_length=8, max_length=160)
    trace_id: str = Field(min_length=8, max_length=160)


class ScimDirectorySummaryResponse(ScimAdminModel):
    workspace_id: str = Field(pattern=r"^wsp_[0-9a-f]{32}$")
    tokens: tuple[ScimTokenSummary, ...] = Field(max_length=10)
    groups: tuple[ScimGroupSummary, ...] = Field(max_length=100)
    role_mappings: tuple[ScimRoleMappingSummary, ...] = Field(max_length=100)
    sync_runs: tuple[ScimSyncSummary, ...] = Field(max_length=100)
    conflicts: tuple[ScimConflictSummary, ...] = Field(max_length=100)
    pending_invitation_count: int = Field(ge=0)
    linked_user_count: int = Field(ge=0)
    linked_group_count: int = Field(ge=0)
    request_id: str = Field(min_length=8, max_length=160)
    trace_id: str = Field(min_length=8, max_length=160)


class ScimSyncListResponse(ScimAdminModel):
    sync_runs: tuple[ScimSyncSummary, ...] = Field(max_length=100)
    request_id: str = Field(min_length=8, max_length=160)
    trace_id: str = Field(min_length=8, max_length=160)


__all__ = [
    "ScimConflictResolveRequest",
    "ScimConflictResponse",
    "ScimDirectorySummaryResponse",
    "ScimGroupSummary",
    "ScimRoleMappingRequest",
    "ScimRoleMappingResponse",
    "ScimSyncListResponse",
    "ScimTokenCreateRequest",
    "ScimTokenResponse",
]
