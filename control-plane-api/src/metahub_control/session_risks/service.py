"""PRD-070 高危会话查询、处置、审计与事件事务。

数据库是冻结 overlay 和报表的权威来源。所有查询均以认证 ``tenant_id`` 为首条件；
人工动作在 ``BEGIN IMMEDIATE`` 内重新验证 read/manage 权限，并原子写 score、freeze、
append-only action、source event 与 Audit。原始 Session/Turn Header、Prompt、Response、
幂等键和完整 API scope HMAC 不进入响应、Webhook、Audit 或日志。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from metahub_gateway.session_risk.models import (
    SessionRiskPendingEvidence,
    SessionRiskSnapshot,
    SessionRiskStoreUnavailable,
)
from metahub_gateway.session_risk.store import RedisSessionRiskStore, SessionRiskStore
from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import WebhookEvent, build_webhook_event
from metahub_shared.session_risk.hmac import (
    ACTION_IDEMPOTENCY_HMAC_DOMAIN,
    ACTION_INTENT_HMAC_DOMAIN,
    SessionRiskHmacKey,
    SessionRiskHmacKeyring,
    derive_owner_slot,
)
from metahub_shared.session_risk.models import SessionRiskContractError, SessionRiskScope

from metahub_control.audit import (
    AuditRecord,
    AuditRequestMeta,
    commit_audited_transaction,
    dispatch_audit_recorded,
    record_audit_log,
)
from metahub_control.auth import AuthContext
from metahub_control.auth.resource_transactions import ResourceWriteTransaction
from metahub_control.webhooks.service import dispatch_webhook_event

from .models import (
    SessionRiskActionInfo,
    SessionRiskActionRequest,
    SessionRiskActionResponse,
    SessionRiskFreezeInfo,
    SessionRiskInfo,
    SessionRiskListResponse,
    SessionRiskSignalCounts,
    SessionRiskSummary,
)

logger = logging.getLogger("metahub.control.session_risk")

# 资源格式、状态矩阵、分页和签名域共同冻结 PRD-070 的公开行为。
SCORE_ID_PATTERN = re.compile(r"^srs_[0-9a-f]{32}$")
VALID_QUERY_STATUSES = frozenset({"high_risk", "frozen", "resolved", "expired"})
DEFAULT_QUERY_STATUSES = ("high_risk", "frozen")
VALID_SCOPE_TYPES = frozenset({"enterprise_chat", "api_session"})
VALID_ACTIONS = frozenset(
    {"freeze_session", "freeze_virtual_key", "dismiss", "release_session", "release_virtual_key"}
)
ACTION_REASON_MATRIX = {
    "freeze_session": frozenset({"confirmed_attack"}),
    "freeze_virtual_key": frozenset({"confirmed_attack"}),
    "dismiss": frozenset({"false_positive", "verified_safe"}),
    "release_session": frozenset({"false_positive", "incident_contained"}),
    "release_virtual_key": frozenset({"false_positive", "incident_contained"}),
}
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
CURSOR_VERSION = 1
CURSOR_DOMAIN = b"metahub-session-risk-cursor-v1"
IDEMPOTENCY_TTL = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class _ListFilters:
    """规范化的 tenant 报表筛选；同一对象也参与 cursor 签名绑定。"""

    statuses: tuple[str, ...]
    scope_type: str | None
    virtual_key_id: str | None
    from_time: datetime | None
    to_time: datetime | None
    limit: int


@dataclass(frozen=True, slots=True)
class _CursorState:
    """已验签的稳定快照和下一页 keyset 边界。"""

    snapshot_time: datetime
    before_time: datetime
    before_id: str


@dataclass(frozen=True, slots=True)
class SessionRiskCommittedEffects:
    """只包含已提交安全事实的提交后副作用载体。"""

    context: AuthContext
    audit_record: AuditRecord | None
    webhook_event: WebhookEvent


@dataclass(frozen=True, slots=True)
class SessionRiskStoreSyncIntent:
    """描述一次已提交动作必须在 Redis 完成的最终对账。

    ``snapshot`` 仅用于仍处于 frozen 的 cycle；resolved 动作使用空快照，按实时读取到的
    score/revision 执行 CAS reset。scope 中的 API HMAC 只驻留在进程内，禁止日志化。
    """

    scope: SessionRiskScope
    score_id: str
    authoritative_revision: int
    snapshot: SessionRiskSnapshot | None


def list_session_risks(
    config: AppConfig,
    context: AuthContext,
    *,
    status: str | None,
    scope_type: str | None,
    virtual_key_id: str | None,
    from_time: str | None,
    to_time: str | None,
    limit: str | None,
    cursor: str | None,
    request_id: str,
    trace_id: str,
    now: datetime | None = None,
) -> SessionRiskListResponse:
    """返回当前认证租户的高危会话快照页和全局状态摘要。

    参数：运行配置、可信认证上下文、白名单筛选与内部链路 ID。返回：安全 DTO。
    关键约束：跨租户数据从 SQL 条件起即不可见；GET 不触发检测、动作或 Webhook。
    """

    current = _service_now(now)
    filters = _normalize_filters(
        status=status,
        scope_type=scope_type,
        virtual_key_id=virtual_key_id,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
    )
    filter_digest = _filter_digest(filters)
    connection = _connect(config)
    try:
        transaction = ResourceWriteTransaction(
            connection, config, context, "session_risk.read", read_only=True
        )
        transaction.begin()
        context = transaction.context
        _validate_virtual_key_owner(connection, context.user.tenant_id, filters.virtual_key_id)
        if cursor is not None:
            cursor_state = _decode_cursor(
                config, cursor, context.user.tenant_id, filter_digest
            )
        else:
            latest = connection.execute(
                "SELECT MAX(updated_at) AS value FROM session_risk_scores WHERE tenant_id=?",
                (context.user.tenant_id,),
            ).fetchone()
            latest_time = (
                _parse_db_time(latest["value"])
                if latest is not None and latest["value"] is not None
                else current
            )
            # 多个动作可能在同一真实秒内用单调 row version 推进到 now+1；快照必须覆盖
            # 已提交事实，否则管理员动作成功后会在下一次 GET 中短暂消失。
            cursor_state = _CursorState(
                snapshot_time=max(current, latest_time),
                before_time=datetime.max.replace(tzinfo=UTC),
                before_id="srs_" + "f" * 32,
            )
        where_sql, params = _list_where(
            context.user.tenant_id,
            filters,
            cursor_state,
        )
        rows = connection.execute(
            f"""
            SELECT score.*
            FROM session_risk_scores score
            WHERE {where_sql}
            ORDER BY score.last_risk_at DESC, score.session_risk_score_id DESC
            LIMIT ?
            """,
            [*params, filters.limit + 1],
        ).fetchall()
        visible = rows[: filters.limit]
        has_more = len(rows) > filters.limit
        items = [_risk_info(connection, row) for row in visible]
        next_cursor = None
        if has_more and visible:
            next_cursor = _encode_cursor(
                config,
                context.user.tenant_id,
                filter_digest,
                snapshot_time=cursor_state.snapshot_time,
                before_time=_parse_db_time(visible[-1]["last_risk_at"]),
                before_id=_stored_score_id(visible[-1]["session_risk_score_id"]),
            )
        response = SessionRiskListResponse(
            runtime_status=_runtime_status(config),
            summary=_summary(connection, context.user.tenant_id),
            session_risks=items,
            next_cursor=next_cursor,
            request_id=request_id,
            trace_id=trace_id,
        )
        transaction.commit()
        return response
    # 已归类业务错误保持原统一错误码，回滚只清理当前只读事务且不得暴露数据库细节。
    except ApiError:
        _rollback_safely(connection)
        raise
    # 数据形状或数据库读取异常统一折叠为证据不可用，防止底层路径和原始字段进入响应。
    except (sqlite3.Error, TypeError, ValueError) as exc:
        _rollback_safely(connection)
        raise ApiError("session_risk.evidence_unavailable") from exc
    finally:
        connection.close()


def apply_session_risk_action(
    config: AppConfig,
    context: AuthContext,
    session_risk_score_id: str,
    payload: SessionRiskActionRequest,
    *,
    idempotency_key: str,
    audit_request_meta: AuditRequestMeta,
    request_id: str,
    trace_id: str,
    now: datetime | None = None,
) -> tuple[
    SessionRiskActionResponse,
    SessionRiskCommittedEffects | None,
    SessionRiskStoreSyncIntent,
]:
    """原子执行人工冻结、释放或误报处置，并支持 24 小时 HMAC 幂等恢复。

    参数：配置、可信 actor、score ID、严格动作、仅驻留内存的幂等键、审计来源和链路 ID。
    返回：响应、首次提交时的安全副作用与每次都要执行的 Redis 对账意图；幂等 replay
    不重复写 Audit、事件或 Webhook，但必须继续修复上次不确定的 Store 同步。
    """

    normalized_id = _normalize_score_id(session_risk_score_id)
    canonical = _canonical_action(payload)
    current = _service_now(now)
    key_candidates = _action_digest_candidates(
        config,
        domain=ACTION_IDEMPOTENCY_HMAC_DOMAIN,
        parts=(context.user.tenant_id, context.user.user_id, idempotency_key),
    )
    # 缺失当前幂等 HMAC 密钥时无法证明动作归属，必须在打开写事务前失败关闭。
    if not key_candidates:
        raise ApiError("session_risk.store_unavailable")
    intent_candidates = {
        version: digest
        for version, digest in _action_digest_candidates(
            config,
            domain=ACTION_INTENT_HMAC_DOMAIN,
            parts=(
                context.user.tenant_id,
                context.user.user_id,
                normalized_id,
                json.dumps(canonical, sort_keys=True, separators=(",", ":")),
            ),
        )
    }
    current_version, current_key_digest = key_candidates[0]
    current_intent_digest = intent_candidates[current_version]
    connection = _connect(config)
    audit_record: AuditRecord | None = None
    webhook_event: WebhookEvent | None = None
    try:
        # Authenticate the actual Cookie or legacy session in the same transaction.
        # Both permissions remain mandatory, including on idempotent replay.
        transaction = ResourceWriteTransaction(
            connection, config, context, "session_risk.read",
            additional_permissions=("session_risk.manage",),
        )
        transaction.begin()
        context = transaction.context
        replay = _load_action_replay(
            connection,
            context.user.tenant_id,
            context.user.user_id,
            key_candidates,
            current,
        )
        # 重放只能读取同租户同执行人的既有动作，且必须重新验证本次意图摘要完全一致。
        if replay is not None:
            version = _positive_integer(replay["digest_key_version"])
            expected_intent = intent_candidates.get(version)
            # 轮换后缺少对应版本或摘要不同都表示同一幂等键被复用于另一动作，固定返回冲突。
            if expected_intent is None or not hmac.compare_digest(
                str(replay["intent_digest"]), expected_intent
            ):
                # 摘要不一致时拒绝复用原动作，避免相同幂等键跨处置意图覆盖安全事实。
                raise ApiError("session_risk.idempotency_conflict")
            replay_score = _load_score(
                connection,
                context.user.tenant_id,
                str(replay["session_risk_score_id"]),
            )
            # 幂等动作引用的租户内风险事实若已损坏，不能拼装不完整响应或改从其他租户查找。
            if replay_score is None:
                raise ApiError("session_risk.evidence_unavailable")
            response = SessionRiskActionResponse(
                session_risk=_risk_info(connection, replay_score),
                action=_action_info(replay),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            sync_intent = _build_store_sync_intent(config, replay_score)
            transaction.commit()
            return response, None, sync_intent

        # 对象始终按认证租户读取；不存在与跨租户使用相同错误，随后用 revision 阻止覆盖并发事实。
        score = _load_score(connection, context.user.tenant_id, normalized_id)
        if score is None:
            raise ApiError("session_risk.not_found")
        # revision 必须与客户端已读快照一致，过期动作不得覆盖自动冻结或其他管理员处置。
        if _positive_integer(score["state_revision"]) != canonical["expected_revision"]:
            raise ApiError("session_risk.state_conflict")
        result = _execute_action(
            connection,
            context,
            score,
            canonical,
            digest_key_version=current_version,
            idempotency_key_digest=current_key_digest,
            intent_digest=current_intent_digest,
            expires_at=current + IDEMPOTENCY_TTL,
            request_id=request_id,
            trace_id=trace_id,
            now=current,
        )
        refreshed = _load_score(connection, context.user.tenant_id, normalized_id)
        # 动作写入后必须在同事务重读权威行，缺失说明证据链断裂且不得提交 Audit 或事件。
        if refreshed is None:
            raise ApiError("session_risk.evidence_unavailable")
        action_row, event_row, audit_before, audit_after, audit_action = result
        audit_record = record_audit_log(
            connection,
            context,
            action=audit_action,
            object_type="session_risk",
            object_id=normalized_id,
            object_name=str(score["scope_type"]),
            before_summary=audit_before,
            after_summary=audit_after,
            request_meta=audit_request_meta,
            request_id=request_id,
            trace_id=trace_id,
            # Action、source event 与 Audit 必须共享同一服务端秒级时间，数据库 owner
            # trigger 才能证明三者属于同一原子处置，而不是跨秒拼接的相似事实。
            created_at=current,
        )
        webhook_event = _build_event(refreshed, action_row, event_row)
        response = SessionRiskActionResponse(
            session_risk=_risk_info(connection, refreshed),
            action=_action_info(action_row),
            idempotent_replay=False,
            request_id=request_id,
            trace_id=trace_id,
        )
        sync_intent = _build_store_sync_intent(config, refreshed)
        transaction.commit(commit_audited_transaction)
    # 业务拒绝已携带稳定错误码，只回滚本事务并保持跨租户同形语义。
    except ApiError:
        _rollback_safely(connection)
        raise
    # 唯一键或状态触发器冲突代表并发事实已获胜，转换为稳定状态冲突而不泄露 SQL 文本。
    except sqlite3.IntegrityError as exc:
        _rollback_safely(connection)
        raise ApiError("session_risk.state_conflict") from exc
    # 其他数据库错误意味着无法证明完整证据链，统一失败关闭且不返回驱动异常正文。
    except sqlite3.Error as exc:
        _rollback_safely(connection)
        raise ApiError("session_risk.evidence_unavailable") from exc
    finally:
        connection.close()

    # 首次动作必须产生同事务来源事件；缺失时即使响应对象存在也不能宣称动作已完整提交。
    if webhook_event is None:
        raise ApiError("session_risk.evidence_unavailable")
    return (
        response,
        SessionRiskCommittedEffects(
            context=context,
            audit_record=audit_record,
            webhook_event=webhook_event,
        ),
        sync_intent,
    )


def get_session_risk_store(config: AppConfig, app_state: object) -> SessionRiskStore:
    """返回 Control 测试注入 Store 或与 Gateway 同合同的生产 Redis Store。

    参数：运行配置与 FastAPI app.state。返回：异步 Store。关键约束：生产环境不允许退化
    为进程内 Store；Redis URL 仅进入 Store 私有连接配置，不进入异常或日志。
    """

    injected = getattr(app_state, "session_risk_store", None)
    # 注入实现只用于应用生命周期内的显式测试或装配，调用方承担其同一接口合同。
    if injected is not None:
        return injected
    # Control 需要 Redis 完成冻结对账；未配置连接时不能把数据库动作伪装成完全成功。
    if not config.redis_url:
        raise ApiError("session_risk.store_unavailable")
    cached = getattr(app_state, "_metahub_session_risk_store", None)
    # 仅复用已验证类型的进程缓存，避免任意动态属性绕过生产 Store 的安全边界。
    if isinstance(cached, RedisSessionRiskStore):
        return cached
    try:
        store = RedisSessionRiskStore(config.redis_url)
    # URL、凭据或协议配置错误只转换为固定业务错误，原始连接信息不得进入异常链。
    except SessionRiskStoreUnavailable as exc:
        raise ApiError("session_risk.store_unavailable") from exc
    app_state._metahub_session_risk_store = store
    return store


async def ensure_session_risk_store_available(store: SessionRiskStore) -> None:
    """在数据库动作前验证 Redis 可达，避免已知不可用时制造新的半完成动作。

    参数：已选择的生产或测试 Store。返回：无。连接、认证、协议及注入故障全部折叠为
    统一错误码，取消信号仍交还 ASGI，不把客户端断连伪装成依赖故障。
    """

    try:
        # 动作事务开始前必须确认 Store 可响应，避免新增一个当前已知无法清偿的跨存储债务。
        if not await store.ping():
            raise SessionRiskStoreUnavailable("session risk store unavailable")
    # pending 与连接故障都意味着不能证明热状态可对账，统一阻断而不暴露 Redis 细节。
    except (SessionRiskPendingEvidence, SessionRiskStoreUnavailable) as exc:
        raise ApiError("session_risk.store_unavailable") from exc


async def synchronize_session_risk_store(
    store: SessionRiskStore,
    intent: SessionRiskStoreSyncIntent,
) -> None:
    """把已提交数据库状态与 Redis 热 cycle 做可证明的 CAS 对账。

    frozen 状态使用 revision 只增 restore 后逐字段复核；resolved 状态先读取实际热 revision，
    再精确 reset 并复读证明旧 score 已消失。任何 pending、竞态或损坏状态都返回 503，调用方
    可用原幂等键恢复重试，绝不能向管理员返回虚假成功。
    """

    try:
        hot = await store.load(intent.scope)
        # frozen 动作必须把数据库权威快照恢复到 Redis 并复读验证，不能仅相信写调用返回。
        if intent.snapshot is not None:
            await store.restore(intent.scope, intent.snapshot)
            verified = await store.load(intent.scope)
            # 复读不一致表示 Redis 数据竞争或协议损坏，不能把未生效冻结报告为成功。
            if verified != intent.snapshot:
                raise SessionRiskStoreUnavailable("session risk store unavailable")
            return

        # Redis 中已经没有旧 cycle，或同一 owner 已进入另一个 score，均说明本动作的旧
        # cycle 不再可执行；不能为了回放旧动作删除后续新 cycle。
        if hot is None or hot.score_id != intent.score_id:
            return
        # Redis 修订领先于数据库动作说明出现新事实，旧的释放动作不得覆盖它。
        if hot.revision > intent.authoritative_revision:
            raise SessionRiskStoreUnavailable("session risk store unavailable")
        await store.reset(intent.scope, intent.score_id, hot.revision)
        verified = await store.load(intent.scope)
        # 精确 reset 后仍能读到同一风险周期即表示释放未完成，必须保留失败以便管理员重试。
        if verified is not None and verified.score_id == intent.score_id:
            raise SessionRiskStoreUnavailable("session risk store unavailable")
    # 所有对账失败都转换为统一 Store 错误；数据库事实保留并允许相同幂等动作恢复。
    except (SessionRiskPendingEvidence, SessionRiskStoreUnavailable) as exc:
        raise ApiError("session_risk.store_unavailable") from exc


def dispatch_session_risk_effects_safely(
    config: AppConfig,
    effects: SessionRiskCommittedEffects,
    *,
    request_id: str,
    trace_id: str,
) -> None:
    """提交后尽力投递审计和风险 Webhook，外部失败不得改写动作结果。

    参数：配置、已提交安全事实与链路 ID。返回：无。异常日志只含稳定资源 ID 与固定码，
    不记录 Endpoint、payload、完整 API scope、幂等键或底层异常文本。
    """

    dispatch_audit_recorded(
        config,
        effects.context,
        effects.audit_record,
        request_id=request_id,
        trace_id=trace_id,
    )
    try:
        dispatch_webhook_event(
            config,
            effects.webhook_event.tenant_id,
            effects.webhook_event,
            request_id=request_id,
            trace_id=trace_id,
        )
    except Exception:
        logger.warning(
            "session_risk.webhook_dispatch_failed",
            extra={
                "event": "session_risk.webhook_dispatch_failed",
                "tenant_id": effects.webhook_event.tenant_id,
                "object_id": effects.webhook_event.object_id,
                "request_id": request_id,
                "trace_id": trace_id,
                "error_code": "webhook.delivery_unavailable",
            },
        )


def _execute_action(
    connection: sqlite3.Connection,
    context: AuthContext,
    score: sqlite3.Row,
    canonical: dict[str, object],
    *,
    digest_key_version: int,
    idempotency_key_digest: str,
    intent_digest: str,
    expires_at: datetime,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> tuple[sqlite3.Row, sqlite3.Row, dict[str, object], dict[str, object], str]:
    """在已加锁事务中执行一次首次动作并追加 action/source event。

    返回：刚写入的 action/event、Audit 前后摘要和固定审计动作。调用者负责同事务写
    Audit 与提交。任何目标都从 score 派生，客户端不能指定任意 Session 或 Virtual Key。
    """

    action = str(canonical["action"])
    reason_code = str(canonical["reason_code"])
    status_before = _stored_status(score["status"], include_monitoring=True)
    score_id = _stored_score_id(score["session_risk_score_id"])
    tenant_id = context.user.tenant_id
    revision_before = _positive_integer(score["state_revision"])
    revision_after = revision_before + 1
    timestamp = _to_db_time(now)
    freeze_row: sqlite3.Row | None = None
    target_type = _action_target_type(action)

    # 会话冻结只能从已识别风险周期派生，目标由服务端 score 决定而不是客户端自由指定。
    if action == "freeze_session":
        # Key overlay 可能先于 Session overlay 建立；二者是独立来源事实，frozen 状态不能
        # 阻止补建尚不存在的另一类 overlay，重复目标由 _create_freeze 精确拒绝。
        if status_before not in {"high_risk", "frozen"}:
            raise ApiError("session_risk.state_conflict")
        freeze_row = _create_freeze(
            connection,
            context,
            score,
            target_type="session",
            request_id=request_id,
            trace_id=trace_id,
            timestamp=timestamp,
        )
        status_after = "frozen"
    # Key 冻结同样要求当前周期已高危，避免普通会话通过管理接口任意停用共享凭据。
    elif action == "freeze_virtual_key":
        # 非高危状态没有足够证据建立 Key 覆盖层，必须保持现有业务状态不变。
        if status_before not in {"high_risk", "frozen"}:
            raise ApiError("session_risk.state_conflict")
        freeze_row = _create_freeze(
            connection,
            context,
            score,
            target_type="virtual_key",
            request_id=request_id,
            trace_id=trace_id,
            timestamp=timestamp,
        )
        status_after = "frozen"
    # 误报处置只接受尚未冻结的高危周期，已有活动覆盖层必须先按目标逐一释放。
    elif action == "dismiss":
        if status_before != "high_risk" or _has_active_freeze(connection, tenant_id, score_id):
            raise ApiError("session_risk.state_conflict")
        status_after = "resolved"
    # 释放操作只作用于当前风险来源的对应覆盖层，不能顺带解除其他来源或另一目标类型。
    elif action in {"release_session", "release_virtual_key"}:
        # 只有 frozen 周期具备可释放覆盖层，其余状态返回冲突以保护状态机单向性。
        if status_before != "frozen":
            raise ApiError("session_risk.state_conflict")
        release_target = "session" if action == "release_session" else "virtual_key"
        freeze_row = _release_freeze(
            connection,
            context,
            score_id,
            release_target,
            request_id=request_id,
            trace_id=trace_id,
            timestamp=timestamp,
        )
        # 只释放本来源的一个 overlay；另一 target 或另一 score 对同 Key 的覆盖继续生效。
        status_after = (
            "frozen" if _has_active_freeze(connection, tenant_id, score_id) else "resolved"
        )
    # 模型层之外出现未知动作代表内部合同漂移，不做任何猜测性兼容或数据库写入。
    else:
        raise ApiError("session_risk.payload_invalid")

    frozen_at = score["frozen_at"]
    if status_after == "frozen" and frozen_at is None:
        frozen_at = timestamp
    resolved_at = timestamp if status_after == "resolved" else None
    updated_at = _monotonic_row_time(score["updated_at"], now)
    connection.execute(
        """
        UPDATE session_risk_scores
        SET status=?, state_revision=?, frozen_at=?, resolved_at=?, window_expires_at=NULL,
            updated_at=?
        WHERE tenant_id=? AND session_risk_score_id=? AND state_revision=? AND status=?
        """,
        (
            status_after,
            revision_after,
            frozen_at,
            resolved_at,
            updated_at,
            tenant_id,
            score_id,
            revision_before,
            status_before,
        ),
    )
    if connection.execute("SELECT changes() AS value").fetchone()["value"] != 1:
        # CAS 未命中说明并发自动冻结或管理员动作已改变当前状态，不能覆盖新事实。
        raise ApiError("session_risk.state_conflict")

    action_id = f"sra_{uuid4().hex}"
    freeze_id = str(freeze_row["session_risk_freeze_id"]) if freeze_row is not None else None
    connection.execute(
        """
        INSERT INTO session_risk_actions(
          session_risk_action_id,tenant_id,session_risk_score_id,session_risk_freeze_id,
          action,reason_code,actor_type,actor_id,status_before,status_after,score_snapshot,
          state_revision,source_request_id,source_trace_id,digest_key_version,
          idempotency_key_digest,intent_digest,idempotency_expires_at,created_at
        ) VALUES(?,?,?, ?,?,?, 'user',?, ?,?,?, ?,?,?, ?,?,?,?,?)
        """,
        (
            action_id,
            tenant_id,
            score_id,
            freeze_id,
            action,
            reason_code,
            context.user.user_id,
            status_before,
            status_after,
            _bounded_integer(score["score"], minimum=0, maximum=10_000),
            revision_after,
            request_id,
            trace_id,
            digest_key_version,
            idempotency_key_digest,
            intent_digest,
            _to_db_time(expires_at),
            timestamp,
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
        ) VALUES(?,?,?,?,?,'security.session_high_risk',?,?,?,?,?,?,?,?,?,?,?,?,
                 'user',?,?,?,?)
        """,
        (
            event_id,
            tenant_id,
            score_id,
            action_id,
            freeze_id,
            str(score["scope_type"]),
            str(score["scope_key"]),
            status_after,
            target_type,
            _bounded_integer(score["score"], minimum=0, maximum=10_000),
            _bounded_integer(score["score_scale"], minimum=10_000, maximum=10_000),
            _positive_integer(score["risky_turn_count"]),
            _non_negative_integer(score["jailbreak_count"]),
            _non_negative_integer(score["sensitive_count"]),
            _non_negative_integer(score["tool_inducement_count"]),
            revision_after,
            str(score["threshold_version"]),
            context.user.user_id,
            request_id,
            trace_id,
            timestamp,
        ),
    )
    action_row = connection.execute(
        "SELECT * FROM session_risk_actions WHERE tenant_id=? AND session_risk_action_id=?",
        (tenant_id, action_id),
    ).fetchone()
    event_row = connection.execute(
        "SELECT * FROM session_risk_events WHERE tenant_id=? AND session_risk_event_id=?",
        (tenant_id, event_id),
    ).fetchone()
    # 新动作和来源事件必须同时可见，否则事务证据不完整且不能继续生成 Audit 或 Webhook。
    if action_row is None or event_row is None:
        raise ApiError("session_risk.evidence_unavailable")
    release_action = action in {"release_session", "release_virtual_key"}
    audit_before = _audit_summary(
        score,
        status=status_before,
        revision=revision_before,
        freeze_id=freeze_id if release_action else None,
        target_type=target_type if release_action else None,
    )
    audit_after = _audit_summary(
        score,
        status=status_after,
        revision=revision_after,
        freeze_id=freeze_id,
        target_type=target_type,
    )
    # 返回对象均来自同一租户事务，调用方据此形成不可变 Audit 并一次性提交完整处置事实。
    return action_row, event_row, audit_before, audit_after, f"session_risk.{action}"


