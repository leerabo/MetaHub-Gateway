"""流式请求的租户级审计会话。

StreamSession 只保存链路和资源状态元数据，不保存请求正文、响应正文、Endpoint、
Authorization 或 Provider Key。创建与 active 推进是上游调用和下游开流前的强制闸门；
终态更新是幂等容错路径，不能覆盖已经确定的客户端结果。
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import WebhookEvent, build_webhook_event

from metahub_gateway.error_maps.facts import insert_provider_error_fact
from metahub_gateway.output_safety import (
    OutputSafetyEventRecord,
    OutputSafetyStoreError,
    OutputSafetyTerminalState,
    PreparedOutputSafetyScan,
    finalize_output_safety_scan_in_transaction,
)
from metahub_gateway.providers.base import NormalizedProviderError
from metahub_gateway.quality import record_model_quality_observation
from metahub_gateway.usage.ledger import RequestLogContext

logger = logging.getLogger("metahub.gateway.streaming")

# 终态集合同时用于应用校验和调用日志映射；增加状态时必须同步 0027 CHECK 约束。
TERMINAL_STREAM_STATUSES = frozenset(
    {
        "completed",
        "timed_out",
        "client_disconnected",
        "upstream_failed",
        "output_blocked",
        "output_safety_unavailable",
    }
)
# 真实上游流失败使用唯一可重建码；本地流处理失败使用独立公开安全码。
UPSTREAM_STREAM_FAILURE_CODE = "gateway.upstream_stream_failed"
LOCAL_STREAM_PROCESSING_ERROR_CODE = "gateway.stream_processing_unavailable"
# 这些旧码仅供 0036 历史迁移保守识别；0037 运行时必须改写成唯一上游流归因码。
LEGACY_STREAM_QUALITY_ERROR_CODES = frozenset(
    {
        "gateway.upstream_timeout",
        "gateway.upstream_rate_limited",
        "gateway.upstream_stream_timeout",
    }
)


@dataclass
class StreamSession:
    """一次流式调用的安全状态快照和内存计数器。

    分片到达时只修改当前对象，避免每块数据库写入拖慢首字节或块间输出。终止时才把
    计数和时间一次回写；所有标识都来自已经鉴权、路由完成的服务端上下文。
    """

    stream_session_id: str
    tenant_id: str
    request_id: str
    trace_id: str
    model_id: str
    provider_id: str
    provider_code: str
    inter_token_timeout_ms: int
    started_at: datetime
    status: str = "starting"
    chunk_count: int = 0
    bytes_sent: int = 0
    first_chunk_at: datetime | None = None
    last_chunk_at: datetime | None = None
    # source event 仅含安全元数据，并在 scan/session 原子提交成功后供 Usage durability 门禁读取。
    output_safety_event: OutputSafetyEventRecord | None = field(default=None, repr=False)

    def note_chunk(self, chunk: bytes, *, occurred_at: datetime | None = None) -> None:
        """记录一个即将交给 ASGI 的非空传输块，不保留块内容。

        `bytes_sent` 的准确语义是已交给响应生成器的字节数，不代表客户端 TCP 已确认。
        空块不证明上游存活，调用方必须先过滤后再调用本函数。
        """

        if not chunk:
            return
        now = occurred_at or datetime.now(UTC)
        self.chunk_count += 1
        self.bytes_sent += len(chunk)
        if self.first_chunk_at is None:
            self.first_chunk_at = now
        self.last_chunk_at = now


def create_stream_session(
    config: AppConfig,
    context: RequestLogContext,
    *,
    inter_token_timeout_ms: int,
) -> StreamSession:
    """在任何上游网络调用前创建 starting 会话。

    模型和 Provider 缺失代表路由上下文不完整；数据库连接、约束或唯一冲突统一映射为
    `gateway.stream_session_unavailable`，并且不能继续调用 Adapter。
    """

    # 缺少实际模型或 Provider 归属时无法建立可信审计记录，必须在网络调用前拒绝。
    if not context.model_id or not context.provider_id or not context.provider_code:
        raise ApiError("gateway.stream_session_unavailable")
    now = datetime.now(UTC)
    session = StreamSession(
        stream_session_id=f"str_{uuid4().hex}",
        tenant_id=context.tenant_id,
        request_id=context.request_id,
        trace_id=context.trace_id,
        model_id=context.model_id,
        provider_id=context.provider_id,
        provider_code=context.provider_code,
        inter_token_timeout_ms=inter_token_timeout_ms,
        started_at=now,
    )
    connection = None
    try:
        connection = connect_database(config.database_url)
        db_time = _to_db_time(now)
        connection.execute(
            """
            INSERT INTO stream_sessions(
              stream_session_id, tenant_id, request_id, trace_id,
              virtual_key_id, environment_id, environment_code, project_id, application_id,
              model_id, provider_id, provider_code, status, inter_token_timeout_ms,
              chunk_count, bytes_sent, started_at, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'starting', ?, 0, 0, ?, ?, ?)
            """,
            (
                session.stream_session_id,
                session.tenant_id,
                session.request_id,
                session.trace_id,
                context.virtual_key_id,
                context.environment_id,
                context.environment_code,
                context.project_id,
                context.application_id,
                session.model_id,
                session.provider_id,
                session.provider_code,
                session.inter_token_timeout_ms,
                db_time,
                db_time,
                db_time,
            ),
        )
        connection.commit()
        # 只有事务提交成功的会话才能作为后续上游调用审计闸门返回。
        return session
    # 创建失败统一映射为业务错误，异常原文和数据库路径不能进入外部响应。
    except (DbError, sqlite3.Error) as exc:
        # 异常字符串可能包含 SQL 参数或文件路径，结构化日志只保留异常类型和链路标识。
        _log_session_write_failure(session, "stream_session.create_failed", exc)
        raise ApiError("gateway.stream_session_unavailable") from exc
    finally:
        if connection is not None:
            connection.close()


def mark_stream_session_active(config: AppConfig, session: StreamSession) -> None:
    """在上游 2xx 握手完成后把会话从 starting 推进为 active。

    条件更新失败意味着数据库中的事实与当前协程不一致。调用方必须立即关闭已经建立的
    上游连接，并且不能向客户端返回 StreamingResponse。
    """

    connection = None
    try:
        connection = connect_database(config.database_url)
        now = _to_db_time(datetime.now(UTC))
        cursor = connection.execute(
            """
            UPDATE stream_sessions
            SET status='active', updated_at=?
            WHERE tenant_id=? AND stream_session_id=? AND status='starting'
            """,
            (now, session.tenant_id, session.stream_session_id),
        )
        # 条件更新必须恰好命中 starting 行，否则说明状态已被其他执行路径改变。
        if cursor.rowcount != 1:
            raise sqlite3.IntegrityError("stream_session_active_transition_failed")
        connection.commit()
        session.status = "active"
    # active 推进失败属于审计闸门失败，调用方会负责关闭刚建立的上游连接。
    except (DbError, sqlite3.Error) as exc:
        _log_session_write_failure(session, "stream_session.activate_failed", exc)
        # 对外只返回统一错误码，不回显 SQLite 异常、路径或 SQL 内容。
        raise ApiError("gateway.stream_session_unavailable") from exc
    finally:
        if connection is not None:
            connection.close()


def safe_finish_stream_session(
    config: AppConfig,
    session: StreamSession,
    *,
    status: str,
    error_code: str | None,
    quality_observation_eligible: bool,
    error: NormalizedProviderError | None = None,
    output_safety_scan: PreparedOutputSafetyScan | None = None,
    output_safety_terminal: OutputSafetyTerminalState | None = None,
    _integrity_retry: bool = False,
) -> bool:
    """幂等提交流终态，失败时只记录安全 warning 并返回 False。

    返回 True 表示数据库已经是请求的同一终态，包括当前协程首次提交成功或另一路清理
    已经提交同一事实；返回 False 时调用方不得发布依赖该终态的 Webhook。
    """

    # scan 与终态必须成对传入；只给其中一项会让会话与安全证据无法原子收口。
    if (output_safety_scan is None) != (output_safety_terminal is None):
        raise ValueError("stream_session_output_safety_contract_invalid")
    if status in {"output_blocked", "output_safety_unavailable"} and output_safety_scan is None:
        # 两个本地安全终态必须与 scan（以及 blocked source event）同事务提交，
        # 禁止调用方只结束 StreamSession 后留下无法解释的安全证据缺口。
        raise ValueError("stream_session_output_safety_contract_invalid")
    if output_safety_scan is not None and (
        output_safety_scan.tenant_id != session.tenant_id
        or output_safety_scan.request_id != session.request_id
        or output_safety_scan.trace_id != session.trace_id
        or output_safety_scan.stream_session_id != session.stream_session_id
        or output_safety_scan.scan_mode != "stream_window"
    ):
        # 任何 owner 错配都必须在写会话前拒绝，防止跨租户/跨请求 scan 被拼入当前流。
        raise ValueError("stream_session_output_safety_owner_invalid")
    # 终结接口只接受数据库 CHECK 同步的固定终态，阻止调用方写入任意状态文本。
    if status not in TERMINAL_STREAM_STATUSES:
        raise ValueError("stream_session_terminal_status_invalid")
    if type(quality_observation_eligible) is not bool:
        # truthy 字符串不能把本地审计故障或客户端断开错误归因给模型。
        raise ValueError("quality_observation_eligibility_invalid")
    expected_quality_eligible = status in {"completed", "timed_out"} or (
        status == "upstream_failed" and error_code == UPSTREAM_STREAM_FAILURE_CODE
    )
    valid_error_contract = (
        (status == "completed" and error_code is None)
        or (status == "timed_out" and error_code == "gateway.upstream_stream_timeout")
        or (status == "client_disconnected" and error_code == "gateway.client_disconnected")
        or (status == "output_blocked" and error_code == "content_policy_violation")
        or (
            status == "output_safety_unavailable"
            and error_code == "gateway.output_safety_unavailable"
        )
        or (
            status == "upstream_failed"
            and isinstance(error_code, str)
            and bool(error_code)
            and error_code not in LEGACY_STREAM_QUALITY_ERROR_CODES
        )
    )
    # Session 状态、内部归因码和显式资格必须构成唯一矩阵，未知组合不得先落终态。
    if not valid_error_contract or quality_observation_eligible is not expected_quality_eligible:
        raise ValueError("stream_session_quality_contract_invalid")
    if output_safety_terminal is not None:
        terminal_pair = (
            output_safety_terminal.scan_status,
            output_safety_terminal.decision,
        )
        valid_output_pair = (
            (status == "output_blocked" and terminal_pair == ("completed", "blocked"))
            or (
                status == "output_safety_unavailable"
                and output_safety_terminal.scan_status == "unavailable"
            )
            or (
                status == "completed"
                and terminal_pair
                in {("completed", "clean"), ("completed", "matched")}
            )
            or (
                status == "upstream_failed"
                and session.status == "starting"
                and terminal_pair == ("not_applicable", "none")
            )
            or (
                status in {"timed_out", "client_disconnected", "upstream_failed"}
                and session.status == "active"
                and output_safety_terminal.scan_status == "unavailable"
            )
        )
        # 会话与 scan 的二维终态必须表达同一事实，不能把 blocked scan 挂到 completed 会话。
        if not valid_output_pair:
            raise ValueError("stream_session_output_safety_terminal_invalid")
    has_adapter_evidence = error is not None and error.evidence is not None
    # evidence 只能绑定握手失败；旧 Adapter 无 evidence 时仍结束 session，但不得补造 Fact。
    if has_adapter_evidence and status != "upstream_failed":
        raise ValueError("stream_session_provider_error_evidence_invalid")
    # 本地输出安全终态只能由 active 会话产生；握手失败的 starting 会话只允许 not_applicable。
    requires_active_origin = status in {
        "output_blocked",
        "output_safety_unavailable",
    }
    origin_predicate = (
        "status='active'"
        if requires_active_origin
        else "status IN ('starting', 'active')"
    )
    connection = None
    try:
        connection = connect_database(config.database_url)
        connection.execute("BEGIN IMMEDIATE")
        completed_at = datetime.now(UTC)
        completed_db = _to_db_time(completed_at)
        terminal_values = (
            status,
            session.chunk_count,
            session.bytes_sent,
            _to_optional_db_time(session.first_chunk_at),
            _to_optional_db_time(session.last_chunk_at),
            error_code,
        )
        if has_adapter_evidence:
            # 0046 新列只在真实握手 evidence 分支显式冻结；其他旧路径沿用迁移默认值。
            cursor = connection.execute(
                f"""
                UPDATE stream_sessions
                SET status=?, chunk_count=?, bytes_sent=?, first_chunk_at=?, last_chunk_at=?,
                    error_code=?, upstream_http_status=?, retryable=?, completed_at=?, updated_at=?
                WHERE tenant_id=? AND stream_session_id=? AND {origin_predicate}
                """,
                terminal_values
                + (
                    error.http_status,
                    1 if error.retryable else 0,
                    completed_db,
                    completed_db,
                    session.tenant_id,
                    session.stream_session_id,
                ),
            )
        else:
            cursor = connection.execute(
                f"""
                UPDATE stream_sessions
                SET status=?, chunk_count=?, bytes_sent=?, first_chunk_at=?, last_chunk_at=?,
                    error_code=?, completed_at=?, updated_at=?
                WHERE tenant_id=? AND stream_session_id=? AND {origin_predicate}
                """,
                terminal_values
                + (
                    completed_db,
                    completed_db,
                    session.tenant_id,
                    session.stream_session_id,
                ),
            )
        if cursor.rowcount == 1:
            output_finalization = None
            if output_safety_scan is not None and output_safety_terminal is not None:
                # scan、blocked source event 和 StreamSession 共用当前 BEGIN IMMEDIATE；任一失败全回滚。
                output_finalization = finalize_output_safety_scan_in_transaction(
                    connection,
                    output_safety_scan,
                    output_safety_terminal,
                    completed_at=completed_at,
                )
            if has_adapter_evidence:
                # Fact INSERT 不自行提交；触发器拒绝时 owner UPDATE 和质量观察必须一起回滚。
                insert_provider_error_fact(
                    connection,
                    tenant_id=session.tenant_id,
                    request_id=session.request_id,
                    trace_id=session.trace_id,
                    stream_session_id=session.stream_session_id,
                    provider_id=session.provider_id,
                    model_id=session.model_id,
                    provider_code=session.provider_code,
                    evidence=error.evidence,
                    owner_terminal_error_code=error_code or "",
                    created_at=completed_db,
                )
            if quality_observation_eligible:
                # 成功流与真实上游 timeout/read failure 使用完整会话耗时；客户端断开传 false。
                latency_ms = _elapsed_milliseconds(session.started_at, completed_at)
                record_model_quality_observation(
                    connection,
                    tenant_id=session.tenant_id,
                    model_id=session.model_id,
                    succeeded=status == "completed",
                    latency_ms=latency_ms,
                    observed_at=completed_at,
                )
            connection.commit()
            session.status = status
            if output_finalization is not None:
                session.output_safety_event = output_finalization.source_event
            return True
        # 幂等清理只接受完全相同的既有终态；不同终态不能被后来分支覆盖。
        row = connection.execute(
            """
            SELECT status FROM stream_sessions
            WHERE tenant_id=? AND stream_session_id=?
            """,
            (session.tenant_id, session.stream_session_id),
        ).fetchone()
        if row is not None and str(row["status"]) == status:
            # 原子终结重入时同时复核 scan 的完全相同终态；不同事实由持久化服务拒绝。
            if output_safety_scan is not None and output_safety_terminal is not None:
                output_finalization = finalize_output_safety_scan_in_transaction(
                    connection,
                    output_safety_scan,
                    output_safety_terminal,
                    completed_at=completed_at,
                )
                connection.commit()
                session.output_safety_event = output_finalization.source_event
            else:
                connection.rollback()
            session.status = status
            return True
        connection.rollback()
        _log_session_write_failure(
            session,
            "stream_session.finish_conflict",
            sqlite3.IntegrityError("stream_session_terminal_conflict"),
        )
        return False
    # 终态是容错写入，数据库异常只能记录安全 warning，不能覆盖已经发生的流结果。
    except (DbError, sqlite3.Error, OutputSafetyStoreError, ValueError) as exc:
        if connection is not None:
            connection.rollback()
        if isinstance(exc, sqlite3.IntegrityError) and not _integrity_retry:
            # A concurrent audit trigger can reject one terminal transaction.
            # Retry the same owner facts once in a fresh transaction; no new
            # provider request is allowed until this terminal commit succeeds.
            if connection is not None:
                connection.close()
                connection = None
            return safe_finish_stream_session(
                config,
                session,
                status=status,
                error_code=error_code,
                quality_observation_eligible=quality_observation_eligible,
                error=error,
                output_safety_scan=output_safety_scan,
                output_safety_terminal=output_safety_terminal,
                _integrity_retry=True,
            )
        _log_session_write_failure(session, "stream_session.finish_failed", exc)
        return False
    finally:
        if connection is not None:
            connection.close()


def build_stream_timeout_event(session: StreamSession) -> WebhookEvent:
    """基于已提交 timed_out 会话构造稳定、无正文的治理事件。"""

    # 非 timed_out 会话发布 timeout 事件会制造虚假治理事实，因此必须直接拒绝。
    if session.status != "timed_out":
        raise ValueError("stream_timeout_event_requires_timed_out_session")
    # 稳定 event_id 让重复清理最多产生同一投递事实，不使用请求内容参与摘要。
    digest = hashlib.sha256(f"stream.timeout:{session.stream_session_id}".encode()).hexdigest()[:32]
    return build_webhook_event(
        tenant_id=session.tenant_id,
        event_type="stream.timeout",
        event_id=f"evt_{digest}",
        object_id=session.stream_session_id,
        trace_id=session.trace_id,
        payload={
            "stream_session_id": session.stream_session_id,
            "request_id": session.request_id,
            "model_id": session.model_id,
            "provider_id": session.provider_id,
            "provider_code": session.provider_code,
            "inter_token_timeout_ms": session.inter_token_timeout_ms,
            "chunk_count": session.chunk_count,
            "bytes_sent": session.bytes_sent,
        },
    )


def _log_session_write_failure(session: StreamSession, event: str, exc: BaseException) -> None:
    """记录会话写入失败的安全元数据，不输出异常字符串或流内容。"""

    logger.warning(
        event,
        extra={
            "event": event,
            "request_id": session.request_id,
            "trace_id": session.trace_id,
            "tenant_id": session.tenant_id,
            "model_id": session.model_id,
            "provider_id": session.provider_id,
            "object_id": session.stream_session_id,
            "payload": {"exception_type": exc.__class__.__name__},
        },
    )


def _to_db_time(value: datetime) -> str:
    """把 UTC 时间转换为 SQLite 可排序、跨时区稳定的 ISO-8601 文本。"""

    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _to_optional_db_time(value: datetime | None) -> str | None:
    """转换可空时间，未收到分片时保持数据库 NULL。"""

    return _to_db_time(value) if value is not None else None


def _elapsed_milliseconds(started_at: datetime, completed_at: datetime) -> int:
    """以整数微秒计算非负完整会话毫秒数，并向下截断不足一毫秒的部分。

    ``timedelta.total_seconds`` 会先转换为 float，恰逢毫秒边界时可能与 0037 的历史
    重建产生一毫秒漂移。这里直接组合 days/seconds/microseconds，使实时累计和迁移 SQL
    对 1.6ms、跨秒及长会话都使用同一确定性口径。
    """

    elapsed = completed_at - started_at
    total_microseconds = (
        elapsed.days * 86_400_000_000 + elapsed.seconds * 1_000_000 + elapsed.microseconds
    )
    # 时钟异常导致的负差值不能进入 SQLite 累计列；沿用既有非负保护语义。
    return max(0, total_microseconds // 1_000)
