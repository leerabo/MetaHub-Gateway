"""Prompt Cache 亲和 Selection 的终态 CAS、惰性对账与单节点启动恢复。

本模块只处理不可逆摘要和运行状态，不接收 Prompt、Response、Provider Key 或请求 Header。
客户端结果由既有 attempt / stream 状态机决定；亲和终态写失败只能输出脱敏告警，不能覆盖
已经得到的上游成功响应。启动恢复发生在 Gateway 接流量之前，失败则阻止实例进入 ready。
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI
from metahub_shared.db.connection import connect_database
from metahub_shared.runtime.config import AppConfig

from .models import CacheAffinitySelectionHandle

logger = logging.getLogger("metahub.gateway.cache_affinity")

# 单批上限避免一次请求或一次启动事务长期占用 SQLite 写锁；启动阶段会按稳定顺序循环。
RECONCILE_BATCH_SIZE = 64


def safe_finalize_cache_affinity_selection(
    config: AppConfig,
    handle: CacheAffinitySelectionHandle | None,
    *,
    succeeded: bool,
) -> bool:
    """依据已提交 execution terminal 幂等完成一条 Selection。

    参数：运行配置、Provider 前冻结的 Selection 句柄和调用方期望的成功布尔值。
    返回：数据库已处于相同终态时为 True；无亲和句柄也视为无需处理并返回 True。
    关键约束：函数会重验 tenant、request、target generation 和父状态；任何存储或一致性
    错误都只记录固定 warning 并返回 False，绝不能改变客户端已经确定的成功或失败结果。
    """

    if handle is None:
        return True
    # bool 必须精确校验，禁止 truthy 对象把失败调用写成可用于 warm 的成功证据。
    if type(succeeded) is not bool:
        # 调用方合同错误不能继续读取或修改任何租户内 Selection 状态。
        raise ValueError("cache_affinity_outcome_contract_invalid")
    connection: sqlite3.Connection | None = None
    failure_stage = "open_transaction"
    try:
        connection = connect_database(config.database_url)
        connection.execute("BEGIN IMMEDIATE")
        selection = connection.execute(
            """
            SELECT * FROM cache_affinity_selections
            WHERE tenant_id=? AND cache_affinity_selection_id=?
            """,
            (handle.tenant_id, handle.cache_affinity_selection_id),
        ).fetchone()
        failure_stage = "validate_handle"
        # 句柄每个租户、父门和双 generation 字段都必须匹配，防止串请求或 ABA 终态写入。
        # 不匹配时只能记录安全阶段，不能输出 Binding、Credential 或摘要值。
        if selection is None or not _selection_matches_handle(selection, handle):
            raise sqlite3.IntegrityError("cache_affinity_selection_handle_mismatch")
        failure_stage = "validate_execution_terminal"
        terminal = _execution_terminal(connection, selection)
        expected_outcome = "succeeded" if succeeded else "failed"
        if terminal is None or terminal[0] != expected_outcome:
            # 亲和层不能凭内存布尔值制造成功证据，父 execution gate 必须先提交完全一致终态。
            raise sqlite3.IntegrityError("cache_affinity_execution_terminal_missing")
        failure_stage = "apply_terminal"
        _finalize_selection_row(
            connection,
            selection,
            outcome_status=terminal[0],
            outcome_at=terminal[1],
        )
        failure_stage = "commit"
        connection.commit()
        return True
    except Exception as exc:
        # Provider 终态已经确定后，任何本地编程/存储异常都只能降级亲和证据，不能覆盖客户端结果。
        if connection is not None:
            connection.rollback()
        _log_outcome_failure(handle, exc, failure_stage=failure_stage)
        return False
    finally:
        if connection is not None:
            connection.close()


def reconcile_pending_cache_affinity_selections(
    connection: sqlite3.Connection,
    *,
    tenant_id: str | None = None,
    cache_affinity_scope_id_snapshot: str | None = None,
    limit: int = RECONCILE_BATCH_SIZE,
) -> int:
    """在调用方写事务内对账已经具有父终态的 pending Selection。

    参数：已经执行 ``BEGIN IMMEDIATE`` 的连接、可选的租户/Scope 快照成对范围和不超过
    64 的批量上限。返回实际完成行数。请求路径必须传入租户与五维 Scope，只能收口当前
    Prompt 域；启动恢复同时省略两者才允许扫描全库。仍在执行的 Selection 不阻塞更晚行。
    """

    # 单批上限是 SQLite 写锁边界，bool 或越界值不能被宽松转换成长事务。
    if type(limit) is not int or not 1 <= limit <= RECONCILE_BATCH_SIZE:
        # 非法批量值在执行任何 SQL 前固定拒绝，避免只处理部分租户事实。
        raise ValueError("cache_affinity_reconcile_limit_invalid")
    scoped = tenant_id is not None or cache_affinity_scope_id_snapshot is not None
    if scoped and (
        not isinstance(tenant_id, str)
        or not tenant_id
        or not isinstance(cache_affinity_scope_id_snapshot, str)
        or not cache_affinity_scope_id_snapshot
    ):
        # 两个所有权维度必须成对出现；禁止仅按 Scope 或仅按 tenant 扫描半个隔离域。
        raise ValueError("cache_affinity_reconcile_scope_invalid")
    scope_filter = ""
    parameters: list[object] = []
    if scoped:
        # SQL 片段是内部固定常量，外部 ID 仍全部走占位符，避免把标识拼入语句或异常。
        scope_filter = (
            "AND selection.tenant_id=? "
            "AND selection.cache_affinity_scope_id_snapshot=?"
        )
        parameters.extend((tenant_id, cache_affinity_scope_id_snapshot))
    parameters.append(limit)
    rows = connection.execute(
        f"""
        SELECT selection.*
        FROM cache_affinity_selections selection
        LEFT JOIN routing_attempts attempt
          ON attempt.tenant_id=selection.tenant_id
         AND attempt.routing_attempt_id=selection.routing_attempt_id
        LEFT JOIN stream_sessions session
          ON session.tenant_id=selection.tenant_id
         AND session.stream_session_id=selection.stream_session_id
        WHERE selection.outcome_status='pending'
          {scope_filter}
          AND (
            (selection.routing_attempt_id IS NOT NULL
              AND attempt.status IN ('succeeded', 'failed')
              AND attempt.completed_at IS NOT NULL)
            OR
            (selection.stream_session_id IS NOT NULL
              AND session.status IN (
                'completed', 'timed_out', 'client_disconnected', 'upstream_failed',
                'output_blocked', 'output_safety_unavailable'
              )
              AND session.completed_at IS NOT NULL)
          )
        ORDER BY selection.created_at, selection.cache_affinity_selection_id
        LIMIT ?
        """,
        tuple(parameters),
    ).fetchall()
    processed = 0
    for selection in rows:
        terminal = _execution_terminal(connection, selection)
        # 同一写事务内父终态不可消失；若发生说明状态图损坏，当前批次必须回滚。
        if terminal is None:  # pragma: no cover - 同一写事务内查询结果不会倒退。
            raise sqlite3.IntegrityError("cache_affinity_terminal_disappeared")
        _finalize_selection_row(
            connection,
            selection,
            outcome_status=terminal[0],
            outcome_at=terminal[1],
        )
        processed += 1
    return processed


def recover_cache_affinity_state(config: AppConfig) -> dict[str, int]:
    """在单 Gateway 节点接流量前恢复遗留 execution gate 与亲和计数。

    返回只包含数量的安全摘要。恢复顺序固定为：先对账已有终态，再把崩溃遗留的 started /
    starting / active 执行门失败化，再对账其 Selection，最后按当前 lease 的 pending 行重算
    Binding 活动数和 scope 指针。任一步失败都会抛固定异常并阻止应用启动。
    """

    summary = {
        "reconciled": 0,
        "interrupted_attempts": 0,
        "interrupted_streams": 0,
        "bindings_rebuilt": 0,
    }
    connection: sqlite3.Connection | None = None
    try:
        connection = connect_database(config.database_url)
        summary["reconciled"] += _reconcile_all_batches(connection)
        summary["interrupted_attempts"] = _interrupt_all_attempts(connection)
        summary["interrupted_streams"] = _interrupt_all_streams(connection)
        summary["reconciled"] += _reconcile_all_batches(connection)
        connection.execute("BEGIN IMMEDIATE")
        _assert_no_pending_selections(connection)
        summary["bindings_rebuilt"] = _rebuild_binding_activity(connection)
        connection.commit()
    except Exception:
        # 启动恢复必须失败关闭，但外层只能看到固定错误，不能泄露数据库路径或 SQL 参数。
        if connection is not None:
            connection.rollback()
        logger.error(
            "cache_affinity.startup_recovery_failed",
            extra={
                "event": "cache_affinity.startup_recovery_failed",
                "request_id": "startup",
                "trace_id": "startup",
                "error_code": "gateway.cache_affinity_unavailable",
            },
        )
        # 启动异常不串联底层 cause，避免 ASGI 启动日志输出数据库路径或 SQL 参数。
        raise RuntimeError("cache_affinity_startup_recovery_failed") from None
    finally:
        if connection is not None:
            connection.close()
    logger.info(
        "cache_affinity.startup_recovered",
        extra={
            "event": "cache_affinity.startup_recovered",
            "request_id": "startup",
            "trace_id": "startup",
            "payload": summary,
        },
    )
    return summary


def install_cache_affinity_recovery(app: FastAPI) -> None:
    """把单节点恢复安装为 Gateway 最内层、最先执行的 lifespan。"""

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(current_app: FastAPI):
        """原应用进入后同步完成恢复，成功后才允许外层 worker 和 Policy cache 启动。"""

        async with original_lifespan(current_app):
            current_app.state.cache_affinity_recovery = recover_cache_affinity_state(
                current_app.state.config
            )
            try:
                yield
            finally:
                current_app.state.cache_affinity_recovery = None

    app.router.lifespan_context = combined_lifespan


def _selection_matches_handle(
    selection: sqlite3.Row,
    handle: CacheAffinitySelectionHandle,
) -> bool:
    """逐字段核对内存句柄与不可变 Selection，阻断跨租户或 ABA 终态写。"""

    return (
        str(selection["request_id"]) == handle.request_id
        and str(selection["trace_id"]) == handle.trace_id
        and int(selection["attempt_sequence"]) == handle.attempt_sequence
        and (selection["routing_attempt_id"] or None) == handle.routing_attempt_id
        and (selection["stream_session_id"] or None) == handle.stream_session_id
        and str(selection["cache_affinity_binding_id_snapshot"]) == handle.cache_affinity_binding_id
        and str(selection["target_kind"]) == handle.target_kind
        and int(selection["target_generation"]) == handle.target_generation
        and int(selection["pending_lease_generation_snapshot"]) == handle.pending_lease_generation
        and str(selection["model_endpoint_id"]) == handle.model_endpoint_id
        and str(selection["provider_id"]) == handle.provider_id
        and str(selection["endpoint_revision_digest_snapshot"]) == handle.endpoint_revision_digest
        and (selection["provider_credential_id_snapshot"] or None) == handle.provider_credential_id
    )


def _execution_terminal(
    connection: sqlite3.Connection,
    selection: sqlite3.Row,
) -> tuple[str, str] | None:
    """读取 Selection 唯一父执行门，并映射为 succeeded/failed 与精确 completed_at。"""

    if selection["routing_attempt_id"] is not None:
        row = connection.execute(
            """
            SELECT status, completed_at FROM routing_attempts
            WHERE tenant_id=? AND routing_attempt_id=? AND request_id=? AND trace_id=?
            """,
            (
                selection["tenant_id"],
                selection["routing_attempt_id"],
                selection["request_id"],
                selection["trace_id"],
            ),
        ).fetchone()
        if (
            row is None
            or row["completed_at"] is None
            or row["status"] not in {"succeeded", "failed"}
        ):
            return None
        return ("succeeded" if row["status"] == "succeeded" else "failed", str(row["completed_at"]))
    row = connection.execute(
        """
        SELECT status, completed_at FROM stream_sessions
        WHERE tenant_id=? AND stream_session_id=? AND request_id=? AND trace_id=?
        """,
        (
            selection["tenant_id"],
            selection["stream_session_id"],
            selection["request_id"],
            selection["trace_id"],
        ),
    ).fetchone()
    if row is None or row["completed_at"] is None:
        return None
    if row["status"] in {
        "completed",
        "output_blocked",
        "output_safety_unavailable",
    }:
        # 输出安全发生在 Provider 已成功握手并产出之后；本地阻断/检测故障不能误罚亲和选择。
        return "succeeded", str(row["completed_at"])
    # timeout、客户端断开和上游失败都只能产生 failed Selection，绝不能写 warm 证据。
    # 状态枚举来自迁移 CHECK，未知状态继续返回 None 触发调用方失败关闭。
    if row["status"] in {"timed_out", "client_disconnected", "upstream_failed"}:
        return "failed", str(row["completed_at"])
    return None


def _finalize_selection_row(
    connection: sqlite3.Connection,
    selection: sqlite3.Row,
    *,
    outcome_status: str,
    outcome_at: str,
) -> None:
    """在同一事务提交 Selection 单向终态及其 Binding 活动计数和 warm 证据。"""

    # 只有固定单向终态和父门精确时间可进入 CAS，调用方不能伪造空时间或第三种结果。
    if outcome_status not in {"succeeded", "failed"} or not outcome_at:
        # 合同错误发生在写 Selection 前，必须让整个当前事务回滚。
        raise ValueError("cache_affinity_outcome_contract_invalid")
    if selection["outcome_status"] != "pending":
        if selection["outcome_status"] == outcome_status and selection["outcome_at"] == outcome_at:
            return
        # 已存在不同终态或时间表示不可变事实冲突，不能用迟到回调覆盖首次结果。
        # 重复相同终态仅由上方精确幂等分支接受。
        raise sqlite3.IntegrityError("cache_affinity_selection_terminal_conflict")
    cursor = connection.execute(
        """
        UPDATE cache_affinity_selections
        SET outcome_status=?, outcome_at=?
        WHERE tenant_id=? AND cache_affinity_selection_id=? AND outcome_status='pending'
        """,
        (
            outcome_status,
            outcome_at,
            selection["tenant_id"],
            selection["cache_affinity_selection_id"],
        ),
    )
    # 条件更新必须恰好命中 pending 行，零行说明并发终态与当前快照发生冲突。
    if cursor.rowcount != 1:
        raise sqlite3.IntegrityError("cache_affinity_selection_terminal_conflict")
    _apply_binding_terminal_effects(
        connection,
        selection,
        outcome_status=outcome_status,
        outcome_at=outcome_at,
    )


def _apply_binding_terminal_effects(
    connection: sqlite3.Connection,
    selection: sqlite3.Row,
    *,
    outcome_status: str,
    outcome_at: str,
) -> None:
    """按 target/lease 双 generation CAS 更新 warm 证据，并安全释放最后一个活动者。"""

    binding = connection.execute(
        """
        SELECT * FROM cache_affinity_bindings
        WHERE tenant_id=? AND cache_affinity_binding_id=?
        """,
        (
            selection["tenant_id"],
            selection["cache_affinity_binding_id_snapshot"],
        ),
    ).fetchone()
    # Selection 引用的 Binding 在历史终态前不得消失，否则活动计数与 warm 无法可信重建。
    if binding is None:
        raise sqlite3.IntegrityError("cache_affinity_binding_missing")
    target_current = (
        int(binding["target_generation"]) == int(selection["target_generation"])
        and str(binding["target_kind"]) == str(selection["target_kind"])
        and (binding["provider_credential_id"] or None)
        == (selection["provider_credential_id_snapshot"] or None)
        and str(binding["model_endpoint_id"]) == str(selection["model_endpoint_id"])
        and str(binding["provider_id"]) == str(selection["provider_id"])
        and str(binding["endpoint_revision_digest"])
        == str(selection["endpoint_revision_digest_snapshot"])
    )
    lease_current = target_current and int(binding["pending_lease_generation"]) == int(
        selection["pending_lease_generation_snapshot"]
    )
    active_count = int(binding["active_selection_count"])
    # 只有当前 lease 才能递减当前活动计数；计数已为零代表重复释放或 generation 图损坏。
    # 旧 lease 的迟到结果仍可按 target generation 写成功时间，但绝不能改新计数。
    if lease_current and active_count <= 0:
        raise sqlite3.IntegrityError("cache_affinity_active_count_underflow")
    new_active_count = active_count - 1 if lease_current else active_count
    last_success = str(binding["last_success_at"]) if binding["last_success_at"] else None
    if outcome_status == "succeeded" and target_current:
        # 成功可以跨 lease 推进同一 target 的真实证据，但旧 target generation 永远不得 warm。
        last_success = max(last_success, outcome_at) if last_success else outcome_at
    release_pointer = (
        outcome_status == "failed"
        and lease_current
        and new_active_count == 0
        and (
            last_success is None
            or _parse_db_time(last_success)
            < _parse_db_time(str(binding["pending_lease_started_at"]))
        )
    )
    if not target_current and not lease_current:
        return
    now = _to_db_time(_utcnow())
    connection.execute(
        """
        UPDATE cache_affinity_bindings
        SET active_selection_count=?, last_success_at=?,
            pending_lease_expires_at=CASE WHEN ? THEN ? ELSE pending_lease_expires_at END,
            updated_at=?
        WHERE tenant_id=? AND cache_affinity_binding_id=?
          AND target_generation=?
        """,
        (
            new_active_count,
            last_success,
            int(release_pointer),
            outcome_at,
            now,
            selection["tenant_id"],
            selection["cache_affinity_binding_id_snapshot"],
            selection["target_generation"],
        ),
    )
    if release_pointer:
        # 只清除仍指向相同 Binding + lease 的当前指针，不能误删并发请求建立的新偏好。
        connection.execute(
            """
            UPDATE cache_affinity_scopes
            SET preferred_binding_id=NULL, preferred_lease_generation=NULL, updated_at=?
            WHERE tenant_id=? AND cache_affinity_scope_id=?
              AND preferred_binding_id=? AND preferred_lease_generation=?
            """,
            (
                now,
                selection["tenant_id"],
                selection["cache_affinity_scope_id_snapshot"],
                selection["cache_affinity_binding_id_snapshot"],
                selection["pending_lease_generation_snapshot"],
            ),
        )


def _reconcile_all_batches(connection: sqlite3.Connection) -> int:
    """按 64 行事务循环对账全部已有父终态，避免长事务和遗漏尾批。"""

    total = 0
    while True:
        connection.execute("BEGIN IMMEDIATE")
        processed = reconcile_pending_cache_affinity_selections(connection)
        connection.commit()
        total += processed
        if processed < RECONCILE_BATCH_SIZE:
            return total


def _interrupt_all_attempts(connection: sqlite3.Connection) -> int:
    """把启动前遗留的非流 started attempt 按稳定 ID 分批失败化且不写质量观察。"""

    total = 0
    while True:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            """
            SELECT tenant_id, routing_attempt_id FROM routing_attempts
            WHERE status='started'
            ORDER BY tenant_id, routing_attempt_id
            LIMIT ?
            """,
            (RECONCILE_BATCH_SIZE,),
        ).fetchall()
        now = _to_db_time(_utcnow())
        for row in rows:
            connection.execute(
                """
                UPDATE routing_attempts
                SET status='failed', error_code='gateway.execution_interrupted',
                    error_category='other', upstream_http_status=NULL, retryable=0,
                    latency_ms=0, usage_status='not_applicable', completed_at=?, updated_at=?
                WHERE tenant_id=? AND routing_attempt_id=? AND status='started'
                """,
                (now, now, row["tenant_id"], row["routing_attempt_id"]),
            )
        connection.commit()
        total += len(rows)
        if len(rows) < RECONCILE_BATCH_SIZE:
            return total


def _interrupt_all_streams(connection: sqlite3.Connection) -> int:
    """把启动前遗留的 starting/active StreamSession 失败化且不生成质量样本。"""

    total = 0
    while True:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            """
            SELECT tenant_id, stream_session_id FROM stream_sessions
            WHERE status IN ('starting', 'active')
            ORDER BY tenant_id, stream_session_id
            LIMIT ?
            """,
            (RECONCILE_BATCH_SIZE,),
        ).fetchall()
        now = _to_db_time(_utcnow())
        for row in rows:
            connection.execute(
                """
                UPDATE stream_sessions
                SET status='upstream_failed', error_code='gateway.execution_interrupted',
                    completed_at=?, updated_at=?
                WHERE tenant_id=? AND stream_session_id=?
                  AND status IN ('starting', 'active')
                """,
                (now, now, row["tenant_id"], row["stream_session_id"]),
            )
        connection.commit()
        total += len(rows)
        if len(rows) < RECONCILE_BATCH_SIZE:
            return total


def _assert_no_pending_selections(connection: sqlite3.Connection) -> None:
    """阻止带孤儿或损坏父终态的 pending Selection 进入 ready。

    正常 started/starting/active 父行已在前序批次失败化并对账，因此此时任何 pending 都表示
    父行缺失、completed_at 缺失或状态图损坏。0040 不允许脱离父终态伪造 failed outcome，
    启动恢复必须整体失败关闭，交由运维按 request_id 审计源事实后修复。
    """

    row = connection.execute(
        """
        SELECT COUNT(*) AS pending_count
        FROM cache_affinity_selections WHERE outcome_status='pending'
        """
    ).fetchone()
    if row is None or int(row["pending_count"]) != 0:
        # 只判断总数，不把 Selection、Binding、Credential 或摘要写入启动日志。
        raise sqlite3.IntegrityError("cache_affinity_pending_recovery_incomplete")


def _rebuild_binding_activity(connection: sqlite3.Connection) -> int:
    """按当前 lease pending Selection 重算活动计数，并释放没有成功证据的遗留指针。"""

    rows = connection.execute(
        """
        SELECT binding.*,
          (
            SELECT COUNT(*) FROM cache_affinity_selections selection
            WHERE selection.tenant_id=binding.tenant_id
              AND selection.cache_affinity_binding_id_snapshot=binding.cache_affinity_binding_id
              AND selection.target_generation=binding.target_generation
              AND selection.pending_lease_generation_snapshot=binding.pending_lease_generation
              AND selection.outcome_status='pending'
          ) AS pending_count
        FROM cache_affinity_bindings binding
        ORDER BY binding.tenant_id, binding.cache_affinity_binding_id
        """
    ).fetchall()
    now = _to_db_time(_utcnow())
    for binding in rows:
        pending_count = int(binding["pending_count"])
        has_current_success = binding["last_success_at"] is not None and _parse_db_time(
            str(binding["last_success_at"])
        ) >= _parse_db_time(str(binding["pending_lease_started_at"]))
        release_pointer = pending_count == 0 and not has_current_success
        connection.execute(
            """
            UPDATE cache_affinity_bindings
            SET active_selection_count=?,
                pending_lease_expires_at=CASE WHEN ? THEN ? ELSE pending_lease_expires_at END,
                updated_at=?
            WHERE tenant_id=? AND cache_affinity_binding_id=?
            """,
            (
                pending_count,
                int(release_pointer),
                now,
                now,
                binding["tenant_id"],
                binding["cache_affinity_binding_id"],
            ),
        )
        if release_pointer:
            connection.execute(
                """
                UPDATE cache_affinity_scopes
                SET preferred_binding_id=NULL, preferred_lease_generation=NULL, updated_at=?
                WHERE tenant_id=? AND preferred_binding_id=?
                  AND preferred_lease_generation=?
                """,
                (
                    now,
                    binding["tenant_id"],
                    binding["cache_affinity_binding_id"],
                    binding["pending_lease_generation"],
                ),
            )
    return len(rows)


def _log_outcome_failure(
    handle: CacheAffinitySelectionHandle,
    exc: BaseException,
    *,
    failure_stage: str,
) -> None:
    """记录终态 CAS 的固定阶段和链路信息，不输出摘要、Binding、Credential 或异常正文。"""

    logger.warning(
        "cache_affinity.outcome_finalize_failed",
        extra={
            "event": "cache_affinity.outcome_finalize_failed",
            "tenant_id": handle.tenant_id,
            "request_id": handle.request_id,
            "trace_id": handle.trace_id,
            "error_code": "gateway.cache_affinity_unavailable",
            "payload": {
                "attempt_sequence": handle.attempt_sequence,
                "exception_type": exc.__class__.__name__,
                "failure_stage": failure_stage,
            },
        },
    )


def _parse_db_time(value: str) -> datetime:
    """解析 SQLite UTC 文本；无时区输入拒绝而不是按本机时区猜测。"""

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # 本机时区会让 TTL、lease 和迟到成功在不同部署产生不同结果，因此无时区文本必须拒绝。
    # 错误只作为内部一致性异常处理，不能回显数据库原值。
    if parsed.tzinfo is None:
        raise ValueError("cache_affinity_time_timezone_missing")
    return parsed.astimezone(UTC)


def _to_db_time(value: datetime) -> str:
    """把带时区时间转为项目统一的 UTC 秒级 Z 文本。

    既有表按文本比较单调时间；同秒混用 ``.ffffffZ`` 与 ``Z`` 会让微秒文本反而排序更小。
    """

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utcnow() -> datetime:
    """集中生成带时区 UTC 时间，便于专项测试冻结边界。"""

    return datetime.now(UTC)
