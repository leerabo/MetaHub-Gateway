"""PRD-081 标准安全事件聚合、delivery 快照和可恢复投递状态机。

0074 trigger 只在权威 DLP、会话风险或 Provider 凭据事实事务内建立 source、member、
aggregate 与 pending outbox。本模块只处理已提交的安全投影，不读取 Prompt、Response、
Provider Key、Credential 标识、规则正文或接收端响应正文。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.security_events import (
    SECURITY_EVENT_ACTOR,
    SECURITY_EVENT_MAX_OCCURRENCE_COUNT,
    SECURITY_EVENT_MAX_SOURCE_COUNT,
    SECURITY_EVENT_TYPE,
)
from metahub_shared.runtime.webhooks import WebhookEvent, build_webhook_event

LEASE_SECONDS = 90
MAX_FAILURE_ATTEMPTS = 10
BACKOFF_SECONDS = (5, 30, 300)
_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_EVENT_ID = re.compile(r"^sevt_[0-9a-f]{32}$")
_OUTBOX_ID = re.compile(r"^seob_[0-9a-f]{32}$")


@dataclass(frozen=True, slots=True)
class SecurityEventClaim:
    """一次不可转让的 aggregate lease；token/owner 只参与数据库 CAS。"""

    outbox_id: str
    tenant_id: str
    security_event_id: str
    claim_token: str
    lease_owner_id: str
    lease_generation: int
    failure_attempts: int
    lease_expires_at: datetime


def claim_next_security_event(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> SecurityEventClaim | None:
    """原子领取一条到期 aggregate；过期 processing lease 可由新 generation 接管。"""

    owner = _worker_id(worker_id)
    current = _now(now)
    current_text = _db_time(current)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT * FROM security_event_outbox
            WHERE attempt_count<? AND (
              (status='pending' AND unixepoch(next_attempt_at)<=unixepoch(?))
              OR (status='processing' AND unixepoch(lease_expires_at)<=unixepoch(?))
            )
            ORDER BY CASE status WHEN 'processing' THEN 0 ELSE 1 END,
                     CASE status WHEN 'processing' THEN lease_expires_at ELSE next_attempt_at END,
                     created_at,security_event_id
            LIMIT 1
            """,
            (MAX_FAILURE_ATTEMPTS, current_text, current_text),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        event_id = _matched(row["security_event_id"], _EVENT_ID)
        outbox_id = _matched(row["security_event_outbox_id"], _OUTBOX_ID)
        if outbox_id != "seob_" + event_id[-32:]:
            raise ApiError("security_event.unavailable")
        tenant_id = _safe_text(row["tenant_id"], maximum=160)
        old_generation = _strict_int(row["lease_generation"], 0, 2_147_483_646)
        failures = _strict_int(row["attempt_count"], 0, MAX_FAILURE_ATTEMPTS - 1)
        token = uuid4().hex
        claimed_at = max(current, _parse_time(row["updated_at"]) + timedelta(seconds=1))
        lease_expires = claimed_at + timedelta(seconds=LEASE_SECONDS)
        updated = connection.execute(
            """
            UPDATE security_event_outbox
            SET status='processing',lease_generation=?,lease_token=?,lease_owner_id=?,
                lease_expires_at=?,last_error_code=NULL,updated_at=?
            WHERE tenant_id=? AND security_event_outbox_id=? AND lease_generation=?
              AND attempt_count=? AND (
                (status='pending' AND unixepoch(next_attempt_at)<=unixepoch(?))
                OR (status='processing' AND unixepoch(lease_expires_at)<=unixepoch(?))
              )
            """,
            (
                old_generation + 1,
                token,
                owner,
                _db_time(lease_expires),
                _db_time(claimed_at),
                tenant_id,
                outbox_id,
                old_generation,
                failures,
                current_text,
                current_text,
            ),
        )
        if updated.rowcount != 1:
            connection.rollback()
            return None
        connection.commit()
        return SecurityEventClaim(
            outbox_id=outbox_id,
            tenant_id=tenant_id,
            security_event_id=event_id,
            claim_token=token,
            lease_owner_id=owner,
            lease_generation=old_generation + 1,
            failure_attempts=failures,
            lease_expires_at=lease_expires,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, TypeError, ValueError) as exc:
        _rollback(connection)
        raise ApiError("security_event.unavailable") from exc
    finally:
        connection.close()


