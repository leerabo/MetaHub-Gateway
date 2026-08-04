"""PRD-053 成本异常 Webhook outbox 的 lease、恢复与 at-least-once 调度。

检测事务只把持久 anomaly 标为 pending，不执行网络请求。本模块在独立事务中领取
event lease，关闭连接后构造精确安全事件并调用公共 Webhook dispatcher；完成结果再以
claim token CAS 回写。远端可能在本地确认前已收到事件，因此合同是稳定 event_id 的
at-least-once，而不是虚假的网络 exactly-once。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_control.webhooks.service import dispatch_webhook_event
from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import build_webhook_event

# 事件租约、最大尝试次数和分级退避共同限定 at-least-once 投递的恢复节奏。
EVENT_LEASE_SECONDS = 30
EVENT_MAX_ATTEMPTS = 10
EVENT_BACKOFF_SECONDS = (5, 30, 300)


@dataclass(frozen=True, slots=True)
class CostAnomalyEventClaim:
    """保存一次 source anomaly event 租约及不可变的安全 Webhook 证据快照。"""

    cost_anomaly_id: str
    event_id: str
    tenant_id: str
    operation_trace_id: str
    event_claim_token: str
    event_lease_owner_id: str
    event_dispatch_attempts: int
    event_lease_expires_at: datetime
    detected_at: datetime
    rule_type: str
    severity: str
    status: str
    environment_id: str
    scope_type: str
    scope_id: str
    window_start: datetime
    window_end: datetime
    current_cost_micros: str
    baseline_total_micros: str
    observed_ratio_millis: str | None
    baseline_bucket_count: int
    threshold_multiplier_millis: int
    minimum_cost_micros: int
    source_entry_count: int
    estimated_entry_count: int
    affected_virtual_key_count: int
    recommended_action: str
    detection_version: str


def claim_next_cost_anomaly_event(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> CostAnomalyEventClaim | None:
    """原子领取一条到期 pending 或租约已过期的 processing 成本异常事件。"""

    current = _now(now)
    owner = _worker_id(worker_id)
    # 候选资格必须按真实服务时钟判断，不能因行版本递增而提前领取未来事件。
    eligible_at = _to_db_time(current)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT ca.*, run.operation_trace_id
            FROM cost_anomalies ca
            JOIN cost_anomaly_detection_runs run
              ON run.tenant_id=ca.tenant_id
             AND run.environment_id=ca.environment_id
             AND run.detection_run_id=ca.detection_run_id
            WHERE (
                event_dispatch_status='pending'
                AND event_dispatch_attempts<?
                AND unixepoch(event_next_attempt_at)<=unixepoch(?)
              )
              OR (
                event_dispatch_status='processing'
                AND event_dispatch_attempts<?
                AND unixepoch(event_lease_expires_at)<=unixepoch(?)
              )
            ORDER BY ca.anomaly_sequence ASC
            LIMIT 1
            """,
            (EVENT_MAX_ATTEMPTS, eligible_at, EVENT_MAX_ATTEMPTS, eligible_at),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        token = uuid4().hex
        attempts = int(row["event_dispatch_attempts"]) + 1
        # detector 插入和上一次 outbox 更新都可能与本次 claim 同秒；锁内基于当前
        # row.updated_at 推进版本时间，并从该时刻计算完整 30 秒租约。
        claim_time = max(current, _parse_time(row["updated_at"]) + timedelta(seconds=1))
        timestamp = _to_db_time(claim_time)
        lease_expires = claim_time + timedelta(seconds=EVENT_LEASE_SECONDS)
        updated = connection.execute(
            """
            UPDATE cost_anomalies
            SET event_dispatch_status='processing',
                event_dispatch_attempts=?,
                event_next_attempt_at=NULL,
                event_claim_token=?,
                event_lease_owner_id=?,
                event_lease_expires_at=?,
                event_last_error_code=NULL,
                updated_at=?
            WHERE tenant_id=? AND cost_anomaly_id=?
              AND event_dispatch_attempts=?
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
                timestamp,
                row["tenant_id"],
                row["cost_anomaly_id"],
                attempts - 1,
                eligible_at,
                eligible_at,
            ),
        )
        if updated.rowcount != 1:
            connection.rollback()
            return None
        connection.commit()
        # claim 只返回已通过 tenant、run 归属和 CAS 校验的不可变事件证据。
        return _claim_from_row(row, token, owner, attempts, lease_expires)
    # 领取事务失败统一折叠为固定领域错误，绝不泄露数据库结构与路径。
    except sqlite3.Error as exc:
        _rollback(connection)
        raise ApiError("cost_anomaly.detector_unavailable") from exc
    finally:
        connection.close()


def dispatch_claimed_cost_anomaly_event(
    config: AppConfig,
    claim: CostAnomalyEventClaim,
    *,
    now: datetime | None = None,
) -> bool:
    """续投 claim 的持久事件，并用 source lease CAS 标记 dispatched 或重试。"""

    event = None
    error_code: str | None = None
    try:
        event = build_webhook_event(
            tenant_id=claim.tenant_id,
            event_type="cost.anomaly_detected",
            event_id=claim.event_id,
            timestamp=claim.detected_at,
            actor_id=None,
            object_id=claim.cost_anomaly_id,
            trace_id=claim.operation_trace_id,
            payload={
                "anomaly_id": claim.cost_anomaly_id,
                "rule_type": claim.rule_type,
                "severity": claim.severity,
                "status": "open",
                "environment_id": claim.environment_id,
                "scope_type": claim.scope_type,
                "scope_id": claim.scope_id,
                "window_start": _to_db_time(claim.window_start),
                "window_end": _to_db_time(claim.window_end),
                "current_cost_micros": claim.current_cost_micros,
                "baseline_total_micros": claim.baseline_total_micros,
                "observed_ratio_millis": claim.observed_ratio_millis,
                "baseline_bucket_count": claim.baseline_bucket_count,
                "threshold_multiplier_millis": claim.threshold_multiplier_millis,
                "minimum_cost_micros": claim.minimum_cost_micros,
                "source_entry_count": claim.source_entry_count,
                "estimated_entry_count": claim.estimated_entry_count,
                "affected_virtual_key_count": claim.affected_virtual_key_count,
                "recommended_action": claim.recommended_action,
                "detection_version": claim.detection_version,
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
    # 构造器拒绝非法事件合同，只保存固定事件类型错误码而不保存原始正文。
    except ValueError:
        error_code = "webhook.event_type_invalid"
    # 公共投递器的领域异常不得把订阅地址、密钥或远端响应带回 source outbox。
    except ApiError:
        error_code = "webhook.delivery_unavailable"
    except Exception:
        # 网络和第三方异常正文可能含 URL 或响应内容，source outbox 只保存固定原因码。
        error_code = "webhook.delivery_unavailable"
    completed_at = _now(now)
    if error_code is None and event is not None:
        return _finish_success(config, claim, completed_at)
    return _finish_failure(
        config,
        claim,
        completed_at,
        error_code or "webhook.delivery_unavailable",
    )


def drain_cost_anomaly_event_once(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> bool:
    """领取并处理一个 outbox 事件；没有可运行事件时返回 False。"""

    claim = claim_next_cost_anomaly_event(config, worker_id=worker_id, now=now)
    if claim is None:
        return False
    dispatch_claimed_cost_anomaly_event(config, claim, now=now)
    return True


def _finish_success(
    config: AppConfig,
    claim: CostAnomalyEventClaim,
    now: datetime,
) -> bool:
    """仅在 source lease 仍有效时标记 dispatched，并清空所有租约字段。"""

    completed_at = _finish_time(claim, now)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        timestamp = _to_db_time(completed_at)
        updated = connection.execute(
            """
            UPDATE cost_anomalies
            SET event_dispatch_status='dispatched',
                event_next_attempt_at=NULL,
                event_dispatched_at=?,
                event_claim_token=NULL,
                event_lease_owner_id=NULL,
                event_lease_expires_at=NULL,
                event_last_error_code=NULL,
                updated_at=?
            WHERE tenant_id=? AND cost_anomaly_id=?
              AND event_dispatch_status='processing'
              AND event_dispatch_attempts=?
              AND event_claim_token=? AND event_lease_owner_id=?
              AND unixepoch(event_lease_expires_at)>unixepoch(?)
            """,
            (
                timestamp,
                timestamp,
                claim.tenant_id,
                claim.cost_anomaly_id,
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
    # source 状态回写失败时保留稳定错误面，调用方可在下一轮安全恢复。
    except sqlite3.Error as exc:
        _rollback(connection)
        raise ApiError("cost_anomaly.detector_unavailable") from exc
    finally:
        connection.close()


def _finish_failure(
    config: AppConfig,
    claim: CostAnomalyEventClaim,
    now: datetime,
    error_code: str,
) -> bool:
    """按源尝试次数退避或终结 failed；原始异常内容永不持久化。"""

    allowed = {
        "webhook.event_type_invalid",
        "webhook.delivery_unavailable",
        "cost_anomaly.detector_database_unavailable",
    }
    reason = error_code if error_code in allowed else "webhook.delivery_unavailable"
    final = claim.event_dispatch_attempts >= EVENT_MAX_ATTEMPTS
    delay_index = min(
        max(claim.event_dispatch_attempts - 1, 0),
        len(EVENT_BACKOFF_SECONDS) - 1,
    )
    completed_at = _finish_time(claim, now)
    next_attempt = completed_at + timedelta(seconds=EVENT_BACKOFF_SECONDS[delay_index])
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        timestamp = _to_db_time(completed_at)
        updated = connection.execute(
            """
            UPDATE cost_anomalies
            SET event_dispatch_status=?,
                event_next_attempt_at=?,
                event_dispatched_at=NULL,
                event_claim_token=NULL,
                event_lease_owner_id=NULL,
                event_lease_expires_at=NULL,
                event_last_error_code=?,
                updated_at=?
            WHERE tenant_id=? AND cost_anomaly_id=?
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
                claim.tenant_id,
                claim.cost_anomaly_id,
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
    # 失败退避的事务异常同样不能记录 SQLite 原文或破坏当前 lease 证据。
    except sqlite3.Error as exc:
        _rollback(connection)
        raise ApiError("cost_anomaly.detector_unavailable") from exc
    finally:
        connection.close()


def _finish_time(claim: CostAnomalyEventClaim, value: datetime) -> datetime:
    """生成满足数据库秒级单调约束的 source finish 时刻。

    scheduler 会把同一个注入时刻同时传给 claim 和 finish；claim 已把 updated_at 写成
    单调业务秒，因此 finish 必须至少再晚 1 秒。detector 刚插入后 claim 可能已比注入
    时钟前进 1 秒，此差值允许继续推进；更大的时钟倒退仍失败关闭。推进值始终处于
    固定 30 秒 lease 内，已经到期则保留当前时刻并由 SQL CAS 返回 False。
    """

    current = _now(value)
    lease_expires = _now(claim.event_lease_expires_at)
    claimed_at = lease_expires - timedelta(seconds=EVENT_LEASE_SECONDS)
    if current < claimed_at - timedelta(seconds=1):
        # 超过同秒单调推进幅度说明注入值或系统时钟真实倒退，必须失败关闭。
        raise ApiError("cost_anomaly.detector_unavailable")
    if current <= claimed_at:
        # detector→claim→finish 共用同一注入秒时，从实际 claim 版本再推进一秒。
        return claimed_at + timedelta(seconds=1)
    return current

def _claim_from_row(
    row: sqlite3.Row,
    token: str,
    owner: str,
    attempts: int,
    lease_expires: datetime,
) -> CostAnomalyEventClaim:
    """从数据库行恢复不可变 source event claim，并验证核心标量类型。"""

    return CostAnomalyEventClaim(
        cost_anomaly_id=str(row["cost_anomaly_id"]),
        event_id=str(row["event_id"]),
        tenant_id=str(row["tenant_id"]),
        operation_trace_id=str(row["operation_trace_id"]),
        event_claim_token=token,
        event_lease_owner_id=owner,
        event_dispatch_attempts=_positive(attempts),
        event_lease_expires_at=lease_expires,
        detected_at=_parse_time(row["detected_at"]),
        rule_type=str(row["rule_type"]),
        severity=str(row["severity"]),
        status=str(row["status"]),
        environment_id=str(row["environment_id"]),
        scope_type=str(row["scope_type"]),
        scope_id=str(row["scope_id"]),
        window_start=_parse_time(row["window_start"]),
        window_end=_parse_time(row["window_end"]),
        current_cost_micros=_decimal_text(row["current_cost_micros"]),
        baseline_total_micros=_decimal_text(row["baseline_total_micros"]),
        observed_ratio_millis=(
            _decimal_text(row["observed_ratio_millis"])
            if row["observed_ratio_millis"] is not None
            else None
        ),
        baseline_bucket_count=_positive(row["baseline_bucket_count"]),
        threshold_multiplier_millis=_positive(row["threshold_multiplier_millis"]),
        minimum_cost_micros=_positive(row["minimum_cost_micros"]),
        source_entry_count=_positive(row["source_entry_count"]),
        estimated_entry_count=_non_negative(row["estimated_entry_count"]),
        affected_virtual_key_count=_positive(row["affected_virtual_key_count"]),
        recommended_action=str(row["recommended_action"]),
        detection_version=str(row["detection_version"]),
    )


def _decimal_text(value: object) -> str:
    """恢复最长四十位 canonical 非负十进制字符串。"""

    if (
        type(value) is not str
        or not 1 <= len(value) <= 40
        or not value.isascii()
        or not value.isdigit()
        or (len(value) > 1 and value.startswith("0"))
    ):
        # 金额证据若不是 canonical 非负十进制文本，事件必须停止投递以免篡改语义。
        raise ApiError("cost_anomaly.detector_unavailable")
    return value


def _positive(value: object) -> int:
    """恢复数据库中的严格正整数，明确拒绝布尔值、浮点值与非正数。"""

    if type(value) is not int or value <= 0:
        # 计数和阈值必须保持数据库整数合同，损坏证据不得进入 Webhook。
        raise ApiError("cost_anomaly.detector_unavailable")
    return value


def _non_negative(value: object) -> int:
    """恢复数据库中的非负整数计数，明确拒绝布尔值、浮点值与负数。"""

    if type(value) is not int or value < 0:
        # 估算计数允许为零但不能为负或浮点，异常值按持久事实损坏处理。
        raise ApiError("cost_anomaly.detector_unavailable")
    return value


def _worker_id(value: object) -> str:
    """校验内部 lease owner，不允许控制字符或无界文本。"""

    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 128 or any(ord(char) < 32 for char in normalized):
        # owner 会写入租约并参与 CAS，空值、控制字符和无界文本都必须拒绝。
        raise ApiError("cost_anomaly.detector_unavailable")
    return normalized


def _now(value: datetime | None) -> datetime:
    """返回 UTC 秒级服务器时刻；仅内部测试可注入。"""

    current = value if value is not None else datetime.now(UTC)
    if not isinstance(current, datetime) or current.tzinfo is None:
        # lease 比较必须基于有时区 UTC 时刻，naive 或非时间值会破坏接管边界。
        raise ApiError("cost_anomaly.detector_unavailable")
    return current.astimezone(UTC).replace(microsecond=0)


def _to_db_time(value: datetime) -> str:
    """把有时区业务时刻编码为数据库统一使用的 UTC 秒级 RFC3339 文本。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_time(value: object) -> datetime:
    """解析持久 UTC 时间，损坏行按 source outbox 不可用处理。"""

    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    # 持久时间无法解析时统一失败关闭，不把底层值或转换异常写入日志。
    except (TypeError, ValueError) as exc:
        raise ApiError("cost_anomaly.detector_unavailable") from exc
    if parsed.tzinfo is None:
        # 数据库时间必须携带显式时区，禁止把本地时区猜测成 UTC。
        raise ApiError("cost_anomaly.detector_unavailable")
    return parsed.astimezone(UTC)


def _connect(config: AppConfig) -> sqlite3.Connection:
    """创建 outbox 独享连接并隐藏数据库位置。"""

    try:
        return connect_database(config.database_url)
    # 连接错误统一映射，数据库 URL、文件位置和驱动正文不得进入 API 响应。
    except DbError as exc:
        raise ApiError("cost_anomaly.detector_unavailable") from exc


def _rollback(connection: sqlite3.Connection) -> None:
    """尽力回滚当前 source outbox 事务，同时保留最初的稳定领域异常。"""

    try:
        connection.rollback()
    # 回滚失败不能遮蔽最初的领域错误，也不能向调用方泄露 SQLite 原文。
    except sqlite3.Error:
        return