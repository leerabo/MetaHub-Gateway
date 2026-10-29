"""Closed PRD-111 disaster-recovery request and response contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DRModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RegionInfo(DRModel):
    region_id: str = Field(pattern=r"^reg_[0-9a-f]{32}$")
    region_code: str = Field(min_length=3, max_length=40)
    residency_group: str = Field(min_length=3, max_length=40)
    health_status: Literal["healthy", "degraded", "unavailable"]
    writer_status: Literal["active", "standby", "fenced"]
    failover_epoch: int = Field(ge=0)
    revision: int = Field(ge=1)
    updated_at: datetime


class FailoverPlanInfo(DRModel):
    failover_plan_id: str = Field(pattern=r"^fpl_[0-9a-f]{32}$")
    plan_code: str = Field(min_length=3, max_length=64)
    version: int = Field(ge=1)
    source_region_code: str = Field(min_length=3, max_length=40)
    target_region_code: str = Field(min_length=3, max_length=40)
    residency_group: str = Field(min_length=3, max_length=40)
    component_profile: Literal["control-full"]
    rpo_seconds: int = Field(ge=60, le=900)
    control_rto_seconds: int = Field(ge=60, le=7200)
    gateway_rto_seconds: int = Field(ge=60, le=600)
    stop_condition_code: Literal["split-brain-or-integrity-failure"]
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["active", "disabled"]


class FailoverApprovalInfo(DRModel):
    approval_kind: Literal["execution", "security_review"]
    operator_id: str = Field(pattern=r"^pto_[0-9a-f]{32}$")
    approved_at: datetime


class RestoreEvidenceInfo(DRModel):
    restore_evidence_id: str = Field(pattern=r"^rev_[0-9a-f]{32}$")
    workspace_id: str = Field(pattern=r"^wsp_[0-9a-f]{32}$")
    component_code: Literal[
        "database", "object_store", "config", "outbox", "ledger", "gateway_route"
    ]
    integrity_status: Literal["verified", "failed"]
    rpo_outcome: Literal["met", "missed"]
    rto_outcome: Literal["met", "missed"]
    duration_seconds: int = Field(ge=0)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime


class DisasterRecoveryRunInfo(DRModel):
    disaster_recovery_run_id: str = Field(pattern=r"^drr_[0-9a-f]{32}$")
    run_kind: Literal["drill", "failover"]
    failover_plan_id: str = Field(pattern=r"^fpl_[0-9a-f]{32}$")
    plan_version: int = Field(ge=1)
    source_region_code: str = Field(min_length=3, max_length=40)
    target_region_code: str = Field(min_length=3, max_length=40)
    expected_epoch: int = Field(ge=0)
    fencing_epoch: int = Field(ge=1)
    reason_code: Literal[
        "quarterly_drill", "regional_outage", "control_recovery", "security_containment"
    ]
    status: Literal[
        "planned",
        "approved",
        "prechecking",
        "running",
        "verifying",
        "succeeded",
        "failed",
        "aborted",
        "rollback_required",
    ]
    checkpoint: Literal["approval", "precheck", "quiesce", "restore", "route", "verify", "complete"]
    platform_job_id: str | None = Field(default=None, pattern=r"^pjob_[0-9a-f]{32}$")
    approvals: tuple[FailoverApprovalInfo, ...]
    restore_evidence: tuple[RestoreEvidenceInfo, ...]
    safe_outcome_code: str | None = Field(default=None, min_length=3, max_length=96)
    revision: int = Field(ge=1)
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class DRObjectiveInfo(DRModel):
    control_rpo_seconds: Literal[900]
    control_rto_seconds: Literal[7200]
    gateway_rto_seconds: Literal[600]
    latest_recovery_age_seconds: int | None = Field(default=None, ge=0)
    recovery_ready: bool


class DRStatusResponse(DRModel):
    regions: tuple[RegionInfo, ...]
    plans: tuple[FailoverPlanInfo, ...]
    recent_runs: tuple[DisasterRecoveryRunInfo, ...]
    objectives: DRObjectiveInfo
    request_id: str
    trace_id: str


class RecoveryPointInfo(DRModel):
    recovery_point_id: str = Field(pattern=r"^rcp_[0-9a-f]{32}$")
    backup_id: str = Field(pattern=r"^bkp_[0-9a-f]{32}$")
    scope_kind: Literal["platform", "workspace"]
    workspace_id: str | None = Field(default=None, pattern=r"^wsp_[0-9a-f]{32}$")
    schema_head: str = Field(pattern=r"^[0-9]{4}$")
    integrity_status: Literal["verified"]
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_created_at: datetime
    age_seconds: int = Field(ge=0)


class BackupListResponse(DRModel):
    items: tuple[RecoveryPointInfo, ...]
    total: int = Field(ge=0)
    request_id: str
    trace_id: str


class DRCommandRequest(DRModel):
    failover_plan_id: str = Field(pattern=r"^fpl_[0-9a-f]{32}$")
    plan_version: int = Field(ge=1)
    reason_code: Literal[
        "quarterly_drill", "regional_outage", "control_recovery", "security_containment"
    ]
    expected_epoch: int = Field(ge=0)
    confirmed: Literal[True]


class DRRunResponse(DRModel):
    run: DisasterRecoveryRunInfo
    approval_recorded: bool
    idempotent_replay: bool
    request_id: str
    trace_id: str


__all__ = [
    "BackupListResponse",
    "DRCommandRequest",
    "DRRunResponse",
    "DRStatusResponse",
    "DisasterRecoveryRunInfo",
    "FailoverPlanInfo",
    "RecoveryPointInfo",
    "RegionInfo",
    "RestoreEvidenceInfo",
]
