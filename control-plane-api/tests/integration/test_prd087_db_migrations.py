"""PRD-087 迁移合约：验证伙伴归属、恢复状态、完整性检查和可回滚边界。"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.db.errors import DbError

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "migrations"
CATALOG_HASH_21 = "03776958f56b3ac3d3d0355d29b4ae2ea3520be09607295f4fa15a549bbfe28b"
ROLE_HASH_21 = "c737744b0a9f8aa577c49f40bd7cdf2457f69ed93bb0dedf8d01365a5855aef0"
CATALOG_HASH_22 = "b44391603c39067c55f6d32742e2cb3daa9833721ab2ed4657c2c50db4dca125"
ROLE_HASH_22 = "0ebf053173c2a5b1acc10143f092753ef2dafe1d63f2a4ba8398ff49599b4379"
ADMIN_HASH_22 = "0d70d1adb89f3809dddbb13e41e25b851049eb656d10d13cd2dffe83f045a57a"
VIEWER_HASH_22 = "7b4af42e2f92112bffbed7c466aa4cbd4f41df89eb9c40ee0da8be91f07169e3"
STAMP = "2030-01-01T00:00:00Z"


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _clone(source: Path, destination: Path) -> str:
    shutil.copy2(source, destination)
    return _url(destination)


def _healthy(connection: sqlite3.Connection) -> None:
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _hashes(connection: sqlite3.Connection) -> tuple[str, str]:
    catalog = [
        "\x1f".join(str(value) for value in row)
        for row in connection.execute(
            """
            SELECT permission_code,category,name,description,risk_level,catalog_revision
            FROM rbac_permission_templates ORDER BY permission_code
            """
        )
    ]
    roles = [
        "\x1f".join(str(value) for value in row)
        for row in connection.execute(
            """
            SELECT role_code,permission_code,default_granted
            FROM rbac_role_permission_templates ORDER BY role_code,permission_code
            """
        )
    ]
    return (
        hashlib.sha256("\x1e".join(catalog).encode()).hexdigest(),
        hashlib.sha256("\x1e".join(roles).encode()).hexdigest(),
    )


@pytest.fixture(scope="module")
def empty_0079(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("prd087-0079")
    migrations = root / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name[:4].isdigit() and int(source.name[:4]) <= 79:
            shutil.copy2(source, migrations / source.name)
    database = root / "empty.sqlite3"
    result = apply_migrations(_url(database), migrations_dir=migrations)
    assert result.applied == [f"{version:04d}" for version in range(1, 80)]
    return database


@pytest.fixture(scope="module")
def empty_0080(empty_0079: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("prd087-0080")
    database = root / "empty.sqlite3"
    shutil.copy2(empty_0079, database)
    assert apply_migrations(_url(database)).applied == ["0080"]
    return database


@pytest.fixture(scope="module")
def seeded_0080(empty_0079: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("prd087-seeded")
    database = root / "seeded.sqlite3"
    shutil.copy2(empty_0079, database)
    apply_seed_data(_url(database))
    assert apply_migrations(_url(database)).applied == ["0080"]
    connection = connect_database(_url(database))
    try:
        tenant_id = connection.execute("SELECT tenant_id FROM tenants LIMIT 1").fetchone()[0]
        user_id = connection.execute(
            "SELECT user_id FROM users WHERE tenant_id=? LIMIT 1", (tenant_id,)
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO projects(project_id,tenant_id,project_code,name,owner_user_id,status,
              created_by,updated_by,created_at,updated_at)
            VALUES('project_prd087',?,'prd087','PRD087',?,'active',?,?,?,?)
            """,
            (tenant_id, user_id, user_id, user_id, STAMP, STAMP),
        )
        connection.commit()
    finally:
        connection.close()
    return database


