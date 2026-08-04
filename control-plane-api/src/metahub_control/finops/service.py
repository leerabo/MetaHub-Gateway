"""FinOps 正式账本查询、调整、幂等、冲正、审计与事件服务。

所有 SQL 都以认证上下文中的 ``tenant_id`` 为第一过滤条件。账本金额只在整数微元和
六位十进制文本之间转换；任何数据库损坏、跨租户引用或审计失败都必须失败关闭，且
不得把 SQL、原因正文、幂等键、Prompt、Response 或密钥写入日志和 Webhook。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import build_webhook_event

from metahub_control.audit import (
    AuditRecord,
    AuditRequestMeta,
    commit_audited_transaction,
    dispatch_audit_recorded,
    record_audit_log,
)
from metahub_control.auth import AuthContext

from .models import (
    FinOpsActorInfo,
    FinOpsAdjustmentInfo,
    FinOpsAdjustmentResponse,
    FinOpsCurrencyTotals,
    FinOpsLedgerEntryInfo,
    FinOpsLedgerListResponse,
    FinOpsLedgerTotals,
    FinOpsOutputSafetyDisputeInfo,
)

logger = logging.getLogger("metahub.control.finops")

# 金额、标识、查询和签名常量集中冻结，避免路由、服务和测试各自形成不同边界。
MICROS_PER_UNIT = 1_000_000
MAX_AMOUNT_MICROS = 1_000_000_000 * MICROS_PER_UNIT
MAX_IDENTIFIER_LENGTH = 160
CURSOR_VERSION = 1
CURSOR_DOMAIN = b"metahub.finops.cursor.v1"
ENTRY_TYPES = frozenset(
    {
        "usage_charge",
        "manual_debit",
        "manual_credit",
        "reversal",
        "gift_credit",
        "trial_credit",
    }
)
ADJUSTMENT_TYPES = ENTRY_TYPES - {"usage_charge"}
DIRECTIONS = frozenset({"debit", "credit"})
NORMALIZATION_STATUSES = frozenset({"normalized", "unavailable"})
POSTING_SOURCES = frozenset({"direct", "spool_replay", "legacy_backfill"})
LEDGER_STATUSES = frozenset({"posted"})
QUOTA_TYPES = frozenset({"paid", "free", "trial", "gift"})
CURRENCIES = frozenset({"CNY", "USD"})
PRICE_EVIDENCE_STATUSES = frozenset(
    {"priced", "unpriced", "legacy_amount_only", "not_applicable"}
)
EXPECTED_ADJUSTMENT_FIELDS = frozenset(
    {
        "adjustment_type",
        "amount",
        "reversal_of_entry_id",
        "related_request_id",
        "reason",
    }
)
REQUIRED_ADJUSTMENT_FIELDS = frozenset({"adjustment_type", "amount", "reason"})


@dataclass(frozen=True)
class _LedgerQuery:
    """已归一的账本筛选及其稳定哈希输入。"""

    start_date: date | None
    end_date: date | None
    entry_type: str | None
    direction: str | None
    quota_type: str | None
    source_request_id: str | None
    price_snapshot_id: str | None
    related_entry_id: str | None


@dataclass(frozen=True)
class _CursorState:
    """通过 HMAC 验证后的租户快照和 keyset 水位。"""

    snapshot_sequence: int
    before_sequence: int


@dataclass(frozen=True)
class _NormalizedAdjustment:
    """进入写事务前完成校验的调整意图。"""

    adjustment_type: str
    amount_micros: int
    amount: str
    reversal_of_entry_id: str | None
    related_request_id: str | None
    reason: str
    reason_sha256: str
    payload_sha256: str


@dataclass(frozen=True)
class _LedgerEventRecord:
    """从已提交分录构造 Webhook 所需的固定安全字段。"""

    event_id: str
    tenant_id: str
    ledger_entry_id: str
    actor_user_id: str | None
    trace_id: str
    created_at: datetime
    payload: dict[str, object]


def list_finops_ledger(
    config: AppConfig,
    context: AuthContext,
    *,
    query: dict[str, str | None],
    limit: int,
    cursor: str | None,
    request_id: str,
    trace_id: str,
) -> FinOpsLedgerListResponse:
    """在同一 SQLite 读快照内返回当前租户列表、精确汇总和签名下一页游标。"""

    normalized = _normalize_ledger_query(query)
    filter_hash = _query_fingerprint(normalized)
    cursor_state = (
        _decode_cursor(config, context.user.tenant_id, filter_hash, cursor)
        if cursor is not None
        else None
    )
    connection = _connect(config)
    try:
        connection.execute("BEGIN DEFERRED")
        if cursor_state is None:
            maximum = connection.execute(
                "SELECT COALESCE(MAX(ledger_sequence), 0) FROM finops_ledger_entries WHERE tenant_id=?",
                (context.user.tenant_id,),
            ).fetchone()
            snapshot_sequence = _non_negative_integer(maximum[0] if maximum else 0)
            before_sequence = snapshot_sequence + 1
        else:
            snapshot_sequence = cursor_state.snapshot_sequence
            before_sequence = cursor_state.before_sequence

        clauses, params = _ledger_filters(
            context.user.tenant_id,
            normalized,
            snapshot_sequence=snapshot_sequence,
        )
        where_sql = " AND ".join(clauses)
        totals = _read_totals(connection, where_sql, params)
        page_rows = connection.execute(
            f"""
            SELECT
              le.*, a.reason,
              u.display_name AS actor_display_name, u.email AS actor_email,
              dispute.finops_ledger_dispute_id AS output_safety_dispute_id,
              dispute.tenant_id AS output_safety_dispute_tenant_id,
              dispute.request_id AS output_safety_dispute_request_id,
              dispute.trace_id AS output_safety_dispute_trace_id,
              dispute.output_safety_scan_id AS output_safety_dispute_scan_id,
              dispute.usage_ledger_id AS output_safety_dispute_usage_ledger_id,
              dispute.finops_ledger_entry_id AS output_safety_dispute_entry_id,
              dispute.review_status AS output_safety_dispute_review_status,
              dispute.reversal_eligible AS output_safety_dispute_reversal_eligible,
              dispute.reason_code AS output_safety_dispute_reason_code,
              scan.tenant_id AS output_safety_scan_tenant_id,
              scan.request_id AS output_safety_scan_request_id,
              scan.trace_id AS output_safety_scan_trace_id,
              scan.output_safety_scan_id AS output_safety_owner_scan_id,
              scan.scan_status AS output_safety_scan_status,
              scan.decision AS output_safety_scan_decision,
              scan.billing_disposition AS output_safety_scan_billing_disposition,
              usage.tenant_id AS output_safety_usage_tenant_id,
              usage.request_id AS output_safety_usage_request_id,
              usage.usage_ledger_id AS output_safety_owner_usage_ledger_id
            FROM finops_ledger_entries le
            LEFT JOIN finops_adjustments a
              ON a.tenant_id=le.tenant_id
             AND a.finops_adjustment_id=le.finops_adjustment_id
            LEFT JOIN users u
              ON u.tenant_id=le.tenant_id AND u.user_id=le.actor_user_id
            LEFT JOIN finops_ledger_disputes dispute
              ON dispute.tenant_id=le.tenant_id
             AND dispute.finops_ledger_entry_id=le.finops_ledger_entry_id
            LEFT JOIN output_safety_scans scan
              ON scan.tenant_id=dispute.tenant_id
             AND scan.output_safety_scan_id=dispute.output_safety_scan_id
            LEFT JOIN usage_ledger usage
              ON usage.tenant_id=dispute.tenant_id
             AND usage.usage_ledger_id=dispute.usage_ledger_id
            WHERE {where_sql} AND le.ledger_sequence<?
            ORDER BY le.ledger_sequence DESC
            LIMIT ?
            """,
            [*params, before_sequence, limit + 1],
        ).fetchall()
        connection.rollback()
    # 领域错误保留原错误码，但仍需回滚读事务，避免连接残留未结束的事务状态。
    except ApiError:
        _safe_rollback(connection)
        raise
    # 数据库或持久事实类型异常统一隐藏，不能向客户端暴露底层实现细节。
    except (sqlite3.Error, TypeError, ValueError, ArithmeticError) as exc:
        _safe_rollback(connection)
        raise ApiError("finops.ledger_unavailable") from exc
    finally:
        connection.close()

    # 正常 schema 保证每个 Usage charge 只有一个 dispute；若约束损坏导致 JOIN 扩行，
    # 必须在分页切片前失败关闭，不能返回重复分录或生成跳页游标。
    ledger_ids = [row["finops_ledger_entry_id"] for row in page_rows]
    if len(ledger_ids) != len(set(ledger_ids)):
        raise ApiError("output_safety.integrity_failed")
    has_more = len(page_rows) > limit
    visible_rows = page_rows[:limit]
    next_cursor = None
    if has_more and visible_rows:
        last_sequence = _positive_integer(visible_rows[-1]["ledger_sequence"])
        next_cursor = _encode_cursor(
            config,
            context.user.tenant_id,
            filter_hash,
            snapshot_sequence=snapshot_sequence,
            before_sequence=last_sequence,
        )
    try:
        entries = [_ledger_entry_info(row) for row in visible_rows]
    # 任一行到 DTO 的转换失败都拒绝整页，避免返回混有损坏事实的部分结果。
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise ApiError("finops.ledger_unavailable") from exc
    return FinOpsLedgerListResponse(
        ledger_entries=entries,
        totals=totals,
        total=totals.entry_count,
        limit=limit,
        next_cursor=next_cursor,
        has_more=has_more,
        request_id=request_id,
        trace_id=trace_id,
    )


def create_finops_adjustment(
    config: AppConfig,
    context: AuthContext,
    payload: dict[str, Any],
    *,
    idempotency_key: str,
    audit_request_meta: AuditRequestMeta,
    request_id: str,
    trace_id: str,
) -> FinOpsAdjustmentResponse:
    """以租户级幂等键创建调整、对应分录和同事务审计，再提交后投递安全事件。"""

    normalized_request_id = _required_identifier(request_id, "finops.adjustment_invalid")
    normalized_trace_id = _required_identifier(trace_id, "finops.adjustment_invalid")
    normalized = _normalize_adjustment(payload)
    idempotency_hash = hashlib.sha256(idempotency_key.encode("ascii")).hexdigest()
    connection = _connect(config)
    audit_record: AuditRecord | None = None
    ledger_event: _LedgerEventRecord | None = None
    adjustment_info: FinOpsAdjustmentInfo | None = None
    ledger_info: FinOpsLedgerEntryInfo | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        recovered = _recover_adjustment(
            connection,
            context,
            idempotency_hash=idempotency_hash,
            payload_hash=normalized.payload_sha256,
        )
        if recovered is not None:
            connection.rollback()
            recovered_adjustment, recovered_entry = recovered
            return FinOpsAdjustmentResponse(
                adjustment=recovered_adjustment,
                ledger_entry=recovered_entry,
                idempotent_replay=True,
                request_id=normalized_request_id,
                trace_id=normalized_trace_id,
            )

        derived = _derive_adjustment_entry(connection, context, normalized)
        now = _to_db_time(_utcnow())
        adjustment_id = f"fadj_{uuid4().hex}"
        ledger_id = f"fle_{uuid4().hex}"
        event_id = f"flevt_{uuid4().hex}"
        connection.execute(
            """
            INSERT INTO finops_adjustments(
              finops_adjustment_id, tenant_id, adjustment_type, amount_micros,
              currency, reversal_of_entry_id, related_request_id, reason,
              reason_sha256, actor_user_id, operation_request_id, operation_trace_id,
              idempotency_key_sha256, payload_sha256, status, created_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'posted', ?, ?)
            """,
            (
                adjustment_id,
                context.user.tenant_id,
                normalized.adjustment_type,
                normalized.amount_micros,
                derived["currency"],
                normalized.reversal_of_entry_id,
                normalized.related_request_id,
                normalized.reason,
                normalized.reason_sha256,
                context.user.user_id,
                normalized_request_id,
                normalized_trace_id,
                idempotency_hash,
                normalized.payload_sha256,
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO finops_ledger_entries(
              finops_ledger_entry_id, event_id, tenant_id, entry_type, direction,
              amount_micros, currency, normalized_cny_micros, cny_normalization_status,
              price_evidence_status, price_snapshot_source, posting_source, quota_type,
              usage_ledger_id, finops_adjustment_id, source_request_id, source_trace_id,
              operation_request_id, operation_trace_id, price_snapshot_id,
              price_schema_version, reversal_of_entry_id, actor_user_id,
              input_tokens, output_tokens, total_tokens, cache_tokens,
              model_id, provider_id, virtual_key_id, environment_id, project_id,
              application_id, estimated, status, created_at, updated_at
            )
            VALUES(
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'direct', ?,
              NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?,
              0, 0, 0, 0, ?, ?, ?, ?, ?, ?, 0, 'posted', ?, ?
            )
            """,
            (
                ledger_id,
                event_id,
                context.user.tenant_id,
                normalized.adjustment_type,
                derived["direction"],
                normalized.amount_micros,
                derived["currency"],
                derived["normalized_cny_micros"],
                derived["cny_normalization_status"],
                derived["price_evidence_status"],
                derived["price_snapshot_source"],
                derived["quota_type"],
                adjustment_id,
                derived["source_request_id"],
                derived["source_trace_id"],
                normalized_request_id,
                normalized_trace_id,
                derived["price_snapshot_id"],
                derived["price_schema_version"],
                normalized.reversal_of_entry_id,
                context.user.user_id,
                derived["model_id"],
                derived["provider_id"],
                derived["virtual_key_id"],
                derived["environment_id"],
                derived["project_id"],
                derived["application_id"],
                now,
                now,
            ),
        )
        row = _load_ledger_entry(connection, context.user.tenant_id, ledger_id)
        adjustment_row = _load_adjustment(connection, context.user.tenant_id, adjustment_id)
        ledger_info = _ledger_entry_info(row)
        adjustment_info = _adjustment_info(adjustment_row)
        audit_record = record_audit_log(
            connection,
            context,
            action="finops.adjustment_created",
            object_type="finops_adjustment",
            object_id=adjustment_id,
            object_name=normalized.adjustment_type,
            before_summary={},
            after_summary={
                "adjustment_type": normalized.adjustment_type,
                "amount": normalized.amount,
                "currency": derived["currency"],
                "reversal_of_entry_id": normalized.reversal_of_entry_id,
                "related_request_id": normalized.related_request_id,
                "reason_sha256": normalized.reason_sha256,
            },
            request_meta=audit_request_meta,
            request_id=normalized_request_id,
            trace_id=normalized_trace_id,
        )
        # 调整、账本和审计必须同事务存在，缺少审计记录时禁止确认财务事实。
        if audit_record is None:
            raise ApiError("finops.adjustment_failed")
        ledger_event = _ledger_event(row)
        commit_audited_transaction(connection)
    # 所有领域失败都先回滚整笔调整事务，避免留下半条账本或孤立审计记录。
    except ApiError as exc:
        _safe_rollback(connection)
        # 审计子系统错误转换为稳定 FinOps 合同，避免暴露内部错误码与实现边界。
        if exc.code in {"audit_log.record_unavailable", "audit_log.summary_invalid"}:
            raise ApiError("finops.adjustment_failed") from exc
        raise
    # 数据库锁定或暂时不可写属于可用性故障，客户端可以按统一合同重试。
    except sqlite3.OperationalError as exc:
        _safe_rollback(connection)
        raise ApiError("finops.ledger_unavailable") from exc
    # 其他持久化或事实转换异常按调整失败收敛，禁止泄露 SQL 与原始异常。
    except (sqlite3.Error, TypeError, ValueError, ArithmeticError) as exc:
        _safe_rollback(connection)
        raise ApiError("finops.adjustment_failed") from exc
    finally:
        connection.close()

    # 提交成功后四类产物都应完整存在，防御未来修改遗漏事务内赋值。
    if adjustment_info is None or ledger_info is None or audit_record is None or ledger_event is None:
        raise ApiError("finops.adjustment_failed")
    dispatch_audit_recorded(
        config,
        context,
        audit_record,
        request_id=normalized_request_id,
        trace_id=normalized_trace_id,
    )
    _dispatch_ledger_event(config, ledger_event, normalized_request_id)
    _log_adjustment_created(context, ledger_info, normalized_request_id, normalized_trace_id)
    return FinOpsAdjustmentResponse(
        adjustment=adjustment_info,
        ledger_entry=ledger_info,
        idempotent_replay=False,
        request_id=normalized_request_id,
        trace_id=normalized_trace_id,
    )


