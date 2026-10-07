"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：gateway-data-plane/src/metahub_gateway/budgets/models.py
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from metahub_shared.runtime.webhooks import WebhookEvent


@dataclass(frozen=True)
class BudgetRule:
    """数据面使用的预算规则快照。

    该结构只包含租户、scope、阈值和策略，不包含 Virtual Key 明文、Provider Key 或请求内容。
    """

    budget_rule_id: str
    tenant_id: str
    scope_type: str
    scope_id: str
    daily_budget_cny: Decimal
    warning_threshold_percent: int
    exhausted_action: str
    environment_id: str = ""


@dataclass(frozen=True)
class BudgetDecision:
    """一次预算命中的安全决策摘要。

    该对象会进入 request_logs 的预算摘要字段，因此只能保存可审计的成本、scope 和原因码。
    """

    budget_rule_id: str
    scope_type: str
    scope_id: str
    budget_status: str
    action_taken: str
    reason_code: str
    current_cost_cny: Decimal
    budget_limit_cny: Decimal
    budget_block_id: str | None = None
    block_created: bool = False
    reservation_created: bool = False


@dataclass(frozen=True)
class EstimatedRequestCost:
    """预算检查使用的请求成本估算。

    估算只服务准入判断，真实 usage 与成本仍由 Provider 返回后的 usage_ledger 负责。
    ``price_available`` 区分真实零价与缺价；缺价时 ``cost_cny=0`` 只是兼容占位，
    调用方不得据此创建零元预留或宣称请求免费。
    """

    input_tokens: int
    output_tokens: int
    cost_cny: Decimal
    price_available: bool
    # Provider cache usage is unknown before the request; trusted callers may provide it.
    cache_tokens: int = 0


@dataclass(frozen=True)
class BudgetGuardResult:
    """预算准入成功后交给调用状态机的安全内存结果。

    ``decision`` 只描述最严重的告警/允许事实；主备分项金额来自同一冻结 token 与价格时点，
    用于 routing_attempts 和终态预留结算。该对象不持久化请求正文或任何凭据。
    """

    decision: BudgetDecision | None
    initial_estimated_cost_cny: Decimal
    fallback_estimated_cost_cny: Decimal
    # Scope 越限事件已在 Guard 事务中提交；调用层只负责在 Provider 前机会性入队。
    # tuple 避免后续状态机原地改写，事件 payload 已由 Shared 严格合同过滤。
    scope_events: tuple[WebhookEvent, ...] = ()
