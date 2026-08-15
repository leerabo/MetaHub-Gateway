"""验证 PRD-080 0073 正反迁移、争议事实守卫和 Outbox 恢复协议。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import test_prd077_db_migrations as prd077
import test_prd079_db_migrations as prd079
from metahub_control.billing_disputes import outbox as dispute_outbox
from metahub_control.webhooks.service import freeze_webhook_event_delivery_snapshot
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.webhooks import build_webhook_event

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
SEED_PASSWORD = "Prd080MigrationOnly!2026"
NOW = "2026-08-15T00:00:00Z"
NEW_PERMISSIONS = (
    "billing.dispute.read",
    "billing.dispute.create",
    "billing.dispute.review",
)
LEGACY_METADATA_CODES = (
    "file.read",
    "file.write",
    "prompt_template.read",
    "prompt_template.use",
    "prompt_template.write",
)
DISPUTE_TABLES = (
    "billing_disputes",
    "billing_dispute_items",
    "billing_dispute_transitions",
    "billing_settlement_holds",
    "billing_dispute_adjustment_links",
    "billing_dispute_events",
    "billing_dispute_event_outbox",
    "billing_dispute_scheduler_tenants",
)


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _migration_subset(destination: Path, maximum_version: int) -> Path:
    destination.mkdir(parents=True, exist_ok=False)
    for source in MIGRATIONS_DIR.glob("*.sql"):
        version = source.name[:4]
        if version.isdigit() and int(version) <= maximum_version:
            shutil.copy2(source, destination / source.name)
    return destination


@pytest.fixture(scope="module")
def prd080_migration_templates(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, Path]:
    """构造 seeded 0072 模板和相互隔离的迁移目录。"""

    root = tmp_path_factory.mktemp("prd080-migration-templates")
    migrations_0072 = _migration_subset(root / "migrations-0072", 72)
    migrations_0073 = _migration_subset(root / "migrations-0073", 73)
    database = root / "template-0072.sqlite3"
    database_url = _url(database)
    result = apply_migrations(database_url, migrations_dir=migrations_0072)
    assert len(result.applied) == 72 and result.applied[-1] == "0072"
    previous = os.environ.get("METAHUB_SEED_ADMIN_PASSWORD")
    os.environ["METAHUB_SEED_ADMIN_PASSWORD"] = SEED_PASSWORD
    try:
        apply_seed_data(database_url)
    finally:
        if previous is None:
            os.environ.pop("METAHUB_SEED_ADMIN_PASSWORD", None)
        else:
            os.environ["METAHUB_SEED_ADMIN_PASSWORD"] = previous
    return {
        "database_0072": database,
        "migrations_0072": migrations_0072,
        "migrations_0073": migrations_0073,
    }


def _clone(templates: dict[str, Path], destination: Path) -> str:
    shutil.copy2(templates["database_0072"], destination)
    return _url(destination)


def _head(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        """SELECT version FROM schema_migrations WHERE status='applied'
           ORDER BY version DESC LIMIT 1"""
    ).fetchone()
    assert row is not None
    return str(row[0])


def _assert_healthy(connection: sqlite3.Connection) -> None:
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _schema_snapshot(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    return tuple(
        tuple(row)
        for row in connection.execute(
            """SELECT type,name,tbl_name,sql FROM sqlite_schema
               WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"""
        )
    )


def _data_snapshot(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    """冻结 0072 全部业务行和 rowid，仅忽略合法单调递增的角色 revision。"""

    snapshot: list[tuple[object, ...]] = []
    tables = [
        str(row[0])
        for row in connection.execute(
            """SELECT name FROM sqlite_schema WHERE type='table'
               AND name NOT LIKE 'sqlite_%' AND name!='schema_migrations' ORDER BY name"""
        )
    ]
    for table in tables:
        columns = [str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')]
        try:
            rows = connection.execute(f'SELECT rowid,* FROM "{table}" ORDER BY rowid').fetchall()
            offset = 1
        except sqlite3.OperationalError:
            rows = connection.execute(f'SELECT * FROM "{table}"').fetchall()
            offset = 0
        for row in rows:
            values = list(row)
            if table == "roles":
                values[offset + columns.index("permission_revision")] = None
            snapshot.append((table, *values))
    return tuple(snapshot)


def _sequence_snapshot(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    return tuple(
        tuple(row) for row in connection.execute("SELECT * FROM sqlite_sequence ORDER BY name")
    )


def _permission_metadata(
    connection: sqlite3.Connection,
) -> tuple[tuple[object, ...], ...]:
    marks = ",".join("?" for _ in LEGACY_METADATA_CODES)
    return tuple(
        tuple(row)
        for row in connection.execute(
            f"""SELECT permission_code,category,name,description,risk_level
                 FROM rbac_permission_templates WHERE permission_code IN({marks})
                 ORDER BY permission_code""",
            LEGACY_METADATA_CODES,
        )
    )


def _insert_reviewer(connection: sqlite3.Connection, tenant_id: str) -> sqlite3.Row:
    reviewer_id = f"user_prd080_{uuid4().hex}"
    connection.execute(
        """INSERT INTO users(
             user_id,tenant_id,email,display_name,password_hash,role,status,created_at,updated_at
           ) VALUES(?,?,?,'PRD-080 Reviewer','synthetic-hash-only',
                    'finance_admin','active',?,?)""",
        (reviewer_id, tenant_id, f"{reviewer_id}@example.invalid", NOW, NOW),
    )
    row = connection.execute("SELECT * FROM users WHERE user_id=?", (reviewer_id,)).fetchone()
    assert row is not None
    return row


def _insert_endpoint(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    actor_id: str,
    status: str = "active",
) -> str:
    endpoint_id = f"wh_prd080_{uuid4().hex}"
    connection.execute(
        """INSERT INTO webhook_endpoints(
             webhook_endpoint_id,tenant_id,name,endpoint_url,event_types_json,
             encrypted_secret,secret_prefix,secret_fingerprint,status,timeout_ms,
             max_attempts,created_by,updated_by,created_at,updated_at
           ) VALUES(?,?,?,'https://example.invalid/prd080',
                    '["billing_dispute.created"]','synthetic-ciphertext',
                    'whsec_test','prd080-test-fingerprint',?,1000,3,?,?,?,?)""",
        (
            endpoint_id,
            tenant_id,
            "PRD-080 migration endpoint",
            status,
            actor_id,
            actor_id,
            NOW,
            NOW,
        ),
    )
    return endpoint_id


def _audit_summary(
    *,
    status: str,
    revision: int,
    currency_sha256: str,
    reason_sha256: str,
    adjustment_count: int = 0,
) -> dict[str, object]:
    return {
        "status": status,
        "state_revision": revision,
        "scope_type": "request_ids",
        "item_count": 1,
        "adjustment_count": adjustment_count,
        "currency_totals_sha256": currency_sha256,
        "reason_sha256": reason_sha256,
    }


def _insert_audit(
    connection: sqlite3.Connection,
    facts: dict[str, object],
    *,
    action: str,
    actor: sqlite3.Row,
    request_id: str,
    trace_id: str,
    before: dict[str, object],
    after: dict[str, object],
    changed_fields: list[str],
    created_at: str,
    object_name: str | None = None,
) -> None:
    connection.execute(
        """INSERT INTO audit_logs(
             audit_log_id,tenant_id,actor_user_id,actor_email,actor_role,action,
             object_type,object_id,object_name,before_summary_json,after_summary_json,
             changed_fields_json,client_ip,user_agent,request_id,trace_id,created_at
           ) VALUES(?,?,?,?,?,?,'billing_dispute',?,?,?,?,?,NULL,NULL,?,?,?)""",
        (
            f"aud_prd080_{uuid4().hex}",
            facts["tenant_id"],
            actor["user_id"],
            actor["email"],
            actor["role"],
            action,
            facts["dispute_id"],
            object_name,
            _json(before),
            _json(after),
            _json(changed_fields),
            request_id,
            trace_id,
            created_at,
        ),
    )


def _insert_charge(connection: sqlite3.Connection) -> sqlite3.Row:
    """复用租户唯一 Plan 创建新的 request/usage/charge owner 图。"""

    plan = connection.execute("SELECT tenant_id,plan_id FROM plans LIMIT 1").fetchone()
    if plan is None:
        owner = prd079._insert_settlement_owner_facts(
            connection,
            reservation_status="settled",
            reservation_actual_micros=100,
            currency="CNY",
        )
        entry = connection.execute(
            "SELECT * FROM finops_ledger_entries WHERE finops_ledger_entry_id=?",
            (owner[4],),
        ).fetchone()
        assert entry is not None
        return entry
    tenant_id = str(plan["tenant_id"])
    request_id = f"req_prd080_charge_{uuid4().hex}"
    trace_id = f"trc_prd080_charge_{uuid4().hex}"
    reservation_id = f"qres_{uuid4().hex}"
    usage_id = f"usg_{uuid4().hex}"
    connection.execute(
        """INSERT INTO gateway_request_claims(
             tenant_id,request_id,trace_id,virtual_key_id,snapshot_version,
             policy_snapshot_status,claimed_at
           ) VALUES(?,?,?,NULL,NULL,'disabled',?)""",
        (tenant_id, request_id, trace_id, NOW),
    )
    connection.execute(
        """INSERT INTO request_logs(request_id,tenant_id,trace_id,status,created_at,updated_at)
           VALUES(?,?,?,'success',?,?)""",
        (request_id, tenant_id, trace_id, NOW, NOW),
    )
    connection.execute(
        """INSERT INTO quota_reservations(
             quota_reservation_id,tenant_id,plan_id,request_id,trace_id,
             estimated_micros,outstanding_micros,actual_micros,unallocated_micros,
             order_version,status,expires_at,created_at,updated_at
           ) VALUES(?,?,?,?,?,100,0,100,0,'trial_free_gift_paid_v1',
                    'settled',?,?,?)""",
        (reservation_id, tenant_id, plan["plan_id"], request_id, trace_id, NOW, NOW, NOW),
    )
    connection.execute(
        """INSERT INTO usage_ledger(
             usage_ledger_id,tenant_id,request_id,raw_usage,normalized_usage,
             cost_amount,cost_cny,cost_usd,currency,created_at,updated_at
           ) VALUES(?,?,?,'{}','{}',0.000100,0.000100,0,'CNY',?,?)""",
        (usage_id, tenant_id, request_id, NOW, NOW),
    )
    entry = connection.execute(
        """SELECT * FROM finops_ledger_entries
           WHERE tenant_id=? AND usage_ledger_id=? AND entry_type='usage_charge'""",
        (tenant_id, usage_id),
    ).fetchone()
    assert entry is not None
    return entry


def _insert_dispute_source(
    connection: sqlite3.Connection,
    *,
    with_endpoint: bool = False,
    insert_outbox: bool = True,
    insert_audit: bool = True,
    insert_hold: bool = True,
    charge: sqlite3.Row | None = None,
) -> dict[str, object]:
    """写入 submitted 争议的完整 canonical source，不提交调用方事务。"""

    if charge is None:
        charge = _insert_charge(connection)
    tenant_id = str(charge["tenant_id"])
    creator = connection.execute(
        """SELECT * FROM users WHERE tenant_id=? AND role='admin' AND status='active'
           ORDER BY user_id LIMIT 1""",
        (tenant_id,),
    ).fetchone()
    assert creator is not None
    reviewer = _insert_reviewer(connection, tenant_id)
    dispute_id = f"bdsp_{uuid4().hex}"
    item_id = f"bdsi_{uuid4().hex}"
    create_transition_id = f"bdtr_{uuid4().hex}"
    hold_id = f"bhold_{uuid4().hex}"
    event_id = f"bdevt_{uuid4().hex}"
    outbox_id = f"bdob_{uuid4().hex}"
    request_id = f"req_prd080_{uuid4().hex}"
    trace_id = f"trc_prd080_{uuid4().hex}"
    reason = "Synthetic migration dispute"
    reason_hash = _hash(reason)
    currency_totals = {str(charge["currency"]): "0.000100"}
    payload = {
        "schema_version": 1,
        "billing_dispute_id": dispute_id,
        "scope_type": "request_ids",
        "item_count": 1,
        "currency_totals": currency_totals,
        "status": "submitted",
        "created_at": NOW,
    }
    create_idempotency_hash = _hash(f"create-idempotency:{dispute_id}")
    payload_hash = _hash(_json(payload))
    connection.execute(
        """INSERT INTO billing_disputes(
             billing_dispute_id,tenant_id,scope_type,period_start,period_end,status,
             state_revision,item_count,reason,reason_sha256,created_by_user_id,
             reviewer_user_id,decision_reason,decision_reason_sha256,create_request_id,
             create_trace_id,create_idempotency_sha256,payload_sha256,review_started_at,
             resolved_at,created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            dispute_id,
            tenant_id,
            "request_ids",
            None,
            None,
            "submitted",
            1,
            1,
            reason,
            reason_hash,
            creator["user_id"],
            None,
            None,
            None,
            request_id,
            trace_id,
            create_idempotency_hash,
            payload_hash,
            None,
            None,
            NOW,
            NOW,
        ),
    )
    connection.execute(
        """INSERT INTO billing_dispute_items(
             billing_dispute_item_id,tenant_id,billing_dispute_id,
             finops_ledger_entry_id,usage_ledger_id,source_request_id,source_trace_id,
             ledger_sequence,currency,amount_micros,disputable_micros,price_snapshot_id,
             price_schema_version,price_snapshot_source,price_evidence_status,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            item_id,
            tenant_id,
            dispute_id,
            charge["finops_ledger_entry_id"],
            charge["usage_ledger_id"],
            charge["source_request_id"],
            charge["source_trace_id"],
            charge["ledger_sequence"],
            charge["currency"],
            charge["amount_micros"],
            charge["amount_micros"],
            charge["price_snapshot_id"],
            charge["price_schema_version"],
            charge["price_snapshot_source"],
            charge["price_evidence_status"],
            NOW,
        ),
    )
    connection.execute(
        """INSERT INTO billing_dispute_transitions(
             billing_dispute_transition_id,tenant_id,billing_dispute_id,action,
             from_status,to_status,expected_revision,new_revision,actor_user_id,
             decision_reason_sha256,approved_item_count,approved_totals_sha256,
             operation_request_id,operation_trace_id,idempotency_key_sha256,
             payload_sha256,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            create_transition_id,
            tenant_id,
            dispute_id,
            "create",
            None,
            "submitted",
            0,
            1,
            creator["user_id"],
            None,
            0,
            None,
            request_id,
            trace_id,
            create_idempotency_hash,
            payload_hash,
            NOW,
        ),
    )
    if insert_hold:
        connection.execute(
            """INSERT INTO billing_settlement_holds(
                 billing_settlement_hold_id,tenant_id,billing_dispute_id,
                 billing_dispute_item_id,finops_ledger_entry_id,status,release_reason,
                 state_revision,created_transition_id,released_transition_id,
                 created_at,released_at,updated_at
               ) VALUES(?,?,?,?,?,'active',NULL,1,?,NULL,?,NULL,?)""",
            (
                hold_id,
                tenant_id,
                dispute_id,
                item_id,
                charge["finops_ledger_entry_id"],
                create_transition_id,
                NOW,
                NOW,
            ),
        )
    if with_endpoint:
        _insert_endpoint(
            connection,
            tenant_id=tenant_id,
            actor_id=str(creator["user_id"]),
        )
    payload_json = _json(payload)
    connection.execute(
        """INSERT INTO billing_dispute_events(
             billing_dispute_event_id,tenant_id,billing_dispute_id,event_type,payload_json,
             actor_user_id,source_request_id,source_trace_id,occurred_at,created_at
           ) VALUES(?,?,?,'billing_dispute.created',?,?,?,?,?,?)""",
        (
            event_id,
            tenant_id,
            dispute_id,
            payload_json,
            creator["user_id"],
            request_id,
            trace_id,
            NOW,
            NOW,
        ),
    )
    event = build_webhook_event(
        tenant_id=tenant_id,
        event_type="billing_dispute.created",
        event_id=event_id,
        timestamp=datetime.fromisoformat(NOW.replace("Z", "+00:00")),
        actor_id=str(creator["user_id"]),
        object_id=dispute_id,
        trace_id=trace_id,
        source_request_id=request_id,
        payload=payload,
    )
    freeze_webhook_event_delivery_snapshot(connection, event, frozen_at=NOW)
    delivery_ids = [
        str(row[0])
        for row in connection.execute(
            """SELECT webhook_delivery_id FROM webhook_deliveries
               WHERE tenant_id=? AND event_id=? AND delivery_kind='original'
               ORDER BY webhook_delivery_id""",
            (tenant_id, event_id),
        )
    ]
    if insert_outbox:
        connection.execute(
            """INSERT INTO billing_dispute_event_outbox(
                 billing_dispute_event_outbox_id,tenant_id,billing_dispute_event_id,
                 status,attempt_count,lease_generation,lease_owner_id,lease_token,
                 lease_expires_at,next_attempt_at,delivery_snapshot_json,snapshot_frozen_at,
                 last_error_code,succeeded_at,created_at,updated_at
               ) VALUES(?,?,?,'pending',0,0,NULL,NULL,NULL,?,?,?,NULL,NULL,?,?)""",
            (outbox_id, tenant_id, event_id, NOW, _json(delivery_ids), NOW, NOW, NOW),
        )
    facts: dict[str, object] = {
        "tenant_id": tenant_id,
        "creator": creator,
        "reviewer": reviewer,
        "dispute_id": dispute_id,
        "item_id": item_id,
        "hold_id": hold_id,
        "event_id": event_id,
        "outbox_id": outbox_id,
        "create_transition_id": create_transition_id,
        "charge": charge,
        "reason_sha256": reason_hash,
        "currency_totals": currency_totals,
        "currency_sha256": _hash(_json(currency_totals)),
        "delivery_ids": delivery_ids,
    }
    if insert_audit:
        _insert_audit(
            connection,
            facts,
            action="billing_dispute.created",
            actor=creator,
            request_id=request_id,
            trace_id=trace_id,
            before={},
            after=_audit_summary(
                status="submitted",
                revision=1,
                currency_sha256=str(facts["currency_sha256"]),
                reason_sha256=reason_hash,
            ),
            changed_fields=[
                "adjustment_count",
                "currency_totals_sha256",
                "item_count",
                "reason_sha256",
                "scope_type",
                "state_revision",
                "status",
            ],
            created_at=NOW,
        )
    return facts


