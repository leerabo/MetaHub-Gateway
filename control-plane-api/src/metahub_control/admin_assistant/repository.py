"""PRD-077 的租户隔离只读模型与不可变审计原子持久化。"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import wraps
from uuid import uuid4

from metahub_shared.db.connection import connect_database, database_operation_deadline
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ERROR_SPECS, ApiError
from metahub_shared.runtime.webhooks import build_assistant_queried_event

from metahub_control.audit import (
    AuditRecord,
    AuditRequestMeta,
    dispatch_audit_recorded,
    new_audit_log_id,
    record_audit_log,
)
from metahub_control.auth import (
    AuthContext,
    refresh_transaction_authentication_fingerprint,
    require_permission,
)

# 执行模式、回答版本与本地零模型成本是 Query/Audit/AuditLog/Webhook 共用的冻结事实；
# 查询类型到来源权限的映射必须按整次查询求交，不能按已返回的非零来源动态降权。
EXECUTION_MODE = "deterministic_read_model_v1"
ANSWER_SCHEMA_VERSION = "admin-assistant-answer-v1"
MODEL_COST_STATUS = "not_applicable_local"
# RequestLog 的 error_code 是历史/上游写入字段，不能直接成为 Assistant 对外文本。
# 在模块装载时冻结共享公开目录，避免运行中可变字典或任意数据库值扩大响应合同。
_PUBLIC_ERROR_CODES = frozenset(ERROR_SPECS)
_SAFE_ERROR_CODE_FALLBACK = "infra.unhandled"
_SQLITE_SAFE_ERROR_FUNCTION = "metahub_admin_assistant_safe_error_code"

QUERY_PERMISSIONS: Mapping[str, tuple[str, ...]] = {
    "usage_statistics": (
        "admin_assistant.query",
        "dashboard.read",
        "billing.usage.read",
    ),
    "error_explanation": (
        "admin_assistant.query",
        "request_log.read",
    ),
    "cost_advice": (
        "admin_assistant.query",
        "dashboard.read",
        "billing.usage.read",
        "billing.finops.read",
        "billing.budget.read",
        "billing.anomaly.read",
        "model_price.read",
    ),
    "policy_explanation": (
        "admin_assistant.query",
        "policy.read",
    ),
}


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    """同一只读事务取得的租户来源值、计数和实时授权指纹。"""

    query_type: str
    values: dict[str, object]
    record_counts: dict[str, int]
    permission_revision: int
    authorization_fingerprint: str
    snapshot_at: datetime


@dataclass(frozen=True, slots=True)
class PersistedAttempt:
    """Query、专用 Audit、通用 AuditLog 与 stable event 原子提交后的安全身份。"""

    assistant_query_id: str
    assistant_audit_id: str
    assistant_event_id: str
    audit_record: AuditRecord
    context: AuthContext


def required_permissions(query_type: str, *, target_request_id: str | None) -> tuple[str, ...]:
    """返回查询类型需要的完整排序权限；request-specific 策略额外要求解释读取。"""

    try:
        permissions = QUERY_PERMISSIONS[query_type]
    # 未知查询类型是服务端/Shared 合同漂移，只公开固定 unsupported 机器码。
    except KeyError as exc:
        raise ApiError("admin_assistant.query_unsupported") from exc
    # request-specific 策略读取了独立 RequestExplanation 域，不能只凭 policy.read 放行。
    if query_type == "policy_explanation" and target_request_id is not None:
        permissions = (*permissions, "request_explanation.read")
    # canonical 排序与数据库 trigger 的 exact JSON 比较保持逐字一致。
    return tuple(sorted(permissions))


def _within_database_deadline(operation):
    """Keep the same absolute budget through checkout, statements and commit."""

    @wraps(operation)
    def run(*args, deadline: float, **kwargs):
        with database_operation_deadline(deadline):
            return operation(*args, deadline=deadline, **kwargs)

    return run


def _is_postgresql(connection: sqlite3.Connection) -> bool:
    return getattr(connection, "backend", None) == "postgresql"


@_within_database_deadline
def read_source_snapshot(
    config: AppConfig,
    context: AuthContext,
    *,
    query_type: str,
    target_request_id: str | None,
    window_started_at: datetime,
    window_ended_at: datetime,
    deadline: float,
    connection: sqlite3.Connection | None = None,
    invalidate_connection: Callable[[sqlite3.Connection], None] | None = None,
) -> SourceSnapshot:
    """在实时来源域 RBAC 下读取同一事务内、query-only 的租户快照。"""

    owns_connection = connection is None
    if connection is None:
        connection = _connect(config, "admin_assistant.source_unavailable")
    try:
        previous_busy_timeout = _read_busy_timeout(
            connection,
            error_code="admin_assistant.source_unavailable",
        )
    # 无法读取原 busy_timeout 时不能覆盖长期连接状态，先摘除该连接再传播固定错误。
    except ApiError:
        _discard_untrusted_connection(
            connection,
            owns_connection=owns_connection,
            invalidate_connection=invalidate_connection,
        )
        raise
    try:
        _apply_sqlite_deadline(
            connection,
            deadline,
            error_code="admin_assistant.source_unavailable",
        )
        if _is_postgresql(connection):
            # One stable, server-enforced read-only snapshot, including live
            # authorization. Transaction-local settings disappear on rollback.
            connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        else:
            connection.execute("PRAGMA query_only=ON")
            connection.set_progress_handler(
                lambda: 1 if time.monotonic() >= deadline else 0,
                1_000,
            )
            connection.execute("BEGIN")
        # 路由认证已完整重算目录和矩阵哈希；来源事务在同一快照内复核 Session、角色指纹
        # 及本次所需 grant，关闭认证后的撤权竞态，同时避免再次扫描无关权限目录。
        live_context = refresh_transaction_authentication_fingerprint(
            connection,
            context,
            unavailable_error_code="admin_assistant.source_unavailable",
        )
        permission_codes = required_permissions(
            query_type,
            target_request_id=target_request_id,
        )
        for permission_code in permission_codes:
            require_permission(live_context, permission_code)
        _require_live_permission_codes(connection, live_context, permission_codes)
        start = _db_time(window_started_at)
        end = _db_time(window_ended_at)
        values, counts = _read_query_values(
            connection,
            tenant_id=live_context.tenant.tenant_id,
            query_type=query_type,
            target_request_id=target_request_id,
            window_started_at=start,
            window_ended_at=end,
        )
        snapshot_at = datetime.now(UTC)
        return SourceSnapshot(
            query_type=query_type,
            values=values,
            record_counts=counts,
            permission_revision=live_context.permission_revision,
            authorization_fingerprint=live_context.authorization_fingerprint,
            snapshot_at=snapshot_at,
        )
    # 权限撤销和固定来源错误保留机器码，但仍须释放只读事务与 progress handler。
    except ApiError:
        raise
    # SQLite busy、schema 或 progress 中断细节可能包含路径/SQL，只映射为来源不可用。
    except sqlite3.Error as exc:
        # finally 统一回滚并恢复连接级状态；底层路径、SQL 和锁信息不得越过仓储层。
        raise ApiError("admin_assistant.source_unavailable") from exc
    finally:
        cleanup_error = _restore_connection_state(
            connection,
            previous_busy_timeout=previous_busy_timeout,
            restore_query_only=not owns_connection,
        )
        if owns_connection:
            try:
                connection.close()
            # 独占连接关闭失败也意味着资源终态不可信，统一并入来源清理失败。
            except sqlite3.Error as exc:
                cleanup_error = cleanup_error or exc
        # handler、事务、PRAGMA 或 close 任一未恢复时，禁止把连接继续交回运行时。
        if cleanup_error is not None:
            if not owns_connection:
                _discard_untrusted_connection(
                    connection,
                    owns_connection=False,
                    invalidate_connection=invalidate_connection,
                )
            # 清理失败意味着无法证明长期连接可安全复用；只公开固定来源错误。
            raise ApiError("admin_assistant.source_unavailable") from cleanup_error


@_within_database_deadline
def persist_attempt(
    config: AppConfig,
    context: AuthContext,
    *,
    query_type: str,
    target_request_id: str | None,
    window_started_at: datetime | None,
    window_ended_at: datetime | None,
    outcome: str,
    failure_code: str | None,
    required_permission_codes: tuple[str, ...],
    source_types: tuple[str, ...],
    claim_count: int,
    source_count: int,
    redaction_count: int,
    request_id: str,
    trace_id: str,
    request_meta: AuditRequestMeta,
    started_monotonic: float,
    deadline: float,
    expected_authorization_fingerprint: str | None,
    enforce_permissions: bool,
    connection: sqlite3.Connection | None = None,
    invalidate_connection: Callable[[sqlite3.Connection], None] | None = None,
) -> PersistedAttempt:
    """在同一租户事务原子持久化查询、专用审计、通用审计和待投递事件来源。"""

    assistant_query_id = f"asq_{uuid4().hex}"
    assistant_audit_id = f"asa_{uuid4().hex}"
    assistant_event_id = f"asevt_{uuid4().hex}"
    audit_log_id = new_audit_log_id()
    now = datetime.now(UTC)
    created_at = _db_time(now)
    owns_connection = connection is None
    if connection is None:
        connection = _connect(config, "admin_assistant.audit_unavailable")
    try:
        previous_busy_timeout = _read_busy_timeout(
            connection,
            error_code="admin_assistant.audit_unavailable",
        )
    # 写连接原锁预算不可读取时禁止进入治理事务，并按身份摘除不可信长期连接。
    except ApiError:
        _discard_untrusted_connection(
            connection,
            owns_connection=owns_connection,
            invalidate_connection=invalidate_connection,
        )
        raise
    try:
        # SQLite 默认锁等待约五秒；最终写入必须使用 endpoint 剩余预算，不能把 1.5 秒
        # 期限重置为驱动默认值。progress handler 同时约束拿到写锁后的 SQL 执行。
        _apply_sqlite_deadline(
            connection,
            deadline,
            error_code="admin_assistant.audit_unavailable",
        )
        if not _is_postgresql(connection):
            connection.set_progress_handler(
                lambda: 1 if time.monotonic() >= deadline else 0,
                1_000,
            )
        connection.execute("BEGIN IMMEDIATE")
        if _is_postgresql(connection):
            connection.execute("SET LOCAL synchronous_commit=on")
        live_context = refresh_transaction_authentication_fingerprint(
            connection,
            context,
            unavailable_error_code="admin_assistant.audit_unavailable",
        )
        # 成功路径在最终写事务内重读完整矩阵并比对来源快照指纹，关闭撤权竞态。
        if enforce_permissions:
            for permission_code in required_permission_codes:
                require_permission(live_context, permission_code)
            _require_live_permission_codes(
                connection,
                live_context,
                required_permission_codes,
            )
            if (
                expected_authorization_fingerprint is not None
                and live_context.authorization_fingerprint != expected_authorization_fingerprint
            ):
                # revision/fingerprint 任一漂移都丢弃已构造回答，不能以旧权限提交 answered。
                raise ApiError("auth.permission_denied")
        # PAGE-107 初始认证保持只读，避免并发 attempt 在建立 actor 前争 writer；可信 Session 的
        # last_seen 与 Query/Audit/AuditLog/outbox 同事务更新，回滚时不能留下伪成功访问痕迹。
        updated_session = connection.execute(
            """
            UPDATE sessions SET last_seen_at=?,updated_at=?
            WHERE tenant_id=? AND session_id=? AND user_id=? AND status='active'
            """,
            (
                created_at,
                created_at,
                live_context.tenant.tenant_id,
                live_context.session.session_id,
                live_context.user.user_id,
            ),
        )
        # Session owner 未被唯一更新说明最终复核后已失效，禁止提交任何回答或治理事实。
        if updated_session.rowcount != 1:
            raise ApiError("auth.permission_denied")
        # duration 必须包含写锁等待与最终认证复核；在 BEGIN IMMEDIATE 之前计算会系统性低报。
        duration_ms = _persist_duration_ms(started_monotonic)
        connection.execute(
            """
            INSERT INTO assistant_queries(
              assistant_query_id,tenant_id,actor_user_id,actor_session_id,
              query_type,target_request_id,window_started_at,window_ended_at,
              outcome,failure_code,claim_count,source_count,redaction_count,
              query_duration_ms,operation_request_id,operation_trace_id,
              execution_mode,model_call_count,model_input_tokens,model_output_tokens,
              model_cost_cny_micros,model_cost_status,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,0,0,?,?)
            """,
            (
                assistant_query_id,
                live_context.tenant.tenant_id,
                live_context.user.user_id,
                live_context.session.session_id,
                query_type,
                target_request_id,
                _db_time(window_started_at) if window_started_at else None,
                _db_time(window_ended_at) if window_ended_at else None,
                outcome,
                failure_code,
                claim_count,
                source_count,
                redaction_count,
                duration_ms,
                request_id,
                trace_id,
                EXECUTION_MODE,
                MODEL_COST_STATUS,
                created_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO assistant_audits(
              assistant_audit_id,assistant_event_id,tenant_id,assistant_query_id,
              actor_user_id,actor_session_id,audit_log_id,required_permissions_json,
              source_types_json,permission_revision,authorization_fingerprint,
              answer_schema_version,outcome,claim_count,source_count,redaction_count,
              query_duration_ms,operation_request_id,operation_trace_id,execution_mode,
              model_call_count,model_input_tokens,model_output_tokens,
              model_cost_cny_micros,model_cost_status,event_dispatch_status,
              event_dispatch_attempts,event_next_attempt_at,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,0,0,?,'pending',0,?,?,?)
            """,
            (
                assistant_audit_id,
                assistant_event_id,
                live_context.tenant.tenant_id,
                assistant_query_id,
                live_context.user.user_id,
                live_context.session.session_id,
                audit_log_id,
                _json_array(required_permission_codes),
                _json_array(source_types),
                live_context.permission_revision,
                live_context.authorization_fingerprint,
                ANSWER_SCHEMA_VERSION,
                outcome,
                claim_count,
                source_count,
                redaction_count,
                duration_ms,
                request_id,
                trace_id,
                EXECUTION_MODE,
                MODEL_COST_STATUS,
                created_at,
                created_at,
                created_at,
            ),
        )
        audit_record = record_audit_log(
            connection,
            live_context,
            action="admin_assistant.queried",
            object_type="assistant_query",
            object_id=assistant_query_id,
            object_name=None,
            before_summary={},
            after_summary={
                "query_type": query_type,
                "outcome": outcome,
                "claim_count": claim_count,
                "source_count": source_count,
                "redaction_count": redaction_count,
                "execution_mode": EXECUTION_MODE,
                "model_call_count": 0,
                "model_cost_status": MODEL_COST_STATUS,
            },
            request_meta=request_meta,
            request_id=request_id,
            trace_id=trace_id,
            audit_log_id=audit_log_id,
            created_at=now,
        )
        # 通用 AuditLog 是专用 Audit 的延迟 owner；缺失意味着整组治理事实不能提交。
        if audit_record is None:
            raise ApiError("admin_assistant.audit_unavailable")
        # assistant.queried 的 original delivery 集合必须与 Query/Audit/AuditLog 在同一
        # 写事务冻结；即使当前没有订阅者，提交后也不能把后来新增 Endpoint 追溯到旧事实。
        try:
            event = build_assistant_queried_event(
                tenant_id=live_context.tenant.tenant_id,
                actor_id=live_context.user.user_id,
                assistant_query_id=assistant_query_id,
                event_id=assistant_event_id,
                trace_id=trace_id,
                source_request_id=request_id,
                query_type=query_type,
                outcome=outcome,
                claim_count=claim_count,
                source_count=source_count,
                redaction_count=redaction_count,
                duration_ms=duration_ms,
                timestamp=now,
            )
            # 延迟导入避免管理助手仓储与 Webhook 恢复 owner 形成模块初始化环。
            from metahub_control.webhooks.service import (
                freeze_webhook_event_delivery_snapshot,
            )

            freeze_webhook_event_delivery_snapshot(
                connection,
                event,
                frozen_at=created_at,
            )
        # 事件与 delivery 快照必须和四类治理事实同事务，构造漂移也要整体失败关闭。
        except (ApiError, TypeError, ValueError) as exc:
            # 快照 helper 不提交事务；任一构造或持久失败由 finally 连同四类治理事实一起回滚。
            raise ApiError("admin_assistant.audit_unavailable") from exc
        # busy_timeout 是每次锁等待预算；提交前按同一绝对期限重新收紧，不能重复使用旧余额。
        _apply_sqlite_deadline(
            connection,
            deadline,
            error_code="admin_assistant.audit_unavailable",
        )
        connection.commit()
        return PersistedAttempt(
            assistant_query_id=assistant_query_id,
            assistant_audit_id=assistant_audit_id,
            assistant_event_id=assistant_event_id,
            audit_record=audit_record,
            context=live_context,
        )
    # 业务权限和固定审计错误保留语义，但同事务四类事实必须一起回滚。
    except ApiError:
        raise
    # 约束、busy 或 commit 错误都可能披露内部结构，对外统一为审计不可用。
    except sqlite3.Error as exc:
        # finally 统一回滚；四类事实无法全部提交时整组失败，并隐藏 SQLite 内部错误细节。
        raise ApiError("admin_assistant.audit_unavailable") from exc
    finally:
        cleanup_error = _restore_connection_state(
            connection,
            previous_busy_timeout=previous_busy_timeout,
            restore_query_only=False,
        )
        if owns_connection:
            try:
                connection.close()
            # 独占写连接关闭失败会破坏可证明资源终态，保留为最早清理错误。
            except sqlite3.Error as exc:
                cleanup_error = cleanup_error or exc
        # 事务提交后仍须证明 handler 与连接 PRAGMA 已恢复，否则长期 owner 不得复用。
        if cleanup_error is not None:
            if not owns_connection:
                _discard_untrusted_connection(
                    connection,
                    owns_connection=False,
                    invalidate_connection=invalidate_connection,
                )
            # 提交后若连接状态无法恢复，也不能把本次治理写入伪装成可安全复用的成功。
            raise ApiError("admin_assistant.audit_unavailable") from cleanup_error


