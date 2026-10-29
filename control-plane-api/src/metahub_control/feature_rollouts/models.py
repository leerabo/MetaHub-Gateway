"""Strict public DTOs for server-authoritative feature rollouts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FeatureResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class FeatureRuleSet(FeatureResponseModel):
    environments: list[Literal["development", "staging", "production"]] = Field(
        default_factory=lambda: ["production"]
    )
    regions: list[str] = Field(default_factory=list)
    cohort_code: str | None = Field(
        default=None, min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9_.-]+$"
    )
    percentage_bps: int = Field(ge=0, le=10000)
    prerequisite_flag_code: str | None = Field(
        default=None, min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9_.-]+$"
    )

    @model_validator(mode="after")
    def validate_collections(self) -> FeatureRuleSet:
        if not self.environments or len(set(self.environments)) != len(self.environments):
            raise ValueError("environments must be unique")
        if len(self.regions) > 16 or len(set(self.regions)) != len(self.regions):
            raise ValueError("regions must be unique")
        if any(
            len(region) < 3
            or len(region) > 40
            or not region.replace("-", "").isalnum()
            or region.lower() != region
            for region in self.regions
        ):
            raise ValueError("region is invalid")
        return self


class FeatureFlagCreateRequest(FeatureResponseModel):
    action: Literal["create", "version"] = "create"
    flag_id: str | None = Field(
        default=None, min_length=36, max_length=36, pattern=r"^ffg_[0-9a-f]{32}$"
    )
    flag_code: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=120)
    risk_level: Literal["standard", "p0"]
    rules: FeatureRuleSet
    confirmed: Literal[True]

    @model_validator(mode="after")
    def validate_action_fields(self) -> FeatureFlagCreateRequest:
        if (self.action == "create") != (self.flag_id is None):
            raise ValueError("flag action fields are invalid")
        return self


class FeatureRolloutCommandRequest(FeatureResponseModel):
    action: Literal["start", "approve", "pause", "resume", "rollback"]
    rollout_id: str | None = Field(
        default=None, min_length=36, max_length=36, pattern=r"^fro_[0-9a-f]{32}$"
    )
    version_number: int | None = Field(default=None, ge=1)
    percentage_bps: int | None = Field(default=None, ge=0, le=10000)
    environment_code: Literal["development", "staging", "production"] | None = None
    region_code: str | None = Field(
        default=None, min_length=3, max_length=40, pattern=r"^[a-z0-9][a-z0-9-]+$"
    )
    reason_code: Literal[
        "release_start",
        "health_recovery",
        "operator_pause",
        "release_rollback",
    ]
    confirmed: Literal[True]

    @model_validator(mode="after")
    def validate_action_fields(self) -> FeatureRolloutCommandRequest:
        starting = self.action == "start"
        if starting != (
            self.rollout_id is None
            and self.version_number is not None
            and self.percentage_bps is not None
            and self.environment_code is not None
            and self.region_code is not None
        ):
            raise ValueError("rollout action fields are invalid")
        if not starting and (
            self.rollout_id is None
            or self.version_number is not None
            or self.percentage_bps is not None
            or self.environment_code is not None
            or self.region_code is not None
        ):
            raise ValueError("rollout action fields are invalid")
        expected_reason = {
            "start": "release_start",
            "approve": "release_start",
            "pause": "operator_pause",
            "resume": "health_recovery",
            "rollback": "release_rollback",
        }[self.action]
        if self.reason_code != expected_reason:
            raise ValueError("rollout reason is invalid")
        return self


class FeatureKillRequest(FeatureResponseModel):
    reason_code: Literal[
        "incident_containment", "security_containment", "release_rollback"
    ]
    confirmed: Literal[True]


class FeatureRolloutInfo(FeatureResponseModel):
    rollout_id: str
    action: Literal["start", "resume", "rollback", "kill"]
    status: Literal[
        "pending_approval",
        "scheduled",
        "running",
        "paused",
        "completed",
        "rolled_back",
        "killed",
        "failed",
    ]
    version_number: int = Field(ge=1)
    target_percentage_bps: int = Field(ge=0, le=10000)
    environment_code: Literal["development", "staging", "production"]
    region_code: str
    health_status: Literal["pending", "healthy", "unhealthy", "unavailable"]
    health_source_watermark: int | None = Field(default=None, ge=1)
    approval_count: int = Field(ge=0, le=2)
    revision: int = Field(ge=1)
    job_id: str | None = None
    created_at: datetime
    updated_at: datetime


class FeatureFlagVersionInfo(FeatureResponseModel):
    version_number: int = Field(ge=1)
    rules: FeatureRuleSet
    exposure_count: int = Field(ge=0)
    created_at: datetime


class FeatureFlagInfo(FeatureResponseModel):
    flag_id: str
    flag_code: str
    display_name: str
    risk_level: Literal["standard", "p0"]
    status: Literal["active", "killed", "disabled"]
    current_version: int = Field(ge=1)
    rollout_percentage_bps: int = Field(ge=0, le=10000)
    revision: int = Field(ge=1)
    kill_generation: int = Field(ge=0)
    kill_reason_code: str | None = None
    exposure_count: int = Field(ge=0)
    rules: FeatureRuleSet
    versions: tuple[FeatureFlagVersionInfo, ...]
    latest_rollout: FeatureRolloutInfo | None = None
    created_at: datetime
    updated_at: datetime


class FeatureFlagListResponse(FeatureResponseModel):
    flags: tuple[FeatureFlagInfo, ...]
    total: int = Field(ge=0)
    request_id: str
    trace_id: str


class FeatureFlagResponse(FeatureResponseModel):
    flag: FeatureFlagInfo
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class FeatureRolloutResponse(FeatureResponseModel):
    flag: FeatureFlagInfo
    rollout: FeatureRolloutInfo
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class WorkspaceCapability(FeatureResponseModel):
    flag_code: str
    enabled: bool
    public_version: int = Field(ge=1)
    reason_code: Literal[
        "enabled",
        "percentage_excluded",
        "environment_excluded",
        "region_excluded",
        "cohort_excluded",
        "prerequisite_disabled",
        "entitlement_denied",
        "workspace_disabled",
        "flag_killed",
        "flag_disabled",
        "not_released",
    ]


class WorkspaceCapabilitiesResponse(FeatureResponseModel):
    capabilities: tuple[WorkspaceCapability, ...]
    evaluated_at: datetime
    request_id: str
    trace_id: str


__all__ = [
    "FeatureFlagCreateRequest",
    "FeatureFlagInfo",
    "FeatureFlagListResponse",
    "FeatureFlagResponse",
    "FeatureFlagVersionInfo",
    "FeatureKillRequest",
    "FeatureRolloutCommandRequest",
    "FeatureRolloutInfo",
    "FeatureRolloutResponse",
    "FeatureRuleSet",
    "WorkspaceCapabilitiesResponse",
    "WorkspaceCapability",
]
