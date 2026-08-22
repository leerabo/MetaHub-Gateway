"""Safe public DTOs for the eight-step Workspace onboarding flow."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

OnboardingStepId = Literal[
    "workspace_profile",
    "environment",
    "provider",
    "model",
    "model_alias",
    "project_application",
    "virtual_key",
    "first_call",
]
OnboardingStepStatus = Literal[
    "current", "blocked", "complete", "deferred", "needs_attention"
]
OnboardingStepSource = Literal[
    "system_created", "user_confirmed", "verified", "legacy_seed", "none"
]
OnboardingAction = Literal["confirm", "configure", "recheck", "view"]
OnboardingProgressStatus = Literal["incomplete", "active"]
OnboardingMutationAction = Literal["confirm", "defer", "reopen"]
OnboardingReasonCode = Literal[
    "user_confirmed", "do_later", "configuration_changed", "retry_required"
]


class WorkspaceOnboardingStep(BaseModel):
    """One public step without resource names, secrets, endpoints, or internal IDs."""

    model_config = ConfigDict(extra="forbid")

    id: OnboardingStepId
    order: int = Field(ge=1, le=8)
    title: str
    status: OnboardingStepStatus
    source: OnboardingStepSource
    action: OnboardingAction
    navigation_target: str
    blocking_code: str | None = None
    last_checked_at: datetime


class WorkspaceOnboardingResponse(BaseModel):
    """Current backend-derived onboarding state for the selected Workspace."""

    model_config = ConfigDict(extra="forbid")

    workspace_kind: Literal["personal", "organization"]
    completed_steps: int = Field(ge=0, le=8)
    total_steps: int = 8
    status: OnboardingProgressStatus
    generation: int = Field(ge=0)
    revision: int = Field(ge=1)
    activated_at: datetime | None = None
    evaluated_at: datetime
    steps: tuple[WorkspaceOnboardingStep, ...]
    request_id: str
    trace_id: str


class WorkspaceOnboardingMutationRequest(BaseModel):
    """A user intention; technical completion is never accepted from the client."""

    model_config = ConfigDict(extra="forbid")

    action: OnboardingMutationAction
    reason_code: OnboardingReasonCode
