"""PRD-079 额度事件 source outbox 与租户限定租约恢复。

模块冻结原始 Webhook 投递集合，并以 owner、generation 和 token 三重 CAS 防止迟到 worker 覆盖终态。
"""

from __future__ import annotations

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

# 租约、退避和标识格式共同限制后台恢复范围，私有 token 只驻留进程与权威数据库。
EVENT_LEASE_SECONDS = 90
MAX_ATTEMPTS = 10
BACKOFF_SECONDS = (5, 30, 300)
_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_EVENT_ID = re.compile(r"^qevt_[0-9a-f]{32}$")
_OUTBOX_ID = re.compile(r"^qobx_[0-9a-f]{32}$")
_DELIVERY_ID = re.compile(r"^whd_[0-9a-f]{32}$")


@dataclass(slots=True)
class QuotaEventClaim:
    """保存不可变事件坐标和单个可续租私有租约，敏感 token 禁止进入表示文本。"""

    outbox_id: str
    event_id: str
    tenant_id: str
    source_request_id: str
    source_trace_id: str
    event: WebhookEvent = field(repr=False)
    attempt_count: int
    lease_generation: int
    lease_owner_id: str
    lease_token: str = field(repr=False)
    lease_expires_at: datetime
    snapshot_json: str = field(repr=False)


