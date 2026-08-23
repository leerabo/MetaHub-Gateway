"""PRD-107 local final-capability owner, propagation and latency gates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from time import perf_counter_ns

from metahub_gateway.feature_flags import LocalFeatureGate, VerifiedCapabilitySnapshot

WORKSPACE_ID = "wsp_" + "1" * 32
OTHER_WORKSPACE_ID = "wsp_" + "2" * 32


def _snapshot(generation: int, enabled: bool) -> VerifiedCapabilitySnapshot:
    now = datetime.now(UTC)
    return VerifiedCapabilitySnapshot(
        workspace_id=WORKSPACE_ID,
        generation=generation,
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
        capabilities={"gateway.new_router": enabled},
    )


def test_verified_snapshot_owner_generation_and_expiry_fail_closed() -> None:
    gate = LocalFeatureGate(WORKSPACE_ID)
    first = _snapshot(1, True)
    assert gate.apply_verified_snapshot(first) is True
    assert gate.enabled("gateway.new_router", workspace_id=WORKSPACE_ID) is True
    assert gate.enabled("gateway.unknown", workspace_id=WORKSPACE_ID) is False
    assert gate.enabled("gateway.new_router", workspace_id=OTHER_WORKSPACE_ID) is False
    assert gate.apply_verified_snapshot(_snapshot(1, False)) is False
    assert gate.generation == 1
    assert gate.enabled(
        "gateway.new_router",
        workspace_id=WORKSPACE_ID,
        now=first.expires_at,
    ) is False


def test_local_decision_p95_below_one_millisecond() -> None:
    gate = LocalFeatureGate(WORKSPACE_ID)
    snapshot = _snapshot(1, True)
    assert gate.apply_verified_snapshot(snapshot)
    samples: list[float] = []
    for _index in range(20_000):
        started = perf_counter_ns()
        assert gate.enabled(
            "gateway.new_router",
            workspace_id=WORKSPACE_ID,
            now=snapshot.issued_at,
        )
        samples.append((perf_counter_ns() - started) / 1_000_000)
    samples.sort()
    p95_ms = samples[int(len(samples) * 0.95) - 1]
    assert p95_ms < 1.0


def test_verified_snapshot_propagates_below_five_seconds() -> None:
    gate = LocalFeatureGate(WORKSPACE_ID)
    started = perf_counter_ns()
    assert gate.apply_verified_snapshot(_snapshot(1, True))
    assert gate.enabled("gateway.new_router", workspace_id=WORKSPACE_ID)
    propagation_seconds = (perf_counter_ns() - started) / 1_000_000_000
    assert propagation_seconds < 5.0
