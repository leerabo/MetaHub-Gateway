"""PRD-052 正式 Usage Charge 预算金额与预留结算边界专项测试。"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest
from metahub_shared.runtime.budget_reservations import load_outstanding_reserved_cny
from metahub_shared.runtime.budget_spend import (
    MAX_BUDGET_CNY_MICROS,
    BudgetSpendUnavailableError,
    load_budget_spend_amounts,
)


def _connection() -> sqlite3.Connection:
    """建立只包含 Shared 金额合同字段的内存数据库。

    精简 schema 刻意不复制生产 CHECK，使测试可以注入类型漂移、未知证据和错误状态，
    验证 Python 边界在数据库约束被绕过时仍会失败关闭。
    """

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE finops_ledger_entries (
          ledger_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
          tenant_id TEXT NOT NULL,
          entry_type TEXT NOT NULL,
          direction TEXT NOT NULL,
          amount_micros,
          currency TEXT NOT NULL DEFAULT 'CNY',
          normalized_cny_micros,
          cny_normalization_status TEXT NOT NULL,
          price_evidence_status TEXT NOT NULL,
          source_request_id TEXT,
          environment_id TEXT,
          project_id TEXT,
          application_id TEXT,
          virtual_key_id TEXT,
          status TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE budget_reservations (
          budget_reservation_id TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
          tenant_id TEXT NOT NULL,
          budget_rule_id TEXT NOT NULL,
          budget_date TEXT NOT NULL,
          request_id TEXT NOT NULL,
          reserved_cost_cny NUMERIC NOT NULL,
          outstanding_cost_cny NUMERIC NULL
        ) WITHOUT ROWID;
        CREATE TABLE usage_ledger (
          tenant_id TEXT NOT NULL,
          request_id TEXT NOT NULL
        );
        CREATE TABLE workspace_tenant_bindings (
          workspace_id TEXT NOT NULL,
          tenant_id TEXT NOT NULL,
          status TEXT NOT NULL
        );
        CREATE TABLE workspace_fx_rate_versions (
          workspace_fx_rate_version_id TEXT NOT NULL,
          workspace_id TEXT NOT NULL,
          tenant_id TEXT NOT NULL,
          version_number INTEGER NOT NULL,
          usd_to_cny TEXT NOT NULL,
          effective_from TEXT NOT NULL,
          created_by_membership_id TEXT,
          created_at TEXT
        );
        """
    )
    return connection


_MISSING = object()


