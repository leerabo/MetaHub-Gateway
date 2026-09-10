"""Native capacity transactions. Protocol fixtures are not benchmark evidence."""

from __future__ import annotations

import os
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
import test_capacity_external_runtime as scenarios
import test_prd113_capacity_management as base
from metahub_control.capacity_management import runtime as capacity
from metahub_control.capacity_management import service
from metahub_control.capacity_management.models import CapacityForecastRequest
from metahub_control.platform_jobs import external
from metahub_control.platform_jobs import runtime as jobs
from metahub_control.platform_jobs import service as job_service
from metahub_control.platform_jobs.models import PlatformJobActionRequest
from metahub_shared.db import (
    DbError,
    apply_migrations,
    apply_seed_data,
    connect_database,
    rollback_last_migration,
)
from metahub_shared.runtime.errors import ApiError
from test_prd147_postgresql_runtime import _create_database, _drop_database

pytestmark = pytest.mark.skipif(
    not os.getenv("METAHUB_TEST_POSTGRESQL_ADMIN_URL"),
    reason="isolated native PostgreSQL admin URL required",
)

setup = scenarios.setup
test_atomic_start_precedes_network_and_pending_never_spends_attempts = (
    scenarios.test_atomic_start_precedes_network_and_pending_never_spends_attempts
)
test_uncertain_delivery_replays_original_permit_then_renews_only_observation = (
    scenarios.test_uncertain_delivery_replays_original_permit_then_renews_only_observation
)
test_cancel_before_or_during_submission_requires_confirmed_stop = (
    scenarios.test_cancel_before_or_during_submission_requires_confirmed_stop
)
test_cancel_racing_success_cannot_create_baseline = (
    scenarios.test_cancel_racing_success_cannot_create_baseline
)
test_queued_cancellation_also_finishes_domain = (
    scenarios.test_queued_cancellation_also_finishes_domain
)
test_expired_unacknowledged_submission_is_cancelled_without_renewed_submit = (
    scenarios.test_expired_unacknowledged_submission_is_cancelled_without_renewed_submit
)
test_deadline_and_unavailable_runner_do_not_claim_stop = (
    scenarios.test_deadline_and_unavailable_runner_do_not_claim_stop
)
test_kill_keeps_domain_pending_until_runner_confirms = (
    scenarios.test_kill_keeps_domain_pending_until_runner_confirms
)
test_expired_observer_response_cannot_commit_domain_or_baseline = (
    scenarios.test_expired_observer_response_cannot_commit_domain_or_baseline
)
test_domain_and_root_completion_roll_back_together = (
    scenarios.test_domain_and_root_completion_roll_back_together
)
test_start_expiry_rolls_back_wait_and_closes_unstarted_domain = (
    scenarios.test_start_expiry_rolls_back_wait_and_closes_unstarted_domain
)
test_long_soak_includes_start_window_without_truncating_requested_duration = (
    scenarios.test_long_soak_includes_start_window_without_truncating_requested_duration
)
test_cancel_before_submit_persists_runner_tombstone_against_late_request = (
    scenarios.test_cancel_before_submit_persists_runner_tombstone_against_late_request
)
test_cancellation_after_claim_before_handler_also_finishes_domain = (
    scenarios.test_cancellation_after_claim_before_handler_also_finishes_domain
)
test_rejected_start_creates_no_wait_or_network_execution = (
    scenarios.test_rejected_start_creates_no_wait_or_network_execution
)


@pytest.fixture(scope="module")
def capacity_template():
    name, url = _create_database()
    try:
        assert "0159" in apply_migrations(url).applied
        with pytest.MonkeyPatch.context() as patch:
            for key in tuple(os.environ):
                if key.startswith("METAHUB_SEED_"):
                    patch.delenv(key)
            for key in ("ADMIN_PASSWORD", "VIRTUAL_KEY", "PROVIDER_API_KEY"):
                patch.setenv("METAHUB_SEED_" + key, secrets.token_urlsafe(48))
            apply_seed_data(url)
        base.seed_operators(url)
        with closing(connect_database(url)) as connection:
            assert connection.execute("SELECT COUNT(*) FROM platform_jobs").fetchone()[0] == 0
        yield name
    finally:
        _drop_database(name)


@pytest.fixture
def runtime(capacity_template):
    name, url = _create_database(template=capacity_template)
    try:
        yield base._config(url)
    finally:
        _drop_database(name)


