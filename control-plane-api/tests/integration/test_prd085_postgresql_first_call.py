"""First-success events are exactly once without aborting competing requests."""

import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Event
from uuid import uuid4

import g6_postgresql_support as native_support
import pytest
import test_postgresql_native_migrations as native_migrations
from metahub_shared.db import DbError, apply_migrations, connect_database, rollback_last_migration
from test_prd043_postgresql_usage_sequence import _rollback_after

pytestmark = native_migrations.pytestmark
native_business_template = native_support.native_business_template
native_business_database = native_support.native_business_database


def _request(connection, transition):
    tenant = connection.execute(
        "SELECT tenant_id FROM tenants ORDER BY tenant_id LIMIT 1"
    ).fetchone()[0]
    request, trace = "req_" + uuid4().hex, "trc_" + uuid4().hex
    stamp = "2026-09-09T00:00:00Z"
    connection.execute(
        "INSERT INTO gateway_request_claims(tenant_id,request_id,trace_id,virtual_key_id,"
        "snapshot_version,policy_snapshot_status,claimed_at) VALUES(?,?,?,NULL,NULL,'disabled',?)",
        (tenant, request, trace, stamp),
    )
    connection.execute(
        "INSERT INTO request_logs(request_id,tenant_id,trace_id,status,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?)",
        (request, tenant, trace, "failed" if transition else "success", stamp, stamp),
    )
    if transition:
        connection.execute(
            "UPDATE request_logs SET status='success' WHERE request_id=?",
            (request,),
        )
    return request


@pytest.mark.parametrize("transition", [False, True], ids=["insert", "update"])
@pytest.mark.parametrize("rollback_first", [False, True], ids=["commit", "rollback"])
def test_native_first_call_competing_success_preserves_both_requests(
    native_business_database,
    transition,
    rollback_first,
):
    database = native_business_database
    started, written = Event(), Event()
    with (
        closing(connect_database(database)) as first_connection,
        closing(connect_database(database)) as second_connection,
    ):
        first = _request(first_connection, transition)
        second_pid = second_connection.execute("SELECT pg_backend_pid()").fetchone()[0]
        second_connection.rollback()

        def second_writer():
            started.set()
            request = _request(second_connection, transition)
            written.set()
            second_connection.commit()
            return request

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
    with closing(connect_database(database)) as connection:
        requests = {
            row[0]
            for row in connection.execute(
                "SELECT request_id FROM request_logs WHERE status='success'"
            )
        }
        assert requests == ({second} if rollback_first else {first, second})
        events = connection.execute(
            "SELECT product_event_id,source_id FROM product_events "
            "WHERE event_type='gateway.first_call_succeeded'"
        ).fetchall()
        assert len(events) == 1
        assert events[0][1] == (second if rollback_first else first)
        assert (
            connection.execute(
                "SELECT count(*) FROM product_event_outbox WHERE product_event_id=?",
                (events[0][0],),
            ).fetchone()[0]
            == 1
        )


def test_native_first_call_migration_empty_roundtrip(native_business_database):
    database = native_business_database
    later = _rollback_after(database, "0152")
    assert rollback_last_migration(database, expected_version="0152").rolled_back == "0152"
    assert apply_migrations(database).applied == ["0152", *reversed(later)]


@pytest.mark.parametrize("direction", ["up", "down"])
@pytest.mark.parametrize("drift", ["function", "index", "manifest"])
def test_native_first_call_migration_rejects_drift(native_business_database, direction, drift):
    database = native_business_database
    _rollback_after(database, "0152")
    if direction == "up":
        assert rollback_last_migration(database, expected_version="0152").rolled_back == "0152"
    with closing(connect_database(database)) as connection:
        if drift == "function":
            connection.execute(
                "CREATE OR REPLACE FUNCTION fn_trg_request_logs_product_event_insert_prd085() "
                "RETURNS trigger LANGUAGE plpgsql AS $$BEGIN RETURN NEW; END;$$"
            )
        elif drift == "index":
            connection.execute("DROP INDEX uq_product_events_once_prd085")
        else:
            connection.execute(
                "ALTER TABLE tenant_deletion_table_manifest DISABLE TRIGGER "
                "trg_tenant_deletion_table_manifest_update_prd089"
            )
            connection.execute(
                "UPDATE tenant_deletion_table_manifest SET trigger_set_sha256=? "
                "WHERE source_table='request_logs'",
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
            rollback_last_migration(database, expected_version="0152")
