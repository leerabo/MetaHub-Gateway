"""Strict HTTP contracts for PRD-128 private-Gateway licenses."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from metahub_shared.private_gateway import LicenseCapacity, SignedEntitlement
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PlatformLicenseIssueRequest(_StrictModel):
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    node_limit: int = Field(ge=1, le=100_000)
    concurrent_request_limit: int = Field(ge=1, le=10_000_000)
    features: list[str] = Field(default_factory=list, max_length=32)
    expires_at: datetime
    grace_days: int = Field(default=7, ge=7, le=30)
    source_order_id: str | None = Field(default=None, pattern=r"^ord_[0-9a-f]{32}$")
    expected_revision: int | None = Field(default=None, ge=1)
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,79}$")

    @model_validator(mode="after")
    def _canonical_capacity(self) -> PlatformLicenseIssueRequest:
        LicenseCapacity(
            node_limit=self.node_limit,
            concurrent_request_limit=self.concurrent_request_limit,
            features=self.features,
        )
        return self


class PlatformLicenseRevokeRequest(_StrictModel):
    expected_revision: int = Field(ge=1)
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,79}$")


class WorkspaceLicenseRefreshRequest(_StrictModel):
    expected_revision: int = Field(ge=1)
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,79}$")


class LicenseUsageSummary(_StrictModel):
    active_node_units: int = Field(ge=0, le=100_000)
    active_request_units: int = Field(ge=0, le=10_000_000)
    denied_count: int = Field(ge=0, le=1_000_000_000)
    bucket_started_at: datetime
    bucket_ended_at: datetime


class LicenseAcknowledgementSummary(_StrictModel):
    acknowledgement_id: str = Field(pattern=r"^lak_[0-9a-f]{32}$")
    generation: int = Field(ge=1)
    disposition: Literal["applied", "rejected"]
    decision_status: Literal[
        "current",
        "grace",
        "restricted",
        "revoked",
        "not_yet_valid",
        "clock_rollback",
    ]
    reason_code: str
    acknowledged_at: datetime


class LicenseSummary(_StrictModel):
    license_id: str = Field(pattern=r"^lic_[0-9a-f]{32}$")
    workspace_id: str = Field(pattern=r"^wsp_[0-9a-f]{32}$")
    tenant_id: str
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    status: Literal["active", "revoked"]
    current_entitlement_id: str = Field(pattern=r"^let_[0-9a-f]{32}$")
    capacity: LicenseCapacity
    generation: int = Field(ge=1)
    refresh_counter: int = Field(ge=1)
    revocation_generation: int = Field(ge=0)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    signing_key_id: str
    issued_at: datetime
    not_before: datetime
    expires_at: datetime
    grace_ends_at: datetime
    last_acknowledgement: LicenseAcknowledgementSummary | None = None
    latest_usage: LicenseUsageSummary | None = None
    revision: int = Field(ge=1)
    updated_at: datetime
    revoked_reason_code: str | None = None
    revoked_at: datetime | None = None


class LicenseResponse(_StrictModel):
    license: LicenseSummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class LicenseListResponse(_StrictModel):
    licenses: tuple[LicenseSummary, ...]
    request_id: str
    trace_id: str


class AgentLicenseResponse(_StrictModel):
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    entitlement: SignedEntitlement
    server_time: datetime
    request_id: str
    trace_id: str


class AgentLicenseAcknowledgementRequest(_StrictModel):
    entitlement_id: str = Field(pattern=r"^let_[0-9a-f]{32}$")
    generation: int = Field(ge=1)
    disposition: Literal["applied", "rejected"]
    decision_status: Literal[
        "current",
        "grace",
        "restricted",
        "revoked",
        "not_yet_valid",
        "clock_rollback",
    ]
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,79}$")
    trusted_wall_time: datetime
    attempt_nonce: str = Field(min_length=32, max_length=128, repr=False)
    active_node_units: int = Field(ge=0, le=100_000)
    active_request_units: int = Field(ge=0, le=10_000_000)
    denied_count: int = Field(ge=0, le=1_000_000_000)
    bucket_started_at: datetime
    bucket_ended_at: datetime

    @model_validator(mode="after")
    def _valid_bucket(self) -> AgentLicenseAcknowledgementRequest:
        if self.bucket_ended_at <= self.bucket_started_at:
            raise ValueError("edge_license.ack_invalid")
        return self


class AgentLicenseAcknowledgementResponse(_StrictModel):
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    acknowledgement: LicenseAcknowledgementSummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


__all__ = tuple(name for name in globals() if not name.startswith("_"))
