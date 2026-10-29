"""PRD-061 Provider 状态只读投影的数据模型。

DTO 只包含当前租户可见的脱敏 Provider 元数据、固定错误码和可核对指标；
不得加入凭据标识、上游正文、Prompt、Response 或任意认证信息。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class ProviderFailureItem(BaseModel):
    """一次可安全展示的 Provider 故障投影。

    `stable_id` 仅在服务内部用于稳定排序，故意不进入本 DTO，避免暴露底层事实表主键。
    """

    source: Literal["non_stream_call", "stream_call", "probe"]
    failure_kind: Literal["provider", "credential", "excluded", "probe"]
    eligible_for_success_rate: bool
    error_code: str
    request_id: str
    trace_id: str
    latency_ms: int | None
    observed_at: datetime


class ProviderStatusItem(BaseModel):
    """单个 Provider 在同一请求时刻生成的状态快照。"""

    provider_id: str
    tenant_id: str
    provider_code: str
    name: str
    provider_type: str
    base_url: str
    auth_type: str
    provider_status: Literal["active", "disabled"]
    health_status: Literal["unknown", "healthy", "unhealthy"]
    effective_health_status: Literal["healthy", "unhealthy", "observing", "unknown", "disabled"]
    availability_status: Literal["available", "degraded", "unavailable", "unknown", "disabled"]
    is_health_stale: bool
    maintenance_note: str | None
    terminal_call_count: int
    eligible_call_count: int
    success_count: int
    provider_failure_count: int
    credential_failure_count: int
    excluded_call_count: int
    success_rate_bps: int | None
    probe_total: int
    probe_success_count: int
    probe_failure_count: int
    probe_inconclusive_count: int
    probe_success_rate_bps: int | None
    call_latency_sample_count: int
    call_latency_p95_ms: int | None
    stream_first_chunk_sample_count: int
    stream_first_chunk_p95_ms: int | None
    probe_latency_sample_count: int
    probe_latency_p95_ms: int | None
    latest_call_at: datetime | None
    latest_probe_at: datetime | None
    last_tested_at: datetime | None
    last_probe_latency_ms: int | None
    last_error: ProviderFailureItem | None
    recent_failures: list[ProviderFailureItem]
    provider_created_at: datetime
    provider_updated_at: datetime


class ProviderStatusSummary(BaseModel):
    """筛选后、分页前的互斥 effective 状态汇总。"""

    total: int
    healthy: int
    unhealthy: int
    observing: int
    unknown: int
    disabled: int


class ProviderStatusListResponse(BaseModel):
    """Provider 状态列表响应，统一携带统计窗口和追踪 ID。"""

    provider_statuses: list[ProviderStatusItem]
    summary: ProviderStatusSummary
    window_started_at: datetime
    window_ended_at: datetime
    history_started_at: datetime
    total: int
    limit: int
    offset: int
    request_id: str
    trace_id: str
