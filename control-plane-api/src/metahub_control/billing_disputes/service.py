"""PRD-080 租户限定的账单争议工作流服务。

读路径使用 tenant_id 与签名双水位游标；写路径在 ``BEGIN IMMEDIATE`` 事务内重新校验权限，并把
争议、冻结费用、hold、FinOps 冲正、Audit 与 source outbox 原子提交。金额始终以整数微单位
计算，Prompt、Response、Token、Key 和自由文本理由不会进入日志或 Webhook/Audit 摘要。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal
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
from metahub_control.auth import AuthContext, require_transaction_permission
from metahub_control.finops.service import (
    FinOpsAdjustmentWriteResult,
    dispatch_finops_adjustment_write,
    write_finops_adjustment_in_transaction,
)

from .models import (
    BillingDisputeActorInfo,
    BillingDisputeAdjustmentLinkInfo,
    BillingDisputeCreateRequest,
    BillingDisputeDetail,
    BillingDisputeDetailResponse,
    BillingDisputeEventInfo,
    BillingDisputeItemInfo,
    BillingDisputeListResponse,
    BillingDisputeMutationResponse,
    BillingDisputePatchRequest,
    BillingDisputeSummary,
    BillingDisputeTransitionInfo,
    BillingSettlementHoldInfo,
)

CREATE_DOMAIN = b"billing-dispute-create-v1"
TRANSITION_DOMAIN = b"billing-dispute-transition-v1"
CURSOR_DOMAIN = b"metahub.billing-dispute.cursor.v1"
CURSOR_VERSION = 1
MAX_AMOUNT_MICROS = 1_000_000_000 * 1_000_000
_AMOUNT = re.compile(r"^(?:0|[1-9][0-9]{0,9})(?:\.[0-9]{1,6})?$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
_DISPUTE_ID = re.compile(r"^bdsp_[0-9a-f]{32}$")
_ITEM_ID = re.compile(r"^bdsi_[0-9a-f]{32}$")
_UTC_SECONDS = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_STATUSES = frozenset({"submitted", "under_review", "approved", "rejected", "cancelled"})
_SCOPES = frozenset({"request_ids", "time_range"})
_CURRENCIES = frozenset({"CNY", "USD"})


@dataclass(frozen=True)
class _CreateIntent:
    """绑定认证 actor 的规范创建意图；明文理由仅用于授权详情与持久业务记录。"""

    scope_type: Literal["request_ids", "time_range"]
    request_ids: tuple[str, ...]
    period_start: str | None
    period_end: str | None
    reason: str
    reason_sha256: str
    payload_sha256: str


@dataclass(frozen=True)
class _ApprovedAmount:
    """单项批准金额的规范字符串与整数微单位表示。"""

    item_id: str
    amount_micros: int
    amount: str


@dataclass(frozen=True)
class _PatchIntent:
    """绑定争议、actor、期望修订号与排序调整项的幂等迁移意图。"""

    action: Literal["start_review", "approve", "reject", "cancel"]
    expected_revision: int
    decision_reason: str | None
    decision_reason_sha256: str | None
    adjustments: tuple[_ApprovedAmount, ...]
    payload_sha256: str


@dataclass(frozen=True)
class _ListQuery:
    """白名单化且规范时间边界明确的列表筛选。"""

    status: str | None
    scope_type: str | None
    created_from: str | None
    created_to: str | None
    limit: int


@dataclass(frozen=True)
class _Cursor:
    """同时冻结争议集合和迁移历史的租户内列表坐标。"""

    dispute_watermark: int
    transition_watermark: int
    before_sequence: int


def list_billing_disputes(
    config: AppConfig,
    context: AuthContext,
    *,
    query: dict[str, str],
    request_id: str,
    trace_id: str,
) -> BillingDisputeListResponse:
    """列出认证租户的不可变双水位视图，并在事务内复核实时读取权限。"""

    normalized = _normalize_list_query(query)
    filter_hash = _sha256_json(
        {
            "status": normalized.status,
            "scope_type": normalized.scope_type,
            "created_from": normalized.created_from,
            "created_to": normalized.created_to,
        }
    )
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        # 所有读写 SQL 都显式携带 tenant_id；事务内权限复核阻断认证后即时撤权的旧上下文。
        live = require_transaction_permission(
            connection,
            context,
            "billing.dispute.read",
            unavailable_error_code="billing_dispute.unavailable",
        )
        raw_cursor = query.get("cursor")
        if raw_cursor is None:
            dispute_watermark = _scalar_int(
                connection,
                """SELECT COALESCE(MAX(billing_dispute_sequence),0)
                   FROM billing_disputes WHERE tenant_id=?""",
                (live.user.tenant_id,),
            )
            transition_watermark = _scalar_int(
                connection,
                """SELECT COALESCE(MAX(billing_dispute_transition_sequence),0)
                   FROM billing_dispute_transitions WHERE tenant_id=?""",
                (live.user.tenant_id,),
            )
            cursor_state = _Cursor(
                dispute_watermark=dispute_watermark,
                transition_watermark=transition_watermark,
                before_sequence=dispute_watermark + 1,
            )
        else:
            cursor_state = _decode_cursor(
                config,
                live.user.tenant_id,
                filter_hash,
                raw_cursor,
            )
        clauses = [
            "d.tenant_id=?",
            "d.billing_dispute_sequence<=?",
            "d.billing_dispute_sequence<?",
        ]
        params: list[object] = [
            live.user.tenant_id,
            cursor_state.dispute_watermark,
            cursor_state.before_sequence,
        ]
        if normalized.status is not None:
            clauses.append("latest.to_status=?")
            params.append(normalized.status)
        if normalized.scope_type is not None:
            clauses.append("d.scope_type=?")
            params.append(normalized.scope_type)
        if normalized.created_from is not None:
            clauses.append("unixepoch(d.created_at)>=unixepoch(?)")
            params.append(normalized.created_from)
        if normalized.created_to is not None:
            clauses.append("unixepoch(d.created_at)<unixepoch(?)")
            params.append(normalized.created_to)
        rows = connection.execute(
            f"""
            WITH latest AS (
              SELECT t.*,
                ROW_NUMBER() OVER (
                  PARTITION BY t.tenant_id,t.billing_dispute_id
                  ORDER BY t.billing_dispute_transition_sequence DESC
                ) AS row_number
              FROM billing_dispute_transitions t
              WHERE t.tenant_id=? AND t.billing_dispute_transition_sequence<=?
            )
            SELECT d.*,latest.to_status AS snapshot_status,
              latest.new_revision AS snapshot_revision,
              latest.actor_user_id AS snapshot_actor_user_id,
              latest.action AS snapshot_action,
              latest.created_at AS snapshot_updated_at
            FROM billing_disputes d
            JOIN latest
              ON latest.tenant_id=d.tenant_id
             AND latest.billing_dispute_id=d.billing_dispute_id
             AND latest.row_number=1
            WHERE {" AND ".join(clauses)}
            ORDER BY d.billing_dispute_sequence DESC
            LIMIT ?
            """,
            (
                live.user.tenant_id,
                cursor_state.transition_watermark,
                *params,
                normalized.limit + 1,
            ),
        ).fetchall()
        visible = rows[: normalized.limit]
        summaries = [
            _summary(
                connection,
                row,
                transition_watermark=cursor_state.transition_watermark,
                snapshot=True,
            )
            for row in visible
        ]
        has_more = len(rows) > normalized.limit
        next_cursor = None
        if has_more and visible:
            next_cursor = _encode_cursor(
                config,
                live.user.tenant_id,
                filter_hash,
                _Cursor(
                    dispute_watermark=cursor_state.dispute_watermark,
                    transition_watermark=cursor_state.transition_watermark,
                    before_sequence=_positive_int(visible[-1]["billing_dispute_sequence"]),
                ),
            )
        connection.commit()
        return BillingDisputeListResponse(
            billing_disputes=summaries,
            limit=normalized.limit,
            next_cursor=next_cursor,
            has_more=has_more,
            request_id=_identifier(request_id),
            trace_id=_identifier(trace_id),
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, TypeError, ValueError, ArithmeticError) as exc:
        _rollback(connection)
        raise ApiError("billing_dispute.integrity_failed") from exc
    finally:
        connection.close()


def get_billing_dispute(
    config: AppConfig,
    context: AuthContext,
    billing_dispute_id: str,
    *,
    request_id: str,
    trace_id: str,
) -> BillingDisputeDetailResponse:
    """返回当前同租户争议及完整授权事实；跨租户标识按不存在处理。"""

    dispute_id = _dispute_id(billing_dispute_id)
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        live = require_transaction_permission(
            connection,
            context,
            "billing.dispute.read",
            unavailable_error_code="billing_dispute.unavailable",
        )
        detail = _detail(connection, live.user.tenant_id, dispute_id)
        connection.commit()
        return BillingDisputeDetailResponse(
            dispute=detail,
            request_id=_identifier(request_id),
            trace_id=_identifier(trace_id),
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, TypeError, ValueError, ArithmeticError) as exc:
        _rollback(connection)
        raise ApiError("billing_dispute.integrity_failed") from exc
    finally:
        connection.close()


def create_billing_dispute(
    config: AppConfig,
    context: AuthContext,
    payload: BillingDisputeCreateRequest,
    *,
    idempotency_key: str,
    audit_request_meta: AuditRequestMeta,
    request_id: str,
    trace_id: str,
) -> BillingDisputeMutationResponse:
    """原子冻结合格费用、hold、创建事件、投递快照和安全审计事实。"""

    normalized_request_id = _identifier(request_id)
    normalized_trace_id = _identifier(trace_id)
    key_hash = _domain_hash(CREATE_DOMAIN, idempotency_key.encode("ascii"))
    intent = _normalize_create(payload, actor_user_id=context.user.user_id)
    connection = _connect(config)
    audit_record: AuditRecord | None = None
    response: BillingDisputeMutationResponse | None = None
    try:
        # BEGIN IMMEDIATE 后事务内重新校验权限，避免撤权与费用冻结之间出现 TOCTOU。
        connection.execute("BEGIN IMMEDIATE")
        live = require_transaction_permission(
            connection,
            context,
            "billing.dispute.read",
            unavailable_error_code="billing_dispute.unavailable",
        )
        live = require_transaction_permission(
            connection,
            live,
            "billing.dispute.create",
            unavailable_error_code="billing_dispute.unavailable",
        )
        recovered = connection.execute(
            """SELECT * FROM billing_disputes
               WHERE tenant_id=? AND create_idempotency_sha256=?""",
            (live.user.tenant_id, key_hash),
        ).fetchone()
        if recovered is not None:
            # 幂等 key 只保存分域摘要；同 key 异意图失败，重放不会新增 hold 或事件。
            if not hmac.compare_digest(str(recovered["payload_sha256"]), intent.payload_sha256):
                raise ApiError("billing_dispute.idempotency_conflict")
            detail = _detail(
                connection,
                live.user.tenant_id,
                _text(recovered["billing_dispute_id"]),
            )
            transition = detail.transitions[0]
            connection.rollback()
            return BillingDisputeMutationResponse(
                dispute=detail,
                transition=transition,
                adjustment_links=[],
                idempotent_replay=True,
                request_id=normalized_request_id,
                trace_id=normalized_trace_id,
            )
        now = _database_now(connection)
        entries = _freeze_scope(connection, live.user.tenant_id, intent, now=now)
        dispute_id = f"bdsp_{uuid4().hex}"
        transition_id = f"bdtr_{uuid4().hex}"
        currency_totals = _currency_totals(entries, amount_key="disputable_micros")
        connection.execute(
            """
            INSERT INTO billing_disputes(
              billing_dispute_id,tenant_id,scope_type,period_start,period_end,status,
              state_revision,item_count,reason,reason_sha256,created_by_user_id,
              reviewer_user_id,decision_reason,decision_reason_sha256,
              create_request_id,create_trace_id,create_idempotency_sha256,payload_sha256,
              review_started_at,resolved_at,created_at,updated_at
            ) VALUES(?,?,?,?,?,'submitted',1,?,?,?,?,NULL,NULL,NULL,?,?,?,?,NULL,NULL,?,?)
            """,
            (
                dispute_id,
                live.user.tenant_id,
                intent.scope_type,
                intent.period_start,
                intent.period_end,
                len(entries),
                intent.reason,
                intent.reason_sha256,
                live.user.user_id,
                normalized_request_id,
                normalized_trace_id,
                key_hash,
                intent.payload_sha256,
                now,
                now,
            ),
        )
        item_ids: list[str] = []
        for entry in entries:
            item_id = f"bdsi_{uuid4().hex}"
            item_ids.append(item_id)
            connection.execute(
                """
                INSERT INTO billing_dispute_items(
                  billing_dispute_item_id,tenant_id,billing_dispute_id,
                  finops_ledger_entry_id,usage_ledger_id,source_request_id,source_trace_id,
                  ledger_sequence,currency,amount_micros,disputable_micros,
                  price_snapshot_id,price_schema_version,price_snapshot_source,
                  price_evidence_status,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    item_id,
                    live.user.tenant_id,
                    dispute_id,
                    entry["finops_ledger_entry_id"],
                    entry["usage_ledger_id"],
                    entry["source_request_id"],
                    entry["source_trace_id"],
                    entry["ledger_sequence"],
                    entry["currency"],
                    entry["amount_micros"],
                    entry["disputable_micros"],
                    entry["price_snapshot_id"],
                    entry["price_schema_version"],
                    entry["price_snapshot_source"],
                    entry["price_evidence_status"],
                    now,
                ),
            )
        connection.execute(
            """
            INSERT INTO billing_dispute_transitions(
              billing_dispute_transition_id,tenant_id,billing_dispute_id,action,
              from_status,to_status,expected_revision,new_revision,actor_user_id,
              decision_reason_sha256,approved_item_count,approved_totals_sha256,
              operation_request_id,operation_trace_id,idempotency_key_sha256,
              payload_sha256,created_at
            ) VALUES(?,?,?,'create',NULL,'submitted',0,1,?,NULL,0,NULL,?,?,?,?,?)
            """,
            (
                transition_id,
                live.user.tenant_id,
                dispute_id,
                live.user.user_id,
                normalized_request_id,
                normalized_trace_id,
                key_hash,
                intent.payload_sha256,
                now,
            ),
        )
        for item_id, entry in zip(item_ids, entries, strict=True):
            # active hold 由数据库唯一约束兜底；显式检查只负责稳定领域错误映射。
            conflict = connection.execute(
                """SELECT 1 FROM billing_settlement_holds
                   WHERE tenant_id=? AND finops_ledger_entry_id=? AND status='active'""",
                (live.user.tenant_id, entry["finops_ledger_entry_id"]),
            ).fetchone()
            if conflict is not None:
                raise ApiError("billing_dispute.hold_conflict")
            connection.execute(
                """
                INSERT INTO billing_settlement_holds(
                  billing_settlement_hold_id,tenant_id,billing_dispute_id,
                  billing_dispute_item_id,finops_ledger_entry_id,status,release_reason,
                  state_revision,created_transition_id,released_transition_id,
                  created_at,released_at,updated_at
                ) VALUES(?,?,?,?,?,'active',NULL,1,?,NULL,?,NULL,?)
                """,
                (
                    f"bhold_{uuid4().hex}",
                    live.user.tenant_id,
                    dispute_id,
                    item_id,
                    entry["finops_ledger_entry_id"],
                    transition_id,
                    now,
                    now,
                ),
            )
        event_id = f"bdevt_{uuid4().hex}"
        event_payload_value = {
            "schema_version": 1,
            "billing_dispute_id": dispute_id,
            "scope_type": intent.scope_type,
            "item_count": len(entries),
            "currency_totals": currency_totals,
            "status": "submitted",
            "created_at": now,
        }
        event_payload = _canonical_json(event_payload_value)
        connection.execute(
            """
            INSERT INTO billing_dispute_events(
              billing_dispute_event_id,tenant_id,billing_dispute_id,event_type,
              payload_json,actor_user_id,source_request_id,source_trace_id,
              occurred_at,created_at
            ) VALUES(?,?,?,'billing_dispute.created',?,?,?,?,?,?)
            """,
            (
                event_id,
                live.user.tenant_id,
                dispute_id,
                event_payload,
                live.user.user_id,
                normalized_request_id,
                normalized_trace_id,
                now,
                now,
            ),
        )
        event = build_webhook_event(
            tenant_id=live.user.tenant_id,
            event_type="billing_dispute.created",
            event_id=event_id,
            timestamp=_parse_time(now),
            actor_id=live.user.user_id,
            object_id=dispute_id,
            trace_id=normalized_trace_id,
            source_request_id=normalized_request_id,
            payload=event_payload_value,
        )
        from metahub_control.webhooks.service import freeze_webhook_event_delivery_snapshot

        # 投递快照与 source 同事务冻结；创建后新增的 Endpoint 不得接收这条历史事件。
        freeze_webhook_event_delivery_snapshot(connection, event, frozen_at=now)
        delivery_rows = connection.execute(
            """SELECT webhook_delivery_id FROM webhook_deliveries
               WHERE tenant_id=? AND event_id=? AND delivery_kind='original'
               ORDER BY webhook_delivery_id""",
            (live.user.tenant_id, event_id),
        ).fetchall()
        delivery_snapshot = _canonical_json(
            [_text(row["webhook_delivery_id"]) for row in delivery_rows]
        )
        connection.execute(
            """
            INSERT INTO billing_dispute_event_outbox(
              billing_dispute_event_outbox_id,tenant_id,billing_dispute_event_id,
              status,attempt_count,lease_generation,lease_owner_id,lease_token,
              lease_expires_at,next_attempt_at,delivery_snapshot_json,
              snapshot_frozen_at,last_error_code,succeeded_at,created_at,updated_at
            ) VALUES(?,?,?,'pending',0,0,NULL,NULL,NULL,?,?,?,NULL,NULL,?,?)
            """,
            (
                f"bdob_{uuid4().hex}",
                live.user.tenant_id,
                event_id,
                now,
                delivery_snapshot,
                now,
                now,
                now,
            ),
        )
        audit_record = record_audit_log(
            connection,
            live,
            action="billing_dispute.created",
            object_type="billing_dispute",
            object_id=dispute_id,
            object_name=None,
            before_summary={},
            after_summary=_audit_summary(
                status="submitted",
                revision=1,
                scope_type=intent.scope_type,
                item_count=len(entries),
                adjustment_count=0,
                totals=currency_totals,
                reason_sha256=intent.reason_sha256,
            ),
            request_meta=audit_request_meta,
            request_id=normalized_request_id,
            trace_id=normalized_trace_id,
        )
        if audit_record is None:
            raise ApiError("billing_dispute.integrity_failed")
        detail = _detail(connection, live.user.tenant_id, dispute_id)
        response = BillingDisputeMutationResponse(
            dispute=detail,
            transition=detail.transitions[0],
            adjustment_links=[],
            idempotent_replay=False,
            request_id=normalized_request_id,
            trace_id=normalized_trace_id,
        )
        commit_audited_transaction(connection)
    except ApiError as exc:
        _rollback(connection)
        raise _map_domain_error(exc) from exc
    except sqlite3.OperationalError as exc:
        _rollback(connection)
        raise ApiError("billing_dispute.unavailable") from exc
    except (sqlite3.Error, TypeError, ValueError, ArithmeticError) as exc:
        _rollback(connection)
        raise ApiError("billing_dispute.integrity_failed") from exc
    finally:
        connection.close()
    if response is None or audit_record is None:
        raise ApiError("billing_dispute.integrity_failed")
    # 事务提交后才触发 Audit 网络投递；失败可由其持久 outbox 恢复，不能回滚已提交业务事实。
    dispatch_audit_recorded(
        config,
        context,
        audit_record,
        request_id=normalized_request_id,
        trace_id=normalized_trace_id,
    )
    return response


