"""数据面预算服务：按租户、项目、应用和 Virtual Key 执行成本准入与批任务覆盖校验。

预算判断只消费可信路由价格、令牌估算和账本事实；成功与失败均保留 request_id，
Prompt、Response、Provider Key、Token 和内部端点不得进入日志。
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal, DecimalException, InvalidOperation, localcontext
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.budget_blocks import (
    BudgetBlockRecord,
    ensure_budget_block,
    get_active_budget_block,
    release_active_budget_blocks,
)
from metahub_shared.runtime.budget_reservations import (
    ensure_budget_reservation,
    get_budget_reservation,
    release_request_budget_reservations,
    set_request_budget_reservations_outstanding,
)
from metahub_shared.runtime.budget_scopes import build_budget_scope_id
from metahub_shared.runtime.budget_spend import (
    MAX_BUDGET_CNY_MICROS,
    MICROS_PER_CNY,
    BudgetSpendAmounts,
    BudgetSpendUnavailableError,
    load_budget_spend_amounts,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.routing_policy import (
    MAX_PRICE_DECIMAL_PLACES,
    MAX_PRICE_PER_1K,
    TokenEstimate,
    estimate_request_tokens,
)
from metahub_shared.runtime.webhooks import WebhookEvent, build_webhook_event

from metahub_gateway.auth.virtual_key import GatewayAuthContext
from metahub_gateway.batch import TrustedBatchContext
from metahub_gateway.pricing import resolve_workspace_cost_rate
from metahub_gateway.routing.models import GatewayRoute, GatewayRoutePlan
from metahub_gateway.usage.ledger import RequestLogContext

from .models import BudgetDecision, BudgetGuardResult, BudgetRule, EstimatedRequestCost

logger = logging.getLogger("metahub.gateway.budgets")

# 数据面预算时区、金额精度和耗尽阈值集中定义，确保网关阻断与控制面展示口径一致。
SHANGHAI = ZoneInfo("Asia/Shanghai")
MONEY_QUANT = Decimal("0.000001")
EXHAUSTED_THRESHOLD = 100
# 多层冲突策略写入事件，便于下游明确“所有适用额度累计约束、任一阻断即拒绝”。
SCOPE_CONFLICT_POLICY = "all_applicable_caps_deny_wins_v1"


class BudgetExhaustedError(ApiError):
    """预算阻断错误，携带安全决策和已提交的稳定 Scope 事件。"""

    def __init__(
        self,
        decision: BudgetDecision,
        scope_events: tuple[WebhookEvent, ...] = (),
    ) -> None:
        """创建统一 402 错误；调用层只能在数据库提交后机会性投递所携事件。"""

        super().__init__("gateway.budget_exhausted")
        self.budget_decision = decision
        # 事件只包含 ID、金额和调用链摘要，不允许携带请求正文、Key 明文或 Provider 凭据。
        self.scope_events = scope_events


def enforce_budget_guard(
    config: AppConfig,
    auth_context: GatewayAuthContext,
    route: GatewayRoute,
    payload: dict[str, Any],
    *,
    endpoint: str,
    request_kind: str,
    request_id: str,
    trace_id: str,
    fallback_route: GatewayRoute | None = None,
    routing_token_estimate: TokenEstimate | None = None,
    routing_price_as_of: datetime | None = None,
    batch_context: TrustedBatchContext | None = None,
    route_plan: GatewayRoutePlan | None = None,
    trusted_initial_estimate: EstimatedRequestCost | None = None,
    replace_existing_reservations: bool = False,
) -> BudgetGuardResult | None:
    """执行租户、项目、兼容 Application 与 Virtual Key 的逐层日预算准入检查。

    预算检查只读取安全元数据和 token/成本估算；不会记录 Prompt、Response、
    Authorization、Provider Key 或 Virtual Key 明文。
    """

    if batch_context is not None:
        if trusted_initial_estimate is not None or replace_existing_reservations:
            # Batch 的冻结 allocation 是独立 owner，不能与普通请求预留替换协议混用。
            raise ApiError("batch.execution_invalid")
        # 预算必须覆盖冻结路由的最坏成本；证据缺失或不一致时拒绝执行，禁止低估。
        if route_plan is None:
            # 预算必须覆盖冻结路由的最坏成本；证据缺失或不一致时拒绝执行，禁止低估。
            raise ApiError("batch.execution_invalid")
        return _enforce_batch_budget_coverage(
            config,
            auth_context,
            payload,
            request_kind=request_kind,
            request_id=request_id,
            trace_id=trace_id,
            route_plan=route_plan,
            token_estimate=routing_token_estimate,
            batch_context=batch_context,
        )

    connection: sqlite3.Connection | None = None
    blocking: BudgetDecision | None = None
    pending_unblocked_logs: list[tuple[BudgetRule, BudgetBlockRecord, str]] = []
    # 结构化日志与 Webhook 都只能在事务提交后离开预算服务；dict 按规则顺序稳定去重。
    pending_budget_event_logs: list[tuple[BudgetRule, BudgetDecision, str]] = []
    pending_scope_events: dict[str, WebhookEvent] = {}

    try:
        connection = connect_database(config.database_url)
        # SQLite 的 BEGIN IMMEDIATE 在读取 Usage 与 reservation 前取得写锁，使并发准入严格串行。
        # 锁持有到本请求全部规则完成，避免两个请求同时看到相同余额后双双放行。
        connection.execute("BEGIN IMMEDIATE")
        rules = _load_budget_rules(
            connection,
            auth_context.tenant_id,
            auth_context.environment_id,
            auth_context.virtual_key_id,
            auth_context.project_id,
            auth_context.application_id,
        )
        if replace_existing_reservations:
            _remove_replaceable_request_reservations(
                connection,
                tenant_id=auth_context.tenant_id,
                request_id=request_id,
                trace_id=trace_id,
            )
        if not rules:
            connection.commit()
            return None
        # cost_first 已在路由事务内形成估算时直接复用同一对象；其他模式也调用 shared
        # canonical 估算器，避免预算与路由分别维护字符遍历规则。
        token_estimate = routing_token_estimate or estimate_request_tokens(request_kind, payload)
        price_as_of = routing_price_as_of or _utcnow()
        initial_estimate = (
            _validated_trusted_estimate(trusted_initial_estimate)
            if trusted_initial_estimate is not None
            else _estimate_request_cost(
                connection,
                route,
                request_kind,
                payload,
                token_estimate=token_estimate,
                price_as_of=price_as_of,
            )
        )
        # 非流式 Fallback 最坏情况下主备上游都产生账单，准入金额必须覆盖两次估算总和。
        fallback_estimate: EstimatedRequestCost | None = None
        estimate = initial_estimate
        if fallback_route is not None:
            if trusted_initial_estimate is not None:
                # Cache candidate 不会调用 Provider；其冻结估算不能被当作主路由价格与备用相加。
                raise ApiError("gateway.budget_unavailable")
            fallback_estimate = _estimate_request_cost(
                connection,
                fallback_route,
                request_kind,
                payload,
                token_estimate=token_estimate,
                price_as_of=price_as_of,
            )
            estimate = EstimatedRequestCost(
                input_tokens=initial_estimate.input_tokens + fallback_estimate.input_tokens,
                output_tokens=initial_estimate.output_tokens + fallback_estimate.output_tokens,
                cost_cny=_quantize_budget_money(
                    initial_estimate.cost_cny + fallback_estimate.cost_cny
                ),
                # 主备任一缺价都无法形成最坏情况预留；不能用另一条已知价格掩盖未知成本。
                price_available=(
                    initial_estimate.price_available and fallback_estimate.price_available
                ),
            )
        budget_date, start_utc, end_utc = _budget_window()
        decisions: list[BudgetDecision] = []
        created_reservation_rule_ids: set[str] = set()
        for rule in rules:
            decision = _evaluate_rule(
                connection,
                rule,
                estimate,
                virtual_key_id=auth_context.virtual_key_id,
                budget_date=budget_date,
                start_utc=start_utc,
                end_utc=end_utc,
                request_id=request_id,
                trace_id=trace_id,
                endpoint=endpoint,
                route=route,
                created_reservation_rule_ids=created_reservation_rule_ids,
                pending_unblocked_logs=pending_unblocked_logs,
                pending_budget_event_logs=pending_budget_event_logs,
                pending_scope_events=pending_scope_events,
            )
            if decision is not None:
                decisions.append(decision)
            if decision is not None and decision.action_taken == "block":
                blocking = decision
                # 前序规则已经准入并预留时，后序规则的阻断必须在同一事务内撤销这些新预留。
                release_request_budget_reservations(
                    connection,
                    tenant_id=auth_context.tenant_id,
                    request_id=request_id,
                    budget_rule_ids=created_reservation_rule_ids,
                )
                break
        connection.commit()
        # 数据库恢复事实提交成功后才输出结构化日志；commit 失败会进入回滚分支且队列不会发布。
        for released_rule, released_block, release_reason in pending_unblocked_logs:
            _log_budget_unblocked(
                released_rule,
                released_block,
                release_reason,
                route,
                endpoint,
                request_id,
                trace_id,
            )
    # 缺价等已分类预算错误也发生在显式事务内；必须先回滚，再保留稳定错误码交给外层。
        for logged_rule, logged_decision, logged_event_type in pending_budget_event_logs:
            _log_budget_event(
                logged_rule,
                logged_decision,
                route,
                endpoint,
                request_id,
                trace_id,
                logged_event_type,
            )

    # 已分类 API 异常必须先回滚当前预算写事务，再保留原统一错误码交给外层记录。
    except ApiError:
        if connection is not None:
            connection.rollback()
        raise
    # 预算账本或规则读取失败时必须失败关闭，不能让成本保护在异常时静默放行。
    except (
        DbError,
        sqlite3.Error,
        DecimalException,
        OverflowError,
        BudgetSpendUnavailableError,
    ) as exc:
        if connection is not None:
            connection.rollback()
        _log_budget_unavailable(auth_context, route, endpoint, request_id, trace_id, exc)
        # 预算规则读取或评估失败时采用失败关闭，避免预算保护静默失效后继续打上游。
        raise ApiError("gateway.budget_unavailable") from exc
    finally:
        if connection is not None:
            connection.close()

    if blocking is not None:
        # blocked 治理事件只在生命周期首次创建时记录；每次拒绝仍由 request_logs 独立追踪。
        if blocking.block_created:
            _log_budget_blocked(auth_context, route, endpoint, request_id, trace_id, blocking)
        # 阻断决策要随异常一起带出，外层失败日志才能写入预算命中原因。
        raise BudgetExhaustedError(blocking, tuple(pending_scope_events.values()))
    return BudgetGuardResult(
        decision=_most_severe_decision(decisions),
        initial_estimated_cost_cny=initial_estimate.cost_cny,
        fallback_estimated_cost_cny=(
            fallback_estimate.cost_cny if fallback_estimate is not None else Decimal("0")
        ),
        scope_events=tuple(pending_scope_events.values()),
    )


def _enforce_batch_budget_coverage(
    config: AppConfig,
    auth_context: GatewayAuthContext,
    payload: dict[str, Any],
    *,
    request_kind: str,
    request_id: str,
    trace_id: str,
    route_plan: GatewayRoutePlan,
    token_estimate: TokenEstimate | None,
    batch_context: TrustedBatchContext,
) -> BudgetGuardResult | None:
    """校验批任务冻结路由的最坏成本已被预算预留覆盖，缺失数据时关闭执行。

    Validate frozen Batch allocations without creating ordinary request reservations.
    """

    if (
        batch_context.tenant_id != auth_context.tenant_id
        or batch_context.request_id != request_id
        or batch_context.trace_id != trace_id
    ):
        # 预算必须覆盖冻结路由的最坏成本；证据缺失或不一致时拒绝执行，禁止低估。
        raise ApiError("batch.execution_invalid")
    _validate_batch_route_coverage(route_plan, batch_context)
    # 预算必须覆盖冻结路由的最坏成本；证据缺失或不一致时拒绝执行，禁止低估。
    if token_estimate is None:
        # 预算必须覆盖冻结路由的最坏成本；证据缺失或不一致时拒绝执行，禁止低估。
        raise ApiError("batch.execution_invalid")
    estimate_tokens = token_estimate
    expected_cost = _batch_frozen_route_cost(batch_context, estimate_tokens)
    connection: sqlite3.Connection | None = None
    try:
        connection = connect_database(config.database_url)
        connection.execute("BEGIN IMMEDIATE")
        rules = _load_budget_rules(
            connection,
            auth_context.tenant_id,
            auth_context.environment_id,
            auth_context.virtual_key_id,
            auth_context.project_id,
            auth_context.application_id,
        )
        budget_date, _start_utc, _end_utc = _budget_window()
        rows = connection.execute(
            """
            SELECT allocation.batch_budget_allocation_id,allocation.budget_rule_id,
                   allocation.budget_date,allocation.reserved_cost_cny,
                   allocation.route_plan_hash,allocation.execution_outcome,
                   allocation.charge_state,allocation.status,
                   allocation.batch_job_id,allocation.batch_task_id,
                   hold.tenant_id AS hold_tenant_id,hold.batch_job_id AS hold_job_id,
                   hold.budget_rule_id AS hold_rule_id,hold.budget_date AS hold_budget_date,
                   hold.status AS hold_status
            FROM batch_budget_allocations allocation
            JOIN batch_budget_holds hold
              ON hold.tenant_id=allocation.tenant_id
             AND hold.batch_budget_hold_id=allocation.batch_budget_hold_id
            WHERE allocation.tenant_id=? AND allocation.batch_attempt_id=?
              AND allocation.budget_date=(
                SELECT MAX(latest.budget_date)
                FROM batch_budget_allocations latest
                WHERE latest.tenant_id=allocation.tenant_id
                  AND latest.batch_attempt_id=allocation.batch_attempt_id
              )
            ORDER BY allocation.budget_rule_id
            """,
            (auth_context.tenant_id, batch_context.batch_attempt_id),
        ).fetchall()
        row_by_rule = {str(row["budget_rule_id"]): row for row in rows}
        context_by_rule = {item.budget_rule_id: item for item in batch_context.allocations}
        expected_rule_ids = {rule.budget_rule_id for rule in rules}
        if (
            len(row_by_rule) != len(rows)
            or set(row_by_rule) != expected_rule_ids
            or set(context_by_rule) != expected_rule_ids
        ):
            # 预算必须覆盖冻结路由的最坏成本；证据缺失或不一致时拒绝执行，禁止低估。
            raise ApiError("batch.execution_invalid")
        expected_text = _money_text(expected_cost)
        for rule in rules:
            row = row_by_rule[rule.budget_rule_id]
            trusted = context_by_rule[rule.budget_rule_id]
            if (
                str(row["batch_budget_allocation_id"])
                != trusted.batch_budget_allocation_id
                or str(row["budget_date"]) != budget_date
                or trusted.budget_date != budget_date
                or str(row["reserved_cost_cny"]) != trusted.reserved_cost_cny
                or _money_text(_decimal_value(row["reserved_cost_cny"])) != expected_text
                or str(row["route_plan_hash"]) != batch_context.route_plan_hash
                or str(row["execution_outcome"]) != "not_invoked"
                or str(row["charge_state"]) != "none"
                or str(row["status"]) != "active"
                or str(row["batch_job_id"]) != batch_context.batch_job_id
                or str(row["batch_task_id"]) != batch_context.batch_task_id
                or str(row["hold_tenant_id"]) != auth_context.tenant_id
                or str(row["hold_job_id"]) != batch_context.batch_job_id
                or str(row["hold_rule_id"]) != rule.budget_rule_id
                or str(row["hold_budget_date"]) != budget_date
                or str(row["hold_status"]) not in {"active", "settled"}
            ):
                # 预算必须覆盖冻结路由的最坏成本；证据缺失或不一致时拒绝执行，禁止低估。
                raise ApiError("batch.execution_invalid")
            if get_active_budget_block(
                connection,
                tenant_id=auth_context.tenant_id,
                budget_rule_id=rule.budget_rule_id,
                budget_date=budget_date,
            ) is not None:
                # 预算必须覆盖冻结路由的最坏成本；证据缺失或不一致时拒绝执行，禁止低估。
                raise ApiError("batch.budget_exceeded")
        connection.commit()
    # 预算必须覆盖冻结路由的最坏成本；证据缺失或不一致时拒绝执行，禁止低估。
    except ApiError:
        if connection is not None and connection.in_transaction:
            connection.rollback()
        raise
    # 预算必须覆盖冻结路由的最坏成本；证据缺失或不一致时拒绝执行，禁止低估。
    except (DbError, sqlite3.Error, DecimalException, ValueError, OverflowError) as exc:
        if connection is not None and connection.in_transaction:
            connection.rollback()
        logger.warning(
            "batch.budget_coverage_unavailable",
            extra={
                "event": "batch.budget_coverage_unavailable",
                "tenant_id": auth_context.tenant_id,
                "request_id": request_id,
                "trace_id": trace_id,
                "error_code": "batch.worker_unavailable",
                "payload": {"exception_type": exc.__class__.__name__},
            },
        )
        # 预算必须覆盖冻结路由的最坏成本；证据缺失或不一致时拒绝执行，禁止低估。
        raise ApiError("batch.worker_unavailable") from None
    finally:
        if connection is not None:
            connection.close()
    return BudgetGuardResult(
        decision=None,
        initial_estimated_cost_cny=expected_cost,
        fallback_estimated_cost_cny=Decimal("0"),
    )


def _validate_batch_route_coverage(
    plan: GatewayRoutePlan,
    context: TrustedBatchContext,
) -> None:
    """核对预算覆盖使用的冻结路由集合与任务执行计划完全一致。

    Require the dynamic Gateway plan to stay inside the frozen ordered candidate plan.
    """

    frozen = context.route
    actual = plan.initial_route
    if (
        actual.model_id != frozen.default.model_id
        or actual.provider_id != frozen.default.provider_id
    ):
        # 预算必须覆盖冻结路由的最坏成本；证据缺失或不一致时拒绝执行，禁止低估。
        raise ApiError("batch.execution_invalid")
    expected_fallback = frozen.fallback
    actual_fallback = plan.fallback_route
    if (expected_fallback is None) != (actual_fallback is None):
        # 预算必须覆盖冻结路由的最坏成本；证据缺失或不一致时拒绝执行，禁止低估。
        raise ApiError("batch.execution_invalid")
    if expected_fallback is not None and actual_fallback is not None and (
        actual_fallback.model_id != expected_fallback.model_id
        or actual_fallback.provider_id != expected_fallback.provider_id
    ):
        # 预算必须覆盖冻结路由的最坏成本；证据缺失或不一致时拒绝执行，禁止低估。
        raise ApiError("batch.execution_invalid")


def _batch_frozen_route_cost(
    context: TrustedBatchContext,
    token_estimate: TokenEstimate,
) -> Decimal:
    """按冻结价格和可信令牌估算计算单次批任务最坏成本，禁止使用动态降级价格。

    Calculate the exact worst-case amount from frozen candidate prices.
    """

    try:
        with localcontext() as decimal_context:
            decimal_context.prec = 64
            total = sum(
                (
                    Decimal(token_estimate.input_tokens)
                    * Decimal(candidate.input_price_per_1k)
                    + Decimal(token_estimate.output_tokens)
                    * Decimal(candidate.output_price_per_1k)
                )
                / Decimal(1000)
                for candidate in context.route.candidates
            )
            return total.quantize(MONEY_QUANT, rounding=ROUND_CEILING)
    # 预算必须覆盖冻结路由的最坏成本；证据缺失或不一致时拒绝执行，禁止低估。
    except (DecimalException, ValueError, OverflowError) as exc:
        # 预算必须覆盖冻结路由的最坏成本；证据缺失或不一致时拒绝执行，禁止低估。
        raise ApiError("batch.execution_invalid") from exc

def load_policy_budget_remaining(
    config: AppConfig,
    auth_context: GatewayAuthContext,
) -> str | None:
    """读取 Policy DSL 可用的最小预算余额且不产生任何预算副作用。

    参数：config 提供数据库，auth_context 提供可信 tenant/environment/Project/Application/Key。
    返回：适用 active 规则中最小的非负 CNY 余额（6 位 Decimal 文本），无规则返回 None。
    约束：复用 Budget Guard 的上海业务日、Usage 与 outstanding reservation 口径，但绝不
    创建 evaluation、alert、block 或 reservation；读取失败必须在 Provider 前统一失败关闭。
    """

    connection: sqlite3.Connection | None = None
    try:
        connection = connect_database(config.database_url)
        # 单个只读事务保证多条 scope 规则看到同一账本快照；最终并发准入仍由后续
        # BEGIN IMMEDIATE Budget Guard 权威串行，Policy 余额不能代替硬阻断。
        connection.execute("BEGIN")
        rules = _load_budget_rules(
            connection,
            auth_context.tenant_id,
            auth_context.environment_id,
            auth_context.virtual_key_id,
            auth_context.project_id,
            auth_context.application_id,
        )
        if not rules:
            connection.rollback()
            return None
        budget_date, start_utc, end_utc = _budget_window()
        remaining_micros = min(
            max(
                0,
                _cny_to_micros(rule.daily_budget_cny, allow_zero=False)
                - _load_spend_amounts(
                    connection, rule, start_utc, end_utc, budget_date
                ).effective_cny_micros,
            )
            for rule in rules
        )
        connection.rollback()
        return _money_text(_cny_from_micros(remaining_micros))
    # 预算上下文读取失败必须显式失败关闭，不能把异常误解释为没有适用预算规则。
    except (
        DbError,
        sqlite3.Error,
        InvalidOperation,
        BudgetSpendUnavailableError,
    ) as exc:
        if connection is not None:
            connection.rollback()
        # 余额是策略输入，读取异常不能伪造成“无预算”或无限额度，否则会改变路由/拒绝语义。
        raise ApiError("gateway.policy_context_unavailable") from exc
    finally:
        if connection is not None:
            connection.close()


def apply_budget_decision(
    context: RequestLogContext,
    decision: BudgetDecision | None,
) -> RequestLogContext:
    """把预算命中摘要复制到 RequestLogContext，供成功或失败日志写入 request_logs。"""

    if decision is None:
        return context
    return replace(
        context,
        budget_status=decision.budget_status,
        budget_rule_id=decision.budget_rule_id,
        budget_scope_type=decision.scope_type,
        budget_scope_id=decision.scope_id,
        budget_action=decision.action_taken,
        budget_reason_code=decision.reason_code,
        budget_current_cost_cny=_money_text(decision.current_cost_cny),
        budget_limit_cny=_money_text(decision.budget_limit_cny),
    )


def finalize_request_budget_reservations(
    config: AppConfig,
    *,
    tenant_id: str,
    request_id: str,
    trace_id: str,
    outstanding_cost_cny: Decimal,
) -> None:
    """把请求终态后仍可能计费的 CNY 分项写回全部命中规则预留。

    参数金额由调用状态机按实际上游尝试计算：未调用 Provider 为 0，只有主尝试未知为主估算，
    主备均未知为两者之和。失败时保留原始最坏总预留，不能覆盖客户端原始业务错误。
    """

    connection: sqlite3.Connection | None = None
    try:
        connection = connect_database(config.database_url)
        connection.execute("BEGIN IMMEDIATE")
        updated = set_request_budget_reservations_outstanding(
            connection,
            tenant_id=tenant_id,
            request_id=request_id,
            outstanding_cost_cny=_money_text(_quantize_budget_money(outstanding_cost_cny)),
        )
        if updated < 1:
            # Guard 已声明本请求存在预留；0-row 表示租户/请求事实漂移，必须走同一安全告警边界。
            raise sqlite3.IntegrityError("budget_reservation_settlement_missing")
        connection.commit()
    # 结算失败不能覆盖原始 Provider/路由错误，只记录不含 SQL、金额原值和数据库路径的安全告警。
    except (DbError, sqlite3.Error, DecimalException, OverflowError):
        if connection is not None:
            connection.rollback()
        logger.warning(
            "budget.reservation_finalize_failed",
            extra={
                "event": "budget.reservation_finalize_failed",
                "tenant_id": tenant_id,
                "request_id": request_id,
                "trace_id": trace_id,
            },
        )
    finally:
        if connection is not None:
            connection.close()


def _remove_replaceable_request_reservations(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    request_id: str,
    trace_id: str,
) -> None:
    """在当前写事务内移除尚未终结的同请求预留，为新的准入估算腾出 owner。

    该入口只用于 cache candidate 在 settlement 二次验证后转为普通 Provider miss。
    已写入 outstanding 或 trace 不一致表示 owner 已进入其他终态，必须失败关闭而不能覆盖。
    """

    rows = connection.execute(
        """
        SELECT budget_rule_id,trace_id,outstanding_cost_cny
        FROM budget_reservations
        WHERE tenant_id=? AND request_id=?
        ORDER BY budget_rule_id
        """,
        (tenant_id, request_id),
    ).fetchall()
    if any(
        str(row["trace_id"]) != trace_id or row["outstanding_cost_cny"] is not None
        for row in rows
    ):
        # 网关关键分支必须维持租户、路由、成本和失败追踪一致，并保护上游凭据。
        raise sqlite3.IntegrityError("budget_reservation_replacement_invalid")
    rule_ids = {str(row["budget_rule_id"]) for row in rows}
    removed = release_request_budget_reservations(
        connection,
        tenant_id=tenant_id,
        request_id=request_id,
        budget_rule_ids=rule_ids,
    )
    if removed != len(rows):
        # 网关关键分支必须维持租户、路由、成本和失败追踪一致，并保护上游凭据。
        raise sqlite3.IntegrityError("budget_reservation_replacement_lost")


def _validated_trusted_estimate(estimate: EstimatedRequestCost) -> EstimatedRequestCost:
    """验证缓存层冻结的 token/金额 DTO，不重新读取 Prompt 或当前价格。"""

    if (
        not isinstance(estimate, EstimatedRequestCost)
        or isinstance(estimate.input_tokens, bool)
        or not isinstance(estimate.input_tokens, int)
        or estimate.input_tokens < 0
        or isinstance(estimate.output_tokens, bool)
        or not isinstance(estimate.output_tokens, int)
        or estimate.output_tokens < 0
        or type(estimate.price_available) is not bool
        or not isinstance(estimate.cost_cny, Decimal)
    ):
        # 网关关键分支必须维持租户、路由、成本和失败追踪一致，并保护上游凭据。
        raise ApiError("gateway.budget_unavailable")
    try:
        normalized = _quantize_budget_money(estimate.cost_cny)
        _cny_to_micros(normalized, allow_zero=True)
    # 网关关键分支必须维持租户、路由、成本和失败追踪一致，并保护上游凭据。
    except (DecimalException, OverflowError, ValueError) as exc:
        # 网关关键分支必须维持租户、路由、成本和失败追踪一致，并保护上游凭据。
        raise ApiError("gateway.budget_unavailable") from exc
    if normalized != estimate.cost_cny or (
        not estimate.price_available and normalized != Decimal("0")
    ):
        # 网关关键分支必须维持租户、路由、成本和失败追踪一致，并保护上游凭据。
        raise ApiError("gateway.budget_unavailable")
    return estimate


def _load_budget_rules(
    connection: sqlite3.Connection,
    tenant_id: str,
    environment_id: str,
    virtual_key_id: str,
    project_id: str | None,
    application_id: str | None,
) -> list[BudgetRule]:
    """加载当前请求可命中的租户、项目、应用和 Virtual Key 预算规则。"""

    rows = connection.execute(
        """
        SELECT budget_rule_id, tenant_id, environment_id, scope_type, scope_id, daily_budget_cny,
               warning_threshold_percent, exhausted_action
        FROM budget_rules
        WHERE tenant_id=?
          AND environment_id=?
          AND status='active'
          AND (
            scope_type='tenant'
            OR (scope_type='project' AND scope_id=?)
            OR (scope_type='application' AND scope_id=?)
            OR (scope_type='virtual_key' AND scope_id=?)
          )
        ORDER BY CASE scope_type
          WHEN 'tenant' THEN 0
          WHEN 'project' THEN 1
          WHEN 'application' THEN 2
          ELSE 3
        END
        """,
        (tenant_id, environment_id, project_id, application_id, virtual_key_id),
    ).fetchall()
    return [
        BudgetRule(
            budget_rule_id=row["budget_rule_id"],
            tenant_id=row["tenant_id"],
            environment_id=row["environment_id"],
            scope_type=row["scope_type"],
            scope_id=row["scope_id"],
            daily_budget_cny=_decimal_value(row["daily_budget_cny"]),
            warning_threshold_percent=int(row["warning_threshold_percent"]),
            exhausted_action=row["exhausted_action"],
        )
        for row in rows
    ]

def _evaluate_rule(
    connection: sqlite3.Connection,
    rule: BudgetRule,
    estimate: EstimatedRequestCost,
    *,
    virtual_key_id: str,
    budget_date: str,
    start_utc: str,
    end_utc: str,
    request_id: str,
    trace_id: str,
    endpoint: str,
    route: GatewayRoute,
    created_reservation_rule_ids: set[str],
    pending_unblocked_logs: list[tuple[BudgetRule, BudgetBlockRecord, str]],
    pending_budget_event_logs: list[tuple[BudgetRule, BudgetDecision, str]],
    pending_scope_events: dict[str, WebhookEvent],
) -> BudgetDecision | None:
    """评估单条规则，并在同一事务内写入预算解释、预留和越限事实。

    参数全部来自已认证 Key、已选定路由和当前写事务；返回值只包含安全预算摘要。
    Scope Webhook 仅在这里构造并留在内存，调用层必须等本事务提交后才允许入队。
    """

    _release_expired_blocks(
        connection,
        rule,
        budget_date,
        request_id,
        trace_id,
        pending_unblocked_logs,
    )
    active_block = get_active_budget_block(
        connection,
        tenant_id=rule.tenant_id,
        budget_rule_id=rule.budget_rule_id,
        budget_date=budget_date,
    )
    if active_block is not None and rule.exhausted_action != "block":
        # 控制面正常会事务内释放；该分支只修复手工改库残留，不能让 alert_only 继续误阻断。
        released = release_active_budget_blocks(
            connection,
            tenant_id=rule.tenant_id,
            budget_rule_id=rule.budget_rule_id,
            reason="mode_alert_only",
            actor_id=None,
            request_id=request_id,
            trace_id=trace_id,
            now=_to_db_time(_utcnow()),
        )
        for block in released:
            pending_unblocked_logs.append((rule, block, "mode_alert_only"))
        active_block = None

    spend = _load_spend_amounts(connection, rule, start_utc, end_utc, budget_date)
    spent_before = _cny_from_micros(spend.effective_cny_micros)
    if active_block is not None:
        # active block 是当日粘性治理事实；账务贷记或预留波动不能绕过它。
        decision = _evaluate_active_block(
            connection,
            rule,
            active_block,
            estimate,
            virtual_key_id=virtual_key_id,
            budget_date=budget_date,
            spent_before=spent_before,
            request_id=request_id,
            trace_id=trace_id,
        )
        # 重复阻断可重投已经提交的稳定事件；历史 block 没有 0042 事件时不伪造首次越限金额。
        existing_event = _load_budget_scope_event(connection, rule, budget_date)
        if existing_event is not None:
            pending_scope_events[existing_event.event_id] = existing_event
        return decision
    if not estimate.price_available:
        # 有预算规则却没有可信版本价格时，零元预留等价于静默关闭预算保护。
        raise ApiError("gateway.budget_unavailable")

    existing_reservation = get_budget_reservation(
        connection,
        tenant_id=rule.tenant_id,
        budget_rule_id=rule.budget_rule_id,
        request_id=request_id,
    )
    estimate_micros = _cny_to_micros(estimate.cost_cny, allow_zero=True)
    # 幂等重试已有 reservation 时，有效消耗已包含该金额，不能再次叠加本次估算。
    estimated_event_micros = 0 if existing_reservation is not None else estimate_micros
    projected_micros = _bounded_budget_add(
        spend.effective_cny_micros,
        estimated_event_micros,
    )
    projected = _cny_from_micros(projected_micros)
    budget_micros = _cny_to_micros(rule.daily_budget_cny, allow_zero=False)
    if projected_micros >= budget_micros:
        status = "exhausted"
        threshold = EXHAUSTED_THRESHOLD
        event_type = "budget.exhausted"
        action = "block" if rule.exhausted_action == "block" else "allow"
        reason = "budget.exhausted.block" if action == "block" else "budget.exhausted.alert_only"
    elif projected >= _threshold_amount(rule):
        status = "threshold_reached"
        threshold = rule.warning_threshold_percent
        event_type = "budget.threshold_reached"
        action = "warn"
        reason = "budget.threshold_reached"
    else:
        _reserve_admitted_request(
            connection,
            rule,
            estimate,
            budget_date=budget_date,
            request_id=request_id,
            trace_id=trace_id,
            created_reservation_rule_ids=created_reservation_rule_ids,
        )
        return None

    decision = BudgetDecision(
        budget_rule_id=rule.budget_rule_id,
        scope_type=rule.scope_type,
        scope_id=rule.scope_id,
        budget_status=status,
        action_taken=action,
        reason_code=reason,
        current_cost_cny=projected,
        budget_limit_cny=rule.daily_budget_cny,
    )
    if action == "block":
        block, created = ensure_budget_block(
            connection,
            tenant_id=rule.tenant_id,
            environment_id=rule.environment_id,
            budget_rule_id=rule.budget_rule_id,
            scope_type=rule.scope_type,
            scope_id=rule.scope_id,
            budget_date=budget_date,
            blocked_cost_cny=_money_text(projected),
            budget_cny=_money_text(rule.daily_budget_cny),
            request_id=request_id,
            trace_id=trace_id,
            now=_to_db_time(_utcnow()),
        )
        decision = replace(decision, budget_block_id=block.budget_block_id, block_created=created)
    else:
        reservation_created = _reserve_admitted_request(
            connection,
            rule,
            estimate,
            budget_date=budget_date,
            request_id=request_id,
            trace_id=trace_id,
            created_reservation_rule_ids=created_reservation_rule_ids,
        )
        decision = replace(decision, reservation_created=reservation_created)

    _insert_budget_evaluation(
        connection,
        rule,
        decision,
        virtual_key_id=virtual_key_id,
        budget_date=budget_date,
        spent_before=spent_before,
        estimate=estimate,
        request_id=request_id,
        trace_id=trace_id,
        threshold_percent=threshold,
    )
    _insert_alert_event(
        connection,
        rule,
        decision,
        budget_date=budget_date,
        spent_before=spent_before,
        request_id=request_id,
        trace_id=trace_id,
        threshold_percent=threshold,
        event_type=event_type,
    )
    if status == "exhausted":
        scope_event = _ensure_budget_scope_event(
            connection,
            rule,
            budget_date=budget_date,
            action_taken=action,
            posted_cost_micros=spend.posted_cny_micros,
            reserved_cost_micros=spend.reserved_cny_micros,
            estimated_request_cost_micros=estimated_event_micros,
            projected_cost_micros=projected_micros,
            budget_micros=budget_micros,
            request_id=request_id,
            trace_id=trace_id,
        )
        pending_scope_events[scope_event.event_id] = scope_event
    # 阈值日志也必须延迟到 commit 后；失败事务不能留下看似成功的治理记录。
    pending_budget_event_logs.append((rule, decision, event_type))
    return decision
def _evaluate_active_block(
    connection: sqlite3.Connection,
    rule: BudgetRule,
    block: BudgetBlockRecord,
    estimate: EstimatedRequestCost,
    *,
    virtual_key_id: str,
    budget_date: str,
    spent_before: Decimal,
    request_id: str,
    trace_id: str,
) -> BudgetDecision:
    """把当日 active block 转成拒绝决策，并为当前失败请求写独立预算解释记录。"""

    decision = BudgetDecision(
        budget_rule_id=rule.budget_rule_id,
        scope_type=rule.scope_type,
        scope_id=rule.scope_id,
        budget_status="exhausted",
        action_taken="block",
        reason_code="budget.active_block",
        current_cost_cny=_decimal_value(block.blocked_cost_cny),
        budget_limit_cny=_decimal_value(block.budget_cny),
        budget_block_id=block.budget_block_id,
        block_created=False,
    )
    _insert_budget_evaluation(
        connection,
        rule,
        decision,
        virtual_key_id=virtual_key_id,
        budget_date=budget_date,
        spent_before=spent_before,
        estimate=estimate,
        request_id=request_id,
        trace_id=trace_id,
        threshold_percent=EXHAUSTED_THRESHOLD,
    )
    return decision


def _release_expired_blocks(
    connection: sqlite3.Connection,
    rule: BudgetRule,
    budget_date: str,
    request_id: str,
    trace_id: str,
    pending_unblocked_logs: list[tuple[BudgetRule, BudgetBlockRecord, str]],
) -> None:
    """事务内释放历史 block，并把结构化日志延迟到调用方 commit 成功之后。"""

    released = release_active_budget_blocks(
        connection,
        tenant_id=rule.tenant_id,
        budget_rule_id=rule.budget_rule_id,
        reason="business_day_ended",
        actor_id=None,
        request_id=request_id,
        trace_id=trace_id,
        now=_to_db_time(_utcnow()),
        exclude_budget_date=budget_date,
    )
    for block in released:
        pending_unblocked_logs.append((rule, block, "business_day_ended"))


def _load_spend_amounts(
    connection: sqlite3.Connection,
    rule: BudgetRule,
    start_utc: str,
    end_utc: str,
    budget_date: str,
) -> BudgetSpendAmounts:
    """按共享正式账本口径读取规则的 posted、reserved 与 effective 整数微元。

    参数来自已经按 tenant/Environment/scope 加载的 BudgetRule 和上海业务日窗口；本函数
    不提交事务。Shared helper 会排除人工调整并对未知 CNY、坏值和总额越界失败关闭。
    """

    return load_budget_spend_amounts(
        connection,
        tenant_id=rule.tenant_id,
        environment_id=rule.environment_id,
        scope_type=rule.scope_type,
        scope_id=rule.scope_id,
        budget_rule_id=rule.budget_rule_id,
        budget_date=budget_date,
        start_utc=start_utc,
        end_utc=end_utc,
    )
def _ensure_budget_scope_event(
    connection: sqlite3.Connection,
    rule: BudgetRule,
    *,
    budget_date: str,
    action_taken: str,
    posted_cost_micros: int,
    reserved_cost_micros: int,
    estimated_request_cost_micros: int,
    projected_cost_micros: int,
    budget_micros: int,
    request_id: str,
    trace_id: str,
) -> WebhookEvent:
    """幂等持久化单规则单业务日的首次越限事实，并返回严格 Webhook 信封。

    所有金额都在调用事务中以整数微元计算；唯一冲突只复用既有 canonical 事实，其他
    约束冲突必须抛出并回滚，不能用 ``OR IGNORE`` 吞掉 owner 或金额损坏。
    """

    budget_scope_id = build_budget_scope_id(
        tenant_id=rule.tenant_id,
        environment_id=rule.environment_id,
        scope_type=rule.scope_type,
        scope_id=rule.scope_id,
    )
    connection.execute(
        """
        INSERT INTO budget_scope_events(
          budget_scope_event_id, event_id, tenant_id, environment_id, budget_rule_id,
          budget_scope_id, scope_type, scope_id, budget_date, action_taken,
          posted_cost_micros, reserved_cost_micros, estimated_request_cost_micros,
          projected_cost_micros, budget_micros, request_id, trace_id, created_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(tenant_id, environment_id, budget_rule_id, budget_date) DO NOTHING
        """,
        (
            f"bse_{uuid4().hex}",
            f"bsevt_{uuid4().hex}",
            rule.tenant_id,
            rule.environment_id,
            rule.budget_rule_id,
            budget_scope_id,
            rule.scope_type,
            rule.scope_id,
            budget_date,
            action_taken,
            posted_cost_micros,
            reserved_cost_micros,
            estimated_request_cost_micros,
            projected_cost_micros,
            budget_micros,
            request_id,
            trace_id,
            _to_db_time(_utcnow()),
        ),
    )
    event = _load_budget_scope_event(connection, rule, budget_date)
    if event is None:
        # 插入或唯一复用后无法回读表示事务事实不一致，必须在 Provider 前失败关闭。
        raise sqlite3.IntegrityError("budget_scope_event_state_missing")
    return event


def _load_budget_scope_event(
    connection: sqlite3.Connection,
    rule: BudgetRule,
    budget_date: str,
) -> WebhookEvent | None:
    """按完整租户、Environment、Rule 和业务日读取稳定越限事件，禁止裸 ID 查询。"""

    row = connection.execute(
        """
        SELECT * FROM budget_scope_events
        WHERE tenant_id=? AND environment_id=? AND budget_rule_id=? AND budget_date=?
        """,
        (rule.tenant_id, rule.environment_id, rule.budget_rule_id, budget_date),
    ).fetchone()
    return _budget_scope_event_from_row(row) if row is not None else None


def _budget_scope_event_from_row(row: sqlite3.Row) -> WebhookEvent:
    """把不可变数据库事实转换为共享严格 Webhook；坏值只产生固定失败关闭异常。"""

    posted = _event_row_micros(row["posted_cost_micros"], allow_zero=True)
    reserved = _event_row_micros(row["reserved_cost_micros"], allow_zero=True)
    estimated = _event_row_micros(row["estimated_request_cost_micros"], allow_zero=True)
    projected = _event_row_micros(row["projected_cost_micros"], allow_zero=True)
    budget = _event_row_micros(row["budget_micros"], allow_zero=False)
    try:
        return build_webhook_event(
            tenant_id=str(row["tenant_id"]),
            event_type="budget.scope_exceeded",
            payload={
                "budget_scope_event_id": str(row["budget_scope_event_id"]),
                "budget_scope_id": str(row["budget_scope_id"]),
                "budget_rule_id": str(row["budget_rule_id"]),
                "environment_id": str(row["environment_id"]),
                "scope_type": str(row["scope_type"]),
                "scope_id": str(row["scope_id"]),
                "budget_date": str(row["budget_date"]),
                "posted_cost_cny": _money_text(_cny_from_micros(posted)),
                "reserved_cost_cny": _money_text(_cny_from_micros(reserved)),
                "estimated_request_cost_cny": _money_text(_cny_from_micros(estimated)),
                "projected_cost_cny": _money_text(_cny_from_micros(projected)),
                "budget_cny": _money_text(_cny_from_micros(budget)),
                "action_taken": str(row["action_taken"]),
                "request_id": str(row["request_id"]),
                "conflict_policy": SCOPE_CONFLICT_POLICY,
            },
            actor_id=None,
            object_id=str(row["budget_scope_id"]),
            trace_id=str(row["trace_id"]),
            event_id=str(row["event_id"]),
            timestamp=_parse_scope_event_time(row["created_at"]),
        )
    # 持久事件若不满足共享白名单，必须失败关闭且不能把原始字段带入异常或日志。
    except (TypeError, ValueError) as exc:
        # Shared 严格合同拒绝的持久事实不能进入队列，也不能把原始字段写入日志。
        raise BudgetSpendUnavailableError("budget_spend.scope_event_invalid") from exc


def _event_row_micros(value: object, *, allow_zero: bool) -> int:
    """恢复事件 INTEGER 微元并执行与迁移一致的非负、上限和非零预算约束。"""

    if (
        type(value) is not int
        or value < 0
        or value > MAX_BUDGET_CNY_MICROS
        or (not allow_zero and value == 0)
    ):
        # 非 INTEGER 或越界金额代表 canonical 事件已损坏，不能静默修正后继续投递。
        raise BudgetSpendUnavailableError("budget_spend.scope_event_invalid")
    return value


def _parse_scope_event_time(value: object) -> datetime:
    """恢复稳定事件时间；无时区或非法文本不能用当前时间掩盖幂等事实损坏。"""

    # 时间字段必须保留原始带时区文本，其他类型不能用隐式字符串转换掩盖损坏。
    if type(value) is not str:
        raise BudgetSpendUnavailableError("budget_spend.scope_event_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # 非法 ISO 时间不能回退为当前时刻，否则同一事件重投会产生不同信封。
    except ValueError as exc:
        raise BudgetSpendUnavailableError("budget_spend.scope_event_invalid") from exc
    # 无时区时间无法证明事件所处绝对时刻，拒绝进入 Webhook 队列。
    if parsed.tzinfo is None:
        raise BudgetSpendUnavailableError("budget_spend.scope_event_invalid")
    return parsed.astimezone(UTC)


def _cny_to_micros(value: Decimal, *, allow_zero: bool) -> int:
    """严格把非负六位 Decimal CNY 转为有界整数微元，不执行静默四舍五入。"""

    try:
        with localcontext() as context:
            context.prec = 64
            scaled = value * MICROS_PER_CNY
            integral = scaled.to_integral_value()
    # Decimal 运算异常统一转换为固定预算不可用原因，避免回显损坏金额。
    except DecimalException as exc:
        raise BudgetSpendUnavailableError("budget_spend.amount_invalid") from exc
    if (
        not value.is_finite()
        or value < 0
        or scaled != integral
        or integral > MAX_BUDGET_CNY_MICROS
        or (not allow_zero and integral == 0)
    ):
        # 非有限、负数、超精度或越界金额均不能通过取整进入准入和事件合同。
        raise BudgetSpendUnavailableError("budget_spend.amount_invalid")
    return int(integral)


def _cny_from_micros(value: int) -> Decimal:
    """把已校验微元还原为精确 Decimal CNY，供兼容 DTO、日志和旧预算表写入。"""

    micros = _event_row_micros(value, allow_zero=True)
    return Decimal(micros) / Decimal(MICROS_PER_CNY)


def _bounded_budget_add(left: int, right: int) -> int:
    """执行预算业务上限内的整数加法，越界时失败关闭而不是截断 projected 金额。"""

    # 操作数类型漂移或负数表示上游校验被绕过，必须在 Provider 前失败关闭。
    if type(left) is not int or type(right) is not int or left < 0 or right < 0:
        raise BudgetSpendUnavailableError("budget_spend.projected_amount_invalid")
    total = left + right
    # 超过业务金额上限时不能截断，否则准入、控制面和事件会产生三套不同事实。
    if total > MAX_BUDGET_CNY_MICROS:
        raise BudgetSpendUnavailableError("budget_spend.projected_amount_invalid")
    return total


def _reserve_admitted_request(
    connection: sqlite3.Connection,
    rule: BudgetRule,
    estimate: EstimatedRequestCost,
    *,
    budget_date: str,
    request_id: str,
    trace_id: str,
    created_reservation_rule_ids: set[str],
) -> bool:
    """在当前串行事务中持久化 Provider 前预留，并记录本事务实际创建的规则。"""

    _, created = ensure_budget_reservation(
        connection,
        tenant_id=rule.tenant_id,
        environment_id=rule.environment_id,
        budget_rule_id=rule.budget_rule_id,
        scope_type=rule.scope_type,
        scope_id=rule.scope_id,
        budget_date=budget_date,
        request_id=request_id,
        trace_id=trace_id,
        reserved_cost_cny=_money_text(estimate.cost_cny),
        now=_to_db_time(_utcnow()),
    )
    if created:
        created_reservation_rule_ids.add(rule.budget_rule_id)
    return created


def _estimate_request_cost(
    connection: sqlite3.Connection,
    route: GatewayRoute,
    request_kind: str,
    payload: dict[str, Any],
    *,
    token_estimate: TokenEstimate | None = None,
    price_as_of: datetime | None = None,
) -> EstimatedRequestCost:
    """按 Workspace ProviderCostRate 估算预算成本。

    返回值始终保留 Token 数和兼容金额；price_available=false 表示没有可审计版本价格，
    调用方必须失败关闭且不得创建零元预留。显式带版本零价仍是有效免费价格。
    """

    estimate = token_estimate or estimate_request_tokens(request_kind, payload)
    input_tokens, output_tokens = estimate.input_tokens, estimate.output_tokens
    try:
        rate = resolve_workspace_cost_rate(
            connection,
            tenant_id=route.tenant_id,
            model_id=route.model_id,
            observed_at=_to_db_time(price_as_of or _utcnow()),
        )
    except sqlite3.IntegrityError as exc:
        raise ApiError("gateway.budget_unavailable") from exc
    if rate is None:
        # 无 Workspace 成本价时没有可预留价格；兼容金额保持 0，但可用性必须明确为 false。
        return EstimatedRequestCost(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_cny=Decimal("0"),
            price_available=False,
        )
    if rate.provider_id != route.provider_id or rate.model_id != route.model_id:
        raise ApiError("gateway.budget_unavailable")
    price_schema_version = rate.price_schema_version.strip()
    if not price_schema_version:
        # 无版本默认 0 是“缺价”而非免费；这里不回退其他来源。
        return EstimatedRequestCost(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_cny=Decimal("0"),
            price_available=False,
        )
    currency = rate.currency
    # 日预算以 CNY 为唯一结算单位；没有版本化汇率时，USD/未知币种都不是“免费”。
    if currency != "CNY":
        return EstimatedRequestCost(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_cny=Decimal("0"),
            price_available=False,
        )
    input_price = _strict_budget_price(rate.input_price_per_1k)
    output_price = _strict_budget_price(rate.output_price_per_1k)
    try:
        # 固定高精度上下文覆盖合法价格与有界 Token 的乘加，避免依赖进程全局 Decimal 设置。
        with localcontext() as context:
            context.prec = 64
            amount = (
                Decimal(input_tokens) * input_price
                + Decimal(output_tokens) * output_price
            ) / Decimal(1000)
            cost_cny = amount.quantize(MONEY_QUANT)
    except DecimalException as exc:
        # 极端或损坏财务事实统一使用预算不可用，不能穿透为 infra.unhandled。
        raise ApiError("gateway.budget_unavailable") from exc
    return EstimatedRequestCost(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_cny=cost_cny,
        price_available=True,
    )


def _strict_budget_price(value: object) -> Decimal:
    """严格解析预算实际采用的版本价格，拒绝把损坏 Decimal 静默压成免费。

    参数为数据库冻结价格字段；返回有限非负 Decimal。任何格式、NaN、Infinity 或负数
    都表示预算依赖不可用，使用既有稳定错误码失败关闭且不回显原始值。
    """

    try:
        amount = Decimal(str(value))
    # Decimal 解析异常代表价格事实损坏，不能静默转换成 0 元后放行预算检查。
    except (InvalidOperation, ValueError) as exc:
        # 只暴露稳定业务错误码，Decimal 原始异常和数据库值不得进入响应。
        raise ApiError("gateway.budget_unavailable") from exc
    decimal_places = max(-amount.as_tuple().exponent, 0)
    if (
        not amount.is_finite()
        or amount < 0
        or amount > MAX_PRICE_PER_1K
        or decimal_places > MAX_PRICE_DECIMAL_PLACES
    ):
        # 非有限、负数、越过共享上限或精度的值均不是可信价格，必须在 Provider 前失败关闭。
        raise ApiError("gateway.budget_unavailable")
    return amount


def _threshold_amount(rule: BudgetRule) -> Decimal:
    """计算 80% 等告警阈值对应的 CNY 金额。"""

    return _quantize_budget_money(
        rule.daily_budget_cny * Decimal(rule.warning_threshold_percent) / Decimal(100)
    )


def _insert_budget_evaluation(
    connection: sqlite3.Connection,
    rule: BudgetRule,
    decision: BudgetDecision,
    *,
    virtual_key_id: str,
    budget_date: str,
    spent_before: Decimal,
    estimate: EstimatedRequestCost,
    request_id: str,
    trace_id: str,
    threshold_percent: int,
) -> None:
    """写入预算命中解释记录，供调用日志详情查询。"""

    connection.execute(
        """
        INSERT INTO budget_evaluations(
          budget_evaluation_id, tenant_id, environment_id, request_id, trace_id, virtual_key_id,
          budget_rule_id, scope_type, scope_id, budget_date, spent_before_cny,
          estimated_request_cost_cny, projected_cost_cny, budget_cny,
          threshold_percent, budget_status, action_taken, reason_code, created_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"be_{uuid4().hex}",
            rule.tenant_id,
            rule.environment_id,
            request_id,
            trace_id,
            virtual_key_id,
            rule.budget_rule_id,
            rule.scope_type,
            rule.scope_id,
            budget_date,
            _money_text(spent_before),
            _money_text(estimate.cost_cny),
            _money_text(decision.current_cost_cny),
            _money_text(rule.daily_budget_cny),
            threshold_percent,
            decision.budget_status,
            decision.action_taken,
            decision.reason_code,
            _to_db_time(_utcnow()),
        ),
    )


