"""Intermediate external phases commit atomically without ending the root attempt."""

from __future__ import annotations

from datetime import timedelta

import pytest
import test_platform_job_external_wait as base
from metahub_control.platform_jobs import external, runtime, service
from metahub_shared.runtime.errors import ApiError

config = base.config
db_clock = base.db_clock


def begin(config, monkeypatch):
    base.install(monkeypatch, lambda *_: None)
    base.begin(config)
    return base.claim(config)


def apply_phase(connection):
    connection.execute("INSERT INTO effect_probe VALUES('restored')")


def test_restore_phase_remains_running_and_replays_after_observer_crash(
    config, db_clock, monkeypatch
):
    first = begin(config, monkeypatch)
    original = tuple(base.row(config, "SELECT * FROM platform_job_external_waits"))
    assert first.commit_effect("dr.restore", {"proof": "private-proof-marker"}, apply_phase)
    assert not first.resolved
    assert tuple(base.row(config, "SELECT * FROM platform_job_external_waits")) == original
    assert base.row(config, "SELECT status FROM platform_job_attempts")[0] == "running"
    db_clock.now += timedelta(seconds=external.RESUMPTION_SECONDS)
    second = base.claim(config, "replacement-observer")
    assert second.lease.resume_generation == first.lease.resume_generation + 1
    assert not second.commit_effect(
        "dr.restore", {"proof": "private-proof-marker"}, lambda _: pytest.fail("Restore repeated")
    )
    assert second.commit_effect("dr.route", {"generation": 2})
    assert base.row(config, "SELECT attempt_count FROM platform_jobs")[0] == 1
    assert base.row(config, "SELECT COUNT(*) FROM effect_probe")[0] == 2
    assert base.row(config, "SELECT COUNT(*) FROM platform_job_effects")[0] == 2
    with pytest.raises(ApiError) as rejected:
        first.commit_effect("dr.restore", {"proof": "private-proof-marker"})
    assert rejected.value.code == "platform_job.lease_lost"
    assert base.finish(second) == "platform_job.probe_completed"
    assert base.row(config, "SELECT status FROM platform_jobs")[0] == "succeeded"
    assert base.row(config, "SELECT COUNT(*) FROM platform_job_effects")[0] == 3
    recorded = str(tuple(base.row(config, "SELECT * FROM platform_job_effects")))
    assert "private-proof-marker" not in recorded


def test_phase_key_rejects_changed_result_without_mutation(config, db_clock, monkeypatch):
    context = begin(config, monkeypatch)
    assert context.commit_effect("dr.restore", {"proof": "first"}, apply_phase)
    with pytest.raises(ApiError) as rejected:
        context.commit_effect("dr.restore", {"proof": "changed"}, apply_phase)
    assert rejected.value.code == "platform_job.effect_conflict"
    assert base.row(config, "SELECT COUNT(*) FROM effect_probe")[0] == 2
    assert base.row(config, "SELECT COUNT(*) FROM platform_job_effects")[0] == 1
    assert base.row(config, "SELECT status FROM platform_jobs")[0] == "running"


@pytest.mark.parametrize("cause", ["exception", "crash", "lease_expired", "deadline_crossed"])
def test_phase_failure_rolls_back_business_and_effect(config, db_clock, monkeypatch, cause):
    context = begin(config, monkeypatch)
    if cause == "deadline_crossed":
        context.defer()
        db_clock.now += timedelta(seconds=3 * 86400 + 119)
        context = base.claim(config)

    def apply(connection):
        apply_phase(connection)
        if cause == "exception":
            raise ValueError("private-phase-detail")
        if cause == "crash":
            raise SystemExit("simulated-worker-crash")
        db_clock.now += timedelta(seconds=45 if cause == "lease_expired" else 2)

    with pytest.raises((ApiError, ValueError, SystemExit)):
        context.commit_effect("dr.restore", {"proof": "bound"}, apply)
    assert base.row(config, "SELECT COUNT(*) FROM effect_probe")[0] == 1
    assert base.row(config, "SELECT COUNT(*) FROM platform_job_effects")[0] == 0
    assert base.row(config, "SELECT status FROM platform_jobs")[0] == "running"
    assert not context.resolved


