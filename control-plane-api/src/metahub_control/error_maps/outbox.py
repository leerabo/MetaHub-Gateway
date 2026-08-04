"""PRD-056 error_map.updated Source Outbox 的 lease/CAS 与失败恢复。

规则事务只写不可变 ``pending`` source event；后台 worker 在短事务中领取，关闭连接后调用
Webhook dispatcher，再以 event ID、attempt、claim token、owner 和未过期 lease 做 CAS 回写。
稳定 event_id 的语义是 at-least-once，不能伪装成 exactly-once。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import build_webhook_event

from metahub_control.webhooks.service import dispatch_webhook_event

# lease 时限、最大尝试、退避、载荷上限与安全 ID 共同定义 Source Outbox 的恢复边界；
# 这些常量确保事件可追踪而不保存 pattern、Provider 正文、Endpoint 或认证信息。
EVENT_LEASE_SECONDS = 30
EVENT_MAX_ATTEMPTS = 10
EVENT_BACKOFF_SECONDS = (5, 30, 300)
MAX_EVENT_PAYLOAD_BYTES = 4096
_EVENT_ID = re.compile(r"^pemevt_[0-9a-f]{32}$")
_MAP_ID = re.compile(r"^pem_[0-9a-f]{32}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_PAYLOAD_FIELDS = frozenset(
    {
        "schema_version",
        "operation",
        "provider_error_map_id",
        "rule_version",
        "head_revision",
        "provider_scope",
        "status",
        "priority",
        "output_error_code",
        "pattern_hash",
        "request_id",
    }
)


@dataclass(frozen=True, slots=True)
class ProviderErrorMapEventClaim:
    """一次 error_map.updated lease 与签名事件所需的安全冻结事实。"""

    provider_error_map_event_id: str
    tenant_id: str
    provider_error_map_id: str
    actor_user_id: str
    request_id: str
    trace_id: str
    payload: dict[str, object]
    created_at: datetime
    event_claim_token: str
    event_lease_owner_id: str
    event_dispatch_attempts: int
    event_lease_expires_at: datetime


def claim_next_provider_error_map_event(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> ProviderErrorMapEventClaim | None:
    """原子领取一条到期事件；第十次孤儿 lease 只终结而不再出网。"""

    current = _now(now)
    owner = _worker_id(worker_id)
    eligible_at = _to_db_time(current)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        # 第十次外呼若在 CAS 前崩溃，lease 到期后转 failed，避免永久 processing 或第十一次调用。
        connection.execute(
            """
            UPDATE provider_error_map_events
            SET dispatch_status='failed', next_attempt_at=NULL, dispatched_at=NULL,
                claim_token=NULL, lease_owner_id=NULL, lease_expires_at=NULL,
                last_error_code='error_map.event_unavailable', updated_at=?
            WHERE provider_error_map_event_id=(
              SELECT provider_error_map_event_id FROM provider_error_map_events
              WHERE dispatch_status='processing' AND dispatch_attempts=?
                AND unixepoch(lease_expires_at)<=unixepoch(?)
              ORDER BY lease_expires_at, created_at, provider_error_map_event_id LIMIT 1
            )
              AND dispatch_status='processing' AND dispatch_attempts=?
              AND unixepoch(lease_expires_at)<=unixepoch(?)
            """,
            (
                eligible_at,
                EVENT_MAX_ATTEMPTS,
                eligible_at,
                EVENT_MAX_ATTEMPTS,
                eligible_at,
            ),
        )
        # 已过期 processing 表示可能丢失 owner 的孤儿 lease，必须先于新 pending 接管，
        # 否则持续写入的 pending 会让旧事件永久饥饿并破坏最大尝试终结合同。
        row = connection.execute(
            """
            SELECT event.*,
                   version.priority AS frozen_priority,
                   version.output_error_code AS frozen_output_error_code
            FROM provider_error_map_events event
            JOIN provider_error_map_versions version
              ON version.tenant_id=event.tenant_id
             AND version.provider_error_map_id=event.provider_error_map_id
             AND version.version=event.rule_version
             AND version.provider_error_map_version_id=event.provider_error_map_version_id
            WHERE event.dispatch_status IN ('pending','processing')
              AND (
                (event.dispatch_status='pending' AND event.dispatch_attempts<?
                  AND unixepoch(event.next_attempt_at)<=unixepoch(?))
                OR
                (event.dispatch_status='processing' AND event.dispatch_attempts<?
                  AND unixepoch(event.lease_expires_at)<=unixepoch(?))
              )
            ORDER BY CASE event.dispatch_status
                       WHEN 'processing' THEN 0 ELSE 1 END,
                     CASE WHEN event.dispatch_status='processing'
                       THEN event.lease_expires_at ELSE event.next_attempt_at END,
                     event.created_at,
                     event.provider_error_map_event_id
            LIMIT 1
            """,
            (EVENT_MAX_ATTEMPTS, eligible_at, EVENT_MAX_ATTEMPTS, eligible_at),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        attempts = _bounded_int(row["dispatch_attempts"], 0, EVENT_MAX_ATTEMPTS - 1) + 1
        token = uuid4().hex
        claim_time = max(current, _parse_time(row["updated_at"]) + timedelta(seconds=1))
        lease_expires = claim_time + timedelta(seconds=EVENT_LEASE_SECONDS)
        updated = connection.execute(
            """
            UPDATE provider_error_map_events
            SET dispatch_status='processing', dispatch_attempts=?, next_attempt_at=NULL,
                claim_token=?, lease_owner_id=?, lease_expires_at=?, last_error_code=NULL,
                updated_at=?
            WHERE provider_error_map_event_id=? AND dispatch_attempts=?
              AND (
                (dispatch_status='pending' AND unixepoch(next_attempt_at)<=unixepoch(?))
                OR
                (dispatch_status='processing' AND unixepoch(lease_expires_at)<=unixepoch(?))
              )
            """,
            (
                attempts,
                token,
                owner,
                _to_db_time(lease_expires),
                _to_db_time(claim_time),
                row["provider_error_map_event_id"],
                attempts - 1,
                eligible_at,
                eligible_at,
            ),
        )
        if updated.rowcount != 1:
            connection.rollback()
            return None
        # 映射在提交 claim 前完成；持久坏行必须让 processing 更新整体回滚。
        claim = _claim_from_row(
            row,
            token=token,
            owner=owner,
            attempts=attempts,
            lease_expires=lease_expires,
        )
        connection.commit()
        return claim
    # 已转换的领域错误保留固定错误码，但仍必须回滚本次 claim，避免 processing 半状态。
    except ApiError:
        _rollback(connection)
        raise
    # 数据库、类型或时间坏值统一转换为 source 不可用，底层异常正文不得进入 API 或日志。
    except (sqlite3.Error, TypeError, ValueError) as exc:
        _rollback(connection)
        # 对外只暴露固定错误码，同时保留异常链供受控调试，不序列化其文本。
        raise ApiError("error_map.event_unavailable") from exc
    finally:
        connection.close()


def dispatch_claimed_provider_error_map_event(
    config: AppConfig,
    claim: ProviderErrorMapEventClaim,
    *,
    now: datetime | None = None,
) -> bool:
    """投递已领取 source event，并以未过期 lease CAS 回写结果。"""

    error_code: str | None = None
    try:
        event = build_webhook_event(
            tenant_id=claim.tenant_id,
            event_type="error_map.updated",
            event_id=claim.provider_error_map_event_id,
            timestamp=claim.created_at,
            actor_id=claim.actor_user_id,
            object_id=claim.provider_error_map_id,
            trace_id=claim.trace_id,
            source_request_id=claim.request_id,
            payload=claim.payload,
        )
        dispatch_webhook_event(
            config,
            claim.tenant_id,
            event,
            request_id=f"req_{claim.provider_error_map_event_id}",
            trace_id=claim.trace_id,
            resume_existing_pending=True,
        )
    # Webhook 构造只映射批准的安全校验码，任意异常文本都不能写回 source 行。
    except ValueError as exc:
        candidate = str(exc)
        error_code = (
            candidate
            if candidate in {"webhook.payload_invalid", "webhook.payload_sensitive"}
            else "webhook.event_type_invalid"
        )
    # 下游投递失败统一为可退避固定码，订阅 URL、响应正文和凭据均不得持久化。
    except ApiError:
        error_code = "webhook.delivery_unavailable"
    except Exception:
        # 网络异常可能含 URL、响应正文或认证头，source 只保存固定错误码。
        error_code = "webhook.delivery_unavailable"
    completed_at = _finish_time(claim, _now(now))
    if error_code is None:
        return _finish_success(config, claim, completed_at)
    return _finish_failure(config, claim, completed_at, error_code)


def drain_provider_error_map_event_once(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> bool:
    """领取并处理最多一条 source event；空队列返回 False。"""

    claim = claim_next_provider_error_map_event(config, worker_id=worker_id, now=now)
    if claim is None:
        return False
    dispatch_claimed_provider_error_map_event(config, claim, now=now)
    return True


def _finish_success(
    config: AppConfig,
    claim: ProviderErrorMapEventClaim,
    completed_at: datetime,
) -> bool:
    """仅当 attempt/token/owner 和未过期 lease 全部匹配时标记 dispatched。"""

    timestamp = _to_db_time(completed_at)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        updated = connection.execute(
            """
            UPDATE provider_error_map_events
            SET dispatch_status='dispatched', next_attempt_at=NULL, dispatched_at=?,
                claim_token=NULL, lease_owner_id=NULL, lease_expires_at=NULL,
                last_error_code=NULL, updated_at=?
            WHERE provider_error_map_event_id=? AND dispatch_status='processing'
              AND dispatch_attempts=? AND claim_token=? AND lease_owner_id=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                timestamp,
                timestamp,
                claim.provider_error_map_event_id,
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
    # 成功 CAS 的数据库失败必须回滚，不能把已投递事件伪装成已确认终态。
    except sqlite3.Error as exc:
        _rollback(connection)
        # 对外转换成统一 Outbox 错误码，不泄露数据库路径、SQL 或参数。
        raise ApiError("error_map.event_unavailable") from exc
    finally:
        connection.close()


