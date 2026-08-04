"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/dashboard/models.py
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class DashboardWindow(BaseModel):
    """Dashboard 当前统计窗口。

    时间窗口按 Asia/Shanghai 业务日计算，但查询服务会转换为 UTC 边界读取数据库，避免不同运行环境时区漂移。
    """

    timezone: str
    start_at: datetime
    end_at: datetime
    generated_at: datetime


class DashboardMetric(BaseModel):
    """Dashboard KPI 指标。

    指标只包含当前租户聚合后的数值和展示状态，不包含任何请求正文、响应正文、Key 明文或上游错误原文。
    """

    id: Literal[
        "today_requests",
        "success_rate",
        "p95_latency_ms",
        "today_cost_cny",
        "total_tokens",
        "error_rate",
    ]
    label: str
    value: float
    unit: Literal["count", "percent", "ms", "cny", "tokens"]
    status: Literal["ok", "warning", "critical", "empty"]
    description: str


class DashboardTopModel(BaseModel):
    """Top 模型聚合项。

    模型维度来自 request_logs 与 usage_ledger 的安全字段，不能回传 Prompt、Response 或 Provider Key。
    """

    model_id: str | None
    resolved_model: str | None
    provider_code: str | None
    request_count: int
    success_count: int
    total_tokens: int
    cost_cny: float


class DashboardTopVirtualKey(BaseModel):
    """Top Virtual Key 聚合项。

    只返回 key_prefix 和名称用于排障定位，绝不返回 Virtual Key 明文、哈希或 Provider Key。
    """

    virtual_key_id: str | None
    virtual_key_name: str | None
    virtual_key_prefix: str | None
    request_count: int
    success_count: int
    total_tokens: int
    cost_cny: float


class DashboardRecentError(BaseModel):
    """Dashboard 最近错误摘要。

    错误列表只展示 request_id、错误码和安全诊断标题，避免把上游响应正文或用户输入带到总览页。
    """

    request_id: str
    trace_id: str
    error_code: str | None
    diagnosis_title: str | None
    model: str | None
    provider_code: str | None
    latency_ms: int | None
    created_at: datetime


class DashboardRecommendedAction(BaseModel):
    """空状态推荐动作。

    推荐动作只指向控制台安全入口，不包含密钥、租户外部链接或用户输入内容。
    """

    label: str
    target: str
    description: str


class DashboardEmptyState(BaseModel):
    """Dashboard 空数据状态。

    当当前租户今日没有任何调用日志时返回引导内容，避免空表格让用户误以为系统不可用。
    """

    is_empty: bool
    title: str
    description: str
    recommended_actions: list[DashboardRecommendedAction]


class DashboardOverviewResponse(BaseModel):
    """Dashboard 总览响应。

    响应统一携带 request_id 和 trace_id，便于控制台错误提示与控制面日志串联。
    """

    window: DashboardWindow
    metrics: list[DashboardMetric]
    top_models: list[DashboardTopModel]
    top_virtual_keys: list[DashboardTopVirtualKey]
    recent_errors: list[DashboardRecentError]
    empty_state: DashboardEmptyState
    request_id: str
    trace_id: str


class _StrictRoleDashboardModel(BaseModel):
    """角色首页 DTO 公共基类，拒绝服务端意外注入未审计字段。"""

    model_config = ConfigDict(extra="forbid")


class RoleDashboardViewOption(_StrictRoleDashboardModel):
    """角色首页可切换视图。

    该列表已经由后端把租户角色配置与本次请求的实时权限快照求交；前端不得按角色名补回未授权视图。
    """

    code: Literal["executive", "finance", "security", "developer", "operations"]
    label: str
    is_default: bool


class RoleDashboardConfigInfo(_StrictRoleDashboardModel):
    """当前角色首页的安全配置摘要。

    响应只暴露稳定配置 ID、revision 和视图目录，不返回 tenant_id、角色名或完整权限集合，避免扩大浏览器状态面。
    """

    role_dashboard_config_id: str
    config_revision: int
    default_view_code: Literal["executive", "finance", "security", "developer", "operations"]
    selected_view_code: Literal["executive", "finance", "security", "developer", "operations"]
    available_views: list[RoleDashboardViewOption]


class RoleDashboardFact(_StrictRoleDashboardModel):
    """角色首页卡片中的一条低风险聚合事实。

    value 只承载非负聚合数值；模型名、Key、Endpoint、请求正文和上游错误正文均不属于该结构。
    """

    code: str
    label: str
    value: float
    unit: Literal["count", "percent", "ms", "cny", "tokens", "steps"]


class RoleDashboardCard(_StrictRoleDashboardModel):
    """角色首页固定卡片。

    卡片顺序由后端视图目录决定，target_navigation_item_id 只有在当前权限快照允许目标能力时才返回。
    """

    code: Literal["cost", "security", "stability", "adoption", "provider_health", "budget_risk"]
    label: str
    status: Literal["ok", "warning", "critical", "empty"]
    primary_value: float
    primary_unit: Literal["count", "percent", "ms", "cny", "tokens", "steps"]
    summary: str
    facts: list[RoleDashboardFact]
    target_navigation_item_id: str | None = None


class RoleDashboardEmptyState(_StrictRoleDashboardModel):
    """角色首页空状态。

    空状态只提供固定说明，不携带由客户端提交的文字或未经后端导航复核的跳转目标。
    """

    is_empty: bool
    title: str
    description: str


class RoleDashboardResponse(_StrictRoleDashboardModel):
    """PAGE-092 角色首页响应。

    响应统一携带 request_id/trace_id，并把视图配置与同一只读快照中的六类聚合事实一起返回。
    """

    config: RoleDashboardConfigInfo
    window: DashboardWindow
    cards: list[RoleDashboardCard]
    empty_state: RoleDashboardEmptyState
    request_id: str
    trace_id: str