def dispatch_generic_audit(
    config: AppConfig,
    persisted: PersistedAttempt,
    *,
    request_id: str,
    trace_id: str,
) -> None:
    """提交后发送通用 audit.recorded；失败不改写已提交的专用治理事实。"""

    dispatch_audit_recorded(
        config,
        persisted.context,
        persisted.audit_record,
        request_id=request_id,
        trace_id=trace_id,
    )


def _read_query_values(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    query_type: str,
    target_request_id: str | None,
    window_started_at: str,
    window_ended_at: str,
) -> tuple[dict[str, object], dict[str, int]]:
    """按冻结查询类型选择 tenant-first SQL，不接受问题生成的表名或自由查询。"""

    # 四个分支只调用静态 SQL helper，query_type 不能参与 SQL 字符串拼接。
    if query_type == "usage_statistics":
        return _usage_values(connection, tenant_id, window_started_at, window_ended_at)
    if query_type == "error_explanation":
        return _error_values(
            connection,
            tenant_id,
            target_request_id,
            window_started_at,
            window_ended_at,
        )
    # 成本建议只调用冻结的 tenant-first 聚合，不允许意图文本参与 SQL 拼接。
    if query_type == "cost_advice":
        return _cost_values(connection, tenant_id, window_started_at, window_ended_at)
    # 策略解释同样限定到静态 helper，并按可选 request_id 使用参数绑定。
    if query_type == "policy_explanation":
        return _policy_values(connection, tenant_id, target_request_id)
    # 未知查询类型不得回退到某个默认数据域，避免越权读取来源。
    raise ApiError("admin_assistant.query_unsupported")


