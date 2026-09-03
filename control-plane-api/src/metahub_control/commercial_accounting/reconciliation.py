"""Frozen PRD-144 billing-domain reconciliation wrapper."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.platform_access.service import (
    authenticate_platform_session_in_transaction,
)

_RUN_ID = re.compile(r"^rec_[0-9a-f]{32}$", re.ASCII)
_PERMISSION = "platform.commercial.reconciliation.manage"


@dataclass(frozen=True, slots=True)
class BillingDomainReconciliationResult:
    billing_domain_reconciliation_id: str
    source_reconciliation_run_id: str
    status: str
    source_set_sha256: str
    result_sha256: str
    difference_count: int
    generation: int
    completed_at: str


@dataclass(frozen=True, slots=True)
class _Snapshot:
    source_kind: str
    workspace_id: str | None
    tenant_id: str | None
    currency: str | None
    row_count: int
    max_sequence: int
    amount_minor_or_micros: int
    owner_coverage_count: int
    source_sha256: str


def run_billing_domain_reconciliation(
    config: AppConfig,
    *,
    platform_session_token: str,
    source_reconciliation_run_id: str,
    request_id: str,
    trace_id: str,
) -> BillingDomainReconciliationResult:
    """Authenticate, freeze safe source summaries, and materialize one wrapper run."""

    if _RUN_ID.fullmatch(source_reconciliation_run_id) is None:
        raise ApiError("reconciliation.unavailable")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_platform_session_in_transaction(
            connection, config, session_token=platform_session_token, touch=True
        )
        if _PERMISSION not in actor.permissions:
            raise ApiError("platform.permission_denied")
        wrapper_id = "bdr_" + hashlib.sha256(
            source_reconciliation_run_id.encode("ascii")
        ).hexdigest()[:32]
        existing = connection.execute(
            "SELECT * FROM billing_domain_reconciliations "
            "WHERE billing_domain_reconciliation_id=?",
            (wrapper_id,),
        ).fetchone()
        if existing is not None:
            if existing["status"] != "completed":
                raise ApiError("reconciliation.unavailable")
            result = _result(existing)
            connection.commit()
            return result

        source_run = connection.execute(
            "SELECT * FROM reconciliation_runs WHERE reconciliation_run_id=?",
            (source_reconciliation_run_id,),
        ).fetchone()
        if source_run is None or source_run["status"] != "completed":
            raise ApiError("reconciliation.unavailable")
        snapshots = _collect_snapshots(connection, source_run)
        source_set_sha = _canonical_sha256(
            [
                asdict(item)
                for item in sorted(
                    snapshots,
                    key=lambda value: (
                        value.source_kind,
                        value.workspace_id or "",
                        value.tenant_id or "",
                        value.currency or "",
                    ),
                )
            ]
        )
        permission_sha = _canonical_sha256(
            {
                "factor_revision": actor.factor_revision,
                "operator_revision": actor.operator_revision,
                "permissions": actor.permissions,
                "roles": actor.roles,
            }
        )
        stamp = _db_time(datetime.now(UTC))
        connection.execute(
            """
            INSERT INTO billing_domain_reconciliations(
              billing_domain_reconciliation_id,status,source_reconciliation_run_id,
              source_set_sha256,result_sha256,difference_count,generation,
              created_by_operator_id,permission_sha256,created_at,updated_at,completed_at)
            VALUES(?,'processing',?,?,NULL,0,?,?,?, ?,?,NULL)
            """,
            (
                wrapper_id,
                source_reconciliation_run_id,
                source_set_sha,
                int(source_run["generation"]),
                actor.operator_id,
                permission_sha,
                stamp,
                stamp,
            ),
        )
        connection.executemany(
            """
            INSERT INTO billing_domain_reconciliation_source_snapshots(
              billing_domain_reconciliation_source_snapshot_id,
              billing_domain_reconciliation_id,source_kind,workspace_id,tenant_id,currency,
              row_count,max_sequence,amount_minor_or_micros,owner_coverage_count,
              source_sha256)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    _snapshot_id(wrapper_id, item),
                    wrapper_id,
                    item.source_kind,
                    item.workspace_id,
                    item.tenant_id,
                    item.currency,
                    item.row_count,
                    item.max_sequence,
                    item.amount_minor_or_micros,
                    item.owner_coverage_count,
                    item.source_sha256,
                )
                for item in snapshots
            ],
        )
        connection.execute(
            """
            INSERT INTO billing_domain_reconciliation_differences(
              billing_domain_reconciliation_difference_id,
              billing_domain_reconciliation_id,workspace_id,tenant_id,difference_type,
              left_source_ref,right_source_ref,currency,amount_difference_minor,status,
              adjustment_reference,detected_at)
            SELECT
              'bdrd_'||substr(metahub_sha256(json_array(
                ?,difference_type,left_source_type,left_source_id)),1,32),
              ?,workspace_id,tenant_id,difference_type,
              left_source_type||':'||left_source_id,
              CASE WHEN right_source_type IS NULL THEN NULL
                ELSE right_source_type||':'||right_source_id END,
              currency,amount_difference_minor,status,adjustment_reference,detected_at
            FROM reconciliation_differences
            WHERE reconciliation_run_id=?
            ORDER BY reconciliation_difference_id
            """,
            (wrapper_id, wrapper_id, source_reconciliation_run_id),
        )
        difference_count = int(
            connection.execute(
                "SELECT count(*) FROM billing_domain_reconciliation_differences "
                "WHERE billing_domain_reconciliation_id=?",
                (wrapper_id,),
            ).fetchone()[0]
        )
        result_sha = _difference_result_sha(connection, wrapper_id)
        updated = connection.execute(
            """
            UPDATE billing_domain_reconciliations
            SET status='completed',result_sha256=?,difference_count=?,completed_at=?,
              updated_at=?
            WHERE billing_domain_reconciliation_id=? AND status='processing'
            """,
            (result_sha, difference_count, stamp, stamp, wrapper_id),
        ).rowcount
        if updated != 1:
            raise ApiError("reconciliation.unavailable")
        connection.execute(
            """
            INSERT INTO platform_audit_logs(
              audit_id,actor_domain,actor_id,action_code,object_type,object_id,reason_code,
              request_id,trace_id,before_sha256,after_sha256,occurred_at)
            VALUES(?,'platform',?,'commercial_accounting.reconciliation.completed',
              'billing_domain_reconciliation',?,'commercial_accounting.command',
              ?,?,NULL,?,?)
            """,
            (
                f"pau_{uuid4().hex}",
                actor.operator_id,
                wrapper_id,
                request_id,
                trace_id,
                result_sha,
                stamp,
            ),
        )
        row = connection.execute(
            "SELECT * FROM billing_domain_reconciliations "
            "WHERE billing_domain_reconciliation_id=?",
            (wrapper_id,),
        ).fetchone()
        connection.commit()
        return _result(row)
    except ApiError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError, OverflowError):
        if connection.in_transaction:
            connection.rollback()
        raise ApiError("reconciliation.unavailable") from None
    finally:
        connection.close()


