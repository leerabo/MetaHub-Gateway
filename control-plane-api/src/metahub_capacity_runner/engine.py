"""Pure fixed-catalog execution engine shared by runner HTTP and test loopback."""

from __future__ import annotations

import hashlib
import json

from fastapi import HTTPException

from .contracts import CapacityPermitClaims, CapacityRunnerResult

SCENARIOS = {
    "gateway-release": (
        "gateway",
        200,
        10_000,
        3_600,
        "5f24c3049b4da0105935b1b2d49f3e88de76d550f3d3dc3028efd71ac360b5e4",
    ),
    "gateway-soak": (
        "gateway",
        200,
        5_000,
        259_200,
        "a85b5fa397f2d7701e35d4496bc836d1b47009565e96f3007314b941292055ce",
    ),
    "control-read": (
        "control",
        100,
        2_000,
        3_600,
        "a2fb6a16a547d693b4ad512b1f6005c5f57828a47ef4ab39aa3920e71ddf51e0",
    ),
    "status-read": (
        "status",
        100,
        5_000,
        3_600,
        "43db72b15e57cc55b3cdf930faedf8ed7f45c084e4dd88061d1e41a0da8ff111",
    ),
}
FAULTS = {
    "dependency-timeout": (
        "shared",
        25,
        900,
        "1259aae8d8f794dd5c1e91d47b37e217c0b439d6225f9e77a423f3bfeb9f2197",
    ),
    "redis-unavailable": (
        "shared",
        25,
        900,
        "0589a0498286307bc74fc6809654ad31b7644cd306fe544e744280b5657f0219",
    ),
    "kms-unavailable": (
        "control",
        10,
        600,
        "b3c8103edddb19854fd56d08c0fca9018de3901018d33c6e638b411a2bad9c33",
    ),
    "object-store-unavailable": (
        "control",
        10,
        600,
        "2cd452f9fcae0318b11d6ae0d8cf61d2ac94cf8129b98da4f79c5ff43ec055cc",
    ),
    "cell-isolation": (
        "gateway",
        50,
        900,
        "d73b9b2f948a4b4e8fb51f06d5fd981baaf1883b7747491562e1a854403cec62",
    ),
    "region-isolation": (
        "gateway",
        100,
        600,
        "3aea2b4f77b5745f147d8b641e9fcb1f738201e64a21d366fd853517e3bab024",
    ),
    "resource-pressure": (
        "shared",
        25,
        900,
        "c6b7d791403fdbaf00a44c0a124029577ce3c5813d5002da89ce91368ab134fe",
    ),
}


def simulate_capacity_run(claims: CapacityPermitClaims) -> CapacityRunnerResult:
    if claims.operation == "load_test":
        scenario = SCENARIOS.get(claims.scenario_code or "")
        if scenario is None:
            raise HTTPException(status_code=422, detail="runner.scenario_invalid")
        service, max_tenants, max_rps, max_duration, manifest = scenario
        if (
            claims.service_code != service
            or claims.synthetic_tenant_count > max_tenants
            or claims.target_rps > max_rps
            or claims.duration_seconds > max_duration
            or claims.manifest_sha256 != manifest
        ):
            raise HTTPException(status_code=422, detail="runner.scenario_invalid")
        p50 = 35 + claims.target_rps // 1000
        p95 = p50 * 2
        p99 = p95 + max(10, p50 // 2)
        aggregate = {
            "operation": claims.operation,
            "object_id": claims.object_id,
            "sample_count": max(
                100, min(1_000_000, claims.target_rps * claims.duration_seconds)
            ),
            "p50_ms": p50,
            "p95_ms": p95,
            "p99_ms": p99,
            "throughput_rps_milli": claims.target_rps * 995,
            "error_rate_bps": 25,
            "cpu_peak_bps": min(9000, 2500 + claims.target_rps // 2),
            "memory_peak_mib": 512,
            "open_connection_delta": 0,
            "handle_delta": 0,
            "fairness_jain_millionths": 995_000,
            "controlled_degradation": True,
        }
        return CapacityRunnerResult(
            **aggregate,
            safe_outcome_code="runner.load_completed",
            result_sha256=_digest(aggregate),
        )
    fault = FAULTS.get(claims.fault_code or "")
    if fault is None:
        raise HTTPException(status_code=422, detail="runner.fault_invalid")
    service, max_tenants, max_duration, manifest = fault
    if (
        (service != "shared" and claims.service_code != service)
        or claims.synthetic_tenant_count > max_tenants
        or claims.duration_seconds > max_duration
        or claims.manifest_sha256 != manifest
    ):
        raise HTTPException(status_code=422, detail="runner.fault_invalid")
    observed_p95 = max(1, claims.objective_p95_ms * 9 // 10)
    observed_error = max(0, claims.objective_error_rate_bps * 9 // 10)
    aggregate = {
        "operation": claims.operation,
        "object_id": claims.object_id,
        "observed_p95_ms": observed_p95,
        "observed_error_rate_bps": observed_error,
        "objective_met": True,
    }
    return CapacityRunnerResult(
        **aggregate,
        safe_outcome_code="runner.chaos_objective_met",
        result_sha256=_digest(aggregate),
    )


def _digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


__all__ = ["FAULTS", "SCENARIOS", "simulate_capacity_run"]