def _start_review(connection: sqlite3.Connection, facts: dict[str, object]) -> None:
    timestamp = "2026-08-15T00:01:00Z"
    transition_id = f"bdtr_{uuid4().hex}"
    request_id = f"req_prd080_review_{uuid4().hex}"
    trace_id = f"trc_prd080_review_{uuid4().hex}"
    reviewer = facts["reviewer"]
    assert isinstance(reviewer, sqlite3.Row)
    connection.execute(
        """UPDATE billing_disputes SET status='under_review',state_revision=2,
             reviewer_user_id=?,review_started_at=?,updated_at=?
           WHERE tenant_id=? AND billing_dispute_id=?""",
        (reviewer["user_id"], timestamp, timestamp, facts["tenant_id"], facts["dispute_id"]),
    )
    connection.execute(
        """INSERT INTO billing_dispute_transitions(
             billing_dispute_transition_id,tenant_id,billing_dispute_id,action,
             from_status,to_status,expected_revision,new_revision,actor_user_id,
             decision_reason_sha256,approved_item_count,approved_totals_sha256,
             operation_request_id,operation_trace_id,idempotency_key_sha256,
             payload_sha256,created_at
           ) VALUES(?,?,?,'start_review','submitted','under_review',1,2,?,NULL,0,NULL,?,?,?,?,?)""",
        (
            transition_id,
            facts["tenant_id"],
            facts["dispute_id"],
            reviewer["user_id"],
            request_id,
            trace_id,
            _hash(f"transition-idempotency:{transition_id}"),
            _hash(f"transition-payload:{transition_id}"),
            timestamp,
        ),
    )
    empty_hash = _hash("{}")
    _insert_audit(
        connection,
        facts,
        action="billing_dispute.review_started",
        actor=reviewer,
        request_id=request_id,
        trace_id=trace_id,
        before=_audit_summary(
            status="submitted",
            revision=1,
            currency_sha256=empty_hash,
            reason_sha256=str(facts["reason_sha256"]),
        ),
        after=_audit_summary(
            status="under_review",
            revision=2,
            currency_sha256=empty_hash,
            reason_sha256=str(facts["reason_sha256"]),
        ),
        changed_fields=["state_revision", "status"],
        created_at=timestamp,
    )
    facts["review_transition_id"] = transition_id


