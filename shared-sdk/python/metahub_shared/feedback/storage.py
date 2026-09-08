"""PRD-049 调用反馈的 SQLite 事务、幂等恢复和只读汇总。

写路径使用独立连接、250ms 写锁预算和 tenant-qualified 查询；Feedback、派生聚合与持久
Event 在同一事务提交。读路径在单连接 query-only 快照中完成全部报表分段，禁止跨租户
扫描或用 all-time 聚合冒充时间窗口结果。
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.db.connection import connect_database, database_operation_deadline
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.errors import ApiError

from .contracts import (
    FEEDBACK_TAGS,
    FeedbackActor,
    FeedbackCreateResult,
    FeedbackEventRecord,
    FeedbackIntent,
    FeedbackModelSummary,
    FeedbackRecord,
    FeedbackSummaryResult,
    FeedbackSummaryTotals,
    FeedbackTagCount,
)
from .eligibility import evaluate_feedback_eligibility
from .validation import (
    feedback_request_fingerprint,
    normalize_feedback_payload,
    normalize_safe_identifier,
    rating_distribution,
    round_ratio_half_up,
)

# SQLite 锁/查询预算、分页上限和时间范围是双服务共享资源边界，调用方不能自行放宽。
FEEDBACK_DATABASE_DEADLINE_SECONDS = 0.250
# 首次 250ms 写锁竞争失败后，只额外观察约一秒；该窗口只用于读取并发胜者的提交结果，
# 不能重新尝试写入，否则会扩大 Gateway 与 Control 对共享 SQLite 的写锁占用。
FEEDBACK_CONCURRENCY_RECOVERY_SECONDS = 1.0
FEEDBACK_CONCURRENCY_POLL_SECONDS = 0.025
FEEDBACK_SUMMARY_MAX_DAYS = 366
FEEDBACK_SUMMARY_MAX_LIMIT = 100
FEEDBACK_SUMMARY_MAX_OFFSET = 10_000
FeedbackAuthorization = Callable[[sqlite3.Connection, bool], None]


def create_request_feedback(
    database_url: str | None,
    *,
    actor: FeedbackActor,
    operation_request_id: str,
    trace_id: str,
    intent: FeedbackIntent,
    now: datetime | None = None,
    authorize: FeedbackAuthorization | None = None,
) -> FeedbackCreateResult:
    """原子创建反馈、派生聚合和持久事件，或恢复同 actor 的幂等结果。

    参数：可信认证 actor、操作 request_id/trace 和 shared 严格解析后的反馈意图。
    返回：客户端安全反馈、首次持久事件和是否重放。
    约束：跨租户/跨 Key 源调用统一 404；未知约束故障不能伪装为业务冲突。
    """

    _validate_actor(actor)
    operation_id = normalize_safe_identifier(
        operation_request_id, error_code="feedback.input_invalid"
    )
    normalized_trace_id = normalize_safe_identifier(trace_id, error_code="feedback.input_invalid")
    normalized_intent = _validate_intent(intent)
    created_at = _normalize_now(now)
    connection = _connect(database_url, error_code="feedback.unavailable")
    connection_open = True
    try:
        # 反馈不能长时间争用 Gateway 的 RequestLog/Usage 写事务，锁等待固定为 250ms。
        postgres = getattr(connection, "backend", "sqlite") == "postgresql"
        if not postgres:
            connection.execute("PRAGMA busy_timeout=250")
        connection.execute("BEGIN IMMEDIATE")
        if postgres:
            connection.execute("SET LOCAL lock_timeout='250ms'")
        if authorize is not None:
            authorize(connection, False)
        recovered = _load_by_operation(
            connection,
            actor=actor,
            operation_request_id=operation_id,
        )
        if recovered is not None:
            if recovered.feedback.request_feedback_id and (
                _load_request_fingerprint(
                    connection,
                    tenant_id=actor.tenant_id,
                    request_feedback_id=recovered.feedback.request_feedback_id,
                )
                != normalized_intent.request_fingerprint
            ):
                # 相同 actor/操作 ID 已绑定另一规范化意图，不能覆盖首次不可变事实。
                raise ApiError("feedback.request_conflict")
            if authorize is not None:
                authorize(connection, False)
            connection.rollback()
            return FeedbackCreateResult(
                feedback=recovered.feedback,
                event=recovered.event,
                idempotent_replay=True,
            )

        source = _load_source_request(connection, actor, normalized_intent.source_request_id)
        if source is None:
            # 查询同时绑定 tenant 和 Virtual Key actor，跨租户/跨 Key 与随机 ID 保持同形 404。
            raise ApiError("feedback.request_not_found")
        eligibility = evaluate_feedback_eligibility(
            request_status=str(source["request_status"]),
            stream_session_id=_optional_text(source["stream_session_id"]),
            stream_status=_optional_text(source["stream_status"]),
            routing_attempt_count=_strict_int(source["routing_attempt_count"]),
            final_routing_attempt_id=_optional_text(source["final_routing_attempt_id"]),
            request_model_id=_optional_text(source["request_model_id"]),
            request_provider_id=_optional_text(source["request_provider_id"]),
            final_attempt_id=_optional_text(source["final_attempt_id"]),
            final_attempt_status=_optional_text(source["final_attempt_status"]),
            final_attempt_model_id=_optional_text(source["final_attempt_model_id"]),
            final_attempt_provider_id=_optional_text(source["final_attempt_provider_id"]),
            has_feedback=bool(source["has_feedback"]),
        )
        if not eligibility.eligible:
            if eligibility.reason == "already_recorded":
                # 同源调用只能有一条反馈；不同操作 ID 不属于幂等恢复。
                raise ApiError("feedback.already_exists")
            # 已授权但请求失败、流未完成或归因不完整统一返回可诊断 409。
            raise ApiError("feedback.request_not_feedbackable")
        if eligibility.model_id is None or eligibility.provider_id is None:
            # 状态机宣称 eligible 却没有归因属于内部一致性故障，禁止写入空快照。
            raise ApiError("feedback.record_failed")

        feedback_id = f"rfb_{uuid4().hex}"
        event_id = f"fbe_{uuid4().hex}"
        tags_json = _canonical_json(list(normalized_intent.tags))
        payload = _event_payload(
            request_feedback_id=feedback_id,
            source_request_id=normalized_intent.source_request_id,
            model_id=eligibility.model_id,
            actor_type=actor.actor_type,
            rating=normalized_intent.rating,
            accepted=normalized_intent.accepted,
            tags=normalized_intent.tags,
        )
        # Feedback 先插入会触发同事务聚合；循环 FK 为 deferred，随后必须补齐 Event 才能提交。
        connection.execute(
            """
            INSERT INTO request_feedbacks(
              request_feedback_id, tenant_id, source_request_id, model_id, provider_id,
              virtual_key_id, project_id, application_id, environment_id,
              rating, accepted, tags_json, actor_type, actor_user_id,
              actor_virtual_key_id, operation_request_id, request_fingerprint,
              feedback_event_id, trace_id, status, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'recorded', ?, ?)
            """,
            (
                feedback_id,
                actor.tenant_id,
                normalized_intent.source_request_id,
                eligibility.model_id,
                eligibility.provider_id,
                source["virtual_key_id"],
                source["project_id"],
                source["application_id"],
                source["environment_id"],
                normalized_intent.rating,
                int(normalized_intent.accepted),
                tags_json,
                actor.actor_type,
                actor.actor_user_id,
                actor.actor_virtual_key_id,
                operation_id,
                normalized_intent.request_fingerprint,
                event_id,
                normalized_trace_id,
                created_at,
                created_at,
            ),
        )
        # Event payload 只含精确七字段；actor ID 仅存在事件行/信封，不进入业务 payload。
        connection.execute(
            """
            INSERT INTO request_feedback_events(
              feedback_event_id, tenant_id, request_feedback_id, source_request_id,
              event_type, actor_type, actor_id, operation_request_id, trace_id,
              payload_json, created_at
            )
            VALUES(?, ?, ?, ?, 'feedback.created', ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                actor.tenant_id,
                feedback_id,
                normalized_intent.source_request_id,
                actor.actor_type,
                actor.actor_id,
                operation_id,
                normalized_trace_id,
                _canonical_json(payload),
                created_at,
            ),
        )
        result = _load_by_feedback_id(
            connection,
            tenant_id=actor.tenant_id,
            request_feedback_id=feedback_id,
        )
        if result is None:
            # 提交前必须重读 Feedback/Event；任一缺失都整体回滚，不能留下半事实。
            raise ApiError("feedback.record_failed")
        _verify_aggregate(
            connection,
            tenant_id=actor.tenant_id,
            feedback=result.feedback,
            intent=normalized_intent,
        )
        if authorize is not None:
            authorize(connection, False)
        connection.commit()
        return FeedbackCreateResult(
            feedback=result.feedback,
            event=result.event,
            idempotent_replay=False,
        )
    # IntegrityError 可能来自并发唯一键或 graph trigger，必须先关闭旧连接再安全重分类。
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        connection.close()
        connection_open = False
        # 并发唯一冲突必须在新连接中按 operation、source、未知故障的固定顺序重分类。
        return _classify_integrity_error(
            database_url,
            actor=actor,
            operation_request_id=operation_id,
            intent=normalized_intent,
            cause=exc,
            authorize=authorize,
        )
    # 已知业务错误保持 404/409/422 语义，但显式事务必须在传播前撤销。
    except ApiError:
        # 领域错误也必须先撤销显式写事务，后续请求不能意外提交中间 Feedback/Aggregate。
        if authorize is not None and connection.in_transaction:
            try:
                authorize(connection, False)
            finally:
                connection.rollback()
        connection.rollback()
        raise
    except sqlite3.OperationalError as exc:
        connection.rollback()
        # 只有 SQLite 原生锁错误才能读取并发胜者；其他运行错误不得被猜测成业务幂等结果。
        if _is_sqlite_lock_error(exc):
            # BEGIN/写入遇到 BUSY 或 LOCKED 时，旧连接可能仍持有失败事务的锁状态和快照；必须
            # 先关闭它，再由独立只读连接等待并发胜者提交，禁止在原连接上盲目重试写入。
            connection.close()
            connection_open = False
            return _recover_after_write_lock(
                database_url,
                actor=actor,
                operation_request_id=operation_id,
                intent=normalized_intent,
                cause=exc,
                authorize=authorize,
            )
        # 缺少迁移、I/O 等其他 OperationalError 仍属于暂不可用，不猜测为并发业务冲突。
        raise ApiError("feedback.unavailable") from exc
    except (sqlite3.DatabaseError, TypeError, ValueError, ArithmeticError) as exc:
        connection.rollback()
        # 未知约束、损坏行或提交前一致性错误使用 500，不能伪装成 already_exists。
        raise ApiError("feedback.record_failed") from exc
    finally:
        if connection_open:
            connection.close()


