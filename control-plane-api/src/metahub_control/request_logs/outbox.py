"""PRD-055 调用日志查询事件的持久 outbox、lease/CAS 与失败恢复。

查询 API 只在短事务中写入 ``pending`` QueryEvent，不同步访问 Webhook Endpoint。本模块由
Control API 后台调度器调用：先原子领取一条 source event，关闭领取事务后构造最小披露
``log.query_executed`` 事件，再用 claim token 和 lease owner 做 CAS 回写。远端可能在本地
确认前已经收到事件，因此投递语义是稳定 event_id 的 at-least-once，而不是伪造 exactly-once。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import build_webhook_event

from metahub_control.webhooks.service import dispatch_webhook_event

# lease、最大尝试和退避构成持久状态机合同；修改时必须同步 0045 触发器及恢复测试。
EVENT_LEASE_SECONDS = 30
EVENT_MAX_ATTEMPTS = 10
EVENT_BACKOFF_SECONDS = (5, 30, 300)

# QueryEvent 只保存已使用字段名，不保存字段值。顺序与共享 Webhook 合同保持一致，确保同一
# 事实重试时签名正文稳定，并显式纳入 Excel 要求的 terminal_stage 阶段筛选。
FILTER_KEY_ORDER = (
    "application_id",
    "cost_amount_max",
    "cost_amount_min",
    "created_from",
    "created_to",
    "currency",
    "environment_id",
    "error_code",
    "latency_ms_max",
    "latency_ms_min",
    "model",
    "project_id",
    "provider_code",
    "request_id",
    "request_model",
    "resolved_model",
    "snapshot_version",
    "status",
    "terminal_stage",
    "total_tokens_max",
    "total_tokens_min",
    "trace_id",
    "virtual_key_id",
)


@dataclass(frozen=True, slots=True)
class RequestLogQueryEventClaim:
    """一次查询事件 lease 及构造安全 Webhook 所需的不可变事实快照。

    该结构刻意不携带原始 filters、SavedFilter 名称或任何 RequestLog 行；worker 只需要
    不可逆指纹、字段名、查询形态和聚合计数即可完成通知。
    """

    query_event_id: str
    event_id: str
    tenant_id: str
    actor_user_id: str
    filter_version: int
    filter_fingerprint: str
    filter_keys: tuple[str, ...]
    saved_filter_id: str | None
    sort_by: str
    sort_direction: str
    query_limit: int
    query_offset: int
    matched_count: int
    returned_count: int
    has_more: bool
    query_duration_ms: int
    operation_request_id: str
    operation_trace_id: str
    executed_at: datetime
    event_claim_token: str
    event_lease_owner_id: str
    event_dispatch_attempts: int
    event_lease_expires_at: datetime


def claim_next_request_log_query_event(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> RequestLogQueryEventClaim | None:
    """原子领取一条到期 pending 或 lease 已过期的 processing 查询事件。

    参数：运行配置、进程级 worker ID，以及仅供专项测试注入的 UTC 当前时间。
    返回：成功领取时返回不可变 claim；没有到期工作时返回 ``None``。
    约束：领取和 source 状态更新位于同一 ``BEGIN IMMEDIATE`` 短事务；第十次网络调用后
    若进程崩溃，下一轮只终结过期 lease，不允许产生第十一次网络调用。
    """

    current = _now(now)
    owner = _worker_id(worker_id)
    eligible_at = _to_db_time(current)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        # attempt=10 表示最后一次外呼已经发生。若 worker 在 CAS 前退出，行会停在 processing；
        # 到期后必须先转 failed，而不是再次 claim，从而同时避免永久悬挂和第十一次外呼。
        connection.execute(
            """
            UPDATE request_log_query_events
            SET event_dispatch_status='failed', event_next_attempt_at=NULL,
                event_dispatched_at=NULL, event_claim_token=NULL,
                event_lease_owner_id=NULL, event_lease_expires_at=NULL,
                event_last_error_code='request_log.query_event_unavailable',
                updated_at=?
            WHERE query_event_id=(
              SELECT query_event_id FROM request_log_query_events
              WHERE event_dispatch_status='processing'
                AND event_dispatch_attempts=?
                AND unixepoch(event_lease_expires_at)<=unixepoch(?)
              ORDER BY event_lease_expires_at, created_at, query_event_id
              LIMIT 1
            )
              AND event_dispatch_status='processing'
              AND event_dispatch_attempts=?
              AND unixepoch(event_lease_expires_at)<=unixepoch(?)
            """,
            (
                eligible_at,
                EVENT_MAX_ATTEMPTS,
                eligible_at,
                EVENT_MAX_ATTEMPTS,
                eligible_at,
            ),
        )
        row = connection.execute(
            """
            SELECT * FROM request_log_query_events
            WHERE event_dispatch_status IN ('pending','processing')
              AND (
                (event_dispatch_status='pending'
                  AND event_dispatch_attempts<?
                  AND unixepoch(event_next_attempt_at)<=unixepoch(?))
                OR
                (event_dispatch_status='processing'
                  AND event_dispatch_attempts<?
                  AND unixepoch(event_lease_expires_at)<=unixepoch(?))
              )
            ORDER BY event_dispatch_status, event_next_attempt_at,
                     event_lease_expires_at, created_at, query_event_id
            LIMIT 1
            """,
            (EVENT_MAX_ATTEMPTS, eligible_at, EVENT_MAX_ATTEMPTS, eligible_at),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        attempts = _bounded_int(
            row["event_dispatch_attempts"], minimum=0, maximum=EVENT_MAX_ATTEMPTS - 1
        ) + 1
        token = uuid4().hex
        # SQLite 状态机使用秒级单调 updated_at；同一注入秒内 claim 也至少推进一秒，避免
        # 测试时钟或快速恢复造成合法 CAS 被 0045 触发器误判为时间回退。
        claim_time = max(current, _parse_time(row["updated_at"]) + timedelta(seconds=1))
        lease_expires = claim_time + timedelta(seconds=EVENT_LEASE_SECONDS)
        updated = connection.execute(
            """
            UPDATE request_log_query_events
            SET event_dispatch_status='processing',
                event_dispatch_attempts=?, event_next_attempt_at=NULL,
                event_claim_token=?, event_lease_owner_id=?,
                event_lease_expires_at=?, event_last_error_code=NULL,
                updated_at=?
            WHERE query_event_id=? AND event_dispatch_attempts=?
              AND (
                (event_dispatch_status='pending'
                  AND unixepoch(event_next_attempt_at)<=unixepoch(?))
                OR
                (event_dispatch_status='processing'
                  AND unixepoch(event_lease_expires_at)<=unixepoch(?))
              )
            """,
            (
                attempts,
                token,
                owner,
                _to_db_time(lease_expires),
                _to_db_time(claim_time),
                row["query_event_id"],
                attempts - 1,
                eligible_at,
                eligible_at,
            ),
        )
        if updated.rowcount != 1:
            connection.rollback()
            return None
        # 在提交 claim 前完成持久事实映射；坏行必须使整个领取回滚，不能留下 processing
        # 孤儿，也不能把不可信 JSON 或计数送入已签名 Webhook。
        claim = _claim_from_row(
            row,
            token=token,
            owner=owner,
            attempts=attempts,
            lease_expires=lease_expires,
        )
        connection.commit()
        return claim
    # 已规范化领域错误需要原样保留稳定 code，同时回滚刚才可能写入的 processing 状态。
    except ApiError:
        _rollback(connection)
        raise
    # 锁、缺表、触发器拒绝和持久状态损坏统一折叠，禁止 SQL 与数据库路径进入日志或响应。
    except (sqlite3.Error, TypeError, ValueError) as exc:
        _rollback(connection)
        raise ApiError("request_log.query_event_unavailable") from exc
    finally:
        connection.close()


def dispatch_claimed_request_log_query_event(
    config: AppConfig,
    claim: RequestLogQueryEventClaim,
    *,
    now: datetime | None = None,
) -> bool:
    """在 claim 事务关闭后投递最小查询事件，并以 source lease CAS 回写结果。

    参数：运行配置、已领取 claim，以及仅供测试注入的完成时间。
    返回：source CAS 命中时返回 ``True``；lease 已被其他 worker 接管时返回 ``False``。
    安全：payload 不包含筛选值、SavedFilter 名称、RequestLog 内容、Key 或 Endpoint URL。
    """

    error_code: str | None = None
    try:
        event = build_webhook_event(
            tenant_id=claim.tenant_id,
            event_type="log.query_executed",
            event_id=claim.event_id,
            timestamp=claim.executed_at,
            actor_id=claim.actor_user_id,
            object_id=claim.query_event_id,
            trace_id=claim.operation_trace_id,
            payload={
                "schema_version": 1,
                "query_event_id": claim.query_event_id,
                "filter_version": claim.filter_version,
                "filter_keys": list(claim.filter_keys),
                "filter_fingerprint": claim.filter_fingerprint,
                "saved_filter_id": claim.saved_filter_id,
                "sort_by": claim.sort_by,
                "sort_direction": claim.sort_direction,
                "limit": claim.query_limit,
                "offset": claim.query_offset,
                "matched_count": claim.matched_count,
                "returned_count": claim.returned_count,
                "has_more": claim.has_more,
                "query_duration_ms": claim.query_duration_ms,
                "executed_at": _to_db_time(claim.executed_at),
            },
        )
        dispatch_webhook_event(
            config,
            claim.tenant_id,
            event,
            # 内部投递 request_id 绑定稳定 event_id，重试不得复制原查询的外部请求头。
            request_id=f"req_{claim.event_id}",
            trace_id=claim.operation_trace_id,
            resume_existing_pending=True,
        )
    # 共享事件构造器只抛固定 ValueError code；分支仍需白名单折叠，防止未来异常正文落库。
    except ValueError as exc:
        # ValueError 正文只允许来自共享固定 code；未知文本统一折叠，不能落库任意 payload。
        candidate = str(exc)
        error_code = (
            candidate
            if candidate in {"webhook.payload_invalid", "webhook.payload_sensitive"}
            else "webhook.event_type_invalid"
        )
    # dispatcher 领域错误可能包装网络、存储或订阅配置细节，source 只保存统一失败码。
    except ApiError:
        # dispatcher 可能包装网络、存储或远端细节，source outbox 只保留固定安全错误码。
        error_code = "webhook.delivery_unavailable"
    except Exception:
        # 网络库异常正文可能含 URL 或响应内容，任何未知异常都不得原样持久化或记录。
        error_code = "webhook.delivery_unavailable"
    completed_at = _finish_time(claim, _now(now))
    if error_code is None:
        return _finish_success(config, claim, completed_at)
    return _finish_failure(config, claim, completed_at, error_code)


def drain_request_log_query_event_once(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> bool:
    """领取并处理最多一个查询事件；没有可运行工作时返回 ``False``。"""

    claim = claim_next_request_log_query_event(config, worker_id=worker_id, now=now)
    if claim is None:
        return False
    dispatch_claimed_request_log_query_event(config, claim, now=now)
    return True


def _finish_success(
    config: AppConfig,
    claim: RequestLogQueryEventClaim,
    completed_at: datetime,
) -> bool:
    """仅在 lease 未过期且 token/owner 匹配时把 source event 标记为 dispatched。"""

    timestamp = _to_db_time(completed_at)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        updated = connection.execute(
            """
            UPDATE request_log_query_events
            SET event_dispatch_status='dispatched', event_next_attempt_at=NULL,
                event_dispatched_at=?, event_claim_token=NULL,
                event_lease_owner_id=NULL, event_lease_expires_at=NULL,
                event_last_error_code=NULL, updated_at=?
            WHERE query_event_id=?
              AND event_dispatch_status='processing'
              AND event_dispatch_attempts=?
              AND event_claim_token=? AND event_lease_owner_id=?
              AND unixepoch(event_lease_expires_at)>unixepoch(?)
            """,
            (
                timestamp,
                timestamp,
                claim.query_event_id,
                claim.event_dispatch_attempts,
                claim.event_claim_token,
                claim.event_lease_owner_id,
                timestamp,
            ),
        )
        if updated.rowcount != 1:
            connection.rollback()
            return False
        connection.commit()
        return True
    # 成功 CAS 的锁、缺表或触发器故障不能伪装成 dispatched，统一失败关闭。
    except sqlite3.Error as exc:
        _rollback(connection)
        # 底层异常可能含 SQL 或绝对路径，只向调度器暴露固定 outbox code。
        raise ApiError("request_log.query_event_unavailable") from exc
    finally:
        connection.close()


def _finish_failure(
    config: AppConfig,
    claim: RequestLogQueryEventClaim,
    completed_at: datetime,
    error_code: str,
) -> bool:
    """按 source 尝试次数退避，或在第十次失败后进入不可逆 failed 终态。"""

    allowed = {
        "webhook.event_type_invalid",
        "webhook.payload_invalid",
        "webhook.payload_sensitive",
        "webhook.delivery_unavailable",
        "request_log.query_event_unavailable",
    }
    reason = error_code if error_code in allowed else "webhook.delivery_unavailable"
    final = claim.event_dispatch_attempts >= EVENT_MAX_ATTEMPTS
    delay_index = min(
        max(claim.event_dispatch_attempts - 1, 0),
        len(EVENT_BACKOFF_SECONDS) - 1,
    )
    next_attempt = completed_at + timedelta(seconds=EVENT_BACKOFF_SECONDS[delay_index])
    timestamp = _to_db_time(completed_at)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        updated = connection.execute(
            """
            UPDATE request_log_query_events
            SET event_dispatch_status=?, event_next_attempt_at=?,
                event_dispatched_at=NULL, event_claim_token=NULL,
                event_lease_owner_id=NULL, event_lease_expires_at=NULL,
                event_last_error_code=?, updated_at=?
            WHERE query_event_id=?
              AND event_dispatch_status='processing'
              AND event_dispatch_attempts=?
              AND event_claim_token=? AND event_lease_owner_id=?
              AND unixepoch(event_lease_expires_at)>unixepoch(?)
            """,
            (
                "failed" if final else "pending",
                None if final else _to_db_time(next_attempt),
                reason,
                timestamp,
                claim.query_event_id,
                claim.event_dispatch_attempts,
                claim.event_claim_token,
                claim.event_lease_owner_id,
                timestamp,
            ),
        )
        if updated.rowcount != 1:
            connection.rollback()
            return False
        connection.commit()
        return True
    # 失败退避 CAS 同样属于权威 source 状态，数据库故障时不得假装已经安排重试。
    except sqlite3.Error as exc:
        _rollback(connection)
        # 底层异常正文不进入日志或事件，仅返回可监控的固定领域 code。
        raise ApiError("request_log.query_event_unavailable") from exc
    finally:
        connection.close()


def _claim_from_row(
    row: sqlite3.Row,
    *,
    token: str,
    owner: str,
    attempts: int,
    lease_expires: datetime,
) -> RequestLogQueryEventClaim:
    """从数据库行恢复 claim，并在出网前严格复验身份、JSON、计数和分页关系。"""

    query_event_id = _prefixed_identifier(row["query_event_id"], "lqe_", 32)
    event_id = _prefixed_identifier(row["event_id"], "lqevt_", 32)
    tenant_id = _safe_identifier(row["tenant_id"], maximum=160)
    actor_user_id = _safe_identifier(row["actor_user_id"], maximum=128)
    operation_request_id = _safe_identifier(row["operation_request_id"], maximum=128)
    operation_trace_id = _safe_identifier(row["operation_trace_id"], maximum=128)
    filter_version = _bounded_int(row["filter_version"], minimum=1, maximum=1)
    fingerprint = _lower_hex(row["filter_fingerprint"], length=64)
    filter_keys = _filter_keys(row["filter_keys_json"])
    saved_filter_id = _optional_prefixed_identifier(row["saved_filter_id"], "sf_", 32)
    sort_by = _enum(
        row["sort_by"],
        {"created_at", "latency_ms", "total_tokens", "estimated_cost_amount"},
    )
    sort_direction = _enum(row["sort_direction"], {"asc", "desc"})
    query_limit = _bounded_int(row["query_limit"], minimum=1, maximum=100)
    query_offset = _bounded_int(row["query_offset"], minimum=0, maximum=10_000)
    matched_count = _bounded_int(
        row["matched_count"], minimum=0, maximum=9_007_199_254_740_991
    )
    returned_count = _bounded_int(row["returned_count"], minimum=0, maximum=100)
    query_duration_ms = _bounded_int(
        row["query_duration_ms"], minimum=0, maximum=9_007_199_254_740_991
    )
    raw_has_more = row["has_more"]
    if type(raw_has_more) is not int or raw_has_more not in {0, 1}:
        # SQLite bool 必须是严格 0/1，字符串或其他整数会改变跨语言事件解释。
        raise ApiError("request_log.query_event_unavailable")
    has_more = bool(raw_has_more)
    remaining = max(matched_count - query_offset, 0)
    if (
        returned_count > query_limit
        or returned_count > remaining
        or has_more != (query_offset + returned_count < matched_count)
    ):
        # has_more 是可审计查询事实，必须由 offset + returned_count 与 matched_count 唯一推导；
        # 不一致时禁止把数据库坏行包装成一个看似合法的签名事件。
        raise ApiError("request_log.query_event_unavailable")
    executed_at = _canonical_time(row["executed_at"])
    return RequestLogQueryEventClaim(
        query_event_id=query_event_id,
        event_id=event_id,
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        filter_version=filter_version,
        filter_fingerprint=fingerprint,
        filter_keys=filter_keys,
        saved_filter_id=saved_filter_id,
        sort_by=sort_by,
        sort_direction=sort_direction,
        query_limit=query_limit,
        query_offset=query_offset,
        matched_count=matched_count,
        returned_count=returned_count,
        has_more=has_more,
        query_duration_ms=query_duration_ms,
        operation_request_id=operation_request_id,
        operation_trace_id=operation_trace_id,
        executed_at=executed_at,
        event_claim_token=token,
        event_lease_owner_id=owner,
        event_dispatch_attempts=attempts,
        event_lease_expires_at=lease_expires,
    )


def _filter_keys(value: object) -> tuple[str, ...]:
    """解析唯一、有序的字段名 JSON 数组，拒绝筛选值或未知字段穿透。"""

    try:
        parsed = json.loads(str(value))
    # JSON 错误正文可能带出持久值，必须在 source 边界折叠为固定 code。
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        # 解析失败不能把原始 filter_keys_json 复制到异常、日志或 Webhook。
        raise ApiError("request_log.query_event_unavailable") from exc
    if not isinstance(parsed, list) or any(type(item) is not str for item in parsed):
        # 只允许字符串数组；对象或数值可能夹带筛选值，必须整体拒绝。
        raise ApiError("request_log.query_event_unavailable")
    positions = {item: index for index, item in enumerate(FILTER_KEY_ORDER)}
    try:
        indexes = [positions[item] for item in parsed]
    # 未知字段表示生产者与共享合同漂移，worker 不能静默删除后继续签名。
    except KeyError as exc:
        # KeyError 正文可能包含未知字段名，统一折叠以避免日志披露业务标识。
        raise ApiError("request_log.query_event_unavailable") from exc
    if indexes != sorted(set(indexes)):
        # 稳定顺序和唯一性决定事件签名；重排或重复不能由 worker 静默修正后外发。
        raise ApiError("request_log.query_event_unavailable")
    return tuple(parsed)


def _finish_time(claim: RequestLogQueryEventClaim, current: datetime) -> datetime:
    """生成数据库秒级单调完成时刻，并拒绝明显倒退的测试或系统时钟。"""

    claimed_at = claim.event_lease_expires_at - timedelta(seconds=EVENT_LEASE_SECONDS)
    if current < claimed_at - timedelta(seconds=1):
        # 倒退时钟会使过期判断与 CAS 语义分叉，不能猜测本地时区或强行覆盖新 owner。
        raise ApiError("request_log.query_event_unavailable")
    return claimed_at + timedelta(seconds=1) if current <= claimed_at else current


def _prefixed_identifier(value: object, prefix: str, suffix_length: int) -> str:
    """恢复固定前缀和小写十六进制后缀的持久对象 ID。"""

    raw = value if type(value) is str else ""
    if not raw.startswith(prefix) or len(raw) != len(prefix) + suffix_length:
        # 对象 ID 漂移会让信封绑定错误事实，禁止补前缀或截断后继续投递。
        raise ApiError("request_log.query_event_unavailable")
    _lower_hex(raw[len(prefix) :], length=suffix_length)
    return raw


def _optional_prefixed_identifier(
    value: object, prefix: str, suffix_length: int
) -> str | None:
    """恢复可空持久对象 ID；空字符串不能冒充未使用 SavedFilter。"""

    if value is None:
        return None
    return _prefixed_identifier(value, prefix, suffix_length)


def _lower_hex(value: object, *, length: int) -> str:
    """校验固定长度小写十六进制摘要，拒绝大小写或短摘要漂移。"""

    raw = value if type(value) is str else ""
    if len(raw) != length or any(character not in "0123456789abcdef" for character in raw):
        # 指纹必须是 canonical 小写十六进制，大小写归一会掩盖持久事实损坏。
        raise ApiError("request_log.query_event_unavailable")
    return raw


def _safe_identifier(value: object, *, maximum: int) -> str:
    """恢复受限 ASCII 标识，避免控制字符进入签名、查询条件或结构化日志。"""

    raw = value if type(value) is str else ""
    allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.:-"
    if not 1 <= len(raw) <= maximum or not raw.isascii() or any(c not in allowed for c in raw):
        # 不可信标识不能进入签名信封或 CAS 条件，也不能在异常中回显原值。
        raise ApiError("request_log.query_event_unavailable")
    return raw


def _enum(value: object, allowed: set[str]) -> str:
    """恢复强枚举文本；未知持久值不能由 worker 猜默认值后继续投递。"""

    if type(value) is not str or value not in allowed:
        # 未知枚举代表迁移或生产者合同漂移，不能猜测默认值后外发。
        raise ApiError("request_log.query_event_unavailable")
    return value


def _bounded_int(value: object, *, minimum: int, maximum: int) -> int:
    """恢复严格有界整数，显式拒绝 bool、浮点、字符串和越界计数。"""

    if type(value) is not int or not minimum <= value <= maximum:
        # bool、浮点和越界计数会破坏分页及重试语义，必须在出网前失败关闭。
        raise ApiError("request_log.query_event_unavailable")
    return value


def _worker_id(value: object) -> str:
    """校验内部 lease owner，禁止控制字符和无界文本进入持久 CAS 条件。"""

    raw = str(value or "").strip()
    if not 1 <= len(raw) <= 128 or any(ord(character) < 32 for character in raw):
        # owner 会写入持久 lease；无界或控制字符文本不得成为数据库状态的一部分。
        raise ApiError("request_log.query_event_unavailable")
    return raw


def _now(value: datetime | None) -> datetime:
    """返回 UTC 秒级服务器时刻；naive 时间会破坏 lease 比较，因此失败关闭。"""

    current = value if value is not None else datetime.now(UTC)
    if not isinstance(current, datetime) or current.tzinfo is None:
        # 无时区时间无法可靠判断 lease 到期，不能按宿主机本地时区猜测。
        raise ApiError("request_log.query_event_unavailable")
    return current.astimezone(UTC).replace(microsecond=0)


def _parse_time(value: object) -> datetime:
    """解析有时区持久时间，损坏值统一映射为 source outbox 不可用。"""

    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    # 解析异常可能包含持久原值，source 边界只允许固定领域 code。
    except (TypeError, ValueError) as exc:
        # 不回显损坏时间，避免数据库正文经 scheduler 日志泄露。
        raise ApiError("request_log.query_event_unavailable") from exc
    if parsed.tzinfo is None:
        # naive 持久时间会破坏跨进程 lease 比较，不能按本机时区补齐。
        raise ApiError("request_log.query_event_unavailable")
    return parsed.astimezone(UTC)


def _canonical_time(value: object) -> datetime:
    """恢复无小数、Z 后缀的 canonical UTC 秒级事件时间。"""

    if type(value) is not str or not value.endswith("Z") or "." in value:
        # 事件签名冻结 UTC 秒级 Z 格式，偏移或小数形式不得由 worker 静默改写。
        raise ApiError("request_log.query_event_unavailable")
    parsed = _parse_time(value)
    if _to_db_time(parsed) != value:
        # 能解析但非 canonical 的时间仍会改变重试签名，必须失败关闭。
        raise ApiError("request_log.query_event_unavailable")
    return parsed


def _to_db_time(value: datetime) -> str:
    """编码 canonical UTC 秒级时间，保证 SQLite 比较、签名和重试结果稳定。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _connect(config: AppConfig) -> sqlite3.Connection:
    """创建 outbox 独享连接，并把可能含绝对路径的连接错误折叠为固定 code。"""

    try:
        return connect_database(config.database_url)
    except Exception as exc:
        # 连接异常常含数据库绝对路径，统一折叠后才能交给 scheduler 记录。
        raise ApiError("request_log.query_event_unavailable") from exc


def _rollback(connection: sqlite3.Connection) -> None:
    """尽力回滚清理事务，且不遮蔽最初的稳定领域错误。"""

    try:
        connection.rollback()
    # 回滚属于清理阶段，失败时不能遮蔽最初更准确的领域错误。
    except sqlite3.Error:
        return


__all__ = [
    "RequestLogQueryEventClaim",
    "claim_next_request_log_query_event",
    "dispatch_claimed_request_log_query_event",
    "drain_request_log_query_event_once",
]
