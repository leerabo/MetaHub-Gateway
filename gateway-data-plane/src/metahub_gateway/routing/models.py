"""模块职责：说明本文件在 MetaHub 企业级 AI Gateway 中承担的接口、服务或测试职责。

维护边界：关键判断必须保留 request_id、tenant_id、权限校验、用量追踪和敏感信息脱敏语义。
Provider Key 与 Token 不得明文落日志。
来源文件：gateway-data-plane/src/metahub_gateway/routing/models.py
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.routing_policy import (
    MAX_ROUTING_CANDIDATES,
    REASON_STANDBY,
    QualityCostConfig,
    RoutingCandidateInput,
    RoutingPriceFact,
    RoutingQualityFact,
    RoutingSelectionResult,
    TokenEstimate,
    estimate_request_tokens,
    select_routing_candidates,
)
from metahub_shared.runtime.webhooks import WebhookEvent, build_webhook_event
from metahub_shared.runtime.workspace_fx import resolve_workspace_fx_rate

from metahub_gateway.cache_affinity.models import CacheAffinityRouteContext
from metahub_gateway.pricing import resolve_workspace_cost_rate
from metahub_gateway.routing.cooldown import RouteKey, route_cooldowns, scoped_tenant

logger = logging.getLogger("metahub.gateway.routing")

# Fallback 条件是持久快照的机器合同；固定顺序同时服务去重校验和确定性审计解释。
_FALLBACK_CONDITION_ORDER = (
    "upstream_timeout",
    "upstream_unavailable",
    "upstream_rate_limited",
    "upstream_quota_exceeded",
    "upstream_5xx",
    "upstream_any_error",
)


@dataclass(frozen=True)
class GatewayRoute:
    """承载 模型目录 的 GatewayRoute 数据结构，明确跨模块传递时允许出现的字段。

    边界：该结构只保存脱敏业务字段，不应加入明文密钥、Token 或 Prompt 内容。
    """

    tenant_id: str
    model_id: str
    model_alias: str
    upstream_model_name: str = field(repr=False)
    capabilities: dict[str, object]
    provider_id: str
    provider_code: str
    provider_type: str
    provider_auth_type: str
    provider_name: str
    provider_base_url: str = field(repr=False)
    requested_model: str = ""
    environment: str = "dev"
    environment_id: str | None = None
    model_alias_id: str | None = None
    public_model_alias: str | None = None
    routing_reason: str = "direct_model"


@dataclass(frozen=True)
class GatewayRoutePlan:
    """一次请求开始时冻结的有界候选计划。

    `initial_route` 是本次先调用的模型；只有它在调用后满足已启用的故障条件时，
    才会按 ``fallback_routes`` 顺序重发。`fallback_route` 保留为首个备用候选的
    兼容投影，真正的计划最多 32 个候选。
    成本、可用性和质量模式仍先按冻结快照选初始候选，不能把候选选择误作故障重试。
    """

    initial_route: GatewayRoute
    initial_role: str
    fallback_route: GatewayRoute | None = None
    fallback_routes: tuple[GatewayRoute, ...] = ()
    fallback_policy_id: str | None = None
    fallback_policy_version: int | None = None
    fallback_conditions: tuple[str, ...] = ()
    routing_policy_id: str | None = None
    routing_policy_version: int | None = None
    routing_mode: str | None = None
    routing_decision_id: str | None = None
    token_estimate: TokenEstimate | None = None
    routing_as_of: datetime | None = None
    policy_selected_event: WebhookEvent | None = None
    cache_affinity_context: CacheAffinityRouteContext | None = None


def validate_requested_model_authority(
    config: AppConfig,
    *,
    tenant_id: str,
    model_alias: str,
    environment: str,
    environment_id: str | None,
) -> None:
    """无副作用校验客户端 requested model 的租户、环境、存在与 active 权威性。

    参数全部来自 Virtual Key 与已校验请求体。返回：合法时无返回值。约束：这里只确认
    alias/direct model 自身，不能检查 Provider 健康、选择候选、构建 Fallback 或写路由决策；
    否则显式 deny/route_to 会被无关源 Provider 故障抢先覆盖。
    """

    connection = _connect(config)
    try:
        requested_model = model_alias.strip()
        normalized_environment = _normalize_environment(environment)
        resolved_environment_id = _resolve_environment_id(
            connection,
            tenant_id,
            normalized_environment,
            environment_id,
        )
        alias_row = connection.execute(
            """
            SELECT status
            FROM model_aliases
            WHERE tenant_id=? AND alias=?
              AND (environment_id=? OR environment_id IS NULL)
              AND environment=?
            """,
            (tenant_id, requested_model, resolved_environment_id, normalized_environment),
        ).fetchone()
        if alias_row is not None:
            # 已命中的租户别名必须处于启用状态，禁用别名不能降级穿透为同名直连模型。
            if alias_row["status"] != "active":
                raise ApiError("gateway.model_alias_disabled")
            return
        direct_row = connection.execute(
            """
            SELECT status
            FROM models
            WHERE tenant_id=? AND model_alias=?
            """,
            (tenant_id, requested_model),
        ).fetchone()
        if direct_row is None or direct_row["status"] != "active":
            # 与既有 direct resolver 保持防枚举合同：disabled 与不存在统一 model_not_found。
            raise ApiError("gateway.model_not_found")
    finally:
        connection.close()


def resolve_default_model_alias(
    config: AppConfig,
    *,
    tenant_id: str,
    environment: str,
    environment_id: str | None,
    request_kind: str,
) -> str | None:
    """为绑定员工 KEY 解析租户当前的默认模型入口。

    外部 OpenAI-compatible 客户端有些会省略 ``model``；员工 KEY 已经代表一个
    明确的人员身份，此时可以使用租户首个启用且具备对应能力的统一入口。管理员仍可
    在请求体中显式传入任意已授权入口，普通（未绑定员工）KEY 不会调用此函数。
    """

    connection = _connect(config)
    normalized_environment = _normalize_environment(environment)
    try:
        resolved_environment_id = _resolve_environment_id(
            connection, tenant_id, normalized_environment, environment_id
        )
        rows = connection.execute(
            """
            SELECT a.alias, a.created_at, m.capabilities
            FROM model_aliases a
            JOIN models m
              ON m.tenant_id=a.tenant_id AND m.model_id=a.default_model_id
            JOIN providers p
              ON p.tenant_id=m.tenant_id AND p.provider_id=m.provider_id
            WHERE a.tenant_id=? AND a.environment=?
              AND (a.environment_id=? OR a.environment_id IS NULL)
              AND a.status='active' AND m.status='active' AND p.status='active'
            ORDER BY a.created_at ASC, a.alias ASC
            """,
            (tenant_id, normalized_environment, resolved_environment_id),
        ).fetchall()
        capability_key = "embeddings" if request_kind == "embeddings" else "chat"
        candidates: list[str] = []
        for row in rows:
            raw_capabilities = row["capabilities"]
            if isinstance(raw_capabilities, dict):
                capabilities = raw_capabilities
            else:
                try:
                    capabilities = json.loads(str(raw_capabilities or "{}"))
                except (TypeError, ValueError):
                    capabilities = {}
            if isinstance(capabilities, dict) and capabilities.get(capability_key) is True:
                candidates.append(str(row["alias"]))
        # Prefer an explicitly conventional default when present; otherwise creation order
        # remains deterministic and requires no new schema field.
        for preferred in ("default", "company-default"):
            if preferred in candidates:
                return preferred
        return candidates[0] if candidates else None
    finally:
        connection.close()


def list_public_model_aliases(
    config: AppConfig,
    *,
    tenant_id: str,
    environment: str,
    environment_id: str | None,
) -> tuple[dict[str, object], ...]:
    """列出员工 KEY 可发现的 OpenAI-compatible 模型入口。

    这是 ``GET /v1/models`` 的只读投影：只返回当前租户、当前环境中已启用且绑定到
    active Provider 的别名，不返回 Provider 地址、密钥或内部模型 ID。这样 Cursor、
    Codex 兼容模式等需要先探测模型列表的客户端也只需网关地址和个人 KEY。
    """

    connection = _connect(config)
    normalized_environment = _normalize_environment(environment)
    try:
        resolved_environment_id = _resolve_environment_id(
            connection, tenant_id, normalized_environment, environment_id
        )
        rows = connection.execute(
            """
            SELECT DISTINCT a.alias, a.created_at
            FROM model_aliases a
            JOIN models m
              ON m.tenant_id=a.tenant_id AND m.model_id=a.default_model_id
            JOIN providers p
              ON p.tenant_id=m.tenant_id AND p.provider_id=m.provider_id
            WHERE a.tenant_id=? AND a.environment=?
              AND (a.environment_id=? OR a.environment_id IS NULL)
              AND a.status='active' AND m.status='active' AND p.status='active'
            ORDER BY a.created_at ASC, a.alias ASC
            """,
            (tenant_id, normalized_environment, resolved_environment_id),
        ).fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            alias = str(row["alias"] or "").strip()
            if not alias:
                continue
            # OpenAI 的 created 字段是 Unix 秒；无效时间只影响展示，固定为 0。
            try:
                parsed_created = datetime.fromisoformat(
                    str(row["created_at"]).replace("Z", "+00:00")
                )
                if parsed_created.tzinfo is None:
                    parsed_created = parsed_created.replace(tzinfo=UTC)
                created = int(parsed_created.timestamp())
            except (TypeError, ValueError, OverflowError):
                created = 0
            result.append(
                {"id": alias, "object": "model", "created": created, "owned_by": "metahub"}
            )
        return tuple(result)
    finally:
        connection.close()


def resolve_policy_target_route(
    config: AppConfig,
    *,
    tenant_id: str,
    target_model_id: str,
    requested_model: str,
    environment: str,
    environment_id: str | None,
    request_id: str,
    trace_id: str,
    virtual_key_id: str,
    policy_id: str | None,
    snapshot_version: int | None,
) -> GatewayRoute:
    """把 Policy route_to 的 concrete model_id 解析成唯一、可审计的运行路由。

    target 必须属于当前 tenant 且模型/Provider active、健康证据不过期；失败统一为
    `gateway.policy_route_unavailable`，不能泄露其他租户对象。成功只记录一个最终路由决策，
    且不继承客户端原 alias 的请求期 Fallback。
    """

    connection = _connect(config)
    normalized_environment = _normalize_environment(environment)
    try:
        resolved_environment_id = _resolve_environment_id(
            connection,
            tenant_id,
            normalized_environment,
            environment_id,
        )
        row = _load_model_route_by_id(connection, tenant_id, target_model_id)
        if (
            row is None
            or row["model_status"] != "active"
            or row["provider_status"] != "active"
            or _provider_is_fresh_unhealthy(row, config.provider_health_stale_seconds)
        ):
            _record_routing_decision(
                connection,
                tenant_id=tenant_id,
                request_id=request_id,
                trace_id=trace_id,
                virtual_key_id=virtual_key_id,
                model_alias_id=None,
                requested_model=requested_model,
                environment=normalized_environment,
                environment_id=resolved_environment_id,
                decision_status="failed",
                decision_reason="policy.route_unavailable",
                candidate_summary={
                    "policy_id": policy_id,
                    "snapshot_version": snapshot_version,
                },
                error_code="gateway.policy_route_unavailable",
            )
            # 目标不可用时保留失败审计并统一收敛错误，不能回退到原模型改变策略语义。
            raise ApiError("gateway.policy_route_unavailable")
        route = _route_from_row(
            row,
            requested_model=requested_model,
            environment=normalized_environment,
            environment_id=resolved_environment_id,
            model_alias_id=None,
            public_model_alias=None,
            routing_reason="policy.route_to",
        )
        _record_route_selected(
            connection,
            route,
            request_id=request_id,
            trace_id=trace_id,
            virtual_key_id=virtual_key_id,
            candidate_summary={
                "policy_id": policy_id,
                "snapshot_version": snapshot_version,
                "policy_target_model_id": target_model_id,
            },
        )
        return route
    # 已分类领域错误已具备稳定错误码和租户防枚举语义，必须原样交给统一响应层。
    except ApiError:
        raise
    # 未分类存储或数据异常同样失败关闭，禁止将损坏目标悄悄替换为客户端请求模型。
    except (sqlite3.Error, ValueError, TypeError) as exc:
        raise ApiError("gateway.policy_route_unavailable") from exc
    finally:
        connection.close()


def resolve_model_route_plan(
    config: AppConfig,
    *,
    tenant_id: str,
    model_alias: str,
    environment: str = "dev",
    environment_id: str | None = None,
    request_id: str | None = None,
    trace_id: str | None = None,
    virtual_key_id: str | None = None,
    project_id: str | None = None,
    request_kind: str | None = None,
    payload: dict[str, Any] | None = None,
    trusted_token_estimate: TokenEstimate | None = None,
) -> GatewayRoutePlan:
    """解析冻结 RoutingPolicy，并在满足 PRD-036 条件时附加请求后备用候选。

    正常 0036 Alias 在一个 ``BEGIN IMMEDIATE`` 事务内冻结 Policy、两候选、Health、
    KeyPool、必要价格与 FallbackPolicy，并原子写决策事实。缺少持久 Policy 的旧夹具继续
    走只读 legacy fixed 路径；direct model 与 Policy ``route_to`` 均不继承本策略。
    """

    persisted_plan = _resolve_persisted_alias_route_plan(
        config,
        tenant_id=tenant_id,
        model_alias=model_alias,
        environment=environment,
        environment_id=environment_id,
        request_id=request_id,
        trace_id=trace_id,
        virtual_key_id=virtual_key_id,
        project_id=project_id,
        request_kind=request_kind,
        payload=payload,
        trusted_token_estimate=trusted_token_estimate,
    )
    if persisted_plan is not None:
        return (
            replace(persisted_plan, token_estimate=trusted_token_estimate)
            if trusted_token_estimate is not None
            else persisted_plan
        )

    initial = resolve_model_route(
        config,
        tenant_id=tenant_id,
        model_alias=model_alias,
        environment=environment,
        environment_id=environment_id,
        request_id=request_id,
        trace_id=trace_id,
        virtual_key_id=virtual_key_id,
    )
    initial_role = "fallback" if initial.routing_reason == "alias.fallback" else "primary"
    # 直接模型和 PRD-035 预检切换均不属于“主调用失败后切换”，不能再附加请求期策略。
    if initial.routing_reason != "alias.default" or initial.model_alias_id is None:
        return GatewayRoutePlan(
            initial_route=initial,
            initial_role=initial_role,
            token_estimate=trusted_token_estimate,
        )

    try:
        connection = _connect(config)
    # legacy fixed 在第二次连接失败时也必须使用路由快照统一码，不能暴露 db.* 基础码。
    except ApiError as exc:
        raise ApiError("gateway.routing_snapshot_unavailable") from exc
    try:
        policy = connection.execute(
            """
            SELECT fallback_policy_id, version, condition_expr
            FROM fallback_policies
            WHERE tenant_id=? AND model_alias_id=? AND status='enabled'
            """,
            (tenant_id, initial.model_alias_id),
        ).fetchone()
        alias_row = connection.execute(
            """
            SELECT fallback_model_id
            FROM model_aliases
            WHERE tenant_id=? AND model_alias_id=? AND status='active'
            """,
            (tenant_id, initial.model_alias_id),
        ).fetchone()
        # 策略、别名或备用目标任一缺失时保持单路由，不允许根据残缺配置猜测执行动作。
        if policy is None or alias_row is None or not alias_row["fallback_model_id"]:
            return GatewayRoutePlan(
                initial_route=initial,
                initial_role="primary",
                token_estimate=trusted_token_estimate,
            )

        fallback_row = _load_model_route_by_id(
            connection,
            tenant_id,
            str(alias_row["fallback_model_id"]),
        )
        if (
            fallback_row is None
            or fallback_row["model_status"] != "active"
            or fallback_row["provider_status"] != "active"
            or _provider_is_fresh_unhealthy(
                fallback_row,
                config.provider_health_stale_seconds,
            )
            or fallback_row["model_id"] == initial.model_id
        ):
            return GatewayRoutePlan(
                initial_route=initial,
                initial_role="primary",
                token_estimate=trusted_token_estimate,
            )

        conditions = _parse_fallback_conditions(policy["condition_expr"])
        fallback = _route_from_row(
            fallback_row,
            requested_model=initial.requested_model,
            environment=initial.environment,
            environment_id=initial.environment_id or environment_id or "",
            model_alias_id=initial.model_alias_id,
            public_model_alias=initial.public_model_alias,
            routing_reason="alias.fallback_runtime",
        )
        return GatewayRoutePlan(
            initial_route=initial,
            initial_role="primary",
            fallback_route=fallback,
            fallback_policy_id=str(policy["fallback_policy_id"]),
            fallback_policy_version=int(policy["version"]),
            fallback_conditions=conditions,
            token_estimate=trusted_token_estimate,
        )
    # legacy SQL 锁或查询中断属于可重试依赖故障，统一为快照不可用且隐藏 SQL/路径。
    except sqlite3.OperationalError as exc:
        raise ApiError("gateway.routing_snapshot_unavailable") from exc
    # 非法 JSON、条件枚举或其他持久关系损坏不可通过重试恢复，必须失败关闭。
    except (sqlite3.Error, ValueError, TypeError) as exc:
        raise ApiError("gateway.routing_snapshot_invalid") from exc
    finally:
        connection.close()


def _resolve_persisted_alias_route_plan(
    config: AppConfig,
    *,
    tenant_id: str,
    model_alias: str,
    environment: str,
    environment_id: str | None,
    request_id: str | None,
    trace_id: str | None,
    virtual_key_id: str | None,
    project_id: str | None,
    request_kind: str | None,
    payload: dict[str, Any] | None,
    trusted_token_estimate: TokenEstimate | None,
) -> GatewayRoutePlan | None:
    """尝试在一个事务中解析 0036 持久 RoutingPolicy；非 Alias/缺 Policy 返回 None。"""

    try:
        connection = _connect(config)
    # 路由快照连接建立失败属于可重试依赖故障，不能把 db.* 裸码或 HTTP 500 暴露给调用方。
    except ApiError as exc:
        raise ApiError("gateway.routing_snapshot_unavailable") from exc
    normalized_environment = _normalize_environment(environment)
    try:
        resolved_environment_id = _resolve_environment_id(
            connection,
            tenant_id,
            normalized_environment,
            environment_id,
        )
        alias_probe = connection.execute(
            """
            SELECT 1
            FROM model_aliases
            WHERE tenant_id=? AND alias=? AND environment=?
              AND environment_id=?
            """,
            (tenant_id, model_alias.strip(), normalized_environment, resolved_environment_id),
        ).fetchone()
        if alias_probe is None:
            # Direct model 不拥有 Alias RoutingPolicy；在获取 SQLite 写锁和读取 Policy 表前
            # 完成分流，确保策略表缺失、损坏或繁忙不会阻断 legacy direct 路由。
            return None

        # SQLite 本地部署用写锁把 Policy、候选依赖与不可变决策提交冻结在同一快照中。
        # 预检结果不作为选择事实；Alias 的身份与策略仍必须在冻结事务内完整重读。
        connection.execute("BEGIN IMMEDIATE")
        alias_policy = connection.execute(
            """
            SELECT
              a.model_alias_id, a.alias, a.environment, a.environment_id,
              a.default_model_id, a.fallback_model_id, a.status AS alias_status,
              rp.routing_policy_id, rp.routing_mode, rp.status AS routing_policy_status,
              rp.version AS routing_policy_version,
              rp.quality_weight_bps, rp.cost_weight_bps, rp.latency_weight_bps,
              rp.quality_floor_bps, rp.minimum_sample_count
            FROM model_aliases a
            LEFT JOIN model_alias_routing_policies rp
              ON rp.tenant_id=a.tenant_id AND rp.model_alias_id=a.model_alias_id
            WHERE a.tenant_id=? AND a.alias=? AND a.environment=?
              AND a.environment_id=?
            """,
            (tenant_id, model_alias.strip(), normalized_environment, resolved_environment_id),
        ).fetchone()
        # direct model、disabled Alias 与缺 Policy 兼容夹具交还旧 resolver，保持既有错误优先级。
        if (
            alias_policy is None
            or alias_policy["alias_status"] != "active"
            or alias_policy["routing_policy_id"] is None
        ):
            connection.rollback()
            return None
        routing_policy_id, routing_mode, routing_policy_version = _validated_routing_policy(
            alias_policy
        )
        quality_config = _quality_cost_config(alias_policy, routing_mode=routing_mode)
        as_of = _utcnow()
        candidate_rows: dict[str, sqlite3.Row] = {}
        candidate_inputs: list[RoutingCandidateInput] = []
        configured_candidates: list[tuple[str, int, object]] = [
            ("default", 0, alias_policy["default_model_id"]),
        ]
        try:
            fallback_rows = connection.execute(
                """
                SELECT model_id, configured_priority
                FROM model_alias_fallback_candidates
                WHERE tenant_id=? AND model_alias_id=?
                ORDER BY configured_priority ASC
                """,
                (tenant_id, alias_policy["model_alias_id"]),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
            fallback_rows = []
        if fallback_rows:
            configured_candidates.extend(
                ("fallback", int(item["configured_priority"]), item["model_id"])
                for item in fallback_rows
            )
        elif alias_policy["fallback_model_id"]:
            configured_candidates.append(("fallback", 1, alias_policy["fallback_model_id"]))
        if len(configured_candidates) > MAX_ROUTING_CANDIDATES:
            raise ValueError("routing candidate limit exceeded")
        for role, priority, configured_model_id in configured_candidates:
            # fallback 可空是 Alias 正式合同；空目标不产生伪 Candidate 行。
            if configured_model_id is None:
                continue
            model_id = str(configured_model_id)
            row = _load_policy_candidate_row(connection, tenant_id, model_id)
            # 正常 0036 owner trigger 保证 Alias 目标图完整；缺失父对象属于快照损坏，
            # 不能伪装成普通 unavailable 后继续调用其他 Provider。
            if row is None or row["provider_id"] is None:
                raise ValueError("routing snapshot owner graph invalid")
            candidate_rows[model_id] = row
            candidate_inputs.append(
                _routing_candidate_input(
                    config,
                    connection,
                    row,
                    role=role,
                    priority=priority,
                    routing_mode=routing_mode,
                    as_of=as_of,
                )
            )

        selection = select_routing_candidates(
            routing_mode,
            candidate_inputs,
            token_estimator=lambda: _routing_request_token_estimate(
                request_kind,
                payload,
                trusted_token_estimate=trusted_token_estimate,
            ),
            price_loader=lambda eligible: _load_routing_prices(
                connection,
                tenant_id,
                tuple(item.model_id for item in eligible),
                as_of,
            ),
            quality_config=quality_config,
            quality_loader=lambda eligible: _load_routing_quality_facts(
                connection,
                tenant_id,
                tuple(item.model_id for item in eligible),
            ),
        )
        deferred_model_ids: tuple[str, ...] = ()
        probe_key: RouteKey | None = None
        if request_id:
            selection, deferred_model_ids, probe_key = _apply_route_cooldowns(
                selection,
                tenant_id=scoped_tenant(config.database_url, tenant_id),
                request_id=request_id,
            )
        cache_affinity_context: CacheAffinityRouteContext | None = None
        cache_affinity_summary: dict[str, object] | None = None
        # 只有带完整服务端追踪身份的真实请求才能创建租户内 route reservation 事实。
        if request_id and trace_id and virtual_key_id:
            # 只有真实 HTTP Alias 请求才创建 route reservation；内部只读解析不得留下运行状态。
            from metahub_gateway.cache_affinity.routing import prepare_cache_affinity_route

            try:
                affinity_facts = prepare_cache_affinity_route(
                    config,
                    connection,
                    tenant_id=tenant_id,
                    virtual_key_id=virtual_key_id,
                    model_alias_id=str(alias_policy["model_alias_id"]),
                    request_kind=request_kind or "",
                    payload=payload or {},
                    base_selection=selection,
                    candidate_rows=candidate_rows,
                    as_of=as_of,
                    deferred_model_ids=deferred_model_ids,
                    recovery_probe_model_id=probe_key[1] if probe_key else None,
                )
                selection = affinity_facts.selection
                cache_affinity_context = affinity_facts.context
                cache_affinity_summary = affinity_facts.candidate_summary
            # 亲和事实与基础 Decision 共用事务，任一存储或数据合同错误都必须整体失败关闭。
            # 这里不得降级到旧路由，否则客户端会在无 Selection 审计的情况下调用 Provider。
            except (sqlite3.Error, ValueError, TypeError) as exc:
                # 有效配置的 reservation 无法原子冻结时必须停在 Provider 前，不能静默走旧路由。
                if probe_key is not None:
                    route_cooldowns.abandoned(probe_key, request_id)
                raise ApiError("gateway.cache_affinity_unavailable") from exc
        try:
            return _finalize_persisted_alias_plan(
                config,
                connection,
                tenant_id=tenant_id,
                alias_policy=alias_policy,
                routing_policy_id=routing_policy_id,
                routing_policy_version=routing_policy_version,
                routing_mode=routing_mode,
                selection=selection,
                candidate_rows=candidate_rows,
                cache_affinity_context=cache_affinity_context,
                cache_affinity_summary=cache_affinity_summary,
                cooldown_deferred_model_ids=deferred_model_ids,
                requested_model=model_alias.strip(),
                environment=normalized_environment,
                environment_id=resolved_environment_id,
                request_id=request_id,
                trace_id=trace_id,
                virtual_key_id=virtual_key_id,
                project_id=project_id,
                as_of=as_of,
            )
        except BaseException:
            if probe_key is not None:
                route_cooldowns.abandoned(probe_key, request_id)
            raise
    # 已分类领域错误已具有稳定码，必须原样交给统一响应层。
    except ApiError:
        connection.rollback()
        raise
    # SQLite 锁或查询故障属于可重试快照不可用，不能猜测动态选择。
    except sqlite3.OperationalError as exc:
        connection.rollback()
        logger.warning(
            "route.snapshot_write_failed",
            extra={
                "event": "route.snapshot_write_failed",
                "request_id": request_id,
                "error": str(exc),
            },
        )
        raise ApiError("gateway.routing_snapshot_unavailable") from exc
    # 其他数据库异常同样不能泄露 SQL 或文件路径，只返回统一基础设施码。
    except sqlite3.Error as exc:
        connection.rollback()
        logger.warning(
            "route.snapshot_write_failed",
            extra={
                "event": "route.snapshot_write_failed",
                "request_id": request_id,
                "error": str(exc),
            },
        )
        raise ApiError("gateway.routing_snapshot_unavailable") from exc
    # 枚举、Decimal、时间或 owner 图错误属于持久事实损坏，重试不能自行恢复。
    except (ValueError, TypeError, InvalidOperation) as exc:
        connection.rollback()
        raise ApiError("gateway.routing_snapshot_invalid") from exc
    finally:
        connection.close()


def _apply_route_cooldowns(
    selection: RoutingSelectionResult, *, tenant_id: str, request_id: str
) -> tuple[RoutingSelectionResult, tuple[str, ...], RouteKey | None]:
    """Move recently failed eligible routes behind healthy candidates before persisting."""

    ranked = sorted(
        (item for item in selection.candidate_results if item.rank is not None),
        key=lambda item: item.rank or 0,
    )
    if not ranked:
        return selection, (), None
    keys = tuple(
        (tenant_id, item.candidate.model_id, item.candidate.provider_id or "")
        for item in ranked
    )
    ordered, probe_key, deferred = route_cooldowns.order(keys, request_id)
    if ordered == keys:
        return selection, tuple(key[1] for key in deferred), probe_key
    rank_by_model = {key[1]: rank for rank, key in enumerate(ordered, start=1)}
    selected_model_id = ordered[0][1]
    results = tuple(
        replace(
            item,
            rank=rank_by_model[item.candidate.model_id],
            status="selected" if item.candidate.model_id == selected_model_id else "standby",
            reason_code=(
                selection.selection_reason
                if item.candidate.model_id == selected_model_id
                else REASON_STANDBY
            ),
        ) if item.rank is not None else item
        for item in selection.candidate_results
    )
    deferred_ids = tuple(key[1] for key in deferred)
    return (
        replace(
            selection,
            selected_model_id=selected_model_id,
            selected_provider_id=ordered[0][2],
            candidate_results=results,
        ),
        deferred_ids,
        probe_key,
    )


def _routing_request_token_estimate(
    request_kind: str | None,
    payload: dict[str, Any] | None,
    *,
    trusted_token_estimate: TokenEstimate | None,
) -> TokenEstimate:
    """优先使用内部批任务可信令牌估算，普通请求才允许采用本地估算值。

    Use the immutable Batch estimate before considering decrypted request content.
    """

    # 内部批任务只接受可信令牌估算；普通估算不得覆盖已冻结的调度输入。
    if trusted_token_estimate is not None:
        # 内部批任务只接受可信令牌估算；普通估算不得覆盖已冻结的调度输入。
        return trusted_token_estimate
    # 内部批任务只接受可信令牌估算；普通估算不得覆盖已冻结的调度输入。
    return estimate_request_tokens(request_kind or "", payload or {})


def _validated_routing_policy(row: sqlite3.Row) -> tuple[str, str, int]:
    """严格恢复 RoutingPolicy 身份、状态和版本，损坏时使用专用统一错误码。"""

    policy_id = row["routing_policy_id"]
    mode = row["routing_mode"]
    version = row["routing_policy_version"]
    if (
        type(policy_id) is not str
        or not policy_id
        or type(mode) is not str
        or mode not in {"fixed", "cost_first", "availability_first", "quality_cost"}
        or row["routing_policy_status"] != "enabled"
        or type(version) is not int
        or version < 1
    ):
        # Policy 身份不完整时不能退回 fixed，否则会绕过管理员已经选择的动态模式。
        raise ApiError("gateway.routing_policy_invalid")
    return policy_id, mode, version


def _quality_cost_config(
    row: sqlite3.Row,
    *,
    routing_mode: str,
) -> QualityCostConfig | None:
    """从同一 Policy 行恢复质量配置，仅在 ``quality_cost`` 模式构造强类型快照。

    参数：包含五个配置列的租户限定 Policy 行及已验证模式。返回：质量模式的不可变配置，
    旧三模式返回 ``None``。关键约束：不能为旧模式主动解释休眠配置，也不能对非法值做
    截断或默认回退；shared 校验器会把任何类型、范围或权重和损坏统一判为快照无效。
    """

    if routing_mode != "quality_cost":
        return None
    return QualityCostConfig(
        quality_weight_bps=row["quality_weight_bps"],
        cost_weight_bps=row["cost_weight_bps"],
        latency_weight_bps=row["latency_weight_bps"],
        quality_floor_bps=row["quality_floor_bps"],
        minimum_sample_count=row["minimum_sample_count"],
    )


def _load_routing_quality_facts(
    connection: sqlite3.Connection,
    tenant_id: str,
    model_ids: tuple[str, ...],
) -> tuple[RoutingQualityFact, ...]:
    """在当前路由事务中按 ``tenant_id + model_id`` 冻结候选质量事实。

    参数：已持有 ``BEGIN IMMEDIATE`` 的连接、认证租户和 shared 已筛出的候选 ID。
    返回：与候选一一对应的强类型 Score 元组。关键约束：Score 缺失、停用或重复均表示
    owner 图损坏，不能伪装成冷启动；查询和返回值绝不包含 Prompt、Response 或错误正文。
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
        if row is None or row["status"] != "active":
            # Model 与 Score 应由 0037 trigger 一一创建；缺失不能自动补默认值改变线上排序。
            raise ValueError("routing quality score graph invalid")
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


