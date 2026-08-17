"""PRD-086 客户健康只读接口的严格公开模型。"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

# 公开评分、请求链标识、六信号顺序与权重共同冻结 v1 响应合同。
_SCORE_ID = re.compile(r"^chs_[0-9a-f]{32}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SIGNAL_ORDER = (
    "usage_trend",
    "reliability",
    "cost_anomaly_health",
    "budget_readiness",
    "team_activity",
    "ticket_health",
)
_WEIGHTS = (2500, 2500, 1500, 1000, 1500, 1000)


class _StrictModel(BaseModel):
    """拒绝未审计字段进入客户健康公开合同。"""

    model_config = ConfigDict(extra="forbid")


class CustomerHealthWindow(_StrictModel):
    """约束当前窗口和比较窗口必须是相邻的七日 UTC 区间。"""

    start: str
    end: str
    comparison_start: str
    comparison_end: str
    timezone: Literal["UTC"] = "UTC"

    @model_validator(mode="after")
    def validate_windows(self) -> CustomerHealthWindow:
        """解析并核对两个窗口边界，防止重叠、空洞或非七日区间。"""

        comparison_start = _utc_second(self.comparison_start)
        comparison_end = _utc_second(self.comparison_end)
        start = _utc_second(self.start)
        end = _utc_second(self.end)
        # 评分窗口必须首尾相接且各为七日，否则公开趋势没有可比较语义。
        if (
            end - start != timedelta(days=7)
            or start - comparison_start != timedelta(days=7)
            or comparison_end != start
        ):
            # 窗口长度或衔接不符合冻结公式时拒绝响应模型。
            raise ValueError("customer health windows must be adjacent UTC seven-day ranges")
        return self


class CustomerHealthSignal(_StrictModel):
    """冻结单个客户健康信号的权重、来源、可用性和安全计数字段。"""

    code: Literal[
        "usage_trend",
        "reliability",
        "cost_anomaly_health",
        "budget_readiness",
        "team_activity",
        "ticket_health",
    ]
    weight_bps: int
    availability: Literal["available", "no_samples", "unavailable"]
    health_status: Literal["healthy", "watch", "at_risk", "critical", "unknown"]
    score_bps: int | None
    contribution_bps: int
    current_value: int | None
    comparison_value: int | None
    rate_bps: int | None
    source_kind: Literal[
        "request_log_aggregate",
        "cost_anomaly_aggregate",
        "budget_rule_aggregate",
        "user_activity_aggregate",
        "support_ticket_unconfigured",
    ]

    @model_validator(mode="after")
    def validate_signal(self) -> CustomerHealthSignal:
        """逐项核对版本权重、基点、计数、来源和工单未接入约束。"""

        index = _SIGNAL_ORDER.index(self.code)
        # 权重与信号顺序是版本化公式的一部分，任何漂移都拒绝序列化。
        if self.weight_bps != _WEIGHTS[index]:
            raise ValueError("signal weight does not match the versioned formula")
        # 所有基点值必须落在闭区间，避免异常值污染公开评分与风险等级。
        for value in (self.score_bps, self.contribution_bps, self.rate_bps):
            if value is not None and not 0 <= value <= 10000:
                raise ValueError("basis-point value is out of range")
        # 聚合计数仅接受数据库安全整数，不允许负数、浮点数或超大值。
        for value in (self.current_value, self.comparison_value):
            if value is not None and not 0 <= value <= 922337203685477:
                raise ValueError("signal count is out of range")
        expected_sources = {
            "usage_trend": "request_log_aggregate",
            "reliability": "request_log_aggregate",
            "cost_anomaly_health": "cost_anomaly_aggregate",
            "budget_readiness": "budget_rule_aggregate",
            "team_activity": "user_activity_aggregate",
            "ticket_health": "support_ticket_unconfigured",
        }
        # 信号代码只能绑定冻结的数据来源类型，防止未知来源冒充可信事实。
        if self.source_kind != expected_sources[self.code]:
            raise ValueError("signal source does not match its code")
        # v1 尚未接入工单源，必须完整保持 unavailable/unknown/空计数投影。
        if self.code == "ticket_health":
            if (
                self.availability != "unavailable"
                or self.health_status != "unknown"
                or self.score_bps is not None
                or self.contribution_bps != 0
                or any(
                    value is not None
                    for value in (self.current_value, self.comparison_value, self.rate_bps)
                )
            ):
                # 工单信号出现任何可用数据都属于未交付能力冒充，必须拒绝。
                raise ValueError("ticket health must remain explicitly unavailable in v1")
        # 其余五个已配置来源不得伪装为未接入，也必须提供可复算分值。
        elif self.availability == "unavailable" or self.score_bps is None:
            raise ValueError("configured signals cannot be unavailable")
        return self


class CustomerHealthAction(_StrictModel):
    """限定建议动作代码及其可选后端导航目标，不接受任意地址。"""

    code: Literal[
        "restore_gateway_activity",
        "review_failed_requests",
        "review_cost_anomalies",
        "configure_tenant_budget",
        "reengage_team",
        "connect_support_ticket_source",
        "maintain_current_adoption",
    ]
    target_navigation_item_id: Literal[
        "growth-analytics", "request-logs", "cost-anomalies", "budgets", "users"
    ] | None

    @model_validator(mode="after")
    def validate_target(self) -> CustomerHealthAction:
        """核对建议代码与导航目标白名单，未授权目标只能投影为空值。"""

        allowed = {
            "restore_gateway_activity": "growth-analytics",
            "review_failed_requests": "request-logs",
            "review_cost_anomalies": "cost-anomalies",
            "configure_tenant_budget": "budgets",
            "reengage_team": "users",
            "connect_support_ticket_source": None,
            "maintain_current_adoption": None,
        }
        expected = allowed[self.code]
        # 建议目标只能为空或等于固定映射，禁止任意页面标识形成越权链接。
        if self.target_navigation_item_id not in {None, expected}:
            raise ValueError("action target does not match its code")
        # 本身没有导航语义的建议必须始终为空目标，不能被调用方附加链接。
        if expected is None and self.target_navigation_item_id is not None:
            raise ValueError("action must not expose a navigation target")
        return self


class CustomerHealthActivation(_StrictModel):
    """公开七阶段激活摘要，并绑定完成数量与最终激活布尔状态。"""

    completed_stage_count: int
    total_stage_count: Literal[7] = 7
    activated: bool

    @model_validator(mode="after")
    def validate_activation(self) -> CustomerHealthActivation:
        """校验完成阶段范围以及 activated 与七阶段完成状态的一致性。"""

        # 完成数量必须位于冻结的七阶段范围内，不能接受负数或溢出值。
        if not 0 <= self.completed_stage_count <= 7:
            raise ValueError("activation stage count is out of range")
        # activated 只在七阶段全部完成时为真，防止前后字段形成矛盾投影。
        if self.activated != (self.completed_stage_count == 7):
            raise ValueError("activation state does not match stage count")
        return self


class CustomerHealthResponse(_StrictModel):
    """定义 tenant-qualified 客户健康响应并验证全部可公开字段的一致性。"""

    tenant_id: str
    customer_health_score_id: str
    score_version: Literal["customer-health-v1"] = "customer-health-v1"
    score_bps: int
    risk_level: Literal["healthy", "watch", "at_risk", "critical"]
    risk_tags: list[
        Literal[
            "usage_declining",
            "high_error_rate",
            "cost_anomaly_open",
            "budget_missing",
            "team_inactive",
        ]
    ]
    missing_signals: list[Literal["ticket_health"]]
    effective_weight_bps: Literal[9000] = 9000
    freshness_status: Literal["fresh", "stale"]
    recommended_actions: list[CustomerHealthAction]
    window: CustomerHealthWindow
    signals: list[CustomerHealthSignal]
    activation: CustomerHealthActivation
    evaluated_at: str
    generated_at: str
    request_id: str
    trace_id: str

    @field_validator("customer_health_score_id")
    @classmethod
    def validate_score_id(cls, value: str) -> str:
        """校验评分标识采用固定前缀和三十二位小写十六进制摘要。"""

        # 稳定评分标识格式异常时失败关闭，避免未审计对象进入事件关联。
        if _SCORE_ID.fullmatch(value) is None:
            raise ValueError("invalid customer health score id")
        return value

    @field_validator("request_id", "trace_id")
    @classmethod
    def validate_chain_id(cls, value: str) -> str:
        """校验 request_id 与 trace_id 只包含安全字符并满足长度上限。"""

        # 请求链标识不合法时拒绝响应，避免控制字符或超长值进入日志与页面。
        if _REQUEST_ID.fullmatch(value) is None:
            raise ValueError("invalid request chain id")
        return value

    @model_validator(mode="after")
    def validate_response(self) -> CustomerHealthResponse:
        """核对租户、规范顺序、缺失信号和贡献总和形成完整响应合同。"""

        # tenant_id 与总分是响应归属和公式的基础边界，缺失或越界必须拒绝。
        if not self.tenant_id or not 0 <= self.score_bps <= 10000:
            raise ValueError("invalid customer health owner or score")
        # 六信号必须按冻结顺序返回，避免客户端按位置复算时产生语义错位。
        if [item.code for item in self.signals] != list(_SIGNAL_ORDER):
            raise ValueError("customer health signals are not in canonical order")
        # 风险标签保持规范顺序，保证事件哈希和前端展示在重复计算时稳定。
        if self.risk_tags != sorted(self.risk_tags, key=_risk_tag_order):
            raise ValueError("risk tags are not in canonical order")
        # v1 唯一缺失来源固定为工单，不能把已配置来源静默标成缺失。
        if self.missing_signals != ["ticket_health"]:
            raise ValueError("ticket health must be the sole missing v1 signal")
        # 每项贡献之和必须精确复现总分，防止浮点或舍入漂移破坏合同。
        if sum(item.contribution_bps for item in self.signals) != self.score_bps:
            raise ValueError("signal contributions do not reproduce total score")
        _utc_second(self.evaluated_at)
        _utc_second(self.generated_at)
        return self


def _risk_tag_order(value: str) -> int:
    """返回冻结风险标签的规范序号，供响应和事件稳定排序。"""

    return (
        "usage_declining",
        "high_error_rate",
        "cost_anomaly_open",
        "budget_missing",
        "team_inactive",
    ).index(value)


def _utc_second(value: str) -> datetime:
    """严格解析 UTC Z 整秒文本，拒绝小数秒、无时区和无效日期。"""

    # 公开时间必须使用规范 UTC 整秒格式，避免不同时区表示产生哈希差异。
    if type(value) is not str or not value.endswith("Z") or "." in value:
        raise ValueError("time must be a UTC Z whole second")
    # 解析异常统一转换为安全校验错误，不透传底层日期异常正文。
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        # 无效日期只返回稳定模型错误，不向接口泄露解析异常正文。
        raise ValueError("invalid UTC time") from None
    # 即使文本可解析也必须确认零时区偏移，防止非 UTC 时间混入窗口。
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("time must be UTC")
    return parsed.astimezone(UTC)


__all__ = [
    "CustomerHealthAction",
    "CustomerHealthActivation",
    "CustomerHealthResponse",
    "CustomerHealthSignal",
    "CustomerHealthWindow",
]
