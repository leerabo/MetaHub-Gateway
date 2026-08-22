"""Database-authoritative metering projection, cycles, summaries, and adjustments."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import unicodedata
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.platform_access.service import (
    PlatformContext,
    authenticate_platform_session_in_transaction,
)
from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    authenticate_workspace_session_in_transaction,
)

from .models import (
    BillingAdjustmentInfo,
    BillingAdjustmentRequest,
    BillingAdjustmentResponse,
    BillingCycleInfo,
    MeterSummaryInfo,
    PlatformBillingCycleInfo,
    PlatformBillingListResponse,
    WorkspaceBillingResponse,
)

_FINANCE_ROLES = frozenset({"owner", "finance_admin"})
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_WORKSPACE_ID_RE = re.compile(r"^wsp_[0-9a-f]{32}$")
_CYCLE_ID_RE = re.compile(r"^pbc_[0-9a-f]{32}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_METER_CODES = ("api_requests", "input_tokens", "output_tokens", "total_tokens")


def project_usage_once(config: AppConfig, *, worker_id: str) -> bool:
    """Project exactly one canonical Usage fact and advance its checkpoint atomically."""

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_schema(connection)
        checkpoint = connection.execute(
            "SELECT * FROM platform_meter_checkpoints WHERE projection_code='usage_ledger_v1'"
        ).fetchone()
        if checkpoint is None:
            raise ApiError("platform_metering.unavailable")
        row = connection.execute(
            """
            SELECT sequence.usage_export_sequence,outbox.*,usage.tenant_id,usage.request_id,
              usage.input_tokens,usage.output_tokens,usage.total_tokens,usage.created_at
            FROM usage_export_ledger_sequences sequence
            JOIN usage_ledger usage ON usage.usage_ledger_id=sequence.usage_ledger_id
            LEFT JOIN platform_meter_outbox outbox
              ON outbox.source_usage_ledger_id=usage.usage_ledger_id
            WHERE sequence.usage_export_sequence>?
            ORDER BY sequence.usage_export_sequence LIMIT 1
            """,
            (int(checkpoint["source_sequence"]),),
        ).fetchone()
        if row is None:
            connection.commit()
            return False
        sequence = int(row["usage_export_sequence"])
        if sequence != int(checkpoint["source_sequence"]) + 1 or row["meter_outbox_id"] is None:
            raise ApiError("platform_metering.checkpoint_conflict")
        if row["status"] == "failed":
            raise ApiError("platform_metering.reconciliation_required")
        if row["status"] != "pending":
            raise ApiError("platform_metering.checkpoint_conflict")
        now = _utcnow()
        lease_token = secrets.token_hex(16)
        claimed = connection.execute(
            """
            UPDATE platform_meter_outbox
            SET status='processing',attempt_count=attempt_count+1,lease_owner_id=?,lease_token=?,
              lease_expires_at=?,updated_at=?
            WHERE meter_outbox_id=? AND status='pending'
            """,
            (
                worker_id,
                lease_token,
                _db_time(now + timedelta(seconds=30)),
                _db_time(now),
                row["meter_outbox_id"],
            ),
        ).rowcount
        if claimed != 1:
            raise ApiError("platform_metering.checkpoint_conflict")
        try:
            _project_usage_row(connection, row, sequence=sequence, now=now)
        except ApiError as exc:
            connection.execute(
                """
                UPDATE platform_meter_outbox
                SET status='failed',lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
                  error_code=?,updated_at=?
                WHERE meter_outbox_id=? AND status='processing' AND lease_token=?
                """,
                (exc.code, _db_time(now), row["meter_outbox_id"], lease_token),
            )
            connection.commit()
            raise
        finished = connection.execute(
            """
            UPDATE platform_meter_outbox
            SET status='completed',lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
              updated_at=?
            WHERE meter_outbox_id=? AND status='processing' AND lease_token=?
            """,
            (_db_time(now), row["meter_outbox_id"], lease_token),
        ).rowcount
        if finished != 1:
            raise ApiError("platform_metering.checkpoint_conflict")
        advanced = connection.execute(
            """
            UPDATE platform_meter_checkpoints
            SET source_sequence=?,source_fact_id=?,updated_at=?
            WHERE projection_code='usage_ledger_v1' AND source_sequence=?
            """,
            (sequence, row["source_usage_ledger_id"], _db_time(now), sequence - 1),
        ).rowcount
        if advanced != 1:
            raise ApiError("platform_metering.checkpoint_conflict")
        connection.commit()
        return True
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError, OverflowError):
        _rollback(connection)
        raise ApiError("platform_metering.unavailable") from None
    finally:
        connection.close()


def close_due_cycles(config: AppConfig, *, limit: int = 50) -> int:
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ApiError("platform_billing.request_invalid")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_schema(connection)
        now = _utcnow()
        rows = connection.execute(
            """
            SELECT * FROM platform_billing_cycles
            WHERE status='open' AND period_ends_at<=?
            ORDER BY period_ends_at,billing_cycle_id LIMIT ?
            """,
            (_db_time(now), limit),
        ).fetchall()
        for row in rows:
            _close_cycle(connection, row, now=now)
        connection.commit()
        return len(rows)
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("platform_billing.unavailable") from None
    finally:
        connection.close()


def get_workspace_billing(
    config: AppConfig,
    *,
    session_token: str,
    cycle_id: str | None,
    request_id: str,
    trace_id: str,
) -> WorkspaceBillingResponse:
    if cycle_id is not None and _CYCLE_ID_RE.fullmatch(cycle_id) is None:
        raise ApiError("platform_billing.request_invalid")
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = authenticate_workspace_session_in_transaction(
            connection, config, session_token=session_token, touch=True
        )
        _require_finance(actor)
        _require_schema(connection)
        ids = tuple(
            str(row[0])
            for row in connection.execute(
                """
                SELECT billing_cycle_id FROM platform_billing_cycles
                WHERE workspace_id=? ORDER BY period_started_at DESC,billing_cycle_id DESC LIMIT 24
                """,
                (actor.workspace_id,),
            ).fetchall()
        )
        selected = cycle_id or (ids[0] if ids else None)
        cycle = (
            _cycle_info(connection, selected, workspace_id=actor.workspace_id)
            if selected
            else None
        )
        connection.commit()
        return WorkspaceBillingResponse(
            cycle=cycle,
            available_cycles=ids,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("platform_billing.unavailable") from None
    finally:
        connection.close()


def list_platform_billing(
    config: AppConfig,
    *,
    session_token: str,
    status: str | None,
    limit: int,
    cursor: str | None,
    request_id: str,
    trace_id: str,
) -> PlatformBillingListResponse:
    if status not in {None, "open", "closed", "reconciliation_required"}:
        raise ApiError("platform_billing.request_invalid")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ApiError("platform_billing.request_invalid")
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = authenticate_platform_session_in_transaction(
            connection, config, session_token=session_token, touch=True
        )
        _require_platform(actor, "platform.billing.read")
        _require_schema(connection)
        after = _decode_cursor(config, actor, status, cursor) if cursor else None
        clauses: list[str] = []
        params: list[object] = []
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if after is not None:
            clauses.append("(updated_at<? OR(updated_at=? AND billing_cycle_id<?))")
            params.extend((after[0], after[0], after[1]))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = connection.execute(
            f"""
            SELECT * FROM platform_billing_cycles{where}
            ORDER BY updated_at DESC,billing_cycle_id DESC LIMIT ?
            """,
            (*params, limit + 1),
        ).fetchall()
        visible = rows[:limit]
        cycles = tuple(
            PlatformBillingCycleInfo(
                **_cycle_info(connection, str(row["billing_cycle_id"])).model_dump(),
                updated_at=_parse_time(row["updated_at"]),
            )
            for row in visible
        )
        next_cursor = None
        if len(rows) > limit and visible:
            tail = visible[-1]
            next_cursor = _encode_cursor(
                config, actor, status, str(tail["updated_at"]), str(tail["billing_cycle_id"])
            )
        connection.commit()
        return PlatformBillingListResponse(
            cycles=cycles,
            next_cursor=next_cursor,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("platform_billing.unavailable") from None
    finally:
        connection.close()


def create_adjustment(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    payload: BillingAdjustmentRequest,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> BillingAdjustmentResponse:
    _validate_adjustment(payload, idempotency_key)
    amount_delta = _signed_money_to_minor(payload.amount_delta)
    if payload.reason_code == "manual_credit" and (
        payload.quantity_delta > 0 or amount_delta > 0
    ):
        raise ApiError("platform_billing.request_invalid")
    if payload.reason_code == "manual_debit" and (
        payload.quantity_delta < 0 or amount_delta < 0
    ):
        raise ApiError("platform_billing.request_invalid")
    if payload.quantity_delta == 0 and amount_delta == 0:
        raise ApiError("platform_billing.request_invalid")
    key_hash = _sha256(idempotency_key)
    request_hash = _sha256(
        json.dumps(payload.model_dump(), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    )
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_platform_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=False,
        )
        _require_platform(actor, "platform.billing.adjust")
        _require_schema(connection)
        existing = connection.execute(
            """
            SELECT adjustment.*,definition.meter_code
            FROM platform_billing_adjustments adjustment
            JOIN platform_billing_summaries summary
              ON summary.billing_summary_id=adjustment.billing_summary_id
            JOIN platform_meter_definitions definition
              ON definition.meter_definition_id=summary.meter_definition_id
            WHERE adjustment.workspace_id=? AND adjustment.idempotency_sha256=?
            """,
            (payload.workspace_id, key_hash),
        ).fetchone()
        if existing is not None:
            if existing["request_sha256"] != request_hash:
                raise ApiError("platform_billing.idempotency_conflict")
            response = _adjustment_response(
                existing, request_id=request_id, trace_id=trace_id, replay=True
            )
            connection.commit()
            return response
        summary = connection.execute(
            """
            SELECT summary.*,definition.meter_code
            FROM platform_billing_summaries summary
            JOIN platform_meter_definitions definition
              ON definition.meter_definition_id=summary.meter_definition_id
            WHERE summary.workspace_id=? AND summary.billing_cycle_id=?
              AND definition.meter_code=?
            """,
            (payload.workspace_id, payload.billing_cycle_id, payload.meter_code),
        ).fetchone()
        if summary is None:
            raise ApiError("platform_billing.not_found")
        if not hmac.compare_digest(
            str(summary["summary_sha256"]), payload.expected_summary_sha256
        ):
            raise ApiError("platform_billing.revision_conflict")
        now = _utcnow()
        adjustment_id = _id("pba_")
        connection.execute(
            """
            INSERT INTO platform_billing_adjustments(
              billing_adjustment_id,workspace_id,tenant_id,billing_cycle_id,
              billing_summary_id,meter_reading_id,actor_domain,actor_operator_id,reason_code,
              quantity_delta,amount_delta_minor,currency,expected_summary_sha256,
              idempotency_sha256,request_sha256,request_id,trace_id,created_at)
            VALUES(?,?,?,?,?,NULL,'platform',?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                adjustment_id,
                summary["workspace_id"],
                summary["tenant_id"],
                summary["billing_cycle_id"],
                summary["billing_summary_id"],
                actor.operator_id,
                payload.reason_code,
                payload.quantity_delta,
                amount_delta,
                summary["currency"],
                summary["summary_sha256"],
                key_hash,
                request_hash,
                request_id,
                trace_id,
                _db_time(now),
            ),
        )
        _platform_audit(
            connection,
            actor=actor,
            adjustment_id=adjustment_id,
            request_id=request_id,
            trace_id=trace_id,
            request_hash=request_hash,
            now=now,
        )
        row = connection.execute(
            """
            SELECT adjustment.*,definition.meter_code
            FROM platform_billing_adjustments adjustment
            JOIN platform_billing_summaries summary
              ON summary.billing_summary_id=adjustment.billing_summary_id
            JOIN platform_meter_definitions definition
              ON definition.meter_definition_id=summary.meter_definition_id
            WHERE adjustment.billing_adjustment_id=?
            """,
            (adjustment_id,),
        ).fetchone()
        response = _adjustment_response(row, request_id=request_id, trace_id=trace_id)
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("platform_billing.unavailable") from None
    finally:
        connection.close()


