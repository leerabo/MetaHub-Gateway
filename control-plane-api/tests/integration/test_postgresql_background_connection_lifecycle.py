"""Real PostgreSQL idle polls and rejected fences must not exhaust a small pool."""

from contextlib import closing
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
import test_postgresql_native_migrations as native
import test_prd147_postgresql_runtime as database_helpers
from metahub_control.brand_domains import scheduler as brand_scheduler
from metahub_control.brand_domains import service as domains
from metahub_control.branding import service as branding
from metahub_control.console_experience import scheduler as console_scheduler
from metahub_control.customer_health import scheduler as customer_health
from metahub_control.customer_support import service as support
from metahub_control.partner_portal import service as partners
from metahub_control.provider_health import service as providers
from metahub_control.provider_status import service as provider_status
from metahub_control.usage import export_service as exports
from metahub_shared.db import apply_migrations, connect_database
from metahub_shared.db import connection as database
from metahub_shared.runtime.config import load_app_config
from metahub_shared.runtime.errors import ApiError

pytestmark = native.pytestmark


@pytest.fixture(scope="module")
def migrated_template():
    name, url = database_helpers._create_database()
    try:
        apply_migrations(url)
        database.close_database_pools()
        yield name
    finally:
        database_helpers._drop_database(name)


@pytest.fixture
def postgresql_database(migrated_template):
    name, url = database_helpers._create_database(template=migrated_template)
    try:
        yield url
    finally:
        database_helpers._drop_database(name)


def test_idle_polls_and_rejected_commit_return_every_postgresql_checkout(
    postgresql_database, monkeypatch,
):
    monkeypatch.setenv("METAHUB_DATABASE_POOL_MIN_SIZE", "1")
    monkeypatch.setenv("METAHUB_DATABASE_POOL_MAX_SIZE", "2")
    monkeypatch.setenv("METAHUB_DATABASE_POOL_TIMEOUT_SECONDS", "1")
    config = load_app_config("control-plane-api").model_copy(
        update={"database_url": postgresql_database, "environment": "test"},
    )
    baseline = sum(pool.get_stats().get("requests_errors", 0)
                   for pool in database._POSTGRES_POOLS.values())
    rejected = 0

    def lost_fence(_connection):
        nonlocal rejected
        rejected += 1
        raise ApiError("platform.unavailable")

    # More repetitions than the pool maximum expose even a one-per-poll leak.
    # These are real service SQL paths on the full migrated, unseeded schema.
    for _ in range(24):
        assert providers.claim_next_scheduled(config) is None
        assert support.process_attachment_scans(config) == 0
        assert customer_health.claim_next_customer_health_tenant(
            config, worker_id="connection-lifecycle",
        ) is None
        assert brand_scheduler._claim_job(
            config, job_code="no-such-test-job", worker_id="connection-lifecycle",
        ) is None
        for service in (domains, branding, partners):
            with closing(service._connect(config)) as connection:
                assert connection.execute("SELECT 1").fetchone()[0] == 1
        assert exports.expire_usage_exports(config) == 0
        assert exports.claim_next_usage_export(config) is None
        with database.database_commit_guard(lost_fence):
            for operation in (exports.expire_usage_exports, exports.claim_next_usage_export):
                with pytest.raises(ApiError) as failure:
                    operation(config)
                assert failure.value.code == "platform.unavailable"
        # Neither the failed fence nor a read-only poll may retain its transaction.
        with closing(connect_database(postgresql_database)) as connection:
            assert not connection.in_transaction
            assert connection.execute("SELECT 1").fetchone()[0] == 1
        stats = tuple(pool.get_stats() for pool in database._POSTGRES_POOLS.values())
        assert all(item["pool_available"] == item["pool_size"] for item in stats)
        assert all(item.get("requests_waiting", 0) == 0 for item in stats)
    assert rejected == 48
    assert sum(pool.get_stats().get("requests_errors", 0)
               for pool in database._POSTGRES_POOLS.values()) == baseline


