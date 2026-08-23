"""Strict public DTOs for the PRD-106 platform job runtime."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

JobStatus = Literal[
    "queued",
    "leased",
    "running",
    "cancel_requested",
    "succeeded",
    "failed",
    "cancelled",
    "dead_lettered",
]


class PlatformJobModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PlatformJobTypeInfo(PlatformJobModel):
    type_code: str
    contract_version: int = Field(ge=1)
    queue_code: Literal["critical", "standard", "bulk"]
    resource_class: Literal["control", "io", "compute"]
    max_attempts: int = Field(ge=1, le=20)
    timeout_seconds: int = Field(ge=1, le=86400)
    status: Literal["active", "disabled"]
    revision: int = Field(ge=1)


class PlatformJobSummary(PlatformJobModel):
    job_id: str
    type_code: str
    queue_code: Literal["critical", "standard", "bulk"]
    priority: int = Field(ge=0, le=100)
    status: JobStatus
    revision: int = Field(ge=1)
    generation: int = Field(ge=0)
    attempt_count: int = Field(ge=0, le=20)
    max_attempts: int = Field(ge=1, le=20)
    safe_outcome_code: str | None = None
    scheduled_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class PlatformJobAttemptInfo(PlatformJobModel):
    attempt_id: str
    attempt_number: int = Field(ge=1, le=20)
    generation: int = Field(ge=1)
    status: Literal["leased", "running", "succeeded", "failed", "cancelled", "stale"]
    safe_outcome_code: str | None = None
    leased_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class PlatformJobCheckpointInfo(PlatformJobModel):
    checkpoint_id: str
    generation: int = Field(ge=1)
    sequence_number: int = Field(ge=1)
    checkpoint_sha256: str
    created_at: datetime


class PlatformWorkQueueInfo(PlatformJobModel):
    queue_code: Literal["critical", "standard", "bulk"]
    priority_weight: int = Field(ge=1, le=100)
    max_concurrency: int = Field(ge=1, le=1000)
    queued_count: int = Field(ge=0)
    active_lease_count: int = Field(ge=0)
    dead_letter_count: int = Field(ge=0)
    oldest_wait_seconds: int | None = Field(default=None, ge=0)


class PlatformJobQueueResponse(PlatformJobModel):
    queues: tuple[PlatformWorkQueueInfo, ...]
    recent_jobs: tuple[PlatformJobSummary, ...]
    job_types: tuple[PlatformJobTypeInfo, ...]
    request_id: str
    trace_id: str


class PlatformJobDetailResponse(PlatformJobModel):
    job: PlatformJobSummary
    attempts: tuple[PlatformJobAttemptInfo, ...]
    checkpoints: tuple[PlatformJobCheckpointInfo, ...]
    request_id: str
    trace_id: str


class PlatformJobActionRequest(PlatformJobModel):
    reason_code: Literal[
        "operator_retry",
        "incident_recovery",
        "operator_cancelled",
        "security_containment",
    ]
    confirmed: Literal[True]


class PlatformJobActionResponse(PlatformJobModel):
    job: PlatformJobSummary
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


class PlatformJobTypeCommandRequest(PlatformJobModel):
    type_code: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9_.-]+$")
    action: Literal["activate", "disable"]
    reason_code: Literal["release_activation", "operational_pause", "security_containment"]
    confirmed: Literal[True]


class PlatformJobTypeResponse(PlatformJobModel):
    job_type: PlatformJobTypeInfo
    idempotent_replay: bool = False
    request_id: str
    trace_id: str


__all__ = [
    "PlatformJobActionRequest",
    "PlatformJobActionResponse",
    "PlatformJobAttemptInfo",
    "PlatformJobCheckpointInfo",
    "PlatformJobDetailResponse",
    "PlatformJobQueueResponse",
    "PlatformJobSummary",
    "PlatformJobTypeCommandRequest",
    "PlatformJobTypeInfo",
    "PlatformJobTypeResponse",
    "PlatformWorkQueueInfo",
]