def _usage_values(
    connection: sqlite3.Connection,
    tenant_id: str,
    start: str,
    end: str,
) -> tuple[dict[str, object], dict[str, int]]:
    """读取当前租户时间窗内的概览与 Usage 聚合，不返回请求或用量行标识。"""

    overview = connection.execute(
        """
        SELECT COUNT(*) AS request_count,
               COALESCE(SUM(CASE WHEN status='success' THEN 1 ELSE 0 END),0) AS success_count,
               COALESCE(SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END),0) AS failure_count,
               COALESCE(CAST(ROUND(AVG(CASE WHEN latency_ms>=0 THEN latency_ms END)) AS INTEGER),0)
                 AS average_latency_ms
        FROM request_logs
        WHERE tenant_id=? AND julianday(created_at)>=julianday(?)
          AND julianday(created_at)<julianday(?)
        """,
        (tenant_id, start, end),
    ).fetchone()
    usage = connection.execute(
        """
        SELECT COUNT(*) AS usage_count,COALESCE(SUM(input_tokens),0) AS input_tokens,
               COALESCE(SUM(output_tokens),0) AS output_tokens,
               COALESCE(SUM(total_tokens),0) AS total_tokens,
               COALESCE(SUM(CAST(ROUND(cost_cny*1000000) AS BIGINT)),0) AS cost_cny_micros,
               COALESCE(SUM(estimated),0) AS estimated_count
        FROM usage_ledger
        WHERE tenant_id=? AND julianday(created_at)>=julianday(?)
          AND julianday(created_at)<julianday(?)
        """,
        (tenant_id, start, end),
    ).fetchone()
    values = {key: int(overview[key] or 0) for key in overview.keys()}
    values.update({key: int(usage[key] or 0) for key in usage.keys()})
    return values, {"overview": values["request_count"], "usage": values["usage_count"]}


