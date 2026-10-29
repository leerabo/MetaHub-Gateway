"""PRD-089 API DTO：只暴露生命周期状态和安全摘要，不暴露密文或正文。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExportJobInfo(_Strict):
    tenant_export_job_id: str
    status: Literal["pending", "processing", "completed", "failed", "expired"]
    schema_version: Literal["metahub.tenant-export/v1"]
    snapshot_at: str
    usage_snapshot_sequence: int = Field(ge=0)
    audit_snapshot_sequence: int = Field(ge=0)
    manifest: dict[str, object] | None = None
    content_size: int | None = Field(default=None, ge=0)
    content_sha256: str | None = None
    expires_at: str
    download_count: int = Field(ge=0)
    error_code: str | None = None
    created_at: str
    updated_at: str
    completed_at: str | None = None


class DeletionRequestInfo(_Strict):
    deletion_request_id: str
    status: Literal["requested", "quiescing", "deleting", "completed", "failed"]
    tenant_revision: int = Field(ge=1)
    archived_rows: int = Field(ge=0)
    deleted_rows: int = Field(ge=0)
    retained_records: int = Field(ge=0)
    schema_manifest_sha256: str
    cache_invalidated_at: str | None = None
    completed_at: str | None = None
    error_code: str | None = None
    created_at: str
    updated_at: str


class TenantDataStateInfo(_Strict):
    lifecycle_status: Literal["active", "deletion_requested", "deleted"]
    revision: int = Field(ge=1)
    cache_generation: int = Field(ge=0)
    updated_at: str


class TenantDataManagementResponse(_Strict):
    state: TenantDataStateInfo
    exports: list[ExportJobInfo]
    deletion_request: DeletionRequestInfo | None = None
    request_id: str
    trace_id: str


class TenantExportResponse(_Strict):
    export_job: ExportJobInfo
    status_url: str
    download_url: str
    request_id: str
    trace_id: str


class TenantDeletionRequest(_Strict):
    """Exact destructive-operation body; the confirmation text is never persisted."""

    confirmation: str = Field(min_length=1, max_length=200)
    tenant_revision: int = Field(ge=1)


class TenantDeletionResponse(_Strict):
    deletion_request: DeletionRequestInfo
    request_id: str
    trace_id: str


__all__ = [
    "DeletionRequestInfo",
    "ExportJobInfo",
    "TenantDataManagementResponse",
    "TenantDataStateInfo",
    "TenantDeletionResponse",
    "TenantDeletionRequest",
    "TenantExportResponse",
]
