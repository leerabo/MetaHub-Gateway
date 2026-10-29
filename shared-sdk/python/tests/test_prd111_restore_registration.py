"""Durable registration precedes dispatch and preserves pre-start cancellation."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
import test_prd111_restore_execution as base
from metahub_shared.infrastructure import backups as b
from metahub_shared.infrastructure import restore_execution as e

config = base.config
store = base.store


def test_registration_has_no_restore_effect_and_restart_can_dispatch(
    config, store, tmp_path, monkeypatch
):
    request, _ = base.prepare(config, store)
    executor = e.RestoreExecutor(tmp_path / "registered")
    real = e.restore_and_verify
    with monkeypatch.context() as no_effect:
        no_effect.setattr(
            e, "restore_and_verify", lambda *a, **k: pytest.fail("Registration restored")
        )
        assert executor.register(config, request) is None
        assert executor.register(config, request) is None
        assert executor.result(request) is None
        assert list(executor.targets.iterdir()) == []
    seen = []

    def restore(*args, **kwargs):
        seen.append(True)
        return real(*args, **kwargs)

    monkeypatch.setattr(e, "restore_and_verify", restore)
    restarted = e.RestoreExecutor(executor.directory)
    result = restarted.execute(config, request)
    assert result.status == "verified"
    assert restarted.register(config, request) == result
    assert restarted.execute(config, request) == result
    assert seen == [True]


@pytest.mark.parametrize("cancel_with_registration", [False, True])
def test_cancel_before_dispatch_is_durable_and_late_submit_cannot_restart(
    config, store, tmp_path, monkeypatch, cancel_with_registration
):
    request, _ = base.prepare(config, store)
    executor = e.RestoreExecutor(tmp_path / "registered")
    executor.register(config, request, cancel_requested=cancel_with_registration)
    if not cancel_with_registration:
        executor.cancel(request)
    restarted = e.RestoreExecutor(executor.directory)
    assert restarted.register(config, request) is None
    monkeypatch.setattr(e, "restore_and_verify", lambda *a, **k: pytest.fail("Cancelled dispatch"))
    result = restarted.execute(config, request)
    assert result.status == "cancelled" and result.stop_confirmed and result.proof is None
    assert restarted.register(config, request) == result
    assert restarted.register(config, request, cancel_requested=True) == result
    assert restarted.execute(config, request) == result
    assert list(restarted.targets.iterdir()) == []
    with sqlite3.connect(restarted.journal) as journal:
        assert journal.execute(
            "SELECT started,cancel_requested FROM restore_executions"
        ).fetchone() == (0, 1)


def test_registration_rejects_changed_request_before_dispatch(config, store, tmp_path):
    request, _ = base.prepare(config, store)
    executor = e.RestoreExecutor(tmp_path / "registered")
    executor.register(config, request)
    other = request.model_copy(update={"job_generation": request.job_generation + 1})
    with pytest.raises(b.BackupRuntimeError, match="^backup.restore_binding_changed$"):
        executor.register(config, other, cancel_requested=True)
    assert executor.execute(config, request).status == "verified"


@pytest.mark.parametrize("cancelled", [False, True])
def test_journal_rejects_start_rewind_or_start_after_cancellation(
    config, store, tmp_path, cancelled
):
    request, _ = base.prepare(config, store)
    executor = e.RestoreExecutor(tmp_path / "registered")
    executor.register(config, request, cancel_requested=cancelled)
    with sqlite3.connect(executor.journal) as journal:
        if not cancelled:
            journal.execute("UPDATE restore_executions SET started=1")
        with pytest.raises(sqlite3.IntegrityError, match="restore_start_invalid"):
            journal.execute("UPDATE restore_executions SET started=?", (int(cancelled),))
        if cancelled:
            with pytest.raises(sqlite3.IntegrityError, match="restore_cancellation_immutable"):
                journal.execute("UPDATE restore_executions SET cancel_requested=0")


def test_legacy_journal_upgrade_never_replays_unknown_running_execution(
    config, store, tmp_path, monkeypatch
):
    request, _ = base.prepare(config, store)
    executor = e.RestoreExecutor(tmp_path / "legacy")
    real = e.restore_and_verify

    def crash(*args, **kwargs):
        real(*args, **kwargs)
        raise SystemExit("simulated-legacy-crash")

    monkeypatch.setattr(e, "restore_and_verify", crash)
    with pytest.raises(SystemExit):
        executor.execute(config, request)
    retained = executor.targets / (request.execution_id + ".sqlite3")
    original = retained.read_bytes()
    with sqlite3.connect(executor.journal) as journal:
        # Reproduce the previously committed private journal schema on test-owned data only.
        journal.execute("DROP TRIGGER restore_start_monotonic")
        journal.execute("ALTER TABLE restore_executions DROP COLUMN started")
    with ThreadPoolExecutor(max_workers=2) as workers:
        upgraded = list(workers.map(lambda _: e.RestoreExecutor(executor.directory), range(2)))
    monkeypatch.setattr(e, "restore_and_verify", lambda *a, **k: pytest.fail("Legacy replay"))
    assert upgraded[0].register(config, request) is None
    result = upgraded[1].execute(config, request)
    assert result.status == "interrupted" and result.stop_confirmed
    assert retained.read_bytes() == original
    with sqlite3.connect(executor.journal) as journal:
        assert journal.execute("SELECT started FROM restore_executions").fetchone()[0] == 1


def test_registration_cancellation_does_not_rewrite_terminal_success(config, store, tmp_path):
    request, _ = base.prepare(config, store)
    executor = e.RestoreExecutor(tmp_path / "registered")
    original = executor.execute(config, request)
    assert original.status == "verified"
    assert executor.register(config, request, cancel_requested=True) == original
    executor.cancel(request)
    assert executor.result(request) == original
