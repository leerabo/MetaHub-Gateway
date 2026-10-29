"""Signed, bounded, zero-sensitive Edge diagnostic contracts for PRD-126."""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

import rfc8785
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DIAGNOSTIC_COMMAND_SCHEMA_VERSION = "metahub.edge-diagnostic-command/v1"
DIAGNOSTIC_RESULT_SCHEMA_VERSION = "metahub.edge-diagnostic-result/v1"
DIAGNOSTIC_SIGNATURE_PURPOSE = "metahub.edge-diagnostic.command/v1"
DIAGNOSTIC_COMMAND_CODES = (
    "runtime.version",
    "runtime.watermarks",
    "runtime.disk_budget",
    "runtime.connectivity_summary",
)
MAX_DIAGNOSTIC_MILLISECONDS = 120_000
MAX_DIAGNOSTIC_RESULT_BYTES = 65_536

type DiagnosticCommandCode = Literal[
    "runtime.version",
    "runtime.watermarks",
    "runtime.disk_budget",
    "runtime.connectivity_summary",
]
type DiagnosticResultStatus = Literal[
    "completed", "failed", "cancelled", "timed_out"
]

_UTC_TIME = re.compile(
    r"^20[0-9]{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])T"
    r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\.[0-9]{3}Z$"
)
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_.-]{1,79}$")
_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_FORBIDDEN_KEY = re.compile(
    r"(?:authorization|cookie|credential|secret|token|password|passphrase|"
    r"private[_-]?key|access[_-]?key|api[_-]?key|client[_-]?secret|prompt|"
    r"response|request[_-]?body|stdout|stderr|stack|traceback|endpoint|url|path)",
    re.IGNORECASE,
)
_CLOCK_SKEW = timedelta(minutes=2)
_MAX_COMMAND_TTL = timedelta(minutes=5)