def _error_values(
    connection: sqlite3.Connection,
    tenant_id: str,
    target_request_id: str | None,
    start: str,
    end: str,
) -> tuple[dict[str, object], dict[str, int]]:
    """读取当前租户的固定错误分类；目标 request 查询始终 tenant-first。"""

    # request-specific 查询先绑定 tenant_id，再匹配 request_id；外租户与不存在保持同形 404。
    if target_request_id is not None:
        row = connection.execute(
            """
            SELECT status,error_code FROM request_logs
            WHERE tenant_id=? AND request_id=?
            """,
            (tenant_id, target_request_id),
        ).fetchone()
        # 不枚举目标属于其他租户还是不存在，避免 request_id 成为跨租户探针。
        if row is None:
            raise ApiError("admin_assistant.source_not_found")
        return {
            "request_status": str(row["status"]),
            "error_code": safe_request_log_error_code(row["error_code"]),
        }, {"request_logs": 1}
    failure_row = connection.execute(
        """
        SELECT COUNT(*) AS failure_count
        FROM request_logs
        WHERE tenant_id=? AND status='failed'
          AND julianday(created_at)>=julianday(?) AND julianday(created_at)<julianday(?)
        """,
        (tenant_id, start, end),
    ).fetchone()
    # UDF 只接收单个分类字段并返回共享公开目录中的固定码；先映射再 GROUP BY，
    # 让 NULL、未知值和任意敏感 canary 合并到同一安全桶，同时保持总计不变。
    if _is_postgresql(connection):
        # Bind the frozen allowlist; never install a Python UDF or pass unknown
        # historical error text to the answer. Map before GROUP BY and LIMIT.
        safe_codes = tuple(sorted(_PUBLIC_ERROR_CODES))
        placeholders = ",".join("?" for _ in safe_codes)
        safe_expression = f"CASE WHEN error_code IN ({placeholders}) THEN error_code ELSE ? END"
        rows = connection.execute(
            f"""
            SELECT safe.error_code,COUNT(*) AS error_count
            FROM (
              SELECT {safe_expression} AS error_code FROM request_logs
              WHERE tenant_id=? AND status='failed'
                AND julianday(created_at)>=julianday(?) AND julianday(created_at)<julianday(?)
            ) safe
            GROUP BY safe.error_code ORDER BY error_count DESC,safe.error_code ASC LIMIT 5
            """,
            (*safe_codes, _SAFE_ERROR_CODE_FALLBACK, tenant_id, start, end),
        ).fetchall()
        errors = tuple((str(row["error_code"]), int(row["error_count"])) for row in rows)
        failure_count = int(failure_row["failure_count"] or 0)
        return (
            {"top_errors": errors, "failure_count": failure_count},
            {"request_logs": failure_count},
        )
    connection.create_function(
        _SQLITE_SAFE_ERROR_FUNCTION,
        1,
        safe_request_log_error_code,
        deterministic=True,
    )
    rows = connection.execute(
        """
        SELECT metahub_admin_assistant_safe_error_code(error_code) AS error_code,
               COUNT(*) AS error_count
        FROM request_logs
        WHERE tenant_id=? AND status='failed'
          AND julianday(created_at)>=julianday(?) AND julianday(created_at)<julianday(?)
        GROUP BY metahub_admin_assistant_safe_error_code(error_code)
        ORDER BY error_count DESC,error_code ASC LIMIT 5
        """,
        (tenant_id, start, end),
    ).fetchall()
    errors = tuple((str(row["error_code"]), int(row["error_count"])) for row in rows)
    # Top-5 只控制回答分类明细；总失败数和来源记录数必须覆盖第六类及之后的错误。
    failure_count = int(failure_row["failure_count"] or 0)
    return {"top_errors": errors, "failure_count": failure_count}, {"request_logs": failure_count}