def _normalize_ledger_query(query: dict[str, str | None]) -> _LedgerQuery:
    """把 wire 查询转换为稳定枚举、日期和精确标识，非法值统一返回查询错误。"""

    start = _optional_date(query.get("start_date"))
    end = _optional_date(query.get("end_date"))
    if start is not None and end is not None:
        # 日期倒置或跨度过大会扩大财务查询成本，必须拒绝而不能交换或截断边界。
        if end < start or (end - start).days > 365:
            raise ApiError("finops.query_invalid")
    return _LedgerQuery(
        start_date=start,
        end_date=end,
        entry_type=_optional_enum(query.get("entry_type"), ENTRY_TYPES),
        direction=_optional_enum(query.get("direction"), DIRECTIONS),
        quota_type=_optional_enum(query.get("quota_type"), QUOTA_TYPES),
        source_request_id=_optional_identifier(query.get("request_id"), "finops.query_invalid"),
        price_snapshot_id=_optional_identifier(
            query.get("price_snapshot_id"), "finops.query_invalid"
        ),
        related_entry_id=_optional_identifier(
            query.get("related_entry_id"), "finops.query_invalid"
        ),
    )


def _ledger_filters(
    tenant_id: str,
    query: _LedgerQuery,
    *,
    snapshot_sequence: int,
) -> tuple[list[str], list[object]]:
    """构造只含内部固定列名的 tenant-first SQL 条件和绑定参数。"""

    clauses = ["le.tenant_id=?", "le.ledger_sequence<=?"]
    params: list[object] = [tenant_id, snapshot_sequence]
    if query.start_date is not None:
        clauses.append("le.created_at>=?")
        params.append(f"{query.start_date.isoformat()}T00:00:00Z")
    if query.end_date is not None:
        clauses.append("le.created_at<?")
        params.append(f"{(query.end_date + timedelta(days=1)).isoformat()}T00:00:00Z")
    for column, value in (
        ("le.entry_type", query.entry_type),
        ("le.direction", query.direction),
        ("le.quota_type", query.quota_type),
        ("le.source_request_id", query.source_request_id),
        ("le.price_snapshot_id", query.price_snapshot_id),
    ):
        if value is not None:
            clauses.append(f"{column}=?")
            params.append(value)
    if query.related_entry_id is not None:
        clauses.append("(le.finops_ledger_entry_id=? OR le.reversal_of_entry_id=?)")
        params.extend((query.related_entry_id, query.related_entry_id))
    return clauses, params


