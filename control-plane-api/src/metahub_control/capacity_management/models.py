"""Closed PRD-113 capacity, performance, chaos, and economics DTOs."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CapacityModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CapacityServiceInfo(CapacityModel):
    service_code: Literal["gateway", "control", "status"]
    region_code: str = Field(min_length=3, max_length=40, pattern=r"^[a-z0-9][a-z0-9-]+$")
    demand_rps: int = Field(ge=0, le=10_000_000)
    capacity_rps: int = Field(ge=1, le=10_000_000)
    utilization_bps: int = Field(ge=0)
    headroom_rps: int
    threshold_bps: int = Field(ge=5000, le=9500)
    threshold_reached: bool
    slo_p95_ms: int = Field(ge=1, le=60_000)
    recommendation_code: Literal["hold", "scale-within-30d", "scale-within-90d"]
    revision: int = Field(ge=1)
    updated_at: datetime


class CapacityScenarioInfo(CapacityModel):
    scenario_code: str = Field(min_length=3, max_length=64)
    service_code: Literal["gateway", "control", "status"]
    run_kind: Literal["load", "soak"]
    max_tenant_count: int = Field(ge=1, le=500)
    max_target_rps: int = Field(ge=1, le=100_000)
    max_duration_seconds: int = Field(ge=10, le=259_200)
    target_peak_multiplier_bps: int = Field(ge=10_000, le=30_000)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["active", "disabled"]


class PerformanceBaselineInfo(CapacityModel):
    performance_baseline_id: str = Field(pattern=r"^pbl_[0-9a-f]{32}$")
    service_code: Literal["gateway", "control", "status"]
    scenario_code: str
    environment_class: Literal["dev", "staging", "production"]
    version: int = Field(ge=1)
    sample_count: int = Field(ge=100)
    p50_ms: int = Field(ge=0)
    p95_ms: int = Field(ge=0)
    p99_ms: int = Field(ge=0)
    throughput_rps_milli: int = Field(ge=0)
    error_rate_bps: int = Field(ge=0, le=10_000)
    created_at: datetime


class CapacityForecastInfo(CapacityModel):
    capacity_forecast_id: str = Field(pattern=r"^cfc_[0-9a-f]{32}$")
    service_code: Literal["gateway", "control", "status"]
    region_code: str
    horizon_days: Literal[30, 90]
    model_code: Literal["linear-v1"]
    predicted_demand_rps: int | None = Field(default=None, ge=0)
    predicted_capacity_rps: int | None = Field(default=None, ge=1)
    confidence_bps: int | None = Field(default=None, ge=0, le=10_000)
    error_outcome_bps: int | None = Field(default=None, ge=0, le=10_000)
    recommendation_code: Literal["hold", "scale-within-30d", "scale-within-90d"] | None
    status: Literal["queued", "running", "succeeded", "failed"]
    job_id: str = Field(pattern=r"^pjob_[0-9a-f]{32}$")
    revision: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime


class LoadTestRunInfo(CapacityModel):
    load_test_run_id: str = Field(pattern=r"^ltr_[0-9a-f]{32}$")
    scenario_code: str
    service_code: Literal["gateway", "control", "status"]
    run_kind: Literal["load", "soak"]
    environment_class: Literal["dev", "staging", "production"]
    region_code: str
    synthetic_tenant_count: int = Field(ge=1, le=500)
    target_rps: int = Field(ge=1, le=100_000)
    duration_seconds: int = Field(ge=10, le=259_200)
    status: Literal["queued", "running", "succeeded", "failed", "stopped"]
    job_id: str = Field(pattern=r"^pjob_[0-9a-f]{32}$")
    sample_count: int | None = Field(default=None, ge=0)
    p50_ms: int | None = Field(default=None, ge=0)
    p95_ms: int | None = Field(default=None, ge=0)
    p99_ms: int | None = Field(default=None, ge=0)
    throughput_rps_milli: int | None = Field(default=None, ge=0)
    error_rate_bps: int | None = Field(default=None, ge=0, le=10_000)
    open_connection_delta: int | None = Field(default=None, ge=0)
    handle_delta: int | None = Field(default=None, ge=0)
    fairness_jain_millionths: int | None = Field(default=None, ge=0, le=1_000_000)
    controlled_degradation: bool | None
    regression_status: Literal[
        "no_baseline", "insufficient_confidence", "within_baseline", "regressed"
    ] | None
    safe_outcome_code: str | None
    revision: int = Field(ge=1)
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ChaosFaultInfo(CapacityModel):
    fault_code: str
    service_code: Literal["gateway", "control", "status", "shared"]
    max_blast_tenants: int = Field(ge=1, le=100)
    max_duration_seconds: int = Field(ge=10, le=3600)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["active", "disabled"]


class ChaosApprovalInfo(CapacityModel):
    approval_kind: Literal["execution", "security_review"]
    operator_id: str = Field(pattern=r"^pto_[0-9a-f]{32}$")
    approved_at: datetime


class ChaosExperimentInfo(CapacityModel):
    chaos_experiment_id: str = Field(pattern=r"^che_[0-9a-f]{32}$")
    fault_code: str
    service_code: Literal["gateway", "control", "status", "shared"]
    environment_class: Literal["staging", "production"]
    region_code: str
    blast_tenant_count: int = Field(ge=1, le=100)
    duration_seconds: int = Field(ge=10, le=3600)
    objective_p95_ms: int = Field(ge=1, le=60_000)
    objective_error_rate_bps: int = Field(ge=0, le=10_000)
    stop_error_rate_bps: int = Field(ge=1, le=10_000)
    status: Literal["pending_approval", "queued", "running", "succeeded", "failed", "killed"]
    job_id: str | None = Field(default=None, pattern=r"^pjob_[0-9a-f]{32}$")
    approvals: tuple[ChaosApprovalInfo, ...]
    kill_generation: int = Field(ge=0)
    observed_p95_ms: int | None = Field(default=None, ge=0)
    observed_error_rate_bps: int | None = Field(default=None, ge=0, le=10_000)
    objective_met: bool | None
    stop_reason_code: str | None
    revision: int = Field(ge=1)
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class PerformanceRolloutBlockInfo(CapacityModel):
    service_code: Literal["gateway", "control", "status"]
    scenario_code: str
    environment_class: Literal["dev", "staging", "production"]
    load_test_run_id: str = Field(pattern=r"^ltr_[0-9a-f]{32}$")
    status: Literal["active", "cleared"]
    created_at: datetime
    cleared_at: datetime | None


class CapacityStatusResponse(CapacityModel):
    services: tuple[CapacityServiceInfo, ...]
    scenarios: tuple[CapacityScenarioInfo, ...]
    baselines: tuple[PerformanceBaselineInfo, ...]
    forecasts: tuple[CapacityForecastInfo, ...]
    load_tests: tuple[LoadTestRunInfo, ...]
    chaos_faults: tuple[ChaosFaultInfo, ...]
    chaos_experiments: tuple[ChaosExperimentInfo, ...]
    rollout_blocks: tuple[PerformanceRolloutBlockInfo, ...]
    request_id: str
    trace_id: str


class CapacityForecastRequest(CapacityModel):
    service_code: Literal["gateway", "control", "status"]
    region_code: str = Field(min_length=3, max_length=40, pattern=r"^[a-z0-9][a-z0-9-]+$")
    horizon_days: Literal[30, 90]
    model_code: Literal["linear-v1"] = "linear-v1"
    confirmed: Literal[True]


class CapacityForecastResponse(CapacityModel):
    forecast: CapacityForecastInfo
    idempotent_replay: bool
    request_id: str
    trace_id: str


class LoadTestRequest(CapacityModel):
    scenario_code: str = Field(min_length=3, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]+$")
    environment_class: Literal["dev", "staging", "production"]
    region_code: str = Field(min_length=3, max_length=40, pattern=r"^[a-z0-9][a-z0-9-]+$")
    synthetic_tenant_count: int = Field(ge=1, le=500)
    target_rps: int = Field(ge=1, le=100_000)
    duration_seconds: int = Field(ge=10, le=259_200)
    confirmed: Literal[True]


class LoadTestResponse(CapacityModel):
    run: LoadTestRunInfo
    idempotent_replay: bool
    request_id: str
    trace_id: str


class ChaosCommandRequest(CapacityModel):
    action: Literal["create", "approve", "kill"]
    chaos_experiment_id: str | None = Field(default=None, pattern=r"^che_[0-9a-f]{32}$")
    fault_code: str | None = Field(default=None, min_length=3, max_length=64)
    service_code: Literal["gateway", "control", "status", "shared"] | None = None
    environment_class: Literal["staging", "production"] | None = None
    region_code: str | None = Field(default=None, min_length=3, max_length=40)
    blast_tenant_count: int | None = Field(default=None, ge=1, le=100)
    duration_seconds: int | None = Field(default=None, ge=10, le=3600)
    objective_p95_ms: int | None = Field(default=None, ge=1, le=60_000)
    objective_error_rate_bps: int | None = Field(default=None, ge=0, le=10_000)
    stop_error_rate_bps: int | None = Field(default=None, ge=1, le=10_000)
    reason_code: Literal[
        "resilience_validation", "release_validation", "operator_containment", "slo_containment"
    ]
    confirmed: Literal[True]

    @model_validator(mode="after")
    def validate_action_fields(self) -> ChaosCommandRequest:
        create_values = (
            self.fault_code,
            self.service_code,
            self.environment_class,
            self.region_code,
            self.blast_tenant_count,
            self.duration_seconds,
            self.objective_p95_ms,
            self.objective_error_rate_bps,
            self.stop_error_rate_bps,
        )
        if self.action == "create":
            if self.chaos_experiment_id is not None or any(
                value is None for value in create_values
            ):
                raise ValueError("chaos create fields are invalid")
            if self.reason_code not in {"resilience_validation", "release_validation"}:
                raise ValueError("chaos create reason is invalid")
            if int(self.stop_error_rate_bps or 0) < int(self.objective_error_rate_bps or 0):
                raise ValueError("chaos stop limit is invalid")
        elif self.chaos_experiment_id is None or any(value is not None for value in create_values):
            raise ValueError("chaos command fields are invalid")
        elif self.action == "approve" and self.reason_code != "release_validation":
            raise ValueError("chaos approval reason is invalid")
        elif self.action == "kill" and self.reason_code not in {
            "operator_containment",
            "slo_containment",
        }:
            raise ValueError("chaos kill reason is invalid")
        return self


class ChaosExperimentResponse(CapacityModel):
    experiment: ChaosExperimentInfo
    approval_recorded: bool
    idempotent_replay: bool
    request_id: str
    trace_id: str


class UnitEconomicsInfo(CapacityModel):
    unit_economics_snapshot_id: str = Field(pattern=r"^ues_[0-9a-f]{32}$")
    billing_period: str = Field(pattern=r"^[0-9]{4}-(?:0[1-9]|1[0-2])$")
    service_code: Literal["gateway", "control", "status", "shared"]
    request_count: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    allocated_cost_micros: int = Field(ge=0)
    cost_per_request_micros: int | None = Field(default=None, ge=0)
    cost_per_million_tokens_micros: int | None = Field(default=None, ge=0)
    currency: Literal["CNY", "USD"]
    usage_watermark: str = Field(pattern=r"^[0-9a-f]{64}$")
    allocation_version: int = Field(ge=1)
    created_at: datetime


class UnitEconomicsResponse(CapacityModel):
    items: tuple[UnitEconomicsInfo, ...]
    total: int = Field(ge=0)
    request_id: str
    trace_id: str


__all__ = [name for name in globals() if name.endswith(("Info", "Request", "Response"))]
