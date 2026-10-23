"""Exercise real migrated guards against PostgreSQL's changing tuple identity.

Temporary table copies isolate each guard from unrelated setup/FK contracts;
the unmodified production trigger function executes on the real server.
"""

import sqlite3
from contextlib import closing

import pytest
import test_postgresql_native_migrations as native
from metahub_shared.db import DbError, apply_migrations, connect_database, rollback_last_migration
from test_postgresql_background_connection_lifecycle import (
    migrated_template as migrated_template,
)
from test_postgresql_background_connection_lifecycle import (
    postgresql_database as postgresql_database,
)

pytestmark = native.pytestmark


def attach_guard(connection, table, function):
    # Identifiers are test constants, never request input.
    connection.execute(f"CREATE TEMP TABLE {table} AS SELECT * FROM public.{table} WITH NO DATA")
    connection.execute(
        f"CREATE TRIGGER identity_probe BEFORE UPDATE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION public.{function}()"
    )


def rejected(connection, statement, parameters=()):
    connection.execute("SAVEPOINT rejected_write")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(statement, parameters)
    connection.execute("ROLLBACK TO SAVEPOINT rejected_write")
    connection.execute("RELEASE SAVEPOINT rejected_write")


def test_affinity_mutable_fields_survive_tuple_move_but_identity_cannot_change(postgresql_database):
    with closing(connect_database(postgresql_database)) as connection:
        attach_guard(connection, "cache_affinity_bindings",
                     "fn_trg_cache_affinity_bindings_prd050_identity_update")
        connection.execute(
            "INSERT INTO cache_affinity_bindings(cache_affinity_binding_id,tenant_id,"
            "cache_affinity_scope_id,affinity_key_digest,model_endpoint_id,"
            "endpoint_revision_digest,provider_id,created_at,updated_at) "
            "VALUES('binding','own','scope','digest','endpoint','revision','provider',?,?)",
            ("2026-10-16T00:00:00Z",) * 2,
        )
        before = connection.execute("SELECT ctid::text FROM cache_affinity_bindings").fetchone()[0]
        connection.execute(
            "UPDATE cache_affinity_bindings SET updated_at='2026-10-16T00:00:01Z'"
        )
        after = connection.execute("SELECT ctid::text FROM cache_affinity_bindings").fetchone()[0]
        assert before != after, "A valid UPDATE must not depend on a stable physical tuple"
        for column in ("cache_affinity_binding_id", "tenant_id", "provider_id", "created_at"):
            rejected(connection, f"UPDATE cache_affinity_bindings SET {column}='changed'")
        assert connection.execute(
            "SELECT cache_affinity_binding_id,tenant_id,provider_id FROM cache_affinity_bindings"
        ).fetchone()[:] == ("binding", "own", "provider")


def test_outbox_claim_renew_and_finish_keep_lease_and_immutable_facts(postgresql_database):
    with closing(connect_database(postgresql_database)) as connection:
        attach_guard(connection, "batch_job_event_outbox", "fn_trg_batch_outbox_update_prd074")
        connection.execute(
            "INSERT INTO batch_job_event_outbox(batch_job_event_id,tenant_id,created_at,"
            "updated_at,dispatch_status,dispatch_attempts,next_attempt_at) "
            "VALUES('event','own',?,?,'pending',0,?)",
            ("2026-10-16T00:00:00Z",) * 3,
        )
        connection.execute(
            "UPDATE batch_job_event_outbox SET dispatch_status='processing',"
            "dispatch_attempts=1,next_attempt_at=NULL,claim_token=?,lease_owner_id='worker',"
            "lease_expires_at='2026-10-16T00:01:31Z',updated_at='2026-10-16T00:00:01Z'",
            ("a" * 32,),
        )
        rejected(connection, "UPDATE batch_job_event_outbox SET lease_owner_id='other'")
        rejected(connection, "UPDATE batch_job_event_outbox SET batch_job_event_id='other'")
        rejected(connection, "UPDATE batch_job_event_outbox SET tenant_id='other'")
        connection.execute(
            "UPDATE batch_job_event_outbox SET updated_at='2026-10-16T00:00:02Z',"
            "lease_expires_at='2026-10-16T00:01:32Z'"
        )
        rejected(connection,
                 "UPDATE batch_job_event_outbox SET lease_expires_at='2026-10-16T00:01:33Z'")
        connection.execute(
            "UPDATE batch_job_event_outbox SET dispatch_status='dispatched',claim_token=NULL,"
            "lease_owner_id=NULL,lease_expires_at=NULL,updated_at='2026-10-16T00:00:03Z',"
            "dispatched_at='2026-10-16T00:00:03Z'"
        )
        assert connection.execute(
            "SELECT batch_job_event_id,tenant_id,dispatch_status,dispatch_attempts "
            "FROM batch_job_event_outbox"
        ).fetchone()[:] == ("event", "own", "dispatched", 1)


def test_composite_identity_keeps_circuit_state_transition_guards(postgresql_database):
    with closing(connect_database(postgresql_database)) as connection:
        attach_guard(connection, "mcp_network_circuits",
                     "fn_trg_mcp_network_circuits_transition_prd083")
        connection.execute(
            "INSERT INTO mcp_network_circuits(tenant_id,mcp_server_id,policy_revision,"
            "mcp_network_policy_id,created_at,updated_at,state_revision,state) "
            "VALUES('own','server',1,'policy',?,?,1,'closed')",
            ("2026-10-16T00:00:00Z",) * 2,
        )
        connection.execute(
            "UPDATE mcp_network_circuits SET state='open',state_revision=2,"
            "updated_at='2026-10-16T00:00:01Z'"
        )
        for column, value in (("tenant_id", "other"), ("mcp_server_id", "other"),
                              ("policy_revision", 2)):
            rejected(connection,
                     f"UPDATE mcp_network_circuits SET state_revision=3,{column}=?", (value,))
        rejected(connection, "UPDATE mcp_network_circuits SET state='closed',state_revision=3")
        rejected(connection, "UPDATE mcp_network_circuits SET state_revision=4")
        assert connection.execute(
            "SELECT tenant_id,mcp_server_id,policy_revision,state_revision,state "
            "FROM mcp_network_circuits"
        ).fetchone()[:] == ("own", "server", 1, 2, "open")


def test_forward_only_identity_repair_keeps_catalog_and_applied_history(postgresql_database):
    def snapshot():
        with closing(connect_database(postgresql_database)) as connection:
            history = tuple(tuple(row) for row in connection.execute(
                "SELECT version,name,checksum,status FROM schema_migrations ORDER BY version"
            ).fetchall())
            functions = tuple(tuple(row) for row in connection.execute(
                "SELECT p.proname,pg_get_functiondef(p.oid) FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid=p.pronamespace "
                "WHERE n.nspname='public' AND p.prokind='f' ORDER BY p.proname,p.oid"
            ).fetchall())
            return history, functions

    before = snapshot()
    with pytest.raises(DbError) as failure:
        rollback_last_migration(postgresql_database, expected_version="0194")
    assert failure.value.code == "db.migration_failed"
    assert snapshot() == before
    assert apply_migrations(postgresql_database).applied == []
    assert snapshot() == before
