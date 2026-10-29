"""Real observation row-lock waits must not consume the scheduler renewal lane."""

import asyncio
import threading
from contextlib import asynccontextmanager, closing
from datetime import UTC, datetime

import pytest
import test_prd147_postgresql_runtime as native
from fastapi import FastAPI
from metahub_control.console_migration import scheduler
from metahub_shared.db import connect_database

pytestmark = native.pytestmark
native_runtime_template = native.native_runtime_template
native_runtime = native.native_runtime


@pytest.mark.anyio
async def test_postgresql_observation_lock_does_not_delay_scheduler_renewal(
    native_runtime, monkeypatch,
):
    config = native_runtime["config"].model_copy(update={
        "console_migration_scheduler_lease_seconds": 3.0,
        "console_migration_scheduler_poll_seconds": 0.05,
        "console_migration_scheduler_shutdown_seconds": 2.0,
    })
    app = FastAPI()
    app.state.config = config
    replica = "native-observation-isolation"
    scheduler._write_observation(
        config, replica, scheduler._CoordinatorStats(), 0,
        observed_at=datetime(2026, 9, 9, tzinfo=UTC),
    )
    waiting = threading.Event()
    observer_pids = []
    child_events = []
    original_connect = scheduler._connect

    class ObservedConnection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, sql, *args):
            if "INSERT INTO runtime_surface_observations" in sql:
                observer_pids.append(self.connection.execute(
                    "SELECT pg_backend_pid()",
                ).fetchone()[0])
                waiting.set()
            return self.connection.execute(sql, *args)

    @asynccontextmanager
    async def child(_app):
        child_events.append("start")
        try:
            yield
        finally:
            child_events.append("stop")

    monkeypatch.setattr(scheduler, "_connect", lambda current:
                        ObservedConnection(original_connect(current)))
    with closing(connect_database(config.database_url)) as blocker:
        blocker.execute("BEGIN")
        sampled = blocker.execute("SELECT observed_at FROM runtime_surface_observations "
                                  "WHERE runtime_surface_code='scheduler-lease-coordinator' "
                                  "FOR UPDATE").fetchone()
        assert sampled[0] == "2026-09-09T00:00:00Z"
        manager = scheduler.scheduler_coordinator_lifespan(app, (
            scheduler.SchedulerContextBinding("status-publication", child),
        ), replica_code=replica)
        await manager.__aenter__()
        try:
            async with asyncio.timeout(3):
                while not waiting.is_set():
                    await asyncio.sleep(0.02)
                with closing(connect_database(config.database_url)) as observer:
                    while True:
                        row = observer.execute(
                            "SELECT wait_event_type FROM pg_stat_activity WHERE pid=?",
                            (observer_pids[0],),
                        ).fetchone()
                        observer.rollback()
                        if row and row[0] == "Lock":
                            break
                        await asyncio.sleep(0.02)
            first = app.state.scheduler_registry_leases["status-publication"]
            await asyncio.sleep(3.2)
            current = app.state.scheduler_registry_leases["status-publication"]
            assert datetime.now(UTC) > first.deadline
            assert current.deadline > datetime.now(UTC)
            assert current.generation == first.generation
            assert len(observer_pids) == 1
            assert child_events == ["start"]
            with closing(connect_database(config.database_url)) as observer:
                scheduler.require_scheduler_fences(observer, config, (current,))
        finally:
            blocker.rollback()
            await manager.__aexit__(None, None, None)
    assert child_events == ["start", "stop"]
    with closing(connect_database(config.database_url)) as connection:
        row = connection.execute(
            "SELECT queued_count,dropped_count,backend_failure_count "
            "FROM runtime_surface_observations "
            "WHERE runtime_surface_code='scheduler-lease-coordinator'",
        ).fetchone()
        assert tuple(row) == (0, 0, 0)