def transition_billing_dispute(
    config: AppConfig,
    context: AuthContext,
    billing_dispute_id: str,
    payload: BillingDisputePatchRequest,
    *,
    idempotency_key: str,
    audit_request_meta: AuditRequestMeta,
    request_id: str,
    trace_id: str,
) -> BillingDisputeMutationResponse:
    """以 CAS 执行一次迁移，并原子写入关联 FinOps、hold 与审计事实。"""

    dispute_id = _dispute_id(billing_dispute_id)
    normalized_request_id = _identifier(request_id)
    normalized_trace_id = _identifier(trace_id)
    key_hash = _domain_hash(TRANSITION_DOMAIN, idempotency_key.encode("ascii"))
    intent = _normalize_patch(payload, actor_user_id=context.user.user_id, dispute_id=dispute_id)
    connection = _connect(config)
    audit_records: list[AuditRecord] = []
    finops_writes: list[FinOpsAdjustmentWriteResult] = []
    response: BillingDisputeMutationResponse | None = None
    try:
        # 状态读取、实时授权、自审检查与写入共享同一立即事务，前端隐藏按钮不能替代校验。
        connection.execute("BEGIN IMMEDIATE")
        live = require_transaction_permission(
            connection,
            context,
            "billing.dispute.read",
            unavailable_error_code="billing_dispute.unavailable",
        )
        permission = (
            "billing.dispute.create" if intent.action == "cancel" else "billing.dispute.review"
        )
        live = require_transaction_permission(
            connection,
            live,
            permission,
            unavailable_error_code="billing_dispute.unavailable",
        )
        if intent.action == "approve":
            live = require_transaction_permission(
                connection,
                live,
                "billing.finops.write",
                unavailable_error_code="billing_dispute.unavailable",
            )
        replay = connection.execute(
            """SELECT * FROM billing_dispute_transitions
               WHERE tenant_id=? AND idempotency_key_sha256=?""",
            (live.user.tenant_id, key_hash),
        ).fetchone()
        if replay is not None:
            if replay["billing_dispute_id"] != dispute_id or not hmac.compare_digest(
                str(replay["payload_sha256"]), intent.payload_sha256
            ):
                raise ApiError("billing_dispute.idempotency_conflict")
            detail = _detail(connection, live.user.tenant_id, dispute_id)
            transition = _transition_info(replay)
            links = [
                link
                for link in detail.adjustment_links
                if link.billing_dispute_transition_id == transition.billing_dispute_transition_id
            ]
            connection.rollback()
            return BillingDisputeMutationResponse(
                dispute=detail,
                transition=transition,
                adjustment_links=links,
                idempotent_replay=True,
                request_id=normalized_request_id,
                trace_id=normalized_trace_id,
            )
        head = connection.execute(
            "SELECT * FROM billing_disputes WHERE tenant_id=? AND billing_dispute_id=?",
            (live.user.tenant_id, dispute_id),
        ).fetchone()
        if head is None:
            raise ApiError("billing_dispute.not_found")
        current_status = _enum(head["status"], _STATUSES)
        current_revision = _positive_int(head["state_revision"])
        if current_revision != intent.expected_revision:
            raise ApiError("billing_dispute.revision_conflict")
        creator = _text(head["created_by_user_id"])
        if intent.action in {"start_review", "approve", "reject"} and creator == live.user.user_id:
            raise ApiError("billing_dispute.self_review_forbidden")
        if intent.action == "cancel" and creator != live.user.user_id:
            raise ApiError("auth.permission_denied")
        expected_from = {
            "start_review": "submitted",
            "cancel": "submitted",
            "approve": "under_review",
            "reject": "under_review",
        }[intent.action]
        if current_status != expected_from:
            raise ApiError("billing_dispute.transition_invalid")
        to_status = {
            "start_review": "under_review",
            "cancel": "cancelled",
            "approve": "approved",
            "reject": "rejected",
        }[intent.action]
        new_revision = current_revision + 1
        now = _database_now(connection)
        approved_totals: dict[str, str] = {}
        approved_rows: list[tuple[sqlite3.Row, _ApprovedAmount, FinOpsAdjustmentWriteResult]] = []
        if intent.action == "approve":
            approved_rows = _write_approved_adjustments(
                connection,
                live,
                dispute_id,
                intent,
                key_hash=key_hash,
                audit_request_meta=audit_request_meta,
                request_id=normalized_request_id,
                trace_id=normalized_trace_id,
            )
            finops_writes = [row[2] for row in approved_rows]
            approved_totals = _approved_totals(approved_rows)
            for write in finops_writes:
                if write.audit_record is None:
                    raise ApiError("billing_dispute.integrity_failed")
        reviewer = (
            live.user.user_id if intent.action in {"start_review", "approve", "reject"} else None
        )
        review_started_at = now if intent.action == "start_review" else head["review_started_at"]
        resolved_at = now if intent.action in {"approve", "reject", "cancel"} else None
        cursor = connection.execute(
            """
            UPDATE billing_disputes
            SET status=?,state_revision=?,reviewer_user_id=?,decision_reason=?,
                decision_reason_sha256=?,review_started_at=?,resolved_at=?,updated_at=?
            WHERE tenant_id=? AND billing_dispute_id=? AND status=? AND state_revision=?
            """,
            (
                to_status,
                new_revision,
                reviewer,
                intent.decision_reason,
                intent.decision_reason_sha256,
                review_started_at,
                resolved_at,
                now,
                live.user.tenant_id,
                dispute_id,
                current_status,
                current_revision,
            ),
        )
        if cursor.rowcount != 1:
            # status 与 state_revision 双条件 CAS 防止两个 reviewer 同时收敛同一状态。
            raise ApiError("billing_dispute.revision_conflict")
        transition_id = f"bdtr_{uuid4().hex}"
        totals_hash = _sha256_json(approved_totals) if approved_totals else None
        connection.execute(
            """
            INSERT INTO billing_dispute_transitions(
              billing_dispute_transition_id,tenant_id,billing_dispute_id,action,
              from_status,to_status,expected_revision,new_revision,actor_user_id,
              decision_reason_sha256,approved_item_count,approved_totals_sha256,
              operation_request_id,operation_trace_id,idempotency_key_sha256,
              payload_sha256,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                transition_id,
                live.user.tenant_id,
                dispute_id,
                intent.action,
                current_status,
                to_status,
                current_revision,
                new_revision,
                live.user.user_id,
                intent.decision_reason_sha256,
                len(approved_rows),
                totals_hash,
                normalized_request_id,
                normalized_trace_id,
                key_hash,
                intent.payload_sha256,
                now,
            ),
        )
        if intent.action == "approve":
            for item, approved, write in approved_rows:
                connection.execute(
                    """
                    INSERT INTO billing_dispute_adjustment_links(
                      billing_dispute_adjustment_link_id,tenant_id,billing_dispute_id,
                      billing_dispute_item_id,finops_adjustment_id,
                      reversal_finops_ledger_entry_id,amount_micros,currency,
                      billing_dispute_transition_id,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        f"bdal_{uuid4().hex}",
                        live.user.tenant_id,
                        dispute_id,
                        item["billing_dispute_item_id"],
                        write.adjustment.finops_adjustment_id,
                        write.ledger_entry.finops_ledger_entry_id,
                        approved.amount_micros,
                        write.ledger_entry.currency,
                        transition_id,
                        now,
                    ),
                )
        if intent.action in {"approve", "reject", "cancel"}:
            # hold 自身修订号只允许 active/1 -> released/2，不得混用 dispute 的新 revision。
            released = connection.execute(
                """
                UPDATE billing_settlement_holds
                SET status='released',release_reason=?,state_revision=?,
                    released_transition_id=?,released_at=?,updated_at=?
                WHERE tenant_id=? AND billing_dispute_id=? AND status='active'
                  AND state_revision=1
                """,
                (
                    to_status,
                    2,
                    transition_id,
                    now,
                    now,
                    live.user.tenant_id,
                    dispute_id,
                ),
            )
            if released.rowcount != _positive_int(head["item_count"]):
                raise ApiError("billing_dispute.integrity_failed")
        audit_action = {
            "start_review": "billing_dispute.review_started",
            "approve": "billing_dispute.approved",
            "reject": "billing_dispute.rejected",
            "cancel": "billing_dispute.cancelled",
        }[intent.action]
        dispute_audit = record_audit_log(
            connection,
            live,
            action=audit_action,
            object_type="billing_dispute",
            object_id=dispute_id,
            object_name=None,
            before_summary=_audit_summary(
                status=current_status,
                revision=current_revision,
                scope_type=_text(head["scope_type"]),
                item_count=_positive_int(head["item_count"]),
                adjustment_count=0,
                totals={},
                reason_sha256=_text(head["reason_sha256"]),
            ),
            after_summary=_audit_summary(
                status=to_status,
                revision=new_revision,
                scope_type=_text(head["scope_type"]),
                item_count=_positive_int(head["item_count"]),
                adjustment_count=len(approved_rows),
                totals=approved_totals,
                reason_sha256=_text(head["reason_sha256"]),
            ),
            request_meta=audit_request_meta,
            request_id=normalized_request_id,
            trace_id=normalized_trace_id,
        )
        if dispute_audit is None:
            raise ApiError("billing_dispute.integrity_failed")
        audit_records.append(dispute_audit)
        detail = _detail(connection, live.user.tenant_id, dispute_id)
        transition = next(
            item
            for item in detail.transitions
            if item.billing_dispute_transition_id == transition_id
        )
        links = [
            item
            for item in detail.adjustment_links
            if item.billing_dispute_transition_id == transition_id
        ]
        response = BillingDisputeMutationResponse(
            dispute=detail,
            transition=transition,
            adjustment_links=links,
            idempotent_replay=False,
            request_id=normalized_request_id,
            trace_id=normalized_trace_id,
        )
        commit_audited_transaction(connection)
    except ApiError as exc:
        _rollback(connection)
        raise _map_domain_error(exc) from exc
    except sqlite3.OperationalError as exc:
        _rollback(connection)
        raise ApiError("billing_dispute.unavailable") from exc
    except (sqlite3.Error, TypeError, ValueError, ArithmeticError, StopIteration) as exc:
        _rollback(connection)
        raise ApiError("billing_dispute.integrity_failed") from exc
    finally:
        connection.close()
    if response is None:
        raise ApiError("billing_dispute.integrity_failed")
    # Audit 与 FinOps Webhook 均在提交后 dispatch；持久 source 保证失败可追踪且不会重复落账。
    for record in audit_records:
        dispatch_audit_recorded(
            config,
            context,
            record,
            request_id=normalized_request_id,
            trace_id=normalized_trace_id,
        )
    for write in finops_writes:
        dispatch_finops_adjustment_write(
            config,
            context,
            write,
            request_id=normalized_request_id,
            trace_id=normalized_trace_id,
        )
    return response


