"""Production queue routing must not duplicate work or borrow another queue's fence."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections import Counter
from datetime import UTC, datetime, timedelta
from importlib import import_module
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from metahub_control.console_migration.execution import (
    scheduler_queue_proof,
    scheduler_queue_scope,
    selected_scheduler_work,
)
from metahub_control.console_migration.manifest import SCHEDULER_QUEUES_BY_SCHEDULER
from metahub_control.console_migration.scheduler import SchedulerLeaseProof
from metahub_shared.runtime.errors import ApiError

MODULES = {
    "console-experience": "console_experience.scheduler",
    "commercial-accounting": "commercial_accounting.scheduler",
    "platform-customer-success": "platform_customer_success.scheduler",
    "platform-provider-engineering": "provider_engineering.scheduler",
    "platform-growth": "platform_growth.scheduler",
    "brand-domain": "brand_domains.scheduler",
    "channel-attribution": "channel_management.scheduler",
    "platform-job-runtime": "platform_jobs.supervisor",
}


def _proof(code, suffix):
    queue = next(q for q in SCHEDULER_QUEUES_BY_SCHEDULER[code] if q.code == f"{code}.{suffix}")
    return SchedulerLeaseProof(code, queue.code, queue.partition_code, "test-only", 1, 1,
                               datetime.now(UTC) + timedelta(seconds=30))


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.parametrize("code", MODULES)
def test_each_manifest_queue_has_disjoint_complete_work_assignments(code):
    module = import_module(f"metahub_control.{MODULES[code]}")
    assignments = module._WORKER_QUEUES
    assert {f"{code}.{suffix}" for suffix in assignments.values()} == {
        q.code for q in SCHEDULER_QUEUES_BY_SCHEDULER[code]
    }
    actual = []
    for suffix in set(assignments.values()):
        with scheduler_queue_scope(_proof(code, suffix)):
            actual.extend(selected_scheduler_work(code, assignments))
    assert Counter(actual) == Counter(assignments.keys())
    assert selected_scheduler_work(code, assignments) == tuple(assignments)


@pytest.mark.anyio
async def test_queue_context_is_task_local_inherits_into_threads_and_restores():
    async def child(suffix):
        proof = _proof("console-experience", suffix)
        with scheduler_queue_scope(proof):
            await asyncio.sleep(0)
            assert await asyncio.to_thread(scheduler_queue_proof, proof.scheduler_code) is proof
            with pytest.raises(ApiError):
                scheduler_queue_proof("commercial-accounting")
        assert scheduler_queue_proof("console-experience") is None

    await asyncio.gather(child("main"), child("workspace-dashboard"))
    assert scheduler_queue_proof("console-experience") is None


def test_all_multi_queue_schedulers_have_actual_work_routing():
    assert set(MODULES) == {code for code, queues in SCHEDULER_QUEUES_BY_SCHEDULER.items()
                            if len(queues) > 1}


@pytest.mark.parametrize("code", ["brand-domain", "platform-provider-engineering"])
def test_real_brand_and_provider_loops_dispatch_each_operation_once(monkeypatch, code):
    worker = import_module(f"metahub_control.{MODULES[code]}")
    called = []
    if code == "brand-domain":
        monkeypatch.setattr(worker, "_claim_job", lambda _config, **kw: called.append(kw["job_code"]))
        def run():
            return worker.run_brand_domain_once(None, worker_id="test-worker")
    else:
        for name, item in {
            "run_adapter_contract_event_once": "adapter_contract",
            "run_provider_error_map_event_once": "error_catalog",
            "run_next_contract_command": "contract_command",
            "refresh_compatibility_projections": "compatibility",
            "drain_provider_engineering_event_once": "engineering_outbox",
        }.items():
            monkeypatch.setattr(worker, name, lambda *args, _item=item, **kw: called.append(_item))
        def run():
            return worker.run_provider_engineering_once(None)
    for suffix in set(worker._WORKER_QUEUES.values()):
        with scheduler_queue_scope(_proof(code, suffix)):
            run()
    assert Counter(called) == Counter(worker._WORKER_QUEUES.keys())


@pytest.mark.parametrize("code", ["platform-customer-success", "platform-growth"])
@pytest.mark.parametrize("suffix", ["main", "child"])
def test_real_sql_claim_excludes_due_work_owned_by_other_queues(monkeypatch, code, suffix):
    worker = import_module(f"metahub_control.{MODULES[code]}")
    suffix = suffix if suffix == "main" else ("health" if code.endswith("success") else "events")
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    customer = code.endswith("success")
    table = "platform_customer_success_scheduler_state" if customer else "platform_growth_scheduler_state"
    connection.execute(f"CREATE TABLE {table}(job_code TEXT,status TEXT,next_run_at TEXT,"
                       "lease_expires_at TEXT,customer_success_scheduler_id TEXT)")
    for job, assigned in worker._WORKER_QUEUES.items():
        if assigned != suffix:
            connection.execute(f"INSERT INTO {table} VALUES(?,?,?,NULL,?)",
                               (job, "pending" if customer else "idle", "2000-01-01T00:00:00Z", job))
    connection.commit()
    monkeypatch.setattr(worker, "_connect", lambda _config: connection)
    if customer:
        monkeypatch.setattr(worker, "_require_schema", lambda _connection: None)
        reconciled = []
        monkeypatch.setattr(worker, "reconcile_accounts", lambda *args, **kw: reconciled.append(True))
        def run():
            return worker.run_platform_customer_success_scheduler_once(None, worker_id="test-worker")
    else:
        monkeypatch.setattr(worker, "_schema_ready", lambda _connection: True)
        def run():
            return worker._claim_job(None, worker_id="test-worker", now=datetime.now(UTC))
    with scheduler_queue_scope(_proof(code, suffix)):
        assert not run()
    if customer:
        assert bool(reconciled) == (suffix == "main")


@pytest.mark.parametrize("suffix,expected", [("main", "outbox"), ("statements", "statements")])
def test_real_channel_batch_executes_only_its_work(monkeypatch, suffix, expected):
    worker = import_module("metahub_control.channel_management.scheduler")
    connection = sqlite3.connect(":memory:")
    monkeypatch.setattr(worker, "_connect", lambda _config: connection)
    monkeypatch.setattr(worker, "_assert_fence", lambda *args: {"bounded_batch_size": 1})
    called = []
    monkeypatch.setattr(worker, "_close_due_statements", lambda *args: called.append("statements"))
    monkeypatch.setattr(worker, "_deliver_outbox", lambda *args: called.append("outbox"))
    with scheduler_queue_scope(_proof("channel-attribution", suffix)):
        assert not worker._process_fenced_batch(None, SimpleNamespace(
            expires_at=datetime.now(UTC) + timedelta(seconds=30)), lambda: False)
    assert called == [expected]


@pytest.mark.anyio
@pytest.mark.parametrize("suffix,expected", [("main", "outbox"), ("jobs", "jobs")])
async def test_platform_runtime_uses_own_proof_not_shared_primary(monkeypatch, suffix, expected):
    worker = import_module("metahub_control.platform_jobs.supervisor")
    app = FastAPI()
    app.state.config = SimpleNamespace(platform_job_poll_interval_seconds=0.05)
    app.state.scheduler_registry_leases = {"platform-job-runtime": _proof("platform-job-runtime", "jobs")}
    app.state.console_migration_event_sink = lambda event: None
    called = []
    def capture(kind):
        def operation(*args, **kw):
            called.append((kind, kw["outer_fence"].partition_code))
            return False
        return operation
    monkeypatch.setattr(worker, "run_platform_job_once", capture("jobs"))
    monkeypatch.setattr(worker, "dispatch_console_migration_event_once", capture("outbox"))
    async def stop_after_iteration(stop, delay):
        stop.set()
    monkeypatch.setattr(worker, "_wait", stop_after_iteration)
    proof = _proof("platform-job-runtime", suffix)
    with scheduler_queue_scope(proof):
        await worker._worker_loop(app, threading.Event())
    assert called == [(expected, proof.partition_code)]