def _create_freeze(
    connection: sqlite3.Connection,
    context: AuthContext,
    score: sqlite3.Row,
    *,
    target_type: str,
    request_id: str,
    trace_id: str,
    timestamp: str,
) -> sqlite3.Row:
    """从租户限定的风险分数派生并创建独立冻结覆盖层。

    参数中的 score 必须已在同一事务内按当前认证租户读取；返回新建冻结行。独立覆盖层不会改写
    Virtual Key 或 ChatSession 原状态，因此常规启停操作不能绕过安全冻结。
    """

    tenant_id = context.user.tenant_id
    score_id = _stored_score_id(score["session_risk_score_id"])
    existing = connection.execute(
        """
        SELECT session_risk_freeze_id
        FROM session_risk_freezes
        WHERE tenant_id=? AND source_session_risk_score_id=?
          AND target_type=? AND status='active'
        """,
        (tenant_id, score_id, target_type),
    ).fetchone()
    # 同一来源和目标已有活动覆盖层时拒绝重复创建，保持冻结事实和审计动作一一对应。
    if existing is not None:
        raise ApiError("session_risk.target_already_frozen")
    # 目标范围只能由固定动作矩阵产生，两类覆盖层都保留原风险会话作为可追踪来源。
    if target_type in {"session", "virtual_key"}:
        # 两类 overlay 都保留来源会话 scope；实际 Key 目标由独立 virtual_key_id 表达和点查。
        # 这使 owner trigger 能证明 Key 冻结确实由当前 tenant 的已识别会话派生。
        target_scope_type = str(score["scope_type"])
        target_scope_key = str(score["scope_key"])
    # 未知目标类型不能降级为会话或 Key，否则可能把客户端污染值写入冻结索引。
    else:
        raise ApiError("session_risk.state_conflict")
    freeze_id = f"srf_{uuid4().hex}"
    connection.execute(
        """
        INSERT INTO session_risk_freezes(
          session_risk_freeze_id,tenant_id,source_session_risk_score_id,target_type,
          target_scope_type,target_scope_key,virtual_key_id,status,state_revision,
          created_by_type,created_by_id,released_by_type,released_by_id,
          created_source_request_id,created_source_trace_id,released_source_request_id,
          released_source_trace_id,created_at,released_at
        ) VALUES(?,?,?,?,?,?,?,'active',1,'user',?,NULL,NULL,?,?,NULL,NULL,?,NULL)
        """,
        (
            freeze_id,
            tenant_id,
            score_id,
            target_type,
            target_scope_type,
            target_scope_key,
            str(score["virtual_key_id"]),
            context.user.user_id,
            request_id,
            trace_id,
            timestamp,
        ),
    )
    row = connection.execute(
        "SELECT * FROM session_risk_freezes WHERE tenant_id=? AND session_risk_freeze_id=?",
        (tenant_id, freeze_id),
    ).fetchone()
    # 插入后必须按认证租户复读新冻结行，缺失时拒绝构造仅存在于内存的成功响应。
    if row is None:
        raise ApiError("session_risk.evidence_unavailable")
    return row


