"""Durable real restoration, cancellation and replay without touching trial data."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import replace
from uuid import uuid4

import pytest
import test_prd111_backup_consistency as base
from filelock import FileLock
from metahub_shared.infrastructure import backups as b
from metahub_shared.infrastructure import restore_execution as e
from metahub_shared.infrastructure.restore_control import RestoreControl, RestoreStopped
from pydantic import ValidationError

config = base.config
store = base.store


def prepare(config, store, *, legacy=False):
    artifact = b.create_snapshot(config, backup_type="full")
    if legacy:
        artifact = replace(artifact, consistency=None)
    backup_id = "bkp_" + uuid4().hex
    stored = b.store_backup(config, backup_id=backup_id, artifact=artifact)
    request = e.RestoreRequest(
        execution_id="rex_" + uuid4().hex,
        run_id="drr_" + uuid4().hex,
        job_id="pjob_" + uuid4().hex,
        job_generation=1,
        backup_id=backup_id,
        object_key=stored.object_key,
        manifest_sha256=stored.manifest_sha256,
        content_sha256=stored.content_sha256,
        source_watermark_sha256=hashlib.sha256(artifact.source_watermark.encode()).hexdigest(),
        schema_head=artifact.schema_head,
    )
    return request, artifact


def execute(config, request, tmp_path, **kwargs):
    return e.RestoreExecutor(tmp_path / "restore-state").execute(config, request, **kwargs)


def test_sqlite_success_retains_real_target_and_replay_never_restores(
    config, store, tmp_path, monkeypatch
):
    request, artifact = prepare(config, store)
    executor = e.RestoreExecutor(tmp_path / "restore-state")
    result = executor.execute(config, request)
    assert result.status == "verified" and result.stop_confirmed
    assert result.proof.consistency == artifact.consistency
    target = executor.targets / (request.execution_id + ".sqlite3")
    with sqlite3.connect(target) as restored:
        assert (
            restored.execute("SELECT SUM(amount_micros) FROM finops_ledger_entries").fetchone()[0]
            == 400
        )
        assert restored.execute("SELECT generation FROM config_snapshot_heads").fetchone()[0] == 7
    monkeypatch.setattr(
        e, "restore_and_verify", lambda *args, **kwargs: pytest.fail("Restore replay")
    )
    assert e.RestoreExecutor(executor.directory).execute(config, request) == result
    assert executor.result(request) == result
    executor.cancel(request)
    assert executor.result(request) == result
    journal = executor.journal.read_bytes()
    for secret in (b"sensitive-test-body", b"ten_test", b"wsp_test", b"isolated-test-secret"):
        assert secret not in journal


def test_two_executions_have_separate_retained_targets(config, store, tmp_path):
    first, _ = prepare(config, store)
    second = first.model_copy(update={"execution_id": "rex_" + uuid4().hex})
    a, c = execute(config, first, tmp_path), execute(config, second, tmp_path)
    assert a.status == c.status == "verified"
    assert a.target_sha256 != c.target_sha256
    assert len(list((tmp_path / "restore-state/targets").glob("*.sqlite3"))) == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("content_sha256", "0" * 64),
        ("schema_head", "0001"),
        ("source_watermark_sha256", "0" * 64),
        ("job_generation", 2),
    ],
)
def test_same_execution_rejects_changed_binding(config, store, tmp_path, field, value):
    request, _ = prepare(config, store)
    execute(config, request, tmp_path)
    with pytest.raises(b.BackupRuntimeError, match="^backup.restore_binding_changed$"):
        execute(config, request.model_copy(update={field: value}), tmp_path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("content_sha256", "0" * 64),
        ("schema_head", "0001"),
        ("source_watermark_sha256", "0" * 64),
    ],
)
def test_restored_values_must_match_bound_request(config, store, tmp_path, field, value):
    request, _ = prepare(config, store)
    result = execute(config, request.model_copy(update={field: value}), tmp_path)
    assert result.status == "failed" and result.proof is None and result.stop_confirmed
    assert result.outcome == "backup.restore_binding_changed"


def test_legacy_backup_cannot_become_verified_evidence(config, store, tmp_path):
    request, _ = prepare(config, store, legacy=True)
    result = execute(config, request, tmp_path)
    assert result.status == "failed" and result.proof is None


def test_existing_sqlite_target_is_never_overwritten(config, store, tmp_path):
    request, _ = prepare(config, store)
    executor = e.RestoreExecutor(tmp_path / "restore-state")
    target = executor.targets / (request.execution_id + ".sqlite3")
    target.write_bytes(b"retained-user-evidence")
    assert executor.execute(config, request).status == "failed"
    assert target.read_bytes() == b"retained-user-evidence"


def test_cancel_before_restore_creates_no_target(config, store, tmp_path):
    request, _ = prepare(config, store)
    result = execute(config, request, tmp_path, stopping=lambda: True)
    assert result.status == "cancelled" and result.stop_confirmed
    assert list((tmp_path / "restore-state/targets").iterdir()) == []


def test_durable_cancel_at_last_observation_prevents_success(config, store, tmp_path, monkeypatch):
    request, _ = prepare(config, store)
    executor = e.RestoreExecutor(tmp_path / "restore-state")
    real = e.restore_and_verify

    def cancel_after_restore(*args, **kwargs):
        proof = real(*args, **kwargs)
        executor.cancel(request)
        return proof

    monkeypatch.setattr(e, "restore_and_verify", cancel_after_restore)
    result = executor.execute(config, request)
    assert result.status == "cancelled" and result.proof is None


def test_crash_after_restore_before_result_is_interrupted_not_success(
    config, store, tmp_path, monkeypatch
):
    request, _ = prepare(config, store)
    executor = e.RestoreExecutor(tmp_path / "restore-state")
    real = e.restore_and_verify

    def crash_after_restore(*args, **kwargs):
        real(*args, **kwargs)
        raise SystemExit("simulated-process-exit")

    monkeypatch.setattr(e, "restore_and_verify", crash_after_restore)
    with pytest.raises(SystemExit):
        executor.execute(config, request)
    assert executor.result(request) is None
    target = executor.targets / (request.execution_id + ".sqlite3")
    before = target.read_bytes()
    monkeypatch.setattr(
        e, "restore_and_verify", lambda *args, **kwargs: pytest.fail("Crash replay")
    )
    result = e.RestoreExecutor(executor.directory).execute(config, request)
    assert result.status == "interrupted" and result.stop_confirmed and result.proof is None
    assert target.read_bytes() == before


def test_execution_lock_excludes_another_owner(config, store, tmp_path):
    request, _ = prepare(config, store)
    executor = e.RestoreExecutor(tmp_path / "restore-state")
    with FileLock(executor.directory / (request.execution_id + ".lock")):
        with pytest.raises(b.BackupRuntimeError, match="^backup.restore_execution_busy$"):
            executor.execute(config, request)


def test_terminal_journal_facts_cannot_be_rewritten_or_deleted(config, store, tmp_path):
    request, _ = prepare(config, store)
    executor = e.RestoreExecutor(tmp_path / "restore-state")
    executor.execute(config, request)
    with sqlite3.connect(executor.journal) as journal:
        for query in (
            "DELETE FROM restore_executions",
            "UPDATE restore_executions SET status='running'",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                journal.execute(query)


@pytest.mark.parametrize(
    "changes",
    [
        {"proof": None},
        {"stop_confirmed": False},
        {"outcome": "backup.restore_failed"},
        {"status": "failed"},
        {"status": "failed", "proof": None},
        {"status": "cancelled", "outcome": "backup.restore_cancelled"},
        {"status": "interrupted", "outcome": "backup.restore_interrupted"},
        {"status": "stop_pending", "outcome": "backup.restore_stop_unconfirmed"},
        {
            "status": "stop_pending",
            "outcome": "backup.restore_stop_unconfirmed",
            "proof": None,
        },
    ],
)
def test_result_rejects_contradictory_status_proof_and_stop(config, store, tmp_path, changes):
    request, _ = prepare(config, store)
    result = execute(config, request, tmp_path)
    assert result.status == "verified"
    with pytest.raises(ValidationError):
        e.RestoreResult.model_validate({**result.model_dump(), **changes})


@pytest.mark.parametrize(
    "field,value",
    [
        ("execution_id", "rex_" + "0" * 32),
        ("request_sha256", "0" * 64),
        ("target_sha256", "0" * 64),
        ("proof.manifest_sha256", "0" * 64),
        ("proof.content_sha256", "0" * 64),
        ("proof.source_watermark_sha256", "0" * 64),
        ("proof.schema_head", "0001"),
        ("proof", None),
        ("stop_confirmed", False),
        ("coherent_failure", None),
        ("missing_result", None),
        pytest.param("invalid_json", "sensitive-journal-corruption", id="invalid-json"),
    ],
)
def test_corrupted_result_is_rejected_on_read_replay_and_cancel(
    config, store, tmp_path, monkeypatch, field, value
):
    request, _ = prepare(config, store)
    executor = e.RestoreExecutor(tmp_path / "restore-state")
    result = executor.execute(config, request)
    assert result.status == "verified"
    document = result.model_dump(mode="json")
    if field.startswith("proof."):
        document["proof"][field.split(".")[1]] = value
    elif field == "coherent_failure":
        document.update(status="failed", outcome="backup.restore_failed", proof=None)
    else:
        document[field] = value
    encoded = json.dumps(document)
    if field == "missing_result":
        encoded = None
    elif field == "invalid_json":
        encoded = value
    with sqlite3.connect(executor.journal) as journal:
        # Model storage corruption only inside this test-owned private journal.
        journal.execute("DROP TRIGGER restore_binding_immutable")
        journal.execute("UPDATE restore_executions SET result_json=?", (encoded,))
    monkeypatch.setattr(
        e, "restore_and_verify", lambda *args, **kwargs: pytest.fail("Corrupt replay")
    )
    for operation in (
        lambda: executor.result(request),
        lambda: executor.execute(config, request),
        lambda: executor.cancel(request),
    ):
        with pytest.raises(b.BackupRuntimeError) as raised:
            operation()
        assert str(raised.value) in {
            "backup.restore_result_invalid",
            "backup.restore_binding_changed",
        }
        assert "sensitive-journal-corruption" not in str(raised.value)
    with sqlite3.connect(executor.journal) as journal:
        row = journal.execute("SELECT status,result_json FROM restore_executions").fetchone()
    assert row == ("verified", encoded)


def test_finish_rejects_another_target_or_proof_binding(config, store, tmp_path, monkeypatch):
    request, _ = prepare(config, store)
    executor = e.RestoreExecutor(tmp_path / "restore-state")
    real_restore = e.restore_and_verify
    proof = None

    def crash_after_verification(*args, **kwargs):
        nonlocal proof
        proof = e._proof(request, real_restore(*args, **kwargs))
        raise SystemExit("simulated-process-exit")

    monkeypatch.setattr(e, "restore_and_verify", crash_after_verification)
    with pytest.raises(SystemExit):
        executor.execute(config, request)
    with sqlite3.connect(executor.journal) as journal:
        target_hash = journal.execute("SELECT target_sha256 FROM restore_executions").fetchone()[0]
    for target, actual_proof in (
        ("0" * 64, proof),
        (target_hash, proof.model_copy(update={"manifest_sha256": "0" * 64})),
    ):
        with pytest.raises(b.BackupRuntimeError, match="^backup.restore_binding_changed$"):
            executor._finish(request, target, "verified", "backup.restore_verified", actual_proof)
        assert executor.result(request) is None


def test_control_cancellation_reaps_real_child_without_recording_output(monkeypatch):
    seen = []
    real = subprocess.Popen

    def track(*args, **kwargs):
        child = real(*args, **kwargs)
        seen.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", track)
    stop = threading.Event()
    timer = threading.Timer(0.3, stop.set)
    timer.start()
    try:
        with pytest.raises(RestoreStopped, match="^backup.restore_cancelled$"):
            RestoreControl(stop.is_set).run(
                [
                    sys.executable,
                    "-c",
                    "import time; print('sensitive-child-error'); time.sleep(60)",
                ],
                env=dict(os.environ),
            )
    finally:
        timer.cancel()
        timer.join()
    assert len(seen) == 1 and seen[0].poll() is not None
    assert seen[0].stdout is None and seen[0].stderr is None


def test_control_expired_deadline_never_starts_child(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Expired child"))
    with pytest.raises(RestoreStopped, match="^backup.restore_timeout$"):
        RestoreControl(lambda: False, time.monotonic() - 1).run(["pg_restore"], env={})


def test_unconfirmed_child_stop_survives_cleanup_error(config, store, tmp_path, monkeypatch):
    request, _ = prepare(config, store)
    executor = e.RestoreExecutor(tmp_path / "restore-state")
    controls = []

    class UnstoppedChild:
        def poll(self):
            return None

        def kill(self):
            raise OSError("sensitive-child-stop-error")

    def start(*args, **kwargs):
        executor.cancel(request)
        return UnstoppedChild()

    def restore(config, *, control, **kwargs):
        controls.append(control)
        try:
            control.run(["pg_restore"], env={})
        finally:
            raise RuntimeError("sensitive-cleanup-error")

    monkeypatch.setattr(subprocess, "Popen", start)
    monkeypatch.setattr(e, "restore_and_verify", restore)
    result = executor.execute(config, request)
    assert not controls[0].child_stop_confirmed
    assert result.status == "stop_pending" and not result.stop_confirmed and result.proof is None
    assert result.outcome == "backup.restore_stop_unconfirmed"
    assert executor.result(request) == result
    assert b"sensitive-" not in executor.journal.read_bytes()


def test_sqlite_progress_and_fingerprint_iteration_are_cancellable(config, store, tmp_path):
    with sqlite3.connect("source.db") as connection:
        connection.executemany(
            "INSERT INTO config_event_outbox VALUES(?,?,'pending')",
            [(str(i), "synthetic") for i in range(2000)],
        )
    request, _ = prepare(config, store)
    calls = 0

    def stop_during_read():
        nonlocal calls
        calls += 1
        return calls > 20

    result = execute(config, request, tmp_path, stopping=stop_during_read)
    assert result.status == "cancelled" and result.stop_confirmed
    assert calls > 20
