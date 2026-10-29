"""Management assistant on the full native schema, without customer data or model calls."""

import asyncio
import secrets
import sqlite3
import time
from contextlib import closing

import pytest
import test_postgresql_native_migrations as native
import test_prd077_admin_assistant_control as control
from httpx import ASGITransport, AsyncClient
from metahub_control.admin_assistant import outbox, repository, service
from metahub_shared.db import apply_seed_data, connect_database
from metahub_shared.db import connection as database
from metahub_shared.db.connection import database_operation_deadline
from test_postgresql_background_connection_lifecycle import migrated_template as migrated_template
from test_postgresql_background_connection_lifecycle import (
    postgresql_database as postgresql_database,
)
from test_prd147_console_migration import _config

pytestmark = native.pytestmark


def test_native_read_only_snapshot_and_deadline_leave_warm_connection_reusable(postgresql_database):
    with closing(connect_database(postgresql_database)) as connection:
        connection.execute("CREATE TABLE assistant_read_only_probe(value INTEGER)")
        connection.execute("INSERT INTO assistant_read_only_probe VALUES(1)")
        connection.commit()
        with database_operation_deadline(time.monotonic() + 5):
            connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            assert (
                connection.execute("SHOW transaction_isolation").fetchone()[0] == "repeatable read"
            )
            assert connection.execute("SHOW transaction_read_only").fetchone()[0] == "on"
            with pytest.raises(sqlite3.Error):
                connection.execute("UPDATE assistant_read_only_probe SET value=2")
        connection.rollback()
        assert not connection.in_transaction
        with database_operation_deadline(time.monotonic() + 0.050):
            with pytest.raises(sqlite3.Error):
                connection.execute("SELECT pg_sleep(1)")
        connection.rollback()
        connection.execute("UPDATE assistant_read_only_probe SET value=3")
        connection.commit()
        assert connection.execute("SELECT value FROM assistant_read_only_probe").fetchone()[0] == 3


def test_native_empty_outbox_can_repeatedly_claim_without_sqlite_rowid(postgresql_database):
    config = _config(postgresql_database)
    for _ in range(5):
        assert outbox.claim_next_assistant_event(config, worker_id="native-assistant") is None


def test_native_error_projection_maps_private_values_before_grouping(postgresql_database):
    with closing(connect_database(postgresql_database)) as connection:
        connection.execute(
            "CREATE TEMP TABLE request_logs AS SELECT * FROM public.request_logs WITH NO DATA"
        )
        connection.executemany(
            "INSERT INTO request_logs(tenant_id,status,error_code,created_at) "
            "VALUES(?,'failed',?,?)",
            [
                ("own", code, "2026-10-15T00:00:00Z")
                for code in (
                    None,
                    "private-canary-a",
                    "private-canary-b",
                    "gateway.upstream_timeout",
                )
            ]
            + [("other", "private-foreign", "2026-10-15T00:00:00Z")],
        )
        values, counts = repository._error_values(
            connection,
            "own",
            None,
            "2026-10-14T00:00:00Z",
            "2026-10-16T00:00:00Z",
        )
        assert values == {
            "top_errors": (("infra.unhandled", 3), ("gateway.upstream_timeout", 1)),
            "failure_count": 4,
        }
        assert counts == {"request_logs": 4}


@pytest.mark.anyio
async def test_native_four_queries_commit_audits_and_deliver_empty_endpoint_snapshot(
    postgresql_database,
    monkeypatch,
):
    password = secrets.token_urlsafe(40)
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", password)
    monkeypatch.setenv("METAHUB_LOG_LEVEL", "ERROR")
    server_errors = []
    raise_postgres_error = database._raise_postgres_error

    def record_server_error(error):
        # Isolated server identifiers and parser diagnostics only; never SQL
        # parameters, password, tokens or constraint DETAIL containing row values.
        diagnostic = getattr(error, "diag", None)
        server_errors.append(
            (
                getattr(error, "sqlstate", None),
                getattr(diagnostic, "table_name", None),
                getattr(diagnostic, "constraint_name", None),
                getattr(diagnostic, "source_function", None),
                getattr(diagnostic, "statement_position", None),
                getattr(diagnostic, "message_primary", None)
                if getattr(error, "sqlstate", None) in {"25001", "42601"}
                else None,
            )
        )
        raise_postgres_error(error)

    monkeypatch.setattr(database, "_raise_postgres_error", record_server_error)
    seed = apply_seed_data(postgresql_database)
    config = _config(postgresql_database)
    with closing(connect_database(postgresql_database)) as connection:
        assert connection.execute("SELECT count(*) FROM webhook_endpoints").fetchone()[0] == 0
    app = control._build_app()
    app.state.config = config
    control._clear_rate_limiter()
    runtime = await service.initialize_admin_assistant_execution_runtime(app)
    try:
        assert runtime.read_connection.backend == runtime.write_connection.backend == "postgresql"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            logged_in = await client.post(
                "/auth/login",
                json={
                    "tenant_slug": "default",
                    "email": "admin@metahub.local",
                    "password": password,
                },
            )
            assert logged_in.status_code == 200, logged_in.json().get("code")
            token = logged_in.json()["access_token"]
            for suffix, question in (
                ("usage", "usage in the last 7 days"),
                ("errors", "explain errors in the last 24 hours"),
                ("cost", "cost advice for the last 30 days"),
                ("policy", "explain the current policy"),
            ):
                response = await control._ask(client, token, "native_" + suffix, question)
                assert response.status_code == 200, (response.json().get("code"), server_errors)
                body = response.json()
                assert body["sources"]
                source_ids = {source["source_id"] for source in body["sources"]}
                assert all(
                    set(claim["source_ids"]) <= source_ids for claim in body["answer"]["claims"]
                )
        with closing(connect_database(postgresql_database)) as connection:
            rows = connection.execute(
                "SELECT query.outcome,query.model_call_count,query.model_input_tokens,"
                "query.model_output_tokens,query.model_cost_cny_micros,log.action "
                "FROM assistant_queries query JOIN assistant_audits audit "
                "ON audit.tenant_id=query.tenant_id "
                "AND audit.assistant_query_id=query.assistant_query_id "
                "JOIN audit_logs log ON log.tenant_id=audit.tenant_id "
                "AND log.audit_log_id=audit.audit_log_id "
                "WHERE query.tenant_id=?",
                (seed.tenant_id,),
            ).fetchall()
            assert len(rows) == 4
            assert all(
                tuple(row) == ("answered", 0, 0, 0, 0, "admin_assistant.queried") for row in rows
            )
        for _ in range(4):
            claim = outbox.claim_next_assistant_event(config, worker_id="native-assistant")
            assert claim is not None
            assert outbox.dispatch_claimed_assistant_event(config, claim)
        assert outbox.claim_next_assistant_event(config, worker_id="native-assistant") is None
    finally:
        await service.clear_admin_assistant_execution_runtime(app, asyncio.get_running_loop())
