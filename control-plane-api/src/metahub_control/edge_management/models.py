"""Strict, secret-aware HTTP contracts for PRD-120 private gateway identity."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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
    permissions: tuple[
        Literal["edge.self.connect", "edge.self.register", "edge.self.rotate"], ...
    ]
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
    updated_at: datetime


class PlatformEdgeFleetResponse(_StrictModel):
    deployments: tuple[PlatformEdgeFleetSummary, ...]
    request_id: str
    trace_id: str
