"""PRD-053 成本异常公开数据合同。

本模块只定义控制面可返回的安全 DTO。金额以 canonical 十进制字符串和六位 CNY
字符串表达，避免 SQLite 聚合上限或 JavaScript Number 精度影响异常判定。任何 DTO 都不得
携带 Virtual Key 明文、Provider Credential、Prompt、Response 或原始异常正文。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictInt


CostAnomalyRuleType = Literal[
    "tenant_spend_spike",
    "virtual_key_spend_spike",
    "virtual_key_off_hours",
]
CostAnomalySeverity = Literal["warning", "critical"]
CostAnomalyStatus = Literal["open", "acknowledged", "dismissed", "mitigated"]
CostAnomalyScopeType = Literal["tenant", "virtual_key"]
CostAnomalyActionType = Literal[
    "acknowledge",
    "dismiss",
    "apply_temporary_rate_limit",
    "release_temporary_rate_limit",
]
CostAnomalyDismissReason = Literal[
    "false_positive",
    "expected_activity",
    "resolved_without_action",
    "other",
]


class TemporaryRateLimitOverrideInfo(BaseModel):
    """临时限流覆盖的安全摘要。

    limit 只包含正整数治理值；状态由持久状态与服务器 UTC 时间共同派生，不能由客户端
    自报。对象只暴露不透明资源 ID，不包含 Virtual Key 明文或计数存储地址。
    """

    temporary_rate_limit_override_id: str
    virtual_key_id: str
    rpm_limit: int | None
    tpm_limit: int | None
    concurrency_limit: int | None
    status: Literal["active", "released"]
    effective_status: Literal["active", "released", "expired"]
    starts_at: datetime
    expires_at: datetime
    released_at: datetime | None


class CostAnomalyActionInfo(BaseModel):
    """一次 append-only 管理动作的可展示摘要。"""

    cost_anomaly_action_id: str
    action_type: CostAnomalyActionType
    status_before: CostAnomalyStatus
    status_after: CostAnomalyStatus
    dismiss_reason: CostAnomalyDismissReason | None
    duration_minutes: int | None
    rpm_limit: int | None
    tpm_limit: int | None
    concurrency_limit: int | None
    effective_rpm_before: int | None
    effective_rpm_after: int | None
    effective_tpm_before: int | None
    effective_tpm_after: int | None
    effective_concurrency_before: int | None
    effective_concurrency_after: int | None
    target_override_id: str | None
    actor_user_id: str
    operation_request_id: str
    operation_trace_id: str
    created_at: datetime


class CostAnomalyInfo(BaseModel):
    """单条不可变检测证据及其当前治理状态。

    Project/Application 字段是正式账本发生时快照；名称仅用于当前租户内辅助展示，不能
    反向作为权限依据。代表 request/trace 仍须通过现有日志接口再次执行 tenant 校验。
    """

    cost_anomaly_id: str
    tenant_id: str
    environment_id: str
    environment_name: str
    rule_type: CostAnomalyRuleType
    severity: CostAnomalySeverity
    status: CostAnomalyStatus
    scope_type: CostAnomalyScopeType
    scope_id: str
    scope_label: str
    virtual_key_id: str | None
    virtual_key_name: str | None
    virtual_key_prefix: str | None
    top_virtual_key_id: str | None
    project_id_snapshot: str | None
    application_id_snapshot: str | None
    window_start: datetime
    window_end: datetime
    baseline_start: datetime
    baseline_end: datetime
    current_cost_micros: str
    current_cost_cny: str
    baseline_total_micros: str
    baseline_total_cny: str
    baseline_average_micros: str
    baseline_average_cny: str
    baseline_bucket_count: int
    observed_ratio_millis: str | None
    threshold_multiplier_millis: int
    minimum_cost_micros: str
    source_entry_count: int
    estimated_entry_count: int
    legacy_entry_count: int
    affected_virtual_key_count: int
    representative_request_id: str | None
    representative_trace_id: str | None
    recommended_action: str
    detection_version: str
    event_dispatch_status: Literal["pending", "processing", "dispatched", "failed"]
    detected_at: datetime
    mitigation_eligible_until: datetime
    mitigation_eligible: bool
    temporary_rate_limit_override: TemporaryRateLimitOverrideInfo | None
    actions: list[CostAnomalyActionInfo]


class CostAnomalySummary(BaseModel):
    """当前租户同一读快照内的异常与检测器汇总。"""

    open_count: int
    critical_count: int
    mitigated_count: int
    active_override_count: int
    detector_status: Literal["healthy", "degraded", "idle"]
    detector_error_code: str | None
    latest_window_end: datetime | None


class CostAnomalyListResponse(BaseModel):
    """成本异常 keyset 分页响应，统一携带 request_id 与 trace_id。"""

    cost_anomalies: list[CostAnomalyInfo]
    summary: CostAnomalySummary
    total: int
    limit: int
    has_more: bool
    next_cursor: str | None
    request_id: str
    trace_id: str


class CostAnomalyActionRequest(BaseModel):
    """严格动作正文。

    `StrictInt` 明确拒绝 bool、float 和数字字符串；具体动作允许的字段组合由服务层再次
    校验，防止 acknowledge 借可选字段携带无意义数据进入幂等摘要或 Audit。
    """

    model_config = ConfigDict(extra="forbid")

    action: CostAnomalyActionType
    dismiss_reason: CostAnomalyDismissReason | None = None
    duration_minutes: StrictInt | None = None
    rpm_limit: StrictInt | None = None
    tpm_limit: StrictInt | None = None
    concurrency_limit: StrictInt | None = None


class CostAnomalyActionResponse(BaseModel):
    """返回动作提交或幂等恢复结果，并携带本次调用的完整链路标识。"""

    cost_anomaly: CostAnomalyInfo
    action: CostAnomalyActionInfo
    temporary_rate_limit_override: TemporaryRateLimitOverrideInfo | None
    idempotent_replay: bool
    request_id: str
    trace_id: str
