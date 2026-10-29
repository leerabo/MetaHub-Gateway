"""Strict zero-sensitive Edge telemetry wire contracts for PRD-122."""

from __future__ import annotations

import hashlib
import re
from typing import Annotated, Literal

import rfc8785
from pydantic import BaseModel, ConfigDict, Field, model_validator

type TelemetryLevel = Literal["minimal", "operational", "billing"]
type TelemetryStream = Literal["health", "operational", "usage", "failure"]

_UTC_TIME = re.compile(
    r"^20[0-9]{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])T"
    r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]{3})?Z$"
)
_DEPLOYMENT_ID = re.compile(r"^edp_[0-9a-f]{32}$")
_EPOCH_ID = re.compile(r"^ete_[0-9a-f]{32}$")
_REFERENCE_ID = re.compile(r"^[a-z][a-z0-9_:-]{2,79}$")
_MAX_CANONICAL_BYTES = 1_048_576
_FORBIDDEN_KEYS = frozenset(
    {
        "api_key",
        "attributes",
        "authorization",
        "body",
        "cookie",
        "endpoint",
        "file_path",
        "headers",
        "message",
        "path",
        "prompt",
        "provider_key",
        "request",
        "response",
        "secret",
        "stack",
        "stack_trace",
        "tags",
        "token",
        "url",
    }
)
_STREAM_KIND = {
    "health": "health",
    "operational": "operational",
    "usage": "usage",
    "failure": "failure",
}
_LEVEL_KINDS = {
    "minimal": frozenset({"health"}),
    "operational": frozenset({"health", "operational", "failure"}),
    "billing": frozenset({"health", "operational", "failure", "usage"}),
}