def load_request_feedback_from_connection(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    source_request_id: str,
) -> FeedbackRecord | None:
    """使用调用方连接按 tenant + 源调用读取反馈。

    参数：处于调用方事务中的 SQLite 连接、可信 tenant 和源 request ID。返回：安全反馈或
    ``None``。关键约束：本函数绝不创建、提交、回滚或关闭连接，使 RequestLog 等聚合读取
    能维持同一快照；跨租户条件仍不会命中。
    """

    normalized_tenant = normalize_safe_identifier(tenant_id, error_code="feedback.input_invalid")
    normalized_source = normalize_safe_identifier(
        source_request_id, error_code="feedback.input_invalid"
    )
    try:
        row = connection.execute(
            """
            SELECT * FROM request_feedbacks
            WHERE tenant_id=? AND source_request_id=?
            """,
            (normalized_tenant, normalized_source),
        ).fetchone()
        return _feedback_from_row(row) if row is not None else None
    # 迁移未应用、损坏行或数据库读取失败统一为依赖不可用，不能泄露路径或 SQL。
    except (sqlite3.Error, TypeError, ValueError) as exc:
        raise ApiError("feedback.unavailable") from exc


def get_request_feedback(
    database_url: str | None,
    *,
    tenant_id: str,
    source_request_id: str,
) -> FeedbackRecord | None:
    """按 tenant + 源调用读取反馈，并在所有成功/失败路径关闭自建连接。"""

    connection = _connect(database_url, error_code="feedback.unavailable")
    try:
        return load_request_feedback_from_connection(
            connection,
            tenant_id=tenant_id,
            source_request_id=source_request_id,
        )
    finally:
        connection.close()


