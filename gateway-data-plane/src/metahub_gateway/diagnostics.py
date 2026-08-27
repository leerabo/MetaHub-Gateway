"""Compiled, shell-free PRD-126 diagnostic handlers for the private Gateway."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from metahub_shared.private_gateway import (
    DIAGNOSTIC_RESULT_SCHEMA_VERSION,
    DiagnosticCommandEnvelope,
    DiagnosticContractError,
    DiagnosticExecutionReport,
    DiagnosticResult,
    RuntimeConnectivityInvocation,
    RuntimeConnectivityResult,
    RuntimeDiskBudgetInvocation,
    RuntimeDiskBudgetResult,
    RuntimeVersionInvocation,
    RuntimeVersionResult,
    RuntimeWatermarksInvocation,
    RuntimeWatermarksResult,
    canonical_diagnostic_bytes,
    diagnostic_result_sha256,
    reject_forbidden_diagnostic_fields,
    verify_diagnostic_command,
)

_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_WATERMARK_CODES = frozenset(
    {
        "desired_generation",
        "applied_generation",
        "lkg_generation",
        "telemetry_health_sequence",
        "telemetry_failure_sequence",
    }
)
_VOLUME_CODES = frozenset({"runtime", "state", "support_bundle"})
_CONNECTIVITY_CODES = frozenset({"control_plane", "provider_pool"})


@dataclass(frozen=True, slots=True, repr=False)
class DiagnosticRuntimeSnapshot:
    """Only fixed, non-sensitive values that compiled handlers may inspect."""

    agent_version: str
    gateway_schema_version: int
    os_family: str
    architecture: str
    watermarks: Mapping[str, int]
    disk_budgets: Mapping[str, tuple[int, int]]
    connectivity: Mapping[str, tuple[int | None, int, int]]

    def __post_init__(self) -> None:
        if (
            _SEMVER.fullmatch(self.agent_version) is None
            or not 1 <= self.gateway_schema_version <= 9_999
            or self.os_family not in {"linux", "windows"}
            or self.architecture not in {"x86_64", "aarch64"}
            or not set(self.watermarks) <= _WATERMARK_CODES
            or not set(self.disk_budgets) <= _VOLUME_CODES
            or not set(self.connectivity) <= _CONNECTIVITY_CODES
        ):
            raise DiagnosticContractError("edge_diagnostic.runtime_invalid")
        if any(type(value) is not int or value < 0 for value in self.watermarks.values()):
            raise DiagnosticContractError("edge_diagnostic.runtime_invalid")
        for capacity, available in self.disk_budgets.values():
            if (
                type(capacity) is not int
                or type(available) is not int
                or capacity <= 0
                or available < 0
                or available > capacity
            ):
                raise DiagnosticContractError("edge_diagnostic.runtime_invalid")
        for latency_ms, checked, reachable in self.connectivity.values():
            if (
                (latency_ms is not None and (type(latency_ms) is not int or latency_ms < 0))
                or type(checked) is not int
                or type(reachable) is not int
                or not 1 <= checked <= 64
                or not 0 <= reachable <= checked
            ):
                raise DiagnosticContractError("edge_diagnostic.runtime_invalid")


def execute_diagnostic_command(
    value: object,
    *,
    trusted_keys: Mapping[str, bytes],
    deployment_id: str,
    minimum_generation: int,
    revocation_generation: int,
    snapshot: DiagnosticRuntimeSnapshot,
    now: datetime,
    clock: Callable[[], datetime] | None = None,
    cancellation_requested: Callable[[], bool] | None = None,
) -> DiagnosticExecutionReport:
    """Verify and execute one signed command without a shell or free-form output."""

    verified = verify_diagnostic_command(
        value,
        trusted_keys=trusted_keys,
        expected_deployment_id=deployment_id,
        minimum_generation=minimum_generation,
        revocation_generation=revocation_generation,
        now=now,
    )
    command = verified.command
    checked_now = _aware(now)
    current_time = clock or (lambda: checked_now)
    cancelled = cancellation_requested or (lambda: False)
    if cancelled():
        return _failure(command, "cancelled", checked_now, checked_now)
    if checked_now >= _time(command.deadline_at):
        return _failure(command, "deadline_exceeded", checked_now, checked_now)
    try:
        result = _dispatch(command, snapshot)
        payload = result.model_dump(mode="json")
        reject_forbidden_diagnostic_fields(payload)
        if len(canonical_diagnostic_bytes(payload)) > command.maximum_result_bytes:
            raise DiagnosticContractError("edge_diagnostic.result_invalid")
    except DiagnosticContractError as exc:
        reason = (
            "sensitive_output"
            if exc.code == "edge_diagnostic.forbidden_field"
            else "result_invalid"
        )
        completed = _bounded_time(checked_now, current_time())
        return _failure(command, reason, checked_now, completed)
    except Exception:
        completed = _bounded_time(checked_now, current_time())
        return _failure(command, "handler_failed", checked_now, completed)
    completed = _bounded_time(checked_now, current_time())
    if cancelled():
        return _failure(command, "cancelled", checked_now, completed)
    if completed >= _time(command.deadline_at):
        return _failure(command, "deadline_exceeded", checked_now, completed)
    return DiagnosticExecutionReport(
        schema_version=DIAGNOSTIC_RESULT_SCHEMA_VERSION,
        command_id=command.command_id,
        grant_id=command.grant_id,
        deployment_id=command.deployment_id,
        generation=command.generation,
        status="completed",
        reason_code="completed",
        result=result,
        result_sha256=diagnostic_result_sha256(result),
        started_at=_db_time(checked_now),
        completed_at=_db_time(completed),
        duration_ms=_duration(checked_now, completed),
    )


def _dispatch(
    command: DiagnosticCommandEnvelope, snapshot: DiagnosticRuntimeSnapshot
) -> DiagnosticResult:
    invocation = command.invocation
    if isinstance(invocation, RuntimeVersionInvocation):
        return RuntimeVersionResult(
            command_code="runtime.version",
            agent_version=snapshot.agent_version,
            gateway_schema_version=snapshot.gateway_schema_version,
            os_family=snapshot.os_family,
            architecture=snapshot.architecture,
        )
    if isinstance(invocation, RuntimeWatermarksInvocation):
        values = {
            code: snapshot.watermarks.get(code)
            for code in invocation.watermark_codes
        }
        return RuntimeWatermarksResult(
            command_code="runtime.watermarks",
            desired_generation=values.get("desired_generation"),
            applied_generation=values.get("applied_generation"),
            lkg_generation=values.get("lkg_generation"),
            telemetry_health_sequence=values.get("telemetry_health_sequence"),
            telemetry_failure_sequence=values.get("telemetry_failure_sequence"),
        )
    if isinstance(invocation, RuntimeDiskBudgetInvocation):
        budget = snapshot.disk_budgets.get(invocation.volume_code)
        if budget is None:
            raise DiagnosticContractError("edge_diagnostic.result_invalid")
        capacity, available = budget
        ratio_basis_points = available * 10_000 // capacity
        capacity_gib = capacity // (1024**3)
        return RuntimeDiskBudgetResult(
            command_code="runtime.disk_budget",
            volume_code=invocation.volume_code,
            capacity_bucket=(
                "small"
                if capacity_gib < 20
                else "medium"
                if capacity_gib < 100
                else "large"
                if capacity_gib < 1_000
                else "very_large"
            ),
            available_bucket=(
                "critical"
                if ratio_basis_points < 500
                else "low"
                if ratio_basis_points < 1_500
                else "medium"
                if ratio_basis_points < 4_000
                else "high"
            ),
            pressure_code=(
                "critical"
                if ratio_basis_points < 500
                else "warning"
                if ratio_basis_points < 1_500
                else "none"
            ),
        )
    if isinstance(invocation, RuntimeConnectivityInvocation):
        summary = snapshot.connectivity.get(invocation.target_code)
        if summary is None:
            raise DiagnosticContractError("edge_diagnostic.result_invalid")
        latency_ms, checked, reachable = summary
        return RuntimeConnectivityResult(
            command_code="runtime.connectivity_summary",
            target_code=invocation.target_code,
            status_code=(
                "unreachable"
                if reachable == 0
                else "reachable"
                if reachable == checked
                else "degraded"
            ),
            latency_bucket=(
                "timeout"
                if reachable == 0
                else "unknown"
                if latency_ms is None
                else "fast"
                if latency_ms < 100
                else "normal"
                if latency_ms < 500
                else "slow"
            ),
            checked_target_count=checked,
            reachable_target_count=reachable,
        )
    raise DiagnosticContractError("edge_diagnostic.command_invalid")


def _failure(
    command: DiagnosticCommandEnvelope,
    reason: str,
    started: datetime,
    completed: datetime,
) -> DiagnosticExecutionReport:
    status = {
        "cancelled": "cancelled",
        "deadline_exceeded": "timed_out",
    }.get(reason, "failed")
    return DiagnosticExecutionReport(
        schema_version=DIAGNOSTIC_RESULT_SCHEMA_VERSION,
        command_id=command.command_id,
        grant_id=command.grant_id,
        deployment_id=command.deployment_id,
        generation=command.generation,
        status=status,
        reason_code=reason,
        result=None,
        result_sha256=None,
        started_at=_db_time(started),
        completed_at=_db_time(completed),
        duration_ms=_duration(started, completed),
    )


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise DiagnosticContractError("edge_diagnostic.time_invalid")
    return value.astimezone(UTC)


def _bounded_time(started: datetime, value: datetime) -> datetime:
    checked = _aware(value)
    if checked < started:
        raise DiagnosticContractError("edge_diagnostic.time_invalid")
    return checked


def _duration(started: datetime, completed: datetime) -> int:
    return int((completed - started).total_seconds() * 1_000)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


__all__ = (
    "DiagnosticRuntimeSnapshot",
    "execute_diagnostic_command",
)
