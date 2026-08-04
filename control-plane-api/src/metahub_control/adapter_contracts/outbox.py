"""PRD-064 ``adapter.contract_failed`` Source Outbox 的 lease、CAS 与恢复投递。

失败 run 事务只写一条 pending source；worker 从不可变 run 重建相同 payload 和链路身份，再交给
Webhook dispatcher 冻结 Endpoint 快照。稳定 event_id 提供 at-least-once 恢复，不能宣称网络
层 exactly-once，也不能把 Endpoint、Secret、响应正文或异常文本写回 source。
"""

from __future__ import annotations

import json
import re
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

from .service import ALLOWED_FAILURE_CODES


# 十次尝试与固定退避沿用控制面已有 Source Outbox 口径；lease 覆盖单轮有界 Webhook 网络超时。
EVENT_LEASE_SECONDS = 30
EVENT_MAX_ATTEMPTS = 10
EVENT_BACKOFF_SECONDS = (5, 30, 300)
_EVENT_ID = re.compile(r"evt_acr_[0-9a-f]{32}\Z")
_RUN_ID = re.compile(r"acr_[0-9a-f]{32}\Z")
_WORKER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


@dataclass(frozen=True, slots=True)
class AdapterContractEventClaim:
    """一次失败事件 lease 及重建签名事件所需的安全冻结事实。"""

    event_id: str
    tenant_id: str
    adapter_contract_run_id: str
    source_request_id: str
    source_trace_id: str
    catalog_version: str
    catalog_hash: str
    suite_kind: str
    mode: str
    total_cases: int
    failed_cases: int
    failure_codes: tuple[str, ...]
    completed_at: datetime
    claim_token: str
    lease_owner_id: str
    dispatch_attempts: int
    lease_expires_at: datetime


