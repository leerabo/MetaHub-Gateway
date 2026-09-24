"""PRD-147 scheduler manifest, lease, fencing, and coordinator tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import threading
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from metahub_control.console_migration import scheduler as scheduler_module
from metahub_control.console_migration.frontend_manifest import (
    FRONTEND_PRODUCTION_ASSET_SET_DIGEST,
    FRONTEND_PRODUCTION_SURFACE_MANIFEST,
    FRONTEND_PRODUCTION_SURFACE_MANIFEST_DIGEST,
)
from metahub_control.console_migration.manifest import (
    BASELINE_SCHEDULER_MAPPINGS,
    INSTALLER_SCHEDULER_MANIFEST,
    SCHEDULER_MANIFEST,
    SCHEDULER_QUEUE_MANIFEST,
    SCHEDULER_QUEUES_BY_SCHEDULER,
    configured_installer_definitions,
)
from metahub_control.console_migration.scheduler import (
    SchedulerContextBinding,
    _database_operation,
    claim_scheduler_lease,
    claim_scheduler_leases,
    release_scheduler_lease,
    release_scheduler_leases,
    renew_scheduler_lease,
    renew_scheduler_leases,
    scheduler_coordinator_lifespan,
)
import metahub_control.platform_jobs.supervisor as supervisor_module
from metahub_control.platform_jobs.supervisor import (
    SchedulerInstallerBinding,
    install_platform_job_supervisor,
)
from metahub_shared.db import connect_database
from metahub_shared.db.connection import database_request_scope
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.logging import JsonFormatter

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "infra" / "migrations" / "0139_console_domain_migration_governance.sql"


def _config(database) -> SimpleNamespace:
    return SimpleNamespace(
        database_url=f"sqlite:///{database.as_posix()}",
        platform_session_secret="s" * 43,
        gateway_placement_enabled=False,
        control_background_schedulers_enabled=False,
        console_migration_scheduler_lease_seconds=3.0,
        console_migration_scheduler_poll_seconds=0.05,
        console_migration_scheduler_shutdown_seconds=1.0,
    )


def test_coordinator_database_lane_survives_saturated_default_executor(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    config = _config(_database(tmp_path))
    app = FastAPI()
    app.state.config = config

    async def scenario():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        unblock = threading.Event()
        occupied = loop.run_in_executor(None, unblock.wait)
        manager = scheduler_coordinator_lifespan(app, (), replica_code="dedicated-db-lane")
        await manager.__aenter__()
        try:
            thread_name = await asyncio.wait_for(scheduler_module._coordinator_database_call(
                app, lambda: threading.current_thread().name), timeout=1)
            assert thread_name.startswith("scheduler-registry-db")
        finally:
            unblock.set()
            await occupied
            await manager.__aexit__(None, None, None)
        assert app.state.scheduler_database_executor is None
        assert app.state.scheduler_observation_executor is None

    asyncio.run(scenario())


def _database(tmp_path, scheduler_code: str | None = "status-publication"):
    database = tmp_path / "scheduler.sqlite3"
    definitions = tuple(item for item in SCHEDULER_MANIFEST
                        if scheduler_code is None or item.code == scheduler_code)
    connection = sqlite3.connect(database)
    try:
        connection.executescript(
            """CREATE TABLE scheduler_registry_entries(
                 scheduler_code TEXT PRIMARY KEY,
                 enabled INTEGER NOT NULL,
                 manifest_digest TEXT NOT NULL
               );
               CREATE TABLE scheduler_leases(
                 scheduler_code TEXT NOT NULL,
                 partition_code TEXT NOT NULL,
                 lease_owner_pseudonym TEXT NOT NULL,
                 lease_deadline_at TEXT NOT NULL,
                 fencing_generation INTEGER NOT NULL,
                 lease_token_hmac TEXT NOT NULL,
                 last_started_at TEXT NOT NULL,
                 last_completed_at TEXT,
                 last_error_code TEXT,
                 revision INTEGER NOT NULL,
                 updated_at TEXT NOT NULL,
                 PRIMARY KEY(scheduler_code,partition_code)
               );
               CREATE TABLE scheduler_queue_contracts(
                 scheduler_code TEXT NOT NULL,
                 queue_code TEXT NOT NULL UNIQUE,
                 owner_domain TEXT NOT NULL,
                 partition_policy_code TEXT NOT NULL,
                 manifest_digest TEXT NOT NULL,
                 enabled INTEGER NOT NULL
               );
               CREATE TABLE scheduler_registry_heads(
                 head_key TEXT PRIMARY KEY,
                 manifest_digest TEXT NOT NULL,
                 generation INTEGER NOT NULL
               );
               CREATE TABLE runtime_surface_observations(
                 runtime_surface_observation_id TEXT PRIMARY KEY,
                 runtime_surface_code TEXT NOT NULL,
                 replica_pseudonym TEXT NOT NULL,
                 accepted_count INTEGER NOT NULL,
                 dropped_count INTEGER NOT NULL,
                 persisted_count INTEGER NOT NULL,
                 backend_failure_count INTEGER NOT NULL,
                 queued_count INTEGER NOT NULL,
                 shutdown_timeout_count INTEGER NOT NULL,
                 observed_at TEXT NOT NULL
               );
               CREATE TABLE scheduler_commit_probes(
                 probe_id TEXT PRIMARY KEY
               );"""
        )
        connection.executemany(
            "INSERT INTO scheduler_registry_entries VALUES(?,1,?)",
            [(definition.code, definition.manifest_digest) for definition in definitions],
        )
        connection.executemany(
            "INSERT INTO scheduler_queue_contracts VALUES(?,?,?,?,?,1)",
            [
                (
                    queue.scheduler_code,
                    queue.code,
                    queue.owner_domain,
                    queue.partition_policy_code,
                    queue.manifest_digest,
                )
                for definition in definitions
                for queue in SCHEDULER_QUEUES_BY_SCHEDULER[definition.code]
            ],
        )
        manifest_digest = hashlib.sha256(
            "|".join(
                f"{item.code}:{item.manifest_digest}"
                for item in sorted(SCHEDULER_MANIFEST, key=lambda item: item.code)
            ).encode("ascii")
        ).hexdigest()
        connection.execute(
            "INSERT INTO scheduler_registry_heads VALUES('production',?,1)",
            (manifest_digest,),
        )
        connection.commit()
    finally:
        connection.close()
    return database


def test_code_manifest_exactly_matches_0139_registry_and_mapping() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    registry_block = sql.split(
        "WITH manifest(scheduler_code,owner_domain,config_condition) AS (VALUES", 1
    )[1].split(")\nINSERT INTO scheduler_registry_entries", 1)[0]
    registry_rows = tuple(
        re.findall(r"\('([^']+)','([^']+)','([^']+)'\)", registry_block)
    )
    mapping_block = sql.split(
        "WITH mapping(baseline_scheduler_code,final_scheduler_code,mapping_kind) AS (VALUES",
        1,
    )[1].split(")\nINSERT INTO scheduler_baseline_mappings", 1)[0]
    mapping_rows = tuple(
        re.findall(r"\('([^']+)','([^']+)','([^']+)'\)", mapping_block)
    )

    assert len(INSTALLER_SCHEDULER_MANIFEST) == 36
    assert len(SCHEDULER_MANIFEST) == 37
    assert len(BASELINE_SCHEDULER_MAPPINGS) == 41
    assert len(SCHEDULER_QUEUE_MANIFEST) == 64
    assert len({item.code for item in SCHEDULER_QUEUE_MANIFEST}) == 64
    assert {item.scheduler_code for item in SCHEDULER_QUEUE_MANIFEST} == {
        item.code for item in SCHEDULER_MANIFEST
    }
    assert len({item.code for item in SCHEDULER_MANIFEST}) == 37
    assert registry_rows == tuple(
        (item.code, item.owner_domain, item.config_condition)
        for item in SCHEDULER_MANIFEST
    )
    assert mapping_rows == tuple(
        (item.baseline_installer, item.final_scheduler_code, item.mapping_kind)
        for item in BASELINE_SCHEDULER_MAPPINGS
    )
    assert {item.final_scheduler_code for item in BASELINE_SCHEDULER_MAPPINGS} <= {
        item.code for item in INSTALLER_SCHEDULER_MANIFEST
    }

    disabled = _config(Path("disabled.sqlite3"))
    assert [item.code for item in configured_installer_definitions(disabled)] == [
        "status-publication"
    ]
    enabled = SimpleNamespace(
        gateway_placement_enabled=True,
        control_background_schedulers_enabled=True,
    )
    assert len(configured_installer_definitions(enabled)) == 36


def test_frontend_release_digest_exactly_matches_production_manifest() -> None:
    disk_manifest = json.loads(
        (ROOT / "console-web" / "production-surface-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    canonical_disk = json.dumps(
        disk_manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    canonical_backend = json.dumps(
        FRONTEND_PRODUCTION_SURFACE_MANIFEST,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert canonical_backend == canonical_disk
    assert (
        hashlib.sha256(canonical_disk.encode("ascii")).hexdigest()
        == FRONTEND_PRODUCTION_SURFACE_MANIFEST_DIGEST
    )
    assert (
        disk_manifest["production_asset_set_sha256"]
        == FRONTEND_PRODUCTION_ASSET_SET_DIGEST
    )


def test_scheduler_lease_has_single_leader_and_rejects_late_fence(tmp_path) -> None:
    database = _database(tmp_path)
    config = _config(database)
    first = claim_scheduler_lease(
        config,
        scheduler_code="status-publication",
        replica_code="replica-one",
        lease_seconds=10,
    )
    assert first is not None
    assert first.generation == 1
    assert (
        claim_scheduler_lease(
            config,
            scheduler_code="status-publication",
            replica_code="replica-two",
            lease_seconds=10,
        )
        is None
    )
    renewed = renew_scheduler_lease(
        config,
        first,
        replica_code="replica-one",
        lease_seconds=10,
    )
    assert renewed is not None
    assert renewed.generation == first.generation

    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE scheduler_leases SET lease_deadline_at='2000-01-01T00:00:00Z'"
        )
        connection.commit()
    finally:
        connection.close()
    takeover = claim_scheduler_lease(
        config,
        scheduler_code="status-publication",
        replica_code="replica-two",
        lease_seconds=10,
    )
    assert takeover is not None
    assert takeover.generation == 2
    assert renew_scheduler_lease(config, renewed, replica_code="replica-one") is None
    assert not release_scheduler_lease(config, renewed, replica_code="replica-one")
    assert release_scheduler_lease(config, takeover, replica_code="replica-two")

    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "UPDATE scheduler_registry_entries SET manifest_digest=?",
            ("0" * 64,),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(ApiError) as captured:
        claim_scheduler_lease(
            config,
            scheduler_code="status-publication",
            replica_code="replica-three",
        )
    assert captured.value.code == "console_migration.registry_drift"


def test_multi_queue_scheduler_claims_renews_and_releases_atomically(tmp_path) -> None:
    scheduler_code = "platform-customer-success"
    database = _database(tmp_path, scheduler_code)
    config = _config(database)
    config.control_background_schedulers_enabled = True
    leases = claim_scheduler_leases(
        config,
        scheduler_code=scheduler_code,
        replica_code="replica-one",
        lease_seconds=10,
    )
    assert leases is not None
    assert {lease.queue_code for lease in leases} == {
        queue.code for queue in SCHEDULER_QUEUES_BY_SCHEDULER[scheduler_code]
    }
    assert len(leases) == 4
    assert (
        claim_scheduler_leases(
            config,
            scheduler_code=scheduler_code,
            replica_code="replica-two",
            lease_seconds=10,
        )
        is None
    )
    renewed = renew_scheduler_leases(
        config, leases, replica_code="replica-one", lease_seconds=10
    )
    assert renewed is not None
    assert [lease.generation for lease in renewed] == [
        lease.generation for lease in leases
    ]
    assert release_scheduler_leases(config, renewed, replica_code="replica-one")
    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT count(*) FROM scheduler_leases WHERE scheduler_code=?",
            (scheduler_code,),
        ).fetchone()[0] == 4
    finally:
        connection.close()


@pytest.mark.anyio
async def test_two_coordinators_start_at_most_one_child_and_take_over(tmp_path) -> None:
    database = _database(tmp_path)
    config = _config(database)
    app_one = FastAPI()
    app_two = FastAPI()
    app_one.state.config = config
    app_two.state.config = config
    active: set[str] = set()
    maximum = [0]
    events: list[str] = []

    def context(replica: str):
        @asynccontextmanager
        async def child(_app):
            active.add(replica)
            maximum[0] = max(maximum[0], len(active))
            events.append(f"enter:{replica}")
            try:
                yield
            finally:
                active.remove(replica)
                events.append(f"exit:{replica}")

        return child

    first = scheduler_coordinator_lifespan(
        app_one,
        [SchedulerContextBinding("status-publication", context("one"))],
        replica_code="replica-one",
    )
    second = scheduler_coordinator_lifespan(
        app_two,
        [SchedulerContextBinding("status-publication", context("two"))],
        replica_code="replica-two",
    )
    await first.__aenter__()
    await second.__aenter__()
    try:
        await _wait_until(lambda: len(active) == 1)
        assert maximum[0] == 1
        assert tuple(app_one.state.scheduler_active_contexts) in (
            (),
            ("status-publication",),
        )
        assert tuple(app_two.state.scheduler_active_contexts) in (
            (),
            ("status-publication",),
        )
        leader = first if "one" in active else second
        follower_app = app_two if leader is first else app_one
        await leader.__aexit__(None, None, None)
        if leader is first:
            first = None
        else:
            second = None
        await _wait_until(
            lambda: tuple(follower_app.state.scheduler_active_contexts)
            == ("status-publication",)
        )
        assert maximum[0] == 1
    finally:
        if second is not None:
            await second.__aexit__(None, None, None)
        if first is not None:
            await first.__aexit__(None, None, None)

    assert events.count("enter:one") + events.count("enter:two") == 2
    assert events.count("exit:one") + events.count("exit:two") == 2
    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT count(*) FROM runtime_surface_observations"
        ).fetchone()[0] == 2
    finally:
        connection.close()


@pytest.mark.anyio
async def test_multi_queue_partitions_are_fairly_split_between_replicas(tmp_path) -> None:
    scheduler_code = "platform-customer-success"
    database = _database(tmp_path, scheduler_code)
    config = _config(database)
    config.control_background_schedulers_enabled = True
    app_one = FastAPI()
    app_two = FastAPI()
    app_one.state.config = config
    app_two.state.config = config

    @asynccontextmanager
    async def child(_app):
        yield

    first = scheduler_coordinator_lifespan(
        app_one,
        [SchedulerContextBinding(scheduler_code, child)],
        replica_code="replica-fair-one",
    )
    second = scheduler_coordinator_lifespan(
        app_two,
        [SchedulerContextBinding(scheduler_code, child)],
        replica_code="replica-fair-two",
    )
    await first.__aenter__()
    await second.__aenter__()
    try:
        expected = {
            f"{scheduler_code}:{queue.partition_code}"
            for queue in SCHEDULER_QUEUES_BY_SCHEDULER[scheduler_code]
        }
        await _wait_until(
            lambda: set(app_one.state.scheduler_active_partitions)
            | set(app_two.state.scheduler_active_partitions)
            == expected
        )
        first_partitions = set(app_one.state.scheduler_active_partitions)
        second_partitions = set(app_two.state.scheduler_active_partitions)
        assert first_partitions
        assert second_partitions
        assert first_partitions.isdisjoint(second_partitions)
        assert first_partitions | second_partitions == expected
        # Once either replica stops, the survivor may legitimately take over its queues.
        connection = sqlite3.connect(database)
        try:
            assert connection.execute(
                """SELECT count(DISTINCT lease_owner_pseudonym)
                   FROM scheduler_leases WHERE scheduler_code=?""",
                (scheduler_code,),
            ).fetchone()[0] == 2
        finally:
            connection.close()
    finally:
        await second.__aexit__(None, None, None)
        await first.__aexit__(None, None, None)


@pytest.mark.anyio
@pytest.mark.parametrize("domain", ["console_experience", "commercial_accounting"])
async def test_real_worker_loops_claim_each_work_item_only_in_its_queue(tmp_path, monkeypatch, domain):
    from collections import Counter
    from importlib import import_module

    worker = import_module(f"metahub_control.{domain}.scheduler")
    scheduler_code = domain.replace("_", "-")
    config = _config(_database(tmp_path, scheduler_code))
    config.control_background_schedulers_enabled = True
    config.workspace_navigation_scheduler_enabled = True
    app = FastAPI()
    app.state.config = config
    claimed = []
    monkeypatch.setattr(worker, "_claim", lambda _config, kind: claimed.append(kind))
    expected = list(worker._WORKER_KINDS)
    if domain == "console_experience":
        expected.extend(worker._NAVIGATION_WORKER_KINDS)
        monkeypatch.setattr(worker, "_claim_navigation", lambda _config, kind: claimed.append(kind))

    @asynccontextmanager
    async def child(_app):
        await asyncio.to_thread(getattr(worker, f"run_{domain}_once"), config)
        yield

    async with scheduler_coordinator_lifespan(
        app, [SchedulerContextBinding(scheduler_code, child)], replica_code="replica-work-routing"
    ):
        await _wait_until(lambda: len(app.state.scheduler_active_partitions)
                          == len(SCHEDULER_QUEUES_BY_SCHEDULER[scheduler_code]))
        assert Counter(claimed) == Counter(expected)


@pytest.mark.anyio
async def test_child_transaction_is_fenced_after_queue_lease_takeover(tmp_path) -> None:
    database = _database(tmp_path)
    config = _config(database)
    config.console_migration_scheduler_poll_seconds = 1.0
    app = FastAPI()
    app.state.config = config
    actions: asyncio.Queue[str] = asyncio.Queue()
    outcomes: asyncio.Queue[str] = asyncio.Queue()

    @asynccontextmanager
    async def child(_app):
        async def commit_loop() -> None:
            while True:
                marker = await actions.get()
                if marker == "stop":
                    return
                connection = connect_database(config.database_url)
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "INSERT INTO scheduler_commit_probes VALUES(?)", (marker,)
                    )
                    connection.commit()
                    await outcomes.put("committed")
                except ApiError as exc:
                    connection.rollback()
                    await outcomes.put(exc.code)
                finally:
                    connection.close()

        task = asyncio.create_task(commit_loop())
        try:
            yield
        finally:
            await actions.put("stop")
            await task

    manager = scheduler_coordinator_lifespan(
        app,
        [SchedulerContextBinding("status-publication", child)],
        replica_code="replica-one",
    )
    await manager.__aenter__()
    takeover = None
    try:
        await _wait_until(
            lambda: tuple(app.state.scheduler_active_contexts)
            == ("status-publication",)
        )
        leases = app.state.scheduler_queue_leases["status-publication"]
        assert len(leases) == 1
        await actions.put("live")
        assert await asyncio.wait_for(outcomes.get(), timeout=2) == "committed"

        assert release_scheduler_leases(config, leases, replica_code="replica-one")
        takeover = claim_scheduler_leases(
            config,
            scheduler_code="status-publication",
            replica_code="replica-two",
            lease_seconds=10,
        )
        assert takeover is not None
        assert takeover[0].generation == leases[0].generation + 1
        await actions.put("stale")
        assert await asyncio.wait_for(outcomes.get(), timeout=2) == (
            "platform_job.lease_lost"
        )
    finally:
        if takeover is not None:
            assert release_scheduler_leases(
                config, takeover, replica_code="replica-two"
            )
        await manager.__aexit__(None, None, None)


@pytest.mark.anyio
async def test_callable_only_supervisor_keeps_reverse_shutdown_compatibility() -> None:
    app = FastAPI()
    app.state.config = SimpleNamespace(control_background_schedulers_enabled=False)
    events: list[str] = []

    def installer(name: str):
        def install(current_app):
            original = current_app.router.lifespan_context

            @asynccontextmanager
            async def combined(active_app):
                async with original(active_app):
                    events.append(f"enter:{name}")
                    try:
                        yield
                    finally:
                        events.append(f"exit:{name}")

            current_app.router.lifespan_context = combined

        return install

    install_platform_job_supervisor(
        app,
        scheduler_installers=(installer("one"), installer("two")),
    )
    async with app.router.lifespan_context(app):
        assert events == ["enter:one", "enter:two"]
        assert app.state.platform_job_scheduler_context_count == 2
    assert events == ["enter:one", "enter:two", "exit:two", "exit:one"]


@pytest.mark.anyio
async def test_structured_supervisor_skips_registry_bindings_when_disabled(monkeypatch) -> None:
    """本地 HTTP 可用性 profile 关闭后台调度器时不得启动 registry lease I/O。"""

    app = FastAPI()
    app.state.config = SimpleNamespace(control_background_schedulers_enabled=False)
    captured: list[tuple[SchedulerContextBinding, ...]] = []

    def installer(current_app: FastAPI) -> None:
        @asynccontextmanager
        async def lifespan(_active_app: FastAPI):
            yield

        current_app.router.lifespan_context = lifespan

    @asynccontextmanager
    async def coordinator(_app, bindings, *, replica_code):
        del replica_code
        captured.append(tuple(bindings))
        yield

    monkeypatch.setattr(supervisor_module, "scheduler_coordinator_lifespan", coordinator)
    install_platform_job_supervisor(
        app,
        scheduler_installers=(
            SchedulerInstallerBinding("status-publication", installer),
        ),
    )
    async with app.router.lifespan_context(app):
        pass

    assert captured == []


async def _wait_until(predicate, *, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("timed out waiting for scheduler coordinator state")
        await asyncio.sleep(0.02)


@pytest.mark.anyio
async def test_coordinator_database_calls_reuse_handles_without_inheriting_request_scope(tmp_path):
    database = _database(tmp_path)
    config = _config(database)
    handles = []
    threads = []
    main_thread = threading.get_ident()

    def operation(*, fail=False):
        connection = connect_database(config.database_url)
        handles.append(connection._connection)
        threads.append(threading.get_ident())
        assert not connection.in_transaction
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM scheduler_commit_probes").fetchone()[0] == 0
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO scheduler_commit_probes VALUES('uncommitted')")
        connection.execute("PRAGMA query_only=ON")
        connection.close()
        if fail:
            raise RuntimeError("controlled failure")
        return "ok"

    with database_request_scope(reuse_connections=True):
        parent = connect_database(config.database_url)
        assert await asyncio.to_thread(_database_operation, operation) == "ok"
        with pytest.raises(RuntimeError, match="controlled failure"):
            await asyncio.to_thread(_database_operation, operation, fail=True)
        assert await asyncio.to_thread(_database_operation, operation) == "ok"
        assert all(handle is handles[0] for handle in handles)
        assert all(handle is not parent._connection for handle in handles)
        assert all(thread != main_thread for thread in threads)
        assert parent.execute("SELECT count(*) FROM scheduler_commit_probes").fetchone()[0] == 0


@pytest.mark.anyio
async def test_slow_observation_cannot_delay_renewal_or_accumulate_snapshots(tmp_path, monkeypatch):
    config = _config(_database(tmp_path))
    app = FastAPI()
    app.state.config = config
    started, release = threading.Event(), threading.Event()
    original_write = scheduler_module._write_observation
    snapshots = []
    child_events = []

    def slow_observation(*args, **kwargs):
        snapshots.append((args[2].persisted_count, args[3], threading.current_thread().name))
        if len(snapshots) == 1:
            started.set()
            if not release.wait(8):
                raise AssertionError("observation was not released")
            assert args[2].persisted_count == snapshots[0][0]
        return original_write(*args, **kwargs)

    @asynccontextmanager
    async def child(_app):
        child_events.append("start")
        try:
            yield
        finally:
            child_events.append("stop")

    monkeypatch.setattr(scheduler_module, "_write_observation", slow_observation)
    manager = scheduler_coordinator_lifespan(app, (
        SchedulerContextBinding("status-publication", child),
    ), replica_code="observation-isolation")
    await manager.__aenter__()
    try:
        await _wait_until(started.is_set)
        first = app.state.scheduler_registry_leases["status-publication"]
        # Cross the original lease deadline while the observation is still pending.
        await asyncio.sleep(3.2)
        current = app.state.scheduler_registry_leases["status-publication"]
        assert datetime.now(UTC) > first.deadline
        assert current.deadline > datetime.now(UTC)
        assert current.generation == first.generation
        assert child_events == ["start"]
        assert len(snapshots) == 1
        assert snapshots[0][2].startswith("scheduler-observation-db")
        with connect_database(config.database_url) as connection:
            scheduler_module.require_scheduler_fences(connection, config, (current,))
    finally:
        release.set()
        await manager.__aexit__(None, None, None)
    assert child_events == ["start", "stop"]
    assert snapshots[-1][1] == 0
    with connect_database(config.database_url) as connection:
        row = connection.execute("SELECT * FROM runtime_surface_observations").fetchone()
        assert row["queued_count"] == row["dropped_count"] == row["backend_failure_count"] == 0
        assert row["persisted_count"] > snapshots[0][0]


@pytest.mark.anyio
async def test_observation_failure_counts_once_without_mutating_worker_snapshot(monkeypatch):
    app = FastAPI()
    app.state.config = SimpleNamespace(control_background_schedulers_enabled=False)
    stats = scheduler_module._CoordinatorStats(accepted_count=2)

    async def fail(_app, _operation, _config, _replica, snapshot, count, *, observed_at):
        assert snapshot is not stats and count == 0
        assert isinstance(observed_at, datetime)
        stats.accepted_count += 1
        assert snapshot.accepted_count == 2
        raise ApiError("console_migration.dependency_unavailable")

    monkeypatch.setattr(scheduler_module, "_coordinator_database_call", fail)
    await scheduler_module._observe(app, "snapshot-owner", {}, stats)
    assert stats.accepted_count == 3
    assert stats.backend_failure_count == 1


@pytest.mark.anyio
async def test_shutdown_waits_for_cancelled_observation_worker(tmp_path, monkeypatch):
    config = _config(_database(tmp_path))
    config.console_migration_scheduler_shutdown_seconds = 0.05
    app = FastAPI()
    app.state.config = config
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def observe(*_args, **_kwargs):
        started.set()
        try:
            if not release.wait(5):
                raise AssertionError("observation was not released")
        finally:
            finished.set()

    monkeypatch.setattr(scheduler_module, "_write_observation", observe)
    manager = scheduler_coordinator_lifespan(app, (), replica_code="cancel-observation")
    await manager.__aenter__()
    closing = None
    try:
        await _wait_until(started.is_set)
        closing = asyncio.create_task(manager.__aexit__(None, None, None))
        await asyncio.sleep(0.15)
        assert not finished.is_set()
        assert not closing.done()
    finally:
        release.set()
        if closing is None:
            closing = asyncio.create_task(manager.__aexit__(None, None, None))
        await asyncio.wait_for(closing, timeout=2)
    assert finished.is_set()
    assert app.state.scheduler_database_executor is None
    assert app.state.scheduler_observation_executor is None


def test_observation_persists_sample_time_not_completion_time(tmp_path):
    config = _config(_database(tmp_path))
    sampled_at = datetime(2026, 9, 9, 0, 0, tzinfo=UTC)
    scheduler_module._write_observation(
        config, "delayed-snapshot", scheduler_module._CoordinatorStats(), 0,
        observed_at=sampled_at,
    )
    with connect_database(config.database_url) as connection:
        row = connection.execute("SELECT observed_at FROM runtime_surface_observations").fetchone()
        assert row[0] == "2026-09-09T00:00:00Z"


@pytest.mark.anyio
async def test_coordinator_checks_renewals_between_context_installations(monkeypatch):
    events = []
    app = FastAPI()
    app.state.config = SimpleNamespace()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    async def renew(*_args):
        events.append("renew")

    def claim(_config, **kwargs):
        events.append(kwargs["scheduler_code"])
        if kwargs["scheduler_code"] == "admin-assistant":
            loop.call_soon_threadsafe(stop.set)
        return None

    async def observe(*_args):
        return None

    monkeypatch.setattr(scheduler_module, "_renew_active", renew)
    monkeypatch.setattr(scheduler_module, "claim_scheduler_lease", claim)
    monkeypatch.setattr(scheduler_module, "_observe", observe)
    await scheduler_module._coordinator_loop(app, (
        SchedulerContextBinding("status-publication", None),
        SchedulerContextBinding("admin-assistant", None),
    ), "test-replica", stop)
    assert events == ["renew", "renew", "status-publication", "renew", "admin-assistant"]


@pytest.mark.anyio
async def test_expired_context_cleanup_does_not_delay_other_lease_renewals(monkeypatch):
    events = []
    app = FastAPI()
    app.state.config = SimpleNamespace()
    deadline = datetime.now(UTC) + timedelta(seconds=1)
    active = {}
    for code in ("status-publication", "admin-assistant"):
        queue = SCHEDULER_QUEUES_BY_SCHEDULER[code][0]
        lease = SimpleNamespace(scheduler_code=code, queue_code=queue.code,
            partition_code=queue.partition_code, deadline=deadline)
        active[(code, queue.code)] = SimpleNamespace(leases=(lease,))

    def renew(_config, groups, **_kwargs):
        events.extend(f"renew:{leases[0].scheduler_code}" for leases in groups)
        return tuple(None if leases[0].scheduler_code == "status-publication" else leases
                     for leases in groups)

    async def stop(_app, _replica, _active, key, _stats):
        events.append(f"stop:{key[0]}")

    monkeypatch.setattr(scheduler_module, "_renew_scheduler_lease_batch", renew, raising=False)
    monkeypatch.setattr(scheduler_module, "_stop_active", stop)
    await scheduler_module._renew_active(app, "test-replica", active,
        scheduler_module._CoordinatorStats(), 15)
    assert events == ["renew:status-publication", "renew:admin-assistant", "stop:status-publication"]


@pytest.mark.anyio
async def test_transient_renewal_database_failure_keeps_unexpired_context(monkeypatch):
    app = FastAPI()
    app.state.config = SimpleNamespace()
    queue = SCHEDULER_QUEUES_BY_SCHEDULER["status-publication"][0]
    lease = SimpleNamespace(
        scheduler_code="status-publication",
        queue_code=queue.code,
        partition_code=queue.partition_code,
        deadline=datetime.now(UTC) + timedelta(seconds=2),
    )
    active = {
        ("status-publication", queue.code): SimpleNamespace(leases=(lease,))
    }

    clock = [10.0]
    attempts = []
    recovered = SimpleNamespace(**vars(lease))
    recovered.deadline += timedelta(seconds=15)

    async def transient_failure(*_args, **_kwargs):
        attempts.append(clock[0])
        if len(attempts) == 1:
            raise ApiError("console_migration.dependency_unavailable")
        return ((recovered,),)

    monkeypatch.setattr(scheduler_module, "_coordinator_database_call", transient_failure)
    monkeypatch.setattr(scheduler_module, "monotonic", lambda: clock[0])
    stats = scheduler_module._CoordinatorStats()
    await scheduler_module._renew_active(app, "test-replica", active, stats, 15)

    assert tuple(active) == (("status-publication", queue.code),)
    assert stats.backend_failure_count == 1
    assert stats.dropped_count == 0
    # Calls between other context installations must not create a retry storm.
    for _ in range(37):
        await scheduler_module._renew_active(app, "test-replica", active, stats, 15)
    assert attempts == [10.0]
    clock[0] = 11.0
    await scheduler_module._renew_active(app, "test-replica", active, stats, 15)
    assert attempts == [10.0, 11.0]
    assert active[("status-publication", queue.code)].leases == (recovered,)
    assert stats.persisted_count == 1
    assert stats.dropped_count == 0


@pytest.mark.anyio
@pytest.mark.parametrize("error_code,remaining", [
    ("console_migration.dependency_unavailable", -1),
    ("console_migration.registry_drift", 2),
    ("console_migration.contract_not_ready", 2),
])
async def test_renewal_failure_stops_expired_or_invalid_authority(
    monkeypatch, error_code, remaining,
):
    app = FastAPI()
    app.state.config = SimpleNamespace()
    queue = SCHEDULER_QUEUES_BY_SCHEDULER["status-publication"][0]
    lease = SimpleNamespace(
        scheduler_code="status-publication", queue_code=queue.code,
        partition_code=queue.partition_code,
        deadline=datetime.now(UTC) + timedelta(seconds=remaining),
    )
    key = (lease.scheduler_code, lease.queue_code)
    active = {key: SimpleNamespace(leases=(lease,))}
    stopped = []

    async def failed(*_args, **_kwargs):
        return (ApiError(error_code),)

    async def stop(_app, _replica, current, expired_key, _stats):
        stopped.append(current.pop(expired_key))

    monkeypatch.setattr(scheduler_module, "_coordinator_database_call", failed)
    monkeypatch.setattr(scheduler_module, "_stop_active", stop)
    stats = scheduler_module._CoordinatorStats()
    await scheduler_module._renew_active(app, "test-replica", active, stats, 15)
    assert len(stopped) == 1 and active == {}
    assert stats.dropped_count == stats.backend_failure_count == 1


def test_registry_connection_setup_failure_closes_handle(tmp_path, monkeypatch):
    config = _config(_database(tmp_path))
    connection = scheduler_module._connect(config)
    monkeypatch.setattr(scheduler_module, "_connect", lambda _config: connection)

    def fail(_connection):
        raise sqlite3.OperationalError("controlled setup failure")

    monkeypatch.setattr(scheduler_module, "_configure_coordinator_connection", fail)
    with pytest.raises(ApiError) as captured:
        claim_scheduler_leases(config, scheduler_code="status-publication",
                               replica_code="setup-failure")
    assert captured.value.code == "console_migration.dependency_unavailable"
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


@pytest.mark.anyio
async def test_full_coordinator_renewal_uses_one_write_transaction(tmp_path, monkeypatch):
    config = _config(_database(tmp_path, None))
    config.control_background_schedulers_enabled = True
    config.gateway_placement_enabled = True
    app = FastAPI()
    app.state.config = config
    active = {}
    for definition in SCHEDULER_MANIFEST:
        leases = claim_scheduler_leases(config, scheduler_code=definition.code,
            replica_code="batch-owner", lease_seconds=120)
        assert leases is not None
        for lease in leases:
            active[(lease.scheduler_code, lease.queue_code)] = SimpleNamespace(leases=(lease,))
    # One due proof triggers a single coalesced round, including later deadlines.
    first = next(iter(active.values()))
    first.leases = (replace(first.leases[0], deadline=datetime.now(UTC)),)
    statements = []
    original_connect = scheduler_module._connect

    def traced_connect(current_config):
        connection = original_connect(current_config)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(scheduler_module, "_connect", traced_connect)
    stats = scheduler_module._CoordinatorStats()
    await scheduler_module._renew_active(app, "batch-owner", active, stats, 120)
    assert len(active) == len(SCHEDULER_QUEUE_MANIFEST)
    assert stats.persisted_count == len(active)
    assert stats.dropped_count == stats.backend_failure_count == 0
    assert statements.count("BEGIN IMMEDIATE") == 1
    assert statements.count("COMMIT") == 1
    deadlines = [child.leases[0].deadline for child in active.values()]
    assert (max(deadlines) - min(deadlines)).total_seconds() <= 1
    await scheduler_module._renew_active(app, "batch-owner", active, stats, 120)
    assert statements.count("BEGIN IMMEDIATE") == 1


def _diagnostic_payloads(caplog):
    formatter = JsonFormatter()
    return [json.loads(formatter.format(record)) for record in caplog.records
            if record.name == scheduler_module.logger.name]


@pytest.mark.parametrize("stage,sqlite_code", [
    ("write", sqlite3.SQLITE_CONSTRAINT_TRIGGER),
    ("commit", sqlite3.SQLITE_CONSTRAINT_FOREIGNKEY),
])
def test_observation_diagnostic_records_real_constraint_stage_without_details(
    tmp_path, monkeypatch, caplog, stage, sqlite_code,
):
    database = _database(tmp_path)
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys=ON")
    if stage == "write":
        connection.executescript("""
            CREATE TRIGGER observation_failure BEFORE INSERT ON runtime_surface_observations
            BEGIN SELECT RAISE(ABORT, 'private-observation-error-sentinel'); END;
        """)
    else:
        connection.executescript("""
            CREATE TABLE diagnostic_parent(id INTEGER PRIMARY KEY);
            CREATE TABLE diagnostic_child(parent_id INTEGER REFERENCES diagnostic_parent(id)
                DEFERRABLE INITIALLY DEFERRED);
            CREATE TRIGGER observation_failure AFTER INSERT ON runtime_surface_observations
            BEGIN INSERT INTO diagnostic_child VALUES(1); END;
        """)
    monkeypatch.setattr(scheduler_module, "_connect", lambda _config: connection)
    with caplog.at_level(logging.WARNING, logger=scheduler_module.logger.name):
        with pytest.raises(ApiError) as error:
            scheduler_module._write_observation(
                _config(database), "private-replica-sentinel",
                scheduler_module._CoordinatorStats(), 0,
            )
    assert error.value.code == "console_migration.dependency_unavailable"
    entries = _diagnostic_payloads(caplog)
    assert len(entries) == 1
    assert entries[0]["payload"] == {
        "operation": "observe", "stage": stage, "sqlite_errorcode": sqlite_code,
    }
    assert entries[0]["event"] == "console_migration.scheduler_observation_failed"
    assert entries[0]["latency_ms"] >= 0
    encoded = json.dumps(entries)
    assert "private-" not in encoded
    assert "INSERT" not in encoded and "TRIGGER" not in encoded
    with sqlite3.connect(database) as check:
        assert check.execute("SELECT count(*) FROM runtime_surface_observations").fetchone()[0] == 0
        if stage == "commit":
            assert check.execute("SELECT count(*) FROM diagnostic_child").fetchone()[0] == 0


def test_observation_diagnostic_distinguishes_real_writer_wait(tmp_path, monkeypatch, caplog):
    database = _database(tmp_path)
    blocker = sqlite3.connect(database)
    blocker.execute("BEGIN IMMEDIATE")
    monkeypatch.setattr(scheduler_module, "_connect", lambda _config:
                        sqlite3.connect(database, timeout=0.02))
    try:
        with caplog.at_level(logging.WARNING, logger=scheduler_module.logger.name):
            with pytest.raises(ApiError) as error:
                scheduler_module._write_observation(
                    _config(database), "private-replica-sentinel",
                    scheduler_module._CoordinatorStats(), 0,
                )
        assert error.value.code == "console_migration.dependency_unavailable"
        entry, = _diagnostic_payloads(caplog)
        assert entry["payload"] == {
            "operation": "observe", "stage": "begin",
            "sqlite_errorcode": sqlite3.SQLITE_BUSY,
        }
        assert entry["latency_ms"] >= 10
    finally:
        blocker.rollback()
        blocker.close()


@pytest.mark.parametrize("error", [
    sqlite3.OperationalError("private-connect-error-sentinel"),
    ApiError("console_migration.dependency_unavailable"),
])
def test_observation_connection_diagnostic_keeps_error_contract(monkeypatch, caplog, error):
    def fail(_config):
        raise error

    monkeypatch.setattr(scheduler_module, "_connect", fail)
    with caplog.at_level(logging.WARNING, logger=scheduler_module.logger.name):
        with pytest.raises(ApiError) as caught:
            scheduler_module._write_observation(
                SimpleNamespace(platform_session_secret="private-hmac-sentinel"),
                "private-replica-sentinel", scheduler_module._CoordinatorStats(), 0,
            )
    assert caught.value.code == "console_migration.dependency_unavailable"
    if isinstance(error, ApiError):
        assert caught.value is error
    entry, = _diagnostic_payloads(caplog)
    assert entry["payload"] == {
        "operation": "observe", "stage": "connect", "sqlite_errorcode": None,
    }
    assert "private-" not in json.dumps(entry)


@pytest.mark.anyio
async def test_coordinator_slow_diagnostic_includes_executor_queue_time(monkeypatch, caplog):
    from concurrent.futures import ThreadPoolExecutor

    loop = asyncio.get_running_loop()
    blocked = threading.Event()
    entered = threading.Event()
    executor = ThreadPoolExecutor(max_workers=1)
    app = FastAPI()
    app.state.scheduler_database_executor = executor
    clock = [10.0]
    monkeypatch.setattr(scheduler_module, "monotonic", lambda: clock[0])

    def occupy():
        entered.set()
        blocked.wait(3)

    occupied = loop.run_in_executor(executor, occupy)
    assert await asyncio.to_thread(entered.wait, 1)
    try:
        with caplog.at_level(logging.WARNING, logger=scheduler_module.logger.name):
            pending = asyncio.create_task(scheduler_module._coordinator_database_call(
                app, lambda: "private-result-sentinel"))
            await asyncio.sleep(0)
            assert not pending.done()
            clock[0] = 10.75
            blocked.set()
            assert await pending == "private-result-sentinel"
        entry, = _diagnostic_payloads(caplog)
        assert entry["latency_ms"] == 750
        assert entry["payload"] == {
            "operation": "other", "stage": "complete", "sqlite_errorcode": None,
        }
        assert "private-" not in json.dumps(entry)
    finally:
        blocked.set()
        await occupied
        executor.shutdown(wait=True)


@pytest.mark.anyio
async def test_coordinator_diagnostic_preserves_operation_errors(monkeypatch, caplog):
    app = FastAPI()
    moments = iter((1.0, 2.0))
    monkeypatch.setattr(scheduler_module, "monotonic", lambda: next(moments))
    error = RuntimeError("private-operation-error-sentinel")

    def operation():
        raise error

    with caplog.at_level(logging.WARNING, logger=scheduler_module.logger.name):
        with pytest.raises(RuntimeError) as caught:
            await scheduler_module._coordinator_database_call(app, operation)
    assert caught.value is error
    entry, = _diagnostic_payloads(caplog)
    assert entry["payload"]["stage"] == "failed"
    assert "private-" not in json.dumps(entry)


@pytest.mark.anyio
async def test_coordinator_diagnostic_preserves_cancellation(monkeypatch, caplog):
    app = FastAPI()
    clock = [1.0]
    monkeypatch.setattr(scheduler_module, "monotonic", lambda: clock[0])
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def operation():
        entered.set()
        release.wait(3)
        finished.set()

    try:
        with caplog.at_level(logging.WARNING, logger=scheduler_module.logger.name):
            pending = asyncio.create_task(
                scheduler_module._coordinator_database_call(app, operation))
            assert await asyncio.to_thread(entered.wait, 1)
            clock[0] = 2.0
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        entry, = _diagnostic_payloads(caplog)
        assert entry["payload"]["stage"] == "cancelled"
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 1)


@pytest.mark.anyio
async def test_fast_coordinator_calls_do_not_emit_diagnostics(monkeypatch, caplog):
    monkeypatch.setattr(scheduler_module, "monotonic", lambda: 10.0)
    with caplog.at_level(logging.WARNING, logger=scheduler_module.logger.name):
        assert await scheduler_module._coordinator_database_call(FastAPI(), lambda: 1) == 1
    assert _diagnostic_payloads(caplog) == []


@pytest.mark.parametrize("invalid", ["owner", "token", "generation", "expired", "registry", "contract"])
def test_batch_renewal_isolates_invalid_groups_and_preserves_atomicity(tmp_path, invalid):
    config = _config(_database(tmp_path, None))
    config.control_background_schedulers_enabled = True
    good = claim_scheduler_leases(config, scheduler_code="status-publication",
        replica_code="batch-owner", lease_seconds=120)
    bad = claim_scheduler_leases(config, scheduler_code="platform-customer-success",
        replica_code="batch-owner", lease_seconds=120)
    assert good and bad and len(bad) == 4
    connection = sqlite3.connect(config.database_url.removeprefix("sqlite:///"))
    try:
        last = bad[-1]
        if invalid == "registry":
            bad = tuple(replace(lease, registry_generation=0) for lease in bad)
        elif invalid == "contract":
            connection.execute("UPDATE scheduler_registry_entries SET enabled=0 WHERE scheduler_code=?",
                (last.scheduler_code,))
        else:
            assignments = {"owner": "lease_owner_pseudonym='revoked'",
                "token": "lease_token_hmac='revoked'", "generation": "fencing_generation=99",
                "expired": "lease_deadline_at='2000-01-01T00:00:00Z'"}
            connection.execute("UPDATE scheduler_leases SET " + assignments[invalid]
                + " WHERE scheduler_code=? AND partition_code=?", (last.scheduler_code, last.partition_code))
        connection.commit()
        results = scheduler_module._renew_scheduler_lease_batch(config, (bad, good),
            replica_code="batch-owner", lease_seconds=120)
        assert results[0] is None or isinstance(results[0], ApiError)
        assert results[1] is not None and not isinstance(results[1], ApiError)
        assert connection.execute("SELECT sum(revision) FROM scheduler_leases WHERE scheduler_code=?",
            (bad[0].scheduler_code,)).fetchone()[0] == len(bad)
        assert connection.execute("SELECT revision FROM scheduler_leases WHERE scheduler_code=?",
            (good[0].scheduler_code,)).fetchone()[0] == 2
    finally:
        connection.close()
    if invalid in {"token", "generation", "expired", "registry"}:
        guarded = connect_database(config.database_url)
        try:
            with pytest.raises(ApiError):
                scheduler_module.require_scheduler_fences(guarded, config, bad)
        finally:
            guarded.close()


def test_batch_renewal_rejects_duplicate_and_unbounded_proofs(tmp_path):
    config = _config(_database(tmp_path))
    leases = claim_scheduler_leases(config, scheduler_code="status-publication",
        replica_code="batch-owner", lease_seconds=120)
    for groups in ((leases, leases), ((),), (leases,) * (len(SCHEDULER_QUEUE_MANIFEST) + 1)):
        with pytest.raises(ApiError) as captured:
            scheduler_module._renew_scheduler_lease_batch(config, groups, replica_code="batch-owner")
        assert captured.value.code == "console_migration.contract_not_ready"


def test_public_multi_queue_renewal_rolls_back_partial_success(tmp_path):
    config = _config(_database(tmp_path, "platform-customer-success"))
    config.control_background_schedulers_enabled = True
    leases = claim_scheduler_leases(config, scheduler_code="platform-customer-success",
        replica_code="batch-owner", lease_seconds=120)
    invalid = leases[:-1] + (replace(leases[-1], token="invalid"),)
    assert renew_scheduler_leases(config, invalid, replica_code="batch-owner") is None
    connection = sqlite3.connect(config.database_url.removeprefix("sqlite:///"))
    try:
        assert connection.execute("SELECT sum(revision) FROM scheduler_leases").fetchone()[0] == len(leases)
    finally:
        connection.close()


def test_batch_database_failure_rolls_back_all_renewals(tmp_path, monkeypatch):
    config = _config(_database(tmp_path, "platform-customer-success"))
    config.control_background_schedulers_enabled = True
    leases = claim_scheduler_leases(config, scheduler_code="platform-customer-success",
        replica_code="batch-owner", lease_seconds=120)
    original = scheduler_module._renew_leases_in_transaction
    calls = []

    def fail_second(*args, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            raise sqlite3.OperationalError("controlled database failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(scheduler_module, "_renew_leases_in_transaction", fail_second)
    with pytest.raises(ApiError) as captured:
        scheduler_module._renew_scheduler_lease_batch(config, tuple((lease,) for lease in leases),
            replica_code="batch-owner")
    assert captured.value.code == "console_migration.dependency_unavailable"
    connection = sqlite3.connect(config.database_url.removeprefix("sqlite:///"))
    try:
        assert connection.execute("SELECT sum(revision) FROM scheduler_leases").fetchone()[0] == len(leases)
        connection.execute("BEGIN IMMEDIATE")
        connection.rollback()
    finally:
        connection.close()
