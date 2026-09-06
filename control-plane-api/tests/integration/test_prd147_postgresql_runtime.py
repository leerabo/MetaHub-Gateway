"""Native PostgreSQL seeding and scheduler fencing, beyond schema compilation."""

from __future__ import annotations

import inspect
import os
import secrets
import sqlite3
from contextlib import closing
from uuid import uuid4

import pytest
import test_postgresql_native_migrations as native_migrations
import test_prd147_console_migration as contracts
from metahub_control.console_migration.scheduler import (
    claim_scheduler_lease,
    release_scheduler_lease,
    renew_scheduler_lease,
    require_scheduler_fences,
)
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    close_database_pools,
    connect_database,
)
from metahub_shared.db.schema import foreign_key_violation_count, table_columns, table_exists
from test_prd147_console_migration import _config

postgresql_database = native_migrations.postgresql_database
pytestmark = pytest.mark.skipif(
    not os.getenv("METAHUB_TEST_POSTGRESQL_ADMIN_URL"),
    reason="METAHUB_TEST_POSTGRESQL_ADMIN_URL is required",
)


@pytest.mark.parametrize("table", contracts.INDIRECT_OWNER_TABLES)
def test_postgresql_indirect_owner_evidence(postgresql_database, monkeypatch, table):
    with closing(connect_database(postgresql_database)) as connection:
        contracts._assert_indirect_owner_evidence(connection, monkeypatch, table)


@pytest.mark.parametrize("match,expected", [("SIMPLE", 1), ("FULL", 3)])
def test_postgresql_foreign_key_check_preserves_transaction(postgresql_database, match, expected):
    with closing(connect_database(postgresql_database)) as connection:
        connection.execute('CREATE TABLE parent(a INTEGER,b INTEGER,PRIMARY KEY(a,b))')
        connection.execute(
            'CREATE TABLE "child odd"("a odd" INTEGER,b INTEGER, '
            f'FOREIGN KEY("a odd",b) REFERENCES parent(a,b) MATCH {match} '
            'DEFERRABLE INITIALLY DEFERRED)'
        )
        connection.execute('CREATE TABLE probe(z TEXT,a INTEGER PRIMARY KEY)')
        connection.execute('CREATE VIEW probe_view AS SELECT * FROM probe')
        connection.execute('INSERT INTO parent VALUES(1,2)')
        connection.commit()
        assert table_exists(connection, "probe")
        assert not table_exists(connection, "probe_view")
        assert not table_exists(connection, "missing")
        assert table_columns(connection, "probe") == ("z", "a")
        assert table_columns(connection, "missing") == ()
        connection.executemany(
            'INSERT INTO "child odd" VALUES(?,?)',
            [(1, 2), (None, None), (9, None), (None, 9), (9, 9)],
        )
        assert foreign_key_violation_count(connection) == expected
        assert connection.in_transaction
        connection.rollback()
        assert connection.execute('SELECT count(*) FROM "child odd"').fetchone()[0] == 0
        assert foreign_key_violation_count(connection) == 0


@pytest.mark.parametrize("guard", ["unvalidated", "disabled", "replica"])
def test_postgresql_foreign_key_check_rejects_unavailable_enforcement(postgresql_database, guard):
    with closing(connect_database(postgresql_database)) as connection:
        connection.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE child(id INTEGER)")
        suffix = " NOT VALID" if guard == "unvalidated" else ""
        connection.execute(
            "ALTER TABLE child ADD CONSTRAINT owner_fk FOREIGN KEY(id) REFERENCES parent(id)"
            + suffix
        )
        if guard == "disabled":
            connection.execute("ALTER TABLE child DISABLE TRIGGER ALL")
        elif guard == "replica":
            connection.execute("SET LOCAL session_replication_role=replica")
        with pytest.raises(sqlite3.DatabaseError, match="Foreign key enforcement is unavailable"):
            foreign_key_violation_count(connection)
        assert connection.in_transaction
        connection.rollback()


