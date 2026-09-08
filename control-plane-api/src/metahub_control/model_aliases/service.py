"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义，Provider Key 与 Token 不得明文落日志。
来源文件：control-plane-api/src/metahub_control/model_aliases/service.py
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import build_webhook_event

from metahub_control.audit import (
    AuditRecord,
    AuditRequestMeta,
    commit_audited_transaction,
    dispatch_audit_recorded,
    record_audit_log,
)
from metahub_control.auth import AuthContext
from metahub_control.auth.resource_transactions import ResourceWriteTransaction
from metahub_control.environments.service import resolve_environment_binding

from .models import (
    CacheAffinityInfo,
    CreateModelAliasRequest,
    FallbackPolicyInfo,
    FallbackPolicyResponse,
    ModelAliasInfo,
    ModelAliasListResponse,
    ModelAliasResponse,
    QualityCostConfigInfo,
    RoutingPolicyCandidateInfo,
    RoutingPolicyInfo,
    RoutingPolicyMutationResponse,
    UpdateFallbackPolicyRequest,
    UpdateModelAliasRequest,
)

logger = logging.getLogger("metahub.model_aliases")

# 模型别名的环境、状态和命名规则集中定义，确保路由别名不会跨环境误用。
VALID_ENVIRONMENTS = {"dev", "test", "prod"}
VALID_STATUSES = {"active", "disabled"}
ALIAS_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,119}$")
MAX_DESCRIPTION_LENGTH = 240
# 运行时只接受 Excel 明确列出的三类上游失败，固定顺序保证 JSON、版本比较和测试结果稳定。
FALLBACK_CONDITION_ORDER = (
    "upstream_timeout",
    "upstream_rate_limited",
    "upstream_5xx",
)
FALLBACK_CONDITIONS = frozenset(FALLBACK_CONDITION_ORDER)
FALLBACK_ACTION = {"type": "use_alias_fallback", "max_attempts": 1}

# RoutingPolicy 的四种执行模式是跨 Control/Gateway/Webhook 的机器合同，不能接受自由文本。
ROUTING_MODES = frozenset({"fixed", "cost_first", "availability_first", "quality_cost"})
# 质量成本配置使用 SQLite 有符号整数内的基点/样本边界，三权重必须精确合计 10000。
MAX_QUALITY_SAMPLE_COUNT = 10_000
QUALITY_COST_CONFIG_FIELDS = frozenset(
    {
        "quality_weight_bps",
        "cost_weight_bps",
        "latency_weight_bps",
        "quality_floor_bps",
        "minimum_sample_count",
    }
)
# 这些 Alias 字段会改变实际初始路由图，数据库 AFTER trigger 会为一次 SQL 更新严格推进一版。
ROUTING_ALIAS_FIELDS = frozenset(
    {"alias", "environment_id", "environment", "status", "default_model_id", "fallback_model_id"}
)


@dataclass(frozen=True)
class _RoutingPolicyEventRecord:
    """从持久配置事件行重建 Webhook 所需的最小安全事实。"""

    routing_policy_event_id: str
    tenant_id: str
    routing_policy_id: str
    model_alias_id: str
    routing_policy_version: int
    payload_schema_version: int
    actor_user_id: str
    request_id: str
    trace_id: str
    request_fingerprint: str
    payload: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class _RecoveredMutation:
    """同 request_id 精确恢复时返回的 Alias、事件和原审计投递事实。"""

    model_alias: ModelAliasInfo
    event: _RoutingPolicyEventRecord
    audit: AuditRecord


def list_model_aliases(
    config: AppConfig,
    context: AuthContext,
    *,
    status: str | None,
    environment_id: str | None,
    environment: str | None,
    request_id: str,
    trace_id: str,
) -> ModelAliasListResponse:
    """列出当前租户的模型别名。

    GET 面向管理员和开发者开放，只返回路由元数据，不包含任何调用密钥或请求内容。
    """

    normalized_status = _normalize_optional_status(status)
    normalized_environment = _normalize_optional_environment(environment)
    params: list[object] = [context.user.tenant_id]
    filters: list[str] = []
    if normalized_status:
        filters.append("a.status=?")
        params.append(normalized_status)
    if normalized_environment:
        filters.append("a.environment=?")
        params.append(normalized_environment)
    connection = _connect(config)
    transaction = ResourceWriteTransaction(connection, config, context, "routing.read", read_only=True)
    try:
        transaction.begin()
        context = transaction.context
        if environment_id:
            binding = resolve_environment_binding(
                connection,
                context.user.tenant_id,
                environment_id=environment_id,
                environment_code=normalized_environment,
                require_active=False,
                allow_default=False,
            )
            filters.append("a.environment_id=?")
            params.append(binding.environment_id)
        where_extra = f" AND {' AND '.join(filters)}" if filters else ""
        rows = connection.execute(
            f"""
        SELECT
          a.model_alias_id, a.tenant_id, a.alias, a.environment,
          e.environment_id AS environment_id, e.name AS environment_name, e.status AS environment_status,
          a.default_model_id,
          a.fallback_model_id, a.status, a.description, a.created_at, a.updated_at,
          a.disabled_at,
          dm.model_alias AS default_internal_model_id,
          dm.display_name AS default_display_name,
          dm.status AS default_model_status,
          dp.provider_code AS default_provider_code,
          dp.name AS default_provider_name,
          dp.status AS default_provider_status,
          fm.model_alias AS fallback_internal_model_id,
          fm.display_name AS fallback_display_name,
          fm.status AS fallback_model_status,
          fp.provider_code AS fallback_provider_code,
          fp.name AS fallback_provider_name,
          fp.status AS fallback_provider_status,
          fpol.fallback_policy_id, fpol.status AS fallback_policy_status,
          fpol.version AS fallback_policy_version, fpol.priority AS fallback_policy_priority,
          fpol.condition_expr AS fallback_condition_expr, fpol.action AS fallback_action,
          fpol.created_at AS fallback_policy_created_at, fpol.updated_at AS fallback_policy_updated_at,
          rp.routing_policy_id, rp.routing_mode,
          rp.status AS routing_policy_status, rp.version AS routing_policy_version,
          rp.quality_weight_bps, rp.cost_weight_bps, rp.latency_weight_bps,
          rp.quality_floor_bps, rp.minimum_sample_count,
          rp.created_at AS routing_policy_created_at, rp.updated_at AS routing_policy_updated_at,
          ca.cache_affinity_id, ca.status AS cache_affinity_status,
          ca.data_class AS cache_affinity_data_class,
          ca.minimum_prefix_chars AS cache_affinity_minimum_prefix_chars,
          ca.ttl_seconds AS cache_affinity_ttl_seconds,
          ca.version AS cache_affinity_version,
          ca.created_at AS cache_affinity_created_at,
          ca.updated_at AS cache_affinity_updated_at
        FROM model_aliases a
        JOIN environments e
          ON e.tenant_id=a.tenant_id
         AND (e.environment_id=a.environment_id OR (a.environment_id IS NULL AND e.code=a.environment))
        JOIN models dm ON dm.model_id = a.default_model_id AND dm.tenant_id = a.tenant_id
        JOIN providers dp ON dp.provider_id = dm.provider_id AND dp.tenant_id = dm.tenant_id
        LEFT JOIN models fm ON fm.model_id = a.fallback_model_id AND fm.tenant_id = a.tenant_id
        LEFT JOIN providers fp ON fp.provider_id = fm.provider_id AND fp.tenant_id = fm.tenant_id
        LEFT JOIN fallback_policies fpol
          ON fpol.model_alias_id=a.model_alias_id AND fpol.tenant_id=a.tenant_id
        LEFT JOIN model_alias_routing_policies rp
          ON rp.model_alias_id=a.model_alias_id AND rp.tenant_id=a.tenant_id
        LEFT JOIN cache_affinities ca
          ON ca.model_alias_id=a.model_alias_id AND ca.tenant_id=a.tenant_id
        WHERE a.tenant_id=? {where_extra}
        ORDER BY a.environment ASC, a.alias ASC
            """,
            params,
        ).fetchall()
        result = ModelAliasListResponse(
            model_aliases=[_model_alias_info(row) for row in rows],
            request_id=request_id,
            trace_id=trace_id,
        )
        transaction.commit()
        return result
    finally:
        transaction.__exit__(None, None, None)