def _insert_reversal(
    connection: sqlite3.Connection,
    facts: dict[str, object],
    *,
    transition_id: str,
    request_id: str,
    trace_id: str,
    created_at: str,
) -> None:
    target = facts["charge"]
    reviewer = facts["reviewer"]
    assert isinstance(target, sqlite3.Row) and isinstance(reviewer, sqlite3.Row)
    adjustment_id = f"fadj_{uuid4().hex}"
    reversal_id = f"fle_{uuid4().hex}"
    reason = "Approved billing dispute adjustment"
    connection.execute(
        """INSERT INTO finops_adjustments(
             finops_adjustment_id,tenant_id,adjustment_type,amount_micros,currency,
             reversal_of_entry_id,related_request_id,reason,reason_sha256,actor_user_id,
             operation_request_id,operation_trace_id,idempotency_key_sha256,payload_sha256,
             status,created_at,updated_at
           ) VALUES(?,?,'reversal',?,?,?,NULL,?,?,?,?,?,?,?,'posted',?,?)""",
        (
            adjustment_id,
            facts["tenant_id"],
            target["amount_micros"],
            target["currency"],
            target["finops_ledger_entry_id"],
            reason,
            _hash(reason),
            reviewer["user_id"],
            request_id,
            trace_id,
            _hash(f"finops-idempotency:{transition_id}"),
            _hash(f"finops-payload:{transition_id}"),
            created_at,
            created_at,
        ),
    )
    connection.execute(
        """INSERT INTO finops_ledger_entries(
             finops_ledger_entry_id,event_id,tenant_id,entry_type,direction,amount_micros,
             currency,normalized_cny_micros,cny_normalization_status,price_evidence_status,
             price_snapshot_source,posting_source,quota_type,usage_ledger_id,
             finops_adjustment_id,source_request_id,source_trace_id,operation_request_id,
             operation_trace_id,price_snapshot_id,price_schema_version,reversal_of_entry_id,
             actor_user_id,input_tokens,output_tokens,total_tokens,cache_tokens,model_id,
             provider_id,virtual_key_id,environment_id,project_id,application_id,estimated,
             status,created_at,updated_at
           ) VALUES(?, ?, ?, 'reversal', ?, ?, ?, ?, ?, ?, ?, 'direct', ?, NULL,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    'posted', ?, ?)""",
        (
            reversal_id,
            f"flevt_{uuid4().hex}",
            facts["tenant_id"],
            "credit" if target["direction"] == "debit" else "debit",
            target["amount_micros"],
            target["currency"],
            target["normalized_cny_micros"],
            target["cny_normalization_status"],
            target["price_evidence_status"],
            target["price_snapshot_source"],
            target["quota_type"],
            adjustment_id,
            target["source_request_id"],
            target["source_trace_id"],
            request_id,
            trace_id,
            target["price_snapshot_id"],
            target["price_schema_version"],
            target["finops_ledger_entry_id"],
            reviewer["user_id"],
            target["input_tokens"],
            target["output_tokens"],
            target["total_tokens"],
            target["cache_tokens"],
            target["model_id"],
            target["provider_id"],
            target["virtual_key_id"],
            target["environment_id"],
            target["project_id"],
            target["application_id"],
            target["estimated"],
            created_at,
            created_at,
        ),
    )
    connection.execute(
        """INSERT INTO billing_dispute_adjustment_links(
             billing_dispute_adjustment_link_id,tenant_id,billing_dispute_id,
             billing_dispute_item_id,finops_adjustment_id,reversal_finops_ledger_entry_id,
             amount_micros,currency,billing_dispute_transition_id,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            f"bdal_{uuid4().hex}",
            facts["tenant_id"],
            facts["dispute_id"],
            facts["item_id"],
            adjustment_id,
            reversal_id,
            target["amount_micros"],
            target["currency"],
            transition_id,
            created_at,
        ),
    )
    facts["adjustment_id"] = adjustment_id
    facts["reversal_id"] = reversal_id


def _finish_dispute(
    connection: sqlite3.Connection,
    facts: dict[str, object],
    *,
    action: str,
) -> None:
    assert action in {"approve", "reject", "cancel"}
    timestamp = "2026-08-15T00:02:00Z"
    transition_id = f"bdtr_{uuid4().hex}"
    request_id = f"req_prd080_{action}_{uuid4().hex}"
    trace_id = f"trc_prd080_{action}_{uuid4().hex}"
    decision = f"Synthetic {action} decision"
    decision_hash = _hash(decision)
    creator = facts["creator"]
    reviewer = facts["reviewer"]
    assert isinstance(creator, sqlite3.Row) and isinstance(reviewer, sqlite3.Row)
    actor = creator if action == "cancel" else reviewer
    from_status = "submitted" if action == "cancel" else "under_review"
    to_status = {"approve": "approved", "reject": "rejected", "cancel": "cancelled"}[action]
    expected = 1 if action == "cancel" else 2
    revision = expected + 1
    approved_count = 1 if action == "approve" else 0
    approved_hash = str(facts["currency_sha256"]) if action == "approve" else None
    connection.execute(
        """UPDATE billing_disputes SET status=?,state_revision=?,decision_reason=?,
             decision_reason_sha256=?,resolved_at=?,updated_at=?
           WHERE tenant_id=? AND billing_dispute_id=?""",
        (
            to_status,
            revision,
            decision,
            decision_hash,
            timestamp,
            timestamp,
            facts["tenant_id"],
            facts["dispute_id"],
        ),
    )
    connection.execute(
        """INSERT INTO billing_dispute_transitions(
             billing_dispute_transition_id,tenant_id,billing_dispute_id,action,
             from_status,to_status,expected_revision,new_revision,actor_user_id,
             decision_reason_sha256,approved_item_count,approved_totals_sha256,
             operation_request_id,operation_trace_id,idempotency_key_sha256,
             payload_sha256,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            transition_id,
            facts["tenant_id"],
            facts["dispute_id"],
            action,
            from_status,
            to_status,
            expected,
            revision,
            actor["user_id"],
            decision_hash,
            approved_count,
            approved_hash,
            request_id,
            trace_id,
            _hash(f"transition-idempotency:{transition_id}"),
            _hash(f"transition-payload:{transition_id}"),
            timestamp,
        ),
    )
    if action == "approve":
        _insert_reversal(
            connection,
            facts,
            transition_id=transition_id,
            request_id=request_id,
            trace_id=trace_id,
            created_at=timestamp,
        )
    connection.execute(
        """UPDATE billing_settlement_holds SET status='released',release_reason=?,
             state_revision=2,released_transition_id=?,released_at=?,updated_at=?
           WHERE tenant_id=? AND billing_dispute_id=? AND status='active' AND state_revision=1""",
        (
            to_status,
            transition_id,
            timestamp,
            timestamp,
            facts["tenant_id"],
            facts["dispute_id"],
        ),
    )
    empty_hash = _hash("{}")
    _insert_audit(
        connection,
        facts,
        action={
            "approve": "billing_dispute.approved",
            "reject": "billing_dispute.rejected",
            "cancel": "billing_dispute.cancelled",
        }[action],
        actor=actor,
        request_id=request_id,
        trace_id=trace_id,
        before=_audit_summary(
            status=from_status,
            revision=expected,
            currency_sha256=empty_hash,
            reason_sha256=str(facts["reason_sha256"]),
        ),
        after=_audit_summary(
            status=to_status,
            revision=revision,
            currency_sha256=approved_hash or empty_hash,
            reason_sha256=str(facts["reason_sha256"]),
            adjustment_count=approved_count,
        ),
        changed_fields=(
            ["adjustment_count", "currency_totals_sha256", "state_revision", "status"]
            if action == "approve"
            else ["state_revision", "status"]
        ),
        created_at=timestamp,
    )
    facts["terminal_transition_id"] = transition_id


