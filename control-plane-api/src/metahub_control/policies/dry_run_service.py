"""Policy Dry-Run 的只读快照计算、安全事实事务与提交后投递。

Prompt 只作为 ``run_policy_dry_run`` 调用栈中的局部值传给纯 DLP 和字符计数函数。本模块
不会记录、持久化、hash 或回显 Prompt，也不会调用 Gateway 的路由决策、DLP Guard、预算
Guard、Virtual Key、Adapter 或 KeyPool 入口。完整解释只在当前 HTTP 响应内存中存在。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.budget_spend import MICROS_PER_CNY, load_budget_spend_amounts
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.policy_dsl import (
    DSL_VERSION,
    PolicyContextError,
    PolicyDslError,
    PolicyEvaluationInput,
    PolicyEvaluationResult,
    compile_policy_expression,
    evaluate_policy_set,
)
from metahub_shared.runtime.policy_preview import (
    BudgetPreviewResult,
    BudgetRuleFact,
    CandidatePolicyFact,
    DlpPreviewResult,
    DlpRuleFact,
    PriceFact,
    RouteCandidateFact,
    RouteCandidatePreview,
    RoutePreviewResult,
    fixed_money,
    parse_nonnegative_decimal,
    policy_candidate_fingerprint,
    preview_budget,
    preview_dlp,
    preview_route,
)
from metahub_shared.runtime.routing_policy import (
    QualityCostConfig,
    RoutingCandidateInput,
    RoutingPriceFact,
    RoutingQualityFact,
    RoutingSelectionResult,
    TokenEstimate,
    estimate_request_tokens,
    select_routing_candidates,
)
from metahub_shared.runtime.webhooks import build_webhook_event

from metahub_control.audit import (
    AuditRecord,
    AuditRequestMeta,
    dispatch_audit_recorded,
    record_audit_log,
)
from metahub_control.auth import AuthContext

from .models import (
    PolicyDryRunBudgetPreview,
    PolicyDryRunBudgetRule,
    PolicyDryRunCheck,
    PolicyDryRunComparison,
    PolicyDryRunCoverage,
    PolicyDryRunDecisionTree,
    PolicyDryRunDlpMatch,
    PolicyDryRunDlpPreview,
    PolicyDryRunInputSummary,
    PolicyDryRunPolicyPreview,
    PolicyDryRunRequest,
    PolicyDryRunResponse,
    PolicyDryRunRouteCandidate,
    PolicyDryRunRoutePreview,
    PolicyDryRunTreeNode,
    PolicyExplanation,
)

logger = logging.getLogger("metahub.control.policies.dry_run")

# 业务日时区、资源上限和审计固定字段共同限定预演的成本与安全摘要边界。
SHANGHAI = ZoneInfo("Asia/Shanghai")
MAX_ACTIVE_POLICIES = 200
MAX_DLP_RULES = 100
AUDIT_SAFE_FIELDS = frozenset(
    {
        "policy_explanation_id",
        "policy_set_fingerprint",
        "decision",
        "decision_reason_code",
        "matched_policy_id",
        "matched_priority",
        "matched_action",
        "route_status",
        "dlp_status",
        "budget_status",
        "would_error_code",
        "checked_policy_count",
        "runtime_compatible",
        "coverage_complete",
    }
)


@dataclass(frozen=True, slots=True)
class _RouteSource:
    """同一只读快照内已验证权威性的请求或策略路由来源。"""

    candidates: tuple[RouteCandidateFact, ...]
    routing_reason_prefix: str
    unavailable_error_code: str
    runtime_fallback_enabled: bool
    model_alias_id: str | None = None
    routing_policy_id: str | None = None
    routing_policy_version: int | None = None
    routing_mode: str | None = None
    quality_config: QualityCostConfig | None = None


@dataclass(frozen=True, slots=True)
class _CandidateSet:
    """重编译后的 active candidate、纯求值输入和发布兼容性。"""

    policies: tuple[PolicyEvaluationInput, ...]
    fingerprint: str
    runtime_compatible: bool
    incompatibility_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _DryRunEvent:
    """提交后重派所需的持久 Policy 事件安全事实。"""

    policy_event_id: str
    tenant_id: str
    actor_user_id: str
    explanation_id: str
    request_id: str
    trace_id: str
    payload: dict[str, object]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class _PersistentFacts:
    """按 exactly-once 键权威回读且已交叉核验的三类持久事实。"""

    explanation_row: sqlite3.Row
    event: _DryRunEvent
    audit: AuditRecord


@dataclass(frozen=True, slots=True)
class _PersistResult:
    """写事务提交或重复恢复后的投递材料。"""

    explanation: PolicyExplanation | None
    event: _DryRunEvent
    audit: AuditRecord
    reused: bool


def run_policy_dry_run(
    config: AppConfig,
    context: AuthContext,
    payload: PolicyDryRunRequest,
    *,
    audit_request_meta: AuditRequestMeta,
    request_id: str,
    trace_id: str,
    recovery_checked: bool = False,
) -> PolicyDryRunResponse:
    """执行一次 active candidate 预演，并原子写入三类安全事实。

    读取与写入使用两条连接：第一条强制 query-only 并在一个 SQLite 快照内完成所有
    权威读取和纯计算；第二条持有写锁后重查 exactly-once 键，再写 explanation、event、
    audit。任何 Prompt 派生值都不会传入第二条连接或投递函数。
    """

    _validate_chain_id(request_id)
    _validate_chain_id(trace_id)
    if not recovery_checked:
        recover_completed_dry_run(
            config,
            context,
            request_id=request_id,
            trace_id=trace_id,
        )
    explanation = _compute_in_query_only_snapshot(config, context, payload)
    persisted = _persist_safe_facts(
        config,
        context,
        explanation,
        audit_request_meta=audit_request_meta,
        request_id=request_id,
        trace_id=trace_id,
    )
    # 两种 Webhook 都在数据库提交或重复事实核验完成后尽力重派，外部失败不改变 API 结果。
    dispatch_audit_recorded(
        config,
        context,
        persisted.audit,
        request_id=request_id,
        trace_id=trace_id,
    )
    _dispatch_dry_run_event(config, persisted.event)
    if persisted.reused:
        # 数据库只保存安全摘要，无法重建旧完整解释；重复请求固定返回 409 而不回放正文结果。
        raise ApiError("policy.dry_run_request_id_reused")
    if persisted.explanation is None:
        raise ApiError("policy.dry_run_unavailable")
    return PolicyDryRunResponse(
        policy_explanation=persisted.explanation,
        request_id=request_id,
        trace_id=trace_id,
    )


def recover_completed_dry_run(
    config: AppConfig,
    context: AuthContext,
    *,
    request_id: str,
    trace_id: str,
) -> None:
    """在读取当前配置前恢复已提交三事实，重派稳定事件后固定返回 409。

    首次请求可能在 COMMIT 后、Webhook dispatch 前退出。重试时 Environment、Project、模型
    或 candidate 已变化也不能阻止恢复，因此本检查只读取旧安全事实，不依赖当前请求正文和
    当前配置。写事务仍会再次检查同一键，解决本检查之后两个并发请求同时计算的竞态。
    """

    _validate_chain_id(request_id)
    _validate_chain_id(trace_id)
    connection = _connect(config)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        existing = _load_persistent_facts(
            connection,
            tenant_id=context.user.tenant_id,
            actor_user_id=context.user.user_id,
            request_id=request_id,
        )
        connection.rollback()
    # 三事实核验产生的领域错误必须原样保留，不能被恢复路径改写成当前配置错误。
    except ApiError:
        _safe_rollback(connection)
        raise
    # 恢复读取依赖失败时无法确认稳定事件，统一失败关闭且不读取请求正文。
    except (DbError, sqlite3.Error, ValueError, TypeError):
        _safe_rollback(connection)
        raise ApiError("policy.dry_run_unavailable") from None
    finally:
        connection.close()
    # 没有旧事实时由调用方继续限流、读正文和计算，不能凭空创建恢复事件。
    if existing is None:
        return
    dispatch_audit_recorded(
        config,
        context,
        existing.audit,
        request_id=existing.event.request_id,
        trace_id=existing.event.trace_id,
    )
    _dispatch_dry_run_event(config, existing.event)
    # 旧完整解释无法从安全摘要重建，稳定重派之后只能返回明确 request_id 冲突。
    raise ApiError("policy.dry_run_request_id_reused")


def _compute_in_query_only_snapshot(
    config: AppConfig,
    context: AuthContext,
    payload: PolicyDryRunRequest,
) -> PolicyExplanation:
    """在单一 query-only 事务中读取所有配置并构造仅存在于内存的完整解释。"""

    connection = _connect(config)
    now = _utcnow()
    try:
        # query_only 是零真实成本的数据库级保护，必须在 BEGIN 前打开，不能只依赖代码约定。
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        tenant_id = context.user.tenant_id
        environment = _load_active_environment(connection, tenant_id, payload.environment_id)
        _require_active_project(connection, tenant_id, payload.project_id)
        requested_source = _load_requested_route_source(
            connection,
            tenant_id,
            environment_id=str(environment["environment_id"]),
            environment_code=str(environment["code"]),
            requested_model=payload.model,
            now=now,
            health_stale_seconds=config.provider_health_stale_seconds,
        )
        candidate = _load_and_compile_candidate(connection, tenant_id)
        budget_rules = _load_budget_rule_facts(
            connection,
            tenant_id=tenant_id,
            environment_id=payload.environment_id,
            project_id=payload.project_id,
            now=now,
        )
        policy_context: dict[str, object] = {
            "tenant": tenant_id,
            "env": str(environment["code"]),
            "project": payload.project_id,
            "model": payload.model,
            "data_class": payload.data_class,
        }
        if (
            any(
                "budget_remaining" in item.compiled_expression.variables
                for item in candidate.policies
            )
            and budget_rules
        ):
            remaining = min(
                max(
                    Decimal("0"),
                    rule.limit_cny - rule.spent_cny - rule.reserved_cny,
                )
                for rule in budget_rules
            )
            policy_context["budget_remaining"] = fixed_money(remaining)
        evaluated = _evaluate_candidate(config, candidate, policy_context)
        published_version = _load_published_snapshot_version(connection, tenant_id)
        explanation = _build_explanation(
            connection,
            config,
            context,
            payload,
            candidate,
            evaluated,
            requested_source=requested_source,
            budget_rules=budget_rules,
            published_snapshot_version=published_version,
            now=now,
        )
        # 无论成功或模拟业务错误都回滚只读事务，显式证明没有依赖隐式 close 提交语义。
        connection.rollback()
        return explanation
    # 已分类上下文错误保留防枚举 HTTP 语义，同时仍需显式释放 query-only 事务。
    except ApiError:
        _safe_rollback(connection)
        raise
    # 图、金额、JSON 或数据库异常均表示当前快照不可证明，不能返回部分 what-if。
    except (DbError, sqlite3.Error, PolicyDslError, PolicyContextError, ValueError, TypeError):
        _safe_rollback(connection)
        raise ApiError("policy.dry_run_unavailable") from None
    finally:
        connection.close()


def _build_explanation(
    connection: sqlite3.Connection,
    config: AppConfig,
    context: AuthContext,
    payload: PolicyDryRunRequest,
    candidate: _CandidateSet,
    evaluated: PolicyEvaluationResult,
    *,
    requested_source: _RouteSource,
    budget_rules: tuple[BudgetRuleFact, ...],
    published_snapshot_version: int | None,
    now: datetime,
) -> PolicyExplanation:
    """按 Policy -> Route -> DLP -> Budget 的真实短路顺序组装强类型响应。"""

    policy_preview = _policy_preview(evaluated)
    route_preview = _not_evaluated_route(payload.model, "policy.stage_short_circuited")
    dlp_preview = _not_evaluated_dlp("policy.stage_short_circuited")
    budget_preview = _not_evaluated_budget("policy.stage_short_circuited")
    would_error_code = _policy_would_error(evaluated)
    # Dry-Run prompt 精确映射为单条 Chat content；路由成本、预算与输入摘要共用同一估算对象。
    canonical_token_estimate = estimate_request_tokens(
        "chat",
        {
            "messages": [{"role": "user", "content": payload.prompt}],
            "max_completion_tokens": payload.max_output_tokens,
        },
    )

    route_result: RoutePreviewResult | None = None
    # mask 是非终止 modifier；只有决定性 deny、上下文或超时错误才阻断配置路由阶段。
    if would_error_code is None:
        route_source = requested_source
        # route_to 必须完全替换请求模型来源，目标失败时绝不能回退改变策略语义。
        if evaluated.decision == "route_to":
            # 缺少目标表示持久动作图损坏，只能模拟 Gateway 的 action unsupported。
            if not evaluated.target_model_id:
                would_error_code = "gateway.policy_action_unsupported"
            else:
                route_source = _load_policy_target_route_source(
                    connection,
                    context.user.tenant_id,
                    evaluated.target_model_id,
                )
        # 仍无策略错误时才做纯路由选择，所有候选继续来自同一 query-only 快照。
        if would_error_code is None:
            route_result = _preview_route_source(
                connection,
                context.user.tenant_id,
                route_source,
                now=now,
                health_stale_seconds=config.provider_health_stale_seconds,
                token_estimate=canonical_token_estimate,
            )
            route_preview = _route_preview(payload.model, route_result)
            # 配置/健康没有选中候选时后续 DLP 与预算都必须显式短路。
            if route_result.status != "selected" or route_result.selected_model_id is None:
                would_error_code = (
                    route_result.would_error_code or "gateway.policy_route_unavailable"
                )
                dlp_preview = _not_evaluated_dlp("route.stage_short_circuited")
                budget_preview = _not_evaluated_budget("route.stage_short_circuited")
            # Chat 能力在初始路由后校验，不能借此改选预检 fallback。
            elif route_result.would_error_code is not None:
                would_error_code = route_result.would_error_code
                dlp_preview = _not_evaluated_dlp("route.chat_capability_unsupported")
                budget_preview = _not_evaluated_budget("route.chat_capability_unsupported")

    # 路由及 Chat 均可继续时，才允许把局部 Prompt 交给纯 DLP 扫描函数。
    if would_error_code is None and route_result is not None:
        dlp_rules = _load_dlp_rule_facts(connection, context.user.tenant_id)
        dlp_result = preview_dlp(payload.prompt, dlp_rules)
        dlp_preview = _dlp_preview(dlp_result)
        if dlp_result.status == "blocked":
            would_error_code = "gateway.dlp_blocked"
            budget_preview = _not_evaluated_budget("dlp.stage_short_circuited")
        elif dlp_result.status == "not_evaluated":
            would_error_code = "gateway.dlp_unavailable"
            budget_preview = _not_evaluated_budget("dlp.stage_unavailable")

    # DLP 未阻断且可用时才读取价格并计算预算，禁止为短路请求创建成本幻觉。
    if would_error_code is None and route_result is not None:
        primary_price = _load_effective_price(
            connection,
            context.user.tenant_id,
            route_result.selected_model_id,
            now,
        )
        fallback_price = (
            _load_effective_price(
                connection,
                context.user.tenant_id,
                route_result.runtime_fallback_model_id,
                now,
            )
            if route_result.runtime_fallback_model_id is not None
            else None
        )
        budget_result = preview_budget(
            len(payload.prompt),
            payload.max_output_tokens,
            primary_price,
            budget_rules,
            fallback_price=fallback_price,
            token_estimate=canonical_token_estimate,
        )
        budget_preview = _budget_preview(budget_result)
        if budget_result.blocked:
            would_error_code = "gateway.budget_exhausted"

    # outcome 只按最终 Gateway 错误分类，context missing 不能被 decision=deny 误标。
    if would_error_code is None:
        outcome = "would_allow"
    # 只有真实 policy denied 语义属于 would_deny，其他策略失败都归 would_error。
    elif would_error_code == "gateway.policy_denied":
        outcome = "would_deny"
    # DLP 和预算硬阻断对页面统一表达为 would_block，仍保持 HTTP 200。
    elif would_error_code in {"gateway.dlp_blocked", "gateway.budget_exhausted"}:
        outcome = "would_block"
    else:
        outcome = "would_error"
    input_tokens = canonical_token_estimate.input_tokens
    coverage = PolicyDryRunCoverage(
        policy=True,
        model_provider_routing=True,
        chat_capability=True,
        dlp=True,
        tenant_project_budget=True,
        virtual_key_application_budget=False,
        rpm_tpm_concurrency_limits=False,
        provider_credentials_keypool=False,
        real_provider=False,
        not_simulated=[
            "application_budget",
            "virtual_key_budget",
            "rpm_tpm_concurrency_limits",
            "provider_credentials_keypool",
            "real_provider_call",
        ],
    )
    tree = _decision_tree(policy_preview, route_preview, dlp_preview, budget_preview)
    return PolicyExplanation(
        # ID 与持久时间只由第二写事务确定；占位值不会离开当前函数并会在入库前替换。
        policy_explanation_id="pexp_" + "0" * 32,
        tenant_id=context.user.tenant_id,
        status="completed",
        version=1,
        policy_source="active_candidate",
        policy_set_fingerprint=candidate.fingerprint,
        published_snapshot_version=published_snapshot_version,
        simulation_only=True,
        runtime_compatible=candidate.runtime_compatible,
        runtime_incompatibility_codes=list(candidate.incompatibility_codes),
        gateway_parity="partial",
        coverage_complete=False,
        provider_called=False,
        usage_recorded=False,
        cost_recorded=False,
        outcome=outcome,
        would_error_code=would_error_code,
        decision_tree=tree,
        policy=policy_preview,
        route=route_preview,
        dlp=dlp_preview,
        budget=budget_preview,
        coverage=coverage,
        input_summary=PolicyDryRunInputSummary(
            prompt_redacted=True,
            prompt_codepoints=len(payload.prompt),
            estimated_input_tokens=input_tokens,
        ),
        created_at=now,
        updated_at=now,
    )


def _load_and_compile_candidate(
    connection: sqlite3.Connection,
    tenant_id: str,
) -> _CandidateSet:
    """重编译全部 active Policy，并核对持久 AST/hash/variables 和一对一图完整性。"""

    rows = connection.execute(
        """
        SELECT
          p.policy_id, p.version AS policy_version, p.priority, p.dsl_version,
          r.policy_rule_id, r.condition_expr, r.condition_ast_json,
          r.condition_hash, r.variables_json,
          a.policy_action_id, a.action_type, a.target_model_id
        FROM policies p
        LEFT JOIN policy_rules r
          ON r.tenant_id=p.tenant_id AND r.policy_id=p.policy_id
        LEFT JOIN policy_actions a
          ON a.tenant_id=p.tenant_id AND a.policy_id=p.policy_id
        WHERE p.tenant_id=? AND p.status='active'
        ORDER BY p.priority ASC, p.policy_id ASC
        LIMIT ?
        """,
        (tenant_id, MAX_ACTIVE_POLICIES + 1),
    ).fetchall()
    # active candidate 超过 DSL 既有硬上限时不能截断，否则 fingerprint 与发布器会分叉。
    if len(rows) > MAX_ACTIVE_POLICIES:
        raise ValueError("active policy count exceeded")
    policies: list[PolicyEvaluationInput] = []
    fingerprint_facts: list[CandidatePolicyFact] = []
    incompatibility_codes: set[str] = set()
    seen_policy: set[str] = set()
    seen_rule: set[str] = set()
    seen_action: set[str] = set()
    for row in rows:
        required = (
            row["policy_id"],
            row["policy_rule_id"],
            row["policy_action_id"],
            row["condition_expr"],
            row["condition_ast_json"],
            row["condition_hash"],
            row["variables_json"],
            row["action_type"],
        )
        # Rule/Action 缺失或 DSL 版本漂移表示一对一图不可证明，必须关闭整个预演。
        if any(value is None for value in required) or row["dsl_version"] != DSL_VERSION:
            raise ValueError("incomplete policy graph")
        policy_id = str(row["policy_id"])
        rule_id = str(row["policy_rule_id"])
        action_id = str(row["policy_action_id"])
        # 重复来源会让求值与指纹出现覆盖歧义，不能依赖排序掩盖损坏数据。
        if policy_id in seen_policy or rule_id in seen_rule or action_id in seen_action:
            raise ValueError("duplicate policy graph source")
        seen_policy.add(policy_id)
        seen_rule.add(rule_id)
        seen_action.add(action_id)
        compiled = compile_policy_expression(str(row["condition_expr"]))
        variables = json.loads(str(row["variables_json"]))
        persisted_ast = json.loads(str(row["condition_ast_json"]))
        # 重编译结果必须逐字段等于持久事实，拒绝语义近似但制品已漂移的 candidate。
        if (
            type(variables) is not list
            or variables != list(compiled.variables)
            or persisted_ast != compiled.ast
            or str(row["condition_ast_json"]) != compiled.canonical_json
            or str(row["condition_hash"]) != compiled.condition_hash
        ):
            # 条件正文、AST、变量或 hash 任一漂移都必须拒绝，不能返回部分候选解释。
            raise ValueError("compiled policy mismatch")
        action_type = str(row["action_type"])
        target_model_id = (
            str(row["target_model_id"]) if row["target_model_id"] is not None else None
        )
        policy = PolicyEvaluationInput(
            policy_id=policy_id,
            policy_rule_id=rule_id,
            policy_action_id=action_id,
            priority=int(row["priority"]),
            compiled_expression=compiled,
            action_type=action_type,  # type: ignore[arg-type]
            target_model_id=target_model_id,
        )
        policies.append(policy)
        fingerprint_facts.append(
            CandidatePolicyFact(
                policy_id=policy_id,
                policy_version=int(row["policy_version"]),
                policy_rule_id=rule_id,
                policy_action_id=action_id,
                priority=int(row["priority"]),
                condition_hash=compiled.condition_hash,
                action_type=action_type,
                target_model_id=target_model_id,
            )
        )
        if "data_class" in compiled.variables:
            incompatibility_codes.add("policy_snapshot.variable_unsupported")
        # runtime compatibility 按整个候选复核 route target，而非只检查本次命中规则。
        if action_type == "route_to":
            active_target = connection.execute(
                """
                SELECT 1 FROM models
                WHERE tenant_id=? AND model_id=? AND status='active'
                """,
                (tenant_id, target_model_id),
            ).fetchone()
            # 发布器按整个 candidate 复核所有 route_to，即使该规则本次 what-if 未命中也会拒绝。
            if active_target is None:
                incompatibility_codes.add("policy_snapshot.route_target_invalid")
    codes = tuple(sorted(incompatibility_codes))
    facts = tuple(fingerprint_facts)
    return _CandidateSet(
        policies=tuple(policies),
        fingerprint=policy_candidate_fingerprint(tenant_id, facts),
        runtime_compatible=not codes,
        incompatibility_codes=codes,
    )


def _evaluate_candidate(
    config: AppConfig,
    candidate: _CandidateSet,
    policy_context: dict[str, object],
) -> PolicyEvaluationResult:
    """复用唯一 shared DSL 求值器；可信上下文异常按 context missing 失败关闭。"""

    try:
        return evaluate_policy_set(
            candidate.policies,
            policy_context,
            timeout_ms=config.policy_evaluation_timeout_ms,
        )
    # 可信上下文类型异常不能降级为默认 allow，必须交给上层统一失败关闭。
    except PolicyContextError:
        raise


def _load_active_environment(
    connection: sqlite3.Connection,
    tenant_id: str,
    environment_id: str,
) -> sqlite3.Row:
    """按认证 tenant 校验 Environment；跨租户、禁用和不存在使用同一防枚举错误。"""

    row = connection.execute(
        """
        SELECT environment_id, code
        FROM environments
        WHERE tenant_id=? AND environment_id=? AND status='active'
        """,
        (tenant_id, environment_id),
    ).fetchone()
    # 跨租户、禁用和不存在共享固定 404，避免通过 ID 枚举 Environment。
    if row is None:
        raise ApiError("policy.dry_run_context_not_found")
    tenant = connection.execute(
        "SELECT tenant_id FROM tenants WHERE tenant_id=? AND status='active'",
        (tenant_id,),
    ).fetchone()
    # 已认证 tenant 若在读取期间失效，同样不能继续返回基于旧权威性的解释。
    if tenant is None:
        raise ApiError("policy.dry_run_context_not_found")
    return row


def _require_active_project(
    connection: sqlite3.Connection,
    tenant_id: str,
    project_id: str,
) -> None:
    """Project 与 Environment 相互独立校验，但都必须属于同一认证 tenant。"""

    row = connection.execute(
        "SELECT project_id FROM projects WHERE tenant_id=? AND project_id=? AND status='active'",
        (tenant_id, project_id),
    ).fetchone()
    # Project 不存在、禁用或跨租户统一不可见，Environment 选择不能替代该校验。
    if row is None:
        raise ApiError("policy.dry_run_context_not_found")


def _load_requested_route_source(
    connection: sqlite3.Connection,
    tenant_id: str,
    *,
    environment_id: str,
    environment_code: str,
    requested_model: str,
    now: datetime,
    health_stale_seconds: int,
) -> _RouteSource:
    """先按 Environment 解析 active alias，再兼容同租户 direct internal model 路径。"""

    alias_rows = connection.execute(
        """
        SELECT
          a.model_alias_id, a.default_model_id, a.fallback_model_id, a.status,
          rp.routing_policy_id, rp.routing_mode, rp.version AS routing_policy_version,
          rp.status AS routing_policy_status,
          rp.quality_weight_bps, rp.cost_weight_bps, rp.latency_weight_bps,
          rp.quality_floor_bps, rp.minimum_sample_count
        FROM model_aliases a
        LEFT JOIN model_alias_routing_policies rp
          ON rp.tenant_id=a.tenant_id AND rp.model_alias_id=a.model_alias_id
        WHERE a.tenant_id=? AND a.alias=? AND a.environment=?
          AND (a.environment_id=? OR a.environment_id IS NULL)
        LIMIT 2
        """,
        (tenant_id, requested_model, environment_code, environment_id),
    ).fetchall()
    # 两条同环境 alias 表示唯一约束或历史图损坏，不能任意选择其中一条。
    if len(alias_rows) > 1:
        raise ValueError("ambiguous model alias")
    if alias_rows:
        alias = alias_rows[0]
        # 禁用 alias 与不存在模型共享上下文 404，阻止控制台探测内部配置状态。
        if alias["status"] != "active":
            raise ApiError("policy.dry_run_context_not_found")
        facts = tuple(
            _load_route_candidate(
                connection,
                tenant_id,
                role,
                str(model_id),
                now=now,
                health_stale_seconds=health_stale_seconds,
                evaluate_keypool=alias["routing_mode"]
                in {
                    "cost_first",
                    "availability_first",
                    "quality_cost",
                },
            )
            for role, model_id in (
                ("default", alias["default_model_id"]),
                ("fallback", alias["fallback_model_id"]),
            )
            if model_id is not None
        )
        runtime_enabled = _runtime_fallback_enabled(
            connection, tenant_id, str(alias["model_alias_id"])
        )
        policy_id = alias["routing_policy_id"]
        policy_mode = alias["routing_mode"]
        policy_version = alias["routing_policy_version"]
        if policy_id is not None and (
            type(policy_id) is not str
            or type(policy_mode) is not str
            or policy_mode not in {"fixed", "cost_first", "availability_first", "quality_cost"}
            or type(policy_version) is not int
            or policy_version < 1
            or alias["routing_policy_status"] != "enabled"
        ):
            # 持久 Policy 图不完整时预演不能合成 fixed，否则会与 Gateway 真实执行分叉。
            raise ValueError("routing policy graph invalid")
        quality_config = None
        if policy_mode == "quality_cost":
            # 配置来自同一个 query-only 快照；不默认、不截断，交由 shared 强校验失败关闭。
            quality_config = QualityCostConfig(
                quality_weight_bps=alias["quality_weight_bps"],
                cost_weight_bps=alias["cost_weight_bps"],
                latency_weight_bps=alias["latency_weight_bps"],
                quality_floor_bps=alias["quality_floor_bps"],
                minimum_sample_count=alias["minimum_sample_count"],
            )
        return _RouteSource(
            candidates=facts,
            routing_reason_prefix="alias",
            unavailable_error_code=_requested_route_error(
                facts, now=now, stale_seconds=health_stale_seconds
            ),
            runtime_fallback_enabled=runtime_enabled,
            model_alias_id=str(alias["model_alias_id"]),
            routing_policy_id=str(policy_id) if policy_id is not None else None,
            routing_policy_version=(int(policy_version) if policy_version is not None else None),
            routing_mode=str(policy_mode) if policy_mode is not None else None,
            quality_config=quality_config,
        )

    direct = connection.execute(
        """
        SELECT model_id, status
        FROM models
        WHERE tenant_id=? AND model_alias=?
        LIMIT 2
        """,
        (tenant_id, requested_model),
    ).fetchall()
    # direct model 必须在当前 tenant 唯一且 active，不能穿透到其他租户同名模型。
    if len(direct) != 1 or direct[0]["status"] != "active":
        raise ApiError("policy.dry_run_context_not_found")
    fact = _load_route_candidate(
        connection,
        tenant_id,
        "direct",
        str(direct[0]["model_id"]),
        now=now,
        health_stale_seconds=health_stale_seconds,
    )
    return _RouteSource(
        candidates=(fact,),
        routing_reason_prefix="direct_model",
        unavailable_error_code=_requested_route_error(
            (fact,), now=now, stale_seconds=health_stale_seconds
        ),
        runtime_fallback_enabled=False,
    )


def _load_policy_target_route_source(
    connection: sqlite3.Connection,
    tenant_id: str,
    target_model_id: str,
) -> _RouteSource:
    """Policy route_to 目标始终用 tenant-qualified model_id 解析，失败不回退请求模型。"""

    return _RouteSource(
        candidates=(_load_route_candidate(connection, tenant_id, "target", target_model_id),),
        routing_reason_prefix="policy.route_to",
        unavailable_error_code="gateway.policy_route_unavailable",
        runtime_fallback_enabled=False,
    )


def _load_route_candidate(
    connection: sqlite3.Connection,
    tenant_id: str,
    role: str,
    model_id: str,
    *,
    now: datetime | None = None,
    health_stale_seconds: int = 300,
    evaluate_keypool: bool = False,
) -> RouteCandidateFact:
    """读取单个安全路由候选；跨租户或悬空目标不暴露其他租户 Provider 元数据。"""

    row = connection.execute(
        """
        SELECT
          m.model_id, m.model_alias, m.status AS model_status, m.capabilities,
          p.provider_id, p.provider_code, p.status AS provider_status,
          p.auth_type AS provider_auth_type,
          p.health_status AS provider_health_status,
          p.health_updated_at AS provider_health_updated_at
        FROM models m
        LEFT JOIN providers p
          ON p.tenant_id=m.tenant_id AND p.provider_id=m.provider_id
        WHERE m.tenant_id=? AND m.model_id=?
        """,
        (tenant_id, model_id),
    ).fetchone()
    if row is None:
        # ID 来自当前租户 alias/Policy 配置，但图已损坏；使用固定占位避免跨租户枚举。
        return RouteCandidateFact(
            role=role,
            model_id="unavailable",
            internal_model_id=None,
            model_status="missing",
            provider_id=None,
            provider_code=None,
            provider_status=None,
            provider_health_status=None,
            provider_health_updated_at=None,
            capabilities={},
            provider_auth_type=None,
            keypool_state="not_evaluated",
        )
    capabilities = json.loads(str(row["capabilities"] or "{}"))
    if type(capabilities) is not dict:
        capabilities = {}
    keypool_state = "not_evaluated"
    if evaluate_keypool and now is not None:
        health_state, health_fresh = _dry_run_health_state(
            row["provider_health_status"],
            row["provider_health_updated_at"],
            now=now,
            stale_seconds=health_stale_seconds,
        )
        generic_eligible = (
            row["model_status"] == "active"
            and row["provider_status"] == "active"
            and not (health_state == "unhealthy" and health_fresh)
        )
        if generic_eligible:
            keypool_state = _dry_run_keypool_state(
                connection,
                tenant_id,
                provider_id=str(row["provider_id"]),
                provider_auth_type=str(row["provider_auth_type"]),
                now=now,
                stale_seconds=health_stale_seconds,
            )
    return RouteCandidateFact(
        role=role,
        model_id=str(row["model_id"]),
        internal_model_id=str(row["model_alias"]),
        model_status=str(row["model_status"]),
        provider_id=str(row["provider_id"]) if row["provider_id"] is not None else None,
        provider_code=(str(row["provider_code"]) if row["provider_code"] is not None else None),
        provider_status=(
            str(row["provider_status"]) if row["provider_status"] is not None else None
        ),
        provider_health_status=(
            str(row["provider_health_status"])
            if row["provider_health_status"] is not None
            else None
        ),
        provider_health_updated_at=(
            str(row["provider_health_updated_at"])
            if row["provider_health_updated_at"] is not None
            else None
        ),
        capabilities=capabilities,
        provider_auth_type=(
            str(row["provider_auth_type"]) if row["provider_auth_type"] is not None else None
        ),
        keypool_state=keypool_state,
    )


def _dry_run_health_state(
    raw_state: object,
    raw_updated_at: object,
    *,
    now: datetime,
    stale_seconds: int,
) -> tuple[str, bool]:
    """按与 Gateway 相同的 as_of 归类健康状态，坏时间不能伪装 stale Fail Open。"""

    state = str(raw_state or "unknown")
    if state not in {"healthy", "unhealthy", "unknown"}:
        # 非法健康枚举不能被预演伪装成 unknown Fail Open。
        raise ValueError("provider health state invalid")
    if not raw_updated_at:
        return "unknown", False
    checked_at = _dry_run_db_time(raw_updated_at)
    if checked_at > now:
        # 未来时间不属于当前 as_of，拒绝后才能保持与 Gateway 的冻结语义一致。
        raise ValueError("provider health timestamp invalid")
    # unknown 不参与排序，但非空时间格式仍属于快照完整性，必须先完成解析校验。
    if state == "unknown":
        return "unknown", False
    return state, checked_at >= now - timedelta(seconds=stale_seconds)


def _dry_run_keypool_state(
    connection: sqlite3.Connection,
    tenant_id: str,
    *,
    provider_id: str,
    provider_auth_type: str,
    now: datetime,
    stale_seconds: int,
) -> str:
    """无解密读取托管 Key 资格；无托管 Key 时诚实返回 unknown 环境覆盖。"""

    # 无认证 Provider 不依赖 Key，Control 可确定为 available 而非 partial unknown。
    if provider_auth_type == "none":
        return "available"
    rows = connection.execute(
        """
        SELECT encrypted_key, status, rate_limited_until,
               probe_health_status, probe_last_tested_at
        FROM provider_credentials
        WHERE tenant_id=? AND provider_id=?
        """,
        (tenant_id, provider_id),
    ).fetchall()
    managed = [row for row in rows if str(row["encrypted_key"] or "").startswith("aesgcm-v1:")]
    if not managed:
        # Control 进程不能证明 Gateway 是否配置环境 Key；unknown 保持 parity=partial。
        return "unknown"
    # 必须先逐行完成强校验，再运行 any；否则首个 available 行会让生成器短路，
    # 后续坏枚举或未来时间被 Dry-Run 忽略，而 Gateway 会把同一快照判为 invalid。
    for row in managed:
        _validate_dry_run_managed_key_snapshot(row, now=now)
    return (
        "available"
        if any(
            _dry_run_managed_key_selectable(
                row,
                now=now,
                stale_seconds=stale_seconds,
            )
            for row in managed
        )
        else "unavailable"
    )


def _validate_dry_run_managed_key_snapshot(
    row: sqlite3.Row,
    *,
    now: datetime,
) -> None:
    """严格验证托管 Key 的只读状态、探测时间和冷却时间。

    参数：当前租户/Provider 的密文凭据行与本次统一 ``as_of``。返回：合法时无返回值。
    约束：不解密、不写库；任一坏行都使完整预演失败，保持与 Gateway 快照一致。
    """

    probe_state = str(row["probe_health_status"] or "")
    if probe_state not in {"unknown", "healthy", "unhealthy"}:
        # 非法探测枚举若降级为 unknown，会让损坏凭据参与真实动态路由。
        raise ValueError("provider key health invalid")
    credential_status = str(row["status"] or "")
    if credential_status not in {"active", "disabled", "rate_limited"}:
        # 凭据状态直接控制资格，不能把未知值静默当作 unavailable。
        raise ValueError("provider key status invalid")
    probe_time = (
        _dry_run_db_time(row["probe_last_tested_at"]) if row["probe_last_tested_at"] else None
    )
    if probe_time is not None and probe_time > now:
        # 未来探测不属于当前冻结时点，不能参与 freshness 判断。
        raise ValueError("provider key probe timestamp invalid")
    # 冷却字段即使当前状态不消费也要验证格式，避免同一坏行随状态变化时而成功。
    if row["rate_limited_until"]:
        _dry_run_db_time(row["rate_limited_until"])


def _dry_run_managed_key_selectable(
    row: sqlite3.Row,
    *,
    now: datetime,
    stale_seconds: int,
) -> bool:
    """按 Key 健康与冷却状态判断资格，不更新 last_used 或执行解密。"""

    probe_state = str(row["probe_health_status"] or "unknown")
    probe_time = (
        _dry_run_db_time(row["probe_last_tested_at"]) if row["probe_last_tested_at"] else None
    )
    if (
        probe_state == "unhealthy"
        and probe_time is not None
        and probe_time >= now - timedelta(seconds=stale_seconds)
    ):
        return False
    status = str(row["status"] or "")
    if status == "active":
        return True
    if status != "rate_limited":
        return False
    cooldown = _dry_run_db_time(row["rate_limited_until"]) if row["rate_limited_until"] else None
    return cooldown is None or cooldown <= now


def _dry_run_db_time(value: object) -> datetime:
    """解析 SQLite UTC 时间；无时区 datetime('now') 按仓库既有 UTC 约定恢复。"""

    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _preview_route_source(
    connection: sqlite3.Connection,
    tenant_id: str,
    source: _RouteSource,
    *,
    now: datetime,
    health_stale_seconds: int,
    token_estimate: TokenEstimate,
) -> RoutePreviewResult:
    """对持久 Alias Policy 复用 shared 排序；direct/route_to 保持旧单路由合同。"""

    if source.routing_policy_id is None:
        return preview_route(
            source.candidates,
            routing_reason_prefix=source.routing_reason_prefix,
            unavailable_error_code=source.unavailable_error_code,
            provider_health_stale_seconds=health_stale_seconds,
            now=now,
            runtime_fallback_enabled=source.runtime_fallback_enabled,
        )

    # 只要存在 Policy ID，mode/version 就必须成组存在，禁止构造半个执行身份。
    if source.routing_mode is None or source.routing_policy_version is None:
        raise ValueError("routing policy identity incomplete")
    facts_by_model = {item.model_id: item for item in source.candidates}
    inputs: list[RoutingCandidateInput] = []
    for priority, fact in enumerate(source.candidates):
        health_state, health_fresh = _dry_run_health_state(
            fact.provider_health_status,
            fact.provider_health_updated_at,
            now=now,
            stale_seconds=health_stale_seconds,
        )
        model_status = (
            fact.model_status if fact.model_status in {"active", "disabled"} else "missing"
        )
        provider_status = (
            fact.provider_status if fact.provider_status in {"active", "disabled"} else "missing"
        )
        inputs.append(
            RoutingCandidateInput(
                candidate_role=fact.role,  # type: ignore[arg-type]
                configured_priority=priority,
                model_id=fact.model_id,
                provider_id=fact.provider_id if provider_status != "missing" else None,
                model_status=model_status,  # type: ignore[arg-type]
                provider_status=provider_status,  # type: ignore[arg-type]
                health_state=health_state,  # type: ignore[arg-type]
                health_fresh=health_fresh,
                keypool_state=(
                    "not_evaluated" if source.routing_mode == "fixed" else fact.keypool_state
                ),  # type: ignore[arg-type]
            )
        )
    selection = select_routing_candidates(
        source.routing_mode,  # type: ignore[arg-type]
        inputs,
        token_estimator=lambda: token_estimate,
        price_loader=lambda eligible: _load_dry_run_routing_prices(
            connection,
            tenant_id,
            tuple(item.model_id for item in eligible),
            now,
        ),
        quality_config=source.quality_config,
        quality_loader=lambda eligible: _load_dry_run_routing_quality_facts(
            connection,
            tenant_id,
            tuple(item.model_id for item in eligible),
        ),
    )
    return _route_result_from_policy_selection(
        source,
        selection,
        facts_by_model=facts_by_model,
        now=now,
    )


def _route_result_from_policy_selection(
    source: _RouteSource,
    selection: RoutingSelectionResult,
    *,
    facts_by_model: dict[str, RouteCandidateFact],
    now: datetime,
) -> RoutePreviewResult:
    """把 shared 强类型选择结果适配为既有 Dry-Run 路由预览对象。"""

    previews: list[RouteCandidatePreview] = []
    for item in selection.candidate_results:
        fact = facts_by_model[item.candidate.model_id]
        previews.append(
            RouteCandidatePreview(
                role=item.candidate.candidate_role,
                model_id=item.candidate.model_id,
                internal_model_id=fact.internal_model_id,
                provider_id=item.candidate.provider_id,
                provider_code=fact.provider_code,
                status=item.status,
                reason_code=item.reason_code,
                rank=item.rank,
                health_state=item.candidate.health_state,
                health_fresh=item.candidate.health_fresh,
                keypool_state=item.candidate.keypool_state,
                price_available=item.price_available,
                price_source=item.price_source,
                price_schema_version=item.price_schema_version,
                currency=item.currency,
                estimated_cost_amount=item.estimated_cost_amount,
                model_quality_score_id=item.model_quality_score_id,
                quality_statistics_version=item.quality_statistics_version,
                quality_feedback_version=item.quality_feedback_version,
                quality_evidence_state=item.quality_evidence_state,
                quality_sample_count=item.quality_sample_count,
                quality_success_count=item.quality_success_count,
                quality_error_count=item.quality_error_count,
                manual_quality_bps=item.manual_quality_bps,
                average_latency_ms=item.average_latency_ms,
                quality_component_bps=item.quality_component_bps,
                cost_component_bps=item.cost_component_bps,
                latency_component_bps=item.latency_component_bps,
                weighted_score_bps=item.weighted_score_bps,
            )
        )
    selected = next(
        (item for item in selection.candidate_results if item.status == "selected"),
        None,
    )
    if selected is None:
        return RoutePreviewResult(
            status="unavailable",
            reason_code=selection.selection_reason,
            would_error_code=(
                source.unavailable_error_code
                if source.routing_mode == "fixed"
                else "gateway.no_route_available"
            ),
            selected_model_id=None,
            internal_model_id=None,
            provider_id=None,
            provider_code=None,
            routing_reason=source.routing_reason_prefix,
            chat_capability_supported=None,
            candidates=tuple(previews),
            runtime_fallback_model_id=None,
            fallback_cost_included=False,
            routing_policy_id=source.routing_policy_id,
            routing_policy_version=source.routing_policy_version,
            routing_mode=source.routing_mode,
            price_as_of=now if selection.cost_comparison_performed else None,
            estimated_input_tokens=(
                selection.token_estimate.input_tokens if selection.token_estimate else None
            ),
            estimated_output_tokens=(
                selection.token_estimate.output_tokens if selection.token_estimate else None
            ),
            token_estimate_truncated=(
                selection.token_estimate.truncated if selection.token_estimate else None
            ),
            eligible_candidate_count=selection.eligible_candidate_count,
            quality_as_of=now if selection.quality_config is not None else None,
            quality_formula_version=selection.quality_formula_version,
            quality_scoring_performed=selection.quality_scoring_performed,
            quality_weight_bps=(
                selection.quality_config.quality_weight_bps
                if selection.quality_config is not None
                else None
            ),
            cost_weight_bps=(
                selection.quality_config.cost_weight_bps
                if selection.quality_config is not None
                else None
            ),
            latency_weight_bps=(
                selection.quality_config.latency_weight_bps
                if selection.quality_config is not None
                else None
            ),
            quality_floor_bps=(
                selection.quality_config.quality_floor_bps
                if selection.quality_config is not None
                else None
            ),
            minimum_sample_count=(
                selection.quality_config.minimum_sample_count
                if selection.quality_config is not None
                else None
            ),
        )
    selected_fact = facts_by_model[selected.candidate.model_id]
    chat_supported = selected_fact.capabilities.get("chat") is not False
    standby_fallback = next(
        (
            item
            for item in selection.candidate_results
            if item.status == "standby" and item.candidate.candidate_role == "fallback"
        ),
        None,
    )
    runtime_fallback_id: str | None = None
    if (
        source.runtime_fallback_enabled
        and selected.candidate.candidate_role == "default"
        and standby_fallback is not None
        and (
            source.routing_mode != "quality_cost"
            or standby_fallback.quality_evidence_state == "ready"
        )
        and facts_by_model[standby_fallback.candidate.model_id].capabilities.get("chat")
        is not False
    ):
        runtime_fallback_id = standby_fallback.candidate.model_id
    return RoutePreviewResult(
        status="selected",
        reason_code=selection.selection_reason,
        would_error_code=None if chat_supported else "gateway.model_not_found",
        selected_model_id=selected.candidate.model_id,
        internal_model_id=selected_fact.internal_model_id,
        provider_id=selected.candidate.provider_id,
        provider_code=selected_fact.provider_code,
        routing_reason=f"{source.routing_reason_prefix}.{selected.candidate.candidate_role}",
        chat_capability_supported=chat_supported,
        candidates=tuple(previews),
        runtime_fallback_model_id=runtime_fallback_id,
        fallback_cost_included=runtime_fallback_id is not None,
        routing_policy_id=source.routing_policy_id,
        routing_policy_version=source.routing_policy_version,
        routing_mode=source.routing_mode,
        price_as_of=now if selection.cost_comparison_performed else None,
        estimated_input_tokens=(
            selection.token_estimate.input_tokens if selection.token_estimate else None
        ),
        estimated_output_tokens=(
            selection.token_estimate.output_tokens if selection.token_estimate else None
        ),
        token_estimate_truncated=(
            selection.token_estimate.truncated if selection.token_estimate else None
        ),
        eligible_candidate_count=selection.eligible_candidate_count,
        quality_as_of=now if selection.quality_config is not None else None,
        quality_formula_version=selection.quality_formula_version,
        quality_scoring_performed=selection.quality_scoring_performed,
        quality_weight_bps=(
            selection.quality_config.quality_weight_bps
            if selection.quality_config is not None
            else None
        ),
        cost_weight_bps=(
            selection.quality_config.cost_weight_bps
            if selection.quality_config is not None
            else None
        ),
        latency_weight_bps=(
            selection.quality_config.latency_weight_bps
            if selection.quality_config is not None
            else None
        ),
        quality_floor_bps=(
            selection.quality_config.quality_floor_bps
            if selection.quality_config is not None
            else None
        ),
        minimum_sample_count=(
            selection.quality_config.minimum_sample_count
            if selection.quality_config is not None
            else None
        ),
    )


def _load_dry_run_routing_quality_facts(
    connection: sqlite3.Connection,
    tenant_id: str,
    model_ids: tuple[str, ...],
) -> tuple[RoutingQualityFact, ...]:
    """在 Dry-Run 的同一 query-only 快照读取租户候选 Score。

    参数：只读连接、认证租户和 shared 通用资格阶段筛出的模型 ID。返回：一一对应的
    质量事实。关键约束：缺行或非 active 表示 owner 图损坏；本函数不写 Score、不读取
    Prompt/Response，也不尝试用默认值掩盖迁移或并发问题。
    """

    facts: list[RoutingQualityFact] = []
    for model_id in model_ids:
        row = connection.execute(
            """
            SELECT
              model_quality_score_id, model_id, status,
              sample_count, success_count, error_count,
              latency_sample_count, total_latency_ms, manual_quality_bps,
              statistics_version, feedback_version
            FROM model_quality_scores
            WHERE tenant_id=? AND model_id=?
            """,
            (tenant_id, model_id),
        ).fetchone()
        # 当前租户每个候选模型必须有唯一 active Score；缺失或停用表示图损坏，禁止跨租户补值。
        if row is None or row["status"] != "active":
            raise ValueError("dry-run quality score graph invalid")
        facts.append(
            RoutingQualityFact(
                model_id=str(row["model_id"]),
                model_quality_score_id=str(row["model_quality_score_id"]),
                sample_count=row["sample_count"],
                success_count=row["success_count"],
                error_count=row["error_count"],
                latency_sample_count=row["latency_sample_count"],
                total_latency_ms=row["total_latency_ms"],
                manual_quality_bps=row["manual_quality_bps"],
                statistics_version=row["statistics_version"],
                feedback_version=row["feedback_version"],
            )
        )
    return tuple(facts)


def _load_dry_run_routing_prices(
    connection: sqlite3.Connection,
    tenant_id: str,
    model_ids: tuple[str, ...],
    now: datetime,
) -> tuple[RoutingPriceFact, ...]:
    """按同一只读快照恢复两候选价格，不把目录无版本默认零误认成免费。"""

    # 价格回调只允许 shared cost 第二阶段以两条合格候选触发。
    if len(model_ids) != 2:
        # 偏离两候选合同表示调用图损坏，不能读取任意数量价格继续模拟。
        raise ValueError("routing price candidate count invalid")
    as_of = _to_db_time(now)
    from metahub_shared.runtime.workspace_fx import resolve_workspace_fx_rate

    try:
        fx = resolve_workspace_fx_rate(connection, tenant_id=tenant_id, observed_at=as_of)
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        fx = None
    facts: list[RoutingPriceFact] = []
    for model_id in model_ids:
        row = connection.execute(
            """
            SELECT
              pp.price_plan_id,
              CAST(pp.input_price_per_1k AS TEXT) AS plan_input_price,
              CAST(pp.output_price_per_1k AS TEXT) AS plan_output_price,
              CAST(pp.cache_price_per_1k AS TEXT) AS plan_cache_price,
              pp.currency AS plan_currency, pp.price_schema_version AS plan_version,
              CAST(m.input_price_per_1k AS TEXT) AS catalog_input_price,
              CAST(m.output_price_per_1k AS TEXT) AS catalog_output_price,
              CAST(m.cache_price_per_1k AS TEXT) AS catalog_cache_price,
              m.currency AS catalog_currency, m.price_schema_version AS catalog_version
            FROM models m
            LEFT JOIN model_price_plans pp ON pp.price_plan_id=(
              SELECT selected.price_plan_id
              FROM model_price_plans selected
              WHERE selected.tenant_id=m.tenant_id AND selected.model_id=m.model_id
                AND selected.effective_from<=?
              ORDER BY selected.effective_from DESC, selected.created_at DESC,
                       selected.price_plan_id DESC
              LIMIT 1
            )
            WHERE m.tenant_id=? AND m.model_id=?
            """,
            (as_of, tenant_id, model_id),
        ).fetchone()
        if row is None:
            # 同一 query-only 快照内模型消失说明关系损坏，不能降级成正常缺价。
            raise ValueError("routing price model missing")
        if row["price_plan_id"] is not None:
            facts.append(
                RoutingPriceFact(
                    model_id=model_id,
                    source="price_plan",
                    input_price_per_1k=Decimal(str(row["plan_input_price"])),
                    output_price_per_1k=Decimal(str(row["plan_output_price"])),
                    cache_price_per_1k=Decimal(str(row["plan_cache_price"])),
                    currency=str(row["plan_currency"]),
                    price_schema_version=str(row["plan_version"] or ""),
                    comparison_currency="CNY" if row["plan_currency"] == "USD" else None,
                    comparison_fx_rate=(fx.usd_to_cny if fx else Decimal("7.2")) if row["plan_currency"] == "USD" else None,
                    comparison_fx_rate_version=(fx.version_id if fx else "legacy-deployment-v1") if row["plan_currency"] == "USD" else None,
                )
            )
        elif row["catalog_version"]:
            facts.append(
                RoutingPriceFact(
                    model_id=model_id,
                    source="model_catalog",
                    input_price_per_1k=Decimal(str(row["catalog_input_price"])),
                    output_price_per_1k=Decimal(str(row["catalog_output_price"])),
                    cache_price_per_1k=Decimal(str(row["catalog_cache_price"])),
                    currency=str(row["catalog_currency"]),
                    price_schema_version=str(row["catalog_version"]),
                    comparison_currency="CNY" if row["catalog_currency"] == "USD" else None,
                    comparison_fx_rate=(fx.usd_to_cny if fx else Decimal("7.2")) if row["catalog_currency"] == "USD" else None,
                    comparison_fx_rate_version=(fx.version_id if fx else "legacy-deployment-v1") if row["catalog_currency"] == "USD" else None,
                )
            )
        else:
            facts.append(RoutingPriceFact.missing(model_id))
    return tuple(facts)


def _runtime_fallback_enabled(
    connection: sqlite3.Connection,
    tenant_id: str,
    model_alias_id: str,
) -> bool:
    """只认可 PRD-036 enabled 且至少含一个支持条件的运行时 Fallback 策略。"""

    row = connection.execute(
        """
        SELECT condition_expr
        FROM fallback_policies
        WHERE tenant_id=? AND model_alias_id=? AND status='enabled'
        """,
        (tenant_id, model_alias_id),
    ).fetchone()
    if row is None:
        return False
    try:
        parsed = json.loads(str(row["condition_expr"]))
    # 损坏 Fallback 条件按无运行时 fallback 处理，不能扩大可触发错误范围。
    except json.JSONDecodeError:
        return False
    allowed = {
        "upstream_timeout",
        "upstream_unavailable",
        "upstream_rate_limited",
        "upstream_quota_exceeded",
        "upstream_5xx",
    }
    return type(parsed) is list and any(type(item) is str and item in allowed for item in parsed)


def _requested_route_error(
    candidates: tuple[RouteCandidateFact, ...],
    *,
    now: datetime,
    stale_seconds: int,
) -> str:
    """在不执行 Gateway 写路径的前提下复刻 alias/direct 失败错误优先级。"""

    if any(
        item.model_status == "active"
        and (item.provider_id is None or item.provider_status != "active")
        for item in candidates
    ):
        return "gateway.provider_not_found"
    if any(
        item.model_status == "active"
        and item.provider_status == "active"
        and _is_fresh_unhealthy(item, now, stale_seconds)
        for item in candidates
    ):
        return "gateway.provider_unhealthy"
    return "gateway.model_disabled"


def _is_fresh_unhealthy(
    candidate: RouteCandidateFact,
    now: datetime,
    stale_seconds: int,
) -> bool:
    """辅助错误码选择使用与纯路由相同的新鲜 unhealthy 证据边界。"""

    # 缺少带时间的新鲜异常证据时按 unknown，避免历史 unhealthy 永久摘流。
    if candidate.provider_health_status != "unhealthy" or not candidate.provider_health_updated_at:
        return False
    try:
        checked = datetime.fromisoformat(
            candidate.provider_health_updated_at.replace("Z", "+00:00")
        )
    # 无法解析的健康时间不能成为拒绝依据，也不能把原始数据库文本带入错误。
    except ValueError:
        return False
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=UTC)
    return checked.astimezone(UTC) >= now - timedelta(seconds=stale_seconds)


def _load_dlp_rule_facts(
    connection: sqlite3.Connection,
    tenant_id: str,
) -> tuple[DlpRuleFact, ...]:
    """按 Gateway 顺序读取至多 100 条 active 规则，名称只用于稳定排序且不进入结果。"""

    rows = connection.execute(
        """
        SELECT dlp_rule_id, rule_type, pattern, action, priority
        FROM dlp_rules
        WHERE tenant_id=? AND status='active'
        ORDER BY priority ASC, updated_at DESC, name ASC
        LIMIT ?
        """,
        (tenant_id, MAX_DLP_RULES),
    ).fetchall()
    return tuple(
        DlpRuleFact(
            dlp_rule_id=str(row["dlp_rule_id"]),
            rule_type=str(row["rule_type"]),  # type: ignore[arg-type]
            pattern=str(row["pattern"]),
            action=str(row["action"]),  # type: ignore[arg-type]
            priority=int(row["priority"]),
        )
        for row in rows
    )


def _load_budget_rule_facts(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    environment_id: str,
    project_id: str,
    now: datetime,
) -> tuple[BudgetRuleFact, ...]:
    """读取 tenant/project 规则的正式消费、未结算预留和当前业务日 block。

    正式金额必须复用 Gateway 与 Control Budget 共用的 FinOps 汇总器，避免 Dry-Run
    继续从旧 ``usage_ledger`` 得出与真实准入不同的预算余额。
    """

    rows = connection.execute(
        """
        SELECT
          *
        FROM budget_rules
        WHERE tenant_id=? AND environment_id=? AND status='active'
          AND (
            (scope_type='tenant' AND scope_id=?)
            OR (scope_type='project' AND scope_id=?)
          )
        ORDER BY CASE scope_type WHEN 'tenant' THEN 0 ELSE 1 END
        LIMIT 3
        """,
        (tenant_id, environment_id, tenant_id, project_id),
    ).fetchall()
    # tenant/project 各至多一条；超出两条表示唯一约束或 scope 归属已经损坏。
    if len(rows) > 2:
        raise ValueError("budget rule set exceeded")
    budget_date, _day_start, _day_end = _budget_window(now)
    facts: list[BudgetRuleFact] = []
    for row in rows:
        period = row["budget_period"] if "budget_period" in row.keys() else "day"
        _same_day, start_utc, end_utc = _budget_window(now, period=period)
        scope_type = str(row["scope_type"])
        scope_id = str(row["scope_id"])
        # 共享汇总器同时校验 tenant、Environment、scope、上海业务日和正式账本证据；
        # 任一金额不可证明时抛出 ValueError 子类，由只读事务边界统一映射为稳定 503。
        amounts = load_budget_spend_amounts(
            connection,
            tenant_id=tenant_id,
            environment_id=environment_id,
            scope_type=scope_type,
            scope_id=scope_id,
            budget_rule_id=str(row["budget_rule_id"]),
            budget_date=budget_date,
            budget_period=period,
            start_utc=start_utc,
            end_utc=end_utc,
        )
        # 微元到 Decimal CNY 的转换是精确十进制运算，不经过 binary float；posted 与
        # reservation 分项传给预演器，保持兼容响应及 budget_remaining 计算语义。
        spent_cny = Decimal(amounts.posted_cny_micros) / Decimal(MICROS_PER_CNY)
        reserved_cny = Decimal(amounts.reserved_cny_micros) / Decimal(MICROS_PER_CNY)
        active_block = connection.execute(
            """
            SELECT budget_block_id
            FROM budget_blocks
            WHERE tenant_id=? AND budget_rule_id=? AND budget_date=? AND status='active'
            LIMIT 1
            """,
            (tenant_id, row["budget_rule_id"], budget_date),
        ).fetchone()
        facts.append(
            BudgetRuleFact(
                budget_rule_id=str(row["budget_rule_id"]),
                scope_type=scope_type,  # type: ignore[arg-type]
                spent_cny=spent_cny,
                reserved_cny=reserved_cny,
                limit_cny=parse_nonnegative_decimal(row["daily_budget_cny"]),
                warning_threshold_percent=int(row["warning_threshold_percent"]),
                exhausted_action=str(row["exhausted_action"]),  # type: ignore[arg-type]
                active_block=active_block is not None,
            )
        )
    return tuple(facts)


def _load_effective_price(
    connection: sqlite3.Connection,
    tenant_id: str,
    model_id: str | None,
    now: datetime,
) -> PriceFact:
    """按当前时刻选择 PricePlan，缺少计划时回退模型目录价格且不创建 price snapshot。"""

    # 未选中模型时不能查询或伪造价格，显式返回价格不可用的零成本事实。
    if model_id is None:
        return PriceFact(Decimal("0"), Decimal("0"), "CNY", available=False)
    row = connection.execute(
        """
        SELECT
          COALESCE(pp.input_price_per_1k, m.input_price_per_1k) AS input_price_per_1k,
          COALESCE(pp.output_price_per_1k, m.output_price_per_1k) AS output_price_per_1k,
          COALESCE(pp.cache_price_per_1k, m.cache_price_per_1k) AS cache_price_per_1k,
          COALESCE(pp.currency, m.currency) AS currency
        FROM models m
        LEFT JOIN model_price_plans pp ON pp.price_plan_id=(
          SELECT selected.price_plan_id
          FROM model_price_plans selected
          WHERE selected.tenant_id=m.tenant_id AND selected.model_id=m.model_id
            AND selected.effective_from<=?
          ORDER BY selected.effective_from DESC, selected.created_at DESC
          LIMIT 1
        )
        WHERE m.tenant_id=? AND m.model_id=?
        """,
        (_to_db_time(now), tenant_id, model_id),
    ).fetchone()
    if row is None:
        return PriceFact(Decimal("0"), Decimal("0"), "CNY", available=False)
    return PriceFact(
        input_price_per_1k=parse_nonnegative_decimal(row["input_price_per_1k"]),
        output_price_per_1k=parse_nonnegative_decimal(row["output_price_per_1k"]),
        currency=str(row["currency"] or "CNY").upper(),
        available=True,
        cache_price_per_1k=parse_nonnegative_decimal(row["cache_price_per_1k"]),
    )


def _load_published_snapshot_version(
    connection: sqlite3.Connection,
    tenant_id: str,
) -> int | None:
    """读取当前 published head 作为对照，不用 snapshot 替换 active candidate。"""

    row = connection.execute(
        """
        SELECT h.current_snapshot_version
        FROM policy_snapshot_heads h
        JOIN policy_snapshots s
          ON s.tenant_id=h.tenant_id
         AND s.policy_snapshot_id=h.current_snapshot_id
         AND s.snapshot_version=h.current_snapshot_version
        WHERE h.tenant_id=? AND s.state='published'
        """,
        (tenant_id,),
    ).fetchone()
    return int(row["current_snapshot_version"]) if row is not None else None


def _policy_preview(result: PolicyEvaluationResult) -> PolicyDryRunPolicyPreview:
    """删除 shared comparison actual 后构造正式强类型 Policy 合同。"""

    reason = _policy_check(result.policy_reason) if result.policy_reason is not None else None
    return PolicyDryRunPolicyPreview(
        decision=result.decision,
        reason_code=result.reason_code,
        target_model_id=result.target_model_id,
        matched_policy_id=reason.policy_id if reason is not None else None,
        matched_priority=reason.priority if reason is not None else None,
        matched_action=reason.action if reason is not None else None,
        policy_reason=reason,
        policy_checks=[_policy_check(item) for item in result.policy_checks],
        log_only_matches=[_policy_check(item) for item in result.log_only_matches],
        mask_matches=[_policy_check(item) for item in result.mask_matches],
        mask_required=result.mask_required,
        checked_policy_count=result.checked_policy_count,
        timed_out=result.timed_out,
        missing_variable=result.missing_variable,
        explanation_truncated=result.explanation_truncated,
    )


def _policy_check(check: object) -> PolicyDryRunCheck:
    """把 shared PolicyCheckExplanation 收窄，并确保 actual 永不进入模型构造。"""

    # 属性来源只可能是 shared frozen dataclass；显式逐字段复制避免调用其含 actual 的 as_dict。
    comparisons = [
        PolicyDryRunComparison(
            variable=item.variable,
            operator=item.operator,
            value_type=item.value_type,
            expected=list(item.expected) if isinstance(item.expected, tuple) else item.expected,
            matched=item.matched,
        )
        for item in check.comparisons  # type: ignore[attr-defined]
    ]
    return PolicyDryRunCheck(
        policy_id=check.policy_id,  # type: ignore[attr-defined]
        policy_rule_id=check.policy_rule_id,  # type: ignore[attr-defined]
        policy_action_id=check.policy_action_id,  # type: ignore[attr-defined]
        priority=check.priority,  # type: ignore[attr-defined]
        condition_hash=check.condition_hash,  # type: ignore[attr-defined]
        action=check.action_type,  # type: ignore[attr-defined]
        matched=check.matched,  # type: ignore[attr-defined]
        match_order=check.match_order,  # type: ignore[attr-defined]
        comparisons=comparisons,
    )


def _policy_would_error(result: PolicyEvaluationResult) -> str | None:
    """把纯策略决定映射为真实 Gateway 将返回的统一错误码。"""

    if result.timed_out:
        return "gateway.policy_evaluation_timeout"
    if result.missing_variable is not None or result.reason_code == "policy.context_missing":
        return "gateway.policy_context_missing"
    if result.decision == "deny":
        return "gateway.policy_denied"
    # log_only/mask 不会成为最终决定，其他未来动作必须按执行器不支持失败关闭。
    if result.decision not in {"allow", "route_to"}:
        return "gateway.policy_action_unsupported"
    return None


def _route_preview(requested_model: str, result: RoutePreviewResult) -> PolicyDryRunRoutePreview:
    """把 shared 路由结果转换为不含凭据和 Endpoint 的响应 DTO。"""

    return PolicyDryRunRoutePreview(
        status=result.status,
        reason_code=result.reason_code,
        requested_model=requested_model,
        routing_reason=result.routing_reason,
        selected_model_id=result.selected_model_id,
        internal_model_id=result.internal_model_id,
        provider_id=result.provider_id,
        provider_code=result.provider_code,
        chat_capability_supported=result.chat_capability_supported,
        candidates=[
            PolicyDryRunRouteCandidate(
                role=item.role,
                model_id=item.model_id,
                internal_model_id=item.internal_model_id,
                provider_id=item.provider_id,
                provider_code=item.provider_code,
                status=item.status,
                reason_code=item.reason_code,
                rank=item.rank,
                health_state=item.health_state,  # type: ignore[arg-type]
                health_fresh=item.health_fresh,
                keypool_state=item.keypool_state,  # type: ignore[arg-type]
                price_available=item.price_available,
                price_source=item.price_source,  # type: ignore[arg-type]
                price_schema_version=item.price_schema_version,
                currency=item.currency,  # type: ignore[arg-type]
                estimated_cost_amount=(
                    format(item.estimated_cost_amount, ".15f")
                    if item.estimated_cost_amount is not None
                    else None
                ),
                model_quality_score_id=item.model_quality_score_id,
                quality_statistics_version=item.quality_statistics_version,
                quality_feedback_version=item.quality_feedback_version,
                quality_evidence_state=item.quality_evidence_state,  # type: ignore[arg-type]
                quality_sample_count=item.quality_sample_count,
                quality_success_count=item.quality_success_count,
                quality_error_count=item.quality_error_count,
                manual_quality_bps=item.manual_quality_bps,
                average_latency_ms=(
                    format(item.average_latency_ms, ".3f")
                    if item.average_latency_ms is not None
                    else None
                ),
                quality_component_bps=item.quality_component_bps,
                cost_component_bps=item.cost_component_bps,
                latency_component_bps=item.latency_component_bps,
                weighted_score_bps=item.weighted_score_bps,
            )
            for item in result.candidates
        ],
        runtime_fallback_model_id=result.runtime_fallback_model_id,
        fallback_cost_included=result.fallback_cost_included,
        routing_policy_id=result.routing_policy_id,
        routing_policy_version=result.routing_policy_version,
        routing_mode=result.routing_mode,  # type: ignore[arg-type]
        price_as_of=result.price_as_of,
        estimated_input_tokens=result.estimated_input_tokens,
        estimated_output_tokens=result.estimated_output_tokens,
        token_estimate_truncated=result.token_estimate_truncated,
        eligible_candidate_count=result.eligible_candidate_count,
        quality_as_of=result.quality_as_of,
        quality_formula_version=result.quality_formula_version,  # type: ignore[arg-type]
        quality_scoring_performed=result.quality_scoring_performed,
        quality_weight_bps=result.quality_weight_bps,
        cost_weight_bps=result.cost_weight_bps,
        latency_weight_bps=result.latency_weight_bps,
        quality_floor_bps=result.quality_floor_bps,
        minimum_sample_count=result.minimum_sample_count,
    )


def _not_evaluated_route(requested_model: str, reason: str) -> PolicyDryRunRoutePreview:
    """构造显式未执行路由阶段，不能用空对象让前端推断。"""

    return PolicyDryRunRoutePreview(
        status="not_evaluated",
        reason_code=reason,
        requested_model=requested_model,
        candidates=[],
    )


def _dlp_preview(result: DlpPreviewResult) -> PolicyDryRunDlpPreview:
    """构造只含规则 ID/优先级/动作的 DLP 正式响应。"""

    return PolicyDryRunDlpPreview(
        status=result.status,
        reason_code=result.reason_code,
        matches=[
            PolicyDryRunDlpMatch(
                dlp_rule_id=item.dlp_rule_id,
                priority=item.priority,
                action=item.action,  # type: ignore[arg-type]
            )
            for item in result.matches
        ],
        match_count=result.match_count,
        scan_truncated=result.scan_truncated,
    )


def _not_evaluated_dlp(reason: str) -> PolicyDryRunDlpPreview:
    """构造显式未执行的 DLP 阶段，使页面无需从空对象猜测短路状态。"""

    return PolicyDryRunDlpPreview(
        status="not_evaluated",
        reason_code=reason,
        matches=[],
        match_count=0,
        scan_truncated=False,
    )


def _budget_preview(result: BudgetPreviewResult) -> PolicyDryRunBudgetPreview:
    """把 Decimal 固定为六位字符串后构造预算正式响应。"""

    return PolicyDryRunBudgetPreview(
        status=result.status,
        reason_code=result.reason_code,
        estimated_input_tokens=result.estimated_input_tokens,
        estimated_output_tokens=result.estimated_output_tokens,
        estimated_cost_cny=fixed_money(result.estimated_cost_cny),
        price_available=result.price_available,
        fallback_cost_included=result.fallback_cost_included,
        rules=[
            PolicyDryRunBudgetRule(
                budget_rule_id=item.budget_rule_id,
                scope_type=item.scope_type,  # type: ignore[arg-type]
                spent_cny=fixed_money(item.spent_cny),
                reserved_cny=fixed_money(item.reserved_cny),
                projected_cny=fixed_money(item.projected_cny),
                limit_cny=fixed_money(item.limit_cny),
                status=item.status,
                action=item.action,
                reason_code=item.reason_code,
            )
            for item in result.rules
        ],
        uncovered_scopes=["application", "virtual_key"],
    )


def _not_evaluated_budget(reason: str) -> PolicyDryRunBudgetPreview:
    """构造显式未执行预算阶段，并保留固定金额与未覆盖 scope 合同。"""

    return PolicyDryRunBudgetPreview(
        status="not_evaluated",
        reason_code=reason,
        estimated_input_tokens=0,
        estimated_output_tokens=0,
        estimated_cost_cny="0.000000",
        price_available=False,
        fallback_cost_included=False,
        rules=[],
        uncovered_scopes=["application", "virtual_key"],
    )


def _decision_tree(
    policy: PolicyDryRunPolicyPreview,
    route: PolicyDryRunRoutePreview,
    dlp: PolicyDryRunDlpPreview,
    budget: PolicyDryRunBudgetPreview,
) -> PolicyDryRunDecisionTree:
    """按求值和阶段顺序构造最多 204 个扁平节点。"""

    nodes = [
        PolicyDryRunTreeNode(
            node_id="node_000",
            order=0,
            node_type="policy_set",
            status="completed",
            reason_code=policy.reason_code,
        )
    ]
    for check in policy.policy_checks:
        order = len(nodes)
        nodes.append(
            PolicyDryRunTreeNode(
                node_id=f"node_{order:03d}",
                parent_id="node_000",
                order=order,
                node_type="policy_rule",
                status="matched" if check.matched else "not_matched",
                reason_code=("policy.rule_matched" if check.matched else "policy.rule_not_matched"),
                policy_id=check.policy_id,
                policy_rule_id=check.policy_rule_id,
                priority=check.priority,
                action=check.action,
                matched=check.matched,
            )
        )
    for node_type, status, reason in (
        ("route", route.status, route.reason_code),
        ("dlp", dlp.status, dlp.reason_code),
        ("budget", budget.status, budget.reason_code),
    ):
        order = len(nodes)
        nodes.append(
            PolicyDryRunTreeNode(
                node_id=f"node_{order:03d}",
                parent_id="node_000",
                order=order,
                node_type=node_type,  # type: ignore[arg-type]
                status=status,
                reason_code=reason,
            )
        )
    return PolicyDryRunDecisionTree(nodes=nodes)


def _persist_safe_facts(
    config: AppConfig,
    context: AuthContext,
    explanation: PolicyExplanation,
    *,
    audit_request_meta: AuditRequestMeta,
    request_id: str,
    trace_id: str,
) -> _PersistResult:
    """持有写锁后重查 exactly-once 键，并原子写入 explanation/event/audit。"""

    connection = _connect(config)
    tenant_id = context.user.tenant_id
    actor_user_id = context.user.user_id
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = _load_persistent_facts(
            connection,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            request_id=request_id,
        )
        # 写锁内发现旧事实表示并发请求已提交，必须核验并走稳定恢复而非再次插入。
        if existing is not None:
            connection.rollback()
            return _PersistResult(
                explanation=None,
                event=existing.event,
                audit=existing.audit,
                reused=True,
            )

        explanation_id = f"pexp_{uuid4().hex}"
        created_at = _utcnow()
        persisted_explanation = explanation.model_copy(
            update={
                "policy_explanation_id": explanation_id,
                "created_at": created_at,
                "updated_at": created_at,
            }
        )
        _insert_explanation(
            connection,
            context,
            persisted_explanation,
            request_id=request_id,
            trace_id=trace_id,
        )
        event_payload = _event_payload(persisted_explanation, request_id)
        policy_event_id = f"pevt_{uuid4().hex}"
        connection.execute(
            """
            INSERT INTO policy_events(
              policy_event_id, tenant_id, policy_id, event_type, actor_user_id,
              request_id, trace_id, reason_code, payload_json, created_at
            )
            VALUES(?, ?, ?, 'policy.dry_run_executed', ?, ?, ?,
                   'policy.dry_run_executed', ?, ?)
            """,
            (
                policy_event_id,
                tenant_id,
                persisted_explanation.policy.matched_policy_id,
                actor_user_id,
                request_id,
                trace_id,
                _json_text(event_payload),
                _to_db_time(created_at),
            ),
        )
        audit = record_audit_log(
            connection,
            context,
            action="policy.dry_run_executed",
            object_type="policy_explanation",
            object_id=explanation_id,
            object_name=None,
            before_summary={},
            after_summary=_audit_summary(persisted_explanation),
            request_meta=audit_request_meta,
            request_id=request_id,
            trace_id=trace_id,
        )
        # Dry-Run 创建必须始终产生 audit；None 表示审计合同未形成可验证变更。
        if audit is None:
            raise ApiError("policy.dry_run_unavailable")
        authoritative = _load_persistent_facts(
            connection,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            request_id=request_id,
        )
        if authoritative is None or not _row_matches_explanation(
            authoritative.explanation_row,
            persisted_explanation,
            actor_user_id=actor_user_id,
            request_id=request_id,
            trace_id=trace_id,
        ):
            # 三事实任何字段漂移都会使稳定投递不可证明，提交前必须整体回滚。
            raise ApiError("policy.dry_run_unavailable")
        connection.commit()
        # 最终响应使用数据库权威时间文本，避免 Python 与 SQLite 序列化边界产生漂移。
        committed = persisted_explanation.model_copy(
            update={
                "created_at": _parse_db_time(authoritative.explanation_row["created_at"]),
                "updated_at": _parse_db_time(authoritative.explanation_row["updated_at"]),
            }
        )
        return _PersistResult(
            explanation=committed,
            event=authoritative.event,
            audit=authoritative.audit,
            reused=False,
        )
    # 所有领域写错先回滚；仅 Dry-Run 专用不可用码允许直接对外保留。
    except ApiError as exc:
        _safe_rollback(connection)
        if exc.code == "policy.dry_run_unavailable":
            raise
        # 审计等下游领域码不能暴露内部接入差异，统一折叠为 Dry-Run 503。
        raise ApiError("policy.dry_run_unavailable") from None
    # SQLite、金额或持久 JSON 异常同样必须回滚全部三事实，不能留下部分成功。
    except (DbError, sqlite3.Error, ValueError, TypeError):
        _safe_rollback(connection)
        raise ApiError("policy.dry_run_unavailable") from None
    finally:
        connection.close()


def _insert_explanation(
    connection: sqlite3.Connection,
    context: AuthContext,
    explanation: PolicyExplanation,
    *,
    request_id: str,
    trace_id: str,
) -> None:
    """只插入 PRD 指定安全列，完整决策树、金额和输入摘要均不落库。"""

    policy = explanation.policy
    connection.execute(
        """
        INSERT INTO policy_explanations(
          policy_explanation_id, tenant_id, actor_user_id, request_id, trace_id,
          status, version, policy_source, policy_set_fingerprint,
          published_snapshot_version, decision, reason_code, matched_policy_id,
          matched_priority, matched_action, route_status, dlp_status, budget_status,
          would_error_code, checked_policy_count, runtime_compatible, coverage_complete,
          explanation_truncated, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, 'completed', 1, 'active_candidate', ?, ?, ?, ?, ?, ?, ?,
               ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
        """,
        (
            explanation.policy_explanation_id,
            context.user.tenant_id,
            context.user.user_id,
            request_id,
            trace_id,
            explanation.policy_set_fingerprint,
            explanation.published_snapshot_version,
            policy.decision,
            policy.reason_code,
            policy.matched_policy_id,
            policy.matched_priority,
            policy.matched_action,
            explanation.route.status,
            explanation.dlp.status,
            explanation.budget.status,
            explanation.would_error_code,
            policy.checked_policy_count,
            int(explanation.runtime_compatible),
            int(policy.explanation_truncated),
            _to_db_time(explanation.created_at),
            _to_db_time(explanation.updated_at),
        ),
    )


def _load_persistent_facts(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    actor_user_id: str,
    request_id: str,
) -> _PersistentFacts | None:
    """按共同 exactly-once 键读取三行并逐字段交叉核验，拒绝缺失、重复和额外摘要。"""

    explanations = connection.execute(
        """
        SELECT * FROM policy_explanations
        WHERE tenant_id=? AND actor_user_id=? AND request_id=?
        LIMIT 2
        """,
        (tenant_id, actor_user_id, request_id),
    ).fetchall()
    events = connection.execute(
        """
        SELECT * FROM policy_events
        WHERE tenant_id=? AND actor_user_id=? AND request_id=?
          AND event_type='policy.dry_run_executed'
        LIMIT 2
        """,
        (tenant_id, actor_user_id, request_id),
    ).fetchall()
    audits = connection.execute(
        """
        SELECT * FROM audit_logs
        WHERE tenant_id=? AND actor_user_id=? AND request_id=?
          AND action='policy.dry_run_executed'
        LIMIT 2
        """,
        (tenant_id, actor_user_id, request_id),
    ).fetchall()
    # 三类事实全空表示首次 request_id；任何部分存在都不能当成可重试的新请求。
    if not explanations and not events and not audits:
        return None
    # exactly-once 合同要求三表各恰好一行，缺失或重复都按不可恢复损坏处理。
    if len(explanations) != 1 or len(events) != 1 or len(audits) != 1:
        raise ApiError("policy.dry_run_unavailable")
    explanation = explanations[0]
    event_row = events[0]
    audit_row = audits[0]
    expected_event = _event_payload_from_row(explanation)
    expected_audit = _audit_summary_from_row(explanation)
    try:
        actual_event = json.loads(str(event_row["payload_json"]))
        audit_before = json.loads(str(audit_row["before_summary_json"]))
        audit_after = json.loads(str(audit_row["after_summary_json"]))
        changed_fields = json.loads(str(audit_row["changed_fields_json"]))
    # 持久 JSON 损坏时不能重派外部事件，也不能把解析文本带入响应。
    except json.JSONDecodeError:
        raise ApiError("policy.dry_run_unavailable") from None
    scalar_match = (
        str(explanation["tenant_id"]) == tenant_id
        and str(explanation["actor_user_id"]) == actor_user_id
        and str(explanation["request_id"]) == request_id
        and str(event_row["tenant_id"]) == tenant_id
        and str(event_row["actor_user_id"]) == actor_user_id
        and str(event_row["request_id"]) == request_id
        and str(event_row["trace_id"]) == str(explanation["trace_id"])
        and event_row["policy_id"] == explanation["matched_policy_id"]
        and str(audit_row["tenant_id"]) == tenant_id
        and str(audit_row["actor_user_id"]) == actor_user_id
        and str(audit_row["request_id"]) == request_id
        and str(audit_row["trace_id"]) == str(explanation["trace_id"])
        and str(audit_row["object_type"]) == "policy_explanation"
        and str(audit_row["object_id"]) == str(explanation["policy_explanation_id"])
    )
    if (
        not scalar_match
        or actual_event != expected_event
        or audit_before != {}
        or audit_after != expected_audit
        or changed_fields != sorted(AUDIT_SAFE_FIELDS)
    ):
        # 标量或摘要集合不一致表示三事实不再指向同一执行，必须停止恢复。
        raise ApiError("policy.dry_run_unavailable")
    event = _DryRunEvent(
        policy_event_id=str(event_row["policy_event_id"]),
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        explanation_id=str(explanation["policy_explanation_id"]),
        request_id=request_id,
        trace_id=str(event_row["trace_id"]),
        payload=expected_event,
        created_at=_parse_db_time(event_row["created_at"]),
    )
    audit = AuditRecord(
        audit_log_id=str(audit_row["audit_log_id"]),
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action="policy.dry_run_executed",
        object_type="policy_explanation",
        object_id=str(explanation["policy_explanation_id"]),
        changed_fields=tuple(sorted(AUDIT_SAFE_FIELDS)),
        trace_id=str(audit_row["trace_id"]),
        created_at=_parse_db_time(audit_row["created_at"]),
    )
    return _PersistentFacts(explanation_row=explanation, event=event, audit=audit)


def _event_payload(explanation: PolicyExplanation, request_id: str) -> dict[str, object]:
    """构造数据库 CHECK 与 Webhook 共用的精确 13 字段事件摘要。"""

    return {
        "policy_explanation_id": explanation.policy_explanation_id,
        "request_id": request_id,
        "policy_set_fingerprint": explanation.policy_set_fingerprint,
        "decision": explanation.policy.decision,
        "decision_reason_code": explanation.policy.reason_code,
        "matched_policy_id": explanation.policy.matched_policy_id,
        "route_status": explanation.route.status,
        "dlp_status": explanation.dlp.status,
        "budget_status": explanation.budget.status,
        "would_error_code": explanation.would_error_code,
        "checked_policy_count": explanation.policy.checked_policy_count,
        "runtime_compatible": explanation.runtime_compatible,
        "coverage_complete": False,
    }


def _event_payload_from_row(row: sqlite3.Row) -> dict[str, object]:
    """只从持久 explanation 安全列重建稳定事件，不依赖当前请求或当前候选状态。"""

    return {
        "policy_explanation_id": str(row["policy_explanation_id"]),
        "request_id": str(row["request_id"]),
        "policy_set_fingerprint": str(row["policy_set_fingerprint"]),
        "decision": str(row["decision"]),
        "decision_reason_code": str(row["reason_code"]),
        "matched_policy_id": (
            str(row["matched_policy_id"]) if row["matched_policy_id"] is not None else None
        ),
        "route_status": str(row["route_status"]),
        "dlp_status": str(row["dlp_status"]),
        "budget_status": str(row["budget_status"]),
        "would_error_code": (
            str(row["would_error_code"]) if row["would_error_code"] is not None else None
        ),
        "checked_policy_count": int(row["checked_policy_count"]),
        "runtime_compatible": bool(row["runtime_compatible"]),
        "coverage_complete": bool(row["coverage_complete"]),
    }


def _audit_summary(explanation: PolicyExplanation) -> dict[str, object]:
    """构造审计服务要求的精确 14 字段 after；可空字段也必须显式保留。"""

    return {
        "policy_explanation_id": explanation.policy_explanation_id,
        "policy_set_fingerprint": explanation.policy_set_fingerprint,
        "decision": explanation.policy.decision,
        "decision_reason_code": explanation.policy.reason_code,
        "matched_policy_id": explanation.policy.matched_policy_id,
        "matched_priority": explanation.policy.matched_priority,
        "matched_action": explanation.policy.matched_action,
        "route_status": explanation.route.status,
        "dlp_status": explanation.dlp.status,
        "budget_status": explanation.budget.status,
        "would_error_code": explanation.would_error_code,
        "checked_policy_count": explanation.policy.checked_policy_count,
        "runtime_compatible": explanation.runtime_compatible,
        "coverage_complete": False,
    }


def _audit_summary_from_row(row: sqlite3.Row) -> dict[str, object]:
    """从 explanation 行重建审计 after，用于重复恢复与提交前权威核验。"""

    return {
        "policy_explanation_id": str(row["policy_explanation_id"]),
        "policy_set_fingerprint": str(row["policy_set_fingerprint"]),
        "decision": str(row["decision"]),
        "decision_reason_code": str(row["reason_code"]),
        "matched_policy_id": (
            str(row["matched_policy_id"]) if row["matched_policy_id"] is not None else None
        ),
        "matched_priority": (
            int(row["matched_priority"]) if row["matched_priority"] is not None else None
        ),
        "matched_action": (
            str(row["matched_action"]) if row["matched_action"] is not None else None
        ),
        "route_status": str(row["route_status"]),
        "dlp_status": str(row["dlp_status"]),
        "budget_status": str(row["budget_status"]),
        "would_error_code": (
            str(row["would_error_code"]) if row["would_error_code"] is not None else None
        ),
        "checked_policy_count": int(row["checked_policy_count"]),
        "runtime_compatible": bool(row["runtime_compatible"]),
        "coverage_complete": bool(row["coverage_complete"]),
    }


def _row_matches_explanation(
    row: sqlite3.Row,
    explanation: PolicyExplanation,
    *,
    actor_user_id: str,
    request_id: str,
    trace_id: str,
) -> bool:
    """提交前核对 explanation 所有持久列，拒绝触发器或并发导致的字段漂移。"""

    expected = {
        "policy_explanation_id": explanation.policy_explanation_id,
        "tenant_id": explanation.tenant_id,
        "actor_user_id": actor_user_id,
        "request_id": request_id,
        "trace_id": trace_id,
        "status": "completed",
        "version": 1,
        "policy_source": "active_candidate",
        "policy_set_fingerprint": explanation.policy_set_fingerprint,
        "published_snapshot_version": explanation.published_snapshot_version,
        "decision": explanation.policy.decision,
        "reason_code": explanation.policy.reason_code,
        "matched_policy_id": explanation.policy.matched_policy_id,
        "matched_priority": explanation.policy.matched_priority,
        "matched_action": explanation.policy.matched_action,
        "route_status": explanation.route.status,
        "dlp_status": explanation.dlp.status,
        "budget_status": explanation.budget.status,
        "would_error_code": explanation.would_error_code,
        "checked_policy_count": explanation.policy.checked_policy_count,
        "runtime_compatible": int(explanation.runtime_compatible),
        "coverage_complete": 0,
        "explanation_truncated": int(explanation.policy.explanation_truncated),
        "created_at": _to_db_time(explanation.created_at),
        "updated_at": _to_db_time(explanation.updated_at),
    }
    return all(row[key] == value for key, value in expected.items())


def _dispatch_dry_run_event(config: AppConfig, event: _DryRunEvent) -> None:
    """提交后尽力投递稳定 policy event；异常日志只含安全 ID 和固定错误码。"""

    try:
        webhook_event = build_webhook_event(
            tenant_id=event.tenant_id,
            event_type="policy.dry_run_executed",
            event_id=f"evt_{event.policy_event_id}",
            timestamp=event.created_at,
            actor_id=event.actor_user_id,
            object_id=event.explanation_id,
            trace_id=event.trace_id,
            payload=event.payload,
        )
        from metahub_control.webhooks.service import dispatch_webhook_event

        dispatch_webhook_event(
            config,
            event.tenant_id,
            webhook_event,
            request_id=event.request_id,
            trace_id=event.trace_id,
        )
    except Exception:
        logger.error(
            "policy.dry_run_webhook_dispatch_failed",
            extra={
                "event": "policy.dry_run_webhook_dispatch_failed",
                "tenant_id": event.tenant_id,
                "actor_id": event.actor_user_id,
                "object_id": event.explanation_id,
                "request_id": event.request_id,
                "trace_id": event.trace_id,
                "error_code": "webhook.delivery_unavailable",
            },
        )


def _validate_chain_id(value: object) -> str:
    """验证中间件生成的 request/trace ID 可安全进入 exactly-once 索引和事件。"""

    # 中间件链路值必须保持严格字符串，不能把其他对象持久化为 exactly-once 键。
    if type(value) is not str:
        raise ApiError("policy.dry_run_payload_invalid")
    normalized = value.strip()
    # 空白、控制字符和超长 ID 会破坏索引与事件关联，必须在任何数据库写入前拒绝。
    if (
        not normalized
        or len(normalized) > 128
        or any(character.isspace() or ord(character) < 32 for character in normalized)
    ):
        # 非规范链路 ID 不能进入三表 exactly-once 键，统一按安全 payload 错误拒绝。
        raise ApiError("policy.dry_run_payload_invalid")
    return normalized


def _budget_window(now: datetime, *, period: str = "day") -> tuple[str, str, str]:
    """返回上海业务日标识和规则所选周期的 UTC 半开区间。"""

    local = now.astimezone(SHANGHAI)
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
        raise ValueError("budget period invalid")
    return (
        local.date().isoformat(),
        _to_db_time(local_start),
        _to_db_time(local_end),
    )


def _json_text(value: object) -> str:
    """生成无额外空白、key 稳定排序且拒绝非标准数值的安全 JSON。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _connect(config: AppConfig) -> sqlite3.Connection:
    """连接控制面数据库，并把路径/驱动异常折叠为 Dry-Run 统一错误。"""

    try:
        return connect_database(config.database_url)
    # 连接异常可能带数据库路径，统一错误只保留安全固定码。
    except DbError as exc:
        raise ApiError("policy.dry_run_unavailable") from exc


def _safe_rollback(connection: sqlite3.Connection) -> None:
    """尽力释放读/写事务，rollback 异常不得覆盖原固定错误。"""

    try:
        connection.rollback()
    # rollback 自身错误不能覆盖触发回滚的领域错误，也不得进入外部响应。
    except sqlite3.Error:
        pass


def _utcnow() -> datetime:
    """返回秒级 UTC 时刻，使业务日、价格和持久事实共享同一时间口径。"""

    return datetime.now(UTC).replace(microsecond=0)


def _to_db_time(value: datetime) -> str:
    """把带时区时刻序列化为 SQLite 使用的 UTC ISO 文本。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_db_time(value: object) -> datetime:
    """把持久 UTC 文本恢复为带时区 datetime；损坏文本由写事务统一失败关闭。"""

    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
