"""Native FinOps allocation keeps the real Usage projection and immutable facts."""

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Event
from uuid import uuid4

import g6_postgresql_support as native_support
import pytest
import test_postgresql_native_migrations as native_migrations
from metahub_shared.db import DbError, apply_migrations, connect_database, rollback_last_migration
from test_prd043_postgresql_usage_sequence import _append, _rollback_after

pytestmark = native_migrations.pytestmark
native_business_template = native_support.native_business_template
native_business_database = native_support.native_business_database


def test_native_finops_usage_projection_rollback_and_owner_evidence(native_business_database):
    database = native_business_database
    with closing(connect_database(database)) as connection:
        _append(connection)
        connection.rollback()
        source = _append(connection)
        connection.commit()
        row = connection.execute(
            "SELECT ledger.ledger_sequence,ledger.tenant_id,ledger.usage_ledger_id,"
            "ledger.amount_micros,"
            "ledger.currency,ledger.input_tokens,ledger.output_tokens,ledger.total_tokens,"
            "ledger.source_request_id,usage.request_id,ledger.price_snapshot_id,"
            "snapshot.price_snapshot_id,"
            "ledger.price_evidence_status,snapshot.source,snapshot.price_schema_version "
            "FROM finops_ledger_entries ledger "
            "JOIN usage_ledger usage "
            "ON usage.usage_ledger_id=ledger.usage_ledger_id JOIN price_snapshots snapshot "
            "ON snapshot.price_snapshot_id=ledger.price_snapshot_id"
        ).fetchone()
        assert tuple(row[:8]) == (1, source[1], source[2], 0, "CNY", 10, 5, 15)
        assert row[8] == row[9] and row[10] == row[11]
        # The source deliberately has no original Gateway pricing snapshot.
        assert tuple(row[12:]) == (
            "legacy_amount_only",
            "finops_legacy_backfill",
            "legacy-unversioned-v1",
        )
        for sql in (
            "UPDATE metahub_finops_ledger_allocator SET last_sequence=last_sequence+1",
            "DELETE FROM metahub_finops_ledger_allocator",
            "TRUNCATE metahub_finops_ledger_allocator",
            "INSERT INTO metahub_finops_ledger_allocator VALUES(1,99)",
            "UPDATE finops_ledger_entries SET ledger_sequence=99",
            "DELETE FROM finops_ledger_entries",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql)
            connection.rollback()
        assert (
            connection.execute(
                "SELECT last_sequence FROM metahub_finops_ledger_allocator"
            ).fetchone()[0]
            == 1
        )
    later = _rollback_after(database, "0151")
    with pytest.raises(DbError):
        rollback_last_migration(database, expected_version="0151")
    assert apply_migrations(database).applied == list(reversed(later))


def _append_adjustment(connection, *, explicit_sequence=None):
    owner = connection.execute(
        "SELECT tenant_id,user_id FROM users WHERE role='admin' AND status='active' "
        "ORDER BY user_id LIMIT 1"
    ).fetchone()
    adjustment, ledger, event = (prefix + uuid4().hex for prefix in ("fadj_", "fle_", "flevt_"))
    stamp = "2026-09-09T00:00:00Z"
    connection.execute(
        "INSERT INTO finops_adjustments(finops_adjustment_id,tenant_id,adjustment_type,"
        "amount_micros,currency,reason,reason_sha256,actor_user_id,operation_request_id,"
        "operation_trace_id,idempotency_key_sha256,payload_sha256,status,created_at,updated_at) "
        "VALUES(?,?,'manual_credit',1,'CNY','Native allocator test',?,?,'req_native','trc_native',"
        "?,?,'posted',?,?)",
        (adjustment, owner[0], "1" * 64, owner[1], uuid4().hex * 2, "2" * 64, stamp, stamp),
    )
    columns = "ledger_sequence," if explicit_sequence is not None else ""
    values = "?," if explicit_sequence is not None else ""
    parameters = (explicit_sequence,) if explicit_sequence is not None else ()
    connection.execute(
        f"INSERT INTO finops_ledger_entries({columns}finops_ledger_entry_id,event_id,tenant_id,"
        "entry_type,direction,amount_micros,currency,normalized_cny_micros,cny_normalization_status,"
        "price_evidence_status,posting_source,quota_type,finops_adjustment_id,operation_request_id,"
        "operation_trace_id,actor_user_id,created_at,updated_at) "
        f"VALUES({values}?,?,?,'manual_credit','credit',1,'CNY',1,'normalized','not_applicable',"
        "'direct','paid',?,'req_native','trc_native',?,?,?)",
        (*parameters, ledger, event, owner[0], adjustment, owner[1], stamp, stamp),
    )
    return tuple(
        connection.execute(
            "SELECT ledger_sequence,finops_ledger_entry_id FROM finops_ledger_entries "
            "WHERE finops_ledger_entry_id=?",
            (ledger,),
        ).fetchone()
    )


