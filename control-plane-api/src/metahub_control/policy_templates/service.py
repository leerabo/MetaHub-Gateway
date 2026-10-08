"""PRD-068 行业/场景策略模板的规划、原子应用与幂等恢复服务。

模板正文来自随迁移冻结的五条只读定义，客户端只能提交有限绑定。精确预览和应用共用同一个
纯规划器；应用在 ``BEGIN IMMEDIATE`` 内重新读取权限、租户资源、Snapshot head、价格、
KeyPool 和预算事实，再创建一个 Policy 与一个不可变 Snapshot。任何审计、事件或数据库约束
失败都会回滚整笔事务，提交后 Webhook 失败则不会反转已经确认的治理事实。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from secrets import token_hex
from typing import Literal
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.keypool import ManagedCredentialFact, evaluate_provider_key_pool
from metahub_shared.runtime.policy_snapshot import (
    PolicySnapshotItem,
    PolicySnapshotValidationError,
    build_policy_snapshot_item,
    compute_policy_snapshot_content_hash,
)
from metahub_shared.runtime.provider_adapter_spec import (
    CUSTOMER_MANAGED,
    DOMESTIC_PROVIDER_TYPES,
    LOCAL_COMPATIBLE,
    MIGRATION_PLATFORM,
    MODEL_VENDOR_CLOUD,
    ProviderAdapterSpecError,
    provider_adapter_spec,
)
from metahub_shared.runtime.webhooks import build_webhook_event

from metahub_control.audit import (
    AuditRecord,
    AuditRequestMeta,
    commit_audited_transaction,
    dispatch_audit_recorded,
    new_audit_log_id,
    record_audit_log,
)
from metahub_control.auth import AuthContext
from metahub_control.auth.resource_transactions import ResourceWriteTransaction
from metahub_control.policies.models import PolicyActionDraft, PolicyInfo
from metahub_control.policies.service import (
    _audit_summary as _policy_audit_summary,
)
from metahub_control.policies.service import (
    _compile_draft,
    _CompiledDraft,
    _dispatch_policy_event,
    _DraftFailure,
    _ensure_policy_quota,
    _insert_action,
    _insert_created_event,
    _insert_rule,
    _load_policy_row,
    _policy_info,
    _PolicyEventRecord,
)
from metahub_control.policies.service import (
    _event_record as _policy_event_record,
)
from metahub_control.policy_snapshots.models import PolicySnapshotMutationResponse
from metahub_control.policy_snapshots.service import (
    MAX_SNAPSHOT_VERSION,
    _advance_head,
    _artifact_audit_summary,
    _Candidate,
    _compile_active_candidate,
    _create_published_snapshot,
    _dispatch_snapshot_event,
    _insert_snapshot_event,
    _load_current_row,
    _load_snapshot_row,
    _load_verified_artifact,
    _policy_item_graph_hash,
    _require_head_version,
    _required_project_values,
    _snapshot_info,
    _snapshot_row_audit_summary,
    _SnapshotEvent,
    _validate_active_template_invariants,
)
from metahub_control.policy_snapshots.service import (
    _load_replay_dispatch_facts as _load_snapshot_replay_dispatch_facts,
)

from .models import (
    ApplyPolicyTemplateRequest,
    PolicyTemplateAliasOption,
    PolicyTemplateApplicationInfo,
    PolicyTemplateApplyResponse,
    PolicyTemplateBindingOptions,
    PolicyTemplateBindingsRequest,
    PolicyTemplateCompatibility,
    PolicyTemplateImpact,
    PolicyTemplateImpactOrderItem,
    PolicyTemplateInfo,
    PolicyTemplateListResponse,
    PolicyTemplateProjectOption,
)

logger = logging.getLogger("metahub.control.policy_templates")

# 路径、CAS、时间与规划上限和 0060 数据库 CHECK 保持一致。任何放宽都必须同步设计与测试。
_TEMPLATE_ID_RE = re.compile(r"ptpl_[a-z0-9_]{3,120}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_HEAD_ETAG_RE = re.compile(r'"(0|[1-9][0-9]{0,9})"\Z')
_CANDIDATE_ETAG_RE = re.compile(r'"([0-9a-f]{64})"\Z')
_MAX_POLICY_COUNT = 200
_MAX_DOMESTIC_ITEMS = 20
_MAX_IDENTIFIER_LENGTH = 128
_MASK_CAPABILITY = "policy-mask-v1"
_CHANGED_RESOURCE_TYPES = ("policy", "policy_snapshot")

# 五条 definition_hash 是不含 tenant/time 的迁移身份。服务层再次核对可防止关闭触发器后
# 篡改蓝图或动作，再借模板入口生成任意 Policy。
_TEMPLATE_CATALOG: dict[str, dict[str, object]] = {
    "low_cost_mode": {
        "id": "ptpl_low_cost_mode",
        "priority": 1000,
        "action": {"type": "route_to", "target_model_id": "$derived_model_id"},
        "bindings": ("project_id",),
        "definition_hash": "cdeeba7fae097a0050b94d259b79b5dc202412990ef387e6596e5d3dfb92e20f",
    },
    "high_security_mode": {
        "id": "ptpl_high_security_mode",
        "priority": 10,
        "action": {"type": "deny"},
        "bindings": ("project_id", "model_alias_id"),
        "definition_hash": "1536ca9581138a80b900e8db8d6cbd69f810e0d7a89665503166f46ebe564060",
    },
    "domestic_models_only": {
        "id": "ptpl_domestic_models_only",
        "priority": 10,
        "action": {"type": "deny"},
        "bindings": ("project_id",),
        "definition_hash": "7617ea2cd178e2de41e06bb0fd09e9b670ed1c0c27a58354d94666a6bfbb52f1",
    },
    "budget_burst_guard": {
        "id": "ptpl_budget_burst_guard",
        "priority": 10,
        "action": {"type": "deny"},
        "bindings": ("project_id", "threshold_cny"),
        "definition_hash": "c11bed7d12ab2d388587377fd51e9ca4a1b7d4190dbf597432888c48462f365f",
    },
    "customer_service_redaction": {
        "id": "ptpl_customer_service_redaction",
        "priority": 10,
        "action": {"type": "mask"},
        "bindings": ("project_id",),
        "definition_hash": "77e32f51cfbf27c98b7c5506b70b14124eaf292458a71f07541d2619516ca4ac",
    },
}
_TEMPLATE_ORDER = tuple(_TEMPLATE_CATALOG)


@dataclass(frozen=True, slots=True)
class _Dependency:
    """保存 application 可持久化的单条无敏感依赖事实。"""

    resource_type: str
    resource_id: str
    relation: str
    resource_name: str | None
    version_or_hash: str


@dataclass(frozen=True, slots=True)
class _SemanticPlan:
    """保存某个模板派生出的固定 DSL、动作和资源依赖。"""

    expression: str
    action_type: Literal["deny", "mask", "route_to"]
    target_model_id: str | None
    dependencies: tuple[_Dependency, ...]
    warning_codes: tuple[str, ...] = ()
    derived_target_model_id: str | None = None
    derived_target_model_name: str | None = None
    allowed_model_alias_names: tuple[str, ...] = ()
    frozen_environment_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Plan:
    """保存一次给定 nonce、时点和数据库快照的完整规划结果。"""

    template_row: sqlite3.Row
    bindings: dict[str, str]
    bindings_hash: str
    status: Literal["ready", "ready_with_warnings", "incompatible"]
    reason_codes: tuple[str, ...]
    warning_codes: tuple[str, ...]
    can_apply: bool
    required_permissions: tuple[str, ...]
    preview_nonce: str
    preview_as_of: datetime
    current_row: sqlite3.Row | None
    before_candidate: _Candidate
    draft: _CompiledDraft | None
    after_candidate: _Candidate | None
    dependencies: tuple[_Dependency, ...]
    applicability_fingerprint: str | None
    impact_hash: str | None
    generated_ids: dict[str, str]
    impact: PolicyTemplateImpact | None


@dataclass(frozen=True, slots=True)
class _TemplateEvent:
    """提交后派发 EVT-075 所需的最小持久事实。"""

    event_id: str
    tenant_id: str
    application_id: str
    actor_user_id: str
    payload: dict[str, object]
    request_id: str
    trace_id: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class _ApplyDispatchFacts:
    """保存一次 apply 提交后可独立补派的六类不可变事实。"""

    policy_audit: AuditRecord
    snapshot_audit: AuditRecord
    template_audit: AuditRecord
    policy_event: _PolicyEventRecord
    snapshot_event: _SnapshotEvent
    template_event: _TemplateEvent


@dataclass(frozen=True, slots=True)
class _RollbackDispatchFacts:
    """保存模板补偿撤销提交后可独立补派的四类不可变事实。"""

    policy_audit: AuditRecord
    snapshot_audit: AuditRecord
    template_audit: AuditRecord
    snapshot_event: _SnapshotEvent


class _NotApplicable(Exception):
    """表示绑定合法但当前租户事实不能安全生成模板。"""

    def __init__(self, reason_code: str) -> None:
        """保存固定安全原因码，不保存绑定、价格或数据库异常正文。"""

        self.reason_code = reason_code
        super().__init__("Policy template is not applicable.")


def list_policy_templates(
    config: AppConfig,
    context: AuthContext,
    *,
    template_id: str | None,
    bindings: PolicyTemplateBindingsRequest | None,
    request_id: str,
    trace_id: str,
) -> tuple[PolicyTemplateListResponse, str | None, str | None]:
    """返回固定五模板目录，并在指定模板时生成一次 tenant-bound 精确预览。

    参数：模板路径和绑定已经过路由 exact-set 解析；tenant/actor 只取认证上下文。返回响应和
    可选 head/candidate 强 ETag。关键约束：每次真实预览生成新 nonce，数据库连接不读取其他
    tenant 的选项或依赖，失败不回显 SQL、价格、Key 或绑定正文。
    """

    connection = _connect(config)
    try:
        rows = _load_template_catalog(connection, context.user.tenant_id)
        options = _load_binding_options(connection, context.user.tenant_id)
        selected_plan: _Plan | None = None
        normalized_template_id: str | None = None
        if template_id is not None:
            normalized_template_id = _normalize_template_id(template_id)
            selected = next(
                (row for row in rows if row["policy_template_id"] == normalized_template_id),
                None,
            )
            # 路径身份必须属于当前租户物化目录，不用全局固定 ID 推断其他租户对象存在性。
            if selected is None:
                raise ApiError("policy_template.not_found")
            # 精确预览必须同时携带完整绑定，禁止把普通目录响应误当成可提交的计划。
            if bindings is None:
                raise ApiError("policy_template.query_invalid")
            selected_plan = _build_plan(
                config,
                connection,
                context,
                selected,
                bindings,
                preview_nonce=token_hex(32),
                preview_as_of=_utcnow(),
            )
        infos = [
            _template_info(
                row,
                context,
                options,
                plan=(
                    selected_plan
                    if normalized_template_id == row["policy_template_id"]
                    else None
                ),
            )
            for row in rows
        ]
        response = PolicyTemplateListResponse(
            policy_templates=infos,
            request_id=request_id,
            trace_id=trace_id,
        )
        # 未选择模板或适配失败时不签发双 CAS，避免客户端提交未经证明的影响计划。
        if selected_plan is None or selected_plan.impact_hash is None:
            return response, None, None
        assert selected_plan.current_row is not None
        return (
            response,
            f'"{int(selected_plan.current_row["snapshot_version"])}"',
            f'"{selected_plan.before_candidate.content_hash}"',
        )
    # 已归类领域异常保留固定错误码，不能被数据库兜底转换掩盖真实修复动作。
    except ApiError:
        raise
    # 存储或反序列化损坏统一收敛，响应不得包含 SQL、价格或绑定原文。
    except (sqlite3.Error, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise ApiError("policy_template.unavailable") from exc
    finally:
        connection.close()


def apply_policy_template(
    config: AppConfig,
    context: AuthContext,
    policy_template_id: str,
    payload: ApplyPolicyTemplateRequest,
    *,
    if_match: str,
    candidate_match: str,
    audit_request_meta: AuditRequestMeta,
    request_id: str,
    trace_id: str,
) -> PolicyTemplateApplyResponse:
    """原子应用一次精确模板预览，并发布唯一的新 PolicySnapshot。

    参数：路径模板 ID、严格 DTO、两条强 CAS Header 与认证/审计上下文。返回：已提交
    application、生成 Policy 和结果 Snapshot 的安全投影。关键约束：tenant、actor 和权限
    只来自认证上下文；写锁内重做权限、依赖、双时点计划与双 CAS；任何业务、审计或事件
    事实失败都会回滚，提交后的六类派发则彼此隔离。
    """

    template_id = _normalize_template_id(policy_template_id)
    expected_head = _normalize_head_match(if_match)
    expected_candidate = _normalize_candidate_match(candidate_match)
    normalized_request_id = _normalize_correlation_id(request_id)
    normalized_trace_id = _normalize_correlation_id(trace_id)
    preview_nonce = _normalize_sha256(payload.preview_nonce, "policy_template.payload_invalid")
    supplied_impact_hash = _normalize_sha256(
        payload.impact_hash,
        "policy_template.payload_invalid",
    )
    preview_as_of = _require_utc_datetime(payload.preview_as_of)
    reason = _normalize_apply_reason(payload.reason)
    bindings_wire = payload.bindings.model_dump(mode="json", exclude_none=True)
    request_fingerprint = _sha256_json(
        {
            "schema": "policy-template-apply-request-v1",
            "actor_user_id": context.user.user_id,
            "policy_template_id": template_id,
            "template_version": payload.template_version,
            "preview_nonce": preview_nonce,
            "preview_as_of": _to_wire_time(preview_as_of),
            "impact_hash": supplied_impact_hash,
            "bindings": bindings_wire,
            "reason_hash": hashlib.sha256(reason.encode("utf-8")).hexdigest(),
            "expected_head": expected_head,
            "expected_candidate": expected_candidate,
        }
    )

    connection = _connect(config)
    result: PolicyTemplateApplyResponse | None = None
    dispatch_facts: _ApplyDispatchFacts | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = _load_application_by_request(
            connection,
            context.user.tenant_id,
            normalized_request_id,
        )
        # tenant+request 命中时只能回放同一 actor 和同一规范化意图，禁止借 ID 读取他人结果。
        if existing is not None:
            if (
                existing["actor_user_id"] != context.user.user_id
                or existing["request_fingerprint"] != request_fingerprint
            ):
                # 同一 request_id 只能由原操作者重放完全相同的命令，防止跨用户借用幂等结果。
                raise ApiError("policy_template.request_conflict")
            locked_context = ResourceWriteTransaction(
                connection, config, context, "policy_template.apply"
            ).verify()
            # 幂等键只证明历史命令相同，不授予继续读取结果或补派事件的权限；回放必须使用
            # 写锁内实时矩阵同时复核专用 apply 权限和该模板的复合领域权限，撤权后立即失败关闭。
            if not _compound_permission_satisfied(
                str(existing["template_code"]),
                locked_context.permission_codes,
            ):
                # 缺少模板复合领域权限时不得恢复响应或重派历史事件，统一按实时撤权返回 403。
                raise ApiError("auth.permission_denied")
            result = _load_application_response(
                connection,
                existing,
                idempotent_replay=True,
            )
            dispatch_facts = _load_apply_dispatch_facts(connection, existing)
            # 幂等回放只读取持久事实；显式结束读事务后再触发任何外部派发。
            connection.rollback()
        else:
            snapshot_request = connection.execute(
                "SELECT 1 FROM policy_snapshots WHERE tenant_id=? AND request_id=? LIMIT 1",
                (context.user.tenant_id, normalized_request_id),
            ).fetchone()
            # Snapshot 与 Template 共用 request 命名空间，避免延迟外键关联到另一条命令。
            if snapshot_request is not None:
                raise ApiError("policy_template.request_conflict")
            consumed_nonce = connection.execute(
                """
                SELECT request_id FROM policy_template_applications
                WHERE tenant_id=? AND preview_nonce=?
                """,
                (context.user.tenant_id, preview_nonce),
            ).fetchone()
            # nonce 只能产生一个成功 application，新 request 复用必须在任何写入前拒绝。
            if consumed_nonce is not None:
                raise ApiError("policy_template.nonce_conflict")

            locked_context = ResourceWriteTransaction(
                connection, config, context, "policy_template.apply"
            ).verify()
            catalog = _load_template_catalog(connection, context.user.tenant_id)
            template_row = next(
                (row for row in catalog if row["policy_template_id"] == template_id),
                None,
            )
            # 写锁内再次按租户目录定位模板，关闭预览后切租户或目录损坏的竞态窗口。
            if template_row is None:
                raise ApiError("policy_template.not_found")
            # 模板定义版本属于 CAS 事实，服务端不能静默升级客户端确认过的治理语义。
            if payload.template_version != int(template_row["version"]):
                raise ApiError("policy_template.version_conflict")
            code = str(template_row["template_code"])
            # 分类权限必须来自锁内数据库矩阵，入口 apply 权限不能替代职责分离复核。
            if not _compound_permission_satisfied(code, locked_context.permission_codes):
                raise ApiError("auth.permission_denied")

            try:
                _ensure_policy_quota(connection, context.user.tenant_id)
            # 原始 Policy quota 错误映射为模板适配失败，避免暴露另一领域的写入口细节。
            except ApiError as exc:
                if exc.code == "policy.quota_exceeded":
                    raise ApiError("policy_template.not_applicable") from exc
                raise

            preview_plan = _build_plan(
                config,
                connection,
                locked_context,
                template_row,
                payload.bindings,
                preview_nonce=preview_nonce,
                preview_as_of=preview_as_of,
            )
            # POST 已声明一份曾可应用的精确预览；写锁内按旧观察时点重算后若因 Alias、
            # Budget、KeyPool 或部署门禁变化而不再兼容，语义是预览漂移而非首次不适配。
            # quota 和同 Project 活跃应用已分别由前置检查/专用分支保留独立错误码。
            _require_applicable_plan(preview_plan, drift=True)
            # 可应用计划必须同时拥有已发布基线和完整候选；缺任一项都不得制造虚构回滚点。
            if preview_plan.current_row is None or preview_plan.after_candidate is None:
                raise ApiError("policy_template.not_applicable")
            # 三项 CAS 分别冻结 head、当前候选和确认影响，任一不一致都要求重新预览。
            if int(preview_plan.current_row["snapshot_version"]) != expected_head:
                raise ApiError("policy_template.impact_conflict")
            if preview_plan.before_candidate.content_hash != expected_candidate:
                # 候选哈希变化说明预览基线已失效，必须拒绝把旧影响确认应用到新策略图。
                raise ApiError("policy_template.impact_conflict")
            if preview_plan.impact_hash != supplied_impact_hash:
                # 影响哈希绑定模板、参数和依赖事实，任何差异都要求调用方重新确认预览。
                raise ApiError("policy_template.impact_conflict")

            applied_as_of = _utcnow()
            if applied_as_of < preview_as_of:
                # 未来时间不能借系统时钟替换成当前时间后通过另一份影响计划。
                raise ApiError("policy_template.impact_conflict")
            current_plan = _build_plan(
                config,
                connection,
                locked_context,
                template_row,
                payload.bindings,
                preview_nonce=preview_nonce,
                preview_as_of=applied_as_of,
            )
            _require_applicable_plan(current_plan, drift=True)
            # 第二观察时点必须与旧预览在授权、依赖和前后候选上完全一致，拒绝时间竞态。
            if (
                current_plan.applicability_fingerprint
                != preview_plan.applicability_fingerprint
                or current_plan.before_candidate.content_hash
                != preview_plan.before_candidate.content_hash
                or current_plan.after_candidate is None
                or current_plan.after_candidate.content_hash
                != preview_plan.after_candidate.content_hash
            ):
                # 写锁内重算结果必须与预览逐字节一致，避免依赖漂移产生未经确认的发布。
                raise ApiError("policy_template.impact_conflict")

            ids = preview_plan.generated_ids
            draft = preview_plan.draft
            # 兼容计划却缺编译草稿表示内部不变量损坏，不能继续写半个 Policy 图。
            if draft is None:
                raise ApiError("policy_template.unavailable")
            applied_text = _to_wire_time(applied_as_of)
            policy_name = _generated_policy_name(str(template_row["name"]), ids["application_id"])
            policy_description = f"由内置策略模板 {code} 自动生成；仅能通过模板回滚流程撤销。"
            connection.execute(
                """
                INSERT INTO policies(
                  policy_id,tenant_id,name,description,status,priority,version,dsl_version,
                  created_by,updated_by,created_at,updated_at
                ) VALUES(?,?,?,?,'active',?,1,?,?,?,?,?)
                """,
                (
                    ids["policy_id"],
                    context.user.tenant_id,
                    policy_name,
                    policy_description,
                    int(template_row["priority"]),
                    draft.compiled.dsl_version,
                    context.user.user_id,
                    context.user.user_id,
                    applied_text,
                    applied_text,
                ),
            )
            _insert_rule(
                connection,
                context.user.tenant_id,
                ids["policy_id"],
                ids["policy_rule_id"],
                draft.compiled.canonical_expression,
                draft,
                applied_text,
            )
            _insert_action(
                connection,
                context.user.tenant_id,
                ids["policy_id"],
                ids["policy_action_id"],
                draft,
                applied_text,
            )
            policy_row = _load_policy_row(
                connection,
                context.user.tenant_id,
                ids["policy_id"],
            )
            # Policy/Rule/Action 写完后必须在同事务回读完整图，触发器或故障不能留下半对象。
            if policy_row is None:
                raise ApiError("policy_template.unavailable")
            policy_audit = record_audit_log(
                connection,
                locked_context,
                action="policy.created",
                object_type="policy",
                object_id=ids["policy_id"],
                object_name=policy_name,
                before_summary={},
                after_summary=_policy_audit_summary(policy_row),
                request_meta=audit_request_meta,
                request_id=normalized_request_id,
                trace_id=normalized_trace_id,
                audit_log_id=new_audit_log_id(),
                created_at=applied_as_of,
            )
            # 审计是应用事务的组成事实，不允许 Policy 成功但创建审计缺失。
            if policy_audit is None:
                raise ApiError("policy_template.unavailable")
            policy_event = _insert_created_event(
                connection,
                locked_context,
                policy_row,
                request_id=normalized_request_id,
                trace_id=normalized_trace_id,
                now=applied_text,
            )

            committed_candidate = _compile_active_candidate(
                connection,
                context.user.tenant_id,
            )
            # 实际编译候选必须逐项等于预览结果，防止 ID 排序或并发配置制造语义偏差。
            if (
                committed_candidate.content_hash != current_plan.after_candidate.content_hash
                or committed_candidate.items != current_plan.after_candidate.items
            ):
                # 落库后的编译图若偏离锁内计划，说明事务事实不可信，必须整体回滚。
                raise ApiError("policy_template.impact_conflict")
            _validate_active_template_invariants(
                connection,
                context.user.tenant_id,
                committed_candidate,
            )
            artifact, snapshot_id, snapshot_created_at = _create_published_snapshot(
                connection,
                locked_context,
                operation="publish",
                reason=reason,
                request_id=normalized_request_id,
                trace_id=normalized_trace_id,
                fingerprint=request_fingerprint,
                previous_row=current_plan.current_row,
                rollback_source_row=None,
                items=committed_candidate.items,
                created_at=applied_as_of,
            )
            # Snapshot helper 返回的时点和内容必须与本次原子计划一致，禁止封印另一份候选。
            if (
                snapshot_created_at != applied_as_of
                or artifact.content_hash != committed_candidate.content_hash
            ):
                # Snapshot 身份、时间和内容必须来自同一原子计划，禁止封印另一份候选制品。
                raise ApiError("policy_template.unavailable")
            snapshot_event = _insert_snapshot_event(
                connection,
                locked_context,
                artifact,
                request_id=normalized_request_id,
                trace_id=normalized_trace_id,
                created_at=applied_as_of,
            )
            _advance_head(connection, locked_context, artifact, updated_at=applied_as_of)
            snapshot_audit = record_audit_log(
                connection,
                locked_context,
                action="policy_snapshot.published",
                object_type="policy_snapshot",
                object_id=snapshot_id,
                object_name=None,
                before_summary=_snapshot_row_audit_summary(current_plan.current_row),
                after_summary=_artifact_audit_summary(artifact, reason),
                request_meta=audit_request_meta,
                request_id=normalized_request_id,
                trace_id=normalized_trace_id,
                audit_log_id=new_audit_log_id(),
                created_at=applied_as_of,
            )
            # Snapshot 推进和对应审计必须同生共死，否则失败请求无法按 request_id 追踪。
            if snapshot_audit is None:
                raise ApiError("policy_template.unavailable")

            template_audit_id = new_audit_log_id()
            template_event_id = f"ptevt_{uuid4().hex}"
            _insert_application(
                connection,
                locked_context,
                template_row=template_row,
                plan=current_plan,
                preview_as_of=preview_as_of,
                applied_as_of=applied_as_of,
                request_id=normalized_request_id,
                trace_id=normalized_trace_id,
                request_fingerprint=request_fingerprint,
                supplied_impact_hash=supplied_impact_hash,
                previous_row=current_plan.current_row,
                result_snapshot_id=snapshot_id,
                result_snapshot_version=artifact.snapshot_version,
                policy_audit_log_id=policy_audit.audit_log_id,
                policy_event_id=policy_event.policy_event_id,
                snapshot_audit_log_id=snapshot_audit.audit_log_id,
                snapshot_event_id=snapshot_event.event_id,
                apply_audit_log_id=template_audit_id,
                template_event_id=template_event_id,
            )
            application_row = _load_application_by_id(
                connection,
                context.user.tenant_id,
                ids["application_id"],
            )
            # application 是串联六类事实的不可变主记录，缺失时延迟外键不能继续提交。
            if application_row is None:
                raise ApiError("policy_template.unavailable")
            generated_item = next(
                item for item in committed_candidate.items if item.policy_id == ids["policy_id"]
            )
            _insert_application_children(
                connection,
                application_row,
                current_plan,
                generated_item_hash=_policy_item_graph_hash(generated_item),
                previous_content_hash=str(current_plan.current_row["content_hash"]),
                result_content_hash=artifact.content_hash,
                created_at=applied_text,
            )
            application_summary = _application_audit_summary(application_row)
            template_audit = record_audit_log(
                connection,
                locked_context,
                action="policy_template.applied",
                object_type="policy_template_application",
                object_id=ids["application_id"],
                object_name=None,
                before_summary={},
                after_summary=application_summary,
                request_meta=audit_request_meta,
                request_id=normalized_request_id,
                trace_id=normalized_trace_id,
                audit_log_id=template_audit_id,
                created_at=applied_as_of,
            )
            # 模板级审计负责解释受限复合命令，不能只依赖底层 Policy/Snapshot 审计猜测。
            if template_audit is None:
                raise ApiError("policy_template.unavailable")
            template_event = _insert_template_event(
                connection,
                application_row,
                template_event_id=template_event_id,
                audit_log_id=template_audit_id,
                created_at=applied_as_of,
            )

            result = _load_application_response(
                connection,
                application_row,
                idempotent_replay=False,
            )
            dispatch_facts = _load_apply_dispatch_facts(connection, application_row)
            # 提交前从数据库重建六类派发事实，证明内存对象与持久 owner/ID 完全一致。
            if dispatch_facts != _ApplyDispatchFacts(
                policy_audit=policy_audit,
                snapshot_audit=snapshot_audit,
                template_audit=template_audit,
                policy_event=policy_event,
                snapshot_event=snapshot_event,
                template_event=template_event,
            ):
                # 提交前必须交叉证明六类审计与事件事实，缺失任一项都不能留下部分成功。
                raise ApiError("policy_template.unavailable")
            commit_audited_transaction(connection)
    # 任何领域失败先回滚写事务；审计组件错误再折叠为模板域的固定 503。
    except ApiError as exc:
        _safe_rollback(connection)
        if exc.code == "audit_log.record_unavailable":
            # 审计不可用属于模板事务整体不可用，不能向外暴露底层审计域实现细节。
            raise ApiError("policy_template.unavailable") from exc
        raise
    # 未分类存储、JSON 或 Snapshot 校验异常同样整体回滚且不透传原异常正文。
    except (
        sqlite3.Error,
        TypeError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        PolicySnapshotValidationError,
    ) as exc:
        _safe_rollback(connection)
        # 未分类存储或制品错误统一失败关闭，避免 SQL、JSON 或敏感配置进入响应。
        raise ApiError("policy_template.unavailable") from exc
    finally:
        connection.close()

    # 成功路径必须同时产出响应和可补派事实，防止提交后返回伪成功或漏通知。
    if result is None or dispatch_facts is None:
        raise ApiError("policy_template.unavailable")
    _dispatch_apply_facts(config, context, dispatch_facts)
    return result


def _try_rollback_policy_template_application(
    config: AppConfig,
    connection: sqlite3.Connection,
    context: AuthContext,
    *,
    target_snapshot_id: str,
    reason: str,
    expected_head: int,
    request_fingerprint: str,
    audit_request_meta: AuditRequestMeta,
    request_id: str,
    trace_id: str,
) -> PolicySnapshotMutationResponse | None:
    """在既有 Snapshot 写事务中尝试执行模板感知补偿撤销。

    调用方已经持有 ``BEGIN IMMEDIATE``，本函数只在 request 命中既有 rollback 或 target
    命中 applied application 时接管事务；否则返回 ``None`` 继续 PRD-045 普通回滚。
    模板分支会自行提交或结束幂等读事务并补派事实，但连接的最终关闭仍由调用方负责。
    """

    tenant_id = context.user.tenant_id
    normalized_trace_id = _normalize_correlation_id(trace_id)
    result: PolicySnapshotMutationResponse | None = None
    dispatch_facts: _RollbackDispatchFacts | None = None
    try:
        replay_application = _load_application_by_rollback_request(
            connection,
            tenant_id,
            request_id,
        )
        if replay_application is not None:
            # 路由鉴权后的写锁内复核阻止权限被撤销的 Session 继续读取补偿结果和补派事件。
            ResourceWriteTransaction(connection, config, context, "policy.publish").verify()
            # 回滚回放仍绑定原 actor 与完整意图，不能把稳定 request_id 当成结果读取凭证。
            if (
                replay_application["rollback_actor_user_id"] != context.user.user_id
                or replay_application["rollback_request_fingerprint"]
                != request_fingerprint
            ):
                # 回滚幂等键同样绑定原操作者与命令指纹，防止他人复用历史成功响应。
                raise ApiError("policy_template.request_conflict")
            rollback_row = _load_snapshot_row(
                connection,
                tenant_id,
                str(replay_application["rollback_snapshot_id"]),
            )
            # application 指向的补偿快照必须仍可按 tenant 回读，否则持久事实链已经损坏。
            if rollback_row is None:
                raise ApiError("policy_template.unavailable")
            dispatch_facts = _load_rollback_dispatch_facts(
                connection,
                replay_application,
            )
            result = PolicySnapshotMutationResponse(
                policy_snapshot=_snapshot_info(rollback_row),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=normalized_trace_id,
            )
            # 回放只读取持久事实；任何外部派发前必须显式释放 SQLite 读事务。
            connection.rollback()
        else:
            application = _load_applied_application_by_previous_snapshot(
                connection,
                tenant_id,
                target_snapshot_id,
            )
            # 目标不属于模板 application 时交还 PRD-045 普通回滚，不能扩大模板分支范围。
            if application is None:
                return None

            locked_context = ResourceWriteTransaction(
                connection, config, context, "policy.publish"
            ).verify()
            # 一个 rollback request_id 不能复用任何既有 Snapshot 事实；否则延迟 FK 可能把
            # application 指向另一条命令，必须在首次写入前返回稳定冲突。
            occupied_request = connection.execute(
                "SELECT 1 FROM policy_snapshots WHERE tenant_id=? AND request_id=? LIMIT 1",
                (tenant_id, request_id),
            ).fetchone()
            if occupied_request is not None:
                # 普通 Snapshot 已占用的 request_id 不能转用于模板回滚，避免两类命令串线。
                raise ApiError("policy_template.request_conflict")

            current_row = _load_current_row(connection, tenant_id)
            _require_head_version(current_row, expected_head)
            # 模板回滚必须基于真实 current head，不能从空链生成补偿性 publish。
            if current_row is None:
                raise ApiError("policy_template.rollback_conflict")
            # current 链必须包含原 apply 结果，避免从不相关发布历史中删除同名 Policy。
            if not _snapshot_chain_contains(
                connection,
                tenant_id,
                current_row,
                str(application["result_snapshot_id"]),
            ):
                # 当前 head 必须仍包含原应用快照，否则补偿会覆盖不相关的历史分支。
                raise ApiError("policy_template.rollback_conflict")

            # 原 apply 的六类事实和两条 application item 必须仍可交叉证明，不能仅凭一行
            # application 进入受控触发器例外路径。
            _load_apply_dispatch_facts(connection, application)
            current_artifact = _load_verified_artifact(connection, current_row)
            active_candidate = _compile_active_candidate(connection, tenant_id)
            # 未发布 candidate 与线上 artifact 必须一致，回滚不能覆盖控制面尚未发布的修改。
            if (
                active_candidate.content_hash != current_artifact.content_hash
                or active_candidate.items != current_artifact.items
            ):
                # 未发布候选与线上制品不一致表示存在待发布修改，补偿不能替其决定结果。
                raise ApiError("policy_template.rollback_conflict")
            try:
                _validate_active_template_invariants(
                    connection,
                    tenant_id,
                    active_candidate,
                )
            # 活跃模板不变量失败统一映射为回滚冲突，避免泄漏具体 Policy 或排序身份。
            except ApiError as exc:
                if exc.code == "policy_snapshot.template_invariant_conflict":
                    raise ApiError("policy_template.rollback_conflict") from exc
                raise

            generated_policy_id = str(application["generated_policy_id"])
            generated_items = [
                item
                for item in current_artifact.items
                if item.policy_id == generated_policy_id
            ]
            generated_candidates = [
                item
                for item in active_candidate.items
                if item.policy_id == generated_policy_id
            ]
            stored_items = connection.execute(
                """
                SELECT after_version,after_hash
                FROM policy_template_application_items
                WHERE tenant_id=? AND application_id=?
                  AND resource_type='policy' AND operation='create'
                LIMIT 2
                """,
                (tenant_id, application["application_id"]),
            ).fetchall()
            policy_before = _load_policy_row(
                connection,
                tenant_id,
                generated_policy_id,
            )
            # 生成 Policy 必须在 current、candidate 和 application 摘要中各精确出现一次且图一致。
            if (
                len(generated_items) != 1
                or len(generated_candidates) != 1
                or len(stored_items) != 1
                or policy_before is None
                or policy_before["status"] != "active"
                or int(policy_before["version"]) != 1
                or int(stored_items[0]["after_version"]) != 1
                or _policy_item_graph_hash(generated_items[0])
                != stored_items[0]["after_hash"]
                or _policy_item_graph_hash(generated_candidates[0])
                != stored_items[0]["after_hash"]
            ):
                # 生成项必须由 current、candidate 与应用摘要共同唯一证明，证据歧义时拒绝撤销。
                raise ApiError("policy_template.rollback_conflict")

            rolled_back_at = _utcnow()
            rolled_back_text = _to_wire_time(rolled_back_at)
            rollback_snapshot_version = int(current_row["snapshot_version"]) + 1
            # 单调版本达到上限时失败关闭，不能回绕或覆盖历史快照身份。
            if rollback_snapshot_version > MAX_SNAPSHOT_VERSION:
                raise ApiError("policy_template.unavailable")
            rollback_snapshot_id = f"psnap_{uuid4().hex}"
            rollback_snapshot_event_id = f"psevt_{uuid4().hex}"
            rollback_policy_audit_id = new_audit_log_id()
            rollback_snapshot_audit_id = new_audit_log_id()
            rollback_template_audit_id = new_audit_log_id()

            application_cursor = connection.execute(
                """
                UPDATE policy_template_applications
                SET status='rolled_back', rollback_snapshot_id=?,
                    rollback_snapshot_version=?, rolled_back_policy_version=2,
                    rollback_actor_user_id=?, rollback_request_id=?, rollback_trace_id=?,
                    rollback_request_fingerprint=?, rollback_policy_audit_log_id=?,
                    rollback_snapshot_audit_log_id=?, rollback_apply_audit_log_id=?,
                    rollback_snapshot_event_id=?, rolled_back_at=?, rolled_back_by=?
                WHERE tenant_id=? AND application_id=? AND status='applied'
                """,
                (
                    rollback_snapshot_id,
                    rollback_snapshot_version,
                    locked_context.user.user_id,
                    request_id,
                    normalized_trace_id,
                    request_fingerprint,
                    rollback_policy_audit_id,
                    rollback_snapshot_audit_id,
                    rollback_template_audit_id,
                    rollback_snapshot_event_id,
                    rolled_back_text,
                    locked_context.user.user_id,
                    tenant_id,
                    application["application_id"],
                ),
            )
            # 状态 CAS 只允许 applied 到 rolled_back 一次，竞争失败不得继续停用 Policy。
            if application_cursor.rowcount != 1:
                raise ApiError("policy_template.rollback_conflict")

            policy_cursor = connection.execute(
                """
                UPDATE policies
                SET status='disabled',version=2,updated_by=?,updated_at=?
                WHERE tenant_id=? AND policy_id=? AND status='active' AND version=1
                """,
                (
                    locked_context.user.user_id,
                    rolled_back_text,
                    tenant_id,
                    generated_policy_id,
                ),
            )
            # 受控例外只接受 active/v1 到 disabled/v2，任何预先漂移都整体冲突。
            if policy_cursor.rowcount != 1:
                raise ApiError("policy_template.rollback_conflict")
            policy_after = _load_policy_row(connection, tenant_id, generated_policy_id)
            # 停用后必须回读完整 Policy 图，为审计 before/after 提供同事务证据。
            if policy_after is None:
                raise ApiError("policy_template.unavailable")
            policy_audit = record_audit_log(
                connection,
                locked_context,
                action="policy.updated",
                object_type="policy",
                object_id=generated_policy_id,
                object_name=str(policy_after["name"]),
                before_summary=_policy_audit_summary(policy_before),
                after_summary=_policy_audit_summary(policy_after),
                request_meta=audit_request_meta,
                request_id=request_id,
                trace_id=normalized_trace_id,
                audit_log_id=rollback_policy_audit_id,
                created_at=rolled_back_at,
            )
            # Policy 停用审计属于补偿事务核心事实，缺失时不能推进补偿快照。
            if policy_audit is None:
                raise ApiError("policy_template.unavailable")

            expected_items = _remove_and_reindex_policy_item(
                current_artifact.items,
                generated_policy_id,
            )
            expected_content_hash = compute_policy_snapshot_content_hash(
                tenant_id,
                expected_items,
            )
            compensated_candidate = _compile_active_candidate(connection, tenant_id)
            try:
                _validate_active_template_invariants(
                    connection,
                    tenant_id,
                    compensated_candidate,
                )
            # 删除目标项后的其余活跃模板仍须满足不变量，否则补偿结果不能发布。
            except ApiError as exc:
                if exc.code == "policy_snapshot.template_invariant_conflict":
                    raise ApiError("policy_template.rollback_conflict") from exc
                raise
            # 重编 candidate 必须等于 current artifact 精确减去生成项，确保保留后续发布内容。
            if (
                compensated_candidate.items != expected_items
                or compensated_candidate.content_hash != expected_content_hash
            ):
                # 补偿候选必须只删除目标生成项并保留其余顺序，任何额外变化都视为冲突。
                raise ApiError("policy_template.rollback_conflict")

            artifact, created_snapshot_id, snapshot_created_at = (
                _create_published_snapshot(
                    connection,
                    locked_context,
                    operation="publish",
                    reason=reason,
                    request_id=request_id,
                    trace_id=normalized_trace_id,
                    fingerprint=request_fingerprint,
                    previous_row=current_row,
                    rollback_source_row=None,
                    items=expected_items,
                    created_at=rolled_back_at,
                    snapshot_id=rollback_snapshot_id,
                )
            )
            # 预生成 Snapshot ID、版本、时间和内容均是 application 状态机的延迟外键事实。
            if (
                created_snapshot_id != rollback_snapshot_id
                or artifact.snapshot_version != rollback_snapshot_version
                or snapshot_created_at != rolled_back_at
                or artifact.content_hash != expected_content_hash
            ):
                # 补偿快照的预留身份、版本、时间和哈希必须全部一致，否则事务事实不完整。
                raise ApiError("policy_template.unavailable")
            snapshot_event = _insert_snapshot_event(
                connection,
                locked_context,
                artifact,
                request_id=request_id,
                trace_id=normalized_trace_id,
                created_at=rolled_back_at,
                event_id=rollback_snapshot_event_id,
            )
            _advance_head(
                connection,
                locked_context,
                artifact,
                updated_at=rolled_back_at,
            )
            snapshot_audit = record_audit_log(
                connection,
                locked_context,
                action="policy_snapshot.published",
                object_type="policy_snapshot",
                object_id=rollback_snapshot_id,
                object_name=None,
                before_summary=_snapshot_row_audit_summary(current_row),
                after_summary=_artifact_audit_summary(artifact, reason),
                request_meta=audit_request_meta,
                request_id=request_id,
                trace_id=normalized_trace_id,
                audit_log_id=rollback_snapshot_audit_id,
                created_at=rolled_back_at,
            )
            # 补偿性 publish 必须有对应 Snapshot 审计，失败时整笔事务回到 applied/v1。
            if snapshot_audit is None:
                raise ApiError("policy_template.unavailable")

            rolled_back_application = _load_application_by_id(
                connection,
                tenant_id,
                str(application["application_id"]),
            )
            # 状态转换后回读主记录，禁止用转换前内存对象构造 rolled_back 审计。
            if rolled_back_application is None:
                raise ApiError("policy_template.unavailable")
            template_audit = record_audit_log(
                connection,
                locked_context,
                action="policy_template.rolled_back",
                object_type="policy_template_application",
                object_id=str(application["application_id"]),
                object_name=None,
                before_summary=_application_audit_summary(
                    rolled_back_application,
                    applied_view=True,
                ),
                after_summary=_application_audit_summary(rolled_back_application),
                request_meta=audit_request_meta,
                request_id=request_id,
                trace_id=normalized_trace_id,
                audit_log_id=rollback_template_audit_id,
                created_at=rolled_back_at,
            )
            # 模板撤销审计是解释补偿命令的权威事实，不能由底层资源事件替代。
            if template_audit is None:
                raise ApiError("policy_template.unavailable")

            dispatch_facts = _load_rollback_dispatch_facts(
                connection,
                rolled_back_application,
            )
            expected_facts = _RollbackDispatchFacts(
                policy_audit=policy_audit,
                snapshot_audit=snapshot_audit,
                template_audit=template_audit,
                snapshot_event=snapshot_event,
            )
            # 提交前交叉核对四类事实的 tenant、request 与稳定 ID，损坏时拒绝提交。
            if dispatch_facts != expected_facts:
                raise ApiError("policy_template.unavailable")
            ResourceWriteTransaction(connection, config, context, "policy.publish").commit(
                commit_audited_transaction
            )
            rollback_row = _load_snapshot_row(
                connection,
                tenant_id,
                rollback_snapshot_id,
            )
            # 提交后响应只能从已持久快照恢复，禁止返回尚未封印的内存 artifact。
            if rollback_row is None:
                raise ApiError("policy_template.unavailable")
            result = PolicySnapshotMutationResponse(
                policy_snapshot=_snapshot_info(rollback_row),
                idempotent_replay=False,
                request_id=request_id,
                trace_id=normalized_trace_id,
            )
    # 调用方统一持有事务回滚责任；这里只转换审计组件错误并保留其他领域错误码。
    except ApiError as exc:
        if exc.code == "audit_log.record_unavailable":
            raise ApiError("policy_template.unavailable") from exc
        raise
    # 未分类持久化或制品校验异常统一失败关闭，原始 SQL/JSON 内容不得进入响应。
    except (
        sqlite3.Error,
        TypeError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        PolicySnapshotValidationError,
    ) as exc:
        # 回滚路径的存储与制品异常统一收敛，禁止把数据库结构或策略正文泄漏给调用方。
        raise ApiError("policy_template.unavailable") from exc

    # 模板分支一旦接管就必须同时产生结果和补派事实，不允许静默落回普通回滚。
    if result is None or dispatch_facts is None:
        raise ApiError("policy_template.unavailable")
    _dispatch_rollback_facts(config, context, dispatch_facts)
    return result


def _load_template_catalog(connection: sqlite3.Connection, tenant_id: str) -> list[sqlite3.Row]:
    """按冻结顺序加载当前 tenant 五条模板，并逐字段核对迁移身份。"""

    rows = connection.execute(
        """
        SELECT * FROM policy_templates
        WHERE tenant_id=?
        ORDER BY CASE template_code
          WHEN 'low_cost_mode' THEN 0 WHEN 'high_security_mode' THEN 1
          WHEN 'domestic_models_only' THEN 2 WHEN 'budget_burst_guard' THEN 3
          WHEN 'customer_service_redaction' THEN 4 ELSE 99 END
        """,
        (tenant_id,),
    ).fetchall()
    # 每租户必须精确物化五模板，缺失或额外行都表示迁移身份不可信。
    if len(rows) != len(_TEMPLATE_ORDER):
        raise ApiError("policy_template.unavailable")
    for expected_code, row in zip(_TEMPLATE_ORDER, rows, strict=True):
        _validate_template_row(row, tenant_id, expected_code)
    return list(rows)


def _validate_template_row(row: sqlite3.Row, tenant_id: str, expected_code: str) -> None:
    """证明数据库蓝图与代码内冻结目录完全一致，篡改时失败关闭。"""

    expected = _TEMPLATE_CATALOG[expected_code]
    try:
        action = json.loads(row["action_json"])
        schema = json.loads(row["binding_schema_json"])
        binding_names = tuple(item["name"] for item in schema)
    # 蓝图 JSON 损坏统一归为目录不可用，不向管理员回显迁移原文或解析位置。
    except (TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ApiError("policy_template.unavailable") from exc
    # tenant、版本、动作和绑定 schema 必须逐字段匹配代码冻结目录，禁止数据库脚本注入。
    if (
        row["tenant_id"] != tenant_id
        or row["template_code"] != expected_code
        or row["policy_template_id"] != expected["id"]
        or row["status"] != "active"
        or type(row["version"]) is not int
        or row["version"] != 1
        or row["priority"] != expected["priority"]
        or row["dsl_version"] != "policy-dsl-v1"
        or row["definition_hash"] != expected["definition_hash"]
        or action != expected["action"]
        or binding_names != expected["bindings"]
        or any(
            set(item) != {"name", "type", "required"} or item["required"] is not True
            for item in schema
        )
    ):
        # 冻结目录任一字段不一致都可能改变模板语义，必须整表失败关闭而非部分展示。
        raise ApiError("policy_template.unavailable")


def _load_binding_options(
    connection: sqlite3.Connection,
    tenant_id: str,
) -> PolicyTemplateBindingOptions:
    """加载当前 tenant 的 active Project 与高安全 Alias 最小安全投影。"""

    projects = connection.execute(
        """
        SELECT project_id,name,status FROM projects
        WHERE tenant_id=? AND status='active'
        ORDER BY name,project_id LIMIT 201
        """,
        (tenant_id,),
    ).fetchall()
    aliases = connection.execute(
        """
        SELECT a.model_alias_id,a.alias,a.status,a.environment_id,e.code AS environment_code
        FROM model_aliases a
        JOIN environments e
          ON e.tenant_id=a.tenant_id AND e.environment_id=a.environment_id
        WHERE a.tenant_id=? AND a.status='active' AND e.status='active'
        ORDER BY e.code,a.alias,a.model_alias_id LIMIT 201
        """,
        (tenant_id,),
    ).fetchall()
    # 超出页面有界目录时拒绝静默截断，避免管理员选择不到真实冲突资源。
    if len(projects) > 200 or len(aliases) > 200:
        raise ApiError("policy_template.unavailable")
    return PolicyTemplateBindingOptions(
        projects=[
            PolicyTemplateProjectOption(
                project_id=str(row["project_id"]),
                name=str(row["name"]),
            )
            for row in projects
        ],
        model_aliases=[
            PolicyTemplateAliasOption(
                model_alias_id=str(row["model_alias_id"]),
                alias=str(row["alias"]),
                environment_id=str(row["environment_id"]),
                environment_code=str(row["environment_code"]),
            )
            for row in aliases
        ],
    )


def _template_info(
    row: sqlite3.Row,
    context: AuthContext,
    options: PolicyTemplateBindingOptions,
    *,
    plan: _Plan | None,
) -> PolicyTemplateInfo:
    """把一条已验证模板和可选精确计划转换为 HTTP 安全对象。"""

    action = json.loads(row["action_json"])
    schema = json.loads(row["binding_schema_json"])
    required = [str(item["name"]) for item in schema]
    code = str(row["template_code"])
    if plan is None:
        compatibility = PolicyTemplateCompatibility(
            status="unbound",
            reason_codes=["bindings_required"],
            warning_codes=[],
            can_apply=False,
            requires_permissions=list(_required_permissions(code)),
        )
    else:
        compatibility = PolicyTemplateCompatibility(
            status=plan.status,
            reason_codes=list(plan.reason_codes),
            warning_codes=list(plan.warning_codes),
            can_apply=plan.can_apply,
            requires_permissions=list(plan.required_permissions),
        )
    ready = plan is not None and plan.impact_hash is not None
    return PolicyTemplateInfo(
        policy_template_id=str(row["policy_template_id"]),
        tenant_id=str(row["tenant_id"]),
        template_code=code,  # type: ignore[arg-type]
        name=str(row["name"]),
        description=str(row["description"]),
        category=str(row["category"]),
        scenario=str(row["scenario"]),
        version=int(row["version"]),
        priority=int(row["priority"]),
        condition_blueprint=str(row["condition_expr"]),
        action_type=str(action["type"]),  # type: ignore[arg-type]
        required_bindings=required,
        binding_options=options,
        compatibility=compatibility,
        impact=plan.impact if plan is not None else None,
        preview_nonce=plan.preview_nonce if ready else None,
        preview_as_of=plan.preview_as_of if ready else None,
        impact_hash=plan.impact_hash if plan is not None else None,
        before_candidate_hash=(
            plan.before_candidate.content_hash if ready and plan is not None else None
        ),
        after_candidate_hash=(
            plan.after_candidate.content_hash
            if ready and plan is not None and plan.after_candidate is not None
            else None
        ),
        expected_head_version=(
            int(plan.current_row["snapshot_version"])
            if ready and plan is not None and plan.current_row is not None
            else None
        ),
        generated_resource_ids=plan.generated_ids if ready and plan is not None else None,
    )


def _build_plan(
    config: AppConfig,
    connection: sqlite3.Connection,
    context: AuthContext,
    template_row: sqlite3.Row,
    bindings_request: PolicyTemplateBindingsRequest,
    *,
    preview_nonce: str,
    preview_as_of: datetime,
) -> _Plan:
    """基于当前事务快照构造一个完整、确定且可复算的模板计划。"""

    code = str(template_row["template_code"])
    bindings = _normalize_bindings(code, bindings_request)
    bindings_hash = _sha256_json(bindings)
    nonce = _normalize_sha256(preview_nonce, "policy_template.payload_invalid")
    as_of = _require_utc_datetime(preview_as_of)
    project = connection.execute(
        """
        SELECT project_id,name,status,project_code
        FROM projects WHERE tenant_id=? AND project_id=?
        """,
        (context.user.tenant_id, bindings["project_id"]),
    ).fetchone()
    # Project 不存在、跨租户或已停用统一按绑定非法处理，不能暴露其他租户对象状态。
    if project is None or project["status"] != "active":
        raise ApiError("policy_template.binding_invalid")
    current_row = _load_current_row(connection, context.user.tenant_id)
    before = _compile_active_candidate(connection, context.user.tenant_id)
    required_permissions = _required_permissions(code)
    permission_ok = _compound_permission_satisfied(code, context.permission_codes)
    generated_ids = _derived_resource_ids(
        context.user.tenant_id,
        str(template_row["policy_template_id"]),
        nonce,
        bindings_hash,
    )
    reasons: list[str] = []
    warnings: list[str] = []
    if current_row is None:
        reasons.append("snapshot_baseline_required")
    if len(before.items) >= _MAX_POLICY_COUNT:
        reasons.append("policy_quota_exceeded")
    active_application = connection.execute(
        """
        SELECT application_id FROM policy_template_applications
        WHERE tenant_id=? AND project_id=? AND status='applied'
        """,
        (context.user.tenant_id, bindings["project_id"]),
    ).fetchone()
    if active_application is not None:
        reasons.append("active_template_application_exists")

    semantic: _SemanticPlan | None = None
    draft: _CompiledDraft | None = None
    after: _Candidate | None = None
    dependencies: tuple[_Dependency, ...] = ()
    try:
        semantic = _build_semantic_plan(
            config,
            connection,
            context.user.tenant_id,
            code,
            bindings,
            as_of,
            project,
        )
        warnings.extend(semantic.warning_codes)
        dependencies = semantic.dependencies
        draft = _compile_draft(
            semantic.expression,
            PolicyActionDraft(type=semantic.action_type, target_model_id=semantic.target_model_id),
        )
        after = _candidate_with_generated_policy(
            context.user.tenant_id,
            before,
            generated_ids,
            draft,
            int(template_row["priority"]),
        )
    except _NotApplicable as exc:
        reasons.append(exc.reason_code)
    except _DraftFailure as exc:
        # 国产集合可能合法但完整表达式超过 shared DSL 任一上限；不得截断或拆成近似规则。
        if code == "domestic_models_only":
            reasons.append("domestic_expression_limit_exceeded")
        else:
            # 非国产固定表达式编译失败表示代码目录损坏，不可降级为客户端可修复问题。
            raise ApiError("policy_template.unavailable") from exc

    impact: PolicyTemplateImpact | None = None
    applicability: str | None = None
    impact_hash: str | None = None
    if current_row is not None and semantic is not None and draft is not None and after is not None:
        blocking_ids, warning_ids, order = _analyze_shadowing(
            before,
            after,
            generated_ids["policy_id"],
            code,
        )
        if blocking_ids:
            reasons.append("policy_shadowing_conflict")
        if warning_ids:
            warnings.append("policy_shadowing_warning")
        reasons = sorted(set(reasons))
        warnings = sorted(set(warnings))
        applicability = _sha256_json(
            {
                "schema": "policy-template-applicability-v1",
                "tenant_id": context.user.tenant_id,
                "authorization_fingerprint": context.authorization_fingerprint,
                "template_id": template_row["policy_template_id"],
                "template_version": template_row["version"],
                "template_definition_hash": template_row["definition_hash"],
                "bindings_hash": bindings_hash,
                "head": {
                    "id": current_row["policy_snapshot_id"],
                    "version": current_row["snapshot_version"],
                    "content_hash": current_row["content_hash"],
                },
                "before_candidate_hash": before.content_hash,
                "after_candidate_hash": after.content_hash,
                "dependencies": [_dependency_wire(item) for item in dependencies],
                "reasons": reasons,
                "warnings": warnings,
                "blocking_policy_ids": blocking_ids,
                "warning_policy_ids": warning_ids,
            }
        )
        impact_hash = _sha256_json(
            {
                "schema": "policy-template-impact-v1",
                "tenant_id": context.user.tenant_id,
                "actor_user_id": context.user.user_id,
                "preview_nonce": nonce,
                "preview_as_of": _to_wire_time(as_of),
                "generated_ids": generated_ids,
                "applicability_fingerprint": applicability,
                "evaluation_order": [item.model_dump(mode="json") for item in order],
            }
        )
        impact = PolicyTemplateImpact(
            project_id=bindings["project_id"],
            generated_resource_types=[
                "policy",
                "policy_rule",
                "policy_action",
                "policy_snapshot",
            ],
            evaluation_order=order,
            before_policy_count=before.policy_count,
            after_policy_count=after.policy_count,
            derived_target_model_id=semantic.derived_target_model_id,
            derived_target_model_name=semantic.derived_target_model_name,
            allowed_model_alias_names=list(semantic.allowed_model_alias_names),
            frozen_environment_codes=list(semantic.frozen_environment_codes),
            expected_head_version=int(current_row["snapshot_version"]),
            rollback_target_snapshot_id=str(current_row["policy_snapshot_id"]),
            shadowing_policy_ids=sorted(set(blocking_ids) | set(warning_ids)),
        )
    reasons = sorted(set(reasons))
    warnings = sorted(set(warnings))
    status: Literal["ready", "ready_with_warnings", "incompatible"]
    if reasons:
        status = "incompatible"
        # 不可应用计划不能交付可被误认为 CAS 凭证的 nonce/hash/impact。
        impact_hash = None
    elif warnings:
        status = "ready_with_warnings"
    else:
        status = "ready"
    return _Plan(
        template_row=template_row,
        bindings=bindings,
        bindings_hash=bindings_hash,
        status=status,
        reason_codes=tuple(reasons),
        warning_codes=tuple(warnings),
        can_apply=status in {"ready", "ready_with_warnings"} and permission_ok,
        required_permissions=required_permissions,
        preview_nonce=nonce,
        preview_as_of=as_of,
        current_row=current_row,
        before_candidate=before,
        draft=draft,
        after_candidate=after,
        dependencies=dependencies,
        applicability_fingerprint=applicability,
        impact_hash=impact_hash,
        generated_ids=generated_ids,
        impact=impact,
    )


def _required_permissions(code: str) -> tuple[str, ...]:
    """返回页面展示的模板分类复合权限；apply 自身权限由独立守卫表达。"""

    # 三类安全模板都生成 Policy 图，因此额外要求原始 Policy 写职责。
    if code in {"high_security_mode", "domestic_models_only", "customer_service_redaction"}:
        return ("policy.write",)
    # 预算模板读取并冻结 Budget 事实，只能由预算管理员提交。
    if code == "budget_burst_guard":
        return ("billing.budget.write",)
    # 低成本是预算或路由两种职责均可执行的受限 any-of 命令。
    if code == "low_cost_mode":
        # 两项是 any-of 替代能力，服务端不能把列表误解释成同时必需。
        return ("billing.budget.write", "routing.write")
    raise ApiError("policy_template.unavailable")


def _compound_permission_satisfied(code: str, permission_codes: tuple[str, ...]) -> bool:
    """按模板类别检查实时数据库权限集合，不按角色名或前端可见性补权。"""

    granted = frozenset(permission_codes)
    required = _required_permissions(code)
    # 只有低成本使用 any-of，其他类别必须满足完整权限子集。
    if code == "low_cost_mode":
        return bool(granted.intersection(required))
    return set(required).issubset(granted)


def _normalize_bindings(
    code: str,
    payload: PolicyTemplateBindingsRequest,
) -> dict[str, str]:
    """按模板 exact-set 规范化联合 DTO，拒绝不相关 Alias 或阈值字段。"""

    expected = set(_TEMPLATE_CATALOG[code]["bindings"])
    provided: dict[str, object] = {"project_id": payload.project_id}
    # 联合 DTO 的可选字段只有实际出现时才进入 exact-set，禁止空字段绕过类别 schema。
    if payload.model_alias_id is not None:
        provided["model_alias_id"] = payload.model_alias_id
    if payload.threshold_cny is not None:
        provided["threshold_cny"] = payload.threshold_cny
    # 客户端夹带其他模板字段会改变哈希解释，必须整组拒绝而不是静默丢弃。
    if set(provided) != expected:
        raise ApiError("policy_template.binding_invalid")
    normalized = {
        key: _normalize_identifier(value)
        for key, value in provided.items()
        if key != "threshold_cny"
    }
    if "threshold_cny" in provided:
        normalized["threshold_cny"] = _normalize_threshold(provided["threshold_cny"])
    return dict(sorted(normalized.items()))


def _normalize_identifier(value: object) -> str:
    """规范资源 ID，拒绝空白、控制字符、Unicode 非规范形态和超长输入。"""

    # 资源身份必须是有界字符串，bool、数字和超长值都不能进入 SQL owner 查询。
    if type(value) is not str or not 1 <= len(value) <= _MAX_IDENTIFIER_LENGTH:
        raise ApiError("policy_template.binding_invalid")
    normalized = unicodedata.normalize("NFC", value)
    # 非规范 Unicode、首尾空白和控制字符会制造视觉相同但哈希不同的绑定身份。
    if normalized != value or value != value.strip() or _contains_control(value):
        raise ApiError("policy_template.binding_invalid")
    return value


def _normalize_threshold(value: object) -> str:
    """把 CNY 阈值规范为普通十进制定点文本，拒绝 bool、指数、NaN 和越界精度。"""

    # 阈值只接受普通十进制定点文本，拒绝浮点、指数和前导零歧义。
    if type(value) is not str or not re.fullmatch(
        r"(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,6})?",
        value,
    ):
        # 非规范阈值会改变预算比较精度，禁止隐式转换或截断后进入策略表达式。
        raise ApiError("policy_template.binding_invalid")
    try:
        amount = Decimal(value)
    # Decimal 解析异常不回显原始阈值，统一为绑定非法。
    except InvalidOperation:
        raise ApiError("policy_template.binding_invalid") from None
    # 金额范围与数据库 NUMERIC 及 Console 输入上限保持一致，越界不做截断。
    if amount < Decimal("0.000001") or amount > Decimal("999999999999.999999"):
        raise ApiError("policy_template.binding_invalid")
    canonical = format(amount.normalize(), "f")
    return canonical.rstrip("0").rstrip(".") if "." in canonical else canonical


def _derived_resource_ids(
    tenant_id: str,
    template_id: str,
    nonce: str,
    bindings_hash: str,
) -> dict[str, str]:
    """以域分离 SHA-256 生成本次预览固定资源 ID，并绑定租户、模板与规范化绑定。"""

    seed = {
        "tenant_id": tenant_id,
        "template_id": template_id,
        "preview_nonce": nonce,
        "bindings_hash": bindings_hash,
    }
    return {
        "application_id": _derived_id("ptapp", "application", seed),
        "policy_id": _derived_id("pol", "policy", seed),
        "policy_rule_id": _derived_id("prule", "rule", seed),
        "policy_action_id": _derived_id("pact", "action", seed),
    }


def _derived_id(prefix: str, resource_type: str, seed: dict[str, str]) -> str:
    """生成兼容既有 wire 的 32 位小写十六进制 ID。"""

    digest = hashlib.sha256(
        b"metahub/policy-template/derived-id/v1\x00"
        + resource_type.encode("ascii")
        + b"\x00"
        + _canonical_json_bytes(seed)
    ).hexdigest()
    return f"{prefix}_{digest[:32]}"


def _candidate_with_generated_policy(
    tenant_id: str,
    before: _Candidate,
    ids: dict[str, str],
    draft: _CompiledDraft,
    priority: int,
) -> _Candidate:
    """在内存中把派生 Policy 加入完整排序集，并重建所有 evaluation_order。"""

    values: list[tuple[int, str, PolicySnapshotItem | None]] = [
        (item.priority, item.policy_id, item) for item in before.items
    ]
    values.append((priority, ids["policy_id"], None))
    values.sort(key=lambda value: (value[0], value[1]))
    items: list[PolicySnapshotItem] = []
    for evaluation_order, (_priority, _policy_id, existing) in enumerate(values):
        if existing is None:
            item = build_policy_snapshot_item(
                evaluation_order=evaluation_order,
                policy_id=ids["policy_id"],
                policy_version=1,
                policy_rule_id=ids["policy_rule_id"],
                policy_action_id=ids["policy_action_id"],
                priority=priority,
                dsl_version=draft.compiled.dsl_version,
                canonical_expression=draft.compiled.canonical_expression,
                condition_hash=draft.compiled.condition_hash,
                variables=draft.compiled.variables,
                action_type=str(draft.action_type),
                target_model_id=draft.target_model_id,
            )
        else:
            item = build_policy_snapshot_item(
                evaluation_order=evaluation_order,
                policy_id=existing.policy_id,
                policy_version=existing.policy_version,
                policy_rule_id=existing.policy_rule_id,
                policy_action_id=existing.policy_action_id,
                priority=existing.priority,
                dsl_version=existing.dsl_version,
                canonical_expression=existing.canonical_expression,
                condition_hash=existing.condition_hash,
                variables=existing.variables,
                action_type=existing.action_type,
                target_model_id=existing.target_model_id,
            )
        items.append(item)
    return _Candidate(
        items=tuple(items),
        content_hash=compute_policy_snapshot_content_hash(tenant_id, tuple(items)),
    )


def _analyze_shadowing(
    before: _Candidate,
    after: _Candidate,
    generated_policy_id: str,
    code: str,
) -> tuple[list[str], list[str], list[PolicyTemplateImpactOrderItem]]:
    """保守分析完整排序中的决策动作遮蔽，安全不确定性按阻断处理。"""

    generated_index = next(
        index for index, item in enumerate(after.items) if item.policy_id == generated_policy_id
    )
    generated_projects = _required_project_values(after.items[generated_index])
    blocking: list[str] = []
    warnings: list[str] = []
    before_ids = {item.policy_id for item in before.items}
    for index, item in enumerate(after.items):
        if item.policy_id == generated_policy_id:
            continue
        item_projects = _required_project_values(item)
        if (
            generated_projects is not None
            and item_projects is not None
            and generated_projects.isdisjoint(item_projects)
        ):
            # 两条策略都能证明只命中互斥 Project 时，不存在真实请求上的短路或遮蔽。
            continue
        decisive = item.action_type in {"allow", "deny", "route_to"}
        # 非决定动作不会结束求值，但低成本 route_to 会让后置 mask 永远没有机会脱敏；
        # 该情况会把原文发送给 Provider，必须与被绕过的 deny 一样阻断，而非仅展示 warning。
        if not decisive:
            if code == "low_cost_mode" and index > generated_index:
                if item.action_type == "mask":
                    blocking.append(item.policy_id)
                elif item.action_type == "log_only":
                    # log_only 不改变正文也不结束求值；即使位于 route_to 后方也只提示顺序影响。
                    warnings.append(item.policy_id)
            continue
        # 所有模板前置 allow/route_to 都可能在模板执行前短路，必须作为阻断冲突。
        if index < generated_index and item.action_type in {"allow", "route_to"}:
            blocking.append(item.policy_id)
        # 低成本 route_to 会绕过后置 deny，因此优化模板不能覆盖安全拒绝。
        elif code == "low_cost_mode" and index > generated_index and item.action_type == "deny":
            blocking.append(item.policy_id)
        elif code in {"high_security_mode", "domestic_models_only"} and (
            index > generated_index and item.action_type == "route_to"
        ):
            blocking.append(item.policy_id)
        elif code == "customer_service_redaction" and index < generated_index:
            # 客服 mask 前的放行或路由会跳过脱敏；前置 deny 只是更严格的可见 warning。
            if item.action_type in {"allow", "route_to"}:
                blocking.append(item.policy_id)
            elif item.action_type == "deny":
                warnings.append(item.policy_id)
        elif item.action_type == "deny":
            warnings.append(item.policy_id)
    blocking_set = set(blocking)
    warning_set = set(warnings) - blocking_set
    order = [
        PolicyTemplateImpactOrderItem(
            policy_id=item.policy_id,
            priority=item.priority,
            action_type=item.action_type,  # type: ignore[arg-type]
            generated=item.policy_id not in before_ids,
            shadowing=(
                "blocking"
                if item.policy_id in blocking_set
                else "warning"
                if item.policy_id in warning_set
                else "none"
            ),
        )
        for item in after.items
    ]
    return sorted(blocking_set), sorted(warning_set), order


def _build_semantic_plan(
    config: AppConfig,
    connection: sqlite3.Connection,
    tenant_id: str,
    code: str,
    bindings: dict[str, str],
    as_of: datetime,
    project: sqlite3.Row,
) -> _SemanticPlan:
    """按冻结 template_code 派生 DSL、动作和依赖，不解释数据库中的占位符脚本。"""

    project_dependency = _Dependency(
        resource_type="project",
        resource_id=str(project["project_id"]),
        relation="bound_project",
        resource_name=None,
        version_or_hash=_resource_hash(
            "project",
            {
                "project_code": project["project_code"],
                "status": project["status"],
            },
        ),
    )
    # template_code 只允许进入五个冻结分支，禁止解释数据库中的任意脚本文本。
    if code == "low_cost_mode":
        return _plan_low_cost(
            config,
            connection,
            tenant_id,
            bindings,
            as_of,
            project_dependency,
        )
    if code == "high_security_mode":
        return _plan_high_security(
            connection,
            tenant_id,
            bindings,
            project_dependency,
        )
    if code == "domestic_models_only":
        return _plan_domestic_only(
            connection,
            tenant_id,
            bindings,
            project_dependency,
        )
    if code == "budget_burst_guard":
        return _plan_budget_guard(
            connection,
            tenant_id,
            bindings,
            project_dependency,
        )
    if code == "customer_service_redaction":
        return _plan_customer_redaction(config, bindings, project_dependency)
    # 未知模板代码表示冻结目录与分派器发生漂移，不能尝试解释数据库中的任意动作。
    raise ApiError("policy_template.unavailable")


def _plan_low_cost(
    config: AppConfig,
    connection: sqlite3.Connection,
    tenant_id: str,
    bindings: dict[str, str],
    as_of: datetime,
    project_dependency: _Dependency,
) -> _SemanticPlan:
    """选择支持对话和流式调用、价格最低且 KeyPool 可用的合格模型。"""

    rows = connection.execute(
        """
        SELECT
          m.model_id,m.provider_id,m.provider_model_id,m.upstream_model_name,
          m.display_name,m.model_alias,
          m.capabilities,m.status AS model_status,
          CAST(m.input_price_per_1k AS TEXT) AS input_price_per_1k,
          CAST(m.output_price_per_1k AS TEXT) AS output_price_per_1k,
          CAST(m.cache_price_per_1k AS TEXT) AS cache_price_per_1k,
          m.currency,m.price_schema_version,
          p.provider_code,p.provider_type,p.auth_type,p.base_url,
          p.status AS provider_status,p.health_status,p.health_generation,p.health_updated_at
        FROM models m
        JOIN providers p ON p.tenant_id=m.tenant_id AND p.provider_id=m.provider_id
        WHERE m.tenant_id=? AND m.status='active' AND p.status='active'
        ORDER BY m.model_id
        """,
        (tenant_id,),
    ).fetchall()
    candidates: list[
        tuple[
            Decimal,
            str,
            sqlite3.Row,
            sqlite3.Row | None,
            str,
            str,
            tuple[str, ...],
        ]
    ] = []
    for row in rows:
        try:
            capabilities = json.loads(row["capabilities"])
            spec = provider_adapter_spec(row["provider_type"])
        # 能力 JSON 或 adapter 分类损坏时只淘汰该候选，不能猜测为可路由上游。
        except (TypeError, json.JSONDecodeError, ProviderAdapterSpecError):
            continue
        if (spec.classification == MIGRATION_PLATFORM
            or spec.classification not in {MODEL_VENDOR_CLOUD, LOCAL_COMPATIBLE, CUSTOMER_MANAGED}):
            continue
        # 模板用于低成本对话；向量调用仍由 Gateway 的目标模型能力校验独立拦截。
        if not isinstance(capabilities, dict) or any(
            capabilities.get(name) is not True
            for name in ("chat", "stream")
        ):
            continue
        credential_rows = connection.execute(
            """
            SELECT provider_credential_id,status,rate_limited_until,
                   probe_health_status,probe_last_tested_at
            FROM provider_credentials
            WHERE tenant_id=? AND provider_id=?
            ORDER BY provider_credential_id
            """,
            (tenant_id, row["provider_id"]),
        ).fetchall()
        try:
            eligibility = evaluate_provider_key_pool(
                provider_auth_type=str(row["auth_type"]),
                managed_credentials=tuple(
                    ManagedCredentialFact(
                        credential_id=str(item["provider_credential_id"]),
                        status=str(item["status"]),
                        rate_limited_until=item["rate_limited_until"],
                        probe_health_status=str(item["probe_health_status"]),
                        probe_last_tested_at=item["probe_last_tested_at"],
                    )
                    for item in credential_rows
                ),
                # 环境 Key 回退依赖进程启动配置与默认租户；Control 不持有足够完整证明时必须 false。
                environment_fallback_available=False,
                as_of=as_of,
                stale_seconds=config.provider_health_stale_seconds,
            )
        # KeyPool 事实非法表示该 Provider 无法证明可用，不能回退环境 Key 猜测。
        except (TypeError, ValueError):
            continue
        if eligibility.state != "available":
            continue
        health_warning = _provider_health_warning(
            row,
            as_of,
            stale_seconds=config.provider_health_stale_seconds,
        )
        # 新鲜 unhealthy 是硬阻断；unknown/stale 仅作为精确预览 warning 冻结。
        if health_warning == "provider_unhealthy":
            continue
        price_row = connection.execute(
            """
             SELECT price_plan_id,
                    CAST(input_price_per_1k AS TEXT) AS input_price_per_1k,
                    CAST(output_price_per_1k AS TEXT) AS output_price_per_1k,
                    CAST(cache_price_per_1k AS TEXT) AS cache_price_per_1k,
                    currency,price_schema_version,effective_from,source
            FROM model_price_plans
            WHERE tenant_id=? AND model_id=? AND currency='CNY'
              AND julianday(effective_from)<=julianday(?)
            ORDER BY julianday(effective_from) DESC,created_at DESC,price_plan_id DESC
            LIMIT 1
            """,
            (tenant_id, row["model_id"], _to_wire_time(as_of)),
        ).fetchone()
        try:
            if price_row is not None:
                input_price = _stored_money(price_row["input_price_per_1k"])
                output_price = _stored_money(price_row["output_price_per_1k"])
                cache_price = _stored_money(price_row["cache_price_per_1k"])
                price_identity = _resource_hash(
                    "price_plan",
                    {
                        "input_price_per_1k": format(input_price, "f"),
                        "output_price_per_1k": format(output_price, "f"),
                        "cache_price_per_1k": format(cache_price, "f"),
                        "currency": price_row["currency"],
                        "schema": price_row["price_schema_version"],
                        "effective_from": price_row["effective_from"],
                        "source": price_row["source"],
                    },
                )
            else:
                # SQLite NUMERIC 列会把合法小数返回为 float；上方先 CAST 为数据库
                # 十进制文本，再用 _stored_money 的定点语法验证，避免全部误判不可用。
                # 目录价回退仍必须是带 schema 的 CNY 定点事实。
                if row["currency"] != "CNY" or not row["price_schema_version"]:
                    continue
                input_price = _stored_money(row["input_price_per_1k"])
                output_price = _stored_money(row["output_price_per_1k"])
                cache_price = _stored_money(row["cache_price_per_1k"])
                price_identity = _resource_hash(
                    "model_catalog_price",
                    {
                        "input_price_per_1k": format(input_price, "f"),
                        "output_price_per_1k": format(output_price, "f"),
                        "cache_price_per_1k": format(cache_price, "f"),
                        "currency": row["currency"],
                        "schema": row["price_schema_version"],
                    },
                )
        # 损坏或负数金额只淘汰当前候选，响应不得携带数据库价格原文。
        except ValueError:
            continue
        warnings = (health_warning,) if health_warning is not None else ()
        candidates.append(
            (
                input_price + output_price + cache_price,
                str(row["model_id"]),
                row,
                price_row,
                price_identity,
                eligibility.dependency_hash,
                warnings,
            )
        )
    # 没有对话/流式能力、可用 KeyPool 和可信价格的候选时失败关闭，不选择迁移平台兜底。
    if not candidates:
        raise _NotApplicable("eligible_low_cost_model_required")
    _price, model_id, row, price_row, price_hash, keypool_hash, warnings = min(
        candidates,
        key=lambda item: (item[0], item[1]),
    )
    dependencies = [
        project_dependency,
        _Dependency(
            "model",
            model_id,
            "route_target",
            None,
            _model_dependency_hash(row),
        ),
        _Dependency(
            "provider",
            str(row["provider_id"]),
            "route_provider",
            None,
            _provider_dependency_hash(row),
        ),
        _Dependency(
            "provider",
            str(row["provider_id"]),
            "keypool",
            None,
            keypool_hash,
        ),
        _Dependency(
            "provider",
            str(row["provider_id"]),
            "health",
            None,
            _resource_hash(
                "provider_health",
                {
                    "status": row["health_status"],
                    "generation": row["health_generation"],
                    "updated_at": row["health_updated_at"],
                },
            ),
        ),
    ]
    if price_row is not None:
        dependencies.append(
            _Dependency(
                "price_plan",
                str(price_row["price_plan_id"]),
                "selected_price",
                None,
                price_hash,
            )
        )
    return _SemanticPlan(
        expression=f"project == {_dsl_string(bindings['project_id'])}",
        action_type="route_to",
        target_model_id=model_id,
        dependencies=_sorted_dependencies(dependencies),
        warning_codes=tuple(warnings),
        derived_target_model_id=model_id,
        derived_target_model_name=str(
            row["display_name"] or row["model_alias"] or row["upstream_model_name"]
        ),
    )


def _plan_high_security(
    connection: sqlite3.Connection,
    tenant_id: str,
    bindings: dict[str, str],
    project_dependency: _Dependency,
) -> _SemanticPlan:
    """冻结所选活跃别名、环境及完整上游模型图，确保高安全规则可重复验证。"""

    alias = connection.execute(
        """
        SELECT a.*,e.code AS environment_code,e.status AS environment_status
        FROM model_aliases a
        JOIN environments e
          ON e.tenant_id=a.tenant_id AND e.environment_id=a.environment_id
        WHERE a.tenant_id=? AND a.model_alias_id=?
        """,
        (tenant_id, bindings["model_alias_id"]),
    ).fetchone()
    if (
        alias is None
        or alias["status"] != "active"
        or alias["environment_status"] != "active"
    ):
        # 仅当前租户的活跃 Alias 与环境可成为安全边界，失效对象按绑定非法处理。
        raise ApiError("policy_template.binding_invalid")
    dependencies: list[_Dependency] = [
        project_dependency,
        _Dependency(
            "environment",
            str(alias["environment_id"]),
            "approved_environment",
            None,
            _resource_hash(
                "environment",
                {"code": alias["environment_code"], "status": alias["environment_status"]},
            ),
        ),
        _Dependency(
            "model_alias",
            str(alias["model_alias_id"]),
            "approved_alias",
            str(alias["alias"]),
            _alias_dependency_hash(alias),
        ),
    ]
    dependencies.extend(
        _model_graph_dependencies(
            connection,
            tenant_id,
            alias["default_model_id"],
            relation="approved_default",
            domestic_only=False,
        )
    )
    # 回退模型也会影响实际路由结果，存在时必须与默认模型一起冻结到影响指纹。
    if alias["fallback_model_id"] is not None:
        dependencies.extend(
            _model_graph_dependencies(
                connection,
                tenant_id,
                alias["fallback_model_id"],
                relation="approved_fallback",
                domestic_only=False,
            )
        )
    expression = (
        f"project == {_dsl_string(bindings['project_id'])} and "
        f"(env != {_dsl_string(str(alias['environment_code']))} or "
        f"model != {_dsl_string(str(alias['alias']))})"
    )
    return _SemanticPlan(
        expression=expression,
        action_type="deny",
        target_model_id=None,
        dependencies=_sorted_dependencies(dependencies),
    )


def _plan_domestic_only(
    connection: sqlite3.Connection,
    tenant_id: str,
    bindings: dict[str, str],
    project_dependency: _Dependency,
) -> _SemanticPlan:
    """证明允许名称在每个 active Environment 都由纯国产 Alias 完整覆盖。"""

    environments = connection.execute(
        """
        SELECT environment_id,code,status FROM environments
        WHERE tenant_id=? AND status='active' ORDER BY code,environment_id
        """,
        (tenant_id,),
    ).fetchall()
    if not environments:
        raise _NotApplicable("active_environment_required")
    if len(environments) > _MAX_DOMESTIC_ITEMS:
        raise _NotApplicable("environment_limit_exceeded")
    aliases = connection.execute(
        """
        SELECT a.*,e.code AS environment_code,e.status AS environment_status
        FROM model_aliases a
        JOIN environments e
          ON e.tenant_id=a.tenant_id AND e.environment_id=a.environment_id
        WHERE a.tenant_id=? AND a.status='active' AND e.status='active'
        ORDER BY a.alias,e.code,a.model_alias_id
        """,
        (tenant_id,),
    ).fetchall()
    by_name: dict[str, dict[str, sqlite3.Row]] = {}
    for alias in aliases:
        by_name.setdefault(str(alias["alias"]), {})[str(alias["environment_id"])] = alias
    environment_ids = {str(row["environment_id"]) for row in environments}
    allowed_names = sorted(
        name for name, values in by_name.items() if set(values) == environment_ids
    )
    if len(allowed_names) > _MAX_DOMESTIC_ITEMS:
        raise _NotApplicable("domestic_alias_limit_exceeded")
    dependencies: list[_Dependency] = [project_dependency]
    for environment in environments:
        dependencies.append(
            _Dependency(
                "environment",
                str(environment["environment_id"]),
                "frozen_environment",
                None,
                _resource_hash(
                    "environment",
                    {"code": environment["code"], "status": environment["status"]},
                ),
            )
        )
    proven_names: list[str] = []
    for name in allowed_names:
        name_dependencies: list[_Dependency] = []
        try:
            for environment in environments:
                alias = by_name[name][str(environment["environment_id"])]
                name_dependencies.append(
                    _Dependency(
                        "model_alias",
                        str(alias["model_alias_id"]),
                        "domestic_alias",
                        name,
                        _alias_dependency_hash(alias),
                    )
                )
                name_dependencies.extend(
                    _model_graph_dependencies(
                        connection,
                        tenant_id,
                        alias["default_model_id"],
                        relation="domestic_default",
                        domestic_only=True,
                    )
                )
                # 国产证明必须覆盖回退路径，否则默认模型合规仍可能在故障时越过边界。
                if alias["fallback_model_id"] is not None:
                    name_dependencies.extend(
                        _model_graph_dependencies(
                            connection,
                            tenant_id,
                            alias["fallback_model_id"],
                            relation="domestic_fallback",
                            domestic_only=True,
                        )
                    )
        except _NotApplicable:
            # 某个名称任一环境不是纯国产时，整个名称退出允许集，不能只保留部分环境实例。
            continue
        proven_names.append(name)
        dependencies.extend(name_dependencies)
    if not proven_names:
        raise _NotApplicable("domestic_alias_coverage_required")
    environment_codes = tuple(str(row["code"]) for row in environments)
    expression = (
        f"project == {_dsl_string(bindings['project_id'])} and "
        f"(env not in {_dsl_string_list(environment_codes)} or "
        f"model not in {_dsl_string_list(tuple(proven_names))})"
    )
    return _SemanticPlan(
        expression=expression,
        action_type="deny",
        target_model_id=None,
        dependencies=_sorted_dependencies(dependencies),
        allowed_model_alias_names=tuple(proven_names),
        frozen_environment_codes=environment_codes,
    )


def _plan_budget_guard(
    connection: sqlite3.Connection,
    tenant_id: str,
    bindings: dict[str, str],
    project_dependency: _Dependency,
) -> _SemanticPlan:
    """逐 active Virtual Key 证明存在同环境可适用预算事实，缺一项即失败关闭。"""

    keys = connection.execute(
        """
        SELECT virtual_key_id,environment_id,project_id,application_id,status
        FROM virtual_keys
        WHERE tenant_id=? AND project_id=? AND status='active'
        ORDER BY virtual_key_id
        """,
        (tenant_id, bindings["project_id"]),
    ).fetchall()
    if not keys:
        raise _NotApplicable("active_virtual_key_required")
    dependencies: list[_Dependency] = [project_dependency]
    for key in keys:
        scope_pairs = [
            ("tenant", tenant_id),
            ("project", bindings["project_id"]),
            ("virtual_key", str(key["virtual_key_id"])),
        ]
        if key["application_id"] is not None:
            scope_pairs.append(("application", str(key["application_id"])))
        clauses = " OR ".join("(scope_type=? AND scope_id=?)" for _ in scope_pairs)
        params: list[object] = [tenant_id, key["environment_id"]]
        for scope_type, scope_id in scope_pairs:
            params.extend((scope_type, scope_id))
        rules = connection.execute(
            f"""
            SELECT budget_rule_id,scope_type,scope_id,daily_budget_cny,
                   exhausted_action,status,updated_at
            FROM budget_rules
            WHERE tenant_id=? AND environment_id=? AND status='active'
              AND (activation_at_utc IS NULL OR activation_at_utc<=?)
              AND ({clauses})
            ORDER BY budget_rule_id
            """,
            tuple([*params[:2], _utcnow().isoformat().replace("+00:00", "Z"), *params[2:]]),
        ).fetchall()
        if not rules:
            raise _NotApplicable("budget_coverage_required")
        dependencies.append(
            _Dependency(
                "virtual_key",
                str(key["virtual_key_id"]),
                "budget_covered_key",
                None,
                _resource_hash(
                    "virtual_key",
                    {
                        "environment_id": key["environment_id"],
                        "project_id": key["project_id"],
                        "application_id": key["application_id"],
                        "status": key["status"],
                    },
                ),
            )
        )
        for rule in rules:
            dependencies.append(
                _Dependency(
                    "budget_rule",
                    str(rule["budget_rule_id"]),
                    "budget_coverage",
                    None,
                    _resource_hash(
                        "budget_rule",
                        {
                            "scope_type": rule["scope_type"],
                            "scope_id": rule["scope_id"],
                            "daily_budget_cny": str(rule["daily_budget_cny"]),
                            "exhausted_action": rule["exhausted_action"],
                            "status": rule["status"],
                            "updated_at": rule["updated_at"],
                        },
                    ),
                )
            )
    expression = (
        f"project == {_dsl_string(bindings['project_id'])} and "
        f"budget_remaining < {bindings['threshold_cny']}"
    )
    return _SemanticPlan(
        expression=expression,
        action_type="deny",
        target_model_id=None,
        dependencies=_sorted_dependencies(dependencies),
    )


def _plan_customer_redaction(
    config: AppConfig,
    bindings: dict[str, str],
    project_dependency: _Dependency,
) -> _SemanticPlan:
    """要求 operator-attested mask 部署门禁开启，再生成非终止 mask Policy。"""

    if not config.policy_mask_runtime_enabled:
        raise _NotApplicable("policy_mask_runtime_required")
    capability_hash = _resource_hash(
        "runtime_capability",
        {"capability": _MASK_CAPABILITY, "enabled": True, "version": 1},
    )
    return _SemanticPlan(
        expression=f"project == {_dsl_string(bindings['project_id'])}",
        action_type="mask",
        target_model_id=None,
        dependencies=(
            project_dependency,
            _Dependency(
                "runtime_capability",
                _MASK_CAPABILITY,
                "deployment_gate",
                None,
                capability_hash,
            ),
        ),
    )


def _model_graph_dependencies(
    connection: sqlite3.Connection,
    tenant_id: str,
    model_id: object,
    *,
    relation: str,
    domestic_only: bool,
) -> list[_Dependency]:
    """加载并冻结一个 Alias 目标的 Model/Provider 图，跨租户和 disabled 同形失败。"""

    # 模型身份必须来自受控 Alias 的非空字符串，禁止宽松转换隐藏损坏引用。
    if type(model_id) is not str or not model_id:
        raise _NotApplicable("active_model_required")
    row = connection.execute(
        """
        SELECT
          m.*,p.provider_code,p.provider_type,p.auth_type,p.base_url,
          p.status AS provider_status,p.health_status,p.health_generation,p.health_updated_at
        FROM models m
        JOIN providers p ON p.tenant_id=m.tenant_id AND p.provider_id=m.provider_id
        WHERE m.tenant_id=? AND m.model_id=?
        """,
        (tenant_id, model_id),
    ).fetchone()
    # tenant-qualified 联表必须同时证明模型与 Provider 活跃，缺失和跨租户保持同形失败。
    if row is None or row["status"] != "active" or row["provider_status"] != "active":
        raise _NotApplicable("active_model_required")
    try:
        spec = provider_adapter_spec(row["provider_type"])
    # 未注册适配器不能进入可执行模板，以免发布 Gateway 无法兑现的上游路由。
    except ProviderAdapterSpecError as exc:
        # 对外只暴露不可适用原因，不泄漏适配器注册表或原始异常内容。
        raise _NotApplicable("provider_adapter_required") from exc
    if domestic_only and (
        row["provider_type"] not in DOMESTIC_PROVIDER_TYPES
        or spec.classification != MODEL_VENDOR_CLOUD
    ):
        # 国产模板要求类型白名单与云厂商分类双重成立，任一失败都必须拒绝证明。
        raise _NotApplicable("domestic_provider_required")
    return [
        _Dependency(
            "model",
            str(row["model_id"]),
            relation,
            None,
            _model_dependency_hash(row),
        ),
        _Dependency(
            "provider",
            str(row["provider_id"]),
            f"{relation}_provider",
            None,
            _provider_dependency_hash(row),
        ),
    ]


def _provider_health_warning(
    row: sqlite3.Row,
    as_of: datetime,
    *,
    stale_seconds: int,
) -> str | None:
    """把上游健康事实投影为硬阻断或时效告警，避免陈旧状态被误当作可用证明。"""

    status = row["health_status"]
    updated = _parse_optional_db_time(row["health_updated_at"])
    fresh = updated is not None and updated >= as_of - timedelta(seconds=stale_seconds)
    if status == "unhealthy" and fresh:
        return "provider_unhealthy"
    if status != "healthy" or not fresh:
        return "provider_health_unverified"
    return None


def _model_dependency_hash(row: sqlite3.Row) -> str:
    """冻结 Model 的上游身份和核心能力，不保存描述或价格之外正文。"""

    return _resource_hash(
        "model",
        {
            "provider_id": row["provider_id"],
            "provider_model_id": row["provider_model_id"],
            "upstream_model_name": row["upstream_model_name"],
            "capabilities": json.loads(row["capabilities"]),
            "status": row["model_status"] if "model_status" in row.keys() else row["status"],
        },
    )


def _provider_dependency_hash(row: sqlite3.Row) -> str:
    """冻结 Provider 分类、Endpoint 指纹和鉴权类型，不持久化 URL 原文。"""

    spec = provider_adapter_spec(row["provider_type"])
    return _resource_hash(
        "provider",
        {
            "provider_type": row["provider_type"],
            "classification": spec.classification,
            "base_url_hash": hashlib.sha256(str(row["base_url"]).encode("utf-8")).hexdigest(),
            "auth_type": row["auth_type"],
            "status": row["provider_status"],
        },
    )


def _alias_dependency_hash(row: sqlite3.Row) -> str:
    """冻结 Alias 环境、名称和默认/备用绑定，防止同 ID 被改指另一上游。"""

    return _resource_hash(
        "model_alias",
        {
            "alias": row["alias"],
            "environment_id": row["environment_id"],
            "environment_code": row["environment_code"],
            "default_model_id": row["default_model_id"],
            "fallback_model_id": row["fallback_model_id"],
            "status": row["status"],
        },
    )


def _stored_money(value: object) -> Decimal:
    """严格读取非负定点金额，拒绝 SQLite float、bool、指数和超精度值。"""

    # SQLite 动态类型可能混入浮点或布尔值，金额事实必须拒绝有损和歧义类型。
    if type(value) not in {str, int} or isinstance(value, bool):
        raise ValueError("stored money is invalid")
    text = str(value)
    # 仅允许有界普通十进制定点文本，避免指数、前导零和超精度造成成本漂移。
    if not re.fullmatch(r"(?:0|[1-9][0-9]{0,17})(?:\.[0-9]{1,9})?", text):
        raise ValueError("stored money is invalid")
    amount = Decimal(text)
    # 负成本会颠倒预算比较语义，存储层出现时必须失败关闭而非自动归零。
    if amount < 0:
        raise ValueError("stored money is invalid")
    return amount


def _dsl_string(value: str) -> str:
    """用 canonical JSON 字符串字面量构造受限 DSL，禁止手写转义。"""

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _dsl_string_list(values: tuple[str, ...]) -> str:
    """使用规范 JSON 数组构造受限列表字面量，避免手工转义改变策略语义。"""

    return json.dumps(list(values), ensure_ascii=False, separators=(",", ":"))


def _sorted_dependencies(values: list[_Dependency]) -> tuple[_Dependency, ...]:
    """稳定排序并拒绝相同资源关系的冲突依赖。"""

    ordered = sorted(
        values,
        key=lambda item: (item.resource_type, item.resource_id, item.relation),
    )
    seen: set[tuple[str, str, str]] = set()
    result: list[_Dependency] = []
    for item in ordered:
        identity = (item.resource_type, item.resource_id, item.relation)
        if identity in seen:
            # 同一模型可能被多个 Alias 引用；内容相同的重复依赖可安全去重，漂移则失败关闭。
            existing = next(
                value
                for value in result
                if (value.resource_type, value.resource_id, value.relation) == identity
            )
            if existing != item:
                # 同一资源关系只能对应一个冻结版本，冲突依赖表示影响计划内部已不可信。
                raise ApiError("policy_template.unavailable")
            continue
        seen.add(identity)
        result.append(item)
    return tuple(result)


def _dependency_wire(value: _Dependency) -> dict[str, object]:
    """把依赖转换为 fingerprint 使用的安全 canonical 对象。"""

    return {
        "resource_type": value.resource_type,
        "resource_id": value.resource_id,
        "relation": value.relation,
        "resource_name": value.resource_name,
        "version_or_hash": value.version_or_hash,
    }


def _resource_hash(resource_type: str, value: dict[str, object]) -> str:
    """对无秘密资源投影计算域隔离摘要，防止不同资源类型出现跨域同哈希歧义。"""

    return hashlib.sha256(
        b"metahub/policy-template/dependency/v1\x00"
        + resource_type.encode("ascii")
        + b"\x00"
        + _canonical_json_bytes(value)
    ).hexdigest()


def _require_applicable_plan(plan: _Plan, *, drift: bool = False) -> None:
    """要求计划仍可执行，并把并发漂移与初始不适配映射为不同错误。"""

    if "active_template_application_exists" in plan.reason_codes:
        # 每个项目与模板只允许一个活跃应用，重复创建会破坏可逆补偿的唯一归属。
        raise ApiError("policy_template.application_conflict")
    if plan.status == "incompatible" or plan.impact_hash is None:
        # 锁内漂移与首次不适用必须分码，调用方据此决定重新预览还是修复依赖。
        raise ApiError(
            "policy_template.impact_conflict" if drift else "policy_template.not_applicable"
        )
    # 分类复合权限必须由写锁内新 AuthContext 决定；页面的 can_apply 只用于展示。
    if not plan.can_apply:
        raise ApiError("auth.permission_denied")


def _generated_policy_name(template_name: str, application_id: str) -> str:
    """生成 tenant 内稳定且不与第二次应用冲突的有界 Policy 名称。"""

    suffix = application_id[-8:]
    return f"{template_name[:69]} [{suffix}]"


def _insert_application(
    connection: sqlite3.Connection,
    context: AuthContext,
    *,
    template_row: sqlite3.Row,
    plan: _Plan,
    preview_as_of: datetime,
    applied_as_of: datetime,
    request_id: str,
    trace_id: str,
    request_fingerprint: str,
    supplied_impact_hash: str,
    previous_row: sqlite3.Row,
    result_snapshot_id: str,
    result_snapshot_version: int,
    policy_audit_log_id: str,
    policy_event_id: str,
    snapshot_audit_log_id: str,
    snapshot_event_id: str,
    apply_audit_log_id: str,
    template_event_id: str,
) -> None:
    """插入引用六个预留事实的不可变 application 主记录。"""

    after = plan.after_candidate
    applicability = plan.applicability_fingerprint
    if after is None or applicability is None:
        # 可提交计划必须同时冻结结果候选和适用性指纹，否则 application 无法自证。
        raise ApiError("policy_template.unavailable")
    ids = plan.generated_ids
    created_text = _to_wire_time(applied_as_of)
    connection.execute(
        """
        INSERT INTO policy_template_applications(
          application_id,tenant_id,policy_template_id,template_version,
          template_definition_hash,project_id,actor_user_id,request_id,trace_id,
          request_fingerprint,preview_nonce,preview_as_of,applied_as_of,bindings_json,
          bindings_hash,impact_hash,applicability_fingerprint,before_candidate_hash,
          after_candidate_hash,previous_snapshot_id,previous_snapshot_version,
          result_snapshot_id,result_snapshot_version,generated_policy_id,
          generated_policy_version,status,policy_audit_log_id,policy_event_id,
          snapshot_audit_log_id,snapshot_event_id,apply_audit_log_id,template_event_id,
          created_at
        ) VALUES(
          ?,?,?,?,?,?,?,?,?,?,
          ?,?,?,?,?,?,?,?,?,?,
          ?,?,?,?,?,?,?,?,?,?,
          ?,?,?
        )
        """,
        (
            ids["application_id"],
            context.user.tenant_id,
            template_row["policy_template_id"],
            int(template_row["version"]),
            template_row["definition_hash"],
            plan.bindings["project_id"],
            context.user.user_id,
            request_id,
            trace_id,
            request_fingerprint,
            plan.preview_nonce,
            _to_wire_time(preview_as_of),
            created_text,
            _canonical_json_bytes(plan.bindings).decode("utf-8"),
            plan.bindings_hash,
            supplied_impact_hash,
            applicability,
            plan.before_candidate.content_hash,
            after.content_hash,
            previous_row["policy_snapshot_id"],
            int(previous_row["snapshot_version"]),
            result_snapshot_id,
            result_snapshot_version,
            ids["policy_id"],
            1,
            "applied",
            policy_audit_log_id,
            policy_event_id,
            snapshot_audit_log_id,
            snapshot_event_id,
            apply_audit_log_id,
            template_event_id,
            created_text,
        ),
    )


def _insert_application_children(
    connection: sqlite3.Connection,
    application_row: sqlite3.Row,
    plan: _Plan,
    *,
    generated_item_hash: str,
    previous_content_hash: str,
    result_content_hash: str,
    created_at: str,
) -> None:
    """写入两条资源变更摘要和 planner 冻结的全部无敏感依赖。"""

    tenant_id = str(application_row["tenant_id"])
    application_id = str(application_row["application_id"])
    item_rows = (
        (
            f"ptitem_{uuid4().hex}",
            tenant_id,
            application_id,
            0,
            "policy",
            "create",
            application_row["generated_policy_id"],
            None,
            None,
            1,
            generated_item_hash,
            created_at,
        ),
        (
            f"ptitem_{uuid4().hex}",
            tenant_id,
            application_id,
            1,
            "policy_snapshot",
            "publish",
            application_row["result_snapshot_id"],
            int(application_row["previous_snapshot_version"]),
            previous_content_hash,
            int(application_row["result_snapshot_version"]),
            result_content_hash,
            created_at,
        ),
    )
    connection.executemany(
        """
        INSERT INTO policy_template_application_items(
          application_item_id,tenant_id,application_id,item_order,resource_type,
          operation,resource_id,before_version,before_hash,after_version,after_hash,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        item_rows,
    )
    for dependency in plan.dependencies:
        connection.execute(
            """
            INSERT INTO policy_template_application_dependencies(
              application_dependency_id,tenant_id,application_id,resource_type,
              resource_id,relation,resource_name,version_or_hash,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                f"ptdep_{uuid4().hex}",
                tenant_id,
                application_id,
                dependency.resource_type,
                dependency.resource_id,
                dependency.relation,
                dependency.resource_name,
                dependency.version_or_hash,
                created_at,
            ),
        )


def _application_audit_summary(
    row: sqlite3.Row,
    *,
    applied_view: bool = False,
) -> dict[str, object]:
    """构造不含绑定正文、价格或 DSL 的 Template application 审计摘要。"""

    status = "applied" if applied_view else str(row["status"])
    summary: dict[str, object] = {
        "status": status,
        "template_code": str(row["template_code"]),
        "template_version": int(row["template_version"]),
        "application_id": str(row["application_id"]),
        "generated_policy_id": str(row["generated_policy_id"]),
        "previous_snapshot_version": int(row["previous_snapshot_version"]),
        "result_snapshot_version": int(row["result_snapshot_version"]),
        "impact_hash": str(row["impact_hash"]),
        "bindings_hash": str(row["bindings_hash"]),
        "changed_resource_types": ",".join(_CHANGED_RESOURCE_TYPES),
    }
    if status == "rolled_back":
        summary["rollback_snapshot_version"] = int(row["rollback_snapshot_version"])
    return summary


def _template_event_payload(row: sqlite3.Row) -> dict[str, object]:
    """从应用主记录生成固定九字段安全事件载荷，明确排除绑定正文与策略内容。"""

    return {
        "template_code": str(row["template_code"]),
        "template_version": int(row["template_version"]),
        "application_id": str(row["application_id"]),
        "generated_policy_id": str(row["generated_policy_id"]),
        "previous_snapshot_version": int(row["previous_snapshot_version"]),
        "result_snapshot_version": int(row["result_snapshot_version"]),
        "impact_hash": str(row["impact_hash"]),
        "bindings_hash": str(row["bindings_hash"]),
        "changed_resource_types": list(_CHANGED_RESOURCE_TYPES),
    }


def _insert_template_event(
    connection: sqlite3.Connection,
    application_row: sqlite3.Row,
    *,
    template_event_id: str,
    audit_log_id: str,
    created_at: datetime,
) -> _TemplateEvent:
    """在 application 事务内追加并严格回读唯一 EVT-075 source event。"""

    payload = _template_event_payload(application_row)
    connection.execute(
        """
        INSERT INTO policy_template_events(
          policy_template_event_id,tenant_id,application_id,policy_template_id,
          template_version,result_snapshot_id,result_snapshot_version,event_type,
          actor_user_id,audit_log_id,request_id,trace_id,payload_json,created_at
        ) VALUES(?,?,?,?,?,?,?,'policy_template.applied',?,?,?,?,?,?)
        """,
        (
            template_event_id,
            application_row["tenant_id"],
            application_row["application_id"],
            application_row["policy_template_id"],
            int(application_row["template_version"]),
            application_row["result_snapshot_id"],
            int(application_row["result_snapshot_version"]),
            application_row["actor_user_id"],
            audit_log_id,
            application_row["request_id"],
            application_row["trace_id"],
            _canonical_json_bytes(payload).decode("utf-8"),
            _to_wire_time(created_at),
        ),
    )
    event_row = connection.execute(
        """
        SELECT * FROM policy_template_events
        WHERE tenant_id=? AND policy_template_event_id=?
        """,
        (application_row["tenant_id"], template_event_id),
    ).fetchone()
    if event_row is None:
        # 事件必须在同事务按 tenant 回读成功，避免提交后只有审计而缺少通知事实。
        raise ApiError("policy_template.unavailable")
    return _template_event_record(event_row, application_row)


def _template_event_record(row: sqlite3.Row, application_row: sqlite3.Row) -> _TemplateEvent:
    """把持久 Template event 解码为已与 application 交叉核验的派发事实。"""

    payload = json.loads(str(row["payload_json"]))
    expected = _template_event_payload(application_row)
    if (
        type(payload) is not dict
        or payload != expected
        or row["tenant_id"] != application_row["tenant_id"]
        or row["application_id"] != application_row["application_id"]
        or row["policy_template_id"] != application_row["policy_template_id"]
        or int(row["template_version"]) != int(application_row["template_version"])
        or row["result_snapshot_id"] != application_row["result_snapshot_id"]
        or int(row["result_snapshot_version"])
        != int(application_row["result_snapshot_version"])
        or row["event_type"] != "policy_template.applied"
        or row["actor_user_id"] != application_row["actor_user_id"]
        or row["audit_log_id"] != application_row["apply_audit_log_id"]
        or row["request_id"] != application_row["request_id"]
        or row["trace_id"] != application_row["trace_id"]
    ):
        # 持久事件必须与 application 的租户、链路和载荷逐项一致，防止串线派发。
        raise ApiError("policy_template.unavailable")
    return _TemplateEvent(
        event_id=str(row["policy_template_event_id"]),
        tenant_id=str(row["tenant_id"]),
        application_id=str(row["application_id"]),
        actor_user_id=str(row["actor_user_id"]),
        payload=payload,
        request_id=str(row["request_id"]),
        trace_id=str(row["trace_id"]),
        created_at=_require_stored_time(row["created_at"]),
    )


# application 查询统一绑定模板代码和 tenant 连接键，避免各恢复路径产生不同租户边界。
_APPLICATION_SELECT = """
SELECT application.*,template.template_code
FROM policy_template_applications application
JOIN policy_templates template
  ON template.tenant_id=application.tenant_id
 AND template.policy_template_id=application.policy_template_id
"""


def _load_application_by_request(
    connection: sqlite3.Connection,
    tenant_id: str,
    request_id: str,
) -> sqlite3.Row | None:
    """按 tenant+request 加载唯一幂等 application，禁止跨租户恢复。"""

    return connection.execute(
        f"{_APPLICATION_SELECT} WHERE application.tenant_id=? AND application.request_id=?",
        (tenant_id, request_id),
    ).fetchone()


def _load_application_by_rollback_request(
    connection: sqlite3.Connection,
    tenant_id: str,
    request_id: str,
) -> sqlite3.Row | None:
    """按 tenant+rollback request 恢复唯一已提交补偿事实。"""

    return connection.execute(
        f"""
        {_APPLICATION_SELECT}
        WHERE application.tenant_id=? AND application.rollback_request_id=?
        """,
        (tenant_id, request_id),
    ).fetchone()


def _load_applied_application_by_previous_snapshot(
    connection: sqlite3.Connection,
    tenant_id: str,
    previous_snapshot_id: str,
) -> sqlite3.Row | None:
    """按 tenant+应用前 Snapshot 定位唯一未撤销模板，不跨租户枚举。"""

    return connection.execute(
        f"""
        {_APPLICATION_SELECT}
        WHERE application.tenant_id=? AND application.previous_snapshot_id=?
          AND application.status='applied'
        """,
        (tenant_id, previous_snapshot_id),
    ).fetchone()


def _load_application_by_id(
    connection: sqlite3.Connection,
    tenant_id: str,
    application_id: str,
) -> sqlite3.Row | None:
    """按 tenant+application 加载响应与事件共同使用的权威行。"""

    return connection.execute(
        f"{_APPLICATION_SELECT} WHERE application.tenant_id=? AND application.application_id=?",
        (tenant_id, application_id),
    ).fetchone()


def _application_info(
    row: sqlite3.Row,
    *,
    applied_view: bool = False,
) -> PolicyTemplateApplicationInfo:
    """转换 application 公共摘要；apply 回放可固定为提交时历史视图。"""

    applied_at = _require_stored_time(row["applied_as_of"])
    status = "applied" if applied_view else str(row["status"])
    rolled_back_at = (
        None if applied_view else _parse_optional_db_time(row["rolled_back_at"])
    )
    return PolicyTemplateApplicationInfo(
        application_id=str(row["application_id"]),
        policy_template_id=str(row["policy_template_id"]),
        template_code=str(row["template_code"]),  # type: ignore[arg-type]
        template_version=int(row["template_version"]),
        status=status,  # type: ignore[arg-type]
        generated_policy_id=str(row["generated_policy_id"]),
        generated_policy_version=int(row["generated_policy_version"]),
        rolled_back_policy_version=(
            None
            if applied_view
            else
            int(row["rolled_back_policy_version"])
            if row["rolled_back_policy_version"] is not None
            else None
        ),
        previous_snapshot_id=str(row["previous_snapshot_id"]),
        previous_snapshot_version=int(row["previous_snapshot_version"]),
        result_snapshot_id=str(row["result_snapshot_id"]),
        result_snapshot_version=int(row["result_snapshot_version"]),
        rollback_snapshot_id=(
            None
            if applied_view
            else
            str(row["rollback_snapshot_id"])
            if row["rollback_snapshot_id"] is not None
            else None
        ),
        rollback_snapshot_version=(
            None
            if applied_view
            else
            int(row["rollback_snapshot_version"])
            if row["rollback_snapshot_version"] is not None
            else None
        ),
        impact_hash=_normalize_sha256(row["impact_hash"], "policy_template.unavailable"),
        bindings_hash=_normalize_sha256(row["bindings_hash"], "policy_template.unavailable"),
        applied_at=applied_at,
        rolled_back_at=rolled_back_at,
    )


def _applied_policy_info(row: sqlite3.Row) -> PolicyInfo:
    """把受控撤销后的 generated Policy 投影回原 apply 的 active/v1 成功事实。"""

    current = _policy_info(row)
    return current.model_copy(
        update={
            "status": "active",
            "version": 1,
            "updated_at": current.created_at,
        }
    )


def _load_application_response(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    idempotent_replay: bool,
) -> PolicyTemplateApplyResponse:
    """从 tenant-qualified 持久图恢复成功响应，并核验两条 application item。"""

    try:
        current = _load_application_by_id(
            connection,
            str(row["tenant_id"]),
            str(row["application_id"]),
        )
        if current is None:
            # 幂等恢复必须重新按 tenant 找到 application，不能信任调用方传入的陈旧行对象。
            raise ApiError("policy_template.unavailable")
        policy_row = _load_policy_row(
            connection,
            str(current["tenant_id"]),
            str(current["generated_policy_id"]),
        )
        snapshot_row = _load_snapshot_row(
            connection,
            str(current["tenant_id"]),
            str(current["result_snapshot_id"]),
        )
        if policy_row is None or snapshot_row is None:
            # 成功响应依赖生成 Policy 与结果 Snapshot 同时存在，孤儿引用统一视为损坏。
            raise ApiError("policy_template.unavailable")
        if (
            int(snapshot_row["snapshot_version"]) != int(current["result_snapshot_version"])
            or snapshot_row["request_id"] != current["request_id"]
            or snapshot_row["trace_id"] != current["trace_id"]
            or (
                current["status"] == "applied"
                and (
                    policy_row["status"] != "active"
                    or int(policy_row["version"]) != 1
                )
            )
            or (
                current["status"] == "rolled_back"
                and (
                    policy_row["status"] != "disabled"
                    or int(policy_row["version"]) != 2
                )
            )
        ):
            # application 状态、Snapshot 链路与 Policy 版本必须互相印证，不能恢复矛盾成功。
            raise ApiError("policy_template.unavailable")
        _validate_application_children(connection, current, snapshot_row)
        return PolicyTemplateApplyResponse(
            application=_application_info(current, applied_view=True),
            generated_policy=_applied_policy_info(policy_row),
            policy_snapshot=_snapshot_info(snapshot_row),
            rollback_target_snapshot_id=str(current["previous_snapshot_id"]),
            idempotent_replay=idempotent_replay,
            request_id=str(current["request_id"]),
            trace_id=str(current["trace_id"]),
        )
    # 恢复路径只允许模板域不可用透传，其他底层领域错误统一折叠以免暴露内部结构。
    except ApiError as exc:
        if exc.code == "policy_template.unavailable":
            raise
        # 所有非模板错误都映射为固定服务不可用，保持损坏与跨租户查询同形响应。
        raise ApiError("policy_template.unavailable") from exc


def _validate_application_children(
    connection: sqlite3.Connection,
    application_row: sqlite3.Row,
    result_snapshot_row: sqlite3.Row,
) -> None:
    """交叉核验两条 item、结果制品和最小 Project 依赖，拒绝残缺回放。"""

    tenant_id = str(application_row["tenant_id"])
    application_id = str(application_row["application_id"])
    previous_row = _load_snapshot_row(
        connection,
        tenant_id,
        str(application_row["previous_snapshot_id"]),
    )
    if previous_row is None:
        # 变更摘要必须能回溯前序快照，缺失时无法证明 apply 只追加了声明资源。
        raise ApiError("policy_template.unavailable")
    artifact = _load_verified_artifact(connection, result_snapshot_row)
    generated = [
        item
        for item in artifact.items
        if item.policy_id == application_row["generated_policy_id"]
        and item.policy_version == int(application_row["generated_policy_version"])
    ]
    if len(generated) != 1:
        # 结果制品中生成 Policy 必须唯一，零个或多个都会破坏精确补偿边界。
        raise ApiError("policy_template.unavailable")
    rows = connection.execute(
        """
        SELECT * FROM policy_template_application_items
        WHERE tenant_id=? AND application_id=? ORDER BY item_order
        """,
        (tenant_id, application_id),
    ).fetchall()
    expected_policy_hash = _policy_item_graph_hash(generated[0])
    if (
        len(rows) != 2
        or rows[0]["resource_type"] != "policy"
        or rows[0]["operation"] != "create"
        or rows[0]["resource_id"] != application_row["generated_policy_id"]
        or rows[0]["before_version"] is not None
        or rows[0]["before_hash"] is not None
        or int(rows[0]["after_version"]) != 1
        or rows[0]["after_hash"] != expected_policy_hash
        or rows[1]["resource_type"] != "policy_snapshot"
        or rows[1]["operation"] != "publish"
        or rows[1]["resource_id"] != application_row["result_snapshot_id"]
        or int(rows[1]["before_version"])
        != int(application_row["previous_snapshot_version"])
        or rows[1]["before_hash"] != previous_row["content_hash"]
        or int(rows[1]["after_version"]) != int(application_row["result_snapshot_version"])
        or rows[1]["after_hash"] != artifact.content_hash
    ):
        # 两条冻结变更摘要必须精确证明 Policy 创建与 Snapshot 发布，顺序或哈希不可漂移。
        raise ApiError("policy_template.unavailable")
    project_dependency = connection.execute(
        """
        SELECT 1 FROM policy_template_application_dependencies
        WHERE tenant_id=? AND application_id=? AND resource_type='project'
          AND resource_id=? AND relation='bound_project'
        """,
        (tenant_id, application_id, application_row["project_id"]),
    ).fetchone()
    if project_dependency is None:
        # application 必须保存绑定项目依赖，缺失时无法审计模板作用域或检测后续漂移。
        raise ApiError("policy_template.unavailable")


def _load_apply_dispatch_facts(
    connection: sqlite3.Connection,
    application_row: sqlite3.Row,
) -> _ApplyDispatchFacts:
    """恢复并交叉核验 apply 的三审计与三 source event，供崩溃补派。"""

    try:
        tenant_id = str(application_row["tenant_id"])
        policy_row = _load_policy_row(
            connection,
            tenant_id,
            str(application_row["generated_policy_id"]),
        )
        snapshot_row = _load_snapshot_row(
            connection,
            tenant_id,
            str(application_row["result_snapshot_id"]),
        )
        if policy_row is None or snapshot_row is None:
            # 补派前必须重新按 tenant 证明底层资源仍存在，禁止从残缺主记录伪造事件。
            raise ApiError("policy_template.unavailable")
        policy_after = _policy_audit_summary(policy_row)
        policy_after["status"] = "active"
        policy_after["version"] = 1
        policy_audit = _load_exact_audit(
            connection,
            application_row,
            audit_log_id=str(application_row["policy_audit_log_id"]),
            action="policy.created",
            object_type="policy",
            object_id=str(application_row["generated_policy_id"]),
            object_name=str(policy_row["name"]),
            before={},
            after=policy_after,
        )
        policy_event_row = connection.execute(
            "SELECT * FROM policy_events WHERE tenant_id=? AND policy_event_id=?",
            (tenant_id, application_row["policy_event_id"]),
        ).fetchone()
        if policy_event_row is None:
            # Policy 事件是 apply 六类提交事实之一，缺失时不能只派发其余通知。
            raise ApiError("policy_template.unavailable")
        policy_event = _policy_event_record(policy_event_row)
        expected_policy_payload = {
            "policy_id": str(application_row["generated_policy_id"]),
            "version": 1,
            "status": "active",
            "priority": int(policy_row["priority"]),
            "action_type": str(policy_row["action_type"]),
            "target_model_id": policy_row["target_model_id"],
            "condition_hash": str(policy_row["condition_hash"]),
        }
        if (
            policy_event.policy_event_id != application_row["policy_event_id"]
            or policy_event.tenant_id != tenant_id
            or policy_event.policy_id != application_row["generated_policy_id"]
            or policy_event.event_type != "policy.created"
            or policy_event.actor_user_id != application_row["actor_user_id"]
            or policy_event.request_id != application_row["request_id"]
            or policy_event.trace_id != application_row["trace_id"]
            or policy_event.payload != expected_policy_payload
        ):
            # Policy 事件的租户、审计、链路及最小载荷必须与 application 完全一致。
            raise ApiError("policy_template.unavailable")

        snapshot_event, snapshot_audit = _load_snapshot_replay_dispatch_facts(
            connection,
            snapshot_row,
        )
        if (
            snapshot_event.event_id != application_row["snapshot_event_id"]
            or snapshot_audit.audit_log_id != application_row["snapshot_audit_log_id"]
        ):
            # Snapshot 事件和审计必须匹配主记录预留身份，禁止跨一次发布拼接事实。
            raise ApiError("policy_template.unavailable")
        template_after = _application_audit_summary(application_row, applied_view=True)
        template_audit = _load_exact_audit(
            connection,
            application_row,
            audit_log_id=str(application_row["apply_audit_log_id"]),
            action="policy_template.applied",
            object_type="policy_template_application",
            object_id=str(application_row["application_id"]),
            object_name=None,
            before={},
            after=template_after,
        )
        template_event_row = connection.execute(
            """
            SELECT * FROM policy_template_events
            WHERE tenant_id=? AND policy_template_event_id=?
            """,
            (tenant_id, application_row["template_event_id"]),
        ).fetchone()
        if template_event_row is None:
            # 模板级事件负责描述复合命令，缺失时底层事件不足以构成完整可补派事实。
            raise ApiError("policy_template.unavailable")
        template_event = _template_event_record(template_event_row, application_row)
        return _ApplyDispatchFacts(
            policy_audit=policy_audit,
            snapshot_audit=snapshot_audit,
            template_audit=template_audit,
            policy_event=policy_event,
            snapshot_event=snapshot_event,
            template_event=template_event,
        )
    # 派发事实恢复只对外保留模板域错误，避免审计或事件内部标识进入接口语义。
    except ApiError as exc:
        if exc.code == "policy_template.unavailable":
            raise
        # 其他领域校验失败统一视为持久事实损坏，不泄漏具体缺失对象或租户线索。
        raise ApiError("policy_template.unavailable") from exc


def _snapshot_chain_contains(
    connection: sqlite3.Connection,
    tenant_id: str,
    current_row: sqlite3.Row,
    expected_snapshot_id: str,
) -> bool:
    """沿 tenant 内单调 previous 链证明模板 result 仍是当前 head 的祖先。"""

    cursor = current_row
    visited: set[str] = set()
    while True:
        current_id = str(cursor["policy_snapshot_id"])
        if current_id == expected_snapshot_id:
            return True
        if current_id in visited:
            return False
        visited.add(current_id)
        previous_id = cursor["previous_snapshot_id"]
        previous_version = cursor["previous_snapshot_version"]
        if previous_id is None or previous_version is None:
            return False
        # 发布图必须逐版递减；出现跳版或缺行说明不能证明当前制品继承了模板结果。
        if int(previous_version) != int(cursor["snapshot_version"]) - 1:
            return False
        previous_row = _load_snapshot_row(connection, tenant_id, str(previous_id))
        if previous_row is None or int(previous_row["snapshot_version"]) != int(
            previous_version
        ):
            return False
        cursor = previous_row


def _remove_and_reindex_policy_item(
    items: tuple[PolicySnapshotItem, ...],
    generated_policy_id: str,
) -> tuple[PolicySnapshotItem, ...]:
    """精确删除唯一模板项并重新编号，保留其他已发布策略的完整相对顺序。"""

    remaining = [item for item in items if item.policy_id != generated_policy_id]
    if len(remaining) != len(items) - 1:
        # 目标生成项必须精确出现一次，缺失或重复都无法执行无歧义的补偿删除。
        raise ApiError("policy_template.rollback_conflict")
    return tuple(
        replace(item, evaluation_order=index)
        for index, item in enumerate(remaining)
    )


def _load_exact_audit(
    connection: sqlite3.Connection,
    application_row: sqlite3.Row,
    *,
    audit_log_id: str,
    action: str,
    object_type: str,
    object_id: str,
    object_name: str | None,
    before: dict[str, object],
    after: dict[str, object],
    actor_user_id: str | None = None,
    request_id: str | None = None,
    trace_id: str | None = None,
    created_at: datetime | None = None,
) -> AuditRecord:
    """按预留 ID 回读 AuditLog，并按 apply 或显式 rollback 身份证明摘要。"""

    row = connection.execute(
        "SELECT * FROM audit_logs WHERE tenant_id=? AND audit_log_id=?",
        (application_row["tenant_id"], audit_log_id),
    ).fetchone()
    if row is None:
        # 预留审计 ID 必须能在同一 tenant 下回读，跨租户或缺失均按事实损坏处理。
        raise ApiError("policy_template.unavailable")
    stored_before = json.loads(str(row["before_summary_json"]))
    stored_after = json.loads(str(row["after_summary_json"]))
    stored_changed = json.loads(str(row["changed_fields_json"]))
    expected_changed = sorted(
        key for key in set(before) | set(after) if before.get(key) != after.get(key)
    )
    expected_actor = actor_user_id or str(application_row["actor_user_id"])
    expected_request = request_id or str(application_row["request_id"])
    expected_trace = trace_id or str(application_row["trace_id"])
    expected_created = _to_wire_time(created_at) if created_at is not None else None
    if (
        row["tenant_id"] != application_row["tenant_id"]
        or row["actor_user_id"] != expected_actor
        or row["action"] != action
        or row["object_type"] != object_type
        or row["object_id"] != object_id
        or row["object_name"] != object_name
        or row["request_id"] != expected_request
        or row["trace_id"] != expected_trace
        or (expected_created is not None and row["created_at"] != expected_created)
        or stored_before != before
        or stored_after != after
        or stored_changed != expected_changed
    ):
        # 审计主体、链路、时间和摘要必须逐项一致，禁止补派经过篡改或串线的记录。
        raise ApiError("policy_template.unavailable")
    return AuditRecord(
        audit_log_id=audit_log_id,
        tenant_id=str(row["tenant_id"]),
        actor_user_id=str(row["actor_user_id"]),
        action=action,
        object_type=object_type,
        object_id=object_id,
        changed_fields=tuple(expected_changed),
        trace_id=str(row["trace_id"]),
        created_at=_require_stored_time(row["created_at"]),
    )


def _load_rollback_dispatch_facts(
    connection: sqlite3.Connection,
    application_row: sqlite3.Row,
) -> _RollbackDispatchFacts:
    """交叉核验模板补偿撤销的三审计与唯一 Snapshot 事件。"""

    try:
        if application_row["status"] != "rolled_back":
            # 只有已完成补偿状态才允许恢复撤销派发事实，applied 状态不得提前通知。
            raise ApiError("policy_template.unavailable")
        tenant_id = str(application_row["tenant_id"])
        rollback_actor = str(application_row["rollback_actor_user_id"])
        rollback_request = str(application_row["rollback_request_id"])
        rollback_trace = str(application_row["rollback_trace_id"])
        rolled_back_at = _require_stored_time(application_row["rolled_back_at"])
        rollback_snapshot_id = str(application_row["rollback_snapshot_id"])
        rollback_snapshot_version = int(application_row["rollback_snapshot_version"])

        snapshot_row = _load_snapshot_row(
            connection,
            tenant_id,
            rollback_snapshot_id,
        )
        policy_row = _load_policy_row(
            connection,
            tenant_id,
            str(application_row["generated_policy_id"]),
        )
        if snapshot_row is None or policy_row is None:
            # 撤销响应依赖补偿 Snapshot 与已停用 Policy 同时存在，孤儿引用不得补派。
            raise ApiError("policy_template.unavailable")
        if (
            snapshot_row["operation"] != "publish"
            or snapshot_row["rollback_source_snapshot_id"] is not None
            or int(snapshot_row["snapshot_version"]) != rollback_snapshot_version
            or snapshot_row["request_id"] != rollback_request
            or snapshot_row["trace_id"] != rollback_trace
            or snapshot_row["request_fingerprint"]
            != application_row["rollback_request_fingerprint"]
            or snapshot_row["created_by"] != rollback_actor
            or snapshot_row["created_at"] != application_row["rolled_back_at"]
            or snapshot_row["published_at"] != application_row["rolled_back_at"]
            or policy_row["status"] != "disabled"
            or int(policy_row["version"]) != 2
            or policy_row["updated_by"] != rollback_actor
            or policy_row["updated_at"] != application_row["rolled_back_at"]
        ):
            # 补偿快照和 Policy 状态必须由同一操作者、请求与时点共同证明。
            raise ApiError("policy_template.unavailable")
        artifact = _load_verified_artifact(connection, snapshot_row)
        if any(
            item.policy_id == application_row["generated_policy_id"]
            for item in artifact.items
        ):
            # 补偿制品不得继续包含已停用生成项，否则 Gateway 与控制面状态会分裂。
            raise ApiError("policy_template.unavailable")

        snapshot_event, snapshot_audit = _load_snapshot_replay_dispatch_facts(
            connection,
            snapshot_row,
        )
        if (
            snapshot_event.event_id
            != application_row["rollback_snapshot_event_id"]
            or snapshot_audit.audit_log_id
            != application_row["rollback_snapshot_audit_log_id"]
            or snapshot_event.created_at != rolled_back_at
            or snapshot_audit.created_at != rolled_back_at
        ):
            # 补偿事件和审计必须引用预留身份及同一发生时间，避免跨事务拼接证据。
            raise ApiError("policy_template.unavailable")

        policy_after = _policy_audit_summary(policy_row)
        policy_before = dict(policy_after)
        policy_before["status"] = "active"
        policy_before["version"] = 1
        policy_audit = _load_exact_audit(
            connection,
            application_row,
            audit_log_id=str(application_row["rollback_policy_audit_log_id"]),
            action="policy.updated",
            object_type="policy",
            object_id=str(application_row["generated_policy_id"]),
            object_name=str(policy_row["name"]),
            before=policy_before,
            after=policy_after,
            actor_user_id=rollback_actor,
            request_id=rollback_request,
            trace_id=rollback_trace,
            created_at=rolled_back_at,
        )
        template_before = _application_audit_summary(
            application_row,
            applied_view=True,
        )
        template_after = _application_audit_summary(application_row)
        template_audit = _load_exact_audit(
            connection,
            application_row,
            audit_log_id=str(application_row["rollback_apply_audit_log_id"]),
            action="policy_template.rolled_back",
            object_type="policy_template_application",
            object_id=str(application_row["application_id"]),
            object_name=None,
            before=template_before,
            after=template_after,
            actor_user_id=rollback_actor,
            request_id=rollback_request,
            trace_id=rollback_trace,
            created_at=rolled_back_at,
        )
        return _RollbackDispatchFacts(
            policy_audit=policy_audit,
            snapshot_audit=snapshot_audit,
            template_audit=template_audit,
            snapshot_event=snapshot_event,
        )
    # 撤销补派同样只暴露模板域不可用，底层审计与快照错误不进入外部契约。
    except ApiError as exc:
        if exc.code == "policy_template.unavailable":
            raise
        # 非模板错误统一折叠为固定错误码，避免从恢复接口探测内部资源存在性。
        raise ApiError("policy_template.unavailable") from exc


def _dispatch_rollback_facts(
    config: AppConfig,
    context: AuthContext,
    facts: _RollbackDispatchFacts,
) -> None:
    """提交后逐一补派三条审计和 Snapshot 事件，单点失败互不连带。"""

    dispatchers = (
        lambda: dispatch_audit_recorded(
            config,
            context,
            facts.policy_audit,
            request_id=facts.snapshot_event.request_id,
            trace_id=facts.snapshot_event.trace_id,
        ),
        lambda: dispatch_audit_recorded(
            config,
            context,
            facts.snapshot_audit,
            request_id=facts.snapshot_event.request_id,
            trace_id=facts.snapshot_event.trace_id,
        ),
        lambda: _dispatch_snapshot_event(config, facts.snapshot_event),
        lambda: dispatch_audit_recorded(
            config,
            context,
            facts.template_audit,
            request_id=facts.snapshot_event.request_id,
            trace_id=facts.snapshot_event.trace_id,
        ),
    )
    for dispatch in dispatchers:
        try:
            dispatch()
        except Exception:
            # 各底层派发器已脱敏记录故障；最后防线仍需继续后续事实，不能反转已提交结果。
            continue


def _dispatch_apply_facts(
    config: AppConfig,
    context: AuthContext,
    facts: _ApplyDispatchFacts,
) -> None:
    """提交后逐一派发六类事实；任一失败不得跳过其余事实或反转成功。"""

    dispatchers = (
        lambda: dispatch_audit_recorded(
            config,
            context,
            facts.policy_audit,
            request_id=facts.policy_event.request_id,
            trace_id=facts.policy_event.trace_id,
        ),
        lambda: _dispatch_policy_event(config, facts.policy_event),
        lambda: dispatch_audit_recorded(
            config,
            context,
            facts.snapshot_audit,
            request_id=facts.snapshot_event.request_id,
            trace_id=facts.snapshot_event.trace_id,
        ),
        lambda: _dispatch_snapshot_event(config, facts.snapshot_event),
        lambda: dispatch_audit_recorded(
            config,
            context,
            facts.template_audit,
            request_id=facts.template_event.request_id,
            trace_id=facts.template_event.trace_id,
        ),
        lambda: _dispatch_template_event(config, facts.template_event),
    )
    for dispatch in dispatchers:
        try:
            dispatch()
        except Exception:
            # 外部实现或测试替身即使越过各派发器自己的隔离边界，也不能阻断后续补派。
            logger.error(
                "policy_template.dispatch_failed",
                extra={
                    "event": "policy_template.dispatch_failed",
                    "tenant_id": facts.template_event.tenant_id,
                    "actor_id": facts.template_event.actor_user_id,
                    "object_id": facts.template_event.application_id,
                    "request_id": facts.template_event.request_id,
                    "trace_id": facts.template_event.trace_id,
                    "error_code": "webhook.delivery_unavailable",
                },
            )


def _dispatch_template_event(config: AppConfig, event: _TemplateEvent) -> None:
    """从持久 EVT-075 构造 Webhook；异常只记录固定安全定位字段。"""

    try:
        webhook_event = build_webhook_event(
            tenant_id=event.tenant_id,
            event_type="policy_template.applied",
            # EVT-075 的 shared 信封直接使用持久 ptevt_ source ID，回放才能保持同一幂等键。
            event_id=event.event_id,
            timestamp=event.created_at,
            actor_id=event.actor_user_id,
            object_id=event.application_id,
            trace_id=event.trace_id,
            source_request_id=event.request_id,
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
            "policy_template.webhook_dispatch_failed",
            extra={
                "event": "policy_template.webhook_dispatch_failed",
                "tenant_id": event.tenant_id,
                "actor_id": event.actor_user_id,
                "object_id": event.application_id,
                "request_id": event.request_id,
                "trace_id": event.trace_id,
                "error_code": "webhook.delivery_unavailable",
            },
        )


def _normalize_template_id(value: object) -> str:
    """规范模板路径 ID；非法与跨租户不存在统一返回 not_found。"""

    if type(value) is not str or _TEMPLATE_ID_RE.fullmatch(value) is None:
        # 路径身份非法与资源不存在保持同形，避免通过格式差异枚举内部模板标识。
        raise ApiError("policy_template.not_found")
    return value


def _normalize_head_match(value: object) -> int:
    """解析模板 apply 的强 head ETag，拒绝弱值、通配符和多值。"""

    if type(value) is not str or (matched := _HEAD_ETAG_RE.fullmatch(value)) is None:
        # head 条件只接受单一强 ETag，弱校验或宽松转换会削弱发布并发保护。
        raise ApiError("policy_template.payload_invalid")
    return int(matched.group(1))


def _normalize_candidate_match(value: object) -> str:
    """解析模板应用的强候选校验标签，仅接受带引号的小写完整摘要。"""

    if type(value) is not str or (matched := _CANDIDATE_ETAG_RE.fullmatch(value)) is None:
        # 候选摘要必须精确绑定预览制品，通配符、大小写变体和多值均拒绝。
        raise ApiError("policy_template.payload_invalid")
    return matched.group(1)


def _normalize_correlation_id(value: object) -> str:
    """验证共享中间件生成的 request/trace ID 可安全持久化。"""

    if type(value) is not str:
        # 链路标识必须由共享中间件提供字符串，禁止把任意对象隐式持久化到审计表。
        raise ApiError("policy_template.payload_invalid")
    normalized = value.strip()
    if not normalized or len(normalized) > 128 or _contains_control(normalized):
        # 空值、超长值和控制字符会破坏日志关联或形成注入边界，必须统一拒绝。
        raise ApiError("policy_template.payload_invalid")
    return normalized


def _normalize_apply_reason(value: object) -> str:
    """规范有界变更原因，控制字符与非规范 Unicode 不得进入 Snapshot。"""

    if type(value) is not str:
        # 变更原因进入 Snapshot 与审计摘要，只接受显式字符串以避免表示歧义。
        raise ApiError("policy_template.payload_invalid")
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized or len(normalized) > 256 or _contains_control(normalized):
        # 规范化后仍须有界且无控制字符，避免持久事实在不同客户端呈现不同语义。
        raise ApiError("policy_template.payload_invalid")
    return normalized


def _normalize_sha256(value: object, error_code: str) -> str:
    """读取精确小写 SHA-256，禁止前缀、大写或宽松类型转换。"""

    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        # 摘要必须保持固定小写编码，禁止宽松转换让不同输入共享同一比较结果。
        raise ApiError(error_code)
    return value


def _require_utc_datetime(value: object) -> datetime:
    """读取 canonical UTC 时间；预览不接受本地时区或含糊 naive 时间。"""

    if isinstance(value, datetime) and not isinstance(value, bool):
        parsed = value
    elif type(value) is str and re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z",
        value,
    ):
        try:
            parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        # 语法匹配后仍由标准库验证真实日历时间，解析细节不得进入接口响应。
        except ValueError:
            # 无效日期统一归为请求载荷非法，不回显可能含调用方数据的异常正文。
            raise ApiError("policy_template.payload_invalid") from None
    else:
        # 预览时点必须使用规范 UTC 文本，拒绝本地时区、空格和非标准精度。
        raise ApiError("policy_template.payload_invalid")
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        # 即使解析成功也必须再次证明 UTC aware，防止标准库宽松行为绕过时区边界。
        raise ApiError("policy_template.payload_invalid")
    return parsed.astimezone(UTC)


def _require_stored_time(value: object) -> datetime:
    """严格解析数据库 UTC 时间；损坏值统一视为模板服务不可用。"""

    if type(value) is not str:
        # 数据库时间不是字符串表示持久事实损坏，不能隐式转换后继续构造审计链。
        raise ApiError("policy_template.unavailable")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # 存量异常不向外暴露原始字段值或解析位置，统一按模板服务不可用处理。
    except ValueError:
        # 损坏时间会使事件排序与幂等恢复失真，因此必须失败关闭而非猜测修复。
        raise ApiError("policy_template.unavailable") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        # 持久时间必须可证明为 UTC aware，非 UTC 值不能参与跨事务事实比较。
        raise ApiError("policy_template.unavailable")
    return parsed.astimezone(UTC)


def _parse_optional_db_time(value: object) -> datetime | None:
    """解析可空数据库 UTC 时间，供依赖 freshness 与 application 响应复用。"""

    if value is None:
        return None
    return _require_stored_time(value)


def _to_wire_time(value: datetime) -> str:
    """编码 UTC 时间；模板复合事务统一使用审计模块的秒级精度。"""

    if value.tzinfo is None:
        # naive 时间缺少可审计时区语义，禁止静默套用服务器本地时区后持久化。
        raise ValueError("timezone required")
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utcnow() -> datetime:
    """返回秒级 UTC aware 时间，与强审计持久化精度保持完全一致。"""

    return datetime.now(UTC).replace(microsecond=0)


def _contains_control(value: str) -> bool:
    """判断文本是否含任意 Unicode 控制、格式或代理字符。"""

    return any(unicodedata.category(character).startswith("C") for character in value)


def _canonical_json_bytes(value: object) -> bytes:
    """生成跨进程稳定的规范 JSON 字节，供摘要与持久事实执行确定性比较。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_json(value: object) -> str:
    """计算规范 JSON 的小写完整摘要，确保预览和提交共享同一内容身份。"""

    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _connect(config: AppConfig) -> sqlite3.Connection:
    """连接控制面数据库，并隐藏底层 URL、路径和驱动异常。"""

    try:
        return connect_database(config.database_url)
    # 连接配置和数据库路径可能含敏感部署信息，底层异常必须在域边界内收敛。
    except DbError as exc:
        # 调用方只获得稳定模板错误码，不得看到数据库 URL、驱动或文件系统细节。
        raise ApiError("policy_template.unavailable") from exc


def _safe_rollback(connection: sqlite3.Connection) -> None:
    """尽力释放模板写事务，rollback 故障不得覆盖原统一错误。"""

    try:
        connection.rollback()
    # 回滚本身失败不能覆盖最初业务异常；连接关闭将继续释放残余事务资源。
    except sqlite3.Error:
        pass
