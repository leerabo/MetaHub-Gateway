"""Strict safe projections for production infrastructure and backups."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class InfrastructureModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class InfrastructureResourceInfo(InfrastructureModel):
    resource_kind: Literal[
        "database_cluster", "cache_cluster", "object_store", "key_ring"
    ]
    provider_code: Literal["sqlite_local", "postgresql", "redis", "s3_compatible", "kms"]
    status: Literal["healthy", "degraded", "unavailable", "not_configured"]
    version_summary: str | None = None
    latency_ms: int | None = Field(default=None, ge=0, le=300_000)
    verified: bool
    last_checked_at: datetime
    last_verified_at: datetime | None = None
    error_code: str | None = None


class InfrastructureStatusResponse(InfrastructureModel):
    resources: tuple[InfrastructureResourceInfo, ...]
    overall_status: Literal["healthy", "degraded", "unavailable"]
    rpo_minutes: int = Field(ge=0)
    rto_minutes: int = Field(ge=0)
    request_id: str
    trace_id: str


class BackupCreateRequest(InfrastructureModel):
    backup_type: Literal["full", "incremental"] = "full"
    scope_kind: Literal["platform", "workspace"] = "platform"
    workspace_id: str | None = None
    tenant_id: str | None = None
    retention_days: int | None = Field(default=None, ge=1, le=3650)
    confirmed: Literal[True]

    @model_validator(mode="after")
    def validate_owner(self) -> BackupCreateRequest:
        if self.scope_kind == "platform" and (
            self.workspace_id is not None or self.tenant_id is not None
        ):
            raise ValueError("platform backup cannot carry a workspace owner")
        if self.scope_kind == "workspace" and (
            self.workspace_id is None or self.tenant_id is None
        ):
            raise ValueError("workspace backup requires both owner identifiers")
        return self


class BackupVerificationInfo(InfrastructureModel):
    status: Literal["pending", "running", "passed", "failed"]
    schema_head: str | None = None
    integrity_status: Literal["ok", "failed"] | None = None
    manifest_sha256: str | None = None
    tenant_owner_sample_count: int | None = Field(default=None, ge=0, le=10_000)
    error_code: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class BackupInfo(InfrastructureModel):
    backup_id: str
    backup_type: Literal["full", "incremental"]
    scope_kind: Literal["platform", "workspace"]
    workspace_id: str | None = None
    tenant_id: str | None = None
    status: Literal["requested", "snapshotting", "uploading", "verifying", "ready", "failed"]
    source_backend: Literal["sqlite", "postgresql"]
    source_watermark: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    content_sha256: str | None = None
    manifest_sha256: str | None = None
    kms_key_version: str | None = None
    schema_head: str | None = None
    error_code: str | None = None
    retention_until: datetime
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    verification: BackupVerificationInfo


class BackupResponse(InfrastructureModel):
    backup: BackupInfo
    idempotent_replay: bool = False
    request_id: str
    trace_id: str
