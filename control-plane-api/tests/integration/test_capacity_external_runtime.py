"""Capacity domain integration on the full schema; fixtures are not performance evidence."""

from contextlib import closing
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import test_prd113_capacity_management as base
from metahub_capacity_runner.contracts import (
    CapacityExecutionInfo,
    CapacityRunnerResult,
    execution_digest,
    result_digest,
)
from metahub_capacity_runner.engine import simulate_capacity_run
from metahub_capacity_runner.store import RunnerStore
from metahub_control.capacity_management import runtime as capacity
from metahub_control.capacity_management import service
from metahub_control.capacity_management.models import ChaosCommandRequest, LoadTestRequest
from metahub_control.platform_jobs import runtime as jobs
from metahub_control.platform_jobs import service as job_service
from metahub_shared.db import connect_database
from metahub_shared.runtime.errors import ApiError

base_database = base.base_database
runtime = base.runtime


@pytest.fixture
def setup(runtime, monkeypatch):
    clock = SimpleNamespace(now=datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1))
    monkeypatch.setattr(jobs, "_database_now", lambda _: clock.now)
    monkeypatch.setattr(job_service, "_utcnow", lambda: clock.now)
    monkeypatch.setattr(service, "_database_now", lambda _: clock.now)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.now

    monkeypatch.setattr(capacity, "datetime", Clock)
    monkeypatch.setattr(
        service, "authenticate_platform_session_in_transaction", lambda *a, **k: base.OPS
    )
    monkeypatch.setattr(
        capacity, "capacity_execution", lambda *a, **k: pytest.fail("unexpected network")
    )
    runtime.capacity_runner_mode = "isolated_http"
    return SimpleNamespace(config=runtime, clock=clock)


def row(setup, sql, params=()):
    with closing(connect_database(setup.config.database_url)) as connection:
        return connection.execute(sql, params).fetchone()


def load(setup, *, soak=False):
    result = service.create_load_test(
        setup.config,
        session_token=base.PLATFORM_TOKEN,
        csrf_token=base.PLATFORM_CSRF,
        payload=LoadTestRequest(
            scenario_code="gateway-soak" if soak else "gateway-release",
            environment_class="staging",
            region_code="cn-north-1",
            synthetic_tenant_count=100,
            target_rps=1600,
            duration_seconds=259200 if soak else 60,
            confirmed=True,
        ),
        idempotency_key="capacity-external-load-0001",
        request_id="request-external",
        trace_id="trace-external",
    )
    return result.run.load_test_run_id


def chaos(setup):
    result = service.command_chaos_experiment(
        setup.config,
        session_token=base.PLATFORM_TOKEN,
        csrf_token=base.PLATFORM_CSRF,
        expected_revision=None,
        payload=ChaosCommandRequest(
            action="create",
            fault_code="redis-unavailable",
            service_code="control",
            environment_class="staging",
            region_code="cn-north-1",
            blast_tenant_count=5,
            duration_seconds=30,
            objective_p95_ms=500,
            objective_error_rate_bps=100,
            stop_error_rate_bps=400,
            reason_code="resilience_validation",
            confirmed=True,
        ),
        idempotency_key="capacity-external-chaos-0001",
        request_id="request-external",
        trace_id="trace-external",
    )
    return result.experiment.chaos_experiment_id


def run(setup, *, advance=0):
    setup.clock.now += timedelta(seconds=advance)
    assert jobs.run_platform_job_once(setup.config, worker_code="capacity-integration")