def _load_policy_candidate_row(
    connection: sqlite3.Connection,
    tenant_id: str,
    model_id: str,
) -> sqlite3.Row | None:
    """按 tenant 加载动态路由需要的完整 Model/Provider 安全事实。"""

    return connection.execute(
        """
        SELECT
          m.model_id, m.tenant_id, m.model_alias, m.status AS model_status,
          COALESCE(m.provider_model_id, m.upstream_model_name) AS upstream_model_name,
          m.capabilities,
          p.provider_id, p.provider_code, p.provider_type, p.name AS provider_name,
          p.auth_type, p.base_url, p.status AS provider_status,
          p.health_status AS provider_health_status,
          p.health_updated_at AS provider_health_updated_at
        FROM models m
        LEFT JOIN providers p
          ON p.tenant_id=m.tenant_id AND p.provider_id=m.provider_id
        WHERE m.tenant_id=? AND m.model_id=?
        """,
        (tenant_id, model_id),
    ).fetchone()


def _routing_candidate_input(
    config: AppConfig,
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    role: str,
    priority: int,
    routing_mode: str,
    as_of: datetime,
) -> RoutingCandidateInput:
    """把数据库候选归一为 shared 纯排序器输入，并按模式最小化 KeyPool 读取。"""

    model_status = str(row["model_status"] or "")
    provider_status = str(row["provider_status"] or "")
    if model_status not in {"active", "disabled"} or provider_status not in {
        "active",
        "disabled",
    }:
        # 未知状态无法安全推断 eligibility，统一交给快照完整性错误处理。
        raise ValueError("routing candidate status invalid")
    health_state, health_fresh = _normalized_provider_health(
        row["provider_health_status"],
        row["provider_health_updated_at"],
        as_of=as_of,
        stale_seconds=config.provider_health_stale_seconds,
    )
    keypool_state = "not_evaluated"
    generic_eligible = (
        model_status == "active"
        and provider_status == "active"
        and not (health_state == "unhealthy" and health_fresh)
    )
    if routing_mode != "fixed" and generic_eligible:
        # 局部导入避免 KeyPool 的 GatewayRoute 类型依赖与本模块形成加载期循环。
        from metahub_gateway.providers.keypool import snapshot_provider_key_pool

        keypool_state = snapshot_provider_key_pool(
            config,
            connection,
            tenant_id=str(row["tenant_id"]),
            provider_id=str(row["provider_id"]),
            provider_code=str(row["provider_code"]),
            provider_auth_type=str(row["auth_type"]),
            as_of=as_of,
        ).state
    return RoutingCandidateInput(
        candidate_role=role,  # type: ignore[arg-type]
        configured_priority=priority,
        model_id=str(row["model_id"]),
        provider_id=str(row["provider_id"]),
        model_status=model_status,  # type: ignore[arg-type]
        provider_status=provider_status,  # type: ignore[arg-type]
        health_state=health_state,  # type: ignore[arg-type]
        health_fresh=health_fresh,
        keypool_state=keypool_state,  # type: ignore[arg-type]
    )


