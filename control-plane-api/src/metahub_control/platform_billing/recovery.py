"""Platform-authorized recovery of the current failed metering fact."""

from __future__ import annotations

import hmac
import json
import sqlite3

from metahub_shared.db.schema import table_exists
from metahub_shared.runtime.errors import ApiError

from . import service as billing
from .models import (
    MeterBlockedFact,
    MeteringStateResponse,
    MeterRecoveryInfo,
    MeterRecoveryRequest,
    MeterRecoveryResponse,
)


def get_metering_state(config, *, session_token, request_id, trace_id):
    connection = billing._connect(config)
    if connection.in_transaction:
        raise ApiError("platform_metering.unavailable")
    try:
        connection.execute("BEGIN")
        _authorize(connection, config, session_token, permission="platform.billing.read")
        _require_recovery_schema(connection)
        checkpoint, head = billing._next_usage_projection(connection)
        pending_count = connection.execute(
            "SELECT COUNT(*) FROM usage_export_ledger_sequences WHERE usage_export_sequence>?",
            (checkpoint["source_sequence"],),
        ).fetchone()[0]
        blocked = None
        if head is not None and head["status"] == "failed":
            workspace_id, can_archive = _owner_and_archive_eligibility(connection, head)
            blocked = MeterBlockedFact(
                meter_outbox_id=head["meter_outbox_id"], source_sequence=head["usage_export_sequence"],
                attempt_count=head["attempt_count"], source_sha256=_source_hash(head, workspace_id),
                workspace_id=workspace_id, occurred_at=billing._parse_time(head["usage_created_at"]),
                error_code=_safe_error_code(head["error_code"]), can_archive_internal_cost=can_archive,
            )
        recent = tuple(_recovery_info(row) for row in connection.execute(
            "SELECT * FROM platform_meter_recovery_actions ORDER BY created_at DESC,recovery_id DESC LIMIT 10"
        ).fetchall())
        response = MeteringStateResponse(
            state="blocked" if blocked else "pending" if head is not None else "idle",
            checkpoint=checkpoint["source_sequence"], pending_count=pending_count,
            blocked_fact=blocked, recent_recoveries=recent, request_id=request_id, trace_id=trace_id,
        )
        connection.rollback()
        connection.execute("BEGIN")
        _authorize(connection, config, session_token, permission="platform.billing.read")
        return response
    except ApiError:
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError, OverflowError):
        raise ApiError("platform_metering.unavailable") from None
    finally:
        billing._rollback(connection)
        connection.close()