def test_retired_health_scheduler_stays_read_only_on_production_schema(postgresql_database):
    with closing(connect_database(postgresql_database)) as connection:
        now = customer_health._transaction_now(connection, None)
        # Migration 0140 retired this scheduler. Fixing its connection handling
        # must not silently disable that production write guard.
        connection.execute(
            "INSERT INTO tenants(tenant_id,name,slug,service_mode,status,created_at,updated_at) "
            "VALUES('health-pool-test','Pool test','health-pool-test','internal_only','active',?,?)",
            (customer_health._db_time(now), customer_health._db_time(now)),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO customer_health_scheduler_tenants(tenant_id,status,generation,"
                "consecutive_failure_count,next_evaluation_at,created_at,updated_at) "
                "VALUES('health-pool-test','pending',0,0,?,?,?)",
                (customer_health._db_time(now),) * 3,
            )
        connection.rollback()
        assert connection.execute(
            "SELECT count(*) FROM tenants WHERE tenant_id='health-pool-test'",
        ).fetchone()[0] == 0


def test_platform_dashboard_can_initialize_on_unseeded_postgresql(postgresql_database):
    """A new Ubuntu owner's overview cannot depend on development seed data."""
    config = load_app_config("control-plane-api").model_copy(
        update={"database_url": postgresql_database, "environment": "test"},
    )
    assert console_scheduler._refresh_platform_dashboard(config) is True
    with closing(connect_database(postgresql_database)) as connection:
        head = connection.execute(
            "SELECT platform_dashboard_projection_id,generation FROM "
            "platform_dashboard_projection_heads WHERE head_key='platform'",
        ).fetchone()
        assert head is not None
        rows = connection.execute(
            "SELECT source_code,metric_value FROM platform_dashboard_projection_sources "
            "WHERE platform_dashboard_projection_id=? AND generation=?",
            (head["platform_dashboard_projection_id"], head["generation"]),
        ).fetchall()
        assert {row["source_code"] for row in rows} == {
            "status", "observability", "jobs", "capacity", "channel", "customer_success",
            "growth", "branding", "provider_engineering", "commercial_accounting",
        }
        assert all(int(row["metric_value"]) >= 0 for row in rows)
    # An unchanged snapshot must remain readable without creating another head.
    assert console_scheduler._refresh_platform_dashboard(config) is False


def test_provider_status_native_aggregation_precision_and_contract(postgresql_database):
    config = load_app_config("control-plane-api").model_copy(
        update={"database_url": postgresql_database, "environment": "test"},
    )
    now = datetime.now(UTC).replace(microsecond=0)
    observed = now - timedelta(minutes=1)
    with closing(provider_status._connect(config)) as connection:
        # Native types from the migrated production tables, without constraints,
        # permit deliberately corrupt facts to exercise read-contract rejection.
        for table in ("routing_attempts", "stream_sessions"):
            connection.execute(f"CREATE TEMP TABLE {table} AS SELECT * FROM public.{table} WITH NO DATA")
        for ordinal in range(1, 21):
            connection.execute(
                "INSERT INTO routing_attempts(tenant_id,provider_id,routing_attempt_id,"
                "request_id,trace_id,status,usage_status,latency_ms,completed_at) "
                "VALUES('test-tenant','test-provider',?,?,?,'succeeded','available',?,?)",
                (f"attempt-{ordinal}", f"request-{ordinal}", "test-trace", ordinal,
                 provider_status._db_time(observed)),
            )
        connection.execute(
            "INSERT INTO stream_sessions(tenant_id,provider_id,stream_session_id,request_id,"
            "trace_id,status,error_code,started_at,first_chunk_at,completed_at) "
            "VALUES('test-tenant','test-provider','test-stream','stream-request','test-trace',"
            "'timed_out','gateway.upstream_stream_timeout',?,?,?)",
            (provider_status._db_time(observed),
             (observed + timedelta(microseconds=123456)).isoformat(),
             provider_status._db_time(observed + timedelta(seconds=2))),
        )
        # A different tenant's corrupt fact must neither enter metrics nor fail
        # this tenant's page; the server-side filter remains authoritative.
        connection.execute(
            "INSERT INTO routing_attempts(tenant_id,provider_id,status,completed_at) "
            "VALUES('other-tenant','test-provider','failed','invalid-date')",
        )
        metrics, failures = provider_status._load_call_facts(
            connection, "test-tenant", ["test-provider"], now - timedelta(days=1),
            now, now - timedelta(days=7),
        )
        item = metrics["test-provider"]
        assert (item.terminal, item.success, item.provider_failure, item.eligible) == (21, 20, 1, 21)
        assert (item.call_latency_sample_count, item.call_latency_p95_ms) == (20, 19)
        assert (item.stream_first_chunk_sample_count, item.stream_first_chunk_p95_ms) == (1, 123)
        assert len(failures) == 1 and failures[0].error_code == "gateway.upstream_stream_timeout"
        assert failures[0].latency_ms == 2000
        # Offset timestamps and historical UTC spellings preserve exact micros.
        for spelling in ("2026-10-15T13:20:30.123456Z", "2026-10-15T21:20:30.123456+08:00",
                         "2026-10-15 13:20:30.123456"):
            connection.execute("CREATE TEMP TABLE IF NOT EXISTS status_time_probe(value TEXT)")
            connection.execute("DELETE FROM status_time_probe")
            connection.execute("INSERT INTO status_time_probe VALUES(?)", (spelling,))
            actual = connection.execute("SELECT metahub_epoch_us(value) FROM status_time_probe").fetchone()[0]
            assert actual == provider_status._epoch_microseconds(spelling)
        connection.execute("UPDATE stream_sessions SET error_code='secret_like_unknown_code'")
        with pytest.raises(provider_status._DataContractError):
            provider_status._load_call_facts(
                connection, "test-tenant", ["test-provider"], now - timedelta(days=1),
                now, now - timedelta(days=7),
            )