def test_prd080_empty_zero_to_73_repeat_down_reup_are_healthy(tmp_path: Path) -> None:
    """从空库真实验证 73 -> repeat -> 72 -> 73 四阶段健康合同。"""

    migrations = _migration_subset(tmp_path / "migrations-0073", 73)
    database_url = _url(tmp_path / "empty-roundtrip.sqlite3")
    fresh = apply_migrations(database_url, migrations_dir=migrations)
    assert len(fresh.applied) == 73 and fresh.applied[-1] == "0073"
    with connect_database(database_url) as connection:
        assert _head(connection) == "0073"
        assert tuple(
            row[1]
            for row in connection.execute(
                "PRAGMA table_info('billing_dispute_scheduler_tenants')"
            ).fetchall()
        ) == (
            "tenant_id",
            "last_claimed_at",
            "last_claim_sequence",
            "state_revision",
            "created_at",
            "updated_at",
        )
        _assert_healthy(connection)
    repeated = apply_migrations(database_url, migrations_dir=migrations)
    assert repeated.applied == [] and len(repeated.skipped) == 73
    with connect_database(database_url) as connection:
        assert _head(connection) == "0073"
        _assert_healthy(connection)
    down = rollback_last_migration(
        database_url,
        migrations_dir=migrations,
        expected_version="0073",
    )
    assert down.rolled_back == "0073"
    with connect_database(database_url) as connection:
        assert _head(connection) == "0072"
        assert not any(
            connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (table,)
            ).fetchone()
            for table in DISPUTE_TABLES
        )
        _assert_healthy(connection)
    assert apply_migrations(database_url, migrations_dir=migrations).applied == ["0073"]
    with connect_database(database_url) as connection:
        assert _head(connection) == "0073"
        _assert_healthy(connection)


