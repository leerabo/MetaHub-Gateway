"""PRD-070 Gateway 会话风险预检、Store 选择与持久证据编排。

预检严格位于 request claim/环境/Policy lease 之后、读取 Prompt 之前：先用 tenant-first
SQLite 点查独立 freeze overlay，再选择 current/previous HMAC cycle，并校验 Redis 热状态。
数据库是冻结与证据权威，Redis 是活动分数权威；两者无法对账时必须在 Provider 前失败关闭。
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.db.schema import tables_exist
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import WebhookEvent, build_webhook_event

from metahub_gateway.auth.virtual_key import GatewayAuthContext
from metahub_gateway.events import enqueue_runtime_event

from .identity import SessionRiskRequestIdentity, session_risk_enabled
from .models import (
    SessionRiskContractError,
    SessionRiskPending,
    SessionRiskPendingEvidence,
    SessionRiskSnapshot,
    SessionRiskStoreUnavailable,
    SessionRiskTurnConflict,
)
from .store import RedisSessionRiskStore, SessionRiskStore

logger = logging.getLogger("metahub.gateway.session_risk")


@dataclass(frozen=True, slots=True)
class SessionRiskPreflightResult:
    """返回读取正文前的固定风险状态与可选可信身份。

    ``status`` 可复制进 RequestLog；身份仅在 ``clean`` 时继续用于本轮分类和累计。降级时
    必须丢弃身份，避免调用方误用进程内状态创建一个不具备多副本一致性的 score。
    """

    status: str
    identity: SessionRiskRequestIdentity | None
    snapshot: SessionRiskSnapshot | None = None
    # degraded 时不能继续累计，但 RequestLog 仍需记录可信 scope 类型；绝不保存 scope_key。
    scope_type: str | None = None


@dataclass(frozen=True, slots=True)
class SessionRiskTurnResult:
    """返回一次 turn 累计后的 RequestLog 安全摘要，不包含 digest 或正文。"""

    status: str
    snapshot: SessionRiskSnapshot | None
    duplicate: bool = False
    freeze_transition: bool = False


async def preflight_session_risk(
    config: AppConfig,
    app_state: Any,
    auth_context: GatewayAuthContext,
    candidates: tuple[SessionRiskRequestIdentity, ...],
) -> SessionRiskPreflightResult:
    """在读取正文前完成 overlay、轮换 cycle、Redis 和 pending 预检。

    参数：启动配置、应用状态、认证 owner 及 current/previous 候选。返回：disabled、
    not_applicable、degraded 或 clean。关键约束：数据库查询始终以 tenant_id 开头；任何
    SQL/Store 异常只记录类型和固定码，不记录 URL、Header、摘要、Prompt 或底层消息。
    """

    connection: sqlite3.Connection | None = None
    try:
        connection = connect_database(config.database_url)
        table_exists = _session_risk_tables_exist(connection)
        # 功能关闭仍必须先检查持久冻结覆盖层，禁止通过切换配置绕过已生效的安全事实。
        if not session_risk_enabled(config):
            # 旧库没有 0062 且功能关闭时保持兼容；一旦存在 active overlay，则禁止靠关开关绕过。
            if table_exists and _has_any_active_overlay_for_key(
                connection,
                tenant_id=auth_context.tenant_id,
                virtual_key_id=auth_context.virtual_key_id,
            ):
                # 已有同租户 Key 覆盖层时必须失败关闭，不能因运行开关关闭而继续读取正文。
                raise ApiError("gateway.session_risk_unavailable")
            # 只有数据库确认无活动覆盖层时才能保持旧部署兼容并返回禁用状态。
            return SessionRiskPreflightResult(status="disabled", identity=None)
        if not table_exists:
            # enabled 却未迁移时无法证明冻结与证据，必须失败关闭而不是静默当空表。
            raise ApiError("gateway.session_risk_unavailable")

        _raise_if_unconfigured_hmac_version(connection, candidates)
        selected, durable = _select_durable_identity(connection, candidates)
        expired_durable: SessionRiskSnapshot | None = None
        now_epoch = int(time.time())
        if selected is not None and _snapshot_window_expired(durable, now_epoch):
            # Redis TTL 到期不能让数据库活动行永久占住 scope。先用 revision/CAS 把旧
            # cycle 变成历史，再重新选择候选；并发请求若已经推进窗口会在复读时获胜。
            expired_durable = _expire_due_durable_cycle(
                connection,
                selected,
                durable,
                now_epoch=now_epoch,
            )
            selected, durable = _select_durable_identity(connection, candidates)
        _raise_if_frozen(
            connection,
            tenant_id=auth_context.tenant_id,
            virtual_key_id=auth_context.virtual_key_id,
            identity=selected,
        )
    # 已分类的统一业务错误直接向上返回，不能再包装成含底层细节的新异常。
    except ApiError:
        raise
    # 数据库、类型和持久合同异常统一记录固定摘要，并在读取正文前失败关闭。
    except (DbError, sqlite3.Error, TypeError, ValueError, SessionRiskContractError) as exc:
        _log_preflight_failure(auth_context, "gateway.session_risk_unavailable", exc)
        # 对外错误不保留 cause，避免数据库 URL、文件路径或损坏字段进入 ASGI 日志。
        raise ApiError("gateway.session_risk_unavailable") from None
    finally:
        if connection is not None:
            connection.close()

    if selected is None:
        # 无可信 Header/grant session 时仍已完成 Key overlay 点查，但绝不按整把 Key 累计。
        return SessionRiskPreflightResult(status="not_applicable", identity=None)

    try:
        store = get_session_risk_store(config, app_state)
        await store.ping()
        pending = await store.load_pending(selected.scope)
        if pending is not None:
            # 先幂等重放数据库证据再 ACK；清债完成前不得读取正文或进入 Provider。
            await _persist_ack_and_dispatch(config, app_state, store, pending)
            # pending 提交把数据库推进到它的 snapshot；后续对账必须使用新 revision，
            # 不能继续拿预检开始时读到的旧 durable 误判 ACK 恢复失败。
            durable = pending.snapshot
        hot = await store.load(selected.scope)
        if (
            expired_durable is not None
            and hot is not None
            and hot.score_id == expired_durable.score_id
            and hot.revision == expired_durable.revision
        ):
            # DB 已完成过期转换后只删除精确旧 cycle；若 Redis 已进入新 cycle 或 pending，
            # reset CAS 不会误删，后续 exact 对账会决定是否可继续。
            await store.reset(selected.scope, hot.score_id, hot.revision)
            hot = await store.load(selected.scope)
        if durable is not None and (hot is None or hot.revision < durable.revision):
            await store.restore(selected.scope, durable)
            hot = await store.load(selected.scope)
        _validate_hot_against_durable(hot, durable)
        # 预检结果仅返回安全快照与 HMAC 身份，原始 Header、Prompt 和 Redis Key 不离开进程。
        return SessionRiskPreflightResult(
            status="clean",
            identity=selected,
            snapshot=hot,
            scope_type=selected.scope.scope_type,
        )
    # 可见 pending 代表数据库证据尚未确认，任何服务模式都必须阻断 Provider 调用。
    except SessionRiskPendingEvidence as exc:
        _log_preflight_failure(
            auth_context,
            "gateway.session_risk_evidence_unavailable",
            exc,
        )
        # 固定证据错误不携带 pending payload 或 Redis 异常链，避免敏感摘要进入响应日志。
        raise ApiError("gateway.session_risk_evidence_unavailable") from None
    # Store 不可用时仅内部模式在数据库已证明无冻结后可降级，公开服务始终失败关闭。
    except SessionRiskStoreUnavailable as exc:
        if config.service_mode == "internal_only":
            # DB 已在上方证明没有 active overlay；内部模式按冻结 ADR 可降级，但不得继续累计。
            _log_degraded(auth_context, exc)
            return SessionRiskPreflightResult(
                status="degraded",
                identity=None,
                scope_type=selected.scope.scope_type,
            )
        _log_preflight_failure(auth_context, "gateway.session_risk_unavailable", exc)
        # public_service 无热状态就无法证明准入安全，必须在 Provider 前返回固定不可用错误。
        raise ApiError("gateway.session_risk_unavailable") from None


async def record_session_risk_turn(
    config: AppConfig,
    app_state: Any,
    identity: SessionRiskRequestIdentity,
    assessment: Any,
) -> SessionRiskTurnResult:
    """原子累计一个 Shared assessment，并提交/ACK 唯一 pending。

    参数：配置、应用状态、可信身份和 ``TurnRiskAssessment``。返回：固定 RequestLog 摘要。
    关键约束：Store 只调用一次 accumulate；数据库提交失败或 ACK 不确定统一 503，不能让
    Provider 带着不可见风险债务继续。相同 turn+intent 重放不升分、不刷新 TTL。
    """

    try:
        if (
            assessment.turn_key_digest != identity.turn_key_digest
            or assessment.chat_message_id != identity.chat_message_id
        ):
            # identity 与 assessment 来自同一可信 turn；拼错对象会破坏幂等和 Chat Message owner。
            raise SessionRiskContractError()
        store = get_session_risk_store(config, app_state)
        result = await store.accumulate(identity.scope, assessment)
        # 重复 turn 已由 Store 原子识别，不升分、不刷新 TTL，RequestLog 只记录安全重复摘要。
        if result.duplicate:
            # 返回值不包含 turn HMAC 或原始会话标识，调用方只能记录固定状态与快照。
            return SessionRiskTurnResult(
                status="duplicate",
                snapshot=result.snapshot,
                duplicate=True,
            )
        # 没有状态跃迁时无需创建持久事件，直接返回当前安全快照。
        if result.pending is None:
            return SessionRiskTurnResult(status="clean", snapshot=result.snapshot)
        pending = result.pending
        await _persist_ack_and_dispatch(config, app_state, store, pending)
        # 只有数据库持久化与三元 ACK 都成功后才返回高危或冻结状态。
        return SessionRiskTurnResult(
            status=pending.snapshot.state,
            snapshot=pending.snapshot,
            freeze_transition=pending.freeze_transition,
        )
    # 未清偿 pending 不能允许下一轮风险累积或 Provider 调用，统一映射为证据错误。
    except SessionRiskPendingEvidence:
        raise ApiError("gateway.session_risk_evidence_unavailable") from None
    # 同一 turn 被另一意图复用时固定冲突，防止幂等键成为篡改已有证据的入口。
    except SessionRiskTurnConflict:
        raise ApiError("gateway.session_risk_turn_conflict") from None
    # 身份与 assessment 合同不一致表示内部拼接错误，按证据不可用失败关闭。
    except SessionRiskContractError:
        raise ApiError("gateway.session_risk_evidence_unavailable") from None
    # Redis 连接或协议故障不泄露端点细节，并在当前请求继续前失败关闭。
    except SessionRiskStoreUnavailable:
        raise ApiError("gateway.session_risk_unavailable") from None


async def _persist_ack_and_dispatch(
    config: AppConfig,
    app_state: Any,
    store: SessionRiskStore,
    pending: SessionRiskPending,
) -> None:
    """持久化 pending、执行三元 ACK，并在提交后机会性派发 canonical Webhook。"""

    try:
        events = _persist_pending(config, pending)
    # 持久化失败保留 Redis pending 供下次幂等恢复，错误响应不携带 SQL 或正文证据。
    except (DbError, sqlite3.Error, TypeError, ValueError, SessionRiskContractError):
        # Redis 保留 pending 供下一请求重放；数据库故障不能通过 Provider 调用掩盖。
        raise ApiError("gateway.session_risk_evidence_unavailable") from None
    try:
        acknowledged = await store.ack_pending(
            pending.scope,
            pending.snapshot.score_id,
            pending.snapshot.revision,
            pending.assessment.turn_key_digest,
        )
    # ACK 连接故障意味着无法证明债务已清除，固定返回证据错误等待下一请求恢复。
    except SessionRiskStoreUnavailable:
        raise ApiError("gateway.session_risk_evidence_unavailable") from None
    if not acknowledged:
        # DB 可能已经提交，但 Redis 状态无法通过三元组证明；下一请求必须重放对账。
        raise ApiError("gateway.session_risk_evidence_unavailable")
    for event in events:
        try:
            enqueue_runtime_event(app_state, event)
        except Exception as exc:
            # Webhook 是提交后通知；队列故障不能回滚 freeze 或改变本次安全准入。
            _log_event_enqueue_failure(pending, event, exc)


def get_session_risk_store(config: AppConfig, app_state: Any) -> SessionRiskStore:
    """返回测试注入 Store 或缓存的生产 Redis Store。

    测试只需设置 ``app.state.session_risk_store``；生产必须配置 Redis URL，绝不自动退化到
    ``InMemorySessionRiskStore``。缓存不保存可公开读取的 URL 属性，避免结构化状态泄密。
    """

    injected = getattr(app_state, "session_risk_store", None)
    # 显式注入只用于应用装配与测试，调用者仍必须遵守生产 Store 的失败关闭合同。
    if injected is not None:
        return injected
    # 缺少 Redis URL 时不能退化为进程内状态，否则多副本会产生不同冻结结论。
    if not config.redis_url:
        raise SessionRiskStoreUnavailable("session risk store unavailable")
    cached = getattr(app_state, "_metahub_session_risk_store", None)
    # 只复用已知生产类型的缓存对象，避免任意 app.state 属性冒充安全 Store。
    if isinstance(cached, RedisSessionRiskStore):
        return cached
    store = RedisSessionRiskStore(config.redis_url)
    app_state._metahub_session_risk_store = store
    return store


def _persist_pending(
    config: AppConfig,
    pending: SessionRiskPending,
) -> tuple[WebhookEvent, ...]:
    """在一个 SQLite 事务内幂等提交 Redis pending 的全部权威证据。

    参数：启动配置与已经通过 Store exact-schema 校验的 pending。返回：提交后可机会性
    派发的 canonical Webhook；普通 monitoring 累计不产生事件。关键约束：tenant_id
    始终是每条查询的首个 owner 条件；score CAS、observation、自动 overlay、action 和
    source event 要么全部提交，要么全部回滚。ACK 丢失后的重放只读取既有事实，不会
    再次插入 observation/action/event。
    """

    connection: sqlite3.Connection | None = None
    try:
        connection = connect_database(config.database_url)
        connection.execute("BEGIN IMMEDIATE")
        replayed, replay_event = _load_committed_pending(connection, pending)
        if replayed:
            connection.commit()
            return (replay_event,) if replay_event is not None else ()

        _write_score_snapshot(connection, pending)
        _insert_observation(connection, pending)
        event = _insert_transition_event(connection, pending)
        connection.commit()
        return (event,) if event is not None else ()
    except Exception:
        if connection is not None:
            connection.rollback()
        raise
    finally:
        if connection is not None:
            connection.close()


def _load_committed_pending(
    connection: sqlite3.Connection,
    pending: SessionRiskPending,
) -> tuple[bool, WebhookEvent | None]:
    """识别“数据库已提交、Redis ACK 响应丢失”的精确重放。

    只有同 tenant/score/turn 的 observation 与 pending 每个安全字段完全相等时才视为
    已提交；仅唯一键碰撞或同 revision 的另一事实都失败关闭，不能为了清除 Redis 债务
    猜测数据库状态。状态跃迁还必须存在同 revision source event，并复用原 event ID。
    """

    scope = pending.scope
    assessment = pending.assessment
    row = connection.execute(
        """
        SELECT turn_intent_digest,hmac_key_version,chat_message_id,
               source_request_id,source_trace_id,intent_risk_result_id,
               intent_risk_type,intent_risk_score,dlp_action,
               dlp_current_turn_match_count,tool_inducement_detected,turn_delta,
               score_before,score_after,risky_turn_index,state_revision,
               input_truncated,detector_version,ruleset_version,threshold_version,
               unixepoch(created_at) AS created_at_epoch
        FROM session_risk_observations
        WHERE tenant_id=? AND session_risk_score_id=? AND turn_key_digest=?
        """,
        (
            scope.tenant_id,
            pending.snapshot.score_id,
            assessment.turn_key_digest,
        ),
    ).fetchone()
    if row is None:
        return False, None

    expected = (
        assessment.turn_intent_digest,
        scope.hmac_key_version,
        assessment.chat_message_id,
        assessment.source_request_id,
        assessment.source_trace_id,
        assessment.intent_risk_result_id,
        assessment.intent_risk_type,
        assessment.intent_risk_score,
        assessment.dlp_action,
        assessment.dlp_current_turn_match_count,
        1 if assessment.tool_inducement_detected else 0,
        assessment.turn_delta,
        pending.score_before,
        pending.snapshot.score,
        pending.snapshot.risky_turn_count,
        pending.snapshot.revision,
        1 if assessment.input_truncated else 0,
        assessment.detector_version,
        assessment.ruleset_version,
        assessment.threshold_version,
        pending.snapshot.last_risk_at_epoch,
    )
    # 已存在 observation 的全部不可变字段必须逐项等于本次 pending，幂等重放不能修改历史。
    if tuple(row) != expected:
        raise SessionRiskContractError()

    score_row = connection.execute(
        """
        SELECT session_risk_score_id,score,risky_turn_count,jailbreak_count,
               sensitive_count,tool_inducement_count,status,state_revision,
               unixepoch(window_started_at) AS window_started_at_epoch,
               unixepoch(last_risk_at) AS last_risk_at_epoch,
               unixepoch(window_expires_at) AS expires_at_epoch,
               hmac_key_version,threshold_version
        FROM session_risk_scores
        WHERE tenant_id=? AND session_risk_score_id=?
        """,
        (scope.tenant_id, pending.snapshot.score_id),
    ).fetchone()
    # score 快照必须与 Redis pending 完全一致，数据库不能仅保存部分计数或较旧 revision。
    if score_row is None or _snapshot_from_row(score_row) != pending.snapshot:
        raise SessionRiskContractError()

    transition_expected = pending.high_risk_transition or pending.freeze_transition
    event_row = _select_transition_event_row(connection, pending)
    # 是否存在来源事件必须和跃迁标志严格等价，避免普通累计误告警或高危跃迁漏审计。
    if transition_expected != (event_row is not None):
        raise SessionRiskContractError()
    return True, _webhook_from_event_row(event_row) if event_row is not None else None


def _write_score_snapshot(
    connection: sqlite3.Connection,
    pending: SessionRiskPending,
) -> None:
    """以 revision CAS 新建或推进持久 score，不允许覆盖另一并发 turn。"""

    scope = pending.scope
    assessment = pending.assessment
    snapshot = pending.snapshot
    window_started_at = _epoch_to_db_time(snapshot.window_started_at_epoch)
    occurred_at = _epoch_to_db_time(snapshot.last_risk_at_epoch)
    expires_at = (
        _epoch_to_db_time(snapshot.expires_at_epoch)
        if snapshot.expires_at_epoch is not None
        else None
    )
    first_high_risk_at = occurred_at if pending.high_risk_transition else None
    frozen_at = occurred_at if pending.freeze_transition else None

    # 首个 revision 只能从空状态和零分开始，防止伪造历史累计直接建立新风险周期。
    if snapshot.revision == 1:
        # 新周期携带旧状态或旧分数表示 Store 合同损坏，数据库不得接纳该 pending。
        if pending.state_before != "none" or pending.score_before != 0:
            raise SessionRiskContractError()
        existing = _load_active_scope_score(
            connection,
            tenant_id=scope.tenant_id,
            scope_type=scope.scope_type,
            scope_key=scope.scope_key,
        )
        if existing is not None:
            # 请求可能在 preflight 后跨过 Redis TTL：新 score 的 pending 到库时，必须先
            # 原子归档已经到期的旧 cycle，才能释放活动 scope 唯一索引。
            expired = _expire_score_row(
                connection,
                existing,
                now_epoch=snapshot.window_started_at_epoch,
            )
            # 已有周期尚未到期时不能复用唯一活动 scope，防止并发建立两个权威 score。
            if expired is None:
                raise SessionRiskContractError()
        connection.execute(
            """
            INSERT INTO session_risk_scores(
              session_risk_score_id,tenant_id,scope_type,scope_key,hmac_key_version,
              virtual_key_id,chat_session_id,status,score,score_scale,risky_turn_count,
              jailbreak_count,sensitive_count,tool_inducement_count,state_revision,
              threshold_version,detector_version,ruleset_version,last_source_request_id,
              last_source_trace_id,window_started_at,last_risk_at,window_expires_at,
              first_high_risk_at,frozen_at,resolved_at,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,10000,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                snapshot.score_id,
                scope.tenant_id,
                scope.scope_type,
                scope.scope_key,
                scope.hmac_key_version,
                scope.virtual_key_id,
                scope.chat_session_id,
                snapshot.state,
                snapshot.score,
                snapshot.risky_turn_count,
                snapshot.jailbreak_count,
                snapshot.sensitive_count,
                snapshot.tool_inducement_count,
                snapshot.revision,
                snapshot.threshold_version,
                assessment.detector_version,
                assessment.ruleset_version,
                assessment.source_request_id,
                assessment.source_trace_id,
                window_started_at,
                occurred_at,
                expires_at,
                first_high_risk_at,
                frozen_at,
                None,
                window_started_at,
                occurred_at,
            ),
        )
        return

    # 非首轮写入必须明确引用旧状态且 revision 连续，不能把更新伪装成新建周期。
    if pending.state_before == "none" or snapshot.revision < 2:
        raise SessionRiskContractError()
    cursor = connection.execute(
        """
        UPDATE session_risk_scores
        SET status=?,score=?,risky_turn_count=?,jailbreak_count=?,sensitive_count=?,
            tool_inducement_count=?,state_revision=?,threshold_version=?,detector_version=?,
            ruleset_version=?,last_source_request_id=?,last_source_trace_id=?,
            last_risk_at=?,window_expires_at=?,
            first_high_risk_at=CASE WHEN ? IS NULL THEN first_high_risk_at
                                    ELSE COALESCE(first_high_risk_at,?) END,
            frozen_at=CASE WHEN ? IS NULL THEN frozen_at ELSE ? END,
            updated_at=?
        WHERE tenant_id=? AND session_risk_score_id=? AND scope_type=? AND scope_key=?
          AND hmac_key_version=? AND virtual_key_id=? AND state_revision=?
          AND status=? AND score=? AND risky_turn_count=?
          AND jailbreak_count=? AND sensitive_count=? AND tool_inducement_count=?
        """,
        (
            snapshot.state,
            snapshot.score,
            snapshot.risky_turn_count,
            snapshot.jailbreak_count,
            snapshot.sensitive_count,
            snapshot.tool_inducement_count,
            snapshot.revision,
            snapshot.threshold_version,
            assessment.detector_version,
            assessment.ruleset_version,
            assessment.source_request_id,
            assessment.source_trace_id,
            occurred_at,
            expires_at,
            first_high_risk_at,
            first_high_risk_at,
            frozen_at,
            frozen_at,
            occurred_at,
            scope.tenant_id,
            snapshot.score_id,
            scope.scope_type,
            scope.scope_key,
            scope.hmac_key_version,
            scope.virtual_key_id,
            snapshot.revision - 1,
            pending.state_before,
            pending.score_before,
            snapshot.risky_turn_count - 1,
            snapshot.jailbreak_count - assessment.jailbreak_count_delta,
            snapshot.sensitive_count - assessment.sensitive_count_delta,
            snapshot.tool_inducement_count - assessment.tool_inducement_count_delta,
        ),
    )
    # CAS 更新必须恰好命中同租户同 revision 行，并发获胜后当前 pending 不能覆盖新事实。
    if cursor.rowcount != 1:
        raise SessionRiskContractError()


