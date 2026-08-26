"""Strict public and Platform DTOs for PRD-119 provider catalog."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProviderCapability(CatalogModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    max_context_tokens: int = Field(ge=1, le=10_000_000)
    supports_stream: bool
    supports_tools: bool
    supports_files: bool


class RegionalEndpointTemplate(CatalogModel):
    region_code: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,39}$")
    cloud_service_code: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    scheme: Literal["https"]
    host: str = Field(min_length=4, max_length=253)
    path_template: str = Field(min_length=1, max_length=256)
    residency_tags: list[str] = Field(min_length=1, max_length=16)


class CompatibilityProfile(CatalogModel):
    level: Literal["native", "openai_compatible", "migration_only"]
    doc_code: str = Field(pattern=r"^[a-z][a-z0-9_.]{2,127}$")
    migration_source_code: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_]{1,63}$"
    )
    request_difference_codes: list[str] = Field(max_length=32)


class ProviderCatalogEntry(CatalogModel):
    entry_code: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    vendor_code: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    display_name: str = Field(min_length=2, max_length=120)
    provider_type: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    service_class: Literal["official_cloud", "local_template", "migration_source"]
    auth_modes: list[Literal["none", "bearer", "custom_header"]] = Field(
        min_length=1, max_length=3
    )
    data_regions: list[str] = Field(min_length=1, max_length=32)
    compliance_tags: list[str] = Field(min_length=1, max_length=32)
    status: Literal["active", "deprecated", "retired"]
    instantiation_mode: Literal["provider", "local_endpoint", "migration_only"]
    capabilities: list[ProviderCapability] = Field(min_length=1, max_length=32)
    endpoints: list[RegionalEndpointTemplate] = Field(max_length=32)
    compatibility: CompatibilityProfile

    @model_validator(mode="after")
    def canonical_collections(self) -> ProviderCatalogEntry:
        for values in (self.auth_modes, self.data_regions, self.compliance_tags):
            if values != sorted(set(values)):
                raise ValueError("catalog lists must be unique and sorted")
        if [item.code for item in self.capabilities] != sorted(
            {item.code for item in self.capabilities}
        ):
            raise ValueError("capabilities must be unique and sorted")
        if [item.region_code for item in self.endpoints] != sorted(
            {item.region_code for item in self.endpoints}
        ):
            raise ValueError("endpoints must be unique and sorted")
        if self.instantiation_mode == "provider":
            if self.status != "active" or not self.endpoints:
                raise ValueError("provider entries require active endpoints")
            if {item.region_code for item in self.endpoints} != set(self.data_regions):
                raise ValueError("provider regions must match endpoint regions")
        elif self.endpoints:
            raise ValueError("non-provider entries cannot publish endpoints")
        if (self.instantiation_mode == "migration_only") != (
            self.compatibility.level == "migration_only"
        ):
            raise ValueError("migration compatibility mode mismatch")
        return self


class ProviderCatalogManifest(CatalogModel):
    schema_version: Literal[1]
    version: str = Field(pattern=r"^[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[1-9][0-9]{0,3}$")
    entries: list[ProviderCatalogEntry] = Field(min_length=10, max_length=100)

    @model_validator(mode="after")
    def canonical_entries(self) -> ProviderCatalogManifest:
        if [item.entry_code for item in self.entries] != sorted(
            {item.entry_code for item in self.entries}
        ):
            raise ValueError("entries must be unique and sorted")
        return self


class ProviderCatalogResponse(CatalogModel):
    version: str
    generation: int = Field(ge=1)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    published_at: datetime
    entries: tuple[ProviderCatalogEntry, ...]
    request_id: str
    trace_id: str


class CatalogProviderCreateRequest(CatalogModel):
    entry_code: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    version: str = Field(pattern=r"^[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[1-9][0-9]{0,3}$")
    region_code: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,39}$")
    capability_codes: list[str] = Field(min_length=1, max_length=32)
    provider_code: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    name: str = Field(min_length=2, max_length=120)
    confirmed: Literal[True]

    @model_validator(mode="after")
    def canonical_capabilities(self) -> CatalogProviderCreateRequest:
        if self.capability_codes != sorted(set(self.capability_codes)):
            raise ValueError("capabilities must be unique and sorted")
        return self


class CatalogInstanceSummary(CatalogModel):
    provider_catalog_instance_id: str = Field(pattern=r"^pci_[0-9a-f]{32}$")
    workspace_id: str = Field(pattern=r"^wsp_[0-9a-f]{32}$")
    provider_id: str
    provider_code: str
    entry_code: str
    catalog_version: str
    region_code: str
    capability_codes: tuple[str, ...]
    revision: int = Field(ge=1)
    created_at: datetime


class CatalogProviderCreateResponse(CatalogModel):
    instance: CatalogInstanceSummary
    idempotent_replay: bool = False
    next_action: Literal["configure_provider_key"]
    request_id: str
    trace_id: str


class ProviderPreflightRequest(CatalogModel):
    timeout_seconds: float = Field(default=5.0, ge=0.5, le=25.0)


class ProviderPreflightRun(CatalogModel):
    provider_preflight_run_id: str = Field(pattern=r"^pfr_[0-9a-f]{32}$")
    provider_id: str
    entry_code: str
    catalog_version: str
    region_code: str
    status: Literal["succeeded", "failed", "inconclusive", "timed_out"]
    outcome_code: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    latency_ms: int = Field(ge=0, le=30_000)
    http_status: int | None = Field(default=None, ge=100, le=599)
    capability_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    checked_at: datetime


class ProviderPreflightResponse(CatalogModel):
    run: ProviderPreflightRun
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class CompatibilityResponse(CatalogModel):
    entry_code: str
    version: str
    status: Literal["active", "deprecated", "retired"]
    compatibility: CompatibilityProfile
    current_instances: int = Field(ge=0)
    request_id: str
    trace_id: str


class PlatformCatalogPublishRequest(CatalogModel):
    manifest: ProviderCatalogManifest
    key_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")
    signature: str = Field(pattern=r"^[A-Za-z0-9_-]{86}$")
    reason_code: Literal["catalog_release", "compliance_update", "endpoint_update"]
    confirmed: Literal[True]


class PlatformCatalogVersionSummary(CatalogModel):
    version: str
    generation: int = Field(ge=1)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    key_id: str
    signature_algorithm: Literal["ed25519"]
    signature_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    entry_count: int = Field(ge=1, le=100)
    compatibility_change_count: int = Field(ge=0, le=100)
    published_by_operator_id: str | None
    published_at: datetime


class PlatformCatalogResponse(CatalogModel):
    current: PlatformCatalogVersionSummary
    recent_versions: tuple[PlatformCatalogVersionSummary, ...]
    security_projection: bool
    request_id: str
    trace_id: str


class PlatformCatalogPublishResponse(CatalogModel):
    catalog: PlatformCatalogVersionSummary
    idempotent_replay: bool = False
    propagated: bool
    request_id: str
    trace_id: str


__all__ = [
    "CatalogInstanceSummary",
    "CatalogProviderCreateRequest",
    "CatalogProviderCreateResponse",
    "CompatibilityResponse",
    "PlatformCatalogPublishRequest",
    "PlatformCatalogPublishResponse",
    "PlatformCatalogResponse",
    "ProviderCatalogEntry",
    "ProviderCatalogManifest",
    "ProviderCatalogResponse",
    "ProviderPreflightRequest",
    "ProviderPreflightResponse",
]
