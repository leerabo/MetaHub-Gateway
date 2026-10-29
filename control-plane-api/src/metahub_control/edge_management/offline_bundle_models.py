"""Strict PRD-130 offline bundle, transfer, and conflict API models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from metahub_shared.private_gateway import OfflineBundleManifest, SignedOfflineBundle
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class WorkspaceOfflineBundleRequest(_StrictModel):
    manifest: OfflineBundleManifest
    reason_code: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9][a-z0-9_.-]+$")


class WorkspaceOfflineResolutionRequest(_StrictModel):
    resolution: Literal["keep_cloud", "keep_local", "supersede_with_new"]
    manifest: OfflineBundleManifest
    expected_current_generation: int = Field(ge=1, le=2_147_483_647)
    reason_code: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9][a-z0-9_.-]+$")


class PlatformOfflineDecisionRequest(_StrictModel):
    action: Literal["approve", "reject"]
    expected_revision: int = Field(ge=1, le=2_147_483_647)
    reason_code: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9][a-z0-9_.-]+$")


class OfflineBundleRequestSummary(_StrictModel):
    request_id: str = Field(pattern=r"^obr_[0-9a-f]{32}$")
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    operation: Literal["export", "import", "resolve"]
    status: Literal["pending_platform", "approved", "rejected", "expired"]
    reason_code: str
    target_bundle_id: str | None = Field(default=None, pattern=r"^obb_[0-9a-f]{32}$")
    target_conflict_id: str | None = Field(default=None, pattern=r"^ofc_[0-9a-f]{32}$")
    revision: int = Field(ge=1)
    requested_at: datetime
    decided_at: datetime | None = None


class OfflineBundleSummary(_StrictModel):
    bundle_id: str = Field(pattern=r"^obb_[0-9a-f]{32}$")
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    direction: Literal["cloud_to_edge", "edge_to_cloud"]
    producer_generation: int = Field(ge=1)
    domains: tuple[
        Literal[
            "desired_state",
            "license",
            "release",
            "status",
            "usage",
            "audit",
            "validation",
        ],
        ...,
    ]
    entry_count: int = Field(ge=1, le=1024)
    encrypted_size_bytes: int = Field(ge=17, le=10_737_418_240)
    transfer_status: Literal[
        "prepared", "transferring", "verified", "applied", "conflict", "failed"
    ]
    next_sequence: int = Field(ge=0)
    chunk_count: int = Field(ge=1)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def _consistent(self) -> OfflineBundleSummary:
        if len(set(self.domains)) != len(self.domains) or self.next_sequence > self.chunk_count:
            raise ValueError("edge_offline_bundle.response_invalid")
        return self


class OfflineConflictSummary(_StrictModel):
    conflict_id: str = Field(pattern=r"^ofc_[0-9a-f]{32}$")
    bundle_id: str = Field(pattern=r"^obb_[0-9a-f]{32}$")
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    domain: Literal["desired_state", "license", "release", "status", "usage", "audit", "validation"]
    base_generation: int = Field(ge=0)
    base_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    cloud_generation: int = Field(ge=1)
    cloud_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    local_generation: int = Field(ge=1)
    local_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resolution: Literal["keep_cloud", "keep_local", "supersede_with_new"] | None = None
    new_generation: int | None = Field(default=None, ge=1)
    detected_at: datetime
    resolved_at: datetime | None = None


class OfflineBundleStateResponse(_StrictModel):
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    asynchronous_management: Literal[True] = True
    requests: tuple[OfflineBundleRequestSummary, ...]
    bundles: tuple[OfflineBundleSummary, ...]
    conflicts: tuple[OfflineConflictSummary, ...]
    request_id: str
    trace_id: str
    idempotent_replay: bool = False


class PlatformOfflineBundleItem(_StrictModel):
    workspace_id: str = Field(pattern=r"^wsp_[0-9a-f]{32}$")
    tenant_id: str
    state: OfflineBundleStateResponse


class PlatformOfflineBundleListResponse(_StrictModel):
    items: tuple[PlatformOfflineBundleItem, ...]
    request_id: str
    trace_id: str


class PlatformOfflineDecisionResponse(_StrictModel):
    request: OfflineBundleRequestSummary
    signed_bundle: SignedOfflineBundle | None = Field(default=None, repr=False)
    request_id: str
    trace_id: str
    idempotent_replay: bool = False


__all__ = (
    "OfflineBundleRequestSummary",
    "OfflineBundleStateResponse",
    "OfflineBundleSummary",
    "OfflineConflictSummary",
    "PlatformOfflineBundleItem",
    "PlatformOfflineBundleListResponse",
    "PlatformOfflineDecisionRequest",
    "PlatformOfflineDecisionResponse",
    "WorkspaceOfflineBundleRequest",
    "WorkspaceOfflineResolutionRequest",
)
