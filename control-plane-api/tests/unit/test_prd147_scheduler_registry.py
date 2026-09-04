"""PRD-147 scheduler manifest, lease, fencing, and coordinator tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
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
    claim_scheduler_lease,
    claim_scheduler_leases,
    release_scheduler_lease,
    release_scheduler_leases,
    renew_scheduler_lease,
    renew_scheduler_leases,
    scheduler_coordinator_lifespan,
)
from metahub_control.platform_jobs.supervisor import install_platform_job_supervisor
from metahub_shared.db import connect_database
from metahub_shared.runtime.errors import ApiError

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


def _database(tmp_path, scheduler_code: str = "status-publication"):
    database = tmp_path / "scheduler.sqlite3"
    definition = next(item for item in SCHEDULER_MANIFEST if item.code == scheduler_code)
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
        connection.execute(
            "INSERT INTO scheduler_registry_entries VALUES(?,1,?)",
            (definition.code, definition.manifest_digest),
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
    finally:
        await second.__aexit__(None, None, None)
        await first.__aexit__(None, None, None)

    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            """SELECT count(DISTINCT lease_owner_pseudonym)
               FROM scheduler_leases WHERE scheduler_code=?""",
            (scheduler_code,),
        ).fetchone()[0] == 2
    finally:
        connection.close()


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


async def _wait_until(predicate, *, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("timed out waiting for scheduler coordinator state")
        await asyncio.sleep(0.02)
