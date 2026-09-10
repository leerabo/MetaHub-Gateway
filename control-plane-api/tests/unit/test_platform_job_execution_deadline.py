"""PRD-106 execution deadlines and automatic lease renewal on the real 0098 schema."""

from __future__ import annotations

import secrets
import sqlite3
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from metahub_control.platform_jobs import runtime, service
from metahub_shared.db import connect_database
from metahub_shared.db.connection import database_request_scope
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo
from metahub_shared.runtime.errors import ApiError

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def config(tmp_path):
    database = tmp_path / "jobs.sqlite3"
    with sqlite3.connect(database) as connection:
        # Minimal prerequisite owners; job tables, checks and triggers are unmodified.
        connection.executescript("""
            CREATE TABLE schema_migrations(version TEXT, status TEXT);
            INSERT INTO schema_migrations VALUES('0097','applied');
            CREATE TABLE platform_roles(role_code TEXT PRIMARY KEY);
            INSERT INTO platform_roles VALUES('platform_owner'),('platform_ops');
            CREATE TABLE platform_operators(operator_id TEXT PRIMARY KEY);
            CREATE TABLE platform_operator_role_bindings(operator_id TEXT, role_code TEXT);
            CREATE TABLE platform_audit_logs(platform_audit_log_id TEXT PRIMARY KEY);
            CREATE TABLE effect_probe(value TEXT);
        """)
        connection.executescript(
            (ROOT / "infra/migrations/0098_platform_job_runtime.sql").read_text(encoding="utf-8")
        )
    return AppConfig(
        service_name="control-plane-api",
        environment="test",
        environment_info=EnvironmentInfo(
            name="test",
            is_production=False,
            config_profile="test",
            service_mode="internal_only",
            loaded_at=datetime.now(UTC),
        ),
        database_url=f"sqlite:///{database.as_posix()}",
        auth_token_secret=secrets.token_urlsafe(32),
        cache_affinity_hmac_secret=secrets.token_urlsafe(32),
        platform_session_secret=secrets.token_urlsafe(32),
        kms_provider="test",
        kms_test_master_key=secrets.token_urlsafe(32),
    ).model_copy(update={"platform_job_lease_seconds": 10})


@pytest.fixture
def db_clock(monkeypatch):
    clock = SimpleNamespace(now=datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1))
    monkeypatch.setattr(runtime, "_database_now", lambda _connection: clock.now)
    return clock


def enqueue(config):
    job_id, replay = service.enqueue_platform_job(
        config,
        type_code="platform.runtime.probe",
        payload={"marker": "deadline-test"},
        priority=50,
        source_code="platform.runtime.test",
        idempotency_key="execution-deadline-test",
        request_id="request-deadline",
        trace_id="trace-deadline",
    )
    assert not replay
    return job_id


def row(config, query, parameters=()):
    connection = connect_database(config.database_url)
    try:
        return connection.execute(query, parameters).fetchone()
    finally:
        connection.close()


def execute(config, query, parameters=()):
    connection = connect_database(config.database_url)
    try:
        connection.execute(query, parameters)
        connection.commit()
    finally:
        connection.close()


def set_handler(monkeypatch, handler):
    definition = runtime.handler_definition("platform.runtime.probe")
    monkeypatch.setattr(
        runtime, "handler_definition", lambda _: replace(definition, handler=handler)
    )


def assert_terminal(config, status, outcome):
    assert tuple(row(config, "SELECT status,safe_outcome_code FROM platform_jobs")) == (
        status,
        outcome,
    )
    assert (
        row(config, "SELECT COUNT(*) FROM platform_scheduler_leases WHERE status='active'")[0] == 0
    )
    assert row(config, "SELECT COUNT(*) FROM pragma_foreign_key_check")[0] == 0


def test_claim_and_renewal_never_exceed_attempt_deadline(config, db_clock):
    enqueue(config)
    lease = runtime.claim_platform_job(config, worker_code="deadline-worker", lease_seconds=300)
    assert lease.expires_at == db_clock.now + timedelta(seconds=30)
    db_clock.now += timedelta(seconds=25)
    assert runtime.heartbeat_platform_job(config, lease)
    expiry = row(config, "SELECT expires_at FROM platform_scheduler_leases")[0]
    assert runtime._time(expiry) == lease.expires_at
    db_clock.now += timedelta(seconds=5)
    assert not runtime.heartbeat_platform_job(config, lease)
    assert_terminal(config, "failed", "platform_job.timeout")
    assert not runtime.heartbeat_platform_job(config, lease)


