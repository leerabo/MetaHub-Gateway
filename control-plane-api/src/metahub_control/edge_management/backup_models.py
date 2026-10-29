"""Cloud-safe private Gateway backup and isolated DR drill DTOs for PRD-129."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class AgentBackupReportRequest(_StrictModel):
    backup_id: str = Field(pattern=r"^ebk_[0-9a-f]{32}$")
    source_generation: int = Field(ge=1)
    schema_head: str = Field(pattern=r"^[0-9]{4}$")
    storage_kind: Literal["local", "customer_object_store"]
    status: Literal["completed", "failed"]
    size_bucket: Literal["lt_100mb", "100mb_1gb", "1gb_10gb", "gte_10gb"]
    chunk_count: int = Field(ge=0, le=1_000_000)
    manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    kms_key_id_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    check_outcome: Literal["verified", "failed"]
    failure_code: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_.-]{1,79}$"
    )
    started_at: datetime
    completed_at: datetime
    retention_until: datetime
    attempt_nonce: str = Field(min_length=32, max_length=128, repr=False)

    @model_validator(mode="after")
    def _valid_report(self) -> AgentBackupReportRequest:
        successful = self.status == "completed"
        if (
            self.completed_at < self.started_at
            or self.retention_until <= self.completed_at
            or successful
            != (
                self.check_outcome == "verified"
                and self.manifest_sha256 is not None
                and self.kms_key_id_sha256 is not None
                and self.chunk_count > 0
                and self.failure_code is None
            )
            or (not successful and self.failure_code is None)
        ):
            raise ValueError("edge_backup.report_invalid")
        return self


class BackupReportSummary(_StrictModel):
    backup_id: str = Field(pattern=r"^ebk_[0-9a-f]{32}$")
    source_generation: int = Field(ge=1)
    schema_head: str
    storage_kind: Literal["local", "customer_object_store"]
    status: Literal["completed", "failed", "deleted"]
    size_bucket: Literal["lt_100mb", "100mb_1gb", "1gb_10gb", "gte_10gb"]
    chunk_count: int = Field(ge=0, le=1_000_000)
    manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    kms_key_id_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    check_outcome: Literal["verified", "failed"]
    failure_code: str | None = None
    started_at: datetime
    completed_at: datetime
    retention_until: datetime


class BackupReportResponse(_StrictModel):
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    backup: BackupReportSummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class RestoreVerificationRequest(_StrictModel):
    verification_code: Literal[
        "manifest_integrity",
        "owner_binding",
        "schema_compatibility",
        "config_consistency",
        "usage_cursor",
        "audit_chain",
        "secret_reference",
        "device_reenrollment",
        "minimal_request",
    ]
    outcome: Literal["passed", "failed"]
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    checked_at: datetime


class AgentDrillReportRequest(_StrictModel):
    restore_job_id: str = Field(pattern=r"^rst_[0-9a-f]{32}$")
    expected_revision: int = Field(ge=1)
    outcome: Literal["succeeded", "failed"]
    actual_rpo_seconds: int = Field(ge=0, le=31_536_000)
    actual_rto_seconds: int = Field(ge=0, le=31_536_000)
    verifications: tuple[RestoreVerificationRequest, ...] = Field(
        min_length=9, max_length=9
    )
    attempt_nonce: str = Field(min_length=32, max_length=128, repr=False)

    @model_validator(mode="after")
    def _complete_verification_set(self) -> AgentDrillReportRequest:
        codes = [item.verification_code for item in self.verifications]
        if len(set(codes)) != 9 or (self.outcome == "succeeded" and any(
            item.outcome != "passed" for item in self.verifications
        )):
            raise ValueError("edge_backup.drill_report_invalid")
        return self


class RestoreDrillMutationRequest(_StrictModel):
    action: Literal["request", "approve"]
    source_backup_id: str | None = Field(
        default=None, pattern=r"^ebk_[0-9a-f]{32}$"
    )
    drill_id: str | None = Field(default=None, pattern=r"^pdd_[0-9a-f]{32}$")
    quarter_code: str | None = Field(default=None, pattern=r"^[0-9]{4}-Q[1-4]$")
    rpo_objective_seconds: int | None = Field(default=None, ge=900, le=604_800)
    rto_objective_seconds: int | None = Field(default=None, ge=900, le=604_800)
    isolated_target_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    expected_revision: int | None = Field(default=None, ge=1)
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,79}$")

    @model_validator(mode="after")
    def _valid_action(self) -> RestoreDrillMutationRequest:
        request_fields = (
            self.source_backup_id,
            self.quarter_code,
            self.rpo_objective_seconds,
            self.rto_objective_seconds,
            self.isolated_target_sha256,
        )
        if self.action == "request":
            valid = all(value is not None for value in request_fields) and (
                self.drill_id is None and self.expected_revision is None
            )
        else:
            valid = self.drill_id is not None and self.expected_revision is not None and all(
                value is None for value in request_fields
            )
        if not valid:
            raise ValueError("edge_backup.drill_request_invalid")
        return self


class RestoreDrillSummary(_StrictModel):
    drill_id: str = Field(pattern=r"^pdd_[0-9a-f]{32}$")
    restore_job_id: str = Field(pattern=r"^rst_[0-9a-f]{32}$")
    source_backup_id: str = Field(pattern=r"^ebk_[0-9a-f]{32}$")
    status: Literal["requested", "approved", "running", "succeeded", "failed"]
    quarter_code: str
    rpo_objective_seconds: int
    rto_objective_seconds: int
    actual_rpo_seconds: int | None = None
    actual_rto_seconds: int | None = None
    isolated_target_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    production_untouched: Literal[True]
    objective_outcome: Literal["met", "missed"] | None = None
    revision: int = Field(ge=1)
    requested_by_account_id: str
    approved_by_account_id: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime


class RestoreDrillResponse(_StrictModel):
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    drill: RestoreDrillSummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class BackupStatusSummary(_StrictModel):
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    status: Literal["never", "current", "overdue", "failed"]
    rpo_objective_seconds: int = Field(ge=900, le=604_800)
    latest_source_generation: int = Field(ge=0)
    last_success_at: datetime | None = None
    last_check_outcome: Literal["verified", "failed"] | None = None
    revision: int = Field(ge=1)
    updated_at: datetime
    latest_backup: BackupReportSummary | None = None
    recent_backups: tuple[BackupReportSummary, ...]
    recent_drills: tuple[RestoreDrillSummary, ...]


class BackupStatusResponse(_StrictModel):
    status: BackupStatusSummary
    request_id: str
    trace_id: str


class PlatformBackupStatusListResponse(_StrictModel):
    statuses: tuple[BackupStatusSummary, ...]
    request_id: str
    trace_id: str


__all__ = tuple(name for name in globals() if not name.startswith("_"))
