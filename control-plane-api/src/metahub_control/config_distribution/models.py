"""Closed platform and internal DTOs for tenant configuration distribution."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ConfigDistributionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ConfigRebuildRequest(ConfigDistributionModel):
    workspace_id: str = Field(pattern=r"^wsp_[0-9a-f]{32}$")
    reason_code: Literal["operator_rebuild", "consistency_repair"]
    confirmed: Literal[True]


class ConfigRebuildInfo(ConfigDistributionModel):
    rebuild_job_id: str
    workspace_id: str
    target_generation: int = Field(ge=1)
    platform_job_id: str
    status: Literal["queued", "running", "completed", "failed"]
    result_manifest_sha256: str | None
    revision: int = Field(ge=1)
    created_at: datetime
    completed_at: datetime | None


class ConfigRebuildResponse(ConfigDistributionModel):
    rebuild: ConfigRebuildInfo
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class ConfigWatermarkSummary(ConfigDistributionModel):
    workspace_id: str
    cell_code: str
    generation: int = Field(ge=0)
    revocation_watermark: int = Field(ge=0)
    manifest_sha256: str | None
    gateway_count: int = Field(ge=0)
    minimum_applied_generation: int = Field(ge=0)
    minimum_lkg_generation: int = Field(ge=0)
    stale_gateway_count: int = Field(ge=0)
    latest_ack_at: datetime | None
    latest_rebuild: ConfigRebuildInfo | None


class ConfigDistributionResponse(ConfigDistributionModel):
    items: tuple[ConfigWatermarkSummary, ...]
    total: int = Field(ge=0)
    request_id: str
    trace_id: str


class ConfigAckRequest(ConfigDistributionModel):
    generation: int = Field(ge=1)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["applied", "failed"]


class ConfigAckResponse(ConfigDistributionModel):
    ack_id: str
    disposition: Literal["accepted", "late"]
    observed_generation: int = Field(ge=0)
    applied_generation: int = Field(ge=0)
    lkg_generation: int = Field(ge=0)
    idempotent_replay: bool = False
    request_id: str
    trace_id: str