def _cost_values(
    connection: sqlite3.Connection,
    tenant_id: str,
    start: str,
    end: str,
) -> tuple[dict[str, object], dict[str, int]]:
    """联合读取 Usage、FinOps、预算、异常与价格的同租户有界计数和微元金额。"""

    overview, overview_counts = _usage_values(connection, tenant_id, start, end)
    ledger = connection.execute(
        """
        SELECT COUNT(*) AS ledger_count,
          COALESCE(SUM(CASE WHEN direction='debit'
                            THEN COALESCE(normalized_cny_micros,0) ELSE 0 END),0)
            AS gross_debit_cost_cny_micros
        FROM finops_ledger_entries
        WHERE tenant_id=? AND status='posted' AND julianday(created_at)>=julianday(?)
          AND julianday(created_at)<julianday(?)
        """,
        (tenant_id, start, end),
    ).fetchone()
    # 最高成本模型只按同租户 posted debit 聚合；别名来自同 tenant Model owner，
    # 不能用客户端问题或跨租户模型补全展示值。
    top_model = connection.execute(
        """
        SELECT COALESCE(model.model_alias,'unattributed') AS model_alias,
          COALESCE(SUM(entry.normalized_cny_micros),0) AS model_cost_cny_micros
        FROM finops_ledger_entries entry
        LEFT JOIN models model
          ON model.tenant_id=entry.tenant_id AND model.model_id=entry.model_id
        WHERE entry.tenant_id=? AND entry.status='posted' AND entry.direction='debit'
          AND COALESCE(entry.normalized_cny_micros,0)>0
          AND julianday(entry.created_at)>=julianday(?)
          AND julianday(entry.created_at)<julianday(?)
        GROUP BY entry.model_id,model.model_alias
        ORDER BY model_cost_cny_micros DESC,model_alias ASC LIMIT 1
        """,
        (tenant_id, start, end),
    ).fetchone()
    budgets = connection.execute(
        "SELECT COUNT(*) AS count FROM budget_rules WHERE tenant_id=? AND status='active'",
        (tenant_id,),
    ).fetchone()
    anomalies = connection.execute(
        "SELECT COUNT(*) AS count FROM cost_anomalies WHERE tenant_id=? AND status='open'",
        (tenant_id,),
    ).fetchone()
    prices = connection.execute(
        "SELECT COUNT(*) AS count FROM model_price_plans WHERE tenant_id=?",
        (tenant_id,),
    ).fetchone()
    values = dict(overview)
    values.update(
        ledger_count=int(ledger["ledger_count"] or 0),
        gross_debit_cost_cny_micros=int(ledger["gross_debit_cost_cny_micros"] or 0),
        top_cost_model_alias=(
            str(top_model["model_alias"]) if top_model is not None else "unavailable"
        ),
        top_cost_model_cny_micros=(
            int(top_model["model_cost_cny_micros"] or 0) if top_model is not None else 0
        ),
        active_budget_count=int(budgets["count"] or 0),
        open_anomaly_count=int(anomalies["count"] or 0),
        model_price_count=int(prices["count"] or 0),
    )
    counts = {
        **overview_counts,
        "finops_ledger": values["ledger_count"],
        "budgets": values["active_budget_count"],
        "cost_anomalies": values["open_anomaly_count"],
        "model_prices": values["model_price_count"],
    }
    return values, counts


