"""External observation fences on an explicitly isolated PostgreSQL instance."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest
import test_platform_job_postgresql_execution as base
from metahub_control.platform_jobs import catalog, external, runtime
from metahub_shared.db import connect_database
from metahub_shared.runtime.errors import ApiError

config = base.config


def begin(config, monkeypatch):
    definition = catalog.handler_definition("platform.runtime.probe")

    def start(context, _):
        context.defer_external({"marker": "isolated-pg"}, timeout_seconds=300000)

    monkeypatch.setitem(
        catalog._HANDLERS,
        "platform.runtime.probe",
        replace(
            definition,
            handler=start,
            external_handler=lambda *_: None,
        ),
    )
    job_id = base.enqueue(config)
    assert runtime.run_platform_job_once(config, worker_code="pg-external-start")
    assert (
        base.read(config, "SELECT status FROM platform_jobs WHERE platform_job_id=?", (job_id,))[0][
            0
        ]
        == "running"
    )
    return job_id


def finish(context):
    return context.finish(
        status="succeeded", outcome="platform_job.probe_completed", value={}, stop_confirmed=True
    )


def test_postgresql_external_claim_is_exclusive(config, monkeypatch):
    job_id = begin(config, monkeypatch)
    barrier = threading.Barrier(2)

    def compete(worker):
        barrier.wait(timeout=5)
        return external.claim_external_job(config, worker_code=worker)

    with ThreadPoolExecutor(max_workers=2) as workers:
        leases = list(workers.map(compete, ["pg-external-one", "pg-external-two"]))
    owned = [lease for lease in leases if lease is not None]
    assert len(owned) == 1
    assert owned[0].job_id == job_id
    finish(external.ExternalJobContext(config, owned[0]))


def test_postgresql_external_reclaims_without_repeating_attempt(config, monkeypatch):
    job_id = begin(config, monkeypatch)
    monkeypatch.setattr(external, "RESUMPTION_SECONDS", 0.15)
    old = external.claim_external_job(config, worker_code="pg-external-old")
    time.sleep(0.2)
    monkeypatch.setattr(external, "RESUMPTION_SECONDS", 45)
    new = external.claim_external_job(config, worker_code="pg-external-new")
    assert new.resume_generation == old.resume_generation + 1
    with pytest.raises(ApiError) as rejected:
        finish(external.ExternalJobContext(config, old))
    assert rejected.value.code == "platform_job.lease_lost"
    finish(external.ExternalJobContext(config, new))
    assert (
        base.read(
            config, "SELECT attempt_count FROM platform_jobs WHERE platform_job_id=?", (job_id,)
        )[0][0]
        == 1
    )


def test_postgresql_cancel_and_stop_commit_same_root(config, monkeypatch):
    job_id = begin(config, monkeypatch)
    observer = external.ExternalJobContext(
        config,
        external.claim_external_job(
            config,
            worker_code="pg-external-cancel",
        ),
    )
    connection = connect_database(config.database_url)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = runtime._database_now(connection) + timedelta(seconds=1)
        connection.execute(
            "UPDATE platform_jobs SET status='cancel_requested',revision=revision+1,updated_at=? "
            "WHERE platform_job_id=?",
            (runtime._db_time(now), job_id),
        )
        connection.commit()
    finally:
        connection.close()
    assert observer.stop_reason() == "platform_job.cancelled"
    assert finish(observer) == "platform_job.cancelled"
    assert (
        base.read(config, "SELECT status FROM platform_jobs WHERE platform_job_id=?", (job_id,))[0][
            0
        ]
        == "cancelled"
    )
    assert (
        base.read(
            config,
            "SELECT status FROM platform_job_external_waits WHERE platform_job_id=?",
            (job_id,),
        )[0][0]
        == "completed"
    )


def test_postgresql_external_down_refuses_retained_facts(config, monkeypatch):
    begin(config, monkeypatch)
    finish(
        external.ExternalJobContext(
            config,
            external.claim_external_job(
                config,
                worker_code="pg-external-down-guard",
            ),
        )
    )
    connection = connect_database(config.database_url)
    try:
        assert (
            connection.execute("SELECT COUNT(*) FROM platform_job_external_waits").fetchone()[0] > 0
        )
        sql = (
            Path(__file__).resolve().parents[3]
            / "infra/migrations/postgresql/0158_platform_job_external_waits.down.sql"
        ).read_text()
        connection.rollback()
        with psycopg.connect(config.database_url, autocommit=True) as native:
            with pytest.raises(
                psycopg.errors.RaiseException, match="platform_external_facts_prevent_down"
            ):
                native.execute(sql, prepare=False)
        assert (
            connection.execute("SELECT COUNT(*) FROM platform_job_external_waits").fetchone()[0] > 0
        )
    finally:
        connection.close()