def export_workspace_reconciliation_csv(
    config: AppConfig,
    *,
    session_token: str,
    cycle_id: str,
) -> tuple[bytes, str]:
    import csv
    import io

    if _CYCLE_ID_RE.fullmatch(cycle_id) is None:
        raise ApiError("platform_billing.request_invalid")
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = authenticate_workspace_session_in_transaction(
            connection, config, session_token=session_token, touch=True
        )
        _require_finance(actor)
        cycle = _cycle_info(connection, cycle_id, workspace_id=actor.workspace_id)
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\r\n")
        writer.writerow(
            (
                "cycle_id",
                "period_start",
                "period_end",
                "meter",
                "unit",
                "usage",
                "allocation",
                "overage",
                "adjustment_quantity",
                "currency",
                "effective_amount",
                "status",
            )
        )
        for meter in cycle.meters:
            writer.writerow(
                tuple(
                    _csv_cell(value)
                    for value in (
                        cycle.billing_cycle_id,
                        _db_time(cycle.period_started_at),
                        _db_time(cycle.period_ends_at),
                        meter.meter_code,
                        meter.unit,
                        meter.usage_quantity,
                        meter.allocated_quantity,
                        meter.overage_quantity,
                        meter.adjustment_quantity,
                        meter.currency,
                        meter.effective_amount,
                        cycle.status,
                    )
                )
            )
        content = ("\ufeff" + output.getvalue()).encode("utf-8")
        if len(content) > 256 * 1024:
            raise ApiError("platform_billing.export_too_large")
        connection.commit()
        return content, f"metahub-billing-{cycle_id[4:12]}.csv"
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("platform_billing.unavailable") from None
    finally:
        connection.close()