def _policy_values(
    connection: sqlite3.Connection,
    tenant_id: str,
    target_request_id: str | None,
) -> tuple[dict[str, object], dict[str, int]]:
    """读取当前租户策略数量、动作与发布版本，不读取或返回 DSL 表达式。"""

    policies = connection.execute(
        """
        SELECT COALESCE(SUM(grouped.policy_count),0) AS active_count,
          COALESCE(SUM(CASE WHEN grouped.policy_count>1 THEN 1 ELSE 0 END),0)
            AS priority_collision_count
        FROM (
          SELECT priority,COUNT(*) AS policy_count FROM policies
          WHERE tenant_id=? AND status='active' GROUP BY priority
        ) grouped
        """,
        (tenant_id,),
    ).fetchone()
    action_rows = connection.execute(
        """
        SELECT action.action_type,COUNT(*) AS action_count
        FROM policy_actions action JOIN policies policy
          ON policy.tenant_id=action.tenant_id AND policy.policy_id=action.policy_id
        WHERE action.tenant_id=? AND policy.status='active'
        GROUP BY action.action_type ORDER BY action.action_type
        """,
        (tenant_id,),
    ).fetchall()
    head = connection.execute(
        "SELECT current_snapshot_version FROM policy_snapshot_heads WHERE tenant_id=?",
        (tenant_id,),
    ).fetchone()
    values: dict[str, object] = {
        "active_policy_count": int(policies["active_count"] or 0),
        "priority_collision_count": int(policies["priority_collision_count"] or 0),
        "action_counts": tuple(
            (str(row["action_type"]), int(row["action_count"])) for row in action_rows
        ),
        "published_snapshot_version": int(head["current_snapshot_version"]) if head else 0,
    }
    counts = {
        "policies": int(policies["active_count"] or 0),
        "policy_snapshots": 1 if head else 0,
    }
    # request-specific 策略解释只公开已持久化决定分类，并要求同租户解释事实实际存在。
    if target_request_id is not None:
        request_row = connection.execute(
            """
            SELECT policy_decision,policy_reason_code,policy_snapshot_status,snapshot_version
            FROM request_logs WHERE tenant_id=? AND request_id=?
            """,
            (tenant_id, target_request_id),
        ).fetchone()
        explanation_count = connection.execute(
            """
            SELECT COUNT(*) AS count FROM request_explanations
            WHERE tenant_id=? AND source_request_id=?
            """,
            (tenant_id, target_request_id),
        ).fetchone()
        # RequestLog 或 Explanation 任一缺失都使用同形 404，不暴露跨域 owner 差异。
        if request_row is None or int(explanation_count["count"] or 0) == 0:
            raise ApiError("admin_assistant.source_not_found")
        values.update(
            request_policy_decision=str(request_row["policy_decision"] or "not_evaluated"),
            request_policy_reason_code=str(request_row["policy_reason_code"] or "not_recorded"),
            request_policy_snapshot_status=str(request_row["policy_snapshot_status"]),
            request_snapshot_version=int(request_row["snapshot_version"] or 0),
        )
        counts["request_explanations"] = int(explanation_count["count"])
    return values, counts