def _insert_entry(
    connection: sqlite3.Connection,
    *,
    amount_micros: object,
    request_id: str,
    tenant_id: str = "tenant_a",
    environment_id: str | None = "env_prod",
    project_id: str | None = "project_a",
    application_id: str | None = "app_a",
    virtual_key_id: str | None = "key_a",
    entry_type: str = "usage_charge",
    direction: str = "debit",
    normalization_status: str = "normalized",
    evidence_status: str = "priced",
    normalized_micros: object = _MISSING,
    currency: str = "CNY",
    status: str = "posted",
    created_at: str = "2026-07-27T00:00:00Z",
) -> None:
    """插入一条可控正式账本测试行，不生成任何请求正文或密钥字段。"""

    connection.execute(
        """
        INSERT INTO finops_ledger_entries(
          tenant_id, entry_type, direction, amount_micros, currency, normalized_cny_micros,
          cny_normalization_status, price_evidence_status, source_request_id,
          environment_id, project_id, application_id, virtual_key_id, status, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            tenant_id,
            entry_type,
            direction,
            amount_micros,
            currency,
            amount_micros if normalized_micros is _MISSING else normalized_micros,
            normalization_status,
            evidence_status,
            request_id,
            environment_id,
            project_id,
            application_id,
            virtual_key_id,
            status,
            created_at,
        ),
    )


def _load(
    connection: sqlite3.Connection,
    *,
    scope_type: str,
    scope_id: str,
    budget_rule_id: str = "rule_scope",
):
    """按固定上海业务日调用 Shared helper，减少测试中的无关参数重复。"""

    return load_budget_spend_amounts(
        connection,
        tenant_id="tenant_a",
        environment_id="env_prod",
        scope_type=scope_type,
        scope_id=scope_id,
        budget_rule_id=budget_rule_id,
        budget_date="2026-07-27",
        start_utc="2026-07-26T16:00:00Z",
        end_utc="2026-07-27T16:00:00Z",
    )


@pytest.mark.parametrize(
    ("scope_type", "scope_id", "expected_micros"),
    [
        ("tenant", "tenant_a", 7_000_000),
        ("project", "project_a", 3_000_000),
        ("application", "app_a", 1_000_000),
        ("virtual_key", "key_b", 2_000_000),
    ],
)
def test_spend_filters_tenant_environment_business_day_and_scope(
    scope_type: str,
    scope_id: str,
    expected_micros: int,
) -> None:
    """四类 scope 只累计可信租户、精确 Environment 和上海日半开区间内的快照。"""

    connection = _connection()
    _insert_entry(
        connection,
        amount_micros=1_000_000,
        request_id="req_a",
        created_at="2026-07-26T16:00:00Z",
    )
    _insert_entry(
        connection,
        amount_micros=2_000_000,
        request_id="req_b",
        application_id="app_b",
        virtual_key_id="key_b",
        evidence_status="legacy_amount_only",
    )
    _insert_entry(
        connection,
        amount_micros=4_000_000,
        request_id="req_c",
        project_id="project_b",
        application_id="app_c",
        virtual_key_id="key_c",
    )
    _insert_entry(
        connection,
        amount_micros=8_000_000,
        request_id="req_other_env",
        environment_id="env_test",
    )
    _insert_entry(
        connection,
        amount_micros=16_000_000,
        request_id="req_other_tenant",
        tenant_id="tenant_b",
    )
    _insert_entry(
        connection,
        amount_micros=32_000_000,
        request_id="req_before_day",
        created_at="2026-07-26T15:59:59Z",
    )
    _insert_entry(
        connection,
        amount_micros=64_000_000,
        request_id="req_end_boundary",
        created_at="2026-07-27T16:00:00Z",
    )

    amounts = _load(connection, scope_type=scope_type, scope_id=scope_id)

    assert amounts.posted_cny_micros == expected_micros
    assert amounts.reserved_cny_micros == 0
    assert amounts.effective_cny_micros == expected_micros


def test_spend_excludes_adjustments_reversal_gift_trial_and_nonformal_rows() -> None:
    """只有 usage_charge/debit/posted 改变模型消费预算，账务余额和坏状态全部排除。"""

    connection = _connection()
    _insert_entry(connection, amount_micros=1_000_000, request_id="req_usage")
    for index, entry_type in enumerate(
        ("manual_debit", "manual_credit", "reversal", "gift_credit", "trial_credit"),
        start=1,
    ):
        _insert_entry(
            connection,
            amount_micros=index * 10_000_000,
            request_id=f"req_adjustment_{index}",
            entry_type=entry_type,
            direction="debit" if entry_type == "manual_debit" else "credit",
            evidence_status="not_applicable",
        )
    _insert_entry(
        connection,
        amount_micros=70_000_000,
        request_id="req_wrong_direction",
        direction="credit",
    )
    _insert_entry(
        connection,
        amount_micros=80_000_000,
        request_id="req_wrong_status",
        status="pending",
    )

    amounts = _load(connection, scope_type="project", scope_id="project_a")

    assert amounts.posted_cny_micros == 1_000_000
    assert amounts.effective_cny_micros == 1_000_000


@pytest.mark.parametrize(
    ("amount", "normalization_status", "evidence_status"),
    [
        (0, "normalized", "unpriced"),
        (None, "unavailable", "priced"),
        (1, "unavailable", "priced"),
        (1, "normalized", "not_applicable"),
        (1, "normalized", "unknown"),
        (1.5, "normalized", "priced"),
        (-1, "normalized", "priced"),
        (MAX_BUDGET_CNY_MICROS + 1, "normalized", "priced"),
    ],
)
def test_spend_fails_closed_for_unknown_or_invalid_usage_amount(
    amount: object,
    normalization_status: str,
    evidence_status: str,
) -> None:
    """未定价、未归一、Usage 非法证据和整数金额坏值都不能按零成本放行。"""

    connection = _connection()
    _insert_entry(
        connection,
        amount_micros=amount,
        request_id="req_invalid",
        normalization_status=normalization_status,
        evidence_status=evidence_status,
    )

    with pytest.raises(BudgetSpendUnavailableError):
        _load(connection, scope_type="tenant", scope_id="tenant_a")


def test_spend_converts_legacy_priced_usd_using_rate_at_charge_time() -> None:
    """旧版 USD 分录虽未写入 CNY 快照，预算读取仍按发生时汇率计入。"""

    connection = _connection()
    connection.execute(
        "INSERT INTO workspace_tenant_bindings(workspace_id, tenant_id, status) VALUES(?, ?, ?)",
        ("workspace_a", "tenant_a", "active"),
    )
    connection.execute(
        """
        INSERT INTO workspace_fx_rate_versions(
          workspace_fx_rate_version_id, workspace_id, tenant_id, version_number,
          usd_to_cny, effective_from, created_by_membership_id, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("fx_1", "workspace_a", "tenant_a", 1, "7.2", "1970-01-01T00:00:00Z", "member_a", "1970-01-01T00:00:00Z"),
    )
    _insert_entry(
        connection,
        amount_micros=144,
        request_id="req_usd_legacy",
        currency="USD",
        normalized_micros=None,
        normalization_status="unavailable",
        evidence_status="priced",
    )

    amounts = _load(connection, scope_type="tenant", scope_id="tenant_a")

    # 0.000144 USD × 7.2 = 0.0010368 CNY，按六位微元量化为 0.001037。
    assert amounts.posted_cny_micros == 1_037
    assert amounts.effective_cny_micros == 1_037


def test_spend_fails_closed_when_python_integer_total_exceeds_budget_bound() -> None:
    """逐行合法但累计越界的正式成本必须失败，不能依赖 SQLite SUM 或截断。"""

    connection = _connection()
    _insert_entry(
        connection,
        amount_micros=600_000_000_000_000,
        request_id="req_large_a",
    )
    _insert_entry(
        connection,
        amount_micros=600_000_000_000_000,
        request_id="req_large_b",
    )

    with pytest.raises(BudgetSpendUnavailableError):
        _load(connection, scope_type="tenant", scope_id="tenant_a")


def test_spend_adds_only_outstanding_reservations_using_formal_charge_boundary() -> None:
    """NULL 预留由正式 Charge 接管，显式 outstanding 仍叠加，裸 Usage 或调整均不结算。"""

    connection = _connection()
    _insert_entry(
        connection,
        amount_micros=1_000_000,
        request_id="req_settled",
    )
    _insert_entry(
        connection,
        amount_micros=2_000_000,
        request_id="req_explicit",
    )
    _insert_entry(
        connection,
        amount_micros=9_000_000,
        request_id="req_raw_only",
        entry_type="manual_debit",
        evidence_status="not_applicable",
    )
    connection.executemany(
        """
        INSERT INTO budget_reservations(
          tenant_id, budget_rule_id, budget_date, request_id,
          reserved_cost_cny, outstanding_cost_cny
        ) VALUES(?, ?, ?, ?, ?, ?)
        """,
        [
            ("tenant_a", "rule_project", "2026-07-27", "req_pending", "0.500000", None),
            ("tenant_a", "rule_project", "2026-07-27", "req_settled", "0.750000", None),
            ("tenant_a", "rule_project", "2026-07-27", "req_explicit", "1", "0.250000"),
            ("tenant_a", "rule_project", "2026-07-27", "req_raw_only", "0.125000", None),
            ("tenant_a", "rule_other", "2026-07-27", "req_other_rule", "9", None),
            ("tenant_b", "rule_project", "2026-07-27", "req_other_tenant", "10", None),
            ("tenant_a", "rule_project", "2026-07-26", "req_other_day", "11", None),
        ],
    )
    # 裸 Usage 已存在但没有 usage_charge 时仍不能释放预留；正式账本才是结算边界。
    connection.executemany(
        "INSERT INTO usage_ledger(tenant_id, request_id) VALUES(?, ?)",
        [("tenant_a", "req_raw_only"), ("tenant_a", "req_settled")],
    )

    amounts = _load(
        connection,
        scope_type="project",
        scope_id="project_a",
        budget_rule_id="rule_project",
    )
    decimal_reserved = load_outstanding_reserved_cny(
        connection,
        tenant_id="tenant_a",
        budget_rule_id="rule_project",
        budget_date="2026-07-27",
    )

    assert amounts.posted_cny_micros == 3_000_000
    assert amounts.reserved_cny_micros == 875_000
    assert amounts.effective_cny_micros == 3_875_000
    assert decimal_reserved == Decimal("0.875")


def test_spend_maps_invalid_reservation_numeric_to_unavailable_error() -> None:
    """超过六位小数的历史预留通过组合 helper 稳定收敛为预算不可用异常。"""

    connection = _connection()
    connection.execute(
        """
        INSERT INTO budget_reservations(
          tenant_id, budget_rule_id, budget_date, request_id,
          reserved_cost_cny, outstanding_cost_cny
        ) VALUES('tenant_a', 'rule_bad', '2026-07-27', 'req_bad', 0.0000001, NULL)
        """
    )

    with pytest.raises(BudgetSpendUnavailableError):
        _load(
            connection,
            scope_type="tenant",
            scope_id="tenant_a",
            budget_rule_id="rule_bad",
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"scope_type": "department", "scope_id": "dep_a"},
        {"scope_type": "tenant", "scope_id": "tenant_b"},
        {"environment_id": ""},
        {
            "start_utc": "2026-07-27T16:00:00Z",
            "end_utc": "2026-07-27T16:00:00Z",
        },
    ],
)
def test_spend_rejects_invalid_internal_scope_and_window(overrides: dict[str, str]) -> None:
    """内部空 Environment、伪租户 scope、未知层级和反向时间窗均失败关闭。"""

    connection = _connection()
    arguments = {
        "tenant_id": "tenant_a",
        "environment_id": "env_prod",
        "scope_type": "tenant",
        "scope_id": "tenant_a",
        "budget_rule_id": "rule_scope",
        "budget_date": "2026-07-27",
        "start_utc": "2026-07-26T16:00:00Z",
        "end_utc": "2026-07-27T16:00:00Z",
    }
    arguments.update(overrides)

    with pytest.raises(BudgetSpendUnavailableError):
        load_budget_spend_amounts(connection, **arguments)
