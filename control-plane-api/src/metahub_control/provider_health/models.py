"""PRD-035 Provider 健康 API 数据模型。

所有 DTO 都只包含脱敏后的 Endpoint 与固定错误分类，不包含凭据 ID、密文或上游响应正文。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ProbeProviderRequest(BaseModel):
    """手工探测请求体。

    当前没有客户端可控参数；禁止多余字段可避免调用方注入 URL、Header、Key 或 tenant_id。
    """

    model_config = ConfigDict(extra="forbid")


class ProviderHealthItem(BaseModel):
    """单个 Provider 的可展示健康快照，只允许出现脱敏后的运维字段。"""

    provider_id: str
    tenant_id: str
    provider_code: str
    name: str
    provider_type: str
    base_url: str
    provider_status: str
    health_status: str
    effective_health_status: str
    is_health_stale: bool
    last_tested_at: datetime | None
    last_success_at: datetime | None
    last_failure_at: datetime | None
    last_probe_latency_ms: int | None
    last_probe_http_status: int | None
    last_probe_error_code: str | None
    last_test_error: str | None
    consecutive_probe_failures: int
    next_probe_at: datetime | None
    health_updated_at: datetime | None


class ProviderHealthSummary(BaseModel):
    """筛选后、分页前的互斥健康状态汇总。"""

    total: int
    active: int
    healthy: int
    unhealthy: int
    observing: int
    unknown: int
    disabled: int


class ProviderHealthListResponse(BaseModel):
    """Provider 健康列表响应，统一携带追踪 ID。"""

    provider_health: list[ProviderHealthItem]
    summary: ProviderHealthSummary
    total: int
    limit: int
    offset: int
    request_id: str
    trace_id: str


class ProbeAttemptInfo(BaseModel):
    """单次网络尝试的安全摘要，不暴露凭据标识和上游正文。"""

    sequence: int
    result_status: str
    auth_result: str
    http_status: int | None
    latency_ms: int
    error_code: str | None
    result_summary: str
    checked_at: datetime


class ProviderProbeResponse(BaseModel):
    """手工探测结果；上游失败仍用 HTTP 200 表达，以便运维查看完整分类。"""

    provider_health: ProviderHealthItem
    result_status: str
    attempts: list[ProbeAttemptInfo]
    request_id: str
    trace_id: str


class ProviderModelDiscoveryResponse(ProviderProbeResponse):
    """Only validated model IDs are exposed; upstream metadata and credentials stay private."""

    model_ids: list[str]
    catalog_truncated: bool