def _normalized_provider_health(
    raw_state: object,
    raw_updated_at: object,
    *,
    as_of: datetime,
    stale_seconds: int,
) -> tuple[str, bool]:
    """按唯一 as_of 严格归类 Provider 健康；无时间证据合法归为 unknown。"""

    state = str(raw_state or "unknown")
    if state not in {"healthy", "unhealthy", "unknown"}:
        # 非白名单状态不能当 unknown Fail Open，否则坏数据会放大线上流量。
        raise ValueError("provider health state invalid")
    if not raw_updated_at:
        return "unknown", False
    checked_at = _parse_strict_db_time(raw_updated_at)
    if checked_at > as_of:
        # 未来健康证据无法属于当前冻结时点，必须失败关闭而不是宣称新鲜。
        raise ValueError("provider health timestamp is in the future")
    # unknown 仍不形成健康/异常证据，但任何非空时间都必须先证明格式合法。
    if state == "unknown":
        return "unknown", False
    fresh = checked_at >= as_of - timedelta(seconds=stale_seconds)
    return state, fresh


def _parse_strict_db_time(value: object) -> datetime:
    """严格解析数据库时间；SQLite ``datetime`` 的无时区文本按既有 UTC 合同恢复。"""

    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        # 历史迁移和测试广泛使用 SQLite datetime('now')；它语义固定为 UTC，不能把合法
        # 存量健康证据误判为损坏，但其他无法解析文本仍由 fromisoformat 失败关闭。
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _load_routing_prices(
    connection: sqlite3.Connection,
    tenant_id: str,
    model_ids: tuple[str, ...],
    as_of: datetime,
) -> tuple[RoutingPriceFact, ...]:
    """读取同一 as_of 的成本价格；项目模板也需要校验唯一候选是否有价。"""

    # 普通别名仅在多候选时比较价格；动态模板也读取单候选价格以排除无价模型。
    if not model_ids:
        raise ValueError("routing price candidate count invalid")
    facts: list[RoutingPriceFact] = []
    as_of_text = _to_db_time(as_of)
    try:
        fx = resolve_workspace_fx_rate(connection, tenant_id=tenant_id, observed_at=as_of_text)
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        # Older local fixtures predate the editable rate table. Keep their routing
        # contract usable; migrated workspaces always take the database value above.
        fx = None
    for model_id in model_ids:
        try:
            rate = resolve_workspace_cost_rate(
                connection,
                tenant_id=tenant_id,
                model_id=model_id,
                observed_at=as_of_text,
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("routing price fact invalid") from exc
        if rate is None:
            facts.append(RoutingPriceFact.missing(model_id))
            continue
        if rate.model_id != model_id:
            raise ValueError("routing price owner invalid")
        facts.append(
            RoutingPriceFact(
                model_id=model_id,
                source=rate.source,
                input_price_per_1k=rate.input_price_per_1k,
                output_price_per_1k=rate.output_price_per_1k,
                cache_price_per_1k=rate.cache_price_per_1k,
                currency=rate.currency,
                price_schema_version=rate.price_schema_version,
                comparison_currency="CNY" if rate.currency == "USD" else None,
                comparison_fx_rate=(
                    (fx.usd_to_cny if fx else Decimal("7.2"))
                    if rate.currency == "USD"
                    else None
                ),
                comparison_fx_rate_version=(
                    (fx.version_id if fx else "legacy-deployment-v1")
                    if rate.currency == "USD"
                    else None
                ),
            )
        )
    return tuple(facts)


def _finalize_persisted_alias_plan(
    config: AppConfig,
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    alias_policy: sqlite3.Row,
    routing_policy_id: str,
    routing_policy_version: int,
    routing_mode: str,
    selection: RoutingSelectionResult,
    candidate_rows: dict[str, sqlite3.Row],
    cache_affinity_context: CacheAffinityRouteContext | None,
    cache_affinity_summary: dict[str, object] | None,
    cooldown_deferred_model_ids: tuple[str, ...],
    requested_model: str,
    environment: str,
    environment_id: str,
    request_id: str | None,
    trace_id: str | None,
    virtual_key_id: str | None,
    project_id: str | None,
    as_of: datetime,
) -> GatewayRoutePlan:
    """构造并提交持久 Alias 路由计划。

    参数：全部 Policy、候选和链路字段来自同一 tenant 冻结事务。返回：仅引用安全 ID 的
    GatewayRoutePlan。约束：动态模式必须先原子写 Decision/Candidate，fixed 才允许记录失败
    时兼容放行；Webhook 对象只能引用已经提交的事件 ID。
    """

    selected_result = next(
        (item for item in selection.candidate_results if item.status == "selected"),
        None,
    )
    route: GatewayRoute | None = None
    fallback_route: GatewayRoute | None = None
    fallback_routes: tuple[GatewayRoute, ...] = ()
    fallback_policy_id: str | None = None
    fallback_policy_version: int | None = None
    fallback_conditions: tuple[str, ...] = ()
    initial_role = "primary"
    if selected_result is not None:
        selected_row = candidate_rows[selected_result.candidate.model_id]
        route = _route_from_row(
            selected_row,
            requested_model=requested_model,
            environment=environment,
            environment_id=environment_id,
            model_alias_id=str(alias_policy["model_alias_id"]),
            public_model_alias=str(alias_policy["alias"]),
            routing_reason=f"alias.{selected_result.candidate.candidate_role}",
        )
        # fixed 必须保留 PRD-035 预检 fallback 的既有 attempt role 语义。
        legacy_fixed_fallback = (
            routing_mode == "fixed" and selected_result.candidate.candidate_role == "fallback"
        )
        # 兼容性：fixed 预检选中 fallback 时保留 PRD-035 已公开的 attempt 角色。
        if legacy_fixed_fallback:
            initial_role = "fallback"
        (
            fallback_routes,
            fallback_policy_id,
            fallback_policy_version,
            fallback_conditions,
        ) = _load_frozen_runtime_fallback(
            connection,
            tenant_id=tenant_id,
            alias_policy=alias_policy,
            selection=selection,
            candidate_rows=candidate_rows,
            initial_route=route,
        )
        fallback_route = fallback_routes[0] if fallback_routes else None

    decision_id = f"rdec_{uuid4().hex}"
    event: WebhookEvent | None = None
    event_id: str | None = None
    # 只有真实已选路由且具备链路 ID 时才生成 request_selected 事件，失败决策不伪造事件。
    if route is not None and request_id:
        # routing_decisions 使用与配置事件相同的 rpe_ 稳定 ID 形状，便于 Webhook 幂等追踪。
        event_id = f"rpe_{uuid4().hex}"
        event = build_webhook_event(
            tenant_id=tenant_id,
            event_type="route.policy_selected",
            event_id=event_id,
            timestamp=as_of,
            actor_id=None,
            object_id=routing_policy_id,
            trace_id=trace_id or request_id,
            payload={
                "trigger": "request_selected",
                "request_id": request_id,
                "routing_decision_id": decision_id,
                "model_alias_id": str(alias_policy["model_alias_id"]),
                "routing_policy_id": routing_policy_id,
                "routing_policy_version": routing_policy_version,
                "routing_mode": routing_mode,
                "selected_model_id": route.model_id,
                "selected_provider_id": route.provider_id,
                "candidate_count": len(selection.candidate_results),
                "eligible_candidate_count": selection.eligible_candidate_count,
                "selection_reason": selection.selection_reason,
                "project_id": project_id,
            },
        )

    # 真实 HTTP 请求必须持久化；无 request_id 只允许内部只读兼容调用。
    if request_id:
        try:
            _persist_policy_routing_facts(
                connection,
                decision_id=decision_id,
                tenant_id=tenant_id,
                request_id=request_id,
                trace_id=trace_id or request_id,
                virtual_key_id=virtual_key_id,
                model_alias_id=str(alias_policy["model_alias_id"]),
                requested_model=requested_model,
                environment=environment,
                environment_id=environment_id,
                routing_policy_id=routing_policy_id,
                routing_policy_version=routing_policy_version,
                routing_mode=routing_mode,
                selection=selection,
                route=route,
                event_id=event_id,
                cache_affinity_summary=cache_affinity_summary,
                cooldown_deferred_model_ids=cooldown_deferred_model_ids,
                as_of=as_of,
            )
            connection.commit()
        # 写事实失败时动态模式必须停在 Provider 前；fixed 才保留已验收的 best-effort 语义。
        except sqlite3.Error as exc:
            connection.rollback()
            if cache_affinity_context is not None:
                # fixed 旧模式仅对无亲和请求保留 best-effort；有效亲和事实缺失必须失败关闭。
                raise ApiError("gateway.cache_affinity_unavailable") from exc
            if routing_mode != "fixed":
                # 动态选择若没有原子解释事实就不能调用 Provider，否则会产生不可审计流量。
                raise ApiError("gateway.routing_decision_unavailable") from exc
            event = None
            event_id = None
            decision_id = ""
            logger.warning(
                "route.decision_log_failed",
                extra={
                    "event": "route.decision_log_failed",
                    "tenant_id": tenant_id,
                    "request_id": request_id,
                    "trace_id": trace_id or request_id,
                    "error_code": "gateway.routing_decision_unavailable",
                },
            )
    else:
        # 只有内部无链路 ID 调用可走只读选择；真实 HTTP 请求总有 request_id 并必须落事实。
        connection.rollback()

    # 无选中候选时使用 fixed 旧错误优先级或动态统一 no-route，均不进入 Provider。
    if route is None:
        error_code = (
            _fixed_selection_failure_code(selection)
            if routing_mode == "fixed"
            else "gateway.no_route_available"
        )
        # 异常转换：统一错误码阻止无候选请求进入 Provider。
        unavailable_error = ApiError(error_code)
        raise unavailable_error

    logger.info(
        "route.selected",
        extra={
            "event": "route.selected",
            "request_id": request_id,
            "trace_id": trace_id or request_id,
            "tenant_id": tenant_id,
            "object_id": str(alias_policy["model_alias_id"]),
            "payload": {
                "routing_policy_id": routing_policy_id,
                "routing_policy_version": routing_policy_version,
                "routing_mode": routing_mode,
                "selected_model_id": route.model_id,
                "provider_id": route.provider_id,
                "decision_reason": selection.selection_reason,
            },
        },
    )
    return GatewayRoutePlan(
        initial_route=route,
        initial_role=initial_role,
        fallback_route=fallback_route,
        fallback_routes=fallback_routes,
        fallback_policy_id=fallback_policy_id,
        fallback_policy_version=fallback_policy_version,
        fallback_conditions=fallback_conditions,
        routing_policy_id=routing_policy_id,
        routing_policy_version=routing_policy_version,
        routing_mode=routing_mode,
        routing_decision_id=decision_id or None,
        token_estimate=selection.token_estimate,
        routing_as_of=as_of,
        policy_selected_event=event,
        cache_affinity_context=cache_affinity_context,
    )


def _load_frozen_runtime_fallback(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    alias_policy: sqlite3.Row,
    selection: RoutingSelectionResult,
    candidate_rows: dict[str, sqlite3.Row],
    initial_route: GatewayRoute,
) -> tuple[tuple[GatewayRoute, ...], str | None, int | None, tuple[str, ...]]:
    """只为已选 default 且同快照合格 fallback 冻结 PRD-036 计划。

    运行时计划冻结所有有序 standby 候选；质量、成本和可用性模式只决定首选顺序，
    上游握手失败后都沿同一故障条件推进到下一个候选。
    """

    selected = next(item for item in selection.candidate_results if item.status == "selected")
    if selected is None:
        return (), None, None, ()
    standby_items = sorted((
        item
        for item in selection.candidate_results
        if item.status == "standby"
    ), key=lambda item: item.rank or 0)
    if not standby_items:
        return (), None, None, ()
    policy = connection.execute(
        """
        SELECT fallback_policy_id, version, condition_expr
        FROM fallback_policies
        WHERE tenant_id=? AND model_alias_id=? AND status='enabled'
        """,
        (tenant_id, alias_policy["model_alias_id"]),
    ).fetchone()
    if policy is None:
        return (), None, None, ()
    conditions = _parse_fallback_conditions(policy["condition_expr"])
    if not conditions:
        return (), None, None, ()
    fallback_routes = tuple(
        _route_from_row(
            candidate_rows[item.candidate.model_id],
            requested_model=initial_route.requested_model,
            environment=initial_route.environment,
            environment_id=initial_route.environment_id or "",
            model_alias_id=initial_route.model_alias_id,
            public_model_alias=initial_route.public_model_alias,
            routing_reason="alias.fallback_runtime",
        )
        for item in standby_items
        if item.candidate.model_id != initial_route.model_id
    )
    return (
        fallback_routes,
        str(policy["fallback_policy_id"]),
        int(policy["version"]),
        conditions,
    )


def _persist_policy_routing_facts(
    connection: sqlite3.Connection,
    *,
    decision_id: str,
    tenant_id: str,
    request_id: str,
    trace_id: str,
    virtual_key_id: str | None,
    model_alias_id: str,
    requested_model: str,
    environment: str,
    environment_id: str,
    routing_policy_id: str,
    routing_policy_version: int,
    routing_mode: str,
    selection: RoutingSelectionResult,
    route: GatewayRoute | None,
    event_id: str | None,
    cache_affinity_summary: dict[str, object] | None,
    cooldown_deferred_model_ids: tuple[str, ...],
    as_of: datetime,
) -> None:
    """在调用方事务内写一条 Decision 和完整候选集合事实。"""

    created_at = _to_db_time(as_of)
    summary = {
        "candidates": [
            {
                "role": item.candidate.candidate_role,
                "model_id": item.candidate.model_id,
                "provider_id": item.candidate.provider_id,
                "rank": item.rank,
                "status": item.status,
                "reason_code": item.reason_code,
                "currency": item.currency,
                "estimated_cost_amount": (
                    format(item.estimated_cost_amount, ".15f")
                    if item.estimated_cost_amount is not None
                    else None
                ),
                "comparison_currency": item.comparison_currency,
                "comparison_fx_rate": (
                    format(item.comparison_fx_rate, ".15f")
                    if item.comparison_fx_rate is not None
                    else None
                ),
                "comparison_fx_rate_version": item.comparison_fx_rate_version,
                "comparison_cost_amount": (
                    format(item.comparison_cost_amount, ".15f")
                    if item.comparison_cost_amount is not None
                    else None
                ),
            }
            for item in selection.candidate_results
        ]
    }
    if cache_affinity_summary is not None:
        summary["cache_affinity"] = cache_affinity_summary
    if cooldown_deferred_model_ids:
        summary["cooldown_deferred_model_ids"] = cooldown_deferred_model_ids
    if routing_mode == "fixed":
        # fixed 是迁移兼容模式，成功和失败均保留 PRD-015/035 已公开的决策原因码。
        decision_reason = route.routing_reason if route is not None else "alias.no_active_model"
    else:
        decision_reason = selection.selection_reason
    error_code = None
    # 失败 Decision 仍保存候选拒绝事实，但不得携带 selected Provider 或事件 ID。
    if route is None:
        error_code = (
            _fixed_selection_failure_code(selection)
            if routing_mode == "fixed"
            else "gateway.no_route_available"
        )
    estimate = selection.token_estimate if selection.cost_comparison_performed else None
    quality_config = selection.quality_config
    connection.execute(
        """
        INSERT INTO routing_decisions(
          routing_decision_id, tenant_id, request_id, trace_id, virtual_key_id,
          model_alias_id, requested_model, environment, environment_id, selected_model_id,
          selected_internal_model_id, provider_id, provider_code, decision_status,
          decision_reason, candidate_summary, error_code, created_at,
          routing_policy_id, routing_policy_version, routing_mode, price_as_of,
          estimated_input_tokens, estimated_output_tokens, token_estimate_truncated,
          routing_policy_event_id,
          quality_weight_bps, cost_weight_bps, latency_weight_bps,
          quality_floor_bps, minimum_sample_count, quality_as_of,
          quality_formula_version, quality_scoring_performed
        )
        VALUES(
          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
          ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            decision_id,
            tenant_id,
            request_id,
            trace_id,
            virtual_key_id,
            model_alias_id,
            requested_model,
            environment,
            environment_id,
            route.model_id if route else None,
            route.model_alias if route else None,
            route.provider_id if route else None,
            route.provider_code if route else None,
            "selected" if route else "failed",
            decision_reason,
            json.dumps(summary, ensure_ascii=False, sort_keys=True),
            error_code,
            created_at,
            routing_policy_id,
            routing_policy_version,
            routing_mode,
            created_at if selection.cost_comparison_performed else None,
            estimate.input_tokens if estimate else None,
            estimate.output_tokens if estimate else None,
            int(estimate.truncated) if estimate else None,
            event_id,
            quality_config.quality_weight_bps if quality_config else None,
            quality_config.cost_weight_bps if quality_config else None,
            quality_config.latency_weight_bps if quality_config else None,
            quality_config.quality_floor_bps if quality_config else None,
            quality_config.minimum_sample_count if quality_config else None,
            created_at if quality_config else None,
            selection.quality_formula_version,
            int(selection.quality_scoring_performed),
        ),
    )
    for candidate in selection.candidate_results:
        connection.execute(
            """
            INSERT INTO routing_candidates(
              routing_candidate_id, tenant_id, routing_decision_id, routing_policy_id,
              model_alias_id, model_id, provider_id, candidate_role, configured_priority,
              rank, status, reason_code, health_state, health_fresh, keypool_state,
              price_available, price_source, price_schema_version, currency,
              estimated_cost_amount,
              comparison_currency, comparison_fx_rate, comparison_fx_rate_version,
              comparison_cost_amount,
              model_quality_score_id, quality_statistics_version,
              quality_feedback_version, quality_evidence_state,
              quality_sample_count, quality_success_count, quality_error_count,
              manual_quality_bps, average_latency_ms, quality_component_bps,
              cost_component_bps, latency_component_bps, weighted_score_bps,
              created_at, updated_at
            )
            VALUES(
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                f"rcand_{uuid4().hex}",
                tenant_id,
                decision_id,
                routing_policy_id,
                model_alias_id,
                candidate.candidate.model_id,
                candidate.candidate.provider_id,
                candidate.candidate.candidate_role,
                candidate.candidate.configured_priority,
                candidate.rank,
                candidate.status,
                candidate.reason_code,
                candidate.candidate.health_state,
                int(candidate.candidate.health_fresh),
                candidate.candidate.keypool_state,
                int(candidate.price_available),
                candidate.price_source,
                candidate.price_schema_version,
                candidate.currency,
                (
                    format(candidate.estimated_cost_amount, ".15f")
                    if candidate.estimated_cost_amount is not None
                    else None
                ),
                candidate.comparison_currency,
                (
                    format(candidate.comparison_fx_rate, ".15f")
                    if candidate.comparison_fx_rate is not None
                    else None
                ),
                candidate.comparison_fx_rate_version,
                (
                    format(candidate.comparison_cost_amount, ".15f")
                    if candidate.comparison_cost_amount is not None
                    else None
                ),
                candidate.model_quality_score_id,
                candidate.quality_statistics_version,
                candidate.quality_feedback_version,
                candidate.quality_evidence_state,
                candidate.quality_sample_count,
                candidate.quality_success_count,
                candidate.quality_error_count,
                candidate.manual_quality_bps,
                (
                    format(candidate.average_latency_ms, ".3f")
                    if candidate.average_latency_ms is not None
                    else None
                ),
                candidate.quality_component_bps,
                candidate.cost_component_bps,
                candidate.latency_component_bps,
                candidate.weighted_score_bps,
                created_at,
                created_at,
            ),
        )


def _fixed_selection_failure_code(selection: RoutingSelectionResult) -> str:
    """保持 legacy fixed 无候选时的 Provider/Health/Model 错误优先级。"""

    reasons = {item.reason_code for item in selection.candidate_results}
    # Provider disabled 在 legacy 合同中优先于模型禁用，保持既有 404/409 分类。
    if "routing.candidate.provider_disabled" in reasons:
        return "gateway.provider_not_found"
    # 新鲜 unhealthy 使用专用可重试 503，不能折叠为普通模型禁用。
    if "routing.candidate.provider_unhealthy" in reasons:
        return "gateway.provider_unhealthy"
    return "gateway.model_disabled"


def _parse_fallback_conditions(value: object) -> tuple[str, ...]:
    """严格解析 PRD-036 Fallback 条件的 canonical JSON 快照。

    参数：数据库 ``condition_expr`` 原值。返回：非空、无重复且按固定顺序排列的条件元组。
    约束：任何类型漂移、未知条件、重复或乱序都表示持久快照损坏；异常只使用固定内部码，
    不携带原始条件正文，交由调用方映射统一 Gateway 错误。
    """

    if type(value) is not str:
        # 数据库合同只接受 JSON 文本；bytes/None 的字符串化可能把损坏事实伪装成合法输入。
        raise ValueError("fallback_policy.conditions_invalid")
    try:
        parsed = json.loads(value)
    # 解析异常转换为固定内部码，禁止把原始 JSON 或 decoder 位置扩散到日志和响应。
    except json.JSONDecodeError as exc:
        raise ValueError("fallback_policy.conditions_invalid") from exc
    if not isinstance(parsed, list) or not parsed:
        # 空数组会把 enabled Policy 静默降级为 disabled，必须视为损坏而不是忽略。
        raise ValueError("fallback_policy.conditions_invalid")
    if any(type(item) is not str for item in parsed):
        # bool/int 等值不得通过 str() 冒充枚举，否则混合非法快照仍可能触发上游重试。
        raise ValueError("fallback_policy.conditions_invalid")
    conditions = tuple(parsed)
    if len(conditions) != len(set(conditions)):
        # 重复条件不是新语义，只会破坏 canonical hash/审计解释，因此整体失败关闭。
        raise ValueError("fallback_policy.conditions_invalid")
    if any(item not in _FALLBACK_CONDITION_ORDER for item in conditions):
        # 白名单外错误类别可能扩大重试面，不能静默过滤后继续执行剩余合法项。
        raise ValueError("fallback_policy.conditions_invalid")
    canonical = tuple(item for item in _FALLBACK_CONDITION_ORDER if item in conditions)
    if conditions != canonical:
        # 控制面按固定顺序保存；乱序说明数据绕过了写入口，必须拒绝不确定快照。
        raise ValueError("fallback_policy.conditions_invalid")
    return conditions


def resolve_model_route(
    config: AppConfig,
    *,
    tenant_id: str,
    model_alias: str,
    environment: str = "dev",
    environment_id: str | None = None,
    request_id: str | None = None,
    trace_id: str | None = None,
    virtual_key_id: str | None = None,
) -> GatewayRoute:
    """按当前租户解析模型路由。

    PRD-015 开始，数据面先把请求体里的 `model` 当作稳定 ModelAlias 解析；
    若当前租户和环境没有同名别名，再兼容 PRD-012 的内部模型 ID 直连路径。
    """

    connection = _connect(config)
    requested_model = model_alias.strip()
    normalized_environment = _normalize_environment(environment)
    resolved_environment_id = _resolve_environment_id(
        connection,
        tenant_id,
        normalized_environment,
        environment_id,
    )

    alias_row = connection.execute(
        """
        SELECT
          model_alias_id, alias, environment, environment_id,
          default_model_id, fallback_model_id, status
        FROM model_aliases
        WHERE tenant_id=? AND alias=?
          AND (environment_id=? OR environment_id IS NULL)
          AND environment=?
        """,
        (tenant_id, requested_model, resolved_environment_id, normalized_environment),
    ).fetchone()
    if alias_row:
        if alias_row["status"] != "active":
            _record_routing_decision(
                connection,
                tenant_id=tenant_id,
                request_id=request_id,
                trace_id=trace_id,
                virtual_key_id=virtual_key_id,
                model_alias_id=alias_row["model_alias_id"],
                requested_model=requested_model,
                environment=normalized_environment,
                environment_id=resolved_environment_id,
                decision_status="failed",
                decision_reason="alias.disabled",
                candidate_summary={"alias_status": alias_row["status"]},
                error_code="gateway.model_alias_disabled",
            )
            # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
            raise ApiError("gateway.model_alias_disabled")
        return _resolve_alias_route(
            connection,
            tenant_id=tenant_id,
            alias_row=alias_row,
            requested_model=requested_model,
            environment=normalized_environment,
            environment_id=resolved_environment_id,
            request_id=request_id,
            trace_id=trace_id,
            virtual_key_id=virtual_key_id,
            provider_health_stale_seconds=config.provider_health_stale_seconds,
        )

    row = connection.execute(
        """
        SELECT
          m.model_id, m.tenant_id, m.model_alias, m.status AS model_status,
          COALESCE(m.provider_model_id, m.upstream_model_name) AS upstream_model_name,
          m.capabilities,
          p.provider_id, p.provider_code, p.provider_type, p.name AS provider_name,
          p.auth_type, p.base_url, p.status AS provider_status,
          p.health_status AS provider_health_status,
          p.health_updated_at AS provider_health_updated_at
        FROM models m
        JOIN providers p ON p.provider_id = m.provider_id AND p.tenant_id = m.tenant_id
        WHERE m.tenant_id=? AND m.model_alias=? AND m.status='active'
        """,
        (tenant_id, requested_model),
    ).fetchone()
    if not row:
        _record_routing_decision(
            connection,
            tenant_id=tenant_id,
            request_id=request_id,
            trace_id=trace_id,
            virtual_key_id=virtual_key_id,
            model_alias_id=None,
            requested_model=requested_model,
            environment=normalized_environment,
            environment_id=resolved_environment_id,
            decision_status="failed",
            decision_reason="direct_model.not_found",
            candidate_summary={},
            error_code="gateway.model_not_found",
        )
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("gateway.model_not_found")
    if row["provider_status"] != "active":
        _record_routing_decision(
            connection,
            tenant_id=tenant_id,
            request_id=request_id,
            trace_id=trace_id,
            virtual_key_id=virtual_key_id,
            model_alias_id=None,
            requested_model=requested_model,
            environment=normalized_environment,
            environment_id=resolved_environment_id,
            decision_status="failed",
            decision_reason="direct_model.provider_disabled",
            candidate_summary={
                "provider_id": row["provider_id"],
                "provider_status": row["provider_status"],
            },
            error_code="gateway.provider_not_found",
        )
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError("gateway.provider_not_found")
    if _provider_is_fresh_unhealthy(row, config.provider_health_stale_seconds):
        _record_routing_decision(
            connection,
            tenant_id=tenant_id,
            request_id=request_id,
            trace_id=trace_id,
            virtual_key_id=virtual_key_id,
            model_alias_id=None,
            requested_model=requested_model,
            environment=normalized_environment,
            environment_id=resolved_environment_id,
            decision_status="failed",
            decision_reason="direct_model.provider_unhealthy",
            candidate_summary={
                "provider_id": row["provider_id"],
                "rejected_reason": "provider_unhealthy",
            },
            error_code="gateway.provider_unhealthy",
        )
        # 直接模型没有下一候选，必须返回可重试统一错误且由上层继续写失败请求日志。
        raise ApiError("gateway.provider_unhealthy")

    route = _route_from_row(
        row,
        requested_model=requested_model,
        environment=normalized_environment,
        environment_id=resolved_environment_id,
        model_alias_id=None,
        public_model_alias=None,
        routing_reason="direct_model",
    )
    _record_route_selected(
        connection,
        route,
        request_id=request_id,
        trace_id=trace_id,
        virtual_key_id=virtual_key_id,
        candidate_summary={"direct_model_id": route.model_id},
    )
    return route


def _resolve_alias_route(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    alias_row: sqlite3.Row,
    requested_model: str,
    environment: str,
    environment_id: str,
    request_id: str | None,
    trace_id: str | None,
    virtual_key_id: str | None,
    provider_health_stale_seconds: int,
) -> GatewayRoute:
    """在 模型目录 内部复用 resolve alias route 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    candidates: list[dict[str, object]] = []
    for role, model_id in (
        ("default", alias_row["default_model_id"]),
        ("fallback", alias_row["fallback_model_id"]),
    ):
        # 路由和模型判断决定实际上游调用，必须保持可解释和可追踪。
        if not model_id:
            continue
        row = _load_model_route_by_id(connection, tenant_id, model_id)
        if row is None:
            candidates.append(
                {"role": role, "model_id": model_id, "rejected_reason": "model_missing"}
            )
            continue
        # 路由和模型判断决定实际上游调用，必须保持可解释和可追踪。
        if row["model_status"] != "active":
            candidates.append(
                {
                    "role": role,
                    "model_id": model_id,
                    "internal_model_id": row["model_alias"],
                    "model_status": row["model_status"],
                    "rejected_reason": "model_disabled",
                }
            )
            continue
        # Provider 分支会影响上游选择和密钥池，必须显式保留决策边界。
        # Provider 分支会影响上游选择和密钥池，必须显式保留决策边界。
        if row["provider_status"] != "active":
            candidates.append(
                {
                    "role": role,
                    "model_id": model_id,
                    "internal_model_id": row["model_alias"],
                    "provider_id": row["provider_id"],
                    "provider_status": row["provider_status"],
                    "rejected_reason": "provider_disabled",
                }
            )
            continue
        # 别名可在发起上游请求前跳过新鲜异常候选，不执行请求失败后的 PRD-036 Fallback。
        if _provider_is_fresh_unhealthy(row, provider_health_stale_seconds):
            candidates.append(
                {
                    "role": role,
                    "model_id": model_id,
                    "internal_model_id": row["model_alias"],
                    "provider_id": row["provider_id"],
                    "rejected_reason": "provider_unhealthy",
                }
            )
            continue
        route = _route_from_row(
            row,
            requested_model=requested_model,
            environment=environment,
            environment_id=environment_id,
            model_alias_id=alias_row["model_alias_id"],
            public_model_alias=alias_row["alias"],
            routing_reason=f"alias.{role}",
        )
        candidates.append(
            {
                "role": role,
                "model_id": route.model_id,
                "internal_model_id": route.model_alias,
                "provider_id": route.provider_id,
                "selected": True,
            }
        )
        _record_route_selected(
            connection,
            route,
            request_id=request_id,
            trace_id=trace_id,
            virtual_key_id=virtual_key_id,
            candidate_summary={"candidates": candidates},
        )
        return route

    error_code = _alias_failure_code(candidates)
    _record_routing_decision(
        connection,
        tenant_id=tenant_id,
        request_id=request_id,
        trace_id=trace_id,
        virtual_key_id=virtual_key_id,
        model_alias_id=alias_row["model_alias_id"],
        requested_model=requested_model,
        environment=environment,
        environment_id=environment_id,
        decision_status="failed",
        decision_reason="alias.no_active_model",
        candidate_summary={"candidates": candidates},
        error_code=error_code,
    )
    # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
    raise ApiError(error_code)


def _load_model_route_by_id(
    connection: sqlite3.Connection, tenant_id: str, model_id: str
) -> sqlite3.Row | None:
    """按当前租户加载 模型目录 的 load model route by id 目标对象，供后续写入或详情响应复用。

    安全：查询必须带 tenant_id 或可信上下文，避免跨租户读取。
    """

    return connection.execute(
        """
        SELECT
          m.model_id, m.tenant_id, m.model_alias, m.status AS model_status,
          COALESCE(m.provider_model_id, m.upstream_model_name) AS upstream_model_name,
          m.capabilities,
          p.provider_id, p.provider_code, p.provider_type, p.name AS provider_name,
          p.auth_type, p.base_url, p.status AS provider_status,
          p.health_status AS provider_health_status,
          p.health_updated_at AS provider_health_updated_at
        FROM models m
        JOIN providers p ON p.provider_id = m.provider_id AND p.tenant_id = m.tenant_id
        WHERE m.tenant_id=? AND m.model_id=?
        """,
        (tenant_id, model_id),
    ).fetchone()


def _route_from_row(
    row: sqlite3.Row,
    *,
    requested_model: str,
    environment: str,
    environment_id: str,
    model_alias_id: str | None,
    public_model_alias: str | None,
    routing_reason: str,
) -> GatewayRoute:
    """在 模型目录 内部复用 route from row 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    return GatewayRoute(
        tenant_id=row["tenant_id"],
        model_id=row["model_id"],
        model_alias=row["model_alias"],
        upstream_model_name=row["upstream_model_name"],
        capabilities=_parse_capabilities(row["capabilities"]),
        provider_id=row["provider_id"],
        provider_code=row["provider_code"],
        provider_type=row["provider_type"],
        provider_auth_type=row["auth_type"],
        provider_name=row["provider_name"],
        provider_base_url=row["base_url"],
        requested_model=requested_model,
        environment=environment,
        environment_id=environment_id,
        model_alias_id=model_alias_id,
        public_model_alias=public_model_alias,
        routing_reason=routing_reason,
    )


def _record_route_selected(
    connection: sqlite3.Connection,
    route: GatewayRoute,
    *,
    request_id: str | None,
    trace_id: str | None,
    virtual_key_id: str | None,
    candidate_summary: dict[str, object],
) -> None:
    """在 模型目录 内部复用 record route selected 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    _record_routing_decision(
        connection,
        tenant_id=route.tenant_id,
        request_id=request_id,
        trace_id=trace_id,
        virtual_key_id=virtual_key_id,
        model_alias_id=route.model_alias_id,
        requested_model=route.requested_model or route.model_alias,
        environment=route.environment,
        environment_id=route.environment_id,
        decision_status="selected",
        decision_reason=route.routing_reason,
        candidate_summary=candidate_summary,
        error_code=None,
        selected_model_id=route.model_id,
        selected_internal_model_id=route.model_alias,
        provider_id=route.provider_id,
        provider_code=route.provider_code,
    )
    logger.info(
        "route.selected",
        extra={
            "event": "route.selected",
            "request_id": request_id,
            "trace_id": trace_id or request_id,
            "tenant_id": route.tenant_id,
            "object_id": route.model_alias_id or route.model_id,
            "payload": {
                "requested_model": route.requested_model or route.model_alias,
                "model_alias_id": route.model_alias_id,
                "selected_model_id": route.model_id,
                "selected_internal_model_id": route.model_alias,
                "provider_id": route.provider_id,
                "provider_code": route.provider_code,
                "decision_reason": route.routing_reason,
            },
        },
    )


def _record_routing_decision(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    request_id: str | None,
    trace_id: str | None,
    virtual_key_id: str | None,
    model_alias_id: str | None,
    requested_model: str,
    environment: str,
    environment_id: str | None,
    decision_status: str,
    decision_reason: str,
    candidate_summary: dict[str, object],
    error_code: str | None,
    selected_model_id: str | None = None,
    selected_internal_model_id: str | None = None,
    provider_id: str | None = None,
    provider_code: str | None = None,
) -> None:
    """在 模型目录 内部复用 record routing decision 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    # 链路标识用于串联响应、日志和调试台，缺失时必须走兜底。
    if not request_id:
        return
    try:
        connection.execute(
            """
            INSERT INTO routing_decisions(
              routing_decision_id, tenant_id, request_id, trace_id, virtual_key_id,
              model_alias_id, requested_model, environment, environment_id, selected_model_id,
              selected_internal_model_id, provider_id, provider_code, decision_status,
              decision_reason, candidate_summary, error_code, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                tenant_id,
                request_id,
                trace_id or request_id,
                virtual_key_id,
                model_alias_id,
                requested_model,
                environment,
                environment_id,
                selected_model_id,
                selected_internal_model_id,
                provider_id,
                provider_code,
                decision_status,
                decision_reason,
                json.dumps(candidate_summary, ensure_ascii=False, sort_keys=True),
                error_code,
                _to_db_time(_utcnow()),
            ),
        )
        connection.commit()
        if decision_status == "failed":
            logger.info(
                "route.failed",
                extra={
                    "event": "route.failed",
                    "request_id": request_id,
                    "trace_id": trace_id or request_id,
                    "tenant_id": tenant_id,
                    "object_id": model_alias_id,
                    "payload": {
                        "requested_model": requested_model,
                        "environment": environment,
                        "decision_reason": decision_reason,
                        "error_code": error_code,
                    },
                },
            )
    # 数据解析失败只返回安全摘要，原始异常不直接暴露给用户。
    except sqlite3.Error:
        connection.rollback()
        logger.warning(
            "route.decision_log_failed",
            extra={
                "event": "route.decision_log_failed",
                "tenant_id": tenant_id,
                "request_id": request_id,
                "trace_id": trace_id or request_id,
                "error_code": error_code,
            },
        )


def _alias_failure_code(candidates: list[dict[str, object]]) -> str:
    """在 模型目录 内部复用 alias failure code 辅助逻辑，集中处理边界值和错误映射。

    边界：该函数不应绕过租户隔离、统一错误码或敏感信息脱敏约束。
    """

    # Provider 分支会影响上游选择和密钥池，必须显式保留决策边界。
    # Provider 分支会影响上游选择和密钥池，必须显式保留决策边界。
    if any(item.get("rejected_reason") == "provider_disabled" for item in candidates):
        return "gateway.provider_not_found"
    if any(item.get("rejected_reason") == "provider_unhealthy" for item in candidates):
        return "gateway.provider_unhealthy"
    return "gateway.model_disabled"


def _provider_is_fresh_unhealthy(row: sqlite3.Row, stale_seconds: int) -> bool:
    """只拒绝具有新版时间证据且仍在新鲜窗口内的 unhealthy Provider。

    旧 `/test` 或调度停止产生的无时间/过期状态按 unknown Fail Open，避免永久摘流。
    """

    # 无新版证据时间的旧状态不能摘流；这也是 0025 迁移前结果的兼容边界。
    if row["provider_health_status"] != "unhealthy" or not row["provider_health_updated_at"]:
        return False
    raw = str(row["provider_health_updated_at"]).replace("Z", "+00:00")
    checked_at = datetime.fromisoformat(raw)
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=UTC)
    return checked_at.astimezone(UTC) >= datetime.now(UTC) - timedelta(seconds=stale_seconds)


def _resolve_environment_id(
    connection: sqlite3.Connection,
    tenant_id: str,
    environment: str,
    environment_id: str | None,
) -> str:
    """在数据面按可信租户和代码解析 active Environment ID。

    正常 Gateway 路径传入 Virtual Key 已绑定 ID；按代码解析仅用于兼容内部调用，
    两种路径都必须约束 tenant_id、代码和状态。
    """

    normalized_id = str(environment_id or "").strip()
    if normalized_id:
        row = connection.execute(
            """
            SELECT environment_id, code, status
            FROM environments
            WHERE tenant_id=? AND environment_id=?
            """,
            (tenant_id, normalized_id),
        ).fetchone()
    else:
        row = connection.execute(
            """
            SELECT environment_id, code, status
            FROM environments
            WHERE tenant_id=? AND code=?
            """,
            (tenant_id, environment),
        ).fetchone()
    if row is None or row["code"] != environment or row["status"] != "active":
        # 环境缺失、禁用或 ID/code 漂移都不能继续解析别名，统一按环境不可用阻断。
        raise ApiError("gateway.environment_disabled")
    return str(row["environment_id"])


def _normalize_environment(value: str | None) -> str:
    """规范化 模型目录 的 normalize environment 参数，拦截空值、非法枚举或越界数字。

    边界：归一化失败统一抛业务错误码，避免脏数据进入数据库或路由决策。
    """

    environment = (value or "dev").strip().lower()
    if environment in {"dev", "test", "prod"}:
        return environment
    return "dev"


def _parse_capabilities(raw: object) -> dict[str, object]:
    """解析 模型目录 的 parse capabilities 数据，把数据库或外部输入转换为内部稳定类型。

    边界：解析失败要么返回空值口径，要么抛统一错误，不能把底层异常透出给 API。
    """

    try:
        parsed = json.loads(str(raw or "{}"))
        return parsed if isinstance(parsed, dict) else {}
    # 数据解析失败只返回安全摘要，原始异常不直接暴露给用户。
    except json.JSONDecodeError:
        return {}


def _connect(config: AppConfig) -> sqlite3.Connection:
    """创建 模型目录 所需的数据库连接，并把基础设施异常映射到统一错误模型。

    边界：调用方只看到业务错误码，不直接暴露数据库路径、SQL 或连接细节。
    """

    try:
        return connect_database(config.database_url)
    # 数据库异常需要折叠为统一错误码并保留 request_id，避免底层细节外泄。
    except DbError as exc:
        # 关键异常统一映射为业务错误码，避免底层实现细节穿透到 API 响应。
        raise ApiError(exc.code, message=exc.message, suggestion=exc.suggestion) from exc


def _to_db_time(value: datetime) -> str:
    """把 模型目录 的 UTC 时间序列化为数据库 ISO 字符串。

    边界：统一时间口径可避免分页、审计和测试在时区上产生歧义。
    """

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utcnow() -> datetime:
    """返回 模型目录 使用的当前 UTC 时间，去掉微秒以保持数据库字段稳定。

    边界：所有状态更新时间共用该口径，避免同一事务内出现混合时间格式。
    """

    return datetime.now(UTC).replace(microsecond=0)
