"""Strict HTTP contracts for PRD-127 Fleet releases and upgrades."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from metahub_shared.private_gateway import (
    GatewayRelease,
    UpgradeAuthority,
    UpgradeCommandEnvelope,
    UpgradeExecutionReport,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PlatformGatewayReleaseMutationRequest(_StrictModel):
    action: Literal["submit", "approve", "withdraw"]
    release_id: str | None = Field(default=None, pattern=r"^ger_[0-9a-f]{32}$")
    installer_release_id: str | None = Field(default=None, pattern=r"^irl_[0-9a-f]{32}$")
    release_channel: Literal["preview", "stable", "extended"] | None = None
    schema_from: int | None = Field(default=None, ge=1, le=9999)
    schema_to: int | None = Field(default=None, ge=1, le=9999)
    down_migration_supported: bool | None = None
    compatible_from_versions: list[str] | None = Field(
        default=None, min_length=1, max_length=8
    )
    expected_revision: int | None = Field(default=None, ge=1)
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,79}$")

    @model_validator(mode="after")
    def _fields_match_action(self) -> PlatformGatewayReleaseMutationRequest:
        submit = self.action == "submit"
        if submit != (
            self.release_id is None
            and self.installer_release_id is not None
            and self.release_channel is not None
            and self.schema_from is not None
            and self.schema_to is not None
            and self.down_migration_supported is not None
            and self.compatible_from_versions is not None
            and self.expected_revision is None
        ):
            raise ValueError("fleet.release_request_invalid")
        if not submit and (
            self.release_id is None
            or self.installer_release_id is not None
            or self.release_channel is not None
            or self.schema_from is not None
            or self.schema_to is not None
            or self.down_migration_supported is not None
            or self.compatible_from_versions is not None
            or self.expected_revision is None
        ):
            raise ValueError("fleet.release_request_invalid")
        return self


class PlatformRolloutCreateRequest(_StrictModel):
    deployment_ids: list[str] = Field(min_length=1, max_length=1000)
    canary_basis_points: int = Field(default=100, ge=100, le=10000)
    observation_seconds: int = Field(default=1800, ge=1800, le=86400)
    batch_size: int = Field(default=50, ge=1, le=1000)
    failure_threshold_basis_points: int = Field(default=1000, ge=1, le=10000)
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,79}$")

    @model_validator(mode="after")
    def _unique_targets(self) -> PlatformRolloutCreateRequest:
        if (
            len(set(self.deployment_ids)) != len(self.deployment_ids)
            or any(
                len(item) != 36
                or not item.startswith("edp_")
                or any(character not in "0123456789abcdef" for character in item[4:])
                for item in self.deployment_ids
            )
        ):
            raise ValueError("fleet.rollout_request_invalid")
        return self


class FleetRolloutMutationRequest(_StrictModel):
    action: Literal["pause", "resume"]
    expected_revision: int = Field(ge=1)
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,79}$")


class MaintenanceWindowUpdateRequest(_StrictModel):
    release_channel: Literal["preview", "stable", "extended"]
    timezone_name: str = Field(
        min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_+./-]+$"
    )
    weekdays: list[int] = Field(min_length=1, max_length=7)
    start_minute: int = Field(ge=0, le=1439)
    duration_minutes: int = Field(ge=30, le=720)
    freeze_starts_at: datetime | None = None
    freeze_ends_at: datetime | None = None
    status: Literal["active", "disabled"] = "active"
    expected_revision: int = Field(ge=0)
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,79}$")

    @model_validator(mode="after")
    def _consistent(self) -> MaintenanceWindowUpdateRequest:
        if (
            len(set(self.weekdays)) != len(self.weekdays)
            or any(type(item) is not int or not 0 <= item <= 6 for item in self.weekdays)
            or (self.freeze_starts_at is None) != (self.freeze_ends_at is None)
            or (
                self.freeze_starts_at is not None
                and self.freeze_ends_at <= self.freeze_starts_at
            )
        ):
            raise ValueError("edge_upgrade.maintenance_invalid")
        return self


class RollbackCreateRequest(_StrictModel):
    target_release_id: str | None = Field(default=None, pattern=r"^ger_[0-9a-f]{32}$")
    expected_head_revision: int = Field(ge=1)
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,79}$")


class AgentUpgradeMutationRequest(_StrictModel):
    action: Literal["lease", "start"]
    lease_nonce: str = Field(min_length=32, max_length=128, repr=False)
    expected_revision: int = Field(ge=1)


class AgentUpgradeReportRequest(_StrictModel):
    report: UpgradeExecutionReport
    attempt_nonce: str = Field(pattern=r"^[0-9a-f]{64}$", repr=False)


class GatewayReleaseSummary(_StrictModel):
    release: GatewayRelease
    status: Literal["approval_pending", "released", "withdrawn"]
    revision: int = Field(ge=1)
    submitted_by_operator_id: str
    approved_by_operator_id: str | None = None
    submitted_at: datetime
    approved_at: datetime | None = None
    withdrawn_at: datetime | None = None


class GatewayReleaseResponse(_StrictModel):
    release: GatewayReleaseSummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class GatewayReleaseListResponse(_StrictModel):
    releases: tuple[GatewayReleaseSummary, ...]
    request_id: str
    trace_id: str


class MaintenanceWindowSummary(_StrictModel):
    maintenance_window_id: str = Field(pattern=r"^mwn_[0-9a-f]{32}$")
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    release_channel: Literal["preview", "stable", "extended"]
    timezone_name: str
    weekdays: tuple[int, ...]
    start_minute: int = Field(ge=0, le=1439)
    duration_minutes: int = Field(ge=30, le=720)
    freeze_starts_at: datetime | None = None
    freeze_ends_at: datetime | None = None
    status: Literal["active", "disabled"]
    policy_generation: int = Field(ge=1)
    revision: int = Field(ge=1)
    reason_code: str
    updated_at: datetime


class DeploymentReleaseHeadSummary(_StrictModel):
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    current_release_id: str | None = Field(default=None, pattern=r"^ger_[0-9a-f]{32}$")
    lkg_release_id: str | None = Field(default=None, pattern=r"^ger_[0-9a-f]{32}$")
    current_version: str
    lkg_version: str
    desired_generation: int = Field(ge=0)
    applied_generation: int = Field(ge=0)
    lkg_generation: int = Field(ge=0)
    revision: int = Field(ge=1)
    updated_at: datetime


class RolloutTargetSummary(_StrictModel):
    rollout_target_id: str = Field(pattern=r"^rtt_[0-9a-f]{32}$")
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    batch_sequence: int = Field(ge=0)
    stable_bucket: int = Field(ge=0, le=9999)
    current_version: str
    target_version: str
    status: Literal[
        "selected",
        "leased",
        "draining",
        "staging",
        "migrating",
        "health_check",
        "committing",
        "succeeded",
        "failed",
        "cancelled",
        "rolled_back",
    ]
    failure_reason_code: str | None = None
    updated_at: datetime


class RolloutBatchSummary(_StrictModel):
    rollout_batch_id: str = Field(pattern=r"^rbt_[0-9a-f]{32}$")
    sequence: int = Field(ge=0)
    batch_kind: Literal["canary", "progressive"]
    status: Literal["pending", "running", "observing", "completed", "failed", "cancelled"]
    target_count: int = Field(ge=1)
    succeeded_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    observe_until: datetime | None = None
    updated_at: datetime


class RolloutSummary(_StrictModel):
    rollout_id: str = Field(pattern=r"^rol_[0-9a-f]{32}$")
    release_id: str = Field(pattern=r"^ger_[0-9a-f]{32}$")
    release_version: str
    release_channel: Literal["preview", "stable", "extended"]
    status: Literal["preparing", "running", "paused", "completed", "failed", "cancelled"]
    generation: int = Field(ge=1)
    cancel_watermark: int = Field(ge=0)
    canary_basis_points: int = Field(ge=100, le=10000)
    observation_seconds: int = Field(ge=1800)
    target_count: int = Field(ge=1)
    succeeded_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    current_batch_sequence: int = Field(ge=0)
    pause_reason_code: str | None = None
    revision: int = Field(ge=1)
    batches: tuple[RolloutBatchSummary, ...]
    targets: tuple[RolloutTargetSummary, ...]
    created_at: datetime
    updated_at: datetime


class RolloutResponse(_StrictModel):
    rollout: RolloutSummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class RolloutListResponse(_StrictModel):
    rollouts: tuple[RolloutSummary, ...]
    request_id: str
    trace_id: str


class CompatibilitySummary(_StrictModel):
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    current_version: str
    lkg_version: str
    release_id: str | None = Field(default=None, pattern=r"^ger_[0-9a-f]{32}$")
    release_version: str | None = None
    compatible: bool
    reason_codes: tuple[str, ...]
    maintenance: MaintenanceWindowSummary | None = None


class FleetCompatibilityResponse(_StrictModel):
    items: tuple[CompatibilitySummary, ...]
    request_id: str
    trace_id: str


class RollbackAttemptSummary(_StrictModel):
    rollback_attempt_id: str = Field(pattern=r"^rba_[0-9a-f]{32}$")
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    source_release_id: str = Field(pattern=r"^ger_[0-9a-f]{32}$")
    target_release_id: str = Field(pattern=r"^ger_[0-9a-f]{32}$")
    desired_generation: int = Field(ge=1)
    status: Literal["requested", "issued", "completed", "failed"]
    reason_code: str
    revision: int = Field(ge=1)
    requested_at: datetime
    completed_at: datetime | None = None
    updated_at: datetime


class WorkspaceUpgradeResponse(_StrictModel):
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    maintenance: MaintenanceWindowSummary | None = None
    head: DeploymentReleaseHeadSummary
    rollouts: tuple[RolloutTargetSummary, ...]
    rollback_attempts: tuple[RollbackAttemptSummary, ...]
    request_id: str
    trace_id: str


class RollbackResponse(_StrictModel):
    rollback: RollbackAttemptSummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class AgentUpgradeCommandResponse(_StrictModel):
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    command: UpgradeCommandEnvelope
    command_revision: int = Field(ge=1)
    authority: UpgradeAuthority
    request_id: str
    trace_id: str


class AgentUpgradeReceiptResponse(_StrictModel):
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    upgrade_command_id: str = Field(pattern=r"^ucm_[0-9a-f]{32}$")
    outcome: Literal["succeeded", "failed", "cancelled"]
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


__all__ = tuple(name for name in globals() if not name.startswith("_"))