def _read_totals(
    connection: sqlite3.Connection,
    where_sql: str,
    params: list[object],
) -> FinOpsLedgerTotals:
    """流式累计筛选快照内整数微元，禁止 SQLite TOTAL 和二进制浮点。"""

    amounts: dict[str, dict[str, int]] = {"CNY": {"debit": 0, "credit": 0}}
    normalized_debit = 0
    normalized_credit = 0
    unnormalized_count = 0
    entry_count = 0
    rows = connection.execute(
        f"""
        SELECT direction, amount_micros, currency, normalized_cny_micros,
               cny_normalization_status
        FROM finops_ledger_entries le
        WHERE {where_sql}
        ORDER BY le.ledger_sequence
        """,
        params,
    )
    for row in rows:
        direction = _database_enum(row["direction"], DIRECTIONS, "finops total direction invalid")
        currency = _database_enum(row["currency"], CURRENCIES, "finops total currency invalid")
        amount = _non_negative_integer(row["amount_micros"])
        amounts.setdefault(currency, {"debit": 0, "credit": 0})[direction] += amount
        normalized = row["normalized_cny_micros"]
        status = _database_enum(
            row["cny_normalization_status"],
            NORMALIZATION_STATUSES,
            "finops normalization status invalid",
        )
        if normalized is None:
            # 归一金额为空时状态必须明确为 unavailable，避免汇总把缺失价格当作零值。
            if status != "unavailable":
                raise ValueError("finops normalization status invalid")
            unnormalized_count += 1
        else:
            # 存在归一金额时状态必须为 normalized，防止错误金额进入人民币净额。
            if status != "normalized":
                raise ValueError("finops normalization status invalid")
            normalized_amount = _non_negative_integer(normalized)
            if direction == "debit":
                normalized_debit += normalized_amount
            else:
                normalized_credit += normalized_amount
        entry_count += 1
    by_currency = {
        currency: FinOpsCurrencyTotals(
            debit=_format_micros(values["debit"]),
            credit=_format_micros(values["credit"]),
            net=_format_micros(values["debit"] - values["credit"]),
        )
        for currency, values in sorted(amounts.items())
    }
    return FinOpsLedgerTotals(
        by_currency=by_currency,
        net_cost_cny=_format_micros(normalized_debit - normalized_credit),
        unnormalized_entry_count=unnormalized_count,
        entry_count=entry_count,
    )


