"""控制面与数据面共享的正式账本预算金额聚合。

本模块只读取租户、Environment、预算 scope、正式 Usage Charge 与预算预留等安全财务
元数据，不接收或记录 Prompt、Response、Authorization、Provider Key、Virtual Key 明文。
正式金额和预留均先恢复为整数微元再逐行累计，避免 SQLite SUM 溢出或退化为浮点。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from metahub_shared.runtime.budget_reservations import load_outstanding_reserved_cny_micros

# 预算金额固定使用六位 CNY 微元；总额上限与既有十亿元日预算合同保持一致。
MICROS_PER_CNY = 1_000_000
MAX_BUDGET_CNY_MICROS = 1_000_000_000 * MICROS_PER_CNY
VALID_BUDGET_SCOPE_TYPES = frozenset({"tenant", "project", "application", "virtual_key"})
VALID_USAGE_PRICE_EVIDENCE = frozenset({"priced", "legacy_amount_only"})


class BudgetSpendUnavailableError(ValueError):
    """表示正式账本或预留事实无法形成可信 CNY 预算金额。

    该异常只携带固定内部原因，不拼接数据库值。Control 与 Gateway 必须分别把它映射为
    budget.status_unavailable 和 gateway.budget_unavailable，不能按零金额继续放行。
    """


@dataclass(frozen=True)
class BudgetSpendAmounts:
    """单条预算规则在同一数据库快照内的整数微元金额。

    字段均为非负且不超过预算金额上限；effective 是正式已入账模型消费与尚未结算
    预留之和。使用整数让 Gateway 比较、Control 聚合和 Webhook 事件共享同一口径。
    """

    # 已提交的正式 Usage Charge 毛成本，不含任何人工调整、赠送、试用或冲正分录。
    posted_cny_micros: int
    # 尚未被正式 Usage Charge 接管，或被显式标记为仍未知分项的 Provider 前预留。
    reserved_cny_micros: int
    # 预算判断使用的有效消耗，恒等于 posted 与 reserved 两项之和。
    effective_cny_micros: int


def load_budget_spend_amounts(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    environment_id: str,
    scope_type: str,
    scope_id: str,
    budget_rule_id: str,
    budget_date: str,
    start_utc: str,
    end_utc: str,
) -> BudgetSpendAmounts:
    """读取一条规则在上海业务日内的正式消费与未结算预留。

    参数：已由调用方开启一致性事务的 SQLite 连接，以及认证租户、精确 Environment、
    权威 scope、规则 ID、业务日和对应 UTC 半开区间。返回：三个整数微元金额。

    约束：租户和 scope 只能来自认证与数据库归属；本函数不提交或回滚调用方事务。任何
    未定价、未归一、类型漂移、金额越界或 scope 合同异常都失败关闭。
    """

    normalized_scope = _validate_scope(
        tenant_id=tenant_id,
        environment_id=environment_id,
        scope_type=scope_type,
        scope_id=scope_id,
        budget_rule_id=budget_rule_id,
        budget_date=budget_date,
        start_utc=start_utc,
        end_utc=end_utc,
    )
    posted = _load_posted_usage_cny_micros(
        connection,
        tenant_id=tenant_id,
        environment_id=environment_id,
        scope_type=normalized_scope,
        scope_id=scope_id,
        start_utc=start_utc,
        end_utc=end_utc,
    )
    try:
        reserved = load_outstanding_reserved_cny_micros(
            connection,
            tenant_id=tenant_id,
            budget_rule_id=budget_rule_id,
            budget_date=budget_date,
        )
    # 历史 NUMERIC 坏值也属于预算状态不可确认，统一收敛为 Shared 专用失败关闭异常。
    except ValueError as exc:
        raise BudgetSpendUnavailableError("budget_spend.reservation_amount_invalid") from exc
    effective = _bounded_add(posted, reserved, reason="budget_spend.effective_amount_invalid")
    return BudgetSpendAmounts(
        posted_cny_micros=posted,
        reserved_cny_micros=reserved,
        effective_cny_micros=effective,
    )


def _load_posted_usage_cny_micros(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    environment_id: str,
    scope_type: str,
    scope_id: str,
    start_utc: str,
    end_utc: str,
) -> int:
    """逐行累计当前租户、Environment、业务日和 scope 的正式 Usage Charge。

    参数均已由 load_budget_spend_amounts 校验。返回：有界非负整数微元。查询明确排除
    manual debit/credit、reversal、gift 和 trial，账务余额不得改变模型消费预算。
    """

    clauses = [
        "tenant_id=?",
        "environment_id=?",
        "created_at>=?",
        "created_at<?",
        "entry_type='usage_charge'",
        "direction='debit'",
        "status='posted'",
    ]
    params: list[object] = [tenant_id, environment_id, start_utc, end_utc]
    if scope_type == "project":
        clauses.append("project_id=?")
        params.append(scope_id)
    elif scope_type == "application":
        # Application 是 PRD-029 兼容维度，仍按正式账本写入时的历史快照聚合。
        clauses.append("application_id=?")
        params.append(scope_id)
    elif scope_type == "virtual_key":
        # Key 改绑后历史成本不能漂移，必须使用正式分录上的写入时 Virtual Key 快照。
        clauses.append("virtual_key_id=?")
        params.append(scope_id)

    rows = connection.execute(
        f"""
        SELECT normalized_cny_micros, cny_normalization_status, price_evidence_status
        FROM finops_ledger_entries
        WHERE {' AND '.join(clauses)}
        ORDER BY ledger_sequence ASC
        """,
        params,
    )
    total = 0
    for row in rows:
        normalization_status = row[1]
        evidence_status = row[2]
        # unpriced 虽以零金额入账，但代表价格未知；把它当免费会关闭预算保护。
        if (
            normalization_status != "normalized"
            or evidence_status not in VALID_USAGE_PRICE_EVIDENCE
        ):
            # 证据不完整时不能把零金额解释为免费，调用方必须在 Provider 前返回统一 503。
            raise BudgetSpendUnavailableError("budget_spend.usage_evidence_unavailable")
        amount = _bounded_micros(row[0], reason="budget_spend.usage_amount_invalid")
        total = _bounded_add(total, amount, reason="budget_spend.posted_total_invalid")
    return total


def _validate_scope(
    *,
    tenant_id: str,
    environment_id: str,
    scope_type: str,
    scope_id: str,
    budget_rule_id: str,
    budget_date: str,
    start_utc: str,
    end_utc: str,
) -> str:
    """校验内部预算查询边界，防止空租户或错误 scope 静默扩大正式账本查询。

    返回规范 scope 类型。调用方仍须在服务层先执行租户 owner 校验；这里是共享 helper
    的失败关闭防线，不替代 RBAC 或数据库触发器。
    """

    identifiers = (tenant_id, environment_id, scope_type, scope_id, budget_rule_id, budget_date)
    # 内部标识若为空或类型错误，继续查询可能把租户或 Environment 条件扩大为宽匹配。
    if any(not isinstance(value, str) or not value for value in identifiers):
        raise BudgetSpendUnavailableError("budget_spend.scope_invalid")
    # 未知层级不能降级成 tenant 查询，否则会把不相干的正式消费合并到同一额度。
    if scope_type not in VALID_BUDGET_SCOPE_TYPES:
        raise BudgetSpendUnavailableError("budget_spend.scope_invalid")
    # Tenant scope 的唯一权威 owner 是认证租户，禁止用其他 scope_id 形成伪租户规则。
    if scope_type == "tenant" and scope_id != tenant_id:
        raise BudgetSpendUnavailableError("budget_spend.scope_invalid")
    # 时间边界缺失或类型漂移时不能执行宽范围账本查询，统一按预算状态不可用处理。
    if (
        not isinstance(start_utc, str)
        or not isinstance(end_utc, str)
        or not start_utc
        or not end_utc
    ):
        # 缺失时间边界会扩大正式账本读取范围，必须失败关闭而不是使用隐式默认日。
        raise BudgetSpendUnavailableError("budget_spend.window_invalid")
    # 仓库时间统一为可按字典序比较的 UTC ISO 文本；反向窗口必须失败关闭。
    if start_utc >= end_utc:
        raise BudgetSpendUnavailableError("budget_spend.window_invalid")
    return scope_type


def _bounded_micros(value: object, *, reason: str) -> int:
    """恢复数据库非负整数微元，拒绝 bool、REAL、NULL、负数和单行越界。

    reason 只能由本模块固定传入；返回合法整数微元。异常不包含原始数据库值，避免
    损坏事实或潜在敏感信息进入日志与 API。
    """

    # bool 是 int 子类但不是合法财务事实；精确类型也会拒绝 SQLite REAL 的隐式截断。
    if type(value) is not int or value < 0 or value > MAX_BUDGET_CNY_MICROS:
        raise BudgetSpendUnavailableError(reason)
    return value


def _bounded_add(left: int, right: int, *, reason: str) -> int:
    """逐项执行有界整数加法，避免超大账本总额脱离预算金额合同。

    参数为已恢复的整数微元；返回不超过十亿元 CNY 的总额。Python 不会整数溢出，
    仍显式检查业务上限，防止损坏或异常大量事实进入准入比较和事件字段。
    """

    # 两个操作数仍做防御校验，避免未来调用点绕过单行金额恢复函数。
    if type(left) is not int or type(right) is not int or left < 0 or right < 0:
        raise BudgetSpendUnavailableError(reason)
    total = left + right
    # 合计超过业务金额上限不能截断为上限，否则控制台、事件和准入会展示不同事实。
    if total > MAX_BUDGET_CNY_MICROS:
        raise BudgetSpendUnavailableError(reason)
    return total


__all__ = [
    "BudgetSpendAmounts",
    "BudgetSpendUnavailableError",
    "MAX_BUDGET_CNY_MICROS",
    "MICROS_PER_CNY",
    "load_budget_spend_amounts",
]
