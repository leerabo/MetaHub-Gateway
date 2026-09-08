"""Native audit watermark allocation, immutable mapping and migration contracts."""

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Event
from uuid import uuid4

import g6_postgresql_support as native_support
import pytest
import test_postgresql_native_migrations as native_migrations
from metahub_control.audit.service import AuditActor, AuditRequestMeta, record_audit_log_for_actor
from metahub_shared.db import DbError, apply_migrations, connect_database, rollback_last_migration

pytestmark = native_migrations.pytestmark
native_business_template = native_support.native_business_template
native_business_database = native_support.native_business_database


def _append(connection):
    owner = connection.execute(
        "SELECT actor.tenant_id,actor.user_id,actor.email,actor.role,provider.provider_id "
        "FROM users actor JOIN providers provider ON provider.tenant_id=actor.tenant_id "
        "WHERE actor.role='admin' AND actor.status='active' ORDER BY actor.user_id,provider.provider_id LIMIT 1"
    ).fetchone()
    assert owner is not None
    record = record_audit_log_for_actor(
        connection,
        AuditActor(
            tenant_id=owner["tenant_id"],
            user_id=owner["user_id"],
            email=owner["email"],
            role=owner["role"],
        ),
        action="provider.updated",
        object_type="provider",
        object_id=owner["provider_id"],
        object_name="Native test",
        before_summary={"name": "Before"},
        after_summary={"name": "After"},
        request_meta=AuditRequestMeta(None, None),
        request_id="audit-" + uuid4().hex,
        trace_id="native-test",
    )
    assert record is not None
    return tuple(
        connection.execute(
            "SELECT audit_log_sequence,tenant_id,audit_log_id FROM audit_log_export_sequences WHERE audit_log_id=?",
            (record.audit_log_id,),
        ).fetchone()
    )


def _state(database):
    with closing(connect_database(database)) as connection:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT audit_log_sequence,tenant_id,audit_log_id FROM audit_log_export_sequences ORDER BY audit_log_sequence"
            ).fetchall()
        ]


def test_native_sequence_empty_roundtrip(native_business_database):
    database = native_business_database
    assert _state(database) == []
    assert rollback_last_migration(database, expected_version="0145").rolled_back == "0145"
    assert apply_migrations(database).applied == ["0145"]
    assert _state(database) == []


def test_native_sequence_mapping_is_immutable_and_blocks_rollback(native_business_database):
    database = native_business_database
    with closing(connect_database(database)) as connection:
        first = _append(connection)
        connection.commit()
        second = _append(connection)
        connection.commit()
        assert 0 < first[0] < second[0]
        before = _state(database)
        for value in (-1, 0, second[0] + 100):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO audit_log_export_sequences(audit_log_sequence,tenant_id,audit_log_id) VALUES(?,?,?)",
                    (value, first[1], first[2]),
                )
            connection.rollback()
        for sql, parameters in (
            (
                "INSERT INTO audit_log_export_sequences(tenant_id,audit_log_id) VALUES(?,?)",
                first[1:],
            ),
            (
                "INSERT INTO audit_log_export_sequences(tenant_id,audit_log_id) VALUES(?,?)",
                ("foreign", "missing"),
            ),
            (
                "UPDATE audit_log_export_sequences SET audit_log_sequence=? WHERE audit_log_id=?",
                (9999, first[2]),
            ),
            ("DELETE FROM audit_log_export_sequences WHERE audit_log_id=?", (first[2],)),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql, parameters)
            connection.rollback()
    with pytest.raises(DbError):
        rollback_last_migration(database, expected_version="0145")
    assert _state(database) == before
    assert not apply_migrations(database).applied


@pytest.mark.parametrize("rollback_first", [False, True])
def test_native_sequence_allocation_waits_for_prior_transaction(
    native_business_database, rollback_first
):
    database = native_business_database
    started = Event()
    written = Event()
    with (
        closing(connect_database(database)) as first_connection,
        closing(connect_database(database)) as second_connection,
    ):
        first = _append(first_connection)
        second_pid = second_connection.execute("SELECT pg_backend_pid()").fetchone()[0]
        second_connection.rollback()

        def second_writer():
            started.set()
            value = _append(second_connection)
            written.set()
            second_connection.commit()
            return value

        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(second_writer)
            try:
                assert started.wait(2)
                deadline = time.monotonic() + 3
                with closing(connect_database(database)) as observer:
                    while time.monotonic() < deadline:
                        waiting = observer.execute(
                            "SELECT 1 FROM pg_locks WHERE pid=? AND locktype='advisory' AND NOT granted",
                            (second_pid,),
                        ).fetchone()
                        if waiting:
                            break
                        time.sleep(0.01)
                assert waiting is not None and not written.is_set()
                if rollback_first:
                    first_connection.rollback()
                else:
                    first_connection.commit()
                second = pending.result(timeout=3)
            finally:
                first_connection.rollback()
        assert second[0] > first[0]
        facts = _state(database)
        assert (first in facts) is not rollback_first
        assert second in facts


@pytest.mark.parametrize("direction", ["up", "down"])
@pytest.mark.parametrize("drift", ["function", "disabled", "manifest"])
def test_native_sequence_migration_rejects_drift(native_business_database, direction, drift):
    database = native_business_database
    if direction == "up":
        rollback_last_migration(database, expected_version="0145")
    with closing(connect_database(database)) as connection:
        if drift == "function":
            connection.execute(
                "CREATE OR REPLACE FUNCTION fn_trg_audit_log_export_sequences_conflict_insert_prd058() "
                "RETURNS trigger LANGUAGE plpgsql AS $$BEGIN RETURN NEW; END;$$;"
            )
        elif drift == "disabled":
            connection.execute(
                "ALTER TABLE audit_log_export_sequences DISABLE TRIGGER trg_audit_log_export_sequences_conflict_insert_prd058"
            )
        else:
            connection.execute(
                "ALTER TABLE tenant_deletion_trigger_manifest DISABLE TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089"
            )
            connection.execute(
                "UPDATE tenant_deletion_trigger_manifest SET trigger_sha256=? WHERE source_table='audit_log_export_sequences'",
                ("0" * 64,),
            )
            connection.execute(
                "ALTER TABLE tenant_deletion_trigger_manifest ENABLE TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089"
            )
        connection.commit()
    with pytest.raises(DbError):
        if direction == "up":
            apply_migrations(database)
        else:
            rollback_last_migration(database, expected_version="0145")
    assert _state(database) == []