def _write_approved_adjustments(
    connection: sqlite3.Connection,
    context: AuthContext,
    dispute_id: str,
    intent: _PatchIntent,
    *,
    key_hash: str,
    audit_request_meta: AuditRequestMeta,
    request_id: str,
    trace_id: str,
) -> list[tuple[sqlite3.Row, _ApprovedAmount, FinOpsAdjustmentWriteResult]]:
    """复核冻结与当前可冲正余额，并在调用方事务中逐项写入 FinOps 冲正。"""

    item_rows = connection.execute(
        "SELECT * FROM billing_dispute_items WHERE tenant_id=? AND billing_dispute_id=?",
        (context.user.tenant_id, dispute_id),
    ).fetchall()
    by_id = {_text(row["billing_dispute_item_id"]): row for row in item_rows}
    if len(by_id) != len(item_rows):
        raise ApiError("billing_dispute.integrity_failed")
    result: list[tuple[sqlite3.Row, _ApprovedAmount, FinOpsAdjustmentWriteResult]] = []
    for approved in intent.adjustments:
        item = by_id.get(approved.item_id)
        if item is None:
            raise ApiError("billing_dispute.adjustment_invalid")
        frozen_remaining = _positive_int(item["disputable_micros"])
        if approved.amount_micros > frozen_remaining:
            raise ApiError("billing_dispute.adjustment_conflict")
        # 冻结余额保护创建时范围，当前余额保护审批时并发冲正；两者都必须足额。
        current_remaining = _remaining_micros(
            connection,
            context.user.tenant_id,
            _text(item["finops_ledger_entry_id"]),
        )
        if approved.amount_micros > current_remaining:
            raise ApiError("billing_dispute.adjustment_conflict")
        finops_key = (
            "bdsp:"
            + hashlib.sha256(
                TRANSITION_DOMAIN
                + b"\x00"
                + key_hash.encode("ascii")
                + b"\x00"
                + approved.item_id.encode("ascii")
            ).hexdigest()
        )
        write = write_finops_adjustment_in_transaction(
            connection,
            context,
            {
                "adjustment_type": "reversal",
                "amount": approved.amount,
                "reversal_of_entry_id": _text(item["finops_ledger_entry_id"]),
                "reason": "Approved billing dispute adjustment",
            },
            idempotency_key=finops_key,
            audit_request_meta=audit_request_meta,
            request_id=request_id,
            trace_id=trace_id,
        )
        if write.idempotent_replay:
            raise ApiError("billing_dispute.adjustment_conflict")
        result.append((item, approved, write))
    return result


