"""模块职责：定义预算规则、三级作用域、聚合状态和严格写请求的数据合同。

维护边界：金额以十进制字符串返回，所有对象保留 tenant_id 与 request_id 语义；
Virtual Key、Provider Key、Token、Prompt 和 Response 不得进入 DTO。
来源文件：control-plane-api/src/metahub_control/budgets/models.py
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

BudgetScopeType = Literal["tenant", "project", "application", "virtual_key"]


class BudgetScopeInfo(BaseModel):
    """预算作用域值对象。

    budget_scope_id 只是由租户、环境和权威资源 ID 生成的稳定引用，不能替代
    tenant-qualified 查询或授权。展示标签由服务端解析，Virtual Key 只显示安全名称与前缀。
    """

    budget_scope_id: str
    tenant_id: str
    environment_id: str
    scope_type: BudgetScopeType
    scope_id: str
    scope_label: str
    priority: int
    parent_scope_type: BudgetScopeType | None = None
    parent_scope_id: str | None = None
    parent_scope_label: str | None = None


class BudgetBlockInfo(BaseModel):
    """预算阻断生命周期的控制面安全 DTO。

    只暴露规则、业务日、金额和调用链 ID，不返回 Virtual Key 明文、Provider Key 或请求正文。
    """

    budget_block_id: str
    budget_rule_id: str
    environment_id: str
    scope_type: str
    scope_id: str
    budget_date: str
    status: str
    blocked_cost_cny: str
    budget_cny: str
    blocked_request_id: str
    blocked_trace_id: str
    blocked_at: datetime


class BudgetStatusInfo(BaseModel):
    """单条预算规则在当前业务日的状态 DTO。

    兼容 spent_cny 仍表示正式已入账金额；新增字段明确拆分正式账本、未结算预留和
    有效消耗。所有金额以字符串返回，避免 JSON 浮点精度破坏财务展示。
    """

    budget_rule_id: str
    environment_id: str
    environment: str
    environment_name: str
    scope_type: str
    scope_id: str
    scope_label: str
    budget_scope: BudgetScopeInfo
    budget_date: str
    budget_period: Literal["day", "month", "year"] = "day"
    effective_from_utc: datetime | None = None
    activation_at_utc: datetime | None = None
    ledger_spent_cny: str
    reserved_cny: str
    effective_spend_cny: str
    effective_remaining_cny: str
    spent_cny: str
    estimated_remaining_cny: str
    budget_cny: str
    usage_percent: float
    status: str
    exhausted_action: str
    next_action: str
    last_event_type: str | None = None
    block_status: str | None = None
    budget_block_id: str | None = None
    blocked_at: datetime | None = None


class BudgetScopeStatusSummary(BaseModel):
    """单个 Environment 下某一 scope 类型的规则状态计数。"""

    scope_type: BudgetScopeType
    rule_count: int
    ok_count: int
    warning_count: int
    exhausted_count: int
    block_count: int


class BudgetEnvironmentSummary(BaseModel):
    """一个 Environment 内相互独立的预算状态摘要。

    各层规则会重复覆盖同一笔 Usage，因此这里只汇总规则数量和状态，绝不把金额跨层相加。
    """

    environment_id: str
    environment: str
    environment_name: str
    budget_date: str
    scope_summaries: list[BudgetScopeStatusSummary]


class BudgetRuleInfo(BaseModel):
    """预算规则 DTO，描述租户、项目、兼容应用或 Virtual Key 日预算。"""

    budget_rule_id: str
    tenant_id: str
    environment_id: str
    environment: str
    environment_name: str
    scope_type: str
    scope_id: str
    scope_label: str
    budget_scope: BudgetScopeInfo
    scope_owner_user_id: str | None = None
    scope_owner_display_name: str | None = None
    scope_owner_email: str | None = None
    name: str
    daily_budget_cny: str
    budget_period: Literal["day", "month", "year"] = "day"
    effective_from_utc: datetime | None = None
    activation_at_utc: datetime | None = None
    warning_threshold_percent: int
    exhausted_threshold_percent: int
    exhausted_action: str
    status: str
    today_status: BudgetStatusInfo
    active_block: BudgetBlockInfo | None = None
    created_at: datetime
    updated_at: datetime


class BudgetRuleListResponse(BaseModel):
    """预算规则列表响应，统一携带 Environment 摘要和调用链标识。"""

    budget_rules: list[BudgetRuleInfo]
    total: int = 0
    offset: int = 0
    page_size: int | None = None
    environment_summaries: list[BudgetEnvironmentSummary] = Field(default_factory=list)
    request_id: str
    trace_id: str


class BudgetRuleResponse(BaseModel):
    """预算规则写入后的单条响应，供控制台保存后局部刷新使用。"""

    budget_rule: BudgetRuleInfo
    request_id: str
    trace_id: str


class BudgetPersonKeyInfo(BaseModel):
    virtual_key_id: str
    name: str
    environment_id: str | None = None
    environment_name: str | None = None


class BudgetPersonInfo(BaseModel):
    user_id: str
    display_name: str
    email: str
    key_count: int
    keys: list[BudgetPersonKeyInfo] = Field(default_factory=list)
    spent_day_cny: str | None = None
    spent_month_cny: str | None = None
    spent_year_cny: str | None = None


class BudgetPeopleResponse(BaseModel):
    people: list[BudgetPersonInfo]
    total: int
    request_id: str
    trace_id: str


class BudgetAggregateInfo(BaseModel):
    """描述可信预算链的累计 AND 聚合结论与稳定限制规则，供页面统一展示。"""

    aggregate_status: Literal["unconfigured", "ok", "threshold_reached", "exhausted"]
    effective_action: Literal["allow", "warn", "block"]
    effective_remaining_cny: str | None
    limiting_rule_id: str | None
    limiting_scope: BudgetScopeInfo | None
    conflict_policy: Literal["all_applicable_caps_deny_wins_v1"] = "all_applicable_caps_deny_wins_v1"
    scope_chain: list[BudgetScopeInfo]


class BudgetStatusResponse(BaseModel):
    """预算状态查询响应，返回可信 scope chain 与统一聚合结论。"""

    budget_date: str
    statuses: list[BudgetStatusInfo]
    aggregate: BudgetAggregateInfo
    request_id: str
    trace_id: str


class UpdateBudgetRuleRequest(BaseModel):
    """创建或更新预算规则的请求体。

    请求体不包含 tenant_id；后端必须从登录态恢复当前租户，并在服务层重新校验资源归属。
    """

    model_config = ConfigDict(extra="forbid")

    scope_type: str | None = None
    scope_id: str | None = None
    environment_id: str | None = None
    environment: str | None = None
    name: str | None = None
    daily_budget_cny: Any = None
    budget_period: Literal["day", "month", "year"] | None = None
    activation_at_utc: datetime | None = None
    warning_threshold_percent: int | None = None
    exhausted_action: str | None = None
    status: str | None = None


class CreateBudgetRuleRequest(UpdateBudgetRuleRequest):
    """create-only 预算规则请求。

    字段保持与兼容 PATCH 一致，必填金额与 scope 约束由服务层统一判断；路由会在
    RBAC 通过后执行严格 JSON 解析，拒绝重复键、未知字段和客户端 tenant_id。
    """


class UpdateBudgetModeRequest(BaseModel):
    """按预算规则 ID 切换耗尽模式的最小请求体。"""

    model_config = ConfigDict(extra="forbid")

    mode: str | None = None