@pytest.fixture
def real_setup(runtime, monkeypatch):
    actor = replace(base.OPS, permissions=(*base.OPS.permissions, "platform.job.manage"))
    for module in (service, job_service):
        monkeypatch.setattr(
            module, "authenticate_platform_session_in_transaction", lambda *a, **k: actor
        )
    monkeypatch.setattr(
        capacity, "capacity_execution", lambda *a, **k: pytest.fail("unexpected network")
    )
    runtime.capacity_runner_mode = "isolated_http"
    return SimpleNamespace(config=runtime)


def _projection_installed(url):
    with psycopg.connect(url) as connection:
        return (
            connection.execute(
                "SELECT COUNT(*) FROM pg_trigger WHERE "
                "tgname='trg_capacity_job_terminal_projection' AND tgenabled='O' "
                "AND tgrelid='platform_jobs'::regclass"
            ).fetchone()[0]
            == 1
        )


def test_native_0159_down_up_and_idempotent_replay(runtime):
    url = runtime.database_url
    assert _projection_installed(url)
    assert rollback_last_migration(url, expected_version="0159").rolled_back == "0159"
    assert not _projection_installed(url)
    assert apply_migrations(url).applied == ["0159"]
    assert _projection_installed(url)
    assert apply_migrations(url).applied == []


def test_native_0159_down_refuses_retained_capacity_facts(real_setup):
    object_id = scenarios.load(real_setup)
    with pytest.raises(DbError) as rejected:
        rollback_last_migration(real_setup.config.database_url, expected_version="0159")
    assert rejected.value.code == "db.migration_failed"
    down = (
        Path(__file__).resolve().parents[3]
        / "infra/migrations/postgresql/0159_capacity_job_terminal_projection.down.sql"
    ).read_text(encoding="utf-8")
    with psycopg.connect(real_setup.config.database_url, autocommit=True) as native:
        with pytest.raises(psycopg.errors.RaiseException, match="capacity_facts_prevent_down"):
            native.execute(down, prepare=False)
    assert _projection_installed(real_setup.config.database_url)
    assert tuple(
        scenarios.row(real_setup, "SELECT load_test_run_id,status FROM load_test_runs")
    ) == (object_id, "queued")
    assert scenarios.row(real_setup, "SELECT status FROM platform_jobs")[0] == "queued"


def test_native_queued_forecast_cancellation_projects_terminal_state(real_setup):
    config = real_setup.config
    forecast = service.create_capacity_forecast(
        config,
        session_token=base.PLATFORM_TOKEN,
        csrf_token=base.PLATFORM_CSRF,
        expected_revision=1,
        payload=CapacityForecastRequest(
            service_code="gateway",
            region_code="cn-north-1",
            horizon_days=30,
            model_code="linear-v1",
            confirmed=True,
        ),
        idempotency_key="pg-capacity-forecast-create-0001",
        request_id="request-pg-forecast",
        trace_id="trace-pg-forecast",
    )
    job = scenarios.row(real_setup, "SELECT platform_job_id,revision FROM platform_jobs")
    response = job_service.update_platform_job(
        config,
        session_token=base.PLATFORM_TOKEN,
        csrf_token=base.PLATFORM_CSRF,
        job_id=job[0],
        action="cancel",
        expected_revision=job[1],
        idempotency_key="pg-capacity-forecast-cancel-0001",
        payload=PlatformJobActionRequest(reason_code="operator_cancelled", confirmed=True),
        request_id="request-pg-forecast-cancel",
        trace_id="trace-pg-forecast-cancel",
    )
    assert response.job.status == "cancelled"
    terminal = scenarios.row(real_setup, "SELECT * FROM capacity_forecasts")
    assert terminal["capacity_forecast_id"] == forecast.forecast.capacity_forecast_id
    assert terminal["status"] == "failed"
    assert terminal["completed_at"] is not None
    assert scenarios.row(real_setup, "SELECT COUNT(*) FROM platform_job_attempts")[0] == 0
    assert scenarios.row(real_setup, "SELECT COUNT(*) FROM platform_job_external_waits")[0] == 0


def test_native_capacity_external_claim_is_exclusive(real_setup):
    scenarios.load(real_setup)
    assert jobs.run_platform_job_once(real_setup.config, worker_code="pg-capacity-start")
    barrier = threading.Barrier(2)

    def compete(worker):
        barrier.wait(timeout=10)
        return external.claim_external_job(real_setup.config, worker_code=worker)

    with ThreadPoolExecutor(max_workers=2) as workers:
        leases = list(workers.map(compete, ("pg-capacity-one", "pg-capacity-two")))
    owned = [lease for lease in leases if lease is not None]
    assert len(owned) == 1
    assert (
        owned[0].job_id == scenarios.row(real_setup, "SELECT platform_job_id FROM platform_jobs")[0]
    )
    external.ExternalJobContext(real_setup.config, owned[0]).defer()
    scenarios.assert_root(real_setup, "running")