def _freeze_scope(
    connection: sqlite3.Connection,
    tenant_id: str,
    intent: _CreateIntent,
    *,
    now: str,
) -> list[dict[str, object]]:
    """按数据库水位冻结最多一百条同租户 usage charge 及其定价证据。"""

    # 水位使同一创建事务的 request/time scope 不受随后新增账本行影响。
    watermark = _scalar_int(
        connection,
        "SELECT COALESCE(MAX(ledger_sequence),0) FROM finops_ledger_entries WHERE tenant_id=?",
        (tenant_id,),
    )
    rows: list[sqlite3.Row] = []
    if intent.scope_type == "request_ids":
        for request_id in intent.request_ids:
            matches = connection.execute(
                """
                SELECT * FROM finops_ledger_entries
                WHERE tenant_id=? AND entry_type='usage_charge' AND source_request_id=?
                  AND ledger_sequence<=?
                ORDER BY ledger_sequence
                LIMIT 2
                """,
                (tenant_id, request_id, watermark),
            ).fetchall()
            if len(matches) != 1:
                raise ApiError("billing_dispute.scope_not_found")
            rows.append(matches[0])
    else:
        if intent.period_end is None or _parse_time(intent.period_end) > _parse_time(now):
            raise ApiError("billing_dispute.request_invalid")
        rows = connection.execute(
            """
            SELECT * FROM finops_ledger_entries le
            WHERE le.tenant_id=? AND le.entry_type='usage_charge'
              AND le.ledger_sequence<=?
              AND unixepoch(le.created_at)>=unixepoch(?)
              AND unixepoch(le.created_at)<unixepoch(?)
              AND le.amount_micros-COALESCE((
                SELECT SUM(reversal.amount_micros)
                FROM finops_ledger_entries reversal
                WHERE reversal.tenant_id=le.tenant_id
                  AND reversal.entry_type='reversal'
                  AND reversal.reversal_of_entry_id=le.finops_ledger_entry_id
              ),0)>0
            ORDER BY le.created_at,le.ledger_sequence
            LIMIT 101
            """,
            (tenant_id, watermark, intent.period_start, intent.period_end),
        ).fetchall()
        if len(rows) > 100:
            raise ApiError("billing_dispute.scope_too_large")
        if not rows:
            raise ApiError("billing_dispute.scope_not_found")
    frozen: list[dict[str, object]] = []
    seen_entries: set[str] = set()
    for row in rows:
        ledger_id = _text(row["finops_ledger_entry_id"])
        if ledger_id in seen_entries:
            raise ApiError("billing_dispute.integrity_failed")
        seen_entries.add(ledger_id)
        remaining = _remaining_micros(connection, tenant_id, ledger_id)
        if remaining <= 0:
            raise ApiError("billing_dispute.scope_not_found")
        currency = _enum(row["currency"], _CURRENCIES)
        usage_id = _required_value(row["usage_ledger_id"])
        source_request_id = _required_value(row["source_request_id"])
        source_trace_id = _required_value(row["source_trace_id"])
        price_snapshot_id = _optional_text(row["price_snapshot_id"])
        price_schema_version = _optional_text(row["price_schema_version"])
        price_snapshot_source = _optional_text(row["price_snapshot_source"])
        price_evidence_status = _text(row["price_evidence_status"])
        if (
            price_snapshot_id is None
            or price_schema_version is None
            or price_snapshot_source is None
            or price_evidence_status not in {"priced", "unpriced", "legacy_amount_only"}
        ):
            raise ApiError("billing_dispute.scope_not_found")
        usage = connection.execute(
            "SELECT 1 FROM usage_ledger WHERE tenant_id=? AND usage_ledger_id=?",
            (tenant_id, usage_id),
        ).fetchone()
        if usage is None:
            raise ApiError("billing_dispute.integrity_failed")
        frozen.append(
            {
                "finops_ledger_entry_id": ledger_id,
                "usage_ledger_id": usage_id,
                "source_request_id": source_request_id,
                "source_trace_id": source_trace_id,
                "ledger_sequence": _positive_int(row["ledger_sequence"]),
                "currency": currency,
                "amount_micros": _positive_int(row["amount_micros"]),
                "disputable_micros": remaining,
                "price_snapshot_id": price_snapshot_id,
                "price_schema_version": price_schema_version,
                "price_snapshot_source": price_snapshot_source,
                "price_evidence_status": price_evidence_status,
            }
        )
    if len(frozen) > 100:
        raise ApiError("billing_dispute.scope_too_large")
    return sorted(frozen, key=lambda item: int(item["ledger_sequence"]))


