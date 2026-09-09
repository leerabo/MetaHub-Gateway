"""Canonical Usage insertion and transactional native sequence allocation."""

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from uuid import uuid4

import g6_postgresql_support as native_support
import pytest
import test_postgresql_native_migrations as native_migrations
import test_prd099_platform_billing as billing
from metahub_shared.db import DbError, apply_migrations, connect_database, rollback_last_migration

pytestmark = native_migrations.pytestmark
native_business_template = native_support.native_business_template
native_business_database = native_support.native_business_database


def _append(connection):
    tenant = connection.execute(
        "SELECT tenant_id FROM tenants ORDER BY tenant_id LIMIT 1"
    ).fetchone()[0]
    usage, request, trace = (prefix + uuid4().hex for prefix in ("usg_", "req_", "trc_"))
    stamp = "2026-09-09T00:00:00Z"
    connection.execute(
        "INSERT INTO gateway_request_claims(tenant_id,request_id,trace_id,virtual_key_id,"
        "snapshot_version,policy_snapshot_status,claimed_at) VALUES(?,?,?,NULL,NULL,'disabled',?)",
        (tenant, request, trace, stamp),
    )
    connection.execute(
        "INSERT INTO request_logs(request_id,tenant_id,trace_id,status,created_at,updated_at) "
        "VALUES(?,?,?,'success',?,?)",
        (request, tenant, trace, stamp, stamp),
    )
    connection.execute(
        "INSERT INTO usage_ledger(usage_ledger_id,tenant_id,request_id,input_tokens,output_tokens,"
        "total_tokens,cache_tokens,raw_usage,normalized_usage,cost_amount,cost_cny,cost_usd,currency,"
        "created_at,updated_at,status,estimated,usage_source) "
        "VALUES(?,?,?,10,5,15,0,'{}','{}',0,0,0,'CNY',?,?,'recorded',0,'upstream')",
        (usage, tenant, request, stamp, stamp),
    )
    return tuple(
        connection.execute(
            "SELECT usage_export_sequence,tenant_id,usage_ledger_id "
            "FROM usage_export_ledger_sequences WHERE usage_ledger_id=?",
            (usage,),
        ).fetchone()
    )


def _state(database):
    with closing(connect_database(database)) as connection:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT usage_export_sequence,tenant_id,usage_ledger_id "
                "FROM usage_export_ledger_sequences ORDER BY usage_export_sequence"
            )
        ]


def _rollback_after(database, version):
    with closing(connect_database(database)) as connection:
        later = [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations WHERE status='applied' AND version>? "
                "ORDER BY version DESC",
                (version,),
            )
        ]
    for candidate in later:
        assert (
            rollback_last_migration(database, expected_version=candidate).rolled_back == candidate
        )
    return later


def test_native_usage_insert_allocates_canonical_sequence(native_business_database):
    database = native_business_database
    with closing(connect_database(database)) as connection:
        tenant_id = connection.execute(
            "SELECT tenant_id FROM tenants ORDER BY tenant_id"
        ).fetchone()[0]
    usage_id = billing._insert_usage(database, tenant_id, datetime(2026, 9, 9, tzinfo=UTC))
    with closing(connect_database(database)) as connection:
        assert tuple(
            connection.execute(
                "SELECT usage_export_sequence,tenant_id,usage_ledger_id "
                "FROM usage_export_ledger_sequences WHERE usage_ledger_id=?",
                (usage_id,),
            ).fetchone()
        ) == (1, tenant_id, usage_id)
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM platform_meter_outbox WHERE source_usage_ledger_id=?",
                (usage_id,),
            ).fetchone()[0]
            == 1
        )


def test_native_usage_migrations_empty_roundtrip(native_business_database):
    database = native_business_database
    later = _rollback_after(database, "0149")
    assert rollback_last_migration(database, expected_version="0149").rolled_back == "0149"
    assert apply_migrations(database).applied == ["0149", *reversed(later)]
    assert _state(database) == []


def test_native_usage_sequence_rollback_and_sql_savepoint_leave_no_holes(native_business_database):
    database = native_business_database
    with closing(connect_database(database)) as connection:
        assert _append(connection)[0] == 1
        connection.rollback()
        first = _append(connection)
        assert first[0] == 1
        connection.execute("SAVEPOINT usage_failure")
        assert _append(connection)[0] == 2
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("SELECT prd043_missing_column FROM usage_ledger")
        connection.execute("ROLLBACK TO SAVEPOINT usage_failure")
        connection.execute("RELEASE SAVEPOINT usage_failure")
        second = _append(connection)
        assert second[0] == 2
        connection.commit()
        assert (
            connection.execute(
                "SELECT last_sequence FROM metahub_usage_export_allocator"
            ).fetchone()[0]
            == 2
        )
    assert _state(database) == [first, second]


