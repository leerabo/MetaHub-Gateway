"""Deterministic billing failures must not hot-loop against SQLite."""

from types import SimpleNamespace

from metahub_control.platform_billing import scheduler
from metahub_shared.runtime.errors import ApiError


def test_meter_projection_failure_is_backed_off(monkeypatch):
    calls = []
    clock = iter([100.0, 100.1, 100.2, 116.0, 116.1])
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(scheduler, "_METER_RETRY_AFTER", 0.0)
    monkeypatch.setattr(scheduler, "_METER_RECONCILIATION_RETRY_AFTER", 0.0)

    def fail(_config, *, worker_id):
        calls.append(worker_id)
        raise ApiError("platform_metering.reconciliation_required")

    monkeypatch.setattr(scheduler, "project_usage_once", fail)
    config = SimpleNamespace()
    assert scheduler.run_platform_meter_once(config) is False
    assert scheduler.run_platform_meter_once(config) is False
    assert scheduler.run_platform_meter_once(config) is False
    assert scheduler.run_platform_meter_once(config) is False
    assert calls == [scheduler.WORKER_ID]


def test_cycle_close_failure_is_backed_off(monkeypatch):
    calls = []
    clock = iter([200.0, 200.1, 200.2, 216.0, 216.1])
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(scheduler, "_CYCLE_RETRY_AFTER", 0.0)

    def fail(_config, *, limit):
        calls.append(limit)
        raise ApiError("platform_billing.reconciliation_required")

    monkeypatch.setattr(scheduler, "close_due_cycles", fail)
    config = SimpleNamespace()
    assert scheduler.run_platform_cycle_close_once(config) is False
    assert scheduler.run_platform_cycle_close_once(config) is False
    assert scheduler.run_platform_cycle_close_once(config) is False
    assert calls == [scheduler.CLOSE_BATCH_SIZE, scheduler.CLOSE_BATCH_SIZE]