def test_postgresql_seed_and_scheduler_fencing(postgresql_database, monkeypatch):
    assert apply_migrations(postgresql_database).applied[-1] == "0139"
    with closing(connect_database(postgresql_database)) as connection:
        assert tuple(connection.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%SZ',?),? % 3,'100%'",
            ("2026-09-06T00:00:00Z", 8),
        ).fetchone()) == ("2026-09-06T00:00:00Z", 2, "100%")
        with closing(sqlite3.connect(":memory:")) as reference:
            documents = (
                ' [ "workspace.read", "workspace.write" ] ',
                '{"zz":1,"a":2,"zz":3}',
                '{ "nested": [ true, false, null, {"text":"a b"} ] }',
                r'{"\u0061":"\u4f60\u597d", "quote":"a\" b", "slash":"a\\ b"}',
                '[1.00,1e+2,-0,123456789012345678901234567890]',
                '\t[\n1,\r2]\n',
                '"   "', 'null',
            )
            for document in documents:
                expected = reference.execute("SELECT json(?)", (document,)).fetchone()[0]
                assert connection.execute(
                    "SELECT metahub_json(?)", (document,)
                ).fetchone()[0] == expected
            for document in ('{"missing":}', '[1,]', 'not-json', None):
                assert connection.execute(
                    "SELECT metahub_json(?)", (document,)
                ).fetchone()[0] is None
    monkeypatch.setenv("METAHUB_SEED_ADMIN_PASSWORD", secrets.token_urlsafe(48))
    seed = apply_seed_data(postgresql_database)
    assert apply_seed_data(postgresql_database).tenant_id == seed.tenant_id
    config = _config(postgresql_database).model_copy(
        update={"control_background_schedulers_enabled": True}
    )
    with closing(connect_database(postgresql_database)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM workspace_tenant_bindings "
            "WHERE tenant_id=? AND status='active'", (seed.tenant_id,),
        ).fetchone()[0] == 1
    lease = claim_scheduler_lease(
        config, scheduler_code="platform-job-runtime", queue_code="platform-job-runtime.jobs",
        replica_code="native-a", lease_seconds=60,
    )
    assert lease is not None
    assert claim_scheduler_lease(
        config, scheduler_code="platform-job-runtime", queue_code="platform-job-runtime.jobs",
        replica_code="native-b", lease_seconds=60,
    ) is None
    renewed = renew_scheduler_lease(config, lease, replica_code="native-a", lease_seconds=60)
    assert renewed is not None
    with closing(connect_database(postgresql_database)) as connection:
        connection.execute("BEGIN")
        require_scheduler_fences(connection, config, (renewed,))
        connection.commit()
    assert release_scheduler_lease(config, renewed, replica_code="native-a")
    replacement = claim_scheduler_lease(
        config, scheduler_code="platform-job-runtime", queue_code="platform-job-runtime.jobs",
        replica_code="native-b", lease_seconds=60,
    )
    assert replacement is not None
    assert replacement.generation > renewed.generation
    assert not release_scheduler_lease(config, renewed, replica_code="native-a")
    assert release_scheduler_lease(config, replacement, replica_code="native-b")


def _create_database(*, template=None):
    database_name = f"metahub_test_{uuid4().hex}"
    command = native_migrations.sql.SQL("CREATE DATABASE {}").format(
        native_migrations.sql.Identifier(database_name)
    )
    if template is not None:
        command += native_migrations.sql.SQL(" TEMPLATE {}").format(
            native_migrations.sql.Identifier(template)
        )
    close_database_pools()
    with native_migrations.psycopg.connect(
        native_migrations.ADMIN_URL, autocommit=True
    ) as admin:
        admin.execute(command)
    return database_name, native_migrations._database_url(
        native_migrations.ADMIN_URL, database_name
    )


def _drop_database(name):
    assert name.startswith("metahub_test_") and len(name) == 45
    close_database_pools()
    with native_migrations.psycopg.connect(
        native_migrations.ADMIN_URL, autocommit=True
    ) as admin:
        admin.execute(native_migrations.sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
            native_migrations.sql.Identifier(name)
        ))


@pytest.fixture(scope="module")
def native_runtime_template():
    name, database_url = _create_database()
    try:
        assert apply_migrations(database_url).applied[-1] == "0139"
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", secrets.token_urlsafe(48))
            seed = apply_seed_data(database_url)
        config = _config(database_url)
        with closing(connect_database(database_url)) as connection:
            owner = dict(connection.execute(
                "SELECT workspace_id,tenant_id FROM workspace_tenant_bindings "
                "WHERE tenant_id=? AND status='active'", (seed.tenant_id,),
            ).fetchone())
            actors = {
                "preparer": contracts._insert_actor(connection, "1", "platform_ops"),
                "executor": contracts._insert_actor(connection, "2", "platform_ops"),
                "owner": contracts._insert_actor(connection, "3", "platform_owner"),
                "security": contracts._insert_actor(connection, "4", "platform_security"),
            }
            connection.commit()
        close_database_pools()
        yield {"template_name": name, "config": config, "owner": owner, "actors": actors}
    finally:
        _drop_database(name)


@pytest.fixture
def native_runtime(native_runtime_template):
    name, database_url = _create_database(template=native_runtime_template["template_name"])
    try:
        config = native_runtime_template["config"].model_copy(update={"database_url": database_url})
        owner = native_runtime_template["owner"]
        yield {
            **native_runtime_template, "config": config,
            "platform_token": "p" * 43,
            "workspace_ref": contracts.service._workspace_ref(
                config, owner["workspace_id"], owner["tenant_id"]
            ),
        }
    finally:
        _drop_database(name)


SCENARIOS = (
    "test_workspace_evidence_is_owner_scoped_for_all_domains",
    "test_status_strict_boundary_and_permission_before_body",
    "test_backfill_idempotency_resume_and_cursor_binding",
    "test_all_active_backfill_fans_out_and_generates_reconciliation",
    "test_cutover_requires_four_independent_accounts_and_releases_contract",
    "test_cutover_sod_reconciliation_and_rollback_fail_closed",
    "test_event_outbox_recovers_expired_claim_and_stops_after_sent",
    "test_event_outbox_retries_sink_failure_after_backoff",
    "test_event_outbox_does_not_finalize_after_outer_fence_loss",
    "test_platform_navigation_projects_console_migration_permission",
)


@pytest.mark.anyio
@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_postgresql_console_domain_contracts(scenario, native_runtime, monkeypatch):
    case = getattr(contracts, scenario)
    if inspect.iscoroutinefunction(case):
        await case(runtime=native_runtime, monkeypatch=monkeypatch)
    elif "monkeypatch" in inspect.signature(case).parameters:
        case(runtime=native_runtime, monkeypatch=monkeypatch)
    else:
        case(native_runtime)
