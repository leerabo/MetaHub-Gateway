"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/budgets/service.py
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from uuid import uuid4
from zoneinfo import ZoneInfo

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.budget_blocks import (
    BudgetBlockRecord,
    get_active_budget_block,
    release_active_budget_blocks,
)
from metahub_shared.runtime.budget_scopes import (
    BUDGET_SCOPE_PRIORITIES,
    budget_scope_priority,
    build_budget_scope_id,
)
from metahub_shared.runtime.budget_spend import (
    MAX_BUDGET_CNY_MICROS,
    MICROS_PER_CNY,
    BudgetSpendUnavailableError,
    load_budget_spend_amounts,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.audit import (
    AuditRequestMeta,
    commit_audited_transaction,
    dispatch_audit_recorded,
    record_audit_log,
)
from metahub_control.auth import AuthContext
from metahub_control.auth.resource_transactions import ResourceWriteTransaction
from metahub_control.environments.service import EnvironmentBinding, resolve_environment_binding

from .models import (
    BudgetAggregateInfo,
    BudgetBlockInfo,
    BudgetEnvironmentSummary,
    BudgetPeopleResponse,
    BudgetPersonInfo,
    BudgetPersonKeyInfo,
    BudgetRuleInfo,
    BudgetRuleListResponse,
    BudgetRuleResponse,
    BudgetScopeInfo,
    BudgetScopeStatusSummary,
    BudgetStatusInfo,
    BudgetStatusResponse,
    CreateBudgetRuleRequest,
    UpdateBudgetModeRequest,
    UpdateBudgetRuleRequest,
)

logger = logging.getLogger("metahub.control.budgets")

# 预算规则的时区、金额精度、阈值和可编辑字段集中定义，确保成本治理判断口径一致。
SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_WARNING_THRESHOLD = 80
EXHAUSTED_THRESHOLD = 100
MAX_BUDGET_CNY = Decimal("1000000000")
MONEY_QUANT = Decimal("0.000001")
VALID_SCOPE_TYPES = {"tenant", "virtual_key", "project", "application"}
VALID_ACTIONS = {"alert_only", "block"}
VALID_STATUSES = {"active", "disabled"}
EDITABLE_FIELDS = {"name", "daily_budget_cny", "budget_period", "warning_threshold_percent", "exhausted_action", "status"}
CONFLICT_POLICY = "all_applicable_caps_deny_wins_v1"


@dataclass(frozen=True)
class _ScopeChainBinding:
    """预算状态查询使用的可信资源归属链。

    所有字段均来自当前 tenant_id 下的数据库查询；请求参数只能用于选择资源，不能覆盖
    Key 或 Application 的持久绑定。
    """

    environment: EnvironmentBinding
    project_id: str | None
    application_id: str | None
    virtual_key_id: str | None


def list_budget_people(
    config: AppConfig,
    context: AuthContext,
    *,
    search: str | None,
    user_id: str | None,
    offset: int,
    request_id: str,
    trace_id: str,
) -> BudgetPeopleResponse:
    """预算页的员工目录，服务端分页，不把大量员工或凭证放进下拉框。"""

    term = (search or "").strip().casefold()
    selected_id = (user_id or "").strip()
    if len(term) > 160 or len(selected_id) > 100 or (term and selected_id):
        raise ApiError("budget.rule_invalid")
    connection = _connect(config)
    try:
        _begin_read_transaction(connection)
        transaction = ResourceWriteTransaction(
            connection, config, context, "billing.budget.read", read_only=True
        )
        context = transaction.verify()
        clauses = ["u.tenant_id=?", "u.status='active'"]
        params: list[object] = [context.user.tenant_id]
        if selected_id:
            clauses.append("u.user_id=?")
            params.append(selected_id)
        elif term:
            clauses.append("(instr(lower(u.display_name), ?) > 0 OR instr(lower(u.email), ?) > 0)")
            params.extend([term, term])
        where = " AND ".join(clauses)
        total = int(connection.execute(
            f"SELECT COUNT(*) FROM users u WHERE {where}", params
        ).fetchone()[0])
        rows = connection.execute(
            f"""
            SELECT u.user_id, u.display_name, u.email,
                   (SELECT COUNT(*) FROM virtual_keys vk
                    WHERE vk.tenant_id=u.tenant_id AND vk.created_by=u.user_id
                      AND vk.deleted_at IS NULL) AS key_count
            FROM users u WHERE {where}
            ORDER BY u.display_name ASC, u.email ASC, u.user_id ASC
            LIMIT 20 OFFSET ?
            """,
            [*params, offset],
        ).fetchall()
        people: list[BudgetPersonInfo] = []
        for row in rows:
            keys: list[BudgetPersonKeyInfo] = []
            spend: dict[str, str] = {}
            if selected_id:
                key_rows = connection.execute(
                    """
                    SELECT vk.virtual_key_id, vk.name, vk.environment_id,
                           e.name AS environment_name
                    FROM virtual_keys vk
                    LEFT JOIN environments e
                      ON e.tenant_id=vk.tenant_id AND e.environment_id=vk.environment_id
                    WHERE vk.tenant_id=? AND vk.created_by=? AND vk.deleted_at IS NULL
                    ORDER BY vk.created_at DESC, vk.virtual_key_id ASC
                    """,
                    (context.user.tenant_id, selected_id),
                ).fetchall()
                keys = [BudgetPersonKeyInfo(**dict(key)) for key in key_rows]
                spend = _person_posted_spend(connection, context.user.tenant_id, selected_id)
            people.append(BudgetPersonInfo(
                user_id=row["user_id"],
                display_name=row["display_name"],
                email=row["email"],
                key_count=int(row["key_count"]),
                keys=keys,
                spent_day_cny=spend.get("day"),
                spent_month_cny=spend.get("month"),
                spent_year_cny=spend.get("year"),
            ))
        result = BudgetPeopleResponse(
            people=people, total=total, request_id=request_id, trace_id=trace_id
        )
        connection.rollback()
        transaction.begin()
        return result
    except ApiError:
        _rollback_safely(connection)
        raise
    except (sqlite3.Error, BudgetSpendUnavailableError) as exc:
        _rollback_safely(connection)
        raise ApiError("budget.status_unavailable") from exc
    finally:
        connection.close()


def _person_posted_spend(
    connection: sqlite3.Connection, tenant_id: str, user_id: str
) -> dict[str, str]:
    """按凭证的持久创建者聚合员工正式入账成本，已删除凭证也计入历史。"""

    now = _utcnow()
    starts = {period: _budget_window(now, period=period)[1] for period in ("day", "month", "year")}
    end = _budget_window(now, period="year")[2]
    totals = {period: 0 for period in starts}
    rows = connection.execute(
        """
        SELECT fle.created_at, fle.normalized_cny_micros,
               fle.cny_normalization_status, fle.price_evidence_status
        FROM finops_ledger_entries fle
        JOIN virtual_keys vk
          ON vk.tenant_id=fle.tenant_id AND vk.virtual_key_id=fle.virtual_key_id
        WHERE fle.tenant_id=? AND vk.created_by=?
          AND fle.created_at>=? AND fle.created_at<?
          AND fle.entry_type='usage_charge' AND fle.direction='debit'
          AND fle.status='posted'
        ORDER BY fle.ledger_sequence ASC
        """,
        (tenant_id, user_id, starts["year"], end),
    )
    for row in rows:
        amount = row["normalized_cny_micros"]
        if (
            type(amount) is not int or amount < 0 or amount > MAX_BUDGET_CNY_MICROS
            or row["cny_normalization_status"] != "normalized"
            or row["price_evidence_status"] not in ("priced", "legacy_amount_only")
        ):
            raise BudgetSpendUnavailableError("budget_person.spend_unavailable")
        for period, start in starts.items():
            if row["created_at"] >= start:
                totals[period] += amount
                if totals[period] > MAX_BUDGET_CNY_MICROS:
                    raise BudgetSpendUnavailableError("budget_person.spend_unavailable")
    return {
        period: _money_text(Decimal(total) / Decimal(MICROS_PER_CNY))
        for period, total in totals.items()
    }


def list_budget_rules(
    config: AppConfig,
    context: AuthContext,
    *,
    scope_type: str | None,
    environment_id: str | None,
    environment: str | None,
    status: str | None,
    request_id: str,
    trace_id: str,
) -> BudgetRuleListResponse:
    """在单一只读事务中返回当前租户规则、正式消费和 Environment 摘要。

    参数：筛选条件均来自严格路由，tenant_id 只取认证上下文。返回：包含三级规则状态、
    Application 兼容项和调用链 ID 的列表。无论成功或异常都关闭独享连接。
    """

    normalized_scope = _normalize_optional_scope_type(scope_type)
    normalized_status = _normalize_query_status(status)
    connection = _connect(config)
    try:
        _begin_read_transaction(connection)
        transaction = ResourceWriteTransaction(
            connection, config, context, "billing.budget.read", read_only=True
        )
        context = transaction.verify()
        clauses = ["br.tenant_id=?"]
        params: list[object] = [context.user.tenant_id]
        if normalized_scope:
            clauses.append("br.scope_type=?")
            params.append(normalized_scope)
        if normalized_status != "all":
            clauses.append("br.status=?")
            params.append(normalized_status)
        if environment_id or environment:
            # 环境筛选必须在当前只读快照和认证租户内解析，不能信任客户端 tenant_id。
            binding = resolve_environment_binding(
                connection,
                context.user.tenant_id,
                environment_id=environment_id,
                environment_code=environment,
                require_active=False,
                allow_default=False,
                mismatch_error="budget.scope_mismatch",
            )
            clauses.append("br.environment_id=?")
            params.append(binding.environment_id)
        rows = connection.execute(
            f"""
            SELECT br.*, e.code AS environment, e.name AS environment_name,
                   e.status AS environment_status
            FROM budget_rules br
            JOIN environments e
              ON e.tenant_id=br.tenant_id AND e.environment_id=br.environment_id
            WHERE {' AND '.join(clauses)}
            ORDER BY e.code ASC,
                     CASE br.scope_type
                       WHEN 'tenant' THEN 0 WHEN 'project' THEN 1
                       WHEN 'application' THEN 2 ELSE 3
                     END,
                     br.updated_at DESC, br.name ASC
            """,
            params,
        ).fetchall()
        rules = [_budget_rule_info(connection, row) for row in rows]
        result = BudgetRuleListResponse(
            budget_rules=rules,
            environment_summaries=_environment_summaries(rules),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.rollback()
        transaction.begin()
        return result
    # 列表业务错误只回滚并保留统一错误码，不能改写为基础设施故障。
    except ApiError:
        _rollback_safely(connection)
        raise
    # 正式账本金额或 SQLite 快照不可读时失败关闭，避免返回低估余额。
    except (BudgetSpendUnavailableError, sqlite3.Error) as exc:
        _rollback_safely(connection)
        raise ApiError("budget.status_unavailable") from exc
    finally:
        connection.close()


def create_budget_rule(
    config: AppConfig,
    context: AuthContext,
    payload: CreateBudgetRuleRequest,
    *,
    audit_request_meta: AuditRequestMeta | None = None,
    request_id: str,
    trace_id: str,
) -> BudgetRuleResponse:
    """create-only 创建当前租户预算规则；同 Environment/scope 已存在时返回 409。

    参数：认证上下文、严格 DTO、审计来源和调用链 ID。返回：提交后的规则安全 DTO。
    约束：数据库 UNIQUE 是并发创建的最终防线，原始 SQLite 文本不得出现在响应。
    """

    return _write_budget_rule(
        config,
        context,
        payload,
        create_only=True,
        audit_request_meta=audit_request_meta,
        request_id=request_id,
        trace_id=trace_id,
    )


def upsert_budget_rule(
    config: AppConfig,
    context: AuthContext,
    payload: UpdateBudgetRuleRequest,
    *,
    audit_request_meta: AuditRequestMeta | None = None,
    request_id: str,
    trace_id: str,
) -> BudgetRuleResponse:
    """兼容既有 PATCH upsert 语义创建或更新当前租户预算规则。

    所有 scope 和 Environment 都会在认证 tenant_id 下重新解析；客户端字段不能覆盖
    Key 的持久归属。写入、阻断恢复和审计记录共享一个 BEGIN IMMEDIATE 事务。
    """

    return _write_budget_rule(
        config,
        context,
        payload,
        create_only=False,
        audit_request_meta=audit_request_meta,
        request_id=request_id,
        trace_id=trace_id,
    )


def _write_budget_rule(
    config: AppConfig,
    context: AuthContext,
    payload: UpdateBudgetRuleRequest,
    *,
    create_only: bool,
    audit_request_meta: AuditRequestMeta | None,
    request_id: str,
    trace_id: str,
) -> BudgetRuleResponse:
    """执行 POST/PATCH 共用写事务，并把所有外部副作用延后到 commit 成功后。"""

    scope_type = _normalize_required_scope_type(payload.scope_type)
    connection = _connect(config)
    identity: tuple[str, str, str] | None = None
    released_blocks: list[BudgetBlockRecord] = []
    release_reason: str | None = None
    try:
        _begin_write_transaction(connection)
        transaction = ResourceWriteTransaction(
            connection, config, context, "billing.budget.write"
        )
        context = transaction.verify()
        derived_environment_id: str | None = None
        if (
            scope_type == "virtual_key"
            and not payload.environment_id
            and not payload.environment
            and _normalize_optional_text(payload.scope_id)
        ):
            # 未显式选 Environment 时必须从可信 Key 绑定恢复，不能误落到默认 dev。
            derived_environment_id, _project_id, _application_id = _load_virtual_key_binding(
                connection,
                context.user.tenant_id,
                str(payload.scope_id).strip(),
            )
        environment_binding = resolve_environment_binding(
            connection,
            context.user.tenant_id,
            environment_id=payload.environment_id or derived_environment_id,
            environment_code=payload.environment,
            mismatch_error="budget.scope_mismatch",
        )
        scope_id = _resolve_scope_id(
            connection,
            context.user.tenant_id,
            environment_binding.environment_id,
            scope_type,
            payload.scope_id,
        )
        identity = (environment_binding.environment_id, scope_type, scope_id)
        existing = _load_existing_rule(
            connection,
            context.user.tenant_id,
            environment_binding.environment_id,
            scope_type,
            scope_id,
        )
        if create_only and existing is not None:
            # POST 不具有 upsert 语义，不能让并发或误提交覆盖既有财务规则。
            raise ApiError("budget.rule_conflict")
        previous_budget = _decimal_value(existing["daily_budget_cny"]) if existing is not None else None
        previous_period = _row_budget_period(existing) if existing is not None else None
        provided = _provided_fields(payload, EDITABLE_FIELDS)
        if not provided:
            # create-only 缺配置与兼容 PATCH 空更新含义不同，必须返回各自稳定错误码。
            raise ApiError("budget.rule_invalid" if create_only else "budget.update_empty")

        now = _to_db_time(_utcnow())
        if existing is None:
            rule_id = f"br_{uuid4().hex}"
            values = _new_rule_values(
                connection,
                context,
                payload,
                environment_binding,
                scope_type,
                scope_id,
                provided,
                rule_id,
                now,
            )
            connection.execute(
                """
                INSERT INTO budget_rules(
                  budget_rule_id, tenant_id, environment_id, scope_type, scope_id, name,
                  daily_budget_cny, budget_period, warning_threshold_percent, exhausted_threshold_percent,
                  exhausted_action, status, created_by, updated_by, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
        else:
            updates, values = _update_rule_values(payload, provided)
            updates.extend(["updated_by=?", "updated_at=?"])
            values.extend(
                [
                    context.user.user_id,
                    now,
                    context.user.tenant_id,
                    environment_binding.environment_id,
                    scope_type,
                    scope_id,
                ]
            )
            connection.execute(
                f"""
                UPDATE budget_rules
                SET {', '.join(updates)}
                WHERE tenant_id=? AND environment_id=? AND scope_type=? AND scope_id=?
                """,
                values,
            )
        row = _load_existing_rule(
            connection,
            context.user.tenant_id,
            environment_binding.environment_id,
            scope_type,
            scope_id,
        )
        if row is None:
            # 写后回读缺失表示事务事实不完整，必须回滚且禁止产生成功副作用。
            raise ApiError("budget.status_unavailable")
        released_blocks, release_reason = _reconcile_blocks_after_rule_update(
            connection,
            context,
            row,
            provided,
            previous_budget=previous_budget,
            previous_period=previous_period,
            request_id=request_id,
            trace_id=trace_id,
        )
        audit_record = record_audit_log(
            connection,
            context,
            action="budget_rule.created" if existing is None else "budget_rule.updated",
            object_type="budget_rule",
            object_id=row["budget_rule_id"],
            object_name=row["name"],
            before_summary={} if existing is None else _budget_audit_summary(existing),
            after_summary=_budget_audit_summary(row),
            request_meta=_audit_request_meta(audit_request_meta),
            request_id=request_id,
            trace_id=trace_id,
        )
        # DTO 也在同一事务快照内物化；状态异常时先回滚，不能提交后才返回 503。
        response_rule = _budget_rule_info(connection, row)
        transaction.commit(commit_audited_transaction)
    except sqlite3.IntegrityError as exc:
        _rollback_safely(connection)
        if create_only and identity is not None:
            environment_id, conflict_scope_type, conflict_scope_id = identity
            try:
                recovery = ResourceWriteTransaction(
                    connection, config, context, "billing.budget.write", read_only=True
                )
                recovery.begin()
                conflict = _load_existing_rule(
                    connection,
                    context.user.tenant_id,
                    environment_id,
                    conflict_scope_type,
                    conflict_scope_id,
                )
                connection.rollback()
                recovery.begin()
            # 冲突恢复查询失败时不能泄露 SQLite 文本，只能退回统一不可用错误。
            except sqlite3.Error:
                conflict = None
            if conflict is not None:
                # 同一租户、环境和预算范围出现并发唯一键竞争时，必须转换为稳定的创建冲突错误。
                raise ApiError("budget.rule_conflict") from exc
        # 无法还原为当前租户既有规则的唯一约束异常必须隐藏数据库细节，统一返回服务不可用。
        raise ApiError("budget.status_unavailable") from exc
    # 已标准化的业务错误仍需回滚未提交事务，并原样保留统一错误码和 request_id。
    except ApiError:
        _rollback_safely(connection)
        raise
    # 金额或数据库异常统一返回可重试 503，原始 SQL 和路径不得外泄。
    except (BudgetSpendUnavailableError, sqlite3.Error) as exc:
        _rollback_safely(connection)
        raise ApiError("budget.status_unavailable") from exc
    except Exception:
        _rollback_safely(connection)
        raise
    finally:
        connection.close()

    # 审计、结构化日志和恢复日志都是外部副作用，只能看到已提交的规则事实。
    dispatch_audit_recorded(config, context, audit_record, request_id=request_id, trace_id=trace_id)
    for block in released_blocks:
        _log_budget_unblocked(
            context,
            block,
            release_reason or "below_threshold",
            request_id,
            trace_id,
        )
    _log_budget_updated(context, row, request_id, trace_id)
    return BudgetRuleResponse(
        budget_rule=response_rule,
        request_id=request_id,
        trace_id=trace_id,
    )


def update_budget_mode(
    config: AppConfig,
    context: AuthContext,
    budget_rule_id: str,
    payload: UpdateBudgetModeRequest,
    *,
    audit_request_meta: AuditRequestMeta | None = None,
    request_id: str,
    trace_id: str,
) -> BudgetRuleResponse:
    """按当前租户规则 ID 切换模式，并只释放该规则的 active block。

    路径 ID 与登录态 tenant_id 联合查询；写入、阻断事件和审计共享 BEGIN IMMEDIATE。
    连接在所有成功、业务异常和数据库异常路径中关闭，日志只在 commit 后产生。
    """

    rule_id = _normalize_budget_rule_id(budget_rule_id)
    mode = _normalize_mode(payload.mode)
    connection = _connect(config)
    released_blocks: list[BudgetBlockRecord] = []
    try:
        _begin_write_transaction(connection)
        transaction = ResourceWriteTransaction(
            connection, config, context, "billing.budget.write"
        )
        context = transaction.verify()
        row = _load_rule_by_id(connection, context.user.tenant_id, rule_id)
        if row is None:
            # 跨租户和不存在规则使用同一 404，不能通过响应差异枚举其他租户预算配置。
            raise ApiError("budget.rule_not_found")
        now = _to_db_time(_utcnow())
        connection.execute(
            """
            UPDATE budget_rules
            SET exhausted_action=?, updated_by=?, updated_at=?
            WHERE tenant_id=? AND budget_rule_id=?
            """,
            (mode, context.user.user_id, now, context.user.tenant_id, rule_id),
        )
        updated = _load_rule_by_id(connection, context.user.tenant_id, rule_id)
        if updated is None:
            # 锁内更新后无法按同租户回读表示事实不完整，禁止提交成功审计。
            raise ApiError("budget.status_unavailable")
        if mode == "alert_only":
            # 只释放目标规则，tenant/project/key 的其他 active block 必须继续生效。
            released_blocks = _release_rule_blocks(
                connection,
                context,
                updated,
                reason="mode_alert_only",
                request_id=request_id,
                trace_id=trace_id,
            )
        audit_record = record_audit_log(
            connection,
            context,
            action="budget_rule.updated",
            object_type="budget_rule",
            object_id=updated["budget_rule_id"],
            object_name=updated["name"],
            before_summary=_budget_audit_summary(row),
            after_summary=_budget_audit_summary(updated),
            request_meta=_audit_request_meta(audit_request_meta),
            request_id=request_id,
            trace_id=trace_id,
        )
        response_rule = _budget_rule_info(connection, updated)
        transaction.commit(commit_audited_transaction)
    # 规则不存在或模式非法时回滚同事务 block 释放，并保留业务错误码。
    except ApiError:
        _rollback_safely(connection)
        raise
    # 模式切换依赖完整金额和数据库事实，任一不可用都不能假装成功。
    except (BudgetSpendUnavailableError, sqlite3.Error) as exc:
        _rollback_safely(connection)
        raise ApiError("budget.status_unavailable") from exc
    except Exception:
        _rollback_safely(connection)
        raise
    finally:
        connection.close()

    dispatch_audit_recorded(config, context, audit_record, request_id=request_id, trace_id=trace_id)
    for block in released_blocks:
        _log_budget_unblocked(context, block, "mode_alert_only", request_id, trace_id)
    _log_budget_updated(context, updated, request_id, trace_id)
    return BudgetRuleResponse(
        budget_rule=response_rule,
        request_id=request_id,
        trace_id=trace_id,
    )


def get_budget_status(
    config: AppConfig,
    context: AuthContext,
    *,
    virtual_key_id: str | None,
    environment_id: str | None,
    project_id: str | None,
    application_id: str | None,
    request_id: str,
    trace_id: str,
) -> BudgetStatusResponse:
    """在单一只读事务中恢复可信 scope chain 并返回累计 AND 聚合状态。

    Key 和 Application 的 Project/Environment 绑定来自当前租户数据库。显式参数与持久
    绑定冲突时返回 budget.scope_mismatch；跨租户或不存在对象统一 scope_not_found。
    """

    connection = _connect(config)
    try:
        _begin_read_transaction(connection)
        transaction = ResourceWriteTransaction(
            connection, config, context, "billing.budget.read", read_only=True
        )
        context = transaction.verify()
        binding = _resolve_scope_chain(
            connection,
            context.user.tenant_id,
            virtual_key_id=virtual_key_id,
            environment_id=environment_id,
            project_id=project_id,
            application_id=application_id,
        )
        budget_date, _start, _end = _budget_window()
        clauses = ["br.tenant_id=?", "br.environment_id=?", "br.status='active'"]
        params: list[object] = [
            context.user.tenant_id,
            binding.environment.environment_id,
        ]
        scoped_conditions: list[str] = ["br.scope_type='tenant'"]
        if binding.project_id:
            scoped_conditions.append("(br.scope_type='project' AND br.scope_id=?)")
            params.append(binding.project_id)
        if binding.application_id:
            scoped_conditions.append("(br.scope_type='application' AND br.scope_id=?)")
            params.append(binding.application_id)
        if binding.virtual_key_id:
            scoped_conditions.append("(br.scope_type='virtual_key' AND br.scope_id=?)")
            params.append(binding.virtual_key_id)
        clauses.append("(" + " OR ".join(scoped_conditions) + ")")
        rows = connection.execute(
            f"""
            SELECT br.*, e.code AS environment, e.name AS environment_name,
                   e.status AS environment_status
            FROM budget_rules br
            JOIN environments e
              ON e.tenant_id=br.tenant_id AND e.environment_id=br.environment_id
            WHERE {' AND '.join(clauses)}
            ORDER BY CASE br.scope_type
                       WHEN 'tenant' THEN 0 WHEN 'project' THEN 1
                       WHEN 'application' THEN 2 ELSE 3
                     END,
                     br.budget_rule_id ASC
            """,
            params,
        ).fetchall()
        statuses = [_budget_status_info(connection, row) for row in rows]
        scope_chain = _scope_chain_infos(connection, context.user.tenant_id, binding)
        result = BudgetStatusResponse(
            budget_date=budget_date,
            statuses=statuses,
            aggregate=_budget_aggregate(statuses, scope_chain),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.rollback()
        transaction.begin()
        return result
    # 作用域 404/409 属于稳定业务结论，回滚快照后必须保留原错误码。
    except ApiError:
        _rollback_safely(connection)
        raise
    # 未知正式金额或数据库异常时不能返回部分作用链，统一失败关闭。
    except (BudgetSpendUnavailableError, sqlite3.Error) as exc:
        _rollback_safely(connection)
        raise ApiError("budget.status_unavailable") from exc
    finally:
        connection.close()


def _audit_request_meta(value: AuditRequestMeta | None) -> AuditRequestMeta:
    """为非 HTTP 调用补齐空来源字段，但不允许跳过预算审计。"""

    return value or AuditRequestMeta(client_ip=None, user_agent=None)


def _budget_audit_summary(row: sqlite3.Row) -> dict[str, object]:
    """构造预算规则审计白名单摘要，金额以定点字符串保存避免浮点漂移。"""

    return {
        "environment_id": row["environment_id"],
        "scope_type": row["scope_type"],
        "scope_id": row["scope_id"],
        "name": row["name"],
        "daily_budget_cny": _money_text(_decimal_value(row["daily_budget_cny"])),
        "budget_period": _row_budget_period(row),
        "warning_threshold_percent": int(row["warning_threshold_percent"]),
        "exhausted_action": row["exhausted_action"],
        "status": row["status"],
    }


def _connect(config: AppConfig) -> sqlite3.Connection:
    """创建预算管理数据库连接，并把基础设施异常映射成统一错误码。"""

    try:
        return connect_database(config.database_url)
    # 数据库异常需要折叠为统一错误码并保留 request_id，避免底层细节外泄。
    except DbError as exc:
        # 预算状态不可用会影响治理判断，控制面需要返回明确可重试错误。
        raise ApiError("budget.status_unavailable") from exc


def _begin_read_transaction(connection: sqlite3.Connection) -> None:
    """开启单一只读快照，防止列表规则、账本金额和摘要来自不同提交时刻。"""

    if getattr(connection, "backend", "sqlite") == "postgresql":
        connection.execute("BEGIN")
        connection.execute("SET TRANSACTION READ ONLY")
        return
    # SQLite query_only is reset by the connection pool on release.
    connection.execute("PRAGMA query_only = ON")
    connection.execute("BEGIN")


def _begin_write_transaction(connection: sqlite3.Connection) -> None:
    """开启 BEGIN IMMEDIATE，串行同一数据库上的预算规则写与数据面准入写。"""

    connection.execute("BEGIN IMMEDIATE")


def _rollback_safely(connection: sqlite3.Connection) -> None:
    """尽力回滚当前事务；原始回滚错误不得覆盖更有意义的预算域异常。"""

    try:
        if connection.in_transaction:
            connection.rollback()
    # 回滚异常不得覆盖原始业务错误；连接随后仍会在 finally 中关闭。
    except sqlite3.Error:
        # 连接随后会在 finally 中关闭，回滚失败只保留原始业务/数据库错误。
        return


def _normalize_required_scope_type(value: object | None) -> str:
    """校验必填预算 scope_type，只允许租户级和 Virtual Key 级预算。"""

    scope_type = str(value or "").strip().lower()
    if scope_type not in VALID_SCOPE_TYPES:
        # scope_type 决定 tenant_id 与 scope_id 的解释方式，非法值必须直接拒绝。
        raise ApiError("budget.rule_invalid")
    return scope_type


def _normalize_optional_scope_type(value: object | None) -> str | None:
    """校验可选 scope_type 查询条件，空值表示不过滤预算维度。"""

    scope_type = str(value or "").strip().lower()
    if not scope_type:
        return None
    if scope_type not in VALID_SCOPE_TYPES:
        # 查询条件同样不能接受未知 scope，避免前端误以为预算维度存在。
        raise ApiError("budget.rule_invalid")
    return scope_type


def _normalize_query_status(value: object | None) -> str:
    """归一化预算规则列表状态筛选条件，默认返回全部规则。"""

    status = str(value or "all").strip().lower()
    if status == "all":
        return "all"
    if status not in VALID_STATUSES:
        # 预算状态筛选只服务控制台配置页，非法值统一视为规则查询错误。
        raise ApiError("budget.rule_invalid")
    return status


def _resolve_scope_id(
    connection: sqlite3.Connection,
    tenant_id: str,
    environment_id: str,
    scope_type: str,
    raw_scope_id: str | None,
) -> str:
    """在认证租户内解析写请求 scope，并核对 Key 的权威 Environment。"""

    raw_normalized = _normalize_optional_text(raw_scope_id)
    # tenant scope 的 ID 只能来自认证上下文，显式异租户值必须按冲突拒绝。
    if scope_type == "tenant":
        # tenant scope 可省略 scope_id；显式提交时也只能等于认证租户。
        if raw_normalized is not None and raw_normalized != tenant_id:
            raise ApiError("budget.scope_mismatch")
        return tenant_id
    if not raw_normalized:
        # 非 tenant 规则必须提供不透明 scope ID，空值不能退回宽泛租户预算。
        raise ApiError("budget.rule_invalid")
    if scope_type == "virtual_key":
        key_environment_id, _project_id, _application_id = _load_virtual_key_binding(
            connection,
            tenant_id,
            raw_normalized,
        )
        if key_environment_id != environment_id:
            # Key 的权威 Environment 与表单选择不一致时拒绝写入，不能自动改绑。
            raise ApiError("budget.scope_mismatch")
        return raw_normalized
    if scope_type == "project":
        _ensure_project(connection, tenant_id, raw_normalized)
        return raw_normalized
    if scope_type == "application":
        _ensure_application(connection, tenant_id, raw_normalized, None)
        return raw_normalized
    # 固定 scope 枚举之外的类型不得进入规则写入或权威资源查询。
    raise ApiError("budget.rule_invalid")


def _load_virtual_key_binding(
    connection: sqlite3.Connection,
    tenant_id: str,
    virtual_key_id: str,
) -> tuple[str, str | None, str | None]:
    """按 tenant_id 读取 Key 的 Environment、Project 和 Application 权威绑定。

    参数只接受当前认证租户与不透明 Key ID；返回权威环境及可空父级 ID，跨租户与不存在
    使用同一 404，避免通过预算接口枚举其他租户 Key。
    """

    row = connection.execute(
        """
        SELECT virtual_key_id, environment_id, environment, project_id, application_id
        FROM virtual_keys
        WHERE tenant_id=? AND virtual_key_id=?
        """,
        (tenant_id, virtual_key_id),
    ).fetchone()
    if row is None:
        # 跨租户和不存在 Key 返回同一 404，不能用预算接口枚举其他租户凭证。
        raise ApiError("budget.scope_not_found")
    environment_id = str(row["environment_id"] or "")
    if not environment_id:
        # 兼容历史 Key：只按当前租户和旧 environment code 恢复，不跨租户猜测 ID。
        environment = resolve_environment_binding(
            connection,
            tenant_id,
            environment_id=None,
            environment_code=row["environment"],
            require_active=False,
            not_found_error="budget.scope_not_found",
        )
        environment_id = environment.environment_id
    return (
        environment_id,
        str(row["project_id"]) if row["project_id"] is not None else None,
        str(row["application_id"]) if row["application_id"] is not None else None,
    )


def _ensure_virtual_key(
    connection: sqlite3.Connection,
    tenant_id: str,
    virtual_key_id: str,
) -> str:
    """兼容内部调用：确认 Key 属于当前租户并返回其权威 Environment ID。"""

    environment_id, _project_id, _application_id = _load_virtual_key_binding(
        connection,
        tenant_id,
        virtual_key_id,
    )
    return environment_id


def _ensure_project(connection: sqlite3.Connection, tenant_id: str, project_id: str) -> None:
    """确认 Project 属于当前租户；不存在与跨租户统一返回 scope_not_found。"""

    row = connection.execute(
        "SELECT project_id FROM projects WHERE tenant_id=? AND project_id=?",
        (tenant_id, project_id),
    ).fetchone()
    if row is None:
        # 跨租户与不存在 Project 使用同形 404，防止资源存在性枚举。
        raise ApiError("budget.scope_not_found")


def _load_application_project(
    connection: sqlite3.Connection,
    tenant_id: str,
    application_id: str,
) -> str:
    """确认 Application 属于当前租户并返回其不可为空的 Project 绑定。"""

    row = connection.execute(
        "SELECT project_id FROM applications WHERE tenant_id=? AND application_id=?",
        (tenant_id, application_id),
    ).fetchone()
    if row is None:
        # 跨租户与不存在 Application 使用同形 404，防止资源存在性枚举。
        raise ApiError("budget.scope_not_found")
    return str(row["project_id"])


def _ensure_application(
    connection: sqlite3.Connection,
    tenant_id: str,
    application_id: str,
    project_id: str | None,
) -> None:
    """确认 Application 归属，并把显式 Project 冲突收敛为预算 409。"""

    application_project_id = _load_application_project(connection, tenant_id, application_id)
    if project_id and application_project_id != project_id:
        # Application 与显式 Project 不一致时按归属冲突返回，不能跨项目聚合。
        raise ApiError("budget.scope_mismatch")


def _resolve_scope_chain(
    connection: sqlite3.Connection,
    tenant_id: str,
    *,
    virtual_key_id: str | None,
    environment_id: str | None,
    project_id: str | None,
    application_id: str | None,
) -> _ScopeChainBinding:
    """恢复 GET /budget-status 的可信归属链并拒绝任意矛盾组合。"""

    key_id = _normalize_optional_text(virtual_key_id)
    explicit_project_id = _normalize_optional_text(project_id)
    explicit_application_id = _normalize_optional_text(application_id)
    key_environment_id: str | None = None
    resolved_project_id = explicit_project_id
    resolved_application_id = explicit_application_id

    if key_id:
        key_environment_id, key_project_id, key_application_id = _load_virtual_key_binding(
            connection,
            tenant_id,
            key_id,
        )
        # 即使显式资源存在，只要与 Key 的 NULL/具体绑定不相同也必须拒绝，不能拼接伪链。
        if explicit_project_id is not None and explicit_project_id != key_project_id:
            raise ApiError("budget.scope_mismatch")
        if explicit_application_id is not None and explicit_application_id != key_application_id:
            # 显式 Application 与 Key 绑定不一致时拒绝，避免拼接不存在的父链。
            raise ApiError("budget.scope_mismatch")
        resolved_project_id = key_project_id
        resolved_application_id = key_application_id

    if resolved_application_id:
        application_project_id = _load_application_project(
            connection,
            tenant_id,
            resolved_application_id,
        )
        if resolved_project_id is not None and resolved_project_id != application_project_id:
            # Application 恢复出的 Project 必须与显式 Project 相同，否则返回稳定冲突。
            raise ApiError("budget.scope_mismatch")
        resolved_project_id = application_project_id
    if resolved_project_id:
        _ensure_project(connection, tenant_id, resolved_project_id)

    environment = resolve_environment_binding(
        connection,
        tenant_id,
        environment_id=environment_id or key_environment_id,
        environment_code=None,
        require_active=False,
        not_found_error="budget.scope_not_found",
        mismatch_error="budget.scope_mismatch",
    )
    if key_environment_id and environment.environment_id != key_environment_id:
        # 显式 Environment 与 Key 权威绑定冲突时拒绝伪链，不能猜测调用方意图。
        raise ApiError("budget.scope_mismatch")
    return _ScopeChainBinding(
        environment=environment,
        project_id=resolved_project_id,
        application_id=resolved_application_id,
        virtual_key_id=key_id,
    )


def _load_existing_rule(
    connection: sqlite3.Connection,
    tenant_id: str,
    environment_id: str,
    scope_type: str,
    scope_id: str,
) -> sqlite3.Row | None:
    """读取当前租户某个 scope 的预算规则，供 upsert 判断创建或更新。"""

    return connection.execute(
        """
        SELECT br.*, e.code AS environment, e.name AS environment_name, e.status AS environment_status
        FROM budget_rules br
        JOIN environments e ON e.tenant_id=br.tenant_id AND e.environment_id=br.environment_id
        WHERE br.tenant_id=? AND br.environment_id=? AND br.scope_type=? AND br.scope_id=?
        """,
        (tenant_id, environment_id, scope_type, scope_id),
    ).fetchone()


def _load_rule_by_id(connection: sqlite3.Connection, tenant_id: str, budget_rule_id: str) -> sqlite3.Row | None:
    """按登录态租户与规则 ID 读取预算规则，供快捷模式更新使用。"""

    return connection.execute(
        """
        SELECT br.*, e.code AS environment, e.name AS environment_name, e.status AS environment_status
        FROM budget_rules br
        JOIN environments e ON e.tenant_id=br.tenant_id AND e.environment_id=br.environment_id
        WHERE br.tenant_id=? AND br.budget_rule_id=?
        """,
        (tenant_id, budget_rule_id),
    ).fetchone()


def _new_rule_values(
    connection: sqlite3.Connection,
    context: AuthContext,
    payload: UpdateBudgetRuleRequest,
    environment: EnvironmentBinding,
    scope_type: str,
    scope_id: str,
    provided: set[str],
    rule_id: str,
    now: str,
) -> tuple[object, ...]:
    """构造新预算规则插入参数，并为未填写字段提供 M0 默认值。"""

    if "daily_budget_cny" not in provided:
        # 新规则必须明确预算金额，避免创建出永远不会触发或无法解释的空预算。
        raise ApiError("budget.rule_invalid")
    budget = _normalize_money(payload.daily_budget_cny)
    name = _normalize_name(payload.name) if "name" in provided else _default_rule_name(connection, context.user.tenant_id, scope_type, scope_id)
    threshold = _normalize_warning_threshold(
        payload.warning_threshold_percent if "warning_threshold_percent" in provided else DEFAULT_WARNING_THRESHOLD
    )
    action = _normalize_action(payload.exhausted_action if "exhausted_action" in provided else "alert_only")
    status = _normalize_status(payload.status if "status" in provided else "active")
    return (
        rule_id,
        context.user.tenant_id,
        environment.environment_id,
        scope_type,
        scope_id,
        name,
        _money_text(budget),
        payload.budget_period or "day",
        threshold,
        EXHAUSTED_THRESHOLD,
        action,
        status,
        context.user.user_id,
        context.user.user_id,
        now,
        now,
    )


def _update_rule_values(
    payload: UpdateBudgetRuleRequest,
    provided: set[str],
) -> tuple[list[str], list[object]]:
    """构造预算规则更新 SQL 片段，集中校验金额、阈值、策略和状态。"""

    updates: list[str] = []
    values: list[object] = []
    if "name" in provided:
        updates.append("name=?")
        values.append(_normalize_name(payload.name))
    if "daily_budget_cny" in provided:
        updates.append("daily_budget_cny=?")
        values.append(_money_text(_normalize_money(payload.daily_budget_cny)))
    if "budget_period" in provided:
        updates.append("budget_period=?")
        values.append(payload.budget_period or "day")
    if "warning_threshold_percent" in provided:
        updates.append("warning_threshold_percent=?")
        values.append(_normalize_warning_threshold(payload.warning_threshold_percent))
    if "exhausted_action" in provided:
        updates.append("exhausted_action=?")
        values.append(_normalize_action(payload.exhausted_action))
    if "status" in provided:
        updates.append("status=?")
        values.append(_normalize_status(payload.status))
    return updates, values


def _provided_fields(payload: UpdateBudgetRuleRequest, allowed: set[str]) -> set[str]:
    """读取 Pydantic 已显式提供字段，兼容 v1/v2 字段集合名称。"""

    if hasattr(payload, "model_fields_set"):
        raw_fields = payload.model_fields_set
    else:
        raw_fields = getattr(payload, "__fields_set__", set())
    return {field for field in raw_fields if field in allowed}


def _normalize_name(value: object | None) -> str:
    """校验预算规则名称，避免空标题或超长名称撑破控制台表格。"""

    name = str(value or "").strip()
    if not name or len(name) > 120:
        # 名称会展示在预算设置页，必须有清晰短标签。
        raise ApiError("budget.rule_invalid")
    return name


def _normalize_money(value: object | None) -> Decimal:
    """校验请求预算金额，只接受普通十进制正数且最多六位小数。"""

    if isinstance(value, str) and ("e" in value.lower()):
        # 请求金额禁止科学计数法，确保管理端确认值与数据库定点值完全一致。
        raise ApiError("budget.rule_invalid")
    try:
        amount = Decimal(str(value))
    # 金额解析失败统一折叠为规则错误，不能把解析器细节暴露给客户端。
    except (InvalidOperation, ValueError) as exc:
        raise ApiError("budget.rule_invalid") from exc
    if not amount.is_finite() or amount <= 0 or amount > MAX_BUDGET_CNY:
        # 非有限、非正数或超上限金额不能进入预算规则，避免绕过阻断。
        raise ApiError("budget.rule_invalid")
    quantized = amount.quantize(MONEY_QUANT)
    if amount != quantized:
        # 财务输入不能静默四舍五入，否则用户确认值会与实际阻断值不同。
        raise ApiError("budget.rule_invalid")
    return quantized


def _normalize_warning_threshold(value: object | None) -> int:
    """校验预算告警阈值，M0 默认 80%，但保留 1-99 的扩展空间。"""

    if isinstance(value, bool) or not isinstance(value, int):
        # bool 是 int 子类，必须排除，避免 true 被当成 1% 阈值。
        raise ApiError("budget.rule_invalid")
    if value <= 0 or value >= EXHAUSTED_THRESHOLD:
        # 告警阈值必须低于 100%，100% 固定表示耗尽阈值。
        raise ApiError("budget.rule_invalid")
    return value


def _normalize_action(value: object | None) -> str:
    """校验预算耗尽后的策略，只允许仅告警或阻断。"""

    action = str(value or "").strip().lower()
    if action not in VALID_ACTIONS:
        # 动作直接影响数据面是否放行请求，非法值必须拒绝。
        raise ApiError("budget.rule_invalid")
    return action


def _normalize_mode(value: object | None) -> str:
    """校验快捷切换模式，使用独立错误码区分完整规则 payload 错误。"""

    mode = str(value or "").strip().lower()
    if mode not in VALID_ACTIONS:
        # mode 直接改变数据面阻断行为，空值和未知动作必须显式拒绝。
        raise ApiError("budget.mode_invalid")
    return mode


def _normalize_budget_rule_id(value: object | None) -> str:
    """校验路径中的预算规则 ID，限制长度并避免空 ID 进入数据库查询。"""

    rule_id = str(value or "").strip()
    if not rule_id or len(rule_id) > 128:
        # 非法路径 ID 与不存在规则使用统一 not found，避免暴露内部 ID 格式细节。
        raise ApiError("budget.rule_not_found")
    return rule_id


def _normalize_status(value: object | None) -> str:
    """校验预算规则启停状态，确保数据面只处理明确启用或停用的规则。"""

    status = str(value or "").strip().lower()
    if status not in VALID_STATUSES:
        # 预算状态只允许 active/disabled，避免半启用状态在数据面产生歧义。
        raise ApiError("budget.rule_invalid")
    return status


def _normalize_optional_text(value: object | None) -> str | None:
    """归一化可选字符串，空字符串统一视为 None。"""

    text = str(value or "").strip()
    return text or None


def _default_rule_name(
    connection: sqlite3.Connection,
    tenant_id: str,
    scope_type: str,
    scope_id: str,
) -> str:
    """根据预算维度生成默认规则名，保证新规则在控制台可读。"""

    # 租户判断决定数据隔离边界，不能跨租户复用或返回对象。
    if scope_type == "tenant":
        return "租户日预算"
    if scope_type == "project":
        row = connection.execute("SELECT name FROM projects WHERE tenant_id=? AND project_id=?", (tenant_id, scope_id)).fetchone()
        return row["name"] if row else scope_id
    if scope_type == "application":
        row = connection.execute("SELECT a.name, p.name AS project_name FROM applications a LEFT JOIN projects p ON p.tenant_id=a.tenant_id AND p.project_id=a.project_id WHERE a.tenant_id=? AND a.application_id=?", (tenant_id, scope_id)).fetchone()
        return f"{row['project_name']} / {row['name']}" if row else scope_id
    row = connection.execute(
        "SELECT name, key_prefix FROM virtual_keys WHERE tenant_id=? AND virtual_key_id=?",
        (tenant_id, scope_id),
    ).fetchone()
    if row is None:
        # 关键控制流影响权限、审计或错误归因，必须保留统一错误语义。
        return "Virtual Key日预算"
    return f"{row['name']} 日预算"


def _budget_rule_info(connection: sqlite3.Connection, row: sqlite3.Row) -> BudgetRuleInfo:
    """把规则行转换为含 BudgetScope 和正式账本拆分的控制面 DTO。"""

    scope = _budget_scope_info(connection, row)
    status = _budget_status_info(connection, row, scope=scope)
    block = _active_block(connection, row, status.budget_date)
    return BudgetRuleInfo(
        budget_rule_id=row["budget_rule_id"],
        tenant_id=row["tenant_id"],
        environment_id=row["environment_id"],
        environment=row["environment"],
        environment_name=row["environment_name"],
        scope_type=row["scope_type"],
        scope_id=row["scope_id"],
        scope_label=scope.scope_label,
        budget_scope=scope,
        name=row["name"],
        daily_budget_cny=_money_text(_decimal_value(row["daily_budget_cny"])),
        budget_period=_row_budget_period(row),
        warning_threshold_percent=int(row["warning_threshold_percent"]),
        exhausted_threshold_percent=int(row["exhausted_threshold_percent"]),
        exhausted_action=row["exhausted_action"],
        status=row["status"],
        today_status=status,
        active_block=_budget_block_info(block) if block is not None else None,
        created_at=_parse_db_time(row["created_at"]),
        updated_at=_parse_db_time(row["updated_at"]),
    )


def _budget_status_info(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    scope: BudgetScopeInfo | None = None,
) -> BudgetStatusInfo:
    """按正式 Usage Charge 加 outstanding reservation 计算单规则今日状态。

    所有累计和阈值比较使用整数微元；spent_cny 为兼容字段，继续只表示正式已入账金额。
    未定价、未归一或损坏金额由共享 helper 失败关闭，不能按零继续展示。
    """

    period = _row_budget_period(row)
    budget_date, start_utc, end_utc = _budget_window(period=period)
    amounts = load_budget_spend_amounts(
        connection,
        tenant_id=str(row["tenant_id"]),
        environment_id=str(row["environment_id"]),
        scope_type=str(row["scope_type"]),
        scope_id=str(row["scope_id"]),
        budget_rule_id=str(row["budget_rule_id"]),
        budget_date=budget_date,
        budget_period=period,
        start_utc=start_utc,
        end_utc=end_utc,
    )
    budget_micros = _database_money_micros(row["daily_budget_cny"], allow_zero=False)
    percent = _usage_percent_micros(amounts.effective_cny_micros, budget_micros)
    block = _active_block(connection, row, budget_date)
    if block is not None:
        # active block 是粘性事实，不能因 reservation 波动或人工贷记自动恢复。
        status = "exhausted"
        next_action = "block"
    elif amounts.effective_cny_micros >= budget_micros:
        status = "exhausted"
        next_action = "block" if row["exhausted_action"] == "block" else "allow"
    elif (
        amounts.effective_cny_micros * EXHAUSTED_THRESHOLD
        >= budget_micros * int(row["warning_threshold_percent"])
    ):
        status = "threshold_reached"
        next_action = "warn"
    else:
        status = "ok"
        next_action = "allow"
    remaining_micros = max(budget_micros - amounts.effective_cny_micros, 0)
    budget_scope = scope or _budget_scope_info(connection, row)
    return BudgetStatusInfo(
        budget_rule_id=row["budget_rule_id"],
        environment_id=row["environment_id"],
        environment=row["environment"],
        environment_name=row["environment_name"],
        scope_type=row["scope_type"],
        scope_id=row["scope_id"],
        scope_label=budget_scope.scope_label,
        budget_scope=budget_scope,
        budget_date=budget_date,
        budget_period=period,
        ledger_spent_cny=_micros_text(amounts.posted_cny_micros),
        reserved_cny=_micros_text(amounts.reserved_cny_micros),
        effective_spend_cny=_micros_text(amounts.effective_cny_micros),
        effective_remaining_cny=_micros_text(remaining_micros),
        spent_cny=_micros_text(amounts.posted_cny_micros),
        estimated_remaining_cny=_micros_text(remaining_micros),
        budget_cny=_micros_text(budget_micros),
        usage_percent=percent,
        status=status,
        exhausted_action=row["exhausted_action"],
        next_action=next_action,
        last_event_type=_last_event_type(
            connection,
            row["tenant_id"],
            row["budget_rule_id"],
            budget_date,
        ),
        block_status=block.status if block is not None else None,
        budget_block_id=block.budget_block_id if block is not None else None,
        blocked_at=_parse_db_time(block.blocked_at) if block is not None else None,
    )


def _budget_scope_info(connection: sqlite3.Connection, row: sqlite3.Row) -> BudgetScopeInfo:
    """从规则行生成稳定 BudgetScope value object，并验证父资源仍属于当前租户。"""

    return _scope_info(
        connection,
        tenant_id=str(row["tenant_id"]),
        environment_id=str(row["environment_id"]),
        scope_type=str(row["scope_type"]),
        scope_id=str(row["scope_id"]),
    )


def _scope_info(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    environment_id: str,
    scope_type: str,
    scope_id: str,
) -> BudgetScopeInfo:
    """按可信身份生成 BudgetScope；现存规则若与主数据漂移则失败关闭。"""

    try:
        priority = budget_scope_priority(scope_type)
        budget_scope_id = build_budget_scope_id(
            tenant_id=tenant_id,
            environment_id=environment_id,
            scope_type=scope_type,
            scope_id=scope_id,
        )
    # 稳定 scope ID 或优先级构造失败表示持久事实损坏，不能返回部分 DTO。
    except ValueError as exc:
        raise BudgetSpendUnavailableError("budget_scope.invalid") from exc
    parent_scope_type: str | None = None
    parent_scope_id: str | None = None
    # 每类 scope 都从同租户权威主数据验证；tenant scope 还必须由当前租户自身拥有。
    if scope_type == "tenant":
        if scope_id != tenant_id:
            raise BudgetSpendUnavailableError("budget_scope.tenant_owner_mismatch")
    elif scope_type == "project":
        _ensure_project(connection, tenant_id, scope_id)
        parent_scope_type, parent_scope_id = "tenant", tenant_id
    elif scope_type == "application":
        parent_scope_type = "project"
        parent_scope_id = _load_application_project(connection, tenant_id, scope_id)
    elif scope_type == "virtual_key":
        key_environment_id, project_id, application_id = _load_virtual_key_binding(
            connection,
            tenant_id,
            scope_id,
        )
        if key_environment_id != environment_id:
            # Key 的权威 Environment 与规则不一致表示事实漂移，状态聚合必须失败关闭。
            raise BudgetSpendUnavailableError("budget_scope.environment_mismatch")
        if application_id:
            parent_scope_type, parent_scope_id = "application", application_id
        elif project_id:
            parent_scope_type, parent_scope_id = "project", project_id
        else:
            parent_scope_type, parent_scope_id = "tenant", tenant_id
    return BudgetScopeInfo(
        budget_scope_id=budget_scope_id,
        tenant_id=tenant_id,
        environment_id=environment_id,
        scope_type=scope_type,
        scope_id=scope_id,
        scope_label=_scope_label(connection, tenant_id, scope_type, scope_id),
        priority=priority,
        parent_scope_type=parent_scope_type,
        parent_scope_id=parent_scope_id,
    )


def _scope_chain_infos(
    connection: sqlite3.Connection,
    tenant_id: str,
    binding: _ScopeChainBinding,
) -> list[BudgetScopeInfo]:
    """按 tenant/project/application/virtual_key 固定顺序物化完整可信链。"""

    identities: list[tuple[str, str]] = [("tenant", tenant_id)]
    if binding.project_id:
        identities.append(("project", binding.project_id))
    if binding.application_id:
        identities.append(("application", binding.application_id))
    if binding.virtual_key_id:
        identities.append(("virtual_key", binding.virtual_key_id))
    return [
        _scope_info(
            connection,
            tenant_id=tenant_id,
            environment_id=binding.environment.environment_id,
            scope_type=scope_type,
            scope_id=scope_id,
        )
        for scope_type, scope_id in identities
    ]


def _environment_summaries(rules: list[BudgetRuleInfo]) -> list[BudgetEnvironmentSummary]:
    """按 Environment 汇总规则状态计数，不对跨层金额求和。"""

    grouped: dict[tuple[str, str, str, str], list[BudgetRuleInfo]] = {}
    for rule in rules:
        key = (
            rule.environment_id,
            rule.environment,
            rule.environment_name,
            rule.today_status.budget_date,
        )
        grouped.setdefault(key, []).append(rule)
    summaries: list[BudgetEnvironmentSummary] = []
    for key in sorted(grouped, key=lambda item: (item[1], item[0])):
        environment_id, environment, environment_name, budget_date = key
        active_rules = [rule for rule in grouped[key] if rule.status == "active"]
        scope_summaries: list[BudgetScopeStatusSummary] = []
        for scope_type in sorted(BUDGET_SCOPE_PRIORITIES, key=budget_scope_priority):
            scoped = [rule for rule in active_rules if rule.scope_type == scope_type]
            scope_summaries.append(
                BudgetScopeStatusSummary(
                    scope_type=scope_type,
                    rule_count=len(scoped),
                    ok_count=sum(rule.today_status.status == "ok" for rule in scoped),
                    warning_count=sum(
                        rule.today_status.status == "threshold_reached" for rule in scoped
                    ),
                    exhausted_count=sum(
                        rule.today_status.status == "exhausted" for rule in scoped
                    ),
                    block_count=sum(
                        rule.today_status.next_action == "block" for rule in scoped
                    ),
                )
            )
        summaries.append(
            BudgetEnvironmentSummary(
                environment_id=environment_id,
                environment=environment,
                environment_name=environment_name,
                budget_date=budget_date,
                scope_summaries=scope_summaries,
            )
        )
    return summaries


def _budget_aggregate(
    statuses: list[BudgetStatusInfo],
    scope_chain: list[BudgetScopeInfo],
) -> BudgetAggregateInfo:
    """按 deny-wins 和稳定余额/层级顺序聚合一条 scope chain。"""

    if not statuses:
        return BudgetAggregateInfo(
            aggregate_status="unconfigured",
            effective_action="allow",
            effective_remaining_cny=None,
            limiting_rule_id=None,
            limiting_scope=None,
            conflict_policy=CONFLICT_POLICY,
            scope_chain=scope_chain,
        )
    if any(status.status == "exhausted" for status in statuses):
        aggregate_status = "exhausted"
    elif any(status.status == "threshold_reached" for status in statuses):
        aggregate_status = "threshold_reached"
    else:
        aggregate_status = "ok"
    if any(status.next_action == "block" for status in statuses):
        effective_action = "block"
    elif any(status.next_action == "warn" for status in statuses):
        effective_action = "warn"
    else:
        effective_action = "allow"
    # limiting 表示纯容量瓶颈：粘性 active block 只决定总体阻断，不能掩盖余额更小的其他层级。
    limiting = min(
        statuses,
        key=lambda status: (
            _database_money_micros(status.effective_remaining_cny, allow_zero=True),
            status.budget_scope.priority,
            status.budget_rule_id,
        ),
    )
    return BudgetAggregateInfo(
        aggregate_status=aggregate_status,
        effective_action=effective_action,
        effective_remaining_cny=limiting.effective_remaining_cny,
        limiting_rule_id=limiting.budget_rule_id,
        limiting_scope=limiting.budget_scope,
        conflict_policy=CONFLICT_POLICY,
        scope_chain=scope_chain,
    )


def _load_spent_cny(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    start_utc: str,
    end_utc: str,
) -> Decimal:
    """兼容阻断恢复调用：只返回正式 Usage Charge 金额，不包含 reservation。"""

    budget_date, _current_start, _current_end = _budget_window(period=_row_budget_period(row))
    amounts = load_budget_spend_amounts(
        connection,
        tenant_id=str(row["tenant_id"]),
        environment_id=str(row["environment_id"]),
        scope_type=str(row["scope_type"]),
        scope_id=str(row["scope_id"]),
        budget_rule_id=str(row["budget_rule_id"]),
        budget_date=budget_date,
        budget_period=_row_budget_period(row),
        start_utc=start_utc,
        end_utc=end_utc,
    )
    return _micros_decimal(amounts.posted_cny_micros)


def _active_block(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    budget_date: str,
) -> BudgetBlockRecord | None:
    """按当前租户、规则和业务日读取 active block，供状态 DTO 与恢复判断复用。"""

    return get_active_budget_block(
        connection,
        tenant_id=row["tenant_id"],
        budget_rule_id=row["budget_rule_id"],
        budget_date=budget_date,
    )


def _budget_block_info(block: BudgetBlockRecord) -> BudgetBlockInfo:
    """把共享阻断记录转换为控制面 DTO，金额保持字符串避免 JSON 浮点误差。"""

    return BudgetBlockInfo(
        budget_block_id=block.budget_block_id,
        budget_rule_id=block.budget_rule_id,
        environment_id=block.environment_id,
        scope_type=block.scope_type,
        scope_id=block.scope_id,
        budget_date=block.budget_date,
        status=block.status,
        blocked_cost_cny=_money_text(_decimal_value(block.blocked_cost_cny)),
        budget_cny=_money_text(_decimal_value(block.budget_cny)),
        blocked_request_id=block.blocked_request_id,
        blocked_trace_id=block.blocked_trace_id,
        blocked_at=_parse_db_time(block.blocked_at),
    )


def _reconcile_blocks_after_rule_update(
    connection: sqlite3.Connection,
    context: AuthContext,
    row: sqlite3.Row,
    provided: set[str],
    *,
    previous_budget: Decimal | None,
    previous_period: str | None,
    request_id: str,
    trace_id: str,
) -> tuple[list[BudgetBlockRecord], str | None]:
    """规则写入后按显式变更字段决定是否解除 active block。

    名称或阈值等无关更新不能意外解封；只有停用、切换仅告警或明确提额才改变阻断生命周期。
    """

    if "status" in provided and row["status"] == "disabled":
        return _release_rule_blocks(
            connection,
            context,
            row,
            reason="rule_disabled",
            request_id=request_id,
            trace_id=trace_id,
        ), "rule_disabled"
    if "exhausted_action" in provided and row["exhausted_action"] == "alert_only":
        return _release_rule_blocks(
            connection,
            context,
            row,
            reason="mode_alert_only",
            request_id=request_id,
            trace_id=trace_id,
        ), "mode_alert_only"
    if previous_period is not None and _row_budget_period(row) != previous_period:
        _budget_date, start_utc, end_utc = _budget_window(period=_row_budget_period(row))
        spent = _load_spent_cny(connection, row, start_utc, end_utc)
        if spent < _decimal_value(row["daily_budget_cny"]):
            return _release_rule_blocks(
                connection, context, row, reason="below_threshold",
                request_id=request_id, trace_id=trace_id,
            ), "below_threshold"
        return [], None
    if "daily_budget_cny" not in provided:
        return [], None
    _budget_date, start_utc, end_utc = _budget_window(period=_row_budget_period(row))
    spent = _load_spent_cny(connection, row, start_utc, end_utc)
    updated_budget = _decimal_value(row["daily_budget_cny"])
    # 必须同时满足“额度严格增加”和“高于已入账成本”；同额或降额 Patch 不能伪装成提额解封。
    if previous_budget is not None and updated_budget > previous_budget and spent < updated_budget:
        return _release_rule_blocks(
            connection,
            context,
            row,
            reason="budget_raised",
            request_id=request_id,
            trace_id=trace_id,
        ), "budget_raised"
    return [], None


def _release_rule_blocks(
    connection: sqlite3.Connection,
    context: AuthContext,
    row: sqlite3.Row,
    *,
    reason: str,
    request_id: str,
    trace_id: str,
) -> list[BudgetBlockRecord]:
    """事务内释放当前租户规则的 active block，调用方在 commit 成功后再记录日志。"""

    return release_active_budget_blocks(
        connection,
        tenant_id=context.user.tenant_id,
        budget_rule_id=row["budget_rule_id"],
        reason=reason,
        actor_id=context.user.user_id,
        request_id=request_id,
        trace_id=trace_id,
        now=_to_db_time(_utcnow()),
    )


def _last_event_type(connection: sqlite3.Connection, tenant_id: str, rule_id: str, budget_date: str) -> str | None:
    """读取当前业务日最后一次预算事件类型，用于控制台展示告警状态。"""

    row = connection.execute(
        """
        SELECT event_type
        FROM budget_alert_events
        WHERE tenant_id=? AND budget_rule_id=? AND budget_date=?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (tenant_id, rule_id, budget_date),
    ).fetchone()
    return row["event_type"] if row else None


def _scope_label(connection: sqlite3.Connection, tenant_id: str, scope_type: str, scope_id: str) -> str:
    """生成预算 scope 的安全展示名称，不返回 Virtual Key 明文。"""

    # 租户判断决定数据隔离边界，不能跨租户复用或返回对象。
    if scope_type == "tenant":
        row = connection.execute("SELECT name FROM tenants WHERE tenant_id=?", (tenant_id,)).fetchone()
        return row["name"] if row else "当前租户"
    if scope_type == "project":
        row = connection.execute("SELECT name FROM projects WHERE tenant_id=? AND project_id=?", (tenant_id, scope_id)).fetchone()
        return row["name"] if row else scope_id
    if scope_type == "application":
        row = connection.execute("SELECT a.name, p.name AS project_name FROM applications a LEFT JOIN projects p ON p.tenant_id=a.tenant_id AND p.project_id=a.project_id WHERE a.tenant_id=? AND a.application_id=?", (tenant_id, scope_id)).fetchone()
        return f"{row['project_name']} / {row['name']}" if row else scope_id
    row = connection.execute(
        "SELECT name, key_prefix FROM virtual_keys WHERE tenant_id=? AND virtual_key_id=?",
        (tenant_id, scope_id),
    ).fetchone()
    if row is None:
        return scope_id
    return f"{row['name']} ({row['key_prefix']})"


def _row_budget_period(row: sqlite3.Row | None) -> str:
    """恢复规则周期；旧测试快照没有新列时仍按日预算解释。"""

    if row is None or "budget_period" not in row.keys():
        return "day"
    period = row["budget_period"]
    if period not in {"day", "month", "year"}:
        raise BudgetSpendUnavailableError("budget_period.invalid")
    return str(period)


def _budget_window(now: datetime | None = None, *, period: str = "day") -> tuple[str, str, str]:
    """返回业务日标识和所选上海日、自然月或自然年的 UTC 半开区间。"""

    current = now or _utcnow()
    local = current.astimezone(SHANGHAI)
    budget_date = local.date().isoformat()
    if period == "day":
        local_start = datetime(local.year, local.month, local.day, tzinfo=SHANGHAI)
        local_end = local_start + timedelta(days=1)
    elif period == "month":
        local_start = datetime(local.year, local.month, 1, tzinfo=SHANGHAI)
        local_end = datetime(local.year + (local.month == 12), local.month % 12 + 1, 1, tzinfo=SHANGHAI)
    elif period == "year":
        local_start = datetime(local.year, 1, 1, tzinfo=SHANGHAI)
        local_end = datetime(local.year + 1, 1, 1, tzinfo=SHANGHAI)
    else:
        raise BudgetSpendUnavailableError("budget_period.invalid")
    return budget_date, _to_db_time(local_start), _to_db_time(local_end)


def _usage_percent_micros(spent_micros: int, budget_micros: int) -> float:
    """仅为展示计算百分比；治理比较已在整数微元上完成。"""

    if budget_micros <= 0:
        # 预算分母必须为正，损坏规则不能通过百分比计算伪装成正常状态。
        raise BudgetSpendUnavailableError("budget_amount.non_positive")
    value = (Decimal(spent_micros) * Decimal(100)) / Decimal(budget_micros)
    return round(float(value), 2)


def _database_money_micros(value: object, *, allow_zero: bool) -> int:
    """严格恢复数据库/内部十进制金额为非负整数微元。

    七位小数、NaN、Infinity、负数、科学计数字符串和超上限事实全部失败关闭；不能静默
    四舍五入或回退为零，否则 Control 与 Gateway 会对同一规则形成不同结论。
    """

    if isinstance(value, str) and ("e" in value.lower()):
        # 科学计数字符串不属于冻结金额 wire，避免不同解析器产生精度差异。
        raise BudgetSpendUnavailableError("budget_amount.scientific_notation")
    try:
        amount = Decimal(str(value))
    # 十进制解析异常只映射稳定内部原因，不传播原始数据库金额。
    except (InvalidOperation, ValueError) as exc:
        raise BudgetSpendUnavailableError("budget_amount.invalid") from exc
    if not amount.is_finite() or amount < 0 or (not allow_zero and amount == 0):
        # 非有限、负数或不允许的零值会低估预算消耗，必须失败关闭。
        raise BudgetSpendUnavailableError("budget_amount.invalid")
    quantized = amount.quantize(MONEY_QUANT)
    if amount != quantized:
        # 数据库金额超过六位小数时拒绝读取，保证 Control 与 Gateway 口径一致。
        raise BudgetSpendUnavailableError("budget_amount.precision_invalid")
    micros = quantized * MICROS_PER_CNY
    if micros != micros.to_integral_value():
        # 乘成微元后仍非整数说明精度合同损坏，不能静默四舍五入。
        raise BudgetSpendUnavailableError("budget_amount.precision_invalid")
    parsed = int(micros)
    if parsed < 0 or parsed > MAX_BUDGET_CNY_MICROS:
        # 超出统一微元上限会破坏跨层比较，因此拒绝而不是截断金额。
        raise BudgetSpendUnavailableError("budget_amount.out_of_range")
    return parsed


def _decimal_value(value: object) -> Decimal:
    """把可信数据库金额转换为六位 Decimal；损坏事实必须失败关闭。"""

    return _micros_decimal(_database_money_micros(value, allow_zero=True))


def _micros_decimal(value: int) -> Decimal:
    """把有界非负整数微元转换为 Decimal CNY。"""

    if type(value) is not int or value < 0 or value > MAX_BUDGET_CNY_MICROS:
        # 微元只接受有界整数，布尔值、负数和溢出事实必须失败关闭。
        raise BudgetSpendUnavailableError("budget_amount.micros_invalid")
    return (Decimal(value) / Decimal(MICROS_PER_CNY)).quantize(MONEY_QUANT)


def _micros_text(value: int) -> str:
    """把整数微元格式化为普通十进制 CNY 字符串。"""

    return _money_text(_micros_decimal(value))


def _money_text(value: Decimal) -> str:
    """把六位以内 Decimal 金额格式化为不含指数的稳定字符串。"""

    if not value.is_finite() or value < 0 or value != value.quantize(MONEY_QUANT):
        # 金额格式化前再次验证有界有限数，损坏内部值不能进入 API 响应。
        raise BudgetSpendUnavailableError("budget_amount.invalid")
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _parse_db_time(value: str) -> datetime:
    """把数据库 UTC ISO 字符串转换为 datetime，兼容旧数据的 naive 时间。"""

    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _to_db_time(value: datetime) -> str:
    """把预算管理使用的时间统一写成 UTC ISO 字符串。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utcnow() -> datetime:
    """返回秒级 UTC 当前时间，避免测试中毫秒差异造成状态不稳定。"""

    return datetime.now(UTC).replace(microsecond=0)


def _log_budget_updated(context: AuthContext, row: sqlite3.Row, request_id: str, trace_id: str) -> None:
    """记录预算规则变更事件，payload 只包含安全配置摘要。"""

    logger.info(
        "budget.rule_updated",
        extra={
            "event": "budget.rule_updated",
            "request_id": request_id,
            "trace_id": trace_id,
            "tenant_id": context.user.tenant_id,
            "actor_id": context.user.user_id,
            "object_id": row["budget_rule_id"],
            "payload": {
                "scope_type": row["scope_type"],
                "scope_id": row["scope_id"],
                "daily_budget_cny": _money_text(_decimal_value(row["daily_budget_cny"])),
                "warning_threshold_percent": row["warning_threshold_percent"],
                "exhausted_action": row["exhausted_action"],
                "status": row["status"],
            },
        },
    )


def _log_budget_unblocked(
    context: AuthContext,
    block: BudgetBlockRecord,
    reason: str,
    request_id: str,
    trace_id: str,
) -> None:
    """记录预算恢复结构化事件，事件只携带 ID、金额和原因等安全摘要。"""

    logger.info(
        "budget.unblocked",
        extra={
            "event": "budget.unblocked",
            "request_id": request_id,
            "trace_id": trace_id,
            "tenant_id": context.user.tenant_id,
            "actor_id": context.user.user_id,
            "object_id": block.budget_block_id,
            "payload": {
                "budget_rule_id": block.budget_rule_id,
                "environment_id": block.environment_id,
                "scope_type": block.scope_type,
                "scope_id": block.scope_id,
                "budget_date": block.budget_date,
                "reason_code": reason,
                "blocked_cost_cny": block.blocked_cost_cny,
                "budget_cny": block.budget_cny,
            },
        },
    )