def _ids(connection: sqlite3.Connection) -> tuple[str, str]:
    tenant_id = connection.execute("SELECT tenant_id FROM tenants LIMIT 1").fetchone()[0]
    user_id = connection.execute(
        "SELECT user_id FROM users WHERE tenant_id=? LIMIT 1", (tenant_id,)
    ).fetchone()[0]
    return tenant_id, user_id


def _insert_partner_customer(
    connection: sqlite3.Connection, *, linked_from: int = 0
) -> tuple[str, str]:
    tenant_id, user_id = _ids(connection)
    connection.execute(
        """
        INSERT INTO partners(partner_id,tenant_id,partner_code,display_name,portal_name,
          brand_primary_color,support_text,default_commission_rate_bps,status,revision,
          idempotency_key_sha256,payload_sha256,created_by_user_id,
          operation_request_id,operation_trace_id,created_at,updated_at)
        VALUES('par_11111111111111111111111111111111',?,'partner-1','Partner One',
          'Portal One','#12AB34',NULL,1000,'active',1,?,?,?,'req-partner','trace-partner',?,?)
        """,
        (tenant_id, "a" * 64, "b" * 64, user_id, STAMP, STAMP),
    )
    connection.execute(
        """
        INSERT INTO partner_customers(partner_customer_id,tenant_id,partner_id,project_id,
          customer_code,display_name,status,linked_from_ledger_sequence,
          commission_cutoff_sequence,commission_rate_bps,revision,idempotency_key_sha256,
          payload_sha256,created_by_user_id,disabled_by_user_id,operation_request_id,
          operation_trace_id,disable_request_id,disable_trace_id,created_at,updated_at,disabled_at)
        VALUES('pcu_22222222222222222222222222222222',?,
          'par_11111111111111111111111111111111','project_prd087','customer-1','Customer One',
          'active',?,NULL,1000,1,?,?,?,NULL,'req-customer','trace-customer',NULL,NULL,?,?,NULL)
        """,
        (tenant_id, linked_from, "c" * 64, "d" * 64, user_id, STAMP, STAMP),
    )
    return tenant_id, user_id


def _insert_cursor(
    connection: sqlite3.Connection, tenant_id: str, *, last_scanned: int = 0
) -> None:
    connection.execute(
        """
        INSERT INTO partner_commission_cursors(
          tenant_id,partner_customer_id,partner_id,project_id,status,
          last_scanned_ledger_sequence,scan_upper_sequence,generation,lease_owner_id,
          lease_token,lease_expires_at,next_run_at,consecutive_failure_count,
          last_error_code,created_at,updated_at)
        VALUES(?,'pcu_22222222222222222222222222222222',
          'par_11111111111111111111111111111111','project_prd087','pending',
          ?,NULL,0,NULL,NULL,NULL,?,0,NULL,?,?)
        """,
        (tenant_id, last_scanned, STAMP, STAMP, STAMP),
    )


def _insert_outbox(
    connection: sqlite3.Connection,
    tenant_id: str,
    user_id: str,
    *,
    include_schema: bool = True,
    include_extra: bool = False,
) -> None:
    customer_id = "pcu_22222222222222222222222222222222"
    payload_values = {
        "partner_id": "par_11111111111111111111111111111111",
        "partner_customer_id": customer_id,
        "customer_code": "customer-1",
        "project_id": "project_prd087",
        "status": "active",
        "commission_rate_bps": 1000,
    }
    if include_schema:
        payload_values = {"schema_version": "1.0", **payload_values}
    if include_extra:
        payload_values["unexpected"] = "rejected"
    payload = json.dumps(
        payload_values,
        separators=(",", ":"),
    )
    outbox_id = "pco_" + hashlib.sha256(customer_id.encode()).hexdigest()[:32]
    event_id = "evt_pcu_" + hashlib.sha256(customer_id.encode()).hexdigest()[:32]
    connection.execute(
        """
        INSERT INTO partner_customer_outbox(
          partner_customer_outbox_id,tenant_id,partner_id,partner_customer_id,
          webhook_event_id,actor_user_id,source_request_id,source_trace_id,event_timestamp,
          payload_json,payload_sha256,status,attempt_count,generation,lease_owner_id,
          lease_token,lease_expires_at,next_attempt_at,subscription_snapshot_json,
          snapshot_frozen_at,last_error_code,delivered_at,created_at,updated_at)
        VALUES(?,?,'par_11111111111111111111111111111111',?,?,?,
          'req-customer','trace-customer',?,?,?,?,0,0,NULL,NULL,NULL,?,NULL,NULL,NULL,NULL,?,?)
        """,
        (
            outbox_id,
            tenant_id,
            customer_id,
            event_id,
            user_id,
            STAMP,
            payload,
            hashlib.sha256(payload.encode()).hexdigest(),
            "pending",
            STAMP,
            STAMP,
            STAMP,
        ),
    )