def test_native_finops_explicit_sequence_and_sql_failure_do_not_consume_watermark(
    native_business_database,
):
    with closing(connect_database(native_business_database)) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            _append_adjustment(connection, explicit_sequence=900)
        connection.rollback()
        assert _append_adjustment(connection)[0] == 1
        connection.execute("SAVEPOINT finops_failure")
        assert _append_adjustment(connection)[0] == 2
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("SELECT missing_native_column FROM finops_ledger_entries")
        connection.execute("ROLLBACK TO SAVEPOINT finops_failure")
        connection.execute("RELEASE SAVEPOINT finops_failure")
        assert _append_adjustment(connection)[0] == 2
        connection.commit()
        assert connection.execute("SELECT COUNT(*) FROM finops_adjustments").fetchone()[0] == 2


@pytest.mark.parametrize("rollback_first", [False, True])
def test_native_finops_allocation_orders_independent_adjustment_writers(
    native_business_database,
    rollback_first,
):
    database = native_business_database
    started, written = Event(), Event()
    with (
        closing(connect_database(database)) as first_connection,
        closing(connect_database(database)) as second_connection,
    ):
        first = _append_adjustment(first_connection)
        second_pid = second_connection.execute("SELECT pg_backend_pid()").fetchone()[0]
        second_connection.rollback()

        def second_writer():
            started.set()
            result = _append_adjustment(second_connection)
            written.set()
            second_connection.commit()
            return result

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
        with closing(connect_database(database)) as connection:
            actual = [
                tuple(row)
                for row in connection.execute(
                    "SELECT ledger_sequence,finops_ledger_entry_id FROM finops_ledger_entries "
                    "ORDER BY ledger_sequence"
                )
            ]
        assert actual == ([second] if rollback_first else [first, second])


@pytest.mark.parametrize("direction", ["up", "down"])
@pytest.mark.parametrize("drift", ["function", "manifest", "constraint"])
def test_native_finops_sequence_migration_rejects_drift(native_business_database, direction, drift):
    database = native_business_database
    _rollback_after(database, "0151")
    if direction == "up":
        assert rollback_last_migration(database, expected_version="0151").rolled_back == "0151"
    with closing(connect_database(database)) as connection:
        if drift == "function":
            connection.execute(
                "CREATE OR REPLACE FUNCTION fn_trg_finops_ledger_conflict_insert_prd051() "
                "RETURNS trigger LANGUAGE plpgsql AS $$BEGIN RETURN NEW; END;$$"
            )
        elif drift == "constraint":
            connection.execute(
                "ALTER TABLE finops_ledger_entries ADD CONSTRAINT "
                "unexpected_finops_constraint CHECK(ledger_sequence<10) NOT VALID"
            )
        else:
            connection.execute(
                "ALTER TABLE tenant_deletion_table_manifest DISABLE TRIGGER "
                "trg_tenant_deletion_table_manifest_update_prd089"
            )
            connection.execute(
                "UPDATE tenant_deletion_table_manifest SET column_signature_sha256=? "
                "WHERE source_table='finops_ledger_entries'",
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
            rollback_last_migration(database, expected_version="0151")
