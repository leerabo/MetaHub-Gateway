"""Focused execution checks against an explicitly isolated PostgreSQL baseline."""

from __future__ import annotations

import os
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from metahub_control.platform_jobs import runtime, service
from metahub_shared.db import connect_database
from metahub_shared.runtime.config import AppConfig, EnvironmentInfo
from metahub_shared.runtime.errors import ApiError


@pytest.fixture
def config():
    url = os.environ.get("METAHUB_TEST_JOB_POSTGRESQL_URL")
    if not url:
        pytest.skip("isolated PostgreSQL job-runtime database not configured")
    target = urlsplit(url)
    assert target.hostname in {"127.0.0.1", "localhost"}
    assert target.path.startswith("/metahub_s3_jobs_")
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
        database_url=url,
        auth_token_secret=secrets.token_urlsafe(32),
        cache_affinity_hmac_secret=secrets.token_urlsafe(32),
        platform_session_secret=secrets.token_urlsafe(32),
        kms_provider="test",
        kms_test_master_key=secrets.token_urlsafe(32),
    ).model_copy(update={"platform_job_lease_seconds": 10})


def enqueue(config):
    marker = "pg-execution-" + uuid4().hex
    job_id, replay = service.enqueue_platform_job(
        config,
        type_code="platform.runtime.probe",
        payload={"marker": marker},
        priority=50,
        source_code="platform.runtime.test",
        idempotency_key=marker,
        request_id=marker,
        trace_id=marker,
    )
    assert not replay
    return job_id


def read(config, query, parameters=()):
    connection = connect_database(config.database_url)
    try:
        return connection.execute(query, parameters).fetchall()
    finally:
        connection.close()


def finish(config, lease):
    assert runtime._start_platform_job(config, lease)
    runtime._complete_platform_job(config, lease, "platform_job.probe_completed")


def test_postgresql_real_heartbeat_checkpoint_and_effect(config, monkeypatch):
    job_id = enqueue(config)
    definition = runtime.handler_definition("platform.runtime.probe")
    renewed = threading.Event()
    original = runtime.heartbeat_platform_job

    def heartbeat(*args):
        result = original(*args)
        if threading.current_thread().name.startswith("platform-job-heartbeat-") and result:
            renewed.set()
        return result

    def handler(context, _payload):
        context.checkpoint({"phase": "waiting"})
        assert renewed.wait(5)
        assert context.commit_effect("pg.effect", {"verified": True})
        assert not context.commit_effect("pg.effect", {"verified": True})
        return "platform_job.probe_completed"

    monkeypatch.setattr(runtime, "heartbeat_platform_job", heartbeat)
    monkeypatch.setattr(
        runtime, "handler_definition", lambda _: replace(definition, handler=handler)
    )
    assert runtime.run_platform_job_once(config, worker_code="pg-renewal-worker")
    assert renewed.is_set()
    assert (
        read(config, "SELECT status FROM platform_jobs WHERE platform_job_id=?", (job_id,))[0][0]
        == "succeeded"
    )
    assert not any(t.name.startswith("platform-job-heartbeat-") for t in threading.enumerate())


def test_postgresql_concurrent_claims_have_one_owner(config):
    job_id = enqueue(config)
    barrier = threading.Barrier(2)

    def claim(worker):
        barrier.wait(timeout=5)
        return runtime.claim_platform_job(config, worker_code=worker)

    with ThreadPoolExecutor(max_workers=2) as executor:
        leases = list(executor.map(claim, ("pg-claim-one", "pg-claim-two")))
    owned = [lease for lease in leases if lease is not None]
    assert len(owned) == 1
    assert owned[0].job_id == job_id
    finish(config, owned[0])


def test_postgresql_wall_clock_rejects_effect_expiring_during_callback(config):
    job_id = enqueue(config)
    lease = runtime.claim_platform_job(config, worker_code="pg-expiring-worker")
    assert lease.job_id == job_id
    connection = connect_database(config.database_url)
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS s3_execution_probe(value TEXT)")
        now = runtime._database_now(connection)
        connection.execute(
            "UPDATE platform_scheduler_leases SET expires_at=? WHERE platform_job_attempt_id=?",
            (runtime._db_time(now + timedelta(milliseconds=300)), lease.attempt_id),
        )
        connection.commit()
    finally:
        connection.close()

    def apply(connection):
        before = runtime._database_now(connection)
        connection.execute("INSERT INTO s3_execution_probe VALUES('rollback-required')")
        connection.execute("SELECT pg_sleep(0.4)")
        assert (runtime._database_now(connection) - before).total_seconds() >= 0.3

    with pytest.raises(ApiError) as rejected:
        runtime.PlatformJobContext(config, lease).commit_effect("pg.late.effect", {}, apply)
    assert rejected.value.code == "platform_job.lease_lost"
    assert read(config, "SELECT COUNT(*) FROM s3_execution_probe")[0][0] == 0
    assert (
        read(
            config, "SELECT COUNT(*) FROM platform_job_effects WHERE platform_job_id=?", (job_id,)
        )[0][0]
        == 0
    )
    fresh = runtime.claim_platform_job(config, worker_code="pg-takeover-worker")
    assert fresh.job_id == job_id
    assert fresh.generation == lease.generation + 1
    assert not runtime.heartbeat_platform_job(config, lease)
    finish(config, fresh)


def test_postgresql_locked_job_is_skipped_and_heartbeat_is_bounded(config):
    job_id = enqueue(config)
    lease = runtime.claim_platform_job(config, worker_code="pg-lock-worker")
    connection = connect_database(config.database_url)
    try:
        connection.execute("BEGIN")
        runtime._live_lease(connection, config, lease)
        assert runtime.claim_platform_job(config, worker_code="pg-skip-worker") is None
        started = time.monotonic()
        assert not runtime.heartbeat_platform_job(config, lease)
        assert time.monotonic() - started < 3
        assert connection.in_transaction
        connection.rollback()
    finally:
        connection.close()
    assert runtime.heartbeat_platform_job(config, lease)
    finish(config, lease)
    assert (
        read(config, "SELECT status FROM platform_jobs WHERE platform_job_id=?", (job_id,))[0][0]
        == "succeeded"
    )