def test_real_heartbeat_thread_keeps_long_handler_owned_and_joins(config, db_clock, monkeypatch):
    enqueue(config)
    renewed = threading.Event()
    original = runtime.heartbeat_platform_job
    thread_names = []

    def heartbeat(*args):
        result = original(*args)
        if threading.current_thread().name.startswith("platform-job-heartbeat-"):
            thread_names.append(threading.current_thread().name)
            if result:
                renewed.set()
        return result

    monkeypatch.setattr(runtime, "heartbeat_platform_job", heartbeat)

    def handler(context, _payload):
        db_clock.now += timedelta(seconds=8)
        assert renewed.wait(4), "automatic renewal did not execute"
        db_clock.now += timedelta(seconds=4)
        assert runtime.claim_platform_job(config, worker_code="competing-worker") is None
        context.commit_effect("deadline.effect", {"verified": True})
        return "platform_job.probe_completed"

    set_handler(monkeypatch, handler)
    assert runtime.run_platform_job_once(config, worker_code="renewed-worker")
    assert_terminal(config, "succeeded", "platform_job.probe_completed")
    assert thread_names
    assert not any(thread.name in thread_names for thread in threading.enumerate())
    assert row(config, "SELECT COUNT(*) FROM platform_job_attempts")[0] == 1


@pytest.mark.parametrize("boundary", ["checkpoint", "effect", "complete", "cancellation"])
def test_late_handler_cannot_commit_after_type_timeout(config, db_clock, monkeypatch, boundary):
    enqueue(config)

    def handler(context, _payload):
        db_clock.now += timedelta(seconds=30)
        if boundary == "checkpoint":
            context.checkpoint({"late": True})
        elif boundary == "effect":
            context.commit_effect("late.effect", {"late": True})
        elif boundary == "cancellation":
            context.cancellation_requested()
        return "platform_job.probe_completed"

    set_handler(monkeypatch, handler)
    runtime.run_platform_job_once(config, worker_code="late-worker")
    assert_terminal(config, "failed", "platform_job.timeout")
    for table in ("platform_job_effects", "platform_job_checkpoints"):
        assert row(config, f"SELECT COUNT(*) FROM {table}")[0] == 0


def test_input_preparation_is_part_of_attempt_timeout(config, db_clock, monkeypatch):
    enqueue(config)
    decrypt = runtime.decrypt_job_value
    called = []

    def delayed_decrypt(*args, **kwargs):
        db_clock.now += timedelta(seconds=30)
        return decrypt(*args, **kwargs)

    monkeypatch.setattr(runtime, "decrypt_job_value", delayed_decrypt)
    set_handler(monkeypatch, lambda *_: called.append(True))
    runtime.run_platform_job_once(config, worker_code="preparation-worker")
    assert called == []
    assert_terminal(config, "failed", "platform_job.timeout")


@pytest.mark.parametrize("failure", ["timeout", "exception", "cancelled"])
def test_effect_callback_changes_roll_back_before_terminal_record(
    config, db_clock, monkeypatch, failure
):
    enqueue(config)

    def handler(context, _payload):
        def apply(connection):
            connection.execute("INSERT INTO effect_probe VALUES('not-committed')")
            if failure == "timeout":
                db_clock.now += timedelta(seconds=30)
            elif failure == "exception":
                raise RuntimeError("synthetic-private-exception-must-not-be-published")
            else:
                connection.execute(
                    """UPDATE platform_jobs SET status='cancel_requested',
                    revision=revision+1,updated_at=?""",
                    (runtime._db_time(db_clock.now),),
                )

        context.commit_effect("slow.effect", {"verified": True}, apply)
        return "platform_job.probe_completed"

    set_handler(monkeypatch, handler)
    runtime.run_platform_job_once(config, worker_code="slow-effect-worker")
    assert row(config, "SELECT COUNT(*) FROM effect_probe")[0] == 0
    assert row(config, "SELECT COUNT(*) FROM platform_job_effects")[0] == 0
    if failure == "timeout":
        assert_terminal(config, "failed", "platform_job.timeout")
    elif failure == "exception":
        assert_terminal(config, "failed", "platform_job.execution_failed")
    else:
        # The callback's cancellation was uncommitted and was rolled back with its effect.
        assert_terminal(config, "failed", "platform_job.cancelled")


