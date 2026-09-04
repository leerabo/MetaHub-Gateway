"""Strict public DTOs for PRD-147 console-domain migration governance."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

DomainCode = Literal[
    "channel_attribution",
    "customer_success",
    "growth_analytics",
    "brand_distribution",
    "provider_engineering",
    "commercial_accounting",
    "console_experience",
    "workspace_navigation",
]
MigrationState = Literal[
    "inventoried",
    "backfill_queued",
    "backfilling",
    "shadowing",
    "ready",
    "quiescing",
    "cutting_over",
    "validating",
    "active",
    "rollback_queued",
    "rolling_back",
    "rolled_back",
    "blocked",
]


class ConsoleMigrationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DomainStatus(ConsoleMigrationModel):
    domain_code: DomainCode
    source_migration: str = Field(pattern=r"^01(?:3[1-8])$")
    migration_count: int = Field(ge=0)
    blocked_count: int = Field(ge=0)
    active_count: int = Field(ge=0)


class MigrationStatusItem(ConsoleMigrationModel):
    migration_id: str = Field(pattern=r"^cdm_[0-9a-f]{32}$")
    workspace_ref: str = Field(pattern=r"^[0-9a-f]{64}$")
    domain_code: DomainCode
    state: MigrationState
    progress_basis_points: int = Field(ge=0, le=10_000)
    operation_generation: int = Field(ge=1)
    target_generation: int = Field(ge=0)
    revision: int = Field(ge=1)
    reconciliation_status: Literal[
        "missing", "pending", "matched", "mismatched", "unavailable", "superseded"
    ]
    reason_code: str = Field(min_length=3, max_length=80)
    approval_ready: bool
    security_review_ready: bool
    route_ready: bool
    scheduler_ready: bool
    last_safe_at: datetime
    allowed_actions: tuple[str, ...] = Field(max_length=8)


class SchedulerRegistryStatus(ConsoleMigrationModel):
    baseline_slot_count: Literal[41]
    final_installer_count: Literal[36]
    cluster_context_count: Literal[37]
    queue_contract_count: int = Field(ge=37)
    registry_generation: int = Field(ge=1)
    enabled_context_count: int = Field(ge=0, le=37)
    active_lease_count: int = Field(ge=0)
    manifest_matches: bool


class RuntimeSurfaceStatus(ConsoleMigrationModel):
    runtime_surface_code: Literal[
        "scheduler-lease-coordinator", "risk-fact-sink", "control-telemetry"
    ]
    execution_class: Literal["per_replica_control_bounded", "per_replica_bounded"]
    queue_capacity: int = Field(ge=1)
    batch_limit: int = Field(ge=1)
    healthy_replica_count: int = Field(ge=0)
    degraded_replica_count: int = Field(ge=0)


class ConsoleContractStatus(ConsoleMigrationModel):
    contract_generation: int = Field(ge=0)
    route_deny_epoch: int = Field(ge=1)
    legacy_write_fenced: bool
    status: Literal["expanded", "active"]
    blocking_check_count: int = Field(ge=0)


class ConsoleMigrationStatusResponse(ConsoleMigrationModel):
    domains: tuple[DomainStatus, ...] = Field(min_length=8, max_length=8)
    items: tuple[MigrationStatusItem, ...] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, max_length=1024)
    scheduler: SchedulerRegistryStatus
    runtime_surfaces: tuple[RuntimeSurfaceStatus, ...] = Field(min_length=3, max_length=3)
    contract: ConsoleContractStatus
    permission_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_id: str
    trace_id: str


class BackfillPrepareCommand(ConsoleMigrationModel):
    action: Literal["prepare"]
    domain_code: DomainCode
    scope: Literal["workspace", "all_active"]
    workspace_ref: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    expected_revision: int = Field(ge=0)
    resource_profile: Literal["bounded-v1"]
    confirmed: Literal[True]

    @model_validator(mode="after")
    def validate_scope(self) -> BackfillPrepareCommand:
        if (self.scope == "workspace") != (self.workspace_ref is not None):
            raise ValueError("workspace scope requires exactly one workspace_ref")
        return self


class BackfillControlCommand(ConsoleMigrationModel):
    action: Literal["start", "resume", "cancel"]
    migration_id: str = Field(pattern=r"^cdm_[0-9a-f]{32}$")
    expected_revision: int = Field(ge=1)
    reconciliation_generation: int = Field(ge=0)
    resource_profile: Literal["bounded-v1"]
    confirmed: Literal[True]


BackfillCommand = Annotated[
    BackfillPrepareCommand | BackfillControlCommand,
    Field(discriminator="action"),
]
BACKFILL_COMMAND_ADAPTER = TypeAdapter(BackfillCommand)


class CutoverBaseCommand(ConsoleMigrationModel):
    migration_id: str = Field(pattern=r"^cdm_[0-9a-f]{32}$")
    expected_revision: int = Field(ge=1)
    confirmed: Literal[True]


class CutoverPrepareCommand(CutoverBaseCommand):
    action: Literal["prepare"]


class CutoverApproveCommand(CutoverBaseCommand):
    action: Literal["approve"]


class CutoverSecurityReviewCommand(CutoverBaseCommand):
    action: Literal["security_review"]
    reconciliation_id: str = Field(pattern=r"^drc_[0-9a-f]{32}$")
    reconciliation_revision: int = Field(ge=1)


class CutoverExecuteCommand(CutoverBaseCommand):
    action: Literal["execute"]


CutoverCommand = Annotated[
    CutoverPrepareCommand
    | CutoverApproveCommand
    | CutoverSecurityReviewCommand
    | CutoverExecuteCommand,
    Field(discriminator="action"),
]
CUTOVER_COMMAND_ADAPTER = TypeAdapter(CutoverCommand)


RollbackReason = Literal[
    "validation_failed", "dependency_failure", "operator_recovery", "release_regression"
]


class RollbackBaseCommand(ConsoleMigrationModel):
    migration_id: str = Field(pattern=r"^cdm_[0-9a-f]{32}$")
    expected_revision: int = Field(ge=1)
    reason_code: RollbackReason
    target_generation: int = Field(ge=1)
    confirmed: Literal[True]


class RollbackPrepareCommand(RollbackBaseCommand):
    action: Literal["prepare"]


class RollbackApproveCommand(RollbackBaseCommand):
    action: Literal["approve"]


class RollbackSecurityReviewCommand(RollbackBaseCommand):
    action: Literal["security_review"]
    reconciliation_id: str = Field(pattern=r"^drc_[0-9a-f]{32}$")
    reconciliation_revision: int = Field(ge=1)


class RollbackExecuteCommand(RollbackBaseCommand):
    action: Literal["execute"]


class RollbackResumeCommand(RollbackBaseCommand):
    action: Literal["resume"]


RollbackCommand = Annotated[
    RollbackPrepareCommand
    | RollbackApproveCommand
    | RollbackSecurityReviewCommand
    | RollbackExecuteCommand
    | RollbackResumeCommand,
    Field(discriminator="action"),
]
ROLLBACK_COMMAND_ADAPTER = TypeAdapter(RollbackCommand)


class ConsoleMigrationCommandResponse(ConsoleMigrationModel):
    operation_id: str = Field(pattern=r"^(?:cdm|cmb)_[0-9a-f]{32}$")
    migration_id: str | None = Field(default=None, pattern=r"^cdm_[0-9a-f]{32}$")
    state: str = Field(min_length=3, max_length=40)
    revision: int = Field(ge=1)
    operation_generation: int = Field(ge=1)
    accepted: bool
    idempotent_replay: bool = False
    polling_location_code: Literal["console-migration-status"]
    contract_generation: int = Field(ge=0)
    registry_generation: int = Field(ge=1)
    permission_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_id: str
    trace_id: str


__all__ = [
    "BACKFILL_COMMAND_ADAPTER",
    "CUTOVER_COMMAND_ADAPTER",
    "ROLLBACK_COMMAND_ADAPTER",
    "BackfillCommand",
    "BackfillControlCommand",
    "BackfillPrepareCommand",
    "ConsoleMigrationCommandResponse",
    "ConsoleMigrationStatusResponse",
    "CutoverCommand",
    "CutoverSecurityReviewCommand",
    "DomainCode",
    "MigrationState",
    "RollbackCommand",
    "RollbackSecurityReviewCommand",
]
