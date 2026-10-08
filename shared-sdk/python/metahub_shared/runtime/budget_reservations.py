"""模块职责：提供数据面预算准入预留的事务内持久化操作。

维护边界：预留仅保存租户、规则、请求链路和估算金额，不保存 Prompt、Response 或密钥明文。
来源文件：shared-sdk/python/metahub_shared/runtime/budget_reservations.py
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal, DecimalException, localcontext
from uuid import uuid4

from metahub_shared.db.schema import table_exists

# 预留沿用六位 CNY 微元和十亿元日预算上限，避免控制面与数据面各自解释 NUMERIC。
MICROS_PER_CNY = 1_000_000
MAX_BUDGET_CNY_MICROS = 1_000_000_000 * MICROS_PER_CNY


@dataclass(frozen=True)
class BudgetReservationRecord:
    """Provider 调用前创建的可审计预算预留，只包含安全成本和调用链元数据。"""

    budget_reservation_id: str
    tenant_id: str
    environment_id: str
    budget_rule_id: str
    scope_type: str
    scope_id: str
    budget_date: str
    request_id: str
    trace_id: str
    reserved_cost_cny: str
    # NULL 表示沿用“无 Usage 才计总预留”的历史语义；非 NULL 是终态确认后仍未知的分项。
    outstanding_cost_cny: str | None
    created_at: str


def load_outstanding_reserved_cny(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    budget_rule_id: str,
    budget_date: str,
) -> Decimal:
    """汇总尚未被正式 Usage Charge 替代的预留，并返回兼容 Decimal CNY。

    不主动删除已结算行，保留完整准入事实。历史 ``NULL`` 行只在同租户同 request 的正式
    Usage Charge 已提交后由真实成本接管；显式覆盖额即使已有备用 Usage Charge 仍须叠加。
    """

    micros = load_outstanding_reserved_cny_micros(
        connection,
        tenant_id=tenant_id,
        budget_rule_id=budget_rule_id,
        budget_date=budget_date,
    )
    return Decimal(micros) / Decimal(MICROS_PER_CNY)


def load_outstanding_reserved_cny_micros(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    budget_rule_id: str,
    budget_date: str,
    period_start_date: str | None = None,
) -> int:
    """逐行累计尚未结算的预留并返回有界整数微元。

    参数必须来自认证租户和已校验预算规则；返回值不会超过预算金额上限。正式结算边界是
    ``usage_charge/debit/posted``，不能仅凭可恢复但尚未正式投影的 Usage 行释放额度。
    """

    rows = connection.execute(
        """
        SELECT CASE
          WHEN r.outstanding_cost_cny IS NOT NULL THEN r.outstanding_cost_cny
          ELSE r.reserved_cost_cny
        END AS reserved_cny
        FROM budget_reservations r
        WHERE r.tenant_id=? AND r.budget_rule_id=?
          AND r.budget_date>=? AND r.budget_date<=?
          AND (
            r.outstanding_cost_cny IS NOT NULL
            OR NOT EXISTS (
              SELECT 1 FROM finops_ledger_entries entry
              WHERE entry.tenant_id=r.tenant_id
                AND entry.source_request_id=r.request_id
                AND entry.entry_type='usage_charge'
                AND entry.direction='debit'
                AND entry.status='posted'
            )
          )
        ORDER BY r.budget_reservation_id ASC
        """,
        (tenant_id, budget_rule_id, period_start_date or budget_date, budget_date),
    )
    total = 0
    for row in rows:
        amount = _reservation_cny_to_micros(row[0])
        total += amount
        # 大量异常预留不能越过统一金额边界；失败关闭比截断或浮点退化更安全。
        if total > MAX_BUDGET_CNY_MICROS:
            raise ValueError("budget reservation total invalid")
    # 0067 is optional during mixed-version rollout. Once present, only unallocated hold balance
    # plus active/possible allocations count; original hold totals would double count settled work.
    if table_exists(connection, "batch_budget_holds"):
        hold_rows = connection.execute(
            """
            SELECT remaining_cost_cny FROM batch_budget_holds
            WHERE tenant_id=? AND budget_rule_id=?
              AND budget_date>=? AND budget_date<=?
              AND status IN ('active','reconciliation')
            ORDER BY batch_budget_hold_id ASC
            """,
            (tenant_id, budget_rule_id, period_start_date or budget_date, budget_date),
        )
        for row in hold_rows:
            total += _reservation_cny_to_micros(row[0])
            if total > MAX_BUDGET_CNY_MICROS:
                # 预算预留总额必须是有限非负值；无效账本状态立即拒绝，不能静默修正。
                raise ValueError("budget reservation total invalid")
    if table_exists(connection, "batch_budget_allocations"):
        allocation_rows = connection.execute(
            """
            SELECT reserved_cost_cny FROM batch_budget_allocations
            WHERE tenant_id=? AND budget_rule_id=?
              AND budget_date>=? AND budget_date<=?
              AND status IN ('active','reconciliation')
            ORDER BY batch_budget_allocation_id ASC
            """,
            (tenant_id, budget_rule_id, period_start_date or budget_date, budget_date),
        )
        for row in allocation_rows:
            total += _reservation_cny_to_micros(row[0])
            if total > MAX_BUDGET_CNY_MICROS:
                # 预算预留总额必须是有限非负值；无效账本状态立即拒绝，不能静默修正。
                raise ValueError("budget reservation total invalid")
    return total


def _reservation_cny_to_micros(value: object) -> int:
    """把历史 NUMERIC 预留严格恢复为非负整数微元。

    SQLite NUMERIC 可能以 int、float 或文本返回，因此先使用其规范字符串构造 Decimal；
    拒绝科学计数、空白、NaN、Infinity、负数、超过六位小数和金额上限。
    """

    if isinstance(value, bool) or value is None:
        # bool/NULL 不是合法金额事实，不能依赖 Decimal 的宽松隐式转换按零处理。
        raise ValueError("budget reservation amount invalid")
    text = str(value)
    # 非规范文本会让不同 SQLite/Python 版本产生不同金额解释，必须在转换前拒绝。
    if not text or text != text.strip() or "e" in text.lower():
        raise ValueError("budget reservation amount invalid")
    try:
        with localcontext() as context:
            context.prec = 64
            amount = Decimal(text)
            # 乘到微元后必须已经是整数；不允许静默四舍五入历史坏值。
            scaled = amount * MICROS_PER_CNY
            integral = scaled.to_integral_value()
    # 数据库坏值统一收敛为固定内部异常，调用方不得把原始值写入日志或响应。
    except (DecimalException, TypeError, ValueError) as exc:
        raise ValueError("budget reservation amount invalid") from exc
    # 非有限、负数或超过六位小数都可能放宽预算，不能四舍五入或改写为零。
    if not amount.is_finite() or amount < 0 or scaled != integral:
        raise ValueError("budget reservation amount invalid")
    micros = int(integral)
    # 单条预留超过日预算业务上限表示事实损坏，必须交给服务边界返回统一 503。
    if micros > MAX_BUDGET_CNY_MICROS:
        raise ValueError("budget reservation amount invalid")
    return micros


def get_budget_reservation(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    budget_rule_id: str,
    request_id: str,
) -> BudgetReservationRecord | None:
    """按租户、规则和请求读取幂等预留，禁止仅凭 request_id 跨租户查询。"""

    row = connection.execute(
        """
        SELECT * FROM budget_reservations
        WHERE tenant_id=? AND budget_rule_id=? AND request_id=?
        """,
        (tenant_id, budget_rule_id, request_id),
    ).fetchone()
    return _record_from_row(row) if row is not None else None


def ensure_budget_reservation(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    environment_id: str,
    budget_rule_id: str,
    scope_type: str,
    scope_id: str,
    budget_date: str,
    request_id: str,
    trace_id: str,
    reserved_cost_cny: str,
    now: str,
) -> tuple[BudgetReservationRecord, bool]:
    """事务内幂等创建预留；同一请求重试不会重复占用同一规则预算。"""

    reservation_id = f"bres_{uuid4().hex}"
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO budget_reservations(
          budget_reservation_id, tenant_id, environment_id, budget_rule_id, scope_type,
          scope_id, budget_date, request_id, trace_id, reserved_cost_cny, created_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            reservation_id,
            tenant_id,
            environment_id,
            budget_rule_id,
            scope_type,
            scope_id,
            budget_date,
            request_id,
            trace_id,
            reserved_cost_cny,
            now,
        ),
    )
    record = get_budget_reservation(
        connection,
        tenant_id=tenant_id,
        budget_rule_id=budget_rule_id,
        request_id=request_id,
    )
    if record is None:
        # 插入后无法回读表示事务内预留状态不一致，上层必须失败关闭而不是继续调用 Provider。
        raise sqlite3.IntegrityError("budget_reservation_state_missing")
    return record, cursor.rowcount == 1


def release_request_budget_reservations(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    request_id: str,
    budget_rule_ids: set[str],
) -> int:
    """清理当前事务为请求新建的规则预留，不误删同 request_id 的历史幂等记录。"""

    if not budget_rule_ids:
        return 0
    placeholders = ", ".join("?" for _ in budget_rule_ids)
    cursor = connection.execute(
        f"""
        DELETE FROM budget_reservations
        WHERE tenant_id=? AND request_id=? AND budget_rule_id IN ({placeholders})
        """,
        (tenant_id, request_id, *sorted(budget_rule_ids)),
    )
    return cursor.rowcount


def set_request_budget_reservations_outstanding(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    request_id: str,
    outstanding_cost_cny: str,
) -> int:
    """按租户和请求原子记录终态后仍可能计费的上游分项。

    参数金额必须是调用方从已冻结价格和实际尝试状态得到的非负 CNY 文本；本函数不读取
    Prompt、Response 或 Provider Key。返回受影响规则预留数，供上层判断结算是否完整。
    """

    cursor = connection.execute(
        """
        UPDATE budget_reservations
        SET outstanding_cost_cny=?
        WHERE tenant_id=? AND request_id=?
        """,
        (outstanding_cost_cny, tenant_id, request_id),
    )
    return cursor.rowcount


def _record_from_row(row: sqlite3.Row) -> BudgetReservationRecord:
    """把数据库行收敛为不含请求正文的稳定结构。"""

    return BudgetReservationRecord(
        budget_reservation_id=str(row["budget_reservation_id"]),
        tenant_id=str(row["tenant_id"]),
        environment_id=str(row["environment_id"]),
        budget_rule_id=str(row["budget_rule_id"]),
        scope_type=str(row["scope_type"]),
        scope_id=str(row["scope_id"]),
        budget_date=str(row["budget_date"]),
        request_id=str(row["request_id"]),
        trace_id=str(row["trace_id"]),
        reserved_cost_cny=str(row["reserved_cost_cny"]),
        outstanding_cost_cny=(
            str(row["outstanding_cost_cny"])
            if row["outstanding_cost_cny"] is not None
            else None
        ),
        created_at=str(row["created_at"]),
    )
