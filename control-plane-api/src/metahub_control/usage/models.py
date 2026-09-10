"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/usage/models.py
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


class UsageTotals(BaseModel):
    """用量汇总总计对象。

    该对象只暴露脱敏统计字段，不返回 prompt、response、Provider Key 或上游请求头，供财务、管理员和开发者追踪 Token 与成本。
    """

    request_count: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_tokens: int
    cost_cny: float
    cost_usd: float
    estimated_records: int
    real_usage_records: int


class UsageDaySummary(BaseModel):
    """按天聚合的用量对象，用于用量概览趋势图和日期筛选结果。"""

    day: str
    request_count: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_tokens: int
    cost_cny: float
    cost_usd: float
    estimated_records: int


class UsageConsumerSummary(UsageTotals):
    """Stable usage identity only; financial access does not grant directory access."""

    consumer_id: str | None
    consumer_name: str | None = None
    consumer_department: str | None = None


class UsageModelSummary(BaseModel):
    """按模型聚合的用量对象，用于定位高成本模型和价格版本影响范围。"""

    model_id: str | None
    resolved_model: str | None
    provider_id: str | None
    provider_code: str | None
    request_count: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_tokens: int
    cost_cny: float
    cost_usd: float
    estimated_records: int


class UsageVirtualKeySummary(BaseModel):
    """按 Virtual Key 聚合的用量对象，只返回 key_prefix，不返回密钥明文或哈希。"""

    virtual_key_id: str | None
    virtual_key_prefix: str | None
    request_count: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_tokens: int
    cost_cny: float
    cost_usd: float
    estimated_records: int


class UsageEnvironmentSummary(BaseModel):
    """按 Environment 聚合的用量对象，用于比较 dev/test/prod 成本边界。"""

    environment_id: str | None
    environment: str | None
    environment_name: str | None
    request_count: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_tokens: int
    cost_cny: float
    cost_usd: float
    estimated_records: int

class UsageProjectSummary(BaseModel):
    """按项目聚合的用量对象，用于团队负责人查看业务系统成本归属。"""

    project_id: str | None
    project_name: str | None
    request_count: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_tokens: int
    cost_cny: float
    cost_usd: float
    estimated_records: int


class UsageApplicationSummary(BaseModel):
    """按应用聚合的用量对象，用于项目内细分业务系统成本。"""

    application_id: str | None
    application_name: str | None
    project_id: str | None
    project_name: str | None
    request_count: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_tokens: int
    cost_cny: float
    cost_usd: float
    estimated_records: int

class UsageSummaryResponse(BaseModel):
    """用量概览响应 DTO，统一携带 request_id/trace_id 便于审计和排障追踪。"""

    totals: UsageTotals
    by_day: list[UsageDaySummary]
    by_model: list[UsageModelSummary]
    by_virtual_key: list[UsageVirtualKeySummary]
    by_consumer: list[UsageConsumerSummary] = []
    by_environment: list[UsageEnvironmentSummary] = []
    by_project: list[UsageProjectSummary] = []
    by_application: list[UsageApplicationSummary] = []
    request_id: str
    trace_id: str


class UsageRecordInfo(BaseModel):
    """单条用量记录 DTO。

    记录保留 request_id、模型、Key 前缀、Token、成本、估算标记和价格快照。
    `provider_raw_usage` 已经过 Adapter 写入前与控制面读取前双重脱敏，不包含认证或请求正文。
    """

    usage_ledger_id: str
    consumer_id: str | None = None
    tenant_id: str
    request_id: str
    trace_id: str | None
    virtual_key_id: str | None
    virtual_key_prefix: str | None
    environment_id: str | None
    environment: str | None
    environment_name: str | None
    project_id: str | None = None
    project_name: str | None = None
    application_id: str | None = None
    application_name: str | None = None
    provider_id: str | None
    provider_code: str | None
    model_id: str | None
    request_model: str | None
    resolved_model: str | None
    endpoint: str | None
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_tokens: int
    cost_amount: float
    cost_cny: float
    cost_usd: float
    currency: str
    price_schema_version: str | None
    price_snapshot_id: str | None
    estimated: bool
    usage_source: str
    normalized_usage: dict[str, Any]
    provider_raw_usage: dict[str, Any]
    created_at: datetime
    updated_at: datetime | None


class UsageRecordListResponse(BaseModel):
    """用量记录列表响应 DTO，支持控制台分页和调用详情跳转。"""

    usage_records: list[UsageRecordInfo]
    total: int
    limit: int
    offset: int
    request_id: str
    trace_id: str


class UsageExportJobInfo(BaseModel):
    """用量导出任务的安全状态 DTO。

    该结构只返回规范化筛选、汇总、校验摘要和生命周期，不返回 CSV BLOB、完整对象路径、
    lease token 或异常正文；所有实例都已经由服务层按认证 tenant_id 查询。
    """

    usage_export_job_id: str
    status: Literal["pending", "processing", "completed", "failed", "expired"]
    generation_mode: Literal["sync", "async"]
    format: Literal["csv"] = "csv"
    filters: dict[str, object | None]
    row_count: int
    total_tokens: int
    total_cost_cny: str
    total_cost_usd: str
    file_name: str
    content_type: str
    content_size_bytes: int
    content_sha256: str | None
    download_count: int
    attempt_count: int
    error_code: str | None
    requested_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime


class UsageExportStatusResponse(BaseModel):
    """异步导出状态响应，URL 由服务端固定路由生成并携带当前请求链路。"""

    export_job: UsageExportJobInfo
    status_url: str
    download_url: str
    request_id: str
    trace_id: str
