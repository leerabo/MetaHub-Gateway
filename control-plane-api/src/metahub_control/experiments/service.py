"""实验控制面服务：按租户创建、查询和读取权威结果，并用事务复核实时权限。

Tenant-scoped PRD-075 experiment creation, reads, and result snapshots.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import re
import sqlite3
import time
from concurrent.futures import Future as ConcurrentFuture
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from threading import Lock
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.db.security import hash_token
from metahub_shared.experiments import (
    EXPERIMENT_ALLOCATION_ALGORITHM_VERSION,
    build_experiment_create_request_fingerprint,
    build_experiment_idempotency_key_hash,
    build_experiment_key_fingerprint,
    build_experiment_route_config_fingerprint,
    calculate_variant_targets,
)
from metahub_shared.rbac import authorization_fingerprint
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.provider_adapter_spec import (
    ProviderAdapterSpecError,
    catalog_identity,
    validate_provider_auth,
)

from metahub_control.audit import (
    AuditRecord,
    AuditRequestMeta,
    commit_audited_transaction,
    dispatch_audit_recorded,
    record_audit_log,
)
from metahub_control.auth import (
    AuthContext,
    require_permission,
    require_transaction_permission,
)

from .models import (
    CreateExperimentRequest,
    ExperimentArmInfo,
    ExperimentCreateResponse,
    ExperimentDetailResponse,
    ExperimentInfo,
    ExperimentListResponse,
    ExperimentResultsResponse,
)
from .statistics import load_statistics_snapshot

# 该常量组固定实验标识、统计单飞时限、游标域和创建权限，不允许运行时漂移。
_EXPERIMENT_ID = re.compile(r"^exp_[0-9a-f]{32}$")
_STATISTICS_GATE = Lock()
# Control 内多个后台 Scheduler 会占用默认线程池；统计读取使用独立单线程，确保 250ms
# 预算衡量自己的排队和查询，而不是被无关后台任务提前耗尽。单线程仍保持 SQLite 单飞边界。
_STATISTICS_EXECUTOR = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="metahub-experiment-statistics",
)
_STATISTICS_QUERY_DEADLINE_SECONDS = 0.250
_STATISTICS_RESPONSE_DEADLINE_SECONDS = 2.0
_CURSOR_MAX_LENGTH = 2048
_CURSOR_DOMAIN = b"metahub/experiment/cursor/v1\x00"
_CREATE_PERMISSIONS = (
    "experiment.write",
    "project.read",
    "environment.read",
    "routing.read",
    "model.read",
)


@dataclass(frozen=True, slots=True)
class _RouteOwner:
    """验证实验范围和路由所有者属于同一租户，并冻结可复核的模型能力。"""
    model_id: str
    provider_id: str
    provider_code: str
    route_config_fingerprint: str


def create_experiment(
    config: AppConfig,
    context: AuthContext,
    payload: CreateExperimentRequest,
    *,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
    audit_request_meta: AuditRequestMeta,
) -> ExperimentCreateResponse:
    """在租户事务内创建实验、双臂与调度游标，并以幂等指纹阻止冲突重放。

    Create one immutable live-traffic experiment or replay its actor-scoped receipt.
    """

    try:
        key_hash = build_experiment_idempotency_key_hash(
            idempotency_key,
            tenant_id=context.user.tenant_id,
            actor_user_id=context.user.user_id,
        )
        request_fingerprint = build_experiment_create_request_fingerprint(
            idempotency_key,
            **_create_fingerprint_fields(payload),
        )
    # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
    except ValueError:
        # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
        raise ApiError("experiment.input_invalid") from None

    connection = _connect(config)
    audit_record: AuditRecord | None = None
    response: ExperimentCreateResponse | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        live = context
        for permission_code in _CREATE_PERMISSIONS:
            live = require_transaction_permission(
                connection,
                live,
                permission_code,
                unavailable_error_code="experiment.unavailable",
            )
        existing = connection.execute(
            """
            SELECT * FROM model_experiments
            WHERE tenant_id=? AND created_by=? AND idempotency_key_hash=?
            """,
            (live.user.tenant_id, live.user.user_id, key_hash),
        ).fetchone()
        if existing is not None:
            if not hmac.compare_digest(
                str(existing["request_fingerprint"]),
                request_fingerprint,
            ):
                # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
                raise ApiError("experiment.idempotency_conflict")
            response = ExperimentCreateResponse(
                experiment=_experiment_info(existing),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return response

        _validate_scope(
            connection,
            tenant_id=live.user.tenant_id,
            project_id=payload.project_id,
            environment_id=payload.environment_id,
            model_alias_id=payload.model_alias_id,
        )
        if connection.execute(
            """
            SELECT 1 FROM model_experiments
            WHERE tenant_id=? AND project_id=? AND environment_id=?
              AND model_alias_id=? AND status='active'
            """,
            (
                live.user.tenant_id,
                payload.project_id,
                payload.environment_id,
                payload.model_alias_id,
            ),
        ).fetchone() is not None:
            # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
            raise ApiError("experiment.scope_conflict")

        experiment_id = f"exp_{uuid4().hex}"
        model_a = _load_route_owner(
            connection,
            config,
            tenant_id=live.user.tenant_id,
            experiment_id=experiment_id,
            model_id=payload.model_a_id,
        )
        model_b = _load_route_owner(
            connection,
            config,
            tenant_id=live.user.tenant_id,
            experiment_id=experiment_id,
            model_id=payload.model_b_id,
        )
        allocation = calculate_variant_targets(
            payload.target_sample_count,
            payload.model_a_allocation_bps,
        )
        created = _database_now(connection)
        created_at = _db_time(created)
        sampling_deadline = _db_time(
            created + timedelta(seconds=payload.sampling_duration_seconds)
        )
        try:
            sampling_fingerprint = build_experiment_key_fingerprint(
                config.auth_token_secret,
                tenant_id=live.user.tenant_id,
                experiment_id=experiment_id,
            )
        # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
        except ValueError:
            # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
            raise ApiError("experiment.unavailable") from None
        connection.execute(
            """
            INSERT INTO model_experiments(
              experiment_id,tenant_id,name,project_id,environment_id,model_alias_id,
              model_a_id,model_a_provider_id,model_a_provider_code,
              model_a_route_config_fingerprint,
              model_b_id,model_b_provider_id,model_b_provider_code,
              model_b_route_config_fingerprint,
              target_sample_count,traffic_sample_bps,model_a_allocation_bps,
              model_b_allocation_bps,model_a_target_count,model_b_target_count,
              sampling_duration_seconds,feedback_collection_enabled,feedback_window_seconds,
              allocation_algorithm_version,sampling_key_fingerprint,status,
              assigned_count,model_a_assigned_count,model_b_assigned_count,facts_version,
              feedback_fence_closed_at,finalization_generation,finalization_owner_id,
              finalization_lease_token,finalization_lease_expires_at,created_by,
              idempotency_key_hash,request_fingerprint,operation_request_id,
              operation_trace_id,execution_max_seconds,settlement_grace_seconds,
              started_at,sampling_deadline_at,sampling_ended_at,feedback_deadline_at,
              settlement_deadline_at,completion_reason,completed_at,created_at,updated_at
            ) VALUES(
              ?,?,?,?,?,?,?,?,?,?,
              ?,?,?,?,?,?,?,?,?,?,
              ?,?,?,?,?,'active',
              0,0,0,0,NULL,0,NULL,NULL,NULL,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,NULL,?,?
            )
            """,
            (
                experiment_id,
                live.user.tenant_id,
                payload.name,
                payload.project_id,
                payload.environment_id,
                payload.model_alias_id,
                model_a.model_id,
                model_a.provider_id,
                model_a.provider_code,
                model_a.route_config_fingerprint,
                model_b.model_id,
                model_b.provider_id,
                model_b.provider_code,
                model_b.route_config_fingerprint,
                payload.target_sample_count,
                payload.traffic_sample_bps,
                payload.model_a_allocation_bps,
                10_000 - payload.model_a_allocation_bps,
                allocation.model_a_target_count,
                allocation.model_b_target_count,
                payload.sampling_duration_seconds,
                int(payload.feedback_collection_enabled),
                payload.feedback_window_seconds,
                EXPERIMENT_ALLOCATION_ALGORITHM_VERSION,
                sampling_fingerprint,
                live.user.user_id,
                key_hash,
                request_fingerprint,
                request_id,
                trace_id,
                config.experiment_execution_max_seconds,
                config.experiment_settlement_grace_seconds,
                created_at,
                sampling_deadline,
                created_at,
                created_at,
            ),
        )
        after_summary = {
            "experiment_id": experiment_id,
            "project_id": payload.project_id,
            "environment_id": payload.environment_id,
            "model_alias_id": payload.model_alias_id,
            "model_a_id": payload.model_a_id,
            "model_b_id": payload.model_b_id,
            "target_sample_count": payload.target_sample_count,
            "traffic_sample_bps": payload.traffic_sample_bps,
            "model_a_allocation_bps": payload.model_a_allocation_bps,
            "sampling_duration_seconds": payload.sampling_duration_seconds,
            "feedback_collection_enabled": payload.feedback_collection_enabled,
            "feedback_window_seconds": payload.feedback_window_seconds,
        }
        audit_record = record_audit_log(
            connection,
            live,
            action="experiment.created",
            object_type="experiment",
            object_id=experiment_id,
            object_name=None,
            before_summary={},
            after_summary=after_summary,
            request_meta=audit_request_meta,
            request_id=request_id,
            trace_id=trace_id,
        )
        if audit_record is None:
            # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
            raise ApiError("audit_log.record_unavailable")
        row = _owned_experiment_row(connection, live.user.tenant_id, experiment_id)
        response = ExperimentCreateResponse(
            experiment=_experiment_info(row),
            idempotent_replay=False,
            request_id=request_id,
            trace_id=trace_id,
        )
        commit_audited_transaction(connection)
    # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
    except ApiError:
        connection.rollback()
        raise
    # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        if "uq_model_experiments_active_scope_prd075" in str(exc) or (
            "model_experiments.tenant_id" in str(exc)
            and "model_experiments.model_alias_id" in str(exc)
        ):
            # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
            raise ApiError("experiment.scope_conflict") from None
        # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
        raise ApiError("experiment.unavailable") from None
    # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
    except (sqlite3.Error, ArithmeticError, TypeError, ValueError):
        connection.rollback()
        # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
        raise ApiError("experiment.unavailable") from None
    finally:
        connection.close()
    dispatch_audit_recorded(
        config,
        context,
        audit_record,
        request_id=request_id,
        trace_id=trace_id,
    )
    if response is None:
        # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
        raise ApiError("experiment.unavailable")
    return response


def list_experiments(
    config: AppConfig,
    context: AuthContext,
    *,
    status: object,
    limit: object,
    cursor: object,
    request_id: str,
    trace_id: str,
) -> ExperimentListResponse:
    """按认证租户读取实验安全投影或权威结果，跨租户对象统一表现为不存在。"""
    normalized_status = _status_filter(status)
    normalized_limit = _limit(limit)
    position = _decode_cursor(
        cursor,
        secret=config.auth_token_secret,
        tenant_id=context.user.tenant_id,
        status=normalized_status,
    )
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        live = require_transaction_permission(
            connection,
            context,
            "experiment.read",
            unavailable_error_code="experiment.unavailable",
        )
        clauses = ["tenant_id=?"]
        params: list[object] = [live.user.tenant_id]
        if normalized_status is not None:
            clauses.append("status=?")
            params.append(normalized_status)
        if position is not None:
            clauses.append("(created_at<? OR (created_at=? AND experiment_id<?))")
            params.extend([position[0], position[0], position[1]])
        params.append(normalized_limit + 1)
        rows = connection.execute(
            f"""
            SELECT * FROM model_experiments
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC,experiment_id DESC LIMIT ?
            """,
            params,
        ).fetchall()
        page = rows[:normalized_limit]
        next_cursor = None
        if len(rows) > normalized_limit and page:
            last = page[-1]
            next_cursor = _encode_cursor(
                created_at=str(last["created_at"]),
                experiment_id=str(last["experiment_id"]),
                secret=config.auth_token_secret,
                tenant_id=live.user.tenant_id,
                status=normalized_status,
            )
        result = ExperimentListResponse(
            experiments=[_experiment_info(row) for row in page],
            next_cursor=next_cursor,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
    except ApiError:
        connection.rollback()
        raise
    # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
    except (sqlite3.Error, TypeError, ValueError):
        connection.rollback()
        # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
        raise ApiError("experiment.unavailable") from None
    finally:
        connection.close()


def get_experiment(
    config: AppConfig,
    context: AuthContext,
    experiment_id: object,
    *,
    request_id: str,
    trace_id: str,
) -> ExperimentDetailResponse:
    """按认证租户读取实验安全投影或权威结果，跨租户对象统一表现为不存在。"""
    normalized_id = _owned_experiment_id(experiment_id)
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        live = require_transaction_permission(
            connection,
            context,
            "experiment.read",
            unavailable_error_code="experiment.unavailable",
        )
        row = _owned_experiment_row(connection, live.user.tenant_id, normalized_id)
        response = ExperimentDetailResponse(
            experiment=_experiment_info(row),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
    except ApiError:
        connection.rollback()
        raise
    # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
    except (sqlite3.Error, TypeError, ValueError):
        connection.rollback()
        # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
        raise ApiError("experiment.unavailable") from None
    finally:
        connection.close()


async def get_experiment_results(
    config: AppConfig,
    context: AuthContext,
    experiment_id: object,
    *,
    request_id: str,
    trace_id: str,
) -> ExperimentResultsResponse:
    """按认证租户读取实验安全投影或权威结果，跨租户对象统一表现为不存在。"""
    normalized_id = _owned_experiment_id(experiment_id)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _STATISTICS_RESPONSE_DEADLINE_SECONDS
    # 非阻塞线程锁不依赖事件循环回调；前一读取仍占用专用 worker 时立即失败，禁止堆积
    # 已经没有完整 250ms 预算的 waiter。锁只表示进程内统计资源，不承载租户身份。
    if not _STATISTICS_GATE.acquire(blocking=False):
        raise ApiError("experiment.statistics_unavailable") from None

    # 专用执行器把统计 lane 与 Scheduler 的默认池隔离；绝对 deadline 仍传入 SQLite
    # progress handler，HTTP 超时后 worker 真正退出前也不会释放上方单飞槽位。
    try:
        worker = _STATISTICS_EXECUTOR.submit(
            partial(
                _get_results_with_query_deadline,
                config,
                context,
                normalized_id,
                request_id,
                trace_id,
            )
        )
    # 执行器关闭属于服务进程生命周期异常；先归还锁，再返回固定统计不可用码。
    except RuntimeError:
        _STATISTICS_GATE.release()
        raise ApiError("experiment.statistics_unavailable") from None

    def release_gate(completed: ConcurrentFuture[ExperimentResultsResponse]) -> None:
        # worker 线程直接归还锁，不能等待可能正被其他同步路由占用的 ASGI 事件循环。
        """封装实验服务的冻结配置或并发围栏，保持租户归属和幂等语义。"""
        if not completed.cancelled():
            try:
                completed.exception()
            except Exception:
                pass
        _STATISTICS_GATE.release()

    worker.add_done_callback(release_gate)
    task = asyncio.wrap_future(worker, loop=loop)
    try:
        return await asyncio.wait_for(
            asyncio.shield(task),
            timeout=max(0.0, deadline - loop.time()),
        )
    # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
    except TimeoutError:
        # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
        raise ApiError("experiment.statistics_unavailable") from None


def _get_results_with_query_deadline(
    config: AppConfig,
    context: AuthContext,
    experiment_id: str,
    request_id: str,
    trace_id: str,
) -> ExperimentResultsResponse:
    """worker 获得 CPU 后才启动 250ms SQLite 期限，线程调度由外层 2 秒包络约束。"""

    return _get_results_sync(
        config,
        context,
        experiment_id,
        request_id,
        trace_id,
        time.monotonic() + _STATISTICS_QUERY_DEADLINE_SECONDS,
    )


def _get_results_sync(
    config: AppConfig,
    context: AuthContext,
    experiment_id: str,
    request_id: str,
    trace_id: str,
    absolute_deadline: float,
) -> ExperimentResultsResponse:
    """按认证租户读取实验安全投影或权威结果，跨租户对象统一表现为不存在。"""
    try:
        connection = _connect(config)
    # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
    except ApiError:
        # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
        raise ApiError("experiment.statistics_unavailable") from None
    try:
        connection.execute("PRAGMA busy_timeout=25")
        connection.execute("PRAGMA query_only=ON")
        connection.set_progress_handler(
            lambda: 1 if time.monotonic() >= absolute_deadline else 0,
            250,
        )
        connection.execute("BEGIN")
        live = _require_statistics_permission(connection, context)
        row = _owned_experiment_row(connection, live.user.tenant_id, experiment_id)
        database_now = _database_now(connection)
        snapshot = load_statistics_snapshot(
            connection,
            row,
            database_now=database_now,
        )
        next_state_at = None
        if row["status"] == "active":
            next_state_at = _time(row["sampling_deadline_at"])
        elif row["status"] == "collecting_feedback":
            feedback_deadline = _time(row["feedback_deadline_at"])
            settlement_deadline = _time(row["settlement_deadline_at"])
            next_state_at = (
                feedback_deadline if database_now < feedback_deadline else settlement_deadline
            )
        response = ExperimentResultsResponse(
            experiment_id=experiment_id,
            status=row["status"],
            is_final=row["status"] == "completed",
            completion_reason=row["completion_reason"],
            facts_version=snapshot.facts_version,
            facts_through_at=snapshot.facts_through_at,
            next_state_at=next_state_at,
            pending_execution_count=snapshot.pending_execution_count,
            pending_settlement_count=snapshot.pending_settlement_count,
            results=list(snapshot.arms),
            request_id=request_id,
            trace_id=trace_id,
        )
        if time.monotonic() >= absolute_deadline:
            # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
            raise ApiError("experiment.statistics_unavailable")
        connection.commit()
        return response
    # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
    except ApiError:
        connection.rollback()
        raise
    # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
    except (sqlite3.Error, ArithmeticError, TypeError, ValueError):
        connection.rollback()
        # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
        raise ApiError("experiment.statistics_unavailable") from None
    finally:
        connection.set_progress_handler(None, 0)
        connection.close()


def _require_statistics_permission(
    connection: sqlite3.Connection,
    context: AuthContext,
) -> AuthContext:
    """在结果快照事务内快速复核刚由路由完整验证的授权指纹。"""

    # 路由依赖已重算完整权限目录；这里仍先验证目标 code 属于可信目录且存在于该快照。
    require_permission(context, "experiment.read")
    if not connection.in_transaction:
        raise ApiError("experiment.statistics_unavailable")
    tenant_id = context.user.tenant_id
    # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
    if tenant_id != context.tenant.tenant_id:
        # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
        raise ApiError("auth.permission_denied")
    # 单条 owner 查询同时证明 Session、用户、租户、角色和 experiment.read 关系仍有效；
    # RolePermission 变更触发器会原子推进 revision/matrix hash，随后由指纹比较发现撤权竞态。
    row = connection.execute(
        """
        SELECT user.role,role.permission_revision,role.matrix_sha256
        FROM sessions session
        JOIN users user
          ON user.tenant_id=session.tenant_id AND user.user_id=session.user_id
        JOIN tenants tenant ON tenant.tenant_id=session.tenant_id
        JOIN roles role ON role.tenant_id=user.tenant_id AND role.code=user.role
        JOIN permissions permission
          ON permission.tenant_id=role.tenant_id
         AND permission.code='experiment.read' AND permission.status='active'
        JOIN role_permissions relation
          ON relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
         AND relation.permission_id=permission.permission_id
        WHERE session.tenant_id=? AND session.user_id=? AND session.session_id=?
          AND session.token_jti=? AND session.token_hash=?
          AND session.status='active'
          AND julianday(session.expires_at)>julianday('now')
          AND user.status='active' AND tenant.status='active' AND role.status='active'
        """,
        (
            tenant_id,
            context.user.user_id,
            context.session.session_id,
            context.token_jti,
            hash_token(context.token),
        ),
    ).fetchone()
    if row is None:
        # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
        raise ApiError("auth.permission_denied")
    try:
        fingerprint = authorization_fingerprint(
            tenant_id=tenant_id,
            user_id=context.user.user_id,
            role=row["role"],
            permission_revision=row["permission_revision"],
            matrix_sha256=row["matrix_sha256"],
        )
    # CHECK/trigger 被关闭后出现的非法 role 事实属于统计依赖损坏，不能回退旧上下文。
    except ValueError:
        raise ApiError("experiment.statistics_unavailable") from None
    if (
        row["role"] != context.user.role
        or row["permission_revision"] != context.permission_revision
        or fingerprint != context.authorization_fingerprint
    ):
        # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
        raise ApiError("auth.permission_denied")
    return context


def _validate_scope(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    project_id: str,
    environment_id: str,
    model_alias_id: str,
) -> None:
    """验证实验范围和路由所有者属于同一租户，并冻结可复核的模型能力。"""
    row = connection.execute(
        """
        SELECT project.project_id,environment.environment_id,environment.code,
               alias.model_alias_id
        FROM projects project
        JOIN environments environment ON environment.tenant_id=project.tenant_id
        JOIN model_aliases alias ON alias.tenant_id=project.tenant_id
        WHERE project.tenant_id=? AND project.project_id=? AND project.status='active'
          AND environment.environment_id=? AND environment.status='active'
          AND alias.model_alias_id=? AND alias.status='active'
          AND (alias.environment_id=environment.environment_id
            OR (alias.environment_id IS NULL AND alias.environment=environment.code))
        """,
        (tenant_id, project_id, environment_id, model_alias_id),
    ).fetchone()
    if row is None:
        # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
        raise ApiError("experiment.input_invalid")


def _load_route_owner(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    tenant_id: str,
    experiment_id: str,
    model_id: str,
) -> _RouteOwner:
    """验证实验范围和路由所有者属于同一租户，并冻结可复核的模型能力。"""
    row = connection.execute(
        """
        SELECT model.model_id,model.status AS model_status,model.upstream_model_name,
               model.capabilities,model.context_window,
               provider.provider_id,provider.provider_code,
               provider.status AS provider_status,provider.provider_type,
               provider.auth_type,provider.base_url,
               activation.catalog_version,activation.catalog_hash
        FROM models model
        JOIN providers provider
          ON provider.tenant_id=model.tenant_id AND provider.provider_id=model.provider_id
        JOIN provider_adapter_catalog_activations activation
          ON activation.tenant_id=model.tenant_id AND activation.status='active'
        WHERE model.tenant_id=? AND model.model_id=?
        """,
        (tenant_id, model_id),
    ).fetchone()
    if (
        row is None
        or row["model_status"] != "active"
        or row["provider_status"] != "active"
    ):
        # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
        raise ApiError("experiment.model_invalid")
    capabilities = _capabilities(row["capabilities"])
    if capabilities.get("chat") is not True:
        # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
        raise ApiError("experiment.model_invalid")
    identity = catalog_identity()
    if (
        row["catalog_version"] != identity.version
        or row["catalog_hash"] != identity.catalog_hash
    ):
        # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
        raise ApiError("experiment.unavailable")
    try:
        spec = validate_provider_auth(row["provider_type"], row["auth_type"])
        fingerprint = build_experiment_route_config_fingerprint(
            config.auth_token_secret,
            tenant_id=tenant_id,
            model_id=str(row["model_id"]),
            model_status=str(row["model_status"]),
            provider_id=str(row["provider_id"]),
            provider_code=str(row["provider_code"]),
            provider_status=str(row["provider_status"]),
            provider_type=str(row["provider_type"]),
            provider_auth_type=str(row["auth_type"]),
            provider_base_url=str(row["base_url"]),
            upstream_model_name=str(row["upstream_model_name"]),
            capabilities=capabilities,
            context_window=row["context_window"],
            adapter_catalog_version=identity.version,
            adapter_catalog_hash=identity.catalog_hash,
            adapter_spec_id=spec.spec_id,
            adapter_version=spec.adapter_version,
        )
        # Recompute the deployment fingerprint with the final experiment owner before returning.
        build_experiment_key_fingerprint(
            config.auth_token_secret,
            tenant_id=tenant_id,
            experiment_id=experiment_id,
        )
    # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
    except ProviderAdapterSpecError:
        # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
        raise ApiError("experiment.model_invalid") from None
    # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
    except ValueError:
        # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
        raise ApiError("experiment.model_invalid") from None
    return _RouteOwner(
        model_id=str(row["model_id"]),
        provider_id=str(row["provider_id"]),
        provider_code=str(row["provider_code"]),
        route_config_fingerprint=fingerprint,
    )


def _capabilities(value: object) -> dict[str, bool]:
    """封装实验服务的冻结配置或并发围栏，保持租户归属和幂等语义。"""
    if type(value) is not str:
        # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
        raise ApiError("experiment.model_invalid")
    try:
        parsed = json.loads(value)
    # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
    except (json.JSONDecodeError, TypeError, ValueError):
        # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
        raise ApiError("experiment.model_invalid") from None
    if (
        type(parsed) is not dict
        or not 1 <= len(parsed) <= 32
        or any(type(key) is not str or type(enabled) is not bool for key, enabled in parsed.items())
    ):
        # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
        raise ApiError("experiment.model_invalid")
    return parsed


def _experiment_info(row: sqlite3.Row) -> ExperimentInfo:
    """封装实验服务的冻结配置或并发围栏，保持租户归属和幂等语义。"""
    return ExperimentInfo(
        experiment_id=row["experiment_id"],
        name=row["name"],
        project_id=row["project_id"],
        environment_id=row["environment_id"],
        model_alias_id=row["model_alias_id"],
        model_a=ExperimentArmInfo(
            variant="a",
            model_id=row["model_a_id"],
            provider_id=row["model_a_provider_id"],
            provider_code=row["model_a_provider_code"],
            allocation_bps=row["model_a_allocation_bps"],
            target_count=row["model_a_target_count"],
            assigned_count=row["model_a_assigned_count"],
        ),
        model_b=ExperimentArmInfo(
            variant="b",
            model_id=row["model_b_id"],
            provider_id=row["model_b_provider_id"],
            provider_code=row["model_b_provider_code"],
            allocation_bps=row["model_b_allocation_bps"],
            target_count=row["model_b_target_count"],
            assigned_count=row["model_b_assigned_count"],
        ),
        target_sample_count=row["target_sample_count"],
        traffic_sample_bps=row["traffic_sample_bps"],
        sampling_duration_seconds=row["sampling_duration_seconds"],
        feedback_collection_enabled=bool(row["feedback_collection_enabled"]),
        feedback_window_seconds=row["feedback_window_seconds"],
        algorithm_version=row["allocation_algorithm_version"],
        status=row["status"],
        assigned_count=row["assigned_count"],
        facts_version=row["facts_version"],
        started_at=_time(row["started_at"]),
        sampling_deadline=_time(row["sampling_deadline_at"]),
        sampling_ended_at=_optional_time(row["sampling_ended_at"]),
        feedback_deadline=_optional_time(row["feedback_deadline_at"]),
        settlement_deadline=_optional_time(row["settlement_deadline_at"]),
        completed_at=_optional_time(row["completed_at"]),
        created_at=_time(row["created_at"]),
        updated_at=_time(row["updated_at"]),
    )


def _owned_experiment_row(
    connection: sqlite3.Connection,
    tenant_id: str,
    experiment_id: str,
) -> sqlite3.Row:
    """封装实验服务的冻结配置或并发围栏，保持租户归属和幂等语义。"""
    row = connection.execute(
        "SELECT * FROM model_experiments WHERE tenant_id=? AND experiment_id=?",
        (tenant_id, experiment_id),
    ).fetchone()
    if row is None:
        # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
        raise ApiError("experiment.not_found")
    return row


def _owned_experiment_id(value: object) -> str:
    """封装实验服务的冻结配置或并发围栏，保持租户归属和幂等语义。"""
    if type(value) is not str or _EXPERIMENT_ID.fullmatch(value) is None:
        # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
        raise ApiError("experiment.not_found")
    return value


def _create_fingerprint_fields(payload: CreateExperimentRequest) -> dict[str, object]:
    """封装实验服务的冻结配置或并发围栏，保持租户归属和幂等语义。"""
    return {
        "name": payload.name,
        "project_id": payload.project_id,
        "environment_id": payload.environment_id,
        "model_alias_id": payload.model_alias_id,
        "model_a_id": payload.model_a_id,
        "model_b_id": payload.model_b_id,
        "target_sample_count": payload.target_sample_count,
        "traffic_sample_bps": payload.traffic_sample_bps,
        "model_a_allocation_bps": payload.model_a_allocation_bps,
        "sampling_duration_seconds": payload.sampling_duration_seconds,
        "feedback_collection_enabled": payload.feedback_collection_enabled,
        "feedback_window_seconds": payload.feedback_window_seconds,
    }


def _status_filter(value: object) -> str | None:
    """规范化实验分页与状态输入，并以签名游标阻止跨租户或篡改翻页。"""
    if value is None:
        return None
    if type(value) is not str or value not in {"active", "collecting_feedback", "completed"}:
        # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
        raise ApiError("experiment.query_invalid")
    return value


def _limit(value: object) -> int:
    """规范化实验分页与状态输入，并以签名游标阻止跨租户或篡改翻页。"""
    if value is None:
        return 50
    if type(value) is not str or not value.isascii() or not value.isdigit():
        # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
        raise ApiError("experiment.query_invalid")
    parsed = int(value)
    if not 1 <= parsed <= 100:
        # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
        raise ApiError("experiment.query_invalid")
    return parsed


def _encode_cursor(
    *,
    created_at: str,
    experiment_id: str,
    secret: str,
    tenant_id: str,
    status: str | None,
) -> str:
    """规范化实验分页与状态输入，并以签名游标阻止跨租户或篡改翻页。"""
    payload = json.dumps(
        {
            "schema_version": 1,
            "tenant_id": tenant_id,
            "status": status,
            "created_at": created_at,
            "experiment_id": experiment_id,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    signature = hmac.new(_secret_bytes(secret), _CURSOR_DOMAIN + payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload + signature).decode("ascii").rstrip("=")


def _decode_cursor(
    value: object,
    *,
    secret: str,
    tenant_id: str,
    status: str | None,
) -> tuple[str, str] | None:
    """规范化实验分页与状态输入，并以签名游标阻止跨租户或篡改翻页。"""
    if value is None:
        return None
    if type(value) is not str or not value or len(value) > _CURSOR_MAX_LENGTH:
        # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
        raise ApiError("experiment.query_invalid")
    try:
        if any(
            character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
            for character in value
        ):
            # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
            raise ValueError
        raw = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
        if len(raw) <= 32:
            # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
            raise ValueError
        payload, signature = raw[:-32], raw[-32:]
        expected = hmac.new(
            _secret_bytes(secret),
            _CURSOR_DOMAIN + payload,
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(signature, expected):
            # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
            raise ValueError
        decoded = json.loads(payload.decode("ascii"))
        if type(decoded) is not dict or set(decoded) != {
            "schema_version",
            "tenant_id",
            "status",
            "created_at",
            "experiment_id",
        } or (
            decoded["schema_version"] != 1
            or decoded["tenant_id"] != tenant_id
            or decoded["status"] != status
            or type(decoded["created_at"]) is not str
            or type(decoded["experiment_id"]) is not str
            or _EXPERIMENT_ID.fullmatch(decoded["experiment_id"]) is None
        ):
            # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
            raise ValueError
        _time(decoded["created_at"])
        return decoded["created_at"], decoded["experiment_id"]
    # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError, binascii.Error):
        # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
        raise ApiError("experiment.query_invalid") from None


def _secret_bytes(value: object) -> bytes:
    """封装实验服务的冻结配置或并发围栏，保持租户归属和幂等语义。"""
    if type(value) is not str:
        # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
        raise ApiError("experiment.unavailable")
    try:
        encoded = value.encode("utf-8", errors="strict")
    # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
    except UnicodeError:
        # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
        raise ApiError("experiment.unavailable") from None
    if not 16 <= len(encoded) <= 512:
        # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
        raise ApiError("experiment.unavailable")
    return encoded


def _database_now(connection: sqlite3.Connection) -> datetime:
    """封装实验服务的冻结配置或并发围栏，保持租户归属和幂等语义。"""
    row = connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')").fetchone()
    if row is None:
        # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
        raise ValueError
    return _time(row[0])


def _time(value: object) -> datetime:
    """提供实验服务的数据库时间或连接边界，由调用方负责事务提交、回滚与关闭。"""
    if type(value) is not str:
        # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
        raise ValueError
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
        raise ValueError
    return parsed.astimezone(UTC)


def _optional_time(value: object) -> datetime | None:
    """提供实验服务的数据库时间或连接边界，由调用方负责事务提交、回滚与关闭。"""
    return None if value is None else _time(value)


def _db_time(value: datetime) -> str:
    """提供实验服务的数据库时间或连接边界，由调用方负责事务提交、回滚与关闭。"""
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _connect(config: AppConfig) -> sqlite3.Connection:
    """提供实验服务的数据库时间或连接边界，由调用方负责事务提交、回滚与关闭。"""
    try:
        return connect_database(config.database_url)
    # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
    except DbError:
        # 租户归属、幂等指纹或结果状态不一致时关闭请求，并返回统一实验错误。
        raise ApiError("experiment.unavailable") from None


__all__ = [
    "create_experiment",
    "get_experiment",
    "get_experiment_results",
    "list_experiments",
]