def _collect_snapshots(
    connection: sqlite3.Connection, source_run: sqlite3.Row
) -> list[_Snapshot]:
    snapshots = [_legacy_run_snapshot(source_run)]
    for row in connection.execute(
        "SELECT * FROM commercial_accounting_migration_cursors ORDER BY source_kind"
    ):
        snapshots.append(
            _Snapshot(
                source_kind=f"migration:{row['source_kind']}",
                workspace_id=None,
                tenant_id=None,
                currency=None,
                row_count=int(row["source_row_count"]),
                max_sequence=int(row["generation"]),
                amount_minor_or_micros=0,
                owner_coverage_count=int(row["owner_coverage_count"]),
                source_sha256=_canonical_sha256(tuple(row)),
            )
        )
    snapshots.extend(
        _grouped_snapshots(
            connection,
            "invoice_reconciliation_difference",
            """
            SELECT difference.rowid source_sequence,
              difference.reconciliation_difference_id source_id,
              difference.workspace_id,difference.tenant_id,difference.currency,
              difference.amount_difference_minor amount_value,
              difference.difference_type,difference.left_source_type,
              difference.left_source_id,difference.right_source_type,
              difference.right_source_id,difference.status,
              difference.adjustment_reference,difference.state_revision,
              difference.detected_at,binding.status owner_status
            FROM reconciliation_differences difference
            LEFT JOIN workspace_tenant_bindings binding
              ON binding.workspace_id=difference.workspace_id
             AND binding.tenant_id=difference.tenant_id
            WHERE difference.reconciliation_run_id=?
            ORDER BY difference.workspace_id,difference.tenant_id,difference.currency,
              difference.reconciliation_difference_id
            """,
            (source_run["reconciliation_run_id"],),
        )
    )
    snapshots.extend(_commercial_projection_snapshots(connection))
    snapshots.append(_commercial_event_snapshot(connection))
    snapshots.append(_commercial_outbox_snapshot(connection))
    snapshots.extend(_channel_statement_snapshots(connection))
    return snapshots