def _wait_for_blocked_connection(url, holder_pid):
    deadline = time.monotonic() + 10
    with psycopg.connect(url, autocommit=True) as connection:
        while time.monotonic() < deadline:
            blocked = connection.execute(
                "SELECT COUNT(*) FROM pg_stat_activity "
                "WHERE datname=current_database() AND %s=ANY(pg_blocking_pids(pid))",
                (holder_pid,),
            ).fetchone()[0]
            if blocked:
                return
            time.sleep(0.02)
    pytest.fail("competing operation did not wait on the actual PostgreSQL row lock")


@pytest.mark.parametrize("winner", ["cancel", "complete"])
def test_native_cancel_completion_lock_order_is_atomic(real_setup, monkeypatch, winner):
    config = real_setup.config
    scenarios.load(real_setup)
    assert jobs.run_platform_job_once(config, worker_code="pg-capacity-race-start")
    lease = external.claim_external_job(config, worker_code="pg-capacity-race-observer")
    assert lease is not None
    context = external.ExternalJobContext(config, lease)
    continuation = external.decrypt_job_value(
        config,
        lease.continuation_envelope,
        expected_sha256=lease.continuation_sha256,
        kind="checkpoint",
        owner_id=lease.job_id,
        type_code=lease.type_code + ":external-v1",
    )
    job = scenarios.row(real_setup, "SELECT platform_job_id,revision FROM platform_jobs")
    entered, release = threading.Event(), threading.Event()
    holder = {}

    def hold(connection):
        holder["pid"] = connection.execute("SELECT pg_backend_pid()").fetchone()[0]
        entered.set()
        assert release.wait(15)

    target = job_service if winner == "cancel" else capacity
    method = "_audit" if winner == "cancel" else "complete_load_test_in_transaction"
    original = getattr(target, method)

    def held(connection, **kwargs):
        value = original(connection, **kwargs)
        hold(connection)
        return value

    monkeypatch.setattr(target, method, held)
    monkeypatch.setattr(
        capacity, "capacity_execution", lambda config, claims, **kw: scenarios.result(claims)
    )

    def cancel():
        try:
            response = job_service.update_platform_job(
                config,
                session_token=base.PLATFORM_TOKEN,
                csrf_token=base.PLATFORM_CSRF,
                job_id=job[0],
                action="cancel",
                expected_revision=job[1],
                idempotency_key="pg-capacity-race-cancel-0001",
                payload=PlatformJobActionRequest(reason_code="operator_cancelled", confirmed=True),
                request_id="request-pg-capacity-cancel",
                trace_id="trace-pg-capacity-cancel",
            )
            return response.job.status
        except ApiError as exc:
            return exc.code

    def complete():
        return capacity._capacity_external_handler(context, continuation)

    operations = {"cancel": cancel, "complete": complete}
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(operations[winner])
        try:
            assert entered.wait(10)
            second = workers.submit(operations["complete" if winner == "cancel" else "cancel"])
            _wait_for_blocked_connection(config.database_url, holder["pid"])
        finally:
            release.set()
        results = [first.result(timeout=15), second.result(timeout=15)]
    assert (
        "platform_job.cancelled" if winner == "cancel" else "capacity.load_test_succeeded"
    ) in results
    assert (
        "cancel_requested" if winner == "cancel" else "platform_job.revision_conflict"
    ) in results
    terminal = "cancelled" if winner == "cancel" else "succeeded"
    scenarios.assert_root(real_setup, terminal)
    assert scenarios.row(real_setup, "SELECT status FROM platform_job_attempts")[0] == terminal
    assert (
        scenarios.row(real_setup, "SELECT status FROM platform_job_external_waits")[0]
        == "completed"
    )
    assert scenarios.row(real_setup, "SELECT COUNT(*) FROM platform_job_effects")[0] == 1
    assert scenarios.row(real_setup, "SELECT status FROM load_test_runs")[0] == (
        "stopped" if winner == "cancel" else "succeeded"
    )
    assert scenarios.row(real_setup, "SELECT COUNT(*) FROM performance_baselines")[0] == (
        0 if winner == "cancel" else 1
    )
