"""PRD-087 分润物化租约、JSON 报表与安全 CSV 导出。"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.auth import AuthContext, require_transaction_permission

from .models import (
    CommissionCurrencyTotal,
    CommissionFreshness,
    CommissionLine,
    PartnerCommissionResponse,
    PartnerWindow,
)
from .service import (
    MAX_CSV_BYTES,
    MAX_CSV_ROWS,
    Attachment,
    DateWindow,
    _canonical_json,
    _connect,
    _count,
    _db_time,
    _ledger_high_water,
    _partner_row,
    _record_operation,
    _sha256,
    _text,
    _transaction_now,
    normalize_customer_id,
    parse_pagination,
)

COMMISSION_LEASE_SECONDS = 90
COMMISSION_BATCH_SIZE = 200
COMMISSION_IDLE_POLL_SECONDS = 2.0
COMMISSION_FAILURE_BACKOFF_SECONDS = (5, 30, 300)
_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


@dataclass(slots=True)
class CommissionClaim:
    """保存一次租户客户游标租约；令牌不得进入对象调试文本。"""

    tenant_id: str
    partner_id: str
    partner_customer_id: str
    project_id: str
    lease_owner_id: str
    lease_token: str = field(repr=False)
    generation: int
    consecutive_failure_count: int
    last_scanned_ledger_sequence: int
    scan_upper_sequence: int
    lease_expires_at: datetime


def claim_next_commission_cursor(
    config: AppConfig, *, worker_id: str, now: datetime | None = None
) -> CommissionClaim | None:
    """短事务公平领取一个到期游标，并冻结本批 ledger 上界。"""

    if type(worker_id) is not str or _WORKER_ID.fullmatch(worker_id) is None:
        raise ApiError("partner.commission_unavailable")
    connection = _connect(config)
    try:
        current = _transaction_now(connection, now)
        now_text = _db_time(current)
        due = connection.execute(
            """
            SELECT 1 FROM partner_commission_cursors
            WHERE (status='pending' AND unixepoch(next_run_at)<=unixepoch(?))
               OR (status='leased' AND unixepoch(lease_expires_at)<=unixepoch(?))
            LIMIT 1
            """,
            (now_text, now_text),
        ).fetchone()
        if due is None:
            return None
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        now_text = _db_time(current)
        row = connection.execute(
            """
            SELECT cursor.*,customer.status customer_status,
                   customer.commission_cutoff_sequence
            FROM partner_commission_cursors cursor
            JOIN partner_customers customer
              ON customer.tenant_id=cursor.tenant_id
             AND customer.partner_customer_id=cursor.partner_customer_id
            WHERE (cursor.status='pending' AND unixepoch(cursor.next_run_at)<=unixepoch(?))
               OR (cursor.status='leased' AND unixepoch(cursor.lease_expires_at)<=unixepoch(?))
            ORDER BY COALESCE(cursor.next_run_at,cursor.lease_expires_at),
                     cursor.tenant_id,cursor.partner_customer_id
            LIMIT 1
            """,
            (now_text, now_text),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        if row["status"] == "leased":
            upper = _count(row["scan_upper_sequence"])
        else:
            high_water = _ledger_high_water(connection, _text(row["tenant_id"]))
            cutoff = row["commission_cutoff_sequence"]
            upper = min(high_water, _count(cutoff)) if cutoff is not None else high_water
        old_generation = _count(row["generation"])
        generation = old_generation + 1
        token = secrets.token_hex(32)
        expires = current + timedelta(seconds=COMMISSION_LEASE_SECONDS)
        connection.execute(
            """
            UPDATE partner_commission_cursors
            SET status='leased',scan_upper_sequence=?,generation=?,lease_owner_id=?,lease_token=?,
                lease_expires_at=?,next_run_at=NULL,last_error_code=NULL,updated_at=?
            WHERE tenant_id=? AND partner_customer_id=? AND status=? AND generation=?
              AND last_scanned_ledger_sequence=? AND scan_upper_sequence IS ?
              AND (status='pending' OR unixepoch(lease_expires_at)<=unixepoch(?))
            """,
            (
                upper,
                generation,
                worker_id,
                token,
                _db_time(expires),
                now_text,
                row["tenant_id"],
                row["partner_customer_id"],
                row["status"],
                old_generation,
                row["last_scanned_ledger_sequence"],
                row["scan_upper_sequence"],
                now_text,
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            connection.rollback()
            return None
        claim = CommissionClaim(
            tenant_id=_text(row["tenant_id"]),
            partner_id=_text(row["partner_id"]),
            partner_customer_id=_text(row["partner_customer_id"]),
            project_id=_text(row["project_id"]),
            lease_owner_id=worker_id,
            lease_token=token,
            generation=generation,
            consecutive_failure_count=_count(row["consecutive_failure_count"]),
            last_scanned_ledger_sequence=_count(row["last_scanned_ledger_sequence"]),
            scan_upper_sequence=upper,
            lease_expires_at=expires,
        )
        connection.commit()
        return claim
    except ApiError:
        connection.rollback()
        raise
    except (sqlite3.Error, TypeError, ValueError):
        connection.rollback()
        raise ApiError("partner.commission_unavailable") from None
    finally:
        connection.close()


def materialize_claimed_commission(
    config: AppConfig, claim: CommissionClaim, *, now: datetime | None = None
) -> int | None:
    """复核租约后物化至多 200 条正式 usage charge，并以 CAS 推进游标。"""

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        now_text = _db_time(current)
        owner = connection.execute(
            """
            SELECT cursor.*,customer.customer_code,customer.display_name,
                   customer.status customer_status,
                   customer.commission_rate_bps,customer.commission_cutoff_sequence
            FROM partner_commission_cursors cursor JOIN partner_customers customer
              ON customer.tenant_id=cursor.tenant_id
             AND customer.partner_customer_id=cursor.partner_customer_id
            WHERE cursor.tenant_id=? AND cursor.partner_customer_id=?
            """,
            (claim.tenant_id, claim.partner_customer_id),
        ).fetchone()
        if owner is None or not _claim_matches(owner, claim, current):
            connection.rollback()
            return None
        high_water = _ledger_high_water(connection, claim.tenant_id)
        cutoff = owner["commission_cutoff_sequence"]
        upper = min(high_water, _count(cutoff)) if cutoff is not None else high_water
        if (
            claim.scan_upper_sequence > upper
            or claim.scan_upper_sequence < claim.last_scanned_ledger_sequence
        ):
            raise ApiError("partner.commission_unavailable")
        rows = connection.execute(
            """
            SELECT * FROM finops_ledger_entries
            WHERE tenant_id=? AND project_id=? AND entry_type='usage_charge'
              AND ledger_sequence>? AND ledger_sequence<=?
            ORDER BY ledger_sequence LIMIT ?
            """,
            (
                claim.tenant_id,
                claim.project_id,
                claim.last_scanned_ledger_sequence,
                claim.scan_upper_sequence,
                COMMISSION_BATCH_SIZE,
            ),
        ).fetchall()
        for row in rows:
            _insert_commission_record(connection, owner, row, now_text)
        next_sequence = (
            _count(rows[-1]["ledger_sequence"])
            if len(rows) == COMMISSION_BATCH_SIZE
            else claim.scan_upper_sequence
        )
        complete = owner["customer_status"] == "disabled" and next_sequence >= _count(
            owner["commission_cutoff_sequence"]
        )
        status = "complete" if complete else "pending"
        if complete:
            next_run_at = None
        elif next_sequence < upper:
            next_run_at = now_text
        else:
            next_run_at = _db_time(current + timedelta(seconds=_idle_poll_seconds(config)))
        connection.execute(
            """
            UPDATE partner_commission_cursors
            SET status=?,last_scanned_ledger_sequence=?,scan_upper_sequence=NULL,
                generation=generation+1,
                lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,next_run_at=?,
                consecutive_failure_count=0,last_error_code=NULL,updated_at=?
            WHERE tenant_id=? AND partner_customer_id=? AND status='leased' AND generation=?
              AND lease_owner_id=? AND lease_token=? AND lease_expires_at=?
              AND last_scanned_ledger_sequence=? AND scan_upper_sequence=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                status,
                next_sequence,
                next_run_at,
                now_text,
                claim.tenant_id,
                claim.partner_customer_id,
                claim.generation,
                claim.lease_owner_id,
                claim.lease_token,
                _db_time(claim.lease_expires_at),
                claim.last_scanned_ledger_sequence,
                claim.scan_upper_sequence,
                now_text,
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            connection.rollback()
            return None
        connection.commit()
        return len(rows)
    except ApiError:
        connection.rollback()
        raise
    except (sqlite3.Error, TypeError, ValueError):
        connection.rollback()
        raise ApiError("partner.commission_unavailable") from None
    finally:
        connection.close()


