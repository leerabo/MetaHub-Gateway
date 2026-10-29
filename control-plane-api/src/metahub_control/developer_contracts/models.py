"""Strict DTOs for PRD-133 public contracts, SDKs, and deprecation gates."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApiVersionInfo(ContractModel):
    api_version_id: str
    version: str
    registry_revision: int = Field(ge=1)
    status: Literal["pending_approval", "published", "deprecated", "sunset"]
    revision: int = Field(ge=1)
    contract_sha256: str
    compatibility_baseline_sha256: str | None
    compatibility_outcome: Literal["compatible", "breaking"]
    breaking_reasons: tuple[str, ...]
    created_at: datetime
    published_at: datetime | None
    deprecated_at: datetime | None
    sunset_at: datetime | None


class ApiVersionListResponse(ContractModel):
    versions: tuple[ApiVersionInfo, ...]
    request_id: str
    trace_id: str


class ChangelogEntry(ContractModel):
    event_id: str
    event_type: Literal[
        "api.deprecated",
        "api.sunset_scheduled",
        "sdk.released",
        "webhook.schema_published",
    ]
    source_object_type: Literal[
        "api_version", "deprecation_notice", "sdk_release", "webhook_schema_version"
    ]
    source_object_id: str
    source_revision: int = Field(ge=1)
    metadata: dict[str, str | int]
    occurred_at: datetime


class DeveloperChangelogResponse(ContractModel):
    entries: tuple[ChangelogEntry, ...]
    next_cursor: str | None
    request_id: str
    trace_id: str


class WebhookSchemaInfo(ContractModel):
    webhook_schema_version_id: str
    api_version: str
    event_name: str
    schema_version: int = Field(ge=1)
    json_schema: dict[str, object]
    schema_sha256: str
    sample_sha256: str
    published_at: datetime


class WebhookSchemaListResponse(ContractModel):
    schemas: tuple[WebhookSchemaInfo, ...]
    request_id: str
    trace_id: str


class SdkReleaseInfo(ContractModel):
    sdk_release_id: str
    api_version: str
    language: Literal["python", "javascript"]
    sdk_version: str
    runtime_version: str
    contract_sha256: str
    artifact_sha256: str
    artifact_receipt_sha256: str
    file_manifest: dict[str, str]
    smoke_matrix: dict[str, object]
    status: Literal["pending_approval", "published", "withdrawn"]
    revision: int = Field(ge=1)
    signing_key_id: str | None
    signature_b64: str | None
    signature_sha256: str | None
    public_key_b64: str | None
    download_url: str | None
    created_at: datetime
    published_at: datetime | None


class SdkReleaseListResponse(ContractModel):
    releases: tuple[SdkReleaseInfo, ...]
    request_id: str
    trace_id: str


class DeprecationNoticeInfo(ContractModel):
    deprecation_notice_id: str
    api_version_id: str
    api_version: str
    replacement_api_version_id: str
    replacement_api_version: str
    affected_operations: tuple[str, ...]
    migration_guide_code: str
    status: Literal["draft", "announced", "sunset_scheduled", "sunset", "withdrawn"]
    minimum_notice_days: int = Field(ge=90)
    adoption_watermark: int = Field(ge=0)
    notification_watermark: int = Field(ge=0)
    sunset_at: datetime
    revision: int = Field(ge=1)
    created_at: datetime
    announced_at: datetime | None
    decided_at: datetime | None


class PlatformContractWorkspaceResponse(ContractModel):
    versions: tuple[ApiVersionInfo, ...]
    sdk_releases: tuple[SdkReleaseInfo, ...]
    deprecations: tuple[DeprecationNoticeInfo, ...]
    request_id: str
    trace_id: str


class ContractBuildRequest(ContractModel):
    api_version: str = Field(pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
    python_sdk_version: str = Field(pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
    javascript_sdk_version: str = Field(
        pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
    )
    producer_revision: int = Field(ge=1)
    confirmed: Literal[True]


class ContractBuildResponse(ContractModel):
    api_version: ApiVersionInfo
    sdk_releases: tuple[SdkReleaseInfo, ...]
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class ContractApproveRequest(ContractModel):
    expected_revision: int = Field(ge=1)
    confirmed: Literal[True]


class DeprecationCreateRequest(ContractModel):
    api_version_id: str
    replacement_api_version_id: str
    affected_operations: tuple[str, ...] = Field(min_length=1, max_length=128)
    migration_guide_code: str = Field(
        min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]*$"
    )
    minimum_notice_days: int = Field(default=90, ge=90, le=730)
    sunset_at: datetime
    confirmed: Literal[True]

    @model_validator(mode="after")
    def validate_distinct_versions(self) -> DeprecationCreateRequest:
        if self.api_version_id == self.replacement_api_version_id:
            raise ValueError("replacement must differ")
        if len(set(self.affected_operations)) != len(self.affected_operations):
            raise ValueError("duplicate operation")
        return self


class DeprecationActionRequest(ContractModel):
    expected_revision: int = Field(ge=1)
    notification_watermark: int | None = Field(default=None, ge=0)
    adoption_watermark: int | None = Field(default=None, ge=0)
    confirmed: Literal[True]


class SdkWithdrawRequest(ContractModel):
    expected_revision: int = Field(ge=1)
    confirmed: Literal[True]


class MutationResponse(ContractModel):
    api_version: ApiVersionInfo | None = None
    deprecation: DeprecationNoticeInfo | None = None
    sdk_release: SdkReleaseInfo | None = None
    idempotent_replay: bool = False
    request_id: str
    trace_id: str