def _finish_failure(
    config: AppConfig,
    claim: ProviderErrorMapEventClaim,
    completed_at: datetime,
    error_code: str,
) -> bool:
    """按 5/30/300 秒退避，或在第十次失败进入不可逆 failed。"""

    allowed = {
        "webhook.event_type_invalid",
        "webhook.payload_invalid",
        "webhook.payload_sensitive",
        "webhook.delivery_unavailable",
        "error_map.event_unavailable",
    }
    reason = error_code if error_code in allowed else "webhook.delivery_unavailable"
    final = claim.event_dispatch_attempts >= EVENT_MAX_ATTEMPTS
    delay = EVENT_BACKOFF_SECONDS[
        min(max(claim.event_dispatch_attempts - 1, 0), len(EVENT_BACKOFF_SECONDS) - 1)
    ]
    next_attempt = completed_at + timedelta(seconds=delay)
    timestamp = _to_db_time(completed_at)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        updated = connection.execute(
            """
            UPDATE provider_error_map_events
            SET dispatch_status=?, next_attempt_at=?, dispatched_at=NULL,
                claim_token=NULL, lease_owner_id=NULL, lease_expires_at=NULL,
                last_error_code=?, updated_at=?
            WHERE provider_error_map_event_id=? AND dispatch_status='processing'
              AND dispatch_attempts=? AND claim_token=? AND lease_owner_id=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                "failed" if final else "pending",
                None if final else _to_db_time(next_attempt),
                reason,
                timestamp,
                claim.provider_error_map_event_id,
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
    # 失败/退避 CAS 的数据库异常同样整体回滚，保持 attempt 与状态一致。
    except sqlite3.Error as exc:
        _rollback(connection)
        # 固定领域错误替代原始 SQLite 文本，避免内部表结构进入日志或响应。
        raise ApiError("error_map.event_unavailable") from exc
    finally:
        connection.close()


def _claim_from_row(
    row: sqlite3.Row,
    *,
    token: str,
    owner: str,
    attempts: int,
    lease_expires: datetime,
) -> ProviderErrorMapEventClaim:
    """恢复并交叉核对持久 payload，坏行不能进入签名 Webhook。"""

    event_id = _matched(row["provider_error_map_event_id"], _EVENT_ID)
    map_id = _matched(row["provider_error_map_id"], _MAP_ID)
    tenant_id = _safe_id(row["tenant_id"])
    actor_id = _safe_id(row["actor_user_id"])
    request_id = _safe_id(row["request_id"])
    trace_id = _safe_id(row["trace_id"])
    raw_payload = row["payload_json"]
    # source payload 必须是有界字符串，超限或异常类型不进入 JSON 解析与 Webhook 签名。
    if type(raw_payload) is not str or len(raw_payload.encode("utf-8")) > MAX_EVENT_PAYLOAD_BYTES:
        raise ApiError("error_map.event_unavailable")
    try:
        payload = json.loads(raw_payload)
    # JSON 坏行只能转换为固定 Outbox 错误，原始载荷可能包含敏感信息，禁止回显。
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ApiError("error_map.event_unavailable") from exc
    # exact schema 阻止历史坏行或未知字段把 pattern、正文等内容扩展到 Webhook。
    if type(payload) is not dict or set(payload) != _PAYLOAD_FIELDS:
        raise ApiError("error_map.event_unavailable")
    if (
        payload.get("schema_version") != 1
        or payload.get("provider_error_map_id") != map_id
        or payload.get("request_id") != request_id
        or payload.get("operation") != row["operation"]
        or payload.get("rule_version") != row["rule_version"]
        or payload.get("head_revision") != row["head_revision"]
        or payload.get("status") != row["status"]
        or payload.get("priority") != row["frozen_priority"]
        or payload.get("output_error_code") != row["frozen_output_error_code"]
        or payload.get("pattern_hash") != row["pattern_hash"]
        or payload.get("provider_scope") != (row["provider_id"] or "all")
    ):
        # Source 标量与 payload 任一漂移都可能导致签名事件指向错误 owner，必须失败关闭。
        raise ApiError("error_map.event_unavailable")
    return ProviderErrorMapEventClaim(
        provider_error_map_event_id=event_id,
        tenant_id=tenant_id,
        provider_error_map_id=map_id,
        actor_user_id=actor_id,
        request_id=request_id,
        trace_id=trace_id,
        payload=payload,
        created_at=_parse_time(row["created_at"]),
        event_claim_token=token,
        event_lease_owner_id=owner,
        event_dispatch_attempts=attempts,
        event_lease_expires_at=lease_expires,
    )


def _finish_time(claim: ProviderErrorMapEventClaim, current: datetime) -> datetime:
    """生成数据库秒级单调完成时刻，并拒绝明显倒退时钟。"""

    claimed_at = claim.event_lease_expires_at - timedelta(seconds=EVENT_LEASE_SECONDS)
    # 明显倒退时钟会破坏未过期 lease CAS，必须拒绝而不能写入早于 claim 的完成时间。
    if current < claimed_at - timedelta(seconds=1):
        raise ApiError("error_map.event_unavailable")
    return claimed_at + timedelta(seconds=1) if current <= claimed_at else current


def _connect(config: AppConfig) -> sqlite3.Connection:
    """打开启用外键的短生命周期 SQLite 连接。"""

    return connect_database(config.database_url)


def _rollback(connection: sqlite3.Connection) -> None:
    """尽力回滚当前短事务；原始数据库异常不得覆盖领域错误。"""

    try:
        connection.rollback()
    # 回滚异常不能覆盖原领域错误，且数据库异常正文可能包含内部路径或 SQL。
    except sqlite3.Error:
        pass


def _worker_id(value: str) -> str:
    """验证 lease owner 为有界安全 ASCII，防止任意 worker 文本进入持久状态与日志。"""

    # owner 只允许固定安全字符和长度，坏值统一失败且不在异常中回显。
    if type(value) is not str or not 1 <= len(value) <= 128 or _SAFE_ID.fullmatch(value) is None:
        raise ApiError("error_map.event_unavailable")
    return value


def _matched(value: object, pattern: re.Pattern[str]) -> str:
    """验证固定前缀资源 ID，不在异常中回显坏值。"""

    # 资源 ID 必须匹配调用方指定前缀合同，禁止不同对象类型互相替代。
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise ApiError("error_map.event_unavailable")
    return value


def _safe_id(value: object) -> str:
    """验证 source owner 与链路 ID 为有界安全标识。"""

    # tenant、actor、request 与 trace 都只能是有界安全字符，避免日志和信封注入。
    if type(value) is not str or _SAFE_ID.fullmatch(value) is None:
        raise ApiError("error_map.event_unavailable")
    return value


def _bounded_int(value: object, minimum: int, maximum: int) -> int:
    """恢复 SQLite 严格整数，拒绝 bool、字符串与越界值。"""

    # bool 虽是 int 子类也不属于 attempt/revision 合同，所有越界值按坏 source 处理。
    if type(value) is not int or not minimum <= value <= maximum:
        raise ApiError("error_map.event_unavailable")
    return value


def _now(value: datetime | None) -> datetime:
    """返回 UTC 当前时间，并规范化测试注入时钟。"""

    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def _parse_time(value: object) -> datetime:
    """解析 SQLite UTC 时间；损坏值统一映射 source 错误。"""

    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    # 持久时间损坏会破坏 lease 顺序，统一转为固定错误且不回显原值。
    except (TypeError, ValueError) as exc:
        raise ApiError("error_map.event_unavailable") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _to_db_time(value: datetime) -> str:
    """以秒级 UTC canonical 文本写入状态机时间。"""

    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = [
    "ProviderErrorMapEventClaim",
    "claim_next_provider_error_map_event",
    "dispatch_claimed_provider_error_map_event",
    "drain_provider_error_map_event_once",
]
