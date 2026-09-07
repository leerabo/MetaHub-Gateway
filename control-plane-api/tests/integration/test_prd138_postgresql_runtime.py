"""Native PostgreSQL contracts; synthetic actors/time are not launch approvals."""

from __future__ import annotations

import inspect
import queue
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace

import pytest
import test_postgresql_native_migrations as native_migrations
import test_prd138_launch_governance as contracts
import test_prd147_postgresql_runtime as native_runtime_helpers
from metahub_shared.db import (
    apply_migrations,
    apply_seed_data,
    close_database_pools,
    connect_database,
)

pytestmark = native_migrations.pytestmark


@pytest.fixture(scope="module")
def native_launch_template():
    name, database_url = native_runtime_helpers._create_database()
    try:
        assert {"0130", "0139"} <= set(apply_migrations(database_url).applied)
        with pytest.MonkeyPatch.context() as patch:
            patch.setenv("METAHUB_SEED_ADMIN_PASSWORD", secrets.token_urlsafe(48))
            apply_seed_data(database_url)
        runtime = contracts._runtime(database_url)
        close_database_pools()
        yield name, runtime
    finally:
        native_runtime_helpers._drop_database(name)


@pytest.fixture
def native_launch_runtime(native_launch_template):
    template, runtime = native_launch_template
    name, database_url = native_runtime_helpers._create_database(template=template)
    try:
        yield replace(runtime, config=runtime.config.model_copy(update={
            "database_url": database_url,
        }))
    finally:
        native_runtime_helpers._drop_database(name)


def test_postgresql_launch_list(native_launch_runtime, monkeypatch):
    contracts.test_prd138_list_supports_reused_request_connection(
        monkeypatch, native_launch_runtime
    )


SCENARIOS = (
    "test_four_routes_strict_body_and_missing_verifier_fail_closed",
    "test_signed_evidence_and_six_independent_decisions_reach_ready",
    "test_permission_revision_change_blocks_wave_start",
    "test_final_decision_rechecks_all_current_signer_permissions",
    "test_four_waves_and_review_can_only_form_release_candidate",
    "test_rollback_requires_distinct_verifier_and_previous_wave",
)


@pytest.mark.anyio
@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_postgresql_launch_contracts(scenario, native_launch_runtime, monkeypatch):
    case = getattr(contracts, scenario)
    assert inspect.iscoroutinefunction(case)
    await case(runtime=native_launch_runtime, monkeypatch=monkeypatch)


@pytest.mark.anyio
@pytest.mark.parametrize("rejection", ["wrong_signature", "removed_key", "invalid_trust"])
async def test_postgresql_real_evidence_signatures(
    native_launch_runtime, monkeypatch, tmp_path, rejection
):
    await contracts.test_real_signature_api_rejects_without_writes_and_persists_only_digests(
        native_launch_runtime, monkeypatch, tmp_path, rejection
    )


@pytest.mark.parametrize("release", ["commit", "rollback"])
def test_postgresql_scheduler_waits_for_governance_lock(
    native_launch_runtime, monkeypatch, release
):
    config = native_launch_runtime.config
    backend_ids = queue.Queue()
    original_connect = contracts.service._connect

    def tracked_connect(current):
        connection = original_connect(current)
        backend_ids.put(connection.execute("SELECT pg_backend_pid()").fetchone()[0])
        connection.commit()
        return connection

    monkeypatch.setattr(contracts.service, "_connect", tracked_connect)
    with (
        closing(connect_database(config.database_url)) as holder,
        closing(connect_database(config.database_url)) as observer,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        holder.execute("BEGIN")
        holder.execute(
            "SELECT singleton_id FROM launch_scheduler_state WHERE singleton_id=1 FOR UPDATE"
        ).fetchone()
        future = executor.submit(
            contracts.service.process_launch_governance_once, config, worker_id="native-worker"
        )
        try:
            backend_id = backend_ids.get(timeout=10)
            deadline = time.monotonic() + 10
            waiting = False
            while time.monotonic() < deadline:
                waiting = bool(observer.execute(
                    "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=? AND NOT granted)",
                    (backend_id,),
                ).fetchone()[0])
                observer.commit()
                if waiting or future.done():
                    break
                threading.Event().wait(0.02)
            assert waiting, "scheduler must wait for the actual PostgreSQL governance lock"
            assert not future.done()
        finally:
            getattr(holder, release)()
        assert future.result(timeout=15) == 0
        state = holder.execute("SELECT * FROM launch_scheduler_state").fetchone()
        assert state["fencing_generation"] == 1
        assert state["lease_owner_id"] is None
        assert state["lease_token_sha256"] is None


def test_postgresql_concurrent_create_returns_one_fact_and_one_replay(
    native_launch_runtime, monkeypatch
):
    runtime = native_launch_runtime
    contracts._app(monkeypatch, runtime)
    command = contracts.service.LaunchCommand.model_validate(
        contracts._candidate_body(runtime.workspace_id)
    )
    barrier = threading.Barrier(2)

    def create():
        barrier.wait(timeout=10)
        return contracts.service.apply_launch_command(
            runtime.config, command, platform_session_token=runtime.tokens["requester"],
            platform_csrf_token=contracts.PLATFORM_CSRF, step_up_grant=contracts.STEP_UP,
            idempotency_key="prd138-concurrent-create-0001",
            request_id="req_prd138_concurrent", trace_id="trc_prd138_concurrent",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(create) for _ in range(2)]
        responses = [future.result(timeout=30) for future in futures]
    assert sorted(response.idempotent_replay for response in responses) == [False, True]
    assert len({response.candidate.launch_candidate_id for response in responses}) == 1
    with closing(connect_database(runtime.config.database_url)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM launch_candidates").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM launch_action_commands").fetchone()[0] == 1
