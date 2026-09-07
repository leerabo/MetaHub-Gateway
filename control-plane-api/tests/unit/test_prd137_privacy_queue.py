"""Queue worker cancellation and private credential boundaries, without auth substitutes."""

from __future__ import annotations

import asyncio
import copy
import json
import secrets
import sqlite3
import threading
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from metahub_assurance_runner.privacy_queue import (
    LocalPrivacyQueue,
    PrivacyQueueSubject,
    _thread_operation,
)
from metahub_assurance_runner.probes import ProbeAccount
from metahub_assurance_runner.resources import LocalResourceInspector
from metahub_control.privacy_center import service
from metahub_control.privacy_center.crypto import encrypt_identity_export


@pytest.mark.parametrize("email,password", [
    ("not-a-fixture@example.test", "s" * 40),
    ("source-" + "a" * 32 + "@example.test", "short"),
    ("source-" + "a" * 32 + "@elsewhere.test", "s" * 40),
])
def test_account_rejects_material_outside_fixed_fixture(email, password):
    with pytest.raises(ValueError, match="invalid isolated account material"):
        ProbeAccount(email, password)


def test_account_repr_hides_credentials():
    account = ProbeAccount("source-" + "a" * 32 + "@example.test", "secret-marker" * 4)
    assert account.email not in repr(account) and account.password not in repr(account)


@pytest.mark.anyio
async def test_denied_thread_operation_does_not_start_worker():
    executed = False

    def operation():
        nonlocal executed
        executed = True

    async def denied():
        raise RuntimeError("denied")

    with pytest.raises(RuntimeError, match="denied"):
        await _thread_operation(SimpleNamespace(_check_lease=denied), operation)
    assert not executed


@pytest.mark.anyio
@pytest.mark.parametrize("withdrawal", ["cancel", "double_cancel", "guard"])
async def test_worker_is_joined_before_cancelled_or_revoked_operation_returns(withdrawal):
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    main_thread = threading.get_ident()
    allow = True

    def operation():
        assert threading.get_ident() != main_thread
        entered.set()
        try:
            assert release.wait(5)
        finally:
            finished.set()

    async def guard():
        if not allow:
            raise RuntimeError("denied")

    task = asyncio.create_task(_thread_operation(SimpleNamespace(_check_lease=guard), operation))
    try:
        async with asyncio.timeout(2):
            while not entered.is_set():
                await asyncio.sleep(0.01)
        if withdrawal in ("cancel", "double_cancel"):
            task.cancel()
        else:
            allow = False
        await asyncio.sleep(0.1)
        assert not task.done() and not finished.is_set()
        if withdrawal == "double_cancel":
            task.cancel()
            await asyncio.sleep(0.1)
            assert not task.done() and not finished.is_set()
        release.set()
        with pytest.raises(RuntimeError if withdrawal == "guard" else asyncio.CancelledError):
            await task
        assert finished.is_set()
    finally:
        release.set()
        if not task.done():
            await asyncio.gather(task, return_exceptions=True)