@pytest.mark.parametrize("reason", ["cancel", "deadline"])
def test_stop_rejects_new_phase_but_keeps_external_execution_owned(
    config, db_clock, monkeypatch, reason
):
    begin(config, monkeypatch).defer()
    if reason == "cancel":
        base.cancel(config, db_clock)
        db_clock.now += timedelta(seconds=external.POLL_SECONDS)
    else:
        db_clock.now += timedelta(days=4)
    context = base.claim(config)
    with pytest.raises(ApiError) as rejected:
        context.commit_effect("dr.route", {}, lambda _: pytest.fail("Stopped phase applied"))
    assert rejected.value.code == (
        "platform_job.cancelled" if reason == "cancel" else "platform_job.timeout"
    )
    assert base.row(config, "SELECT COUNT(*) FROM platform_job_effects")[0] == 0
    assert base.row(config, "SELECT status FROM platform_job_attempts")[0] == "running"
    context.defer()
    assert base.row(config, "SELECT status FROM platform_job_external_waits")[0] == "waiting"


def test_phase_rechecks_outer_fence_after_business_mutation(config, db_clock, monkeypatch):
    base.install(monkeypatch, lambda *_: None)
    base.begin(config)
    active = [True]

    def outer(*args, **kwargs):
        if not active[0]:
            raise ApiError("platform_job.lease_lost")

    monkeypatch.setattr(runtime, "_live_outer_fence", outer)
    context = external.ExternalJobContext(
        config,
        external.claim_external_job(
            config,
            worker_code="phase-fence-test",
            outer_fence=runtime.PlatformJobOuterFence("scheduler", "partition", "token", 1, 1),
        ),
    )

    def apply(connection):
        apply_phase(connection)
        active[0] = False

    with pytest.raises(ApiError):
        context.commit_effect("dr.restore", {}, apply)
    assert base.row(config, "SELECT COUNT(*) FROM effect_probe")[0] == 1
    assert base.row(config, "SELECT COUNT(*) FROM platform_job_effects")[0] == 0


@pytest.mark.parametrize("external_phase", [False, True])
def test_effect_replay_rechecks_deadline_before_commit(
    config, db_clock, monkeypatch, external_phase
):
    if external_phase:
        context = begin(config, monkeypatch)
    else:
        base.enqueue(config)
        lease = runtime.claim_platform_job(config, worker_code="ordinary-phase-test")
        assert runtime._start_platform_job(config, lease)
        context = runtime.PlatformJobContext(config, lease)
    assert context.commit_effect("dr.restore", {"proof": "same"}, apply_phase)
    real = runtime._commit_effect_in_transaction

    def slow_replay(*args, **kwargs):
        committed = real(*args, **kwargs)
        assert not committed
        db_clock.now += timedelta(seconds=46)
        return committed

    monkeypatch.setattr(runtime, "_commit_effect_in_transaction", slow_replay)
    expected_error = ApiError if external_phase else runtime.PlatformJobFailure
    with pytest.raises(expected_error) as rejected:
        context.commit_effect("dr.restore", {"proof": "same"}, apply_phase)
    assert rejected.value.code == (
        "platform_job.lease_lost" if external_phase else "platform_job.timeout"
    )
    assert base.row(config, "SELECT COUNT(*) FROM effect_probe")[0] == (2 if external_phase else 1)
    assert base.row(config, "SELECT COUNT(*) FROM platform_job_effects")[0] == 1


def test_phase_exception_is_sanitized_by_durable_observer(config, db_clock, monkeypatch):
    def resume(context, _value):
        def fail(connection):
            apply_phase(connection)
            raise ValueError("private-phase-sensitive-value")

        context.commit_effect("dr.restore", {"secret": "private-proof-value"}, fail)

    base.install(monkeypatch, resume)
    base.begin(config)
    assert external.run_external_job_once(config, worker_code="phase-error-observer")
    assert base.row(config, "SELECT COUNT(*) FROM effect_probe")[0] == 1
    assert base.row(config, "SELECT COUNT(*) FROM platform_job_effects")[0] == 0
    wait = base.row(config, "SELECT * FROM platform_job_external_waits")
    assert wait["safe_outcome_code"] == "platform_job.external_unavailable"
    assert "private-" not in str(tuple(wait))
    assert base.row(config, "SELECT attempt_count FROM platform_jobs")[0] == 1
    connection = runtime._connect(config)
    try:
        public = service._external_wait_info(connection, wait["platform_job_id"]).model_dump_json()
    finally:
        connection.close()
    assert "platform_job.external_unavailable" in public
    for private in ("private-", "envelope", "lease_token", "worker_code"):
        assert private not in public