class DiagnosticContractError(ValueError):
    """Stable diagnostic contract failure that never includes submitted data."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class RuntimeVersionInvocation(_StrictModel):
    command_code: Literal["runtime.version"]


class RuntimeWatermarksInvocation(_StrictModel):
    command_code: Literal["runtime.watermarks"]
    watermark_codes: tuple[
        Literal[
            "desired_generation",
            "applied_generation",
            "lkg_generation",
            "telemetry_health_sequence",
            "telemetry_failure_sequence",
        ],
        ...,
    ] = Field(min_length=1, max_length=5)

    @field_validator("watermark_codes", mode="before")
    @classmethod
    def _json_codes_are_immutable(cls, value: object) -> object:
        return tuple(value) if type(value) is list else value

    @field_validator("watermark_codes")
    @classmethod
    def _ordered_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        expected = (
            "desired_generation",
            "applied_generation",
            "lkg_generation",
            "telemetry_health_sequence",
            "telemetry_failure_sequence",
        )
        if len(set(value)) != len(value) or tuple(sorted(value, key=expected.index)) != value:
            raise ValueError("edge_diagnostic.parameters_invalid")
        return value


class RuntimeDiskBudgetInvocation(_StrictModel):
    command_code: Literal["runtime.disk_budget"]
    volume_code: Literal["runtime", "state", "support_bundle"]


class RuntimeConnectivityInvocation(_StrictModel):
    command_code: Literal["runtime.connectivity_summary"]
    target_code: Literal["control_plane", "provider_pool"]


type DiagnosticInvocation = Annotated[
    RuntimeVersionInvocation
    | RuntimeWatermarksInvocation
    | RuntimeDiskBudgetInvocation
    | RuntimeConnectivityInvocation,
    Field(discriminator="command_code"),
]


class RuntimeVersionResult(_StrictModel):
    command_code: Literal["runtime.version"]
    agent_version: str = Field(pattern=_SEMVER.pattern)
    gateway_schema_version: int = Field(ge=1, le=9_999)
    os_family: Literal["linux", "windows"]
    architecture: Literal["x86_64", "aarch64"]


class RuntimeWatermarksResult(_StrictModel):
    command_code: Literal["runtime.watermarks"]
    desired_generation: int | None = Field(default=None, ge=0)
    applied_generation: int | None = Field(default=None, ge=0)
    lkg_generation: int | None = Field(default=None, ge=0)
    telemetry_health_sequence: int | None = Field(default=None, ge=0)
    telemetry_failure_sequence: int | None = Field(default=None, ge=0)


class RuntimeDiskBudgetResult(_StrictModel):
    command_code: Literal["runtime.disk_budget"]
    volume_code: Literal["runtime", "state", "support_bundle"]
    capacity_bucket: Literal["small", "medium", "large", "very_large"]
    available_bucket: Literal["critical", "low", "medium", "high"]
    pressure_code: Literal["none", "warning", "critical"]


class RuntimeConnectivityResult(_StrictModel):
    command_code: Literal["runtime.connectivity_summary"]
    target_code: Literal["control_plane", "provider_pool"]
    status_code: Literal["reachable", "degraded", "unreachable"]
    latency_bucket: Literal["unknown", "fast", "normal", "slow", "timeout"]
    checked_target_count: int = Field(ge=1, le=64)
    reachable_target_count: int = Field(ge=0, le=64)

    @model_validator(mode="after")
    def _counts_match_status(self) -> RuntimeConnectivityResult:
        if self.reachable_target_count > self.checked_target_count:
            raise ValueError("edge_diagnostic.result_invalid")
        expected = (
            "unreachable"
            if self.reachable_target_count == 0
            else (
                "reachable"
                if self.reachable_target_count == self.checked_target_count
                else "degraded"
            )
        )
        if self.status_code != expected:
            raise ValueError("edge_diagnostic.result_invalid")
        return self


type DiagnosticResult = Annotated[
    RuntimeVersionResult
    | RuntimeWatermarksResult
    | RuntimeDiskBudgetResult
    | RuntimeConnectivityResult,
    Field(discriminator="command_code"),
]


class DiagnosticGrantContract(_StrictModel):
    grant_id: str = Field(pattern=r"^dgr_[0-9a-f]{32}$")
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    invocation: DiagnosticInvocation
    parameters_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_runs: int = Field(ge=1, le=3)
    deployment_revision: int = Field(ge=1)
    revocation_generation: int = Field(ge=1)
    workspace_authority_revision: int = Field(ge=1)
    platform_authority_revision: int = Field(ge=1)
    approved_at: str = Field(pattern=_UTC_TIME.pattern)
    expires_at: str = Field(pattern=_UTC_TIME.pattern)

    @model_validator(mode="after")
    def _binding_is_exact(self) -> DiagnosticGrantContract:
        approved = _time(self.approved_at)
        expires = _time(self.expires_at)
        if (
            self.parameters_sha256 != diagnostic_invocation_sha256(self.invocation)
            or expires <= approved
            or expires - approved > _MAX_COMMAND_TTL
        ):
            raise ValueError("edge_diagnostic.grant_invalid")
        return self


class DiagnosticUnsignedCommand(_StrictModel):
    schema_version: Literal["metahub.edge-diagnostic-command/v1"]
    signature_purpose: Literal["metahub.edge-diagnostic.command/v1"]
    command_id: str = Field(pattern=r"^dcm_[0-9a-f]{32}$")
    grant_id: str = Field(pattern=r"^dgr_[0-9a-f]{32}$")
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    generation: int = Field(ge=1)
    revocation_generation: int = Field(ge=1)
    grant_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    invocation: DiagnosticInvocation
    parameters_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    issued_at: str = Field(pattern=_UTC_TIME.pattern)
    not_before: str = Field(pattern=_UTC_TIME.pattern)
    deadline_at: str = Field(pattern=_UTC_TIME.pattern)
    expires_at: str = Field(pattern=_UTC_TIME.pattern)
    maximum_result_bytes: int = Field(ge=1_024, le=MAX_DIAGNOSTIC_RESULT_BYTES)
    algorithm: Literal["Ed25519"]
    kid: str = Field(pattern=r"^edge-ds-key-v[1-9][0-9]*$")

    @model_validator(mode="after")
    def _command_is_bounded(self) -> DiagnosticUnsignedCommand:
        issued = _time(self.issued_at)
        not_before = _time(self.not_before)
        deadline = _time(self.deadline_at)
        expires = _time(self.expires_at)
        if (
            self.parameters_sha256 != diagnostic_invocation_sha256(self.invocation)
            or not (issued <= not_before <= deadline <= expires)
            or expires - issued > _MAX_COMMAND_TTL
            or int((deadline - not_before).total_seconds() * 1_000)
            > MAX_DIAGNOSTIC_MILLISECONDS
        ):
            raise ValueError("edge_diagnostic.command_invalid")
        return self


class DiagnosticCommandEnvelope(DiagnosticUnsignedCommand):
    signature: str = Field(min_length=86, max_length=86, repr=False)


class DiagnosticExecutionReport(_StrictModel):
    schema_version: Literal["metahub.edge-diagnostic-result/v1"]
    command_id: str = Field(pattern=r"^dcm_[0-9a-f]{32}$")
    grant_id: str = Field(pattern=r"^dgr_[0-9a-f]{32}$")
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    generation: int = Field(ge=1)
    status: DiagnosticResultStatus
    reason_code: Literal[
        "completed",
        "cancelled",
        "deadline_exceeded",
        "handler_failed",
        "result_invalid",
        "sensitive_output",
    ]
    result: DiagnosticResult | None = Field(default=None, repr=False)
    result_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    started_at: str = Field(pattern=_UTC_TIME.pattern)
    completed_at: str = Field(pattern=_UTC_TIME.pattern)
    duration_ms: int = Field(ge=0, le=MAX_DIAGNOSTIC_MILLISECONDS)

    @model_validator(mode="after")
    def _result_matches_status(self) -> DiagnosticExecutionReport:
        started = _time(self.started_at)
        completed = _time(self.completed_at)
        elapsed = int((completed - started).total_seconds() * 1_000)
        completed_successfully = self.status == "completed"
        if (
            completed < started
            or abs(elapsed - self.duration_ms) > 1
            or completed_successfully != (self.reason_code == "completed")
            or completed_successfully != (self.result is not None)
            or completed_successfully != (self.result_sha256 is not None)
        ):
            raise ValueError("edge_diagnostic.report_invalid")
        if self.result is not None:
            if self.result.command_code not in DIAGNOSTIC_COMMAND_CODES:
                raise ValueError("edge_diagnostic.report_invalid")
            if self.result_sha256 != diagnostic_result_sha256(self.result):
                raise ValueError("edge_diagnostic.report_invalid")
        return self


@dataclass(frozen=True, slots=True, repr=False)
class VerifiedDiagnosticCommand:
    command: DiagnosticCommandEnvelope
    canonical_envelope: bytes
    envelope_sha256: str


def canonical_diagnostic_bytes(
    value: object, *, maximum: int = MAX_DIAGNOSTIC_RESULT_BYTES
) -> bytes:
    """RFC 8785 encoding with a fixed ceiling and no value-bearing error."""

    try:
        encoded = rfc8785.dumps(value)
    except (rfc8785.CanonicalizationError, TypeError, ValueError, UnicodeError):
        raise DiagnosticContractError("edge_diagnostic.payload_invalid") from None
    if not 0 < len(encoded) <= maximum:
        raise DiagnosticContractError("edge_diagnostic.payload_invalid")
    return encoded


def reject_forbidden_diagnostic_fields(value: object) -> None:
    """Reject credential, free-text, filesystem, and raw transport fields recursively."""

    stack = [value]
    visited = 0
    while stack:
        current = stack.pop()
        visited += 1
        if visited > 2_048:
            raise DiagnosticContractError("edge_diagnostic.payload_invalid")
        if type(current) is dict:
            for key, item in current.items():
                if type(key) is not str or _FORBIDDEN_KEY.search(key):
                    raise DiagnosticContractError("edge_diagnostic.forbidden_field")
                stack.append(item)
        elif type(current) in {list, tuple}:
            stack.extend(current)
        elif type(current) not in {str, int, bool, type(None)}:
            raise DiagnosticContractError("edge_diagnostic.payload_invalid")


def diagnostic_invocation_sha256(invocation: DiagnosticInvocation) -> str:
    payload = invocation.model_dump(mode="json")
    reject_forbidden_diagnostic_fields(payload)
    return hashlib.sha256(canonical_diagnostic_bytes(payload)).hexdigest()


def diagnostic_grant_sha256(grant: DiagnosticGrantContract) -> str:
    payload = grant.model_dump(mode="json")
    reject_forbidden_diagnostic_fields(payload)
    return hashlib.sha256(canonical_diagnostic_bytes(payload)).hexdigest()


def diagnostic_result_sha256(result: DiagnosticResult) -> str:
    payload = result.model_dump(mode="json")
    reject_forbidden_diagnostic_fields(payload)
    return hashlib.sha256(canonical_diagnostic_bytes(payload)).hexdigest()


def sign_diagnostic_command(
    unsigned: DiagnosticUnsignedCommand | Mapping[str, object],
    private_key: Ed25519PrivateKey,
) -> DiagnosticCommandEnvelope:
    try:
        command = (
            unsigned
            if isinstance(unsigned, DiagnosticUnsignedCommand)
            else DiagnosticUnsignedCommand.model_validate(unsigned)
        )
        payload = command.model_dump(mode="json")
        reject_forbidden_diagnostic_fields(payload)
        signature = _b64(private_key.sign(canonical_diagnostic_bytes(payload)))
        return DiagnosticCommandEnvelope.model_validate({**payload, "signature": signature})
    except DiagnosticContractError:
        raise
    except (TypeError, ValueError):
        raise DiagnosticContractError("edge_diagnostic.command_invalid") from None


def verify_diagnostic_command(
    value: object,
    *,
    trusted_keys: Mapping[str, bytes],
    expected_deployment_id: str,
    minimum_generation: int,
    revocation_generation: int,
    now: datetime,
) -> VerifiedDiagnosticCommand:
    try:
        command = DiagnosticCommandEnvelope.model_validate(value)
        payload = command.model_dump(mode="json")
        reject_forbidden_diagnostic_fields(payload)
        key = trusted_keys.get(command.kid)
        if (
            key is None
            or command.deployment_id != expected_deployment_id
            or command.generation < minimum_generation
            or command.revocation_generation != revocation_generation
        ):
            raise DiagnosticContractError("edge_diagnostic.command_untrusted")
        checked_now = _aware(now)
        if checked_now + _CLOCK_SKEW < _time(command.not_before):
            raise DiagnosticContractError("edge_diagnostic.command_not_active")
        if checked_now >= _time(command.expires_at):
            raise DiagnosticContractError("edge_diagnostic.command_expired")
        signature = _unb64(command.signature, expected=64)
        unsigned = {key: item for key, item in payload.items() if key != "signature"}
        Ed25519PublicKey.from_public_bytes(key).verify(
            signature, canonical_diagnostic_bytes(unsigned)
        )
        canonical = canonical_diagnostic_bytes(payload)
        return VerifiedDiagnosticCommand(
            command=command,
            canonical_envelope=canonical,
            envelope_sha256=hashlib.sha256(canonical).hexdigest(),
        )
    except DiagnosticContractError:
        raise
    except (InvalidSignature, TypeError, ValueError):
        raise DiagnosticContractError("edge_diagnostic.command_untrusted") from None


def _time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValueError("edge_diagnostic.time_invalid") from None
    if (
        parsed.tzinfo is None
        or parsed.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        != value
    ):
        raise ValueError("edge_diagnostic.time_invalid")
    return parsed.astimezone(UTC)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise DiagnosticContractError("edge_diagnostic.time_invalid")
    return value.astimezone(UTC)


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str, *, expected: int) -> bytes:
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, TypeError):
        raise DiagnosticContractError("edge_diagnostic.command_untrusted") from None
    if len(decoded) != expected or _b64(decoded) != value:
        raise DiagnosticContractError("edge_diagnostic.command_untrusted")
    return decoded


__all__ = (
    "DIAGNOSTIC_COMMAND_CODES",
    "DIAGNOSTIC_COMMAND_SCHEMA_VERSION",
    "DIAGNOSTIC_RESULT_SCHEMA_VERSION",
    "DIAGNOSTIC_SIGNATURE_PURPOSE",
    "MAX_DIAGNOSTIC_MILLISECONDS",
    "MAX_DIAGNOSTIC_RESULT_BYTES",
    "DiagnosticCommandCode",
    "DiagnosticCommandEnvelope",
    "DiagnosticContractError",
    "DiagnosticExecutionReport",
    "DiagnosticGrantContract",
    "DiagnosticInvocation",
    "DiagnosticResult",
    "DiagnosticResultStatus",
    "DiagnosticUnsignedCommand",
    "RuntimeConnectivityInvocation",
    "RuntimeConnectivityResult",
    "RuntimeDiskBudgetInvocation",
    "RuntimeDiskBudgetResult",
    "RuntimeVersionInvocation",
    "RuntimeVersionResult",
    "RuntimeWatermarksInvocation",
    "RuntimeWatermarksResult",
    "VerifiedDiagnosticCommand",
    "canonical_diagnostic_bytes",
    "diagnostic_grant_sha256",
    "diagnostic_invocation_sha256",
    "diagnostic_result_sha256",
    "reject_forbidden_diagnostic_fields",
    "sign_diagnostic_command",
    "verify_diagnostic_command",
)