class EdgeTelemetryError(ValueError):
    """Fixed-code failure that cannot echo telemetry or credential material."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EdgeHealthTelemetry(_StrictModel):
    kind: Literal["health"]
    bucket_start: str = Field(pattern=_UTC_TIME.pattern)
    bucket_end: str = Field(pattern=_UTC_TIME.pattern)
    component_code: Literal["gateway", "config_sync", "telemetry_sync"]
    heartbeat_count: int = Field(ge=1, le=86_400)
    uptime_seconds: int = Field(ge=0, le=31_536_000)
    cpu_millis: int = Field(ge=0, le=86_400_000)
    memory_peak_bytes: int = Field(ge=0, le=1_099_511_627_776)


class EdgeOperationalTelemetry(_StrictModel):
    kind: Literal["operational"]
    bucket_start: str = Field(pattern=_UTC_TIME.pattern)
    bucket_end: str = Field(pattern=_UTC_TIME.pattern)
    component_code: Literal["gateway", "config_sync", "provider_pool", "usage_spool"]
    request_count: int = Field(ge=0, le=1_000_000_000)
    success_count: int = Field(ge=0, le=1_000_000_000)
    failure_count: int = Field(ge=0, le=1_000_000_000)
    latency_sum_ms: int = Field(ge=0, le=86_400_000_000)
    queue_depth_max: int = Field(ge=0, le=10_000_000)

    @model_validator(mode="after")
    def _counts_are_consistent(self) -> EdgeOperationalTelemetry:
        if self.success_count + self.failure_count != self.request_count:
            raise ValueError("edge_telemetry.count_invalid")
        return self


class EdgeUsageTelemetry(_StrictModel):
    kind: Literal["usage"]
    bucket_start: str = Field(pattern=_UTC_TIME.pattern)
    bucket_end: str = Field(pattern=_UTC_TIME.pattern)
    model_id: str = Field(min_length=3, max_length=80)
    provider_id: str = Field(min_length=3, max_length=80)
    request_count: int = Field(ge=1, le=1_000_000_000)
    input_tokens: int = Field(ge=0, le=9_000_000_000_000_000)
    output_tokens: int = Field(ge=0, le=9_000_000_000_000_000)
    cache_tokens: int = Field(ge=0, le=9_000_000_000_000_000)
    cost_cny_micros: int = Field(ge=0, le=9_000_000_000_000_000)
    cost_usd_micros: int = Field(ge=0, le=9_000_000_000_000_000)

    @model_validator(mode="after")
    def _references_are_fixed_ids(self) -> EdgeUsageTelemetry:
        if _REFERENCE_ID.fullmatch(self.model_id) is None:
            raise ValueError("edge_telemetry.reference_invalid")
        if _REFERENCE_ID.fullmatch(self.provider_id) is None:
            raise ValueError("edge_telemetry.reference_invalid")
        return self


class EdgeFailureTelemetry(_StrictModel):
    kind: Literal["failure"]
    bucket_start: str = Field(pattern=_UTC_TIME.pattern)
    bucket_end: str = Field(pattern=_UTC_TIME.pattern)
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
    count: int = Field(ge=1, le=1_000_000_000)


type EdgeTelemetryRecord = Annotated[
    EdgeHealthTelemetry | EdgeOperationalTelemetry | EdgeUsageTelemetry | EdgeFailureTelemetry,
    Field(discriminator="kind"),
]


class EdgeTelemetryBatch(_StrictModel):
    schema_version: Literal["1"]
    deployment_id: str = Field(pattern=_DEPLOYMENT_ID.pattern)
    stream: TelemetryStream
    epoch_id: str = Field(pattern=_EPOCH_ID.pattern)
    previous_epoch_id: str | None = Field(default=None, pattern=_EPOCH_ID.pattern)
    sequence: int = Field(ge=1, le=9_223_372_036_854_775_807)
    policy_revision: int = Field(ge=1, le=2_147_483_647)
    sent_at: str = Field(pattern=_UTC_TIME.pattern)
    records: list[EdgeTelemetryRecord] = Field(min_length=1, max_length=256, repr=False)

    @model_validator(mode="after")
    def _stream_is_homogeneous(self) -> EdgeTelemetryBatch:
        expected = _STREAM_KIND[self.stream]
        if any(record.kind != expected for record in self.records):
            raise ValueError("edge_telemetry.stream_invalid")
        if self.previous_epoch_id == self.epoch_id:
            raise ValueError("edge_telemetry.epoch_invalid")
        return self


class EdgeTelemetryReceiptWire(_StrictModel):
    receipt_id: str = Field(pattern=r"^etr_[0-9a-f]{32}$")
    batch_id: str = Field(pattern=r"^etb_[0-9a-f]{32}$")
    deployment_id: str = Field(pattern=_DEPLOYMENT_ID.pattern)
    stream: TelemetryStream
    epoch_id: str = Field(pattern=_EPOCH_ID.pattern)
    sequence: int = Field(ge=1)
    disposition: Literal["accepted", "duplicate"]
    contiguous_sequence: int = Field(ge=0)
    expected_sequence: int = Field(ge=1)
    highest_seen_sequence: int = Field(ge=1)
    policy_level: TelemetryLevel
    policy_revision: int = Field(ge=1)
    received_at: str = Field(pattern=_UTC_TIME.pattern)
    request_id: str = Field(min_length=1, max_length=160)
    trace_id: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def _cursor_is_contiguous(self) -> EdgeTelemetryReceiptWire:
        if self.expected_sequence != self.contiguous_sequence + 1:
            raise ValueError("edge_telemetry.receipt_invalid")
        if self.highest_seen_sequence < max(self.contiguous_sequence, self.sequence):
            raise ValueError("edge_telemetry.receipt_invalid")
        return self


def reject_forbidden_telemetry_fields(value: object) -> None:
    """Reject credential, body, endpoint, and free-form diagnostic field names recursively."""

    stack = [value]
    visited = 0
    while stack:
        current = stack.pop()
        visited += 1
        if visited > 4096:
            raise EdgeTelemetryError("edge_telemetry.payload_invalid")
        if type(current) is dict:
            for key, item in current.items():
                if type(key) is not str or key.lower() in _FORBIDDEN_KEYS:
                    raise EdgeTelemetryError("edge_telemetry.forbidden_field")
                stack.append(item)
        elif type(current) is list:
            stack.extend(current)
        elif type(current) not in {str, int, bool, type(None)}:
            raise EdgeTelemetryError("edge_telemetry.payload_invalid")


def validate_telemetry_batch(batch: EdgeTelemetryBatch, policy_level: TelemetryLevel) -> None:
    """Enforce collection-time policy and bounded canonical bucket ordering."""

    allowed = _LEVEL_KINDS[policy_level]
    if any(record.kind not in allowed for record in batch.records):
        raise EdgeTelemetryError("edge_telemetry.policy_violation")
    previous_end: str | None = None
    for record in batch.records:
        if record.bucket_start >= record.bucket_end:
            raise EdgeTelemetryError("edge_telemetry.bucket_invalid")
        if previous_end is not None and record.bucket_start < previous_end:
            raise EdgeTelemetryError("edge_telemetry.bucket_invalid")
        previous_end = record.bucket_end


def canonical_telemetry_bytes(
    value: object, *, maximum: int = _MAX_CANONICAL_BYTES
) -> bytes:
    """Encode one RFC 8785 telemetry value without leaking invalid input in errors."""

    try:
        encoded = rfc8785.dumps(value)
    except (rfc8785.CanonicalizationError, TypeError, ValueError, UnicodeError):
        raise EdgeTelemetryError("edge_telemetry.payload_invalid") from None
    if not 0 < len(encoded) <= maximum:
        raise EdgeTelemetryError("edge_telemetry.payload_invalid")
    return encoded


def telemetry_batch_sha256(batch: EdgeTelemetryBatch) -> str:
    return hashlib.sha256(
        canonical_telemetry_bytes(batch.model_dump(mode="json"))
    ).hexdigest()


__all__ = (
    "EdgeFailureTelemetry",
    "EdgeHealthTelemetry",
    "EdgeOperationalTelemetry",
    "EdgeTelemetryBatch",
    "EdgeTelemetryError",
    "EdgeTelemetryReceiptWire",
    "EdgeUsageTelemetry",
    "TelemetryLevel",
    "TelemetryStream",
    "canonical_telemetry_bytes",
    "reject_forbidden_telemetry_fields",
    "telemetry_batch_sha256",
    "validate_telemetry_batch",
)
