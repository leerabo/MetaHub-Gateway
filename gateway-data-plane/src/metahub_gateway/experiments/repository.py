"""PRD-075 在线实验的数据面热路径仓储，负责租户隔离分桶、精确路由与租约围栏。"""

from __future__ import annotations

import hmac
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.experiments import (
    ExperimentAllocationPlan,
    build_experiment_buckets,
    build_experiment_key_fingerprint,
    build_experiment_route_config_fingerprint,
    calculate_variant_targets,
    choose_experiment_variant,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.provider_adapter_spec import (
    CATALOG_HASH,
    CATALOG_VERSION,
    ProviderAdapterSpecError,
    validate_provider_auth,
)

from metahub_gateway.providers.factory import adapter_for_route
from metahub_gateway.providers.keypool import snapshot_provider_key_pool
from metahub_gateway.providers.openai_compatible import upstream_url
from metahub_gateway.routing.models import GatewayRoute, GatewayRoutePlan

from .guard import SendPermissionResult, process_lease_owner_id
from .models import (
    ExperimentExecutionHandle,
    ExperimentRouteResolution,
    ExperimentTerminalIntent,
)


def resolve_experiment_route(
    config: AppConfig,
    *,
    tenant_id: str,
    request_id: str,
    trace_id: str,
    virtual_key_id: str,
    project_id: str,
    application_id: str | None,
    environment_id: str,
    environment_code: str,
    requested_model: str,
    streaming: bool,
) -> ExperimentRouteResolution:
    """在认证租户作用域内原子分配实验臂；只有权威证明未命中时才允许旁路。"""

    connection: sqlite3.Connection | None = None
    try:
        connection = connect_database(config.database_url)
        connection.execute("BEGIN IMMEDIATE")
        now, now_db = _database_now(connection)
        _require_scope_claim(
            connection,
            tenant_id=tenant_id,
            request_id=request_id,
            trace_id=trace_id,
            virtual_key_id=virtual_key_id,
            project_id=project_id,
            application_id=application_id,
            environment_id=environment_id,
            environment_code=environment_code,
        )
        alias = connection.execute(
            """
            SELECT model_alias_id, alias, environment, environment_id, status
            FROM model_aliases
            WHERE tenant_id=? AND alias=? AND environment_id=? AND environment=?
            """,
            (tenant_id, requested_model, environment_id, environment_code),
        ).fetchone()
        # A concrete model request is explicitly outside PRD-075. Authority was already checked
        # by the caller, so absence of an exact canonical Alias is an authoritative bypass.
        if alias is None or str(alias["status"]) != "active":
            connection.rollback()
            return ExperimentRouteResolution.bypass()

        experiment = connection.execute(
            """
            SELECT *
            FROM model_experiments
            WHERE tenant_id=? AND project_id=? AND environment_id=?
              AND model_alias_id=? AND status='active'
            """,
            (tenant_id, project_id, environment_id, alias["model_alias_id"]),
        ).fetchone()
        if experiment is None:
            connection.rollback()
            return ExperimentRouteResolution.bypass()
        if now >= _parse_db_time(experiment["sampling_deadline_at"]):
            _end_expired_sampling(connection, experiment, now_db)
            connection.commit()
            return ExperimentRouteResolution.bypass()

        expected_key_fingerprint = build_experiment_key_fingerprint(
            config.auth_token_secret,
            tenant_id=tenant_id,
            experiment_id=str(experiment["experiment_id"]),
        )
        if not hmac.compare_digest(
            expected_key_fingerprint,
            str(experiment["sampling_key_fingerprint"]),
        ):
            # 活跃实验的采样密钥一旦漂移必须在调用 Provider 前失败关闭，不能静默重新分桶。
            raise ValueError("experiment_sampling_key_drift")
        buckets = build_experiment_buckets(
            config.auth_token_secret,
            tenant_id=tenant_id,
            experiment_id=str(experiment["experiment_id"]),
            request_id=request_id,
        )
        if buckets.sample_bucket >= int(experiment["traffic_sample_bps"]):
            connection.rollback()
            return ExperimentRouteResolution.bypass()

        plan = calculate_variant_targets(
            int(experiment["target_sample_count"]),
            int(experiment["model_a_allocation_bps"]),
        )
        _validate_allocation_plan(plan, experiment)
        allocation = choose_experiment_variant(
            plan,
            assigned_count=int(experiment["assigned_count"]),
            model_a_count=int(experiment["model_a_assigned_count"]),
            model_b_count=int(experiment["model_b_assigned_count"]),
            variant_bucket=buckets.variant_bucket,
        )
        arm_prefix = "model_a" if allocation.variant == "a" else "model_b"
        model_id = str(experiment[f"{arm_prefix}_id"])
        provider_id = str(experiment[f"{arm_prefix}_provider_id"])
        provider_code = str(experiment[f"{arm_prefix}_provider_code"])
        frozen_route_fingerprint = str(
            experiment[f"{arm_prefix}_route_config_fingerprint"]
        )
        arm = _load_arm(connection, tenant_id=tenant_id, model_id=model_id)
        if arm is None:
            # 已冻结实验臂缺少同租户模型事实表示 owner 图损坏，不能退回普通路由。
            raise ValueError("experiment_arm_missing")
        route = _route_from_arm(
            arm,
            requested_model=requested_model,
            environment_code=environment_code,
            environment_id=environment_id,
            model_alias_id=str(alias["model_alias_id"]),
            public_model_alias=str(alias["alias"]),
            routing_reason=f"experiment.variant_{allocation.variant}",
        )
        route_available = _arm_is_available(
            config,
            connection,
            arm,
            route,
            now=now,
            expected_model_id=model_id,
            expected_provider_id=provider_id,
            expected_provider_code=provider_code,
            frozen_route_fingerprint=frozen_route_fingerprint,
            streaming=streaming,
        )

        assignment_id = f"exas_{uuid4().hex}"
        execution_id = f"exrn_{uuid4().hex}"
        settlement_id = f"exst_{uuid4().hex}"
        routing_decision_id = str(uuid4())
        candidate_summary = json.dumps(
            {
                "algorithm_version": buckets.algorithm_version,
                "assignment_id": assignment_id,
                "assignment_reason": allocation.assignment_reason,
                "experiment_id": str(experiment["experiment_id"]),
                "variant": allocation.variant,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        error_code = None if route_available else "gateway.experiment_variant_unavailable"
        _insert_routing_decision(
            connection,
            routing_decision_id=routing_decision_id,
            tenant_id=tenant_id,
            request_id=request_id,
            trace_id=trace_id,
            virtual_key_id=virtual_key_id,
            model_alias_id=str(alias["model_alias_id"]),
            requested_model=requested_model,
            environment_code=environment_code,
            environment_id=environment_id,
            route=route,
            candidate_summary=candidate_summary,
            selected=route_available,
            error_code=error_code,
            created_at=now_db,
        )
        connection.execute(
            """
            INSERT INTO experiment_assignments(
              assignment_id,tenant_id,experiment_id,request_id,trace_id,virtual_key_id,
              routing_decision_id,execution_id,usage_settlement_id,assignment_sequence,
              variant,model_id,provider_id,provider_code,sample_bucket,variant_bucket,
              assignment_reason,algorithm_version,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                assignment_id,
                tenant_id,
                experiment["experiment_id"],
                request_id,
                trace_id,
                virtual_key_id,
                routing_decision_id,
                execution_id,
                settlement_id,
                allocation.assigned_count_after,
                allocation.variant,
                model_id,
                provider_id,
                provider_code,
                buckets.sample_bucket,
                buckets.variant_bucket,
                allocation.assignment_reason,
                buckets.algorithm_version,
                now_db,
            ),
        )
        absolute_deadline = now + timedelta(seconds=int(experiment["execution_max_seconds"]))
        lease_expires_at = min(
            absolute_deadline,
            now + timedelta(seconds=config.experiment_execution_lease_seconds),
        )
        lease_token = uuid4().hex
        lease_owner_id = process_lease_owner_id()
        connection.execute(
            """
            INSERT INTO experiment_request_executions(
              execution_id,tenant_id,experiment_id,assignment_id,request_id,status,generation,
              lease_owner_id,lease_token,lease_expires_at,absolute_deadline_at,send_fence_at,
              created_at,updated_at
            ) VALUES(?,?,?,?,?,'assigned',1,?,?,?,?,NULL,?,?)
            """,
            (
                execution_id,
                tenant_id,
                experiment["experiment_id"],
                assignment_id,
                request_id,
                lease_owner_id,
                lease_token,
                _to_db_time(lease_expires_at),
                _to_db_time(absolute_deadline),
                now_db,
                now_db,
            ),
        )
        # 可用臂先创建待结算事实；不可用臂则在同一事务冻结未暴露失败，严禁跨臂回退。
        if route_available:
            connection.execute(
                """
                INSERT INTO experiment_usage_settlements(
                  usage_settlement_id,tenant_id,experiment_id,assignment_id,request_id,variant,
                  model_id,provider_id,status,usage_ledger_id,finops_ledger_entry_id,gross_cny_micros,
                  normalization_status,settlement_reason,settlement_version,settled_at,created_at,updated_at
                ) VALUES(
                  ?,?,?,?,?,?,?,?,'pending',NULL,NULL,NULL,
                  'pending','awaiting_usage',1,NULL,?,?
                )
                """,
                (
                    settlement_id,
                    tenant_id,
                    experiment["experiment_id"],
                    assignment_id,
                    request_id,
                    allocation.variant,
                    model_id,
                    provider_id,
                    now_db,
                    now_db,
                ),
            )
            handle = ExperimentExecutionHandle(
                tenant_id=tenant_id,
                experiment_id=str(experiment["experiment_id"]),
                assignment_id=assignment_id,
                execution_id=execution_id,
                request_id=request_id,
                trace_id=trace_id,
                generation=1,
                lease_owner_id=lease_owner_id,
                lease_token=lease_token,
                absolute_deadline=absolute_deadline,
            )
            route_plan = GatewayRoutePlan(
                initial_route=route,
                initial_role="primary",
                routing_decision_id=routing_decision_id,
            )
            resolution = ExperimentRouteResolution.selected(route_plan, handle)
        else:
            _insert_variant_unavailable_terminal(
                connection,
                tenant_id=tenant_id,
                experiment_id=str(experiment["experiment_id"]),
                assignment_id=assignment_id,
                execution_id=execution_id,
                settlement_id=settlement_id,
                request_id=request_id,
                variant=allocation.variant,
                model_id=model_id,
                provider_id=provider_id,
                now_db=now_db,
            )
            resolution = ExperimentRouteResolution.failed(
                route,
                "gateway.experiment_variant_unavailable",
            )
        connection.commit()
        return resolution
    # 已规范化 API 错误保持原错误码，但必须回滚尚未提交的分桶与计数事实。
    except ApiError:
        if connection is not None:
            connection.rollback()
        raise
    except (
        DbError,
        ProviderAdapterSpecError,
        sqlite3.Error,
        TypeError,
        ValueError,
    ) as exc:
        if connection is not None:
            connection.rollback()
        # 数据库、适配器或 owner 图异常统一收敛为安全错误，不能泄露内部异常或悄悄旁路实验。
        raise ApiError("gateway.experiment_unavailable") from exc
    finally:
        if connection is not None:
            connection.close()


class SqliteExperimentExecutionStore:
    """以 0068 租约和代际围栏推进实验请求，防止失效进程继续调用上游或改写终态。"""

    def __init__(self, config: AppConfig) -> None:
        """保存只读运行配置；租户和执行 owner 始终从不可伪造的 handle 取得。"""

        self.config = config

    def renew_execution(
        self,
        handle: ExperimentExecutionHandle,
        *,
        lease_seconds: int,
    ) -> bool:
        """仅为仍由当前 owner 持有且未到绝对截止的执行续租，失败时返回关闭信号。"""

        connection: sqlite3.Connection | None = None
        try:
            connection = connect_database(self.config.database_url)
            now, now_db = _database_now(connection)
            lease_expires = min(
                handle.absolute_deadline,
                now + timedelta(seconds=lease_seconds),
            )
            cursor = connection.execute(
                """
                UPDATE experiment_request_executions
                SET lease_expires_at=?,updated_at=?
                WHERE tenant_id=? AND execution_id=? AND assignment_id=? AND request_id=?
                  AND generation=? AND lease_owner_id=? AND lease_token=?
                  AND status IN ('assigned','provider_calling')
                  AND unixepoch(lease_expires_at)>unixepoch(?)
                  AND unixepoch(absolute_deadline_at)>unixepoch(?)
                """,
                (
                    _to_db_time(lease_expires),
                    now_db,
                    handle.tenant_id,
                    handle.execution_id,
                    handle.assignment_id,
                    handle.request_id,
                    handle.generation,
                    handle.lease_owner_id,
                    handle.lease_token,
                    now_db,
                    now_db,
                ),
            )
            connection.commit()
            return cursor.rowcount == 1
        # 续租异常按失去租约处理，调用方必须停止等待和后续客户端输出。
        except (DbError, sqlite3.Error, TypeError, ValueError):
            if connection is not None:
                connection.rollback()
            return False
        finally:
            if connection is not None:
                connection.close()

    def acquire_send_permission(
        self,
        handle: ExperimentExecutionHandle,
        *,
        lease_seconds: int,
    ) -> SendPermissionResult:
        """以单行 CAS 取得最终 Provider 发送许可，并区分租约过期与 owner 丢失。"""

        connection: sqlite3.Connection | None = None
        try:
            connection = connect_database(self.config.database_url)
            connection.execute("BEGIN IMMEDIATE")
            now, now_db = _database_now(connection)
            lease_expires = min(
                handle.absolute_deadline,
                now + timedelta(seconds=lease_seconds),
            )
            cursor = connection.execute(
                """
                UPDATE experiment_request_executions
                SET status='provider_calling',send_fence_at=?,lease_expires_at=?,updated_at=?
                WHERE tenant_id=? AND execution_id=? AND assignment_id=? AND request_id=?
                  AND generation=? AND lease_owner_id=? AND lease_token=? AND status='assigned'
                  AND unixepoch(lease_expires_at)>unixepoch(?)
                  AND unixepoch(absolute_deadline_at)>unixepoch(?)
                """,
                (
                    now_db,
                    _to_db_time(lease_expires),
                    now_db,
                    handle.tenant_id,
                    handle.execution_id,
                    handle.assignment_id,
                    handle.request_id,
                    handle.generation,
                    handle.lease_owner_id,
                    handle.lease_token,
                    now_db,
                    now_db,
                ),
            )
            if cursor.rowcount == 1:
                connection.commit()
                return "acquired"
            row = connection.execute(
                """
                SELECT generation,lease_owner_id,lease_token,lease_expires_at,absolute_deadline_at
                FROM experiment_request_executions
                WHERE tenant_id=? AND execution_id=?
                """,
                (handle.tenant_id, handle.execution_id),
            ).fetchone()
            connection.rollback()
            if (
                row is not None
                and int(row["generation"]) == handle.generation
                and hmac.compare_digest(str(row["lease_owner_id"]), handle.lease_owner_id)
                and hmac.compare_digest(str(row["lease_token"]), handle.lease_token)
                and (
                    _parse_db_time(row["lease_expires_at"]) <= now
                    or _parse_db_time(row["absolute_deadline_at"]) <= now
                )
            ):
                return "expired"
            return "lost"
        # 无法证明当前 owner 仍有效时一律视为租约丢失，绝不尝试调用 Provider。
        except (DbError, sqlite3.Error, TypeError, ValueError):
            if connection is not None:
                connection.rollback()
            return "lost"
        finally:
            if connection is not None:
                connection.close()

    def finalize_execution(
        self,
        handle: ExperimentExecutionHandle,
        intent: ExperimentTerminalIntent,
    ) -> bool:
        """校验租户 owner 和终态矩阵后，原子写入不可变 Outcome、Execution 与结算状态。"""

        connection: sqlite3.Connection | None = None
        try:
            _validate_terminal_intent(intent)
            connection = connect_database(self.config.database_url)
            connection.execute("BEGIN IMMEDIATE")
            now, now_db = _database_now(connection)
            owner = connection.execute(
                """
                SELECT execution.status,execution.generation,execution.lease_owner_id,
                       execution.lease_token,execution.lease_expires_at,
                       execution.absolute_deadline_at,assignment.experiment_id,
                       assignment.variant,assignment.model_id,assignment.provider_id,
                       assignment.request_id,assignment.trace_id
                FROM experiment_request_executions execution
                JOIN experiment_assignments assignment
                  ON assignment.tenant_id=execution.tenant_id
                 AND assignment.assignment_id=execution.assignment_id
                WHERE execution.tenant_id=? AND execution.execution_id=?
                  AND execution.assignment_id=?
                """,
                (handle.tenant_id, handle.execution_id, handle.assignment_id),
            ).fetchone()
            if not _terminal_owner_matches(owner, handle, now=now):
                connection.rollback()
                return False
            connection.execute(
                """
                INSERT INTO experiment_request_outcomes(
                  outcome_id,tenant_id,experiment_id,assignment_id,execution_id,request_id,
                  variant,model_id,provider_id,gateway_status,exposure_status,outcome_reason,
                  error_code,routing_attempt_id,stream_session_id,quality_eligible,
                  quality_succeeded,latency_ms,recovery_generation,facts_at,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"exoc_{uuid4().hex}",
                    handle.tenant_id,
                    owner["experiment_id"],
                    handle.assignment_id,
                    handle.execution_id,
                    handle.request_id,
                    owner["variant"],
                    owner["model_id"],
                    owner["provider_id"],
                    intent.gateway_outcome,
                    intent.exposure,
                    intent.outcome_reason,
                    intent.error_code,
                    intent.routing_attempt_id,
                    intent.stream_session_id,
                    1 if intent.quality_eligible else 0,
                    (
                        1 if intent.quality_succeeded else 0
                        if intent.quality_succeeded is not None
                        else None
                    ),
                    intent.latency_ms,
                    handle.generation,
                    now_db,
                    now_db,
                ),
            )
            cursor = connection.execute(
                """
                UPDATE experiment_request_executions
                SET status='terminal',updated_at=?
                WHERE tenant_id=? AND execution_id=? AND assignment_id=? AND request_id=?
                  AND generation=? AND lease_owner_id=? AND lease_token=?
                  AND status IN ('assigned','provider_calling')
                  AND unixepoch(lease_expires_at)>unixepoch(?)
                  AND unixepoch(absolute_deadline_at)>unixepoch(?)
                """,
                (
                    now_db,
                    handle.tenant_id,
                    handle.execution_id,
                    handle.assignment_id,
                    handle.request_id,
                    handle.generation,
                    handle.lease_owner_id,
                    handle.lease_token,
                    now_db,
                    now_db,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return False
            _settle_terminal_disposition(
                connection,
                handle,
                disposition=intent.settlement_disposition,
                now_db=now_db,
            )
            connection.commit()
            return True
        # 终态事务失败只返回未提交，禁止用部分事实冒充已完成；后续由租约恢复器接管。
        except (DbError, sqlite3.Error, TypeError, ValueError):
            if connection is not None:
                connection.rollback()
            return False
        finally:
            if connection is not None:
                connection.close()


def _require_scope_claim(
    connection: sqlite3.Connection,
    **expected: str | None,
) -> None:
    """验证认证事务冻结的租户、项目和环境 scope，拒绝认证后 Virtual Key 改绑漂移。"""

    row = connection.execute(
        """
        SELECT tenant_id,request_id,trace_id,virtual_key_id,project_id,application_id,
               environment_id,environment_code
        FROM gateway_request_scope_claims
        WHERE tenant_id=? AND request_id=?
        """,
        (expected["tenant_id"], expected["request_id"]),
    ).fetchone()
    if row is None or any(row[key] != value for key, value in expected.items()):
        # 缺失或不一致的 scope claim 无法证明实验租户归属，必须在 Provider 前失败关闭。
        raise ValueError("experiment_scope_claim_mismatch")


def _end_expired_sampling(
    connection: sqlite3.Connection,
    experiment: sqlite3.Row,
    now_db: str,
) -> None:
    """用数据库时钟把到期实验单向切入反馈收集，并冻结反馈与结算截止时间。"""

    cursor = connection.execute(
        """
        UPDATE model_experiments
        SET status='collecting_feedback',sampling_ended_at=?,
            feedback_deadline_at=strftime(
              '%Y-%m-%dT%H:%M:%SZ',?,'+'||feedback_window_seconds||' seconds'
            ),
            settlement_deadline_at=max(
              strftime('%Y-%m-%dT%H:%M:%SZ',?,'+'||feedback_window_seconds||' seconds'),
              strftime(
                '%Y-%m-%dT%H:%M:%SZ',?,
                '+'||(execution_max_seconds+settlement_grace_seconds)||' seconds'
              )
            ),updated_at=?
        WHERE tenant_id=? AND experiment_id=? AND status='active'
        """,
        (
            now_db,
            now_db,
            now_db,
            now_db,
            now_db,
            experiment["tenant_id"],
            experiment["experiment_id"],
        ),
    )
    if cursor.rowcount != 1:
        # 状态 CAS 未命中表示并发状态已漂移，当前事务不能继续按旧快照分配样本。
        raise ValueError("experiment_sampling_transition_failed")


def _validate_allocation_plan(plan: ExperimentAllocationPlan, experiment: sqlite3.Row) -> None:
    """复算 A/B 整数配额并核对冻结值，避免配置漂移污染实验比例。"""

    if (
        plan.model_a_target_count != int(experiment["model_a_target_count"])
        or plan.model_b_target_count != int(experiment["model_b_target_count"])
        or int(experiment["model_b_allocation_bps"])
        != 10_000 - plan.model_a_allocation_bps
    ):
        # 任一目标或比例不一致都表示冻结配置损坏，不能继续消耗实验样本。
        raise ValueError("experiment_allocation_drift")


def _load_arm(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    model_id: str,
) -> sqlite3.Row | None:
    """只在指定租户内联表读取模型与 Provider 执行配置，不接受客户端 owner。"""

    return connection.execute(
        """
        SELECT model.model_id,model.tenant_id,model.model_alias,model.status AS model_status,
               COALESCE(model.provider_model_id,model.upstream_model_name) AS upstream_model_name,
               model.capabilities,model.context_window,
               provider.provider_id,provider.provider_code,provider.provider_type,
               provider.name AS provider_name,provider.auth_type,provider.base_url,
               provider.status AS provider_status,provider.health_status,
               provider.health_updated_at
        FROM models model
        JOIN providers provider
          ON provider.tenant_id=model.tenant_id AND provider.provider_id=model.provider_id
        WHERE model.tenant_id=? AND model.model_id=?
        """,
        (tenant_id, model_id),
    ).fetchone()


def _route_from_arm(
    arm: sqlite3.Row,
    *,
    requested_model: str,
    environment_code: str,
    environment_id: str,
    model_alias_id: str,
    public_model_alias: str,
    routing_reason: str,
) -> GatewayRoute:
    """把已冻结实验臂投影为单候选路由，明确不附加 fallback 或动态重排计划。"""

    capabilities = _parse_capabilities(arm["capabilities"])
    return GatewayRoute(
        tenant_id=str(arm["tenant_id"]),
        model_id=str(arm["model_id"]),
        model_alias=str(arm["model_alias"]),
        upstream_model_name=str(arm["upstream_model_name"]),
        capabilities=capabilities,
        provider_id=str(arm["provider_id"]),
        provider_code=str(arm["provider_code"]),
        provider_type=str(arm["provider_type"]),
        provider_auth_type=str(arm["auth_type"]),
        provider_name=str(arm["provider_name"]),
        provider_base_url=str(arm["base_url"]),
        requested_model=requested_model,
        environment=environment_code,
        environment_id=environment_id,
        model_alias_id=model_alias_id,
        public_model_alias=public_model_alias,
        routing_reason=routing_reason,
    )


def _arm_is_available(
    config: AppConfig,
    connection: sqlite3.Connection,
    arm: sqlite3.Row,
    route: GatewayRoute,
    *,
    now: datetime,
    expected_model_id: str,
    expected_provider_id: str,
    expected_provider_code: str,
    frozen_route_fingerprint: str,
    streaming: bool,
) -> bool:
    """重建实验臂执行配置并核验健康、适配器、密钥池和敏感配置指纹。"""

    # 模型、Provider、能力或健康任一不符都使当前臂不可用，不能改派另一实验臂。
    if (
        route.model_id != expected_model_id
        or route.provider_id != expected_provider_id
        or route.provider_code != expected_provider_code
        or str(arm["model_status"]) != "active"
        or str(arm["provider_status"]) != "active"
        or route.capabilities.get("chat") is not True
        or (streaming and route.capabilities.get("stream") is False)
        or not _provider_is_fresh_healthy(
            arm,
            now=now,
            stale_seconds=config.provider_health_stale_seconds,
        )
    ):
        return False
    try:
        spec = validate_provider_auth(route.provider_type, route.provider_auth_type)
        if spec.status != "active":
            return False
        route_fingerprint = build_experiment_route_config_fingerprint(
            config.auth_token_secret,
            tenant_id=route.tenant_id,
            model_id=route.model_id,
            model_status=str(arm["model_status"]),
            provider_id=route.provider_id,
            provider_code=route.provider_code,
            provider_status=str(arm["provider_status"]),
            provider_type=route.provider_type,
            provider_auth_type=route.provider_auth_type,
            provider_base_url=route.provider_base_url,
            upstream_model_name=route.upstream_model_name,
            capabilities=route.capabilities,
            context_window=int(arm["context_window"]),
            adapter_catalog_version=CATALOG_VERSION,
            adapter_catalog_hash=CATALOG_HASH,
            adapter_spec_id=spec.spec_id,
            adapter_version=spec.adapter_version,
        )
        # 指纹包含 Endpoint 等敏感执行配置，但这里只做常量时间比较且绝不记录原值。
        if not hmac.compare_digest(route_fingerprint, frozen_route_fingerprint):
            return False
        adapter_for_route(route)
        upstream_url(route, "chat/completions")
        keypool = snapshot_provider_key_pool(
            config,
            connection,
            tenant_id=route.tenant_id,
            provider_id=route.provider_id,
            provider_code=route.provider_code,
            provider_auth_type=route.provider_auth_type,
            as_of=now,
        )
        return keypool.state == "available"
    # 适配器、URL 或密钥池无法安全构造时按当前臂不可用收口，不暴露底层异常。
    except (ApiError, ProviderAdapterSpecError, TypeError, ValueError):
        return False


def _insert_routing_decision(
    connection: sqlite3.Connection,
    *,
    routing_decision_id: str,
    tenant_id: str,
    request_id: str,
    trace_id: str,
    virtual_key_id: str,
    model_alias_id: str,
    requested_model: str,
    environment_code: str,
    environment_id: str,
    route: GatewayRoute,
    candidate_summary: str,
    selected: bool,
    error_code: str | None,
    created_at: str,
) -> None:
    """在 Assignment 前写入同租户 RoutingDecision，冻结唯一实验臂及安全候选摘要。"""

    connection.execute(
        """
        INSERT INTO routing_decisions(
          routing_decision_id,tenant_id,request_id,trace_id,virtual_key_id,
          model_alias_id,requested_model,environment,environment_id,selected_model_id,
          selected_internal_model_id,provider_id,provider_code,decision_status,
          decision_reason,candidate_summary,error_code,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            routing_decision_id,
            tenant_id,
            request_id,
            trace_id,
            virtual_key_id,
            model_alias_id,
            requested_model,
            environment_code,
            environment_id,
            route.model_id,
            route.model_alias,
            route.provider_id,
            route.provider_code,
            "selected" if selected else "failed",
            route.routing_reason,
            candidate_summary,
            error_code,
            created_at,
        ),
    )


def _insert_variant_unavailable_terminal(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    experiment_id: str,
    assignment_id: str,
    execution_id: str,
    settlement_id: str,
    request_id: str,
    variant: str,
    model_id: str,
    provider_id: str,
    now_db: str,
) -> None:
    """对不可执行的冻结臂原子写失败 Outcome 与未暴露结算，且不触发 Provider。"""

    error_code = "gateway.experiment_variant_unavailable"
    connection.execute(
        """
        INSERT INTO experiment_request_outcomes(
          outcome_id,tenant_id,experiment_id,assignment_id,execution_id,request_id,
          variant,model_id,provider_id,gateway_status,exposure_status,outcome_reason,
          error_code,routing_attempt_id,stream_session_id,quality_eligible,
          quality_succeeded,latency_ms,recovery_generation,facts_at,created_at
        ) VALUES(
          ?,?,?,?,?,?,?,?,?,'failed','not_exposed','variant_unavailable',?,
          NULL,NULL,0,NULL,NULL,1,?,?
        )
        """,
        (
            f"exoc_{uuid4().hex}",
            tenant_id,
            experiment_id,
            assignment_id,
            execution_id,
            request_id,
            variant,
            model_id,
            provider_id,
            error_code,
            now_db,
            now_db,
        ),
    )
    cursor = connection.execute(
        """
        UPDATE experiment_request_executions
        SET status='terminal',updated_at=?
        WHERE tenant_id=? AND execution_id=? AND assignment_id=? AND status='assigned'
        """,
        (now_db, tenant_id, execution_id, assignment_id),
    )
    if cursor.rowcount != 1:
        # Execution 终态 CAS 失败时整个分配事务必须回滚，不能留下已计数的半图。
        raise ValueError("experiment_variant_terminal_failed")
    connection.execute(
        """
        INSERT INTO experiment_usage_settlements(
          usage_settlement_id,tenant_id,experiment_id,assignment_id,request_id,variant,
          model_id,provider_id,status,usage_ledger_id,finops_ledger_entry_id,gross_cny_micros,
          normalization_status,settlement_reason,settlement_version,settled_at,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,'not_exposed',NULL,NULL,NULL,'not_applicable',
                 'variant_unavailable',1,?,?,?)
        """,
        (
            settlement_id,
            tenant_id,
            experiment_id,
            assignment_id,
            request_id,
            variant,
            model_id,
            provider_id,
            now_db,
            now_db,
            now_db,
        ),
    )


def _validate_terminal_intent(intent: ExperimentTerminalIntent) -> None:
    """验证 Gateway 终态、暴露、质量和结算组合，拒绝由调用方伪造不一致结果。"""

    if not isinstance(intent, ExperimentTerminalIntent):
        # 只接受冻结 DTO，避免松散对象绕过枚举与隐私边界。
        raise ValueError("experiment_terminal_intent_invalid")
    if intent.quality_eligible:
        if type(intent.quality_succeeded) is not bool or type(intent.latency_ms) is not int:
            # 可计质量请求必须同时提供布尔结果与整数延迟，不能以缺失值污染分母。
            raise ValueError("experiment_terminal_quality_invalid")
        if intent.latency_ms < 0 or intent.exposure != "exposed":
            # 未证明已暴露或负延迟的请求不得进入模型质量统计。
            raise ValueError("experiment_terminal_quality_invalid")
    elif intent.quality_succeeded is not None or intent.latency_ms is not None:
        # 不可计质量请求不得夹带成功或延迟事实，防止页面重算出错误分母。
        raise ValueError("experiment_terminal_quality_invalid")
    if intent.outcome_reason == "completed":
        valid = (
            intent.gateway_outcome == "success"
            and intent.exposure == "exposed"
            and intent.error_code is None
        )
    elif intent.outcome_reason == "gateway_failed":
        valid = intent.gateway_outcome == "failed" and intent.error_code is not None
    elif intent.outcome_reason == "execution_timeout":
        valid = (
            intent.gateway_outcome == "failed"
            and intent.error_code == "gateway.experiment_execution_timeout"
            and not intent.quality_eligible
        )
    elif intent.outcome_reason == "cancelled":
        valid = (
            intent.gateway_outcome == "failed"
            and intent.error_code is not None
            and not intent.quality_eligible
        )
    else:
        valid = False
    if not valid:
        # 未列入冻结状态机的终态组合整体拒绝，不能降级为近似失败事实。
        raise ValueError("experiment_terminal_matrix_invalid")


def _terminal_owner_matches(
    owner: sqlite3.Row | None,
    handle: ExperimentExecutionHandle,
    *,
    now: datetime,
) -> bool:
    """以租户、代际、owner、私有 token 和双截止时间共同证明终态写入权。"""

    return bool(
        owner is not None
        and str(owner["status"]) in {"assigned", "provider_calling"}
        and int(owner["generation"]) == handle.generation
        and str(owner["experiment_id"]) == handle.experiment_id
        and str(owner["request_id"]) == handle.request_id
        and str(owner["trace_id"]) == handle.trace_id
        and hmac.compare_digest(str(owner["lease_owner_id"]), handle.lease_owner_id)
        and hmac.compare_digest(str(owner["lease_token"]), handle.lease_token)
        and _parse_db_time(owner["lease_expires_at"]) > now
        and _parse_db_time(owner["absolute_deadline_at"]) > now
    )


def _settle_terminal_disposition(
    connection: sqlite3.Connection,
    handle: ExperimentExecutionHandle,
    *,
    disposition: str,
    now_db: str,
) -> None:
    """按最终暴露结论推进结算；真实上游用量仍等待 Usage/FinOps 事实触发。"""

    if disposition == "pending":
        return
    if disposition == "not_exposed":
        values = ("not_exposed", "not_applicable", "not_exposed")
    elif disposition == "unavailable":
        values = ("unavailable", "unavailable", "usage_unavailable")
    else:
        # 未知处置不能改写成本事实，避免把待结算请求错误显示为零成本。
        raise ValueError("experiment_settlement_disposition_invalid")
    connection.execute(
        """
        UPDATE experiment_usage_settlements
        SET status=?,normalization_status=?,settlement_reason=?,settled_at=?,updated_at=?
        WHERE tenant_id=? AND assignment_id=? AND status='pending'
        """,
        values
        + (
            now_db,
            now_db,
            handle.tenant_id,
            handle.assignment_id,
        ),
    )


def _parse_capabilities(raw: object) -> dict[str, object]:
    """解析模型能力快照并要求对象形状，禁止以字符串或数组绕过 Chat 能力校验。"""

    value = json.loads(str(raw)) if isinstance(raw, str) else raw
    if not isinstance(value, dict):
        # 非对象能力值无法证明实验臂支持当前 Chat 请求，必须失败关闭。
        raise ValueError("experiment_capabilities_invalid")
    return {str(key): item for key, item in value.items()}


def _provider_is_fresh_healthy(
    row: sqlite3.Row,
    *,
    now: datetime,
    stale_seconds: int,
) -> bool:
    """只接受仍在配置时效内的健康证据，过期或缺失证据不视为可路由。"""

    if str(row["health_status"]) != "healthy" or row["health_updated_at"] is None:
        return False
    observed = _parse_db_time(row["health_updated_at"])
    age = (now - observed).total_seconds()
    return 0 <= age <= stale_seconds


def _database_now(connection: sqlite3.Connection) -> tuple[datetime, str]:
    """从同一 SQLite 连接取得权威 UTC 秒级时钟，统一采样、租约和终态边界。"""

    row = connection.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now') AS current_time"
    ).fetchone()
    if row is None:
        # 无数据库时钟就无法证明截止条件，当前实验事务必须失败关闭。
        raise ValueError("experiment_database_time_unavailable")
    value = str(row["current_time"])
    return _parse_db_time(value), value


def _parse_db_time(value: object) -> datetime:
    """把数据库规范 UTC 时间解析为有时区对象，拒绝宽松或本地时间语义。"""

    if not isinstance(value, str):
        # 非字符串时间不属于迁移冻结合同，不能参与租约或采样比较。
        raise ValueError("experiment_time_invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        # 无时区时间会导致不同节点产生不同截止判断，因此直接拒绝。
        raise ValueError("experiment_time_invalid")
    return parsed.astimezone(UTC)


def _to_db_time(value: datetime) -> str:
    """将有时区时间规范化为 SQLite 使用的 UTC Z 秒级格式。"""

    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
