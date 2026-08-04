"""输出安全规则快照与扫描证据持久化服务。

Provider 调用前必须先提交 pending owner，再从同租户 SQL 快照 output/both 规则。终结只允许
pending 到一个合法终态；blocked source event 与 scan 终态共享事务。任何路径都不保存或记录
模型正文、命中片段、DLP pattern、Provider Key、Token 或数据库异常文本。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.output_safety import OutputRule, OutputRuleSnapshot
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.safe_regex import UnsafeRegexError, compile_safe_dlp_regex
from metahub_shared.runtime.webhooks import WebhookEvent, build_webhook_event

from .models import (
    DEFAULT_INTERVAL_TOKENS,
    DEFAULT_WINDOW_TOKENS,
    DETECTOR_VERSION,
    OutputSafetyEventRecord,
    OutputSafetyFinalization,
    OutputSafetyStoreError,
    OutputSafetyTerminalState,
    PreparedOutputSafetyScan,
)

logger = logging.getLogger("metahub.gateway.output_safety")

# 规则数上限与既有输入 DLP 热路径一致；多一行探测用于拒绝超限，而不是静默漏扫后伪报 clean。
MAX_OUTPUT_RULES_PER_REQUEST = 100

# failure_reason_code 只能来自固定目录，阻止调用方把底层异常或模型正文当成 reason 落库。
ALLOWED_FAILURE_REASON_CODES = frozenset(
    {
        "output_safety.fragment_limit_exceeded",
        "output_safety.response_shape_invalid",
        "output_safety.response_too_large",
        "output_safety.evidence_unavailable",
        "output_safety.rule_invalid",
        "output_safety.rule_snapshot_unavailable",
        "output_safety.sse_event_incomplete",
        "output_safety.sse_event_too_large",
        "output_safety.sse_json_duplicate_key",
        "output_safety.sse_json_invalid",
        "output_safety.sse_utf8_invalid",
        "output_safety.stream_incomplete",
        "output_safety.window_capacity_exceeded",
    }
)

# 幂等比较字段顺序与 UPDATE 完全一致；completed_at 单独复用首次提交值，不能被重放时钟改写。
_TERMINAL_COLUMNS = (
    "scan_status",
    "decision",
    "action",
    "dlp_rule_id",
    "rule_type",
    "match_count",
    "ruleset_fingerprint",
    "checkpoint_count",
    "observed_output_token_count",
    "scanned_token_count",
    "emitted_token_count",
    "emitted_sse_event_count",
    "emitted_bytes",
    "exposure_state",
    "billing_disposition",
    "failure_reason_code",
    "completed_at",
)


def prepare_output_safety_scan(
    config: AppConfig,
    *,
    tenant_id: str,
    request_id: str,
    trace_id: str,
    scan_mode: str,
    stream_session_id: str | None = None,
    interval_tokens: int | None = None,
    window_tokens: int | None = None,
) -> PreparedOutputSafetyScan:
    """提交 pending owner 并冻结当前租户的输出方向规则。

    参数：鉴权后 tenant/request/trace owner、扫描模式和可选 StreamSession/窗口参数。
    返回：带短期内存规则快照的扫描句柄；零规则时返回已终结的 not_configured 句柄。
    关键约束：pending commit 先于规则读取；任何失败都使用固定 503 且 Provider 调用数必须仍为零。
    """

    normalized = _normalize_prepare_contract(
        tenant_id=tenant_id,
        request_id=request_id,
        trace_id=trace_id,
        scan_mode=scan_mode,
        stream_session_id=stream_session_id,
        interval_tokens=interval_tokens,
        window_tokens=window_tokens,
    )
    scan_id = f"oss_{uuid4().hex}"
    started_at = _to_db_time(_utcnow())
    try:
        _insert_pending_scan(config, scan_id=scan_id, started_at=started_at, **normalized)
    # pending 是 Provider 前强制审计门，失败时绝不能继续读取正文或访问上游网络。
    except (DbError, sqlite3.Error, OutputSafetyStoreError) as exc:
        _log_store_failure(normalized, scan_id, "pending_create_failed", exc)
        # 对外只返回统一固定错误，数据库路径和驱动异常文本不得进入响应或异常链。
        raise ApiError("gateway.output_safety_unavailable") from None

    try:
        snapshot = _load_rule_snapshot(config, normalized["tenant_id"])
    # 规则行、正则或数据库任一损坏都使完整扫描不可证明，必须在调用 Provider 前失败关闭。
    except (DbError, sqlite3.Error, UnsafeRegexError, OutputSafetyStoreError, ValueError) as exc:
        # pending 已独立提交；快照损坏必须尽力推进 unavailable，而不能回滚掉“开始过”的证据。
        _finalize_snapshot_failure(
            config,
            scan_id=scan_id,
            normalized=normalized,
            failure_reason_code="output_safety.rule_snapshot_unavailable",
        )
        _log_store_failure(normalized, scan_id, "snapshot_failed", exc)
        # 即使 unavailable 证据二次写入失败，也只能暴露固定安全错误而不是底层异常内容。
        raise ApiError("gateway.output_safety_unavailable") from None

    prepared = PreparedOutputSafetyScan(
        output_safety_scan_id=scan_id,
        tenant_id=normalized["tenant_id"],
        request_id=normalized["request_id"],
        trace_id=normalized["trace_id"],
        stream_session_id=normalized["stream_session_id"],
        scan_mode=normalized["scan_mode"],  # type: ignore[arg-type]
        scan_status="pending",
        interval_tokens=normalized["interval_tokens"],
        window_tokens=normalized["window_tokens"],
        snapshot=snapshot,
    )
    if snapshot.rules:
        return prepared

    # 空规则也必须持久化明确事实；该终态在 Provider 前完成，不启用 SSE event gate。
    terminal = OutputSafetyTerminalState(scan_status="not_configured", decision="none")
    finalize_output_safety_scan(config, prepared, terminal)
    return replace(prepared, scan_status="not_configured")


def finalize_output_safety_scan(
    config: AppConfig,
    scan: PreparedOutputSafetyScan,
    terminal: OutputSafetyTerminalState,
) -> OutputSafetyFinalization:
    """在独立短事务中终结 OutputSafetyScan，并在阻断时写 source event。

    参数：prepare 返回的可信句柄和不含正文的完整终态。
    返回：终态、可空 source event 以及是否为完全相同的幂等重放。
    关键约束：不同的第二终态固定失败关闭；本函数不写 Usage、charge、dispute 或 Webhook outbox。
    """

    connection: sqlite3.Connection | None = None
    try:
        connection = connect_database(config.database_url)
        connection.execute("BEGIN IMMEDIATE")
        result = finalize_output_safety_scan_in_transaction(connection, scan, terminal)
        connection.commit()
        return result
    # 终态或 source event 任一写入失败都必须整体回滚，不能留下半个安全事实。
    except (DbError, sqlite3.Error, OutputSafetyStoreError, ValueError) as exc:
        _rollback_safely(connection)
        _log_store_failure(_scan_owner(scan), scan.output_safety_scan_id, "finalize_failed", exc)
        # 公开边界仅保留统一错误码，SQLite 约束名和规则元数据不得拼进异常链。
        raise ApiError("gateway.output_safety_unavailable") from None
    finally:
        _close_safely(connection)


def finalize_output_safety_scan_in_transaction(
    connection: sqlite3.Connection,
    scan: PreparedOutputSafetyScan,
    terminal: OutputSafetyTerminalState,
    *,
    completed_at: datetime | None = None,
) -> OutputSafetyFinalization:
    """在调用方现有事务中终结 scan，供 StreamSession 实现原子收口。

    参数：已经开启事务且启用外键的连接、可信 scan 和完整终态；可选统一完成时刻。
    返回：不含正文的终结结果。
    关键约束：本函数绝不 commit/rollback；调用方必须把 StreamSession 更新、scan 和事件作为一个事务处理。
    """

    if scan.scan_status != "pending":
        # not_configured 已在 Provider 前终结，之后再进入 finalizer 是调用方状态机错误。
        raise OutputSafetyStoreError("output_safety.scan_not_pending")
    if (
        scan.snapshot.tenant_id != scan.tenant_id
        or scan.snapshot.detector_version != DETECTOR_VERSION
    ):
        # 快照 owner/版本必须与 pending 句柄一致，禁止调用方拼接另一租户的内存规则集。
        raise OutputSafetyStoreError("output_safety.snapshot_owner_mismatch")
    _validate_terminal(scan, terminal)
    completed_at_text = _to_db_time(completed_at or _utcnow())
    fingerprint = scan.snapshot.ruleset_fingerprint
    values = _terminal_values(terminal, fingerprint, completed_at_text)
    cursor = connection.execute(
        """
        UPDATE output_safety_scans
        SET scan_status=?,decision=?,action=?,dlp_rule_id=?,rule_type=?,match_count=?,
            ruleset_fingerprint=?,checkpoint_count=?,observed_output_token_count=?,
            scanned_token_count=?,emitted_token_count=?,emitted_sse_event_count=?,
            emitted_bytes=?,exposure_state=?,billing_disposition=?,failure_reason_code=?,
            completed_at=?
        WHERE tenant_id=? AND output_safety_scan_id=? AND request_id=? AND trace_id=?
          AND scan_status='pending'
        """,
        values
        + (
            scan.tenant_id,
            scan.output_safety_scan_id,
            scan.request_id,
            scan.trace_id,
        ),
    )
    idempotent_replay = False
    if cursor.rowcount != 1:
        row = connection.execute(
            f"""
            SELECT {','.join(_TERMINAL_COLUMNS)}
            FROM output_safety_scans
            WHERE tenant_id=? AND output_safety_scan_id=? AND request_id=? AND trace_id=?
            """,
            (
                scan.tenant_id,
                scan.output_safety_scan_id,
                scan.request_id,
                scan.trace_id,
            ),
        ).fetchone()
        if row is None or tuple(row[column] for column in _TERMINAL_COLUMNS[:-1]) != values[:-1]:
            # 串租户、owner 错配和不同第二终态使用同一固定完整性错误，不输出数据库行内容。
            raise OutputSafetyStoreError("output_safety.terminal_conflict")
        # completed_at 是首次成功终结时由服务端生成的事实；幂等重放必须复用旧值，
        # 不能要求调用方记住内部时钟，也不能用新时刻改写 source event。
        completed_at_text = str(row["completed_at"])
        idempotent_replay = True

    source_event = None
    if terminal.decision == "blocked":
        source_event = _ensure_blocked_event(
            connection,
            scan,
            terminal,
            occurred_at=completed_at_text,
        )
    return OutputSafetyFinalization(
        scan=scan,
        terminal=terminal,
        source_event=source_event,
        idempotent_replay=idempotent_replay,
    )


def output_safety_event_to_webhook(event: OutputSafetyEventRecord) -> WebhookEvent:
    """把已提交 blocked source event 转换成统一 Webhook 信封。

    参数：从同事务 ``output_safety_events`` 返回或读取的严格记录。
    返回：已通过 Shared exact payload/envelope 校验的 ``WebhookEvent``。
    关键约束：本函数不入队；调用方必须先确认 Usage durability 为 committed/spooled，lost 时禁止发布。
    """

    occurred_at = _parse_db_time(event.occurred_at)
    return build_webhook_event(
        tenant_id=event.tenant_id,
        event_type=event.event_type,
        event_id=event.event_id,
        timestamp=occurred_at,
        actor_id=None,
        object_id=event.output_safety_scan_id,
        trace_id=event.trace_id,
        source_request_id=event.request_id,
        payload={
            "schema_version": event.schema_version,
            "output_safety_scan_id": event.output_safety_scan_id,
            "request_id": event.request_id,
            "scan_mode": event.scan_mode,
            "scan_status": event.scan_status,
            "decision": event.decision,
            "error_code": event.error_code,
            "exposure_state": event.exposure_state,
            "emitted_token_count": event.emitted_token_count,
            "checkpoint_count": event.checkpoint_count,
            "dlp_rule_id": event.dlp_rule_id,
            "detector_version": event.detector_version,
            "billing_disposition": event.billing_disposition,
            "occurred_at": event.occurred_at,
        },
    )


def _insert_pending_scan(
    config: AppConfig,
    *,
    scan_id: str,
    started_at: str,
    tenant_id: str,
    request_id: str,
    trace_id: str,
    scan_mode: str,
    stream_session_id: str | None,
    interval_tokens: int | None,
    window_tokens: int | None,
) -> None:
    """在独立事务中建立 pending owner，且提交后才允许读取规则或调用 Provider。"""

    connection: sqlite3.Connection | None = None
    try:
        connection = connect_database(config.database_url)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO output_safety_scans(
              output_safety_scan_id,tenant_id,request_id,trace_id,stream_session_id,
              scan_mode,scan_status,decision,action,dlp_rule_id,rule_type,match_count,
              ruleset_fingerprint,detector_version,interval_tokens,window_tokens,
              checkpoint_count,observed_output_token_count,scanned_token_count,
              emitted_token_count,emitted_sse_event_count,emitted_bytes,exposure_state,
              billing_disposition,failure_reason_code,started_at,completed_at
            ) VALUES(?,?,?,?,?,?,'pending','none',NULL,NULL,NULL,0,NULL,?,?,?,
                     0,0,0,0,0,0,'unknown','not_required',NULL,?,NULL)
            """,
            (
                scan_id,
                tenant_id,
                request_id,
                trace_id,
                stream_session_id,
                scan_mode,
                DETECTOR_VERSION,
                interval_tokens,
                window_tokens,
                started_at,
            ),
        )
        connection.commit()
    # pending INSERT 失败必须在当前短事务内回滚，禁止无 owner 的 Provider 流量继续执行。
    except (DbError, sqlite3.Error):
        _rollback_safely(connection)
        raise
    finally:
        _close_safely(connection)


