"""产品事件源队列以租户快照、租约续期和完整 CAS 保证 Webhook 崩溃恢复。"""

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

# 固定租约、退避、身份格式和七类事实共同约束恢复协议，禁止运行时自由扩展。
EVENT_LEASE_SECONDS = 90
EVENT_MAX_ATTEMPTS = 10
EVENT_BACKOFF_SECONDS = (5, 30, 300)
_PRODUCT_EVENT_ID = re.compile(r"^pev_[0-9a-f]{32}$")
_OUTBOX_ID = re.compile(r"^pevo_[0-9a-f]{32}$")
_DELIVERY_ID = re.compile(r"^whd_[0-9a-f]{32}$")
_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_EVENT_TYPES = {
    "tenant.registered",
    "provider.created",
    "virtual_key.created",
    "gateway.first_call_succeeded",
    "budget.configured",
    "teammate.invited",
    "request_logs.viewed",
}


@dataclass(slots=True)
class ProductEventClaim:
    """保存租户限定的源事件租约；事件正文和租约令牌不得进入对象调试表示。"""

    product_event_id: str
    tenant_id: str
    source_request_id: str | None
    source_trace_id: str | None
    event: WebhookEvent = field(repr=False)
    lease_owner_id: str
    lease_token: str = field(repr=False)
    attempt_count: int
    generation: int
    lease_expires_at: datetime