def cancel(setup):
    with closing(connect_database(setup.config.database_url)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        job = connection.execute(
            "SELECT * FROM platform_jobs WHERE type_code='capacity.run'"
        ).fetchone()
        queued = job["status"] == "queued"
        connection.execute(
            "UPDATE platform_jobs SET status=?,revision=revision+1,updated_at=?,"
            "completed_at=?,safe_outcome_code=? WHERE platform_job_id=?",
            (
                "cancelled" if queued else "cancel_requested",
                jobs._db_time(jobs._changed_time(setup.clock.now, job["updated_at"])),
                jobs._db_time(setup.clock.now) if queued else None,
                "platform_job.cancelled" if queued else None,
                job["platform_job_id"],
            ),
        )
        connection.commit()


def observation(claims, state="running"):
    return CapacityExecutionInfo(
        object_id=claims.object_id,
        execution_sha256=execution_digest(claims),
        state=state,
        safe_outcome_code=f"runner.{state}",
        sample_count=0,
        elapsed_ms=0,
        stop_scope=(
            "runner_requests_and_fault"
            if claims.operation == "chaos" and state not in {"queued", "running", "stopping"}
            else "runner_requests"
        ),
    )


def result(claims):
    value = simulate_capacity_run(claims).model_dump(mode="json")
    value["evidence_kind"] = "measured"
    value["result_sha256"] = result_digest(value)
    return CapacityRunnerResult.model_validate(value)


def assert_root(setup, status):
    assert tuple(row(setup, "SELECT status,attempt_count FROM platform_jobs")) == (status, 1)
    assert row(setup, "SELECT COUNT(*) FROM platform_job_attempts")[0] == 1


def test_atomic_start_precedes_network_and_pending_never_spends_attempts(setup, monkeypatch):
    object_id = load(setup)
    run(setup)
    assert_root(setup, "running")
    assert row(setup, "SELECT status FROM load_test_runs")[0] == "running"
    assert row(setup, "SELECT status FROM platform_scheduler_leases")[0] == "released"
    calls = []

    def exchange(config, claims, *, action):
        assert claims.object_id == object_id
        calls.append((action, claims))
        return observation(claims) if len(calls) <= 25 else result(claims)

    monkeypatch.setattr(capacity, "capacity_execution", exchange)
    for _ in range(25):
        run(setup, advance=5)
        assert_root(setup, "running")
    run(setup, advance=5)
    assert_root(setup, "succeeded")
    assert [action for action, _ in calls] == ["submit"] + ["status"] * 25
    assert len({execution_digest(claims) for _, claims in calls}) == 1
    assert row(setup, "SELECT COUNT(*) FROM performance_baselines")[0] == 1
    assert row(setup, "SELECT COUNT(*) FROM pragma_foreign_key_check")[0] == 0


def test_uncertain_delivery_replays_original_permit_then_renews_only_observation(
    setup, monkeypatch
):
    load(setup)
    run(setup)
    calls = []

    def exchange(config, claims, *, action):
        calls.append((action, claims))
        if len(calls) == 1:
            raise ApiError("capacity.runner_unavailable")
        return observation(claims)

    monkeypatch.setattr(capacity, "capacity_execution", exchange)
    for _ in range(3):
        run(setup, advance=5)
    assert [action for action, _ in calls] == ["submit", "submit", "status"]
    assert calls[0][1] == calls[1][1]
    assert calls[2][1].issued_at > calls[1][1].issued_at
    assert_root(setup, "running")


@pytest.mark.parametrize("during_submit", [False, True])
def test_cancel_before_or_during_submission_requires_confirmed_stop(
    setup, monkeypatch, during_submit
):
    load(setup)
    run(setup)
    if not during_submit:
        cancel(setup)
    calls = []

    def exchange(config, claims, *, action):
        calls.append(action)
        if action == "submit":
            cancel(setup)
            return observation(claims)
        return observation(claims, "stopping" if calls.count("cancel") == 1 else "cancelled")

    monkeypatch.setattr(capacity, "capacity_execution", exchange)
    if during_submit:
        run(setup)
    run(setup, advance=5)
    assert_root(setup, "cancel_requested")
    assert row(setup, "SELECT status,completed_at FROM load_test_runs")[1] is None
    run(setup, advance=5)
    assert_root(setup, "cancelled")
    assert row(setup, "SELECT status FROM load_test_runs")[0] == "stopped"
    assert row(setup, "SELECT COUNT(*) FROM performance_baselines")[0] == 0


def test_cancel_racing_success_cannot_create_baseline(setup, monkeypatch):
    load(setup)
    run(setup)

    def exchange(config, claims, *, action):
        cancel(setup)
        return result(claims)

    monkeypatch.setattr(capacity, "capacity_execution", exchange)
    run(setup)
    assert_root(setup, "cancelled")
    assert row(setup, "SELECT status FROM load_test_runs")[0] == "stopped"
    assert row(setup, "SELECT COUNT(*) FROM performance_baselines")[0] == 0


@pytest.mark.parametrize(
    "creator,table,terminal",
    [
        (load, "load_test_runs", "stopped"),
        (chaos, "chaos_experiments", "killed"),
    ],
)
def test_queued_cancellation_also_finishes_domain(setup, creator, table, terminal):
    creator(setup)
    cancel(setup)
    assert row(setup, f"SELECT status FROM {table}")[0] == terminal
    assert row(setup, "SELECT COUNT(*) FROM platform_job_external_waits")[0] == 0


def test_expired_unacknowledged_submission_is_cancelled_without_renewed_submit(setup, monkeypatch):
    load(setup)
    run(setup)
    calls = []

    def exchange(config, claims, *, action):
        calls.append(action)
        return observation(claims, "cancelled")

    monkeypatch.setattr(capacity, "capacity_execution", exchange)
    run(setup, advance=121)
    assert calls == ["cancel"]
    assert_root(setup, "failed")
    assert (
        row(setup, "SELECT safe_outcome_code FROM load_test_runs")[0]
        == "capacity.submission_expired"
    )


def test_deadline_and_unavailable_runner_do_not_claim_stop(setup, monkeypatch):
    load(setup)
    run(setup)
    calls = []

    def exchange(config, claims, *, action):
        calls.append(action)
        if len(calls) == 2:
            raise ApiError("capacity.runner_unavailable")
        return observation(claims, "running" if action == "submit" else "cancelled")

    monkeypatch.setattr(capacity, "capacity_execution", exchange)
    run(setup)
    run(setup, advance=481)
    assert_root(setup, "running")
    assert row(setup, "SELECT completed_at FROM load_test_runs")[0] is None
    run(setup, advance=31)
    assert calls == ["submit", "cancel", "cancel"]
    assert_root(setup, "failed")
    assert row(setup, "SELECT safe_outcome_code FROM load_test_runs")[0] == "platform_job.timeout"


def test_kill_keeps_domain_pending_until_runner_confirms(setup, monkeypatch):
    object_id = chaos(setup)
    run(setup)
    stopped = service.command_chaos_experiment(
        setup.config,
        session_token=base.PLATFORM_TOKEN,
        csrf_token=base.PLATFORM_CSRF,
        expected_revision=2,
        payload=ChaosCommandRequest(
            action="kill",
            chaos_experiment_id=object_id,
            reason_code="operator_containment",
            confirmed=True,
        ),
        idempotency_key="capacity-external-kill-0001",
        request_id="request-stop",
        trace_id="trace-stop",
    )
    assert stopped.experiment.status == "running"
    assert stopped.experiment.completed_at is None
    assert_root(setup, "cancel_requested")

    def exchange(config, claims, *, action):
        assert action == "cancel"
        assert claims.kill_generation == 0
        return observation(claims, "cancelled")

    monkeypatch.setattr(capacity, "capacity_execution", exchange)
    run(setup)
    assert_root(setup, "cancelled")
    assert tuple(
        row(setup, "SELECT status,kill_generation,objective_met FROM chaos_experiments")
    ) == (
        "killed",
        1,
        0,
    )


def test_expired_observer_response_cannot_commit_domain_or_baseline(setup, monkeypatch):
    load(setup)
    run(setup)

    def exchange(config, claims, *, action):
        setup.clock.now += timedelta(seconds=46)
        return result(claims)

    monkeypatch.setattr(capacity, "capacity_execution", exchange)
    run(setup)
    assert_root(setup, "running")
    assert row(setup, "SELECT COUNT(*) FROM performance_baselines")[0] == 0
    monkeypatch.setattr(capacity, "capacity_execution", lambda config, claims, **kw: result(claims))
    run(setup)
    assert_root(setup, "succeeded")


def test_domain_and_root_completion_roll_back_together(setup, monkeypatch):
    load(setup)
    run(setup)
    monkeypatch.setattr(capacity, "capacity_execution", lambda config, claims, **kw: result(claims))
    original = capacity.complete_load_test_in_transaction

    def fail(connection, **kwargs):
        original(connection, **kwargs)
        raise RuntimeError("private-exception-not-a-public-outcome")

    monkeypatch.setattr(capacity, "complete_load_test_in_transaction", fail)
    run(setup)
    assert_root(setup, "running")
    assert row(setup, "SELECT status FROM load_test_runs")[0] == "running"
    assert row(setup, "SELECT COUNT(*) FROM performance_baselines")[0] == 0
    assert (
        row(setup, "SELECT safe_outcome_code FROM platform_job_external_waits")[0]
        == "platform_job.external_unavailable"
    )
    monkeypatch.setattr(capacity, "complete_load_test_in_transaction", original)
    run(setup, advance=5)
    assert_root(setup, "succeeded")


def test_start_expiry_rolls_back_wait_and_closes_unstarted_domain(setup, monkeypatch):
    load(setup)
    original = capacity.start_load_test_in_transaction

    def delayed(connection, **kwargs):
        value = original(connection, **kwargs)
        setup.clock.now += timedelta(days=1)
        return value

    monkeypatch.setattr(capacity, "start_load_test_in_transaction", delayed)
    run(setup)
    assert row(setup, "SELECT COUNT(*) FROM platform_job_external_waits")[0] == 0
    assert row(setup, "SELECT status FROM load_test_runs")[0] == "failed"
    assert_root(setup, "failed")


def test_long_soak_includes_start_window_without_truncating_requested_duration(setup, monkeypatch):
    load(setup, soak=True)
    run(setup)
    calls = []

    def exchange(config, claims, *, action):
        calls.append((action, execution_digest(claims)))
        return observation(claims)

    monkeypatch.setattr(capacity, "capacity_execution", exchange)
    run(setup)
    run(setup, advance=3500)
    run(setup, advance=3 * 86400)
    assert_root(setup, "running")
    assert [action for action, _ in calls] == ["submit", "status", "status"]
    assert len({digest for _, digest in calls}) == 1
    assert row(setup, "SELECT COUNT(*) FROM platform_job_effects")[0] == 0


def test_cancel_before_submit_persists_runner_tombstone_against_late_request(
    setup, monkeypatch, tmp_path
):
    load(setup)
    run(setup)
    cancel(setup)
    store = RunnerStore(tmp_path / "runner")
    permits = []

    def exchange(config, claims, *, action):
        assert action == "cancel"
        permits.append(claims)
        return store.cancel(claims)

    monkeypatch.setattr(capacity, "capacity_execution", exchange)
    run(setup)
    assert_root(setup, "cancelled")
    assert store.submit(permits[0], "a" * 64).state == "cancelled"
    with store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 1


def test_cancellation_after_claim_before_handler_also_finishes_domain(setup):
    load(setup)
    lease = jobs.claim_platform_job(setup.config, worker_code="claimed-before-cancel")
    assert lease is not None
    cancel(setup)
    assert not jobs._start_platform_job(setup.config, lease)
    assert_root(setup, "cancelled")
    assert row(setup, "SELECT status FROM load_test_runs")[0] == "stopped"


def test_rejected_start_creates_no_wait_or_network_execution(setup, monkeypatch):
    load(setup)
    original = capacity.start_load_test_in_transaction

    def drift(connection, **kwargs):
        before = dict(original(connection, **kwargs))
        before["platform_job_id"] = "pjob_" + "0" * 32
        return before

    monkeypatch.setattr(capacity, "start_load_test_in_transaction", drift)
    run(setup)
    assert_root(setup, "failed")
    assert row(setup, "SELECT COUNT(*) FROM platform_job_external_waits")[0] == 0
    assert row(setup, "SELECT started_at FROM load_test_runs")[0] is None
