"""PRD-073 文件安全 Webhook source outbox 的 lease、CAS 与崩溃恢复。

扫描事务通过 0066 trigger 原子写入 pending intent。本模块在短事务中领取 source，关闭连接后
调用公共 Webhook dispatcher，再用 tenant/event/token/owner/attempt/未过期 lease 完成 CAS。
该状态机提供稳定事件 ID 的 at-least-once 语义，不宣称网络 exactly-once。
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
from metahub_shared.runtime.webhooks import WebhookEvent, build_webhook_event

# 单次 Webhook HTTP 超时上限为 30 秒；每次真实网络尝试前续租，90 秒窗口同时覆盖
# Secret 解密、连接建立、attempt 落库和正常调度抖动，避免合法慢请求被另一实例提前接管。
EVENT_LEASE_SECONDS = 90
EVENT_MAX_ATTEMPTS = 10
EVENT_BACKOFF_SECONDS = (5, 30, 300)
_EVENT_ID = re.compile(r"^fsev_[0-9a-f]{32}$")
_SCAN_ID = re.compile(r"^fscn_[0-9a-f]{32}$")
_FILE_ID = re.compile(r"^file_[0-9a-f]{32}$")
_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


@dataclass(frozen=True, slots=True)
class FileSecurityEventClaim:
    """一次文件安全 source lease 及重建七字段事件所需的不可变快照。"""

    event_id: str
    tenant_id: str
    scan_id: str
    file_id: str
    actor_user_id: str
    source_request_id: str
    source_trace_id: str
    risk_level: str
    finding_count: int
    high_finding_count: int
    model_access_status: str
    detector_version: str
    occurred_at: datetime
    claim_token: str
    lease_owner_id: str
    dispatch_attempts: int
    lease_expires_at: datetime


def claim_next_file_security_event(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> FileSecurityEventClaim | None:
    """原子领取一条到期 source；坏 source 会隔离，第十次孤儿 lease 只终结不再出网。"""

    claim, _progressed = _claim_next_file_security_event_with_progress(
        config,
        worker_id=worker_id,
        now=now,
    )
    return claim


def _claim_next_file_security_event_with_progress(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> tuple[FileSecurityEventClaim | None, bool]:
    """领取可投递 source，并区分“空队列”和“已隔离/回收但无 claim”。

    返回：二元组第一项保持公开领取合同，第二项表示本轮是否已持久化处理进展。
    约束：坏 source 必须先形成一次真实 processing claim，再由同一 owner 完整 CAS 终结；
    SQLite 或连接异常不能被当作确定性 payload 损坏隔离。
    """

    current = _now(now)
    owner = _worker_id(worker_id)
    current_text = _db_time(current)
    progressed = False
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        # 第十次真实外呼若在 source 完成 CAS 前崩溃，租约到期后直接失败，禁止第十一次发送。
        orphan = connection.execute(
            """
            SELECT tenant_id,file_security_event_id,claim_token,lease_owner_id,lease_expires_at
            FROM file_security_event_outbox
            WHERE dispatch_status='processing' AND dispatch_attempts=?
              AND unixepoch(lease_expires_at)<=unixepoch(?)
            ORDER BY lease_expires_at,created_at,file_security_event_id LIMIT 1
            """,
            (EVENT_MAX_ATTEMPTS, current_text),
        ).fetchone()
        if orphan is not None:
            _fail_pending_original_deliveries(
                connection,
                tenant_id=orphan["tenant_id"],
                event_id=orphan["file_security_event_id"],
                updated_at=current_text,
                failure_code="webhook.delivery_unavailable",
            )
            reaped = connection.execute(
                """
                UPDATE file_security_event_outbox
                SET dispatch_status='failed',next_attempt_at=NULL,dispatched_at=NULL,
                    claim_token=NULL,lease_owner_id=NULL,lease_expires_at=NULL,
                    last_error_code='webhook.delivery_unavailable',updated_at=?
                WHERE tenant_id=? AND file_security_event_id=?
                  AND dispatch_status='processing' AND dispatch_attempts=?
                  AND claim_token=? AND lease_owner_id=? AND lease_expires_at=?
                  AND unixepoch(lease_expires_at)<=unixepoch(?)
                """,
                (
                    current_text,
                    orphan["tenant_id"],
                    orphan["file_security_event_id"],
                    EVENT_MAX_ATTEMPTS,
                    orphan["claim_token"],
                    orphan["lease_owner_id"],
                    orphan["lease_expires_at"],
                    current_text,
                ),
            )
            if reaped.rowcount != 1:
                # BEGIN IMMEDIATE 下仍未命中说明持久状态不再满足完整 CAS，整轮必须失败关闭。
                raise ApiError("file.security_scan_unavailable")
            # 孤儿终态与死信先独立提交，后续坏候选不能把已完成的失败关闭一并回滚。
            connection.commit()
            progressed = True
            connection.execute("BEGIN IMMEDIATE")

        while True:
            row = connection.execute(
                """
                SELECT
                  outbox.file_security_event_id AS outbox_event_id,
                  outbox.tenant_id AS outbox_tenant_id,
                  outbox.dispatch_status,outbox.dispatch_attempts,
                  outbox.next_attempt_at,outbox.dispatched_at,outbox.claim_token,
                  outbox.lease_owner_id,outbox.lease_expires_at,
                  outbox.delivery_snapshot_frozen_at,outbox.last_error_code,
                  outbox.created_at AS outbox_created_at,
                  outbox.updated_at AS outbox_updated_at,event.*,
                  scan.status AS scan_status,scan.requested_by AS scan_requested_by,
                  scan.source_request_id AS scan_request_id,
                  scan.source_trace_id AS scan_trace_id,scan.risk_level AS scan_risk_level,
                  scan.finding_count AS scan_finding_count,
                  scan.high_finding_count AS scan_high_finding_count,
                  scan.model_access_status AS scan_model_access_status,
                  scan.detector_version AS scan_detector_version,
                  scan.completed_at AS scan_completed_at,
                  CASE WHEN EXISTS (
                    SELECT 1 FROM file_security_findings finding
                    WHERE finding.tenant_id=event.tenant_id
                      AND finding.scan_id=event.scan_id AND finding.file_id=event.file_id
                      AND finding.detector_code IN (
                        'prompt.visible_instruction','ocr.prompt_instruction'
                      )
                  ) THEN 1 ELSE 0 END AS has_prompt_finding
                FROM file_security_event_outbox outbox
                JOIN file_security_events event
                  ON event.tenant_id=outbox.tenant_id
                 AND event.file_security_event_id=outbox.file_security_event_id
                LEFT JOIN file_security_scans scan
                  ON scan.tenant_id=event.tenant_id AND scan.scan_id=event.scan_id
                 AND scan.file_id=event.file_id
                WHERE outbox.dispatch_status IN ('pending','processing')
                  AND (
                    (outbox.dispatch_status='pending' AND outbox.dispatch_attempts<?
                      AND unixepoch(outbox.next_attempt_at)<=unixepoch(?))
                    OR
                    (outbox.dispatch_status='processing' AND outbox.dispatch_attempts<?
                      AND unixepoch(outbox.lease_expires_at)<=unixepoch(?))
                  )
                ORDER BY CASE outbox.dispatch_status WHEN 'processing' THEN 0 ELSE 1 END,
                         CASE WHEN outbox.dispatch_status='processing'
                           THEN outbox.lease_expires_at ELSE outbox.next_attempt_at END,
                         outbox.created_at,outbox.file_security_event_id
                LIMIT 1
                """,
                (
                    EVENT_MAX_ATTEMPTS,
                    current_text,
                    EVENT_MAX_ATTEMPTS,
                    current_text,
                ),
            ).fetchone()
            if row is None:
                connection.commit()
                return None, progressed
            token = uuid4().hex
            attempts = _strict_int(
                row["dispatch_attempts"],
                0,
                EVENT_MAX_ATTEMPTS - 1,
            ) + 1
            # 秒级状态机要求 claim 时间不早于旧 updated_at，且同秒领取至少推进一秒。
            claim_time = max(
                current,
                _parse_time(row["outbox_updated_at"]) + timedelta(seconds=1),
            )
            lease_expires = claim_time + timedelta(seconds=EVENT_LEASE_SECONDS)
            updated = connection.execute(
                """
                UPDATE file_security_event_outbox
                SET dispatch_status='processing',dispatch_attempts=?,next_attempt_at=NULL,
                    dispatched_at=NULL,claim_token=?,lease_owner_id=?,lease_expires_at=?,
                    last_error_code=NULL,updated_at=?
                WHERE tenant_id=? AND file_security_event_id=? AND dispatch_attempts=?
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
                    row["outbox_tenant_id"],
                    row["outbox_event_id"],
                    attempts - 1,
                    current_text,
                    current_text,
                ),
            )
            if updated.rowcount != 1:
                connection.rollback()
                return None, progressed
            try:
                # 先持有真实 attempt/owner/lease，再验证不可变事实；确定性坏 source 因而能用
                # 与正常完成相同的完整 CAS 终结，不能从 pending 直接跳 failed 或伪造第十次。
                claim = _claim_from_row(
                    row,
                    token=token,
                    owner=owner,
                    attempts=attempts,
                    lease_expires=lease_expires,
                )
            # 交叉校验失败属于持久 source 的确定性损坏；此处已完成真实 claim，才允许进入
            # payload_invalid 的有效 processing-owner CAS 隔离路径。
            except ApiError:
                _quarantine_invalid_source(
                    connection,
                    tenant_id=row["outbox_tenant_id"],
                    event_id=row["outbox_event_id"],
                    claim_token=token,
                    lease_owner_id=owner,
                    dispatch_attempts=attempts,
                    lease_expires_at=_db_time(lease_expires),
                    updated_at=_db_time(claim_time + timedelta(seconds=1)),
                )
                # 每条确定性坏 source 独立提交，后续候选的瞬时数据库错误不能让队首复活。
                connection.commit()
                progressed = True
                connection.execute("BEGIN IMMEDIATE")
                continue
            connection.commit()
            return claim, True
    # 统一领域错误仍需回滚本次 claim，避免 worker 没拿到对象但行已经变成 processing。
    except ApiError:
        _rollback(connection)
        raise
    # SQLite、类型或时间损坏不得把 SQL、路径或持久字段带入 scheduler 日志。
    except (sqlite3.Error, TypeError, ValueError) as exc:
        _rollback(connection)
        raise ApiError("file.security_scan_unavailable") from exc
    finally:
        connection.close()


