"""PRD-074 Batch source events run only in the Control webhook process."""

from __future__ import annotations

import ast
import inspect
import logging
import threading
from datetime import UTC, datetime, timedelta

import metahub_control.batch_jobs.outbox as batch_outbox
import metahub_control.batch_jobs.worker_main as batch_worker_main
import metahub_control.webhooks.scheduler as webhook_scheduler
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_control_webhook_lifespan_waits_for_batch_dispatch_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    finished = threading.Event()
    pause = threading.Event()

    def wait_for_stop(
        _config: object,
        *,
        now: datetime | None = None,
        should_stop: object = None,
    ) -> bool:
        assert now is None
        assert callable(should_stop)
        started.set()
        while not should_stop():
            pause.wait(timeout=0.01)
        finished.set()
        return False

    monkeypatch.setattr(webhook_scheduler, "run_webhook_replay_once", wait_for_stop)
    app = FastAPI()
    app.state.config = object()
    webhook_scheduler.install_webhook_replay_scheduler(app)

    with TestClient(app):
        assert started.wait(timeout=2.0)
        assert not finished.is_set()
    assert finished.is_set()


def test_control_webhook_tick_drains_batch_source_before_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def drain_once(
        _config: object,
        *,
        worker_id: str,
        now: datetime | None = None,
        should_stop: object = None,
    ) -> bool:
        assert worker_id.startswith("batch-event-")
        assert now is None
        assert callable(should_stop)
        calls.append("batch")
        return True

    monkeypatch.setattr(webhook_scheduler, "drain_batch_event_once", drain_once)
    monkeypatch.setattr(
        webhook_scheduler,
        "expire_dead_letters",
        lambda _config, *, now=None: calls.append("expire") or 0,
    )
    monkeypatch.setattr(
        webhook_scheduler,
        "claim_next_replay",
        lambda _config, *, worker_id, now=None: calls.append("replay") and None,
    )

    assert webhook_scheduler.run_webhook_replay_once(
        object(),
        should_stop=lambda: False,
    )
    assert calls == ["batch", "expire", "replay"]


def test_batch_dispatch_stop_prevents_snapshot_and_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 8, tzinfo=UTC)
    claim = batch_outbox.BatchEventClaim(
        event_id="bjev_" + "1" * 32,
        tenant_id="tenant_prd074_scheduler",
        batch_job_id="bjob_" + "2" * 32,
        event_type="batch_job.created",
        actor_id="user_prd074_scheduler",
        source_request_id="req_prd074_scheduler",
        source_trace_id="trc_prd074_scheduler",
        occurred_at=now,
        payload={},
        claim_token="3" * 32,
        lease_owner_id="batch-event-test",
        dispatch_attempts=1,
        lease_expires_at=now + timedelta(seconds=90),
    )
    finished: list[str | None] = []

    def unexpected_snapshot(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("stopped dispatch must not freeze or send")

    def finish(
        _config: object,
        _claim: object,
        *,
        error_code: str | None,
        now: datetime,
    ) -> bool:
        assert now == claim.occurred_at
        finished.append(error_code)
        return True

    monkeypatch.setattr(batch_outbox, "_freeze_snapshot", unexpected_snapshot)
    monkeypatch.setattr(batch_outbox, "_finish", finish)
    assert batch_outbox.dispatch_claimed_batch_event(
        object(),
        claim,
        now=now,
        should_stop=lambda: True,
    )
    assert finished == ["webhook.delivery_unavailable"]


def test_batch_source_scheduler_logs_only_fixed_unknown_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sensitive_exception = "https://private.example/secret-token"

    def fail_once(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError(sensitive_exception)

    monkeypatch.setattr(webhook_scheduler, "drain_batch_event_once", fail_once)
    with caplog.at_level(logging.WARNING):
        assert webhook_scheduler.run_batch_event_once(object()) is False
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert sensitive_exception not in rendered
    assert all(
        record.__dict__.get("error_code") == "batch.worker_unavailable"
        for record in caplog.records
    )


def test_independent_batch_worker_has_no_outbox_dependency() -> None:
    tree = ast.parse(inspect.getsource(batch_worker_main))
    assert all(
        not (
            isinstance(node, ast.ImportFrom)
            and node.module == "outbox"
            and any(alias.name == "drain_batch_event_once" for alias in node.names)
        )
        for node in ast.walk(tree)
    )
    assert all(
        not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "drain_batch_event_once"
        )
        for node in ast.walk(tree)
    )