def claim_next_quota_event(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> QuotaEventClaim | None:
    """领取一个到期事件并冻结创建时的原始投递集合，所有更新按租户执行 CAS。"""

    claim, _ = _claim_next_quota_event_with_progress(
        config,
        worker_id=worker_id,
        now=now,
    )
    return claim


def _claim_next_quota_event_with_progress(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None,
) -> tuple[QuotaEventClaim | None, bool]:
    """领取一条可投递事件，并报告坏 source 是否已被持久隔离。"""

    owner = _worker_id(worker_id)
    connection = _connect(config)
    progressed = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        current_text = _db_time(current)
        while True:
            row = connection.execute(
                """
                SELECT event.*,outbox.quota_event_outbox_id,outbox.status,
                  outbox.attempt_count,outbox.lease_generation,outbox.lease_owner_id,
                  outbox.lease_token,outbox.lease_expires_at,outbox.next_attempt_at,
                  outbox.delivery_snapshot_json,outbox.snapshot_frozen_at
                FROM quota_event_outbox outbox
                JOIN quota_events event
                  ON event.tenant_id=outbox.tenant_id
                 AND event.quota_event_id=outbox.quota_event_id
                WHERE (outbox.status='pending'
                        AND unixepoch(outbox.next_attempt_at)<=unixepoch(?))
                   OR (outbox.status='processing'
                        AND unixepoch(outbox.lease_expires_at)<=unixepoch(?))
                ORDER BY COALESCE(outbox.next_attempt_at,outbox.lease_expires_at),
                         outbox.created_at,outbox.quota_event_outbox_id
                LIMIT 1
                """,
                (current_text, current_text),
            ).fetchone()
            if row is None:
                connection.commit()
                return None, progressed
            token = secrets.token_hex(32)
            generation = int(row["lease_generation"]) + 1
            expires = current + timedelta(seconds=EVENT_LEASE_SECONDS)
            try:
                event = _event_from_row(row)
                snapshot_json = _freeze_or_verify_snapshot(
                    connection,
                    event,
                    existing=row["delivery_snapshot_json"],
                    frozen_at=row["snapshot_frozen_at"],
                    now=current_text,
                )
            # 确定性持久 source 损坏不能反复占据队首；以私有租约隔离后继续扫描。
            except (ApiError, TypeError, ValueError, json.JSONDecodeError):
                _quarantine_invalid_event(
                    connection,
                    row,
                    owner=owner,
                    token=token,
                    generation=generation,
                    lease_expires=expires,
                    now=current_text,
                )
                connection.commit()
                progressed = True
                connection.execute("BEGIN IMMEDIATE")
                continue
            cursor = connection.execute(
                """
                UPDATE quota_event_outbox
                SET status='processing',lease_generation=?,lease_owner_id=?,lease_token=?,
                    lease_expires_at=?,delivery_snapshot_json=?,
                    snapshot_frozen_at=COALESCE(snapshot_frozen_at,?),last_error_code=NULL,
                    updated_at=?
                WHERE tenant_id=? AND quota_event_outbox_id=? AND status=?
                  AND attempt_count=? AND lease_generation=?
                  AND (status='pending' OR unixepoch(lease_expires_at)<=unixepoch(?))
                """,
                (
                    generation,
                    owner,
                    token,
                    _db_time(expires),
                    snapshot_json,
                    current_text,
                    current_text,
                    row["tenant_id"],
                    row["quota_event_outbox_id"],
                    row["status"],
                    row["attempt_count"],
                    row["lease_generation"],
                    current_text,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return None, progressed
            claim = QuotaEventClaim(
                outbox_id=_matched(row["quota_event_outbox_id"], _OUTBOX_ID),
                event_id=_matched(row["quota_event_id"], _EVENT_ID),
                tenant_id=_text(row["tenant_id"]),
                source_request_id=_text(row["source_request_id"]),
                source_trace_id=_text(row["source_trace_id"]),
                event=event,
                attempt_count=int(row["attempt_count"]),
                lease_generation=generation,
                lease_owner_id=owner,
                lease_token=token,
                lease_expires_at=expires,
                snapshot_json=snapshot_json,
            )
            connection.commit()
            return claim, True
    # 领域错误先回滚当前租约事务，再保持原有稳定错误码交给调度层处理。
    except ApiError:
        _rollback(connection)
        raise
    # 数据库或冻结快照损坏统一失败关闭，禁止泄露 SQL、payload 与租约 token。
    except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as exc:
        _rollback(connection)
        raise ApiError("quota.unavailable") from exc
    finally:
        connection.close()


def dispatch_claimed_quota_event(
    config: AppConfig,
    claim: QuotaEventClaim,
    *,
    now: datetime | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """投递冻结快照并以精确租约 CAS 收敛成功、重试或失败终态。"""

    def source_guard() -> None:
        """在每次外部投递前复核停止信号并续租，阻断迟到 worker。"""

        # 应用停止后禁止发起新的外部调用，避免 teardown 期间产生无 owner 副作用。
        if should_stop is not None and should_stop():
            raise ApiError("quota.unavailable")
        _renew(config, claim, now=now)

    error_code: str | None = None
    try:
        # 首次网络调用前同样检查停止信号，不能只依赖投递过程中的续租回调。
        if should_stop is not None and should_stop():
            raise ApiError("quota.unavailable")
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
    # payload 无法重建时固定终结事件，禁止把损坏事实无限重试或写入日志正文。
    except ValueError:
        error_code = "webhook.payload_invalid"
    except Exception:
        error_code = "webhook.delivery_unavailable"
    return _finish(config, claim, error_code=error_code, now=now)


def drain_quota_event_once(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """领取并恢复至多一个事件 source，空队列和停止信号均返回无工作。"""

    if should_stop is not None and should_stop():
        return False
    claim, progressed = _claim_next_quota_event_with_progress(
        config,
        worker_id=worker_id,
        now=now,
    )
    if claim is None:
        return progressed
    dispatch_claimed_quota_event(config, claim, now=now, should_stop=should_stop)
    return True


def recover_quota_source_request_id(
    connection: sqlite3.Connection,
    envelope: dict[str, object],
) -> str:
    """从不可变同租户 source 图恢复私有请求 owner，并逐字段验证公开信封。"""

    # 恢复入口只接受结构化信封，其他类型不能参与租户和事件联合查询。
    if type(envelope) is not dict:
        raise ApiError("webhook.delivery_unavailable")
    row = connection.execute(
        """
        SELECT * FROM quota_events
        WHERE tenant_id=? AND quota_event_id=?
        """,
        (envelope.get("tenant_id"), envelope.get("event_id")),
    ).fetchone()
    # 不存在或跨租户 source 使用同一固定错误，避免暴露事件归属。
    if row is None:
        raise ApiError("webhook.delivery_unavailable")
    event = _event_from_row(row)
    # 公开信封必须与权威事件逐字段一致，客户端不能替换 payload 或链路字段。
    if event.to_payload() != envelope:
        raise ApiError("webhook.delivery_unavailable")
    return _text(row["source_request_id"])


def _event_from_row(row: sqlite3.Row) -> WebhookEvent:
    """验证额度事件权威行并重建安全 WebhookEvent，不信任持久 JSON 的自报 owner。"""

    event_id = _matched(row["quota_event_id"], _EVENT_ID)
    tenant_id = _text(row["tenant_id"])
    plan_id = _text(row["plan_id"])
    event_type = _text(row["event_type"])
    # 只允许 PRD-079 冻结的两个公开事件类型，动态事件名必须失败关闭。
    if event_type not in {"quota.exhausted", "trial.expired"}:
        raise ApiError("quota.unavailable")
    try:
        payload = json.loads(_text(row["payload_json"]))
    # 持久 payload 无法解析时按事件不可用收敛，不能把原始内容带入异常。
    except (ValueError, json.JSONDecodeError) as exc:
        raise ApiError("quota.unavailable") from exc
    # payload 的 Plan owner 必须与独立列一致，禁止 JSON 覆盖租户内对象归属。
    if type(payload) is not dict or payload.get("plan_id") != plan_id:
        raise ApiError("quota.unavailable")
    if event_type == "trial.expired":
        # 试用到期只允许 trial cycle，不能混入额度 generation 形成歧义事件。
        if row["trial_cycle"] != payload.get("trial_cycle") or row["quota_generation"] is not None:
            raise ApiError("quota.unavailable")
    elif (
        row["quota_generation"] != payload.get("quota_generation")
        or row["trial_cycle"] is not None
    ):
        # 额度耗尽只允许 generation，持久列与 payload 不一致时拒绝投递。
        raise ApiError("quota.unavailable")
    return build_webhook_event(
        tenant_id=tenant_id,
        event_type=event_type,
        event_id=event_id,
        timestamp=_parse_time(row["occurred_at"]),
        actor_id=_text(row["actor_id"]),
        object_id=plan_id,
        trace_id=_text(row["source_trace_id"]),
        source_request_id=_text(row["source_request_id"]),
        payload=payload,
    )


def _freeze_or_verify_snapshot(
    connection: sqlite3.Connection,
    event: WebhookEvent,
    *,
    existing: object,
    frozen_at: object,
    now: str,
) -> str:
    """首次冻结原始 delivery ID 集合，后续领取必须验证快照与权威行完全一致。"""

    from metahub_control.webhooks.service import freeze_webhook_event_delivery_snapshot

    # 未冻结快照时 frozen_at 也必须为空，避免接受半提交的事件 source。
    if existing is None:
        if frozen_at is not None:
            raise ApiError("quota.unavailable")
        freeze_webhook_event_delivery_snapshot(connection, event, frozen_at=now)
        values = _delivery_ids(connection, event)
    else:
        # 已有快照必须同时具有规范冻结时间，否则租约无法证明投递集合来源。
        if type(frozen_at) is not str:
            raise ApiError("quota.unavailable")
        values = _parse_snapshot(existing)
        # 当前权威 delivery 集合不得漂移，防止恢复时扩大或缩小原始订阅者范围。
        if values != _delivery_ids(connection, event):
            raise ApiError("quota.unavailable")
    return _canonical_json(values)


def _quarantine_invalid_event(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    owner: str,
    token: str,
    generation: int,
    lease_expires: datetime,
    now: str,
) -> None:
    """通过两步租约 CAS 将无法重建的持久事件固定为 payload invalid。"""

    claimed = connection.execute(
        """
        UPDATE quota_event_outbox
        SET status='processing',lease_generation=?,lease_owner_id=?,lease_token=?,
            lease_expires_at=?,next_attempt_at=NULL,last_error_code=NULL,updated_at=?
        WHERE tenant_id=? AND quota_event_outbox_id=? AND status=?
          AND attempt_count=? AND lease_generation=?
          AND (status='pending' OR unixepoch(lease_expires_at)<=unixepoch(?))
        """,
        (
            generation,
            owner,
            token,
            _db_time(lease_expires),
            now,
            row["tenant_id"],
            row["quota_event_outbox_id"],
            row["status"],
            row["attempt_count"],
            row["lease_generation"],
            now,
        ),
    )
    if claimed.rowcount != 1:
        raise ApiError("quota.unavailable")
    event_type = _text(row["event_type"])
    if event_type in {"quota.exhausted", "trial.expired"}:
        from metahub_control.webhooks.service import fail_pending_webhook_event_deliveries

        fail_pending_webhook_event_deliveries(
            connection,
            tenant_id=_text(row["tenant_id"]),
            event_id=_text(row["quota_event_id"]),
            event_type=event_type,
            updated_at=now,
            failure_code="webhook.payload_invalid",
        )
    failed = connection.execute(
        """
        UPDATE quota_event_outbox
        SET status='failed',attempt_count=attempt_count+1,
            lease_generation=lease_generation+1,lease_owner_id=NULL,lease_token=NULL,
            lease_expires_at=NULL,next_attempt_at=NULL,
            last_error_code='webhook.payload_invalid',dispatched_at=NULL,updated_at=?
        WHERE tenant_id=? AND quota_event_outbox_id=? AND status='processing'
          AND attempt_count=? AND lease_generation=? AND lease_owner_id=? AND lease_token=?
        """,
        (
            now,
            row["tenant_id"],
            row["quota_event_outbox_id"],
            row["attempt_count"],
            generation,
            owner,
            token,
        ),
    )
    if failed.rowcount != 1:
        raise ApiError("quota.unavailable")


def _delivery_ids(connection: sqlite3.Connection, event: WebhookEvent) -> list[str]:
    """读取同租户原始 delivery 标识并校验固定格式，结果始终稳定排序。"""

    rows = connection.execute(
        """
        SELECT webhook_delivery_id FROM webhook_deliveries
        WHERE tenant_id=? AND event_id=? AND delivery_kind='original'
        ORDER BY webhook_delivery_id
        """,
        (event.tenant_id, event.event_id),
    ).fetchall()
    values = [str(row["webhook_delivery_id"]) for row in rows]
    # 任一异常 delivery ID 都表示持久事实不可证明，不能带入冻结快照。
    if any(_DELIVERY_ID.fullmatch(value) is None for value in values):
        raise ApiError("quota.unavailable")
    return values


def _parse_snapshot(value: object) -> list[str]:
    """解析规范 delivery 快照，拒绝非字符串、重复、乱序或非法标识。"""

    # 快照只能是持久化 JSON 文本，其他类型表示数据库事实已损坏。
    if type(value) is not str:
        raise ApiError("quota.unavailable")
    decoded = json.loads(value)
    if (
        type(decoded) is not list
        or decoded != sorted(set(decoded))
        or any(type(item) is not str or _DELIVERY_ID.fullmatch(item) is None for item in decoded)
    ):
        # 快照必须是去重排序后的固定 ID 集合，避免同一投递被重复或遗漏。
        raise ApiError("quota.unavailable")
    return decoded


def _renew(config: AppConfig, claim: QuotaEventClaim, *, now: datetime | None) -> None:
    """以当前 owner、generation、token 和快照续租，成功后才更新内存 claim。"""

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        token = secrets.token_hex(32)
        generation = claim.lease_generation + 1
        expires = current + timedelta(seconds=EVENT_LEASE_SECONDS)
        cursor = connection.execute(
            """
            UPDATE quota_event_outbox
            SET lease_generation=?,lease_token=?,lease_expires_at=?,updated_at=?
            WHERE tenant_id=? AND quota_event_outbox_id=? AND status='processing'
              AND attempt_count=? AND lease_generation=? AND lease_owner_id=?
              AND lease_token=? AND delivery_snapshot_json=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                generation,
                token,
                _db_time(expires),
                _db_time(current),
                claim.tenant_id,
                claim.outbox_id,
                claim.attempt_count,
                claim.lease_generation,
                claim.lease_owner_id,
                claim.lease_token,
                claim.snapshot_json,
                _db_time(current),
            ),
        )
        # CAS 未命中表示租约已过期或被接管，旧 worker 必须在外部调用前停止。
        if cursor.rowcount != 1:
            raise ApiError("quota.unavailable")
        connection.commit()
        claim.lease_generation = generation
        claim.lease_token = token
        claim.lease_expires_at = expires
    except Exception:
        _rollback(connection)
        raise
    finally:
        connection.close()


def _finish(
    config: AppConfig,
    claim: QuotaEventClaim,
    *,
    error_code: str | None,
    now: datetime | None,
) -> bool:
    """按精确租约提交事件终态或有界退避，CAS 失败时不覆盖新 owner。"""

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        current_text = _db_time(current)
        attempts = claim.attempt_count + 1
        terminal = error_code == "webhook.payload_invalid" or attempts >= MAX_ATTEMPTS
        # 只有没有错误的投递可以进入 dispatched，固定错误决定终结或有界重试。
        if error_code is None:
            status = "dispatched"
        elif terminal:
            status = "failed"
            from metahub_control.webhooks.service import fail_pending_webhook_event_deliveries

            fail_pending_webhook_event_deliveries(
                connection,
                tenant_id=claim.tenant_id,
                event_id=claim.event_id,
                event_type=claim.event.event_type,
                updated_at=current_text,
                failure_code=error_code,
            )
        else:
            status = "pending"
        delay = BACKOFF_SECONDS[min(max(attempts - 1, 0), len(BACKOFF_SECONDS) - 1)]
        # 只有可重试状态携带下一次调度时间；终态显式清空，避免运维查询把历史完成时间
        # 误读为仍然到期的任务。真正的完成时间由 dispatched_at/updated_at 分别表达。
        next_attempt = (
            _db_time(current + timedelta(seconds=delay)) if status == "pending" else None
        )
        cursor = connection.execute(
            """
            UPDATE quota_event_outbox
            SET status=?,attempt_count=?,lease_generation=lease_generation+1,
                lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
                next_attempt_at=?,last_error_code=?,dispatched_at=?,updated_at=?
            WHERE tenant_id=? AND quota_event_outbox_id=? AND status='processing'
              AND attempt_count=? AND lease_generation=? AND lease_owner_id=?
              AND lease_token=? AND delivery_snapshot_json=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                status,
                attempts,
                next_attempt,
                None if status == "dispatched" else error_code,
                current_text if status == "dispatched" else None,
                current_text,
                claim.tenant_id,
                claim.outbox_id,
                claim.attempt_count,
                claim.lease_generation,
                claim.lease_owner_id,
                claim.lease_token,
                claim.snapshot_json,
                current_text,
            ),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            return False
        connection.commit()
        return True
    # 终态提交失败只回滚当前 worker 事务并返回失败，不泄露 SQL 或租约坐标。
    except (ApiError, sqlite3.Error, TypeError, ValueError):
        _rollback(connection)
        return False
    finally:
        connection.close()


def _transaction_now(connection: sqlite3.Connection, override: datetime | None) -> datetime:
    """在事务内取得规范 UTC 秒；测试覆盖值也必须携带明确时区。"""

    if override is not None:
        # 无时区覆盖会破坏租约比较，必须在写入任何状态前失败关闭。
        if override.tzinfo is None:
            raise ApiError("quota.unavailable")
        return override.astimezone(UTC).replace(microsecond=0)
    row = connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')").fetchone()
    # 数据库时钟无返回值时无法证明租约顺序，当前事务必须失败关闭。
    if row is None:
        raise ApiError("quota.unavailable")
    return _parse_time(row[0])


def _parse_time(value: object) -> datetime:
    """解析数据库规范 UTC 秒文本，拒绝小数秒、无时区或非规范等价值。"""

    # 只接受数据库约定的 Z 结尾秒级文本，避免多种等价时间格式参与 CAS。
    if type(value) is not str or not value.endswith("Z") or "." in value:
        raise ValueError("quota time invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # 解析后必须能逐字规范化回原值，否则拒绝模糊或非 UTC 时间。
    if parsed.tzinfo is None or _db_time(parsed) != value:
        raise ValueError("quota time invalid")
    return parsed.astimezone(UTC)


def _db_time(value: datetime) -> str:
    """把有时区时间规范化为数据库使用的 UTC 秒级 Z 文本。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical_json(value: object) -> str:
    """生成稳定 ASCII JSON，用于冻结投递快照和后续逐字验证。"""

    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _worker_id(value: object) -> str:
    """校验有界后台 worker 标识，拒绝控制字符和动态长 owner。"""

    # worker owner 会进入租约 CAS，格式异常时不能接触数据库状态。
    if type(value) is not str or _WORKER_ID.fullmatch(value) is None:
        raise ApiError("quota.unavailable")
    return value


def _matched(value: object, pattern: re.Pattern[str]) -> str:
    """校验持久标识符合指定领域格式，异常值统一按额度不可用处理。"""

    text = _text(value)
    # 持久 ID 不符合领域前缀时不能继续组装租户事件或投递坐标。
    if pattern.fullmatch(text) is None:
        raise ApiError("quota.unavailable")
    return text


def _text(value: object) -> str:
    """读取非空持久文本，禁止空值被当作租户、事件或链路 owner。"""

    # owner 与链路字段必须是非空文本，数据库类型漂移时立即失败关闭。
    if type(value) is not str or not value:
        raise ApiError("quota.unavailable")
    return value


def _connect(config: AppConfig) -> sqlite3.Connection:
    """建立额度恢复数据库连接，并把连接故障收敛为稳定公开错误。"""

    try:
        return connect_database(config.database_url)
    # 连接错误不暴露数据库路径或驱动正文，只返回额度不可用合同。
    except DbError as exc:
        raise ApiError("quota.unavailable") from exc


def _rollback(connection: sqlite3.Connection) -> None:
    """尽力回滚当前连接；回滚自身失败不能覆盖原始领域错误。"""

    try:
        if connection.in_transaction:
            connection.rollback()
    # 回滚异常仅表示清理失败，调用方仍需保留先前的固定错误语义。
    except sqlite3.Error:
        return


__all__ = [
    "QuotaEventClaim",
    "claim_next_quota_event",
    "dispatch_claimed_quota_event",
    "drain_quota_event_once",
    "recover_quota_source_request_id",
]
