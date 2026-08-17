"""PRD-086 客户健康变更 source outbox 的订阅快照、租约与崩溃恢复。"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import WebhookEvent, build_webhook_event

from .service import _validated_row

# 事件租约、重试次数、退避和标识格式共同限定 source outbox 的恢复边界。
EVENT_LEASE_SECONDS = 90
EVENT_MAX_ATTEMPTS = 10
EVENT_BACKOFF_SECONDS = (5, 30, 300)
_SCORE_ID = re.compile(r"^chs_[0-9a-f]{32}$")
_OUTBOX_ID = re.compile(r"^chso_[0-9a-f]{32}$")
_EVENT_ID = re.compile(r"^evt_chs_[0-9a-f]{32}$")
_DELIVERY_ID = re.compile(r"^whd_[0-9a-f]{32}$")
_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


@dataclass(slots=True)
class CustomerHealthEventClaim:
    """保存 source lease；公开事件与令牌均不进入调试表示。"""

    customer_health_score_id: str
    tenant_id: str
    source_request_id: str
    source_trace_id: str
    event: WebhookEvent = field(repr=False)
    lease_owner_id: str
    lease_token: str = field(repr=False)
    attempt_count: int
    generation: int
    lease_expires_at: datetime


def claim_next_customer_health_event(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> CustomerHealthEventClaim | None:
    """领取至多一个到期客户健康事件，并隐藏内部进度标记。"""

    claim, _progressed = _claim_with_progress(config, worker_id=worker_id, now=now)
    return claim


def _claim_with_progress(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> tuple[CustomerHealthEventClaim | None, bool]:
    """以只读预探测和短写事务领取事件，同时报告坏记录是否已推进。"""

    owner = _worker(worker_id)
    connection = _connect(config)
    try:
        current = _transaction_now(connection, now)
        now_text = _db_time(current)
        due = connection.execute(
            """
            SELECT 1 FROM customer_health_score_outbox
            WHERE (status='pending' AND unixepoch(next_attempt_at)<=unixepoch(?))
               OR (status='leased' AND unixepoch(lease_expires_at)<=unixepoch(?))
            LIMIT 1
            """,
            (now_text, now_text),
        ).fetchone()
        if due is None:
            return None, False
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        now_text = _db_time(current)
        row = connection.execute(
            """
            SELECT score.*,outbox.customer_health_score_outbox_id,
                   outbox.webhook_event_id,outbox.status AS outbox_status,
                   outbox.attempt_count,outbox.generation,outbox.lease_owner_id,
                   outbox.lease_token,outbox.lease_expires_at,outbox.next_attempt_at,
                   outbox.subscription_snapshot_json,outbox.snapshot_frozen_at,
                   outbox.last_error_code,outbox.delivered_at,
                   outbox.created_at AS outbox_created_at,
                   outbox.updated_at AS outbox_updated_at
            FROM customer_health_score_outbox outbox
            JOIN customer_health_scores score
              ON score.tenant_id=outbox.tenant_id
             AND score.customer_health_score_id=outbox.customer_health_score_id
            WHERE (outbox.status='pending' AND unixepoch(outbox.next_attempt_at)<=unixepoch(?))
               OR (outbox.status='leased' AND unixepoch(outbox.lease_expires_at)<=unixepoch(?))
            ORDER BY COALESCE(outbox.next_attempt_at,outbox.lease_expires_at),
                     outbox.tenant_id,outbox.created_at,outbox.customer_health_score_outbox_id
            LIMIT 1
            """,
            (now_text, now_text),
        ).fetchone()
        if row is None:
            connection.commit()
            return None, False
        try:
            event, source_request_id, source_trace_id = _event_from_row(row)
        # 坏评分或事件行也要累计有界失败次数，不能永久阻塞公平队列。
        except (ApiError, TypeError, ValueError, KeyError):
            _record_corrupt_attempt(connection, row, owner=owner, current=current)
            connection.commit()
            return None, True

        if row["snapshot_frozen_at"] is None:
            from metahub_control.webhooks.service import freeze_webhook_event_delivery_snapshot

            frozen_at = now_text
            freeze_webhook_event_delivery_snapshot(connection, event, frozen_at=frozen_at)
            snapshot = _delivery_snapshot(connection, event)
        else:
            frozen_at = _time_text(row["snapshot_frozen_at"])
            snapshot = _parse_snapshot(row["subscription_snapshot_json"])
            # 已冻结订阅集合必须与派生 delivery 完全一致，漂移说明恢复状态不可信。
            if snapshot != _delivery_snapshot(connection, event):
                raise ApiError("customer_health.unavailable")

        old_status = _outbox_status(row["outbox_status"])
        old_attempt = _integer(row["attempt_count"], minimum=0, maximum=10)
        attempt = old_attempt + 1 if old_status == "pending" else old_attempt
        # 尝试次数必须在冻结范围内，防止损坏计数绕过终态或无限重试。
        if not 1 <= attempt <= EVENT_MAX_ATTEMPTS:
            raise ApiError("customer_health.unavailable")
        generation = _integer(row["generation"], minimum=0) + 1
        token = secrets.token_hex(32)
        expires = current + timedelta(seconds=EVENT_LEASE_SECONDS)
        connection.execute(
            """
            UPDATE customer_health_score_outbox
            SET status='leased',attempt_count=?,generation=?,lease_owner_id=?,lease_token=?,
                lease_expires_at=?,next_attempt_at=NULL,subscription_snapshot_json=?,
                snapshot_frozen_at=?,last_error_code=NULL,delivered_at=NULL,updated_at=?
            WHERE tenant_id=? AND customer_health_score_id=? AND status=?
              AND attempt_count=? AND generation=?
              AND lease_owner_id IS ? AND lease_token IS ? AND lease_expires_at IS ?
              AND next_attempt_at IS ? AND last_error_code IS ? AND delivered_at IS ?
              AND subscription_snapshot_json IS ? AND snapshot_frozen_at IS ?
              AND (status='pending' OR unixepoch(lease_expires_at)<=unixepoch(?))
            """,
            (
                attempt, generation, owner, token, _db_time(expires),
                _canonical_json(snapshot), frozen_at, now_text, row["tenant_id"],
                row["customer_health_score_id"], old_status, old_attempt,
                row["generation"], row["lease_owner_id"], row["lease_token"],
                row["lease_expires_at"], row["next_attempt_at"], row["last_error_code"],
                row["delivered_at"], row["subscription_snapshot_json"],
                row["snapshot_frozen_at"], now_text,
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            connection.rollback()
            return None, False
        claim = CustomerHealthEventClaim(
            customer_health_score_id=_text(row["customer_health_score_id"]),
            tenant_id=_text(row["tenant_id"]),
            source_request_id=source_request_id,
            source_trace_id=source_trace_id,
            event=event,
            lease_owner_id=owner,
            lease_token=token,
            attempt_count=attempt,
            generation=generation,
            lease_expires_at=expires,
        )
        connection.commit()
        return claim, True
    # 已分类业务错误在回滚后原样上抛，保留统一目录中的安全错误码。
    except ApiError:
        connection.rollback()
        raise
    # 数据库与冻结字段异常统一映射为不可用，不透传底层异常正文。
    except (sqlite3.Error, TypeError, ValueError):
        connection.rollback()
        raise ApiError("customer_health.unavailable") from None
    finally:
        connection.close()


def dispatch_claimed_customer_health_event(
    config: AppConfig,
    claim: CustomerHealthEventClaim,
    *,
    now: datetime | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """每次网络尝试前续租，完成后以完整 CAS 提交 source 结果。"""

    def source_guard() -> None:
        """每次外部投递前检查停止信号并续租，阻止过期 worker 发送。"""

        # 停止请求到达后不得继续上游投递，避免关机期间产生重复 webhook。
        if should_stop is not None and should_stop():
            raise ApiError("customer_health.unavailable")
        _renew(config, claim, now=now)

    try:
        # 停止信号在导入和投递前再次检查，关闭阶段不得开始新的网络尝试。
        if should_stop is not None and should_stop():
            raise ApiError("customer_health.unavailable")
        from metahub_control.webhooks.service import dispatch_webhook_event

        dispatch_webhook_event(
            config,
            claim.tenant_id,
            claim.event,
            request_id=claim.source_request_id,
            trace_id=claim.source_trace_id,
            resume_existing_pending=True,
            source_lease_guard=source_guard,
            create_snapshot_if_missing=False,
        )
        return _finish(config, claim, error_code=None, now=now)
    # exact envelope 校验失败固定为 payload_invalid，不记录原始负载或异常正文。
    except ValueError:
        return _finish(config, claim, error_code="webhook.payload_invalid", now=now)
    # 其他投递失败统一为 delivery_unavailable，Provider 响应不会进入 source 状态。
    except Exception:
        return _finish(config, claim, error_code="webhook.delivery_unavailable", now=now)


def drain_customer_health_outbox_once(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """处理一次 source outbox；停止、坏记录推进和真实投递均返回明确状态。"""

    if should_stop is not None and should_stop():
        return False
    claim, progressed = _claim_with_progress(config, worker_id=worker_id, now=now)
    if claim is None:
        return progressed
    dispatch_claimed_customer_health_event(
        config, claim, now=now, should_stop=should_stop
    )
    return True


def recover_customer_health_source_request_id(
    connection: sqlite3.Connection,
    envelope: dict[str, object],
) -> str:
    """从 tenant-qualified 不可变 score 重建 exact 事件后恢复内部 request owner。"""

    expected = {
        "event_id", "event_type", "timestamp", "tenant_id", "actor_id",
        "object_id", "trace_id", "payload",
    }
    # 恢复入口只接受 exact envelope，额外字段可能携带未审计或敏感内容。
    if type(envelope) is not dict or set(envelope) != expected:
        raise ApiError("webhook.delivery_unavailable")
    row = connection.execute(
        """
        SELECT score.*,outbox.customer_health_score_outbox_id,
               outbox.webhook_event_id,outbox.status AS outbox_status,
               outbox.attempt_count,outbox.generation,outbox.subscription_snapshot_json,
               outbox.snapshot_frozen_at
        FROM customer_health_scores score
        JOIN customer_health_score_outbox outbox
          ON outbox.tenant_id=score.tenant_id
         AND outbox.customer_health_score_id=score.customer_health_score_id
        WHERE score.tenant_id=? AND score.customer_health_score_id=?
        """,
        (envelope.get("tenant_id"), envelope.get("object_id")),
    ).fetchone()
    # tenant_id 与 object_id 无法定位唯一不可变评分时拒绝恢复请求 owner。
    if row is None:
        raise ApiError("webhook.delivery_unavailable")
    try:
        event, request_id, _trace_id = _event_from_row(row)
    # 重建异常统一收敛为投递不可用，不能向调用方暴露持久化字段。
    except (ApiError, TypeError, ValueError, KeyError):
        raise ApiError("webhook.delivery_unavailable") from None
    # 只有字节语义一致的重建事件才能恢复内部 request_id，防止伪造关联。
    if event.to_payload() != envelope:
        raise ApiError("webhook.delivery_unavailable")
    return request_id


def _event_from_row(row: sqlite3.Row) -> tuple[WebhookEvent, str, str]:
    """从不可变评分行重建 exact EVT-098，并返回安全请求链标识。"""

    source, projection = _validated_row(row)
    score_id = _text(row["customer_health_score_id"])
    outbox_id = _text(row["customer_health_score_outbox_id"])
    event_id = _text(row["webhook_event_id"])
    digest = hashlib.sha256(score_id.encode("ascii")).hexdigest()[:32]
    # 评分、outbox 与事件稳定 ID 必须共享同一摘要，任一漂移都视为腐坏。
    if (
        _SCORE_ID.fullmatch(score_id) is None
        or _OUTBOX_ID.fullmatch(outbox_id) is None
        or _EVENT_ID.fullmatch(event_id) is None
        or outbox_id != f"chso_{digest}"
        or event_id != f"evt_chs_{digest}"
    ):
        # 三类 ID 无法共同复现稳定摘要时拒绝构造任何外发事件。
        raise ApiError("customer_health.unavailable")
    request_id = _text(row["operation_request_id"])
    trace_id = _text(row["operation_trace_id"])
    event = build_webhook_event(
        tenant_id=source.tenant_id,
        event_type="customer.health_changed",
        event_id=event_id,
        timestamp=_parse_time(source.evaluated_at),
        actor_id="system:customer-health-scheduler",
        object_id=score_id,
        trace_id=trace_id,
        source_request_id=request_id,
        payload={
            "schema_version": "1.0",
            "customer_health_score_id": score_id,
            "score_version": "customer-health-v1",
            "score_bps": projection.score_bps,
            "risk_level": projection.risk_level,
            "risk_tags": list(projection.risk_tags),
            "missing_signals": ["ticket_health"],
            "window_end": source.window_end,
        },
    )
    return event, request_id, trace_id


def _renew(config: AppConfig, claim: CustomerHealthEventClaim, *, now: datetime | None) -> None:
    """在每次投递尝试前以完整 CAS 续租，并轮换不可打印租约令牌。"""

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        token = secrets.token_hex(32)
        generation = claim.generation + 1
        expires = max(
            current + timedelta(seconds=EVENT_LEASE_SECONDS),
            claim.lease_expires_at + timedelta(seconds=1),
        )
        connection.execute(
            """
            UPDATE customer_health_score_outbox
            SET generation=?,lease_token=?,lease_expires_at=?,updated_at=?
            WHERE tenant_id=? AND customer_health_score_id=? AND status='leased'
              AND attempt_count=? AND generation=? AND lease_owner_id=? AND lease_token=?
              AND lease_expires_at=? AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                generation, token, _db_time(expires), _db_time(current), claim.tenant_id,
                claim.customer_health_score_id, claim.attempt_count, claim.generation,
                claim.lease_owner_id, claim.lease_token, _db_time(claim.lease_expires_at),
                _db_time(current),
            ),
        )
        # 续租 CAS 失败表示 claim 已过期或被接管，旧 worker 必须停止投递。
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise ApiError("customer_health.unavailable")
        connection.commit()
        claim.generation = generation
        claim.lease_token = token
        claim.lease_expires_at = expires
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _finish(
    config: AppConfig,
    claim: CustomerHealthEventClaim,
    *,
    error_code: str | None,
    now: datetime | None,
) -> bool:
    """以完整租约 CAS 提交 sent、pending 或 failed 终态，并安排固定退避。"""

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        now_text = _db_time(current)
        terminal = error_code is not None and claim.attempt_count >= EVENT_MAX_ATTEMPTS
        status = "sent" if error_code is None else ("failed" if terminal else "pending")
        if terminal:
            _fail_pending_deliveries(
                connection,
                tenant_id=claim.tenant_id,
                event_id=claim.event.event_id,
                updated_at=now_text,
                failure_code=error_code,
            )
        delay = EVENT_BACKOFF_SECONDS[
            min(max(claim.attempt_count - 1, 0), len(EVENT_BACKOFF_SECONDS) - 1)
        ]
        next_attempt = (
            _db_time(current + timedelta(seconds=delay)) if status == "pending" else None
        )
        connection.execute(
            """
            UPDATE customer_health_score_outbox
            SET status=?,generation=generation+1,lease_owner_id=NULL,lease_token=NULL,
                lease_expires_at=NULL,next_attempt_at=?,last_error_code=?,delivered_at=?,updated_at=?
            WHERE tenant_id=? AND customer_health_score_id=? AND status='leased'
              AND attempt_count=? AND generation=? AND lease_owner_id=? AND lease_token=?
              AND lease_expires_at=? AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                status, next_attempt, None if status == "sent" else error_code,
                now_text if status == "sent" else None, now_text, claim.tenant_id,
                claim.customer_health_score_id, claim.attempt_count, claim.generation,
                claim.lease_owner_id, claim.lease_token, _db_time(claim.lease_expires_at),
                now_text,
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            connection.rollback()
            return False
        connection.commit()
        return True
    # 完成事务失败时回滚并返回假值，调用方只能依据固定状态重试。
    except (sqlite3.Error, TypeError, ValueError, ApiError):
        connection.rollback()
        return False
    finally:
        connection.close()


def _record_corrupt_attempt(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    owner: str,
    current: datetime,
) -> None:
    """坏 source 仍累计有界次数，避免永久阻塞公平队列。"""

    status = _outbox_status(row["outbox_status"])
    old_attempt = _integer(row["attempt_count"], minimum=0, maximum=10)
    attempt = old_attempt + 1 if status == "pending" else old_attempt
    # 坏记录同样受最大尝试次数约束，避免永久占据 source outbox 队首。
    if not 1 <= attempt <= EVENT_MAX_ATTEMPTS:
        raise ApiError("customer_health.unavailable")
    token = secrets.token_hex(32)
    generation = _integer(row["generation"], minimum=0) + 1
    expires = current + timedelta(seconds=EVENT_LEASE_SECONDS)
    connection.execute(
        """
        UPDATE customer_health_score_outbox
        SET status='leased',attempt_count=?,generation=?,lease_owner_id=?,lease_token=?,
            lease_expires_at=?,next_attempt_at=NULL,last_error_code=NULL,
            delivered_at=NULL,updated_at=?
        WHERE tenant_id=? AND customer_health_score_id=? AND status=?
          AND attempt_count=? AND generation=?
          AND (status='pending' OR unixepoch(lease_expires_at)<=unixepoch(?))
        """,
        (
            attempt, generation, owner, token, _db_time(expires), _db_time(current),
            row["tenant_id"], row["customer_health_score_id"], status, old_attempt,
            row["generation"], _db_time(current),
        ),
    )
    # 坏记录领取也必须完整 CAS 成功，否则不能推进其他 worker 的 claim。
    if connection.execute("SELECT changes()").fetchone()[0] != 1:
        raise ApiError("customer_health.unavailable")
    terminal = attempt == EVENT_MAX_ATTEMPTS
    next_attempt = None if terminal else _db_time(current + timedelta(seconds=5))
    if terminal and _EVENT_ID.fullmatch(str(row["webhook_event_id"])) is not None:
        _fail_pending_deliveries(
            connection,
            tenant_id=_text(row["tenant_id"]),
            event_id=_text(row["webhook_event_id"]),
            updated_at=_db_time(current),
            failure_code="webhook.payload_invalid",
        )
    connection.execute(
        """
        UPDATE customer_health_score_outbox
        SET status=?,generation=generation+1,lease_owner_id=NULL,lease_token=NULL,
            lease_expires_at=NULL,next_attempt_at=?,
            last_error_code='webhook.payload_invalid',delivered_at=NULL,updated_at=?
        WHERE tenant_id=? AND customer_health_score_id=? AND status='leased'
          AND attempt_count=? AND generation=? AND lease_owner_id=? AND lease_token=?
          AND lease_expires_at=? AND unixepoch(lease_expires_at)>unixepoch(?)
        """,
        (
            "failed" if terminal else "pending", next_attempt, _db_time(current),
            row["tenant_id"], row["customer_health_score_id"], attempt, generation,
            owner, token, _db_time(expires), _db_time(current),
        ),
    )
    # 坏记录终态提交必须仍持有原租约，失败即停止本次恢复事务。
    if connection.execute("SELECT changes()").fetchone()[0] != 1:
        raise ApiError("customer_health.unavailable")


def _delivery_snapshot(connection: sqlite3.Connection, event: WebhookEvent) -> list[str]:
    """读取按稳定 ID 排序的原始 delivery 快照，并拒绝重复或坏标识。"""

    rows = connection.execute(
        """
        SELECT webhook_delivery_id FROM webhook_deliveries
        WHERE tenant_id=? AND event_id=? AND delivery_kind='original'
        ORDER BY webhook_delivery_id
        """,
        (event.tenant_id, event.event_id),
    ).fetchall()
    values = [str(row[0]) for row in rows]
    # 快照必须去重、排序且标识合法，保证崩溃恢复不会扩散到新订阅。
    if values != sorted(set(values)) or any(
        _DELIVERY_ID.fullmatch(value) is None for value in values
    ):
        # 快照存在重复、乱序或坏 ID 时停止恢复，避免错误扩大投递集合。
        raise ApiError("customer_health.unavailable")
    return values


def _parse_snapshot(value: object) -> list[str]:
    """解析已冻结订阅快照的规范 JSON，并逐项校验 delivery 标识。"""

    # 数据库快照必须是字符串，隐式转换可能掩盖损坏或注入结构。
    if type(value) is not str:
        raise ApiError("customer_health.unavailable")
    # JSON 解析失败统一映射为 source 不可用，不透出原始快照内容。
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        # 解析失败只暴露固定错误码，数据库快照正文不得进入日志或 API。
        raise ApiError("customer_health.unavailable") from None
    # 解析结果必须是已排序唯一的合法 delivery 标识数组，其他结构全部拒绝。
    if (
        type(decoded) is not list
        or decoded != sorted(set(decoded))
        or any(type(item) is not str or _DELIVERY_ID.fullmatch(item) is None for item in decoded)
    ):
        # 快照结构或元素不符合冻结合同时拒绝继续投递。
        raise ApiError("customer_health.unavailable")
    return decoded


def _fail_pending_deliveries(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    event_id: str,
    updated_at: str,
    failure_code: str,
) -> None:
    """把同租户同事件仍待处理的 delivery 原子收敛为固定失败码。"""

    from metahub_control.webhooks.service import fail_pending_webhook_event_deliveries

    fail_pending_webhook_event_deliveries(
        connection,
        tenant_id=tenant_id,
        event_id=event_id,
        event_type="customer.health_changed",
        updated_at=updated_at,
        failure_code=failure_code,
    )


def _connect(config: AppConfig) -> sqlite3.Connection:
    """创建 source outbox 数据库连接并收敛底层连接错误。"""

    try:
        return connect_database(config.database_url)
    # 数据库地址与路径不得通过异常正文泄露，统一返回客户健康不可用。
    except DbError:
        raise ApiError("customer_health.unavailable") from None


def _transaction_now(connection: sqlite3.Connection, override: datetime | None) -> datetime:
    """取得事务内 UTC 整秒时钟，测试覆盖值同样必须带时区。"""

    if override is not None:
        # 无时区覆盖值会破坏租约先后关系，进入写事务前必须拒绝。
        if override.tzinfo is None:
            raise ApiError("customer_health.unavailable")
        return override.astimezone(UTC).replace(microsecond=0)
    row = connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')").fetchone()
    # 数据库时钟缺失时不能退回进程本地时间，否则多 worker 判断会漂移。
    if row is None:
        raise ApiError("customer_health.unavailable")
    return datetime.fromisoformat(str(row[0]).replace("Z", "+00:00")).astimezone(UTC)


def _time_text(value: object) -> str:
    """读取并往返校验数据库 UTC 整秒时间文本。"""

    text = _text(value)
    # 时间解析失败统一为安全错误，不记录数据库中的原始异常值。
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        # 数据库坏时间只映射为固定错误码，不能回显原始字段。
        raise ApiError("customer_health.unavailable") from None
    # 往返格式不一致说明时间不是规范 UTC 整秒，不能用于租约 CAS。
    if _db_time(parsed) != text:
        raise ApiError("customer_health.unavailable")
    return text


def _parse_time(value: object) -> datetime:
    """解析规范 UTC 整秒文本并返回有时区 datetime。"""

    text = _text(value)
    # 解析异常只返回固定不可用码，避免坏持久化内容进入错误证据。
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        # 事件坏时间只映射为固定错误码，不能进入 webhook 负载。
        raise ApiError("customer_health.unavailable") from None
    # 时间必须带时区且规范化后不变，防止多种表示破坏事件哈希。
    if parsed.tzinfo is None or _db_time(parsed) != text:
        raise ApiError("customer_health.unavailable")
    return parsed.astimezone(UTC)


def _db_time(value: datetime) -> str:
    """把有时区时间规范化为数据库使用的 UTC Z 整秒文本。"""

    # 租约与事件时间缺少时区会造成跨节点歧义，必须失败关闭。
    if value.tzinfo is None:
        raise ApiError("customer_health.unavailable")
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _text(value: object) -> str:
    """读取数据库必填文本，拒绝空值与隐式字符串转换。"""

    # tenant、ID 和请求链文本异常时停止投递，不能在日志中展开原值。
    if type(value) is not str or not value:
        raise ApiError("customer_health.unavailable")
    return value


def _integer(value: object, *, minimum: int, maximum: int | None = None) -> int:
    """读取有界原生整数，用于尝试次数和租约代次校验。"""

    # 布尔值、浮点数和越界计数都可能破坏重试终态，必须统一拒绝。
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ApiError("customer_health.unavailable")
    return value


def _outbox_status(value: object) -> str:
    """校验可领取 source outbox 状态，只允许待处理或已租约两类冻结状态。"""

    # 未知状态可能绕过租约 CAS，必须按持久化腐坏处理。
    if value not in {"pending", "leased"}:
        raise ApiError("customer_health.unavailable")
    return str(value)


def _worker(value: object) -> str:
    """校验 worker owner 的安全字符和长度后再写入租约。"""

    # worker 标识进入数据库与 CAS，非法值不得写入或被日志记录。
    if type(value) is not str or _WORKER_ID.fullmatch(value) is None:
        raise ApiError("customer_health.unavailable")
    return value


def _canonical_json(value: object) -> str:
    """把订阅快照编码为稳定 ASCII JSON，保证崩溃恢复比较可重复。"""

    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


__all__ = [
    "CustomerHealthEventClaim",
    "claim_next_customer_health_event",
    "dispatch_claimed_customer_health_event",
    "drain_customer_health_outbox_once",
    "recover_customer_health_source_request_id",
]