def dispatch_claimed_security_event(
    config: AppConfig,
    claim: SecurityEventClaim,
    *,
    now: datetime | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """冻结并恢复一个 aggregate 的全部 Endpoint delivery，再以完整 lease CAS 收敛。"""

    if should_stop is not None and should_stop():
        return _release_for_stop(config, claim, current=_now(now))
    if not _has_active_lease(config, claim, current=_now(now)):
        return False
    active_expires = claim.lease_expires_at

    def preserve_lease() -> None:
        """每次真实网络前检查停服并续租，防止迟到 owner 访问客户 Endpoint。"""

        nonlocal active_expires
        if should_stop is not None and should_stop():
            raise ApiError("security_event.unavailable")
        active_expires = _renew_lease(
            config,
            claim,
            expected_expires=active_expires,
            current=_now(now),
        )

    error_code: str | None = None
    try:
        event, active_expires = _freeze_snapshot(
            config,
            claim,
            expected_expires=active_expires,
            current=_now(now),
        )
        from metahub_control.webhooks.service import dispatch_webhook_event

        deliveries = dispatch_webhook_event(
            config,
            claim.tenant_id,
            event,
            request_id=f"req_{claim.security_event_id}",
            trace_id=event.trace_id,
            resume_existing_pending=True,
            source_lease_guard=preserve_lease,
            create_snapshot_if_missing=False,
        )
        pending = [delivery for delivery in deliveries if delivery.status == "pending"]
        if pending:
            if any(delivery.next_retry_at is None for delivery in pending):
                raise ApiError("security_event.unavailable")
            return _defer_rate_limited(
                config,
                claim,
                expected_expires=active_expires,
                current=_finish_time(claim, _now(now)),
            )
        if any(delivery.status not in {"success", "failed"} for delivery in deliveries):
            raise ApiError("security_event.unavailable")
        succeeded = all(delivery.status == "success" for delivery in deliveries)
        return _finish_terminal(
            config,
            claim,
            expected_expires=active_expires,
            current=_finish_time(claim, _now(now)),
            succeeded=succeeded,
        )
    except ValueError:
        error_code = "webhook.payload_invalid"
    except ApiError:
        error_code = "security_event.unavailable"
    except Exception:
        # 第三方或测试桩异常可能携带正文、URL 或 Secret，只允许固定分类落库。
        error_code = "security_event.unavailable"
    return _finish_failure(
        config,
        claim,
        expected_expires=active_expires,
        current=_finish_time(claim, _now(now)),
        error_code=error_code,
    )


def drain_security_event_once(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """处理最多一个 aggregate；真实时钟下顺带清理可删除的 30 天 rate claim。"""

    cleaned = cleanup_expired_rate_claims(config) if now is None else 0
    claim = claim_next_security_event(config, worker_id=worker_id, now=now)
    if claim is None:
        return cleaned > 0
    dispatch_claimed_security_event(
        config,
        claim,
        now=now,
        should_stop=should_stop,
    )
    return True


def cleanup_expired_rate_claims(config: AppConfig) -> int:
    """删除已过 30 天且 delivery 已终态的 slot；pending 证据由 trigger 再次保护。"""

    current = _db_time(_now(None))
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """
            DELETE FROM webhook_security_rate_claims
            WHERE unixepoch(expires_at)<=unixepoch(?) AND EXISTS(
              SELECT 1 FROM webhook_deliveries delivery
              WHERE delivery.tenant_id=webhook_security_rate_claims.tenant_id
                AND delivery.webhook_delivery_id=webhook_security_rate_claims.webhook_delivery_id
                AND delivery.status IN('success','failed')
            )
            """,
            (current,),
        )
        connection.commit()
        return max(int(cursor.rowcount), 0)
    except sqlite3.Error as exc:
        _rollback(connection)
        raise ApiError("security_event.unavailable") from exc
    finally:
        connection.close()


def _freeze_snapshot(
    config: AppConfig,
    claim: SecurityEventClaim,
    *,
    expected_expires: datetime,
    current: datetime,
) -> tuple[WebhookEvent, datetime]:
    """反算全部 member、构造 exact 事件，并原子冻结当时订阅者及 format/wire/rate。"""

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        outbox = _load_owned_outbox(
            connection,
            claim,
            expected_expires=expected_expires,
            current=current,
        )
        if outbox is None:
            raise ApiError("security_event.unavailable")
        aggregate = connection.execute(
            """
            SELECT * FROM security_event_aggregates
            WHERE tenant_id=? AND security_event_id=?
            """,
            (claim.tenant_id, claim.security_event_id),
        ).fetchone()
        if aggregate is None:
            raise ApiError("security_event.unavailable")
        members = connection.execute(
            """
            SELECT source.*
            FROM security_event_aggregate_members member
            JOIN security_event_sources source
              ON source.tenant_id=member.tenant_id
             AND source.security_event_source_id=member.security_event_source_id
            WHERE member.tenant_id=? AND member.security_event_id=?
            ORDER BY source.occurred_at,source.security_event_source_id
            """,
            (claim.tenant_id, claim.security_event_id),
        ).fetchall()
        event = _event_from_aggregate(aggregate, members)
        source_count = len(members)
        snapshot_source_count = outbox["snapshot_source_count"]
        snapshot_endpoint_count = outbox["snapshot_endpoint_count"]
        if (snapshot_source_count is None) != (snapshot_endpoint_count is None):
            raise ApiError("security_event.unavailable")
        frozen_at = max(current, _parse_time(outbox["updated_at"]))
        renewed_expires = max(
            expected_expires,
            frozen_at + timedelta(seconds=LEASE_SECONDS),
        )
        if snapshot_source_count is None:
            from metahub_control.webhooks.service import freeze_webhook_event_delivery_snapshot

            endpoint_count = freeze_webhook_event_delivery_snapshot(
                connection,
                event,
                frozen_at=_db_time(frozen_at),
            )
            marker_clause = (
                "snapshot_source_count IS NULL AND snapshot_endpoint_count IS NULL"
            )
        else:
            if (
                _strict_int(
                    snapshot_source_count,
                    1,
                    SECURITY_EVENT_MAX_SOURCE_COUNT,
                )
                != source_count
            ):
                raise ApiError("security_event.unavailable")
            endpoint_count = connection.execute(
                """
                SELECT COUNT(*) FROM webhook_deliveries
                WHERE tenant_id=? AND event_id=? AND event_type=? AND delivery_kind='original'
                """,
                (claim.tenant_id, claim.security_event_id, SECURITY_EVENT_TYPE),
            ).fetchone()[0]
            if endpoint_count != _strict_int(snapshot_endpoint_count, 0, 10_000):
                raise ApiError("security_event.unavailable")
            marker_clause = (
                "snapshot_source_count=? AND snapshot_endpoint_count=?"
            )
        params: list[object] = [
            source_count,
            endpoint_count,
            _db_time(renewed_expires),
            _db_time(frozen_at),
            claim.tenant_id,
            claim.outbox_id,
            claim.lease_generation,
            claim.claim_token,
            claim.lease_owner_id,
            _db_time(expected_expires),
            _db_time(current),
        ]
        if snapshot_source_count is not None:
            params.extend([source_count, endpoint_count])
        updated = connection.execute(
            f"""
            UPDATE security_event_outbox
            SET snapshot_source_count=?,snapshot_endpoint_count=?,lease_expires_at=?,updated_at=?
            WHERE tenant_id=? AND security_event_outbox_id=? AND status='processing'
              AND lease_generation=? AND lease_token=? AND lease_owner_id=?
              AND lease_expires_at=? AND unixepoch(lease_expires_at)>unixepoch(?)
              AND {marker_clause}
            """,
            params,
        )
        if updated.rowcount != 1:
            raise ApiError("security_event.unavailable")
        connection.commit()
        return event, renewed_expires
    except ApiError:
        _rollback(connection)
        raise
    except ValueError:
        _rollback(connection)
        raise
    except (sqlite3.Error, TypeError) as exc:
        _rollback(connection)
        raise ApiError("security_event.unavailable") from exc
    finally:
        connection.close()


def _event_from_aggregate(
    aggregate: sqlite3.Row,
    members: list[sqlite3.Row],
) -> WebhookEvent:
    """逐成员重算 aggregate 并交给 Shared exact validator 构造签名事件。"""

    source_count = len(members)
    if not 1 <= source_count <= SECURITY_EVENT_MAX_SOURCE_COUNT:
        raise ValueError("webhook.payload_invalid")
    occurrence_count = sum(
        _payload_int(member["occurrence_count"], 1, SECURITY_EVENT_MAX_SOURCE_COUNT)
        for member in members
    )
    if occurrence_count > SECURITY_EVENT_MAX_OCCURRENCE_COUNT:
        raise ValueError("webhook.payload_invalid")
    first = members[0]
    last = members[-1]
    updated_at = max(str(member["created_at"]) for member in members)
    expected = {
        "source_event_type": first["source_event_type"],
        "category": first["category"],
        "severity": first["severity"],
        "severity_score": first["severity_score"],
        "action": first["action"],
        "outcome": first["outcome"],
        "resource_type": first["resource_type"],
        "resource_id": first["resource_id"],
    }
    if any(any(member[key] != value for key, value in expected.items()) for member in members):
        raise ValueError("webhook.payload_invalid")
    if any(member["tenant_id"] != aggregate["tenant_id"] for member in members):
        raise ValueError("webhook.payload_invalid")
    comparisons = {
        **expected,
        "event_count": occurrence_count,
        "source_event_count": source_count,
        "first_seen_at": first["occurred_at"],
        "last_seen_at": last["occurred_at"],
        "first_request_id": first["source_request_id"],
        "last_request_id": last["source_request_id"],
        "first_trace_id": first["source_trace_id"],
        "last_trace_id": last["source_trace_id"],
        "updated_at": updated_at,
    }
    if any(aggregate[key] != value for key, value in comparisons.items()):
        raise ValueError("webhook.payload_invalid")
    event_id = _payload_matched(aggregate["security_event_id"], _EVENT_ID)
    tenant_id = _payload_text(aggregate["tenant_id"], maximum=160)
    window_started = _canonical_time(aggregate["window_started_at"])
    window_ended = _canonical_time(aggregate["window_ended_at"])
    if window_ended - window_started != timedelta(seconds=5):
        raise ValueError("webhook.payload_invalid")
    return build_webhook_event(
        tenant_id=tenant_id,
        event_type=SECURITY_EVENT_TYPE,
        event_id=event_id,
        timestamp=window_ended,
        actor_id=SECURITY_EVENT_ACTOR,
        object_id=event_id,
        trace_id=str(aggregate["last_trace_id"]),
        source_request_id=str(aggregate["last_request_id"]),
        payload={
            "schema_version": 1,
            "security_event_id": event_id,
            "status": "aggregated",
            "category": aggregate["category"],
            "source_event_type": aggregate["source_event_type"],
            "severity": aggregate["severity"],
            "severity_score": aggregate["severity_score"],
            "action": aggregate["action"],
            "outcome": aggregate["outcome"],
            "resource_type": aggregate["resource_type"],
            "resource_id": aggregate["resource_id"],
            "event_count": occurrence_count,
            "source_event_count": source_count,
            "first_seen_at": first["occurred_at"],
            "last_seen_at": last["occurred_at"],
            "window_started_at": aggregate["window_started_at"],
            "window_ended_at": aggregate["window_ended_at"],
            "first_request_id": first["source_request_id"],
            "last_request_id": last["source_request_id"],
            "created_at": aggregate["created_at"],
            "updated_at": aggregate["updated_at"],
        },
    )


def _renew_lease(
    config: AppConfig,
    claim: SecurityEventClaim,
    *,
    expected_expires: datetime,
    current: datetime,
) -> datetime:
    """在真实 HTTP 前用完整 claim CAS 续租并返回新的精确 expiry。"""

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = _load_owned_outbox(
            connection,
            claim,
            expected_expires=expected_expires,
            current=current,
        )
        if row is None:
            raise ApiError("security_event.unavailable")
        updated_at = max(current, _parse_time(row["updated_at"]) + timedelta(seconds=1))
        renewed = max(expected_expires, updated_at + timedelta(seconds=LEASE_SECONDS))
        cursor = connection.execute(
            """
            UPDATE security_event_outbox SET lease_expires_at=?,updated_at=?
            WHERE tenant_id=? AND security_event_outbox_id=? AND status='processing'
              AND lease_generation=? AND lease_token=? AND lease_owner_id=?
              AND lease_expires_at=? AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                _db_time(renewed),
                _db_time(updated_at),
                claim.tenant_id,
                claim.outbox_id,
                claim.lease_generation,
                claim.claim_token,
                claim.lease_owner_id,
                _db_time(expected_expires),
                _db_time(current),
            ),
        )
        if cursor.rowcount != 1:
            raise ApiError("security_event.unavailable")
        connection.commit()
        return renewed
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.Error as exc:
        _rollback(connection)
        raise ApiError("security_event.unavailable") from exc
    finally:
        connection.close()


def _defer_rate_limited(
    config: AppConfig,
    claim: SecurityEventClaim,
    *,
    expected_expires: datetime,
    current: datetime,
) -> bool:
    """把 pending delivery 的最早精确 due time 投影回 source，零 HTTP attempt 不计失败。"""

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            """
            SELECT status,next_retry_at,event_type FROM webhook_deliveries
            WHERE tenant_id=? AND event_id=? AND delivery_kind='original'
            """,
            (claim.tenant_id, claim.security_event_id),
        ).fetchall()
        pending = [row for row in rows if row["status"] == "pending"]
        if (
            not pending
            or any(row["event_type"] != SECURITY_EVENT_TYPE for row in rows)
            or any(row["next_retry_at"] is None for row in pending)
        ):
            raise ApiError("security_event.unavailable")
        due_at = min(str(row["next_retry_at"]) for row in pending)
        if due_at <= _db_time(current):
            raise ApiError("security_event.unavailable")
        cursor = connection.execute(
            """
            UPDATE security_event_outbox
            SET status='pending',lease_token=NULL,lease_owner_id=NULL,lease_expires_at=NULL,
                next_attempt_at=?,last_error_code=NULL,updated_at=?
            WHERE tenant_id=? AND security_event_outbox_id=? AND status='processing'
              AND lease_generation=? AND lease_token=? AND lease_owner_id=?
              AND lease_expires_at=? AND unixepoch(lease_expires_at)>unixepoch(?)
              AND snapshot_endpoint_count=?
            """,
            (
                due_at,
                _db_time(current),
                claim.tenant_id,
                claim.outbox_id,
                claim.lease_generation,
                claim.claim_token,
                claim.lease_owner_id,
                _db_time(expected_expires),
                _db_time(current),
                len(rows),
            ),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            return False
        connection.commit()
        return True
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.Error as exc:
        _rollback(connection)
        raise ApiError("security_event.unavailable") from exc
    finally:
        connection.close()


def _finish_terminal(
    config: AppConfig,
    claim: SecurityEventClaim,
    *,
    expected_expires: datetime,
    current: datetime,
    succeeded: bool,
) -> bool:
    """只有全部 delivery 终态后才提交 source succeeded/failed。"""

    timestamp = _db_time(current)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        counts = connection.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending,
                   SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed
            FROM webhook_deliveries
            WHERE tenant_id=? AND event_id=? AND event_type=? AND delivery_kind='original'
            """,
            (claim.tenant_id, claim.security_event_id, SECURITY_EVENT_TYPE),
        ).fetchone()
        total = int(counts["total"] or 0)
        failed = int(counts["failed"] or 0)
        if int(counts["pending"] or 0) != 0 or succeeded != (failed == 0):
            raise ApiError("security_event.unavailable")
        cursor = connection.execute(
            """
            UPDATE security_event_outbox
            SET status=?,attempt_count=attempt_count+?,lease_token=NULL,lease_owner_id=NULL,
                lease_expires_at=NULL,next_attempt_at=?,last_error_code=?,updated_at=?,
                succeeded_at=?,failed_at=?
            WHERE tenant_id=? AND security_event_outbox_id=? AND status='processing'
              AND lease_generation=? AND lease_token=? AND lease_owner_id=?
              AND lease_expires_at=? AND unixepoch(lease_expires_at)>unixepoch(?)
              AND snapshot_endpoint_count=?
            """,
            (
                "succeeded" if succeeded else "failed",
                0 if succeeded else 1,
                timestamp,
                None if succeeded else "webhook.delivery_unavailable",
                timestamp,
                timestamp if succeeded else None,
                None if succeeded else timestamp,
                claim.tenant_id,
                claim.outbox_id,
                claim.lease_generation,
                claim.claim_token,
                claim.lease_owner_id,
                _db_time(expected_expires),
                timestamp,
                total,
            ),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            return False
        connection.commit()
        return True
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.Error as exc:
        _rollback(connection)
        raise ApiError("security_event.unavailable") from exc
    finally:
        connection.close()


def _finish_failure(
    config: AppConfig,
    claim: SecurityEventClaim,
    *,
    expected_expires: datetime,
    current: datetime,
    error_code: str,
) -> bool:
    """确定性 payload 立即隔离；瞬时故障按 5/30/300 秒退避，最多十次。"""

    reason = (
        "webhook.payload_invalid"
        if error_code == "webhook.payload_invalid"
        else "security_event.unavailable"
    )
    next_attempts = claim.failure_attempts + 1
    terminal = reason == "webhook.payload_invalid" or next_attempts >= MAX_FAILURE_ATTEMPTS
    delay = BACKOFF_SECONDS[min(next_attempts - 1, len(BACKOFF_SECONDS) - 1)]
    timestamp = _db_time(current)
    due_at = _db_time(current + timedelta(seconds=delay))
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if terminal:
            from metahub_control.webhooks.service import fail_pending_webhook_event_deliveries

            fail_pending_webhook_event_deliveries(
                connection,
                tenant_id=claim.tenant_id,
                event_id=claim.security_event_id,
                event_type=SECURITY_EVENT_TYPE,
                updated_at=timestamp,
                failure_code=(
                    "webhook.payload_invalid"
                    if reason == "webhook.payload_invalid"
                    else "webhook.delivery_unavailable"
                ),
            )
        cursor = connection.execute(
            """
            UPDATE security_event_outbox
            SET status=?,attempt_count=?,lease_token=NULL,lease_owner_id=NULL,
                lease_expires_at=NULL,next_attempt_at=?,last_error_code=?,updated_at=?,
                succeeded_at=NULL,failed_at=?
            WHERE tenant_id=? AND security_event_outbox_id=? AND status='processing'
              AND lease_generation=? AND lease_token=? AND lease_owner_id=?
              AND lease_expires_at=? AND unixepoch(lease_expires_at)>unixepoch(?)
              AND attempt_count=?
            """,
            (
                "failed" if terminal else "pending",
                next_attempts,
                timestamp if terminal else due_at,
                reason,
                timestamp,
                timestamp if terminal else None,
                claim.tenant_id,
                claim.outbox_id,
                claim.lease_generation,
                claim.claim_token,
                claim.lease_owner_id,
                _db_time(expected_expires),
                timestamp,
                claim.failure_attempts,
            ),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            return False
        connection.commit()
        return True
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.Error as exc:
        _rollback(connection)
        raise ApiError("security_event.unavailable") from exc
    finally:
        connection.close()


def _release_for_stop(
    config: AppConfig,
    claim: SecurityEventClaim,
    *,
    current: datetime,
) -> bool:
    """零网络停服立即归还 lease，不消耗 source failure attempt。"""

    timestamp = _db_time(_finish_time(claim, current))
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """
            UPDATE security_event_outbox
            SET status='pending',lease_token=NULL,lease_owner_id=NULL,lease_expires_at=NULL,
                next_attempt_at=?,last_error_code=NULL,updated_at=?
            WHERE tenant_id=? AND security_event_outbox_id=? AND status='processing'
              AND lease_generation=? AND lease_token=? AND lease_owner_id=?
              AND lease_expires_at=? AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                timestamp,
                timestamp,
                claim.tenant_id,
                claim.outbox_id,
                claim.lease_generation,
                claim.claim_token,
                claim.lease_owner_id,
                _db_time(claim.lease_expires_at),
                timestamp,
            ),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            return False
        connection.commit()
        return True
    except sqlite3.Error as exc:
        _rollback(connection)
        raise ApiError("security_event.unavailable") from exc
    finally:
        connection.close()


def _has_active_lease(
    config: AppConfig,
    claim: SecurityEventClaim,
    *,
    current: datetime,
) -> bool:
    """联网或冻结前验证完整 lease owner，数据库故障不得降级为盲发。"""

    connection = _connect(config)
    try:
        return _load_owned_outbox(
            connection,
            claim,
            expected_expires=claim.lease_expires_at,
            current=current,
        ) is not None
    except sqlite3.Error as exc:
        raise ApiError("security_event.unavailable") from exc
    finally:
        connection.close()


def _load_owned_outbox(
    connection: sqlite3.Connection,
    claim: SecurityEventClaim,
    *,
    expected_expires: datetime,
    current: datetime,
) -> sqlite3.Row | None:
    """以 tenant/outbox/event/generation/token/owner/expiry 七重条件读取当前 lease。"""

    return connection.execute(
        """
        SELECT * FROM security_event_outbox
        WHERE tenant_id=? AND security_event_outbox_id=? AND security_event_id=?
          AND status='processing' AND lease_generation=? AND lease_token=?
          AND lease_owner_id=? AND lease_expires_at=?
          AND unixepoch(lease_expires_at)>unixepoch(?)
        """,
        (
            claim.tenant_id,
            claim.outbox_id,
            claim.security_event_id,
            claim.lease_generation,
            claim.claim_token,
            claim.lease_owner_id,
            _db_time(expected_expires),
            _db_time(current),
        ),
    ).fetchone()


def _finish_time(claim: SecurityEventClaim, current: datetime) -> datetime:
    """生成不早于领取秒后一秒的 CAS 时间，并拒绝明显时钟回退。"""

    claimed_at = claim.lease_expires_at - timedelta(seconds=LEASE_SECONDS)
    if current < claimed_at - timedelta(seconds=1):
        raise ApiError("security_event.unavailable")
    return claimed_at + timedelta(seconds=1) if current <= claimed_at else current


def _canonical_time(value: object) -> datetime:
    """严格读取 SQLite 中的 UTC 秒，禁止本地时区或非规范同义文本。"""

    parsed = _parse_time(value)
    if _db_time(parsed) != value:
        raise ValueError("webhook.payload_invalid")
    return parsed


def _parse_time(value: object) -> datetime:
    if type(value) is not str or len(value) != 20 or not value.endswith("Z"):
        raise ValueError("invalid database time")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("invalid database time")
    return parsed.astimezone(UTC).replace(microsecond=0)


def _matched(value: object, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise ApiError("security_event.unavailable")
    return value


def _payload_matched(value: object, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise ValueError("webhook.payload_invalid")
    return value


def _safe_text(value: object, *, maximum: int) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= maximum
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise ApiError("security_event.unavailable")
    return value


def _payload_text(value: object, *, maximum: int) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= maximum
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise ValueError("webhook.payload_invalid")
    return value


def _strict_int(value: object, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ApiError("security_event.unavailable")
    return value


def _payload_int(value: object, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("webhook.payload_invalid")
    return value


def _worker_id(value: object) -> str:
    if type(value) is not str or _WORKER_ID.fullmatch(value) is None:
        raise ApiError("security_event.unavailable")
    return value


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except DbError as exc:
        raise ApiError("security_event.unavailable") from exc


def _now(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).replace(microsecond=0)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _rollback(connection: sqlite3.Connection) -> None:
    try:
        connection.rollback()
    except sqlite3.Error:
        return


__all__ = [
    "SecurityEventClaim",
    "claim_next_security_event",
    "cleanup_expired_rate_claims",
    "dispatch_claimed_security_event",
    "drain_security_event_once",
]