def dispatch_claimed_file_security_event(
    config: AppConfig,
    claim: FileSecurityEventClaim,
    *,
    now: datetime | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """只由仍持有效 source lease 的 worker 恢复 Webhook intent，并以 CAS 回写结果。

    ``should_stop`` 由 scheduler 停服信号提供；每次真实网络尝试前先检查它并续租，
    因而一个 source 即使包含多个 Endpoint，也不会依赖一次固定 lease 覆盖整个串行投递时长。
    """

    # Claim 返回后线程可能暂停至 lease 过期；出网前必须复核 owner，最终 CAS 本身不足以防迟到发送。
    if not _has_active_source_lease(config, claim, _now(now)):
        return False
    active_lease_expires_at = claim.lease_expires_at

    def preserve_source_lease() -> None:
        """在每次外呼前协作停服并续租；任一失败都禁止继续访问客户 Endpoint。"""

        nonlocal active_lease_expires_at
        if should_stop is not None and should_stop():
            # 停服请求不撤销已提交 source；本轮按固定失败路径释放 lease，留给下次启动恢复。
            raise ApiError("file.security_scan_unavailable")
        active_lease_expires_at = _renew_source_lease(
            config,
            claim,
            expected_lease_expires_at=active_lease_expires_at,
            current=_now(now),
        )

    error_code: str | None = None
    try:
        if should_stop is not None and should_stop():
            # 停服信号若先于本轮处理到达，不应先冻结快照或访问 Endpoint。
            raise ApiError("file.security_scan_unavailable")
        event = build_webhook_event(
            tenant_id=claim.tenant_id,
            event_type="file.prompt_injection_detected",
            event_id=claim.event_id,
            timestamp=claim.occurred_at,
            actor_id=claim.actor_user_id,
            object_id=claim.file_id,
            trace_id=claim.source_trace_id,
            source_request_id=claim.source_request_id,
            payload={
                "scan_id": claim.scan_id,
                "file_id": claim.file_id,
                "risk_level": claim.risk_level,
                "finding_count": claim.finding_count,
                "high_finding_count": claim.high_finding_count,
                "model_access_status": claim.model_access_status,
                "detector_version": claim.detector_version,
            },
        )
        # 首次 claim 在同一 SQLite 事务补齐 legacy 部分 delivery，并冻结包括空集合在内的快照；
        # marker 提交后即使进程退出，后续恢复也不能把新增 Endpoint 追溯到旧 source。
        active_lease_expires_at = _freeze_delivery_snapshot(
            config,
            claim,
            event,
            expected_lease_expires_at=active_lease_expires_at,
            current=_now(now),
        )
        # 延迟导入避免文件服务和 Webhook 服务在应用初始化时形成循环依赖。
        from metahub_control.webhooks.service import dispatch_webhook_event

        dispatch_webhook_event(
            config,
            claim.tenant_id,
            event,
            request_id=f"req_{claim.event_id}",
            trace_id=claim.source_trace_id,
            resume_existing_pending=True,
            source_lease_guard=preserve_source_lease,
            create_snapshot_if_missing=False,
        )
    # Shared exact-key 或信封绑定失败只保存固定 payload 分类，不复制损坏字段。
    except ValueError:
        error_code = "webhook.payload_invalid"
    # Dispatcher 的 Endpoint、网络和持久异常统一折叠，不写 URL、响应正文或 Secret。
    except ApiError:
        error_code = "webhook.delivery_unavailable"
    except Exception:
        # 未知测试桩或第三方异常也可能携带敏感内容，只允许固定领域码进入 outbox。
        error_code = "webhook.delivery_unavailable"
    completed_at = _finish_time(claim, _now(now))
    if error_code is None:
        return _finish_success(
            config,
            claim,
            active_lease_expires_at,
            completed_at,
        )
    return _finish_failure(
        config,
        claim,
        active_lease_expires_at,
        completed_at,
        error_code,
    )


def drain_file_security_event_once(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """领取并处理最多一条 source；空队列返回 False，供 scheduler 与验收器复用。"""

    claim, progressed = _claim_next_file_security_event_with_progress(
        config,
        worker_id=worker_id,
        now=now,
    )
    if claim is None:
        # 只有坏 source 或第十次孤儿时仍属于已处理进展，scheduler 应立即进入下一轮。
        return progressed
    dispatch_claimed_file_security_event(
        config,
        claim,
        now=now,
        should_stop=should_stop,
    )
    return True


def _has_active_source_lease(
    config: AppConfig,
    claim: FileSecurityEventClaim,
    current: datetime,
) -> bool:
    """联网前确认 tenant/event/token/owner/attempt/lease 仍与数据库完全一致。"""

    connection = _connect(config)
    try:
        row = connection.execute(
            """
            SELECT 1 FROM file_security_event_outbox
            WHERE tenant_id=? AND file_security_event_id=?
              AND dispatch_status='processing' AND dispatch_attempts=?
              AND claim_token=? AND lease_owner_id=? AND lease_expires_at=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                claim.tenant_id,
                claim.event_id,
                claim.dispatch_attempts,
                claim.claim_token,
                claim.lease_owner_id,
                _db_time(claim.lease_expires_at),
                _db_time(current),
            ),
        ).fetchone()
        return row is not None
    # 预检数据库不可用时不能降级为盲目 resume，否则非 owner 也可能触发真实网络请求。
    except sqlite3.Error as exc:
        raise ApiError("file.security_scan_unavailable") from exc
    finally:
        connection.close()


def _renew_source_lease(
    config: AppConfig,
    claim: FileSecurityEventClaim,
    *,
    expected_lease_expires_at: datetime,
    current: datetime,
) -> datetime:
    """以完整 source 身份和当前 lease 值 CAS 续租，并返回新到期时间。"""

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT updated_at,lease_expires_at
            FROM file_security_event_outbox
            WHERE tenant_id=? AND file_security_event_id=?
              AND dispatch_status='processing' AND dispatch_attempts=?
              AND claim_token=? AND lease_owner_id=? AND lease_expires_at=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                claim.tenant_id,
                claim.event_id,
                claim.dispatch_attempts,
                claim.claim_token,
                claim.lease_owner_id,
                _db_time(expected_lease_expires_at),
                _db_time(current),
            ),
        ).fetchone()
        if row is None:
            connection.rollback()
            # 任一 owner 字段或当前 lease 不匹配都表示本 worker 已失权，禁止继续续租。
            raise ApiError("file.security_scan_unavailable")
        old_updated = _parse_time(row["updated_at"])
        old_expires = _parse_time(row["lease_expires_at"])
        if old_expires != expected_lease_expires_at:
            # 读取条件和解析结果必须一致；非 canonical 或被替换的旧 lease 不能参与续租。
            connection.rollback()
            # 旧 lease 漂移说明 CAS 前提失效，不能根据数据库新值替调用方静默续租。
            raise ApiError("file.security_scan_unavailable")
        # 同一秒内重复 guard 保持原 lease；时钟推进后才延长，避免测试冻结时钟制造未来时间。
        renewed_at = max(current, old_updated)
        renewed_expires = max(
            old_expires,
            renewed_at + timedelta(seconds=EVENT_LEASE_SECONDS),
        )
        updated = connection.execute(
            """
            UPDATE file_security_event_outbox
            SET lease_expires_at=?,updated_at=?
            WHERE tenant_id=? AND file_security_event_id=?
              AND dispatch_status='processing' AND dispatch_attempts=?
              AND claim_token=? AND lease_owner_id=? AND lease_expires_at=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                _db_time(renewed_expires),
                _db_time(renewed_at),
                claim.tenant_id,
                claim.event_id,
                claim.dispatch_attempts,
                claim.claim_token,
                claim.lease_owner_id,
                _db_time(old_expires),
                _db_time(current),
            ),
        )
        if updated.rowcount != 1:
            connection.rollback()
            # 并发改写导致 CAS 未命中时必须零网络，不能把续租失败当作可忽略竞态。
            raise ApiError("file.security_scan_unavailable")
        connection.commit()
        return renewed_expires
    # 领域异常已经完成脱敏分类，但当前写事务仍必须显式回滚。
    except ApiError:
        _rollback(connection)
        raise
    # 续租失败不能退化为无 lease 外呼；异常路径只暴露固定文件安全领域码。
    except (sqlite3.Error, TypeError, ValueError) as exc:
        _rollback(connection)
        raise ApiError("file.security_scan_unavailable") from exc
    finally:
        connection.close()


def _freeze_delivery_snapshot(
    config: AppConfig,
    claim: FileSecurityEventClaim,
    event: WebhookEvent,
    *,
    expected_lease_expires_at: datetime,
    current: datetime,
) -> datetime:
    """原子补齐 Webhook delivery intent、冻结快照 marker，并为随后的发送续租。"""

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT updated_at,lease_expires_at,delivery_snapshot_frozen_at
            FROM file_security_event_outbox
            WHERE tenant_id=? AND file_security_event_id=?
              AND dispatch_status='processing' AND dispatch_attempts=?
              AND claim_token=? AND lease_owner_id=? AND lease_expires_at=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                claim.tenant_id,
                claim.event_id,
                claim.dispatch_attempts,
                claim.claim_token,
                claim.lease_owner_id,
                _db_time(expected_lease_expires_at),
                _db_time(current),
            ),
        ).fetchone()
        if row is None:
            connection.rollback()
            # 快照冻结只允许当前完整 lease owner 执行，失权后不能读取或补齐订阅者。
            raise ApiError("file.security_scan_unavailable")
        marker = row["delivery_snapshot_frozen_at"]
        if marker is not None:
            # 非空 marker 只能由同一事务中的生产快照 helper 写入；严格解析可阻断手工坏值。
            _parse_time(marker)
            connection.commit()
            return expected_lease_expires_at
        old_updated = _parse_time(row["updated_at"])
        old_expires = _parse_time(row["lease_expires_at"])
        if old_expires != expected_lease_expires_at:
            connection.rollback()
            # 调用方持有的旧 lease 与库内不一致时，整个 delivery 快照事务必须失败关闭。
            raise ApiError("file.security_scan_unavailable")
        frozen_at = max(current, old_updated)
        renewed_expires = max(
            old_expires,
            frozen_at + timedelta(seconds=EVENT_LEASE_SECONDS),
        )
        # 延迟导入让 Webhook 继续拥有 Endpoint 过滤、canonical JSON 和 delivery 约束的唯一实现。
        from metahub_control.webhooks.service import freeze_webhook_event_delivery_snapshot

        freeze_webhook_event_delivery_snapshot(
            connection,
            event,
            frozen_at=_db_time(frozen_at),
        )
        updated = connection.execute(
            """
            UPDATE file_security_event_outbox
            SET delivery_snapshot_frozen_at=?,lease_expires_at=?,updated_at=?
            WHERE tenant_id=? AND file_security_event_id=?
              AND dispatch_status='processing' AND dispatch_attempts=?
              AND claim_token=? AND lease_owner_id=? AND lease_expires_at=?
              AND delivery_snapshot_frozen_at IS NULL
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                _db_time(frozen_at),
                _db_time(renewed_expires),
                _db_time(frozen_at),
                claim.tenant_id,
                claim.event_id,
                claim.dispatch_attempts,
                claim.claim_token,
                claim.lease_owner_id,
                _db_time(old_expires),
                _db_time(current),
            ),
        )
        if updated.rowcount != 1:
            connection.rollback()
            # marker CAS 失败必须连同已补齐的 delivery 一起回滚，禁止留下部分冻结事实。
            raise ApiError("file.security_scan_unavailable")
        connection.commit()
        return renewed_expires
    # 生产快照 helper 的领域拒绝同样属于当前事务，必须先回滚再向 worker 传播。
    except ApiError:
        _rollback(connection)
        raise
    # 快照构造与 marker 必须全成或全败；数据库和类型异常统一脱敏。
    except (sqlite3.Error, TypeError, ValueError) as exc:
        _rollback(connection)
        raise ApiError("file.security_scan_unavailable") from exc
    finally:
        connection.close()


def _finish_success(
    config: AppConfig,
    claim: FileSecurityEventClaim,
    expected_lease_expires_at: datetime,
    completed_at: datetime,
) -> bool:
    """仅在同 tenant claim 仍有效且 lease 未过期时标记 dispatched。"""

    timestamp = _db_time(completed_at)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        updated = connection.execute(
            """
            UPDATE file_security_event_outbox
            SET dispatch_status='dispatched',next_attempt_at=NULL,dispatched_at=?,
                claim_token=NULL,lease_owner_id=NULL,lease_expires_at=NULL,
                last_error_code=NULL,updated_at=?
            WHERE tenant_id=? AND file_security_event_id=?
              AND dispatch_status='processing' AND dispatch_attempts=?
              AND claim_token=? AND lease_owner_id=? AND lease_expires_at=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                timestamp,
                timestamp,
                claim.tenant_id,
                claim.event_id,
                claim.dispatch_attempts,
                claim.claim_token,
                claim.lease_owner_id,
                _db_time(expected_lease_expires_at),
                timestamp,
            ),
        )
        if updated.rowcount != 1:
            connection.rollback()
            return False
        connection.commit()
        return True
    # 成功 CAS 失败必须回滚，不能把未确认状态伪报成 dispatched。
    except sqlite3.Error as exc:
        _rollback(connection)
        raise ApiError("file.security_scan_unavailable") from exc
    finally:
        connection.close()