def _release_freeze(
    connection: sqlite3.Connection,
    context: AuthContext,
    score_id: str,
    target_type: str,
    *,
    request_id: str,
    trace_id: str,
    timestamp: str,
) -> sqlite3.Row:
    """仅释放当前风险来源对指定目标建立的活动冻结覆盖层。

    参数必须包含认证上下文、风险分数和目标类型；返回释放后的冻结行。其他风险分数建立的覆盖层
    继续有效，避免一次处置误解除同一 Virtual Key 上尚未关闭的独立风险事实。
    """

    row = connection.execute(
        """
        SELECT * FROM session_risk_freezes
        WHERE tenant_id=? AND source_session_risk_score_id=?
          AND target_type=? AND status='active'
        """,
        (context.user.tenant_id, score_id, target_type),
    ).fetchone()
    # 未找到当前来源的活动覆盖层时保持同形冲突，绝不转而释放其他风险周期的冻结。
    if row is None:
        raise ApiError("session_risk.state_conflict")
    revision = _positive_integer(row["state_revision"])
    connection.execute(
        """
        UPDATE session_risk_freezes
        SET status='released',state_revision=?,released_by_type='user',released_by_id=?,
            released_source_request_id=?,released_source_trace_id=?,released_at=?
        WHERE tenant_id=? AND session_risk_freeze_id=? AND status='active' AND state_revision=?
        """,
        (
            revision + 1,
            context.user.user_id,
            request_id,
            trace_id,
            timestamp,
            context.user.tenant_id,
            str(row["session_risk_freeze_id"]),
            revision,
        ),
    )
    # revision 条件更新必须恰好命中一行；并发处置获胜后旧动作不能覆盖新状态。
    if connection.execute("SELECT changes() AS value").fetchone()["value"] != 1:
        raise ApiError("session_risk.state_conflict")
    refreshed = connection.execute(
        "SELECT * FROM session_risk_freezes WHERE tenant_id=? AND session_risk_freeze_id=?",
        (context.user.tenant_id, str(row["session_risk_freeze_id"])),
    ).fetchone()
    # 释放后的持久行是响应和 Audit 的权威来源，复读失败按证据不可用关闭。
    if refreshed is None:
        raise ApiError("session_risk.evidence_unavailable")
    return refreshed


