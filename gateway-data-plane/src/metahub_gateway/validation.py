"""Fail-closed execution of the compiled PRD-125 delivery-validation plan."""

from __future__ import annotations

import hashlib
import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Literal, Protocol

import rfc8785
from metahub_shared.private_gateway import (
    MAX_VALIDATION_MILLISECONDS,
    VALIDATION_CHECK_CODES,
    VALIDATION_PLAN_VERSION,
    VALIDATION_SCHEMA_VERSION,
    DeploymentValidationReport,
    ValidationCheckEvidence,
)

_DEPLOYMENT_ID = re.compile(r"^edp_[0-9a-f]{32}$")
_VALIDATION_ID = re.compile(r"^dvr_[0-9a-f]{32}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_.-]{1,79}$")
_SAFE_BUCKET = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")

type EvidenceValue = bool | int | str
type ProbeOutcome = Literal["passed", "failed"]

_EVIDENCE_SCHEMA: dict[str, dict[str, type]] = {
    "device_certificate": {
        "certificate_valid": bool,
        "identity_generation": int,
        "certificate_generation": int,
    },
    "signed_config_lkg": {
        "signature_verified": bool,
        "desired_generation": int,
        "applied_generation": int,
        "lkg_generation": int,
    },
    "provider_connectivity": {
        "configured_count": int,
        "reachable_count": int,
    },
    "minimal_gateway_request": {
        "probe_sha256": str,
        "gateway_outcome": str,
    },
    "usage_cost_failure_sync": {
        "probe_sha256": str,
        "usage_recorded": bool,
        "cost_recorded": bool,
        "failure_sync_verified": bool,
    },
    "backup_readiness": {
        "ready": bool,
        "age_bucket": str,
    },
    "capacity_readiness": {
        "cpu_bucket": str,
        "memory_bucket": str,
        "disk_bucket": str,
    },
    "performance_baseline": {
        "latency_bucket": str,
        "error_rate_bucket": str,
        "sample_count": int,
    },
}


