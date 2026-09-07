"""Real owner transactions with controlled auth/time, not real-login evidence."""

from __future__ import annotations

import hashlib
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
import test_prd134_privacy_center as privacy
from httpx import ASGITransport, AsyncClient
from metahub_control.privacy_center import service
from metahub_shared.db import connect_database
from metahub_shared.runtime.errors import ApiError

base_database = privacy.base_database
runtime = privacy.runtime


async def _create(config):
    async with AsyncClient(
        transport=ASGITransport(app=privacy._app(config)), base_url="http://test"
    ) as client:
        response = await client.post(
            "/identity/data-subject-requests",
            headers=privacy._identity_headers(write=True, key=uuid4().hex),
            json={"request_kind": "export", "step_up_grant": "s" * 43, "confirmed": True},
        )
    assert response.status_code == 201
    return response.json()["request"]["data_subject_request_id"]


def _snapshot(config):
    with closing(connect_database(config.database_url)) as connection:
        return {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table}").fetchall()]
            for table in (
                "data_subject_requests", "data_subject_request_owner_jobs", "identity_exports",
                "erasure_decisions", "retention_exceptions",
            )
        }


def _fenced(config, claim):
    with pytest.raises(ApiError) as failure:
        service._process_owner_job(config, claim)
    assert failure.value.code == "privacy.worker_fenced"


@pytest.mark.anyio
async def test_claim_binding_forgery_completion_and_failure_are_noops(runtime):
    config, actor, _platform = runtime
    request_id = await _create(config)
    claim = service._claim_owner_job(config, "first-worker")
    assert isinstance(claim, service.PrivacyOwnerJobLease)
    assert (claim.workspace_id, claim.tenant_id, claim.account_id) == (
        actor.workspace_id, actor.tenant_id, actor.account_id,
    )
    assert claim.data_subject_request_id == request_id
    assert claim.attempt_count == 1 and claim.revision == 2
    assert claim.lease_token not in repr(claim)
    before = _snapshot(config)
    job = next(row for row in before["data_subject_request_owner_jobs"]
               if row["owner_job_id"] == claim.owner_job_id)
    assert job["lease_owner_id"] == claim.lease_owner_id
    assert job["lease_token_sha256"] == hashlib.sha256(claim.lease_token.encode()).hexdigest()
    assert service._time(job["lease_expires_at"]) == claim.lease_expires_at
    substitutions = {
        "owner_job_id": "poj_" + uuid4().hex,
        "workspace_id": "wsp_" + uuid4().hex,
        "tenant_id": "ten_" + uuid4().hex,
        "account_id": "acc_" + uuid4().hex,
        "data_subject_request_id": "dsr_" + uuid4().hex,
        "owner_domain": "not-the-domain",
        "adapter_version": claim.adapter_version + 1,
        "attempt_count": claim.attempt_count + 1,
        "revision": claim.revision + 1,
        "lease_owner_id": "other-worker",
        "lease_token": secrets.token_urlsafe(32),
        "lease_expires_at": claim.lease_expires_at + timedelta(seconds=1),
    }
    for name, value in substitutions.items():
        forged = replace(claim, **{name: value})
        _fenced(config, forged)
        service._fail_owner_job(config, forged, "privacy.owner_unavailable")
        assert _snapshot(config) == before, name
    _fenced(config, {"owner_job_id": claim.owner_job_id, "lease_token": claim.lease_token})
    service._process_owner_job(config, claim)
    completed = _snapshot(config)
    _fenced(config, claim)
    service._fail_owner_job(config, claim, "privacy.owner_unavailable")
    assert _snapshot(config) == completed


@pytest.mark.anyio
async def test_expiry_takeover_and_late_failure_preserve_replacement(runtime, monkeypatch):
    config, _actor, _platform = runtime
    await _create(config)
    old = service._claim_owner_job(config, "first-worker")
    monkeypatch.setattr(service, "_database_now", lambda _connection: old.lease_expires_at)
    before = _snapshot(config)
    _fenced(config, old)
    service._fail_owner_job(config, old, "privacy.owner_unavailable")
    assert _snapshot(config) == before
    replacement = service._claim_owner_job(config, "replacement-worker")
    assert replacement.owner_job_id == old.owner_job_id
    assert replacement.attempt_count == 2 and replacement.revision == old.revision + 1
    assert replacement.lease_token != old.lease_token
    acquired = _snapshot(config)
    _fenced(config, old)
    service._fail_owner_job(config, old, "privacy.owner_unavailable")
    assert _snapshot(config) == acquired
    service._process_owner_job(config, replacement)