def _has_active_freeze(
    connection: sqlite3.Connection,
    tenant_id: str,
    score_id: str,
) -> bool:
    """判断当前来源 score 是否仍有任一 active overlay。"""

    return (
        connection.execute(
            """
            SELECT 1 FROM session_risk_freezes
            WHERE tenant_id=? AND source_session_risk_score_id=? AND status='active'
            LIMIT 1
            """,
            (tenant_id, score_id),
        ).fetchone()
        is not None
    )


def _load_action_replay(
    connection: sqlite3.Connection,
    tenant_id: str,
    actor_id: str,
    candidates: list[tuple[int, str]],
    now: datetime,
) -> sqlite3.Row | None:
    """按 current/previous HMAC 候选恢复仍在有效期内的 actor 级 action。"""

    if not candidates:
        return None
    placeholders = ",".join("?" for _ in candidates)
    return connection.execute(
        f"""
        SELECT * FROM session_risk_actions
        WHERE tenant_id=? AND actor_type='user' AND actor_id=?
          AND idempotency_key_digest IN ({placeholders})
          AND julianday(idempotency_expires_at)>julianday(?)
        ORDER BY created_at DESC LIMIT 1
        """,
        (tenant_id, actor_id, *(digest for _, digest in candidates), _to_db_time(now)),
    ).fetchone()


def _load_score(
    connection: sqlite3.Connection,
    tenant_id: str,
    score_id: str,
) -> sqlite3.Row | None:
    """使用 tenant-first 联合条件读取一条 score，跨租户与不存在同形。"""

    return connection.execute(
        "SELECT * FROM session_risk_scores WHERE tenant_id=? AND session_risk_score_id=?",
        (tenant_id, score_id),
    ).fetchone()


def _risk_info(connection: sqlite3.Connection, row: sqlite3.Row) -> SessionRiskInfo:
    """把一条持久 score 及同租户子事实严格物化为公开安全 DTO。"""

    tenant_id = _stored_required_text(row["tenant_id"], maximum=160)
    score_id = _stored_score_id(row["session_risk_score_id"])
    scope_type = _stored_scope_type(row["scope_type"])
    scope_key = _stored_required_text(row["scope_key"], maximum=256)
    freezes = connection.execute(
        """
        SELECT * FROM session_risk_freezes
        WHERE tenant_id=? AND source_session_risk_score_id=?
        ORDER BY created_at DESC, session_risk_freeze_id DESC
        """,
        (tenant_id, score_id),
    ).fetchall()
    actions = connection.execute(
        """
        SELECT * FROM session_risk_actions
        WHERE tenant_id=? AND session_risk_score_id=?
        ORDER BY created_at DESC, session_risk_action_id DESC
        """,
        (tenant_id, score_id),
    ).fetchall()
    # DTO 仅投影当前租户的安全摘要和可展示引用，不包含内部 scope HMAC 或原始会话 Header。
    return SessionRiskInfo(
        session_risk_score_id=score_id,
        scope_type=scope_type,
        scope_reference=_scope_reference(scope_type, scope_key, row["chat_session_id"]),
        virtual_key_id=_stored_virtual_key_id(row["virtual_key_id"]),
        status=_stored_status(row["status"]),
        score=_bounded_integer(row["score"], minimum=0, maximum=10_000),
        score_scale=_bounded_integer(row["score_scale"], minimum=10_000, maximum=10_000),
        risky_turn_count=_positive_integer(row["risky_turn_count"]),
        signal_counts=SessionRiskSignalCounts(
            jailbreak=_non_negative_integer(row["jailbreak_count"]),
            sensitive=_non_negative_integer(row["sensitive_count"]),
            tool_inducement=_non_negative_integer(row["tool_inducement_count"]),
        ),
        state_revision=_positive_integer(row["state_revision"]),
        threshold_version=_stored_required_text(row["threshold_version"], maximum=128),
        last_source_request_id=_stored_required_text(
            row["last_source_request_id"], maximum=160
        ),
        window_expires_at=_parse_optional_db_time(row["window_expires_at"]),
        first_high_risk_at=_parse_optional_db_time(row["first_high_risk_at"]),
        frozen_at=_parse_optional_db_time(row["frozen_at"]),
        updated_at=_parse_db_time(row["updated_at"]),
        freezes=[_freeze_info(item) for item in freezes],
        actions=[_action_info(item) for item in actions],
    )


