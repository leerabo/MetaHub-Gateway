"""PRD-054 请求解释查看事件的持久 outbox、lease/CAS 与失败恢复。

GET 事务只插入 pending RequestExplanation，不执行网络请求。本模块在独立短事务中领取
source event，关闭连接后构造最小披露 Webhook 并调用公共 dispatcher，再按 claim token
CAS 回写成功或退避。远端可能在本地确认前已收到事件，因此语义是稳定 event_id 的
at-least-once，而不是网络 exactly-once。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_control.webhooks.service import dispatch_webhook_event
from metahub_shared.db.connection import connect_database
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import build_webhook_event

from .contracts import SECTION_ORDER

# lease、最大尝试和退避是持久状态机边界；修改时必须同步迁移触发器与恢复测试。
EVENT_LEASE_SECONDS = 30
EVENT_MAX_ATTEMPTS = 10
EVENT_BACKOFF_SECONDS = (5, 30, 300)


@dataclass(frozen=True, slots=True)
class RequestExplanationEventClaim:
    """一次 source event lease 及构造安全查看事件所需的不可变快照。"""

    request_explanation_id: str
    event_id: str
    tenant_id: str
    source_request_id: str
    source_trace_id: str
    actor_user_id: str
    source_status: str
    explanation_status: str
    missing_sections: tuple[str, ...]
    truncated_sections: tuple[str, ...]
    evidence_version: str
    operation_trace_id: str
    viewed_at: datetime
    event_claim_token: str
    event_lease_owner_id: str
    event_dispatch_attempts: int
    event_lease_expires_at: datetime


def claim_next_request_explanation_event(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> RequestExplanationEventClaim | None:
    """原子领取一条到期 pending 或 lease 已过期的 processing 查看事件。"""

    current = _now(now)
    owner = _worker_id(worker_id)
    eligible_at = _to_db_time(current)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        # 第十次网络调用后如果进程在 CAS 回写前退出，行会以 attempt=10 留在 processing。
        # 该 lease 已过期时不能再次出网（会突破最大次数），也不能永久悬挂；先在同一短事务
        # 中把一个最老的孤儿 lease 终结为固定失败，再继续领取其他可投递事件。
        connection.execute(
            """
            UPDATE request_explanations
            SET event_dispatch_status='failed', event_next_attempt_at=NULL,
                event_dispatched_at=NULL, event_claim_token=NULL,
                event_lease_owner_id=NULL, event_lease_expires_at=NULL,
                event_last_error_code='request_explanation.event_unavailable',
                updated_at=?
            WHERE request_explanation_id=(
              SELECT request_explanation_id FROM request_explanations
              WHERE event_dispatch_status='processing'
                AND event_dispatch_attempts=?
                AND unixepoch(event_lease_expires_at)<=unixepoch(?)
              ORDER BY event_lease_expires_at, created_at, request_explanation_id
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
            SELECT * FROM request_explanations
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
                     event_lease_expires_at, created_at, request_explanation_id
            LIMIT 1
            """,
            (EVENT_MAX_ATTEMPTS, eligible_at, EVENT_MAX_ATTEMPTS, eligible_at),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        attempts = _non_negative_int(row["event_dispatch_attempts"]) + 1
        token = uuid4().hex
        # SQLite 时间精度为秒；同一注入秒内 claim 至少推进一秒，满足 0044 单调状态机。
        claim_time = max(current, _parse_time(row["updated_at"]) + timedelta(seconds=1))
        lease_expires = claim_time + timedelta(seconds=EVENT_LEASE_SECONDS)
        updated = connection.execute(
            """
            UPDATE request_explanations
            SET event_dispatch_status='processing',
                event_dispatch_attempts=?, event_next_attempt_at=NULL,
                event_claim_token=?, event_lease_owner_id=?,
                event_lease_expires_at=?, event_last_error_code=NULL,
                updated_at=?
            WHERE request_explanation_id=? AND event_dispatch_attempts=?
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
                row["request_explanation_id"],
                attempts - 1,
                eligible_at,
                eligible_at,
            ),
        )
        if updated.rowcount != 1:
            connection.rollback()
            return None
        claim = _claim_from_row(
            row,
            token=token,
            owner=owner,
            attempts=attempts,
            lease_expires=lease_expires,
        )
        connection.commit()
        return claim
    # 领取阶段的锁、缺表或行损坏只返回固定事件错误，不能泄露数据库正文。
    except sqlite3.Error as exc:
        _rollback(connection)
        raise ApiError("request_explanation.event_unavailable") from exc
    finally:
        connection.close()


