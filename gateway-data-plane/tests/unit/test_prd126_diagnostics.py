from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from metahub_gateway import diagnostics as runtime
from metahub_gateway.diagnostics import (
    DiagnosticRuntimeSnapshot,
    execute_diagnostic_command,
)
from metahub_shared.private_gateway import (
    DIAGNOSTIC_COMMAND_SCHEMA_VERSION,
    DIAGNOSTIC_SIGNATURE_PURPOSE,
    DiagnosticUnsignedCommand,
    RuntimeConnectivityInvocation,
    RuntimeDiskBudgetInvocation,
    RuntimeVersionInvocation,
    RuntimeWatermarksInvocation,
    diagnostic_invocation_sha256,
    sign_diagnostic_command,
)

DEPLOYMENT_ID = "edp_" + "1" * 32
GRANT_ID = "dgr_" + "2" * 32


def _time(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes(range(32)))


def _public_key(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


def _command(invocation: object, now: datetime, generation: int) -> dict[str, object]:
    command_id = f"dcm_{generation:032x}"
    unsigned = DiagnosticUnsignedCommand(
        schema_version=DIAGNOSTIC_COMMAND_SCHEMA_VERSION,
        signature_purpose=DIAGNOSTIC_SIGNATURE_PURPOSE,
        command_id=command_id,
        grant_id=GRANT_ID,
        deployment_id=DEPLOYMENT_ID,
        generation=generation,
        revocation_generation=3,
        grant_sha256="3" * 64,
        invocation=invocation,
        parameters_sha256=diagnostic_invocation_sha256(invocation),
        issued_at=_time(now),
        not_before=_time(now),
        deadline_at=_time(now + timedelta(seconds=30)),
        expires_at=_time(now + timedelta(minutes=2)),
        maximum_result_bytes=16_384,
        algorithm="Ed25519",
        kid="edge-ds-key-v1",
    )
    return sign_diagnostic_command(unsigned, _key()).model_dump(mode="json")


def _snapshot() -> DiagnosticRuntimeSnapshot:
    return DiagnosticRuntimeSnapshot(
        agent_version="1.2.3",
        gateway_schema_version=118,
        os_family="linux",
        architecture="x86_64",
        watermarks={
            "desired_generation": 8,
            "applied_generation": 8,
            "lkg_generation": 7,
            "telemetry_health_sequence": 21,
            "telemetry_failure_sequence": 12,
        },
        disk_budgets={
            "runtime": (200 * 1024**3, 100 * 1024**3),
            "state": (50 * 1024**3, 4 * 1024**3),
        },
        connectivity={
            "control_plane": (80, 2, 2),
            "provider_pool": (640, 4, 3),
        },
    )


def _execute(invocation: object, generation: int = 1):
    now = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    key = _key()
    return execute_diagnostic_command(
        _command(invocation, now, generation),
        trusted_keys={"edge-ds-key-v1": _public_key(key)},
        deployment_id=DEPLOYMENT_ID,
        minimum_generation=generation,
        revocation_generation=3,
        snapshot=_snapshot(),
        now=now,
        clock=lambda: now + timedelta(milliseconds=125),
    )


def test_compiled_handlers_return_only_exact_structured_results() -> None:
    reports = (
        _execute(RuntimeVersionInvocation(command_code="runtime.version"), 1),
        _execute(
            RuntimeWatermarksInvocation(
                command_code="runtime.watermarks",
                watermark_codes=("desired_generation", "lkg_generation"),
            ),
            2,
        ),
        _execute(
            RuntimeDiskBudgetInvocation(
                command_code="runtime.disk_budget", volume_code="state"
            ),
            3,
        ),
        _execute(
            RuntimeConnectivityInvocation(
                command_code="runtime.connectivity_summary",
                target_code="provider_pool",
            ),
            4,
        ),
    )
    assert [report.status for report in reports] == ["completed"] * 4
    assert [report.result.command_code for report in reports if report.result] == [
        "runtime.version",
        "runtime.watermarks",
        "runtime.disk_budget",
        "runtime.connectivity_summary",
    ]
    assert reports[1].result is not None
    assert reports[1].result.model_dump(mode="json") == {
        "command_code": "runtime.watermarks",
        "desired_generation": 8,
        "applied_generation": None,
        "lkg_generation": 7,
        "telemetry_health_sequence": None,
        "telemetry_failure_sequence": None,
    }
    assert reports[2].result is not None
    assert reports[2].result.pressure_code == "warning"
    assert reports[3].result is not None
    assert reports[3].result.status_code == "degraded"


def test_cancellation_and_deadline_return_fixed_zero_output_reports() -> None:
    now = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    invocation = RuntimeVersionInvocation(command_code="runtime.version")
    key = _key()
    command = _command(invocation, now, 5)
    cancelled = execute_diagnostic_command(
        command,
        trusted_keys={"edge-ds-key-v1": _public_key(key)},
        deployment_id=DEPLOYMENT_ID,
        minimum_generation=5,
        revocation_generation=3,
        snapshot=_snapshot(),
        now=now,
        cancellation_requested=lambda: True,
    )
    assert cancelled.status == "cancelled"
    assert cancelled.reason_code == "cancelled"
    assert cancelled.result is None

    timed_out = execute_diagnostic_command(
        command,
        trusted_keys={"edge-ds-key-v1": _public_key(key)},
        deployment_id=DEPLOYMENT_ID,
        minimum_generation=5,
        revocation_generation=3,
        snapshot=_snapshot(),
        now=now,
        clock=lambda: now + timedelta(seconds=30),
    )
    assert timed_out.status == "timed_out"
    assert timed_out.reason_code == "deadline_exceeded"
    assert timed_out.result is None


def test_runtime_contains_no_shell_or_subprocess_escape_hatch() -> None:
    source = inspect.getsource(runtime)
    for forbidden in ("subprocess", "os.system", "Popen", "shell=True", "stdout"):
        assert forbidden not in source
