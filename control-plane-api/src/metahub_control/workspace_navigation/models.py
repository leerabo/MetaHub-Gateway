"""Strict public DTOs for PRD-146 Workspace navigation."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class NavigationItem(StrictModel):
    route_code: str = Field(min_length=3, max_length=120)
    label_key: str = Field(min_length=3, max_length=160)
    description_key: str = Field(min_length=3, max_length=180)
    kind: Literal["primary", "task_home"]
    order: int = Field(ge=0, le=1000)


class NavigationGroup(StrictModel):
    group_code: str = Field(min_length=2, max_length=60)
    label_key: str = Field(min_length=3, max_length=120)
    expanded: bool
    items: tuple[NavigationItem, ...] = Field(max_length=30)


class NavigationPreferenceProjection(StrictModel):
    revision: int = Field(ge=0)
    last_primary_route_code: str | None = Field(default=None, max_length=120)
    expanded_group_code: str | None = Field(default=None, max_length=60)
    sidebar_collapsed: bool


class WorkspaceNavigationResponse(StrictModel):
    owner_ref: str = Field(min_length=64, max_length=64)
    contract_snapshot_id: str = Field(min_length=36, max_length=36)
    contract_version: Literal["workspace-navigation-v1"]
    contract_generation: int = Field(ge=1)
    projection_generation: int = Field(ge=1)
    projection_status: Literal["ready", "partial", "stale"]
    navigation_epoch: int = Field(ge=1)
    groups: tuple[NavigationGroup, ...] = Field(max_length=8)
    detail_route_codes: tuple[str, ...] = Field(max_length=20)
    developer_route_codes: tuple[str, ...] = Field(max_length=10)
    account_action_codes: tuple[str, ...] = Field(max_length=10)
    advanced_tools_available: bool
    preference: NavigationPreferenceProjection
    item_set_digest: str = Field(min_length=64, max_length=64)
    membership_revision: int = Field(ge=1)
    permission_revision: int = Field(ge=1)
    permission_fingerprint: str = Field(min_length=64, max_length=64)
    observed_at: datetime
    request_id: str
    trace_id: str


class RoleTask(StrictModel):
    task_code: str = Field(min_length=3, max_length=80)
    title_key: str = Field(min_length=3, max_length=140)
    description_key: str = Field(min_length=3, max_length=160)
    status: Literal[
        "not_started",
        "ready",
        "blocked",
        "in_progress",
        "completed",
        "needs_attention",
        "not_applicable",
    ]
    priority: int = Field(ge=0, le=1000)
    action_route_code: str | None = Field(default=None, max_length=120)
    action_enabled: bool
    source_freshness: Literal["fresh", "stale", "unavailable"]

    @model_validator(mode="after")
    def require_action_coherence(self) -> RoleTask:
        expected = self.action_route_code is not None and self.status in {
            "ready",
            "needs_attention",
        }
        if self.action_enabled is not expected:
            raise ValueError("task action state is inconsistent")
        return self


class GettingStartedResponse(StrictModel):
    owner_ref: str = Field(min_length=64, max_length=64)
    role_code: str = Field(min_length=3, max_length=40)
    task_generation: int = Field(ge=1)
    tasks: tuple[RoleTask, ...] = Field(max_length=30)
    next_task_code: str | None = Field(default=None, max_length=80)
    completed_count: int = Field(ge=0)
    needs_attention_count: int = Field(ge=0)
    source_freshness: Literal["fresh", "stale", "unavailable"]
    evidence_digest: str = Field(min_length=64, max_length=64)
    contract_snapshot_id: str = Field(min_length=36, max_length=36)
    contract_generation: int = Field(ge=1)
    navigation_epoch: int = Field(ge=1)
    membership_revision: int = Field(ge=1)
    permission_revision: int = Field(ge=1)
    permission_fingerprint: str = Field(min_length=64, max_length=64)
    observed_at: datetime
    request_id: str
    trace_id: str

    @model_validator(mode="after")
    def require_task_summary_coherence(self) -> GettingStartedResponse:
        enabled = [task.task_code for task in self.tasks if task.action_enabled]
        if (
            self.completed_count != sum(task.status == "completed" for task in self.tasks)
            or self.needs_attention_count
            != sum(task.status == "needs_attention" for task in self.tasks)
            or self.next_task_code != (enabled[0] if enabled else None)
        ):
            raise ValueError("task summary is inconsistent")
        return self


class NavigationPreferencePatch(StrictModel):
    workspace_id: str = Field(min_length=36, max_length=36)
    contract_snapshot_id: str = Field(min_length=36, max_length=36)
    last_primary_route_code: str | None = Field(default=None, max_length=120)
    expanded_group_code: str | None = Field(default=None, max_length=60)
    sidebar_collapsed: bool | None = None

    @model_validator(mode="after")
    def require_change(self) -> NavigationPreferencePatch:
        mutable = {
            "last_primary_route_code",
            "expanded_group_code",
            "sidebar_collapsed",
        }
        if not (self.model_fields_set & mutable):
            raise ValueError("navigation preference change is empty")
        return self


class NavigationPreferenceResponse(StrictModel):
    preference_id: str = Field(min_length=36, max_length=36)
    workspace_ref: str = Field(min_length=64, max_length=64)
    contract_snapshot_id: str = Field(min_length=36, max_length=36)
    contract_generation: int = Field(ge=1)
    navigation_epoch: int = Field(ge=1)
    membership_revision: int = Field(ge=1)
    permission_revision: int = Field(ge=1)
    permission_fingerprint: str = Field(min_length=64, max_length=64)
    last_primary_route_code: str | None = Field(default=None, max_length=120)
    expanded_group_code: str | None = Field(default=None, max_length=60)
    sidebar_collapsed: bool
    revision: int = Field(ge=1)
    changed: bool
    idempotent_replay: bool
    updated_at: datetime
    request_id: str
    trace_id: str


class AdvancedTool(StrictModel):
    tool_code: str = Field(min_length=3, max_length=80)
    route_code: str = Field(min_length=3, max_length=120)
    label_key: str = Field(min_length=3, max_length=140)
    availability: Literal["available", "blocked", "revoked", "not_applicable"]
    reason_code: str | None = Field(default=None, max_length=80)
    action_enabled: bool
    entitlement_revision: int = Field(ge=1)
    observed_at: datetime

    @model_validator(mode="after")
    def require_action_coherence(self) -> AdvancedTool:
        if self.action_enabled is not (self.availability == "available"):
            raise ValueError("advanced tool action state is inconsistent")
        return self


class AdvancedToolsResponse(StrictModel):
    owner_ref: str = Field(min_length=64, max_length=64)
    tools: tuple[AdvancedTool, ...] = Field(max_length=20)
    entitlement_generation: int = Field(ge=1)
    contract_snapshot_id: str = Field(min_length=36, max_length=36)
    contract_generation: int = Field(ge=1)
    navigation_epoch: int = Field(ge=1)
    membership_revision: int = Field(ge=1)
    permission_revision: int = Field(ge=1)
    permission_fingerprint: str = Field(min_length=64, max_length=64)
    observed_at: datetime
    request_id: str
    trace_id: str


class AdvancedToolOpenResponse(StrictModel):
    owner_ref: str = Field(min_length=64, max_length=64)
    tool_code: str = Field(min_length=3, max_length=80)
    route_code: str = Field(min_length=3, max_length=120)
    contract_snapshot_id: str = Field(min_length=36, max_length=36)
    contract_generation: int = Field(ge=1)
    entitlement_revision: int = Field(ge=1)
    navigation_epoch: int = Field(ge=1)
    membership_revision: int = Field(ge=1)
    permission_revision: int = Field(ge=1)
    permission_fingerprint: str = Field(min_length=64, max_length=64)
    idempotent_replay: bool
    request_id: str
    trace_id: str