def dispatch_claimed_request_explanation_event(
    config: AppConfig,
    claim: RequestExplanationEventClaim,
    *,
    now: datetime | None = None,
) -> bool:
    """关闭 claim 事务后投递最小查看事件，并以 source lease CAS 回写结果。"""

    error_code: str | None = None
    try:
        event = build_webhook_event(
            tenant_id=claim.tenant_id,
            event_type="request.explanation_viewed",
            event_id=claim.event_id,
            timestamp=claim.viewed_at,
            actor_id=claim.actor_user_id,
            object_id=claim.request_explanation_id,
            trace_id=claim.operation_trace_id,
            payload={
                "schema_version": 1,
                "request_explanation_id": claim.request_explanation_id,
                "source_request_id": claim.source_request_id,
                "source_trace_id": claim.source_trace_id,
                "source_status": claim.source_status,
                "explanation_status": claim.explanation_status,
                "missing_sections": list(claim.missing_sections),
                "truncated_sections": list(claim.truncated_sections),
                "evidence_version": claim.evidence_version,
                "viewed_at": _to_db_time(claim.viewed_at),
            },
        )
        dispatch_webhook_event(
            config,
            claim.tenant_id,
            event,
            request_id=f"req_{claim.event_id}",
            trace_id=claim.operation_trace_id,
            resume_existing_pending=True,
        )
    # 信封校验失败只能采用共享白名单 code，不能把 payload 或任意异常文本持久化。
    except ValueError as exc:
        # ValueError 文本来自共享固定 code；不保留 payload 或任意异常正文。
        candidate = str(exc)
        error_code = (
            candidate
            if candidate in {"webhook.payload_invalid", "webhook.payload_sensitive"}
            else "webhook.event_type_invalid"
        )
    # dispatcher 的领域异常可能包装网络或存储细节，source outbox 只保留固定失败码。
    except ApiError:
        error_code = "webhook.delivery_unavailable"
    except Exception:
        # 网络/第三方异常可能含 URL 或响应内容，只折叠成固定安全错误码。
        error_code = "webhook.delivery_unavailable"
    completed_at = _finish_time(claim, _now(now))
    if error_code is None:
        return _finish_success(config, claim, completed_at)
    return _finish_failure(config, claim, completed_at, error_code)


