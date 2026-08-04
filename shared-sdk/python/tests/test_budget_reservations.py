"""预算预留分项结算的历史兼容、租户隔离与更新范围测试。"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

from metahub_shared.runtime.budget_reservations import (
    load_outstanding_reserved_cny,
    set_request_budget_reservations_outstanding,
)


def _connection() -> sqlite3.Connection:
    """建立只含聚合合同所需列的内存账本，避免测试依赖完整业务迁移。"""

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE budget_reservations (
          tenant_id TEXT NOT NULL,
          budget_rule_id TEXT NOT NULL,
          budget_date TEXT NOT NULL,
          request_id TEXT NOT NULL,
          reserved_cost_cny NUMERIC NOT NULL,
          outstanding_cost_cny NUMERIC NULL
        );
        CREATE TABLE finops_ledger_entries (
          tenant_id TEXT NOT NULL,
          source_request_id TEXT NOT NULL,
          entry_type TEXT NOT NULL,
          direction TEXT NOT NULL,
          status TEXT NOT NULL
        );
        """
    )
    return connection


def test_outstanding_aggregation_preserves_legacy_and_new_four_quadrants() -> None:
    """NULL/0/正覆盖额与 Usage 组合必须保持历史合同，并隔离同 request 的其他租户。"""

    connection = _connection()
    connection.executemany(
        """
        INSERT INTO budget_reservations(
          tenant_id, budget_rule_id, budget_date, request_id,
          reserved_cost_cny, outstanding_cost_cny
        ) VALUES(?, ?, '2026-07-19', ?, ?, ?)
        """,
        [
            # 历史 NULL 且无 Usage：继续按原始总预留计费。
            ("tenant_a", "rule_shared", "req_null_without_usage", "10", None),
            # 历史 NULL 且已有 Usage：由真实 Usage 替代，不重复计入预留。
            ("tenant_a", "rule_shared", "req_null_with_usage", "20", None),
            # 新终态 0 且无 Usage：确认未调用 Provider，不能继续占用预算。
            ("tenant_a", "rule_shared", "req_zero_without_usage", "30", "0"),
            # 新终态正数且已有 Usage：备用 Usage 之外仍保留未知主账单。
            ("tenant_a", "rule_shared", "req_positive_with_usage", "40", "3"),
            # 同 request/rule/date 的其他租户事实不得影响 tenant_a 聚合。
            ("tenant_b", "rule_shared", "req_positive_with_usage", "50", "7"),
        ],
    )
    connection.executemany(
        """
        INSERT INTO finops_ledger_entries(
          tenant_id, source_request_id, entry_type, direction, status
        ) VALUES(?, ?, 'usage_charge', 'debit', 'posted')
        """,
        [
            ("tenant_a", "req_null_with_usage"),
            ("tenant_a", "req_positive_with_usage"),
            ("tenant_b", "req_positive_with_usage"),
        ],
    )

    amount = load_outstanding_reserved_cny(
        connection,
        tenant_id="tenant_a",
        budget_rule_id="rule_shared",
        budget_date="2026-07-19",
    )
    other_amount = load_outstanding_reserved_cny(
        connection,
        tenant_id="tenant_b",
        budget_rule_id="rule_shared",
        budget_date="2026-07-19",
    )

    assert amount == Decimal("13")
    assert other_amount == Decimal("7")


def test_outstanding_update_requires_matching_tenant_and_request() -> None:
    """覆盖额更新必须同时命中 tenant_id 与 request_id，禁止共享请求号导致跨租户改账。"""

    connection = _connection()
    connection.executemany(
        """
        INSERT INTO budget_reservations(
          tenant_id, budget_rule_id, budget_date, request_id,
          reserved_cost_cny, outstanding_cost_cny
        ) VALUES(?, ?, '2026-07-19', 'req_shared', '10', NULL)
        """,
        [("tenant_a", "rule_a"), ("tenant_b", "rule_b")],
    )

    updated = set_request_budget_reservations_outstanding(
        connection,
        tenant_id="tenant_a",
        request_id="req_shared",
        outstanding_cost_cny="2",
    )
    missing = set_request_budget_reservations_outstanding(
        connection,
        tenant_id="tenant_a",
        request_id="req_missing",
        outstanding_cost_cny="9",
    )
    rows = connection.execute(
        "SELECT tenant_id, outstanding_cost_cny FROM budget_reservations ORDER BY tenant_id"
    ).fetchall()

    assert updated == 1
    assert missing == 0
    assert [(row["tenant_id"], row["outstanding_cost_cny"]) for row in rows] == [
        ("tenant_a", 2),
        ("tenant_b", None),
    ]
