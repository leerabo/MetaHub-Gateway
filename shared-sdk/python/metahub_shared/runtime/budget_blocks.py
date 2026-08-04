"""模块职责：提供控制面与数据面共享的预算阻断生命周期持久化操作。

维护边界：只保存租户、规则、金额、原因和调用链元数据，不接收 Prompt、Response、Authorization 或任何 Key 明文。
来源文件：shared-sdk/python/metahub_shared/runtime/budget_blocks.py
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True)
class BudgetBlockRecord:
    """预算阻断生命周期的安全只读快照。

    该结构可进入控制面 DTO 或数据面决策，但不包含用户输入、认证头和密钥字段。
    """

    budget_block_id: str
    tenant_id: str
    environment_id: str
    budget_rule_id: str
    scope_type: str
    scope_id: str
    budget_date: str
    status: str
    blocked_cost_cny: str
    budget_cny: str
    blocked_request_id: str
    blocked_trace_id: str
    blocked_at: str
    release_reason: str | None
    released_by: str | None
    release_request_id: str | None
    release_trace_id: str | None
    released_at: str | None


def get_active_budget_block(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    budget_rule_id: str,
    budget_date: str,
) -> BudgetBlockRecord | None:
    """读取当前租户、规则和业务日唯一 active 阻断，不允许跨租户仅凭规则 ID 查询。"""

    row = connection.execute(
        """
        SELECT *
        FROM budget_blocks
        WHERE tenant_id=? AND budget_rule_id=? AND budget_date=? AND status='active'
        LIMIT 1
        """,
        (tenant_id, budget_rule_id, budget_date),
    ).fetchone()
    return _record_from_row(row) if row is not None else None


def ensure_budget_block(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    environment_id: str,
    budget_rule_id: str,
    scope_type: str,
    scope_id: str,
    budget_date: str,
    blocked_cost_cny: str,
    budget_cny: str,
    request_id: str,
    trace_id: str,
    now: str,
) -> tuple[BudgetBlockRecord, bool]:
    """幂等创建当日 active BudgetBlock，并在首次创建时写入 `budget.blocked` 事件。

    返回值第二项表示本调用是否真正创建；并发请求依靠部分唯一索引和 `INSERT OR IGNORE` 收敛为一条状态。
    """

    budget_block_id = f"bb_{uuid4().hex}"
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO budget_blocks(
          budget_block_id, tenant_id, environment_id, budget_rule_id, scope_type, scope_id,
          budget_date, status, blocked_cost_cny, budget_cny, blocked_request_id,
          blocked_trace_id, blocked_at, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            budget_block_id,
            tenant_id,
            environment_id,
            budget_rule_id,
            scope_type,
            scope_id,
            budget_date,
            blocked_cost_cny,
            budget_cny,
            request_id,
            trace_id,
            now,
            now,
            now,
        ),
    )
    created = cursor.rowcount == 1
    # 只有赢得并发插入的请求才能写 blocked 事件，重复拒绝不会制造事件风暴。
    if created:
        _insert_block_event(
            connection,
            budget_block_id=budget_block_id,
            tenant_id=tenant_id,
            environment_id=environment_id,
            budget_rule_id=budget_rule_id,
            event_type="budget.blocked",
            actor_id=None,
            request_id=request_id,
            trace_id=trace_id,
            reason_code="budget.exhausted.block",
            current_cost_cny=blocked_cost_cny,
            budget_cny=budget_cny,
            now=now,
        )
    record = get_active_budget_block(
        connection,
        tenant_id=tenant_id,
        budget_rule_id=budget_rule_id,
        budget_date=budget_date,
    )
    if record is None:
        # 插入后无法读取 active 状态表示事务内数据不一致，上层应失败关闭而不是继续调用 Provider。
        raise sqlite3.IntegrityError("budget_block_active_state_missing")
    return record, created


def release_active_budget_blocks(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    budget_rule_id: str,
    reason: str,
    actor_id: str | None,
    request_id: str,
    trace_id: str,
    now: str,
    exclude_budget_date: str | None = None,
) -> list[BudgetBlockRecord]:
    """事务内释放规则的 active 阻断并写入 `budget.unblocked` 事件。

    `exclude_budget_date` 用于数据面仅关闭历史业务日，控制面恢复操作不传该参数并释放全部 active 状态。
    """

    clauses = ["tenant_id=?", "budget_rule_id=?", "status='active'"]
    params: list[object] = [tenant_id, budget_rule_id]
    if exclude_budget_date is not None:
        # 新业务日只释放非当前日期的陈旧状态，不能误关刚由并发请求创建的当日 block。
        clauses.append("budget_date<>?")
        params.append(exclude_budget_date)
    rows = connection.execute(
        f"SELECT * FROM budget_blocks WHERE {' AND '.join(clauses)} ORDER BY blocked_at ASC",
        params,
    ).fetchall()
    released: list[BudgetBlockRecord] = []
    for row in rows:
        cursor = connection.execute(
            """
            UPDATE budget_blocks
            SET status='released', release_reason=?, released_by=?, release_request_id=?,
                release_trace_id=?, released_at=?, updated_at=?
            WHERE tenant_id=? AND budget_block_id=? AND status='active'
            """,
            (
                reason,
                actor_id,
                request_id or None,
                trace_id or None,
                now,
                now,
                tenant_id,
                row["budget_block_id"],
            ),
        )
        # 并发恢复时只有成功把 active 改为 released 的事务写 unblocked 事件。
        if cursor.rowcount != 1:
            continue
        _insert_block_event(
            connection,
            budget_block_id=row["budget_block_id"],
            tenant_id=tenant_id,
            environment_id=row["environment_id"],
            budget_rule_id=budget_rule_id,
            event_type="budget.unblocked",
            actor_id=actor_id,
            request_id=request_id,
            trace_id=trace_id,
            reason_code=reason,
            current_cost_cny=str(row["blocked_cost_cny"]),
            budget_cny=str(row["budget_cny"]),
            now=now,
        )
        updated = connection.execute(
            "SELECT * FROM budget_blocks WHERE tenant_id=? AND budget_block_id=?",
            (tenant_id, row["budget_block_id"]),
        ).fetchone()
        if updated is not None:
            released.append(_record_from_row(updated))
    return released


def _insert_block_event(
    connection: sqlite3.Connection,
    *,
    budget_block_id: str,
    tenant_id: str,
    environment_id: str,
    budget_rule_id: str,
    event_type: str,
    actor_id: str | None,
    request_id: str,
    trace_id: str,
    reason_code: str,
    current_cost_cny: str,
    budget_cny: str,
    now: str,
) -> None:
    """写入有唯一约束的阻断事件事实，payload 仅由安全结构化列组成。"""

    connection.execute(
        """
        INSERT OR IGNORE INTO budget_block_events(
          budget_block_event_id, event_id, tenant_id, environment_id, budget_block_id,
          budget_rule_id, event_type, actor_id, request_id, trace_id, reason_code,
          current_cost_cny, budget_cny, created_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"bbe_{uuid4().hex}",
            f"evt_{uuid4().hex}",
            tenant_id,
            environment_id,
            budget_block_id,
            budget_rule_id,
            event_type,
            actor_id,
            request_id or None,
            trace_id or None,
            reason_code,
            current_cost_cny,
            budget_cny,
            now,
        ),
    )


def _record_from_row(row: sqlite3.Row) -> BudgetBlockRecord:
    """把数据库行转换为稳定安全结构，防止调用方依赖表中未来新增的内部字段。"""

    return BudgetBlockRecord(
        budget_block_id=str(row["budget_block_id"]),
        tenant_id=str(row["tenant_id"]),
        environment_id=str(row["environment_id"]),
        budget_rule_id=str(row["budget_rule_id"]),
        scope_type=str(row["scope_type"]),
        scope_id=str(row["scope_id"]),
        budget_date=str(row["budget_date"]),
        status=str(row["status"]),
        blocked_cost_cny=str(row["blocked_cost_cny"]),
        budget_cny=str(row["budget_cny"]),
        blocked_request_id=str(row["blocked_request_id"]),
        blocked_trace_id=str(row["blocked_trace_id"]),
        blocked_at=str(row["blocked_at"]),
        release_reason=row["release_reason"],
        released_by=row["released_by"],
        release_request_id=row["release_request_id"],
        release_trace_id=row["release_trace_id"],
        released_at=row["released_at"],
    )