def _legacy_run_snapshot(row: sqlite3.Row) -> _Snapshot:
    watermarks = tuple(
        int(row[name])
        for name in (
            "order_watermark",
            "payment_watermark",
            "meter_watermark",
            "statement_watermark",
            "invoice_watermark",
        )
    )
    return _Snapshot(
        "invoice_reconciliation_run",
        None,
        None,
        None,
        1,
        max(watermarks),
        0,
        1,
        _canonical_sha256(
            (
                row["reconciliation_run_id"],
                *watermarks,
                row["order_source_sha256"],
                row["payment_source_sha256"],
                row["meter_source_sha256"],
                row["statement_source_sha256"],
                row["invoice_source_sha256"],
                row["generation"],
            )
        ),
    )


def _commercial_projection_snapshots(connection: sqlite3.Connection) -> list[_Snapshot]:
    return _grouped_snapshots(
        connection,
        "subscription_projection",
        """
        SELECT projection.rowid source_sequence,
          projection.workspace_subscription_projection_id source_id,
          projection.workspace_id,projection.tenant_id,projection.currency,
          COALESCE(projection.current_due_minor,0) amount_value,
          projection.subscription_id,projection.subscription_status,
          projection.paid_minor,projection.refunded_minor,projection.invoiced_minor,
          projection.order_status,projection.payment_status,projection.invoice_status,
          projection.dispute_status,projection.projection_state,
          projection.source_watermark,projection.source_sha256,projection.revision,
          projection.projection_generation,projection.projected_at,binding.status owner_status
        FROM workspace_subscription_projections projection
        LEFT JOIN workspace_tenant_bindings binding
          ON binding.workspace_id=projection.workspace_id
         AND binding.tenant_id=projection.tenant_id
        ORDER BY projection.workspace_id,projection.tenant_id,projection.currency,
          projection.workspace_subscription_projection_id
        """,
        (),
    )


def _channel_statement_snapshots(connection: sqlite3.Connection) -> list[_Snapshot]:
    return _grouped_snapshots(
        connection,
        "channel_statement",
        """
        SELECT statement.rowid source_sequence,statement.channel_statement_id source_id,
          statement.owner_workspace_id workspace_id,statement.tenant_id,statement.currency,
          statement.total_commission_micros amount_value,statement.channel_partner_id,
          statement.period_start,statement.period_end,statement.total_amount_micros,
          statement.line_count,statement.reconciliation_sha256,statement.status,
          statement.close_revision,statement.closed_at,binding.status owner_status
        FROM channel_statements statement
        LEFT JOIN workspace_tenant_bindings binding
          ON binding.workspace_id=statement.owner_workspace_id
         AND binding.tenant_id=statement.tenant_id
        ORDER BY statement.owner_workspace_id,statement.tenant_id,statement.currency,
          statement.channel_statement_id
        """,
        (),
    )


