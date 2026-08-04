"""PRD-044 策略 DSL 的控制面校验、CRUD、审计与事件服务。

本模块只管理当前认证租户的本地策略候选，不把策略接入 Gateway 热路径。表达式必须先由
shared 受限编译器生成不可执行 AST；创建/更新的 Policy、Rule、Action、审计和 created
事件同事务提交，校验失败事件则在业务事务释放写锁后独立追加。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.policy_dsl import (
    CompiledPolicyExpression,
    PolicyActionError,
    PolicyContextError,
    PolicyDiagnostic,
    PolicyDslError,
    PolicyEvaluationInput,
    canonical_policy_action,
    compile_policy_expression,
    evaluate_policy_set,
    policy_draft_fingerprint,
)
from metahub_shared.runtime.webhooks import build_webhook_event

from metahub_control.audit import (
    AuditRequestMeta,
    commit_audited_transaction,
    dispatch_audit_recorded,
    record_audit_log,
)
from metahub_control.auth import AuthContext

from .models import (
    CreatePolicyRequest,
    PolicyActionDraft,
    PolicyActionInfo,
    PolicyDiagnosticInfo,
    PolicyInfo,
    PolicyListResponse,
    PolicyResponse,
    PolicyRuleInfo,
    PolicyValidationResponse,
    UpdatePolicyRequest,
    ValidatePolicyRequest,
)

logger = logging.getLogger("metahub.control.policies")

# 这些上限与 0033 数据库 CHECK、共享 DSL 和控制台字段保持一致；任何放宽都要同步设计与测试。
VALID_STATUSES = frozenset({"active", "disabled"})
VALID_ACTIONS = frozenset({"allow", "deny", "mask", "log_only", "route_to"})
QUERY_KEYS = frozenset({"status", "action"})
MAX_NAME_LENGTH = 80
MAX_DESCRIPTION_LENGTH = 500
MAX_TARGET_LENGTH = 128
MAX_POLICIES_PER_TENANT = 200
MAX_DIAGNOSTICS = 8
MAX_EVENT_VALUE_LENGTH = 128
POLICY_ID_RE = re.compile(r"pol_[0-9a-f]{32}\Z")
IF_MATCH_RE = re.compile(r'"([1-9][0-9]{0,9})"\Z')
DECIMAL_CONTEXT_RE = re.compile(r"(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,12})?\Z")


@dataclass(frozen=True)
class _CompiledDraft:
    """保存已经通过表达式与动作结构校验的草稿。"""

    compiled: CompiledPolicyExpression
    action_type: str
    target_model_id: str | None
    fingerprint: str


@dataclass(frozen=True)
class _DraftFailure(Exception):
    """承载需要形成 validation_failed 的安全领域失败。"""

    code: str
    diagnostics: tuple[PolicyDiagnostic, ...]
    compiled: CompiledPolicyExpression | None = None
    action_type: str | None = None
    target_model_id: str | None = None
    fingerprint: str | None = None


@dataclass(frozen=True)
class _PolicyEventRecord:
    """提交后分发 Webhook 所需的最小持久事件事实。"""

    policy_event_id: str
    tenant_id: str
    policy_id: str | None
    event_type: str
    actor_user_id: str
    request_id: str
    trace_id: str
    reason_code: str
    payload: dict[str, object]
    created_at: datetime


class _RouteTargetInvalid(Exception):
    """表示写事务内 route_to 目标不属于当前租户或不是 active。"""


def list_policies(
    config: AppConfig,
    context: AuthContext,
    *,
    status: str | None,
    action: str | None,
    query_keys: tuple[str, ...],
    request_id: str,
    trace_id: str,
) -> PolicyListResponse:
    """列出当前租户完整策略聚合，未知 query 与不完整三对象均 fail closed。"""

    # 查询参数只允许后端声明的状态和动作键，禁止借任意 tenant_id 参数改变认证租户边界。
    if any(key not in QUERY_KEYS for key in query_keys):
        raise ApiError("policy.query_invalid")
    normalized_status = _normalize_status_filter(status)
    normalized_action = _normalize_action_filter(action)
    connection = _connect(config)
    try:
        _ensure_complete_policy_graph(connection, context.user.tenant_id)
        clauses = ["p.tenant_id=?"]
        params: list[object] = [context.user.tenant_id]
        if normalized_status != "all":
            clauses.append("p.status=?")
            params.append(normalized_status)
        if normalized_action is not None:
            clauses.append("a.action_type=?")
            params.append(normalized_action)
        rows = connection.execute(
            f"""
            {_POLICY_SELECT}
            WHERE {' AND '.join(clauses)}
            ORDER BY p.priority ASC, p.policy_id ASC
            """,
            params,
        ).fetchall()
    # 策略聚合读取失败时不能返回部分 Rule/Action，统一按可重试服务故障处理且不暴露 SQL。
    except sqlite3.Error as exc:
        raise ApiError("policy.unavailable") from exc
    try:
        return PolicyListResponse(
            policies=[_policy_info(row) for row in rows],
            request_id=request_id,
            trace_id=trace_id,
        )
    # 已经标准化的 Policy 错误保持原码，不能被后续模型构造兜底再次改写。
    except ApiError:
        raise
    # 持久行到响应模型的日期/枚举转换失败属于数据库腐损，不能泄露为 Pydantic 或 Python 500。
    except (TypeError, ValueError) as exc:
        raise ApiError("policy.unavailable") from exc


def validate_policy(
    config: AppConfig,
    context: AuthContext,
    payload: ValidatePolicyRequest,
    *,
    request_id: str,
    trace_id: str,
) -> PolicyValidationResponse:
    """校验一个无副作用草稿，并可在可信 tenant 上下文内解释单条命中结果。"""

    expression = payload.rule.condition_expr
    try:
        draft = _compile_draft(expression, payload.action)
        _require_active_route_target(config, context.user.tenant_id, draft)
    except _DraftFailure as failure:
        _persist_and_dispatch_validation_failure(
            config,
            context,
            failure,
            expression=expression,
            policy_id=None,
            request_id=request_id,
            trace_id=trace_id,
        )
        return _invalid_validation_response(failure, request_id=request_id, trace_id=trace_id)

    evaluation: dict[str, Any] | None = None
    if payload.context is not None:
        try:
            evaluation_context = _normalize_evaluation_context(
                context.user.tenant_id,
                payload.context.model_dump(),
            )
            result = evaluate_policy_set(
                (
                    PolicyEvaluationInput(
                        policy_id="draft",
                        policy_rule_id="draft_rule",
                        policy_action_id="draft_action",
                        priority=100,
                        compiled_expression=draft.compiled,
                        action_type=draft.action_type,  # type: ignore[arg-type]
                        target_model_id=draft.target_model_id,
                    ),
                ),
                evaluation_context,
                timeout_ms=getattr(config, "policy_evaluation_timeout_ms", 5),
            )
        # 示例上下文的类型或范围错误属于可记录领域失败，不能变成未追踪的框架校验异常。
        except PolicyContextError as exc:
            failure = _context_failure(exc.variable, exc.message, draft)
            _persist_and_dispatch_validation_failure(
                config,
                context,
                failure,
                expression=expression,
                policy_id=None,
                request_id=request_id,
                trace_id=trace_id,
            )
            return _invalid_validation_response(failure, request_id=request_id, trace_id=trace_id)

        if result.reason_code == "policy.context_missing":
            failure = _context_failure(
                result.missing_variable or "context",
                "A referenced policy context variable is missing.",
                draft,
            )
            _persist_and_dispatch_validation_failure(
                config,
                context,
                failure,
                expression=expression,
                policy_id=None,
                request_id=request_id,
                trace_id=trace_id,
            )
            return _invalid_validation_response(failure, request_id=request_id, trace_id=trace_id)

        evaluation = result.as_dict()
        if result.timed_out:
            timeout_failure = _DraftFailure(
                code="policy.evaluation_timeout",
                diagnostics=(
                    _diagnostic(
                        "policy.evaluation_timeout",
                        "context",
                        "Policy evaluation exceeded the configured time budget.",
                    ),
                ),
                compiled=draft.compiled,
                action_type=draft.action_type,
                target_model_id=draft.target_model_id,
                fingerprint=draft.fingerprint,
            )
            _persist_and_dispatch_validation_failure(
                config,
                context,
                timeout_failure,
                expression=expression,
                policy_id=None,
                request_id=request_id,
                trace_id=trace_id,
            )

    return PolicyValidationResponse(
        valid=True,
        diagnostics=[],
        dsl_version=draft.compiled.dsl_version,
        draft_fingerprint=draft.fingerprint,
        condition_hash=draft.compiled.condition_hash,
        variables=list(draft.compiled.variables),
        canonical_expression=draft.compiled.canonical_expression,
        evaluation=evaluation,
        request_id=request_id,
        trace_id=trace_id,
    )


def create_policy(
    config: AppConfig,
    context: AuthContext,
    payload: CreatePolicyRequest,
    *,
    audit_request_meta: AuditRequestMeta,
    request_id: str,
    trace_id: str,
) -> PolicyResponse:
    """创建当前租户策略，并原子写入 Rule、Action、审计和 created 事件。"""

    name = _normalize_name(payload.name)
    description = _normalize_description(payload.description)
    status = _normalize_status(payload.status, default="active")
    priority = _normalize_priority(payload.priority, default=100)
    expression = payload.rule.condition_expr
    try:
        draft = _compile_draft(expression, payload.action)
    except _DraftFailure as failure:
        _persist_and_dispatch_validation_failure(
            config,
            context,
            failure,
            expression=expression,
            policy_id=None,
            request_id=request_id,
            trace_id=trace_id,
        )
        # 保存路径不会信任前端“已校验”状态；编译失败写入安全事件后仍必须拒绝持久化。
        raise ApiError(failure.code) from failure

    connection = _connect(config)
    now = _to_db_time(_utcnow())
    policy_id = f"pol_{uuid4().hex}"
    rule_id = f"prule_{uuid4().hex}"
    action_id = f"pact_{uuid4().hex}"
    try:
        connection.execute("BEGIN IMMEDIATE")
        _ensure_policy_quota(connection, context.user.tenant_id)
        _require_active_route_target_in_transaction(connection, context.user.tenant_id, draft)
        connection.execute(
            """
            INSERT INTO policies(
              policy_id, tenant_id, name, description, status, priority, version, dsl_version,
              created_by, updated_by, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
            """,
            (
                policy_id,
                context.user.tenant_id,
                name,
                description,
                status,
                priority,
                draft.compiled.dsl_version,
                context.user.user_id,
                context.user.user_id,
                now,
                now,
            ),
        )
        _insert_rule(connection, context.user.tenant_id, policy_id, rule_id, expression, draft, now)
        _insert_action(connection, context.user.tenant_id, policy_id, action_id, draft, now)
        row = _load_policy_row(connection, context.user.tenant_id, policy_id)
        # 同事务回读不到完整三对象说明数据库合同已损坏，禁止继续生成空审计或 created 事件。
        if row is None:
            raise ApiError("policy.unavailable")
        audit_record = record_audit_log(
            connection,
            context,
            action="policy.created",
            object_type="policy",
            object_id=policy_id,
            object_name=name,
            before_summary={},
            after_summary=_audit_summary(row),
            request_meta=audit_request_meta,
            request_id=request_id,
            trace_id=trace_id,
        )
        policy_event = _insert_created_event(
            connection,
            context,
            row,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        commit_audited_transaction(connection)
    # 锁内目标复核失败必须先释放写锁，再由独立事务记录 validation_failed，避免自锁和半成品策略。
    except _RouteTargetInvalid as exc:
        connection.rollback()
        failure = _route_target_failure(draft)
        _persist_and_dispatch_validation_failure(
            config,
            context,
            failure,
            expression=expression,
            policy_id=None,
            request_id=request_id,
            trace_id=trace_id,
        )
        # 对外只返回统一无泄露目标错误，不能说明模型位于其他租户还是已被禁用。
        raise ApiError("policy.route_target_invalid") from exc
    # 唯一约束与数据库 active trigger 需要精确分类，其余约束错误一律按服务不可用收敛。
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        # 只有 tenant+name 唯一冲突属于可安全公开的重复名称，不暴露其他约束或 SQL 细节。
        if _is_policy_name_conflict(exc):
            raise ApiError("policy.duplicate") from exc
        if "policy_action_target_invalid" in str(exc):
            failure = _route_target_failure(draft)
            _persist_and_dispatch_validation_failure(
                config,
                context,
                failure,
                expression=expression,
                policy_id=None,
                request_id=request_id,
                trace_id=trace_id,
            )
            # 数据库最终防线发现目标失效时仍沿用无枚举泄露的 route_target_invalid 合同。
            raise ApiError("policy.route_target_invalid") from exc
        # 未识别约束可能表示迁移漂移或归属破坏，不能猜测成客户端错误后继续执行。
        raise ApiError("policy.unavailable") from exc
    # created 事件或其他事务内 SQL 也可能遇到磁盘/锁故障；必须回滚三对象与审计并统一返回可重试错误。
    except sqlite3.Error as exc:
        connection.rollback()
        raise ApiError("policy.unavailable") from exc
    # Policy 对外不暴露审计子系统错误命名；存储或提交失败统一为当前领域的可重试 503。
    except ApiError as exc:
        connection.rollback()
        if exc.code == "audit_log.record_unavailable":
            # 审计子系统存储错误在 Policy API 边界统一翻译，避免客户端依赖跨领域内部码。
            raise ApiError("policy.unavailable") from exc
        raise
    except Exception:
        connection.rollback()
        raise

    # 两类 Webhook 各自处理失败，任何外部副作用都不能改变已经提交的 201 结果。
    dispatch_audit_recorded(config, context, audit_record, request_id=request_id, trace_id=trace_id)
    _dispatch_policy_event(config, policy_event)
    _log_policy_changed(context, row, request_id, trace_id, event="policy.created")
    return PolicyResponse(policy=_policy_info(row), request_id=request_id, trace_id=trace_id)


def update_policy(
    config: AppConfig,
    context: AuthContext,
    policy_id: str,
    payload: UpdatePolicyRequest,
    *,
    if_match: str | None,
    audit_request_meta: AuditRequestMeta,
    request_id: str,
    trace_id: str,
) -> PolicyResponse:
    """按 If-Match 更新策略，事务内重读 before 状态并把版本精确推进一。"""

    normalized_policy_id = _normalize_policy_id(policy_id)
    expected_version = _normalize_if_match(if_match)
    provided = set(payload.model_fields_set)
    # 空 PATCH 无法形成可审计变更，必须拒绝而不是无意义递增策略版本。
    if not provided:
        raise ApiError("policy.update_empty")
    if ("rule" in provided and payload.rule is None) or (
        "action" in provided and payload.action is None
    ):
        # 显式 null 不能被当作“保留旧值”，否则客户端状态错误会被静默保存为成功。
        raise ApiError("policy.payload_invalid")

    connection = _connect(config)
    try:
        preliminary = _load_policy_row(connection, context.user.tenant_id, normalized_policy_id)
    # 锁外 tenant-qualified 初读也可能遇到 SQLite I/O 故障；
    # 必须统一返回可重试 503，而不是落入 infra.unhandled。
    except sqlite3.Error as exc:
        raise ApiError("policy.unavailable") from exc
    # 锁外初筛始终带认证 tenant，跨租户策略与真实不存在统一返回 404 防止资源枚举。
    if preliminary is None:
        raise ApiError("policy.not_found")
    # 提前拒绝明显陈旧版本减少写锁竞争；锁内还会再次权威校验避免 TOCTOU。
    if int(preliminary["version"]) != expected_version:
        raise ApiError("policy.version_conflict")

    name = _normalize_name(payload.name) if "name" in provided else str(preliminary["name"])
    description = (
        _normalize_description(payload.description)
        if "description" in provided
        else preliminary["description"]
    )
    status = (
        _normalize_status(payload.status, default=None)
        if "status" in provided
        else str(preliminary["status"])
    )
    priority = (
        _normalize_priority(payload.priority, default=None)
        if "priority" in provided
        else int(preliminary["priority"])
    )
    expression = (
        payload.rule.condition_expr
        if "rule" in provided and payload.rule is not None
        else preliminary["condition_expr"]
    )
    action_draft = (
        payload.action
        if "action" in provided and payload.action is not None
        else PolicyActionDraft(
            type=preliminary["action_type"],
            target_model_id=preliminary["target_model_id"],
        )
    )
    try:
        draft = _compile_draft(expression, action_draft)
    except _DraftFailure as failure:
        _persist_and_dispatch_validation_failure(
            config,
            context,
            failure,
            expression=expression,
            policy_id=normalized_policy_id,
            request_id=request_id,
            trace_id=trace_id,
        )
        # PATCH 编译失败只产生独立安全事件，现有策略版本和三对象必须保持原状。
        raise ApiError(failure.code) from failure

    now = _to_db_time(_utcnow())
    try:
        connection.execute("BEGIN IMMEDIATE")
        before = _load_policy_row(connection, context.user.tenant_id, normalized_policy_id)
        # 进入写事务后重新按 tenant 读取，删除或跨租户状态变化都不能继续覆盖。
        if before is None:
            raise ApiError("policy.not_found")
        # If-Match 在锁内再次比较，确保两个并发 PATCH 只有一个能推进版本。
        if int(before["version"]) != expected_version:
            raise ApiError("policy.version_conflict")
        _require_active_route_target_in_transaction(connection, context.user.tenant_id, draft)
        cursor = connection.execute(
            """
            UPDATE policies
            SET name=?, description=?, status=?, priority=?, version=version+1,
                updated_by=?, updated_at=?
            WHERE tenant_id=? AND policy_id=? AND version=?
            """,
            (
                name,
                description,
                status,
                priority,
                context.user.user_id,
                now,
                context.user.tenant_id,
                normalized_policy_id,
                expected_version,
            ),
        )
        # 条件更新未命中表示版本已变化或数据被并发移除，不能继续写 Rule/Action。
        if cursor.rowcount != 1:
            raise ApiError("policy.version_conflict")
        connection.execute(
            """
            UPDATE policy_rules
            SET condition_expr=?, condition_ast_json=?, condition_hash=?,
                variables_json=?, updated_at=?
            WHERE tenant_id=? AND policy_id=?
            """,
            (
                expression,
                draft.compiled.canonical_json,
                draft.compiled.condition_hash,
                _json_text(list(draft.compiled.variables)),
                now,
                context.user.tenant_id,
                normalized_policy_id,
            ),
        )
        connection.execute(
            """
            UPDATE policy_actions
            SET action_type=?, target_model_id=?, updated_at=?
            WHERE tenant_id=? AND policy_id=?
            """,
            (
                draft.action_type,
                draft.target_model_id,
                now,
                context.user.tenant_id,
                normalized_policy_id,
            ),
        )
        after = _load_policy_row(connection, context.user.tenant_id, normalized_policy_id)
        # 更新后三对象缺失时禁止写入与真实状态不一致的 after 审计摘要。
        if after is None:
            raise ApiError("policy.unavailable")
        audit_record = record_audit_log(
            connection,
            context,
            action="policy.updated",
            object_type="policy",
            object_id=normalized_policy_id,
            object_name=name,
            before_summary=_audit_summary(before),
            after_summary=_audit_summary(after),
            request_meta=audit_request_meta,
            request_id=request_id,
            trace_id=trace_id,
        )
        commit_audited_transaction(connection)
    # 锁内 route target 失效时先整体回滚，再独立提交失败事件并保持旧版本可继续读取。
    except _RouteTargetInvalid as exc:
        connection.rollback()
        failure = _route_target_failure(draft)
        _persist_and_dispatch_validation_failure(
            config,
            context,
            failure,
            expression=expression,
            policy_id=normalized_policy_id,
            request_id=request_id,
            trace_id=trace_id,
        )
        # 跨租户、缺失和 disabled 目标共享同一错误，避免通过 PATCH 探测模型目录。
        raise ApiError("policy.route_target_invalid") from exc
    # 数据库约束是租户和 active 状态的最终防线，必须在回滚后再分类安全错误。
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        # 仅策略名称唯一冲突可映射为 duplicate，其他约束文本不允许直接回显。
        if _is_policy_name_conflict(exc):
            raise ApiError("policy.duplicate") from exc
        if "policy_action_target_invalid" in str(exc):
            failure = _route_target_failure(draft)
            _persist_and_dispatch_validation_failure(
                config,
                context,
                failure,
                expression=expression,
                policy_id=normalized_policy_id,
                request_id=request_id,
                trace_id=trace_id,
            )
            # active trigger 与服务层使用相同无枚举泄露语义，失败事件在锁释放后单独落库。
            raise ApiError("policy.route_target_invalid") from exc
        # 未分类完整性故障可能涉及父子归属或版本触发器，按可重试服务故障拒绝。
        raise ApiError("policy.unavailable") from exc
    # Rule/Action/审计更新的非约束类 SQLite 故障同样不能留下已加版本的半完成策略。
    except sqlite3.Error as exc:
        connection.rollback()
        raise ApiError("policy.unavailable") from exc
    # 更新事务中的审计写入/提交故障也必须保持 Policy API 的统一错误码并回滚旧版本。
    except ApiError as exc:
        connection.rollback()
        if exc.code == "audit_log.record_unavailable":
            # PATCH 与创建使用同一 Policy 错误合同，审计失败不能穿透为 audit_log 命名。
            raise ApiError("policy.unavailable") from exc
        raise
    except Exception:
        connection.rollback()
        raise

    dispatch_audit_recorded(config, context, audit_record, request_id=request_id, trace_id=trace_id)
    _log_policy_changed(context, after, request_id, trace_id, event="policy.updated")
    return PolicyResponse(policy=_policy_info(after), request_id=request_id, trace_id=trace_id)


# 所有策略读取都用相同 tenant-qualified LEFT JOIN；额外完整性检查可识别缺失 Rule/Action。
_POLICY_SELECT = """
SELECT
  p.policy_id, p.tenant_id, p.name, p.description, p.status, p.priority,
  p.version, p.dsl_version, p.created_by, p.updated_by, p.created_at, p.updated_at,
  r.policy_rule_id, r.condition_expr, r.condition_ast_json, r.condition_hash,
  r.variables_json, r.created_at AS rule_created_at, r.updated_at AS rule_updated_at,
  a.policy_action_id, a.action_type, a.target_model_id,
  a.created_at AS action_created_at, a.updated_at AS action_updated_at
FROM policies p
LEFT JOIN policy_rules r ON r.tenant_id=p.tenant_id AND r.policy_id=p.policy_id
LEFT JOIN policy_actions a ON a.tenant_id=p.tenant_id AND a.policy_id=p.policy_id
"""


def _compile_draft(expression: object, action: PolicyActionDraft) -> _CompiledDraft:
    """编译表达式并规范动作，失败只返回固定诊断，不保存不可信正文。"""

    raw_type = action.type
    action_type = raw_type if isinstance(raw_type, str) and raw_type in VALID_ACTIONS else None
    raw_target = action.target_model_id
    safe_target = (
        unicodedata.normalize("NFC", raw_target) if isinstance(raw_target, str) else None
    )
    try:
        compiled = compile_policy_expression(expression)  # type: ignore[arg-type]
    # DSL 编译器只返回固定诊断；原始表达式或解析异常不得进入日志、事件或 HTTP 错误。
    except PolicyDslError as exc:
        raise _DraftFailure(
            code=exc.code,
            diagnostics=exc.diagnostics,
            action_type=action_type,
            target_model_id=safe_target,
        ) from exc

    if raw_target is not None and not isinstance(raw_target, str):
        diagnostic = _diagnostic(
            "policy.action_invalid",
            "action.target_model_id",
            "target_model_id must be a bounded string when provided.",
        )
        raise _DraftFailure(
            code="policy.action_invalid",
            diagnostics=(diagnostic,),
            compiled=compiled,
            action_type=action_type,
        )
    target_model_id = safe_target
    try:
        canonical_policy_action(action_type, target_model_id)  # type: ignore[arg-type]
        fingerprint = policy_draft_fingerprint(
            compiled,
            action_type,  # type: ignore[arg-type]
            target_model_id,
        )
    # 动作规范化失败属于安全领域错误，只保留固定码和已编译 condition hash。
    except PolicyActionError as exc:
        diagnostic = _diagnostic(
            "policy.action_invalid",
            "action",
            exc.message,
        )
        raise _DraftFailure(
            code="policy.action_invalid",
            diagnostics=(diagnostic,),
            compiled=compiled,
            action_type=action_type,
            target_model_id=target_model_id,
        ) from exc
    return _CompiledDraft(
        compiled=compiled,
        action_type=action_type,
        target_model_id=target_model_id,
        fingerprint=fingerprint,
    )


def _normalize_evaluation_context(
    tenant_id: str,
    raw: dict[str, object],
) -> dict[str, object]:
    """规范可选示例上下文，tenant 只取认证值并拒绝 float/bool 预算。"""

    context: dict[str, object] = {"tenant": tenant_id}
    limits = {"env": 64, "project": 128, "model": 128, "data_class": 64}
    for field, limit in limits.items():
        value = raw.get(field)
        if value is None:
            continue
        # 上下文字符串必须有界且无控制字符，避免把 Prompt 片段或终端控制序列带入解释结果。
        if not isinstance(value, str) or _contains_forbidden_text(value) or len(value) > limit:
            raise PolicyContextError(field, f"{field} must be a bounded string.")
        context[field] = unicodedata.normalize("NFC", value)
    budget = raw.get("budget_remaining")
    if budget is not None:
        # 金额只接受普通非负十进制字符串，拒绝 float/bool、指数和非有限值造成比较漂移。
        if type(budget) is not str or not DECIMAL_CONTEXT_RE.fullmatch(budget):
            raise PolicyContextError(
                "budget_remaining",
                "budget_remaining must be a non-negative plain decimal string.",
            )
        # Decimal 再次确认范围和非负，不能依赖正则隐含金额语义。
        if Decimal(budget) > Decimal("999999999999.999999999999"):
            raise PolicyContextError(
                "budget_remaining",
                "budget_remaining exceeds the supported range.",
            )
        context["budget_remaining"] = budget
    return context


def _require_active_route_target(
    config: AppConfig,
    tenant_id: str,
    draft: _CompiledDraft,
) -> None:
    """在无写副作用的 validate 路径按 tenant+active 检查 route_to 目标。"""

    # 非 route_to 动作没有模型目标，不能额外查询模型表或扩大校验副作用。
    if draft.action_type != "route_to":
        return
    connection = _connect(config)
    try:
        row = connection.execute(
            "SELECT 1 FROM models WHERE tenant_id=? AND model_id=? AND status='active'",
            (tenant_id, draft.target_model_id),
        ).fetchone()
    # 模型目录读取故障不能降级为“目标不存在”，否则会掩盖数据库异常并误导管理员。
    except sqlite3.Error as exc:
        raise ApiError("policy.unavailable") from exc
    # 缺失、跨租户与 disabled 统一为安全领域失败，禁止泄露目标在其他边界的真实状态。
    if row is None:
        raise _route_target_failure(draft)


def _require_active_route_target_in_transaction(
    connection: sqlite3.Connection,
    tenant_id: str,
    draft: _CompiledDraft,
) -> None:
    """在 BEGIN IMMEDIATE 同一快照内复核 route_to 目标，消除状态 TOCTOU。"""

    # 只有 route_to 需要锁内模型复核，其他动作直接保持既有事务状态。
    if draft.action_type != "route_to":
        return
    row = connection.execute(
        "SELECT 1 FROM models WHERE tenant_id=? AND model_id=? AND status='active'",
        (tenant_id, draft.target_model_id),
    ).fetchone()
    # 锁内失效使用内部异常触发先 rollback、后独立事件事务，不能在持锁连接上递归写事件。
    if row is None:
        raise _RouteTargetInvalid


def _persist_and_dispatch_validation_failure(
    config: AppConfig,
    context: AuthContext,
    failure: _DraftFailure,
    *,
    expression: object,
    policy_id: str | None,
    request_id: str,
    trace_id: str,
) -> None:
    """独立提交幂等失败事件，并仅在首次插入后分发稳定 Webhook。"""

    event, inserted = _insert_validation_failed_event(
        config,
        context,
        failure,
        expression=expression,
        policy_id=policy_id,
        request_id=request_id,
        trace_id=trace_id,
    )
    if inserted:
        _dispatch_policy_event(config, event)


def _insert_validation_failed_event(
    config: AppConfig,
    context: AuthContext,
    failure: _DraftFailure,
    *,
    expression: object,
    policy_id: str | None,
    request_id: str,
    trace_id: str,
) -> tuple[_PolicyEventRecord, bool]:
    """使用 partial unique 定向去重 validation_failed，其他约束错误一律失败。"""

    connection = _connect(config)
    now = _to_db_time(_utcnow())
    event_id = f"pevt_{uuid4().hex}"
    payload = _validation_event_payload(failure, expression, policy_id=policy_id)
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """
            INSERT INTO policy_events(
              policy_event_id, tenant_id, policy_id, event_type, actor_user_id,
              request_id, trace_id, reason_code, payload_json, created_at
            )
            VALUES(?, ?, ?, 'policy.validation_failed', ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, actor_user_id, request_id)
              WHERE event_type='policy.validation_failed'
            DO NOTHING
            """,
            (
                event_id,
                context.user.tenant_id,
                policy_id,
                context.user.user_id,
                request_id,
                trace_id,
                failure.code,
                _json_text(payload),
                now,
            ),
        )
        inserted = cursor.rowcount == 1
        row = connection.execute(
            """
            SELECT * FROM policy_events
            WHERE tenant_id=? AND actor_user_id=? AND request_id=?
              AND event_type='policy.validation_failed'
            """,
            (context.user.tenant_id, context.user.user_id, request_id),
        ).fetchone()
        # 定向冲突后必须回读同租户同 actor 的既有事件；缺失说明幂等合同或索引已漂移。
        if row is None:
            raise sqlite3.IntegrityError("policy_validation_event_missing")
        connection.commit()
    # 失败事件自身写入或提交失败时必须 rollback，并升级为 503，不能假装原领域错误已可追踪。
    except sqlite3.Error as exc:
        connection.rollback()
        raise ApiError("policy.unavailable") from exc
    return _event_record(row), inserted


def _insert_created_event(
    connection: sqlite3.Connection,
    context: AuthContext,
    row: sqlite3.Row,
    *,
    request_id: str,
    trace_id: str,
    now: str,
) -> _PolicyEventRecord:
    """在创建事务内追加唯一 policy.created 事实并回读持久事件。"""

    event_id = f"pevt_{uuid4().hex}"
    payload = {
        "policy_id": row["policy_id"],
        "version": int(row["version"]),
        "status": row["status"],
        "priority": int(row["priority"]),
        "action_type": row["action_type"],
        "target_model_id": row["target_model_id"],
        "condition_hash": row["condition_hash"],
    }
    connection.execute(
        """
        INSERT INTO policy_events(
          policy_event_id, tenant_id, policy_id, event_type, actor_user_id,
          request_id, trace_id, reason_code, payload_json, created_at
        )
        VALUES(?, ?, ?, 'policy.created', ?, ?, ?, 'policy.created', ?, ?)
        """,
        (
            event_id,
            context.user.tenant_id,
            row["policy_id"],
            context.user.user_id,
            request_id,
            trace_id,
            _json_text(payload),
            now,
        ),
    )
    event_row = connection.execute(
        "SELECT * FROM policy_events WHERE tenant_id=? AND policy_event_id=?",
        (context.user.tenant_id, event_id),
    ).fetchone()
    # created Webhook 只能由已持久化事件重建，回读缺失时必须回滚整个创建事务。
    if event_row is None:
        raise ApiError("policy.unavailable")
    return _event_record(event_row)


def _dispatch_policy_event(config: AppConfig, event: _PolicyEventRecord) -> None:
    """从已提交行构造稳定 Webhook；任何外部失败只记录脱敏摘要。"""

    try:
        webhook_event = build_webhook_event(
            tenant_id=event.tenant_id,
            event_type=event.event_type,
            event_id=f"evt_{event.policy_event_id}",
            timestamp=event.created_at,
            actor_id=event.actor_user_id,
            object_id=event.policy_id or event.policy_event_id,
            trace_id=event.trace_id,
            payload=event.payload,
        )
        # 延迟导入避免 policies 与 webhook service 在应用启动阶段形成循环依赖。
        from metahub_control.webhooks.service import dispatch_webhook_event

        dispatch_webhook_event(
            config,
            event.tenant_id,
            webhook_event,
            request_id=event.request_id,
            trace_id=event.trace_id,
        )
    except Exception:
        # Webhook 构造/发送异常可能携带客户正文或底层 SQL，日志只能保留固定脱敏定位摘要。
        logger.error(
            "policy.webhook_dispatch_failed",
            extra={
                "event": "policy.webhook_dispatch_failed",
                "tenant_id": event.tenant_id,
                "actor_id": event.actor_user_id,
                "object_id": event.policy_id or event.policy_event_id,
                "request_id": event.request_id,
                "trace_id": event.trace_id,
                "error_code": "webhook.delivery_unavailable",
            },
        )


def _validation_event_payload(
    failure: _DraftFailure,
    expression: object,
    *,
    policy_id: str | None,
) -> dict[str, object]:
    """构造含可空 policy_id 的失败事件白名单摘要，绝不散列无法编译的低熵原文。"""

    safe_action = failure.action_type if failure.action_type in VALID_ACTIONS else "invalid"
    payload: dict[str, object] = {
        "policy_id": policy_id,
        "expression_length": len(expression) if isinstance(expression, str) else 0,
        "action_type": safe_action,
        "diagnostic_codes": [item.code for item in failure.diagnostics[:MAX_DIAGNOSTICS]],
    }
    if failure.compiled is not None:
        payload["condition_hash"] = failure.compiled.condition_hash
    if failure.fingerprint is not None:
        payload["draft_fingerprint"] = failure.fingerprint
    return payload


def _event_record(row: sqlite3.Row) -> _PolicyEventRecord:
    """把持久事件行转换为提交后安全事实，并严格校验 payload object。"""

    try:
        payload = json.loads(row["payload_json"])
    # 事件 payload 若被损坏，禁止把解析异常或原始 JSON 带入日志和外部 Webhook。
    except (TypeError, ValueError) as exc:
        raise ApiError("policy.unavailable") from exc
    # payload 必须维持安全 object 合同，数组或标量不能作为事件信封继续分发。
    if not isinstance(payload, dict):
        raise ApiError("policy.unavailable")
    return _PolicyEventRecord(
        policy_event_id=row["policy_event_id"],
        tenant_id=row["tenant_id"],
        policy_id=row["policy_id"],
        event_type=row["event_type"],
        actor_user_id=row["actor_user_id"],
        request_id=row["request_id"],
        trace_id=row["trace_id"],
        reason_code=row["reason_code"],
        payload=payload,
        created_at=_parse_db_time(row["created_at"]),
    )


def _insert_rule(
    connection: sqlite3.Connection,
    tenant_id: str,
    policy_id: str,
    rule_id: str,
    expression: object,
    draft: _CompiledDraft,
    now: str,
) -> None:
    """插入当前策略唯一 Rule；调用前表达式已经确保为字符串并安全编译。"""

    connection.execute(
        """
        INSERT INTO policy_rules(
          policy_rule_id, tenant_id, policy_id, condition_expr, condition_ast_json,
          condition_hash, variables_json, created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rule_id,
            tenant_id,
            policy_id,
            expression,
            draft.compiled.canonical_json,
            draft.compiled.condition_hash,
            _json_text(list(draft.compiled.variables)),
            now,
            now,
        ),
    )