def _load_rule_snapshot(config: AppConfig, tenant_id: str) -> OutputRuleSnapshot:
    """按 tenant SQL 过滤并冻结 active output/both 规则，不把 pattern 带出当前请求。"""

    connection: sqlite3.Connection | None = None
    try:
        connection = connect_database(config.database_url)
        rows = connection.execute(
            """
            SELECT dlp_rule_id,rule_type,pattern,pattern_hash,action,priority,
                   scan_scope,updated_at
            FROM dlp_rules
            WHERE tenant_id=? AND status='active'
              AND scan_scope IN ('output','both')
            ORDER BY priority ASC, updated_at DESC, dlp_rule_id ASC
            LIMIT ?
            """,
            (tenant_id, MAX_OUTPUT_RULES_PER_REQUEST + 1),
        ).fetchall()
    finally:
        _close_safely(connection)
    if len(rows) > MAX_OUTPUT_RULES_PER_REQUEST:
        # 不能静默截到 100 条后宣称 clean；规则治理超限时整个请求失败关闭。
        raise OutputSafetyStoreError("output_safety.rule_limit_exceeded")

    rules: list[OutputRule] = []
    canonical_rules: list[dict[str, object]] = []
    for row in rows:
        rule_id, rule_type, pattern, pattern_hash, action, priority, scope, updated_at = (
            row["dlp_rule_id"],
            row["rule_type"],
            row["pattern"],
            row["pattern_hash"],
            row["action"],
            row["priority"],
            row["scan_scope"],
            row["updated_at"],
        )
        _validate_rule_row(
            rule_id=rule_id,
            rule_type=rule_type,
            pattern=pattern,
            pattern_hash=pattern_hash,
            action=action,
            priority=priority,
            scope=scope,
            updated_at=updated_at,
        )
        compiled = compile_safe_dlp_regex(pattern).compiled if rule_type == "regex" else None
        rules.append(
            OutputRule(
                dlp_rule_id=rule_id,
                rule_type=rule_type,
                action=action,
                priority=priority,
                pattern=pattern,
                compiled_regex=compiled,
            )
        )
        canonical_rules.append(
            {
                "action": action,
                "dlp_rule_id": rule_id,
                "pattern_hash": pattern_hash,
                "priority": priority,
                "scan_scope": scope,
                "updated_at": updated_at,
            }
        )
    return OutputRuleSnapshot(
        tenant_id=tenant_id,
        ruleset_fingerprint=_ruleset_fingerprint(canonical_rules),
        detector_version=DETECTOR_VERSION,
        rules=tuple(rules),
    )