def claim_next_product_event(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> ProductEventClaim | None:
    """领取一个到期 source，并在同一事务冻结 original delivery ID 快照。"""

    claim, _progressed = _claim_next_product_event_with_progress(
        config,
        worker_id=worker_id,
        now=now,
    )
    return claim


def _claim_next_product_event_with_progress(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> tuple[ProductEventClaim | None, bool]:
    """原子选择一个到期源事件，冻结订阅快照并通过完整旧值条件取得租约。"""

    owner = _worker_id(worker_id)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        now_text = _db_time(current)
        row = connection.execute(
            """
            SELECT event.*,
                   outbox.product_event_outbox_id,
                   outbox.status AS outbox_status,
                   outbox.attempt_count,outbox.generation,
                   outbox.lease_owner_id,outbox.lease_token,outbox.lease_expires_at,
                   outbox.subscription_snapshot_json,outbox.snapshot_frozen_at,
                   outbox.next_attempt_at,outbox.last_error_code,outbox.delivered_at,
                   outbox.created_at AS outbox_created_at,
                   outbox.updated_at AS outbox_updated_at
            FROM product_event_outbox outbox
            JOIN product_events event
              ON event.tenant_id=outbox.tenant_id
             AND event.product_event_id=outbox.product_event_id
            WHERE (outbox.status='pending'
                    AND unixepoch(outbox.next_attempt_at)<=unixepoch(?))
               OR (outbox.status='leased'
                    AND unixepoch(outbox.lease_expires_at)<=unixepoch(?))
            ORDER BY COALESCE(outbox.next_attempt_at,outbox.lease_expires_at),
                     outbox.tenant_id,outbox.created_at,outbox.product_event_outbox_id
            LIMIT 1
            """,
            (now_text, now_text),
        ).fetchone()
        # 没有到期 source 属于正常空轮询，提交只读事务后明确返回未取得工作。
        if row is None:
            connection.commit()
            return None, False

        try:
            event, source_request_id, source_trace_id = _event_from_row(row)
        # 坏 source 也必须在同一事务累计固定失败次数，避免永久阻塞后续租户事件。
        except (ApiError, TypeError, ValueError):
            _record_corrupt_attempt(connection, row, owner=owner, current=current)
            connection.commit()
            return None, True

        frozen_at = row["snapshot_frozen_at"]
        if frozen_at is None:
            from metahub_control.webhooks.service import (
                freeze_webhook_event_delivery_snapshot,
            )

            frozen_at = now_text
            freeze_webhook_event_delivery_snapshot(
                connection,
                event,
                frozen_at=frozen_at,
            )
            snapshot = _delivery_snapshot(connection, event)
        else:
            snapshot = _parse_snapshot(row["subscription_snapshot_json"])
            # 已冻结快照必须与持久 original delivery 集合一致，不能受后来订阅变化影响。
            if snapshot != _delivery_snapshot(connection, event):
                raise ApiError("product_analytics.unavailable")

        old_status = str(row["outbox_status"])
        old_attempt = _integer(row["attempt_count"], minimum=0, maximum=10)
        attempt = old_attempt + 1 if old_status == "pending" else old_attempt
        # 尝试计数越界表示状态机损坏，不能重置后继续投递造成重复外发。
        if not 1 <= attempt <= EVENT_MAX_ATTEMPTS:
            raise ApiError("product_analytics.unavailable")
        token = secrets.token_hex(32)
        generation = _integer(row["generation"], minimum=0) + 1
        expires = current + timedelta(seconds=EVENT_LEASE_SECONDS)
        connection.execute(
            """
            UPDATE product_event_outbox
            SET status='leased',attempt_count=?,generation=?,lease_owner_id=?,
                lease_token=?,lease_expires_at=?,next_attempt_at=NULL,
                subscription_snapshot_json=?,snapshot_frozen_at=?,
                last_error_code=NULL,delivered_at=NULL,updated_at=?
            WHERE tenant_id=? AND product_event_id=? AND status=?
              AND attempt_count=? AND generation=?
              AND (status='pending' OR unixepoch(lease_expires_at)<=unixepoch(?))
            """,
            (
                attempt,
                generation,
                owner,
                token,
                _db_time(expires),
                _canonical_json(snapshot),
                frozen_at,
                now_text,
                row["tenant_id"],
                row["product_event_id"],
                old_status,
                old_attempt,
                row["generation"],
                now_text,
            ),
        )
        # 完整 CAS 未命中说明其他 worker 已取得或更新租约，本 worker 必须放弃。
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            connection.rollback()
            return None, False
        claim = ProductEventClaim(
            product_event_id=str(row["product_event_id"]),
            tenant_id=str(row["tenant_id"]),
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
    # 已识别领域错误在回滚后保持原错误码，供调度器安全记录和重试。
    except ApiError:
        connection.rollback()
        raise
    # 底层数据库或类型异常不携带原值出界，统一映射为产品分析不可用。
    except (sqlite3.Error, TypeError, ValueError):
        connection.rollback()
        raise ApiError("product_analytics.unavailable") from None
    finally:
        connection.close()


def dispatch_claimed_product_event(
    config: AppConfig,
    claim: ProductEventClaim,
    *,
    now: datetime | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """只在 source lease 有效时恢复快照投递，并以完整 CAS 写回结果。"""

    def source_guard() -> None:
        """在每次网络尝试前复核停服信号并续租，防止过期 worker 继续发送。"""

        # 停服后禁止开始新的 Endpoint 尝试，已提交业务事实仍留给后续 worker 恢复。
        if should_stop is not None and should_stop():
            raise ApiError("product_analytics.unavailable")
        _renew(config, claim, now=now)

    try:
        # 分发前再次检查停服状态，覆盖领取成功到首次网络尝试之间的竞态窗口。
        if should_stop is not None and should_stop():
            raise ApiError("product_analytics.unavailable")
        from metahub_control.webhooks.service import dispatch_webhook_event

        dispatch_webhook_event(
            config,
            claim.tenant_id,
            claim.event,
            request_id=claim.source_request_id or f"req_{claim.event.event_id}",
            trace_id=claim.source_trace_id or f"trc_{claim.event.event_id}",
            resume_existing_pending=True,
            source_lease_guard=source_guard,
            create_snapshot_if_missing=False,
        )
        return _finish(config, claim, error_code=None, now=now)
    # Shared exact validator 拒绝载荷时记录固定无效码，不暴露源字段或异常文本。
    except ValueError:
        return _finish(config, claim, error_code="webhook.payload_invalid", now=now)
    # 网络和未知投递异常统一进入可重试失败路径，不能回滚已提交 ProductEvent。
    except Exception:
        return _finish(config, claim, error_code="webhook.delivery_unavailable", now=now)


def drain_product_event_once(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """每轮最多处理一个 source；确定性坏 source 的失败计数也算处理进展。"""

    if should_stop is not None and should_stop():
        return False
    claim, progressed = _claim_next_product_event_with_progress(
        config,
        worker_id=worker_id,
        now=now,
    )
    if claim is None:
        return progressed
    dispatch_claimed_product_event(
        config,
        claim,
        now=now,
        should_stop=should_stop,
    )
    return True


def recover_product_event_source_request_id(
    connection: sqlite3.Connection,
    envelope: dict[str, object],
) -> str | None:
    """从不可变 ProductEvent 恢复未公开的原始 request owner。"""

    expected = {
        "event_id",
        "event_type",
        "timestamp",
        "tenant_id",
        "actor_id",
        "object_id",
        "trace_id",
        "payload",
    }
    # 恢复入口只接受完整公开信封，额外字段可能夹带来源身份或未审计业务正文。
    if type(envelope) is not dict or set(envelope) != expected:
        raise ApiError("webhook.delivery_unavailable")
    row = connection.execute(
        """
        SELECT event.*,
               outbox.product_event_outbox_id,
               outbox.status AS outbox_status,
               outbox.attempt_count,outbox.generation,
               outbox.subscription_snapshot_json,outbox.snapshot_frozen_at
        FROM product_events event
        JOIN product_event_outbox outbox
          ON outbox.tenant_id=event.tenant_id
         AND outbox.product_event_id=event.product_event_id
        WHERE event.tenant_id=? AND event.product_event_id=?
        """,
        (envelope.get("tenant_id"), envelope.get("object_id")),
    ).fetchone()
    # 必须按信封 tenant 与 object 联合命中同一 source，禁止跨租户猜测原请求身份。
    if row is None:
        raise ApiError("webhook.delivery_unavailable")
    try:
        event, source_request_id, _trace_id = _event_from_row(row)
    # 持久 source 无法重建时只返回投递不可用，不能暴露损坏列或原始值。
    except (ApiError, TypeError, ValueError):
        raise ApiError("webhook.delivery_unavailable") from None
    # 调用方信封必须与权威行逐字段相同，任何漂移都不能用于恢复内部 request owner。
    if event.to_payload() != envelope:
        raise ApiError("webhook.delivery_unavailable")
    return source_request_id


def _event_from_row(
    row: sqlite3.Row,
) -> tuple[WebhookEvent, str | None, str | None]:
    """从租户限定的不可变 ProductEvent 行重建 exact Webhook 与内部操作链路。"""

    product_event_id = row["product_event_id"]
    tenant_id = row["tenant_id"]
    event_type = row["event_type"]
    outbox_id = row["product_event_outbox_id"]
    # ProductEvent、outbox、租户和固定状态必须互相一致，否则不能生成替代外发事实。
    if (
        type(product_event_id) is not str
        or _PRODUCT_EVENT_ID.fullmatch(product_event_id) is None
        or type(outbox_id) is not str
        or _OUTBOX_ID.fullmatch(outbox_id) is None
        or outbox_id != _product_outbox_id(product_event_id)
        or type(tenant_id) is not str
        or not tenant_id
        or type(event_type) is not str
        or event_type not in _EVENT_TYPES
        or row["capture_mode"] != "live"
        or row["status"] != "recorded"
    ):
        # 任一身份或状态漂移都表示 source graph 不可信，禁止构造替代事件继续外发。
        raise ApiError("product_analytics.unavailable")
    actor_id = _optional_text(row["actor_user_id"])
    source_request_id = _optional_text(row["operation_request_id"])
    source_trace_id = _optional_text(row["operation_trace_id"])
    occurred_at = _canonical_time(row["occurred_at"])
    webhook_event_id = _webhook_event_id(product_event_id)
    event = build_webhook_event(
        tenant_id=tenant_id,
        event_type="product_event.recorded",
        event_id=webhook_event_id,
        timestamp=occurred_at,
        actor_id=actor_id,
        object_id=product_event_id,
        trace_id=source_trace_id,
        source_request_id=source_request_id,
        payload={
            "schema_version": "1.0",
            "product_event_id": product_event_id,
            "event_type": event_type,
            "capture_mode": "live",
            "occurred_at": _db_time(occurred_at),
        },
    )
    return event, source_request_id, source_trace_id


def _delivery_snapshot(
    connection: sqlite3.Connection,
    event: WebhookEvent,
) -> list[str]:
    """读取同租户同事件的 original delivery 身份，并验证有序去重及固定格式。"""

    rows = connection.execute(
        """
        SELECT webhook_delivery_id FROM webhook_deliveries
        WHERE tenant_id=? AND event_id=? AND delivery_kind='original'
        ORDER BY webhook_delivery_id
        """,
        (event.tenant_id, event.event_id),
    ).fetchall()
    values = [str(row["webhook_delivery_id"]) for row in rows]
    # 快照顺序或 ID 漂移会破坏重启幂等性，不能静默排序或丢弃坏记录。
    if values != sorted(set(values)) or any(
        _DELIVERY_ID.fullmatch(value) is None for value in values
    ):
        # 不可信 delivery 集合不能作为恢复快照，否则可能漏投、重投或跨对象投递。
        raise ApiError("product_analytics.unavailable")
    return values


def _parse_snapshot(value: object) -> list[str]:
    """解析持久订阅快照，并拒绝非数组、重复、乱序或非法 delivery 身份。"""

    # 未冻结快照必须以 NULL 表示；进入解析的值只能是已经持久化的 JSON 文本。
    if type(value) is not str:
        raise ApiError("product_analytics.unavailable")
    try:
        decoded = json.loads(value)
    # JSON 解析异常不回显原快照，避免把内部 Endpoint 关系带入错误证据。
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ApiError("product_analytics.unavailable") from None
    # 快照必须是规范排序的 original delivery ID 集合，重启时不得重新查询当前订阅。
    if (
        type(decoded) is not list
        or decoded != sorted(set(decoded))
        or any(
            type(item) is not str or _DELIVERY_ID.fullmatch(item) is None
            for item in decoded
        )
    ):
        # 解码后的集合不规范时保持失败关闭，不能修剪后制造新的订阅事实。
        raise ApiError("product_analytics.unavailable")
    return decoded


def _renew(
    config: AppConfig,
    claim: ProductEventClaim,
    *,
    now: datetime | None,
) -> None:
    """在网络尝试前通过旧租约完整 CAS 旋转令牌、递增世代并延长到期时间。"""

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        token = secrets.token_hex(32)
        generation = claim.generation + 1
        expires = current + timedelta(seconds=EVENT_LEASE_SECONDS)
        connection.execute(
            """
            UPDATE product_event_outbox
            SET generation=?,lease_token=?,lease_expires_at=?,updated_at=?
            WHERE tenant_id=? AND product_event_id=? AND status='leased'
              AND attempt_count=? AND generation=? AND lease_owner_id=? AND lease_token=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                generation,
                token,
                _db_time(expires),
                _db_time(current),
                claim.tenant_id,
                claim.product_event_id,
                claim.attempt_count,
                claim.generation,
                claim.lease_owner_id,
                claim.lease_token,
                _db_time(current),
            ),
        )
        # 续租 CAS 未命中表示租约已过期或被接管，旧 worker 必须立即停止发送。
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise ApiError("product_analytics.unavailable")
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
    claim: ProductEventClaim,
    *,
    error_code: str | None,
    now: datetime | None,
) -> bool:
    """以租约完整 CAS 提交成功、退避重试或十次失败终态，并清除租约字段。"""

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
        next_attempt_at = (
            _db_time(current + timedelta(seconds=delay)) if status == "pending" else None
        )
        connection.execute(
            """
            UPDATE product_event_outbox
            SET status=?,generation=generation+1,lease_owner_id=NULL,lease_token=NULL,
                lease_expires_at=NULL,next_attempt_at=?,last_error_code=?,
                delivered_at=?,updated_at=?
            WHERE tenant_id=? AND product_event_id=? AND status='leased'
              AND attempt_count=? AND generation=? AND lease_owner_id=? AND lease_token=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                status,
                next_attempt_at,
                None if status == "sent" else error_code,
                now_text if status == "sent" else None,
                now_text,
                claim.tenant_id,
                claim.product_event_id,
                claim.attempt_count,
                claim.generation,
                claim.lease_owner_id,
                claim.lease_token,
                now_text,
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            connection.rollback()
            return False
        connection.commit()
        return True
    # 写回竞争或数据库异常只能回滚并报告未完成，不能覆盖接管 worker 的新状态。
    except (ApiError, sqlite3.Error, TypeError, ValueError):
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
    """以合法状态转换累计坏 source，十次后失败关闭其 frozen deliveries。"""

    now_text = _db_time(current)
    old_status = str(row["outbox_status"])
    old_attempt = _integer(row["attempt_count"], minimum=0, maximum=10)
    attempt = old_attempt + 1 if old_status == "pending" else old_attempt
    # 坏 source 的计数仍受固定上限约束，避免损坏行造成无限重试或整数漂移。
    if not 1 <= attempt <= EVENT_MAX_ATTEMPTS:
        raise ApiError("product_analytics.unavailable")
    token = secrets.token_hex(32)
    generation = _integer(row["generation"], minimum=0) + 1
    connection.execute(
        """
        UPDATE product_event_outbox
        SET status='leased',attempt_count=?,generation=?,lease_owner_id=?,
            lease_token=?,lease_expires_at=?,next_attempt_at=NULL,
            last_error_code=NULL,delivered_at=NULL,updated_at=?
        WHERE tenant_id=? AND product_event_id=? AND status=?
          AND attempt_count=? AND generation=?
          AND (status='pending' OR unixepoch(lease_expires_at)<=unixepoch(?))
        """,
        (
            attempt,
            generation,
            owner,
            token,
            _db_time(current + timedelta(seconds=EVENT_LEASE_SECONDS)),
            now_text,
            row["tenant_id"],
            row["product_event_id"],
            old_status,
            old_attempt,
            row["generation"],
            now_text,
        ),
    )
    # 坏 source 也必须先通过状态旧值 CAS 取得租约，不能与健康 worker 并发改写。
    if connection.execute("SELECT changes()").fetchone()[0] != 1:
        raise ApiError("product_analytics.unavailable")
    terminal = attempt >= EVENT_MAX_ATTEMPTS
    # 仅合法 ProductEvent 身份可映射公开事件 ID，并在十次失败后同步关闭冻结 delivery。
    if terminal and _PRODUCT_EVENT_ID.fullmatch(str(row["product_event_id"])):
        _fail_pending_deliveries(
            connection,
            tenant_id=str(row["tenant_id"]),
            event_id=_webhook_event_id(str(row["product_event_id"])),
            updated_at=now_text,
            failure_code="webhook.payload_invalid",
        )
    connection.execute(
        """
        UPDATE product_event_outbox
        SET status=?,generation=generation+1,lease_owner_id=NULL,lease_token=NULL,
            lease_expires_at=NULL,next_attempt_at=?,
            last_error_code='webhook.payload_invalid',delivered_at=NULL,updated_at=?
        WHERE tenant_id=? AND product_event_id=? AND status='leased'
          AND attempt_count=? AND generation=? AND lease_owner_id=? AND lease_token=?
        """,
        (
            "failed" if terminal else "pending",
            None if terminal else _db_time(current + timedelta(seconds=5)),
            now_text,
            row["tenant_id"],
            row["product_event_id"],
            attempt,
            generation,
            owner,
            token,
        ),
    )
    # 终态写回仍需匹配刚取得的令牌和世代，否则由接管者继续负责恢复。
    if connection.execute("SELECT changes()").fetchone()[0] != 1:
        raise ApiError("product_analytics.unavailable")


def _fail_pending_deliveries(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    event_id: str,
    updated_at: str,
    failure_code: str,
) -> None:
    """把同租户源事件的冻结待投递项统一转为失败，并生成既有死信事实。"""

    from metahub_control.webhooks.service import fail_pending_webhook_event_deliveries

    fail_pending_webhook_event_deliveries(
        connection,
        tenant_id=tenant_id,
        event_id=event_id,
        event_type="product_event.recorded",
        updated_at=updated_at,
        failure_code=failure_code,
    )


def _webhook_event_id(product_event_id: str) -> str:
    """从不可变 ProductEvent 身份派生稳定公开事件 ID，重试和重启保持不变。"""

    digest = hashlib.sha256(product_event_id.encode("ascii")).hexdigest()[:32]
    return f"evt_pev_{digest}"


def _product_outbox_id(product_event_id: str) -> str:
    """从不可变 ProductEvent 身份派生内部 source outbox 主键，不对客户公开。"""

    digest = hashlib.sha256(product_event_id.encode("ascii")).hexdigest()[:32]
    return f"pevo_{digest}"


def _canonical_time(value: object) -> datetime:
    """解析权威数据库时间为 UTC，并仅兼容迁移历史使用的无后缀整秒文本。"""

    # 时间必须来自数据库文本列，其他类型无法证明源事实的发生时刻。
    if type(value) is not str:
        raise ApiError("product_analytics.unavailable")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # 解析错误统一收敛，不把可能损坏或敏感的原始列值写入异常。
    except ValueError:
        raise ApiError("product_analytics.unavailable") from None
    # 无时区值只兼容 SQLite 历史 UTC 整秒格式，其他模糊时间禁止推断。
    if parsed.tzinfo is None:
        if len(value) != 19 or value[10] not in {" ", "T"}:
            raise ApiError("product_analytics.unavailable")
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _optional_text(value: object) -> str | None:
    """校验可空操作链路标识；非空值必须是实际文本且不能退化为空字符串。"""

    if value is None:
        return None
    # 坏链路身份会污染 request/trace 关联，不能转换类型或生成替代标识。
    if type(value) is not str or not value:
        raise ApiError("product_analytics.unavailable")
    return value


def _integer(value: object, *, minimum: int, maximum: int | None = None) -> int:
    """校验 outbox 状态计数使用真整数并位于调用方给定的闭区间内。"""

    # bool 和越界数字都会破坏尝试次数或世代 CAS，必须在写事务内失败关闭。
    if type(value) is not int or value < minimum or (
        maximum is not None and value > maximum
    ):
        # 非法状态数字不得进入租约更新，否则完整 CAS 将失去可证明的旧值边界。
        raise ApiError("product_analytics.unavailable")
    return value


def _transaction_now(
    connection: sqlite3.Connection,
    override: datetime | None,
) -> datetime:
    """取得事务统一 UTC 整秒；测试覆盖值也必须带时区，生产值由 SQLite 生成。"""

    if override is not None:
        # 无时区覆盖值会让租约到期比较产生歧义，不能按本地时区猜测。
        if override.tzinfo is None:
            raise ApiError("product_analytics.unavailable")
        return override.astimezone(UTC).replace(microsecond=0)
    row = connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')").fetchone()
    # 数据库未返回事务时间意味着连接状态异常，禁止使用进程时间继续领取租约。
    if row is None:
        raise ApiError("product_analytics.unavailable")
    return _canonical_time(row[0])


def _db_time(value: datetime) -> str:
    """把租约和状态时间规范成数据库使用的 UTC Z 整秒文本。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical_json(value: object) -> str:
    """生成键排序且无多余空白的快照 JSON，保证重启时逐字稳定。"""

    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _worker_id(value: object) -> str:
    """校验进程 worker 身份为有界安全字符，避免污染租约与排障字段。"""

    # worker 身份参与完整 CAS，非法或超长值不能进入持久租约列。
    if type(value) is not str or _WORKER_ID.fullmatch(value) is None:
        raise ApiError("product_analytics.unavailable")
    return value


def _connect(config: AppConfig) -> sqlite3.Connection:
    """建立 Control SQLite 连接，并把配置或连接失败收敛为稳定领域错误。"""

    try:
        return connect_database(config.database_url)
    # 连接异常不能泄露数据库 URL 或路径，只允许调度器看到固定不可用错误码。
    except DbError:
        raise ApiError("product_analytics.unavailable") from None


__all__ = [
    "ProductEventClaim",
    "claim_next_product_event",
    "dispatch_claimed_product_event",
    "drain_product_event_once",
    "recover_product_event_source_request_id",
]