def _finish_failure(
    config: AppConfig,
    claim: FileSecurityEventClaim,
    expected_lease_expires_at: datetime,
    completed_at: datetime,
    error_code: str,
) -> bool:
    """按固定 5/30/300 秒退避，或在第十次失败后进入不可逆 failed。"""

    allowed = {"webhook.payload_invalid", "webhook.delivery_unavailable"}
    reason = error_code if error_code in allowed else "webhook.delivery_unavailable"
    # payload 合同损坏是确定性失败，重试不会恢复；必须按真实 attempt 立即终结。
    terminal = reason == "webhook.payload_invalid" or (
        claim.dispatch_attempts >= EVENT_MAX_ATTEMPTS
    )
    delay = EVENT_BACKOFF_SECONDS[
        min(max(claim.dispatch_attempts - 1, 0), len(EVENT_BACKOFF_SECONDS) - 1)
    ]
    timestamp = _db_time(completed_at)
    next_attempt = _db_time(completed_at + timedelta(seconds=delay))
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if terminal:
            # 先终结 delivery；后续 source CAS 未命中会回滚整个事务，不留下伪终态死信。
            _fail_pending_original_deliveries(
                connection,
                tenant_id=claim.tenant_id,
                event_id=claim.event_id,
                updated_at=timestamp,
                failure_code=reason,
            )
        updated = connection.execute(
            """
            UPDATE file_security_event_outbox
            SET dispatch_status=?,next_attempt_at=?,dispatched_at=NULL,
                claim_token=NULL,lease_owner_id=NULL,lease_expires_at=NULL,
                last_error_code=?,updated_at=?
            WHERE tenant_id=? AND file_security_event_id=?
              AND dispatch_status='processing' AND dispatch_attempts=?
              AND claim_token=? AND lease_owner_id=? AND lease_expires_at=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                "failed" if terminal else "pending",
                None if terminal else next_attempt,
                reason,
                timestamp,
                claim.tenant_id,
                claim.event_id,
                claim.dispatch_attempts,
                claim.claim_token,
                claim.lease_owner_id,
                _db_time(expected_lease_expires_at),
                timestamp,
            ),
        )
        if updated.rowcount != 1:
            connection.rollback()
            return False
        connection.commit()
        return True
    # Webhook 终态 helper 的固定领域拒绝必须连同 source CAS 一起回滚。
    except ApiError:
        _rollback(connection)
        raise
    # 失败状态同样受完整 CAS 保护；数据库错误不得留下部分清理的 lease 字段。
    except sqlite3.Error as exc:
        _rollback(connection)
        raise ApiError("file.security_scan_unavailable") from exc
    finally:
        connection.close()


def _quarantine_invalid_source(
    connection: sqlite3.Connection,
    *,
    tenant_id: object,
    event_id: object,
    claim_token: str,
    lease_owner_id: str,
    dispatch_attempts: int,
    lease_expires_at: str,
    updated_at: str,
) -> None:
    """由当前有效 processing owner 原子隔离坏 source，并释放后续队列。

    参数：source 身份、真实 claim token/owner/attempt/lease 和同一事务终态时间。
    返回：无；任一 CAS 漂移都会抛出固定领域错误并由调用方整体回滚。
    """

    _fail_pending_original_deliveries(
        connection,
        tenant_id=tenant_id,
        event_id=event_id,
        updated_at=updated_at,
        failure_code="webhook.payload_invalid",
    )
    quarantined = connection.execute(
        """
        UPDATE file_security_event_outbox SET dispatch_status='failed',next_attempt_at=NULL,
            dispatched_at=NULL,claim_token=NULL,lease_owner_id=NULL,lease_expires_at=NULL,
            last_error_code='webhook.payload_invalid',updated_at=?
        WHERE tenant_id=? AND file_security_event_id=?
          AND dispatch_status='processing' AND dispatch_attempts=?
          AND claim_token=? AND lease_owner_id=? AND lease_expires_at=?
          AND unixepoch(lease_expires_at)>unixepoch(?)
        """,
        (
            updated_at,
            tenant_id,
            event_id,
            dispatch_attempts,
            claim_token,
            lease_owner_id,
            lease_expires_at,
            updated_at,
        ),
    )
    if quarantined.rowcount != 1:
        # 隔离也是完整状态 CAS；未命中时不能跳过该行并假装队列健康。
        raise ApiError("file.security_scan_unavailable")


def _fail_pending_original_deliveries(
    connection: sqlite3.Connection,
    *,
    tenant_id: object,
    event_id: object,
    updated_at: str,
    failure_code: str,
) -> None:
    """复用 Webhook 所有权模块，在当前 source 事务中终结剩余 delivery。"""

    # 延迟导入避免文件安全与 Webhook 服务初始化形成循环；helper 不提交调用方事务。
    from metahub_control.webhooks.service import fail_pending_webhook_event_deliveries

    fail_pending_webhook_event_deliveries(
        connection,
        tenant_id=tenant_id,
        event_id=event_id,
        updated_at=updated_at,
        failure_code=failure_code,
    )


def _claim_from_row(
    row: sqlite3.Row,
    *,
    token: str,
    owner: str,
    attempts: int,
    lease_expires: datetime,
) -> FileSecurityEventClaim:
    """逐字段复核 outbox、不可变事件、blocked Scan 和 Prompt finding 后构造 claim。"""

    event_id = _matched_id(row["file_security_event_id"], _EVENT_ID)
    scan_id = _matched_id(row["scan_id"], _SCAN_ID)
    file_id = _matched_id(row["file_id"], _FILE_ID)
    tenant_id = _bounded_text(row["tenant_id"], maximum=128)
    actor_id = _bounded_text(row["actor_user_id"], maximum=128)
    request_id = _bounded_text(row["source_request_id"], maximum=128)
    trace_id = _bounded_text(row["source_trace_id"], maximum=128)
    finding_count = _strict_int(row["finding_count"], 1, 256)
    high_count = _strict_int(row["high_finding_count"], 1, finding_count)
    occurred_at = _parse_time(row["occurred_at"])
    if (
        row["outbox_event_id"] != event_id
        or row["outbox_tenant_id"] != tenant_id
        or row["outbox_created_at"] != row["occurred_at"]
        or row["event_type"] != "file.prompt_injection_detected"
        or row["risk_level"] not in {"high", "critical"}
        or row["model_access_status"] != "blocked"
        or row["detector_version"] != "file-security-detector-v1"
        or row["scan_status"] != "blocked"
        or row["scan_requested_by"] != actor_id
        or row["scan_request_id"] != request_id
        or row["scan_trace_id"] != trace_id
        or row["scan_risk_level"] != row["risk_level"]
        or row["scan_finding_count"] != finding_count
        or row["scan_high_finding_count"] != high_count
        or row["scan_model_access_status"] != row["model_access_status"]
        or row["scan_detector_version"] != row["detector_version"]
        or row["scan_completed_at"] != row["occurred_at"]
        or row["has_prompt_finding"] != 1
    ):
        # 任一 owner 或摘要漂移都说明持久事实不再能为签名事件背书，必须在网络前失败关闭。
        raise ApiError("file.security_scan_unavailable")
    try:
        # 复用 Shared exact-key 和信封校验，避免 worker 维护一套更宽松的私有合同。
        build_webhook_event(
            tenant_id=tenant_id,
            event_type="file.prompt_injection_detected",
            event_id=event_id,
            timestamp=occurred_at,
            actor_id=actor_id,
            object_id=file_id,
            trace_id=trace_id,
            source_request_id=request_id,
            payload={
                "scan_id": scan_id,
                "file_id": file_id,
                "risk_level": row["risk_level"],
                "finding_count": finding_count,
                "high_finding_count": high_count,
                "model_access_status": row["model_access_status"],
                "detector_version": row["detector_version"],
            },
        )
    # 构造器错误不得把字段值带入 scheduler 日志，统一折叠为扫描依赖不可用。
    except ValueError:
        raise ApiError("file.security_scan_unavailable") from None
    return FileSecurityEventClaim(
        event_id=event_id,
        tenant_id=tenant_id,
        scan_id=scan_id,
        file_id=file_id,
        actor_user_id=actor_id,
        source_request_id=request_id,
        source_trace_id=trace_id,
        risk_level=row["risk_level"],
        finding_count=finding_count,
        high_finding_count=high_count,
        model_access_status=row["model_access_status"],
        detector_version=row["detector_version"],
        occurred_at=occurred_at,
        claim_token=token,
        lease_owner_id=owner,
        dispatch_attempts=attempts,
        lease_expires_at=lease_expires,
    )


def _finish_time(claim: FileSecurityEventClaim, current: datetime) -> datetime:
    """生成不早于领取秒的完成时间，并拒绝会绕过 lease 的明显时钟回退。"""

    claimed_at = claim.lease_expires_at - timedelta(seconds=EVENT_LEASE_SECONDS)
    if current < claimed_at - timedelta(seconds=1):
        # 明显倒退的时钟会让过期 worker 伪装成仍持 lease，必须失败关闭。
        raise ApiError("file.security_scan_unavailable")
    return claimed_at + timedelta(seconds=1) if current <= claimed_at else current


def _matched_id(value: object, pattern: re.Pattern[str]) -> str:
    """返回符合固定小写十六进制形状的持久 ID，坏值统一失败关闭。"""

    if type(value) is not str or pattern.fullmatch(value) is None:
        # source 身份将进入签名信封，形状漂移时禁止继续构造或出网。
        raise ApiError("file.security_scan_unavailable")
    return value


def _bounded_text(value: object, *, maximum: int) -> str:
    """校验 tenant/actor/链路标识为无空白控制符的有界文本。"""

    if (
        type(value) is not str
        or not 1 <= len(value) <= maximum
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        # tenant、actor 和链路字段不能携带控制符或无界正文进入日志与 Webhook。
        raise ApiError("file.security_scan_unavailable")
    return value


def _strict_int(value: object, minimum: int, maximum: int) -> int:
    """拒绝 bool/浮点类型混淆，并返回指定闭区间内的 SQLite 整数。"""

    if type(value) is not int or not minimum <= value <= maximum:
        # 计数直接参与 exact payload；类型或范围不可信时必须在网络前终止。
        raise ApiError("file.security_scan_unavailable")
    return value


def _worker_id(value: object) -> str:
    """限制 lease owner 为短 ASCII 运行身份，禁止主机路径或自由正文进入数据库。"""

    if type(value) is not str or _WORKER_ID.fullmatch(value) is None:
        # owner 会持久化到 outbox，拒绝路径、空白或自由异常文本污染恢复状态。
        raise ApiError("file.security_scan_unavailable")
    return value


def _connect(config: AppConfig) -> sqlite3.Connection:
    """创建 source-outbox 短连接，并把数据库 URL 错误折叠为固定领域码。"""

    try:
        return connect_database(config.database_url)
    # 配置错误可能包含真实数据库路径，转换边界只能保留固定领域码。
    except DbError as exc:
        # 数据库路径可能包含宿主机信息，只保留统一错误供 scheduler 分类。
        raise ApiError("file.security_scan_unavailable") from exc


def _parse_time(value: object) -> datetime:
    """严格解析 canonical UTC 秒；非规范持久时间不得参与 lease 或事件签名。"""

    if type(value) is not str or len(value) != 20 or not value.endswith("Z"):
        # 非 canonical 时间会破坏 lease 比较和签名稳定性，不能容错改写。
        raise ApiError("file.security_scan_unavailable")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # 解析异常可能回显损坏字段，切断异常链并统一分类。
    except ValueError:
        # 解析异常可能复制坏值，切断异常链后只暴露固定领域码。
        raise ApiError("file.security_scan_unavailable") from None
    if parsed.tzinfo is None or _db_time(parsed) != value:
        # 可解析但非规范的时间同样可能改变事件身份，必须失败关闭。
        raise ApiError("file.security_scan_unavailable")
    return parsed.astimezone(UTC)


def _db_time(value: datetime) -> str:
    """把可信时间编码为 SQLite 状态机使用的秒级 UTC 字符串。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _now(value: datetime | None) -> datetime:
    """规范化运行时或测试注入时钟为带时区的 UTC 秒。"""

    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).replace(microsecond=0)


def _rollback(connection: sqlite3.Connection) -> None:
    """在异常边界幂等回滚当前短事务。"""

    try:
        connection.rollback()
    # 回滚异常可能包含路径或 SQL；原始领域错误必须保持为唯一公开原因。
    except sqlite3.Error:
        # 原异常已经统一分类，回滚失败不能用第二个含路径异常覆盖它。
        return


__all__ = [
    "FileSecurityEventClaim",
    "claim_next_file_security_event",
    "dispatch_claimed_file_security_event",
    "drain_file_security_event_once",
]