def _finalize_snapshot_failure(
    config: AppConfig,
    *,
    scan_id: str,
    normalized: dict[str, Any],
    failure_reason_code: str,
) -> None:
    """快照失败后尽力把已提交 pending 推进 unavailable，且 fingerprint 保持 NULL。"""

    connection: sqlite3.Connection | None = None
    try:
        connection = connect_database(config.database_url)
        connection.execute("BEGIN IMMEDIATE")
        completed_at = _to_db_time(_utcnow())
        cursor = connection.execute(
            """
            UPDATE output_safety_scans
            SET scan_status='unavailable',decision='unknown',action=NULL,dlp_rule_id=NULL,
                rule_type=NULL,match_count=0,ruleset_fingerprint=NULL,checkpoint_count=0,
                observed_output_token_count=0,scanned_token_count=0,emitted_token_count=0,
                emitted_sse_event_count=0,emitted_bytes=0,exposure_state='unknown',
                billing_disposition='not_required',failure_reason_code=?,completed_at=?
            WHERE tenant_id=? AND output_safety_scan_id=? AND request_id=? AND trace_id=?
              AND scan_status='pending'
            """,
            (
                failure_reason_code,
                completed_at,
                normalized["tenant_id"],
                scan_id,
                normalized["request_id"],
                normalized["trace_id"],
            ),
        )
        if cursor.rowcount != 1:
            # snapshot 失败终态只能抢占一次 pending，owner 丢失或并发终结必须固定报冲突。
            raise OutputSafetyStoreError("output_safety.snapshot_failure_conflict")
        connection.commit()
    # unavailable 证据的失败只做安全日志，原始快照异常仍由外层固定 503 负责对外收口。
    except (DbError, sqlite3.Error, OutputSafetyStoreError) as exc:
        _rollback_safely(connection)
        _log_store_failure(normalized, scan_id, "snapshot_failure_finalize_failed", exc)
    finally:
        _close_safely(connection)


