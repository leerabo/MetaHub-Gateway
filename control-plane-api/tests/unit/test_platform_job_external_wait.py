"""External work preserves root ownership through waits, cancellation and crashes."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta

import pytest
import test_platform_job_execution_deadline as base
from metahub_control.platform_jobs import catalog, external, runtime, service
from metahub_shared.db import connect_database
from metahub_shared.runtime.errors import ApiError

ROOT = base.ROOT
config = base.config
db_clock = base.db_clock
enqueue = base.enqueue
execute = base.execute
row = base.row


def install(monkeypatch, resume, start=None):
    def default_start(context, _payload):
        context.defer_external(
            {"marker": "encrypted-continuation-only"},
            timeout_seconds=3 * 86400 + 120,
            apply=lambda connection: connection.execute(
                "INSERT INTO effect_probe VALUES('started')"
            ),
        )

    definition = catalog.handler_definition("platform.runtime.probe")
    monkeypatch.setitem(
        catalog._HANDLERS,
        "platform.runtime.probe",
        replace(
            definition,
            handler=start or default_start,
            external_handler=resume,
        ),
    )


def begin(config):
    job_id = enqueue(config)
    assert runtime.run_platform_job_once(config, worker_code="external-start")
    assert tuple(row(config, "SELECT status,attempt_count FROM platform_jobs")) == ("running", 1)
    assert row(config, "SELECT status FROM platform_scheduler_leases")[0] == "released"
    return job_id


def claim(config, worker="external-observer"):
    lease = external.claim_external_job(config, worker_code=worker)
    assert lease is not None
    return external.ExternalJobContext(config, lease)


def cancel(config, db_clock):
    execute(
        config,
        "UPDATE platform_jobs SET status='cancel_requested',revision=revision+1,updated_at=?",
        (runtime._db_time(db_clock.now + timedelta(microseconds=10)),),
    )


def finish(context, apply=None):
    return context.finish(
        status="succeeded",
        outcome="platform_job.probe_completed",
        value={"result": "fixed"},
        stop_confirmed=True,
        apply=apply,
    )


def test_pending_over_twenty_observations_and_one_day_never_retries(config, db_clock, monkeypatch):
    calls = []

    def resume(context, value):
        assert value == {"marker": "encrypted-continuation-only"}
        calls.append(context.lease.resume_generation)
        return context.defer(acknowledged=True) if len(calls) <= 25 else finish(context)

    install(monkeypatch, resume)
    original_job = begin(config)
    for _ in range(25):
        assert runtime.run_platform_job_once(config, worker_code="external-observer")
        assert tuple(
            row(config, "SELECT platform_job_id,status,attempt_count FROM platform_jobs")
        ) == (
            original_job,
            "running",
            1,
        )
        assert not runtime.run_platform_job_once(config, worker_code="not-yet-due")
        db_clock.now += timedelta(seconds=external.POLL_SECONDS)
    db_clock.now += timedelta(days=1)
    assert runtime.run_platform_job_once(config, worker_code="resumed-after-restart")
    assert row(config, "SELECT status FROM platform_jobs")[0] == "succeeded"
    assert row(config, "SELECT COUNT(*) FROM platform_job_attempts")[0] == 1
    assert row(config, "SELECT COUNT(*) FROM platform_job_effects")[0] == 1
    assert row(config, "SELECT COUNT(*) FROM effect_probe")[0] == 1
    assert row(config, "SELECT COUNT(*) FROM platform_job_resumption_leases")[0] == 26
    assert row(config, "SELECT COUNT(*) FROM pragma_foreign_key_check")[0] == 0


def test_defer_start_and_registration_rollback_together(config, db_clock, monkeypatch):
    def start(context, _payload):
        def apply(connection):
            connection.execute("INSERT INTO effect_probe VALUES('rollback')")
            db_clock.now += timedelta(seconds=31)

        context.defer_external({}, timeout_seconds=100, apply=apply)

    install(monkeypatch, lambda *_: None, start)
    enqueue(config)
    runtime.run_platform_job_once(config, worker_code="expired-start")
    assert row(config, "SELECT status FROM platform_jobs")[0] == "failed"
    assert row(config, "SELECT COUNT(*) FROM effect_probe")[0] == 0
    assert row(config, "SELECT COUNT(*) FROM platform_job_external_waits")[0] == 0


def test_handoff_invalidates_original_capabilities(config, db_clock, monkeypatch):
    install(monkeypatch, lambda *_: None)
    enqueue(config)
    lease = runtime.claim_platform_job(config, worker_code="original-worker")
    assert runtime._start_platform_job(config, lease)
    context = runtime.PlatformJobContext(config, lease)
    with pytest.raises(runtime.PlatformJobDeferred):
        context.defer_external({}, timeout_seconds=100)
    assert not runtime.heartbeat_platform_job(config, lease)
    with pytest.raises(ApiError):
        context.commit_effect("late.original", {})
    runtime._fail_platform_job(config, lease, "platform_job.execution_failed", retryable=False)
    assert row(config, "SELECT status FROM platform_jobs")[0] == "running"


def test_reclaim_fences_old_observer_and_preserves_attempt(config, db_clock, monkeypatch):
    install(monkeypatch, lambda *_: None)
    begin(config)
    old = claim(config, "old-process")
    assert external.claim_external_job(config, worker_code="competitor") is None
    db_clock.now += timedelta(seconds=external.RESUMPTION_SECONDS)
    current = claim(config, "replacement-process")
    assert current.lease.resume_generation == old.lease.resume_generation + 1
    with pytest.raises(ApiError) as rejected:
        finish(old, lambda conn, *_: conn.execute("INSERT INTO effect_probe VALUES('stale')"))
    assert rejected.value.code == "platform_job.lease_lost"
    finish(current)
    assert row(config, "SELECT COUNT(*) FROM effect_probe")[0] == 1
    assert row(config, "SELECT COUNT(*) FROM platform_job_attempts")[0] == 1
    assert (
        row(config, "SELECT status FROM platform_job_resumption_leases ORDER BY resume_generation")[
            0
        ]
        == "expired"
    )


@pytest.mark.parametrize("late_cancel", [False, True])
def test_cancel_waits_for_confirmed_stop_and_wins_completion(
    config, db_clock, monkeypatch, late_cancel
):
    install(monkeypatch, lambda *_: None)
    begin(config)
    if not late_cancel:
        cancel(config, db_clock)
    observer = claim(config)
    if late_cancel:
        assert observer.stop_reason() is None
        cancel(config, db_clock)
    assert observer.stop_reason() == "platform_job.cancelled"
    with pytest.raises(ApiError):
        observer.finish(
            status="cancelled", outcome="platform_job.cancelled", value={}, stop_confirmed=False
        )
    observer.defer(outcome="capacity.runner_unavailable")
    assert row(config, "SELECT status FROM platform_jobs")[0] == "cancel_requested"
    db_clock.now += timedelta(seconds=30)
    observed = []
    finish(claim(config), lambda _conn, status, outcome: observed.append((status, outcome)))
    assert observed == [("cancelled", "platform_job.cancelled")]
    assert row(config, "SELECT status FROM platform_jobs")[0] == "cancelled"
    assert row(config, "SELECT status FROM platform_job_attempts")[0] == "cancelled"


def test_deadline_requests_stop_but_does_not_abandon_external_work(config, db_clock, monkeypatch):
    install(monkeypatch, lambda *_: None)
    begin(config)
    db_clock.now += timedelta(days=4)
    observer = claim(config)
    assert observer.stop_reason() == "platform_job.timeout"
    observer.defer(outcome="capacity.runner_unavailable")
    assert row(config, "SELECT status FROM platform_jobs")[0] == "running"
    db_clock.now += timedelta(seconds=30)
    finish(claim(config))
    assert tuple(row(config, "SELECT status,safe_outcome_code FROM platform_jobs")) == (
        "failed",
        "platform_job.timeout",
    )


@pytest.mark.parametrize("cause", ["exception", "lease_expired", "deadline_crossed"])
def test_effect_and_terminal_state_are_atomic(config, db_clock, monkeypatch, cause):
    install(monkeypatch, lambda *_: None)
    begin(config)
    if cause == "deadline_crossed":
        db_clock.now += timedelta(seconds=3 * 86400 + 119)
    observer = claim(config)

    def apply(connection, *_):
        connection.execute("INSERT INTO effect_probe VALUES('uncommitted')")
        if cause == "exception":
            raise ValueError("private-detail-never-published")
        db_clock.now += timedelta(seconds=45 if cause == "lease_expired" else 2)

    with pytest.raises((ApiError, ValueError)):
        finish(observer, apply)
    assert row(config, "SELECT COUNT(*) FROM effect_probe")[0] == 1
    assert row(config, "SELECT COUNT(*) FROM platform_job_effects")[0] == 0
    assert row(config, "SELECT status FROM platform_jobs")[0] == "running"


def test_unknown_failure_is_durable_sanitized_and_does_not_spend_retries(
    config, db_clock, monkeypatch
):
    def broken(*_):
        raise RuntimeError("private-secret-do-not-emit")

    install(monkeypatch, broken)
    job_id = begin(config)
    for _ in range(22):
        assert runtime.run_platform_job_once(config, worker_code="unavailable-observer")
        db_clock.now += timedelta(seconds=30)
    assert row(config, "SELECT attempt_count FROM platform_jobs")[0] == 1
    connection = connect_database(config.database_url)
    try:
        public = service._external_wait_info(connection, job_id).model_dump_json()
    finally:
        connection.close()
    assert "platform_job.external_unavailable" in public
    assert '"failure_count":22' in public
    for secret in (
        "private-secret",
        "encrypted-continuation-only",
        "lease_token",
        "worker_code",
        "envelope",
    ):
        assert secret not in public


def test_concurrent_resumption_has_one_owner(config, db_clock, monkeypatch):
    install(monkeypatch, lambda *_: None)
    begin(config)
    barrier = threading.Barrier(2)

    def compete(worker):
        barrier.wait(timeout=5)
        return external.claim_external_job(config, worker_code=worker)

    with ThreadPoolExecutor(max_workers=2) as workers:
        leases = list(workers.map(compete, ["observer-one", "observer-two"]))
    assert sum(value is not None for value in leases) == 1


def test_outer_fence_is_rechecked_before_terminal_commit(config, db_clock, monkeypatch):
    install(monkeypatch, lambda *_: None)
    begin(config)
    valid = [True]

    def outer(*_, **__):
        if not valid[0]:
            raise ApiError("platform_job.lease_lost")

    monkeypatch.setattr(runtime, "_live_outer_fence", outer)
    fence = runtime.PlatformJobOuterFence("scheduler", "partition", "token", 1, 1)
    observer = external.ExternalJobContext(
        config,
        external.claim_external_job(
            config,
            worker_code="fenced-observer",
            outer_fence=fence,
        ),
    )
    with pytest.raises(ApiError):
        finish(observer, lambda *_: valid.__setitem__(0, False))
    assert row(config, "SELECT status FROM platform_jobs")[0] == "running"


def test_database_rejects_ordinary_terminal_and_fact_deletion(config, db_clock, monkeypatch):
    install(monkeypatch, lambda *_: None)
    begin(config)
    with pytest.raises(Exception, match="platform_external_stop_unconfirmed"):
        execute(
            config,
            "UPDATE platform_jobs SET status='failed',completed_at=?,"
            "revision=revision+1,updated_at=?",
            (runtime._db_time(db_clock.now), runtime._db_time(db_clock.now + timedelta(seconds=1))),
        )
    with pytest.raises(Exception, match="platform_external_fact_immutable"):
        execute(config, "DELETE FROM platform_job_external_waits")
    database = config.database_url.removeprefix("sqlite:///")
    with sqlite3.connect(database) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.executescript(
            (ROOT / "infra/migrations/0151_platform_job_external_waits.down.sql").read_text()
        )


def test_empty_external_schema_can_be_rolled_back(config):
    database = config.database_url.removeprefix("sqlite:///")
    with sqlite3.connect(database) as connection:
        connection.executescript(
            (ROOT / "infra/migrations/0151_platform_job_external_waits.down.sql").read_text()
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_schema WHERE name='platform_job_external_waits'"
            ).fetchone()[0]
            == 0
        )


def test_shutdown_releases_external_owner_without_ending_root(config, db_clock, monkeypatch):
    install(monkeypatch, lambda *_: None)
    begin(config)
    observer = claim(config, "stopping-process")
    assert runtime.fence_platform_worker(config, worker_code="stopping-process") == 1
    assert row(config, "SELECT status FROM platform_jobs")[0] == "running"
    with pytest.raises(ApiError):
        finish(observer)
    finish(claim(config, "replacement-process"))


def test_waiting_does_not_take_slot_but_observation_does(config, db_clock, monkeypatch):
    install(monkeypatch, lambda *_: None)
    begin(config)
    connection = connect_database(config.database_url)
    try:
        assert external._active_count(connection, "standard", db_clock.now) == 0
        observer = claim(config)
        assert external._active_count(connection, "standard", db_clock.now) == 1
        observer.defer()
        assert external._active_count(connection, "standard", db_clock.now) == 0
    finally:
        connection.close()


def test_queue_admission_counts_regular_and_external_leases(config, db_clock, monkeypatch):
    install(monkeypatch, lambda *_: None)
    begin(config)
    for index in range(8):
        service.enqueue_platform_job(
            config,
            type_code="platform.runtime.probe",
            payload={"marker": "queue-limit"},
            priority=50,
            source_code="platform.runtime.test",
            idempotency_key=f"queue-limit-test-{index:04d}",
            request_id="request",
            trace_id="trace",
        )
        assert runtime.claim_platform_job(config, worker_code=f"regular-worker-{index}") is not None
    assert external.claim_external_job(config, worker_code="queue-full") is None
    assert runtime.fence_platform_worker(config, worker_code="regular-worker-0") == 1
    observer = claim(config)
    assert runtime.claim_platform_job(config, worker_code="external-counts-too") is None
    finish(observer)
    assert runtime.claim_platform_job(config, worker_code="capacity-freed") is not None


def test_disabling_type_does_not_abandon_existing_external_work(config, db_clock, monkeypatch):
    install(monkeypatch, lambda *_: None)
    begin(config)
    execute(
        config,
        "UPDATE platform_job_types SET status='disabled',revision=revision+1,updated_at=? "
        "WHERE type_code='platform.runtime.probe'",
        (runtime._db_time(db_clock.now + timedelta(seconds=1)),),
    )
    finish(claim(config))
    assert row(config, "SELECT status FROM platform_jobs")[0] == "succeeded"


def test_slow_decryption_cannot_invoke_handler_after_resumption_expiry(
    config, db_clock, monkeypatch
):
    calls = []

    def resume(context, _value):
        calls.append(True)
        return finish(context)

    install(monkeypatch, resume)
    begin(config)
    original = external.decrypt_job_value

    def slow(*args, **kwargs):
        value = original(*args, **kwargs)
        db_clock.now += timedelta(seconds=46)
        return value

    monkeypatch.setattr(external, "decrypt_job_value", slow)
    assert runtime.run_platform_job_once(config, worker_code="slow-key-service")
    assert calls == []
    assert row(config, "SELECT status FROM platform_jobs")[0] == "running"
    monkeypatch.setattr(external, "decrypt_job_value", original)
    assert runtime.run_platform_job_once(config, worker_code="recovered-key-service")
    assert calls == [True]
    assert row(config, "SELECT attempt_count FROM platform_jobs")[0] == 1