def _normalize_adjustment(payload: dict[str, Any]) -> _NormalizedAdjustment:
    """严格归一人工调整字段，并生成不包含幂等键原文的稳定载荷摘要。"""

    if not REQUIRED_ADJUSTMENT_FIELDS.issubset(payload) or not set(payload).issubset(
        EXPECTED_ADJUSTMENT_FIELDS
    ):
        # 调整载荷必须同时满足必填集合和字段白名单，防止静默接收未知财务指令。
        raise ApiError("finops.adjustment_invalid")
    adjustment_type = payload.get("adjustment_type")
    # 类型只接受冻结枚举与原始字符串，禁止隐式转换产生未定义账本类别。
    if type(adjustment_type) is not str or adjustment_type not in ADJUSTMENT_TYPES:
        raise ApiError("finops.adjustment_invalid")
    amount_micros, amount = _parse_amount(payload.get("amount"))
    reversal_id = _optional_identifier(
        payload.get("reversal_of_entry_id"), "finops.adjustment_invalid"
    )
    related_request_id = _optional_identifier(
        payload.get("related_request_id"), "finops.adjustment_invalid"
    )
    if adjustment_type == "reversal":
        # 冲正只能引用目标分录且不得同时关联请求，避免两种派生来源发生冲突。
        if reversal_id is None or related_request_id is not None:
            raise ApiError("finops.adjustment_invalid")
    elif reversal_id is not None:
        # 非冲正类型不得携带冲正目标，防止客户端伪造反向财务关系。
        raise ApiError("finops.adjustment_invalid")
    reason_value = payload.get("reason")
    # 原因必须由管理员显式提供文本，不能把其他 JSON 类型隐式字符串化。
    if type(reason_value) is not str:
        raise ApiError("finops.adjustment_invalid")
    reason = reason_value.strip()
    if (
        not reason
        or len(reason.encode("utf-8")) > 500
        or any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in reason)
    ):
        # 原因进入数据库与审计边界，空值、超限或控制字符必须失败关闭。
        raise ApiError("finops.adjustment_invalid")
    reason_hash = hashlib.sha256(reason.encode("utf-8")).hexdigest()
    canonical = json.dumps(
        {
            "adjustment_type": adjustment_type,
            "amount": amount,
            "reversal_of_entry_id": reversal_id,
            "related_request_id": related_request_id,
            "reason": reason,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _NormalizedAdjustment(
        adjustment_type=adjustment_type,
        amount_micros=amount_micros,
        amount=amount,
        reversal_of_entry_id=reversal_id,
        related_request_id=related_request_id,
        reason=reason,
        reason_sha256=reason_hash,
        payload_sha256=hashlib.sha256(canonical).hexdigest(),
    )


def _derive_adjustment_entry(
    connection: sqlite3.Connection,
    context: AuthContext,
    adjustment: _NormalizedAdjustment,
) -> dict[str, object | None]:
    """从同租户目标或关联 Usage 派生不可由客户端自报的分录字段。"""

    if adjustment.adjustment_type == "reversal":
        target = connection.execute(
            """
            SELECT * FROM finops_ledger_entries
            WHERE tenant_id=? AND finops_ledger_entry_id=?
            """,
            (context.user.tenant_id, adjustment.reversal_of_entry_id),
        ).fetchone()
        # 查询始终绑定当前租户；未命中也按不存在返回，避免暴露跨租户目标。
        if target is None:
            raise ApiError("finops.entry_not_found")
        target_entry_type = _database_enum(
            target["entry_type"], ENTRY_TYPES, "finops target entry type invalid"
        )
        if target_entry_type == "reversal" or _non_negative_integer(
            target["amount_micros"]
        ) == 0:
            # 冲正分录及零金额事实不能再次冲正，避免形成循环或无意义链路。
            raise ApiError("finops.reversal_not_allowed")
        reversed_row = connection.execute(
            """
            SELECT COALESCE(SUM(amount_micros), 0)
            FROM finops_ledger_entries
            WHERE tenant_id=? AND entry_type='reversal' AND reversal_of_entry_id=?
            """,
            (context.user.tenant_id, adjustment.reversal_of_entry_id),
        ).fetchone()
        reversed_micros = _non_negative_integer(reversed_row[0] if reversed_row else 0)
        target_amount = _non_negative_integer(target["amount_micros"])
        # 已冲正总额超过原金额表示持久事实损坏，禁止继续追加新分录。
        if reversed_micros > target_amount:
            raise ApiError("finops.adjustment_failed")
        # 新冲正不得超过剩余额度，确保累计信用方向金额不会穿透原分录。
        if adjustment.amount_micros > target_amount - reversed_micros:
            raise ApiError("finops.reversal_exceeds_remaining")
        currency = _database_enum(target["currency"], CURRENCIES, "finops target currency invalid")
        target_direction = _database_enum(
            target["direction"], DIRECTIONS, "finops target direction invalid"
        )
        return {
            "direction": "credit" if target_direction == "debit" else "debit",
            "currency": currency,
            "normalized_cny_micros": adjustment.amount_micros if currency == "CNY" else None,
            "cny_normalization_status": "normalized" if currency == "CNY" else "unavailable",
            "price_evidence_status": _price_evidence_status(target["price_evidence_status"]),
            "price_snapshot_source": _optional_db_text(target["price_snapshot_source"]),
            "quota_type": _quota_type(target["quota_type"]),
            "source_request_id": _optional_db_identifier(target["source_request_id"]),
            "source_trace_id": _optional_db_identifier(target["source_trace_id"]),
            "price_snapshot_id": _optional_db_identifier(target["price_snapshot_id"]),
            "price_schema_version": _optional_db_text(target["price_schema_version"]),
            "model_id": _optional_db_identifier(target["model_id"]),
            "provider_id": _optional_db_identifier(target["provider_id"]),
            "virtual_key_id": _optional_db_identifier(target["virtual_key_id"]),
            "environment_id": _optional_db_identifier(target["environment_id"]),
            "project_id": _optional_db_identifier(target["project_id"]),
            "application_id": _optional_db_identifier(target["application_id"]),
        }

    related: sqlite3.Row | None = None
    # 关联请求只用于继承当前租户的 Usage 维度，客户端不能直接上报这些字段。
    if adjustment.related_request_id is not None:
        related_rows = connection.execute(
            """
            SELECT * FROM finops_ledger_entries
            WHERE tenant_id=? AND entry_type='usage_charge' AND source_request_id=?
            ORDER BY ledger_sequence DESC
            LIMIT 2
            """,
            (context.user.tenant_id, adjustment.related_request_id),
        ).fetchall()
        # 当前租户不存在对应 Usage 时按请求不存在返回，不泄露其他租户命中情况。
        if not related_rows:
            raise ApiError("finops.request_not_found")
        # 同一请求出现多条 Usage 正式分录属于账本不变量破坏，禁止猜测选择。
        if len(related_rows) != 1:
            raise ApiError("finops.adjustment_failed")
        related = related_rows[0]
    direction = "debit" if adjustment.adjustment_type == "manual_debit" else "credit"
    quota_type = {
        "gift_credit": "gift",
        "trial_credit": "trial",
    }.get(adjustment.adjustment_type, "paid")
    return {
        "direction": direction,
        "currency": "CNY",
        "normalized_cny_micros": adjustment.amount_micros,
        "cny_normalization_status": "normalized",
        "price_evidence_status": "not_applicable",
        "price_snapshot_source": None,
        "quota_type": quota_type,
        "source_request_id": adjustment.related_request_id,
        "source_trace_id": _optional_db_text(related["source_trace_id"]) if related else None,
        "price_snapshot_id": None,
        "price_schema_version": None,
        "model_id": _optional_db_text(related["model_id"]) if related else None,
        "provider_id": _optional_db_text(related["provider_id"]) if related else None,
        "virtual_key_id": _optional_db_text(related["virtual_key_id"]) if related else None,
        "environment_id": _optional_db_text(related["environment_id"]) if related else None,
        "project_id": _optional_db_text(related["project_id"]) if related else None,
        "application_id": _optional_db_text(related["application_id"]) if related else None,
    }


def _recover_adjustment(
    connection: sqlite3.Connection,
    context: AuthContext,
    *,
    idempotency_hash: str,
    payload_hash: str,
) -> tuple[FinOpsAdjustmentInfo, FinOpsLedgerEntryInfo] | None:
    """按 tenant + 幂等摘要恢复完全相同的调整，不重复写审计或事件。"""

    row = connection.execute(
        """
        SELECT * FROM finops_adjustments
        WHERE tenant_id=? AND idempotency_key_sha256=?
        """,
        (context.user.tenant_id, idempotency_hash),
    ).fetchone()
    if row is None:
        return None
    # 相同幂等键只允许恢复完全相同的载荷，摘要不一致必须明确冲突。
    if not hmac.compare_digest(str(row["payload_sha256"]), payload_hash):
        raise ApiError("finops.idempotency_conflict")
    ledger_rows = connection.execute(
        """
        SELECT
          le.*, a.reason,
          u.display_name AS actor_display_name, u.email AS actor_email
        FROM finops_ledger_entries le
        JOIN finops_adjustments a
          ON a.tenant_id=le.tenant_id AND a.finops_adjustment_id=le.finops_adjustment_id
        LEFT JOIN users u
          ON u.tenant_id=le.tenant_id AND u.user_id=le.actor_user_id
        WHERE le.tenant_id=? AND le.finops_adjustment_id=?
        """,
        (context.user.tenant_id, row["finops_adjustment_id"]),
    ).fetchall()
    # 一次调整必须且只能对应一条正式分录，缺失或重复都视为原子性损坏。
    if len(ledger_rows) != 1:
        raise ApiError("finops.adjustment_failed")
    return _adjustment_info(row), _ledger_entry_info(ledger_rows[0])


def _load_ledger_entry(
    connection: sqlite3.Connection,
    tenant_id: str,
    ledger_entry_id: str,
) -> sqlite3.Row:
    """按租户和正式 ID 回读刚写入的完整安全分录。"""

    row = connection.execute(
        """
        SELECT
          le.*, a.reason,
          u.display_name AS actor_display_name, u.email AS actor_email
        FROM finops_ledger_entries le
        LEFT JOIN finops_adjustments a
          ON a.tenant_id=le.tenant_id AND a.finops_adjustment_id=le.finops_adjustment_id
        LEFT JOIN users u
          ON u.tenant_id=le.tenant_id AND u.user_id=le.actor_user_id
        WHERE le.tenant_id=? AND le.finops_ledger_entry_id=?
        """,
        (tenant_id, ledger_entry_id),
    ).fetchone()
    # 写入后按租户回读仍未命中说明事务事实不完整，不能返回虚构成功。
    if row is None:
        raise ApiError("finops.adjustment_failed")
    return row


def _load_adjustment(
    connection: sqlite3.Connection,
    tenant_id: str,
    adjustment_id: str,
) -> sqlite3.Row:
    """按租户回读刚写入的不可变 AdjustmentRecord。"""

    row = connection.execute(
        "SELECT * FROM finops_adjustments WHERE tenant_id=? AND finops_adjustment_id=?",
        (tenant_id, adjustment_id),
    ).fetchone()
    # 调整记录回读缺失表示账本与来源记录不再原子一致，必须失败关闭。
    if row is None:
        raise ApiError("finops.adjustment_failed")
    return row


def _ledger_entry_info(row: sqlite3.Row) -> FinOpsLedgerEntryInfo:
    """把数据库分录转换为安全 DTO，并失败关闭损坏枚举或金额。"""

    currency = _database_enum(row["currency"], CURRENCIES, "finops currency invalid")
    entry_type = _database_enum(row["entry_type"], ENTRY_TYPES, "finops entry type invalid")
    direction = _database_enum(row["direction"], DIRECTIONS, "finops direction invalid")
    status = _database_enum(row["status"], LEDGER_STATUSES, "finops status invalid")
    normalization_status = _database_enum(
        row["cny_normalization_status"],
        NORMALIZATION_STATUSES,
        "finops normalization status invalid",
    )
    evidence_status = _price_evidence_status(row["price_evidence_status"])
    posting_source = _database_enum(
        row["posting_source"], POSTING_SOURCES, "finops posting source invalid"
    )
    quota_type = _quota_type(row["quota_type"])
    amount = _non_negative_integer(row["amount_micros"])
    normalized_value = row["normalized_cny_micros"]
    normalized_cny = None
    if normalized_value is not None:
        # 非空人民币归一金额只能搭配 normalized 状态，防止 DTO 隐藏持久事实矛盾。
        if normalization_status != "normalized":
            raise ValueError("finops normalized amount invalid")
        normalized_cny = _format_micros(_non_negative_integer(normalized_value))
    elif normalization_status != "unavailable":
        # 缺失归一金额只能标为 unavailable，不能把缺失价格包装成已归一结果。
        raise ValueError("finops normalized amount invalid")
    actor_id = _optional_db_identifier(row["actor_user_id"])
    actor = None
    if actor_id is not None:
        email = _optional_db_text(row["actor_email"])
        # 有操作者标识时必须恢复完整账号信息，避免审计主体成为半结构记录。
        if email is None:
            raise ValueError("finops actor missing")
        actor = FinOpsActorInfo(
            user_id=actor_id,
            display_name=_optional_db_text(row["actor_display_name"]),
            email=email,
        )
    output_safety_dispute = _finops_output_safety_dispute(
        row,
        entry_type=entry_type,
        amount_micros=amount,
    )
    return FinOpsLedgerEntryInfo(
        finops_ledger_entry_id=_required_db_identifier(row["finops_ledger_entry_id"]),
        tenant_id=_required_db_identifier(row["tenant_id"]),
        entry_type=entry_type,
        direction=direction,
        amount=_format_micros(amount),
        currency=currency,
        normalized_cny=normalized_cny,
        cny_normalization_status=normalization_status,
        price_evidence_status=evidence_status,
        price_snapshot_source=_optional_db_text(row["price_snapshot_source"]),
        posting_source=posting_source,
        quota_type=quota_type,
        usage_ledger_id=_optional_db_identifier(row["usage_ledger_id"]),
        finops_adjustment_id=_optional_db_identifier(row["finops_adjustment_id"]),
        source_request_id=_optional_db_identifier(row["source_request_id"]),
        source_trace_id=_optional_db_identifier(row["source_trace_id"]),
        operation_request_id=_required_db_identifier(row["operation_request_id"]),
        operation_trace_id=_required_db_identifier(row["operation_trace_id"]),
        price_snapshot_id=_optional_db_identifier(row["price_snapshot_id"]),
        price_schema_version=_optional_db_text(row["price_schema_version"]),
        reversal_of_entry_id=_optional_db_identifier(row["reversal_of_entry_id"]),
        actor=actor,
        reason=_optional_db_text(row["reason"]),
        input_tokens=_non_negative_integer(row["input_tokens"]),
        output_tokens=_non_negative_integer(row["output_tokens"]),
        total_tokens=_non_negative_integer(row["total_tokens"]),
        cache_tokens=_non_negative_integer(row["cache_tokens"]),
        model_id=_optional_db_identifier(row["model_id"]),
        provider_id=_optional_db_identifier(row["provider_id"]),
        virtual_key_id=_optional_db_identifier(row["virtual_key_id"]),
        environment_id=_optional_db_identifier(row["environment_id"]),
        project_id=_optional_db_identifier(row["project_id"]),
        application_id=_optional_db_identifier(row["application_id"]),
        estimated=_sqlite_boolean(row["estimated"]),
        status=status,
        created_at=_parse_db_time(row["created_at"]),
        output_safety_dispute=output_safety_dispute,
    )


def _finops_output_safety_dispute(
    row: sqlite3.Row,
    *,
    entry_type: str,
    amount_micros: int,
) -> FinOpsOutputSafetyDisputeInfo | None:
    """恢复 FinOps 最小输出安全徽标，并在服务端验证完整 owner 图。

    参数：账本 JOIN 行、已校验分录类型和微元金额。返回：无 marker 时 ``None``，
    否则只返回 scan ID、固定复核状态和冲资格。关键约束：规则、模式、暴露计数、
    Usage/charge 内部 ID 与正文只参与校验，绝不进入 FinanceAdmin DTO。
    """

    keys = set(row.keys())
    if "output_safety_dispute_id" not in keys:
        # 调整创建/幂等恢复的内部回读没有 PRD-071 JOIN，保持既有响应兼容。
        return None
    fields = (
        "output_safety_dispute_id",
        "output_safety_dispute_tenant_id",
        "output_safety_dispute_request_id",
        "output_safety_dispute_trace_id",
        "output_safety_dispute_scan_id",
        "output_safety_dispute_usage_ledger_id",
        "output_safety_dispute_entry_id",
        "output_safety_dispute_review_status",
        "output_safety_dispute_reversal_eligible",
        "output_safety_dispute_reason_code",
        "output_safety_scan_tenant_id",
        "output_safety_scan_request_id",
        "output_safety_scan_trace_id",
        "output_safety_owner_scan_id",
        "output_safety_scan_status",
        "output_safety_scan_decision",
        "output_safety_scan_billing_disposition",
        "output_safety_usage_tenant_id",
        "output_safety_usage_request_id",
        "output_safety_owner_usage_ledger_id",
    )
    if row["output_safety_dispute_id"] is None:
        if any(row[field] is not None for field in fields[1:]):
            # 部分命中的 LEFT JOIN 不能降级为“无争议”，否则会隐藏损坏的财务事实。
            raise ApiError("output_safety.integrity_failed")
        return None
    try:
        dispute_id = _required_db_identifier(row["output_safety_dispute_id"])
        scan_id = _required_db_identifier(row["output_safety_dispute_scan_id"])
        if not _prefixed_hex_id(dispute_id, prefix="fld_", body_length=32):
            # 内部 marker 身份损坏时不向 FinanceAdmin 回显原值。
            raise ValueError("output safety dispute id invalid")
        if not _prefixed_hex_id(scan_id, prefix="oss_", body_length=32):
            # scan ID 必须保持 0063 冻结格式，避免把任意文本带入 DTO。
            raise ValueError("output safety scan id invalid")
        reversal_eligible = _sqlite_boolean(
            row["output_safety_dispute_reversal_eligible"]
        )
        valid = (
            entry_type == "usage_charge"
            and row["output_safety_dispute_tenant_id"] == row["tenant_id"]
            and row["output_safety_dispute_request_id"] == row["source_request_id"]
            and row["output_safety_dispute_trace_id"] == row["source_trace_id"]
            and row["output_safety_dispute_usage_ledger_id"] == row["usage_ledger_id"]
            and row["output_safety_dispute_entry_id"] == row["finops_ledger_entry_id"]
            and row["output_safety_dispute_review_status"] == "dispute_pending"
            and row["output_safety_dispute_reason_code"]
            == "output_safety.content_policy_violation"
            and row["output_safety_scan_tenant_id"] == row["tenant_id"]
            and row["output_safety_scan_request_id"] == row["source_request_id"]
            and row["output_safety_scan_trace_id"] == row["source_trace_id"]
            and row["output_safety_owner_scan_id"] == scan_id
            and row["output_safety_scan_status"] == "completed"
            and row["output_safety_scan_decision"] == "blocked"
            and row["output_safety_scan_billing_disposition"] == "dispute_pending"
            and row["output_safety_usage_tenant_id"] == row["tenant_id"]
            and row["output_safety_usage_request_id"] == row["source_request_id"]
            and row["output_safety_owner_usage_ledger_id"] == row["usage_ledger_id"]
            and reversal_eligible == (amount_micros > 0)
        )
        if not valid:
            # 任一 owner、终态或金额资格不一致都使用统一完整性错误失败关闭。
            raise ApiError("output_safety.integrity_failed")
        return FinOpsOutputSafetyDisputeInfo(
            output_safety_scan_id=scan_id,
            review_status="dispute_pending",
            reversal_eligible=reversal_eligible,
        )
    # 已归一的完整性错误保持专用 503，不覆盖成普通账本不可用。
    except ApiError:
        # 原错误正文为固定目录文案，可安全原样向上交给统一异常处理器。
        raise
    except Exception as exc:
        # 类型或 DTO 恢复异常可能携带内部值，只保留统一错误码。
        raise ApiError("output_safety.integrity_failed") from exc


def _prefixed_hex_id(value: str, *, prefix: str, body_length: int) -> bool:
    """判断内部 ID 是否为固定前缀和小写十六进制主体。"""

    if not value.startswith(prefix) or len(value) != len(prefix) + body_length:
        return False
    return all(character in "0123456789abcdef" for character in value[len(prefix) :])


def _adjustment_info(row: sqlite3.Row) -> FinOpsAdjustmentInfo:
    """把 AdjustmentRecord 转为公开 DTO，不返回原因摘要或幂等摘要。"""

    currency = _database_enum(row["currency"], CURRENCIES, "finops adjustment currency invalid")
    adjustment_type = _database_enum(
        row["adjustment_type"], ADJUSTMENT_TYPES, "finops adjustment type invalid"
    )
    _database_enum(row["status"], LEDGER_STATUSES, "finops adjustment status invalid")
    return FinOpsAdjustmentInfo(
        finops_adjustment_id=_required_db_identifier(row["finops_adjustment_id"]),
        tenant_id=_required_db_identifier(row["tenant_id"]),
        adjustment_type=adjustment_type,
        amount=_format_micros(_positive_integer(row["amount_micros"])),
        currency=currency,
        reversal_of_entry_id=_optional_db_identifier(row["reversal_of_entry_id"]),
        related_request_id=_optional_db_identifier(row["related_request_id"]),
        reason=_required_db_text(row["reason"]),
        actor_user_id=_required_db_identifier(row["actor_user_id"]),
        operation_request_id=_required_db_identifier(row["operation_request_id"]),
        operation_trace_id=_required_db_identifier(row["operation_trace_id"]),
        status="posted",
        created_at=_parse_db_time(row["created_at"]),
    )


def _ledger_event(row: sqlite3.Row) -> _LedgerEventRecord:
    """从权威分录行构造 canonical `ledger.entry_created` 安全载荷。"""

    info = _ledger_entry_info(row)
    return _LedgerEventRecord(
        event_id=_required_db_identifier(row["event_id"]),
        tenant_id=info.tenant_id,
        ledger_entry_id=info.finops_ledger_entry_id,
        actor_user_id=info.actor.user_id if info.actor else None,
        trace_id=info.operation_trace_id,
        created_at=info.created_at,
        payload={
            "ledger_entry_id": info.finops_ledger_entry_id,
            "entry_type": info.entry_type,
            "direction": info.direction,
            "amount": info.amount,
            "currency": info.currency,
            "normalized_cny": info.normalized_cny,
            "cny_normalization_status": info.cny_normalization_status,
            "quota_type": info.quota_type,
            "source_request_id": info.source_request_id,
            "price_snapshot_id": info.price_snapshot_id,
            "price_schema_version": info.price_schema_version,
            "price_snapshot_source": info.price_snapshot_source,
            "price_evidence_status": info.price_evidence_status,
            "reversal_of_entry_id": info.reversal_of_entry_id,
        },
    )


def _dispatch_ledger_event(
    config: AppConfig,
    event: _LedgerEventRecord,
    request_id: str,
) -> None:
    """提交后尽力投递账本事件；外部故障不得改变已确认财务事实。"""

    try:
        envelope = build_webhook_event(
            tenant_id=event.tenant_id,
            event_type="ledger.entry_created",
            event_id=event.event_id,
            timestamp=event.created_at,
            actor_id=event.actor_user_id,
            object_id=event.ledger_entry_id,
            trace_id=event.trace_id,
            payload=event.payload,
        )
        # 延迟导入避免 FinOps、Audit 和 Webhook 在应用启动时形成循环依赖。
        from metahub_control.webhooks.service import dispatch_webhook_event

        dispatch_webhook_event(
            config,
            event.tenant_id,
            envelope,
            request_id=request_id,
            trace_id=event.trace_id,
        )
    except Exception:
        # 接收端 URL、正文和异常可能敏感，只记录稳定定位字段与统一错误码。
        logger.error(
            "finops.ledger_webhook_dispatch_failed",
            extra={
                "event": "finops.ledger_webhook_dispatch_failed",
                "tenant_id": event.tenant_id,
                "actor_id": event.actor_user_id,
                "object_id": event.ledger_entry_id,
                "request_id": request_id,
                "trace_id": event.trace_id,
                "error_code": "webhook.delivery_unavailable",
            },
        )


def _encode_cursor(
    config: AppConfig,
    tenant_id: str,
    filter_hash: str,
    *,
    snapshot_sequence: int,
    before_sequence: int,
) -> str:
    """生成绑定版本、租户、筛选和水位的 HMAC opaque cursor。"""

    payload = json.dumps(
        {
            "before": before_sequence,
            "filters": filter_hash,
            "snapshot": snapshot_sequence,
            "tenant": tenant_id,
            "v": CURSOR_VERSION,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    signature = hmac.new(
        config.auth_token_secret.encode("utf-8"),
        CURSOR_DOMAIN + b"\x00" + payload,
        hashlib.sha256,
    ).digest()
    return f"{_base64_encode(payload)}.{_base64_encode(signature)}"


def _decode_cursor(
    config: AppConfig,
    tenant_id: str,
    filter_hash: str,
    cursor: str,
) -> _CursorState:
    """验证 opaque cursor 的 HMAC、租户、筛选和值域，任何差异统一返回 422。"""

    try:
        encoded_payload, encoded_signature = cursor.split(".", 1)
        payload = _base64_decode(encoded_payload)
        signature = _base64_decode(encoded_signature)
        expected = hmac.new(
            config.auth_token_secret.encode("utf-8"),
            CURSOR_DOMAIN + b"\x00" + payload,
            hashlib.sha256,
        ).digest()
        if len(signature) != hashlib.sha256().digest_size or not hmac.compare_digest(
            signature, expected
        ):
            # 签名长度和常量时间比对必须同时通过，任何篡改都不能进入游标载荷解析。
            raise ValueError("cursor signature invalid")
        parsed = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_json_object)
        if not isinstance(parsed, dict) or set(parsed) != {
            "before",
            "filters",
            "snapshot",
            "tenant",
            "v",
        }:
            # 游标只接受冻结字段集合，未知或缺失字段不能被旧版本静默解释。
            raise ValueError("cursor shape invalid")
        if (
            type(parsed["v"]) is not int
            or parsed["v"] != CURSOR_VERSION
            or type(parsed["tenant"]) is not str
            or not hmac.compare_digest(parsed["tenant"], tenant_id)
            or type(parsed["filters"]) is not str
            or not hmac.compare_digest(parsed["filters"], filter_hash)
        ):
            # 版本、租户和筛选摘要必须全部绑定，防止游标跨租户或跨查询复用。
            raise ValueError("cursor binding invalid")
        snapshot = _non_negative_integer(parsed["snapshot"])
        before = _positive_integer(parsed["before"])
        # 页水位只能落在已签名快照内，避免构造未来序号跳过或扩展可见范围。
        if snapshot < 1 or before > snapshot:
            raise ValueError("cursor range invalid")
        return _CursorState(snapshot_sequence=snapshot, before_sequence=before)
    # 编码、JSON、签名或值域错误统一收敛为查询无效，不暴露游标内部结构。
    except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ApiError("finops.query_invalid") from exc


def _query_fingerprint(query: _LedgerQuery) -> str:
    """为规范筛选生成稳定 SHA-256，游标不携带可篡改的完整查询。"""

    canonical = json.dumps(
        {
            "direction": query.direction,
            "end_date": query.end_date.isoformat() if query.end_date else None,
            "entry_type": query.entry_type,
            "price_snapshot_id": query.price_snapshot_id,
            "quota_type": query.quota_type,
            "related_entry_id": query.related_entry_id,
            "request_id": query.source_request_id,
            "start_date": query.start_date.isoformat() if query.start_date else None,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _parse_amount(value: object) -> tuple[int, str]:
    """把普通十进制字符串精确转换为微元，并返回固定六位规范文本。"""

    # 金额必须是无首尾空白的原始字符串，禁止 JSON 数字先经过浮点表示。
    if type(value) is not str or not value or value != value.strip():
        raise ApiError("finops.amount_invalid")
    # 多个小数点不存在唯一十进制语义，必须在进入 Decimal 前明确拒绝。
    if value.count(".") > 1:
        raise ApiError("finops.amount_invalid")
    integer_text, separator, fraction_text = value.partition(".")
    if (
        not integer_text
        or len(integer_text) > 10
        or any(character < "0" or character > "9" for character in integer_text)
        or (len(integer_text) > 1 and integer_text.startswith("0"))
        or (separator and (not fraction_text or len(fraction_text) > 6))
        or any(character < "0" or character > "9" for character in fraction_text)
    ):
        # 仅接受无前导零且最多六位小数的 ASCII 定点格式，保证微元换算唯一。
        raise ApiError("finops.amount_invalid")
    try:
        decimal_value = Decimal(value)
    except InvalidOperation as exc:
        # Decimal 解析异常也映射为统一金额错误，不能向外泄露运行库细节。
        raise ApiError("finops.amount_invalid") from exc
    micros = int(integer_text) * MICROS_PER_UNIT + int((fraction_text + "000000")[:6] or "0")
    # 调整金额必须为正且受业务上限约束，防止零分录与 SQLite 整数溢出。
    if decimal_value <= 0 or micros < 1 or micros > MAX_AMOUNT_MICROS:
        raise ApiError("finops.amount_invalid")
    return micros, _format_micros(micros)


def _optional_date(value: str | None) -> date | None:
    """解析严格 ASCII ISO 日期；无值表示不限制对应边界。"""

    if value is None:
        return None
    if (
        len(value) != 10
        or value[4] != "-"
        or value[7] != "-"
        or any(value[index] < "0" or value[index] > "9" for index in (*range(4), 5, 6, 8, 9))
    ):
        # 日期 wire 格式必须固定为 ASCII YYYY-MM-DD，禁止宽松解析或隐式归一。
        raise ApiError("finops.query_invalid")
    try:
        return date.fromisoformat(value)
    # 格式正确但日历值不存在时仍按查询无效收敛，不能自动滚动到其他日期。
    except ValueError as exc:
        raise ApiError("finops.query_invalid") from exc


def _optional_enum(value: str | None, allowed: frozenset[str]) -> str | None:
    """校验可选枚举并保留精确 wire 值，禁止隐式大小写或空白归一。"""

    if value is None:
        return None
    # 枚举值必须与冻结合同精确一致，大小写或空白差异不能隐式修正。
    if value not in allowed:
        raise ApiError("finops.query_invalid")
    return value


def _required_identifier(value: object, error_code: str) -> str:
    """校验必填标识为有界、无空白和控制字符的原样文本。"""

    if (
        type(value) is not str
        or not 1 <= len(value) <= MAX_IDENTIFIER_LENGTH
        or value != value.strip()
        or any(character.isspace() or ord(character) < 32 or 127 <= ord(character) <= 159 for character in value)
    ):
        # 标识会进入租户过滤与审计关联，空白及控制字符可能制造多义或日志污染。
        raise ApiError(error_code)
    return value


def _optional_identifier(value: object, error_code: str) -> str | None:
    """校验可空资源标识；显式空串不等同于未提供。"""

    if value is None:
        return None
    return _required_identifier(value, error_code)


def _format_micros(value: int) -> str:
    """把任意精度有符号微元整数格式化为固定六位十进制文本。"""

    # bool 也是 int 子类但不是合法金额事实，因此必须使用精确类型判断。
    if type(value) is not int:
        raise ValueError("finops micros type invalid")
    sign = "-" if value < 0 else ""
    whole, fraction = divmod(abs(value), MICROS_PER_UNIT)
    return f"{sign}{whole}.{fraction:06d}"


def _positive_integer(value: object) -> int:
    """恢复 SQLite 正整数并拒绝 bool、REAL、负值和越界事实。"""

    # 正序号必须落在 SQLite 有符号整数范围内，类型漂移一律视为事实损坏。
    if type(value) is not int or value < 1 or value > 9_223_372_036_854_775_807:
        raise ValueError("finops positive integer invalid")
    return value


def _non_negative_integer(value: object) -> int:
    """恢复 SQLite 非负整数并拒绝 bool、REAL、负值和越界事实。"""

    # 金额与计数不得为负且不能接受 bool 或 REAL，避免损坏事实进入汇总。
    if type(value) is not int or value < 0 or value > 9_223_372_036_854_775_807:
        raise ValueError("finops non-negative integer invalid")
    return value


def _sqlite_boolean(value: object) -> bool:
    """把受 CHECK 保护的 SQLite 0/1 转为布尔值，损坏值失败关闭。"""

    # 只有整数零和一是可恢复布尔事实，其他真值不能被 Python 隐式转换掩盖。
    if type(value) is not int or value not in {0, 1}:
        raise ValueError("finops boolean invalid")
    return bool(value)


def _required_db_text(value: object) -> str:
    """恢复必填数据库文本，拒绝类型漂移、空值和控制字符。"""

    if (
        type(value) is not str
        or not value
        or any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value)
    ):
        # 持久文本缺失、类型漂移或含控制字符表示数据库事实已损坏，不能宽松恢复。
        raise ValueError("finops text invalid")
    return value


def _optional_db_text(value: object) -> str | None:
    """恢复可空数据库文本；非文本、空文本或控制字符视为持久事实损坏。"""

    if value is None:
        return None
    return _required_db_text(value)


def _required_db_identifier(value: object) -> str:
    """恢复必填持久标识，拒绝空白、DEL/C1 和异常长度。"""

    normalized = _required_db_text(value)
    if (
        len(normalized) > MAX_IDENTIFIER_LENGTH
        or normalized != normalized.strip()
        or any(character.isspace() for character in normalized)
    ):
        # 数据库标识必须保持唯一规范形式，超长或空白变体不能进入租户关联字段。
        raise ValueError("finops identifier invalid")
    return normalized


def _optional_db_identifier(value: object) -> str | None:
    """恢复可空持久标识，非空值必须满足统一标识边界。"""

    if value is None:
        return None
    return _required_db_identifier(value)


def _database_enum(value: object, allowed: frozenset[str], message: str) -> str:
    """恢复数据库枚举，禁止用 str() 把错误存储类型伪装成合法文本。"""

    # 未知枚举或非字符串类型代表迁移/数据不一致，禁止猜测默认值继续响应。
    if type(value) is not str or value not in allowed:
        raise ValueError(message)
    return value


def _price_evidence_status(value: object) -> str:
    """恢复价格证据状态，拒绝未知枚举掩盖未定价或 legacy 事实。"""

    return _database_enum(value, PRICE_EVIDENCE_STATUSES, "finops price evidence invalid")


def _quota_type(value: object) -> str:
    """恢复额度类型，未知值不能进入 API、冲正或事件。"""

    return _database_enum(value, QUOTA_TYPES, "finops quota type invalid")


def _base64_encode(value: bytes) -> str:
    """把游标字节编码为无 padding 的 URL-safe Base64，保证游标只有一种规范文本。"""

    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64_decode(value: str) -> bytes:
    """严格解码 URL-safe Base64，并拒绝超长、空值和非规范编码。"""

    if not value or len(value) > 2048 or any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        for character in value
    ):
        # 游标片段只接受有界 URL-safe 字符集，避免宽松解码器忽略非法输入。
        raise ValueError("cursor base64 invalid")
    padded = value + "=" * (-len(value) % 4)
    decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
    # 解码后必须能还原完全相同文本，拒绝多种编码指向同一签名载荷。
    if _base64_encode(decoded) != value:
        raise ValueError("cursor base64 non-canonical")
    return decoded


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """解析签名 JSON 时拒绝重复键，避免签名内容出现多义解释。"""

    result: dict[str, object] = {}
    for key, value in pairs:
        # 重复键会让签名载荷产生 first-win/last-win 歧义，因此必须失败关闭。
        if key in result:
            raise ValueError("duplicate cursor key")
        result[key] = value
    return result


def _parse_db_time(value: object) -> datetime:
    """解析数据库 UTC 时间；无时区历史值按仓库 UTC 约定恢复。"""

    # 时间字段必须保持文本存储合同，其他 SQLite 类型不能被隐式字符串化。
    if type(value) is not str:
        raise ValueError("finops timestamp invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # 非法日历或 ISO 文本表示持久事实损坏，统一转为稳定的内部值错误。
    except (TypeError, ValueError) as exc:
        raise ValueError("finops timestamp invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _to_db_time(value: datetime) -> str:
    """把可信时刻转换为秒级 UTC `Z` 文本。"""

    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utcnow() -> datetime:
    """返回秒级 UTC 当前时刻，便于账本、审计和事件稳定关联。"""

    return datetime.now(UTC).replace(microsecond=0)


def _connect(config: AppConfig) -> sqlite3.Connection:
    """建立控制面数据库连接，并隐藏数据库 URL 与驱动异常。"""

    try:
        return connect_database(config.database_url)
    # 数据库 URL 与驱动异常均可能含敏感信息，只允许向外返回统一可用性错误。
    except DbError as exc:
        raise ApiError("finops.ledger_unavailable") from exc


def _safe_rollback(connection: sqlite3.Connection) -> None:
    """尽力回滚失败事务，回滚异常不得覆盖原领域错误。"""

    try:
        connection.rollback()
    # 回滚本身失败时保留最初领域异常，避免清理错误覆盖真正失败原因。
    except sqlite3.Error:
        return


def _log_adjustment_created(
    context: AuthContext,
    entry: FinOpsLedgerEntryInfo,
    request_id: str,
    trace_id: str,
) -> None:
    """记录不含原因、幂等键或请求正文的安全管理摘要。"""

    logger.info(
        "finops.adjustment_created",
        extra={
            "event": "finops.adjustment_created",
            "tenant_id": context.user.tenant_id,
            "actor_id": context.user.user_id,
            "object_id": entry.finops_adjustment_id,
            "request_id": request_id,
            "trace_id": trace_id,
            "payload": {
                "ledger_entry_id": entry.finops_ledger_entry_id,
                "entry_type": entry.entry_type,
                "currency": entry.currency,
                "price_evidence_status": entry.price_evidence_status,
            },
        },
    )


__all__ = ["create_finops_adjustment", "list_finops_ledger"]
