"""模型质量评分 API 的严格、安全数据契约。

响应只包含模型归属、累计整数指标、人工先验和版本；不得加入 Endpoint、凭据、
Prompt、Response 或逐请求反馈正文。人工评分使用基点整数，避免 JSON float 误差。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class ModelQualityScoreInfo(BaseModel):
    """当前租户内一条模型评分和 lifetime 观测摘要。"""

    model_quality_score_id: str
    tenant_id: str
    model_id: str
    internal_model_id: str
    display_name: str | None
    provider_code: str
    provider_name: str
    model_status: str
    status: str
    sample_count: int
    success_count: int
    error_count: int
    latency_sample_count: int
    total_latency_ms: int
    success_rate_bps: int
    error_rate_bps: int
    average_latency_ms: str | None
    manual_quality_bps: int | None
    effective_quality_bps: int
    observation_status: str
    formula_version: str
    statistics_version: int
    feedback_version: int
    first_observed_at: datetime | None
    last_observed_at: datetime | None
    feedback_updated_by: str | None
    feedback_updated_at: datetime | None
    # 以下字段来自 PRD-049 独立反馈聚合，不参与 PRD-048 的路由质量公式或版本推进。
    request_feedback_count: int
    request_feedback_rating_sum: int
    request_feedback_average_milli: int
    request_feedback_accepted_count: int
    request_feedback_adoption_rate_bps: int
    request_feedback_statistics_version: int
    request_feedback_rating_distribution: dict[str, int]
    created_at: datetime
    updated_at: datetime


class ModelQualityScoreListResponse(BaseModel):
    """分页评分列表，统一携带服务器快照时间与链路 ID。"""

    model_quality_scores: list[ModelQualityScoreInfo]
    total: int
    limit: int
    offset: int
    as_of: datetime
    request_id: str
    trace_id: str


class ModelQualityScoreMutationResponse(BaseModel):
    """人工评分写响应，显式区分真实变化和版本匹配的同值 no-op。"""

    model_quality_score: ModelQualityScoreInfo
    changed: bool
    request_id: str
    trace_id: str