def _ensure_blocked_event(
    connection: sqlite3.Connection,
    scan: PreparedOutputSafetyScan,
    terminal: OutputSafetyTerminalState,
    *,
    occurred_at: str,
) -> OutputSafetyEventRecord:
    """在 scan 终态事务中插入或精确读取唯一 blocked source event。"""

    existing = connection.execute(
        """
        SELECT event_id,tenant_id,output_safety_scan_id,request_id,trace_id,event_type,
               schema_version,scan_mode,scan_status,decision,error_code,exposure_state,
               emitted_token_count,checkpoint_count,dlp_rule_id,detector_version,
               billing_disposition,occurred_at
        FROM output_safety_events
        WHERE tenant_id=? AND output_safety_scan_id=?
        """,
        (scan.tenant_id, scan.output_safety_scan_id),
    ).fetchone()
    if existing is not None:
        event = _event_from_row(existing)
        _assert_event_matches(event, scan, terminal, occurred_at)
        return event

    event = OutputSafetyEventRecord(
        event_id=f"osevt_{uuid4().hex}",
        tenant_id=scan.tenant_id,
        output_safety_scan_id=scan.output_safety_scan_id,
        request_id=scan.request_id,
        trace_id=scan.trace_id,
        event_type="security.output_blocked",
        schema_version=1,
        scan_mode=scan.scan_mode,
        scan_status="completed",
        decision="blocked",
        error_code="content_policy_violation",
        exposure_state=terminal.exposure_state,  # type: ignore[arg-type]
        emitted_token_count=terminal.emitted_token_count,
        checkpoint_count=terminal.checkpoint_count,
        dlp_rule_id=terminal.dlp_rule_id or "",
        detector_version=DETECTOR_VERSION,
        billing_disposition="dispute_pending",
        occurred_at=occurred_at,
    )
    connection.execute(
        """
        INSERT INTO output_safety_events(
          event_id,tenant_id,output_safety_scan_id,request_id,trace_id,event_type,
          schema_version,scan_mode,scan_status,decision,error_code,exposure_state,
          emitted_token_count,checkpoint_count,dlp_rule_id,detector_version,
          billing_disposition,occurred_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event.event_id,
            event.tenant_id,
            event.output_safety_scan_id,
            event.request_id,
            event.trace_id,
            event.event_type,
            event.schema_version,
            event.scan_mode,
            event.scan_status,
            event.decision,
            event.error_code,
            event.exposure_state,
            event.emitted_token_count,
            event.checkpoint_count,
            event.dlp_rule_id,
            event.detector_version,
            event.billing_disposition,
            event.occurred_at,
        ),
    )
    return event


def _validate_terminal(
    scan: PreparedOutputSafetyScan,
    terminal: OutputSafetyTerminalState,
) -> None:
    """校验二维状态、规则元数据、计数、暴露和账单意图的唯一合法组合。"""

    integer_values = (
        terminal.match_count,
        terminal.checkpoint_count,
        terminal.observed_output_token_count,
        terminal.scanned_token_count,
        terminal.emitted_token_count,
        terminal.emitted_sse_event_count,
        terminal.emitted_bytes,
    )
    if any(type(value) is not int or value < 0 for value in integer_values):
        # bool、浮点或负计数会破坏用量和暴露审计口径，必须在进入 SQL 前拒绝。
        raise OutputSafetyStoreError("output_safety.counter_invalid")
    if terminal.exposure_state not in {"none", "partial", "complete", "unknown"}:
        # 暴露边界只能使用冻结枚举，任意文本状态可能误导已发送内容是否可撤回。
        raise OutputSafetyStoreError("output_safety.exposure_invalid")
    if terminal.billing_disposition not in {"not_required", "dispute_pending"}:
        # 数据面只能标记待复核或无需复核，不能伪造已经完成财务冲正的状态。
        raise OutputSafetyStoreError("output_safety.billing_disposition_invalid")
    if (
        terminal.scanned_token_count > terminal.observed_output_token_count
        or terminal.emitted_token_count > terminal.observed_output_token_count
        or terminal.match_count > len(scan.snapshot.rules)
    ):
        # observed 是 Provider 已到达 Gateway 的上界；扫描/交付计数和不同规则命中数均不得凭空增加。
        raise OutputSafetyStoreError("output_safety.counter_relationship_invalid")
    if terminal.failure_reason_code is not None and (
        terminal.failure_reason_code not in ALLOWED_FAILURE_REASON_CODES
    ):
        # 自由 reason 可能携带正文或异常原文，持久层只允许预先审核过的固定原因码。
        raise OutputSafetyStoreError("output_safety.failure_reason_invalid")

    rule_metadata_empty = (
        terminal.action is None
        and terminal.dlp_rule_id is None
        and terminal.rule_type is None
        and terminal.match_count == 0
    )
    if terminal.scan_status in {"not_configured", "not_applicable"}:
        valid = (
            terminal.decision == "none"
            and rule_metadata_empty
            and terminal.failure_reason_code is None
            and terminal.exposure_state == "unknown"
            and terminal.billing_disposition == "not_required"
            and all(value == 0 for value in integer_values)
        )
        if terminal.scan_status == "not_configured":
            valid = valid and not scan.snapshot.rules
        else:
            valid = valid and bool(scan.snapshot.rules)
        if not valid:
            # 未配置与不适用都不能伪造 clean、命中规则、输出计数或客户暴露事实。
            raise OutputSafetyStoreError("output_safety.terminal_contract_invalid")
        return

    if terminal.scan_status == "completed":
        if terminal.failure_reason_code is not None or terminal.decision not in {
            "clean",
            "matched",
            "blocked",
        }:
            # completed 只表达实际完成的内容判定，不得携带失败原因或未知决策。
            raise OutputSafetyStoreError("output_safety.terminal_contract_invalid")
        if not scan.snapshot.rules or terminal.checkpoint_count < 1:
            # 有规则扫描必须至少执行一次检查点，空规则只能提前终结为 not_configured。
            raise OutputSafetyStoreError("output_safety.completed_without_rules")
        if terminal.decision == "clean":
            valid = (
                rule_metadata_empty
                and terminal.exposure_state == "complete"
                and terminal.billing_disposition == "not_required"
            )
        elif terminal.decision == "matched":
            valid = (
                terminal.action in {"allow", "log_only"}
                and bool(terminal.dlp_rule_id)
                and terminal.rule_type in {"keyword", "regex"}
                and terminal.match_count >= 1
                and terminal.exposure_state == "complete"
                and terminal.billing_disposition == "not_required"
                and _effective_rule_matches_snapshot(scan, terminal)
            )
        else:
            valid = (
                terminal.action == "block"
                and bool(terminal.dlp_rule_id)
                and terminal.rule_type in {"keyword", "regex"}
                and terminal.match_count >= 1
                and terminal.exposure_state in {"none", "partial"}
                and terminal.billing_disposition == "dispute_pending"
                and _effective_rule_matches_snapshot(scan, terminal)
            )
        if not valid:
            # clean、matched、blocked 的规则元数据、暴露和账单组合必须与二维状态精确一致。
            raise OutputSafetyStoreError("output_safety.terminal_contract_invalid")
        return

    if terminal.scan_status == "unavailable":
        valid_decision = (
            terminal.decision == "unknown" and rule_metadata_empty
        ) or (
            terminal.decision == "matched"
            and terminal.action in {"allow", "log_only"}
            and bool(terminal.dlp_rule_id)
            and terminal.rule_type in {"keyword", "regex"}
            and terminal.match_count >= 1
            and _effective_rule_matches_snapshot(scan, terminal)
        )
        valid = (
            valid_decision
            and terminal.failure_reason_code is not None
            and terminal.exposure_state in {"none", "partial", "unknown"}
            and terminal.billing_disposition == "not_required"
        )
        if not valid:
            # 不可用状态只能保留已确认 matched 或 unknown，绝不能降级保存 clean/blocked。
            raise OutputSafetyStoreError("output_safety.terminal_contract_invalid")
        return
    # 任何未知 scan_status 都属于调用方状态机破坏，禁止宽松映射为普通失败状态。
    raise OutputSafetyStoreError("output_safety.scan_status_invalid")


def _effective_rule_matches_snapshot(
    scan: PreparedOutputSafetyScan,
    terminal: OutputSafetyTerminalState,
) -> bool:
    """确认决定性规则确实属于冻结快照，且类型与动作没有被调用方改写。"""

    return any(
        rule.dlp_rule_id == terminal.dlp_rule_id
        and rule.rule_type == terminal.rule_type
        and rule.action == terminal.action
        for rule in scan.snapshot.rules
    )


def _normalize_prepare_contract(
    *,
    tenant_id: str,
    request_id: str,
    trace_id: str,
    scan_mode: str,
    stream_session_id: str | None,
    interval_tokens: int | None,
    window_tokens: int | None,
) -> dict[str, Any]:
    """规范化可信 owner 和窗口参数，非法调用必须在任何数据库写入前终止。"""

    # owner 必须全部来自鉴权后服务端上下文，空白或非字符串身份不能进入复合租户键。
    if any(
        not isinstance(value, str) or not value.strip()
        for value in (tenant_id, request_id, trace_id)
    ):
        # owner 校验失败使用统一不可用错误，不能回显客户端传入的可疑身份内容。
        raise ApiError("gateway.output_safety_unavailable")
    if scan_mode not in {"non_stream_full", "stream_window"}:
        # 未知模式可能绕过全文或事件门禁，必须在建立 pending owner 之前拒绝。
        raise ApiError("gateway.output_safety_unavailable")
    if scan_mode == "non_stream_full":
        # 非流式 owner 不得引用 StreamSession 或伪装窗口配置，否则状态投影会产生歧义。
        if stream_session_id is not None or interval_tokens is not None or window_tokens is not None:
            # 模式字段冲突属于内部合同错误，只向调用链暴露固定安全不可用结果。
            raise ApiError("gateway.output_safety_unavailable")
        normalized_interval = None
        normalized_window = None
    else:
        # 流式 pending 必须先引用同租户、同请求的已提交 starting StreamSession。
        if not isinstance(stream_session_id, str) or not stream_session_id.strip():
            # 缺失 session owner 时禁止先插空 ID 再通过后续 UPDATE 绑定，避免跨租户错配。
            raise ApiError("gateway.output_safety_unavailable")
        normalized_interval = DEFAULT_INTERVAL_TOKENS if interval_tokens is None else interval_tokens
        normalized_window = DEFAULT_WINDOW_TOKENS if window_tokens is None else window_tokens
        if (
            type(normalized_interval) is not int
            or not 8 <= normalized_interval <= 256
            or type(normalized_window) is not int
            or not 32 <= normalized_window <= 1_024
            or normalized_window < normalized_interval
        ):
            # 非法窗口可能造成漏扫或无界资源消耗，不能被静默裁剪成默认值继续执行。
            raise ApiError("gateway.output_safety_unavailable")
    return {
        "tenant_id": tenant_id.strip(),
        "request_id": request_id.strip(),
        "trace_id": trace_id.strip(),
        "scan_mode": scan_mode,
        "stream_session_id": stream_session_id.strip() if stream_session_id else None,
        "interval_tokens": normalized_interval,
        "window_tokens": normalized_window,
    }


def _validate_rule_row(**values: object) -> None:
    """验证 DLP 行和 pattern_hash 一致性，损坏规则不得被静默跳过。"""

    rule_id = values["rule_id"]
    rule_type = values["rule_type"]
    pattern = values["pattern"]
    pattern_hash = values["pattern_hash"]
    action = values["action"]
    priority = values["priority"]
    scope = values["scope"]
    updated_at = values["updated_at"]
    valid = (
        isinstance(rule_id, str)
        and bool(rule_id)
        and rule_type in {"keyword", "regex"}
        and isinstance(pattern, str)
        and bool(pattern)
        and isinstance(pattern_hash, str)
        and action in {"allow", "log_only", "block"}
        and type(priority) is int
        and 1 <= priority <= 10_000
        and scope in {"output", "both"}
        and isinstance(updated_at, str)
        and bool(updated_at)
    )
    if not valid:
        # 任一规则元数据损坏都会使冻结顺序和匹配语义不可证明，不能静默跳过该行。
        raise OutputSafetyStoreError("output_safety.rule_row_invalid")
    expected_hash = "sha256:" + hashlib.sha256(
        f"{rule_type}:{pattern}".encode("utf-8")
    ).hexdigest()
    if pattern_hash != expected_hash:
        # pattern 与治理版本摘要不一致表示数据库事实被污染，必须在 Provider 前失败关闭。
        raise OutputSafetyStoreError("output_safety.rule_hash_mismatch")


def _ruleset_fingerprint(canonical_rules: list[dict[str, object]]) -> str:
    """计算只用于版本证据的 canonical SHA-256，不持久化单条 pattern_hash。"""

    serialized = json.dumps(
        canonical_rules,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _terminal_values(
    terminal: OutputSafetyTerminalState,
    ruleset_fingerprint: str | None,
    completed_at: str,
) -> tuple[object, ...]:
    """按数据库 UPDATE 列顺序投影终态，集中保障幂等比较使用同一合同。"""

    return (
        terminal.scan_status,
        terminal.decision,
        terminal.action,
        terminal.dlp_rule_id,
        terminal.rule_type,
        terminal.match_count,
        ruleset_fingerprint,
        terminal.checkpoint_count,
        terminal.observed_output_token_count,
        terminal.scanned_token_count,
        terminal.emitted_token_count,
        terminal.emitted_sse_event_count,
        terminal.emitted_bytes,
        terminal.exposure_state,
        terminal.billing_disposition,
        terminal.failure_reason_code,
        completed_at,
    )


def _event_from_row(row: sqlite3.Row) -> OutputSafetyEventRecord:
    """把 tenant-qualified source event 行转换成严格、无正文的内存记录。"""

    return OutputSafetyEventRecord(
        event_id=str(row["event_id"]),
        tenant_id=str(row["tenant_id"]),
        output_safety_scan_id=str(row["output_safety_scan_id"]),
        request_id=str(row["request_id"]),
        trace_id=str(row["trace_id"]),
        event_type=str(row["event_type"]),  # type: ignore[arg-type]
        schema_version=int(row["schema_version"]),  # type: ignore[arg-type]
        scan_mode=str(row["scan_mode"]),  # type: ignore[arg-type]
        scan_status=str(row["scan_status"]),  # type: ignore[arg-type]
        decision=str(row["decision"]),  # type: ignore[arg-type]
        error_code=str(row["error_code"]),  # type: ignore[arg-type]
        exposure_state=str(row["exposure_state"]),  # type: ignore[arg-type]
        emitted_token_count=int(row["emitted_token_count"]),
        checkpoint_count=int(row["checkpoint_count"]),
        dlp_rule_id=str(row["dlp_rule_id"]),
        detector_version=str(row["detector_version"]),
        billing_disposition=str(row["billing_disposition"]),  # type: ignore[arg-type]
        occurred_at=str(row["occurred_at"]),
    )


def _assert_event_matches(
    event: OutputSafetyEventRecord,
    scan: PreparedOutputSafetyScan,
    terminal: OutputSafetyTerminalState,
    occurred_at: str,
) -> None:
    """拒绝同 scan 下任一字段不同的既有 event，避免幂等调用掩盖证据污染。"""

    expected = (
        scan.tenant_id,
        scan.output_safety_scan_id,
        scan.request_id,
        scan.trace_id,
        "security.output_blocked",
        1,
        scan.scan_mode,
        "completed",
        "blocked",
        "content_policy_violation",
        terminal.exposure_state,
        terminal.emitted_token_count,
        terminal.checkpoint_count,
        terminal.dlp_rule_id,
        DETECTOR_VERSION,
        "dispute_pending",
        occurred_at,
    )
    actual = (
        event.tenant_id,
        event.output_safety_scan_id,
        event.request_id,
        event.trace_id,
        event.event_type,
        event.schema_version,
        event.scan_mode,
        event.scan_status,
        event.decision,
        event.error_code,
        event.exposure_state,
        event.emitted_token_count,
        event.checkpoint_count,
        event.dlp_rule_id,
        event.detector_version,
        event.billing_disposition,
        event.occurred_at,
    )
    if actual != expected:
        # 同 scan 的事件字段不一致属于不可变证据污染，禁止覆盖或创建第二条事件。
        raise OutputSafetyStoreError("output_safety.event_conflict")


def _scan_owner(scan: PreparedOutputSafetyScan) -> dict[str, Any]:
    """把 scan 裁剪成日志允许的 owner 字段，不携带规则快照或 pattern。"""

    return {
        "tenant_id": scan.tenant_id,
        "request_id": scan.request_id,
        "trace_id": scan.trace_id,
    }


def _log_store_failure(
    owner: dict[str, Any],
    scan_id: str,
    operation: str,
    exc: BaseException,
) -> None:
    """记录固定操作、owner、scan ID 和异常类型，禁止输出异常字符串。"""

    logger.warning(
        "output_safety.persistence_failed",
        extra={
            "event": "output_safety.persistence_failed",
            "tenant_id": owner.get("tenant_id"),
            "request_id": owner.get("request_id"),
            "trace_id": owner.get("trace_id"),
            "object_id": scan_id,
            "error_code": "gateway.output_safety_unavailable",
            "payload": {
                "operation": operation,
                "exception_type": exc.__class__.__name__,
            },
        },
    )


def _rollback_safely(connection: sqlite3.Connection | None) -> None:
    """机会性回滚失败事务，二次故障不得覆盖固定输出安全错误。"""

    if connection is None:
        return
    try:
        connection.rollback()
    # 回滚二次失败不应覆盖原始固定安全错误，也不能把数据库异常文本写入日志。
    except sqlite3.Error:
        return


def _close_safely(connection: sqlite3.Connection | None) -> None:
    """释放短连接；close 故障不改变已提交证据，也不暴露数据库路径。"""

    if connection is None:
        return
    try:
        connection.close()
    # close 发生在提交或回滚之后，二次故障不能改写已经确定的持久化结果。
    except sqlite3.Error:
        return


def _to_db_time(value: datetime) -> str:
    """把带时区时刻转换成 scan 与 source event 共用的规范 UTC 秒文本。"""

    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_db_time(value: str) -> datetime:
    """把持久 source event 的规范 UTC 文本恢复为 Shared Webhook 使用的 aware datetime。"""

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # source row 时间损坏时固定拒绝构造事件，不能使用当前时间伪造另一条阻断事实。
    except (TypeError, ValueError):
        raise ValueError("output_safety.event_time_invalid") from None
    if parsed.tzinfo is None:
        # 无时区文本无法证明与 scan.completed_at 是同一时刻，禁止进入签名与投递队列。
        raise ValueError("output_safety.event_time_invalid")
    return parsed.astimezone(UTC)


def _utcnow() -> datetime:
    """返回秒级 UTC 时刻，保证 scan 和 source event 可逐字段核验。"""

    return datetime.now(UTC).replace(microsecond=0)


__all__ = [
    "ALLOWED_FAILURE_REASON_CODES",
    "MAX_OUTPUT_RULES_PER_REQUEST",
    "finalize_output_safety_scan",
    "finalize_output_safety_scan_in_transaction",
    "output_safety_event_to_webhook",
    "prepare_output_safety_scan",
]