def drain_request_explanation_event_once(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> bool:
    """领取并处理最多一个查看事件；没有可运行工作时返回 False。"""

    claim = claim_next_request_explanation_event(config, worker_id=worker_id, now=now)
    if claim is None:
        return False
    dispatch_claimed_request_explanation_event(config, claim, now=now)
    return True


def _finish_success(
    config: AppConfig,
    claim: RequestExplanationEventClaim,
    completed_at: datetime,
) -> bool:
    """仅在 source lease 仍有效且 token/owner 匹配时标记投递完成。

    返回 CAS 是否命中；旧 worker 失去 lease 时只返回 ``False``，不得覆盖新 owner 的状态。
    """

    timestamp = _to_db_time(completed_at)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        updated = connection.execute(
            """
            UPDATE request_explanations
            SET event_dispatch_status='dispatched', event_next_attempt_at=NULL,
                event_dispatched_at=?, event_claim_token=NULL,
                event_lease_owner_id=NULL, event_lease_expires_at=NULL,
                event_last_error_code=NULL, updated_at=?
            WHERE request_explanation_id=?
              AND event_dispatch_status='processing'
              AND event_dispatch_attempts=?
              AND event_claim_token=? AND event_lease_owner_id=?
              AND unixepoch(event_lease_expires_at)>unixepoch(?)
            """,
            (
                timestamp,
                timestamp,
                claim.request_explanation_id,
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
    # 完成 CAS 的数据库异常不能回显 SQL，也不能把未确认状态误报为 dispatched。
    except sqlite3.Error as exc:
        _rollback(connection)
        raise ApiError("request_explanation.event_unavailable") from exc
    finally:
        connection.close()


def _finish_failure(
    config: AppConfig,
    claim: RequestExplanationEventClaim,
    completed_at: datetime,
    error_code: str,
) -> bool:
    """按 source 尝试次数退避或在第十次终结 failed。"""

    allowed = {
        "webhook.event_type_invalid",
        "webhook.payload_invalid",
        "webhook.payload_sensitive",
        "webhook.delivery_unavailable",
        "request_explanation.event_unavailable",
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
            UPDATE request_explanations
            SET event_dispatch_status=?, event_next_attempt_at=?,
                event_dispatched_at=NULL, event_claim_token=NULL,
                event_lease_owner_id=NULL, event_lease_expires_at=NULL,
                event_last_error_code=?, updated_at=?
            WHERE request_explanation_id=?
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
                claim.request_explanation_id,
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
    # 失败回写仍受同一 lease/CAS 保护；数据库故障时禁止伪造退避或终态。
    except sqlite3.Error as exc:
        _rollback(connection)
        raise ApiError("request_explanation.event_unavailable") from exc
    finally:
        connection.close()


def _claim_from_row(
    row: sqlite3.Row,
    *,
    token: str,
    owner: str,
    attempts: int,
    lease_expires: datetime,
) -> RequestExplanationEventClaim:
    """从数据库行恢复不可变 claim，并严格校验 JSON 数组顺序、枚举与标量。"""

    missing = _section_array(row["missing_sections_json"])
    truncated = _section_array(row["truncated_sections_json"])
    if set(missing) & set(truncated):
        # 同一区块不能既缺失又截断，损坏的持久 JSON 不得进入已签名 Webhook。
        raise ApiError("request_explanation.event_unavailable")
    if row["source_status"] not in {"success", "failed"} or row["status"] not in {
        "complete",
        "partial",
    }:
        # 源请求与解释状态必须来自冻结枚举，未知值不能被降级成任意字符串投递。
        raise ApiError("request_explanation.event_unavailable")
    return RequestExplanationEventClaim(
        request_explanation_id=_identifier(row["request_explanation_id"]),
        event_id=_identifier(row["event_id"]),
        tenant_id=_identifier(row["tenant_id"]),
        source_request_id=_identifier(row["source_request_id"]),
        source_trace_id=_identifier(row["source_trace_id"]),
        actor_user_id=_identifier(row["actor_user_id"]),
        source_status=str(row["source_status"]),
        explanation_status=str(row["status"]),
        missing_sections=missing,
        truncated_sections=truncated,
        evidence_version=str(row["evidence_version"]),
        operation_trace_id=_identifier(row["operation_trace_id"]),
        viewed_at=_parse_time(row["viewed_at"]),
        event_claim_token=token,
        event_lease_owner_id=owner,
        event_dispatch_attempts=attempts,
        event_lease_expires_at=lease_expires,
    )


def _section_array(value: object) -> tuple[str, ...]:
    """解析稳定、有序、唯一的区块 JSON 数组；未知或损坏值失败关闭。"""

    try:
        parsed = json.loads(str(value))
    # JSON 损坏可能包含任意正文，错误响应和日志只能保留固定 outbox code。
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ApiError("request_explanation.event_unavailable") from exc
    if not isinstance(parsed, list) or any(type(item) is not str for item in parsed):
        # 区块数组只允许纯字符串列表，拒绝对象嵌套以阻断任意 JSON 穿透。
        raise ApiError("request_explanation.event_unavailable")
    positions = {name: index for index, name in enumerate(SECTION_ORDER)}
    try:
        indexes = [positions[item] for item in parsed]
    # 未知区块名表示持久合同漂移，不能由 worker 静默丢弃后继续投递。
    except KeyError as exc:
        raise ApiError("request_explanation.event_unavailable") from exc
    if indexes != sorted(set(indexes)):
        # 重复或乱序会改变事件签名和完整性语义，因此按损坏事实失败关闭。
        raise ApiError("request_explanation.event_unavailable")
    return tuple(parsed)


def _finish_time(
    claim: RequestExplanationEventClaim,
    current: datetime,
) -> datetime:
    """生成数据库秒级单调 finish 时刻，并确保仍处于 30 秒 lease。"""

    claimed_at = claim.event_lease_expires_at - timedelta(seconds=EVENT_LEASE_SECONDS)
    if current < claimed_at - timedelta(seconds=1):
        # 倒退时钟会破坏 lease 单调性，禁止用异常时间覆盖并发 worker 的状态。
        raise ApiError("request_explanation.event_unavailable")
    return claimed_at + timedelta(seconds=1) if current <= claimed_at else current


def _identifier(value: object) -> str:
    """恢复 1..160 的受限 ASCII 标识，不把任意文本带入事件或日志。"""

    raw = str(value or "")
    if (
        not 1 <= len(raw) <= 160
        or not raw.isascii()
        or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.:-" for character in raw)
    ):
        # 事件标识只允许受限 ASCII，避免控制字符进入签名、查询条件或结构化日志。
        raise ApiError("request_explanation.event_unavailable")
    return raw


def _worker_id(value: object) -> str:
    """校验内部 lease owner，禁止控制字符与无界文本。"""

    raw = str(value or "").strip()
    if not 1 <= len(raw) <= 128 or any(ord(character) < 32 for character in raw):
        # lease owner 会进入持久 CAS 条件，控制字符或无界文本必须在开事务前拒绝。
        raise ApiError("request_explanation.event_unavailable")
    return raw


def _non_negative_int(value: object) -> int:
    """恢复严格非负 SQLite 尝试次数，拒绝布尔值、浮点值与负数状态。"""

    if type(value) is not int or value < 0:
        # 尝试次数参与退避和终态判断，类型漂移不能被 Python 隐式转换掩盖。
        raise ApiError("request_explanation.event_unavailable")
    return value


def _now(value: datetime | None) -> datetime:
    """返回 UTC 秒级服务器时刻；仅专项测试可注入。"""

    current = value if value is not None else datetime.now(UTC)
    if not isinstance(current, datetime) or current.tzinfo is None:
        # naive 时间无法与 UTC lease 比较，继续执行会造成提前接管或重复投递。
        raise ApiError("request_explanation.event_unavailable")
    return current.astimezone(UTC).replace(microsecond=0)


def _parse_time(value: object) -> datetime:
    """解析有时区持久时间，损坏值按 source outbox 不可用处理。"""

    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    # 持久时间解析失败不得回显原值，避免数据库正文进入日志或 HTTP 错误。
    except (TypeError, ValueError) as exc:
        raise ApiError("request_explanation.event_unavailable") from exc
    if parsed.tzinfo is None:
        # 无时区时间会破坏 lease 到期比较，不能按本机时区猜测。
        raise ApiError("request_explanation.event_unavailable")
    return parsed.astimezone(UTC)


def _to_db_time(value: datetime) -> str:
    """编码 canonical UTC 秒级时间，保证 SQLite 比较、事件签名与测试结果稳定。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _connect(config: AppConfig) -> sqlite3.Connection:
    """创建 outbox 独享连接并隐藏数据库位置。"""

    try:
        return connect_database(config.database_url)
    # 连接错误可能携带数据库绝对路径，统一折叠后才能进入 scheduler 日志。
    except Exception as exc:
        raise ApiError("request_explanation.event_unavailable") from exc


def _rollback(connection: sqlite3.Connection) -> None:
    """尽力回滚且不遮蔽最初的稳定领域错误。"""

    try:
        connection.rollback()
    # 回滚失败属于清理阶段，不能遮蔽最初更准确的领域错误码。
    except sqlite3.Error:
        return


__all__ = [
    "RequestExplanationEventClaim",
    "claim_next_request_explanation_event",
    "dispatch_claimed_request_explanation_event",
    "drain_request_explanation_event_once",
]