def _require_live_permission_codes(
    connection: sqlite3.Connection,
    context: AuthContext,
    permission_codes: tuple[str, ...],
) -> None:
    """在 writer reservation 内精确确认本次所需 grant 仍属于当前角色。"""

    expected = tuple(sorted(set(permission_codes)))
    # 内部权限集合也必须是非空规范序列，避免调用方用重复或乱序 grant 绕过精确比对。
    if not expected or expected != permission_codes:
        raise ApiError("admin_assistant.audit_unavailable")
    placeholders = ",".join("?" for _ in expected)
    rows = connection.execute(
        f"""
        SELECT permission.code
        FROM roles role
        JOIN role_permissions relation
          ON relation.tenant_id=role.tenant_id AND relation.role_id=role.role_id
        JOIN permissions permission
          ON permission.tenant_id=relation.tenant_id
         AND permission.permission_id=relation.permission_id
        WHERE role.tenant_id=? AND role.code=? AND role.status='active'
          AND permission.status='active' AND permission.code IN ({placeholders})
        ORDER BY permission.code
        """,
        (
            context.tenant.tenant_id,
            context.user.role,
            *expected,
        ),
    ).fetchall()
    # Writer 锁内的真实授权必须逐项等于来源阶段需求，缺一项即丢弃已构造回答。
    if tuple(str(row["code"]) for row in rows) != expected:
        raise ApiError("auth.permission_denied")


def _apply_sqlite_deadline(
    connection: sqlite3.Connection,
    deadline: float,
    *,
    error_code: str,
) -> None:
    """把单调绝对期限转换为当前 SQLite 锁等待的剩余毫秒预算。"""

    # 非有限 deadline 不能安全换算为锁等待预算，统一按当前阶段依赖失败关闭。
    if not math.isfinite(deadline):
        raise ApiError(error_code)
    remaining = deadline - time.monotonic()
    # 已耗尽的预算不得再进入 SQLite 等锁，避免越过端点治理期限后继续启动写入。
    if remaining <= 0:
        raise ApiError(error_code)
    # busy_timeout 接受整数毫秒；向下取整可确保一次锁等待不越过剩余预算。
    remaining_ms = max(1, int(remaining * 1_000))
    if not _is_postgresql(connection):
        connection.execute(f"PRAGMA busy_timeout={remaining_ms}")


def safe_request_log_error_code(value: object) -> str:
    """只允许共享冻结错误目录中的 code 离开 RequestLog 存储边界。"""

    # 只有共享冻结目录中的机器码可以进入回答，任意存量自由文本均折叠为安全桶。
    if type(value) is str and value in _PUBLIC_ERROR_CODES:
        return value
    return _SAFE_ERROR_CODE_FALLBACK


def _read_busy_timeout(connection: sqlite3.Connection, *, error_code: str) -> int:
    """读取长期连接原锁预算；无法证明旧值时禁止覆盖连接级状态。"""

    if _is_postgresql(connection):
        # PostgreSQL uses transaction-local statement/lock timeouts from the
        # enclosing absolute deadline, with no session setting to restore.
        return 0
    try:
        row = connection.execute("PRAGMA busy_timeout").fetchone()
        if row is None:
            # 驱动未返回当前锁预算时无法保证最终恢复原值，禁止修改该长期连接。
            raise sqlite3.OperationalError("busy timeout unavailable")
        value = int(row[0])
        if value < 0:
            # 负锁预算不是合法 SQLite 连接状态，不能被静默钳制成新配置。
            raise sqlite3.OperationalError("busy timeout invalid")
        # 读取或强类型转换失败都隐藏驱动细节，并按当前调用阶段映射固定错误。
        return value
    except (sqlite3.Error, TypeError, ValueError, OverflowError) as exc:
        # 调用方只需知道连接状态不可证明，不能获得 PRAGMA 或底层异常正文。
        raise ApiError(error_code) from exc