def _freeze_info(row: sqlite3.Row) -> SessionRiskFreezeInfo:
    """解码一条冻结覆盖，不返回 target scope 原文或创建者内部字段。"""

    freeze_id = _stored_prefixed_id(row["session_risk_freeze_id"], "srf_")
    target_type = str(row["target_type"])
    status = str(row["status"])
    # 持久冻结类型和状态必须落在公开白名单，未知值按证据损坏处理而不是透传到 Console。
    if target_type not in {"session", "virtual_key"} or status not in {"active", "released"}:
        raise ApiError("session_risk.evidence_unavailable")
    # 返回冻结摘要前已验证 ID、目标和时间字段，避免页面依据损坏事实开放错误动作。
    return SessionRiskFreezeInfo(
        session_risk_freeze_id=freeze_id,
        target_type=target_type,
        status=status,
        state_revision=_positive_integer(row["state_revision"]),
        created_at=_parse_db_time(row["created_at"]),
        released_at=_parse_optional_db_time(row["released_at"]),
    )


def _action_info(row: sqlite3.Row) -> SessionRiskActionInfo:
    """解码一条不可变动作；幂等 HMAC 与 trace 不进入返回对象。"""

    action = str(row["action"])
    reason = str(row["reason_code"])
    actor_type = str(row["actor_type"])
    allowed_actions = {
        "high_risk",
        "freeze_session",
        "freeze_virtual_key",
        "dismiss",
        "release_session",
        "release_virtual_key",
        "expire",
    }
    allowed_reasons = {
        "threshold_reached",
        "confirmed_attack",
        "false_positive",
        "verified_safe",
        "incident_contained",
        "window_expired",
    }
    # 动作、原因和执行主体均必须来自固定状态机，防止历史脏值进入 API、Audit 或页面 DOM。
    if (
        action not in allowed_actions
        or reason not in allowed_reasons
        or actor_type not in {"system", "user"}
    ):
        # 任一枚举越界都表示持久动作证据损坏，不能透传未知值或猜测兼容。
        raise ApiError("session_risk.evidence_unavailable")
    freeze_value = row["session_risk_freeze_id"]
    # 动作 DTO 只返回稳定标识和处置摘要，不返回幂等 HMAC、意图摘要或内部请求载荷。
    return SessionRiskActionInfo(
        session_risk_action_id=_stored_prefixed_id(row["session_risk_action_id"], "sra_"),
        action=action,
        reason_code=reason,
        actor_type=actor_type,
        actor_reference=(
            "system"
            if actor_type == "system"
            else _stored_required_text(row["actor_id"], maximum=160)
        ),
        session_risk_freeze_id=(
            _stored_prefixed_id(freeze_value, "srf_") if freeze_value is not None else None
        ),
        status_before=_stored_status(row["status_before"], include_monitoring=True),
        status_after=_stored_status(row["status_after"], include_monitoring=True),
        score_snapshot=_bounded_integer(row["score_snapshot"], minimum=0, maximum=10_000),
        state_revision=_positive_integer(row["state_revision"]),
        source_request_id=_stored_required_text(row["source_request_id"], maximum=160),
        created_at=_parse_db_time(row["created_at"]),
    )


def _summary(connection: sqlite3.Connection, tenant_id: str) -> SessionRiskSummary:
    """聚合当前租户报表状态，历史 monitoring 不计入 PAGE-098 摘要。"""

    rows = connection.execute(
        """
        SELECT status,COUNT(*) AS value
        FROM session_risk_scores
        WHERE tenant_id=? AND status IN ('high_risk','frozen','resolved')
        GROUP BY status
        """,
        (tenant_id,),
    ).fetchall()
    counts = {str(row["status"]): _non_negative_integer(row["value"]) for row in rows}
    # 汇总只基于已带 tenant_id 条件的分组结果，缺失状态按零处理且不会读取全局计数。
    return SessionRiskSummary(
        high_risk_count=counts.get("high_risk", 0),
        frozen_count=counts.get("frozen", 0),
        resolved_count=counts.get("resolved", 0),
    )


def _build_event(
    score: sqlite3.Row,
    action: sqlite3.Row,
    event: sqlite3.Row,
) -> WebhookEvent:
    """从同事务已提交前的数据库行构造 exact 安全 Webhook 事件。"""

    scope_type = _stored_scope_type(score["scope_type"])
    scope_reference = _scope_reference(
        scope_type,
        str(score["scope_key"]),
        score["chat_session_id"],
    )
    freeze_value = event["session_risk_freeze_id"]
    target_type = event["target_type"]
    # Webhook 目标类型必须与数据库状态机一致，未知值不能作为字符串原样发送给租户端点。
    if target_type is not None and target_type not in {"session", "virtual_key"}:
        raise ApiError("session_risk.evidence_unavailable")
    payload = {
        "schema_version": 1,
        "session_risk_score_id": _stored_score_id(score["session_risk_score_id"]),
        "session_risk_action_id": _stored_prefixed_id(
            action["session_risk_action_id"], "sra_"
        ),
        "session_risk_freeze_id": (
            _stored_prefixed_id(freeze_value, "srf_") if freeze_value is not None else None
        ),
        "scope_type": scope_type,
        "scope_reference": scope_reference,
        "status": _stored_status(event["status"], include_monitoring=True),
        "target_type": target_type,
        "score": _bounded_integer(event["score"], minimum=0, maximum=10_000),
        "score_scale": _bounded_integer(event["score_scale"], minimum=10_000, maximum=10_000),
        "risky_turn_count": _positive_integer(event["risky_turn_count"]),
        "jailbreak_count": _non_negative_integer(event["jailbreak_count"]),
        "sensitive_count": _non_negative_integer(event["sensitive_count"]),
        "tool_inducement_count": _non_negative_integer(event["tool_inducement_count"]),
        "state_revision": _positive_integer(event["state_revision"]),
        "threshold_version": _stored_required_text(event["threshold_version"], maximum=128),
        "occurred_at": _to_db_time(_parse_db_time(event["occurred_at"])),
    }
    return build_webhook_event(
        tenant_id=_stored_required_text(event["tenant_id"], maximum=160),
        event_type="security.session_high_risk",
        payload=payload,
        actor_id=_stored_required_text(event["actor_id"], maximum=160),
        object_id=payload["session_risk_score_id"],
        trace_id=_stored_required_text(event["source_trace_id"], maximum=160),
        source_request_id=_stored_required_text(event["source_request_id"], maximum=160),
        event_id=_stored_prefixed_id(event["session_risk_event_id"], "srevt_"),
        timestamp=_parse_db_time(event["occurred_at"]),
    )


def _audit_summary(
    score: sqlite3.Row,
    *,
    status: str,
    revision: int,
    freeze_id: str | None,
    target_type: str | None,
) -> dict[str, object]:
    """构造只含固定状态、分数和不透明 ID 的扁平审计摘要。"""

    return {
        "scope_type": _stored_scope_type(score["scope_type"]),
        "status": status,
        "score": _bounded_integer(score["score"], minimum=0, maximum=10_000),
        "state_revision": revision,
        "session_risk_freeze_id": freeze_id,
        "target_type": target_type,
    }


def _action_target_type(action: str) -> str | None:
    """把固定动作映射为事件/Audit target，不读取客户端 target 字段。"""

    # Key 动作只能映射到固定目标枚举，事件与 Audit 不接受客户端提供的自由 target。
    if action in {"freeze_virtual_key", "release_virtual_key"}:
        return "virtual_key"
    # 会话动作与 Key 动作严格分离，保证释放操作不会误解除另一类覆盖层。
    if action in {"freeze_session", "release_session"}:
        # 返回固定枚举供数据库 owner trigger 复核，不能返回原始会话标识。
        return "session"
    return None


