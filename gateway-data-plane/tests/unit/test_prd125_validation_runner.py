"""PRD-125 compiled Agent validation execution and SLO boundaries."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from metahub_gateway.validation import (
    DeploymentValidationRunner,
    EdgeValidationRunnerError,
    ValidationProbeContext,
    ValidationProbeResult,
)
from metahub_shared.private_gateway import (
    MAX_VALIDATION_MILLISECONDS,
    VALIDATION_CHECK_CODES,
)

VALIDATION_ID = "dvr_12500000000000000000000000000001"
DEPLOYMENT_ID = "edp_12500000000000000000000000000001"
INPUT_SHA256 = "a" * 64
NOW = datetime(2026, 8, 27, tzinfo=UTC)


def _evidence(code: str, context: ValidationProbeContext) -> dict[str, object]:
    return {
        "device_certificate": {
            "certificate_valid": True,
            "identity_generation": 2,
            "certificate_generation": 3,
        },
        "signed_config_lkg": {
            "signature_verified": True,
            "desired_generation": 7,
            "applied_generation": 7,
            "lkg_generation": 6,
        },
        "provider_connectivity": {"configured_count": 2, "reachable_count": 2},
        "minimal_gateway_request": {
            "probe_sha256": context.probe_sha256,
            "gateway_outcome": "passed",
        },
        "usage_cost_failure_sync": {
            "probe_sha256": context.probe_sha256,
            "usage_recorded": True,
            "cost_recorded": True,
            "failure_sync_verified": True,
        },
        "backup_readiness": {"ready": True, "age_bucket": "under_24h"},
        "capacity_readiness": {
            "cpu_bucket": "ready",
            "memory_bucket": "ready",
            "disk_bucket": "ready",
        },
        "performance_baseline": {
            "latency_bucket": "under_1s",
            "error_rate_bucket": "under_1pct",
            "sample_count": 3,
        },
    }[code]


def _probes(calls: list[tuple[str, str]] | None = None):
    result = {}
    for code in VALIDATION_CHECK_CODES:
        def probe(context: ValidationProbeContext, current: str = code):
            if calls is not None:
                calls.append((current, context.probe_sha256))
            return ValidationProbeResult(
                outcome="passed",
                reason_code=f"{current}.passed",
                recovery_action_code=None,
                evidence=_evidence(current, context),
            )

        result[code] = probe
    return result


def _run(runner: DeploymentValidationRunner, requested=VALIDATION_CHECK_CODES):
    return runner.run(
        validation_id=VALIDATION_ID,
        deployment_id=DEPLOYMENT_ID,
        attempt=1,
        input_contract_sha256=INPUT_SHA256,
        requested_check_codes=requested,
    )


def test_runner_executes_compiled_plan_in_order_with_one_probe_identity() -> None:
    calls: list[tuple[str, str]] = []
    ticks = iter(float(value) for value in range(0, 19))
    report = _run(
        DeploymentValidationRunner(_probes(calls), now=lambda: NOW, monotonic=lambda: next(ticks))
    )
    assert report.outcome == "passed"
    assert [item.code for item in report.checks] == list(VALIDATION_CHECK_CODES)
    assert [code for code, _probe in calls] == list(VALIDATION_CHECK_CODES)
    assert len({probe for _code, probe in calls}) == 1
    assert report.duration_ms == 17_000
    assert report.started_at == "2026-08-27T00:00:00.000Z"
    assert report.completed_at == "2026-08-27T00:00:17.000Z"


def test_runner_executes_only_ordered_retry_subset_and_preserves_failure() -> None:
    calls: list[tuple[str, str]] = []
    probes = _probes(calls)

    def failed(context: ValidationProbeContext) -> ValidationProbeResult:
        calls.append(("provider_connectivity", context.probe_sha256))
        return ValidationProbeResult(
            outcome="failed",
            reason_code="provider_connectivity.unreachable",
            recovery_action_code="provider_connectivity.retry",
            evidence={"configured_count": 2, "reachable_count": 1},
        )

    probes["provider_connectivity"] = failed
    ticks = iter((0.0, 0.001, 0.002, 0.003))
    report = _run(
        DeploymentValidationRunner(probes, now=lambda: NOW, monotonic=lambda: next(ticks)),
        ("provider_connectivity",),
    )
    assert report.outcome == "failed"
    assert calls == [("provider_connectivity", calls[0][1])]
    assert report.checks[0].reason_code == "provider_connectivity.unreachable"
    assert "unreachable" not in report.checks[0].evidence_sha256


def test_runner_maps_probe_exceptions_without_echoing_private_detail() -> None:
    probes = _probes()

    def broken(_context: ValidationProbeContext) -> ValidationProbeResult:
        raise RuntimeError("private-provider-path-and-token")

    probes["provider_connectivity"] = broken
    ticks = iter((0.0, 0.001, 0.002, 0.003))
    report = _run(
        DeploymentValidationRunner(probes, now=lambda: NOW, monotonic=lambda: next(ticks)),
        ("provider_connectivity",),
    )
    assert report.checks[0].reason_code == "provider_connectivity.unavailable"
    assert "private-provider" not in repr(report)


@pytest.mark.parametrize(
    "code,evidence",
    (
        ("minimal_gateway_request", {"probe_sha256": "b" * 64, "gateway_outcome": "passed"}),
        ("provider_connectivity", {"configured_count": 1, "reachable_count": 0}),
        ("device_certificate", {"certificate_valid": True, "secret": "hidden"}),
    ),
)
def test_runner_rejects_mismatched_or_noncanonical_evidence(
    code: str, evidence: dict[str, object]
) -> None:
    probes = _probes()
    probes[code] = lambda _context: ValidationProbeResult(
        outcome="passed",
        reason_code=f"{code}.passed",
        recovery_action_code=None,
        evidence=evidence,
    )
    ticks = iter((0.0, 0.001, 0.002))
    runner = DeploymentValidationRunner(
        probes, now=lambda: NOW, monotonic=lambda: next(ticks)
    )
    with pytest.raises(EdgeValidationRunnerError) as invalid:
        _run(runner, (code,))
    assert invalid.value.code == "edge_validation.evidence_invalid"
    assert "hidden" not in repr(invalid.value)


def test_runner_marks_current_and_remaining_checks_failed_at_deadline() -> None:
    probes = _probes()
    ticks = iter((0.0, 0.0, 900.0, 900.0, 900.0, 900.0))
    report = _run(
        DeploymentValidationRunner(probes, now=lambda: NOW, monotonic=lambda: next(ticks)),
        ("device_certificate", "signed_config_lkg"),
    )
    assert report.duration_ms == MAX_VALIDATION_MILLISECONDS
    assert [item.reason_code for item in report.checks] == [
        "device_certificate.timeout",
        "signed_config_lkg.timeout",
    ]
    assert report.outcome == "failed"