def _restore_connection_state(
    connection: sqlite3.Connection,
    *,
    previous_busy_timeout: int,
    restore_query_only: bool,
) -> sqlite3.Error | None:
    """独立尽力清理事务、handler 与连接 PRAGMA，并返回最早固定化失败。"""

    first_error: sqlite3.Error | None = None

    def attempt(operation: Callable[[], object]) -> None:
        """记录最早 SQLite 清理错误，但继续恢复其余独立连接状态。"""

        nonlocal first_error
        try:
            operation()
        # 每项清理彼此独立；记录首错后继续恢复剩余事务、handler 与 PRAGMA。
        except sqlite3.Error as exc:
            # 最早失败决定连接是否可复用，后续错误不得覆盖原始清理坐标。
            if first_error is None:
                first_error = exc

    postgresql = _is_postgresql(connection)
    if not postgresql:
        attempt(lambda: connection.set_progress_handler(None, 0))
    try:
        in_transaction = connection.in_transaction
    # 连事务状态都无法读取时按不可信处理，但仍尝试恢复其它连接级状态。
    except sqlite3.Error as exc:
        in_transaction = False
        first_error = first_error or exc
    if in_transaction:
        attempt(connection.rollback)
    if restore_query_only and not postgresql:
        attempt(lambda: connection.execute("PRAGMA query_only=OFF"))
    # busy_timeout 是连接级状态，必须在成功、业务异常和 SQLite 异常三条路径都恢复。
    if not postgresql:
        attempt(lambda: connection.execute(f"PRAGMA busy_timeout={previous_busy_timeout}"))
    return first_error


def _discard_untrusted_connection(
    connection: sqlite3.Connection,
    *,
    owns_connection: bool,
    invalidate_connection: Callable[[sqlite3.Connection], None] | None,
) -> None:
    """关闭无法证明状态完整的句柄，并通知长期 runtime 按身份摘除其引用。"""

    if not owns_connection and invalidate_connection is not None:
        try:
            invalidate_connection(connection)
            return
        # callback 属于内部 owner 边界；异常不能覆盖当前阶段的固定领域错误。
        except Exception:
            pass
    try:
        connection.close()
    # close 失败不能把数据库路径或驱动正文覆盖到当前阶段的固定领域错误。
    except sqlite3.Error:
        # 即使底层 close 失败，调用方也只能收到固定领域错误；runtime callback 会另行清引用。
        return


def _persist_duration_ms(started_monotonic: float) -> int:
    """在写锁取得后计算端到端服务耗时，并服从数据库 30 秒整数边界。"""

    # 非有限起点会污染不可变审计耗时，必须在落库前使用稳定错误拒绝。
    if not math.isfinite(started_monotonic):
        raise ApiError("admin_assistant.audit_unavailable")
    return max(0, min(30_000, int((time.monotonic() - started_monotonic) * 1_000)))


def _connect(config: AppConfig, error_code: str) -> sqlite3.Connection:
    """连接部署数据库，并按调用阶段映射为来源或审计固定错误。"""

    try:
        return connect_database(config.database_url)
    # 数据库 URL、驱动和本机路径不得越过领域错误边界。
    except Exception as exc:
        raise ApiError(error_code) from exc


def _db_time(value: datetime) -> str:
    """把带时区时间规范化为专用表、AuditLog 与 Webhook 共用的 UTC 秒。"""

    # naive 时间无法证明 owner 图的同一时刻，禁止按机器本地时区容错解释。
    if value.tzinfo is None:
        raise ApiError("admin_assistant.audit_unavailable")
    # 通用 AuditLog 与全部 Webhook source 使用 canonical UTC 秒；专用 Query/Audit 必须
    # 与它们逐字相等，不能用毫秒精度制造无法通过 owner trigger 的同一时刻。
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json_array(values: tuple[str, ...]) -> str:
    """编码排序去重的 ASCII JSON 数组，供数据库 trigger 执行逐字 exact 校验。"""

    # 重复或乱序会让同一权限图产生多个字节表示，必须在写入前失败关闭。
    if tuple(sorted(set(values))) != values:
        raise ApiError("admin_assistant.audit_unavailable")
    return json.dumps(list(values), ensure_ascii=True, separators=(",", ":"))


__all__ = [
    "ANSWER_SCHEMA_VERSION",
    "EXECUTION_MODE",
    "MODEL_COST_STATUS",
    "PersistedAttempt",
    "QUERY_PERMISSIONS",
    "SourceSnapshot",
    "dispatch_generic_audit",
    "persist_attempt",
    "read_source_snapshot",
    "required_permissions",
    "safe_request_log_error_code",
]