def _grouped_snapshots(
    connection: sqlite3.Connection,
    source_kind: str,
    sql: str,
    parameters: tuple[object, ...],
) -> list[_Snapshot]:
    groups: dict[tuple[str, str, str], list[object]] = {}
    for row in connection.execute(sql, parameters):
        key = (str(row["workspace_id"]), str(row["tenant_id"]), str(row["currency"]))
        state = groups.get(key)
        if state is None:
            state = [hashlib.sha256(), 0, 0, 0, 0]
            groups[key] = state
        if row["owner_status"] != "active":
            raise ApiError("reconciliation.unavailable")
        _hash_row(state[0], tuple(row)[:-1])
        state[1] = int(state[1]) + 1
        state[2] = max(int(state[2]), int(row["source_sequence"]))
        state[3] = int(state[3]) + int(row["amount_value"])
        state[4] = int(state[4]) + 1
    return [
        _Snapshot(source_kind, workspace_id, tenant_id, currency, int(state[1]),
                  int(state[2]), int(state[3]), int(state[4]), state[0].hexdigest())
        for (workspace_id, tenant_id, currency), state in sorted(groups.items())
    ]


def _commercial_event_snapshot(connection: sqlite3.Connection) -> _Snapshot:
    cursor = connection.execute(
        """
        SELECT rowid,event_type,object_kind,object_id,owner_sha256,revision,
          payload_sha256,occurred_at
        FROM commercial_accounting_events ORDER BY rowid
        """
    )
    return _global_snapshot("commercial_event", cursor, owner_index=4)


def _commercial_outbox_snapshot(connection: sqlite3.Connection) -> _Snapshot:
    cursor = connection.execute(
        """
        SELECT rowid,commercial_accounting_event_outbox_id,
          commercial_accounting_event_id,status,attempt_count,lease_generation,
          next_attempt_at,dispatched_at,created_at,updated_at
        FROM commercial_accounting_event_outbox ORDER BY rowid
        """
    )
    return _global_snapshot("commercial_outbox", cursor)


def _global_snapshot(
    source_kind: str, cursor: sqlite3.Cursor, *, owner_index: int | None = None
) -> _Snapshot:
    digest = hashlib.sha256()
    count = maximum = coverage = 0
    for row in cursor:
        values = tuple(row)
        _hash_row(digest, values)
        count += 1
        maximum = max(maximum, int(values[0]))
        if owner_index is not None and len(str(values[owner_index])) == 64:
            coverage += 1
    return _Snapshot(
        source_kind, None, None, None, count, maximum, 0,
        count if owner_index is None else coverage, digest.hexdigest()
    )


def _difference_result_sha(connection: sqlite3.Connection, wrapper_id: str) -> str:
    digest = hashlib.sha256()
    cursor = connection.execute(
        """
        SELECT billing_domain_reconciliation_difference_id,workspace_id,tenant_id,
          difference_type,left_source_ref,right_source_ref,currency,
          amount_difference_minor,status,adjustment_reference,detected_at
        FROM billing_domain_reconciliation_differences
        WHERE billing_domain_reconciliation_id=?
        ORDER BY billing_domain_reconciliation_difference_id
        """,
        (wrapper_id,),
    )
    for row in cursor:
        _hash_row(digest, tuple(row))
    return digest.hexdigest()


def _snapshot_id(wrapper_id: str, snapshot: _Snapshot) -> str:
    value = "\0".join(
        (
            wrapper_id,
            snapshot.source_kind,
            snapshot.workspace_id or "",
            snapshot.tenant_id or "",
            snapshot.currency or "",
        )
    )
    return "bdrs_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _result(row: sqlite3.Row) -> BillingDomainReconciliationResult:
    return BillingDomainReconciliationResult(
        billing_domain_reconciliation_id=str(row["billing_domain_reconciliation_id"]),
        source_reconciliation_run_id=str(row["source_reconciliation_run_id"]),
        status=str(row["status"]),
        source_set_sha256=str(row["source_set_sha256"]),
        result_sha256=str(row["result_sha256"]),
        difference_count=int(row["difference_count"]),
        generation=int(row["generation"]),
        completed_at=str(row["completed_at"]),
    )


def _hash_row(digest: object, values: object) -> None:
    payload = json.dumps(
        values, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("ascii")
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(payload)


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode("ascii")
    ).hexdigest()


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("reconciliation.unavailable") from None


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


__all__ = ["BillingDomainReconciliationResult", "run_billing_domain_reconciliation"]