def test_fresh_repeat_pristine_down_and_reapply(empty_0080: Path, tmp_path: Path) -> None:
    database_url = _clone(empty_0080, tmp_path / "roundtrip.sqlite3")
    connection = connect_database(database_url)
    try:
        assert _hashes(connection) == (CATALOG_HASH_22, ROLE_HASH_22)
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger' AND name LIKE '%_prd087'"
        ).fetchone()[0] == 18
        _healthy(connection)
    finally:
        connection.close()
    repeated = apply_migrations(database_url)
    assert repeated.applied == [] and repeated.skipped[-1] == "0080"
    assert rollback_last_migration(database_url, expected_version="0080").rolled_back == "0080"
    connection = connect_database(database_url)
    try:
        assert _hashes(connection) == (CATALOG_HASH_21, ROLE_HASH_21)
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd087'"
        ).fetchone()[0] == 0
        _healthy(connection)
    finally:
        connection.close()
    assert apply_migrations(database_url).applied == ["0080"]


def test_existing_and_new_tenant_rbac_projection(seeded_0080: Path, tmp_path: Path) -> None:
    database_url = _clone(seeded_0080, tmp_path / "rbac.sqlite3")
    connection = connect_database(database_url)
    try:
        tenant_id, _ = _ids(connection)
        grants = list(
            connection.execute(
                """
                SELECT role.code,COUNT(*) FROM roles role
                JOIN role_permissions relation ON relation.tenant_id=role.tenant_id
                  AND relation.role_id=role.role_id
                JOIN permissions permission ON permission.tenant_id=relation.tenant_id
                  AND permission.permission_id=relation.permission_id
                WHERE role.tenant_id=? AND permission.code LIKE 'partner_%'
                GROUP BY role.code ORDER BY role.code
                """,
                (tenant_id,),
            )
        )
        assert [tuple(row) for row in grants] == [("admin", 6), ("viewer", 5)]
        assert connection.execute(
            "SELECT matrix_sha256 FROM roles WHERE tenant_id=? AND code='admin'", (tenant_id,)
        ).fetchone()[0] == ADMIN_HASH_22
        assert connection.execute(
            "SELECT matrix_sha256 FROM roles WHERE tenant_id=? AND code='viewer'", (tenant_id,)
        ).fetchone()[0] == VIEWER_HASH_22
        connection.execute(
            """
            INSERT INTO tenants(tenant_id,slug,name,service_mode,status,created_at,updated_at)
            VALUES('tenant_prd087_new','prd087-new','PRD087 New','internal_only','active',?,?)
            """,
            (STAMP, STAMP),
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM permissions WHERE tenant_id='tenant_prd087_new'"
        ).fetchone()[0] == 96
        _healthy(connection)
    finally:
        connection.close()