def test_native_usage_sequence_rejects_direct_mutation_and_populated_down(native_business_database):
    database = native_business_database
    with closing(connect_database(database)) as connection:
        first = _append(connection)
        connection.commit()
        for sql, parameters in (
            (
                "INSERT INTO usage_export_ledger_sequences("
                "usage_export_sequence,tenant_id,usage_ledger_id) "
                "VALUES(?,?,?)",
                (900, *first[1:]),
            ),
            (
                "INSERT INTO usage_export_ledger_sequences(tenant_id,usage_ledger_id) VALUES(?,?)",
                first[1:],
            ),
            (
                "INSERT INTO usage_export_ledger_sequences(tenant_id,usage_ledger_id) VALUES(?,?)",
                ("foreign", first[2]),
            ),
            ("UPDATE usage_export_ledger_sequences SET usage_export_sequence=2", ()),
            ("DELETE FROM usage_export_ledger_sequences", ()),
            ("UPDATE metahub_usage_export_allocator SET last_sequence=last_sequence+1", ()),
            ("DELETE FROM metahub_usage_export_allocator", ()),
            ("INSERT INTO metahub_usage_export_allocator VALUES(1,99)", ()),
            ("TRUNCATE metahub_usage_export_allocator", ()),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql, parameters)
            connection.rollback()
        assert _append(connection)[0] == 2
        connection.rollback()
    down = (
        Path(__file__).resolve().parents[3]
        / "infra/migrations/postgresql/0149_usage_export_transactional_sequence.down.sql"
    )
    with closing(connect_database(database)) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.executescript("BEGIN;\n" + down.read_text(encoding="utf-8") + "\nROLLBACK;")
    assert _state(database) == [first]
    assert not apply_migrations(database).applied


@pytest.mark.parametrize("rollback_first", [False, True])
def test_native_usage_allocation_waits_for_prior_transaction(
    native_business_database, rollback_first
):
    database = native_business_database
    started, written = Event(), Event()
    with (
        closing(connect_database(database)) as first_connection,
        closing(connect_database(database)) as second_connection,
    ):
        first = _append(first_connection)
        second_pid = second_connection.execute("SELECT pg_backend_pid()").fetchone()[0]
        second_connection.rollback()

        def second_writer():
            started.set()
            row = _append(second_connection)
            written.set()
            second_connection.commit()
            return row

        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(second_writer)
            try:
                assert started.wait(5)
                waiting = None
                deadline = time.monotonic() + 10
                with closing(connect_database(database)) as observer:
                    while time.monotonic() < deadline:
                        waiting = observer.execute(
                            "SELECT 1 FROM pg_locks WHERE pid=? "
                            "AND locktype='transactionid' AND NOT granted",
                            (second_pid,),
                        ).fetchone()
                        if waiting:
                            break
                        time.sleep(0.02)
                assert waiting is not None and not written.is_set()
                if rollback_first:
                    first_connection.rollback()
                else:
                    first_connection.commit()
                second = pending.result(timeout=10)
            finally:
                first_connection.rollback()
        assert second[0] == (1 if rollback_first else 2)
        assert _state(database) == ([second] if rollback_first else [first, second])


@pytest.mark.parametrize("direction", ["up", "down"])
@pytest.mark.parametrize("drift", ["function", "disabled", "manifest", "constraint"])
def test_native_usage_migration_rejects_drift(native_business_database, direction, drift):
    database = native_business_database
    _rollback_after(database, "0149")
    if direction == "up":
        assert rollback_last_migration(database, expected_version="0149").rolled_back == "0149"
    with closing(connect_database(database)) as connection:
        if drift == "function":
            connection.execute(
                "CREATE OR REPLACE FUNCTION fn_trg_usage_export_ledger_sequences_owner_insert() "
                "RETURNS trigger LANGUAGE plpgsql AS $$BEGIN RETURN NEW; END;$$"
            )
        elif drift == "disabled":
            connection.execute(
                "ALTER TABLE usage_export_ledger_sequences DISABLE TRIGGER "
                "trg_usage_export_ledger_sequences_immutable_update"
            )
        elif drift == "constraint":
            connection.execute(
                "ALTER TABLE usage_export_ledger_sequences ADD CONSTRAINT "
                "unexpected_native_constraint CHECK(usage_export_sequence<10) NOT VALID"
            )
        else:
            connection.execute(
                "ALTER TABLE tenant_deletion_table_manifest DISABLE TRIGGER "
                "trg_tenant_deletion_table_manifest_update_prd089"
            )
            connection.execute(
                "UPDATE tenant_deletion_table_manifest SET column_signature_sha256=? "
                "WHERE source_table='usage_export_ledger_sequences'",
                ("0" * 64,),
            )
            connection.execute(
                "ALTER TABLE tenant_deletion_table_manifest ENABLE TRIGGER "
                "trg_tenant_deletion_table_manifest_update_prd089"
            )
        connection.commit()
    with pytest.raises(DbError):
        if direction == "up":
            apply_migrations(database)
        else:
            rollback_last_migration(database, expected_version="0149")
    assert _state(database) == []