def _detail(
    connection: sqlite3.Connection, tenant_id: str, dispute_id: str
) -> BillingDisputeDetail:
    """从同租户 owner 图重建详情，任一 item/hold/event 数量漂移均失败关闭。"""

    head = connection.execute(
        "SELECT * FROM billing_disputes WHERE tenant_id=? AND billing_dispute_id=?",
        (tenant_id, dispute_id),
    ).fetchone()
    if head is None:
        raise ApiError("billing_dispute.not_found")
    summary = _summary(connection, head, transition_watermark=None, snapshot=False)
    holds_rows = connection.execute(
        """SELECT * FROM billing_settlement_holds
           WHERE tenant_id=? AND billing_dispute_id=?
           ORDER BY created_at,billing_settlement_hold_id""",
        (tenant_id, dispute_id),
    ).fetchall()
    link_rows = connection.execute(
        """SELECT * FROM billing_dispute_adjustment_links
           WHERE tenant_id=? AND billing_dispute_id=?
           ORDER BY created_at,billing_dispute_adjustment_link_id""",
        (tenant_id, dispute_id),
    ).fetchall()
    transition_rows = connection.execute(
        """SELECT * FROM billing_dispute_transitions
           WHERE tenant_id=? AND billing_dispute_id=?
           ORDER BY billing_dispute_transition_sequence""",
        (tenant_id, dispute_id),
    ).fetchall()
    item_rows = connection.execute(
        """SELECT * FROM billing_dispute_items
           WHERE tenant_id=? AND billing_dispute_id=? ORDER BY ledger_sequence""",
        (tenant_id, dispute_id),
    ).fetchall()
    holds = [_hold_info(row) for row in holds_rows]
    links = [_link_info(row) for row in link_rows]
    hold_by_item = {item.billing_dispute_item_id: item for item in holds}
    link_by_item = {item.billing_dispute_item_id: item for item in links}
    if len(holds) != len(item_rows) or len(hold_by_item) != len(holds):
        raise ApiError("billing_dispute.integrity_failed")
    items = [
        _item_info(connection, tenant_id, row, hold_by_item, link_by_item) for row in item_rows
    ]
    if len(items) != _positive_int(head["item_count"]):
        raise ApiError("billing_dispute.integrity_failed")
    event_rows = connection.execute(
        """
        SELECT event.*,outbox.status AS delivery_status,outbox.attempt_count,
          outbox.last_error_code,outbox.succeeded_at
        FROM billing_dispute_events event
        JOIN billing_dispute_event_outbox outbox
          ON outbox.tenant_id=event.tenant_id
         AND outbox.billing_dispute_event_id=event.billing_dispute_event_id
        WHERE event.tenant_id=? AND event.billing_dispute_id=?
        """,
        (tenant_id, dispute_id),
    ).fetchall()
    if len(event_rows) != 1:
        raise ApiError("billing_dispute.integrity_failed")
    return BillingDisputeDetail(
        summary=summary,
        reason=_text(head["reason"]),
        decision_reason=_optional_text(head["decision_reason"]),
        create_request_id=_text(head["create_request_id"]),
        create_trace_id=_text(head["create_trace_id"]),
        items=items,
        holds=holds,
        adjustment_links=links,
        transitions=[_transition_info(row) for row in transition_rows],
        event=_event_info(event_rows[0]),
    )


