"""`audit.exported` 持久 source outbox 的 lease、CAS 与有界重试。

导出事务只写 pending source；本 worker 在短事务内领取，关闭数据库连接后调用 Webhook
dispatcher，再用 event/token/owner/未过期 lease 原子终结。该机制提供可恢复的 at-least-once
source 投递，不宣称网络 exactly-once。
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

# lease、最大尝试、退避和精确 ID/payload 白名单共同限制可恢复投递的状态空间。
EVENT_LEASE_SECONDS = 30
EVENT_MAX_ATTEMPTS = 10
EVENT_BACKOFF_SECONDS = (5, 30, 300)
MAX_EVENT_PAYLOAD_BYTES = 4096
_EVENT_ID = re.compile(r"^aexevt_[0-9a-f]{32}$")
_EXPORT_ID = re.compile(r"^aex_[0-9a-f]{32}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_PAYLOAD_FIELDS = frozenset(
    {
        "audit_export_id",
        "status",
        "format",
        "schema_version",
        "row_count",
        "content_size_bytes",
        "content_sha256",
        "filter_fingerprint",
    }
)


@dataclass(frozen=True, slots=True)
class AuditExportEventClaim:
    """持有一条 `audit.exported` source lease 所需的冻结安全事实。"""

    event_id: str
    tenant_id: str
    audit_export_id: str
    actor_user_id: str
    request_id: str
    trace_id: str
    payload: dict[str, object]
    created_at: datetime
    claim_token: str
    claim_owner: str
    attempt_count: int
    lease_until: datetime


def claim_next_audit_export_event(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> AuditExportEventClaim | None:
    """原子领取到期 pending 或孤儿 processing；第十次过期 lease 直接失败关闭。"""

    current = _now(now)
    owner = _worker_id(worker_id)
    current_text = _to_db_time(current)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        # 第十次外呼若在 CAS 前崩溃，lease 到期后不能进行第十一次真实出网。
        connection.execute(
            """
            UPDATE audit_export_events
            SET status='failed', next_attempt_at=NULL, claim_token=NULL, claim_owner=NULL,
                lease_until=NULL, last_error_code='audit_export.event_unavailable',
                dispatched_at=NULL, updated_at=?
            WHERE audit_export_event_id=(
              SELECT audit_export_event_id FROM audit_export_events
              WHERE status='processing' AND attempt_count=?
                AND unixepoch(lease_until)<=unixepoch(?)
              ORDER BY lease_until, created_at, audit_export_event_id LIMIT 1
            ) AND status='processing' AND attempt_count=?
              AND unixepoch(lease_until)<=unixepoch(?)
            """,
            (current_text, EVENT_MAX_ATTEMPTS, current_text, EVENT_MAX_ATTEMPTS, current_text),
        )
        row = connection.execute(
            """
            SELECT event.*,
                   export.requested_by, export.request_id, export.trace_id,
                   export.event_id, export.event_payload_json,
                   export.status AS export_status, export.format AS export_format,
                   export.schema_version, export.row_count, export.content_size_bytes,
                   export.content_sha256, export.filter_fingerprint,
                   export.created_at AS export_created_at
            FROM audit_export_events event
            JOIN audit_exports export
              ON export.tenant_id=event.tenant_id
             AND export.audit_export_id=event.audit_export_id
             AND export.event_id=event.audit_export_event_id
            WHERE event.status IN ('pending','processing')
              AND (
                (event.status='pending' AND event.attempt_count<?
                  AND unixepoch(event.next_attempt_at)<=unixepoch(?))
                OR
                (event.status='processing' AND event.attempt_count<?
                  AND unixepoch(event.lease_until)<=unixepoch(?))
              )
            ORDER BY CASE event.status WHEN 'processing' THEN 0 ELSE 1 END,
                     CASE WHEN event.status='processing'
                       THEN event.lease_until ELSE event.next_attempt_at END,
                     event.created_at, event.audit_export_event_id
            LIMIT 1
            """,
            (EVENT_MAX_ATTEMPTS, current_text, EVENT_MAX_ATTEMPTS, current_text),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        attempts = _strict_int(row["attempt_count"], 0, EVENT_MAX_ATTEMPTS - 1) + 1
        token = uuid4().hex
        claim_time = max(current, _parse_time(row["updated_at"]) + timedelta(seconds=1))
        lease_until = claim_time + timedelta(seconds=EVENT_LEASE_SECONDS)
        updated = connection.execute(
            """
            UPDATE audit_export_events
            SET status='processing', attempt_count=?, next_attempt_at=NULL,
                claim_token=?, claim_owner=?, lease_until=?, last_error_code=NULL,
                dispatched_at=NULL, updated_at=?
            WHERE audit_export_event_id=? AND attempt_count=?
              AND (
                (status='pending' AND unixepoch(next_attempt_at)<=unixepoch(?))
                OR
                (status='processing' AND unixepoch(lease_until)<=unixepoch(?))
              )
            """,
            (
                attempts,
                token,
                owner,
                _to_db_time(lease_until),
                _to_db_time(claim_time),
                row["audit_export_event_id"],
                attempts - 1,
                current_text,
                current_text,
            ),
        )
        if updated.rowcount != 1:
            connection.rollback()
            return None
        # 在提交 lease 前完成 payload 与不可变 Export 的交叉核验，坏行不会进入 processing 半状态。
        claim = _claim_from_row(
            row,
            token=token,
            owner=owner,
            attempts=attempts,
            lease_until=lease_until,
        )
        connection.commit()
        return claim
    # 已归一化 source 错误仍需回滚 claim，不能留下没有返回给 worker 的 processing 状态。
    except ApiError:
        _rollback(connection)
        raise
    # 数据库、类型或时间坏值可能暴露内部行内容，必须折叠为统一导出不可用。
    except (sqlite3.Error, TypeError, ValueError) as exc:
        _rollback(connection)
        # 异常正文不进入 API 或 scheduler 日志，只保留固定错误码。
        raise ApiError("audit_export.unavailable") from exc
    finally:
        connection.close()


def dispatch_claimed_audit_export_event(
    config: AppConfig,
    claim: AuditExportEventClaim,
    *,
    now: datetime | None = None,
) -> bool:
    """仅由 source lease owner 恢复同一 Webhook intent，并用未过期 lease CAS 回写。"""

    # Claim 返回后可能因线程暂停而过期或已被另一 worker 接管；出网前必须重新核对
    # 持久 token/owner/attempt，不能只依赖最终 CAS 阻止迟到 worker 改写状态。
    if not _has_active_source_lease(config, claim, _now(now)):
        return False
    error_code: str | None = None
    try:
        event = build_webhook_event(
            tenant_id=claim.tenant_id,
            event_type="audit.exported",
            event_id=claim.event_id,
            timestamp=claim.created_at,
            actor_id=claim.actor_user_id,
            object_id=claim.audit_export_id,
            trace_id=claim.trace_id,
            source_request_id=claim.request_id,
            payload=claim.payload,
        )
        # 延迟导入同时避免 audit/webhooks 在应用启动时形成模块初始化环；只有持有效
        # source lease 的 worker 才能到达此处并取得恢复权限。
        from metahub_control.webhooks.service import dispatch_webhook_event

        dispatch_webhook_event(
            config,
            claim.tenant_id,
            event,
            request_id=f"req_{claim.event_id}",
            trace_id=claim.trace_id,
            resume_existing_pending=True,
        )
    # 共享 Webhook 构造拒绝说明持久 source 已失配，只保存固定内部错误码。
    except ValueError:
        # 构造失败只保存固定内部错误，不写 payload、筛选、CSV 或损坏值。
        error_code = "audit_export.event_unavailable"
    except ApiError:
        error_code = "webhook.delivery_unavailable"
    except Exception:
        # 网络和测试桩异常可能包含 Endpoint/正文，统一折叠为固定码。
        error_code = "webhook.delivery_unavailable"
    completed_at = _finish_time(claim, _now(now))
    if error_code is None:
        return _finish_success(config, claim, completed_at)
    return _finish_failure(config, claim, completed_at, error_code)


def _has_active_source_lease(
    config: AppConfig,
    claim: AuditExportEventClaim,
    current: datetime,
) -> bool:
    """在恢复 Webhook intent 前确认 claim 仍是数据库中的未过期 source lease。

    参数：应用配置、不可变 claim 和可信带时区当前时刻；返回：全部 CAS 身份仍匹配时为真。
    约束：检查连接必须在网络调用前关闭，不能跨 Endpoint I/O 持有 SQLite 锁。
    """

    connection = _connect(config)
    try:
        row = connection.execute(
            """
            SELECT 1 FROM audit_export_events
            WHERE audit_export_event_id=? AND tenant_id=? AND audit_export_id=?
              AND status='processing' AND attempt_count=?
              AND claim_token=? AND claim_owner=? AND lease_until=?
              AND unixepoch(lease_until)>unixepoch(?)
            """,
            (
                claim.event_id,
                claim.tenant_id,
                claim.audit_export_id,
                claim.attempt_count,
                claim.claim_token,
                claim.claim_owner,
                _to_db_time(claim.lease_until),
                _to_db_time(current),
            ),
        ).fetchone()
        return row is not None
    # 预检数据库失败不能降级为盲目恢复，否则非 owner 也可能进入 resume 模式。
    except sqlite3.Error as exc:
        # SQL、数据库路径和底层异常只留在异常链，公开层继续使用统一错误码。
        raise ApiError("audit_export.unavailable") from exc
    finally:
        connection.close()


def drain_audit_export_event_once(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> bool:
    """领取并处理最多一条 source event；空队列返回 False。"""

    claim = claim_next_audit_export_event(config, worker_id=worker_id, now=now)
    if claim is None:
        return False
    dispatch_claimed_audit_export_event(config, claim, now=now)
    return True


def _finish_success(
    config: AppConfig,
    claim: AuditExportEventClaim,
    completed_at: datetime,
) -> bool:
    """attempt/token/owner 与未过期 lease 全部匹配时才标记 dispatched。"""

    timestamp = _to_db_time(completed_at)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        updated = connection.execute(
            """
            UPDATE audit_export_events
            SET status='dispatched', next_attempt_at=NULL, claim_token=NULL,
                claim_owner=NULL, lease_until=NULL, last_error_code=NULL,
                dispatched_at=?, updated_at=?
            WHERE audit_export_event_id=? AND status='processing' AND attempt_count=?
              AND claim_token=? AND claim_owner=?
              AND unixepoch(lease_until)>unixepoch(?)
            """,
            (
                timestamp,
                timestamp,
                claim.event_id,
                claim.attempt_count,
                claim.claim_token,
                claim.claim_owner,
                timestamp,
            ),
        )
        if updated.rowcount != 1:
            connection.rollback()
            return False
        connection.commit()
        return True
    # 成功 CAS 的数据库失败必须回滚，不能伪装成已确认终态。
    except sqlite3.Error as exc:
        _rollback(connection)
        # 数据库路径和 SQL 不得穿透 source worker 边界。
        raise ApiError("audit_export.unavailable") from exc
    finally:
        connection.close()


def _finish_failure(
    config: AppConfig,
    claim: AuditExportEventClaim,
    completed_at: datetime,
    error_code: str,
) -> bool:
    """失败后按 5/30/300 秒退避，第十次失败进入终态 failed。"""

    allowed = {"webhook.delivery_unavailable", "audit_export.event_unavailable"}
    reason = error_code if error_code in allowed else "webhook.delivery_unavailable"
    final = claim.attempt_count >= EVENT_MAX_ATTEMPTS
    delay = EVENT_BACKOFF_SECONDS[
        min(max(claim.attempt_count - 1, 0), len(EVENT_BACKOFF_SECONDS) - 1)
    ]
    timestamp = _to_db_time(completed_at)
    next_attempt = completed_at + timedelta(seconds=delay)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        updated = connection.execute(
            """
            UPDATE audit_export_events
            SET status=?, next_attempt_at=?, claim_token=NULL, claim_owner=NULL,
                lease_until=NULL, last_error_code=?, dispatched_at=NULL, updated_at=?
            WHERE audit_export_event_id=? AND status='processing' AND attempt_count=?
              AND claim_token=? AND claim_owner=?
              AND unixepoch(lease_until)>unixepoch(?)
            """,
            (
                "failed" if final else "pending",
                None if final else _to_db_time(next_attempt),
                reason,
                timestamp,
                claim.event_id,
                claim.attempt_count,
                claim.claim_token,
                claim.claim_owner,
                timestamp,
            ),
        )
        if updated.rowcount != 1:
            connection.rollback()
            return False
        connection.commit()
        return True
    # 退避/失败 CAS 的数据库异常同样整体回滚，保持 attempt 与状态一致。
    except sqlite3.Error as exc:
        _rollback(connection)
        # 原始 SQLite 文本可能含内部路径，仅返回固定领域错误。
        raise ApiError("audit_export.unavailable") from exc
    finally:
        connection.close()


def _claim_from_row(
    row: sqlite3.Row,
    *,
    token: str,
    owner: str,
    attempts: int,
    lease_until: datetime,
) -> AuditExportEventClaim:
    """恢复 source payload，并与 AuditExport 每个安全标量精确核对。"""

    event_id = _matched(row["audit_export_event_id"], _EVENT_ID)
    if row["event_id"] != event_id:
        # Source 与不可变 Export 的 event ID 不一致时禁止签名和出网。
        raise ApiError("audit_export.unavailable")
    export_id = _matched(row["audit_export_id"], _EXPORT_ID)
    tenant_id = _safe_id(row["tenant_id"])
    actor_id = _safe_id(row["requested_by"])
    request_id = _safe_id(row["request_id"])
    trace_id = _safe_id(row["trace_id"])
    raw_payload = row["event_payload_json"]
    if type(raw_payload) is not str or len(raw_payload.encode("utf-8")) > MAX_EVENT_PAYLOAD_BYTES:
        # 非文本或超限载荷可能是损坏/注入数据，不能进入 JSON 解析器。
        raise ApiError("audit_export.unavailable")
    try:
        payload = json.loads(raw_payload)
    # JSON 解析失败不能回显持久正文，只能固定失败关闭。
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        # 保留异常链供专项测试，公开层只看统一错误码。
        raise ApiError("audit_export.unavailable") from exc
    if type(payload) is not dict or set(payload) != _PAYLOAD_FIELDS:
        # 精确键集阻止筛选、CSV、邮箱或内部状态扩散到 Webhook。
        raise ApiError("audit_export.unavailable")
    if (
        payload.get("audit_export_id") != export_id
        or payload.get("status") != row["export_status"]
        or payload.get("format") != row["export_format"]
        or payload.get("schema_version") != row["schema_version"]
        or payload.get("row_count") != row["row_count"]
        or payload.get("content_size_bytes") != row["content_size_bytes"]
        or payload.get("content_sha256") != row["content_sha256"]
        or payload.get("filter_fingerprint") != row["filter_fingerprint"]
        or _HASH.fullmatch(str(payload.get("content_sha256"))) is None
        or _HASH.fullmatch(str(payload.get("filter_fingerprint"))) is None
        or row["created_at"] != row["export_created_at"]
    ):
        # Payload 与不可变 Export 任一标量漂移都会破坏事件复核身份。
        raise ApiError("audit_export.unavailable")
    return AuditExportEventClaim(
        event_id=event_id,
        tenant_id=tenant_id,
        audit_export_id=export_id,
        actor_user_id=actor_id,
        request_id=request_id,
        trace_id=trace_id,
        payload=payload,
        created_at=_parse_time(row["export_created_at"]),
        claim_token=token,
        claim_owner=owner,
        attempt_count=attempts,
        lease_until=lease_until,
    )


def _finish_time(claim: AuditExportEventClaim, current: datetime) -> datetime:
    """生成秒级单调完成时间，并拒绝明显倒退的 worker 时钟。"""

    claimed_at = claim.lease_until - timedelta(seconds=EVENT_LEASE_SECONDS)
    if current < claimed_at - timedelta(seconds=1):
        # 明显倒退时钟会绕过 lease 时效判断，必须拒绝写回。
        raise ApiError("audit_export.unavailable")
    return claimed_at + timedelta(seconds=1) if current <= claimed_at else current


def _connect(config: AppConfig) -> sqlite3.Connection:
    """打开启用外键的短生命周期 SQLite 连接。"""

    return connect_database(config.database_url)


def _rollback(connection: sqlite3.Connection) -> None:
    """尽力回滚，原始数据库异常不得覆盖领域错误。"""

    try:
        connection.rollback()
    # 回滚异常不能覆盖首个 source 错误，也不能写出数据库细节。
    except sqlite3.Error:
        pass


def _worker_id(value: str) -> str:
    """限制 lease owner 为安全 ASCII，阻止任意文本进入持久状态。"""

    if type(value) is not str or not 1 <= len(value) <= 128 or _SAFE_ID.fullmatch(value) is None:
        # 任意 worker 文本不得进入 claim_owner 或调度日志。
        raise ApiError("audit_export.unavailable")
    return value


def _matched(value: object, pattern: re.Pattern[str]) -> str:
    """校验固定前缀资源 ID，不在错误中回显损坏值。"""

    if type(value) is not str or pattern.fullmatch(value) is None:
        # 坏资源 ID 不回显，防止持久内容经异常文本泄露。
        raise ApiError("audit_export.unavailable")
    return value


# Source 信封的 tenant/actor/request/trace 必须统一走同一安全 ID 门禁。
def _safe_id(value: object) -> str:
    """校验租户、操作者和链路字段都是有界安全关联标识，拒绝控制字符。"""

    if type(value) is not str or _SAFE_ID.fullmatch(value) is None:
        # 控制字符、空值和超长关联 ID 均禁止进入签名事件。
        raise ApiError("audit_export.unavailable")
    return value


def _strict_int(value: object, minimum: int, maximum: int) -> int:
    """恢复 SQLite 严格整数，拒绝 bool、字符串和越界值。"""

    if type(value) is not int or not minimum <= value <= maximum:
        # bool、字符串或越界 attempt 会破坏十次终结合同。
        raise ApiError("audit_export.unavailable")
    return value


def _now(value: datetime | None) -> datetime:
    """返回带时区 UTC 时刻；测试注入无时区时间也必须失败关闭。"""

    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        # 无时区 worker 时钟无法可靠比较 lease，必须失败关闭。
        raise ApiError("audit_export.unavailable")
    return current.astimezone(UTC)


def _parse_time(value: object) -> datetime:
    """解析 source UTC 时间；坏值不进入签名信封或日志。"""

    if type(value) is not str:
        # 非文本 source 时间不能用于 lease 或事件 timestamp。
        raise ApiError("audit_export.unavailable")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # 损坏时间不回显原值，仅转换为固定 source 错误。
    except ValueError as exc:
        # 异常链只用于受控测试定位，不进入结构化日志。
        raise ApiError("audit_export.unavailable") from exc
    if parsed.tzinfo is None:
        # 数据库无时区值在不同主机解释不一致，不能继续投递。
        raise ApiError("audit_export.unavailable")
    return parsed.astimezone(UTC)


def _to_db_time(value: datetime) -> str:
    """把 lease 状态机时间稳定表示为 UTC 秒级文本。"""

    if value.tzinfo is None:
        # 所有状态机时间必须先规范到 UTC，禁止依赖主机本地时区。
        raise ApiError("audit_export.unavailable")
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "AuditExportEventClaim",
    "claim_next_audit_export_event",
    "dispatch_claimed_audit_export_event",
    "drain_audit_export_event_once",
]