def _insert_action(
    connection: sqlite3.Connection,
    tenant_id: str,
    policy_id: str,
    action_id: str,
    draft: _CompiledDraft,
    now: str,
) -> None:
    """插入当前策略唯一 Action；route target 已在相同事务内验证。"""

    connection.execute(
        """
        INSERT INTO policy_actions(
          policy_action_id, tenant_id, policy_id, action_type, target_model_id,
          created_at, updated_at
        )
        VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (
            action_id,
            tenant_id,
            policy_id,
            draft.action_type,
            draft.target_model_id,
            now,
            now,
        ),
    )


def _load_policy_row(
    connection: sqlite3.Connection,
    tenant_id: str,
    policy_id: str,
) -> sqlite3.Row | None:
    """按 tenant+policy 回读完整三对象；不完整父对象按服务不可用拒绝。"""

    row = connection.execute(
        f"{_POLICY_SELECT} WHERE p.tenant_id=? AND p.policy_id=?",
        (tenant_id, policy_id),
    ).fetchone()
    if row is None:
        return None
    # Policy 缺 Rule 或 Action 表示事务原子性被破坏，不能向 admin 返回看似可编辑的半对象。
    if row["policy_rule_id"] is None or row["policy_action_id"] is None:
        raise ApiError("policy.unavailable")
    return row


def _ensure_complete_policy_graph(connection: sqlite3.Connection, tenant_id: str) -> None:
    """拒绝任何缺 Rule/Action 的父策略，避免筛选把损坏对象静默隐藏。"""

    row = connection.execute(
        """
        SELECT 1
        FROM policies p
        LEFT JOIN policy_rules r ON r.tenant_id=p.tenant_id AND r.policy_id=p.policy_id
        LEFT JOIN policy_actions a ON a.tenant_id=p.tenant_id AND a.policy_id=p.policy_id
        WHERE p.tenant_id=? AND (r.policy_rule_id IS NULL OR a.policy_action_id IS NULL)
        LIMIT 1
        """,
        (tenant_id,),
    ).fetchone()
    # 任一孤立父策略都会让筛选和优先级解释失真，因此整次租户列表必须 fail closed。
    if row is not None:
        raise ApiError("policy.unavailable")


def _ensure_policy_quota(connection: sqlite3.Connection, tenant_id: str) -> None:
    """在 BEGIN IMMEDIATE 内检查租户策略配额，避免并发创建同时越限。"""

    row = connection.execute(
        "SELECT COUNT(*) AS policy_count FROM policies WHERE tenant_id=?",
        (tenant_id,),
    ).fetchone()
    # 配额在 BEGIN IMMEDIATE 内按认证 tenant 计数，空结果或达到上限都禁止继续创建。
    if row is None or int(row["policy_count"]) >= MAX_POLICIES_PER_TENANT:
        raise ApiError("policy.quota_exceeded")


def _policy_info(row: sqlite3.Row) -> PolicyInfo:
    """把三表 JOIN 行转换为 admin 管理 DTO，并验证 variables JSON。"""

    try:
        variables = json.loads(row["variables_json"])
        # 编译变量必须是字符串数组；其他结构可能绕过白名单并污染后续快照。
        if not isinstance(variables, list) or any(not isinstance(item, str) for item in variables):
            raise ValueError("policy_variables_invalid")
        return PolicyInfo(
            policy_id=row["policy_id"],
            tenant_id=row["tenant_id"],
            name=row["name"],
            description=row["description"],
            status=row["status"],
            priority=int(row["priority"]),
            version=int(row["version"]),
            dsl_version=row["dsl_version"],
            rule=PolicyRuleInfo(
                policy_rule_id=row["policy_rule_id"],
                condition_expr=row["condition_expr"],
                condition_hash=row["condition_hash"],
                variables=variables,
                created_at=row["rule_created_at"],
                updated_at=row["rule_updated_at"],
            ),
            action=PolicyActionInfo(
                policy_action_id=row["policy_action_id"],
                type=row["action_type"],
                target_model_id=row["target_model_id"],
                created_at=row["action_created_at"],
                updated_at=row["action_updated_at"],
            ),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
    # variables、数值或 Pydantic 日期/枚举解析失败都说明持久行损坏，只返回统一可重试错误。
    except (TypeError, ValueError) as exc:
        raise ApiError("policy.unavailable") from exc


def _audit_summary(row: sqlite3.Row) -> dict[str, object]:
    """构造策略审计白名单摘要，表达式、AST 与描述正文永不进入审计。"""

    try:
        variables = json.loads(row["variables_json"])
        # 审计 variables 必须是纯字符串白名单，数字或对象不能进入 join 或审计摘要。
        if not isinstance(variables, list) or any(not isinstance(item, str) for item in variables):
            raise ValueError("policy_variables_invalid")
        return {
            "name": row["name"],
            "status": row["status"],
            "priority": int(row["priority"]),
            "version": int(row["version"]),
            "dsl_version": row["dsl_version"],
            "action_type": row["action_type"],
            "target_model_id": row["target_model_id"],
            "condition_hash": row["condition_hash"],
            # 审计摘要保持扁平标量；固定六变量按排序后逗号连接，不引入任意嵌套 JSON。
            "variables": ",".join(variables),
            "description_hash": _optional_text_hash(row["description"]),
        }
    # 损坏摘要不能变成 TypeError 500，更不能在事务已改版本后跳过 rollback。
    except (TypeError, ValueError) as exc:
        raise ApiError("policy.unavailable") from exc


def _invalid_validation_response(
    failure: _DraftFailure,
    *,
    request_id: str,
    trace_id: str,
) -> PolicyValidationResponse:
    """把领域失败转换为 HTTP 200 valid=false 的安全编辑器响应。"""

    return PolicyValidationResponse(
        valid=False,
        diagnostics=[
            PolicyDiagnosticInfo(**diagnostic.as_dict())
            for diagnostic in failure.diagnostics[:MAX_DIAGNOSTICS]
        ],
        dsl_version=failure.compiled.dsl_version if failure.compiled else None,
        draft_fingerprint=failure.fingerprint,
        condition_hash=failure.compiled.condition_hash if failure.compiled else None,
        variables=list(failure.compiled.variables) if failure.compiled else None,
        canonical_expression=(
            failure.compiled.canonical_expression if failure.compiled else None
        ),
        request_id=request_id,
        trace_id=trace_id,
    )


def _route_target_failure(draft: _CompiledDraft) -> _DraftFailure:
    """构造不区分不存在、跨租户或 disabled 的统一目标诊断。"""

    return _DraftFailure(
        code="policy.route_target_invalid",
        diagnostics=(
            _diagnostic(
                "policy.route_target_invalid",
                "action.target_model_id",
                "route_to target is not an active model in the current tenant.",
            ),
        ),
        compiled=draft.compiled,
        action_type=draft.action_type,
        target_model_id=draft.target_model_id,
        fingerprint=draft.fingerprint,
    )


def _context_failure(variable: str, message: str, draft: _CompiledDraft) -> _DraftFailure:
    """构造不包含上下文实际值的固定 context 失败。"""

    context_variables = {"env", "project", "model", "data_class", "budget_remaining"}
    safe_variable = variable if variable in context_variables else "context"
    return _DraftFailure(
        code="policy.context_invalid",
        diagnostics=(
            _diagnostic(
                "policy.context_invalid",
                f"context.{safe_variable}" if safe_variable != "context" else "context",
                message,
            ),
        ),
        compiled=draft.compiled,
        action_type=draft.action_type,
        target_model_id=draft.target_model_id,
        fingerprint=draft.fingerprint,
    )


def _diagnostic(code: str, field: str, message: str) -> PolicyDiagnostic:
    """生成无源码片段的字段级诊断。"""

    return PolicyDiagnostic(code=code, field=field, start=None, end=None, message=message)


def _normalize_name(value: object) -> str:
    """规范策略名称并拒绝控制字符、空值和超长文本。"""

    # 名称是审计和控制台对象标识，只接受明确字符串，不能隐式转换任意 DTO。
    if not isinstance(value, str):
        raise ApiError("policy.payload_invalid")
    normalized = unicodedata.normalize("NFC", value.strip())
    # 空名、超长名与控制字符会破坏唯一性和展示边界，统一按安全 payload 错误拒绝。
    if not normalized or len(normalized) > MAX_NAME_LENGTH or _contains_forbidden_text(normalized):
        raise ApiError("policy.payload_invalid")
    return normalized


def _normalize_description(value: object) -> str | None:
    """规范可选描述；空白等价于清除，正文只保存在策略主表。"""

    if value is None:
        return None
    # 描述只允许自由文本字符串，其他结构不能被序列化进策略或审计 hash。
    if not isinstance(value, str):
        raise ApiError("policy.payload_invalid")
    normalized = unicodedata.normalize("NFC", value.strip())
    if not normalized:
        return None
    # 有界且无控制字符的描述才可入库，避免日志终端注入和无界治理正文。
    if len(normalized) > MAX_DESCRIPTION_LENGTH or _contains_forbidden_text(normalized):
        raise ApiError("policy.payload_invalid")
    return normalized


def _normalize_status(value: object, *, default: str | None) -> str:
    """规范 active/disabled；显式 null 在 PATCH 中不能被误当成省略。"""

    candidate = default if value is None and default is not None else value
    # 状态必须来自固定枚举，不能让任意字符串改变未来快照候选语义。
    if not isinstance(candidate, str) or candidate not in VALID_STATUSES:
        raise ApiError("policy.payload_invalid")
    return candidate


def _normalize_priority(value: object, *, default: int | None) -> int:
    """规范 1-10000 优先级，并显式拒绝 Python bool/int 混淆。"""

    candidate = default if value is None and default is not None else value
    # bool 在 Python 中是 int 子类，必须用精确类型判断避免 True 被当作最高优先级 1。
    if type(candidate) is not int or not 1 <= candidate <= 10_000:
        raise ApiError("policy.payload_invalid")
    return candidate


def _normalize_status_filter(value: str | None) -> str:
    """规范 GET 状态筛选并默认 all，避免任意值改变租户列表查询合同。"""

    normalized = str(value or "all").strip().lower()
    # 只允许 all/active/disabled，未知筛选不能静默返回空列表掩盖客户端错误。
    if normalized not in {"all", *VALID_STATUSES}:
        raise ApiError("policy.query_invalid")
    return normalized


def _normalize_action_filter(value: str | None) -> str | None:
    """规范 GET 动作筛选；空值表示不限制。"""

    if value is None or not value.strip():
        return None
    normalized = value.strip().lower()
    # 动作筛选复用 DSL v1 固定白名单，禁止未知动作落入动态 SQL 条件。
    if normalized not in VALID_ACTIONS:
        raise ApiError("policy.query_invalid")
    return normalized


def _normalize_policy_id(value: str) -> str:
    """校验路径 Policy ID，避免无界文本进入 SQL、日志或事件。"""

    normalized = str(value or "").strip()
    # 非服务端 pol_ UUID 统一按当前租户不存在处理，避免路径探测进入数据库或日志。
    if not POLICY_ID_RE.fullmatch(normalized):
        raise ApiError("policy.not_found")
    return normalized


def _normalize_if_match(value: str | None) -> int:
    """解析强 If-Match 版本；缺失、弱 ETag 或任意通配均按冲突拒绝。"""

    match = IF_MATCH_RE.fullmatch(str(value or "").strip())
    # 缺失或畸形版本不能退化为无条件覆盖，统一按并发冲突要求客户端重新读取。
    if match is None:
        raise ApiError("policy.version_conflict")
    return int(match.group(1))


def _contains_forbidden_text(value: str) -> bool:
    """识别控制字符和 Unicode 代理字符，保护数据库、事件和前端边界。"""

    return any(unicodedata.category(character) in {"Cc", "Cs"} for character in value)


def _optional_text_hash(value: object) -> str | None:
    """对可选描述生成不可逆摘要，避免自由文本扩散到审计。"""

    if value is None:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _is_policy_name_conflict(exc: sqlite3.IntegrityError) -> bool:
    """只把 policies tenant+name 唯一冲突映射为 duplicate，其他约束保持 503。"""

    message = str(exc)
    # 仅检查稳定的 tenant+name 唯一约束片段，不把完整 SQLite 文本返回给调用方。
    return "policies.tenant_id, policies.name" in message


def _json_text(value: object) -> str:
    """稳定序列化安全摘要与 canonical 派生数组，不接收 DTO 或异常对象。"""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _connect(config: AppConfig) -> sqlite3.Connection:
    """连接控制面数据库，并把基础设施错误收敛为策略服务不可用。"""

    try:
        return connect_database(config.database_url)
    # 数据库 URL 或连接故障统一为策略 503，不能把文件路径和底层驱动错误暴露给 API。
    # DbError 覆盖连接合同，OSError 额外覆盖数据库目录创建等文件系统故障，两者均不得泄露路径。
    except (DbError, OSError) as exc:
        raise ApiError("policy.unavailable") from exc


def _utcnow() -> datetime:
    """返回 UTC 当前时间，独立函数便于事务与事件测试注入。"""

    return datetime.now(UTC)


def _to_db_time(value: datetime) -> str:
    """把 UTC 时间转换为稳定数据库字符串。"""

    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_db_time(value: str) -> datetime:
    """把数据库 UTC 字符串解析为 Webhook 所需 aware datetime。"""

    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    # 持久事件时间损坏时禁止构造错误 Webhook 时间戳，统一停止外部分发。
    except ValueError as exc:
        raise ApiError("policy.unavailable") from exc
    return parsed.astimezone(UTC)


def _log_policy_changed(
    context: AuthContext,
    row: sqlite3.Row,
    request_id: str,
    trace_id: str,
    *,
    event: str,
) -> None:
    """记录不含表达式、AST、描述或 target 之外正文的策略变更摘要。"""

    logger.info(
        event,
        extra={
            "event": event,
            "tenant_id": context.user.tenant_id,
            "actor_id": context.user.user_id,
            "object_id": row["policy_id"],
            "request_id": request_id,
            "trace_id": trace_id,
            "payload": {
                "version": int(row["version"]),
                "status": row["status"],
                "priority": int(row["priority"]),
                "action_type": row["action_type"],
                "condition_hash": row["condition_hash"],
            },
        },
    )