def _summary(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    transition_watermark: int | None,
    snapshot: bool,
) -> BillingDisputeSummary:
    tenant_id = _text(row["tenant_id"])
    dispute_id = _text(row["billing_dispute_id"])
    if snapshot:
        status = _enum(row["snapshot_status"], _STATUSES)
        revision = _positive_int(row["snapshot_revision"])
        reviewer_id = (
            _text(row["snapshot_actor_user_id"])
            if status in {"under_review", "approved", "rejected"}
            else None
        )
        updated_at = _text(row["snapshot_updated_at"])
        review_started_row = connection.execute(
            """SELECT created_at FROM billing_dispute_transitions
               WHERE tenant_id=? AND billing_dispute_id=? AND action='start_review'
                 AND billing_dispute_transition_sequence<=?
               ORDER BY billing_dispute_transition_sequence LIMIT 1""",
            (tenant_id, dispute_id, transition_watermark),
        ).fetchone()
        review_started_at = review_started_row[0] if review_started_row else None
        resolved_at = updated_at if status in {"approved", "rejected", "cancelled"} else None
    else:
        status = _enum(row["status"], _STATUSES)
        revision = _positive_int(row["state_revision"])
        reviewer_id = _optional_text(row["reviewer_user_id"])
        updated_at = _text(row["updated_at"])
        review_started_at = row["review_started_at"]
        resolved_at = row["resolved_at"]
    items = connection.execute(
        """SELECT currency,disputable_micros FROM billing_dispute_items
           WHERE tenant_id=? AND billing_dispute_id=?""",
        (tenant_id, dispute_id),
    ).fetchall()
    disputed = _currency_totals(items, amount_key="disputable_micros")
    if transition_watermark is None:
        links = connection.execute(
            """SELECT link.currency,link.amount_micros
               FROM billing_dispute_adjustment_links link
               WHERE link.tenant_id=? AND link.billing_dispute_id=?""",
            (tenant_id, dispute_id),
        ).fetchall()
        active_holds = _scalar_int(
            connection,
            """SELECT COUNT(*) FROM billing_settlement_holds
               WHERE tenant_id=? AND billing_dispute_id=? AND status='active'""",
            (tenant_id, dispute_id),
        )
    else:
        links = connection.execute(
            """
            SELECT link.currency,link.amount_micros
            FROM billing_dispute_adjustment_links link
            JOIN billing_dispute_transitions transition_row
              ON transition_row.tenant_id=link.tenant_id
             AND transition_row.billing_dispute_transition_id=link.billing_dispute_transition_id
            WHERE link.tenant_id=? AND link.billing_dispute_id=?
              AND transition_row.billing_dispute_transition_sequence<=?
            """,
            (tenant_id, dispute_id, transition_watermark),
        ).fetchall()
        active_holds = _scalar_int(
            connection,
            """
            SELECT COUNT(*)
            FROM billing_settlement_holds hold
            JOIN billing_dispute_transitions created_transition
              ON created_transition.tenant_id=hold.tenant_id
             AND created_transition.billing_dispute_transition_id=hold.created_transition_id
            LEFT JOIN billing_dispute_transitions released_transition
              ON released_transition.tenant_id=hold.tenant_id
             AND released_transition.billing_dispute_transition_id=hold.released_transition_id
            WHERE hold.tenant_id=? AND hold.billing_dispute_id=?
              AND created_transition.billing_dispute_transition_sequence<=?
              AND (released_transition.billing_dispute_transition_sequence IS NULL
                   OR released_transition.billing_dispute_transition_sequence>?)
            """,
            (tenant_id, dispute_id, transition_watermark, transition_watermark),
        )
    event_status = connection.execute(
        """SELECT outbox.status FROM billing_dispute_event_outbox outbox
           JOIN billing_dispute_events event
             ON event.tenant_id=outbox.tenant_id
            AND event.billing_dispute_event_id=outbox.billing_dispute_event_id
           WHERE event.tenant_id=? AND event.billing_dispute_id=?""",
        (tenant_id, dispute_id),
    ).fetchall()
    if len(event_status) != 1:
        raise ApiError("billing_dispute.integrity_failed")
    return BillingDisputeSummary(
        billing_dispute_id=dispute_id,
        tenant_id=tenant_id,
        scope_type=_enum(row["scope_type"], _SCOPES),
        period_start=_optional_time(row["period_start"]),
        period_end=_optional_time(row["period_end"]),
        status=status,
        state_revision=revision,
        item_count=_positive_int(row["item_count"]),
        reason_sha256=_sha256(row["reason_sha256"]),
        created_by=_actor(connection, tenant_id, _text(row["created_by_user_id"])),
        reviewer=_actor(connection, tenant_id, reviewer_id) if reviewer_id else None,
        disputed_currency_totals=disputed,
        approved_currency_totals=_currency_totals(links, amount_key="amount_micros"),
        active_hold_count=active_holds,
        adjustment_count=len(links),
        event_delivery_status=_enum(
            event_status[0]["status"],
            frozenset({"pending", "processing", "succeeded", "failed"}),
        ),
        review_started_at=_optional_time(review_started_at),
        resolved_at=_optional_time(resolved_at),
        created_at=_time(row["created_at"]),
        updated_at=_time(updated_at),
    )


def _item_info(
    connection: sqlite3.Connection,
    tenant_id: str,
    row: sqlite3.Row,
    holds: dict[str, BillingSettlementHoldInfo],
    links: dict[str, BillingDisputeAdjustmentLinkInfo],
) -> BillingDisputeItemInfo:
    item_id = _text(row["billing_dispute_item_id"])
    hold = holds.get(item_id)
    if hold is None:
        raise ApiError("billing_dispute.integrity_failed")
    frozen_disputable = _positive_int(row["disputable_micros"])
    current_disputable = _remaining_micros(
        connection,
        tenant_id,
        _text(row["finops_ledger_entry_id"]),
    )
    if current_disputable > frozen_disputable:
        raise ApiError("billing_dispute.integrity_failed")
    return BillingDisputeItemInfo(
        billing_dispute_item_id=item_id,
        finops_ledger_entry_id=_text(row["finops_ledger_entry_id"]),
        usage_ledger_id=_text(row["usage_ledger_id"]),
        source_request_id=_text(row["source_request_id"]),
        source_trace_id=_text(row["source_trace_id"]),
        ledger_sequence=_positive_int(row["ledger_sequence"]),
        currency=_enum(row["currency"], _CURRENCIES),
        amount=_format_micros(_positive_int(row["amount_micros"])),
        disputable_amount=_format_micros(frozen_disputable),
        current_disputable_amount=_format_micros(current_disputable),
        price_snapshot_id=_optional_text(row["price_snapshot_id"]),
        price_schema_version=_optional_text(row["price_schema_version"]),
        price_snapshot_source=_optional_text(row["price_snapshot_source"]),
        price_evidence_status=_text(row["price_evidence_status"]),
        created_at=_time(row["created_at"]),
        hold=hold,
        adjustment_link=links.get(item_id),
    )


def _hold_info(row: sqlite3.Row) -> BillingSettlementHoldInfo:
    return BillingSettlementHoldInfo(
        billing_settlement_hold_id=_text(row["billing_settlement_hold_id"]),
        billing_dispute_item_id=_text(row["billing_dispute_item_id"]),
        finops_ledger_entry_id=_text(row["finops_ledger_entry_id"]),
        status=_enum(row["status"], frozenset({"active", "released"})),
        release_reason=(
            _enum(row["release_reason"], frozenset({"approved", "rejected", "cancelled"}))
            if row["release_reason"] is not None
            else None
        ),
        state_revision=_positive_int(row["state_revision"]),
        created_transition_id=_text(row["created_transition_id"]),
        released_transition_id=_optional_text(row["released_transition_id"]),
        created_at=_time(row["created_at"]),
        released_at=_optional_time(row["released_at"]),
        updated_at=_time(row["updated_at"]),
    )


def _link_info(row: sqlite3.Row) -> BillingDisputeAdjustmentLinkInfo:
    return BillingDisputeAdjustmentLinkInfo(
        billing_dispute_adjustment_link_id=_text(row["billing_dispute_adjustment_link_id"]),
        billing_dispute_item_id=_text(row["billing_dispute_item_id"]),
        finops_adjustment_id=_text(row["finops_adjustment_id"]),
        reversal_finops_ledger_entry_id=_text(row["reversal_finops_ledger_entry_id"]),
        amount=_format_micros(_positive_int(row["amount_micros"])),
        currency=_enum(row["currency"], _CURRENCIES),
        billing_dispute_transition_id=_text(row["billing_dispute_transition_id"]),
        created_at=_time(row["created_at"]),
    )


