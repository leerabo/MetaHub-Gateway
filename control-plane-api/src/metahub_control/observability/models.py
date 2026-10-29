"""Closed platform observability, SLO, budget, and alert DTOs."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ObservabilityModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ServiceCatalogEntryInfo(ObservabilityModel):
    service_catalog_entry_id: str = Field(pattern=r"^sce_[0-9a-f]{32}$")
    service_code: str
    display_name: str
    owner_team_code: str
    service_tier: int = Field(ge=1, le=3)
    golden_signals: tuple[str, ...]
    status: Literal["active", "disabled"]
    revision: int = Field(ge=1)
    source_watermark: int = Field(ge=0)
    total_count: int = Field(ge=0)
    error_rate_bps: int = Field(ge=0, le=10000)
    latency_p95_ms: int = Field(ge=0)
    health_status: Literal["healthy", "unhealthy", "unavailable"]


class ServiceCatalogResponse(ObservabilityModel):
    items: tuple[ServiceCatalogEntryInfo, ...]
    total: int = Field(ge=0)
    projection: Literal["operations", "security"]
    request_id: str
    trace_id: str


class ServiceLevelObjectiveInfo(ObservabilityModel):
    service_level_objective_id: str = Field(pattern=r"^slo_[0-9a-f]{32}$")
    slo_code: str
    version_number: int = Field(ge=1)
    service_code: str
    display_name: str
    signal_class: Literal["reliability", "security"]
    sli_kind: Literal["availability", "latency"]
    operation_code: str
    region_code: str
    window_minutes: int = Field(ge=5, le=43200)
    target_bps: int = Field(ge=1, le=10000)
    latency_threshold_ms: int | None
    burn_warning_bps: int = Field(ge=10000)
    burn_critical_bps: int = Field(gt=10000)
    definition_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    is_current: bool
    created_at: datetime


class ServiceLevelObjectiveListResponse(ObservabilityModel):
    items: tuple[ServiceLevelObjectiveInfo, ...]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=200)
    offset: int = Field(ge=0)
    projection: Literal["operations", "security"]
    request_id: str
    trace_id: str


class ServiceLevelObjectiveRequest(ObservabilityModel):
    service_code: Literal["gateway", "control"]
    slo_code: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{2,63}$")
    display_name: str = Field(min_length=1, max_length=120)
    signal_class: Literal["reliability", "security"]
    sli_kind: Literal["availability", "latency"]
    operation_code: Literal[
        "chat.completions",
        "embeddings",
        "internal.chat",
        "internal.batch.chat",
        "adapter.identity_probe",
        "observability.services",
        "observability.slos.read",
        "observability.slos.write",
        "observability.budgets",
        "observability.alert_ack",
        "health",
        "readiness",
        "other",
    ]
    region_code: Literal[
        "global",
        "cn-east-1",
        "cn-north-1",
        "cn-south-1",
        "us-east-1",
        "us-west-1",
        "eu-west-1",
        "ap-southeast-1",
    ]
    window_minutes: int = Field(ge=5, le=43200)
    target_bps: int = Field(ge=1, le=10000)
    latency_threshold_ms: Literal[10, 50, 100, 250, 500, 1000, 2500, 5000] | None
    burn_warning_bps: int = Field(ge=10000, le=100000000)
    burn_critical_bps: int = Field(gt=10000, le=100000000)
    previous_version: int = Field(ge=0)
    reason_code: Literal["slo_create", "slo_revision"]
    confirmed: Literal[True]

    @model_validator(mode="after")
    def validate_contract(self) -> ServiceLevelObjectiveRequest:
        gateway_operations = {
            "chat.completions",
            "embeddings",
            "internal.chat",
            "internal.batch.chat",
            "adapter.identity_probe",
            "health",
            "readiness",
            "other",
        }
        control_operations = {
            "observability.services",
            "observability.slos.read",
            "observability.slos.write",
            "observability.budgets",
            "observability.alert_ack",
            "health",
            "readiness",
            "other",
        }
        if (
            (self.sli_kind == "latency") != (self.latency_threshold_ms is not None)
            or self.burn_critical_bps <= self.burn_warning_bps
            or (self.previous_version == 0) != (self.reason_code == "slo_create")
            or (
                self.operation_code
                not in (
                    gateway_operations
                    if self.service_code == "gateway"
                    else control_operations
                )
            )
        ):
            raise ValueError("invalid SLO contract")
        return self


class ServiceLevelObjectiveResponse(ObservabilityModel):
    slo: ServiceLevelObjectiveInfo
    idempotent_replay: bool = False
    aggregation_job_id: str = Field(pattern=r"^pjob_[0-9a-f]{32}$")
    request_id: str
    trace_id: str


class ErrorBudgetInfo(ObservabilityModel):
    error_budget_id: str = Field(pattern=r"^ebg_[0-9a-f]{32}$")
    service_level_objective_id: str = Field(pattern=r"^slo_[0-9a-f]{32}$")
    slo_code: str
    slo_version: int = Field(ge=1)
    service_code: str
    signal_class: Literal["reliability", "security"]
    region_code: str
    window_started_at: datetime
    window_ended_at: datetime
    total_count: int = Field(ge=0)
    good_count: int = Field(ge=0)
    allowed_bad_count: int = Field(ge=0)
    consumed_bad_count: int = Field(ge=0)
    remaining_bad_count: int = Field(ge=0)
    burn_rate_bps: int = Field(ge=0)
    health_status: Literal["healthy", "warning", "critical", "exhausted", "unavailable"]
    source_watermark: int = Field(ge=0)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    computed_at: datetime


class AlertInfo(ObservabilityModel):
    alert_id: str = Field(pattern=r"^alt_[0-9a-f]{32}$")
    service_level_objective_id: str = Field(pattern=r"^slo_[0-9a-f]{32}$")
    slo_code: str
    service_code: str
    signal_class: Literal["reliability", "security"]
    region_code: str
    severity: Literal["warning", "critical"]
    state: Literal["firing", "suppressed", "resolved"]
    safe_reason_code: Literal["slo_breached", "error_budget_exhausted"]
    source_watermark: int = Field(ge=0)
    revision: int = Field(ge=1)
    first_seen_at: datetime
    last_seen_at: datetime
    resolved_at: datetime | None
    latest_acknowledgement_at: datetime | None


class ErrorBudgetListResponse(ObservabilityModel):
    items: tuple[ErrorBudgetInfo, ...]
    alerts: tuple[AlertInfo, ...]
    limit: int = Field(ge=1, le=200)
    next_cursor: str | None
    projection: Literal["operations", "security"]
    request_id: str
    trace_id: str


class AlertAcknowledgementRequest(ObservabilityModel):
    revision: int = Field(ge=1)
    reason_code: Literal["investigating", "known_issue", "mitigating"]
    confirmed: Literal[True]


class AlertAcknowledgementInfo(ObservabilityModel):
    acknowledgement_id: str = Field(pattern=r"^aak_[0-9a-f]{32}$")
    alert_id: str = Field(pattern=r"^alt_[0-9a-f]{32}$")
    alert_revision: int = Field(ge=1)
    reason_code: Literal["investigating", "known_issue", "mitigating"]
    acknowledged_at: datetime


class AlertAcknowledgementResponse(ObservabilityModel):
    acknowledgement: AlertAcknowledgementInfo
    idempotent_replay: bool = False
    request_id: str
    trace_id: str