def fail_claimed_commission(
    config: AppConfig, claim: CommissionClaim, *, now: datetime | None = None
) -> bool:
    """物化失败时按固定退避释放有效租约，不记录源异常正文。"""

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        failures = claim.consecutive_failure_count + 1
        delay = COMMISSION_FAILURE_BACKOFF_SECONDS[
            min(failures - 1, len(COMMISSION_FAILURE_BACKOFF_SECONDS) - 1)
        ]
        now_text = _db_time(current)
        connection.execute(
            """
            UPDATE partner_commission_cursors
            SET status='pending',scan_upper_sequence=NULL,generation=generation+1,
                lease_owner_id=NULL,lease_token=NULL,
                lease_expires_at=NULL,next_run_at=?,consecutive_failure_count=?,
                last_error_code='partner.commission_unavailable',updated_at=?
            WHERE tenant_id=? AND partner_customer_id=? AND status='leased' AND generation=?
              AND lease_owner_id=? AND lease_token=? AND lease_expires_at=?
              AND scan_upper_sequence=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                _db_time(current + timedelta(seconds=delay)),
                failures,
                now_text,
                claim.tenant_id,
                claim.partner_customer_id,
                claim.generation,
                claim.lease_owner_id,
                claim.lease_token,
                _db_time(claim.lease_expires_at),
                claim.scan_upper_sequence,
                now_text,
            ),
        )
        changed = connection.execute("SELECT changes()").fetchone()[0] == 1
        if changed:
            connection.commit()
        else:
            connection.rollback()
        return changed
    except (ApiError, sqlite3.Error, TypeError, ValueError):
        connection.rollback()
        return False
    finally:
        connection.close()


def run_commission_scheduler_once(
    config: AppConfig, *, worker_id: str, now: datetime | None = None
) -> bool:
    """处理一个分润游标；已领取任务的失败收敛为可恢复退避。"""

    claim = claim_next_commission_cursor(config, worker_id=worker_id, now=now)
    if claim is None:
        return False
    try:
        materialize_claimed_commission(config, claim, now=now)
    except Exception:
        fail_claimed_commission(config, claim, now=now)
    return True


def get_partner_commission(
    config: AppConfig,
    context: AuthContext,
    *,
    window: DateWindow,
    customer_id: str | None,
    limit: int,
    offset: int,
    request_id: str,
    trace_id: str,
) -> PartnerCommissionResponse:
    """读取当前 Partner 的不可变分润行、分币种总计与物化新鲜度。"""

    limit, offset = parse_pagination(limit, offset)
    customer_id = normalize_customer_id(customer_id) if customer_id is not None else None
    connection = _connect(config)
    try:
        partner = _partner_row(connection, context.user.tenant_id)
        if customer_id is not None:
            _require_customer(
                connection, context.user.tenant_id, partner["partner_id"], customer_id
            )
        where_sql, params = _report_filter(
            context.user.tenant_id, partner["partner_id"], window, customer_id
        )
        total = _count(
            connection.execute(
                f"SELECT COUNT(*) FROM commission_records WHERE {where_sql}", params
            ).fetchone()[0]
        )
        rows = connection.execute(
            f"""SELECT * FROM commission_records WHERE {where_sql}
                ORDER BY source_created_at,commission_record_sequence LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ).fetchall()
        totals = connection.execute(
            f"""
            SELECT currency,COUNT(*) request_count,COALESCE(SUM(total_tokens),0) total_tokens,
              COALESCE(SUM(amount_micros),0) amount_micros,
              COALESCE(SUM(commission_amount_micros),0) commission_amount_micros,
              COALESCE(SUM(CASE WHEN estimated=1 THEN 1 ELSE 0 END),0) estimated_records
            FROM commission_records WHERE {where_sql} GROUP BY currency ORDER BY currency
            """,
            params,
        ).fetchall()
        freshness = _freshness(
            connection, context.user.tenant_id, partner["partner_id"], customer_id
        )
        return PartnerCommissionResponse(
            window=PartnerWindow(start_date=window.start_text, end_date=window.end_text),
            items=[_commission_line(row) for row in rows],
            totals=[
                CommissionCurrencyTotal(
                    currency=row["currency"],
                    request_count=_count(row["request_count"]),
                    total_tokens=_count(row["total_tokens"]),
                    amount_micros=_count(row["amount_micros"]),
                    commission_amount_micros=_count(row["commission_amount_micros"]),
                    estimated_records=_count(row["estimated_records"]),
                )
                for row in totals
            ],
            cursor=freshness,
            total=total,
            limit=limit,
            offset=offset,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        raise
    except (sqlite3.Error, TypeError, ValueError):
        raise ApiError("partner.commission_unavailable") from None
    finally:
        connection.close()


def export_partner_commission_csv(
    config: AppConfig,
    context: AuthContext,
    *,
    window: DateWindow,
    customer_id: str | None,
    request_id: str,
    trace_id: str,
    now: datetime | None = None,
) -> Attachment:
    """生成带 BOM 的公式安全 CSV，并在返回正文前提交脱敏导出审计。"""

    customer_id = normalize_customer_id(customer_id) if customer_id is not None else None
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for permission in ("partner_portal.read", "partner_commission.export"):
            require_transaction_permission(
                connection, context, permission, unavailable_error_code="partner.unavailable"
            )
        partner = _partner_row(connection, context.user.tenant_id)
        if customer_id is not None:
            _require_customer(
                connection, context.user.tenant_id, partner["partner_id"], customer_id
            )
        where_sql, params = _report_filter(
            context.user.tenant_id, partner["partner_id"], window, customer_id
        )
        grouped_sql = f"""
            SELECT substr(source_created_at,1,10) period_day,partner_customer_id,
              customer_code_snapshot customer_code,display_name_snapshot customer_name,currency,
              COUNT(*) request_count,COALESCE(SUM(total_tokens),0) total_tokens,
              COALESCE(SUM(amount_micros),0) amount_micros,commission_rate_bps,
              COALESCE(SUM(commission_amount_micros),0) commission_amount_micros,
              COALESCE(SUM(CASE WHEN estimated=1 THEN 1 ELSE 0 END),0) estimated_records
            FROM commission_records WHERE {where_sql}
            GROUP BY period_day,partner_customer_id,customer_code_snapshot,
                     display_name_snapshot,currency,commission_rate_bps
            ORDER BY period_day,customer_code,partner_customer_id,currency
        """
        row_count = _count(
            connection.execute(f"SELECT COUNT(*) FROM ({grouped_sql})", params).fetchone()[0]
        )
        if row_count > MAX_CSV_ROWS:
            raise ApiError("partner.export_too_large")
        rows = connection.execute(grouped_sql, params).fetchall()
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, lineterminator="\r\n")
        writer.writerow(
            (
                "period_day",
                "customer_code",
                "customer_name",
                "currency",
                "request_count",
                "total_tokens",
                "amount_micros",
                "commission_rate_bps",
                "commission_amount_micros",
                "estimated_records",
            )
        )
        for row in rows:
            writer.writerow(
                (
                    _csv_text(row["period_day"]),
                    _csv_text(row["customer_code"]),
                    _csv_text(row["customer_name"]),
                    _csv_text(row["currency"]),
                    _count(row["request_count"]),
                    _count(row["total_tokens"]),
                    _count(row["amount_micros"]),
                    _count(row["commission_rate_bps"]),
                    _count(row["commission_amount_micros"]),
                    _count(row["estimated_records"]),
                )
            )
        body = b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8")
        if len(body) > MAX_CSV_BYTES:
            raise ApiError("partner.export_too_large")
        current = _transaction_now(connection, now)
        currencies = sorted({_text(row["currency"]) for row in rows})
        filter_hash = _sha256(
            _canonical_json([window.start_text, window.end_text, customer_id]).encode("utf-8")
        )
        _record_operation(
            connection,
            tenant_id=context.user.tenant_id,
            partner_id=partner["partner_id"],
            action="partner.commission_exported",
            object_id=partner["partner_id"],
            actor_user_id=context.user.user_id,
            request_id=request_id,
            trace_id=trace_id,
            summary={
                "row_count": row_count,
                "currency_count": len(currencies),
                "filter_sha256": filter_hash,
            },
            created_at=_db_time(current),
        )
        connection.commit()
        return Attachment(
            body=body,
            content_type="text/csv; charset=utf-8",
            filename=f"partner-commission-{partner['partner_code']}-{current.date().isoformat()}.csv",
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        connection.rollback()
        raise
    except (sqlite3.Error, TypeError, ValueError, UnicodeError):
        connection.rollback()
        raise ApiError("partner.export_unavailable") from None
    finally:
        connection.close()


def _insert_commission_record(
    connection: sqlite3.Connection, customer: sqlite3.Row, source: sqlite3.Row, created_at: str
) -> None:
    source_id = _text(source["finops_ledger_entry_id"])
    digest = hashlib.sha256(
        _canonical_json(
            [
                "partner-commission-v1",
                customer["tenant_id"],
                customer["partner_customer_id"],
                source_id,
            ]
        ).encode("utf-8")
    ).hexdigest()[:32]
    amount = _count(source["amount_micros"])
    rate = _count(customer["commission_rate_bps"])
    commission = (amount // 10_000) * rate + (((amount % 10_000) * rate + 5_000) // 10_000)
    values = (
        f"com_{digest}",
        customer["tenant_id"],
        customer["partner_id"],
        customer["partner_customer_id"],
        source_id,
        source["ledger_sequence"],
        source["source_request_id"],
        source["source_trace_id"],
        customer["customer_code"],
        customer["display_name"],
        customer["project_id"],
        amount,
        source["currency"],
        rate,
        commission,
        source["input_tokens"],
        source["output_tokens"],
        source["total_tokens"],
        source["cache_tokens"],
        source["estimated"],
        source["created_at"],
        created_at,
        created_at,
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO commission_records(
          commission_record_id,tenant_id,partner_id,partner_customer_id,
          source_finops_ledger_entry_id,source_ledger_sequence,source_request_id,source_trace_id,
          customer_code_snapshot,display_name_snapshot,project_id,amount_micros,currency,
          commission_rate_bps,commission_amount_micros,input_tokens,output_tokens,total_tokens,
          cache_tokens,estimated,status,source_created_at,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'accrued',?,?,?)
        """,
        values,
    )
    if connection.execute("SELECT changes()").fetchone()[0] == 0:
        existing = connection.execute(
            """SELECT * FROM commission_records
                WHERE tenant_id=? AND partner_customer_id=?
                  AND source_finops_ledger_entry_id=?""",
            (customer["tenant_id"], customer["partner_customer_id"], source_id),
        ).fetchone()
        if (
            existing is None
            or tuple(
                existing[name]
                for name in (
                    "commission_record_id",
                    "tenant_id",
                    "partner_id",
                    "partner_customer_id",
                    "source_finops_ledger_entry_id",
                    "source_ledger_sequence",
                    "source_request_id",
                    "source_trace_id",
                    "customer_code_snapshot",
                    "display_name_snapshot",
                    "project_id",
                    "amount_micros",
                    "currency",
                    "commission_rate_bps",
                    "commission_amount_micros",
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                    "cache_tokens",
                    "estimated",
                    "source_created_at",
                )
            )
            != values[:21]
        ):
            raise ApiError("partner.commission_unavailable")


def _claim_matches(row: sqlite3.Row, claim: CommissionClaim, now: datetime) -> bool:
    return (
        row["status"] == "leased"
        and row["tenant_id"] == claim.tenant_id
        and row["partner_id"] == claim.partner_id
        and row["project_id"] == claim.project_id
        and row["generation"] == claim.generation
        and row["lease_owner_id"] == claim.lease_owner_id
        and row["lease_token"] == claim.lease_token
        and row["lease_expires_at"] == _db_time(claim.lease_expires_at)
        and row["last_scanned_ledger_sequence"] == claim.last_scanned_ledger_sequence
        and row["scan_upper_sequence"] == claim.scan_upper_sequence
        and claim.lease_expires_at > now
    )


def _idle_poll_seconds(config: AppConfig) -> float:
    """复用部署级轮询间隔，并把异常配置收敛到安全默认值。"""

    value = getattr(
        config, "partner_portal_scheduler_poll_interval_seconds", COMMISSION_IDLE_POLL_SECONDS
    )
    if type(value) not in {int, float} or isinstance(value, bool):
        return COMMISSION_IDLE_POLL_SECONDS
    return min(max(float(value), 0.1), 300.0)


def _report_filter(
    tenant_id: str, partner_id: str, window: DateWindow, customer_id: str | None
) -> tuple[str, tuple[object, ...]]:
    where = (
        "tenant_id=? AND partner_id=? "
        "AND julianday(source_created_at)>=julianday(?) "
        "AND julianday(source_created_at)<julianday(?)"
    )
    params: tuple[object, ...] = (
        tenant_id,
        partner_id,
        window.start_timestamp,
        window.end_exclusive_timestamp,
    )
    if customer_id is not None:
        where += " AND partner_customer_id=?"
        params += (customer_id,)
    return where, params


def _require_customer(
    connection: sqlite3.Connection, tenant_id: str, partner_id: str, customer_id: str
) -> None:
    if (
        connection.execute(
            """SELECT 1 FROM partner_customers
                WHERE tenant_id=? AND partner_id=? AND partner_customer_id=?""",
            (tenant_id, partner_id, customer_id),
        ).fetchone()
        is None
    ):
        raise ApiError("partner.customer_not_found")


def _freshness(
    connection: sqlite3.Connection, tenant_id: str, partner_id: str, customer_id: str | None
) -> CommissionFreshness:
    where = "customer.tenant_id=? AND customer.partner_id=?"
    params: tuple[object, ...] = (tenant_id, partner_id)
    if customer_id is not None:
        where += " AND customer.partner_customer_id=?"
        params += (customer_id,)
    rows = connection.execute(
        f"""
        SELECT cursor.last_scanned_ledger_sequence,customer.commission_cutoff_sequence
        FROM partner_customers customer JOIN partner_commission_cursors cursor
          ON cursor.tenant_id=customer.tenant_id
         AND cursor.partner_customer_id=customer.partner_customer_id
        WHERE {where}
        """,
        params,
    ).fetchall()
    high_water = _ledger_high_water(connection, tenant_id)
    if not rows:
        materialized = high_water
        current = True
    else:
        materialized = min(_count(row["last_scanned_ledger_sequence"]) for row in rows)
        current = all(
            _count(row["last_scanned_ledger_sequence"])
            >= min(high_water, _count(row["commission_cutoff_sequence"]))
            if row["commission_cutoff_sequence"] is not None
            else _count(row["last_scanned_ledger_sequence"]) >= high_water
            for row in rows
        )
    return CommissionFreshness(
        materialized_through_sequence=materialized,
        source_high_water_sequence=high_water,
        freshness_status="current" if current else "lagging",
    )


def _commission_line(row: sqlite3.Row) -> CommissionLine:
    return CommissionLine(
        commission_record_id=_text(row["commission_record_id"]),
        partner_customer_id=_text(row["partner_customer_id"]),
        customer_code=_text(row["customer_code_snapshot"]),
        customer_name=_text(row["display_name_snapshot"]),
        currency=row["currency"],
        amount_micros=_count(row["amount_micros"]),
        commission_rate_bps=_count(row["commission_rate_bps"]),
        commission_amount_micros=_count(row["commission_amount_micros"]),
        input_tokens=_count(row["input_tokens"]),
        output_tokens=_count(row["output_tokens"]),
        total_tokens=_count(row["total_tokens"]),
        cache_tokens=_count(row["cache_tokens"]),
        estimated=bool(row["estimated"]),
        status=row["status"],
        source_request_id=_text(row["source_request_id"]),
        source_trace_id=_text(row["source_trace_id"]),
        source_created_at=_text(row["source_created_at"]),
    )


def _csv_text(value: object) -> str:
    text = _text(value)
    if any(character in text for character in ("\r", "\n", "\x00")):
        raise ApiError("partner.export_unavailable")
    return f"'{text}" if text[0] in "=+-@" else text


__all__ = [
    "CommissionClaim",
    "claim_next_commission_cursor",
    "export_partner_commission_csv",
    "fail_claimed_commission",
    "get_partner_commission",
    "materialize_claimed_commission",
    "run_commission_scheduler_once",
]
