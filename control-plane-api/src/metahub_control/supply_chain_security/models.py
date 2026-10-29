"""Strict public DTOs for PRD-136 supply-chain security."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator


def _timestamp(value: object) -> datetime:
    if type(value) is not str or not 20 <= len(value) <= 40:
        raise ValueError("timestamp must be an ISO-8601 string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include an offset")
    return parsed


RequestTimestamp = Annotated[datetime, BeforeValidator(_timestamp)]


class SupplyChainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SbomSummary(SupplyChainModel):
    sbom_report_id: str = Field(pattern=r"^sbr_[0-9a-f]{32}$")
    format: Literal["cyclonedx", "spdx"]
    format_version: Literal["1.5", "2.3"]
    component_count: int = Field(ge=1, le=1_000_000)
    sbom_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    immutable_object_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    license_summary: dict[str, int]
    vulnerability_summary: dict[str, int]
    generated_at: datetime


class ArtifactSignatureSummary(SupplyChainModel):
    artifact_signature_id: str = Field(pattern=r"^asi_[0-9a-f]{32}$")
    purpose: Literal["metahub.artifact.attestation/v1"]
    algorithm: Literal["ed25519"]
    key_id: str
    key_version: int = Field(ge=1)
    public_key_b64: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sbom_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    signature_b64: str = Field(pattern=r"^[A-Za-z0-9_-]{86}$")
    signature_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verification_outcome: Literal["passed", "failed", "revoked"]
    signed_at: datetime


class PublicSecurityExceptionSummary(SupplyChainModel):
    security_exception_id: str = Field(pattern=r"^sex_[0-9a-f]{32}$")
    workspace_id: str = Field(pattern=r"^wsp_[0-9a-f]{32}$")
    reason_code: Literal["vendor_fix_pending", "mitigated_exposure", "emergency_continuity"]
    status: Literal["pending", "active", "expired", "revoked"]
    revision: int = Field(ge=1)
    starts_at: datetime
    expires_at: datetime


class PatchCompatibilityTarget(SupplyChainModel):
    n: Literal["passed"]
    n_minus_1: Literal["passed"]


class PatchCompatibilityMatrix(SupplyChainModel):
    cloud: PatchCompatibilityTarget
    edge: PatchCompatibilityTarget


class PatchReleaseSummary(SupplyChainModel):
    patch_release_id: str = Field(pattern=r"^ptr_[0-9a-f]{32}$")
    workspace_id: str = Field(pattern=r"^wsp_[0-9a-f]{32}$")
    vulnerability_finding_id: str = Field(pattern=r"^vfn_[0-9a-f]{32}$")
    affected_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fixed_sdk_release_id: str = Field(pattern=r"^sdr_[0-9a-f]{32}$")
    fixed_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    compatibility_matrix: PatchCompatibilityMatrix
    compatibility_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rollback_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["proposed", "approved", "released", "rolled_back"]
    revision: int = Field(ge=1)
    released_at: datetime | None
    updated_at: datetime


class VulnerabilitySummary(SupplyChainModel):
    vulnerability_finding_id: str = Field(pattern=r"^vfn_[0-9a-f]{32}$")
    advisory_code: str
    severity: Literal["low", "medium", "high", "critical"]
    evaluation_generation: int = Field(ge=1)
    remediation_state: Literal["open", "fixed_available", "patched", "accepted", "closed"]
    fixed_version_code: str | None
    first_seen_at: datetime
    last_seen_at: datetime
    sla_deadline_at: datetime
    exceptions: tuple[PublicSecurityExceptionSummary, ...]
    patch_releases: tuple[PatchReleaseSummary, ...]


class ReleaseSecurityReport(SupplyChainModel):
    sdk_release_id: str = Field(pattern=r"^sdr_[0-9a-f]{32}$")
    language: Literal["python", "javascript"]
    sdk_version: str
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    gate_outcome: Literal["passed", "blocked", "withdrawn", "attestation_invalid"]
    blocking_finding_count: int = Field(ge=0)
    sbom: SbomSummary
    signature: ArtifactSignatureSummary
    vulnerabilities: tuple[VulnerabilitySummary, ...]
    generated_at: datetime


class ReleaseSecurityReportResponse(SupplyChainModel):
    report: ReleaseSecurityReport
    request_id: str
    trace_id: str


class BuildProvenanceSummary(SupplyChainModel):
    build_provenance_id: str = Field(pattern=r"^bpr_[0-9a-f]{32}$")
    sdk_release_id: str = Field(pattern=r"^sdr_[0-9a-f]{32}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_commit_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_tree_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dependency_lock_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    builder_verified: bool
    builder_image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    build_invocation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    environment_allowlist_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reproducibility_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reproducible: bool
    generated_at: datetime


class BuildProvenanceResponse(SupplyChainModel):
    provenance: BuildProvenanceSummary
    request_id: str
    trace_id: str


class SecurityExceptionCommand(SupplyChainModel):
    operation: Literal["request", "approve"]
    workspace_id: str | None = Field(default=None, pattern=r"^wsp_[0-9a-f]{32}$")
    reason_code: (
        Literal["vendor_fix_pending", "mitigated_exposure", "emergency_continuity"] | None
    ) = None
    compensating_controls: (
        list[Literal["network_isolation", "runtime_detection", "feature_disablement"]] | None
    ) = Field(default=None, min_length=1, max_length=3)
    starts_at: RequestTimestamp | None = None
    expires_at: RequestTimestamp | None = None
    security_exception_id: str | None = Field(default=None, pattern=r"^sex_[0-9a-f]{32}$")
    expected_revision: int | None = Field(default=None, ge=1)
    confirmed: Literal[True]

    @model_validator(mode="after")
    def operation_shape(self) -> SecurityExceptionCommand:
        request_fields = (
            self.workspace_id,
            self.reason_code,
            self.compensating_controls,
            self.starts_at,
            self.expires_at,
        )
        if self.operation == "request":
            if any(value is None for value in request_fields):
                raise ValueError("request fields required")
            if self.security_exception_id is not None or self.expected_revision is not None:
                raise ValueError("approval fields forbidden")
            if self.compensating_controls != sorted(set(self.compensating_controls or [])):
                raise ValueError("controls must be canonical")
            if self.starts_at and self.expires_at and self.expires_at <= self.starts_at:
                raise ValueError("invalid exception interval")
        else:
            if self.security_exception_id is None or self.expected_revision is None:
                raise ValueError("approval fields required")
            if any(value is not None for value in request_fields):
                raise ValueError("request fields forbidden")
        return self


class SecurityExceptionSummary(SupplyChainModel):
    security_exception_id: str = Field(pattern=r"^sex_[0-9a-f]{32}$")
    workspace_id: str = Field(pattern=r"^wsp_[0-9a-f]{32}$")
    vulnerability_finding_id: str = Field(pattern=r"^vfn_[0-9a-f]{32}$")
    affected_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason_code: Literal["vendor_fix_pending", "mitigated_exposure", "emergency_continuity"]
    compensating_controls: tuple[
        Literal["network_isolation", "runtime_detection", "feature_disablement"], ...
    ]
    status: Literal["pending", "active", "expired", "revoked"]
    review_generation: int = Field(ge=1)
    revision: int = Field(ge=1)
    starts_at: datetime
    expires_at: datetime
    approved_at: datetime | None
    updated_at: datetime


class SecurityExceptionResponse(SupplyChainModel):
    exception: SecurityExceptionSummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class PatchReleaseCommand(SupplyChainModel):
    operation: Literal["propose", "approve", "release", "rollback"]
    workspace_id: str | None = Field(default=None, pattern=r"^wsp_[0-9a-f]{32}$")
    fixed_sdk_release_id: str | None = Field(default=None, pattern=r"^sdr_[0-9a-f]{32}$")
    compatibility_matrix: PatchCompatibilityMatrix | None = None
    rollback_artifact_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    patch_release_id: str | None = Field(default=None, pattern=r"^ptr_[0-9a-f]{32}$")
    expected_revision: int | None = Field(default=None, ge=1)
    confirmed: Literal[True]

    @model_validator(mode="after")
    def operation_shape(self) -> PatchReleaseCommand:
        proposal_fields = (
            self.workspace_id,
            self.fixed_sdk_release_id,
            self.compatibility_matrix,
            self.rollback_artifact_sha256,
        )
        if self.operation == "propose":
            if any(value is None for value in proposal_fields):
                raise ValueError("proposal fields required")
            if self.patch_release_id is not None or self.expected_revision is not None:
                raise ValueError("transition fields forbidden")
        else:
            if self.patch_release_id is None or self.expected_revision is None:
                raise ValueError("transition fields required")
            if any(value is not None for value in proposal_fields):
                raise ValueError("proposal fields forbidden")
        return self


class PatchReleaseResponse(SupplyChainModel):
    patch_release: PatchReleaseSummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str