def _insert_alert_event(
    connection: sqlite3.Connection,
    rule: BudgetRule,
    decision: BudgetDecision,
    *,
    budget_date: str,
    spent_before: Decimal,
    request_id: str,
    trace_id: str,
    threshold_percent: int,
    event_type: str,
) -> None:
    """写入预算阈值事件，使用唯一键去重同一天同一阈值的重复告警。"""

    connection.execute(
        """
        INSERT OR IGNORE INTO budget_alert_events(
          budget_alert_event_id, tenant_id, environment_id, budget_rule_id, scope_type, scope_id,
          budget_date, threshold_percent, event_type, current_cost_cny,
          projected_cost_cny, budget_cny, action_taken, request_id, trace_id, created_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"bae_{uuid4().hex}",
            rule.tenant_id,
            rule.environment_id,
            rule.budget_rule_id,
            rule.scope_type,
            rule.scope_id,
            budget_date,
            threshold_percent,
            event_type,
            _money_text(spent_before),
            _money_text(decision.current_cost_cny),
            _money_text(rule.daily_budget_cny),
            decision.action_taken,
            request_id,
            trace_id,
            _to_db_time(_utcnow()),
        ),
    )


def _most_severe_decision(decisions: list[BudgetDecision]) -> BudgetDecision | None:
    """选择最适合写入 request_logs 的预算命中摘要。"""

    if not decisions:
        return None
    exhausted = next(
        (decision for decision in decisions if decision.budget_status == "exhausted"),
        None,
    )
    return exhausted or decisions[0]


def _budget_window(now: datetime | None = None) -> tuple[str, str, str]:
    """返回 Asia/Shanghai 业务日和对应 UTC 起止边界。"""

    current = now or _utcnow()
    local = current.astimezone(SHANGHAI)
    local_start = datetime(local.year, local.month, local.day, tzinfo=SHANGHAI)
    local_end = local_start + timedelta(days=1)
    return local_start.date().isoformat(), _to_db_time(local_start), _to_db_time(local_end)


def _decimal_value(value: object) -> Decimal:
    """严格解析数据库金额；损坏值抛 Decimal 异常交由服务边界失败关闭。"""

    try:
        amount = Decimal(str(value if value is not None else "0"))
    # 数据库财务坏值必须在预算边界失败关闭，不能按零金额继续放行 Provider 请求。
    except (InvalidOperation, ValueError) as exc:
        # 财务聚合不能把坏值静默改写为 0，否则会直接放宽硬预算。
        raise InvalidOperation from exc
    return _quantize_budget_money(amount)


def _quantize_budget_money(value: Decimal) -> Decimal:
    """把有限非负 CNY 金额量化到六位，并拒绝超出高精度上下文的异常事实。"""

    if not value.is_finite() or value < 0:
        raise InvalidOperation
    try:
        with localcontext() as context:
            context.prec = 64
            return value.quantize(MONEY_QUANT)
    except DecimalException as exc:
        raise InvalidOperation from exc


def _money_text(value: Decimal) -> str:
    """把 Decimal 金额转换成稳定字符串，避免日志和数据库出现浮点科学计数法。"""

    text = format(_quantize_budget_money(value), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _to_db_time(value: datetime) -> str:
    """把预算保护使用的时间统一序列化为 UTC ISO 字符串。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utcnow() -> datetime:
    """返回秒级 UTC 当前时间，避免同一请求内预算记录时间格式不稳定。"""

    return datetime.now(UTC).replace(microsecond=0)


def _log_budget_event(
    rule: BudgetRule,
    decision: BudgetDecision,
    route: GatewayRoute,
    endpoint: str,
    request_id: str,
    trace_id: str,
    event_type: str,
) -> None:
    """记录预算阈值事件，payload 只包含安全成本摘要。"""

    logger.info(
        event_type,
        extra={
            "event": event_type,
            "request_id": request_id,
            "trace_id": trace_id,
            "tenant_id": rule.tenant_id,
            "actor_id": rule.scope_id,
            "object_id": rule.budget_rule_id,
            "payload": {
                "scope_type": rule.scope_type,
                "budget_status": decision.budget_status,
                "action_taken": decision.action_taken,
                "current_cost_cny": _money_text(decision.current_cost_cny),
                "budget_cny": _money_text(decision.budget_limit_cny),
                "endpoint": endpoint,
                "model_id": route.model_id,
                "provider_id": route.provider_id,
            },
        },
    )


def _log_budget_blocked(
    auth_context: GatewayAuthContext,
    route: GatewayRoute,
    endpoint: str,
    request_id: str,
    trace_id: str,
    decision: BudgetDecision,
) -> None:
    """记录预算阻断事件，不输出请求体、密钥或上游响应。"""

    logger.warning(
        "budget.blocked",
        extra={
            "event": "budget.blocked",
            "request_id": request_id,
            "trace_id": trace_id,
            "tenant_id": auth_context.tenant_id,
            "actor_id": auth_context.virtual_key_id,
            "object_id": decision.budget_rule_id,
            "payload": {
                "scope_type": decision.scope_type,
                "scope_id": decision.scope_id,
                "current_cost_cny": _money_text(decision.current_cost_cny),
                "budget_cny": _money_text(decision.budget_limit_cny),
                "endpoint": endpoint,
                "model_id": route.model_id,
                "provider_id": route.provider_id,
            },
        },
    )


def _log_budget_unblocked(
    rule: BudgetRule,
    block: BudgetBlockRecord,
    reason: str,
    route: GatewayRoute,
    endpoint: str,
    request_id: str,
    trace_id: str,
) -> None:
    """记录数据面防御性自动恢复事件，不包含请求正文或密钥。"""

    logger.info(
        "budget.unblocked",
        extra={
            "event": "budget.unblocked",
            "request_id": request_id,
            "trace_id": trace_id,
            "tenant_id": rule.tenant_id,
            "actor_id": None,
            "object_id": block.budget_block_id,
            "payload": {
                "budget_rule_id": rule.budget_rule_id,
                "environment_id": rule.environment_id,
                "scope_type": rule.scope_type,
                "scope_id": rule.scope_id,
                "budget_date": block.budget_date,
                "reason_code": reason,
                "endpoint": endpoint,
                "model_id": route.model_id,
                "provider_id": route.provider_id,
            },
        },
    )


def _log_budget_unavailable(
    auth_context: GatewayAuthContext,
    route: GatewayRoute,
    endpoint: str,
    request_id: str,
    trace_id: str,
    exc: Exception,
) -> None:
    """记录预算保护不可用事件，隐藏数据库路径和 SQL 细节。"""

    logger.warning(
        "budget.unavailable",
        extra={
            "event": "budget.unavailable",
            "request_id": request_id,
            "trace_id": trace_id,
            "tenant_id": auth_context.tenant_id,
            "actor_id": auth_context.virtual_key_id,
            "object_id": request_id,
            "payload": {
                "endpoint": endpoint,
                "model_id": route.model_id,
                "provider_id": route.provider_id,
                "exception_type": exc.__class__.__name__,
            },
        },
    )