def create_model_alias(
    config: AppConfig,
    context: AuthContext,
    payload: CreateModelAliasRequest,
    *,
    audit_request_meta: AuditRequestMeta,
    request_id: str,
    trace_id: str,
) -> ModelAliasResponse:
    """在单一写事务中创建 Alias、fixed v1 Policy、审计和配置事件。

    同 tenant/actor/request_id 的精确重试会从持久事件恢复原结果；不同意图复用链路 ID
    返回 409。Alias trigger 自动创建 Policy，服务层不得另插一行造成双写。
    """

    normalized_request_id = _normalize_request_id(request_id)
    alias = _normalize_alias(payload.alias)
    status = _normalize_status(payload.status)
    description = _normalize_description(payload.description)
    default_model_id = _normalize_model_id(payload.default_model_id)
    fallback_model_id = _normalize_optional_model_id(payload.fallback_model_id)
    fingerprint = _mutation_fingerprint(
        "create",
        {
            "alias": alias,
            "environment_id": (
                str(payload.environment_id).strip()
                if payload.environment_id is not None
                else None
            ),
            "environment": (
                str(payload.environment).strip().lower()
                if payload.environment is not None
                else None
            ),
            "default_model_id": default_model_id,
            "fallback_model_id": fallback_model_id,
            "description_hash": _description_hash(description),
            "status": status,
        },
    )
    connection = _connect(config)
    now = _to_db_time(_utcnow())
    disabled_at = now if status == "disabled" else None
    disabled_by = context.user.user_id if status == "disabled" else None
    audit_record: AuditRecord | None = None
    event: _RoutingPolicyEventRecord | None = None
    replay_to_dispatch: _RecoveredMutation | None = None
    transaction = ResourceWriteTransaction(connection, config, context, "routing.write")
    try:
        # 写锁必须覆盖恢复检查、目标复核、Alias/Policy、audit/event 和提交，消除并发覆盖窗口。
        transaction.begin()
        context = transaction.context
        recovered = _recover_mutation(
            connection,
            context,
            request_id=normalized_request_id,
            fingerprint=fingerprint,
        )
        if recovered is not None:
            transaction.verify()
            connection.rollback()
            replay_to_dispatch = recovered
            return ModelAliasResponse(
                model_alias=recovered.model_alias,
                request_id=normalized_request_id,
                trace_id=recovered.event.trace_id,
            )

        environment_binding = resolve_environment_binding(
            connection,
            context.user.tenant_id,
            environment_id=payload.environment_id,
            environment_code=payload.environment,
            invalid_code_error="model_alias.environment_invalid",
            mismatch_error="model_alias.environment_mismatch",
        )
        _validate_target_models(
            connection,
            context.user.tenant_id,
            default_model_id=default_model_id,
            fallback_model_id=fallback_model_id,
            require_active=status == "active",
        )
        model_alias_id = str(uuid4())
        connection.execute(
            """
            INSERT INTO model_aliases(
              model_alias_id, tenant_id, alias, environment, environment_id, default_model_id,
              fallback_model_id, status, description, created_by, disabled_at,
              disabled_by, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                model_alias_id,
                context.user.tenant_id,
                alias,
                environment_binding.code,
                environment_binding.environment_id,
                default_model_id,
                fallback_model_id,
                status,
                description,
                context.user.user_id,
                disabled_at,
                disabled_by,
                now,
                now,
            ),
        )
        # 创建时已经配置备用模型，应在同一事务建立标准策略；否则别名可见但 Gateway 看不到策略。
        if fallback_model_id is not None:
            _write_fallback_policy(
                connection,
                tenant_id=context.user.tenant_id,
                model_alias_id=model_alias_id,
                enabled=True,
                trigger_conditions=list(FALLBACK_CONDITION_ORDER),
                actor_id=context.user.user_id,
                now=now,
            )
        model_alias = _load_model_alias(connection, context.user.tenant_id, model_alias_id)
        routing_policy = _require_routing_policy(model_alias)
        audit_record = record_audit_log(
            connection,
            context,
            action="routing_policy.created",
            object_type="routing_policy",
            object_id=routing_policy.routing_policy_id,
            object_name=model_alias.alias,
            before_summary={},
            after_summary=_routing_audit_summary(model_alias),
            request_meta=audit_request_meta,
            request_id=normalized_request_id,
            trace_id=trace_id,
        )
        # Alias 创建必须同时生成可追溯审计；缺少审计对象时整个策略和事件一起回滚。
        if audit_record is None:
            raise ApiError("model_alias.routing_policy_update_failed")
        event = _insert_routing_policy_event(
            connection,
            context,
            model_alias,
            request_id=normalized_request_id,
            trace_id=trace_id,
            fingerprint=fingerprint,
            change_kinds=["created"],
            changed_routing_fields=[
                "alias",
                "environment",
                "environment_id",
                "status",
                "default_model_id",
                "fallback_model_id",
                "routing_mode",
                "quality_cost_config",
            ],
            previous_mode=None,
            now=now,
        )
        transaction.commit(commit_audited_transaction)
    # 已分类业务错误先回滚，再把审计基础错误折叠为 ModelAlias 稳定领域码。
    except ApiError as exc:
        connection.rollback()
        # 审计不可用不能暴露内部审计模块错误，也不能让客户端误以为 Alias 已创建。
        if exc.code == "audit_log.record_unavailable":
            raise ApiError("model_alias.routing_policy_update_failed") from exc
        raise
    # 唯一键冲突单独映射 duplicate；其他关系约束统一视为策略事务不可用。
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        # 只有明确的租户内 Alias 唯一键冲突才向客户端返回可修正的 duplicate。
        if _is_alias_duplicate(exc):
            raise ApiError("model_alias.duplicate") from exc
        raise ApiError("model_alias.routing_policy_update_failed") from exc
    # 其他 SQLite 故障可能包含路径或 SQL，响应只能保留稳定可重试错误码。
    except sqlite3.Error as exc:
        connection.rollback()
        raise ApiError("model_alias.routing_policy_update_failed") from exc
    finally:
        transaction.__exit__(None, None, None)
        if replay_to_dispatch is not None:
            _dispatch_recovered_mutation(config, context, replay_to_dispatch)

    # 网络投递只在业务/audit/event 已提交后执行，外部失败绝不能回滚已生效 Alias。
    dispatch_audit_recorded(
        config,
        context,
        audit_record,
        request_id=normalized_request_id,
        trace_id=trace_id,
    )
    if event is not None:
        _dispatch_routing_policy_event(config, event)
    _log_changed(context, model_alias, request_id, trace_id, action="created", changed_fields=list(_create_fields()))
    return ModelAliasResponse(model_alias=model_alias, request_id=normalized_request_id, trace_id=trace_id)


def update_model_alias(
    config: AppConfig,
    context: AuthContext,
    model_alias_id: str,
    payload: UpdateModelAliasRequest,
    *,
    audit_request_meta: AuditRequestMeta,
    request_id: str,
    trace_id: str,
) -> ModelAliasResponse:
    """按 RoutingPolicy 版本更新 Alias，并原子保存路由图审计与配置事件。

    description-only 更新不改变执行语义，因此允许省略策略版本且不写 RoutingPolicy audit/event；
    其他 Alias 字段必须先在写锁内完成 CAS，再判断 no-op，避免旧客户端覆盖并发路由图。
    """

    normalized_request_id = _normalize_request_id(request_id)
    supplied_fields = _supplied_fields(payload)
    editable_fields = supplied_fields - {"expected_routing_policy_version"}
    # 空 PATCH 没有可审计业务意图，不能只更新时间或制造虚假策略版本。
    if not editable_fields:
        raise ApiError("model_alias.update_empty")
    routing_requested = bool(editable_fields & ROUTING_ALIAS_FIELDS)
    expected_version = (
        _normalize_expected_version(payload.expected_routing_policy_version)
        if routing_requested
        else None
    )
    # exactly-once 指纹必须基于最终可落库的规范值；否则大小写或首尾空格不同、
    # 但业务结果相同的重试会被错误判成另一项写意图。
    normalized_intent: dict[str, object | None] = {}
    if "alias" in editable_fields:
        normalized_intent["alias"] = _normalize_alias(payload.alias)
    # 默认目标决定真实 Provider 调用，指纹和落库必须复用同一规范化 Model ID。
    if "default_model_id" in editable_fields:
        normalized_intent["default_model_id"] = _normalize_model_id(payload.default_model_id)
    # 显式空 fallback 与省略字段语义不同，规范化后仍必须保留该差异进入指纹。
    if "fallback_model_id" in editable_fields:
        normalized_intent["fallback_model_id"] = _normalize_optional_model_id(
            payload.fallback_model_id
        )
    if "description" in editable_fields:
        normalized_intent["description"] = _normalize_description(payload.description)
    if "status" in editable_fields:
        normalized_intent["status"] = _normalize_status(payload.status)
    fingerprint_values: dict[str, object] = {
        "model_alias_id": model_alias_id,
        "expected_version": expected_version,
    }
    for field in sorted(editable_fields):
        if field == "environment_id":
            value = str(payload.environment_id or "").strip()
        elif field == "environment":
            value = str(payload.environment or "").strip().lower()
        else:
            value = normalized_intent[field]
        fingerprint_values[field] = _description_hash(value) if field == "description" else value
    fingerprint = _mutation_fingerprint("alias_update", fingerprint_values)

    connection = _connect(config)
    updates: dict[str, object | None] = dict(normalized_intent)
    now = _to_db_time(_utcnow())
    audit_record: AuditRecord | None = None
    event: _RoutingPolicyEventRecord | None = None
    replay_to_dispatch: _RecoveredMutation | None = None
    transaction = ResourceWriteTransaction(connection, config, context, "routing.write")
    try:
        transaction.begin()
        context = transaction.context
        recovered = _recover_mutation(
            connection,
            context,
            request_id=normalized_request_id,
            fingerprint=fingerprint,
        )
        if recovered is not None:
            transaction.verify()
            connection.rollback()
            replay_to_dispatch = recovered
            return ModelAliasResponse(
                model_alias=recovered.model_alias,
                request_id=normalized_request_id,
                trace_id=recovered.event.trace_id,
            )

        before = _load_model_alias(connection, context.user.tenant_id, model_alias_id)
        before_policy = _require_routing_policy(before)
        # CAS 必须在 no-op 和目标校验之前执行，阻止旧客户端覆盖并发路由图。
        if routing_requested and before_policy.version != expected_version:
            raise ApiError("model_alias.routing_policy_version_conflict")

        if editable_fields & {"environment_id", "environment"}:
            # Environment ID 和兼容代码必须在锁内解析并双写，不能与并发 Alias 状态分叉。
            environment_binding = resolve_environment_binding(
                connection,
                context.user.tenant_id,
                environment_id=payload.environment_id,
                environment_code=payload.environment,
                allow_default=False,
                invalid_code_error="model_alias.environment_invalid",
                mismatch_error="model_alias.environment_mismatch",
            )
            updates["environment_id"] = environment_binding.environment_id
            updates["environment"] = environment_binding.code
        changed_updates = {
            field: value
            for field, value in updates.items()
            if getattr(before, field) != value
        }
        if not changed_updates:
            transaction.verify()
            connection.rollback()
            return ModelAliasResponse(
                model_alias=before,
                request_id=normalized_request_id,
                trace_id=trace_id,
            )

        final_default_model_id = str(
            changed_updates.get("default_model_id", before.default_model_id)
        )
        final_fallback_model_id = changed_updates.get(
            "fallback_model_id", before.fallback_model_id
        )
        final_status = str(changed_updates.get("status", before.status))
        _validate_target_models(
            connection,
            context.user.tenant_id,
            default_model_id=final_default_model_id,
            fallback_model_id=str(final_fallback_model_id) if final_fallback_model_id else None,
            require_active=final_status == "active",
        )

        set_clauses = [f"{column}=?" for column in changed_updates]
        values = list(changed_updates.values())
        disabled_event = before.status != "disabled" and changed_updates.get("status") == "disabled"
        reactivated = before.status == "disabled" and changed_updates.get("status") == "active"
        if disabled_event:
            set_clauses.extend(["disabled_at=?", "disabled_by=?"])
            values.extend([now, context.user.user_id])
        elif reactivated:
            set_clauses.extend(["disabled_at=NULL", "disabled_by=NULL"])
        set_clauses.append("updated_at=?")
        values.append(now)
        values.extend([context.user.tenant_id, model_alias_id])
        connection.execute(
            f"""
            UPDATE model_aliases
            SET {', '.join(set_clauses)}
            WHERE tenant_id=? AND model_alias_id=?
            """,
            values,
        )
        # 兼容旧 PATCH：真实备用目标变化时同步 PRD-036 策略，配置源仍只有 Alias 一份。
        if "fallback_model_id" in changed_updates:
            _write_fallback_policy(
                connection,
                tenant_id=context.user.tenant_id,
                model_alias_id=model_alias_id,
                enabled=changed_updates["fallback_model_id"] is not None,
                trigger_conditions=_policy_conditions(before.fallback_policy),
                actor_id=context.user.user_id,
                now=now,
            )
        routing_changed = bool(set(changed_updates) & ROUTING_ALIAS_FIELDS)
        if routing_changed:
            # Alias AFTER trigger 已推进版本；这里只补可信操作者元数据，不得再次 bump。
            connection.execute(
                """
                UPDATE model_alias_routing_policies
                SET updated_by=?, updated_at=?
                WHERE tenant_id=? AND model_alias_id=?
                """,
                (context.user.user_id, now, context.user.tenant_id, model_alias_id),
            )
        model_alias = _load_model_alias(connection, context.user.tenant_id, model_alias_id)
        if routing_changed:
            audit_record = record_audit_log(
                connection,
                context,
                action="routing_policy.updated",
                object_type="routing_policy",
                object_id=_require_routing_policy(model_alias).routing_policy_id,
                object_name=model_alias.alias,
                before_summary=_routing_audit_summary(before),
                after_summary=_routing_audit_summary(model_alias),
                request_meta=audit_request_meta,
                request_id=normalized_request_id,
                trace_id=trace_id,
            )
            # 路由图版本没有同事务审计就不可验收，必须中止而不是仅记录告警。
            if audit_record is None:
                raise ApiError("model_alias.routing_policy_update_failed")
            changed_routing_fields = _ordered_routing_fields(set(changed_updates))
            event = _insert_routing_policy_event(
                connection,
                context,
                model_alias,
                request_id=normalized_request_id,
                trace_id=trace_id,
                fingerprint=fingerprint,
                change_kinds=_change_kinds(changed_routing_fields),
                changed_routing_fields=changed_routing_fields,
                previous_mode=before_policy.routing_mode,
                now=now,
            )
            transaction.commit(commit_audited_transaction)
        else:
            transaction.commit()
    # 所有领域异常先回滚 Alias、FallbackPolicy、audit 和配置事件的同一事务。
    except ApiError as exc:
        connection.rollback()
        # 审计模块错误折叠为公开的路由策略更新失败，避免泄漏内部所有者边界。
        if exc.code == "audit_log.record_unavailable":
            raise ApiError("model_alias.routing_policy_update_failed") from exc
        raise
    # 唯一键与 owner trigger 都可能抛 IntegrityError，只有明确 Alias 重名可细分。
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        # 重名是调用方可修正输入；其余约束失败代表当前配置图不可安全更新。
        if _is_alias_duplicate(exc):
            raise ApiError("model_alias.duplicate") from exc
        raise ApiError("model_alias.routing_policy_update_failed") from exc
    # 未分类存储异常不返回 SQL 或文件路径，只提供统一可重试领域码。
    except sqlite3.Error as exc:
        connection.rollback()
        raise ApiError("model_alias.routing_policy_update_failed") from exc
    finally:
        transaction.__exit__(None, None, None)
        if replay_to_dispatch is not None:
            _dispatch_recovered_mutation(config, context, replay_to_dispatch)

    dispatch_audit_recorded(
        config,
        context,
        audit_record,
        request_id=normalized_request_id,
        trace_id=trace_id,
    )
    if event is not None:
        _dispatch_routing_policy_event(config, event)
    action = "disabled" if disabled_event else "updated"
    _log_changed(
        context,
        model_alias,
        normalized_request_id,
        trace_id,
        action=action,
        changed_fields=sorted(changed_updates),
    )
    return ModelAliasResponse(
        model_alias=model_alias,
        request_id=normalized_request_id,
        trace_id=trace_id,
    )


def update_fallback_policy(
    config: AppConfig,
    context: AuthContext,
    model_alias_id: str,
    payload: UpdateFallbackPolicyRequest,
    *,
    audit_request_meta: AuditRequestMeta,
    request_id: str,
    trace_id: str,
) -> FallbackPolicyResponse:
    """原子修改备用模型目标与非流式 Fallback 策略。

    模型目标保存在 model_aliases，策略表只保存启停、条件和固定动作。两处写入共用
    一个 IMMEDIATE 事务，任何校验或约束失败都整体回滚，避免数据面读到半配置状态。
    """

    normalized_request_id = _normalize_request_id(request_id)
    supplied_fields = _model_fields_set(payload)
    conditions = _normalize_fallback_conditions(payload.trigger_conditions)
    expected_version = _normalize_expected_version(payload.expected_routing_policy_version)
    # 目标 ID 先按真实落库规则去空白；精确重试不能因等价文本表示产生 request conflict。
    normalized_target_intent = (
        _normalize_optional_model_id(payload.fallback_model_id)
        if "fallback_model_id" in supplied_fields
        else "__omitted__"
    )
    fingerprint = _mutation_fingerprint(
        "fallback_update",
        {
            "model_alias_id": model_alias_id,
            "expected_version": expected_version,
            "enabled": payload.enabled,
            "fallback_model_id": normalized_target_intent,
            "trigger_conditions": conditions,
        },
    )
    connection = _connect(config)
    now = _to_db_time(_utcnow())
    audit_record: AuditRecord | None = None
    event: _RoutingPolicyEventRecord | None = None
    replay_to_dispatch: _RecoveredMutation | None = None
    transaction = ResourceWriteTransaction(connection, config, context, "routing.write")
    try:
        transaction.begin()
        context = transaction.context
        recovered = _recover_mutation(
            connection,
            context,
            request_id=normalized_request_id,
            fingerprint=fingerprint,
        )
        if recovered is not None:
            transaction.verify()
            connection.rollback()
            replay_to_dispatch = recovered
            return FallbackPolicyResponse(
                model_alias=recovered.model_alias,
                fallback_policy=_require_fallback_policy(recovered.model_alias),
                request_id=normalized_request_id,
                trace_id=recovered.event.trace_id,
            )

        before = _load_model_alias(connection, context.user.tenant_id, model_alias_id)
        before_routing_policy = _require_routing_policy(before)
        # 即使本次只改 Fallback 条件，也先 CAS，防止与并发候选目标更新交错提交。
        if before_routing_policy.version != expected_version:
            raise ApiError("model_alias.routing_policy_version_conflict")
        target_model_id = before.fallback_model_id
        # 未提供目标字段时沿用旧目标，避免仅切换 enabled 状态意外清空模型绑定。
        if "fallback_model_id" in supplied_fields:
            target_model_id = normalized_target_intent

        # 显式清空目标优先于 enabled=true，不能留下启用但没有执行目标的悬空策略。
        effective_enabled = payload.enabled and target_model_id is not None
        if payload.enabled and target_model_id is None:
            raise ApiError("model_alias.fallback_invalid")
        # 主备模型相同不会产生容灾效果，还会造成重复计费，因此在写事务内直接拒绝。
        if target_model_id == before.default_model_id:
            # 对外仅返回固定业务错误码，不能暴露模型表和约束实现细节。
            raise ApiError("model_alias.fallback_invalid")
        # 只有实际存在的目标才做租户归属和 active 状态校验；清空目标允许直接禁用策略。
        if target_model_id is not None:
            _require_active_fallback_model(connection, context.user.tenant_id, target_model_id)

        # 仅在目标发生真实变化时更新别名，保证重复 PATCH 不产生无意义的更新时间漂移。
        target_changed = (
            "fallback_model_id" in supplied_fields
            and target_model_id != before.fallback_model_id
        )
        if target_changed:
            connection.execute(
                """
                UPDATE model_aliases
                SET fallback_model_id=?, updated_at=?
                WHERE tenant_id=? AND model_alias_id=?
                """,
                (target_model_id, now, context.user.tenant_id, model_alias_id),
            )
        policy = _write_fallback_policy(
            connection,
            tenant_id=context.user.tenant_id,
            model_alias_id=model_alias_id,
            enabled=effective_enabled,
            trigger_conditions=conditions,
            actor_id=context.user.user_id,
            now=now,
        )
        if target_changed:
            # Alias AFTER trigger 已完成版本 +1；补充当前认证 admin 作为可信更新者。
            connection.execute(
                """
                UPDATE model_alias_routing_policies
                SET updated_by=?, updated_at=?
                WHERE tenant_id=? AND model_alias_id=?
                """,
                (context.user.user_id, now, context.user.tenant_id, model_alias_id),
            )
        model_alias = _load_model_alias(connection, context.user.tenant_id, model_alias_id)
        if target_changed:
            audit_record = record_audit_log(
                connection,
                context,
                action="routing_policy.updated",
                object_type="routing_policy",
                object_id=_require_routing_policy(model_alias).routing_policy_id,
                object_name=model_alias.alias,
                before_summary=_routing_audit_summary(before),
                after_summary=_routing_audit_summary(model_alias),
                request_meta=audit_request_meta,
                request_id=normalized_request_id,
                trace_id=trace_id,
            )
            # 候选目标变化必须与策略审计同事务，缺少审计时禁止提交新目标。
            if audit_record is None:
                raise ApiError("model_alias.routing_policy_update_failed")
            event = _insert_routing_policy_event(
                connection,
                context,
                model_alias,
                request_id=normalized_request_id,
                trace_id=trace_id,
                fingerprint=fingerprint,
                change_kinds=["candidates"],
                changed_routing_fields=["fallback_model_id"],
                previous_mode=before_routing_policy.routing_mode,
                now=now,
            )
            transaction.commit(commit_audited_transaction)
        else:
            transaction.commit()
    # 业务校验失败必须完整回滚目标与策略两处写入，再保留原错误码交给统一异常处理器。
    except ApiError as exc:
        connection.rollback()
        # 审计服务不可用意味着候选变更不可追踪，统一按策略事务失败返回。
        if exc.code == "audit_log.record_unavailable":
            raise ApiError("model_alias.routing_policy_update_failed") from exc
        raise
    # SQLite 并发或约束异常统一收敛，禁止把 SQL、表名或数据库路径返回给控制台。
    except sqlite3.Error as exc:
        connection.rollback()
        # 固定错误码允许客户端提示重试，同时避免泄露底层异常文本。
        raise ApiError("model_alias.routing_policy_update_failed") from exc
    finally:
        transaction.__exit__(None, None, None)
        if replay_to_dispatch is not None:
            _dispatch_recovered_mutation(config, context, replay_to_dispatch)

    dispatch_audit_recorded(
        config,
        context,
        audit_record,
        request_id=normalized_request_id,
        trace_id=trace_id,
    )
    if event is not None:
        _dispatch_routing_policy_event(config, event)
    logger.info(
        "model_alias.fallback_updated",
        extra={
            "event": "model_alias.fallback_updated",
            "request_id": normalized_request_id,
            "trace_id": trace_id,
            "tenant_id": context.user.tenant_id,
            "actor_id": context.user.user_id,
            "object_id": model_alias_id,
            "payload": {
                "fallback_model_id": model_alias.fallback_model_id,
                "status": policy.status,
                "version": policy.version,
                "trigger_conditions": policy.trigger_conditions,
            },
        },
    )
    return FallbackPolicyResponse(
        model_alias=model_alias,
        fallback_policy=policy,
        request_id=normalized_request_id,
        trace_id=trace_id,
    )


def update_routing_mode(
    config: AppConfig,
    context: AuthContext,
    model_alias_id: str,
    payload: dict[str, Any],
    *,
    audit_request_meta: AuditRequestMeta,
    request_id: str,
    trace_id: str,
) -> RoutingPolicyMutationResponse:
    """按 expected_version CAS 修改 RoutingPolicy mode/质量配置并原子提交三事实。

    参数来自路由层认证后的严格 JSON reader，但领域层仍执行精确字段/类型校验，避免内部
    调用绕过 HTTP 边界。相同 mode 只有在 CAS 通过后才作为 no-op 返回。
    """

    routing_mode = payload.get("routing_mode")
    # 模式只接受共享合同中的四个小写枚举，未知值不能降级成 fixed。
    if type(routing_mode) is not str or routing_mode not in ROUTING_MODES:
        raise ApiError("model_alias.routing_mode_invalid")
    expected_fields = {"routing_mode", "expected_version"}
    quality_config: QualityCostConfigInfo | None = None
    if routing_mode == "quality_cost":
        expected_fields.add("quality_cost_config")
        quality_config = _normalize_quality_cost_config(payload.get("quality_cost_config"))
    # 旧模式必须保持 PRD-047 两字段 wire；质量模式必须完整提交配置，不能静默合并缺字段。
    if set(payload) != expected_fields:
        raise ApiError("model_alias.routing_mode_invalid")
    expected_version = _normalize_expected_version(payload.get("expected_version"))
    normalized_request_id = _normalize_request_id(request_id)
    fingerprint = _mutation_fingerprint(
        "routing_mode_update",
        {
            "model_alias_id": model_alias_id,
            "routing_mode": routing_mode,
            "expected_version": expected_version,
            **(
                {"quality_cost_config": quality_config.model_dump()}
                if quality_config is not None
                else {}
            ),
        },
    )
    connection = _connect(config)
    now = _to_db_time(_utcnow())
    audit_record: AuditRecord | None = None
    event: _RoutingPolicyEventRecord | None = None
    replay_to_dispatch: _RecoveredMutation | None = None
    transaction = ResourceWriteTransaction(connection, config, context, "routing.write")
    try:
        transaction.begin()
        context = transaction.context
        recovered = _recover_mutation(
            connection,
            context,
            request_id=normalized_request_id,
            fingerprint=fingerprint,
        )
        if recovered is not None:
            transaction.verify()
            connection.rollback()
            replay_to_dispatch = recovered
            return RoutingPolicyMutationResponse(
                routing_policy=_require_routing_policy(recovered.model_alias),
                changed=True,
                request_id=normalized_request_id,
                trace_id=recovered.event.trace_id,
            )

        before = _load_model_alias(connection, context.user.tenant_id, model_alias_id)
        before_policy = _require_routing_policy(before)
        # 锁内实际版本与客户端版本不一致时立即冲突，不能先把同值更新当作幂等成功。
        if before_policy.version != expected_version:
            raise ApiError("model_alias.routing_policy_version_conflict")
        config_changed = (
            quality_config is not None
            and before_policy.quality_cost_config != quality_config
        )
        mode_changed = before_policy.routing_mode != routing_mode
        if not mode_changed and not config_changed:
            transaction.verify()
            connection.rollback()
            return RoutingPolicyMutationResponse(
                routing_policy=before_policy,
                changed=False,
                request_id=normalized_request_id,
                trace_id=trace_id,
            )

        # quality_cost 的 mode/五字段必须在一条 SQL 内共同推进一次版本；旧模式只改 mode，
        # 保留休眠配置供未来再次启用时由管理员显式确认。
        if quality_config is not None:
            cursor = connection.execute(
                """
                UPDATE model_alias_routing_policies
                SET routing_mode=?, quality_weight_bps=?, cost_weight_bps=?,
                    latency_weight_bps=?, quality_floor_bps=?, minimum_sample_count=?,
                    version=version+1, updated_by=?, updated_at=?
                WHERE tenant_id=? AND routing_policy_id=? AND version=?
                """,
                (
                    routing_mode,
                    quality_config.quality_weight_bps,
                    quality_config.cost_weight_bps,
                    quality_config.latency_weight_bps,
                    quality_config.quality_floor_bps,
                    quality_config.minimum_sample_count,
                    context.user.user_id,
                    now,
                    context.user.tenant_id,
                    before_policy.routing_policy_id,
                    expected_version,
                ),
            )
        else:
            cursor = connection.execute(
                """
                UPDATE model_alias_routing_policies
                SET routing_mode=?, version=version+1, updated_by=?, updated_at=?
                WHERE tenant_id=? AND routing_policy_id=? AND version=?
                """,
                (
                    routing_mode,
                    context.user.user_id,
                    now,
                    context.user.tenant_id,
                    before_policy.routing_policy_id,
                    expected_version,
                ),
            )
        # WHERE 未更新一行表示锁内身份或版本已漂移，必须返回 CAS 冲突而非继续写审计。
        if cursor.rowcount != 1:
            raise ApiError("model_alias.routing_policy_version_conflict")
        model_alias = _load_model_alias(connection, context.user.tenant_id, model_alias_id)
        routing_policy = _require_routing_policy(model_alias)
        audit_record = record_audit_log(
            connection,
            context,
            action="routing_policy.updated",
            object_type="routing_policy",
            object_id=routing_policy.routing_policy_id,
            object_name=model_alias.alias,
            before_summary=_routing_audit_summary(before),
            after_summary=_routing_audit_summary(model_alias),
            request_meta=audit_request_meta,
            request_id=normalized_request_id,
            trace_id=trace_id,
        )
        # 模式版本必须有同事务审计事实；空结果代表本次更新不可安全提交。
        if audit_record is None:
            raise ApiError("model_alias.routing_policy_update_failed")
        changed_routing_fields = []
        if mode_changed:
            changed_routing_fields.append("routing_mode")
        if config_changed:
            changed_routing_fields.append("quality_cost_config")
        event = _insert_routing_policy_event(
            connection,
            context,
            model_alias,
            request_id=normalized_request_id,
            trace_id=trace_id,
            fingerprint=fingerprint,
            change_kinds=_change_kinds(changed_routing_fields),
            changed_routing_fields=changed_routing_fields,
            previous_mode=before_policy.routing_mode,
            now=now,
        )
        transaction.commit(commit_audited_transaction)
    # 领域错误统一回滚模式、版本、审计和事件，避免部分提交后再转换异常。
    except ApiError as exc:
        connection.rollback()
        # 审计模块的内部错误不穿透边界，对外只说明 RoutingPolicy 更新整体失败。
        if exc.code == "audit_log.record_unavailable":
            raise ApiError("model_alias.routing_policy_update_failed") from exc
        raise
    # SQLite 锁、I/O 或 trigger 故障均禁止提交动态模式，并折叠底层细节。
    except sqlite3.Error as exc:
        connection.rollback()
        raise ApiError("model_alias.routing_policy_update_failed") from exc
    finally:
        transaction.__exit__(None, None, None)
        if replay_to_dispatch is not None:
            _dispatch_recovered_mutation(config, context, replay_to_dispatch)

    dispatch_audit_recorded(
        config,
        context,
        audit_record,
        request_id=normalized_request_id,
        trace_id=trace_id,
    )
    if event is not None:
        _dispatch_routing_policy_event(config, event)
    _log_changed(
        context,
        model_alias,
        normalized_request_id,
        trace_id,
        action="updated",
        changed_fields=changed_routing_fields,
    )
    return RoutingPolicyMutationResponse(
        routing_policy=routing_policy,
        changed=True,
        request_id=normalized_request_id,
        trace_id=trace_id,
    )


def _connect(config: AppConfig) -> sqlite3.Connection:
    """创建 模型别名路由 所需的数据库连接，并把基础设施异常映射到统一错误模型。
    
    边界：调用方只看到业务错误码，不直接暴露数据库路径、SQL 或连接细节。
    """


    try:
        return connect_database(config.database_url)
    # 数据库异常需要折叠为统一错误码并保留 request_id，避免底层细节外泄。
    except DbError as exc:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError(exc.code, message=exc.message, suggestion=exc.suggestion) from exc


def _load_model_alias(connection: sqlite3.Connection, tenant_id: str, model_alias_id: str) -> ModelAliasInfo:
    """按当前租户加载 模型别名路由 的 load model alias 目标对象，供后续写入或详情响应复用。
    
    安全：查询必须带 tenant_id 或可信上下文，避免跨租户读取。
    """


    row = connection.execute(
        """
        SELECT
          a.model_alias_id, a.tenant_id, a.alias, a.environment,
          e.environment_id AS environment_id, e.name AS environment_name, e.status AS environment_status,
          a.default_model_id,
          a.fallback_model_id, a.status, a.description, a.created_at, a.updated_at,
          a.disabled_at,
          dm.model_alias AS default_internal_model_id,
          dm.display_name AS default_display_name,
          dm.status AS default_model_status,
          dp.provider_code AS default_provider_code,
          dp.name AS default_provider_name,
          dp.status AS default_provider_status,
          fm.model_alias AS fallback_internal_model_id,
          fm.display_name AS fallback_display_name,
          fm.status AS fallback_model_status,
          fp.provider_code AS fallback_provider_code,
          fp.name AS fallback_provider_name,
          fp.status AS fallback_provider_status,
          fpol.fallback_policy_id, fpol.status AS fallback_policy_status,
          fpol.version AS fallback_policy_version, fpol.priority AS fallback_policy_priority,
          fpol.condition_expr AS fallback_condition_expr, fpol.action AS fallback_action,
          fpol.created_at AS fallback_policy_created_at, fpol.updated_at AS fallback_policy_updated_at,
          rp.routing_policy_id, rp.routing_mode,
          rp.status AS routing_policy_status, rp.version AS routing_policy_version,
          rp.quality_weight_bps, rp.cost_weight_bps, rp.latency_weight_bps,
          rp.quality_floor_bps, rp.minimum_sample_count,
          rp.created_at AS routing_policy_created_at, rp.updated_at AS routing_policy_updated_at,
          ca.cache_affinity_id, ca.status AS cache_affinity_status,
          ca.data_class AS cache_affinity_data_class,
          ca.minimum_prefix_chars AS cache_affinity_minimum_prefix_chars,
          ca.ttl_seconds AS cache_affinity_ttl_seconds,
          ca.version AS cache_affinity_version,
          ca.created_at AS cache_affinity_created_at,
          ca.updated_at AS cache_affinity_updated_at
        FROM model_aliases a
        JOIN environments e
          ON e.tenant_id=a.tenant_id
         AND (e.environment_id=a.environment_id OR (a.environment_id IS NULL AND e.code=a.environment))
        JOIN models dm ON dm.model_id = a.default_model_id AND dm.tenant_id = a.tenant_id
        JOIN providers dp ON dp.provider_id = dm.provider_id AND dp.tenant_id = dm.tenant_id
        LEFT JOIN models fm ON fm.model_id = a.fallback_model_id AND fm.tenant_id = a.tenant_id
        LEFT JOIN providers fp ON fp.provider_id = fm.provider_id AND fp.tenant_id = fm.tenant_id
        LEFT JOIN fallback_policies fpol
          ON fpol.model_alias_id=a.model_alias_id AND fpol.tenant_id=a.tenant_id
        LEFT JOIN model_alias_routing_policies rp
          ON rp.model_alias_id=a.model_alias_id AND rp.tenant_id=a.tenant_id
        LEFT JOIN cache_affinities ca
          ON ca.model_alias_id=a.model_alias_id AND ca.tenant_id=a.tenant_id
        WHERE a.tenant_id=? AND a.model_alias_id=?
        """,
        (tenant_id, model_alias_id),
    ).fetchone()
    if not row:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("model_alias.not_found")
    return _model_alias_info(row)


def _validate_target_models(
    connection: sqlite3.Connection,
    tenant_id: str,
    *,
    default_model_id: str,
    fallback_model_id: str | None,
    require_active: bool,
) -> None:
    """在 模型别名路由 内部复用 validate target models 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    default_model = _require_model(connection, tenant_id, default_model_id)
    if require_active and default_model["status"] != "active":
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("model_alias.model_invalid")
    if fallback_model_id is None:
        return
    # 模型路由结果决定上游调用，异常时必须用统一错误码停止。
    if fallback_model_id == default_model_id:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("model_alias.model_invalid")
    fallback_model = _require_model(connection, tenant_id, fallback_model_id)
    if require_active and fallback_model["status"] != "active":
        # 路由和模型判断决定实际上游调用，必须保持可解释和可追踪。
        raise ApiError("model_alias.model_invalid")


def _require_model(connection: sqlite3.Connection, tenant_id: str, model_id: str) -> sqlite3.Row:
    """按当前租户加载 模型别名路由 的 require model 目标对象，供后续写入或详情响应复用。
    
    安全：查询必须带 tenant_id 或可信上下文，避免跨租户读取。
    """


    row = connection.execute(
        """
        SELECT model_id, tenant_id, status
        FROM models
        WHERE tenant_id=? AND model_id=?
        """,
        (tenant_id, model_id),
    ).fetchone()
    if not row:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("model_alias.model_invalid")
    return row


def _require_active_fallback_model(
    connection: sqlite3.Connection,
    tenant_id: str,
    model_id: str,
) -> sqlite3.Row:
    """校验备用模型属于当前租户且处于 active 状态。

    不存在和跨租户统一返回 404，避免通过模型 ID 枚举其他租户；当前租户内已禁用模型
    返回 409，便于管理员先恢复模型再启用 Fallback。
    """

    row = connection.execute(
        "SELECT model_id, status FROM models WHERE tenant_id=? AND model_id=?",
        (tenant_id, model_id),
    ).fetchone()
    if row is None:
        # 不区分不存在与跨租户目标，避免攻击者通过 ID 探测其他租户模型。
        raise ApiError("model_alias.fallback_model_not_found")
    if row["status"] != "active":
        # 已停用模型不可成为运行时路由目标，管理员需先恢复模型状态。
        raise ApiError("model_alias.fallback_model_disabled")
    return row


def _write_fallback_policy(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    model_alias_id: str,
    enabled: bool,
    trigger_conditions: list[str],
    actor_id: str,
    now: str,
) -> FallbackPolicyInfo:
    """在调用方事务内幂等创建或更新一条标准 Fallback 策略。

    完全相同的 PATCH 不递增版本；状态、条件或固定动作任一变化才生成新版本，
    routing_attempts 保存该版本后即可解释一次历史调用使用了哪份策略。
    """

    normalized_conditions = _normalize_fallback_conditions(trigger_conditions)
    condition_json = json.dumps(normalized_conditions, ensure_ascii=False, separators=(",", ":"))
    action_json = json.dumps(FALLBACK_ACTION, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    target_status = "enabled" if enabled else "disabled"
    row = connection.execute(
        "SELECT * FROM fallback_policies WHERE tenant_id=? AND model_alias_id=?",
        (tenant_id, model_alias_id),
    ).fetchone()
    if row is None:
        policy_id = f"fbp_{uuid4().hex}"
        connection.execute(
            """
            INSERT INTO fallback_policies(
              fallback_policy_id, tenant_id, model_alias_id, status, version, priority,
              condition_expr, action, created_by, updated_by, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, 1, 100, ?, ?, ?, ?, ?, ?)
            """,
            (
                policy_id,
                tenant_id,
                model_alias_id,
                target_status,
                condition_json,
                action_json,
                actor_id,
                actor_id,
                now,
                now,
            ),
        )
    elif (
        row["status"] != target_status
        or row["condition_expr"] != condition_json
        or row["action"] != action_json
    ):
        connection.execute(
            """
            UPDATE fallback_policies
            SET status=?, version=version + 1, condition_expr=?, action=?, updated_by=?, updated_at=?
            WHERE tenant_id=? AND model_alias_id=?
            """,
            (
                target_status,
                condition_json,
                action_json,
                actor_id,
                now,
                tenant_id,
                model_alias_id,
            ),
        )
    current = connection.execute(
        "SELECT * FROM fallback_policies WHERE tenant_id=? AND model_alias_id=?",
        (tenant_id, model_alias_id),
    ).fetchone()
    if current is None:
        # UPSERT 后仍无法回读表示事务状态损坏，必须抛出并由调用方整体回滚。
        raise sqlite3.IntegrityError("fallback_policy_state_missing")
    return _fallback_policy_info(current)


def _normalize_fallback_conditions(value: list[str] | None) -> list[str]:
    """把触发条件收敛为固定顺序的非空白名单。

    禁止重复条件和任意表达式，确保运行时只比较枚举，不执行用户输入代码。
    """

    raw = list(FALLBACK_CONDITION_ORDER) if value is None else [str(item).strip() for item in value]
    if not raw or len(raw) != len(set(raw)) or any(item not in FALLBACK_CONDITIONS for item in raw):
        # 空列表、重复项和白名单外表达式都不可执行，统一返回策略参数错误。
        raise ApiError("model_alias.fallback_invalid")
    return [condition for condition in FALLBACK_CONDITION_ORDER if condition in raw]


def _fallback_policy_info(row: sqlite3.Row) -> FallbackPolicyInfo:
    """把策略数据库行转换为不含原始表达式的稳定 API DTO。"""

    try:
        conditions = json.loads(str(row["condition_expr"]))
        action = json.loads(str(row["action"]))
    # 存量策略 JSON 损坏时拒绝下发，防止数据面基于不完整条件执行错误切换。
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        # 返回固定可重试错误，不把原始 JSON 或解析异常暴露给调用者。
        raise ApiError("model_alias.fallback_update_unavailable") from exc
    normalized_conditions = _normalize_fallback_conditions(conditions if isinstance(conditions, list) else [])
    max_attempts = int(action.get("max_attempts", 1)) if isinstance(action, dict) else 1
    return FallbackPolicyInfo(
        fallback_policy_id=str(row["fallback_policy_id"]),
        model_alias_id=str(row["model_alias_id"]),
        status=str(row["status"]),
        version=int(row["version"]),
        priority=int(row["priority"]),
        trigger_conditions=normalized_conditions,
        max_attempts=max_attempts,
        created_at=_parse_db_time(row["created_at"]),
        updated_at=_parse_db_time(row["updated_at"]),
    )


def _policy_conditions(policy: FallbackPolicyInfo | None) -> list[str]:
    """兼容通用模型别名 PATCH：沿用现有条件，无策略时回落到三个标准条件。"""

    return list(policy.trigger_conditions) if policy is not None else list(FALLBACK_CONDITION_ORDER)


def _model_alias_info(row: sqlite3.Row) -> ModelAliasInfo:
    """在 模型别名路由 内部复用 model alias info 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    policy = None
    if row["fallback_policy_id"] is not None:
        policy = FallbackPolicyInfo(
            fallback_policy_id=str(row["fallback_policy_id"]),
            model_alias_id=str(row["model_alias_id"]),
            status=str(row["fallback_policy_status"]),
            version=int(row["fallback_policy_version"]),
            priority=int(row["fallback_policy_priority"]),
            trigger_conditions=_normalize_fallback_conditions(json.loads(str(row["fallback_condition_expr"]))),
            max_attempts=int(json.loads(str(row["fallback_action"])).get("max_attempts", 1)),
            created_at=_parse_db_time(row["fallback_policy_created_at"]),
            updated_at=_parse_db_time(row["fallback_policy_updated_at"]),
        )
    candidates = [
        RoutingPolicyCandidateInfo(
            candidate_role="default",
            configured_priority=0,
            model_id=str(row["default_model_id"]),
            internal_model_id=str(row["default_internal_model_id"]),
            display_name=row["default_display_name"],
            model_status=str(row["default_model_status"]),
            provider_code=str(row["default_provider_code"]),
            provider_name=str(row["default_provider_name"]),
            provider_status=str(row["default_provider_status"]),
        )
    ]
    # 只有真实配置备用模型时才展开第二候选，不能用空占位伪造可用 Provider。
    if row["fallback_model_id"] is not None:
        # fallback 只有完整同租户 Model/Provider JOIN 成功时才进入安全配置摘要；迁移 owner
        # trigger 会阻止新坏图，历史坏图则由查询失败关闭而不是伪造 Provider 信息。
        candidates.append(
            RoutingPolicyCandidateInfo(
                candidate_role="fallback",
                configured_priority=1,
                model_id=str(row["fallback_model_id"]),
                internal_model_id=str(row["fallback_internal_model_id"]),
                display_name=row["fallback_display_name"],
                model_status=str(row["fallback_model_status"]),
                provider_code=str(row["fallback_provider_code"]),
                provider_name=str(row["fallback_provider_name"]),
                provider_status=str(row["fallback_provider_status"]),
            )
        )
    routing_policy = None
    if row["routing_policy_id"] is not None:
        routing_policy = RoutingPolicyInfo(
            routing_policy_id=str(row["routing_policy_id"]),
            tenant_id=str(row["tenant_id"]),
            model_alias_id=str(row["model_alias_id"]),
            routing_mode=str(row["routing_mode"]),
            quality_cost_config=QualityCostConfigInfo(
                quality_weight_bps=int(row["quality_weight_bps"]),
                cost_weight_bps=int(row["cost_weight_bps"]),
                latency_weight_bps=int(row["latency_weight_bps"]),
                quality_floor_bps=int(row["quality_floor_bps"]),
                minimum_sample_count=int(row["minimum_sample_count"]),
            ),
            status=str(row["routing_policy_status"]),
            version=int(row["routing_policy_version"]),
            candidates=candidates,
            warnings=[],
            created_at=_parse_db_time(row["routing_policy_created_at"]),
            updated_at=_parse_db_time(row["routing_policy_updated_at"]),
        )
    cache_affinity = None
    if row["cache_affinity_id"] is not None:
        # Alias GET 只展开静态配置；运行 Selection、Binding 和任何摘要都不进入该 DTO。
        cache_affinity = CacheAffinityInfo(
            cache_affinity_id=str(row["cache_affinity_id"]),
            tenant_id=str(row["tenant_id"]),
            model_alias_id=str(row["model_alias_id"]),
            status=str(row["cache_affinity_status"]),
            data_class=row["cache_affinity_data_class"],
            minimum_prefix_chars=int(row["cache_affinity_minimum_prefix_chars"]),
            ttl_seconds=int(row["cache_affinity_ttl_seconds"]),
            version=int(row["cache_affinity_version"]),
            created_at=_parse_db_time(row["cache_affinity_created_at"]),
            updated_at=_parse_db_time(row["cache_affinity_updated_at"]),
        )
    return ModelAliasInfo(
        model_alias_id=row["model_alias_id"],
        tenant_id=row["tenant_id"],
        alias=row["alias"],
        environment=row["environment"],
        environment_id=row["environment_id"],
        environment_name=row["environment_name"],
        environment_status=row["environment_status"],
        default_model_id=row["default_model_id"],
        default_internal_model_id=row["default_internal_model_id"],
        default_display_name=row["default_display_name"],
        default_model_status=row["default_model_status"],
        default_provider_code=row["default_provider_code"],
        default_provider_name=row["default_provider_name"],
        default_provider_status=row["default_provider_status"],
        fallback_model_id=row["fallback_model_id"],
        fallback_internal_model_id=row["fallback_internal_model_id"],
        fallback_display_name=row["fallback_display_name"],
        fallback_provider_code=row["fallback_provider_code"],
        fallback_provider_name=row["fallback_provider_name"],
        fallback_policy=policy,
        cache_affinity=cache_affinity,
        routing_policy=routing_policy,
        effective_routing_mode=(routing_policy.routing_mode if routing_policy else "fixed"),
        routing_policy_implicit=routing_policy is None,
        status=row["status"],
        description=row["description"],
        created_at=_parse_db_time(row["created_at"]),
        updated_at=_parse_db_time(row["updated_at"]),
        disabled_at=_parse_optional_db_time(row["disabled_at"]),
    )


def _require_routing_policy(model_alias: ModelAliasInfo) -> RoutingPolicyInfo:
    """写路径要求 Alias 已有持久 RoutingPolicy，缺行时失败关闭而不合成虚假 v1。"""

    # 写路径缺少持久策略时失败关闭，禁止临时合成 fixed v1 掩盖迁移损坏。
    if model_alias.routing_policy is None:
        raise ApiError("model_alias.routing_policy_update_failed")
    return model_alias.routing_policy


def _require_fallback_policy(model_alias: ModelAliasInfo) -> FallbackPolicyInfo:
    """恢复 Fallback PATCH 时要求既有策略事实仍存在，缺失表示数据库状态不可确认。"""

    # 已提交重试若找不到配套 FallbackPolicy，说明三事实不完整，不能恢复伪响应。
    if model_alias.fallback_policy is None:
        raise ApiError("model_alias.routing_policy_update_failed")
    return model_alias.fallback_policy


def _normalize_expected_version(value: object) -> int:
    """把 CAS 版本限制为 SQLite 正整数，并显式拒绝 bool 借用 int 继承关系。"""

    # bool 虽继承 int 也必须拒绝，版本上限与 SQLite 有符号整数保持一致。
    if type(value) is not int or value < 1 or value > 9_223_372_036_854_775_807:
        raise ApiError("model_alias.routing_policy_version_invalid")
    return value


def _normalize_quality_cost_config(value: object) -> QualityCostConfigInfo:
    """严格校验质量/成本/延迟配置的完整字段、整数类型和成组约束。"""

    # 配置必须一次提交完整白名单；接受半组或额外字段会让不同版本客户端形成不同执行语义。
    if not isinstance(value, dict) or set(value) != QUALITY_COST_CONFIG_FIELDS:
        raise ApiError("model_alias.quality_cost_settings_invalid")
    normalized: dict[str, int] = {}
    for field in QUALITY_COST_CONFIG_FIELDS:
        item = value.get(field)
        # bool 继承 int 但不能作为权重、门槛或样本数进入线上行为开关。
        if type(item) is not int:
            raise ApiError("model_alias.quality_cost_settings_invalid")
        normalized[field] = item
    quality_weight = normalized["quality_weight_bps"]
    cost_weight = normalized["cost_weight_bps"]
    latency_weight = normalized["latency_weight_bps"]
    floor = normalized["quality_floor_bps"]
    minimum_samples = normalized["minimum_sample_count"]
    if (
        not 1 <= quality_weight <= 10_000
        or not 0 <= cost_weight <= 10_000
        or not 0 <= latency_weight <= 10_000
        or quality_weight + cost_weight + latency_weight != 10_000
        or not 1 <= floor <= 10_000
        or not 1 <= minimum_samples <= MAX_QUALITY_SAMPLE_COUNT
    ):
        # 不自动归一权重，否则客户端提交的配置意图与持久审计会发生漂移。
        raise ApiError("model_alias.quality_cost_settings_invalid")
    return QualityCostConfigInfo(**normalized)


def _normalize_request_id(value: str) -> str:
    """校验配置事件 exactly-once 键，禁止空值、控制字符和超长链路标识入库。"""

    normalized = str(value or "").strip()
    # request_id 是 exactly-once 唯一键，空白、控制字符或超长值会破坏审计恢复。
    if (
        not normalized
        or len(normalized) > 128
        or any(character.isspace() or ord(character) < 32 for character in normalized)
    ):
        # 非法链路键不能进入唯一事件表，也不能被裁剪后与另一请求发生碰撞。
        raise ApiError("model_alias.routing_policy_update_failed")
    return normalized


def _description_hash(value: object) -> str | None:
    """只把描述的 SHA-256 放入幂等指纹，避免低频配置事件保存描述明文。"""

    if value is None:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _mutation_fingerprint(operation: str, values: dict[str, object]) -> str:
    """生成绑定 path/版本/规范化安全意图的稳定 SHA-256 exactly-once 指纹。"""

    canonical = json.dumps(
        {"operation": operation, "values": values},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _routing_audit_summary(model_alias: ModelAliasInfo) -> dict[str, object]:
    """构造 RoutingPolicy 审计白名单快照，不复制描述、价格、Endpoint 或 KeyPool。"""

    policy = _require_routing_policy(model_alias)
    return {
        "model_alias_id": model_alias.model_alias_id,
        "alias": model_alias.alias,
        "environment": model_alias.environment,
        "status": model_alias.status,
        "default_model_id": model_alias.default_model_id,
        "fallback_model_id": model_alias.fallback_model_id,
        "routing_mode": policy.routing_mode,
        "quality_weight_bps": policy.quality_cost_config.quality_weight_bps,
        "cost_weight_bps": policy.quality_cost_config.cost_weight_bps,
        "latency_weight_bps": policy.quality_cost_config.latency_weight_bps,
        "quality_floor_bps": policy.quality_cost_config.quality_floor_bps,
        "minimum_sample_count": policy.quality_cost_config.minimum_sample_count,
        "version": policy.version,
    }


def _ordered_routing_fields(fields: set[str]) -> list[str]:
    """按 Webhook 合同固定顺序输出真实变化字段，避免集合遍历导致签名正文漂移。"""

    order = (
        "alias",
        "environment",
        "environment_id",
        "status",
        "default_model_id",
        "fallback_model_id",
        "routing_mode",
        "quality_cost_config",
    )
    return [field for field in order if field in fields]


def _change_kinds(changed_fields: list[str]) -> list[str]:
    """把字段变化折叠为有序事件类别，保持 created/mode/candidates/alias_state 合同。"""

    changed = set(changed_fields)
    result: list[str] = []
    if "routing_mode" in changed:
        result.append("mode")
    # 五个质量字段对外折叠为一个稳定类别，避免事件消费者依赖内部配置拆分顺序。
    if "quality_cost_config" in changed:
        result.append("quality_cost")
    # 主备目标任一变化都属于候选图变化，Webhook 只暴露固定类别而非模型详情。
    if changed & {"default_model_id", "fallback_model_id"}:
        result.append("candidates")
    if changed & {"alias", "environment", "environment_id", "status"}:
        result.append("alias_state")
    # 没有可解释类别表示调用方传入了非路由字段，不能创建含糊配置事件。
    if not result:
        raise ApiError("model_alias.routing_policy_update_failed")
    return result


def _insert_routing_policy_event(
    connection: sqlite3.Connection,
    context: AuthContext,
    model_alias: ModelAliasInfo,
    *,
    request_id: str,
    trace_id: str,
    fingerprint: str,
    change_kinds: list[str],
    changed_routing_fields: list[str],
    previous_mode: str | None,
    now: str,
) -> _RoutingPolicyEventRecord:
    """在当前 Alias 事务内追加配置事件，并从持久行回读稳定 Webhook 事实。"""

    policy = _require_routing_policy(model_alias)
    event_id = f"rpe_{uuid4().hex}"
    payload = {
        "trigger": "configuration_changed",
        "request_id": request_id,
        "model_alias_id": model_alias.model_alias_id,
        "routing_policy_id": policy.routing_policy_id,
        "routing_policy_version": policy.version,
        "configuration_schema_version": 2,
        "change_kinds": change_kinds,
        "changed_routing_fields": changed_routing_fields,
        "previous_mode": previous_mode,
        "routing_mode": policy.routing_mode,
        "quality_cost_config": policy.quality_cost_config.model_dump(),
    }
    connection.execute(
        """
        INSERT INTO routing_policy_events(
          routing_policy_event_id, tenant_id, routing_policy_id, model_alias_id,
          routing_policy_version, payload_schema_version, event_type, trigger_type, actor_user_id,
          request_id, trace_id, request_fingerprint, payload_json, created_at
        )
        VALUES(?, ?, ?, ?, ?, 2, 'route.policy_selected', 'configuration_changed',
               ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            context.user.tenant_id,
            policy.routing_policy_id,
            model_alias.model_alias_id,
            policy.version,
            context.user.user_id,
            request_id,
            trace_id,
            fingerprint,
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            now,
        ),
    )
    row = connection.execute(
        "SELECT * FROM routing_policy_events WHERE tenant_id=? AND routing_policy_event_id=?",
        (context.user.tenant_id, event_id),
    ).fetchone()
    # INSERT 后必须回读同租户权威行，缺失时禁止构造只存在内存中的 Webhook。
    if row is None:
        raise ApiError("model_alias.routing_policy_update_failed")
    return _routing_policy_event_record(row)


def _routing_policy_event_record(row: sqlite3.Row) -> _RoutingPolicyEventRecord:
    """把事件数据库行解析为安全对象；JSON 损坏必须失败关闭，不能回显原始字符串。"""

    try:
        payload = json.loads(str(row["payload_json"]))
    # 持久 payload 损坏时只返回稳定错误，不把原 JSON 或解析器消息带入日志。
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ApiError("model_alias.routing_policy_update_failed") from exc
    # 事件正文必须保持精确对象形状，数组或标量不能进入 Webhook 签名合同。
    if not isinstance(payload, dict):
        raise ApiError("model_alias.routing_policy_update_failed")
    return _RoutingPolicyEventRecord(
        routing_policy_event_id=str(row["routing_policy_event_id"]),
        tenant_id=str(row["tenant_id"]),
        routing_policy_id=str(row["routing_policy_id"]),
        model_alias_id=str(row["model_alias_id"]),
        routing_policy_version=int(row["routing_policy_version"]),
        payload_schema_version=int(row["payload_schema_version"]),
        actor_user_id=str(row["actor_user_id"]),
        request_id=str(row["request_id"]),
        trace_id=str(row["trace_id"]),
        request_fingerprint=str(row["request_fingerprint"]),
        payload=payload,
        created_at=_parse_db_time(row["created_at"]),
    )


def _recover_mutation(
    connection: sqlite3.Connection,
    context: AuthContext,
    *,
    request_id: str,
    fingerprint: str,
) -> _RecoveredMutation | None:
    """在写锁内按 tenant/actor/request_id 恢复已提交配置事实并核对当前资源版本。

    不同 fingerprint 必须 409；当前 Policy 已继续演进时也不能把新状态伪装成旧请求响应。
    """

    row = connection.execute(
        """
        SELECT *
        FROM routing_policy_events
        WHERE tenant_id=? AND actor_user_id=? AND request_id=?
        """,
        (context.user.tenant_id, context.user.user_id, request_id),
    ).fetchone()
    if row is None:
        return None
    event = _routing_policy_event_record(row)
    # 同一 actor/request_id 的不同规范化意图必须 409，避免把旧提交冒充新请求。
    if event.request_fingerprint != fingerprint:
        raise ApiError("model_alias.routing_policy_request_conflict")
    model_alias = _load_model_alias(connection, context.user.tenant_id, event.model_alias_id)
    policy = _require_routing_policy(model_alias)
    # 当前策略继续演进后不能回放旧版本响应，否则客户端会误认为旧版本仍是权威状态。
    if (
        policy.routing_policy_id != event.routing_policy_id
        or policy.version != event.routing_policy_version
        or policy.routing_mode != event.payload.get("routing_mode")
    ):
        # 身份、版本或模式任一漂移都说明旧响应已失效，必须返回稳定请求冲突。
        raise ApiError("model_alias.routing_policy_request_conflict")
    event_config = event.payload.get("quality_cost_config")
    if event_config is not None:
        # v2 恢复还必须核对完整休眠/执行配置；自动统计变化与此 CAS 无关。
        try:
            normalized_event_config = _normalize_quality_cost_config(event_config)
        except ApiError as exc:
            # 已提交事件若不再满足 v2 wire 合同即表示历史事实损坏，不能返回原校验码误导重试。
            raise ApiError("model_alias.routing_policy_update_failed") from exc
        # 当前休眠配置继续变化后，旧 request_id 响应已不再代表权威 Policy，必须拒绝回放。
        if policy.quality_cost_config != normalized_event_config:
            raise ApiError("model_alias.routing_policy_request_conflict")
    audit = _load_routing_audit_record(connection, context, event)
    return _RecoveredMutation(model_alias=model_alias, event=event, audit=audit)


def _load_routing_audit_record(
    connection: sqlite3.Connection,
    context: AuthContext,
    event: _RoutingPolicyEventRecord,
) -> AuditRecord:
    """加载与配置事件同事务提交的唯一审计事实，供精确重试幂等重派。"""

    rows = connection.execute(
        """
        SELECT audit_log_id, tenant_id, actor_user_id, action, object_type,
               object_id, changed_fields_json, trace_id, created_at
        FROM audit_logs
        WHERE tenant_id=? AND actor_user_id=? AND request_id=?
          AND object_type='routing_policy' AND object_id=?
          AND action IN ('routing_policy.created', 'routing_policy.updated')
        ORDER BY created_at, audit_log_id
        """,
        (
            context.user.tenant_id,
            context.user.user_id,
            event.request_id,
            event.routing_policy_id,
        ),
    ).fetchall()
    # 配置事件必须恰好对应一条同事务审计，多行或缺行都表示 exactly-once 事实损坏。
    if len(rows) != 1:
        raise ApiError("model_alias.routing_policy_update_failed")
    row = rows[0]
    try:
        changed_fields = json.loads(str(row["changed_fields_json"]))
    # 审计 JSON 损坏时不得回显正文，只能中止恢复并返回统一策略错误。
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ApiError("model_alias.routing_policy_update_failed") from exc
    # changed_fields 只接受字符串数组，阻止嵌套对象或敏感值进入重派摘要。
    if not isinstance(changed_fields, list) or not all(
        isinstance(item, str) for item in changed_fields
    ):
        # 非字符串数组可能夹带敏感嵌套值，恢复流程必须失败关闭且不回显原 JSON。
        raise ApiError("model_alias.routing_policy_update_failed")
    return AuditRecord(
        audit_log_id=str(row["audit_log_id"]),
        tenant_id=str(row["tenant_id"]),
        actor_user_id=str(row["actor_user_id"]),
        action=str(row["action"]),
        object_type=str(row["object_type"]),
        object_id=str(row["object_id"]),
        changed_fields=tuple(changed_fields),
        trace_id=str(row["trace_id"]),
        created_at=_parse_db_time(row["created_at"]),
    )


def _dispatch_recovered_mutation(
    config: AppConfig,
    context: AuthContext,
    recovered: _RecoveredMutation,
) -> None:
    """重派精确恢复的 audit/config Webhook；稳定 event ID 会阻止重复网络投递。"""

    dispatch_audit_recorded(
        config,
        context,
        recovered.audit,
        request_id=recovered.event.request_id,
        trace_id=recovered.event.trace_id,
    )
    _dispatch_routing_policy_event(config, recovered.event)


def _dispatch_routing_policy_event(
    config: AppConfig,
    event: _RoutingPolicyEventRecord,
) -> None:
    """从已提交配置事件构造稳定 Webhook，外部故障只记录脱敏定位摘要。"""

    try:
        webhook_event = build_webhook_event(
            tenant_id=event.tenant_id,
            event_type="route.policy_selected",
            event_id=event.routing_policy_event_id,
            timestamp=event.created_at,
            actor_id=event.actor_user_id,
            object_id=event.routing_policy_id,
            trace_id=event.trace_id,
            payload=event.payload,
        )
        # 延迟导入避免 ModelAlias 与 Webhook 服务在应用启动时形成循环依赖。
        from metahub_control.webhooks.service import dispatch_webhook_event

        dispatch_webhook_event(
            config,
            event.tenant_id,
            webhook_event,
            request_id=event.request_id,
            trace_id=event.trace_id,
        )
    except Exception:
        # 外部异常可能包含 URL/SQL/接收端正文；日志只保留稳定 ID 和固定错误码。
        logger.error(
            "routing_policy.webhook_dispatch_failed",
            extra={
                "event": "routing_policy.webhook_dispatch_failed",
                "tenant_id": event.tenant_id,
                "actor_id": event.actor_user_id,
                "object_id": event.routing_policy_id,
                "request_id": event.request_id,
                "trace_id": event.trace_id,
                "error_code": "webhook.delivery_unavailable",
            },
        )


def _is_alias_duplicate(exc: sqlite3.IntegrityError) -> bool:
    """只把 Alias tenant/environment 唯一冲突公开为 duplicate，其他约束细节统一脱敏。"""

    message = str(exc)
    return (
        "model_aliases.tenant_id, model_aliases.alias, model_aliases.environment" in message
        or "model_aliases.tenant_id, model_aliases.environment_id, model_aliases.alias" in message
    )


def _normalize_alias(value: str | None) -> str:
    """规范化 模型别名路由 的 normalize alias 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    alias = (value or "").strip().lower()
    if not ALIAS_PATTERN.match(alias):
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("model_alias.alias_invalid")
    return alias


def _normalize_environment(value: str | None) -> str:
    """规范化 模型别名路由 的 normalize environment 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    environment = (value or "dev").strip().lower()
    if environment not in VALID_ENVIRONMENTS:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("model_alias.environment_invalid")
    return environment


def _normalize_optional_environment(value: str | None) -> str | None:
    """规范化 模型别名路由 的 normalize optional environment 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    if value is None or value == "":
        return None
    return _normalize_environment(value)


def _normalize_status(value: str | None) -> str:
    """规范化 模型别名路由 的 normalize status 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    status = (value or "active").strip().lower()
    if status not in VALID_STATUSES:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("model_alias.status_invalid")
    return status


def _normalize_optional_status(value: str | None) -> str | None:
    """规范化 模型别名路由 的 normalize optional status 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    if value is None or value == "":
        return None
    return _normalize_status(value)


def _normalize_model_id(value: str | None) -> str:
    """规范化 模型别名路由 的 normalize model id 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    model_id = (value or "").strip()
    # 模型路由结果决定上游调用，异常时必须用统一错误码停止。
    if not model_id or len(model_id) > 80:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("model_alias.model_invalid")
    return model_id


def _normalize_optional_model_id(value: str | None) -> str | None:
    """规范化 模型别名路由 的 normalize optional model id 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    if value is None:
        return None
    model_id = value.strip()
    # 路由和模型判断决定实际上游调用，必须保持可解释和可追踪。
    if not model_id:
        return None
    if len(model_id) > 80:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("model_alias.model_invalid")
    return model_id


def _normalize_description(value: str | None) -> str | None:
    """规范化 模型别名路由 的 normalize description 参数，拦截空值、非法枚举或越界数字。
    
    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """


    if value is None:
        return None
    description = value.strip()
    if len(description) > MAX_DESCRIPTION_LENGTH:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("model_alias.create_invalid")
    return description or None


def _parse_optional_db_time(value: object) -> datetime | None:
    """解析 模型别名路由 的 parse optional db time 数据，把数据库或外部输入转换为内部稳定类型。
    
    边界：解析失败要么返回空值口径，要么抛统一错误，不能把底层异常透出给 API。
    """


    if not value:
        return None
    return _parse_db_time(value)


def _parse_db_time(value: object) -> datetime:
    """解析 模型别名路由 的 parse db time 数据，把数据库或外部输入转换为内部稳定类型。
    
    边界：解析失败要么返回空值口径，要么抛统一错误，不能把底层异常透出给 API。
    """


    raw = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _to_db_time(value: datetime) -> str:
    """把 模型别名路由 的 UTC 时间序列化为数据库 ISO 字符串。
    
    边界：统一时间口径可避免分页、审计和测试在时区上产生歧义。
    """


    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utcnow() -> datetime:
    """返回 模型别名路由 使用的当前 UTC 时间，去掉微秒以保持数据库字段稳定。
    
    边界：所有状态更新时间共用该口径，避免同一事务内出现混合时间格式。
    """


    return datetime.now(UTC).replace(microsecond=0)


def _supplied_fields(payload: UpdateModelAliasRequest) -> set[str]:
    """在 模型别名路由 内部复用 supplied fields 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    return _model_fields_set(payload)


def _model_fields_set(payload: object) -> set[str]:
    """兼容 Pydantic v1/v2 读取显式提交字段，区分省略和显式 null。"""

    fields = getattr(payload, "model_fields_set", None)
    if fields is None:
        fields = getattr(payload, "__fields_set__", set())
    return set(fields)


def _create_fields() -> tuple[str, ...]:
    """在 模型别名路由 内部复用 create fields 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    return (
        "alias",
        "environment_id",
        "environment",
        "default_model_id",
        "fallback_model_id",
        "description",
        "status",
    )


def _log_changed(
    context: AuthContext,
    model_alias: ModelAliasInfo,
    request_id: str,
    trace_id: str,
    *,
    action: str,
    changed_fields: list[str],
) -> None:
    """在 模型别名路由 内部复用 log changed 辅助逻辑，集中处理边界值和错误映射。
    
    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """


    event = f"model_alias.{action}" if action in {"created", "disabled"} else "model_alias.updated"
    logger.info(
        event,
        extra={
            "event": event,
            "request_id": request_id,
            "trace_id": trace_id,
            "tenant_id": context.user.tenant_id,
            "actor_id": context.user.user_id,
            "object_id": model_alias.model_alias_id,
            "payload": {
                "action": action,
                "alias": model_alias.alias,
                "environment": model_alias.environment,
                "default_model_id": model_alias.default_model_id,
                "fallback_model_id": model_alias.fallback_model_id,
                "status": model_alias.status,
                "changed_fields": changed_fields,
            },
        },
    )
