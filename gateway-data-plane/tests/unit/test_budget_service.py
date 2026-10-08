"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：gateway-data-plane/tests/unit/test_budget_service.py
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from metahub_gateway.budgets import service as budget_service
from metahub_gateway.budgets.models import BudgetDecision, BudgetRule, EstimatedRequestCost
from metahub_gateway.budgets.service import (
    _budget_window,
    _rule_effective_start,
    _remove_replaceable_request_reservations,
    _validated_trusted_estimate,
    apply_budget_decision,
)
from metahub_gateway.usage.ledger import RequestLogContext
from metahub_shared.runtime.errors import ApiError


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


def test_budget_window_uses_shanghai_calendar_month_and_year() -> None:
    """月、年预算在上海自然周期边界重置，包括跨年。"""

    before_new_year = datetime(2025, 12, 31, 15, 59, 59, tzinfo=UTC)
    after_new_year = datetime(2025, 12, 31, 16, 0, 0, tzinfo=UTC)

    assert _budget_window(before_new_year, period="month") == (
        "2025-12-31", "2025-11-30T16:00:00Z", "2025-12-31T16:00:00Z",
    )
    assert _budget_window(after_new_year, period="month") == (
        "2026-01-01", "2025-12-31T16:00:00Z", "2026-01-31T16:00:00Z",
    )
    assert _budget_window(after_new_year, period="year") == (
        "2026-01-01", "2025-12-31T16:00:00Z", "2026-12-31T16:00:00Z",
    )


def test_budget_partial_first_month_returns_to_calendar_window_next_month() -> None:
    rule = BudgetRule(
        budget_rule_id="br_month", tenant_id="ten_1", scope_type="tenant", scope_id="ten_1",
        daily_budget_cny=Decimal("100"), warning_threshold_percent=80,
        exhausted_action="block", budget_period="month",
        effective_from_utc="2026-10-08T03:00:00Z",
    )
    october_start = _budget_window(datetime(2026, 10, 8, 4, tzinfo=UTC), period="month")[1]
    november_start = _budget_window(datetime(2026, 11, 2, 4, tzinfo=UTC), period="month")[1]
    assert _rule_effective_start(rule, october_start) == "2026-10-08T03:00:00Z"
    assert _rule_effective_start(rule, november_start) == november_start


def test_gateway_ignores_scheduled_rule_until_activation(monkeypatch) -> None:
    monkeypatch.setattr(budget_service, "_utcnow", lambda: datetime(2026, 10, 8, 4, tzinfo=UTC))
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
      CREATE TABLE budget_rules(
        budget_rule_id TEXT, tenant_id TEXT, environment_id TEXT, scope_type TEXT,
        scope_id TEXT, daily_budget_cny TEXT, warning_threshold_percent INTEGER,
        exhausted_action TEXT, budget_period TEXT, effective_from_utc TEXT,
        activation_at_utc TEXT, status TEXT
      );
      INSERT INTO budget_rules VALUES
        ('future','t','e','tenant','t','100',80,'block','month',NULL,'2026-10-08T05:00:00Z','active');
    """)
    try:
        assert budget_service._load_budget_rules(connection, "t", "e", "k", None, None) == []
        monkeypatch.setattr(budget_service, "_utcnow", lambda: datetime(2026, 10, 8, 6, tzinfo=UTC))
        rules = budget_service._load_budget_rules(connection, "t", "e", "k", None, None)
        assert [rule.budget_rule_id for rule in rules] == ["future"]
        assert _rule_effective_start(rules[0], "2026-09-30T16:00:00Z") == "2026-10-08T05:00:00Z"
    finally:
        connection.close()


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


def test_response_cache_trusted_estimate_requires_exact_money_contract() -> None:
    """缓存候选只能传入六位小数内的冻结金额，缺价不能伪装成非零可信估算。"""

    valid = EstimatedRequestCost(
        input_tokens=11,
        output_tokens=7,
        cost_cny=Decimal("0.123456"),
        price_available=True,
    )
    assert _validated_trusted_estimate(valid) is valid

    with pytest.raises(ApiError) as unquantized:
        _validated_trusted_estimate(
            EstimatedRequestCost(11, 7, Decimal("0.1234567"), True)
        )
    assert unquantized.value.code == "gateway.budget_unavailable"

    with pytest.raises(ApiError) as unknown_non_zero:
        _validated_trusted_estimate(
            EstimatedRequestCost(11, 7, Decimal("0.100000"), False)
        )
    assert unknown_non_zero.value.code == "gateway.budget_unavailable"


def test_trusted_estimate_rejects_cache_tokens_above_input() -> None:
    """缓存命中 Token 是输入 Token 的子集，越界事实必须失败关闭。"""

    with pytest.raises(ApiError) as invalid:
        _validated_trusted_estimate(
            EstimatedRequestCost(
                input_tokens=10,
                output_tokens=2,
                cost_cny=Decimal("0.100000"),
                price_available=True,
                cache_tokens=11,
            )
        )
    assert invalid.value.code == "gateway.budget_unavailable"


def test_response_cache_reservation_replacement_is_atomic_and_owner_qualified() -> None:
    """candidate 转 miss 只删除同 tenant/request/trace 且尚未终结的旧预留。"""

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE budget_reservations(
          tenant_id TEXT NOT NULL,
          request_id TEXT NOT NULL,
          trace_id TEXT NOT NULL,
          budget_rule_id TEXT NOT NULL,
          outstanding_cost_cny TEXT,
          UNIQUE(tenant_id,budget_rule_id,request_id)
        )
        """
    )
    connection.executemany(
        """
        INSERT INTO budget_reservations(
          tenant_id,request_id,trace_id,budget_rule_id,outstanding_cost_cny
        ) VALUES(?,?,?,?,NULL)
        """,
        (
            ("tenant_a", "req_a", "trace_a", "rule_a"),
            ("tenant_a", "req_a", "trace_a", "rule_b"),
            ("tenant_b", "req_a", "trace_b", "rule_c"),
        ),
    )
    connection.commit()

    connection.execute("BEGIN IMMEDIATE")
    _remove_replaceable_request_reservations(
        connection,
        tenant_id="tenant_a",
        request_id="req_a",
        trace_id="trace_a",
    )
    assert [
        str(row["budget_rule_id"])
        for row in connection.execute(
            "SELECT budget_rule_id FROM budget_reservations ORDER BY budget_rule_id"
        ).fetchall()
    ] == ["rule_c"]
    connection.rollback()

    connection.execute(
        "UPDATE budget_reservations SET trace_id='trace_other' "
        "WHERE tenant_id='tenant_a' AND budget_rule_id='rule_a'"
    )
    with pytest.raises(sqlite3.IntegrityError):
        _remove_replaceable_request_reservations(
            connection,
            tenant_id="tenant_a",
            request_id="req_a",
            trace_id="trace_a",
        )
    assert connection.execute(
        "SELECT COUNT(*) FROM budget_reservations WHERE tenant_id='tenant_a'"
    ).fetchone()[0] == 2