def test_prd080_seeded_roundtrip_preserves_0072_rows_schema_and_metadata(
    prd080_migration_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """验证 down 精确恢复 0072 schema、业务行、rowid、sequence 和旧权限文案。"""

    database_url = _clone(prd080_migration_templates, tmp_path / "seeded-roundtrip.sqlite3")
    with connect_database(database_url) as connection:
        prd077._seed_legacy_audit(connection)
        connection.commit()
        schema_before = _schema_snapshot(connection)
        data_before = _data_snapshot(connection)
        sequence_before = _sequence_snapshot(connection)
        metadata_before = _permission_metadata(connection)
        assert len(metadata_before) == len(LEGACY_METADATA_CODES)

    assert apply_migrations(
        database_url,
        migrations_dir=prd080_migration_templates["migrations_0073"],
    ).applied == ["0073"]
    with connect_database(database_url) as connection:
        assert tuple(
            connection.execute(
                """SELECT COUNT(*),MIN(catalog_revision),MAX(catalog_revision)
                   FROM rbac_permission_templates"""
            ).fetchone()
        ) == (79, 16, 16)
        assert (
            connection.execute("SELECT COUNT(*) FROM rbac_role_permission_templates").fetchone()[0]
            == 208
        )
        assert _permission_metadata(connection) == metadata_before
        _assert_healthy(connection)

    down = rollback_last_migration(
        database_url,
        migrations_dir=prd080_migration_templates["migrations_0073"],
        expected_version="0073",
    )
    assert down.rolled_back == "0073"
    with connect_database(database_url) as connection:
        assert _head(connection) == "0072"
        assert _schema_snapshot(connection) == schema_before
        assert _data_snapshot(connection) == data_before
        assert _sequence_snapshot(connection) == sequence_before
        assert _permission_metadata(connection) == metadata_before
        _assert_healthy(connection)

    assert apply_migrations(
        database_url,
        migrations_dir=prd080_migration_templates["migrations_0073"],
    ).applied == ["0073"]
    with connect_database(database_url) as connection:
        assert _permission_metadata(connection) == metadata_before
        _assert_healthy(connection)


def test_prd080_audit_actions_hold_revision_and_owner_guards(
    prd080_migration_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """实际写入五类 Audit，并验证 hold 1->2、owner、FK、immutable 与 CAS 负路径。"""

    database_url = _clone(prd080_migration_templates, tmp_path / "audit-owner.sqlite3")
    assert apply_migrations(
        database_url,
        migrations_dir=prd080_migration_templates["migrations_0073"],
    ).applied == ["0073"]
    with connect_database(database_url) as connection:
        approved = _insert_dispute_source(connection)
        _start_review(connection, approved)
        _finish_dispute(connection, approved, action="approve")
        rejected = _insert_dispute_source(connection)
        _start_review(connection, rejected)
        _finish_dispute(connection, rejected, action="reject")
        cancelled = _insert_dispute_source(connection)
        _finish_dispute(connection, cancelled, action="cancel")

        actions = tuple(
            (str(row[0]), int(row[1]))
            for row in connection.execute(
                """SELECT action,COUNT(*) FROM audit_logs
                   WHERE action LIKE 'billing_dispute.%' GROUP BY action ORDER BY action"""
            )
        )
        assert actions == (
            ("billing_dispute.approved", 1),
            ("billing_dispute.cancelled", 1),
            ("billing_dispute.created", 3),
            ("billing_dispute.rejected", 1),
            ("billing_dispute.review_started", 2),
        )
        for row in connection.execute(
            """SELECT action,object_name,before_summary_json,after_summary_json,
                      changed_fields_json FROM audit_logs
               WHERE action LIKE 'billing_dispute.%'"""
        ):
            assert row["object_name"] is None
            before = json.loads(str(row["before_summary_json"]))
            after = json.loads(str(row["after_summary_json"]))
            assert set(after) == {
                "status",
                "state_revision",
                "scope_type",
                "item_count",
                "adjustment_count",
                "currency_totals_sha256",
                "reason_sha256",
            }
            assert before == {} or set(before) == set(after)
            expected_changed = (
                [
                    "adjustment_count",
                    "currency_totals_sha256",
                    "item_count",
                    "reason_sha256",
                    "scope_type",
                    "state_revision",
                    "status",
                ]
                if row["action"] == "billing_dispute.created"
                else (
                    [
                        "adjustment_count",
                        "currency_totals_sha256",
                        "state_revision",
                        "status",
                    ]
                    if row["action"] == "billing_dispute.approved"
                    else ["state_revision", "status"]
                )
            )
            assert json.loads(str(row["changed_fields_json"])) == expected_changed
        assert (
            connection.execute(
                """SELECT COUNT(*) FROM billing_settlement_holds
               WHERE status='released' AND state_revision=2"""
            ).fetchone()[0]
            == 3
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM billing_settlement_holds WHERE state_revision!=2"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM billing_dispute_adjustment_links").fetchone()[
                0
            ]
            == 1
        )

        with pytest.raises(sqlite3.IntegrityError, match="billing_dispute_item_immutable"):
            connection.execute(
                """UPDATE billing_dispute_items SET amount_micros=amount_micros+1
                   WHERE billing_dispute_item_id=?""",
                (approved["item_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="billing_dispute_transition_invalid"):
            connection.execute(
                """UPDATE billing_disputes SET state_revision=state_revision+2,updated_at=?
                   WHERE billing_dispute_id=?""",
                ("2026-08-15T00:03:00Z", cancelled["dispute_id"]),
            )
        with pytest.raises(
            sqlite3.IntegrityError, match="billing_dispute_outbox_transition_invalid"
        ):
            connection.execute(
                """UPDATE billing_dispute_event_outbox SET attempt_count=attempt_count+1,
                     lease_generation=lease_generation+1,updated_at=?
                   WHERE billing_dispute_event_outbox_id=?""",
                ("2026-08-15T00:03:00Z", cancelled["outbox_id"]),
            )
        with pytest.raises(
            sqlite3.IntegrityError, match="billing_dispute_adjustment_link_owner_mismatch"
        ):
            connection.execute(
                """INSERT INTO billing_dispute_adjustment_links(
                     billing_dispute_adjustment_link_id,tenant_id,billing_dispute_id,
                     billing_dispute_item_id,finops_adjustment_id,
                     reversal_finops_ledger_entry_id,amount_micros,currency,
                     billing_dispute_transition_id,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"bdal_{uuid4().hex}",
                    rejected["tenant_id"],
                    rejected["dispute_id"],
                    rejected["item_id"],
                    approved["adjustment_id"],
                    approved["reversal_id"],
                    100,
                    "CNY",
                    f"bdtr_{uuid4().hex}",
                    "2026-08-15T00:03:00Z",
                ),
            )
        _assert_healthy(connection)


def test_prd080_audit_rejects_object_name_and_non_exact_changed_fields(
    prd080_migration_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """Audit owner 必须拒绝名称泄漏和动作不匹配字段，再接受精确七字段摘要。"""

    database_url = _clone(prd080_migration_templates, tmp_path / "audit-negative.sqlite3")
    assert apply_migrations(
        database_url,
        migrations_dir=prd080_migration_templates["migrations_0073"],
    ).applied == ["0073"]
    with connect_database(database_url) as connection:
        facts = _insert_dispute_source(connection, insert_audit=False)
        creator = facts["creator"]
        assert isinstance(creator, sqlite3.Row)
        after = _audit_summary(
            status="submitted",
            revision=1,
            currency_sha256=str(facts["currency_sha256"]),
            reason_sha256=str(facts["reason_sha256"]),
        )
        changed = [
            "adjustment_count",
            "currency_totals_sha256",
            "item_count",
            "reason_sha256",
            "scope_type",
            "state_revision",
            "status",
        ]
        transition = connection.execute(
            """SELECT operation_request_id,operation_trace_id FROM billing_dispute_transitions
               WHERE billing_dispute_transition_id=?""",
            (facts["create_transition_id"],),
        ).fetchone()
        assert transition is not None
        with pytest.raises(sqlite3.IntegrityError, match="audit_log_owner_mismatch"):
            _insert_audit(
                connection,
                facts,
                action="billing_dispute.created",
                actor=creator,
                request_id=str(transition["operation_request_id"]),
                trace_id=str(transition["operation_trace_id"]),
                before={},
                after=after,
                changed_fields=changed,
                created_at=NOW,
                object_name="must-not-persist",
            )
        with pytest.raises(sqlite3.IntegrityError, match="audit_log_owner_mismatch"):
            _insert_audit(
                connection,
                facts,
                action="billing_dispute.created",
                actor=creator,
                request_id=str(transition["operation_request_id"]),
                trace_id=str(transition["operation_trace_id"]),
                before={},
                after=after,
                changed_fields=[*changed, "reason"],
                created_at=NOW,
            )
        _insert_audit(
            connection,
            facts,
            action="billing_dispute.created",
            actor=creator,
            request_id=str(transition["operation_request_id"]),
            trace_id=str(transition["operation_trace_id"]),
            before={},
            after=after,
            changed_fields=changed,
            created_at=NOW,
        )
        _assert_healthy(connection)


def test_prd080_tenant_owner_active_hold_unique_and_immediate_transition_fk(
    prd080_migration_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """验证跨租户 item、重复 active hold 和伪造 transition 均即时失败。"""

    database_url = _clone(prd080_migration_templates, tmp_path / "owner-fk.sqlite3")
    assert apply_migrations(
        database_url,
        migrations_dir=prd080_migration_templates["migrations_0073"],
    ).applied == ["0073"]
    with connect_database(database_url) as connection:
        first = _insert_dispute_source(connection)
        charge = first["charge"]
        assert isinstance(charge, sqlite3.Row)
        second = _insert_dispute_source(
            connection,
            charge=charge,
            insert_hold=False,
            insert_outbox=False,
            insert_audit=False,
        )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
            connection.execute(
                """INSERT INTO billing_settlement_holds(
                     billing_settlement_hold_id,tenant_id,billing_dispute_id,
                     billing_dispute_item_id,finops_ledger_entry_id,status,release_reason,
                     state_revision,created_transition_id,released_transition_id,
                     created_at,released_at,updated_at
                   ) VALUES(?,?,?,?,?,'active',NULL,1,?,NULL,?,NULL,?)""",
                (
                    f"bhold_{uuid4().hex}",
                    second["tenant_id"],
                    second["dispute_id"],
                    second["item_id"],
                    charge["finops_ledger_entry_id"],
                    second["create_transition_id"],
                    NOW,
                    NOW,
                ),
            )
        third = _insert_dispute_source(
            connection,
            insert_hold=False,
            insert_outbox=False,
            insert_audit=False,
        )
        third_charge = third["charge"]
        assert isinstance(third_charge, sqlite3.Row)
        with pytest.raises(
            sqlite3.IntegrityError,
            match="billing_settlement_hold_owner_mismatch",
        ):
            connection.execute(
                """INSERT INTO billing_settlement_holds(
                     billing_settlement_hold_id,tenant_id,billing_dispute_id,
                     billing_dispute_item_id,finops_ledger_entry_id,status,release_reason,
                     state_revision,created_transition_id,released_transition_id,
                     created_at,released_at,updated_at
                   ) VALUES(?,?,?,?,?,'active',NULL,1,?,NULL,?,NULL,?)""",
                (
                    f"bhold_{uuid4().hex}",
                    third["tenant_id"],
                    third["dispute_id"],
                    third["item_id"],
                    third_charge["finops_ledger_entry_id"],
                    f"bdtr_{uuid4().hex}",
                    NOW,
                    NOW,
                ),
            )
        other_tenant = f"tenant_prd080_{uuid4().hex}"
        connection.execute(
            """INSERT INTO tenants(
                 tenant_id,slug,name,service_mode,status,created_at,updated_at
               ) VALUES(?,?,?,'internal_only','active',?,?)""",
            (other_tenant, f"prd080-{uuid4().hex}", "PRD-080 Other", NOW, NOW),
        )
        with pytest.raises(
            sqlite3.IntegrityError,
            match="billing_dispute_item_owner_mismatch",
        ):
            connection.execute(
                """INSERT INTO billing_dispute_items(
                     billing_dispute_item_id,tenant_id,billing_dispute_id,
                     finops_ledger_entry_id,usage_ledger_id,source_request_id,source_trace_id,
                     ledger_sequence,currency,amount_micros,disputable_micros,
                     price_snapshot_id,price_schema_version,price_snapshot_source,
                     price_evidence_status,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"bdsi_{uuid4().hex}",
                    other_tenant,
                    first["dispute_id"],
                    charge["finops_ledger_entry_id"],
                    charge["usage_ledger_id"],
                    charge["source_request_id"],
                    charge["source_trace_id"],
                    charge["ledger_sequence"],
                    charge["currency"],
                    charge["amount_micros"],
                    charge["amount_micros"],
                    charge["price_snapshot_id"],
                    charge["price_schema_version"],
                    charge["price_snapshot_source"],
                    charge["price_evidence_status"],
                    NOW,
                ),
            )
        _assert_healthy(connection)


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def test_prd080_outbox_prefreeze_takeover_renew_retry_success_and_failure(
    prd080_migration_templates: dict[str, Path],
    tmp_path: Path,
) -> None:
    """验证创建时快照、后增订阅隔离及完整 generation/attempt 状态机。"""

    database_url = _clone(prd080_migration_templates, tmp_path / "outbox-cas.sqlite3")
    assert apply_migrations(
        database_url,
        migrations_dir=prd080_migration_templates["migrations_0073"],
    ).applied == ["0073"]
    with connect_database(database_url) as connection:
        facts = _insert_dispute_source(connection, with_endpoint=True)
        assert len(facts["delivery_ids"]) == 1
        outbox_row = connection.execute(
            "SELECT * FROM billing_dispute_event_outbox WHERE billing_dispute_event_outbox_id=?",
            (facts["outbox_id"],),
        ).fetchone()
        assert outbox_row is not None
        assert json.loads(str(outbox_row["delivery_snapshot_json"])) == facts["delivery_ids"]
        assert outbox_row["snapshot_frozen_at"] == outbox_row["created_at"] == NOW
        with pytest.raises(
            sqlite3.IntegrityError,
            match="billing_dispute_outbox_transition_invalid",
        ):
            connection.execute(
                """UPDATE billing_dispute_event_outbox
                   SET status='processing',lease_generation=1,lease_owner_id='worker-invalid',
                       lease_token='invalid-token',lease_expires_at=?,next_attempt_at=NULL,
                       delivery_snapshot_json='[]',updated_at=?
                   WHERE billing_dispute_event_outbox_id=?""",
                ("2026-08-15T00:01:00Z", NOW, facts["outbox_id"]),
            )
        creator = facts["creator"]
        assert isinstance(creator, sqlite3.Row)
        _insert_endpoint(
            connection,
            tenant_id=str(facts["tenant_id"]),
            actor_id=str(creator["user_id"]),
        )
        connection.commit()

    config = SimpleNamespace(database_url=database_url)
    claim_one = dispute_outbox.claim_next_billing_dispute_event(
        config,
        worker_id="worker-prd080-a",
        now=_time(NOW) + timedelta(seconds=1),
    )
    assert claim_one is not None
    assert (claim_one.attempt_count, claim_one.lease_generation) == (0, 1)
    with connect_database(database_url) as connection:
        assert (
            connection.execute(
                """SELECT COUNT(*) FROM webhook_deliveries
               WHERE tenant_id=? AND event_id=? AND delivery_kind='original'""",
                (facts["tenant_id"], facts["event_id"]),
            ).fetchone()[0]
            == 1
        )

    takeover_at = claim_one.lease_expires_at + timedelta(seconds=1)
    claim_two = dispute_outbox.claim_next_billing_dispute_event(
        config,
        worker_id="worker-prd080-b",
        now=takeover_at,
    )
    assert claim_two is not None
    assert (claim_two.attempt_count, claim_two.lease_generation) == (0, 2)
    dispute_outbox._renew(config, claim_two, now=takeover_at + timedelta(seconds=1))
    assert claim_two.lease_generation == 3
    assert dispute_outbox._finish(
        config,
        claim_two,
        error_code="webhook.delivery_unavailable",
        now=takeover_at + timedelta(seconds=2),
    )
    with connect_database(database_url) as connection:
        retry = connection.execute(
            "SELECT * FROM billing_dispute_event_outbox WHERE billing_dispute_event_outbox_id=?",
            (facts["outbox_id"],),
        ).fetchone()
        assert retry is not None
        assert (retry["status"], retry["attempt_count"], retry["lease_generation"]) == (
            "pending",
            1,
            4,
        )
        retry_at = _time(str(retry["next_attempt_at"]))

    claim_three = dispute_outbox.claim_next_billing_dispute_event(
        config,
        worker_id="worker-prd080-c",
        now=retry_at,
    )
    assert claim_three is not None
    assert (claim_three.attempt_count, claim_three.lease_generation) == (1, 5)
    assert not dispute_outbox._finish(
        config,
        claim_two,
        error_code=None,
        now=retry_at + timedelta(seconds=1),
    )
    assert dispute_outbox._finish(
        config,
        claim_three,
        error_code=None,
        now=retry_at + timedelta(seconds=2),
    )
    with connect_database(database_url) as connection:
        succeeded = connection.execute(
            "SELECT * FROM billing_dispute_event_outbox WHERE billing_dispute_event_outbox_id=?",
            (facts["outbox_id"],),
        ).fetchone()
        assert succeeded is not None
        assert (
            succeeded["status"],
            succeeded["attempt_count"],
            succeeded["lease_generation"],
        ) == ("succeeded", 2, 6)
        assert succeeded["delivery_snapshot_json"] == _json(facts["delivery_ids"])
        failed_facts = _insert_dispute_source(connection)
        connection.commit()

    failed_claim = dispute_outbox.claim_next_billing_dispute_event(
        config,
        worker_id="worker-prd080-fail",
        now=retry_at + timedelta(seconds=3),
    )
    assert failed_claim is not None and failed_claim.event_id == failed_facts["event_id"]
    assert dispute_outbox._finish(
        config,
        failed_claim,
        error_code="webhook.delivery_unavailable",
        delivery_terminal=True,
        now=retry_at + timedelta(seconds=4),
    )
    with connect_database(database_url) as connection:
        failed = connection.execute(
            "SELECT * FROM billing_dispute_event_outbox WHERE billing_dispute_event_outbox_id=?",
            (failed_facts["outbox_id"],),
        ).fetchone()
        assert failed is not None
        assert (failed["status"], failed["attempt_count"], failed["lease_generation"]) == (
            "failed",
            1,
            2,
        )
        assert failed["last_error_code"] == "webhook.delivery_unavailable"
        assert (
            connection.execute(
                """SELECT COUNT(*) FROM webhook_deliveries
                   WHERE tenant_id=? AND event_id=? AND status='pending'""",
                (failed_facts["tenant_id"], failed_facts["event_id"]),
            ).fetchone()[0]
            == 0
        )
        stopped_facts = _insert_dispute_source(connection)
        connection.commit()

    stopped_at = retry_at + timedelta(seconds=5)
    stopped_claim = dispute_outbox.claim_next_billing_dispute_event(
        config,
        worker_id="worker-prd080-stop",
        now=stopped_at,
    )
    assert stopped_claim is not None and stopped_claim.event_id == stopped_facts["event_id"]
    assert dispute_outbox.dispatch_claimed_billing_dispute_event(
        config,
        stopped_claim,
        now=stopped_at,
        should_stop=lambda: True,
    )
    with connect_database(database_url) as connection:
        stopped = connection.execute(
            """SELECT status,attempt_count,lease_generation,lease_owner_id,
                      next_attempt_at,last_error_code
               FROM billing_dispute_event_outbox
               WHERE billing_dispute_event_outbox_id=?""",
            (stopped_facts["outbox_id"],),
        ).fetchone()
        assert stopped is not None
        assert tuple(stopped) == (
            "pending",
            0,
            2,
            None,
            stopped_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            None,
        )
        _assert_healthy(connection)


def _install_custom_billing_matrix(connection: sqlite3.Connection) -> None:
    role = connection.execute(
        "SELECT role_id,permission_revision,matrix_sha256 FROM roles WHERE code='finance_admin'"
    ).fetchone()
    assert role is not None and role["matrix_sha256"] != "a" * 64
    connection.execute(
        """UPDATE roles SET permission_revision=?,matrix_sha256=? WHERE role_id=?""",
        (int(role["permission_revision"]) + 1, "a" * 64, role["role_id"]),
    )


def _install_billing_delivery(connection: sqlite3.Connection) -> None:
    owner = connection.execute(
        """SELECT tenant_id,user_id FROM users WHERE role='admin' AND status='active'
           ORDER BY tenant_id,user_id LIMIT 1"""
    ).fetchone()
    assert owner is not None
    _insert_endpoint(
        connection,
        tenant_id=str(owner["tenant_id"]),
        actor_id=str(owner["user_id"]),
    )
    event_id = f"bdevt_{uuid4().hex}"
    dispute_id = f"bdsp_{uuid4().hex}"
    payload = {
        "schema_version": 1,
        "billing_dispute_id": dispute_id,
        "scope_type": "request_ids",
        "item_count": 1,
        "currency_totals": {"CNY": "0.000100"},
        "status": "submitted",
        "created_at": NOW,
    }
    event = build_webhook_event(
        tenant_id=str(owner["tenant_id"]),
        event_type="billing_dispute.created",
        event_id=event_id,
        timestamp=_time(NOW),
        actor_id=str(owner["user_id"]),
        object_id=dispute_id,
        trace_id=f"trc_prd080_{uuid4().hex}",
        source_request_id=f"req_prd080_{uuid4().hex}",
        payload=payload,
    )
    assert freeze_webhook_event_delivery_snapshot(connection, event, frozen_at=NOW) == 1


def _down_fact_snapshot(connection: sqlite3.Connection) -> tuple[int, ...]:
    values = [
        int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        for table in DISPUTE_TABLES
    ]
    values.extend(
        (
            int(
                connection.execute(
                    """SELECT COUNT(*) FROM audit_logs WHERE action LIKE 'billing_dispute.%'
                       OR object_type='billing_dispute'"""
                ).fetchone()[0]
            ),
            int(
                connection.execute(
                    """SELECT COUNT(*) FROM webhook_endpoints
                       WHERE instr(event_types_json,'"billing_dispute.created"')>0"""
                ).fetchone()[0]
            ),
            int(
                connection.execute(
                    """SELECT COUNT(*) FROM webhook_deliveries
                       WHERE event_type='billing_dispute.created'"""
                ).fetchone()[0]
            ),
            int(
                connection.execute(
                    """SELECT COUNT(*) FROM role_permissions relation
                       JOIN permissions permission
                         ON permission.tenant_id=relation.tenant_id
                        AND permission.permission_id=relation.permission_id
                       JOIN roles role ON role.tenant_id=relation.tenant_id
                        AND role.role_id=relation.role_id
                       WHERE permission.code IN(?,?,?)
                         AND NOT (role.code='admin' AND role.matrix_sha256=?)
                         AND NOT (role.code='finance_admin' AND role.matrix_sha256=?)""",
                    (
                        *NEW_PERMISSIONS,
                        "c56c271d650b89b1d4d6d49c1b8e326c89b075bb274116930a335f4c8f230e16",
                        "86b64ca3b997f5fa252f027ecf07430e677252e6d2c17677b72ac049130cba8c",
                    ),
                ).fetchone()[0]
            ),
        )
    )
    return tuple(values)


@pytest.mark.parametrize(
    "fact_kind",
    ("business_graph", "subscription", "delivery", "custom_billing_matrix"),
)
def test_prd080_down_business_guards_are_atomic(
    prd080_migration_templates: dict[str, Path],
    tmp_path: Path,
    fact_kind: str,
) -> None:
    """业务图、Audit/Link/Outbox、订阅、delivery 或定制授权均阻断降级。"""

    database_url = _clone(
        prd080_migration_templates,
        tmp_path / f"down-guard-{fact_kind}.sqlite3",
    )
    assert apply_migrations(
        database_url,
        migrations_dir=prd080_migration_templates["migrations_0073"],
    ).applied == ["0073"]
    with connect_database(database_url) as connection:
        if fact_kind == "business_graph":
            facts = _insert_dispute_source(connection)
            _start_review(connection, facts)
            _finish_dispute(connection, facts, action="approve")
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM billing_dispute_adjustment_links"
                ).fetchone()[0]
                == 1
            )
        elif fact_kind == "subscription":
            owner = connection.execute(
                """SELECT tenant_id,user_id FROM users WHERE role='admin' AND status='active'
                   ORDER BY tenant_id,user_id LIMIT 1"""
            ).fetchone()
            assert owner is not None
            _insert_endpoint(
                connection,
                tenant_id=str(owner["tenant_id"]),
                actor_id=str(owner["user_id"]),
                status="disabled",
            )
        elif fact_kind == "delivery":
            _install_billing_delivery(connection)
        else:
            _install_custom_billing_matrix(connection)
        connection.commit()
        schema_before = _schema_snapshot(connection)
        facts_before = _down_fact_snapshot(connection)
        assert any(facts_before)
        _assert_healthy(connection)

    with pytest.raises(DbError) as caught:
        rollback_last_migration(
            database_url,
            migrations_dir=prd080_migration_templates["migrations_0073"],
            expected_version="0073",
        )
    assert caught.value.code == "db.migration_failed"
    assert "prd080_down_guard_failed" in caught.value.message
    with connect_database(database_url) as connection:
        assert _head(connection) == "0073"
        assert _schema_snapshot(connection) == schema_before
        assert _down_fact_snapshot(connection) == facts_before
        _assert_healthy(connection)


def test_prd080_down_guard_covers_every_destructive_fact_class() -> None:
    """静态锁定 down 首个 DDL 前必须计数的全部不可丢弃事实。"""

    source = (MIGRATIONS_DIR / "0073_billing_dispute_adjustment_workflow.down.sql").read_text(
        encoding="utf-8"
    )
    guard = source[: source.index("-- Restore the exact 0072 AuditLog")]
    for required in (
        *DISPUTE_TABLES,
        "billing_dispute.created",
        "object_type='billing_dispute'",
        "webhook_endpoints",
        "webhook_deliveries",
        "role_permissions",
        *NEW_PERMISSIONS,
    ):
        assert required in guard
    first_persistent_ddl = source.index("DROP TABLE IF EXISTS audit_logs_prd080_old")
    assert source.index("prd080_down_guard_failed") < first_persistent_ddl
    assert source.index("DROP TABLE prd080_down_guard;") < first_persistent_ddl