def _insert_observation(
    connection: sqlite3.Connection,
    pending: SessionRiskPending,
) -> None:
    """追加一个风险 turn 的最小证据，不保存正文、Header 或命中片段。"""

    scope = pending.scope
    assessment = pending.assessment
    snapshot = pending.snapshot
    connection.execute(
        """
        INSERT INTO session_risk_observations(
          session_risk_observation_id,tenant_id,session_risk_score_id,
          turn_identity_type,turn_key_digest,turn_intent_digest,hmac_key_version,
          chat_message_id,source_request_id,source_trace_id,intent_risk_result_id,
          intent_risk_type,intent_risk_score,intent_risk_score_scale,dlp_action,
          dlp_current_turn_match_count,tool_inducement_detected,turn_delta,
          score_before,score_after,risky_turn_index,state_revision,input_truncated,
          detector_version,ruleset_version,threshold_version,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,10000,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            f"sro_{uuid4().hex}",
            scope.tenant_id,
            snapshot.score_id,
            "chat_message" if scope.scope_type == "enterprise_chat" else "api_turn",
            assessment.turn_key_digest,
            assessment.turn_intent_digest,
            scope.hmac_key_version,
            assessment.chat_message_id,
            assessment.source_request_id,
            assessment.source_trace_id,
            assessment.intent_risk_result_id,
            assessment.intent_risk_type,
            assessment.intent_risk_score,
            assessment.dlp_action,
            assessment.dlp_current_turn_match_count,
            1 if assessment.tool_inducement_detected else 0,
            assessment.turn_delta,
            pending.score_before,
            snapshot.score,
            snapshot.risky_turn_count,
            snapshot.revision,
            1 if assessment.input_truncated else 0,
            assessment.detector_version,
            assessment.ruleset_version,
            assessment.threshold_version,
            _epoch_to_db_time(snapshot.last_risk_at_epoch),
        ),
    )


def _insert_transition_event(
    connection: sqlite3.Connection,
    pending: SessionRiskPending,
) -> WebhookEvent | None:
    """为首次高风险或自动会话冻结写入动作与来源事件。

    参数是同一租户范围内尚待持久化的状态跃迁；返回可投递 Webhook 的安全事件。普通累计不写事件，
    避免重放和并发请求生成重复告警。
    """

    if not pending.high_risk_transition and not pending.freeze_transition:
        return None
    if pending.state_before == "none":
        # 高危至少需要两个风险 turn；出现 none -> 高危表示 Store 与迁移阈值已经漂移。
        raise SessionRiskContractError()

    scope = pending.scope
    assessment = pending.assessment
    snapshot = pending.snapshot
    occurred_at = _epoch_to_db_time(snapshot.last_risk_at_epoch)
    freeze_id: str | None = None
    if pending.freeze_transition:
        freeze_id = f"srf_{uuid4().hex}"
        connection.execute(
            """
            INSERT INTO session_risk_freezes(
              session_risk_freeze_id,tenant_id,source_session_risk_score_id,
              target_type,target_scope_type,target_scope_key,virtual_key_id,status,
              state_revision,created_by_type,created_by_id,released_by_type,released_by_id,
              created_source_request_id,created_source_trace_id,released_source_request_id,
              released_source_trace_id,created_at,released_at
            ) VALUES(?,?,?,'session',?,?,?,'active',1,'system','session-risk-engine',
                     NULL,NULL,?,?,NULL,NULL,?,NULL)
            """,
            (
                freeze_id,
                scope.tenant_id,
                snapshot.score_id,
                scope.scope_type,
                scope.scope_key,
                scope.virtual_key_id,
                assessment.source_request_id,
                assessment.source_trace_id,
                occurred_at,
            ),
        )

    action_id = f"sra_{uuid4().hex}"
    action = "freeze_session" if pending.freeze_transition else "high_risk"
    # 0062 对所有系统阈值跃迁使用同一固定原因，具体 high/freeze 由 action/status 表达；
    # 不再发明两个近义原因，避免 Gateway 与 Control 的枚举目录漂移。
    reason_code = "threshold_reached"
    connection.execute(
        """
        INSERT INTO session_risk_actions(
          session_risk_action_id,tenant_id,session_risk_score_id,session_risk_freeze_id,
          action,reason_code,actor_type,actor_id,status_before,status_after,
          score_snapshot,state_revision,source_request_id,source_trace_id,
          digest_key_version,idempotency_key_digest,intent_digest,idempotency_expires_at,
          created_at
        ) VALUES(?,?,?,?,?,?,'system','session-risk-engine',?,?,?,?,?,?,NULL,NULL,NULL,NULL,?)
        """,
        (
            action_id,
            scope.tenant_id,
            snapshot.score_id,
            freeze_id,
            action,
            reason_code,
            pending.state_before,
            snapshot.state,
            snapshot.score,
            snapshot.revision,
            assessment.source_request_id,
            assessment.source_trace_id,
            occurred_at,
        ),
    )

    event_id = f"srevt_{uuid4().hex}"
    connection.execute(
        """
        INSERT INTO session_risk_events(
          session_risk_event_id,tenant_id,session_risk_score_id,session_risk_action_id,
          session_risk_freeze_id,event_type,scope_type,scope_key,status,target_type,
          score,score_scale,risky_turn_count,jailbreak_count,sensitive_count,
          tool_inducement_count,state_revision,threshold_version,actor_type,actor_id,
          source_request_id,source_trace_id,occurred_at
        ) VALUES(?,?,?,?,?,'security.session_high_risk',?,?,?,?,?,10000,?,?,?,?,?,?,'system',
                 'session-risk-engine',?,?,?)
        """,
        (
            event_id,
            scope.tenant_id,
            snapshot.score_id,
            action_id,
            freeze_id,
            scope.scope_type,
            scope.scope_key,
            snapshot.state,
            "session" if freeze_id is not None else None,
            snapshot.score,
            snapshot.risky_turn_count,
            snapshot.jailbreak_count,
            snapshot.sensitive_count,
            snapshot.tool_inducement_count,
            snapshot.revision,
            snapshot.threshold_version,
            assessment.source_request_id,
            assessment.source_trace_id,
            occurred_at,
        ),
    )
    event_row = _select_transition_event_row(connection, pending)
    # 写入跃迁后必须复读同租户同 revision 来源事件，缺失时事务证据不完整。
    if event_row is None:
        raise SessionRiskContractError()
    return _webhook_from_event_row(event_row)


def _select_transition_event_row(
    connection: sqlite3.Connection,
    pending: SessionRiskPending,
) -> sqlite3.Row | None:
    """按租户、风险分数和修订号读取唯一来源事件及固定执行主体。

    返回值只用于幂等恢复与 Webhook 重放；三项条件共同限定可防止误读其他租户或其他状态修订。
    """

    return connection.execute(
        """
        SELECT session_risk_event_id,tenant_id,session_risk_score_id,
               session_risk_action_id,session_risk_freeze_id,event_type,scope_type,
               scope_key,status,target_type,score,score_scale,risky_turn_count,
               jailbreak_count,sensitive_count,tool_inducement_count,state_revision,
               threshold_version,actor_id,source_request_id,source_trace_id,
               unixepoch(occurred_at) AS occurred_at_epoch
        FROM session_risk_events
        WHERE tenant_id=? AND session_risk_score_id=? AND state_revision=?
        """,
        (
            pending.scope.tenant_id,
            pending.snapshot.score_id,
            pending.snapshot.revision,
        ),
    ).fetchone()


def _webhook_from_event_row(row: sqlite3.Row) -> WebhookEvent:
    """把已持久 source row 转成 Shared exact-validator 接受的安全事件。"""

    occurred_at_epoch = int(row["occurred_at_epoch"])
    occurred_at = datetime.fromtimestamp(occurred_at_epoch, UTC)
    scope_type = str(row["scope_type"])
    scope_key = str(row["scope_key"])
    scope_reference = (
        scope_key if scope_type == "enterprise_chat" else f"api_{scope_key[4:20]}"
    )
    return build_webhook_event(
        tenant_id=str(row["tenant_id"]),
        event_type=str(row["event_type"]),
        event_id=str(row["session_risk_event_id"]),
        actor_id=str(row["actor_id"]),
        object_id=str(row["session_risk_score_id"]),
        trace_id=str(row["source_trace_id"]),
        source_request_id=str(row["source_request_id"]),
        timestamp=occurred_at,
        payload={
            "schema_version": 1,
            "session_risk_score_id": str(row["session_risk_score_id"]),
            "session_risk_action_id": str(row["session_risk_action_id"]),
            "session_risk_freeze_id": row["session_risk_freeze_id"],
            "scope_type": scope_type,
            "scope_reference": scope_reference,
            "status": str(row["status"]),
            "target_type": row["target_type"],
            "score": int(row["score"]),
            "score_scale": int(row["score_scale"]),
            "risky_turn_count": int(row["risky_turn_count"]),
            "jailbreak_count": int(row["jailbreak_count"]),
            "sensitive_count": int(row["sensitive_count"]),
            "tool_inducement_count": int(row["tool_inducement_count"]),
            "state_revision": int(row["state_revision"]),
            "threshold_version": str(row["threshold_version"]),
            "occurred_at": _epoch_to_db_time(occurred_at_epoch),
        },
    )


def _epoch_to_db_time(epoch: int) -> str:
    """把 Store 的整数秒规范化为 SQLite/Webhook 共用 UTC ``Z`` 时间。"""

    # Store 时间必须是非负原生整数，拒绝 bool、浮点和负值避免时间证据漂移。
    if type(epoch) is not int or epoch < 0:
        raise SessionRiskContractError()
    try:
        return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    # 超出平台时间范围时统一转换合同错误，不把系统时区或平台异常泄露到响应。
    except (OverflowError, OSError, ValueError):
        raise SessionRiskContractError() from None


def _log_event_enqueue_failure(
    pending: SessionRiskPending,
    event: WebhookEvent,
    exc: Exception,
) -> None:
    """记录提交后派发异常，只输出固定身份和异常类型，不输出事件 payload。"""

    logger.warning(
        "security.session_risk_event_enqueue_failed",
        extra={
            "event": "security.session_risk_event_enqueue_failed",
            "tenant_id": pending.scope.tenant_id,
            "trace_id": pending.assessment.source_trace_id,
            "object_id": event.event_id,
            "payload": {
                "event_type": event.event_type,
                "error_code": "webhook.delivery_unavailable",
                "exception_type": exc.__class__.__name__,
            },
        },
    )


def _session_risk_tables_exist(connection: sqlite3.Connection) -> bool:
    """确认 0062 的 score/freeze 两张权威表同时存在。"""

    # 两张权威表必须同时存在才视为迁移完成，半迁移状态不能被当成空风险数据。
    return tables_exist(connection, ("session_risk_scores", "session_risk_freezes"))


def _has_any_active_overlay_for_key(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    virtual_key_id: str,
) -> bool:
    """功能关闭时检查同租户/Key 是否仍有任意 active overlay。

    关闭状态无法安全解析 session keyring，因此同 Key 的 session overlay 也会阻断该 Key；这是
    运维配置违规下的失败关闭，不是按 Key 建立风险画像。
    """

    row = connection.execute(
        """
        SELECT 1 FROM session_risk_freezes
        WHERE tenant_id=? AND virtual_key_id=? AND status='active'
        LIMIT 1
        """,
        (tenant_id, virtual_key_id),
    ).fetchone()
    return row is not None


def _raise_if_unconfigured_hmac_version(
    connection: sqlite3.Connection,
    candidates: tuple[SessionRiskRequestIdentity, ...],
) -> None:
    """拒绝在同租户/Key 仍引用已移除 HMAC 版本时创建旁路 cycle。

    current/previous 候选最多两个。API session 在旧 secret 被删除后无法重新派生旧 scope，
    因此维护门禁必须按认证 tenant/Key 检查全部活动 score；命中时整把 Key 暂停会话累计，
    直到运维恢复 previous 或按既定 verifier 清空引用。
    """

    if not candidates:
        return
    owner = candidates[0].scope
    versions = tuple(candidate.scope.hmac_key_version for candidate in candidates)
    placeholders = ",".join("?" for _ in versions)
    row = connection.execute(
        f"""
        SELECT 1 FROM session_risk_scores
        WHERE tenant_id=? AND virtual_key_id=?
          AND status IN ('monitoring','high_risk','frozen')
          AND hmac_key_version NOT IN ({placeholders})
        LIMIT 1
        """,
        (owner.tenant_id, owner.virtual_key_id, *versions),
    ).fetchone()
    if row is not None:
        # 不返回缺失版本号或 scope，避免配置与会话身份经公开错误被枚举。
        raise ApiError("gateway.session_risk_hmac_version_unavailable")


def _snapshot_window_expired(
    snapshot: SessionRiskSnapshot | None,
    now_epoch: int,
) -> bool:
    """判断非冻结活动快照是否已越过风险窗口；冻结态永不依赖 TTL 解封。"""

    return bool(
        snapshot is not None
        and snapshot.state in {"monitoring", "high_risk"}
        and snapshot.expires_at_epoch is not None
        and snapshot.expires_at_epoch <= now_epoch
    )


def _expire_due_durable_cycle(
    connection: sqlite3.Connection,
    identity: SessionRiskRequestIdentity,
    expected: SessionRiskSnapshot,
    *,
    now_epoch: int,
) -> SessionRiskSnapshot | None:
    """在独立即时事务中 CAS 归档一个预检发现的到期数据库 cycle。

    参数：现有连接、可信 scope、预读快照和当前 epoch。返回：确实归档的旧快照；并发已经
    推进或处置时返回空并由调用方复读。关键约束：只按 tenant/scope/score/revision 精确
    更新，不能让过期清理覆盖新的风险 turn 或人工冻结。
    """

    connection.execute("BEGIN IMMEDIATE")
    try:
        row = _load_active_score_by_id(
            connection,
            tenant_id=identity.scope.tenant_id,
            score_id=expected.score_id,
        )
        if row is None:
            connection.commit()
            return None
        current = _snapshot_from_row(row)
        if (
            current != expected
            or str(row["scope_type"]) != identity.scope.scope_type
            or str(row["scope_key"]) != identity.scope.scope_key
            or int(row["hmac_key_version"]) != identity.scope.hmac_key_version
        ):
            connection.commit()
            return None
        expired = _expire_score_row(connection, row, now_epoch=now_epoch)
        connection.commit()
        return expired
    except Exception:
        connection.rollback()
        raise


def _load_active_scope_score(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    scope_type: str,
    scope_key: str,
) -> sqlite3.Row | None:
    """读取同 tenant/scope 唯一活动 score，供新 cycle 入库前执行过期归档。"""

    rows = connection.execute(
        """
        SELECT *,unixepoch(window_started_at) AS window_started_at_epoch,
               unixepoch(last_risk_at) AS last_risk_at_epoch,
               unixepoch(window_expires_at) AS expires_at_epoch,
               unixepoch(updated_at) AS updated_at_epoch
        FROM session_risk_scores
        WHERE tenant_id=? AND scope_type=? AND scope_key=?
          AND status IN ('monitoring','high_risk','frozen')
        LIMIT 2
        """,
        (tenant_id, scope_type, scope_key),
    ).fetchall()
    # 同一租户 scope 出现多个活动周期违反唯一权威合同，不能任意挑选一行继续累计。
    if len(rows) > 1:
        raise SessionRiskContractError()
    return rows[0] if rows else None


def _load_active_score_by_id(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    score_id: str,
) -> sqlite3.Row | None:
    """按 tenant/score 精确读取活动行及 epoch 投影，不接受历史行参与自动归档。"""

    return connection.execute(
        """
        SELECT *,unixepoch(window_started_at) AS window_started_at_epoch,
               unixepoch(last_risk_at) AS last_risk_at_epoch,
               unixepoch(window_expires_at) AS expires_at_epoch,
               unixepoch(updated_at) AS updated_at_epoch
        FROM session_risk_scores
        WHERE tenant_id=? AND session_risk_score_id=?
          AND status IN ('monitoring','high_risk','frozen')
        """,
        (tenant_id, score_id),
    ).fetchone()


def _expire_score_row(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    now_epoch: int,
) -> SessionRiskSnapshot | None:
    """把已到期 monitoring/high-risk 行转成 append-only 历史并写 system action。

    参数：持有即时事务的连接、带 epoch 投影的活动行和当前时间。返回：转换前快照；尚未
    到期返回空。monitoring 对外成为 expired，曾达到 high-risk 的行成为 resolved，借此
    保留 ``first_high_risk_at`` 证据并释放活动 scope 唯一索引。
    """

    snapshot = _snapshot_from_row(row)
    if not _snapshot_window_expired(snapshot, now_epoch):
        return None
    if snapshot.state == "monitoring":
        status_after = "expired"
    elif snapshot.state == "high_risk":
        status_after = "resolved"
    # frozen 或未知状态不允许走窗口到期路径，避免定时逻辑自动解除安全冻结。
    else:  # pragma: no cover - 上方状态谓词已经排除 frozen/未知值。
        raise SessionRiskContractError()
    updated_at_epoch = int(row["updated_at_epoch"])
    occurred_at = _epoch_to_db_time(max(now_epoch, updated_at_epoch))
    next_revision = snapshot.revision + 1
    cursor = connection.execute(
        """
        UPDATE session_risk_scores
        SET status=?,state_revision=?,window_expires_at=NULL,resolved_at=?,updated_at=?
        WHERE tenant_id=? AND session_risk_score_id=? AND state_revision=? AND status=?
          AND window_expires_at IS NOT NULL AND unixepoch(window_expires_at)<=?
        """,
        (
            status_after,
            next_revision,
            occurred_at,
            occurred_at,
            str(row["tenant_id"]),
            snapshot.score_id,
            snapshot.revision,
            snapshot.state,
            now_epoch,
        ),
    )
    # 过期归档同样采用 revision CAS，未命中表示并发请求已推进状态且当前事务必须停止。
    if cursor.rowcount != 1:
        raise SessionRiskContractError()
    connection.execute(
        """
        INSERT INTO session_risk_actions(
          session_risk_action_id,tenant_id,session_risk_score_id,session_risk_freeze_id,
          action,reason_code,actor_type,actor_id,status_before,status_after,
          score_snapshot,state_revision,source_request_id,source_trace_id,
          digest_key_version,idempotency_key_digest,intent_digest,idempotency_expires_at,
          created_at
        ) VALUES(?,?,?,NULL,'expire','window_expired','system','session-risk-engine',
                 ?,?,?,?,?,?,NULL,NULL,NULL,NULL,?)
        """,
        (
            f"sra_{uuid4().hex}",
            str(row["tenant_id"]),
            snapshot.score_id,
            snapshot.state,
            status_after,
            snapshot.score,
            next_revision,
            str(row["last_source_request_id"]),
            str(row["last_source_trace_id"]),
            occurred_at,
        ),
    )
    return snapshot


def _select_durable_identity(
    connection: sqlite3.Connection,
    candidates: tuple[SessionRiskRequestIdentity, ...],
) -> tuple[SessionRiskRequestIdentity | None, SessionRiskSnapshot | None]:
    """用 current/previous 候选定位活动 cycle；无历史时选择 current。

    查询条件从候选的认证 tenant/Key 得到，客户端不能提供 tenant。若多个候选同时命中，说明
    轮换期间出现旁路 cycle，必须失败关闭，不能猜测一个继续写入。
    """

    if not candidates:
        return None, None
    first = candidates[0]
    matches: list[tuple[SessionRiskRequestIdentity, SessionRiskSnapshot]] = []
    for candidate in candidates:
        # 每个候选最多一个 active cycle 由迁移部分唯一索引保证；仍显式 LIMIT 2 检测损坏库。
        rows = connection.execute(
            """
            SELECT session_risk_score_id,score,risky_turn_count,jailbreak_count,
                   sensitive_count,tool_inducement_count,status,state_revision,
                   unixepoch(window_started_at) AS window_started_at_epoch,
                   unixepoch(last_risk_at) AS last_risk_at_epoch,
                   unixepoch(window_expires_at) AS expires_at_epoch,
                   hmac_key_version,threshold_version
            FROM session_risk_scores
            WHERE tenant_id=? AND virtual_key_id=? AND scope_type=? AND scope_key=?
              AND hmac_key_version=? AND status IN ('monitoring','high_risk','frozen')
            ORDER BY state_revision DESC
            LIMIT 2
            """,
            (
                candidate.scope.tenant_id,
                candidate.scope.virtual_key_id,
                candidate.scope.scope_type,
                candidate.scope.scope_key,
                candidate.scope.hmac_key_version,
            ),
        ).fetchall()
        # 单个 HMAC 候选最多对应一个活动周期，多行说明唯一约束或迁移事实已损坏。
        if len(rows) > 1:
            raise ValueError("multiple active session risk cycles")
        if rows:
            matches.append((candidate, _snapshot_from_row(rows[0])))
    # current 与 previous 同时存在活动周期时无法安全选主，必须阻止密钥轮换旁路。
    if len(matches) > 1:
        raise ValueError("multiple hmac candidates are active")
    if matches:
        return matches[0]
    return first, None


def _raise_if_frozen(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    virtual_key_id: str,
    identity: SessionRiskRequestIdentity | None,
) -> None:
    """按 Virtual Key 或当前会话覆盖层执行租户优先的冻结点查。

    参数中的租户和 Key 来自已认证上下文；命中后抛出固定业务错误，且检查发生在读取正文和调用
    Provider 之前，防止冻结会话继续产生敏感处理或费用。
    """

    scope_type = identity.scope.scope_type if identity is not None else ""
    scope_key = identity.scope.scope_key if identity is not None else ""
    row = connection.execute(
        """
        SELECT target_type FROM session_risk_freezes
        WHERE tenant_id=? AND virtual_key_id=? AND status='active'
          AND (
            target_type='virtual_key'
            OR (
              target_type='session' AND target_scope_type=? AND target_scope_key=?
            )
          )
        ORDER BY CASE target_type WHEN 'virtual_key' THEN 0 ELSE 1 END
        LIMIT 1
        """,
        (tenant_id, virtual_key_id, scope_type, scope_key),
    ).fetchone()
    if row is not None:
        # 不返回 target/source 细节，避免公开 API 成为其他 session 风险状态枚举器。
        error = ApiError("gateway.session_risk_frozen")
        # 固定内部原因只进入 RequestLog；公开响应仍使用同一错误，不能暴露冻结目标类型。
        error.session_risk_reason_code = (
            "key_frozen" if row["target_type"] == "virtual_key" else "session_frozen"
        )
        # 抛出的统一错误只附带固定 RequestLog 原因，不暴露命中的覆盖层或会话摘要。
        raise error


def _snapshot_from_row(row: sqlite3.Row) -> SessionRiskSnapshot:
    """把持久 score 精确转换为 Redis 恢复快照，拒绝 NULL 时间或非法状态。"""

    expires = row["expires_at_epoch"]
    state = str(row["status"])
    # 快照字段逐项来自同一租户权威行，Shared 模型会再次拒绝非法状态、计数和版本。
    return SessionRiskSnapshot(
        score_id=str(row["session_risk_score_id"]),
        score=int(row["score"]),
        risky_turn_count=int(row["risky_turn_count"]),
        jailbreak_count=int(row["jailbreak_count"]),
        sensitive_count=int(row["sensitive_count"]),
        tool_inducement_count=int(row["tool_inducement_count"]),
        state=state,  # type: ignore[arg-type]
        revision=int(row["state_revision"]),
        window_started_at_epoch=int(row["window_started_at_epoch"]),
        last_risk_at_epoch=int(row["last_risk_at_epoch"]),
        expires_at_epoch=None if state == "frozen" else int(expires),
        hmac_key_version=int(row["hmac_key_version"]),
        threshold_version=str(row["threshold_version"]),
    )


def _validate_hot_against_durable(
    hot: SessionRiskSnapshot | None,
    durable: SessionRiskSnapshot | None,
) -> None:
    """保证 ACK 后 Redis/DB 至少 revision 一致且关键字段逐项相等。"""

    # 两侧均无活动周期表示干净状态，只有这一种缺失组合可以继续请求。
    if hot is None and durable is None:
        return
    # 仅一侧存在时无法证明 Redis 与数据库一致，必须失败关闭等待对账恢复。
    if hot is None or durable is None:
        raise SessionRiskStoreUnavailable("session risk evidence unavailable")
    # 两侧快照任一字段不同都可能改变冻结结论，不能仅比较分数或 revision。
    if hot != durable:
        raise SessionRiskStoreUnavailable("session risk evidence unavailable")


def _log_preflight_failure(
    auth_context: GatewayAuthContext,
    error_code: str,
    exc: Exception,
) -> None:
    """记录预检失败，只包含认证 owner、固定码和异常类型。"""

    logger.warning(
        "security.session_risk_preflight_failed",
        extra={
            "event": "security.session_risk_preflight_failed",
            "tenant_id": auth_context.tenant_id,
            "actor_id": auth_context.virtual_key_id,
            "object_id": auth_context.virtual_key_id,
            "error_code": error_code,
            "payload": {"exception_type": exc.__class__.__name__},
        },
    )


def _log_degraded(auth_context: GatewayAuthContext, exc: Exception) -> None:
    """记录 internal_only Redis 降级，不输出 URL、摘要或异常消息。"""

    logger.warning(
        "security.session_risk_degraded",
        extra={
            "event": "security.session_risk_degraded",
            "tenant_id": auth_context.tenant_id,
            "actor_id": auth_context.virtual_key_id,
            "object_id": auth_context.virtual_key_id,
            "payload": {
                "reason": "store_unavailable",
                "exception_type": exc.__class__.__name__,
            },
        },
    )


__all__ = [
    "SessionRiskPreflightResult",
    "SessionRiskTurnResult",
    "get_session_risk_store",
    "preflight_session_risk",
    "record_session_risk_turn",
]