def _transition_info(row: sqlite3.Row) -> BillingDisputeTransitionInfo:
    return BillingDisputeTransitionInfo(
        billing_dispute_transition_id=_text(row["billing_dispute_transition_id"]),
        action=_enum(
            row["action"],
            frozenset({"create", "start_review", "approve", "reject", "cancel"}),
        ),
        from_status=_enum(row["from_status"], _STATUSES)
        if row["from_status"] is not None
        else None,
        to_status=_enum(row["to_status"], _STATUSES),
        expected_revision=_non_negative_int(row["expected_revision"]),
        new_revision=_positive_int(row["new_revision"]),
        actor_user_id=_text(row["actor_user_id"]),
        decision_reason_sha256=_optional_sha256(row["decision_reason_sha256"]),
        approved_item_count=_non_negative_int(row["approved_item_count"]),
        approved_totals_sha256=_optional_sha256(row["approved_totals_sha256"]),
        operation_request_id=_text(row["operation_request_id"]),
        operation_trace_id=_text(row["operation_trace_id"]),
        created_at=_time(row["created_at"]),
    )


def _event_info(row: sqlite3.Row) -> BillingDisputeEventInfo:
    return BillingDisputeEventInfo(
        billing_dispute_event_id=_text(row["billing_dispute_event_id"]),
        event_type=_enum(row["event_type"], frozenset({"billing_dispute.created"})),
        delivery_status=_enum(
            row["delivery_status"], frozenset({"pending", "processing", "succeeded", "failed"})
        ),
        attempt_count=_non_negative_int(row["attempt_count"]),
        last_error_code=_optional_text(row["last_error_code"]),
        occurred_at=_time(row["occurred_at"]),
        succeeded_at=_optional_time(row["succeeded_at"]),
    )


def _normalize_create(payload: BillingDisputeCreateRequest, *, actor_user_id: str) -> _CreateIntent:
    """规范互斥 scope 与理由，并把认证 actor 绑定进创建幂等摘要。"""

    reason, reason_hash = _reason(payload.reason)
    if payload.scope_type == "request_ids":
        if (
            payload.request_ids is None
            or payload.period_start is not None
            or payload.period_end is not None
        ):
            raise ApiError("billing_dispute.request_invalid")
        request_ids = tuple(_identifier(value) for value in payload.request_ids)
        if len(request_ids) != len(set(request_ids)):
            raise ApiError("billing_dispute.request_invalid")
        period_start = period_end = None
    else:
        if (
            payload.request_ids is not None
            or payload.period_start is None
            or payload.period_end is None
        ):
            raise ApiError("billing_dispute.request_invalid")
        request_ids = ()
        start = _time_text(payload.period_start)
        end = _time_text(payload.period_end)
        start_time = _parse_time(start)
        end_time = _parse_time(end)
        if start_time >= end_time or end_time - start_time > timedelta(days=31):
            raise ApiError("billing_dispute.request_invalid")
        period_start, period_end = start, end
    canonical = {
        "actor_user_id": _identifier(actor_user_id),
        "scope_type": payload.scope_type,
        "reason": reason,
        "request_ids": sorted(request_ids) if request_ids else None,
        "period_start": period_start,
        "period_end": period_end,
    }
    return _CreateIntent(
        scope_type=payload.scope_type,
        request_ids=request_ids,
        period_start=period_start,
        period_end=period_end,
        reason=reason,
        reason_sha256=reason_hash,
        payload_sha256=_domain_hash(CREATE_DOMAIN, _canonical_json(canonical).encode("utf-8")),
    )


def _normalize_patch(
    payload: BillingDisputePatchRequest,
    *,
    actor_user_id: str,
    dispute_id: str,
) -> _PatchIntent:
    """规范动作、理由和调整项，把 actor、争议与期望修订号绑定进摘要。"""

    reason: str | None = None
    reason_hash: str | None = None
    adjustments: tuple[_ApprovedAmount, ...] = ()
    if payload.action == "start_review":
        if payload.decision_reason is not None or payload.adjustments is not None:
            raise ApiError("billing_dispute.request_invalid")
    elif payload.action == "approve":
        if payload.decision_reason is None or payload.adjustments is None:
            raise ApiError("billing_dispute.request_invalid")
        reason, reason_hash = _reason(payload.decision_reason)
        values = [
            _ApprovedAmount(
                item_id=_item_id(item.billing_dispute_item_id),
                amount_micros=_parse_amount(item.amount)[0],
                amount=_parse_amount(item.amount)[1],
            )
            for item in payload.adjustments
        ]
        if len({item.item_id for item in values}) != len(values):
            raise ApiError("billing_dispute.adjustment_invalid")
        adjustments = tuple(sorted(values, key=lambda item: item.item_id))
    else:
        if payload.decision_reason is None or payload.adjustments is not None:
            raise ApiError("billing_dispute.request_invalid")
        reason, reason_hash = _reason(payload.decision_reason)
    canonical = {
        "actor_user_id": _identifier(actor_user_id),
        "billing_dispute_id": dispute_id,
        "action": payload.action,
        "expected_revision": payload.expected_revision,
        "decision_reason": reason,
        "adjustments": [
            {"billing_dispute_item_id": item.item_id, "amount": item.amount} for item in adjustments
        ],
    }
    return _PatchIntent(
        action=payload.action,
        expected_revision=payload.expected_revision,
        decision_reason=reason,
        decision_reason_sha256=reason_hash,
        adjustments=adjustments,
        payload_sha256=_domain_hash(
            TRANSITION_DOMAIN,
            _canonical_json(canonical).encode("utf-8"),
        ),
    )


def _normalize_list_query(query: dict[str, str]) -> _ListQuery:
    status = query.get("status")
    if status is not None and status not in _STATUSES:
        raise ApiError("billing_dispute.query_invalid")
    scope_type = query.get("scope_type")
    if scope_type is not None and scope_type not in _SCOPES:
        raise ApiError("billing_dispute.query_invalid")
    try:
        created_from = _time_text(query["created_from"]) if "created_from" in query else None
        created_to = _time_text(query["created_to"]) if "created_to" in query else None
    except ApiError as exc:
        raise ApiError("billing_dispute.query_invalid") from exc
    if created_from and created_to and _parse_time(created_from) >= _parse_time(created_to):
        raise ApiError("billing_dispute.query_invalid")
    raw_limit = query.get("limit", "50")
    if not raw_limit.isdigit() or len(raw_limit) > 3:
        raise ApiError("billing_dispute.query_invalid")
    limit = int(raw_limit)
    if not 1 <= limit <= 100:
        raise ApiError("billing_dispute.query_invalid")
    return _ListQuery(status, scope_type, created_from, created_to, limit)


def _remaining_micros(connection: sqlite3.Connection, tenant_id: str, ledger_id: str) -> int:
    """用同租户正式 usage charge 减去全部冲正，返回当前整数微单位余额。"""

    target = connection.execute(
        """SELECT amount_micros,entry_type FROM finops_ledger_entries
           WHERE tenant_id=? AND finops_ledger_entry_id=?""",
        (tenant_id, ledger_id),
    ).fetchone()
    if target is None or target["entry_type"] != "usage_charge":
        raise ApiError("billing_dispute.scope_not_found")
    amount = _non_negative_int(target["amount_micros"])
    if amount == 0:
        raise ApiError("billing_dispute.scope_not_found")
    reversed_micros = _scalar_int(
        connection,
        """SELECT COALESCE(SUM(amount_micros),0) FROM finops_ledger_entries
           WHERE tenant_id=? AND entry_type='reversal' AND reversal_of_entry_id=?""",
        (tenant_id, ledger_id),
    )
    if reversed_micros > amount:
        raise ApiError("billing_dispute.integrity_failed")
    return amount - reversed_micros


def _approved_totals(
    rows: list[tuple[sqlite3.Row, _ApprovedAmount, FinOpsAdjustmentWriteResult]],
) -> dict[str, str]:
    values: dict[str, int] = {}
    for _, approved, write in rows:
        currency = write.ledger_entry.currency
        values[currency] = values.get(currency, 0) + approved.amount_micros
    return {currency: _format_micros(value) for currency, value in sorted(values.items())}


def _currency_totals(rows: list[object], *, amount_key: str) -> dict[str, str]:
    totals: dict[str, int] = {}
    for row in rows:
        currency = _enum(row["currency"], _CURRENCIES)  # type: ignore[index]
        amount = _non_negative_int(row[amount_key])  # type: ignore[index]
        totals[currency] = totals.get(currency, 0) + amount
    return {currency: _format_micros(amount) for currency, amount in sorted(totals.items())}