def _normalize_filters(
    *,
    status: str | None,
    scope_type: str | None,
    virtual_key_id: str | None,
    from_time: str | None,
    to_time: str | None,
    limit: str | None,
) -> _ListFilters:
    """规范化查询枚举、时间与页大小；非 canonical 表示统一拒绝。"""

    if status is None:
        statuses = DEFAULT_QUERY_STATUSES
    else:
        values = status.split(",")
        # 状态筛选拒绝未知值、空项和重复项，避免同一筛选产生多种游标签名表示。
        if (
            not values
            or any(value not in VALID_QUERY_STATUSES for value in values)
            or len(set(values)) != len(values)
        ):
            # 非法或重复状态不能进入签名筛选，统一返回固定查询错误。
            raise ApiError("session_risk.query_invalid")
        statuses = tuple(sorted(values))
    normalized_scope = None
    # scope 类型只接受产品固定枚举，不能把任意文本拼接进查询或签名载荷。
    if scope_type is not None:
        # 未知范围统一视为非法查询，不尝试兼容未来枚举造成当前租户数据误筛选。
        if scope_type not in VALID_SCOPE_TYPES:
            raise ApiError("session_risk.query_invalid")
        normalized_scope = scope_type
    normalized_key = None
    # Virtual Key 筛选必须是既有 canonical UUID，租户归属稍后仍由数据库再次校验。
    if virtual_key_id is not None:
        try:
            parsed_key_id = UUID(virtual_key_id)
        # 解析失败只返回固定查询错误，不把库异常或用户输入回显到错误正文。
        except (TypeError, ValueError, AttributeError):
            # Virtual Key 是前序 PRD 已存在的 UUID 资源；本功能只能引用，不能另造 vk_ ID 合同。
            raise ApiError("session_risk.query_invalid") from None
        if str(parsed_key_id) != virtual_key_id:
            # 仅接受 canonical 小写连字符 UUID，避免同一资源出现大小写或等价编码两种 cursor 绑定。
            raise ApiError("session_risk.query_invalid")
        normalized_key = virtual_key_id
    start = _parse_optional_query_time(from_time)
    end = _parse_optional_query_time(to_time)
    # 时间窗口采用左闭右开语义，空窗口或反向窗口会破坏稳定分页，因此直接拒绝。
    if start is not None and end is not None and end <= start:
        raise ApiError("session_risk.query_invalid")
    if limit is None:
        page_size = DEFAULT_PAGE_SIZE
    elif (
        not limit
        or not limit.isascii()
        or not limit.isdigit()
        or (len(limit) > 1 and limit.startswith("0"))
    ):
        # 页大小只接受无前导零的 ASCII 十进制，拒绝等价表示保持游标合同唯一。
        raise ApiError("session_risk.query_invalid")
    else:
        page_size = int(limit)
    # 页大小必须落在产品上限内，防止一次查询跨租户无关地放大数据库负载。
    if not 1 <= page_size <= MAX_PAGE_SIZE:
        raise ApiError("session_risk.query_invalid")
    return _ListFilters(
        statuses=statuses,
        scope_type=normalized_scope,
        virtual_key_id=normalized_key,
        from_time=start,
        to_time=end,
        limit=page_size,
    )


def _validate_virtual_key_owner(
    connection: sqlite3.Connection,
    tenant_id: str,
    virtual_key_id: str | None,
) -> None:
    """对可选 Key 筛选执行 tenant-qualified owner 校验，跨租户与不存在同形。"""

    # 未提供 Key 时无需额外点查；其余查询仍由主 SQL 的 tenant_id 首条件隔离。
    if virtual_key_id is None:
        return
    row = connection.execute(
        "SELECT 1 FROM virtual_keys WHERE tenant_id=? AND virtual_key_id=?",
        (tenant_id, virtual_key_id),
    ).fetchone()
    # 跨租户 Key 与不存在 Key 返回同形未找到，避免筛选接口被用作资源枚举器。
    if row is None:
        raise ApiError("session_risk.not_found")


def _list_where(
    tenant_id: str,
    filters: _ListFilters,
    cursor: _CursorState,
) -> tuple[str, list[object]]:
    """生成列名完全来自内部白名单的 tenant-first keyset SQL。"""

    status_marks = ",".join("?" for _ in filters.statuses)
    clauses = [
        "score.tenant_id=?",
        f"score.status IN ({status_marks})",
        "julianday(score.updated_at)<=julianday(?)",
        "(julianday(score.last_risk_at)<julianday(?) OR "
        "(score.last_risk_at=? AND score.session_risk_score_id<?))",
    ]
    before_time = _to_db_time(cursor.before_time)
    params: list[object] = [
        tenant_id,
        *filters.statuses,
        _to_db_time(cursor.snapshot_time),
        before_time,
        before_time,
        cursor.before_id,
    ]
    if filters.scope_type is not None:
        clauses.append("score.scope_type=?")
        params.append(filters.scope_type)
    if filters.virtual_key_id is not None:
        clauses.append("score.virtual_key_id=?")
        params.append(filters.virtual_key_id)
    if filters.from_time is not None:
        clauses.append("julianday(score.last_risk_at)>=julianday(?)")
        params.append(_to_db_time(filters.from_time))
    if filters.to_time is not None:
        clauses.append("julianday(score.last_risk_at)<julianday(?)")
        params.append(_to_db_time(filters.to_time))
    return " AND ".join(clauses), params