def claim_next_adapter_contract_event(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> AdapterContractEventClaim | None:
    """原子领取一条到期 source；第十次孤儿 lease 到期后只终结，不再出网。"""

    owner = _normalize_worker_id(worker_id)
    current = _now(now)
    current_text = _db_time(current)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        # 第十次外呼可能在 finish CAS 前崩溃；lease 到期后转 failed，防止第十一次发送。
        connection.execute(
            """
            UPDATE adapter_contract_event_outbox
            SET dispatch_status='failed',next_attempt_at=NULL,dispatched_at=NULL,
                claim_token=NULL,lease_owner_id=NULL,lease_expires_at=NULL,
                last_error_code='adapter.contract_storage_unavailable',updated_at=?
            WHERE event_id=(
              SELECT event_id FROM adapter_contract_event_outbox
              WHERE dispatch_status='processing' AND dispatch_attempts=?
                AND unixepoch(lease_expires_at)<=unixepoch(?)
              ORDER BY lease_expires_at,created_at,event_id LIMIT 1
            )
              AND dispatch_status='processing' AND dispatch_attempts=?
              AND unixepoch(lease_expires_at)<=unixepoch(?)
            """,
            (current_text, EVENT_MAX_ATTEMPTS, current_text, EVENT_MAX_ATTEMPTS, current_text),
        )
        row = connection.execute(
            """
            SELECT source.*,run.source_request_id,run.source_trace_id,run.catalog_version,
                   run.catalog_hash,run.suite_kind,run.mode,run.total_cases,run.failed_cases,
                   run.failure_codes_json,run.completed_at,run.status AS run_status
            FROM adapter_contract_event_outbox source
            JOIN adapter_contract_runs run
              ON run.tenant_id=source.tenant_id
             AND run.adapter_contract_run_id=source.adapter_contract_run_id
            WHERE source.dispatch_status IN ('pending','processing')
              AND (
                (source.dispatch_status='pending' AND source.dispatch_attempts<?
                  AND unixepoch(source.next_attempt_at)<=unixepoch(?))
                OR
                (source.dispatch_status='processing' AND source.dispatch_attempts<?
                  AND unixepoch(source.lease_expires_at)<=unixepoch(?))
              )
            ORDER BY CASE source.dispatch_status WHEN 'processing' THEN 0 ELSE 1 END,
                     CASE WHEN source.dispatch_status='processing'
                       THEN source.lease_expires_at ELSE source.next_attempt_at END,
                     source.created_at,source.event_id
            LIMIT 1
            """,
            (EVENT_MAX_ATTEMPTS, current_text, EVENT_MAX_ATTEMPTS, current_text),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        attempts = int(row["dispatch_attempts"]) + 1
        token = uuid4().hex
        # updated_at 不回退；同秒 claim 允许相等，lease 仍由 UTC 秒加固定时限派生。
        claim_time = max(current, _parse_time(row["updated_at"]))
        lease_expires = claim_time + timedelta(seconds=EVENT_LEASE_SECONDS)
        updated = connection.execute(
            """
            UPDATE adapter_contract_event_outbox
            SET dispatch_status='processing',dispatch_attempts=?,next_attempt_at=NULL,
                claim_token=?,lease_owner_id=?,lease_expires_at=?,last_error_code=NULL,updated_at=?
            WHERE event_id=? AND dispatch_attempts=?
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
                _db_time(lease_expires),
                _db_time(claim_time),
                row["event_id"],
                attempts - 1,
                current_text,
                current_text,
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
    # 已归一化领域错误需要保留原错误码，但事务仍必须回滚后再向上抛出。
    except ApiError:
        connection.rollback()
        raise
    # 数据库或持久事实解析失败都按存储不可用处理，禁止泄露损坏字段内容。
    except (sqlite3.Error, TypeError, ValueError):
        connection.rollback()
        # 回滚完成后切断底层异常链，只向调用方公开固定的 503 领域错误。
        raise ApiError("adapter.contract_storage_unavailable") from None
    finally:
        connection.close()


def dispatch_claimed_adapter_contract_event(
    config: AppConfig,
    claim: AdapterContractEventClaim,
    *,
    now: datetime | None = None,
) -> bool:
    """重建并投递一个已领取事件，再以 token/owner/attempt/lease 五重 CAS 回写。"""

    error_code: str | None = None
    try:
        event = build_webhook_event(
            tenant_id=claim.tenant_id,
            event_type="adapter.contract_failed",
            event_id=claim.event_id,
            timestamp=claim.completed_at,
            actor_id="adapter-contract-runner",
            object_id=claim.adapter_contract_run_id,
            trace_id=claim.source_trace_id,
            source_request_id=claim.source_request_id,
            payload={
                "run_id": claim.adapter_contract_run_id,
                "catalog_version": claim.catalog_version,
                "catalog_hash": claim.catalog_hash,
                "suite_kind": claim.suite_kind,
                "mode": claim.mode,
                "total_cases": claim.total_cases,
                "failed_cases": claim.failed_cases,
                "failure_codes": list(claim.failure_codes),
                "completed_at": _db_time(claim.completed_at),
            },
        )
        dispatch_webhook_event(
            config,
            claim.tenant_id,
            event,
            request_id=f"req_{claim.event_id}",
            trace_id=claim.source_trace_id,
            resume_existing_pending=True,
        )
    # Shared 构造器拒绝重建 payload 时只记录固定分类，避免异常携带字段值。
    except ValueError:
        # 构造器异常可能带字段名；source 只保存固定 payload 错误分类。
        error_code = "webhook.payload_invalid"
    # Dispatcher 领域错误不复制 Endpoint 或响应细节，只进入统一可重试分类。
    except ApiError:
        error_code = "webhook.delivery_unavailable"
    # 未知网络或测试桩异常也必须脱敏，source 绝不保存异常类型与正文。
    except Exception:
        # 网络或测试桩异常可能包含 URL/响应正文/Secret，绝不序列化到 source 或日志。
        error_code = "webhook.delivery_unavailable"
    completed = _finish_time(claim, _now(now))
    if error_code is None:
        return _finish_success(config, claim, completed)
    return _finish_failure(config, claim, completed, error_code)


def drain_adapter_contract_event_once(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> bool:
    """领取并处理至多一条 source；空队列返回 False，供调度器和验收复用。"""

    claim = claim_next_adapter_contract_event(config, worker_id=worker_id, now=now)
    if claim is None:
        return False
    dispatch_claimed_adapter_contract_event(config, claim, now=now)
    return True


def _claim_from_row(
    row: sqlite3.Row,
    *,
    token: str,
    owner: str,
    attempts: int,
    lease_expires: datetime,
) -> AdapterContractEventClaim:
    """验证不可变 source/run 联表结果并构造不含正文的 claim。"""

    try:
        raw_codes = json.loads(row["failure_codes_json"])
    # 持久 JSON 损坏不能进入 Webhook 构造器，也不能在异常中回显原始内容。
    except (TypeError, json.JSONDecodeError):
        # 对外只暴露固定存储错误，避免 JSON 解码异常复制持久字段片段。
        raise ApiError("adapter.contract_storage_unavailable") from None
    if (
        row["run_status"] != "failed"
        or not _EVENT_ID.fullmatch(row["event_id"])
        or not _RUN_ID.fullmatch(row["adapter_contract_run_id"])
        or row["event_id"] != f"evt_{row['adapter_contract_run_id']}"
        or type(raw_codes) is not list
        or not raw_codes
        or raw_codes != sorted(set(raw_codes))
        or any(type(code) is not str or code not in ALLOWED_FAILURE_CODES for code in raw_codes)
    ):
        # 联表身份或失败码漂移说明不可变证据已损坏，必须在任何出网动作前失败关闭。
        raise ApiError("adapter.contract_storage_unavailable")
    return AdapterContractEventClaim(
        event_id=row["event_id"],
        tenant_id=row["tenant_id"],
        adapter_contract_run_id=row["adapter_contract_run_id"],
        source_request_id=row["source_request_id"],
        source_trace_id=row["source_trace_id"],
        catalog_version=row["catalog_version"],
        catalog_hash=row["catalog_hash"],
        suite_kind=row["suite_kind"],
        mode=row["mode"],
        total_cases=int(row["total_cases"]),
        failed_cases=int(row["failed_cases"]),
        failure_codes=tuple(raw_codes),
        completed_at=_parse_time(row["completed_at"]),
        claim_token=token,
        lease_owner_id=owner,
        dispatch_attempts=attempts,
        lease_expires_at=lease_expires,
    )


def _finish_success(
    config: AppConfig,
    claim: AdapterContractEventClaim,
    completed_at: datetime,
) -> bool:
    """只在 claim 仍有效时标记 dispatched，旧 worker 失权时返回 False。"""

    timestamp = _db_time(completed_at)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        updated = connection.execute(
            """
            UPDATE adapter_contract_event_outbox
            SET dispatch_status='dispatched',next_attempt_at=NULL,dispatched_at=?,
                claim_token=NULL,lease_owner_id=NULL,lease_expires_at=NULL,
                last_error_code=NULL,updated_at=?
            WHERE event_id=? AND dispatch_status='processing' AND dispatch_attempts=?
              AND claim_token=? AND lease_owner_id=? AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                timestamp,
                timestamp,
                claim.event_id,
                claim.dispatch_attempts,
                claim.claim_token,
                claim.lease_owner_id,
                timestamp,
            ),
        )
        connection.commit()
        return updated.rowcount == 1
    # CAS 写回失败时回滚当前短事务，不能让半提交状态误报为已投递。
    except sqlite3.Error:
        connection.rollback()
        # 底层数据库细节不进入调度日志，只上抛统一存储错误码。
        raise ApiError("adapter.contract_storage_unavailable") from None
    finally:
        connection.close()


def _finish_failure(
    config: AppConfig,
    claim: AdapterContractEventClaim,
    completed_at: datetime,
    error_code: str,
) -> bool:
    """按固定退避回到 pending，或在第十次失败后进入不可逆 failed。"""

    reason = (
        error_code
        if error_code in {"webhook.payload_invalid", "webhook.delivery_unavailable"}
        else "webhook.delivery_unavailable"
    )
    terminal = claim.dispatch_attempts >= EVENT_MAX_ATTEMPTS
    delay = EVENT_BACKOFF_SECONDS[
        min(max(claim.dispatch_attempts - 1, 0), len(EVENT_BACKOFF_SECONDS) - 1)
    ]
    timestamp = _db_time(completed_at)
    next_attempt = _db_time(completed_at + timedelta(seconds=delay))
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        updated = connection.execute(
            """
            UPDATE adapter_contract_event_outbox
            SET dispatch_status=?,next_attempt_at=?,dispatched_at=NULL,
                claim_token=NULL,lease_owner_id=NULL,lease_expires_at=NULL,
                last_error_code=?,updated_at=?
            WHERE event_id=? AND dispatch_status='processing' AND dispatch_attempts=?
              AND claim_token=? AND lease_owner_id=? AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                "failed" if terminal else "pending",
                None if terminal else next_attempt,
                reason,
                timestamp,
                claim.event_id,
                claim.dispatch_attempts,
                claim.claim_token,
                claim.lease_owner_id,
                timestamp,
            ),
        )
        connection.commit()
        return updated.rowcount == 1
    # 失败重试状态写回也必须保持原子性，数据库异常时回滚而不篡改 claim。
    except sqlite3.Error:
        connection.rollback()
        # 统一错误使 scheduler 可安全重试，同时不暴露 SQL 或持久字段。
        raise ApiError("adapter.contract_storage_unavailable") from None
    finally:
        connection.close()


def _finish_time(claim: AdapterContractEventClaim, current: datetime) -> datetime:
    """把 finish 限制在当前 lease 内且不早于领取秒，防止测试时钟回退。"""

    # lease 精确到秒；若外部调用恰好给到到期秒，CAS 应失权而不是倒退到过期前伪提交。
    return max(current, claim.lease_expires_at - timedelta(seconds=EVENT_LEASE_SECONDS))


def _normalize_worker_id(value: object) -> str:
    """限制 worker owner 为短 ASCII 身份，禁止主机路径或自由文本进入数据库。"""

    if type(value) is not str or not _WORKER_ID.fullmatch(value):
        # 非规范 owner 会破坏 lease CAS 身份边界，因此按存储不可用失败关闭。
        raise ApiError("adapter.contract_storage_unavailable")
    return value


def _connect(config: AppConfig) -> sqlite3.Connection:
    """创建 Outbox 短连接，并把部署路径错误折叠为固定 503。"""

    try:
        return connect_database(config.database_url)
    # 数据库配置与路径异常可能包含宿主机信息，转换前先建立固定异常边界。
    except DbError as exc:
        # 仅保留统一领域码供调度器分类，不把数据库 URL 写入公开响应。
        raise ApiError("adapter.contract_storage_unavailable") from exc


def _parse_time(value: object) -> datetime:
    """解析数据库 canonical UTC 秒，损坏事实必须失败关闭。"""

    if type(value) is not str or len(value) != 20 or not value.endswith("Z"):
        # 非规范持久时间无法参与 lease 比较，禁止尝试宽松修复后继续投递。
        raise ApiError("adapter.contract_storage_unavailable")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # 时间解析异常不得携带原始持久值进入日志或 API 错误链。
    except ValueError:
        # 切断底层异常文本并统一映射为存储不可用。
        raise ApiError("adapter.contract_storage_unavailable") from None
    if parsed.tzinfo is None or _db_time(parsed) != value:
        # 非 UTC 或非 canonical 秒可能改变 lease 先后顺序，必须拒绝。
        raise ApiError("adapter.contract_storage_unavailable")
    return parsed.astimezone(UTC)


def _db_time(value: datetime) -> str:
    """把受信时间编码为秒级 UTC，供 lease 比较与事件时间精确绑定。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _now(value: datetime | None) -> datetime:
    """规范化运行或测试注入时钟为秒级 UTC。"""

    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).replace(microsecond=0)


__all__ = [
    "AdapterContractEventClaim",
    "claim_next_adapter_contract_event",
    "dispatch_claimed_adapter_contract_event",
    "drain_adapter_contract_event_once",
]