@pytest.fixture
def queue_material(tmp_path, monkeypatch):
    database = tmp_path / "queue.sqlite3"
    sqlite3.connect(database).close()
    config = SimpleNamespace(
        database_url=f"sqlite:///{database.as_posix()}", environment="test",
        control_background_schedulers_enabled=False,
        tenant_data_encryption_secret=secrets.token_urlsafe(48), tenant_data_encryption_version=1,
    )
    queue = LocalPrivacyQueue(config, LocalResourceInspector(
        database, tmp_path / "file-storage", secrets.token_urlsafe(48),
    ))
    subject = PrivacyQueueSubject(
        "wsp_" + "1" * 32, "ten_" + "2" * 32, "gac_" + "3" * 32, "dsr_" + "4" * 32,
    )
    owner = dict(workspace_id=subject.workspace_id, tenant_id=subject.tenant_id,
                 account_id=subject.account_id, data_subject_request_id=subject.request_id)
    fragments = {
        "identity": {"account_id": subject.account_id, "email": "queue-unit@example.test",
                     "display_name": "Private unit marker"},
        "membership": {"workspace_id": subject.workspace_id, "role": "owner", "status": "active"},
        **{domain: {"controlled_fixture": True}
           for domain in ("audit", "billing", "support", "edge", "data_governance")},
    }
    jobs, manifest = [], []
    for domain, fragment in sorted(fragments.items()):
        receipt_fields = dict(schema_version=1, request_id=subject.request_id, owner_domain=domain,
                              adapter_version=1, outcome="exported",
                              fragment_sha256=service._digest(fragment),
                              policy_generation=None, hold_generation=None)
        jobs.append({**owner, "owner_domain": domain, "adapter_version": 1,
                     "status": "completed", "outcome_code": "exported", "attempt_count": 1,
                     "revision": 3, "lease_owner_id": None, "lease_token_sha256": None,
                     "lease_expires_at": None, "completed_at": "2026-09-07T00:00:00Z",
                     "export_fragment_json": service._canonical(fragment),
                     "executor_receipt_sha256": service._digest(receipt_fields),
                     "policy_generation": None, "hold_generation": None})
        manifest.append({key: receipt_fields[key] for key in (
            "owner_domain", "adapter_version", "outcome", "fragment_sha256",
        )})
    body = service._canonical({
        "subject": {"workspace_id": subject.workspace_id, "account_id": subject.account_id},
        "owners": fragments,
    }).encode("ascii")
    export_id = "pex_" + "5" * 32
    ciphertext, nonce, artifact_sha, plain_sha = encrypt_identity_export(
        config, body, export_id=export_id, workspace_id=subject.workspace_id,
        tenant_id=subject.tenant_id, account_id=subject.account_id,
    )
    export = {**owner, "identity_export_id": export_id, "artifact_ciphertext_b64": ciphertext,
              "artifact_nonce_b64": nonce, "artifact_sha256": artifact_sha,
              "plaintext_sha256": plain_sha, "size_bytes": len(body), "receipt_status": "none",
              "owner_manifest_json": service._canonical(manifest),
              "owner_manifest_sha256": service._digest(manifest)}
    account = {"email_normalized": fragments["identity"]["email"],
               "display_name": fragments["identity"]["display_name"]}
    rows = ({**owner, "status": "ready", "request_kind": "export"}, jobs, [export], account)
    monkeypatch.setattr(LocalPrivacyQueue, "_rows", lambda _self, _subject: copy.deepcopy(rows))
    return queue, subject, rows


def test_queue_observation_checks_encrypted_artifact_and_excludes_private_material(queue_material):
    queue, subject, rows = queue_material
    observation = queue.observe(subject, "ready")
    assert all(bool(value) for _, value in observation.checks)
    report = json.dumps(asdict(observation), default=str) + repr(queue) + repr(subject)
    private_values = (
        queue.config.tenant_data_encryption_secret, subject.account_id, subject.request_id,
        subject.tenant_id, subject.workspace_id, rows[3]["email_normalized"],
        rows[3]["display_name"], rows[2][0]["artifact_ciphertext_b64"],
    )
    clean = all(value not in report for value in private_values)
    assert clean


@pytest.mark.parametrize("fault,expected", [
    ("tenant", "compound_owner_bound"), ("missing_job", "seven_owner_jobs"),
    ("second_attempt", "completed_once"), ("receipt", "owner_receipts_match"),
    ("manifest", "manifest_matches_fragments"), ("identity", "nonempty_real_identity"),
])
def test_queue_observation_detects_controlled_persisted_faults(queue_material, fault, expected):
    queue, subject, rows = queue_material
    if fault == "tenant":
        rows[1][0]["tenant_id"] = "ten_" + "9" * 32
    elif fault == "missing_job":
        rows[1].pop(0)
    elif fault == "second_attempt":
        rows[1][0]["attempt_count"] = 2
    elif fault == "receipt":
        rows[1][0]["executor_receipt_sha256"] = "9" * 64
    elif fault == "manifest":
        rows[2][0]["owner_manifest_sha256"] = "9" * 64
    else:
        rows[3]["display_name"] = "Changed subject"
    checks = dict(queue.observe(subject, "ready").checks)
    assert not checks[expected]
