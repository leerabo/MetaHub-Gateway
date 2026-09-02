"""Strict safe DTOs for PRD-143 Provider Engineering."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator


class ProviderEngineeringModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


AdapterStatus = Literal[
    "legacy_baseline",
    "candidate",
    "testing",
    "review_required",
    "approved",
    "released",
    "deprecated",
    "revoked",
]


class ContractAggregate(ProviderEngineeringModel):
    queued: int = Field(ge=0)
    running: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    qualifying_run_id: str | None = None


class ProviderAdapterReleaseInfo(ProviderEngineeringModel):
    provider_adapter_release_id: str
    adapter_code: str
    release_version: str
    protocol_family: Literal[
        "openai_compatible", "anthropic_compatible", "generic_http", "legacy_catalog"
    ]
    sdk_release_id: str | None = None
    artifact_sha256: str
    sbom_sha256: str
    provenance_sha256: str
    signature_sha256: str
    spec_manifest_version: str
    spec_manifest_sha256: str
    compatibility_sha256: str
    required_runtime_version: str
    required_api_version: str
    status: AdapterStatus
    revision: int = Field(ge=1)
    release_generation: int = Field(ge=0)
    is_head: bool
    readiness: Literal[
        "legacy", "candidate", "testing", "review", "ready", "released", "deprecated", "revoked"
    ]
    contract: ContractAggregate
    actions: tuple[str, ...]
    created_at: datetime
    updated_at: datetime
    released_at: datetime | None = None


class ProviderAdapterListResponse(ProviderEngineeringModel):
    adapters: tuple[ProviderAdapterReleaseInfo, ...]
    next_cursor: str | None = None
    limit: int = Field(ge=1, le=100)
    permission_fingerprint: str
    request_id: str
    trace_id: str


class AdapterContractRunInfo(ProviderEngineeringModel):
    contract_run_id: str
    scope: Literal["platform_release_ci", "legacy_tenant_merge_gate", "legacy_tenant_probe"]
    provider_adapter_release_id: str | None = None
    adapter_code: str
    schema_version: str
    runner_version: str
    suite_kind: Literal["merge_gate", "probe", "release_ci"]
    mode: Literal["mock", "gateway_probe", "isolated_runner"]
    source_revision_sha256: str
    artifact_sha256: str
    catalog_sha256: str
    status: Literal["queued", "running", "passed", "failed", "cancelled"]
    coverage_complete: bool
    total_cases: int = Field(ge=0, le=100000)
    passed_cases: int = Field(ge=0, le=100000)
    failed_cases: int = Field(ge=0, le=100000)
    failure_codes: tuple[str, ...]
    report_sha256: str
    attestation_sha256: str
    qualifies_release: bool
    legacy: bool
    generation: int = Field(ge=1)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime


class AdapterContractCaseInfo(ProviderEngineeringModel):
    contract_case_id: str
    contract_run_id: str
    ordinal: int = Field(ge=1, le=100000)
    case_key_sha256: str
    case_kind: Literal["request", "non_stream", "stream", "error", "usage"]
    variant: str
    provider_code: str
    adapter_code: str
    status: Literal["passed", "failed", "skipped"]
    failure_code: str | None = None
    duration_bucket: Literal["lt_10ms", "lt_100ms", "lt_1s", "lt_10s", "gte_10s"]
    evidence_sha256: str
    created_at: datetime


class AdapterContractProjectionResponse(ProviderEngineeringModel):
    provider_adapter_release_id: str
    view: Literal["runs", "cases"]
    runs: tuple[AdapterContractRunInfo, ...] = ()
    cases: tuple[AdapterContractCaseInfo, ...] = ()
    next_cursor: str | None = None
    limit: int = Field(ge=1, le=500)
    permission_fingerprint: str
    request_id: str
    trace_id: str


class AdapterCommandRequest(ProviderEngineeringModel):
    action: Literal[
        "create_candidate", "run_contract", "review_failure", "release", "deprecate", "revoke"
    ]
    sdk_release_id: str | None = None
    release_version: str | None = None
    protocol_family: Literal["openai_compatible", "anthropic_compatible", "generic_http"] | None = (
        None
    )
    spec_manifest_version: str | None = None
    spec_manifest_sha256: str | None = None
    compatibility_sha256: str | None = None
    required_runtime_version: str | None = None
    required_api_version: str | None = None
    contract_run_id: str | None = None
    failure_disposition: Literal["approve_remediation", "reject"] | None = None

    @model_validator(mode="after")
    def exact_action_shape(self) -> AdapterCommandRequest:
        supplied = self.model_fields_set - {"action"}
        candidate = {
            "sdk_release_id",
            "release_version",
            "protocol_family",
            "spec_manifest_version",
            "spec_manifest_sha256",
            "compatibility_sha256",
            "required_runtime_version",
            "required_api_version",
        }
        if self.action == "create_candidate" and supplied == candidate:
            return self
        if self.action == "review_failure" and supplied == {
            "contract_run_id",
            "failure_disposition",
        }:
            return self
        if self.action in {"run_contract", "release", "deprecate", "revoke"} and not supplied:
            return self
        raise ValueError("invalid adapter command shape")


class AdapterCommandResponse(ProviderEngineeringModel):
    adapter: ProviderAdapterReleaseInfo
    command_id: str
    contract_run_id: str | None = None
    idempotent_replay: bool = False
    permission_fingerprint: str
    request_id: str
    trace_id: str


CatalogStatus = Literal[
    "legacy_reconciled", "candidate", "security_review", "approved", "released", "deprecated"
]


class BuiltinErrorCatalogInfo(ProviderEngineeringModel):
    error_catalog_release_id: str
    builtin_catalog_version: str
    catalog_sha256: str
    signed_candidate_id: str | None = None
    sdk_release_id: str | None = None
    artifact_sha256: str
    rule_count: int = Field(ge=1, le=100000)
    compatibility_sha256: str
    status: CatalogStatus
    revision: int = Field(ge=1)
    release_generation: int = Field(ge=0)
    is_head: bool
    actions: tuple[str, ...]
    created_at: datetime
    updated_at: datetime
    released_at: datetime | None = None


class ErrorCatalogCommandRequest(ProviderEngineeringModel):
    action: Literal["create_candidate", "submit_review", "security_approve", "release", "deprecate"]
    error_catalog_release_id: str | None = None
    builtin_catalog_version: str | None = None
    sdk_release_id: str | None = None
    signed_candidate_id: str | None = None
    expected_revision: StrictInt | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def exact_action_shape(self) -> ErrorCatalogCommandRequest:
        supplied = self.model_fields_set - {"action"}
        if self.action == "create_candidate" and supplied == {
            "builtin_catalog_version",
            "sdk_release_id",
            "signed_candidate_id",
        }:
            return self
        if self.action != "create_candidate" and supplied == {
            "error_catalog_release_id",
            "expected_revision",
        }:
            return self
        raise ValueError("invalid catalog command shape")


class ErrorCatalogCommandResponse(ProviderEngineeringModel):
    catalog: BuiltinErrorCatalogInfo
    command_id: str
    idempotent_replay: bool = False
    permission_fingerprint: str
    request_id: str
    trace_id: str


class WorkspaceErrorOverrideInfo(ProviderEngineeringModel):
    provider_error_override_id: str
    provider_id: str | None = None
    provider_code: str | None = None
    provider_name: str | None = None
    priority: int = Field(ge=1, le=1000)
    http_statuses: tuple[int, ...]
    http_status_class: Literal["4xx"] | None = None
    error_field: Literal["error.code", "error.type", "code", "type"] | None = None
    field_operator: Literal["equals", "contains"] | None = None
    field_pattern: str | None = None
    output_error_code: str
    status: Literal["active", "disabled", "archived"]
    version: int = Field(ge=1)
    revision: int = Field(ge=1)
    pattern_sha256: str
    content_sha256: str
    updated_at: datetime
    actions: tuple[str, ...]


class WorkspaceOverrideHeadInfo(ProviderEngineeringModel):
    revision: int = Field(ge=0)
    content_sha256: str
    builtin_catalog_version: str
    updated_at: datetime | None = None


class ProviderCompatibilityInfo(ProviderEngineeringModel):
    provider_id: str
    provider_code: str
    provider_catalog_version: str | None = None
    provider_adapter_release_id: str | None = None
    builtin_error_catalog_release_id: str | None = None
    override_head_revision: int = Field(ge=0)
    compatibility_status: Literal["compatible", "warning", "blocked", "unknown"]
    reason_codes: tuple[str, ...]
    health_status: Literal["healthy", "degraded", "unavailable", "unknown"]
    projected_at: datetime


class WorkspaceErrorOverrideListResponse(ProviderEngineeringModel):
    overrides: tuple[WorkspaceErrorOverrideInfo, ...]
    head: WorkspaceOverrideHeadInfo
    compatibility: ProviderCompatibilityInfo | None = None
    next_cursor: str | None = None
    limit: int = Field(ge=1, le=100)
    permission_revision: int = Field(ge=1)
    permission_fingerprint: str
    request_id: str
    trace_id: str


class WorkspaceOverrideCommandRequest(ProviderEngineeringModel):
    action: Literal["create", "update", "enable", "disable", "archive"]
    provider_error_override_id: str | None = None
    provider_id: str | None = None
    priority: StrictInt | None = Field(default=None, ge=1, le=1000)
    http_statuses: tuple[StrictInt, ...] | None = None
    http_status_class: Literal["4xx"] | None = None
    error_field: Literal["error.code", "error.type", "code", "type"] | None = None
    field_operator: Literal["equals", "contains"] | None = None
    field_pattern: str | None = None
    output_error_code: str | None = None
    expected_version: StrictInt | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def exact_action_shape(self) -> WorkspaceOverrideCommandRequest:
        supplied = self.model_fields_set - {"action"}
        editable = {
            "provider_id",
            "priority",
            "http_statuses",
            "http_status_class",
            "error_field",
            "field_operator",
            "field_pattern",
            "output_error_code",
        }
        if self.action == "create" and supplied == editable:
            return self
        if (
            self.action == "update"
            and {
                "provider_error_override_id",
                "expected_version",
            }.issubset(supplied)
            and bool(supplied & editable)
        ):
            return self
        if self.action in {"enable", "disable", "archive"} and supplied == {
            "provider_error_override_id",
            "expected_version",
        }:
            return self
        raise ValueError("invalid override command shape")


class WorkspaceOverrideCommandResponse(ProviderEngineeringModel):
    override: WorkspaceErrorOverrideInfo
    head: WorkspaceOverrideHeadInfo
    compatibility: ProviderCompatibilityInfo
    command_id: str
    changed: bool
    idempotent_replay: bool = False
    permission_revision: int = Field(ge=1)
    permission_fingerprint: str
    request_id: str
    trace_id: str


__all__ = [name for name in globals() if not name.startswith("_")]
