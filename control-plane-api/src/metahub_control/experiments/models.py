"""实验 API 数据契约：固定请求与响应字段，不接收租户、权限或敏感业务正文。

Strict public DTOs for PRD-075 online model experiments.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Literal

from metahub_shared.experiments import calculate_variant_targets
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator

ExperimentStatus = Literal["active", "collecting_feedback", "completed"]
ExperimentVariant = Literal["a", "b"]

# 该常量固定公共资源标识格式，避免任意租户或自由文本进入实验契约。
_RESOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class CreateExperimentRequest(BaseModel):
    """定义实验创建请求的严格字段集合，租户和操作者只能来自认证上下文。

    The immutable configuration accepted by ``POST /experiments``.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    name: StrictStr = Field(min_length=1, max_length=120, repr=False)
    project_id: StrictStr = Field(min_length=1, max_length=128)
    environment_id: StrictStr = Field(min_length=1, max_length=128)
    model_alias_id: StrictStr = Field(min_length=1, max_length=128)
    model_a_id: StrictStr = Field(min_length=1, max_length=128)
    model_b_id: StrictStr = Field(min_length=1, max_length=128)
    target_sample_count: StrictInt = Field(ge=2, le=100_000)
    traffic_sample_bps: StrictInt = Field(ge=1, le=10_000)
    model_a_allocation_bps: StrictInt = Field(ge=1, le=9_999)
    sampling_duration_seconds: StrictInt = Field(ge=60, le=604_800)
    feedback_collection_enabled: StrictBool
    feedback_window_seconds: StrictInt = Field(ge=0, le=604_800)

    @model_validator(mode="after")
    def validate_configuration(self) -> CreateExperimentRequest:
        """交叉校验实验时间窗、范围和双臂配置，拒绝冲突字段与未知输入。"""
        if (
            self.name != self.name.strip()
            or any(
                unicodedata.category(character) == "Cc"
                for character in self.name
            )
            or any(
                _RESOURCE_ID.fullmatch(value) is None
                for value in (
                    self.project_id,
                    self.environment_id,
                    self.model_alias_id,
                    self.model_a_id,
                    self.model_b_id,
                )
            )
            or self.model_a_id == self.model_b_id
            or (
                self.feedback_collection_enabled
                and not 60 <= self.feedback_window_seconds <= 604_800
            )
            or (
                not self.feedback_collection_enabled
                and self.feedback_window_seconds != 0
            )
        ):
            # 实验字段组合或类型不满足严格契约时拒绝，禁止静默补默认值或接受未知字段。
            raise ValueError("experiment.input_invalid")
        try:
            calculate_variant_targets(
                self.target_sample_count,
                self.model_a_allocation_bps,
            )
        # 实验字段组合或类型不满足严格契约时拒绝，禁止静默补默认值或接受未知字段。
        except ValueError:
            # 实验字段组合或类型不满足严格契约时拒绝，禁止静默补默认值或接受未知字段。
            raise ValueError("experiment.input_invalid") from None
        return self


class ExperimentArmInfo(BaseModel):
    """定义实验公共响应投影，隐藏内部租约、密钥、路由快照和敏感业务正文。"""
    model_config = ConfigDict(extra="forbid", strict=True)

    variant: ExperimentVariant
    model_id: str
    provider_id: str
    provider_code: str
    allocation_bps: int = Field(ge=1, le=9_999)
    target_count: int = Field(ge=1, le=100_000)
    assigned_count: int = Field(ge=0, le=100_000)


class ExperimentInfo(BaseModel):
    """定义实验公共响应投影，隐藏内部租约、密钥、路由快照和敏感业务正文。"""
    model_config = ConfigDict(extra="forbid", strict=True)

    experiment_id: str
    name: str = Field(repr=False)
    project_id: str
    environment_id: str
    model_alias_id: str
    model_a: ExperimentArmInfo
    model_b: ExperimentArmInfo
    target_sample_count: int = Field(ge=2, le=100_000)
    traffic_sample_bps: int = Field(ge=1, le=10_000)
    sampling_duration_seconds: int = Field(ge=60, le=604_800)
    feedback_collection_enabled: bool
    feedback_window_seconds: int = Field(ge=0, le=604_800)
    algorithm_version: int = Field(ge=1)
    status: ExperimentStatus
    assigned_count: int = Field(ge=0, le=100_000)
    facts_version: int = Field(ge=0)
    started_at: datetime
    sampling_deadline: datetime
    sampling_ended_at: datetime | None
    feedback_deadline: datetime | None
    settlement_deadline: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ExperimentCreateResponse(BaseModel):
    """定义实验公共响应投影，隐藏内部租约、密钥、路由快照和敏感业务正文。"""
    model_config = ConfigDict(extra="forbid", strict=True)

    experiment: ExperimentInfo
    idempotent_replay: bool
    request_id: str
    trace_id: str


