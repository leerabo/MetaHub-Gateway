"""Signed, closed wire contracts shared by Control and the isolated runner."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_MAX_CLOCK_SKEW = timedelta(seconds=30)


class RunnerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CapacityPermitClaims(RunnerModel):
    permit_id: str = Field(pattern=r"^crp_[0-9a-f]{32}$")
    operation: Literal["load_test", "chaos"]
    object_id: str = Field(pattern=r"^(?:ltr|che)_[0-9a-f]{32}$")
    service_code: Literal["gateway", "control", "status", "shared"]
    scenario_code: str | None = Field(default=None, min_length=3, max_length=64)
    fault_code: str | None = Field(default=None, min_length=3, max_length=64)
    environment_class: Literal["dev", "staging", "production"]
    region_code: str = Field(min_length=3, max_length=40, pattern=r"^[a-z0-9][a-z0-9-]+$")
    synthetic_tenant_count: int = Field(ge=1, le=500)
    target_rps: int = Field(ge=0, le=100_000)
    duration_seconds: int = Field(ge=10, le=259_200)
    objective_p95_ms: int = Field(ge=0, le=60_000)
    objective_error_rate_bps: int = Field(ge=0, le=10_000)
    stop_error_rate_bps: int = Field(ge=0, le=10_000)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kill_generation: int = Field(ge=0)
    issued_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def validate_operation(self) -> CapacityPermitClaims:
        if (
            self.issued_at.utcoffset() is None
            or self.expires_at.utcoffset() is None
            or self.expires_at <= self.issued_at
            or self.expires_at - self.issued_at > timedelta(hours=1)
        ):
            raise ValueError("permit expiry is invalid")
        if self.operation == "load_test":
            if (
                not self.object_id.startswith("ltr_")
                or self.scenario_code is None
                or self.fault_code is not None
                or self.target_rps < 1
            ):
                raise ValueError("load permit is invalid")
        elif (
            not self.object_id.startswith("che_")
            or self.fault_code is None
            or self.scenario_code is not None
            or self.target_rps != 0
        ):
            raise ValueError("chaos permit is invalid")
        return self


class CapacityRunnerRequest(RunnerModel):
    permit: str = Field(min_length=80, max_length=8192)


class ChaosExecutionEvidence(RunnerModel):
    baseline_sample_count: int = Field(ge=1)
    fault_sample_count: int = Field(ge=1)
    recovery_sample_count: int = Field(ge=1)
    reset_connections: int = Field(ge=0)
    withheld_bytes: int = Field(ge=0)
    injected_duration_ms: int = Field(ge=1)
    recovery_duration_ms: int = Field(ge=0)
    fault_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_configuration_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fault_state: Literal["released", "expired"]
    requests_stopped: Literal[True]
    fault_reversed: Literal[True]
    target_recovered: Literal[True]

    @model_validator(mode="after")
    def validate_observation(self):
        if self.reset_connections + self.withheld_bytes < 1:
            raise ValueError("chaos fault was not observed")
        return self


class CapacityRunnerResult(RunnerModel):
    contract_version: Literal[2]
    evidence_kind: Literal["measured", "simulation"]
    execution_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation: Literal["load_test", "chaos"]
    object_id: str = Field(pattern=r"^(?:ltr|che)_[0-9a-f]{32}$")
    safe_outcome_code: str = Field(min_length=3, max_length=96)
    sample_count: int | None = Field(default=None, ge=0)
    p50_ms: int | None = Field(default=None, ge=0)
    p95_ms: int | None = Field(default=None, ge=0)
    p99_ms: int | None = Field(default=None, ge=0)
    throughput_rps_milli: int | None = Field(default=None, ge=0)
    error_rate_bps: int | None = Field(default=None, ge=0, le=10_000)
    cpu_peak_bps: int | None = Field(default=None, ge=0, le=10_000)
    memory_peak_mib: int | None = Field(default=None, ge=0)
    open_connection_delta: int | None = Field(default=None, ge=0)
    handle_delta: int | None = Field(default=None, ge=0)
    fairness_jain_millionths: int | None = Field(default=None, ge=0, le=1_000_000)
    controlled_degradation: bool | None = None
    observed_p95_ms: int | None = Field(default=None, ge=0)
    observed_error_rate_bps: int | None = Field(default=None, ge=0, le=10_000)
    objective_met: bool | None = None
    chaos_evidence: ChaosExecutionEvidence | None = None
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_result(self) -> CapacityRunnerResult:
        load_fields = (
            self.sample_count,
            self.p50_ms,
            self.p95_ms,
            self.p99_ms,
            self.throughput_rps_milli,
            self.error_rate_bps,
            self.cpu_peak_bps,
            self.memory_peak_mib,
            self.open_connection_delta,
            self.handle_delta,
            self.fairness_jain_millionths,
            self.controlled_degradation,
        )
        chaos_fields = (
            self.observed_p95_ms,
            self.observed_error_rate_bps,
            self.objective_met,
        )
        if self.operation == "load_test" and (
            any(value is None for value in load_fields)
            or any(value is not None for value in chaos_fields)
        ):
            raise ValueError("load result is invalid")
        if self.operation == "chaos" and (
            any(value is None for value in chaos_fields)
            or any(value is not None for value in load_fields)
        ):
            raise ValueError("chaos result is invalid")
        if self.operation == "load_test" and (
            not self.object_id.startswith("ltr_")
            or self.safe_outcome_code != "runner.load_completed"
            or self.sample_count < 1
            or not self.p50_ms <= self.p95_ms <= self.p99_ms
        ):
            raise ValueError("load result is inconsistent")
        if self.operation == "chaos" and (
            not self.object_id.startswith("che_")
            or self.safe_outcome_code
            != (
                "runner.chaos_objective_met"
                if self.objective_met
                else "runner.chaos_objective_missed"
            )
        ):
            raise ValueError("chaos result is inconsistent")
        if self.operation == "load_test" and self.chaos_evidence is not None:
            raise ValueError("load result includes chaos evidence")
        if (
            self.operation == "chaos"
            and self.evidence_kind == "measured"
            and self.chaos_evidence is None
        ):
            raise ValueError("measured chaos evidence is missing")
        if not hmac.compare_digest(
            self.result_sha256, result_digest(self.model_dump(mode="json", exclude_none=True))
        ):
            raise ValueError("result digest is invalid")
        return self


class CapacityKillClaims(RunnerModel):
    experiment_id: str = Field(pattern=r"^che_[0-9a-f]{32}$")
    kill_generation: int = Field(ge=1)
    issued_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def validate_expiry(self) -> CapacityKillClaims:
        if (
            self.issued_at.utcoffset() is None
            or self.expires_at.utcoffset() is None
            or self.expires_at <= self.issued_at
            or self.expires_at - self.issued_at > timedelta(seconds=60)
        ):
            raise ValueError("kill permit expiry is invalid")
        return self


class CapacityExecutionInfo(RunnerModel):
    object_id: str = Field(pattern=r"^(?:ltr|che)_[0-9a-f]{32}$")
    execution_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: Literal[
        "queued", "running", "stopping", "succeeded", "failed", "cancelled", "interrupted"
    ]
    safe_outcome_code: str = Field(pattern=r"^runner\.[a-z0-9_]{2,80}$")
    sample_count: int = Field(ge=0)
    elapsed_ms: int = Field(ge=0)
    stop_scope: Literal["runner_requests", "runner_requests_and_fault"] = "runner_requests"
    result: CapacityRunnerResult | None = None

    @model_validator(mode="after")
    def validate_terminal(self):
        if (
            self.object_id.startswith("che_")
            and self.state not in {"queued", "running", "stopping"}
            and self.stop_scope != "runner_requests_and_fault"
        ):
            raise ValueError("chaos fault stop is unconfirmed")
        if (self.state == "succeeded") != (self.result is not None):
            raise ValueError("execution result state is invalid")
        if self.result is not None and (
            self.result.object_id != self.object_id
            or self.result.execution_sha256 != self.execution_sha256
            or self.result.evidence_kind != "measured"
            or (
                self.result.operation == "load_test"
                and self.result.sample_count != self.sample_count
            )
        ):
            raise ValueError("execution result binding is invalid")
        return self


class CapacityResourceSample(RunnerModel):
    process_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sampled_at: datetime
    cpu_time_ms: int = Field(ge=0)
    memory_rss_bytes: int = Field(ge=0)
    open_connections: int = Field(ge=0)
    handle_count: int = Field(ge=0)
    processor_count: int = Field(ge=1, le=4096)


class CapacityKillRequest(RunnerModel):
    permit: str = Field(min_length=80, max_length=4096)


class CapacityKillResponse(RunnerModel):
    accepted: Literal[True]
    experiment_id: str = Field(pattern=r"^che_[0-9a-f]{32}$")
    kill_generation: int = Field(ge=1)


def execution_digest(claims: CapacityPermitClaims) -> str:
    return _digest(
        claims.model_dump(mode="json", exclude={"issued_at", "expires_at"}, exclude_none=True)
    )


def result_digest(value: dict[str, object]) -> str:
    return _digest(
        {key: item for key, item in value.items() if key != "result_sha256" and item is not None}
    )


def _digest(value: object) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def encode_signed(value: BaseModel, secret: str, *, domain: str) -> str:
    payload = json.dumps(
        value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    signature = hmac.new(
        secret.encode("utf-8"), f"prd113/{domain}/v1\0{encoded}".encode(), hashlib.sha256
    ).hexdigest()
    return f"{encoded}.{signature}"


def decode_signed(value: str, secret: str, *, domain: str, model_type):
    parts = value.split(".")
    if len(parts) != 2 or len(parts[1]) != 64:
        raise ValueError("signed value is malformed")
    expected = hmac.new(
        secret.encode("utf-8"), f"prd113/{domain}/v1\0{parts[0]}".encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(parts[1], expected):
        raise ValueError("signed value is invalid")
    payload = base64.urlsafe_b64decode(parts[0] + "=" * (-len(parts[0]) % 4))
    result = model_type.model_validate_json(payload)
    now = datetime.now(UTC)
    if result.issued_at > now + _MAX_CLOCK_SKEW or result.expires_at <= now:
        raise ValueError("signed value has expired")
    return result


__all__ = [
    "CapacityKillClaims",
    "CapacityKillRequest",
    "CapacityKillResponse",
    "CapacityPermitClaims",
    "CapacityRunnerRequest",
    "CapacityRunnerResult",
    "decode_signed",
    "encode_signed",
    "execution_digest",
    "result_digest",
]