@pytest.mark.anyio
@pytest.mark.parametrize("phase", ["adapter", "assembly", "failure", "cancelled"])
async def test_expiry_during_work_rolls_back_all_writes(runtime, monkeypatch, phase):
    config, _actor, _platform = runtime
    request_id = await _create(config)
    if phase == "assembly":
        for _ in range(6):
            service._process_owner_job(config, service._claim_owner_job(config, "assembler"))
    claim = service._claim_owner_job(config, "slow-worker")
    now = claim.lease_expires_at - timedelta(minutes=1)
    monkeypatch.setattr(service, "_database_now", lambda _connection: now)
    if phase == "cancelled":
        with closing(connect_database(config.database_url)) as connection:
            row = connection.execute(
                "SELECT updated_at FROM data_subject_requests WHERE data_subject_request_id=?",
                (request_id,),
            ).fetchone()
            stamp = service._db_time(service._changed_time(now, row["updated_at"]))
            connection.execute(
                "UPDATE data_subject_requests SET status='cancelled',revision=revision+1,"
                "terminal_reason_code='subject_cancelled',terminal_at=?,updated_at=? "
                "WHERE data_subject_request_id=?", (stamp, stamp, request_id),
            )
            connection.commit()
    function_name = {
        "adapter": "_export_fragment", "assembly": "_create_identity_export",
        "failure": "_record_owner_failure", "cancelled": "_complete_cancelled_job",
    }[phase]
    operation = getattr(service, function_name)

    def delayed(*args, **kwargs):
        nonlocal now
        result = operation(*args, **kwargs)
        now = claim.lease_expires_at
        return result

    monkeypatch.setattr(service, function_name, delayed)
    before = _snapshot(config)
    if phase == "failure":
        service._fail_owner_job(config, claim, "privacy.owner_unavailable")
    else:
        _fenced(config, claim)
    assert _snapshot(config) == before


@pytest.mark.anyio
async def test_exhausted_crash_recovery_is_bounded_and_other_request_progresses(runtime):
    config, _actor, _platform = runtime
    exhausted_request = await _create(config)
    with closing(connect_database(config.database_url)) as connection:
        service._begin_write(connection)
        now = service._database_now(connection)
        jobs = connection.execute(
            "SELECT * FROM data_subject_request_owner_jobs WHERE data_subject_request_id=?",
            (exhausted_request,),
        ).fetchall()
        for job in jobs:
            connection.execute(
                "UPDATE data_subject_request_owner_jobs SET status='running',attempt_count=8,"
                "lease_owner_id='crashed-worker',lease_token_sha256=?,lease_expires_at=?,"
                "revision=revision+1,updated_at=? WHERE owner_job_id=?",
                (hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
                 service._db_time(now - timedelta(seconds=1)),
                 service._db_time(service._changed_time(now, job["updated_at"])),
                 job["owner_job_id"]),
            )
        connection.commit()
    next_request = await _create(config)
    claim = service._claim_owner_job(config, "healthy-worker")
    assert claim is not None and claim.data_subject_request_id == next_request
    snapshot = _snapshot(config)
    old_jobs = [row for row in snapshot["data_subject_request_owner_jobs"]
                if row["data_subject_request_id"] == exhausted_request]
    assert len(old_jobs) == 7
    assert all(row["status"] == "failed" and row["attempt_count"] == 8
               and row["lease_owner_id"] is None for row in old_jobs)
    old_request = next(row for row in snapshot["data_subject_requests"]
                       if row["data_subject_request_id"] == exhausted_request)
    assert old_request["status"] == "failed"
    service._process_owner_job(config, claim)


@pytest.mark.anyio
async def test_concurrent_workers_claim_once_and_assemble_one_export(runtime):
    config, _actor, _platform = runtime
    request_id = await _create(config)

    def work(worker):
        claim = service._claim_owner_job(config, worker)
        if claim is not None:
            service._process_owner_job(config, claim)
        return claim

    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(work, (f"worker-{index}" for index in range(8))))
    acquired = [claim for claim in claims if claim is not None]
    assert len(acquired) == 7 and len({claim.owner_job_id for claim in acquired}) == 7
    snapshot = _snapshot(config)
    jobs = snapshot["data_subject_request_owner_jobs"]
    assert all(row["status"] == "completed" and row["attempt_count"] == 1 for row in jobs)
    exports = snapshot["identity_exports"]
    assert len(exports) == 1 and exports[0]["data_subject_request_id"] == request_id
    assert next(row for row in snapshot["data_subject_requests"]
                if row["data_subject_request_id"] == request_id)["status"] == "ready"


def test_database_time_advances_inside_one_transaction(runtime):
    config, _actor, _platform = runtime
    with closing(connect_database(config.database_url)) as connection:
        service._begin_write(connection)
        first = service._database_now(connection)
        time.sleep(0.03)
        second = service._database_now(connection)
        connection.rollback()
    assert second > first