def _project_usage_row(
    connection: sqlite3.Connection, row: sqlite3.Row, *, sequence: int, now: datetime
) -> None:
    quantities = {
        "api_requests": 1,
        "input_tokens": _quantity(row["input_tokens"]),
        "output_tokens": _quantity(row["output_tokens"]),
        "total_tokens": _quantity(row["total_tokens"]),
    }
    if quantities["total_tokens"] != quantities["input_tokens"] + quantities["output_tokens"]:
        raise ApiError("platform_metering.source_invalid")
    owner = connection.execute(
        "SELECT workspace_id FROM workspace_tenant_bindings WHERE tenant_id=?",
        (row["tenant_id"],),
    ).fetchall()
    if len(owner) != 1:
        raise ApiError("platform_metering.owner_invalid")
    workspace_id = str(owner[0]["workspace_id"])
    occurred_at = _parse_time(row["created_at"])
    cycle = _ensure_cycle(
        connection,
        workspace_id=workspace_id,
        tenant_id=str(row["tenant_id"]),
        occurred_at=occurred_at,
        now=now,
    )
    definitions = connection.execute(
        "SELECT * FROM platform_meter_definitions WHERE status='active' ORDER BY meter_code"
    ).fetchall()
    if tuple(str(item["meter_code"]) for item in definitions) != tuple(sorted(_METER_CODES)):
        raise ApiError("platform_metering.meter_unknown")
    owner_hash = _sha256(
        json.dumps(
            [
                row["tenant_id"],
                workspace_id,
                row["request_id"],
                row["source_usage_ledger_id"],
                sequence,
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        )
    )
    for definition in definitions:
        meter_code = str(definition["meter_code"])
        reading_id = _id("pmr_")
        connection.execute(
            """
            INSERT INTO platform_meter_readings(
              meter_reading_id,workspace_id,tenant_id,billing_cycle_id,meter_definition_id,
              source_fact_type,source_fact_id,source_sequence,source_owner_sha256,quantity,
              occurred_at,projected_at)
            VALUES(?,?,?,?,?,'usage_ledger',?,?,?,?,?,?)
            """,
            (
                reading_id,
                workspace_id,
                row["tenant_id"],
                cycle["billing_cycle_id"],
                definition["meter_definition_id"],
                row["source_usage_ledger_id"],
                sequence,
                owner_hash,
                quantities[meter_code],
                _db_time(occurred_at),
                _db_time(now),
            ),
        )
        if cycle["status"] != "open" and quantities[meter_code] > 0:
            _late_adjustment(
                connection,
                cycle=cycle,
                definition=definition,
                reading_id=reading_id,
                quantity=quantities[meter_code],
                sequence=sequence,
                now=now,
            )


def _ensure_cycle(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    tenant_id: str,
    occurred_at: datetime,
    now: datetime,
) -> sqlite3.Row:
    source = connection.execute(
        """
        SELECT subscription.subscription_id,subscription.created_at subscription_created_at,
          snapshot.entitlement_snapshot_id,snapshot.plan_version_id,snapshot.entitlements_json,
          snapshot.effective_from,snapshot.effective_until,version.billing_period,
          version.currency,version.unit_price_minor
        FROM workspace_subscriptions subscription
        JOIN entitlement_snapshots snapshot
          ON snapshot.subscription_id=subscription.subscription_id
        JOIN saas_plan_versions version ON version.plan_version_id=snapshot.plan_version_id
        WHERE subscription.workspace_id=? AND subscription.tenant_id=?
          AND snapshot.effective_from<=?
          AND(snapshot.effective_until IS NULL OR snapshot.effective_until>?)
        ORDER BY snapshot.snapshot_version DESC LIMIT 1
        """,
        (workspace_id, tenant_id, _db_time(occurred_at), _db_time(occurred_at)),
    ).fetchone()
    if source is None:
        raise ApiError("platform_metering.owner_invalid")
    anchor = _parse_time(source["effective_from"])
    days = 365 if source["billing_period"] == "yearly" else 30
    elapsed = max(0, (occurred_at - anchor).days // days)
    period_start = anchor + timedelta(days=elapsed * days)
    period_end = period_start + timedelta(days=days)
    effective_until = _optional_time(source["effective_until"])
    if effective_until is not None and effective_until < period_end:
        period_end = effective_until
    if period_end <= period_start:
        raise ApiError("platform_metering.source_invalid")
    existing = connection.execute(
        """
        SELECT * FROM platform_billing_cycles
        WHERE subscription_id=? AND period_started_at=? AND period_ends_at=?
        """,
        (source["subscription_id"], _db_time(period_start), _db_time(period_end)),
    ).fetchone()
    if existing is not None:
        return existing
    trial = connection.execute(
        """
        SELECT 1 FROM subscription_trials WHERE subscription_id=?
          AND starts_at<=? AND ends_at>? LIMIT 1
        """,
        (source["subscription_id"], _db_time(occurred_at), _db_time(occurred_at)),
    ).fetchone()
    base_amount = 0 if trial is not None else int(source["unit_price_minor"])
    cycle_id = _id("pbc_")
    connection.execute(
        """
        INSERT INTO platform_billing_cycles(
          billing_cycle_id,workspace_id,tenant_id,subscription_id,plan_version_id,
          entitlement_snapshot_id,status,period_started_at,period_ends_at,currency,
          base_amount_minor,source_watermark,summary_set_sha256,revision,created_at,updated_at)
        VALUES(?,?,?,?,?,?,'open',?,?,?,?,0,NULL,1,?,?)
        """,
        (
            cycle_id,
            workspace_id,
            tenant_id,
            source["subscription_id"],
            source["plan_version_id"],
            source["entitlement_snapshot_id"],
            _db_time(period_start),
            _db_time(period_end),
            source["currency"],
            base_amount,
            _db_time(now),
            _db_time(now),
        ),
    )
    entitlements = json.loads(source["entitlements_json"])
    for definition in connection.execute(
        "SELECT * FROM platform_meter_definitions WHERE status='active' ORDER BY meter_code"
    ).fetchall():
        key = definition["entitlement_key"]
        allocation = 0 if key is None else _quantity(entitlements.get(str(key)))
        connection.execute(
            """
            INSERT INTO platform_quota_allocations(
              quota_allocation_id,workspace_id,tenant_id,billing_cycle_id,meter_definition_id,
              entitlement_snapshot_id,allocated_quantity,created_at)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                _id("pqa_"),
                workspace_id,
                tenant_id,
                cycle_id,
                definition["meter_definition_id"],
                source["entitlement_snapshot_id"],
                allocation,
                _db_time(now),
            ),
        )
    return connection.execute(
        "SELECT * FROM platform_billing_cycles WHERE billing_cycle_id=?", (cycle_id,)
    ).fetchone()


def _close_cycle(connection: sqlite3.Connection, cycle: sqlite3.Row, *, now: datetime) -> None:
    summary_hashes: list[str] = []
    watermark = 0
    for definition in connection.execute(
        "SELECT * FROM platform_meter_definitions WHERE status='active' ORDER BY meter_code"
    ).fetchall():
        aggregate = connection.execute(
            """
            SELECT COALESCE(SUM(quantity),0) quantity,COALESCE(MAX(source_sequence),0) watermark
            FROM platform_meter_readings
            WHERE billing_cycle_id=? AND meter_definition_id=?
            """,
            (cycle["billing_cycle_id"], definition["meter_definition_id"]),
        ).fetchone()
        allocation = connection.execute(
            """
            SELECT allocated_quantity FROM platform_quota_allocations
            WHERE billing_cycle_id=? AND meter_definition_id=?
            """,
            (cycle["billing_cycle_id"], definition["meter_definition_id"]),
        ).fetchone()
        if allocation is None:
            raise ApiError("platform_billing.reconciliation_required")
        usage = int(aggregate["quantity"])
        allocated = int(allocation["allocated_quantity"])
        overage = max(0, usage - allocated) if definition["entitlement_key"] else 0
        source_rows = connection.execute(
            """
            SELECT meter_reading_id,source_owner_sha256,quantity,source_sequence
            FROM platform_meter_readings
            WHERE billing_cycle_id=? AND meter_definition_id=?
            ORDER BY source_sequence,meter_reading_id
            """,
            (cycle["billing_cycle_id"], definition["meter_definition_id"]),
        ).fetchall()
        source_hash = _sha256(
            json.dumps([tuple(row) for row in source_rows], separators=(",", ":"))
        )
        meter_watermark = int(aggregate["watermark"])
        watermark = max(watermark, meter_watermark)
        amount = (
            int(cycle["base_amount_minor"])
            if definition["billing_mode"] == "subscription_base"
            else 0
        )
        summary_payload = [
            cycle["billing_cycle_id"],
            definition["meter_code"],
            usage,
            allocated,
            overage,
            cycle["currency"],
            amount,
            meter_watermark,
            source_hash,
        ]
        summary_hash = _sha256(json.dumps(summary_payload, separators=(",", ":")))
        summary_hashes.append(summary_hash)
        connection.execute(
            """
            INSERT INTO platform_billing_summaries(
              billing_summary_id,workspace_id,tenant_id,billing_cycle_id,meter_definition_id,
              summary_revision,usage_quantity,allocated_quantity,overage_quantity,currency,
              amount_minor,source_watermark,source_sha256,summary_sha256,created_at)
            VALUES(?,?,?,?,?,1,?,?,?,?,?,?,?,?,?)
            """,
            (
                _id("pbs_"),
                cycle["workspace_id"],
                cycle["tenant_id"],
                cycle["billing_cycle_id"],
                definition["meter_definition_id"],
                usage,
                allocated,
                overage,
                cycle["currency"],
                amount,
                meter_watermark,
                source_hash,
                summary_hash,
                _db_time(now),
            ),
        )
    set_hash = _sha256(json.dumps(summary_hashes, separators=(",", ":")))
    updated = connection.execute(
        """
        UPDATE platform_billing_cycles
        SET status='closed',source_watermark=?,summary_set_sha256=?,revision=revision+1,updated_at=?
        WHERE billing_cycle_id=? AND status='open'
        """,
        (watermark, set_hash, _db_time(now), cycle["billing_cycle_id"]),
    ).rowcount
    if updated != 1:
        raise ApiError("platform_billing.revision_conflict")


def _late_adjustment(
    connection: sqlite3.Connection,
    *,
    cycle: sqlite3.Row,
    definition: sqlite3.Row,
    reading_id: str,
    quantity: int,
    sequence: int,
    now: datetime,
) -> None:
    summary = connection.execute(
        """
        SELECT * FROM platform_billing_summaries
        WHERE billing_cycle_id=? AND meter_definition_id=?
        """,
        (cycle["billing_cycle_id"], definition["meter_definition_id"]),
    ).fetchone()
    if summary is None:
        raise ApiError("platform_billing.reconciliation_required")
    key_hash = _sha256(f"late-usage:{reading_id}")
    request_hash = _sha256(f"late-usage:{sequence}:{quantity}")
    correlation = f"meter-late-{uuid4().hex}"
    connection.execute(
        """
        INSERT INTO platform_billing_adjustments(
          billing_adjustment_id,workspace_id,tenant_id,billing_cycle_id,billing_summary_id,
          meter_reading_id,actor_domain,actor_operator_id,reason_code,quantity_delta,
          amount_delta_minor,currency,expected_summary_sha256,idempotency_sha256,request_sha256,
          request_id,trace_id,created_at)
        VALUES(?,?,?,?,?,?,'system',NULL,'late_usage',?,0,?,?,?,?,?,?,?)
        """,
        (
            _id("pba_"),
            cycle["workspace_id"],
            cycle["tenant_id"],
            cycle["billing_cycle_id"],
            summary["billing_summary_id"],
            reading_id,
            quantity,
            cycle["currency"],
            summary["summary_sha256"],
            key_hash,
            request_hash,
            correlation,
            correlation,
            _db_time(now),
        ),
    )
    connection.execute(
        """
        UPDATE platform_billing_cycles
        SET status='reconciliation_required',revision=revision+1,updated_at=?
        WHERE billing_cycle_id=? AND status='closed'
        """,
        (_db_time(now), cycle["billing_cycle_id"]),
    )


def _cycle_info(
    connection: sqlite3.Connection, cycle_id: str, *, workspace_id: str | None = None
) -> BillingCycleInfo:
    clauses = ["billing_cycle_id=?"]
    params: list[object] = [cycle_id]
    if workspace_id is not None:
        clauses.append("workspace_id=?")
        params.append(workspace_id)
    cycle = connection.execute(
        "SELECT * FROM platform_billing_cycles WHERE " + " AND ".join(clauses), params
    ).fetchone()
    if cycle is None:
        raise ApiError("platform_billing.not_found")
    meters: list[MeterSummaryInfo] = []
    for definition in connection.execute(
        "SELECT * FROM platform_meter_definitions WHERE status='active' ORDER BY meter_code"
    ).fetchall():
        summary = connection.execute(
            """
            SELECT * FROM platform_billing_summaries
            WHERE billing_cycle_id=? AND meter_definition_id=?
            """,
            (cycle_id, definition["meter_definition_id"]),
        ).fetchone()
        allocation = connection.execute(
            """
            SELECT allocated_quantity FROM platform_quota_allocations
            WHERE billing_cycle_id=? AND meter_definition_id=?
            """,
            (cycle_id, definition["meter_definition_id"]),
        ).fetchone()
        if allocation is None:
            raise ApiError("platform_billing.reconciliation_required")
        if summary is None:
            usage = int(
                connection.execute(
                    """
                    SELECT COALESCE(SUM(quantity),0) FROM platform_meter_readings
                    WHERE billing_cycle_id=? AND meter_definition_id=?
                    """,
                    (cycle_id, definition["meter_definition_id"]),
                ).fetchone()[0]
            )
            allocated = int(allocation["allocated_quantity"])
            overage = max(0, usage - allocated) if definition["entitlement_key"] else 0
            amount = (
                int(cycle["base_amount_minor"])
                if definition["billing_mode"] == "subscription_base"
                else 0
            )
            summary_revision = None
            summary_hash = None
        else:
            usage = int(summary["usage_quantity"])
            allocated = int(summary["allocated_quantity"])
            overage = int(summary["overage_quantity"])
            amount = int(summary["amount_minor"])
            summary_revision = int(summary["summary_revision"])
            summary_hash = str(summary["summary_sha256"])
        adjustments = connection.execute(
            """
            SELECT COALESCE(SUM(quantity_delta),0),COALESCE(SUM(amount_delta_minor),0)
            FROM platform_billing_adjustments adjustment
            JOIN platform_billing_summaries summary
              ON summary.billing_summary_id=adjustment.billing_summary_id
            WHERE summary.billing_cycle_id=? AND summary.meter_definition_id=?
            """,
            (cycle_id, definition["meter_definition_id"]),
        ).fetchone()
        adjustment_quantity = int(adjustments[0])
        adjustment_amount = int(adjustments[1])
        meters.append(
            MeterSummaryInfo(
                meter_code=definition["meter_code"],
                unit=definition["unit"],
                billing_mode=definition["billing_mode"],
                usage_quantity=usage,
                allocated_quantity=allocated,
                overage_quantity=overage,
                adjustment_quantity=adjustment_quantity,
                currency=cycle["currency"],
                amount=_minor_to_money(amount),
                adjustment_amount=_signed_minor_to_money(adjustment_amount),
                effective_amount=_signed_minor_to_money(amount + adjustment_amount),
                summary_revision=summary_revision,
                summary_sha256=summary_hash,
            )
        )
    return BillingCycleInfo(
        billing_cycle_id=cycle["billing_cycle_id"],
        workspace_id=cycle["workspace_id"],
        status=cycle["status"],
        plan_version_id=cycle["plan_version_id"],
        period_started_at=_parse_time(cycle["period_started_at"]),
        period_ends_at=_parse_time(cycle["period_ends_at"]),
        currency=cycle["currency"],
        source_watermark=int(cycle["source_watermark"]),
        revision=int(cycle["revision"]),
        meters=tuple(meters),
    )


def _adjustment_response(
    row: sqlite3.Row,
    *,
    request_id: str,
    trace_id: str,
    replay: bool = False,
) -> BillingAdjustmentResponse:
    return BillingAdjustmentResponse(
        adjustment=BillingAdjustmentInfo(
            billing_adjustment_id=row["billing_adjustment_id"],
            workspace_id=row["workspace_id"],
            billing_cycle_id=row["billing_cycle_id"],
            meter_code=row["meter_code"],
            reason_code=row["reason_code"],
            quantity_delta=int(row["quantity_delta"]),
            amount_delta=_signed_minor_to_money(int(row["amount_delta_minor"])),
            currency=row["currency"],
            created_at=_parse_time(row["created_at"]),
        ),
        idempotent_replay=replay,
        request_id=request_id,
        trace_id=trace_id,
    )


def _platform_audit(
    connection: sqlite3.Connection,
    *,
    actor: PlatformContext,
    adjustment_id: str,
    request_id: str,
    trace_id: str,
    request_hash: str,
    now: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO platform_audit_logs(
          audit_id,actor_domain,actor_id,action_code,object_type,object_id,reason_code,
          request_id,trace_id,before_sha256,after_sha256,occurred_at)
        VALUES(?,'platform',?,'platform.billing_adjusted','platform_billing_adjustment',?,
          'billing_reconciliation',?,?,NULL,?,?)
        """,
        (
            _id("pau_"),
            actor.operator_id,
            adjustment_id,
            request_id,
            trace_id,
            request_hash,
            _db_time(now),
        ),
    )


def _validate_adjustment(payload: BillingAdjustmentRequest, idempotency_key: str) -> None:
    if (
        _WORKSPACE_ID_RE.fullmatch(payload.workspace_id) is None
        or _CYCLE_ID_RE.fullmatch(payload.billing_cycle_id) is None
        or _HASH_RE.fullmatch(payload.expected_summary_sha256) is None
        or _IDEMPOTENCY_RE.fullmatch(idempotency_key) is None
    ):
        raise ApiError("platform_billing.request_invalid")


def _encode_cursor(
    config: AppConfig,
    actor: PlatformContext,
    status: str | None,
    updated_at: str,
    cycle_id: str,
) -> str:
    payload = json.dumps(
        [actor.operator_id, actor.operator_revision, status, updated_at, cycle_id],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    signature = hmac.new(
        config.platform_session_secret.encode("utf-8"), b"prd099/cursor\0" + payload, hashlib.sha256
    ).digest()
    return base64.urlsafe_b64encode(payload + signature).decode("ascii").rstrip("=")


def _decode_cursor(
    config: AppConfig,
    actor: PlatformContext,
    status: str | None,
    value: str,
) -> tuple[str, str]:
    if type(value) is not str or not 1 <= len(value) <= 512:
        raise ApiError("platform_billing.request_invalid")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        payload, signature = raw[:-32], raw[-32:]
        expected = hmac.new(
            config.platform_session_secret.encode("utf-8"),
            b"prd099/cursor\0" + payload,
            hashlib.sha256,
        ).digest()
        decoded = json.loads(payload)
    except (ValueError, TypeError, json.JSONDecodeError):
        raise ApiError("platform_billing.request_invalid") from None
    if type(decoded) is not list or len(decoded) != 5:
        raise ApiError("platform_billing.request_invalid")
    if not hmac.compare_digest(signature, expected) or decoded[:3] != [
        actor.operator_id,
        actor.operator_revision,
        status,
    ]:
        raise ApiError("platform_billing.request_invalid")
    if any(type(item) is not str for item in decoded[3:]):
        raise ApiError("platform_billing.request_invalid")
    _parse_time(decoded[3])
    if _CYCLE_ID_RE.fullmatch(decoded[4]) is None:
        raise ApiError("platform_billing.request_invalid")
    return decoded[3], decoded[4]


def _require_schema(connection: sqlite3.Connection) -> None:
    required = {
        "platform_meter_definitions",
        "platform_meter_readings",
        "platform_billing_cycles",
        "platform_quota_allocations",
        "platform_billing_summaries",
        "platform_billing_adjustments",
        "platform_meter_outbox",
        "platform_meter_checkpoints",
    }
    found = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name LIKE 'platform_%'"
        ).fetchall()
    }
    if not required <= found:
        raise ApiError("platform_billing.unavailable")


def _require_finance(actor: WorkspaceSessionContext) -> None:
    if actor.membership_role not in _FINANCE_ROLES:
        raise ApiError("workspace_session.permission_denied")


def _require_platform(actor: PlatformContext, permission: str) -> None:
    if permission not in actor.permissions:
        raise ApiError("platform.permission_denied")


def _quantity(value: object) -> int:
    if type(value) is not int or value < 0 or value > 1_000_000_000_000:
        raise ApiError("platform_metering.source_invalid")
    return value


def _signed_money_to_minor(value: str) -> int:
    if type(value) is not str or re.fullmatch(r"-?(?:0|[1-9][0-9]{0,10})\.[0-9]{2}", value) is None:
        raise ApiError("platform_billing.request_invalid")
    try:
        amount = int(Decimal(value) * 100)
    except (InvalidOperation, ValueError):
        raise ApiError("platform_billing.request_invalid") from None
    if not -1_000_000_000_000 <= amount <= 1_000_000_000_000:
        raise ApiError("platform_billing.request_invalid")
    return amount


def _minor_to_money(value: int) -> str:
    return f"{value // 100}.{value % 100:02d}"


def _signed_minor_to_money(value: int) -> str:
    sign = "-" if value < 0 else ""
    absolute = abs(value)
    return f"{sign}{absolute // 100}.{absolute % 100:02d}"


def _csv_cell(value: object) -> str:
    cleaned = "".join(
        " " if unicodedata.category(char).startswith("C") else char for char in str(value)
    )
    return "'" + cleaned if cleaned.lstrip().startswith(("=", "+", "-", "@")) else cleaned


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except DbError as exc:
        raise ApiError("platform_billing.unavailable") from exc


def _rollback(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        connection.rollback()


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_time(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("invalid time")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _optional_time(value: object) -> datetime | None:
    return None if value is None else _parse_time(value)


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