class ExperimentDetailResponse(BaseModel):
    """定义实验公共响应投影，隐藏内部租约、密钥、路由快照和敏感业务正文。"""
    model_config = ConfigDict(extra="forbid", strict=True)

    experiment: ExperimentInfo
    request_id: str
    trace_id: str


class ExperimentListResponse(BaseModel):
    """定义实验公共响应投影，隐藏内部租约、密钥、路由快照和敏感业务正文。"""
    model_config = ConfigDict(extra="forbid", strict=True)

    experiments: list[ExperimentInfo]
    next_cursor: str | None
    request_id: str
    trace_id: str


class ExperimentArmResult(BaseModel):
    """定义实验臂与权威结果投影，只暴露聚合指标、样本计数和固定状态。"""
    model_config = ConfigDict(extra="forbid", strict=True)

    variant: ExperimentVariant
    model_id: str
    provider_id: str
    provider_code: str
    assigned_count: int = Field(ge=0)
    gateway_success_count: int = Field(ge=0)
    gateway_failure_count: int = Field(ge=0)
    unresolved_count: int = Field(ge=0)
    gateway_success_rate_bps: int | None = Field(default=None, ge=0, le=10_000)
    exposed_count: int = Field(ge=0)
    not_exposed_count: int = Field(ge=0)
    exposure_unknown_count: int = Field(ge=0)
    quality_success_count: int = Field(ge=0)
    quality_failure_count: int = Field(ge=0)
    quality_success_rate_bps: int | None = Field(default=None, ge=0, le=10_000)
    latency_sample_count: int = Field(ge=0)
    total_latency_ms: int = Field(ge=0)
    average_latency_micros: int | None = Field(default=None, ge=0)
    p95_latency_ms: int | None = Field(default=None, ge=0)
    settlement_recorded_count: int = Field(ge=0)
    settlement_not_exposed_count: int = Field(ge=0)
    settlement_unavailable_count: int = Field(ge=0)
    settlement_pending_count: int = Field(ge=0)
    gross_cost_cny_micros: int = Field(ge=0)
    cost_known_count: int = Field(ge=0)
    cost_unknown_count: int = Field(ge=0)
    average_cost_cny_micros: int | None = Field(default=None, ge=0)
    feedback_collection_enabled: bool
    feedback_count: int = Field(ge=0)
    rating_sum: int = Field(ge=0)
    average_rating_milli: int = Field(ge=0)
    accepted_count: int = Field(ge=0)
    adoption_rate_bps: int = Field(ge=0, le=10_000)


class ExperimentResultsResponse(BaseModel):
    """定义实验臂与权威结果投影，只暴露聚合指标、样本计数和固定状态。"""
    model_config = ConfigDict(extra="forbid", strict=True)

    experiment_id: str
    status: ExperimentStatus
    is_final: bool
    completion_reason: Literal["target_reached", "sampling_deadline_reached"] | None
    facts_version: int = Field(ge=0)
    facts_through_at: datetime
    next_state_at: datetime | None
    pending_execution_count: int = Field(ge=0)
    pending_settlement_count: int = Field(ge=0)
    results: list[ExperimentArmResult] = Field(min_length=2, max_length=2)
    request_id: str
    trace_id: str


__all__ = [
    "CreateExperimentRequest",
    "ExperimentArmInfo",
    "ExperimentArmResult",
    "ExperimentCreateResponse",
    "ExperimentDetailResponse",
    "ExperimentInfo",
    "ExperimentListResponse",
    "ExperimentResultsResponse",
    "ExperimentStatus",
]
