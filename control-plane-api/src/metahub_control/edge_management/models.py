"""Strict, secret-aware HTTP contracts for PRD-120 private gateway identity."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from metahub_shared.private_gateway import (
    DeploymentValidationReport,
    DiagnosticCommandEnvelope,
    DiagnosticExecutionReport,
    DiagnosticInvocation,
    DiagnosticResult,
    InstallerRelease,
    PreflightCheck,
    PreflightReport,
    SecretReference,
    TelemetryLevel,
    TelemetryStream,
    ValidationCheckCode,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class WorkspaceRegistrationCreateRequest(_StrictModel):
    display_name: str = Field(min_length=2, max_length=120)
    region_code: str = Field(min_length=2, max_length=40)
    mode: Literal["connected", "isolated"] = "connected"
    token_ttl_seconds: int = Field(default=600, ge=60, le=600)


class EdgeAgentRegistrationRequest(_StrictModel):
    deployment_id: str = Field(min_length=36, max_length=36)
    registration_token: str = Field(min_length=47, max_length=47, repr=False)
    challenge: str = Field(min_length=47, max_length=47, repr=False)
    challenge_proof: str = Field(min_length=43, max_length=43, repr=False)
    csr_pem: str = Field(min_length=128, max_length=8192, repr=False)
    agent_version: str = Field(min_length=1, max_length=64)
    capability_codes: list[str] = Field(default_factory=list, max_length=12)
    hardware_proof_sha256: str | None = Field(default=None, min_length=64, max_length=64)


class EdgeCertificateRotationRequest(_StrictModel):
    deployment_id: str = Field(min_length=36, max_length=36)
    expected_generation: int = Field(ge=1)
    rotation_nonce: str = Field(min_length=36, max_length=68, repr=False)
    csr_pem: str = Field(min_length=128, max_length=8192, repr=False)
    hardware_proof_sha256: str | None = Field(default=None, min_length=64, max_length=64)


class EdgeSessionCreateRequest(_StrictModel):
    deployment_id: str = Field(min_length=36, max_length=36)
    handshake_nonce: str = Field(min_length=36, max_length=68, repr=False)
    ttl_seconds: int = Field(default=45, ge=15, le=45)


class EdgeDeploymentRevokeRequest(_StrictModel):
    reason_code: Literal[
        "owner_requested", "security_incident", "device_lost", "deployment_retired"
    ] = "owner_requested"


class EdgeDesiredStatePublishRequest(_StrictModel):
    action: Literal["publish"]
    source_config_snapshot_id: str = Field(pattern=r"^csn_[0-9a-f]{32}$")


class EdgeDesiredStateApprovalRequest(_StrictModel):
    action: Literal["approve", "reject"]
    desired_state_id: str = Field(pattern=r"^edst_[0-9a-f]{32}$")
    reason_code: Literal["approved_change", "risk_unacceptable", "change_not_required"]


class EdgeDesiredStateRollbackRequest(_StrictModel):
    lkg_generation: int = Field(ge=1)
    reason_code: Literal["operator_requested", "apply_failed_recovery"]


class EdgeAckCreateRequest(_StrictModel):
    signed_snapshot_id: str = Field(pattern=r"^edsn_[0-9a-f]{32}$")
    generation: int = Field(ge=1)
    envelope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["applied", "failed"]
    reason_code: Literal["applied", "preflight_failed", "apply_failed", "storage_failed"]
    apply_duration_ms: int = Field(ge=0, le=300_000)
    lkg_generation: int = Field(ge=0)
    lkg_envelope_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    attempt_nonce: str = Field(min_length=36, max_length=68, repr=False)


class EdgeCertificateSummary(_StrictModel):
    certificate_id: str
    identity_id: str
    generation: int
    status: Literal["active", "draining", "superseded", "revoked", "expired"]
    certificate_sha256: str
    public_key_sha256: str
    issuer_key_id: Literal["edge-device-ca-v1"]
    not_before: datetime
    not_after: datetime
    drain_until: datetime | None = None


class EdgeEventSummary(_StrictModel):
    event_id: str
    event_type: Literal[
        "edge.registered",
        "edge.certificate_rotated",
        "edge.revoked",
        "edge.authentication_failed",
    ]
    generation: int
    outcome_code: Literal["succeeded", "rejected"]
    reason_code: str
    occurred_at: datetime


class EdgeDeploymentSummary(_StrictModel):
    deployment_id: str
    workspace_id: str
    display_name: str
    region_code: str
    mode: Literal["connected", "isolated"]
    status: Literal["pending", "active", "revoked", "retired"]
    revision: int
    revocation_generation: int
    agent_version: str | None = None
    health_code: Literal[
        "not_registered",
        "healthy",
        "certificate_expiring",
        "revoked",
        "offline",
        "unknown",
    ]
    last_connected_at: datetime | None = None
    certificate_expires_at: datetime | None = None
    identity_generation: int | None = None
    current_certificate: EdgeCertificateSummary | None = None
    recent_events: tuple[EdgeEventSummary, ...] = ()
    created_at: datetime
    updated_at: datetime
    revoked_at: datetime | None = None


class EdgeDeploymentListResponse(_StrictModel):
    deployments: tuple[EdgeDeploymentSummary, ...]
    request_id: str
    trace_id: str


class WorkspaceRegistrationResponse(_StrictModel):
    deployment: EdgeDeploymentSummary
    registration_intent_id: str
    registration_token: str | None = Field(default=None, repr=False)
    challenge: str | None = Field(default=None, repr=False)
    expires_at: datetime
    idempotent_replay: bool
    request_id: str
    trace_id: str


class EdgeDeviceIdentitySummary(_StrictModel):
    identity_id: str
    deployment_id: str
    public_key_sha256: str
    hardware_proof_sha256: str | None = None
    capability_codes: tuple[str, ...]
    agent_version: str
    generation: int
    status: Literal["active", "superseded", "revoked", "retired"]
    created_at: datetime


class EdgeCertificateResponse(_StrictModel):
    deployment_id: str
    identity: EdgeDeviceIdentitySummary
    certificate: EdgeCertificateSummary
    certificate_pem: str = Field(repr=False)
    ca_certificate_pem: str
    permissions: tuple[Literal["edge.self.connect", "edge.self.register", "edge.self.rotate"], ...]
    idempotent_replay: bool
    request_id: str
    trace_id: str


class EdgeSessionResponse(_StrictModel):
    session_id: str
    deployment_id: str
    identity_id: str
    certificate_id: str
    identity_generation: int
    revocation_generation: int
    session_token: str = Field(repr=False)
    expires_at: datetime
    request_id: str
    trace_id: str


class EdgeDeploymentRevokeResponse(_StrictModel):
    deployment: EdgeDeploymentSummary
    revoked_session_count: int
    idempotent_replay: bool
    request_id: str
    trace_id: str


class EdgeDesiredStateSummary(_StrictModel):
    desired_state_id: str
    deployment_id: str
    generation: int
    base_generation: int
    rollback_of_generation: int | None = None
    source_config_snapshot_id: str
    source_generation: int
    source_revocation_watermark: int
    change_class: Literal["standard", "high_risk"]
    status: Literal["approval_pending", "published", "superseded", "rejected"]
    revision: int
    head_revision: int
    requested_by_account_id: str
    approved_by_account_id: str | None = None
    desired_generation: int
    applied_generation: int
    lkg_generation: int
    last_ack_status: Literal["not_observed", "applied", "failed"]
    last_reason_code: Literal[
        "not_observed", "applied", "preflight_failed", "apply_failed", "storage_failed"
    ]
    drifted: bool
    requested_at: datetime
    decided_at: datetime | None = None
    published_at: datetime | None = None
    last_ack_at: datetime | None = None
    updated_at: datetime


class EdgeDesiredStateResponse(_StrictModel):
    state: EdgeDesiredStateSummary | None = None
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class EdgeDesiredStatePullResponse(_StrictModel):
    deployment_id: str
    generation: int
    envelope_sha256: str
    snapshot: dict[str, object]
    trust_manifest: dict[str, object]
    request_id: str
    trace_id: str


class EdgeAckResponse(_StrictModel):
    ack_id: str
    disposition: Literal["accepted", "late"]
    status: Literal["applied", "failed"]
    desired_generation: int
    applied_generation: int
    lkg_generation: int
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class EdgeTelemetryPolicyPatchRequest(_StrictModel):
    telemetry_level: TelemetryLevel


class EdgeTelemetryPolicySummary(_StrictModel):
    policy_id: str = Field(pattern=r"^etp_[0-9a-f]{32}$")
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    telemetry_level: TelemetryLevel
    revision: int = Field(ge=1)
    updated_at: datetime


class EdgeTelemetryCursorSummary(_StrictModel):
    cursor_id: str = Field(pattern=r"^euc_[0-9a-f]{32}$")
    stream: TelemetryStream
    epoch_id: str = Field(pattern=r"^ete_[0-9a-f]{32}$")
    contiguous_sequence: int = Field(ge=0)
    highest_seen_sequence: int = Field(ge=0)
    expected_sequence: int = Field(ge=1)
    updated_at: datetime


class EdgeUsageTotals(_StrictModel):
    request_count: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_tokens: int = Field(ge=0)
    cost_cny_micros: int = Field(ge=0)
    cost_usd_micros: int = Field(ge=0)


class EdgeFailureCountSummary(_StrictModel):
    component_code: Literal[
        "gateway", "config_sync", "provider_pool", "usage_spool", "telemetry_sync"
    ]
    failure_code: Literal[
        "capacity_exhausted",
        "config_unavailable",
        "internal_error",
        "network_unreachable",
        "policy_denied",
        "provider_5xx",
        "provider_rate_limited",
        "provider_timeout",
        "request_cancelled",
    ]
    count: int = Field(ge=1)


class EdgeTelemetryWatermarkResponse(_StrictModel):
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    policy: EdgeTelemetryPolicySummary | None = None
    cursors: tuple[EdgeTelemetryCursorSummary, ...] = ()
    usage: EdgeUsageTotals | None = None
    failures: tuple[EdgeFailureCountSummary, ...] = ()
    last_receipt_at: datetime | None = None
    lag_seconds: int | None = Field(default=None, ge=0)
    backlog_seconds: int | None = Field(default=None, ge=0)
    request_id: str
    trace_id: str


class EdgeAgentTelemetryWatermarkResponse(_StrictModel):
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    policy: EdgeTelemetryPolicySummary
    cursors: tuple[EdgeTelemetryCursorSummary, ...]
    request_id: str
    trace_id: str


class EdgeTelemetryPolicyResponse(_StrictModel):
    policy: EdgeTelemetryPolicySummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class EdgeSecretReferenceListResponse(_StrictModel):
    references: tuple[SecretReference, ...]
    request_id: str
    trace_id: str


class InstallerArtifactDownload(_StrictModel):
    artifact_id: str = Field(pattern=r"^ira_[0-9a-f]{32}$")
    release_id: str = Field(pattern=r"^irl_[0-9a-f]{32}$")
    filename: str
    download_path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1)


class EdgePreflightSummary(_StrictModel):
    preflight_report_id: str = Field(pattern=r"^pfr_[0-9a-f]{32}$")
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    release_id: str = Field(pattern=r"^irl_[0-9a-f]{32}$")
    install_profile_id: str = Field(pattern=r"^ipr_[0-9a-f]{32}$")
    generation: int = Field(ge=1)
    outcome: Literal["passed", "failed"]
    checks: tuple[PreflightCheck, ...]
    duration_ms: int = Field(ge=0, le=120_000)
    started_at: datetime
    completed_at: datetime
    received_at: datetime


class EdgeInstallationSummary(_StrictModel):
    installation_id: str = Field(pattern=r"^ins_[0-9a-f]{32}$")
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    release_id: str = Field(pattern=r"^irl_[0-9a-f]{32}$")
    install_profile_id: str = Field(pattern=r"^ipr_[0-9a-f]{32}$")
    preflight_report_id: str = Field(pattern=r"^pfr_[0-9a-f]{32}$")
    generation: int = Field(ge=1)
    status: Literal[
        "preflight_passed",
        "installing",
        "installed",
        "connecting",
        "active",
        "failed",
        "uninstalling",
        "uninstalled",
    ]
    current_step: Literal[
        "staging_prepared",
        "artifacts_copied",
        "runtime_validated",
        "runtime_started",
        "health_verified",
        "release_committed",
        "device_registered",
        "initial_snapshot",
    ] | None = None
    revision: int = Field(ge=1)
    journal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    last_receipt_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    compensation_status: Literal["not_required", "pending", "completed", "failed"]
    retain_config: bool
    retain_audit: bool
    retain_secrets: bool
    failure_reason_code: str | None = None
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    failed_at: datetime | None = None


class EdgeInstallerCatalogResponse(_StrictModel):
    releases: tuple[InstallerRelease, ...]
    downloads: tuple[InstallerArtifactDownload, ...]
    preflights: tuple[EdgePreflightSummary, ...]
    installations: tuple[EdgeInstallationSummary, ...]
    request_id: str
    trace_id: str


class EdgePreflightRecordRequest(_StrictModel):
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    report: PreflightReport


class EdgePreflightRecordResponse(_StrictModel):
    preflight_report_id: str = Field(pattern=r"^pfr_[0-9a-f]{32}$")
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    release_id: str = Field(pattern=r"^irl_[0-9a-f]{32}$")
    install_profile_id: str = Field(pattern=r"^ipr_[0-9a-f]{32}$")
    generation: int = Field(ge=1)
    outcome: Literal["passed", "failed"]
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotent_replay: bool
    request_id: str
    trace_id: str


class EdgeInstallationStartRequest(_StrictModel):
    installation_id: str = Field(pattern=r"^ins_[0-9a-f]{32}$")
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    preflight_report_id: str = Field(pattern=r"^pfr_[0-9a-f]{32}$")
    generation: int = Field(ge=1)
    journal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class EdgeInstallationUpdateRequest(_StrictModel):
    status: Literal[
        "installing",
        "installed",
        "connecting",
        "active",
        "failed",
        "uninstalling",
        "uninstalled",
    ]
    current_step: Literal[
        "staging_prepared",
        "artifacts_copied",
        "runtime_validated",
        "runtime_started",
        "health_verified",
        "release_committed",
        "device_registered",
        "initial_snapshot",
    ] | None = None
    journal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    compensation_status: Literal["not_required", "pending", "completed", "failed"]
    retain_config: bool = True
    retain_audit: bool = True
    retain_secrets: bool = True
    reason_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{1,79}$")


class EdgeInstallationResponse(_StrictModel):
    installation: EdgeInstallationSummary
    idempotent_replay: bool
    request_id: str
    trace_id: str


class EdgeValidationStartRequest(_StrictModel):
    action: Literal["start", "retry_failed"]
    source_validation_run_id: str | None = Field(
        default=None, pattern=r"^dvr_[0-9a-f]{32}$"
    )

    @model_validator(mode="after")
    def _source_matches_action(self) -> EdgeValidationStartRequest:
        if (self.action == "retry_failed") != (self.source_validation_run_id is not None):
            raise ValueError("edge.validation_request_invalid")
        return self


class EdgeValidationCheckSummary(_StrictModel):
    validation_check_id: str = Field(pattern=r"^dvc_[0-9a-f]{32}$")
    code: ValidationCheckCode
    outcome: Literal["passed", "failed"]
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,79}$")
    recovery_action_code: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_.-]{1,79}$"
    )
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    duration_ms: int = Field(ge=0, le=900_000)
    source_validation_run_id: str = Field(pattern=r"^dvr_[0-9a-f]{32}$")
    created_at: datetime


class EdgeDeliveryAcceptanceSummary(_StrictModel):
    delivery_acceptance_id: str = Field(pattern=r"^eda_[0-9a-f]{32}$")
    validation_run_id: str = Field(pattern=r"^dvr_[0-9a-f]{32}$")
    validation_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    deployment_revision: int = Field(ge=1)
    installer_release_id: str = Field(pattern=r"^irl_[0-9a-f]{32}$")
    desired_generation: int = Field(ge=0)
    applied_generation: int = Field(ge=0)
    lkg_generation: int = Field(ge=0)
    accepted_by_account_id: str
    accepted_at: datetime


class EdgeValidationRunSummary(_StrictModel):
    validation_run_id: str = Field(pattern=r"^dvr_[0-9a-f]{32}$")
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    source_validation_run_id: str | None = Field(
        default=None, pattern=r"^dvr_[0-9a-f]{32}$"
    )
    attempt: int = Field(ge=1)
    plan_version: Literal["edge-delivery-v1"]
    deployment_revision: int = Field(ge=1)
    installer_release_id: str = Field(pattern=r"^irl_[0-9a-f]{32}$")
    desired_generation: int = Field(ge=0)
    applied_generation: int = Field(ge=0)
    lkg_generation: int = Field(ge=0)
    input_contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    requested_check_codes: tuple[ValidationCheckCode, ...]
    status: Literal["requested", "completed"]
    outcome: Literal["passed", "failed"] | None = None
    duration_ms: int | None = Field(default=None, ge=0, le=900_000)
    checks: tuple[EdgeValidationCheckSummary, ...] = ()
    effective_checks: tuple[EdgeValidationCheckSummary, ...] = ()
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class EdgeValidationResponse(_StrictModel):
    validation: EdgeValidationRunSummary
    delivery_acceptance: EdgeDeliveryAcceptanceSummary | None = None
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class EdgeValidationReportRequest(_StrictModel):
    report: DeploymentValidationReport


class EdgeDeliveryAcceptanceRequest(_StrictModel):
    validation_run_id: str = Field(pattern=r"^dvr_[0-9a-f]{32}$")


class EdgeDeliveryAcceptanceResponse(_StrictModel):
    acceptance: EdgeDeliveryAcceptanceSummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class EdgeSupportBundleEntrySummary(_StrictModel):
    collector_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,79}$")
    classification: Literal["version", "watermark", "health", "error_code", "log_summary"]
    media_type: Literal["application/json", "application/x-ndjson"]
    size_bytes: int = Field(ge=1, le=8 * 1024 * 1024)


class EdgeSupportBundleSummary(_StrictModel):
    support_bundle_id: str = Field(pattern=r"^esb_[0-9a-f]{32}$")
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    status: Literal["ready", "expired", "deleted"]
    revision: int = Field(ge=1)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    entries: tuple[EdgeSupportBundleEntrySummary, ...] = Field(min_length=1, max_length=64)
    scan_version: Literal["support-bundle-scan-v1"]
    scan_outcome: Literal["clean"]
    archive_size_bytes: int = Field(ge=1, le=100 * 1024 * 1024)
    object_size_bytes: int = Field(ge=34, le=100 * 1024 * 1024 + 64)
    approved_by_account_id: str
    approved_at: datetime
    expires_at: datetime
    created_at: datetime


class EdgeSupportBundleResponse(_StrictModel):
    bundle: EdgeSupportBundleSummary
    request_id: str
    trace_id: str


class PlatformInstallerReleaseSubmitRequest(_StrictModel):
    release: InstallerRelease
    reason_code: Literal["release_candidate"]


class PlatformInstallerReleaseDecisionRequest(_StrictModel):
    action: Literal["approve", "withdraw"]
    reason_code: Literal[
        "release_approved",
        "release_cancelled",
        "integrity_incident",
        "superseded",
    ]


class PlatformInstallerReleaseSummary(_StrictModel):
    release: InstallerRelease
    status: Literal["approval_pending", "released", "withdrawn"]
    revision: int = Field(ge=1)
    submitted_by_account_id: str
    approved_by_account_id: str | None = None
    submitted_at: datetime
    approved_at: datetime | None = None
    withdrawn_at: datetime | None = None


class PlatformInstallerReleaseResponse(_StrictModel):
    release: PlatformInstallerReleaseSummary
    idempotent_replay: bool
    request_id: str
    trace_id: str


class PlatformInstallerReleaseListResponse(_StrictModel):
    releases: tuple[PlatformInstallerReleaseSummary, ...]
    security_projection: bool
    request_id: str
    trace_id: str


class PlatformEdgeFleetSummary(_StrictModel):
    deployment_id: str
    region_code: str
    mode: Literal["connected", "isolated"]
    status: Literal["pending", "active", "revoked", "retired"]
    revision: int
    revocation_generation: int
    agent_version: str | None = None
    health_code: str
    certificate_expires_at: datetime | None = None
    last_connected_at: datetime | None = None
    desired_generation: int = 0
    applied_generation: int = 0
    lkg_generation: int = 0
    desired_state_status: Literal["not_published", "not_observed", "applied", "failed"] = (
        "not_published"
    )
    desired_state_reason_code: Literal[
        "not_published",
        "not_observed",
        "applied",
        "preflight_failed",
        "apply_failed",
        "storage_failed",
    ] = "not_published"
    desired_state_drifted: bool = False
    telemetry_last_receipt_at: datetime | None = None
    telemetry_lag_seconds: int | None = Field(default=None, ge=0)
    telemetry_backlog_seconds: int | None = Field(default=None, ge=0)
    telemetry_cpu_bucket: Literal["unknown", "low", "medium", "high", "critical"] = (
        "unknown"
    )
    telemetry_memory_bucket: Literal[
        "unknown", "low", "medium", "high", "critical"
    ] = "unknown"
    telemetry_error_rate_basis_points: int = Field(default=0, ge=0, le=10_000)
    telemetry_failure_code: Literal[
        "none",
        "capacity_exhausted",
        "config_unavailable",
        "internal_error",
        "network_unreachable",
        "policy_denied",
        "provider_5xx",
        "provider_rate_limited",
        "provider_timeout",
        "request_cancelled",
    ] = "none"
    updated_at: datetime


class PlatformEdgeFleetResponse(_StrictModel):
    deployments: tuple[PlatformEdgeFleetSummary, ...]
    request_id: str
    trace_id: str


class PlatformFleetDeployment(_StrictModel):
    """Platform-safe projection; it intentionally contains no Workspace details."""

    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    region_code: str = Field(min_length=2, max_length=40)
    mode: Literal["connected", "isolated"]
    status: Literal["pending", "active", "revoked", "retired"]
    health_code: Literal["healthy", "degraded", "critical", "offline", "revoked", "unknown"]
    health_score: int = Field(ge=0, le=100)
    alert_code: Literal[
        "none",
        "certificate",
        "version",
        "capacity",
        "configuration",
        "telemetry",
        "validation",
        "offline",
        "revoked",
    ]
    maintenance_mode: bool
    agent_version: str | None = Field(default=None, min_length=1, max_length=64)
    desired_generation: int = Field(ge=0)
    applied_generation: int = Field(ge=0)
    lkg_generation: int = Field(ge=0)
    telemetry_lag_seconds: int | None = Field(default=None, ge=0)
    certificate_signal: Literal["healthy", "expiring", "expired", "missing", "revoked"]
    source_health_generation: int = Field(ge=1)
    index_revision: int = Field(ge=1)
    updated_at: datetime


class PlatformFleetPageResponse(_StrictModel):
    deployments: tuple[PlatformFleetDeployment, ...]
    next_cursor: str | None = Field(default=None, min_length=16, max_length=2048)
    count: int = Field(ge=0, le=200)
    request_id: str
    trace_id: str


class EdgeDiagnosticGrantMutationRequest(_StrictModel):
    action: Literal["request", "approve", "revoke"]
    diagnostic_grant_id: str | None = Field(default=None, pattern=r"^dgr_[0-9a-f]{32}$")
    invocation: DiagnosticInvocation | None = None
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,79}$")
    max_runs: int | None = Field(default=None, ge=1, le=3)
    ttl_seconds: int | None = Field(default=None, ge=60, le=300)

    @model_validator(mode="after")
    def _fields_match_action(self) -> EdgeDiagnosticGrantMutationRequest:
        request_fields = self.invocation is not None and self.max_runs is not None
        if self.action == "request":
            if (
                not request_fields
                or self.diagnostic_grant_id is not None
                or self.ttl_seconds is not None
            ):
                raise ValueError("edge.diagnostic_request_invalid")
        elif self.action == "approve":
            if (
                self.diagnostic_grant_id is None
                or self.ttl_seconds is None
                or self.invocation is not None
                or self.max_runs is not None
            ):
                raise ValueError("edge.diagnostic_request_invalid")
        elif (
            self.diagnostic_grant_id is None
            or self.invocation is not None
            or self.max_runs is not None
            or self.ttl_seconds is not None
        ):
            raise ValueError("edge.diagnostic_request_invalid")
        return self


class EdgeDiagnosticGrantSummary(_StrictModel):
    diagnostic_grant_id: str = Field(pattern=r"^dgr_[0-9a-f]{32}$")
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    status: Literal["pending_customer", "active", "revoked", "expired", "exhausted"]
    command_code: Literal[
        "runtime.version",
        "runtime.watermarks",
        "runtime.disk_budget",
        "runtime.connectivity_summary",
    ]
    invocation: DiagnosticInvocation
    parameters_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,79}$")
    max_runs: int = Field(ge=1, le=3)
    run_count: int = Field(ge=0, le=3)
    revision: int = Field(ge=1)
    requested_at: datetime
    approved_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    updated_at: datetime


class EdgeDiagnosticGrantResponse(_StrictModel):
    grant: EdgeDiagnosticGrantSummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class EdgeDiagnosticCommandMutationRequest(_StrictModel):
    action: Literal["run", "cancel"]
    diagnostic_grant_id: str | None = Field(default=None, pattern=r"^dgr_[0-9a-f]{32}$")
    diagnostic_command_id: str | None = Field(default=None, pattern=r"^dcm_[0-9a-f]{32}$")
    deadline_seconds: int | None = Field(default=None, ge=1, le=120)
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,79}$")

    @model_validator(mode="after")
    def _fields_match_action(self) -> EdgeDiagnosticCommandMutationRequest:
        if self.action == "run":
            if (
                self.diagnostic_grant_id is None
                or self.deadline_seconds is None
                or self.diagnostic_command_id is not None
            ):
                raise ValueError("edge.diagnostic_request_invalid")
        elif (
            self.diagnostic_command_id is None
            or self.diagnostic_grant_id is not None
            or self.deadline_seconds is not None
        ):
            raise ValueError("edge.diagnostic_request_invalid")
        return self


class EdgeDiagnosticCommandSummary(_StrictModel):
    diagnostic_command_id: str = Field(pattern=r"^dcm_[0-9a-f]{32}$")
    diagnostic_grant_id: str = Field(pattern=r"^dgr_[0-9a-f]{32}$")
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    generation: int = Field(ge=1)
    status: Literal[
        "authorized", "leased", "running", "completed", "failed", "cancelled", "expired"
    ]
    command_code: Literal[
        "runtime.version",
        "runtime.watermarks",
        "runtime.disk_budget",
        "runtime.connectivity_summary",
    ]
    envelope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: int = Field(ge=1)
    issued_at: datetime
    deadline_at: datetime
    expires_at: datetime
    lease_expires_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime


class EdgeDiagnosticCommandResponse(_StrictModel):
    command: EdgeDiagnosticCommandSummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class EdgeDiagnosticLeaseRequest(_StrictModel):
    action: Literal["lease", "start"]
    lease_nonce: str = Field(min_length=32, max_length=128, repr=False)
    expected_revision: int = Field(ge=1)


class EdgeDiagnosticPendingResponse(_StrictModel):
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    command: DiagnosticCommandEnvelope | None = Field(default=None, repr=False)
    command_revision: int | None = Field(default=None, ge=1)
    cancelled_command_ids: tuple[str, ...] = Field(default=(), max_length=20)
    request_id: str
    trace_id: str


class EdgeDiagnosticLeaseResponse(_StrictModel):
    command: EdgeDiagnosticCommandSummary
    request_id: str
    trace_id: str


class EdgeDiagnosticResultSummary(_StrictModel):
    diagnostic_result_id: str = Field(pattern=r"^drs_[0-9a-f]{32}$")
    diagnostic_grant_id: str = Field(pattern=r"^dgr_[0-9a-f]{32}$")
    diagnostic_command_id: str = Field(pattern=r"^dcm_[0-9a-f]{32}$")
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    generation: int = Field(ge=1)
    status: Literal["completed", "failed", "cancelled", "timed_out"]
    reason_code: Literal[
        "completed",
        "cancelled",
        "deadline_exceeded",
        "handler_failed",
        "result_invalid",
        "sensitive_output",
    ]
    result: DiagnosticResult | None = Field(default=None, repr=False)
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    duration_ms: int = Field(ge=0, le=120_000)
    started_at: datetime
    completed_at: datetime
    received_at: datetime


class EdgeDiagnosticResultResponse(_StrictModel):
    result: EdgeDiagnosticResultSummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class EdgeHealthSnapshotSummary(_StrictModel):
    edge_health_snapshot_id: str = Field(pattern=r"^ehs_[0-9a-f]{32}$")
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    generation: int = Field(ge=1)
    score_model_version: Literal["edge-health-v1"]
    health_score: int = Field(ge=0, le=100)
    health_code: Literal["healthy", "degraded", "critical", "offline", "revoked", "unknown"]
    certificate_signal: Literal["healthy", "expiring", "expired", "missing", "revoked"]
    version_signal: Literal["current", "outdated", "unknown"]
    capacity_signal: Literal["healthy", "warning", "critical", "unknown"]
    config_signal: Literal["converged", "drifted", "not_published", "unknown"]
    telemetry_signal: Literal["current", "lagging", "stale", "missing"]
    validation_signal: Literal["passed", "failed", "pending", "missing"]
    desired_generation: int = Field(ge=0)
    applied_generation: int = Field(ge=0)
    lkg_generation: int = Field(ge=0)
    telemetry_lag_seconds: int | None = Field(default=None, ge=0)
    certificate_expires_at: datetime | None = None
    agent_version: str | None = Field(default=None, min_length=1, max_length=64)
    observed_at: datetime
    created_at: datetime


class EdgeHealthResponse(_StrictModel):
    health: EdgeHealthSnapshotSummary
    diagnostic_grants: tuple[EdgeDiagnosticGrantSummary, ...] = Field(default=(), max_length=20)
    diagnostic_results: tuple[EdgeDiagnosticResultSummary, ...] = Field(default=(), max_length=20)
    request_id: str
    trace_id: str


class EdgeDiagnosticReportRequest(_StrictModel):
    report: DiagnosticExecutionReport