def test_owner_immutability_branding_and_customer_state(
    seeded_0080: Path, tmp_path: Path
) -> None:
    database_url = _clone(seeded_0080, tmp_path / "owners.sqlite3")
    connection = connect_database(database_url)
    try:
        tenant_id, user_id = _insert_partner_customer(connection)
        connection.execute(
            """
            UPDATE partners SET portal_name='Portal Two',revision=2,
              operation_request_id='req-brand',operation_trace_id='trace-brand',
              updated_at='2030-01-01T00:00:01Z' WHERE tenant_id=?
            """,
            (tenant_id,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="partner_transition_invalid"):
            connection.execute(
                "UPDATE partners SET partner_code='changed',revision=3 WHERE tenant_id=?",
                (tenant_id,),
            )
        connection.execute(
            """
            UPDATE partner_customers SET display_name='Customer Two',revision=2,
              operation_request_id='req-name',operation_trace_id='trace-name',
              updated_at='2030-01-01T00:00:02Z' WHERE tenant_id=?
            """,
            (tenant_id,),
        )
        connection.execute(
            """
            UPDATE partner_customers SET status='disabled',commission_cutoff_sequence=0,
              disabled_by_user_id=?,disable_request_id='req-disable',
              disable_trace_id='trace-disable',disabled_at='2030-01-01T00:00:03Z',
              revision=3,operation_request_id='req-disable',operation_trace_id='trace-disable',
              updated_at='2030-01-01T00:00:03Z' WHERE tenant_id=?
            """,
            (user_id, tenant_id),
        )
        with pytest.raises(sqlite3.IntegrityError, match="partner_customer_transition_invalid"):
            connection.execute(
                "UPDATE partner_customers SET status='active',revision=4 WHERE tenant_id=?",
                (tenant_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="partner_customer_immutable"):
            connection.execute("DELETE FROM partner_customers WHERE tenant_id=?", (tenant_id,))
        _healthy(connection)
    finally:
        connection.close()


def test_cursor_full_cas_and_outbox_snapshot_recovery(
    seeded_0080: Path, tmp_path: Path
) -> None:
    database_url = _clone(seeded_0080, tmp_path / "recovery.sqlite3")
    connection = connect_database(database_url)
    try:
        tenant_id, user_id = _insert_partner_customer(connection)
        _insert_cursor(connection, tenant_id)
        with pytest.raises(
            sqlite3.IntegrityError, match="partner_customer_outbox_owner_mismatch"
        ):
            _insert_outbox(connection, tenant_id, user_id, include_schema=False)
        with pytest.raises(
            sqlite3.IntegrityError, match="partner_customer_outbox_owner_mismatch"
        ):
            _insert_outbox(connection, tenant_id, user_id, include_extra=True)
        _insert_outbox(connection, tenant_id, user_id)
        connection.execute(
            """
            UPDATE partner_commission_cursors SET status='leased',scan_upper_sequence=0,
              generation=1,lease_owner_id='worker-a',lease_token=?,
              lease_expires_at='2030-01-01T00:03:00Z',next_run_at=NULL,
              updated_at='2030-01-01T00:00:01Z' WHERE tenant_id=?
            """,
            ("1" * 64, tenant_id),
        )
        connection.execute(
            """
            UPDATE partner_commission_cursors SET generation=2,lease_token=?,
              lease_expires_at='2030-01-01T00:04:00Z',updated_at='2030-01-01T00:00:02Z'
            WHERE tenant_id=?
            """,
            ("2" * 64, tenant_id),
        )
        connection.execute(
            """
            UPDATE partner_commission_cursors SET generation=3,lease_owner_id='worker-b',
              lease_token=?,lease_expires_at='2030-01-01T00:08:00Z',
              updated_at='2030-01-01T00:05:00Z' WHERE tenant_id=?
            """,
            ("3" * 64, tenant_id),
        )
        with pytest.raises(
            sqlite3.IntegrityError, match="partner_commission_cursor_transition_invalid"
        ):
            connection.execute(
                """
                UPDATE partner_commission_cursors SET generation=4,lease_token=?,
                  scan_upper_sequence=6,lease_expires_at='2030-01-01T00:09:00Z',
                  updated_at='2030-01-01T00:05:01Z' WHERE tenant_id=?
                """,
                ("4" * 64, tenant_id),
            )
        connection.execute(
            """
            UPDATE partner_commission_cursors SET status='pending',
              last_scanned_ledger_sequence=0,scan_upper_sequence=NULL,generation=4,
              lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
              next_run_at='2030-01-01T00:06:00Z',consecutive_failure_count=0,
              last_error_code=NULL,updated_at='2030-01-01T00:05:02Z'
            WHERE tenant_id=?
            """,
            (tenant_id,),
        )
        connection.execute(
            """
            UPDATE partner_customers SET status='disabled',commission_cutoff_sequence=0,
              disabled_by_user_id=?,disable_request_id='req-disable',
              disable_trace_id='trace-disable',disabled_at='2030-01-01T00:06:00Z',
              revision=2,operation_request_id='req-disable',operation_trace_id='trace-disable',
              updated_at='2030-01-01T00:06:00Z' WHERE tenant_id=?
            """,
            (user_id, tenant_id),
        )
        connection.execute(
            """
            UPDATE partner_commission_cursors SET status='leased',scan_upper_sequence=0,
              generation=5,lease_owner_id='worker-c',lease_token=?,
              lease_expires_at='2030-01-01T00:09:00Z',next_run_at=NULL,
              updated_at='2030-01-01T00:06:01Z' WHERE tenant_id=?
            """,
            ("4" * 64, tenant_id),
        )
        connection.execute(
            """
            UPDATE partner_commission_cursors SET status='complete',
              scan_upper_sequence=NULL,generation=6,lease_owner_id=NULL,lease_token=NULL,
              lease_expires_at=NULL,next_run_at=NULL,consecutive_failure_count=0,
              last_error_code=NULL,updated_at='2030-01-01T00:06:02Z' WHERE tenant_id=?
            """,
            (tenant_id,),
        )
        outbox_id = "pco_" + hashlib.sha256(
            b"pcu_22222222222222222222222222222222"
        ).hexdigest()[:32]
        connection.execute(
            """
            UPDATE partner_customer_outbox SET status='leased',attempt_count=1,generation=1,
              lease_owner_id='worker-a',lease_token=?,lease_expires_at='2030-01-01T00:03:00Z',
              next_attempt_at=NULL,subscription_snapshot_json='[]',
              snapshot_frozen_at='2030-01-01T00:00:01Z',updated_at='2030-01-01T00:00:01Z'
            WHERE partner_customer_outbox_id=?
            """,
            ("5" * 64, outbox_id),
        )
        connection.execute(
            """
            UPDATE partner_customer_outbox SET status='pending',generation=2,
              lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
              next_attempt_at='2030-01-01T00:04:00Z',
              last_error_code='partner.event_unavailable',updated_at='2030-01-01T00:00:02Z'
            WHERE partner_customer_outbox_id=?
            """,
            (outbox_id,),
        )
        assert tuple(
            connection.execute(
                """
                SELECT subscription_snapshot_json,snapshot_frozen_at,attempt_count
                FROM partner_customer_outbox WHERE partner_customer_outbox_id=?
                """,
                (outbox_id,),
            ).fetchone()
        ) == ("[]", "2030-01-01T00:00:01Z", 1)
        generation = 2
        for attempt in range(2, 11):
            generation += 1
            connection.execute(
                """
                UPDATE partner_customer_outbox SET status='leased',attempt_count=?,
                  generation=?,lease_owner_id='worker-retry',lease_token=?,
                  lease_expires_at='2030-01-01T00:10:00Z',next_attempt_at=NULL,
                  last_error_code=NULL,updated_at=? WHERE partner_customer_outbox_id=?
                """,
                (
                    attempt,
                    generation,
                    f"{attempt:x}" * 64,
                    f"2030-01-01T00:00:{attempt * 2:02d}Z",
                    outbox_id,
                ),
            )
            generation += 1
            terminal = attempt == 10
            connection.execute(
                """
                UPDATE partner_customer_outbox SET status=?,generation=?,
                  lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,
                  next_attempt_at=?,last_error_code='partner.event_unavailable',
                  updated_at=? WHERE partner_customer_outbox_id=?
                """,
                (
                    "failed" if terminal else "pending",
                    generation,
                    None if terminal else "2030-01-01T00:09:00Z",
                    f"2030-01-01T00:00:{attempt * 2 + 1:02d}Z",
                    outbox_id,
                ),
            )
        assert tuple(
            connection.execute(
                """
                SELECT status,attempt_count,generation,subscription_snapshot_json
                FROM partner_customer_outbox WHERE partner_customer_outbox_id=?
                """,
                (outbox_id,),
            ).fetchone()
        ) == ("failed", 10, 20, "[]")
        with pytest.raises(
            sqlite3.IntegrityError, match="partner_customer_outbox_transition_invalid"
        ):
            connection.execute(
                """
                UPDATE partner_customer_outbox SET subscription_snapshot_json='[1]'
                WHERE partner_customer_outbox_id=?
                """,
                (outbox_id,),
            )
        _healthy(connection)
    finally:
        connection.close()


def test_commission_deterministic_id_half_up_and_append_only(
    seeded_0080: Path, tmp_path: Path
) -> None:
    database_url = _clone(seeded_0080, tmp_path / "commission.sqlite3")
    connection = connect_database(database_url)
    try:
        tenant_id, _ = _insert_partner_customer(connection)
        amount = 999_999_999_999_999
        rate = 1000
        sql_value = connection.execute(
            "SELECT (?/10000)*?+(((?%10000)*?+5000)/10000)",
            (amount, rate, amount, rate),
        ).fetchone()[0]
        assert sql_value == (amount * rate + 5000) // 10000
        trigger_sql = connection.execute(
            """
            SELECT sql FROM sqlite_schema
            WHERE type='trigger' AND name='trg_commission_records_owner_insert_prd087'
            """
        ).fetchone()[0]
        assert "partner-commission-v1" in trigger_sql
        assert "source_finops_ledger_entry_id" in trigger_sql
        with pytest.raises(sqlite3.IntegrityError, match="commission_record_owner_mismatch"):
            connection.execute(
                """
                INSERT INTO commission_records(
                  commission_record_id,tenant_id,partner_id,partner_customer_id,
                  source_finops_ledger_entry_id,source_ledger_sequence,source_request_id,
                  source_trace_id,customer_code_snapshot,display_name_snapshot,project_id,
                  amount_micros,currency,commission_rate_bps,commission_amount_micros,
                  input_tokens,output_tokens,total_tokens,cache_tokens,estimated,status,
                  source_created_at,created_at,updated_at)
                VALUES('com_00000000000000000000000000000000',?,
                  'par_11111111111111111111111111111111',
                  'pcu_22222222222222222222222222222222','fle_missing',1,
                  'req-missing','trace-missing','customer-1','Customer One','project_prd087',
                  5,'CNY',1000,1,1,1,2,0,0,'accrued',?,?,?)
                """,
                (tenant_id, STAMP, STAMP, STAMP),
            )
        with pytest.raises(sqlite3.IntegrityError, match="partner_owner_mismatch"):
            _insert_partner_customer(connection)
        _healthy(connection)
    finally:
        connection.close()


def test_cursor_high_water_is_tenant_qualified(
    seeded_0080: Path, tmp_path: Path
) -> None:
    database_url = _clone(seeded_0080, tmp_path / "tenant-high-water.sqlite3")
    connection = connect_database(database_url)
    try:
        tenant_id, user_id = _ids(connection)
        protected_triggers = {
            name: connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?", (name,)
            ).fetchone()[0]
            for name in (
                "trg_finops_ledger_owner_insert_prd051",
                "trg_finops_ledger_immutable_delete_prd051",
            )
        }
        connection.execute("PRAGMA foreign_keys=OFF")
        for name in protected_triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
        rows = (
            (
                101,
                "fle_" + "1" * 32,
                "flevt_" + "1" * 32,
                tenant_id,
                "adj_tenant_a",
                user_id,
            ),
            (
                202,
                "fle_" + "2" * 32,
                "flevt_" + "2" * 32,
                "tenant_other_prd087",
                "adj_tenant_b",
                "user_other_prd087",
            ),
        )
        connection.executemany(
            """
            INSERT INTO finops_ledger_entries(
              ledger_sequence,finops_ledger_entry_id,event_id,tenant_id,entry_type,
              direction,amount_micros,currency,normalized_cny_micros,
              cny_normalization_status,price_evidence_status,posting_source,quota_type,
              finops_adjustment_id,operation_request_id,operation_trace_id,actor_user_id,
              created_at,updated_at)
            VALUES(?,?,?,?,'manual_credit','credit',1,'CNY',1,'normalized',
              'not_applicable','direct','paid',?,'req-ledger','trace-ledger',?,?,?)
            """,
            tuple((*row, STAMP, STAMP) for row in rows),
        )
        _insert_partner_customer(connection, linked_from=101)
        _insert_cursor(connection, tenant_id, last_scanned=101)
        connection.execute(
            """
            UPDATE partner_commission_cursors SET status='leased',scan_upper_sequence=101,
              generation=1,lease_owner_id='worker-tenant-a',lease_token=?,
              lease_expires_at='2030-01-01T00:03:00Z',next_run_at=NULL,
              updated_at='2030-01-01T00:00:01Z' WHERE tenant_id=?
            """,
            ("9" * 64, tenant_id),
        )
        assert connection.execute(
            """
            SELECT scan_upper_sequence FROM partner_commission_cursors
            WHERE tenant_id=?
            """,
            (tenant_id,),
        ).fetchone()[0] == 101
        connection.execute("DELETE FROM finops_ledger_entries")
        for sql in protected_triggers.values():
            connection.execute(sql)
        connection.commit()
        connection.execute("PRAGMA foreign_keys=ON")
        _healthy(connection)
    finally:
        connection.close()


def _insert_webhook_fact(
    connection: sqlite3.Connection,
    tenant_id: str,
    user_id: str,
    fact_kind: str,
) -> None:
    endpoint_id = f"whe_prd087_{fact_kind}"
    event_types = (
        '["partner.customer_created"]'
        if fact_kind == "subscription"
        else '["user.invited"]'
    )
    connection.execute(
        """
        INSERT INTO webhook_endpoints(
          webhook_endpoint_id,tenant_id,name,endpoint_url,event_types_json,
          encrypted_secret,secret_prefix,secret_fingerprint,status,timeout_ms,max_attempts,
          secret_version,created_by,updated_by,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,'active',5000,3,1,?,?,?,?)
        """,
        (
            endpoint_id,
            tenant_id,
            f"PRD087 {fact_kind}",
            "https://example.invalid/prd087",
            event_types,
            "enc:v1:test-fixture",
            "test",
            "f" * 64,
            user_id,
            user_id,
            STAMP,
            STAMP,
        ),
    )
    if fact_kind == "subscription":
        return
    delivery_id = "whd_" + hashlib.sha256(fact_kind.encode()).hexdigest()[:32]
    failed = fact_kind == "dead_letter"
    connection.execute(
        """
        INSERT INTO webhook_deliveries(
          webhook_delivery_id,tenant_id,webhook_endpoint_id,event_id,event_type,
          event_timestamp,payload_json,status,attempt_count,max_attempts,http_status,
          duration_ms,error_code,error_summary,next_retry_at,delivery_kind,
          replayed_from_delivery_id,replay_idempotency_key,replayed_by,replayed_at,
          secret_version,created_at,updated_at)
        VALUES(?,?,?,?,?,?,'{}',?,?,3,?,?,?,?,NULL,'original',NULL,NULL,NULL,NULL,1,?,?)
        """,
        (
            delivery_id,
            tenant_id,
            endpoint_id,
            f"evt_prd087_{fact_kind}",
            "partner.customer_created",
            STAMP,
            "failed" if failed else "pending",
            1 if failed else 0,
            503 if failed else None,
            12 if failed else None,
            "webhook.delivery_unavailable" if failed else None,
            "test-only" if failed else None,
            STAMP,
            STAMP,
        ),
    )
    if fact_kind == "attempt":
        connection.execute(
            """
            INSERT INTO webhook_delivery_attempts(
              webhook_delivery_attempt_id,tenant_id,webhook_delivery_id,
              webhook_endpoint_id,attempt_no,status,http_status,duration_ms,
              error_code,error_summary,created_at)
            VALUES(?,?,?,?,1,'failed',503,12,
              'webhook.delivery_unavailable','test-only',?)
            """,
            ("wha_" + "8" * 32, tenant_id, delivery_id, endpoint_id, STAMP),
        )


@pytest.mark.parametrize(
    "fact_kind",
    (
        "business",
        "cursor",
        "outbox",
        "subscription",
        "delivery",
        "attempt",
        "dead_letter",
        "rbac",
    ),
)
def test_down_guards_are_atomic(
    fact_kind: str, seeded_0080: Path, tmp_path: Path
) -> None:
    database_url = _clone(seeded_0080, tmp_path / f"down-{fact_kind}.sqlite3")
    connection = connect_database(database_url)
    try:
        tenant_id, user_id = _ids(connection)
        if fact_kind in {"business", "cursor", "outbox"}:
            _insert_partner_customer(connection)
            if fact_kind == "cursor":
                _insert_cursor(connection, tenant_id)
            elif fact_kind == "outbox":
                _insert_outbox(connection, tenant_id, user_id)
        elif fact_kind in {"subscription", "delivery", "attempt", "dead_letter"}:
            _insert_webhook_fact(connection, tenant_id, user_id, fact_kind)
        else:
            viewer_id = connection.execute(
                "SELECT role_id FROM roles WHERE tenant_id=? AND code='viewer'", (tenant_id,)
            ).fetchone()[0]
            permission_id = connection.execute(
                """
                SELECT permission_id FROM permissions
                WHERE tenant_id=? AND code='partner_portal.read'
                """,
                (tenant_id,),
            ).fetchone()[0]
            connection.execute(
                "DELETE FROM role_permissions WHERE tenant_id=? AND role_id=? AND permission_id=?",
                (tenant_id, viewer_id, permission_id),
            )
            connection.execute(
                """
                UPDATE roles SET permission_revision=permission_revision+1,
                  matrix_sha256=?,updated_at=?
                WHERE tenant_id=? AND role_id=?
                """,
                ("e" * 64, STAMP, tenant_id, viewer_id),
            )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(DbError, match="prd087_down_guard_failed"):
        rollback_last_migration(database_url, expected_version="0080")
    connection = connect_database(database_url)
    try:
        assert connection.execute(
            """
            SELECT version FROM schema_migrations
            WHERE status='applied' ORDER BY version DESC LIMIT 1
            """
        ).fetchone()[0] == "0080"
        _healthy(connection)
    finally:
        connection.close()


def test_down_guard_precedes_destructive_ddl() -> None:
    source = (MIGRATIONS_DIR / "0080_channel_partner_white_label_portal.down.sql").read_text(
        encoding="utf-8"
    )
    guard = source.index("prd087_down_guard_failed")
    first_ddl = source.index("DROP TRIGGER trg_partner_customer_outbox_delete_prd087")
    assert guard < first_ddl
    for marker in (
        "partners",
        "partner_customers",
        "commission_records",
        "partner_commission_cursors",
        "partner_customer_outbox",
        "partner_portal_operations",
        "partner.customer_created",
        "webhook_delivery_attempts",
        "webhook_dead_letter_events",
        "partner_%",
    ):
        assert marker in source[guard:first_ddl]