def test_cancel_heartbeat_finishes_cancel_without_waiting_for_handler(config, db_clock):
    enqueue(config)
    lease = runtime.claim_platform_job(config, worker_code="cancel-worker")
    runtime._start_platform_job(config, lease)
    execute(
        config,
        """UPDATE platform_jobs SET status='cancel_requested',
        revision=revision+1,updated_at=?""",
        (runtime._db_time(db_clock.now + timedelta(seconds=1)),),
    )
    assert not runtime.heartbeat_platform_job(config, lease)
    assert_terminal(config, "cancelled", "platform_job.cancelled")
    with pytest.raises(ApiError) as rejected:
        runtime.PlatformJobContext(config, lease).commit_effect("cancelled.effect", {})
    assert rejected.value.code == "platform_job.lease_lost"


def test_expired_attempt_is_failed_without_reclaim_when_deadline_reached(config, db_clock):
    enqueue(config)
    lease = runtime.claim_platform_job(config, worker_code="expired-worker")
    db_clock.now += timedelta(seconds=30)
    assert runtime.claim_platform_job(config, worker_code="replacement-worker") is None
    assert_terminal(config, "failed", "platform_job.timeout")
    assert not runtime.heartbeat_platform_job(config, lease)
    assert row(config, "SELECT COUNT(*) FROM platform_job_attempts")[0] == 1


def test_old_generation_cannot_renew_or_mutate_after_takeover(config, db_clock):
    enqueue(config)
    stale = runtime.claim_platform_job(config, worker_code="stale-worker")
    db_clock.now += timedelta(seconds=11)
    fresh = runtime.claim_platform_job(config, worker_code="replacement-worker")
    assert fresh.generation == stale.generation + 1
    assert not runtime.heartbeat_platform_job(config, stale)
    with pytest.raises(ApiError) as rejected:
        runtime.PlatformJobContext(config, stale).checkpoint({"late": True})
    assert rejected.value.code == "platform_job.lease_lost"
    assert runtime.heartbeat_platform_job(config, fresh)


def test_heartbeat_owns_connection_even_inside_handler_transaction(config, db_clock):
    enqueue(config)
    lease = runtime.claim_platform_job(config, worker_code="connection-worker")
    with database_request_scope(reuse_connections=True):
        connection = connect_database(config.database_url)
        connection.execute("BEGIN")
        assert runtime.heartbeat_platform_job(config, lease)
        assert connection.in_transaction
        connection.rollback()


def test_database_failure_does_not_extend_lease_or_escape_heartbeat(config, db_clock, monkeypatch):
    enqueue(config)
    lease = runtime.claim_platform_job(config, worker_code="failure-worker")
    expiry = row(config, "SELECT expires_at FROM platform_scheduler_leases")[0]

    def unavailable(_config):
        raise ApiError("platform_job.unavailable")

    monkeypatch.setattr(runtime, "_connect", unavailable)
    assert not runtime.heartbeat_platform_job(config, lease)
    assert row(config, "SELECT expires_at FROM platform_scheduler_leases")[0] == expiry


def test_database_clock_uses_postgres_wall_clock():
    queries = []

    def query(sql):
        queries.append(sql)
        return SimpleNamespace(fetchone=lambda: ["2026-09-10T00:00:00+00:00"])

    connection = SimpleNamespace(backend="postgresql", execute=query)
    assert runtime._database_now(connection) == datetime(2026, 9, 10, tzinfo=UTC)
    assert queries == ["SELECT clock_timestamp()"]


def test_submillisecond_clock_still_advances_persisted_revision_time():
    now = datetime(2026, 9, 10, microsecond=500, tzinfo=UTC)
    previous = "2026-09-10T00:00:00.000Z"
    assert runtime._db_time(runtime._changed_time(now, previous)) == "2026-09-10T00:00:00.001Z"


def test_lease_is_not_recovered_early_within_its_last_second(config, db_clock):
    db_clock.now += timedelta(milliseconds=800)
    enqueue(config)
    lease = runtime.claim_platform_job(config, worker_code="millisecond-worker")
    db_clock.now += timedelta(seconds=9, milliseconds=500)
    assert runtime.claim_platform_job(config, worker_code="early-recovery-worker") is None
    assert runtime.heartbeat_platform_job(config, lease)
    assert row(config, "SELECT COUNT(*) FROM platform_job_attempts")[0] == 1