def _audit_summary(
    *,
    status: str,
    revision: int,
    scope_type: str,
    item_count: int,
    adjustment_count: int,
    totals: dict[str, str],
    reason_sha256: str,
) -> dict[str, object]:
    return {
        "status": status,
        "state_revision": revision,
        "scope_type": scope_type,
        "item_count": item_count,
        "adjustment_count": adjustment_count,
        "currency_totals_sha256": _sha256_json(totals),
        "reason_sha256": reason_sha256,
    }


def _actor(connection: sqlite3.Connection, tenant_id: str, user_id: str) -> BillingDisputeActorInfo:
    row = connection.execute(
        "SELECT user_id,display_name,email FROM users WHERE tenant_id=? AND user_id=?",
        (tenant_id, user_id),
    ).fetchone()
    if row is None:
        raise ApiError("billing_dispute.integrity_failed")
    return BillingDisputeActorInfo(
        user_id=_text(row["user_id"]),
        display_name=_optional_text(row["display_name"]),
        email=_text(row["email"]),
    )


def _encode_cursor(config: AppConfig, tenant_id: str, filter_hash: str, state: _Cursor) -> str:
    payload = _canonical_json(
        {
            "before": state.before_sequence,
            "dispute_watermark": state.dispute_watermark,
            "filters": filter_hash,
            "tenant": tenant_id,
            "transition_watermark": state.transition_watermark,
            "v": CURSOR_VERSION,
        }
    ).encode("utf-8")
    signature = hmac.new(
        config.auth_token_secret.encode("utf-8"),
        CURSOR_DOMAIN + b"\x00" + payload,
        hashlib.sha256,
    ).digest()
    return f"{_b64(payload)}.{_b64(signature)}"


def _decode_cursor(config: AppConfig, tenant_id: str, filter_hash: str, cursor: str) -> _Cursor:
    """验证长度、HMAC、租户、筛选和双水位范围，拒绝跨租户游标重放。"""

    try:
        if len(cursor) > 2048:
            raise ValueError("cursor length")
        encoded_payload, encoded_signature = cursor.split(".", 1)
        payload = _unb64(encoded_payload)
        signature = _unb64(encoded_signature)
        expected = hmac.new(
            config.auth_token_secret.encode("utf-8"),
            CURSOR_DOMAIN + b"\x00" + payload,
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("cursor signature")
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_json)
        if type(value) is not dict or set(value) != {
            "before",
            "dispute_watermark",
            "filters",
            "tenant",
            "transition_watermark",
            "v",
        }:
            raise ValueError("cursor shape")
        if (
            value["v"] != CURSOR_VERSION
            or type(value["tenant"]) is not str
            or not hmac.compare_digest(value["tenant"], tenant_id)
            or type(value["filters"]) is not str
            or not hmac.compare_digest(value["filters"], filter_hash)
        ):
            raise ValueError("cursor binding")
        result = _Cursor(
            dispute_watermark=_non_negative_int(value["dispute_watermark"]),
            transition_watermark=_non_negative_int(value["transition_watermark"]),
            before_sequence=_positive_int(value["before"]),
        )
        if result.before_sequence > result.dispute_watermark + 1:
            raise ValueError("cursor range")
        return result
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ApiError("billing_dispute.query_invalid") from exc


def _parse_amount(value: object) -> tuple[int, str]:
    """严格解析最多六位小数的正金额，并精确转换为有上限的整数微单位。"""

    # 金额只经 Decimal 校验后转换成整数微单位，禁止 float 舍入改变冲正余额。
    if type(value) is not str or _AMOUNT.fullmatch(value) is None:
        raise ApiError("billing_dispute.adjustment_invalid")
    try:
        decimal = Decimal(value)
        micros_decimal = decimal * Decimal(1_000_000)
        micros = int(micros_decimal)
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise ApiError("billing_dispute.adjustment_invalid") from exc
    if decimal <= 0 or micros_decimal != micros or micros > MAX_AMOUNT_MICROS:
        raise ApiError("billing_dispute.adjustment_invalid")
    return micros, _format_micros(micros)


def _reason(value: object) -> tuple[str, str]:
    """去除首尾空白并限制 UTF-8 字节与控制字符，返回规范明文和 SHA-256。"""

    if type(value) is not str:
        raise ApiError("billing_dispute.request_invalid")
    normalized = unicodedata.normalize("NFKC", value).strip()
    if (
        not normalized
        or len(normalized.encode("utf-8")) > 2000
        or any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in normalized)
    ):
        raise ApiError("billing_dispute.request_invalid")
    return normalized, hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _database_now(connection: sqlite3.Connection) -> str:
    row = connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')").fetchone()
    if row is None:
        raise ApiError("billing_dispute.unavailable")
    return _time_text(row[0])


def _time_text(value: object) -> str:
    if type(value) is not str or _UTC_SECONDS.fullmatch(value) is None:
        raise ApiError("billing_dispute.request_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApiError("billing_dispute.request_invalid") from exc
    if (
        parsed.tzinfo is None
        or parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z") != value
    ):
        raise ApiError("billing_dispute.request_invalid")
    return value


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _time(value: object) -> datetime:
    if type(value) is not str or _UTC_SECONDS.fullmatch(value) is None:
        raise ValueError("database time")
    parsed = _parse_time(value)
    if parsed.isoformat(timespec="seconds").replace("+00:00", "Z") != value:
        raise ValueError("database time")
    return parsed


def _optional_time(value: object) -> datetime | None:
    return None if value is None else _time(value)


def _dispute_id(value: object) -> str:
    if type(value) is not str or _DISPUTE_ID.fullmatch(value) is None:
        raise ApiError("billing_dispute.not_found")
    return value


def _item_id(value: object) -> str:
    if type(value) is not str or _ITEM_ID.fullmatch(value) is None:
        raise ApiError("billing_dispute.adjustment_invalid")
    return value


def _identifier(value: object) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ApiError("billing_dispute.request_invalid")
    return value


def _sha256(value: object) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError("sha256")
    return value


def _optional_sha256(value: object) -> str | None:
    return None if value is None else _sha256(value)


def _enum(value: object, allowed: frozenset[str]):
    if type(value) is not str or value not in allowed:
        raise ValueError("enum")
    return value


def _text(value: object) -> str:
    if type(value) is not str or not value:
        raise ValueError("text")
    return value


def _required_value(value: object) -> str:
    return _text(value)


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _positive_int(value: object) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("positive integer")
    return value


def _non_negative_int(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("non-negative integer")
    return value


def _scalar_int(connection: sqlite3.Connection, sql: str, params: tuple[object, ...]) -> int:
    row = connection.execute(sql, params).fetchone()
    if row is None:
        raise ValueError("scalar missing")
    return _non_negative_int(row[0])


def _format_micros(value: int) -> str:
    """从非负整数微单位生成固定六位小数字符串，不经过 float。"""

    value = _non_negative_int(value)
    whole, fraction = divmod(value, 1_000_000)
    return f"{whole}.{fraction:06d}"


def _domain_hash(domain: bytes, value: bytes) -> str:
    return hashlib.sha256(domain + b"\x00" + value).hexdigest()


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _unique_json(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate json")
        result[key] = value
    return result


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    if not value or "=" in value:
        raise ValueError("base64url shape")
    decoded = base64.b64decode(
        value + "=" * (-len(value) % 4),
        altchars=b"-_",
        validate=True,
    )
    if _b64(decoded) != value:
        raise ValueError("base64url canonical form")
    return decoded


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except DbError as exc:
        raise ApiError("billing_dispute.unavailable") from exc


def _rollback(connection: sqlite3.Connection) -> None:
    try:
        if connection.in_transaction:
            connection.rollback()
    except sqlite3.Error:
        return


def _map_domain_error(error: ApiError) -> ApiError:
    if error.code in {"audit_log.record_unavailable", "audit_log.summary_invalid"}:
        return ApiError("billing_dispute.unavailable")
    if error.code == "finops.reversal_exceeds_remaining":
        return ApiError("billing_dispute.adjustment_conflict")
    if error.code in {
        "finops.entry_not_found",
        "finops.reversal_not_allowed",
        "finops.adjustment_invalid",
    }:
        return ApiError("billing_dispute.adjustment_invalid")
    if error.code in {"finops.adjustment_failed", "finops.ledger_unavailable"}:
        return ApiError("billing_dispute.unavailable")
    return error


__all__ = [
    "create_billing_dispute",
    "get_billing_dispute",
    "list_billing_disputes",
    "transition_billing_dispute",
]