def recover_meter_fact(
    config, *, session_token, csrf_token, payload: MeterRecoveryRequest,
    idempotency_key, request_id, trace_id,
):
    if billing._IDEMPOTENCY_RE.fullmatch(idempotency_key) is None:
        raise ApiError("platform_billing.request_invalid")
    key_hash = billing._sha256(idempotency_key)
    request_hash = billing._sha256(json.dumps(
        payload.model_dump(), sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ))
    connection = billing._connect(config)
    if connection.in_transaction:
        raise ApiError("platform_metering.unavailable")
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _authorize(connection, config, session_token, csrf_token=csrf_token)
        _require_recovery_schema(connection)
        existing = connection.execute(
            "SELECT * FROM platform_meter_recovery_actions WHERE operator_id=? AND idempotency_sha256=?",
            (actor.operator_id, key_hash),
        ).fetchone()
        if existing is not None:
            if not hmac.compare_digest(existing["request_sha256"], request_hash):
                raise ApiError("platform_billing.idempotency_conflict")
            _authorize(connection, config, session_token, csrf_token=csrf_token)
            return MeterRecoveryResponse(
                recovery=_recovery_info(existing), idempotent_replay=True,
                request_id=request_id, trace_id=trace_id,
            )
        checkpoint, head = billing._next_usage_projection(connection)
        if (head is None or head["meter_outbox_id"] != payload.meter_outbox_id
                or head["status"] != "failed" or head["usage_export_sequence"] != payload.source_sequence
                or head["attempt_count"] != payload.expected_attempt_count):
            raise ApiError("platform_billing.revision_conflict")
        if payload.action == "internal_cost_only" and getattr(connection, "backend", None) == "postgresql":
            # Freeze contract evidence until the classification commits, including concurrent inserts.
            connection.execute("LOCK TABLE workspace_subscriptions,entitlement_snapshots IN SHARE MODE")
        workspace_id, can_archive = _owner_and_archive_eligibility(connection, head)
        if workspace_id is None:
            raise ApiError("platform_metering.owner_invalid")
        if not hmac.compare_digest(_source_hash(head, workspace_id), payload.expected_source_sha256):
            raise ApiError("platform_billing.revision_conflict")
        if payload.action == "internal_cost_only" and not can_archive:
            raise ApiError("platform_metering.reconciliation_required")
        if connection.execute(
            "SELECT 1 FROM platform_meter_readings WHERE source_fact_id=? LIMIT 1",
            (head["source_usage_ledger_id"],),
        ).fetchone() is not None:
            raise ApiError("platform_metering.reconciliation_required")
        recovery_id, now = billing._id("pmx_"), billing._utcnow()
        connection.execute(
            """INSERT INTO platform_meter_recovery_actions(
                 recovery_id,meter_outbox_id,source_fact_id,source_sequence,tenant_id,workspace_id,
                 operator_id,action,source_attempt_count,source_sha256,idempotency_sha256,
                 request_sha256,request_id,trace_id,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (recovery_id, head["meter_outbox_id"], head["source_usage_ledger_id"],
             head["usage_export_sequence"], head["tenant_id"], workspace_id, actor.operator_id,
             payload.action, head["attempt_count"], payload.expected_source_sha256,
             key_hash, request_hash, request_id, trace_id, billing._db_time(now)),
        )
        if payload.action == "retry":
            changed = connection.execute(
                """UPDATE platform_meter_outbox SET status='pending',error_code=NULL,updated_at=?
                   WHERE meter_outbox_id=? AND status='failed' AND attempt_count=?""",
                (billing._db_time(now), head["meter_outbox_id"], head["attempt_count"]),
            ).rowcount
        else:
            # An explicit classification advances one source position without manufacturing a bill.
            changed = connection.execute(
                """UPDATE platform_meter_checkpoints SET source_sequence=?,source_fact_id=?,updated_at=?
                   WHERE projection_code='usage_ledger_v1' AND source_sequence=?""",
                (head["usage_export_sequence"], head["source_usage_ledger_id"], billing._db_time(now),
                 checkpoint["source_sequence"]),
            ).rowcount
        if changed != 1:
            raise ApiError("platform_billing.revision_conflict")
        _record_recovery_audit(connection, actor.operator_id, recovery_id, payload.action,
                               request_hash, request_id, trace_id, billing._db_time(now))
        result = _recovery_info(connection.execute(
            "SELECT * FROM platform_meter_recovery_actions WHERE recovery_id=?", (recovery_id,),
        ).fetchone())
        _authorize(connection, config, session_token, csrf_token=csrf_token)
        connection.commit()
        return MeterRecoveryResponse(recovery=result, request_id=request_id, trace_id=trace_id)
    except ApiError:
        raise
    except sqlite3.IntegrityError:
        raise ApiError("platform_billing.revision_conflict") from None
    except (sqlite3.Error, KeyError, TypeError, ValueError, OverflowError):
        raise ApiError("platform_metering.unavailable") from None
    finally:
        billing._rollback(connection)
        connection.close()


def _owner_and_archive_eligibility(connection, head):
    owners = connection.execute(
        "SELECT workspace_id FROM workspace_tenant_bindings WHERE tenant_id=?", (head["tenant_id"],)
    ).fetchall()
    if len(owners) != 1:
        return None, False
    workspace_id = owners[0]["workspace_id"]
    if head["status"] != "failed" or head["error_code"] != "platform_metering.owner_invalid":
        return workspace_id, False
    if connection.execute(
        "SELECT 1 FROM platform_meter_readings WHERE source_fact_id=? LIMIT 1",
        (head["source_usage_ledger_id"],),
    ).fetchone() is not None:
        return workspace_id, False
    try:
        quantities = [billing._quantity(head[name]) for name in ("input_tokens", "output_tokens", "total_tokens")]
        if quantities[0] + quantities[1] != quantities[2]:
            return workspace_id, False
        occurred_at = billing._parse_time(head["usage_created_at"])
        subscriptions = connection.execute(
            "SELECT created_at FROM workspace_subscriptions WHERE workspace_id=? AND tenant_id=?",
            (workspace_id, head["tenant_id"]),
        ).fetchall()
        if any(billing._parse_time(row["created_at"]) <= occurred_at for row in subscriptions):
            return workspace_id, False
        snapshots = connection.execute(
            """SELECT snapshot.effective_from,snapshot.effective_until FROM entitlement_snapshots snapshot
               JOIN workspace_subscriptions subscription ON subscription.subscription_id=snapshot.subscription_id
               WHERE subscription.workspace_id=? AND subscription.tenant_id=?""",
            (workspace_id, head["tenant_id"]),
        ).fetchall()
        for row in snapshots:
            start, end = billing._parse_time(row["effective_from"]), billing._optional_time(row["effective_until"])
            if start <= occurred_at and (end is None or end > occurred_at):
                return workspace_id, False
    except (ApiError, TypeError, ValueError, OverflowError):
        return workspace_id, False
    return workspace_id, True


def _source_hash(head, workspace_id):
    evidence = [head[name] for name in (
        "meter_outbox_id", "source_usage_ledger_id", "usage_export_sequence", "tenant_id",
        "request_id", "input_tokens", "output_tokens", "total_tokens", "usage_created_at",
        "status", "error_code", "attempt_count",
    )]
    return billing._sha256(json.dumps([*evidence, workspace_id], separators=(",", ":"), ensure_ascii=True))


def _authorize(connection, config, session_token, *, csrf_token=None, permission="platform.billing.adjust"):
    actor = billing.authenticate_platform_session_in_transaction(
        connection, config, session_token=session_token, csrf_token=csrf_token, touch=False,
    )
    billing._require_platform(actor, permission)
    return actor


def _require_recovery_schema(connection):
    billing._require_schema(connection)
    if not table_exists(connection, "platform_meter_recovery_actions"):
        raise ApiError("platform_metering.unavailable")


def _recovery_info(row):
    return MeterRecoveryInfo(
        recovery_id=row["recovery_id"], source_sequence=row["source_sequence"],
        workspace_id=row["workspace_id"], action=row["action"], created_at=billing._parse_time(row["created_at"]),
    )


def _safe_error_code(value):
    return value if value in {
        "platform_metering.owner_invalid", "platform_metering.source_invalid",
        "platform_metering.meter_unknown", "platform_metering.checkpoint_conflict",
        "platform_metering.unavailable", "platform_billing.reconciliation_required",
    } else "platform_metering.reconciliation_required"


def _record_recovery_audit(connection, operator_id, recovery_id, action, request_hash, request_id, trace_id, now):
    connection.execute(
        """INSERT INTO platform_audit_logs(
             audit_id,actor_domain,actor_id,action_code,object_type,object_id,reason_code,
             request_id,trace_id,before_sha256,after_sha256,occurred_at)
           VALUES(?,'platform',?,'platform.meter_recovered','platform_meter_recovery',?,?,?, ?,NULL,?,?)""",
        (billing._id("pau_"), operator_id, recovery_id, action, request_id, trace_id, request_hash, now),
    )