def query_feedback_summary(
    database_url: str | None,
    *,
    tenant_id: str,
    from_time: datetime,
    to_time: datetime,
    model_id: str | None = None,
    limit: int = 20,
    offset: int = 0,
    deadline_seconds: float = FEEDBACK_DATABASE_DEADLINE_SECONDS,
    monotonic_deadline: float | None = None,
    authorize: FeedbackAuthorization | None = None,
) -> FeedbackSummaryResult:
    """在一个 query-only SQLite 快照内计算窗口总计、模型页和标签计数。

    全部 SQL 先绑定认证 tenant 和闭开时间窗口。单独调用时由 ``deadline_seconds`` 建立
    函数预算；HTTP endpoint 必须传认证后冻结的 ``monotonic_deadline``，使查询解析、
    资源排队和线程调度也消耗同一份 250ms。平均分与采纳率在 Python 任意精度整数中
    half-up，避免 SQLite 溢出/浮点漂移。
    """

    normalized_tenant = normalize_safe_identifier(tenant_id, error_code="feedback.query_invalid")
    normalized_model = (
        normalize_safe_identifier(model_id, error_code="feedback.query_invalid")
        if model_id is not None
        else None
    )
    normalized_from, normalized_to = _validate_summary_window(from_time, to_time)
    if (
        type(limit) is not int
        or type(offset) is not int
        or not 1 <= limit <= FEEDBACK_SUMMARY_MAX_LIMIT
        or not 0 <= offset <= FEEDBACK_SUMMARY_MAX_OFFSET
        or isinstance(deadline_seconds, bool)
        or not isinstance(deadline_seconds, (int, float))
        or deadline_seconds <= 0
        or not math.isfinite(float(deadline_seconds))
        or (
            monotonic_deadline is not None
            and (
                isinstance(monotonic_deadline, bool)
                or not isinstance(monotonic_deadline, (int, float))
                or not math.isfinite(float(monotonic_deadline))
            )
        )
    ):
        # 分页和资源预算无效属于 query 合同错误，不应开启数据库连接。
        raise ApiError("feedback.query_invalid")

    deadline = (
        float(monotonic_deadline)
        if monotonic_deadline is not None
        else time.monotonic() + float(deadline_seconds)
    )
    # 绝对 deadline 已在认证后的 endpoint 消耗完时，禁止打开连接或偷偷刷新查询预算。
    _ensure_summary_deadline(deadline)
    with database_operation_deadline(deadline):
        return _query_feedback_summary_snapshot(
            database_url, normalized_tenant=normalized_tenant, normalized_model=normalized_model,
            normalized_from=normalized_from, normalized_to=normalized_to,
            limit=limit, offset=offset, deadline=deadline, authorize=authorize,
        )