class EdgeValidationRunnerError(RuntimeError):
    """Stable local failure that never includes probe evidence or credentials."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True, repr=False)
class ValidationProbeContext:
    validation_id: str
    deployment_id: str
    attempt: int
    input_contract_sha256: str
    requested_check_codes: tuple[str, ...]
    probe_sha256: str
    deadline_monotonic: float


@dataclass(frozen=True, slots=True, repr=False)
class ValidationProbeResult:
    outcome: ProbeOutcome
    reason_code: str
    recovery_action_code: str | None
    evidence: Mapping[str, EvidenceValue]

    def __post_init__(self) -> None:
        if self.outcome not in {"passed", "failed"}:
            raise EdgeValidationRunnerError("edge_validation.result_invalid")
        if type(self.reason_code) is not str or _SAFE_CODE.fullmatch(self.reason_code) is None:
            raise EdgeValidationRunnerError("edge_validation.result_invalid")
        if (self.outcome == "failed") != (self.recovery_action_code is not None):
            raise EdgeValidationRunnerError("edge_validation.result_invalid")
        if self.recovery_action_code is not None and (
            type(self.recovery_action_code) is not str
            or _SAFE_CODE.fullmatch(self.recovery_action_code) is None
        ):
            raise EdgeValidationRunnerError("edge_validation.result_invalid")
        if not isinstance(self.evidence, Mapping):
            raise EdgeValidationRunnerError("edge_validation.evidence_invalid")
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))


class ValidationProbe(Protocol):
    def __call__(self, context: ValidationProbeContext) -> ValidationProbeResult: ...


class DeploymentValidationRunner:
    """Run only the compiled checks and return one immutable, redacted report."""

    def __init__(
        self,
        probes: Mapping[str, ValidationProbe],
        *,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if set(probes) != set(VALIDATION_CHECK_CODES) or any(
            not callable(probe) for probe in probes.values()
        ):
            raise EdgeValidationRunnerError("edge_validation.plan_invalid")
        self._probes = dict(probes)
        self._now = now or (lambda: datetime.now(UTC))
        self._monotonic = monotonic or time.monotonic

    def run(
        self,
        *,
        validation_id: str,
        deployment_id: str,
        attempt: int,
        input_contract_sha256: str,
        requested_check_codes: Sequence[str],
    ) -> DeploymentValidationReport:
        requested = _requested_codes(requested_check_codes)
        if (
            type(validation_id) is not str
            or _VALIDATION_ID.fullmatch(validation_id) is None
            or type(deployment_id) is not str
            or _DEPLOYMENT_ID.fullmatch(deployment_id) is None
            or type(attempt) is not int
            or attempt < 1
            or type(input_contract_sha256) is not str
            or _HEX_64.fullmatch(input_contract_sha256) is None
        ):
            raise EdgeValidationRunnerError("edge_validation.assignment_invalid")
        started_at = self._wall_now()
        started_tick = self._tick()
        probe_sha256 = hashlib.sha256(
            (
                "metahub/edge-delivery-validation/probe/v1\0"
                f"{validation_id}\0{deployment_id}\0{input_contract_sha256}"
            ).encode("ascii")
        ).hexdigest()
        context = ValidationProbeContext(
            validation_id=validation_id,
            deployment_id=deployment_id,
            attempt=attempt,
            input_contract_sha256=input_contract_sha256,
            requested_check_codes=requested,
            probe_sha256=probe_sha256,
            deadline_monotonic=started_tick + (MAX_VALIDATION_MILLISECONDS / 1000),
        )
        checks: list[ValidationCheckEvidence] = []
        timed_out = False
        for code in requested:
            check_started = self._tick()
            if timed_out or _elapsed_ms(started_tick, check_started) >= MAX_VALIDATION_MILLISECONDS:
                result = _timeout_result(code, context)
                duration_ms = 0
                timed_out = True
            else:
                result = self._run_probe(code, context)
                check_finished = self._tick()
                duration_ms = min(
                    _elapsed_ms(check_started, check_finished), MAX_VALIDATION_MILLISECONDS
                )
                if _elapsed_ms(started_tick, check_finished) >= MAX_VALIDATION_MILLISECONDS:
                    result = _timeout_result(code, context)
                    timed_out = True
            evidence = _validated_evidence(code, result, context)
            checks.append(
                ValidationCheckEvidence(
                    code=code,
                    outcome=result.outcome,
                    reason_code=result.reason_code,
                    recovery_action_code=result.recovery_action_code,
                    evidence_sha256=hashlib.sha256(
                        rfc8785.dumps({"check_code": code, "evidence": evidence})
                    ).hexdigest(),
                    duration_ms=duration_ms,
                )
            )
        duration_ms = min(
            _elapsed_ms(started_tick, self._tick()), MAX_VALIDATION_MILLISECONDS
        )
        completed_at = started_at + timedelta(milliseconds=duration_ms)
        return DeploymentValidationReport(
            schema_version=VALIDATION_SCHEMA_VERSION,
            validation_id=validation_id,
            deployment_id=deployment_id,
            plan_version=VALIDATION_PLAN_VERSION,
            attempt=attempt,
            input_contract_sha256=input_contract_sha256,
            outcome=("failed" if any(item.outcome == "failed" for item in checks) else "passed"),
            checks=tuple(checks),
            started_at=_utc_text(started_at),
            completed_at=_utc_text(completed_at),
            duration_ms=duration_ms,
        )

    def _run_probe(self, code: str, context: ValidationProbeContext) -> ValidationProbeResult:
        try:
            result = self._probes[code](context)
        except Exception:
            return _unavailable_result(code, context)
        if type(result) is not ValidationProbeResult:
            raise EdgeValidationRunnerError("edge_validation.result_invalid")
        if not result.reason_code.startswith(f"{code}.") or (
            result.recovery_action_code is not None
            and not result.recovery_action_code.startswith(f"{code}.")
        ):
            raise EdgeValidationRunnerError("edge_validation.result_invalid")
        return result

    def _tick(self) -> float:
        try:
            value = self._monotonic()
        except Exception:
            raise EdgeValidationRunnerError("edge_validation.clock_invalid") from None
        if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
            raise EdgeValidationRunnerError("edge_validation.clock_invalid")
        return float(value)

    def _wall_now(self) -> datetime:
        try:
            return _aware_utc(self._now())
        except EdgeValidationRunnerError:
            raise
        except Exception:
            raise EdgeValidationRunnerError("edge_validation.clock_invalid") from None


def _requested_codes(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise EdgeValidationRunnerError("edge_validation.assignment_invalid")
    requested = tuple(values)
    if (
        not requested
        or any(type(code) is not str for code in requested)
        or len(set(requested)) != len(requested)
        or requested != tuple(code for code in VALIDATION_CHECK_CODES if code in set(requested))
    ):
        raise EdgeValidationRunnerError("edge_validation.assignment_invalid")
    return requested


def _validated_evidence(
    code: str, result: ValidationProbeResult, context: ValidationProbeContext
) -> dict[str, EvidenceValue]:
    evidence = dict(result.evidence)
    schema = _EVIDENCE_SCHEMA[code]
    if set(evidence) != set(schema):
        raise EdgeValidationRunnerError("edge_validation.evidence_invalid")
    for key, expected in schema.items():
        value = evidence[key]
        if type(value) is not expected:
            raise EdgeValidationRunnerError("edge_validation.evidence_invalid")
        if expected is int and not 0 <= value <= 2_147_483_647:
            raise EdgeValidationRunnerError("edge_validation.evidence_invalid")
        if expected is str:
            if key == "probe_sha256":
                valid = _HEX_64.fullmatch(value) is not None and value == context.probe_sha256
            else:
                valid = _SAFE_BUCKET.fullmatch(value) is not None
            if not valid:
                raise EdgeValidationRunnerError("edge_validation.evidence_invalid")
    if result.outcome == "passed" and not _passed_evidence(code, evidence):
        raise EdgeValidationRunnerError("edge_validation.evidence_invalid")
    return evidence


def _passed_evidence(code: str, evidence: Mapping[str, EvidenceValue]) -> bool:
    if code == "device_certificate":
        return evidence["certificate_valid"] is True
    if code == "signed_config_lkg":
        return (
            evidence["signature_verified"] is True
            and evidence["desired_generation"] == evidence["applied_generation"]
            and evidence["lkg_generation"] <= evidence["applied_generation"]
        )
    if code == "provider_connectivity":
        return evidence["configured_count"] > 0 and (
            evidence["reachable_count"] == evidence["configured_count"]
        )
    if code == "minimal_gateway_request":
        return evidence["gateway_outcome"] == "passed"
    if code == "usage_cost_failure_sync":
        return all(
            evidence[key] is True
            for key in ("usage_recorded", "cost_recorded", "failure_sync_verified")
        )
    if code == "backup_readiness":
        return evidence["ready"] is True
    if code == "capacity_readiness":
        return all(evidence[key] not in {"insufficient", "unknown"} for key in evidence)
    return evidence["sample_count"] > 0 and (
        evidence["latency_bucket"] not in {"failed", "unknown"}
        and evidence["error_rate_bucket"] not in {"failed", "unknown"}
    )


def _unavailable_result(code: str, context: ValidationProbeContext) -> ValidationProbeResult:
    return ValidationProbeResult(
        outcome="failed",
        reason_code=f"{code}.unavailable",
        recovery_action_code=f"{code}.retry",
        evidence=_default_evidence(code, context),
    )


def _timeout_result(code: str, context: ValidationProbeContext) -> ValidationProbeResult:
    return ValidationProbeResult(
        outcome="failed",
        reason_code=f"{code}.timeout",
        recovery_action_code=f"{code}.retry",
        evidence=_default_evidence(code, context),
    )


def _default_evidence(code: str, context: ValidationProbeContext) -> dict[str, EvidenceValue]:
    defaults: dict[str, EvidenceValue] = {}
    for key, expected in _EVIDENCE_SCHEMA[code].items():
        if key == "probe_sha256":
            defaults[key] = context.probe_sha256
        elif expected is bool:
            defaults[key] = False
        elif expected is int:
            defaults[key] = 0
        else:
            defaults[key] = "unknown"
    return defaults


def _elapsed_ms(started: float, finished: float) -> int:
    if finished < started:
        raise EdgeValidationRunnerError("edge_validation.clock_invalid")
    return int((finished - started) * 1000)


def _aware_utc(value: datetime) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise EdgeValidationRunnerError("edge_validation.clock_invalid")
    return value.astimezone(UTC)


def _utc_text(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


__all__ = (
    "DeploymentValidationRunner",
    "EdgeValidationRunnerError",
    "ValidationProbe",
    "ValidationProbeContext",
    "ValidationProbeResult",
)
