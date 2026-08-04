"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：gateway-data-plane/tests/unit/test_budget_service.py
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from metahub_gateway.budgets.models import BudgetDecision
from metahub_gateway.budgets.service import _budget_window, apply_budget_decision
from metahub_gateway.usage.ledger import RequestLogContext


def test_budget_window_uses_asia_shanghai_business_day() -> None:
    """边界时间：UTC 16:00 对应上海零点，日预算窗口必须切到下一业务日。"""

    before_midnight = datetime(2026, 7, 8, 15, 59, 59, tzinfo=UTC)
    after_midnight = datetime(2026, 7, 8, 16, 0, 0, tzinfo=UTC)

    before_date, before_start, before_end = _budget_window(before_midnight)
    after_date, after_start, after_end = _budget_window(after_midnight)

    assert before_date == "2026-07-08"
    assert before_start == "2026-07-07T16:00:00Z"
    assert before_end == "2026-07-08T16:00:00Z"
    assert after_date == "2026-07-09"
    assert after_start == "2026-07-08T16:00:00Z"
    assert after_end == "2026-07-09T16:00:00Z"


def test_apply_budget_decision_enriches_request_log_context() -> None:
    """日志摘要：预算命中后要把规则、scope、动作和金额写入调用日志上下文。"""

    context = RequestLogContext(
        tenant_id="tenant_prd023",
        request_id="req_prd023_unit",
        trace_id="trc_prd023_unit",
        virtual_key_id="vk_prd023",
    )
    decision = BudgetDecision(
        budget_rule_id="br_prd023",
        scope_type="virtual_key",
        scope_id="vk_prd023",
        budget_status="exhausted",
        action_taken="block",
        reason_code="budget.exhausted.block",
        current_cost_cny=Decimal("12.5"),
        budget_limit_cny=Decimal("10"),
    )

    enriched = apply_budget_decision(context, decision)

    assert enriched.budget_status == "exhausted"
    assert enriched.budget_rule_id == "br_prd023"
    assert enriched.budget_scope_type == "virtual_key"
    assert enriched.budget_scope_id == "vk_prd023"
    assert enriched.budget_action == "block"
    assert enriched.budget_reason_code == "budget.exhausted.block"
    assert enriched.budget_current_cost_cny == "12.5"
    assert enriched.budget_limit_cny == "10"