def _filter_digest(filters: _ListFilters) -> str:
    """对规范筛选生成不可逆稳定摘要，cursor 不携带可直接展示的筛选值。"""

    payload = json.dumps(
        {
            "statuses": filters.statuses,
            "scope_type": filters.scope_type,
            "virtual_key_id": filters.virtual_key_id,
            "from_time": _to_db_time(filters.from_time) if filters.from_time else None,
            "to_time": _to_db_time(filters.to_time) if filters.to_time else None,
            "limit": filters.limit,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _encode_cursor(
    config: AppConfig,
    tenant_id: str,
    filter_digest: str,
    *,
    snapshot_time: datetime,
    before_time: datetime,
    before_id: str,
) -> str:
    """签发绑定租户、筛选条件、稳定快照和键集边界的不透明游标。

    返回值使用 URL-safe 编码但不承载明文筛选；签名把认证租户纳入域，防止跨租户复用分页游标。
    """

    payload = json.dumps(
        {
            "v": CURSOR_VERSION,
            "tenant": tenant_id,
            "filter": filter_digest,
            "snapshot": _to_db_time(snapshot_time),
            "before_time": _to_db_time(before_time),
            "before_id": before_id,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    signature = hmac.new(
        config.auth_token_secret.encode("utf-8"),
        CURSOR_DOMAIN + b"\x00" + payload,
        hashlib.sha256,
    ).digest()
    return f"{_base64_encode(payload)}.{_base64_encode(signature)}"


def _decode_cursor(
    config: AppConfig,
    cursor: str,
    tenant_id: str,
    filter_digest: str,
) -> _CursorState:
    """验签 cursor 并拒绝跨租户、跨筛选、替代编码和损坏时间。"""

    try:
        # 游标长度和分段数量先受限，避免对超大攻击载荷执行 Base64、JSON 或 HMAC 处理。
        if not 20 <= len(cursor) <= 2048 or cursor.count(".") != 1:
            raise ValueError("cursor shape")
        encoded_payload, encoded_signature = cursor.split(".", 1)
        payload = _base64_decode(encoded_payload)
        signature = _base64_decode(encoded_signature)
        expected = hmac.new(
            config.auth_token_secret.encode("utf-8"),
            CURSOR_DOMAIN + b"\x00" + payload,
            hashlib.sha256,
        ).digest()
        # 签名必须等长且常量时间比较，任何差异都不能继续解析租户和筛选字段。
        if len(signature) != hashlib.sha256().digest_size or not hmac.compare_digest(
            signature, expected
        ):
            # 长度或常量时间比较失败都视为同一签名错误，不泄露哪一部分不匹配。
            raise ValueError("cursor signature")
        parsed = json.loads(payload.decode("ascii"))
        expected_fields = {"v", "tenant", "filter", "snapshot", "before_time", "before_id"}
        # exact-key 结构拒绝未知扩展和字段缺失，防止不同版本对同一游标产生歧义解释。
        if not isinstance(parsed, dict) or set(parsed) != expected_fields:
            raise ValueError("cursor fields")
        # 版本、认证租户、筛选摘要和边界 ID 必须全部绑定，跨请求复制游标会稳定失败。
        if (
            parsed["v"] != CURSOR_VERSION
            or parsed["tenant"] != tenant_id
            or parsed["filter"] != filter_digest
            or not SCORE_ID_PATTERN.fullmatch(str(parsed["before_id"]))
        ):
            # 任一绑定字段不匹配即拒绝整个游标，禁止跨租户或跨筛选继续分页。
            raise ValueError("cursor binding")
        canonical = json.dumps(
            parsed,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        # 重编码必须与签名载荷逐字节一致，非规范 JSON 即使语义等价也不接受。
        if canonical != payload:
            raise ValueError("cursor canonical")
        snapshot = _parse_cursor_time(parsed["snapshot"])
        before = _parse_cursor_time(parsed["before_time"])
        # 时间边界仍执行显式上限校验，避免异常 datetime 值参与数据库比较。
        if before > datetime.max.replace(tzinfo=UTC):
            raise ValueError("cursor time")
        return _CursorState(
            snapshot_time=snapshot,
            before_time=before,
            before_id=str(parsed["before_id"]),
        )
    # 所有解码、验签和结构错误统一为查询错误，绝不暴露签名密钥、载荷或解析器细节。
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ApiError("session_risk.query_invalid") from exc


def _base64_encode(value: bytes) -> str:
    """生成无填充且只有一种表示形式的 URL-safe Base64 文本。

    统一编码用于游标签名，避免同一字节串存在多种等价文本并绕过完整性比较。
    """

    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64_decode(value: str) -> bytes:
    """严格恢复规范 URL-safe Base64，并拒绝填充符和替代字符。

    返回原始字节；任何非唯一表示都作为非法游标处理，避免验签前后的文本含义发生漂移。
    """

    if (
        not value
        or "=" in value
        or not value.isascii()
        or any(
            character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
            for character in value
        )
    ):
        # 非 URL-safe 或带填充的文本存在替代编码风险，验签前即拒绝。
        raise ValueError("base64 shape")
    decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    # 解码后重编码必须逐字符一致，语义等价但非规范的表示仍然无效。
    if _base64_encode(decoded) != value:
        raise ValueError("base64 canonical")
    return decoded


def _canonical_action(payload: SessionRiskActionRequest) -> dict[str, object]:
    """校验动作/原因矩阵并生成唯一幂等意图对象。"""

    action = str(payload.action)
    reason = str(payload.reason_code)
    # 动作与原因必须符合固定处置矩阵，避免错误原因被用于绕过状态转换或污染审计。
    if action not in VALID_ACTIONS or reason not in ACTION_REASON_MATRIX[action]:
        raise ApiError("session_risk.payload_invalid")
    return {
        "action": action,
        "reason_code": reason,
        "expected_revision": int(payload.expected_revision),
    }


def _action_digest_candidates(
    config: AppConfig,
    *,
    domain: bytes,
    parts: tuple[str, ...],
) -> list[tuple[int, str]]:
    """按 current/previous keyring 生成版本化结构 HMAC 候选。

    这是 Control 的启动兼容边界；Shared helper 落盘后会由合同测试证明二者编码一致。
    每段使用 uint32 大端长度前缀，禁止分隔符拼接造成边界碰撞。
    """

    values: list[tuple[int, str | None]] = [
        (
            int(getattr(config, "session_risk_hmac_version", 1)),
            getattr(config, "session_risk_hmac_secret", None),
        )
    ]
    previous_version = getattr(config, "session_risk_previous_hmac_version", None)
    previous_secret = getattr(config, "session_risk_previous_hmac_secret", None)
    # previous 版本和密钥必须成对出现；半配置状态无法可靠验证轮换前的幂等记录。
    if previous_version is not None or previous_secret is not None:
        # 非正整数版本或空密钥代表启动合同损坏，不能静默忽略旧证据。
        if type(previous_version) is not int or previous_version < 1 or not previous_secret:
            raise ApiError("session_risk.store_unavailable")
        values.append((previous_version, previous_secret))
    try:
        # 当前密钥缺失时返回空候选让调用方在事务前失败，避免使用可枚举裸摘要降级。
        if not values[0][1]:
            return []
        current = SessionRiskHmacKey(values[0][0], values[0][1])
        previous = (
            SessionRiskHmacKey(values[1][0], values[1][1])
            if len(values) == 2 and values[1][1]
            else None
        )
        keyring = SessionRiskHmacKeyring(current=current, previous=previous)
        return [
            (candidate.key_version, candidate.digest)
            for candidate in keyring.derive_candidates(domain, *parts)
        ]
    # 密钥或结构化输入不可信时统一失败关闭，异常链不得携带 Secret 或租户业务标识。
    except (SessionRiskContractError, TypeError, ValueError) as exc:
        # 密钥或输入配置不可信时失败关闭，异常不得回显 Secret、摘要或业务 owner。
        raise ApiError("session_risk.store_unavailable") from exc


def _runtime_status(config: AppConfig) -> str:
    """返回 Control 可公开的依赖状态，不泄露 Redis URL 或 HMAC 配置。"""

    # 功能关闭只公开固定状态，绝不在响应中说明密钥、Redis 地址或内部配置差异。
    if not bool(getattr(config, "session_risk_enabled", False)):
        return "disabled"
    # 已启用但缺少当前 HMAC 时不能建立可信 owner，运行状态必须明确不可用。
    if not getattr(config, "session_risk_hmac_secret", None):
        return "unavailable"
    if getattr(config, "redis_url", None):
        return "active"
    return "degraded" if config.service_mode == "internal_only" else "unavailable"


def _build_store_sync_intent(
    config: AppConfig,
    score: sqlite3.Row,
) -> SessionRiskStoreSyncIntent:
    """从 tenant-qualified 权威 score 构造不含原始 Header 的 Redis 对账意图。"""

    tenant_id = _stored_required_text(score["tenant_id"], maximum=160)
    virtual_key_id = _stored_virtual_key_id(score["virtual_key_id"])
    scope_type = _stored_scope_type(score["scope_type"])
    scope_key = _stored_required_text(score["scope_key"], maximum=160)
    hmac_key_version = _positive_integer(score["hmac_key_version"])
    hmac_key = _session_risk_hmac_key(config, hmac_key_version)
    owner_slot = derive_owner_slot(
        hmac_key,
        tenant_id=tenant_id,
        virtual_key_id=virtual_key_id,
        scope_type=scope_type,
        scope_key=scope_key,
    )
    scope = SessionRiskScope(
        tenant_id=tenant_id,
        virtual_key_id=virtual_key_id,
        scope_type=scope_type,  # type: ignore[arg-type]
        scope_key=scope_key,
        owner_slot=owner_slot.digest,
        hmac_key_version=hmac_key_version,
        chat_session_id=scope_key if scope_type == "enterprise_chat" else None,
    )
    status = _stored_status(score["status"], include_monitoring=True)
    revision = _positive_integer(score["state_revision"])
    snapshot = _frozen_store_snapshot(score) if status == "frozen" else None
    if status not in {"frozen", "resolved"}:
        # 当前人工动作矩阵只能落到 frozen/resolved；其他状态表示持久事实或迁移已漂移。
        raise ApiError("session_risk.evidence_unavailable")
    return SessionRiskStoreSyncIntent(
        scope=scope,
        score_id=_stored_score_id(score["session_risk_score_id"]),
        authoritative_revision=revision,
        snapshot=snapshot,
    )


def _session_risk_hmac_key(config: AppConfig, version: int) -> SessionRiskHmacKey:
    """按 score 固定版本选择 current/previous HMAC，缺失旧密钥时失败关闭。"""

    candidates = (
        (
            getattr(config, "session_risk_hmac_version", None),
            getattr(config, "session_risk_hmac_secret", None),
        ),
        (
            getattr(config, "session_risk_previous_hmac_version", None),
            getattr(config, "session_risk_previous_hmac_secret", None),
        ),
    )
    for candidate_version, candidate_secret in candidates:
        # 只选择 score 固定的精确版本，禁止用当前密钥重算旧 owner 形成冻结旁路。
        if candidate_version == version and candidate_secret:
            try:
                # 返回前再次执行 Shared 密钥长度与版本校验，配置漂移必须失败关闭。
                return SessionRiskHmacKey(version, candidate_secret)
            # 密钥构造错误只映射固定 Store 错误，Secret 不进入 API 或日志异常链。
            except SessionRiskContractError as exc:
                raise ApiError("session_risk.store_unavailable") from exc
    # current 与 previous 都无法解释持久版本时不可对账，不能创建一个新的旁路 scope。
    raise ApiError("session_risk.store_unavailable")


def _frozen_store_snapshot(score: sqlite3.Row) -> SessionRiskSnapshot:
    """把冻结数据库事实转换为 Gateway Store 的严格结构快照。

    输入字段逐项执行类型和范围校验，返回仅用于 Redis 对账的安全摘要；损坏记录必须失败关闭，
    不能用默认值恢复后削弱已有冻结。
    """

    try:
        # 快照构造严格校验全部计数、时间和版本；任一损坏字段都不能用默认值掩盖。
        return SessionRiskSnapshot(
            score_id=_stored_score_id(score["session_risk_score_id"]),
            score=_bounded_integer(score["score"], minimum=1, maximum=10_000),
            risky_turn_count=_positive_integer(score["risky_turn_count"]),
            jailbreak_count=_non_negative_integer(score["jailbreak_count"]),
            sensitive_count=_non_negative_integer(score["sensitive_count"]),
            tool_inducement_count=_non_negative_integer(score["tool_inducement_count"]),
            state="frozen",
            revision=_positive_integer(score["state_revision"]),
            window_started_at_epoch=_epoch_seconds(score["window_started_at"]),
            last_risk_at_epoch=_epoch_seconds(score["last_risk_at"]),
            expires_at_epoch=None,
            hmac_key_version=_positive_integer(score["hmac_key_version"]),
            threshold_version=_stored_required_text(score["threshold_version"], maximum=128),
        )
    # Shared 模型拒绝数据库行时转换为证据不可用，保留冻结而不泄露原始坏值。
    except SessionRiskContractError as exc:
        raise ApiError("session_risk.evidence_unavailable") from exc


def _epoch_seconds(value: object) -> int:
    """把 canonical 数据库时间转换为整数 epoch，供 Redis exact-schema 使用。"""

    return int(_parse_db_time(value).timestamp())


def _scope_reference(scope_type: str, scope_key: str, chat_session_id: object) -> str:
    """生成可展示 scope 引用；API 只返回 HMAC 前缀，企业 Chat 返回服务端资源 ID。"""

    # 企业 Chat 仅展示服务端资源 ID，该 ID 已由租户 owner 约束保护且不来自公开 Header。
    if scope_type == "enterprise_chat":
        return _stored_prefixed_id(chat_session_id, "chs_")
    # 公开 API 会话只允许内部 HMAC scope，并仅截取固定前缀作为不可逆展示引用。
    if scope_type == "api_session":
        # 非规范 HMAC scope 说明持久证据损坏，不能把原始值或猜测性摘要暴露给页面。
        if not re.fullmatch(r"srh_[0-9a-f]{64}", scope_key):
            raise ApiError("session_risk.evidence_unavailable")
        # 公开引用使用独立 api_ 命名空间，避免把内部 srh_ 摘要的结构或完整值暴露给
        # Console/Webhook；固定 16 位小写十六进制同时满足共享 strict wire validator。
        return f"api_{scope_key[4:20]}"
    # 未知范围没有安全展示规则，统一失败关闭而不是回退为原始数据库文本。
    raise ApiError("session_risk.evidence_unavailable")


def _normalize_score_id(value: object) -> str:
    """校验公开 score path ID；大小写、空白和替代编码均拒绝。"""

    # 路径 ID 非规范时使用未找到同形响应，避免格式差异成为对象枚举侧信道。
    if type(value) is not str or not SCORE_ID_PATTERN.fullmatch(value):
        raise ApiError("session_risk.not_found")
    return value


def _stored_score_id(value: object) -> str:
    """读取数据库 score ID，损坏值按证据不可用失败关闭。"""

    # 数据库中的 score ID 若不符合冻结合同即视为证据损坏，禁止清洗后继续使用。
    if type(value) is not str or not SCORE_ID_PATTERN.fullmatch(value):
        raise ApiError("session_risk.evidence_unavailable")
    return value


def _stored_prefixed_id(value: object, prefix: str) -> str:
    """校验数据库内部 ``prefix + 32 lowercase hex`` 不透明身份。"""

    # 内部 ID 必须精确匹配调用方指定前缀和小写十六进制，避免跨对象类型混用。
    if type(value) is not str or not re.fullmatch(re.escape(prefix) + r"[0-9a-f]{32}", value):
        raise ApiError("session_risk.evidence_unavailable")
    return value


def _stored_virtual_key_id(value: object) -> str:
    """恢复前序模块生成的 canonical Virtual Key UUID，拒绝损坏的兼容层事实。"""

    # Virtual Key 技术主键只能是字符串 UUID，其他 SQLite 动态类型代表迁移或数据损坏。
    if type(value) is not str:
        raise ApiError("session_risk.evidence_unavailable")
    try:
        parsed = UUID(value)
    # 解析错误只映射为证据不可用，不能把坏值或库异常信息回显给请求方。
    except (ValueError, AttributeError):
        raise ApiError("session_risk.evidence_unavailable") from None
    # 等价但非 canonical 的 UUID 也拒绝，确保游标、索引和审计只存在一种资源表示。
    if str(parsed) != value:
        raise ApiError("session_risk.evidence_unavailable")
    return value


def _stored_scope_type(value: object) -> str:
    """恢复持久化的两类会话范围枚举并拒绝未知扩展值。

    返回经过白名单验证的范围字符串；未知值代表持久证据损坏，不能降级为任意默认范围。
    """

    normalized = str(value)
    # 范围枚举必须属于当前版本白名单，未知持久值不能自动映射到另一会话类型。
    if normalized not in VALID_SCOPE_TYPES:
        raise ApiError("session_risk.evidence_unavailable")
    return normalized


def _stored_status(value: object, *, include_monitoring: bool = False) -> str:
    """恢复 score/action 状态，按调用场景决定是否允许内部 monitoring。"""

    allowed = set(VALID_QUERY_STATUSES)
    # monitoring 仅供明确内部调用者选择，公开读取不会默认扩大可见状态集合。
    if include_monitoring:
        allowed.add("monitoring")
    normalized = str(value)
    # 持久状态不在调用场景白名单时表示状态机漂移，必须停止响应物化。
    if normalized not in allowed:
        raise ApiError("session_risk.evidence_unavailable")
    return normalized


def _stored_required_text(value: object, *, maximum: int) -> str:
    """读取有界必填文本；不清洗损坏事实，避免把篡改值伪装成合法。"""

    # 必填文本拒绝类型漂移、空值和首尾空白，避免清洗后改变已签名或已审计事实。
    if type(value) is not str or not value or value != value.strip():
        raise ApiError("session_risk.evidence_unavailable")
    try:
        encoded = value.encode("utf-8")
    # 无法编码的代理字符不能进入 JSON、日志或 Webhook，统一按损坏证据处理。
    except UnicodeEncodeError as exc:
        raise ApiError("session_risk.evidence_unavailable") from exc
    # 长度和控制字符限制阻止持久脏值污染响应头、日志行或下游序列化边界。
    if len(encoded) > maximum or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        # 超长文本或控制字符会破坏日志与响应边界，损坏证据必须失败关闭。
        raise ApiError("session_risk.evidence_unavailable")
    return value


def _positive_integer(value: object) -> int:
    """恢复严格正整数；bool、REAL 和文本数字都不接受。"""

    # 严格类型检查排除 bool 和文本数字，避免 SQLite 宽松类型掩盖计数证据损坏。
    if type(value) is not int or value < 1:
        raise ApiError("session_risk.evidence_unavailable")
    return value


def _non_negative_integer(value: object) -> int:
    """恢复严格非负整数；bool 不得借用 int 子类关系通过。"""

    # 非负计数必须是原生整数，任何负值或类型漂移都不能进入安全汇总。
    if type(value) is not int or value < 0:
        raise ApiError("session_risk.evidence_unavailable")
    return value


def _bounded_integer(value: object, *, minimum: int, maximum: int) -> int:
    """恢复处于闭区间的严格整数。"""

    # 有界数值同时校验原生类型和闭区间，防止异常分数影响冻结动作与页面判断。
    if type(value) is not int or not minimum <= value <= maximum:
        raise ApiError("session_risk.evidence_unavailable")
    return value


def _parse_optional_query_time(value: str | None) -> datetime | None:
    """解析查询中的显式时区 ISO-8601；空白和 naive 时间拒绝。"""

    # 缺省筛选与非法空字符串语义不同，只有真正未提供时才返回空条件。
    if value is None:
        return None
    # 用户时间筛选拒绝空白和非规范前后空格，避免游标签名出现等价输入。
    if not value or value != value.strip():
        raise ApiError("session_risk.query_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # 解析异常统一转换为公开查询错误，不回显用户输入或 Python datetime 细节。
    except ValueError as exc:
        raise ApiError("session_risk.query_invalid") from exc
    # 无时区时间会随服务器区域产生不同结果，查询合同必须明确拒绝。
    if parsed.tzinfo is None:
        raise ApiError("session_risk.query_invalid")
    return parsed.astimezone(UTC)


def _parse_cursor_time(value: object) -> datetime:
    """解析 cursor 内 canonical UTC 秒级时间，不接受等价替代表示。"""

    # 游标时间只能来自已验签 canonical JSON 字符串，其他类型说明载荷结构损坏。
    if type(value) is not str:
        raise ValueError("cursor time")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # 时区缺失或重编码不同均代表非唯一表示，防止等价时间绕过游标绑定。
    if parsed.tzinfo is None or _to_db_time(parsed) != value:
        raise ValueError("cursor time")
    return parsed.astimezone(UTC)


def _parse_db_time(value: object) -> datetime:
    """解析数据库 UTC 时间；损坏事实只映射为证据不可用。"""

    # 数据库时间类型漂移意味着持久证据不可解释，禁止强制转成字符串继续处理。
    if type(value) is not str:
        raise ApiError("session_risk.evidence_unavailable")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # 损坏时间只映射为证据不可用，原始数据库值和解析细节不得进入响应。
    except ValueError as exc:
        raise ApiError("session_risk.evidence_unavailable") from exc
    # 无时区数据库时间无法证明事件先后关系，不能按本地时区猜测。
    if parsed.tzinfo is None:
        raise ApiError("session_risk.evidence_unavailable")
    return parsed.astimezone(UTC)


def _parse_optional_db_time(value: object | None) -> datetime | None:
    """解析 nullable 数据库时间，同时保留缺失语义。"""

    return None if value is None else _parse_db_time(value)


def _service_now(value: datetime | None) -> datetime:
    """返回 UTC 秒级服务时钟；仅专项测试可显式注入。"""

    current = value if value is not None else datetime.now(UTC)
    # 注入时钟必须是带时区 datetime，测试值也不能绕过生产时间合同。
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise ApiError("session_risk.evidence_unavailable")
    return current.astimezone(UTC).replace(microsecond=0)


def _monotonic_row_time(persisted: object, now: datetime) -> str:
    """生成不早于服务时钟且严格晚于旧 row version 的更新时间。"""

    previous = _parse_db_time(persisted)
    return _to_db_time(max(_service_now(now), previous + timedelta(seconds=1)))


def _to_db_time(value: datetime) -> str:
    """把带时区时间编码为唯一的 UTC 秒级数据库文本。

    返回规范时间字符串；无时区输入会被拒绝，避免游标、审计和状态修订使用不同本地时区解释。
    """

    # 无时区值不能参与审计和状态修订排序，拒绝它比猜测服务器时区更安全。
    if value.tzinfo is None:
        raise ApiError("session_risk.evidence_unavailable")
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _connect(config: AppConfig) -> sqlite3.Connection:
    """创建独享数据库连接，并隐藏 URL、文件路径和驱动异常。"""

    try:
        return connect_database(config.database_url)
    # 连接失败统一转换为证据不可用，数据库 URL、文件路径和驱动信息不得泄露。
    except DbError as exc:
        raise ApiError("session_risk.evidence_unavailable") from exc


def _rollback_safely(connection: sqlite3.Connection) -> None:
    """尽力回滚；失败不能遮蔽既有稳定领域错误。"""

    try:
        connection.rollback()
    # 回滚失败只能被原业务异常遮蔽，避免数据库路径或驱动细节覆盖统一错误。
    except sqlite3.Error:
        return


__all__ = [
    "SessionRiskCommittedEffects",
    "SessionRiskStoreSyncIntent",
    "apply_session_risk_action",
    "dispatch_session_risk_effects_safely",
    "ensure_session_risk_store_available",
    "get_session_risk_store",
    "list_session_risks",
    "synchronize_session_risk_store",
]