def _query_feedback_summary_snapshot(
    database_url, *, normalized_tenant, normalized_model, normalized_from, normalized_to,
    limit, offset, deadline, authorize,
):
    connection = _connect(database_url, error_code="feedback.unavailable")
    postgres = getattr(connection, "backend", "sqlite") == "postgresql"
    try:
        # 文件连接或线程调度也可能耗时；在安装 SQLite 门限前再次扣减剩余预算。
        _ensure_summary_deadline(deadline)
        remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
        if not postgres:
            connection.execute(f"PRAGMA busy_timeout={remaining_ms}")
            connection.execute("PRAGMA query_only=ON")
            connection.set_progress_handler(
                lambda: 1 if time.monotonic() >= deadline else 0,
                1_000,
            )
        connection.execute("BEGIN")
        if postgres:
            connection.execute("SET TRANSACTION READ ONLY")
        if authorize is not None:
            authorize(connection, True)
        _ensure_summary_deadline(deadline)
        where, params = _summary_scope(
            tenant_id=normalized_tenant,
            from_time=_to_db_time(normalized_from),
            to_time=_to_db_time(normalized_to),
            model_id=normalized_model,
        )
        # JOIN 查询必须把同一固定 scope 显式限定到 Feedback 表，避免列名歧义且不引入动态标识符。
        qualified_where = (
            where.replace("tenant_id", "feedback.tenant_id")
            .replace("created_at", "feedback.created_at")
            .replace("model_id", "feedback.model_id")
        )
        total_row = connection.execute(
            f"""
            SELECT
              COUNT(*) AS feedback_count,
              COALESCE(SUM(rating), 0) AS rating_sum,
              COALESCE(SUM(accepted), 0) AS accepted_count,
              COALESCE(SUM(CASE WHEN rating=1 THEN 1 ELSE 0 END), 0) AS rating_1_count,
              COALESCE(SUM(CASE WHEN rating=2 THEN 1 ELSE 0 END), 0) AS rating_2_count,
              COALESCE(SUM(CASE WHEN rating=3 THEN 1 ELSE 0 END), 0) AS rating_3_count,
              COALESCE(SUM(CASE WHEN rating=4 THEN 1 ELSE 0 END), 0) AS rating_4_count,
              COALESCE(SUM(CASE WHEN rating=5 THEN 1 ELSE 0 END), 0) AS rating_5_count,
              COUNT(DISTINCT model_id) AS model_total
            FROM request_feedbacks
            {where}
            """,
            params,
        ).fetchone()
        _ensure_summary_deadline(deadline)
        model_rows = connection.execute(
            f"""
            SELECT
              feedback.model_id,
              model.model_alias AS internal_model_id,
              COALESCE(model.display_name, model.model_alias) AS display_name,
              COUNT(*) AS feedback_count,
              SUM(feedback.rating) AS rating_sum,
              SUM(feedback.accepted) AS accepted_count,
              SUM(CASE WHEN feedback.rating=1 THEN 1 ELSE 0 END) AS rating_1_count,
              SUM(CASE WHEN feedback.rating=2 THEN 1 ELSE 0 END) AS rating_2_count,
              SUM(CASE WHEN feedback.rating=3 THEN 1 ELSE 0 END) AS rating_3_count,
              SUM(CASE WHEN feedback.rating=4 THEN 1 ELSE 0 END) AS rating_4_count,
              SUM(CASE WHEN feedback.rating=5 THEN 1 ELSE 0 END) AS rating_5_count
            FROM request_feedbacks AS feedback
            JOIN models AS model
              ON model.tenant_id=feedback.tenant_id AND model.model_id=feedback.model_id
            {qualified_where}
            GROUP BY feedback.model_id, model.model_alias, model.display_name
            ORDER BY feedback_count DESC, feedback.model_id ASC
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
        _ensure_summary_deadline(deadline)
        tag_source = (
            "CROSS JOIN LATERAL jsonb_array_elements_text(feedback.tags_json::jsonb) AS tag(value)"
            if postgres else "JOIN json_each(feedback.tags_json) AS tag"
        )
        tag_rows = connection.execute(
            f"""
            SELECT tag.value AS tag, COUNT(*) AS tag_count
            FROM request_feedbacks AS feedback
            {tag_source}
            {qualified_where}
            GROUP BY tag.value
            """,
            params,
        ).fetchall()
        _ensure_summary_deadline(deadline)
        connection.rollback()
        if postgres:
            total_row = _postgres_summary_aggregates(total_row)
            model_rows = [_postgres_summary_aggregates(row) for row in model_rows]
        result = _summary_result(
            total_row=total_row,
            model_rows=model_rows,
            tag_rows=tag_rows,
            from_time=normalized_from,
            to_time=normalized_to,
            limit=limit,
            offset=offset,
        )
        # 任意精度 half-up 与 DTO 前置整理同属报表计算，不能在 SQL 完成后越过 deadline。
        _ensure_summary_deadline(deadline)
        if authorize is not None:
            connection.execute("BEGIN")
            if postgres:
                connection.execute("SET TRANSACTION READ ONLY")
            authorize(connection, True)
            connection.rollback()
            _ensure_summary_deadline(deadline)
        return result
    # query_invalid 等共享领域错误不得被宽泛数据库异常处理改写成 unavailable。
    except ApiError:
        # 查询业务错误保留稳定 code，finally 仍负责清理 snapshot 和 progress handler。
        raise
    except (sqlite3.Error, TypeError, ValueError, ArithmeticError) as exc:
        # 超时会表现为 interrupted OperationalError，也统一映射为 feedback.unavailable。
        raise ApiError("feedback.unavailable") from exc
    finally:
        try:
            connection.rollback()
        finally:
            if not postgres:
                connection.set_progress_handler(None, 0)
            connection.close()


def _postgres_summary_aggregates(row):
    # SUM(bigint) is numeric in PostgreSQL; the shared driver retains exact decimal text.
    result = dict(row)
    for name in ("rating_sum", "accepted_count"):
        value = result[name]
        if type(value) is str and value.isascii() and value.isdecimal():
            result[name] = int(value)
    return result


def _load_source_request(
    connection: sqlite3.Connection,
    actor: FeedbackActor,
    source_request_id: str,
) -> sqlite3.Row | None:
    """按可信租户/Key 查询源 RequestLog，并同查询读取 final attempt 与已有反馈。"""

    key_clause = ""
    params: list[object] = [actor.tenant_id, source_request_id]
    if actor.actor_type == "virtual_key":
        # Virtual Key 只能评价自己产生的调用；NULL 或另一 Key 与随机 ID 均保持未找到。
        key_clause = " AND request.virtual_key_id=?"
        params.append(actor.actor_virtual_key_id)
    return connection.execute(
        f"""
        SELECT
          request.status AS request_status,
          request.stream_session_id,
          request.stream_status,
          request.routing_attempt_count,
          request.final_routing_attempt_id,
          request.model_id AS request_model_id,
          request.provider_id AS request_provider_id,
          request.virtual_key_id,
          request.project_id,
          request.application_id,
          request.environment_id,
          attempt.routing_attempt_id AS final_attempt_id,
          attempt.status AS final_attempt_status,
          attempt.model_id AS final_attempt_model_id,
          attempt.provider_id AS final_attempt_provider_id,
          EXISTS(
            SELECT 1 FROM request_feedbacks feedback
            WHERE feedback.tenant_id=request.tenant_id
              AND feedback.source_request_id=request.request_id
          ) AS has_feedback
        FROM request_logs AS request
        LEFT JOIN routing_attempts AS attempt
          ON attempt.tenant_id=request.tenant_id
         AND attempt.request_id=request.request_id
         AND attempt.routing_attempt_id=request.final_routing_attempt_id
        WHERE request.tenant_id=? AND request.request_id=?{key_clause}
        """,
        params,
    ).fetchone()


def _load_by_operation(
    connection: sqlite3.Connection,
    *,
    actor: FeedbackActor,
    operation_request_id: str,
) -> FeedbackCreateResult | None:
    """按 tenant、exact actor 与操作 ID 恢复首次不可变 Feedback/Event。

    Session user 和 Virtual Key 使用不同 partial unique scope；本函数只选择由可信 actor 类型
    决定的固定列名，客户端正文不能控制 SQL 标识符或跨 actor 查找幂等事实。
    """

    actor_clause, actor_id = _actor_query_scope(actor)
    row = connection.execute(
        f"""
        SELECT feedback.request_feedback_id
        FROM request_feedbacks AS feedback
        WHERE feedback.tenant_id=? AND feedback.actor_type=?
          AND {actor_clause}=? AND feedback.operation_request_id=?
        """,
        (actor.tenant_id, actor.actor_type, actor_id, operation_request_id),
    ).fetchone()
    if row is None:
        return None
    return _load_by_feedback_id(
        connection,
        tenant_id=actor.tenant_id,
        request_feedback_id=str(row["request_feedback_id"]),
    )


def _load_by_feedback_id(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    request_feedback_id: str,
) -> FeedbackCreateResult | None:
    """联表恢复一对一 Feedback/Event，并通过 DTO 解析复核持久类型。"""

    row = connection.execute(
        """
        SELECT
          feedback.*,
          event.feedback_event_id AS event_feedback_event_id,
          event.event_type AS event_type,
          event.actor_type AS event_actor_type,
          event.actor_id AS event_actor_id,
          event.operation_request_id AS event_operation_request_id,
          event.trace_id AS event_trace_id,
          event.payload_json AS event_payload_json,
          event.created_at AS event_created_at
        FROM request_feedbacks AS feedback
        JOIN request_feedback_events AS event
          ON event.tenant_id=feedback.tenant_id
         AND event.request_feedback_id=feedback.request_feedback_id
         AND event.feedback_event_id=feedback.feedback_event_id
        WHERE feedback.tenant_id=? AND feedback.request_feedback_id=?
        """,
        (tenant_id, request_feedback_id),
    ).fetchone()
    if row is None:
        return None
    feedback = _feedback_from_row(row)
    event = _event_from_row(row)
    return FeedbackCreateResult(feedback=feedback, event=event, idempotent_replay=False)


def _feedback_from_row(row: sqlite3.Row) -> FeedbackRecord:
    """把持久反馈恢复为不含租户、actor ID 和 Provider 的客户端安全 DTO。"""

    intent = normalize_feedback_payload(
        {
            "request_id": row["source_request_id"],
            "rating": row["rating"],
            "accepted": bool(row["accepted"]),
            "tags": json.loads(str(row["tags_json"])),
        }
    )
    if type(row["accepted"]) is not int or row["accepted"] not in (0, 1):
        # SQLite 行类型损坏时不能用 bool() 把任意值折叠为采纳事实。
        raise ValueError("feedback accepted fact invalid")
    if str(row["request_fingerprint"]) != intent.request_fingerprint:
        # 指纹必须与规范化意图一致，损坏行不能用于幂等恢复或 API 响应。
        raise ValueError("feedback fingerprint mismatch")
    actor_type = str(row["actor_type"])
    if actor_type not in {"session_user", "virtual_key"} or row["status"] != "recorded":
        # actor/status 漂移会破坏响应和事件合同，必须失败关闭。
        raise ValueError("feedback actor or status invalid")
    return FeedbackRecord(
        request_feedback_id=str(row["request_feedback_id"]),
        source_request_id=intent.source_request_id,
        model_id=str(row["model_id"]),
        rating=intent.rating,
        accepted=intent.accepted,
        tags=intent.tags,
        actor_type=actor_type,  # type: ignore[arg-type]  # 上方已精确校验白名单。
        status="recorded",
        created_at=_parse_db_time(row["created_at"]),
    )


def _event_from_row(row: sqlite3.Row) -> FeedbackEventRecord:
    """恢复提交后重派所需的持久事件，并拒绝 payload 类型漂移。"""

    payload = json.loads(str(row["event_payload_json"]))
    if type(payload) is not dict:
        # Webhook 事件必须是 JSON object，标量/数组不能进入签名与投递。
        raise ValueError("feedback event payload invalid")
    actor_type = str(row["event_actor_type"])
    if actor_type not in {"session_user", "virtual_key"}:
        # 未知 actor 类型不能构造事件信封，避免接收端错误解释身份。
        raise ValueError("feedback event actor invalid")
    return FeedbackEventRecord(
        feedback_event_id=str(row["event_feedback_event_id"]),
        tenant_id=str(row["tenant_id"]),
        request_feedback_id=str(row["request_feedback_id"]),
        source_request_id=str(row["source_request_id"]),
        event_type="feedback.created",
        actor_type=actor_type,  # type: ignore[arg-type]  # 上方已精确校验白名单。
        actor_id=str(row["event_actor_id"]),
        operation_request_id=str(row["event_operation_request_id"]),
        trace_id=str(row["event_trace_id"]),
        payload={str(key): value for key, value in payload.items()},
        created_at=_parse_db_time(row["event_created_at"]),
    )


def _classify_integrity_error(
    database_url: str | None,
    *,
    actor: FeedbackActor,
    operation_request_id: str,
    intent: FeedbackIntent,
    cause: sqlite3.IntegrityError,
    authorize: FeedbackAuthorization | None = None,
) -> FeedbackCreateResult:
    """回滚关闭后用新连接按固定优先级重分类并发唯一冲突。"""

    connection = _connect(database_url, error_code="feedback.record_failed")
    try:
        classified = _classify_committed_feedback(
            connection,
            actor=actor,
            operation_request_id=operation_request_id,
            intent=intent,
            cause=cause,
            authorize=authorize,
        )
        if classified is not None:
            return classified
        # PK 碰撞、graph/payload/aggregate trigger 或未知约束均不能伪装成业务冲突。
        raise ApiError("feedback.record_failed") from cause
    except ApiError:
        raise
    # 重分类读取若自身损坏，只能返回 record_failed，不能泄露 SQL 或猜测冲突类型。
    except (sqlite3.Error, TypeError, ValueError) as exc:
        # 重分类自身失败时保持 500 一致性语义，不回显原 IntegrityError/SQL。
        raise ApiError("feedback.record_failed") from exc
    finally:
        connection.close()


def _recover_after_write_lock(
    database_url: str | None,
    *,
    actor: FeedbackActor,
    operation_request_id: str,
    intent: FeedbackIntent,
    cause: sqlite3.OperationalError,
    authorize: FeedbackAuthorization | None = None,
) -> FeedbackCreateResult:
    """在首次写锁预算耗尽后，有界等待并读取并发胜者的已提交事实。

    参数：原数据库、可信 actor、规范 operation/意图和原始锁错误；返回同意图 replay。
    约束：本阶段只读、不续写，按 operation 优先于 source 重分类；约一秒仍没有可见胜者时
    返回 ``feedback.unavailable``，从而区分短暂并发提交与持续外部写锁。
    """

    deadline = time.monotonic() + FEEDBACK_CONCURRENCY_RECOVERY_SECONDS
    connection = _connect(database_url, error_code="feedback.unavailable")
    try:
        try:
            # 单次读等待保持很短，使轮询可在 deadline 内重新取得新提交快照；query_only 从
            # SQLite 层阻止恢复代码未来误引入写 SQL，不能成为第二条写竞争路径。
            if getattr(connection, "backend", "sqlite") != "postgresql":
                connection.execute(
                    f"PRAGMA busy_timeout={int(FEEDBACK_CONCURRENCY_POLL_SECONDS * 1000)}"
                )
                connection.execute("PRAGMA query_only=ON")
        # 只读恢复连接无法初始化时没有可信提交事实，必须保持依赖暂不可用语义。
        except sqlite3.Error as exc:
            # 固定错误码阻止 SQLite 正文、数据库路径或 SQL 片段进入 API 响应。
            raise ApiError("feedback.unavailable") from exc

        while True:
            if time.monotonic() >= deadline:
                # 持续外部锁或没有任何胜者提交时保持 503，不能猜测 replay/conflict。
                raise ApiError("feedback.unavailable") from cause
            try:
                classified = _classify_committed_feedback(
                    connection,
                    actor=actor,
                    operation_request_id=operation_request_id,
                    intent=intent,
                    cause=cause,
                    authorize=authorize,
                )
                if classified is not None:
                    return classified
            # 已提交事实证明的领域冲突不得被后续数据库异常分支改写成笼统的 503。
            except ApiError:
                # request_conflict/already_exists 已由同租户已提交事实证明，原样保留业务语义。
                raise
            except sqlite3.OperationalError as exc:
                if not _is_sqlite_lock_error(exc):
                    # 恢复读取发生非锁数据库错误时不能继续猜测胜者状态。
                    raise ApiError("feedback.unavailable") from exc
                # BUSY/LOCKED 表示胜者或外部连接尚未释放锁；在同一绝对 deadline 内继续观察。
            except sqlite3.Error as exc:
                raise ApiError("feedback.unavailable") from exc
            except (TypeError, ValueError) as exc:
                # 已提交行存在但 DTO/事件图损坏属于一致性失败，不能降级成暂时锁竞争。
                raise ApiError("feedback.record_failed") from exc

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # 绝对恢复预算耗尽后失败关闭，避免一次反馈请求无限占用工作线程。
                raise ApiError("feedback.unavailable") from cause
            time.sleep(min(FEEDBACK_CONCURRENCY_POLL_SECONDS, remaining))
    finally:
        connection.close()


def _classify_committed_feedback(
    connection: sqlite3.Connection,
    *,
    actor: FeedbackActor,
    operation_request_id: str,
    intent: FeedbackIntent,
    cause: sqlite3.Error,
    authorize: FeedbackAuthorization | None = None,
) -> FeedbackCreateResult | None:
    with _authorized_feedback_read(connection, authorize):
        return _classify_committed_feedback_snapshot(
            connection, actor=actor, operation_request_id=operation_request_id,
            intent=intent, cause=cause,
        )


@contextmanager
def _authorized_feedback_read(
    connection: sqlite3.Connection,
    authorize: FeedbackAuthorization | None,
) -> Iterator[None]:
    postgres = getattr(connection, "backend", "sqlite") == "postgresql"
    if authorize is None and not postgres:
        yield
        return
    connection.execute("BEGIN")
    try:
        if postgres:
            connection.execute("SET TRANSACTION READ ONLY")
            connection.execute("SET LOCAL lock_timeout='25ms'")
        if authorize is not None:
            authorize(connection, True)
        try:
            yield
        finally:
            # End the data snapshot before checking revocation, including conflict exits.
            connection.rollback()
            if authorize is not None:
                connection.execute("BEGIN")
                if postgres:
                    connection.execute("SET TRANSACTION READ ONLY")
                    connection.execute("SET LOCAL lock_timeout='25ms'")
                authorize(connection, True)
    finally:
        connection.rollback()


def _classify_committed_feedback_snapshot(
    connection: sqlite3.Connection,
    *,
    actor: FeedbackActor,
    operation_request_id: str,
    intent: FeedbackIntent,
    cause: sqlite3.Error,
) -> FeedbackCreateResult | None:
    """按 operation/source 固定优先级检查一个已经提交的并发胜者。

    返回 ``None`` 表示当前快照尚无胜者；同 operation 同指纹返回 replay，异指纹抛出
    ``feedback.request_conflict``，仅 source 命中抛出 ``feedback.already_exists``。全部查询
    绑定可信 tenant 和 actor，不返回胜者身份或数据库错误正文。
    """

    recovered = _load_by_operation(
        connection,
        actor=actor,
        operation_request_id=operation_request_id,
    )
    if recovered is not None:
        fingerprint = _load_request_fingerprint(
            connection,
            tenant_id=actor.tenant_id,
            request_feedback_id=recovered.feedback.request_feedback_id,
        )
        if fingerprint == intent.request_fingerprint:
            # 并发同意图胜者已提交，输者恢复同一 Feedback/Event 并标记 replay。
            return FeedbackCreateResult(
                feedback=recovered.feedback,
                event=recovered.event,
                idempotent_replay=True,
            )
        # operation 命中优先于 source 唯一，异指纹稳定返回 request_conflict。
        raise ApiError("feedback.request_conflict") from cause

    source_row = connection.execute(
        """
        SELECT 1 FROM request_feedbacks
        WHERE tenant_id=? AND source_request_id=?
        """,
        (actor.tenant_id, intent.source_request_id),
    ).fetchone()
    if source_row is None:
        return None

    # 两次 SELECT 处于 autocommit 快照：胜者可能恰在首次 operation 查询后提交。source 已
    # 命中时复查 operation，才能保证同 operation 永远优先于 already_exists，避免时序误判。
    recovered = _load_by_operation(
        connection,
        actor=actor,
        operation_request_id=operation_request_id,
    )
    if recovered is not None:
        fingerprint = _load_request_fingerprint(
            connection,
            tenant_id=actor.tenant_id,
            request_feedback_id=recovered.feedback.request_feedback_id,
        )
        if fingerprint == intent.request_fingerprint:
            return FeedbackCreateResult(
                feedback=recovered.feedback,
                event=recovered.event,
                idempotent_replay=True,
            )
        # 同一操作标识绑定了不同规范指纹，必须拒绝覆盖首次不可变反馈事实。
        raise ApiError("feedback.request_conflict") from cause
    # 另一 actor/operation 已评价该源调用，只返回 already_exists，不泄露提交者。
    raise ApiError("feedback.already_exists") from cause


def _is_sqlite_lock_error(error: sqlite3.OperationalError) -> bool:
    """只按 SQLite 原生错误码识别 BUSY/LOCKED，不解析可能含路径或 SQL 的错误正文。"""

    if getattr(error, "sqlstate", None) in {"55P03", "40001", "40P01"}:
        return True
    error_code = getattr(error, "sqlite_errorcode", None)
    # 原生码缺失时禁止解析异常文本，以免路径、SQL 或本地化消息进入分类与日志分支。
    if type(error_code) is not int:
        # 当前受支持 Python 会提供原生码；缺失时失败关闭为普通 unavailable，不猜测文本。
        return False
    # 扩展错误码低八位仍是主码，可同时覆盖 BUSY_SNAPSHOT、LOCKED_SHAREDCACHE 等变体。
    return error_code & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}


def _verify_aggregate(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    feedback: FeedbackRecord,
    intent: FeedbackIntent,
) -> None:
    """按可信 tenant 提交前确认当前反馈已被唯一纳入 model 派生聚合。"""

    row = connection.execute(
        """
        SELECT statistic.*,
          (SELECT COUNT(*) FROM request_feedbacks feedback
           WHERE feedback.tenant_id=statistic.tenant_id
             AND feedback.model_id=statistic.model_id) AS source_count
        FROM model_feedback_statistics AS statistic
        JOIN request_feedbacks AS current
          ON current.tenant_id=statistic.tenant_id AND current.model_id=statistic.model_id
        WHERE current.tenant_id=? AND current.request_feedback_id=?
        """,
        (tenant_id, feedback.request_feedback_id),
    ).fetchone()
    if row is None:
        # AFTER INSERT trigger 没有生成聚合时事务必须失败关闭。
        raise ApiError("feedback.record_failed")
    count = _strict_int(row["feedback_count"])
    if (
        count != _strict_int(row["source_count"])
        or _strict_int(row["feedback_statistics_version"]) != count
        or count < 1
        or intent.rating < 1
    ):
        # 版本、计数与原始事实不一致表示聚合 trigger 损坏，不能提交部分统计。
        raise ApiError("feedback.record_failed")


def _summary_scope(
    *, tenant_id: str, from_time: str, to_time: str, model_id: str | None
) -> tuple[str, list[object]]:
    """构造只含固定 SQL 片段的 tenant/time/model 窗口和绑定参数。"""

    where = "WHERE tenant_id=? AND created_at>=? AND created_at<?"
    params: list[object] = [tenant_id, from_time, to_time]
    # 可选 model 仍附加在 tenant/time scope 内，不能仅凭全局 model ID 形成跨租户查询。
    if model_id is not None:
        # model 筛选仍位于认证 tenant 和时间窗口之后，不能单独按全局 ID 查询。
        where += " AND model_id=?"
        params.append(model_id)
    return where, params


def _summary_result(
    *,
    total_row: sqlite3.Row | None,
    model_rows: list[sqlite3.Row],
    tag_rows: list[sqlite3.Row],
    from_time: datetime,
    to_time: datetime,
    limit: int,
    offset: int,
) -> FeedbackSummaryResult:
    """把同一只读快照的三个查询分段组合为任意精度整数 DTO。"""

    if total_row is None:
        # 聚合 SELECT 理论上总会返回一行，缺失表示数据库驱动或 schema 不可用。
        raise ValueError("feedback summary total missing")
    feedback_count = _strict_int(total_row["feedback_count"])
    rating_sum = _strict_int(total_row["rating_sum"])
    accepted_count = _strict_int(total_row["accepted_count"])
    total_distribution = _distribution_from_row(total_row)
    raw_tag_counts = {str(row["tag"]): _strict_int(row["tag_count"]) for row in tag_rows}
    if any(tag not in FEEDBACK_TAGS for tag in raw_tag_counts):
        # 数据库标签 trigger 若被绕过，未知标签不能进入报表或改变固定顺序。
        raise ValueError("feedback summary tag invalid")
    totals = FeedbackSummaryTotals(
        feedback_count=feedback_count,
        rating_sum=rating_sum,
        average_rating_milli=round_ratio_half_up(rating_sum, feedback_count, 1_000),
        accepted_count=accepted_count,
        adoption_rate_bps=round_ratio_half_up(accepted_count, feedback_count, 10_000),
        rating_distribution=total_distribution,
        tag_counts=tuple(
            FeedbackTagCount(tag=tag, count=raw_tag_counts[tag])
            for tag in FEEDBACK_TAGS
            if raw_tag_counts.get(tag, 0) > 0
        ),
    )
    models: list[FeedbackModelSummary] = []
    for row in model_rows:
        count = _strict_int(row["feedback_count"])
        model_rating_sum = _strict_int(row["rating_sum"])
        model_accepted = _strict_int(row["accepted_count"])
        models.append(
            FeedbackModelSummary(
                model_id=str(row["model_id"]),
                internal_model_id=str(row["internal_model_id"]),
                display_name=str(row["display_name"]),
                feedback_count=count,
                average_rating_milli=round_ratio_half_up(model_rating_sum, count, 1_000),
                accepted_count=model_accepted,
                adoption_rate_bps=round_ratio_half_up(model_accepted, count, 10_000),
                rating_distribution=_distribution_from_row(row),
            )
        )
    return FeedbackSummaryResult(
        scope_type="tenant",
        from_time=from_time,
        to_time=to_time,
        summary=totals,
        models=tuple(models),
        total=_strict_int(total_row["model_total"]),
        limit=limit,
        offset=offset,
    )


def _distribution_from_row(row: sqlite3.Row) -> dict[str, int]:
    """从五个显式 SQL 计数列恢复稳定评分分布。"""

    return rating_distribution(
        {rating: _strict_int(row[f"rating_{rating}_count"]) for rating in range(1, 6)}
    )


def _validate_actor(actor: FeedbackActor) -> None:
    """复核可信 actor 的 tenant、安全 ID 与 exact-one 类型关系。"""

    normalize_safe_identifier(actor.tenant_id, error_code="feedback.record_failed")
    # Session 分支精确要求 user actor，不能读取或容忍同时存在的 Key actor。
    if actor.actor_type == "session_user":
        if actor.actor_user_id is None or actor.actor_virtual_key_id is not None:
            # Session actor 只能有 user ID；双 actor 会破坏幂等 scope 和事件归因。
            raise ApiError("feedback.record_failed")
        normalize_safe_identifier(actor.actor_user_id, error_code="feedback.record_failed")
        return
    # Virtual Key 分支精确要求 Key 对象 ID，禁止把明文 Key 或 user ID 当作替代字段。
    if actor.actor_type == "virtual_key":
        if actor.actor_virtual_key_id is None or actor.actor_user_id is not None:
            # Key actor 只能有 Key 对象 ID，绝不接受明文 Token 作为 actor 字段。
            raise ApiError("feedback.record_failed")
        normalize_safe_identifier(actor.actor_virtual_key_id, error_code="feedback.record_failed")
        return
    # 未知 actor 类型不能降级到任一分支，否则可能绕过 Session RBAC 或同 Key 限制。
    raise ApiError("feedback.record_failed")


def _validate_intent(intent: FeedbackIntent) -> FeedbackIntent:
    """重新通过公开规范化器验证内部 DTO，并核对调用方携带的指纹。"""

    normalized = normalize_feedback_payload(
        {
            "request_id": intent.source_request_id,
            "rating": intent.rating,
            "accepted": intent.accepted,
            "tags": list(intent.tags),
        }
    )
    if normalized.request_fingerprint != intent.request_fingerprint:
        # 手工构造或被修改的 DTO 不能绕过 v1 指纹合同进入幂等索引。
        raise ApiError("feedback.input_invalid")
    if feedback_request_fingerprint(normalized) != normalized.request_fingerprint:
        # shared 自身 canonical 结果不一致时失败关闭，避免两个入口形成不同事实。
        raise ApiError("feedback.input_invalid")
    return normalized


def _actor_query_scope(actor: FeedbackActor) -> tuple[str, str]:
    """返回固定 actor 列名和可信对象 ID，禁止客户端字段参与 SQL 标识符。"""

    # 认证 actor 类型只决定两个固定列之一，绝不接受外部字符串作为 SQL 列名。
    if actor.actor_type == "session_user":
        return "feedback.actor_user_id", actor.actor_id
    return "feedback.actor_virtual_key_id", actor.actor_id


def _load_request_fingerprint(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    request_feedback_id: str,
) -> str:
    """按 tenant + Feedback ID 读取幂等指纹，缺失时视为一致性故障。"""

    row = connection.execute(
        """
        SELECT request_fingerprint FROM request_feedbacks
        WHERE tenant_id=? AND request_feedback_id=?
        """,
        (tenant_id, request_feedback_id),
    ).fetchone()
    if row is None:
        # 已恢复 DTO 却缺少原始行表示事务图损坏，不能退化成首次创建。
        raise ValueError("feedback fingerprint row missing")
    return str(row["request_fingerprint"])


def _event_payload(
    *,
    request_feedback_id: str,
    source_request_id: str,
    model_id: str,
    actor_type: str,
    rating: int,
    accepted: bool,
    tags: tuple[str, ...],
) -> dict[str, object]:
    """构造 ``feedback.created`` 允许的精确七字段安全 payload。"""

    return {
        "request_feedback_id": request_feedback_id,
        "request_id": source_request_id,
        "model_id": model_id,
        "actor_type": actor_type,
        "rating": rating,
        "accepted": accepted,
        "tags": list(tags),
    }


def _canonical_json(value: object) -> str:
    """生成无空白、ASCII、安全拒绝 NaN 的持久 JSON 文本。"""

    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=False,
        allow_nan=False,
    )


def _ensure_summary_deadline(deadline: float) -> None:
    """在每个报表阶段边界检查绝对 monotonic deadline，耗尽时安全拒绝。"""

    if time.monotonic() >= deadline:
        # 超时属于暂时资源不可用，不是客户端 query 错误；响应不得泄露执行到哪条 SQL。
        raise ApiError("feedback.unavailable")


def _validate_summary_window(
    from_time: datetime, to_time: datetime
) -> tuple[datetime, datetime]:
    """校验 canonical UTC 秒精度闭开窗口及 366 天上限。"""

    if (
        not isinstance(from_time, datetime)
        or not isinstance(to_time, datetime)
        or from_time.tzinfo is None
        or to_time.tzinfo is None
    ):
        # 无时区时间不能安全映射 SQLite TEXT 顺序，必须由路由先解析 canonical Z。
        raise ApiError("feedback.query_invalid")
    normalized_from = from_time.astimezone(UTC)
    normalized_to = to_time.astimezone(UTC)
    if (
        normalized_from.microsecond != 0
        or normalized_to.microsecond != 0
        or normalized_from >= normalized_to
        or normalized_to - normalized_from > timedelta(days=FEEDBACK_SUMMARY_MAX_DAYS)
    ):
        # 分数秒、空/反向窗口或超 366 天都拒绝，防止文本比较歧义和无界扫描。
        raise ApiError("feedback.query_invalid")
    return normalized_from, normalized_to


def _normalize_now(value: datetime | None) -> str:
    """把服务端创建时刻规范为 UTC 秒精度数据库文本。"""

    resolved = value or datetime.now(UTC)
    if not isinstance(resolved, datetime) or resolved.tzinfo is None:
        # 测试注入也必须带时区，禁止本地时间被误记为 UTC。
        raise ApiError("feedback.record_failed")
    return _to_db_time(resolved.astimezone(UTC).replace(microsecond=0))


def _to_db_time(value: datetime) -> str:
    """序列化为 SQLite 可按字典序比较的 canonical UTC 秒精度文本。"""

    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_db_time(value: object) -> datetime:
    """严格恢复 UTC 数据库时间，损坏值不能进入响应或 Webhook。"""

    if type(value) is not str:
        # 时间字段必须是数据库 TEXT，隐式 str() 会掩盖 schema/驱动类型漂移。
        raise ValueError("feedback timestamp type invalid")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    # 非 canonical Z 时间会破坏窗口边界与事件时间线，整体失败关闭。
    except ValueError as exc:
        raise ValueError("feedback timestamp invalid") from exc
    return parsed


def _strict_int(value: object) -> int:
    """恢复非负 SQLite 整数并拒绝 bool、float、文本或负数。"""

    if type(value) is not int or value < 0:
        # 权威反馈计数/状态不能由隐式 int() 修复，否则会隐藏数据库事实损坏。
        raise ValueError("feedback integer fact invalid")
    return value


def _optional_text(value: object) -> str | None:
    """恢复可空数据库 ID；非空值必须保持原始文本类型。"""

    if value is None:
        return None
    if type(value) is not str:
        # ID 类型漂移时不能用 str() 生成看似合法的归因。
        raise ValueError("feedback optional text invalid")
    return value


def _connect(database_url: str | None, *, error_code: str) -> sqlite3.Connection:
    """建立共享数据库连接，并把路径/驱动细节折叠为 Feedback 领域错误。"""

    try:
        return connect_database(database_url)
    # 数据库 URL 和文件路径可能包含部署信息，不能穿透到 Feedback API 响应。
    except DbError as exc:
        raise ApiError(error_code) from exc