@pytest.mark.parametrize("reason", ["timeout", "cancelled"])
def test_automatic_heartbeat_closes_execution_while_handler_is_waiting(
    config, db_clock, monkeypatch, reason
):
    enqueue(config)
    closed = threading.Event()
    original = runtime.heartbeat_platform_job

    def heartbeat(*args):
        result = original(*args)
        if not result:
            closed.set()
        return result

    monkeypatch.setattr(runtime, "heartbeat_platform_job", heartbeat)

    def handler(context, _payload):
        if reason == "timeout":
            db_clock.now += timedelta(seconds=30)
        else:
            execute(
                config,
                """UPDATE platform_jobs SET status='cancel_requested',
                revision=revision+1,updated_at=?""",
                (runtime._db_time(db_clock.now + timedelta(seconds=1)),),
            )
        assert closed.wait(4), "heartbeat did not close execution"
        assert_terminal(
            config, "failed" if reason == "timeout" else "cancelled", f"platform_job.{reason}"
        )
        with pytest.raises(ApiError) as rejected:
            context.commit_effect("late.effect", {})
        assert rejected.value.code == "platform_job.lease_lost"
        return "platform_job.probe_completed"

    set_handler(monkeypatch, handler)
    runtime.run_platform_job_once(config, worker_code="waiting-worker")
    assert closed.is_set()
    assert_terminal(
        config, "failed" if reason == "timeout" else "cancelled", f"platform_job.{reason}"
    )
    assert row(config, "SELECT COUNT(*) FROM platform_job_effects")[0] == 0
    assert not any(t.name.startswith("platform-job-heartbeat-") for t in threading.enumerate())


def test_lock_contention_has_bounded_heartbeat_and_keeps_handler_transaction(config, db_clock):
    enqueue(config)
    lease = runtime.claim_platform_job(config, worker_code="lock-worker")
    connection = connect_database(config.database_url)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO effect_probe VALUES('pending')")
        started = time.monotonic()
        assert not runtime.heartbeat_platform_job(config, lease)
        assert time.monotonic() - started < 3
        assert connection.in_transaction
        connection.rollback()
    finally:
        connection.close()
    assert row(config, "SELECT COUNT(*) FROM effect_probe")[0] == 0
    assert runtime.heartbeat_platform_job(config, lease)


def test_cancelled_expired_attempt_converges_without_stale_attempt(config, db_clock):
    enqueue(config)
    runtime.claim_platform_job(config, worker_code="cancel-expired-worker")
    execute(
        config,
        """UPDATE platform_jobs SET status='cancel_requested',
        revision=revision+1,updated_at=?""",
        (runtime._db_time(db_clock.now + timedelta(seconds=1)),),
    )
    db_clock.now += timedelta(seconds=30)
    assert runtime.claim_platform_job(config, worker_code="recovery-worker") is None
    assert_terminal(config, "cancelled", "platform_job.cancelled")
    assert row(config, "SELECT status FROM platform_job_attempts")[0] == "cancelled"


@pytest.mark.parametrize("change", ["registry", "generation", "expiry"])
def test_outer_scheduler_fence_still_controls_heartbeat_and_effects(config, db_clock, change):
    connection = connect_database(config.database_url)
    fence = runtime.PlatformJobOuterFence("test-scheduler", "all", "test-token", 1, 1)
    try:
        connection.executescript("""
            CREATE TABLE scheduler_registry_heads(head_key TEXT PRIMARY KEY, generation INTEGER);
            INSERT INTO scheduler_registry_heads VALUES('production',1);
            CREATE TABLE scheduler_leases(scheduler_code TEXT,partition_code TEXT,
                fencing_generation INTEGER,lease_token_hmac TEXT,lease_deadline_at TEXT);
        """)
        connection.execute(
            "INSERT INTO scheduler_leases VALUES(?,?,?,?,?)",
            (
                fence.scheduler_code,
                fence.partition_code,
                1,
                runtime._scheduler_lease_hmac(config, fence.lease_token),
                runtime._db_time(db_clock.now + timedelta(seconds=60)),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    enqueue(config)
    lease = runtime.claim_platform_job(config, worker_code="outer-worker", outer_fence=fence)
    assert runtime.heartbeat_platform_job(config, lease)
    if change == "registry":
        execute(config, "UPDATE scheduler_registry_heads SET generation=2")
    elif change == "generation":
        execute(config, "UPDATE scheduler_leases SET fencing_generation=2")
    else:
        execute(
            config,
            "UPDATE scheduler_leases SET lease_deadline_at=?",
            (runtime._db_time(db_clock.now),),
        )
    assert not runtime.heartbeat_platform_job(config, lease)
    with pytest.raises(ApiError) as rejected:
        runtime.PlatformJobContext(config, lease).commit_effect("fenced.effect", {})
    assert rejected.value.code == "platform_job.lease_lost"
    assert row(config, "SELECT COUNT(*) FROM platform_job_effects")[0] == 0
