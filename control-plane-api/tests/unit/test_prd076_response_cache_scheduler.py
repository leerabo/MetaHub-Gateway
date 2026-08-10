"""PRD-076 Response Cache source outbox scheduler 生命周期与日志边界。

该测试冻结控制面的租户、权限、迁移和恢复合同，防止前端状态替代后端校验。
"""

from __future__ import annotations

import logging
import threading
import time
from types import SimpleNamespace

import metahub_control.response_cache.scheduler as cache_scheduler
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_response_cache_scheduler_waits_for_thread_and_never_overlaps_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """停服必须等同步 attempt 收尾；重启不得与旧 lifespan 的线程重叠。"""

    started = (threading.Event(), threading.Event())
    finished = (threading.Event(), threading.Event())
    lock = threading.Lock()
    calls = 0
    active = 0
    maximum_active = 0

    def slow_attempt(_config: object, *, should_stop=None) -> bool:
        """模拟网络调用已开始，停服后仍需短暂收尾才能返回同步线程。"""

        assert should_stop is not None
        nonlocal calls, active, maximum_active
        with lock:
            current = calls
            calls += 1
            active += 1
            maximum_active = max(maximum_active, active)
        assert current < len(started)
        started[current].set()
        deadline = time.monotonic() + 2.0
        while not should_stop() and time.monotonic() < deadline:
            time.sleep(0.005)
        assert should_stop()
        time.sleep(0.1)
        with lock:
            active -= 1
        finished[current].set()
        return False

    monkeypatch.setattr(cache_scheduler, "run_response_cache_outbox_once", slow_attempt)
    # 旧实现会读取该硬上限并提前 cancel 外层 Task；新实现必须忽略这种不安全退出。
    monkeypatch.setattr(cache_scheduler, "SHUTDOWN_TIMEOUT_SECONDS", 0.01, raising=False)
    app = FastAPI()
    app.state.config = SimpleNamespace(response_cache_outbox_enabled=True)
    cache_scheduler.install_response_cache_outbox_scheduler(app)

    with TestClient(app):
        assert started[0].wait(timeout=2.0)
        assert not finished[0].is_set()
    assert finished[0].is_set()

    with TestClient(app):
        assert started[1].wait(timeout=2.0)
        assert not finished[1].is_set()
    assert finished[1].is_set()
    assert calls == 2
    assert maximum_active == 1


def test_response_cache_scheduler_logs_only_fixed_unknown_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """未知异常正文可能含 Endpoint 或 Secret，scheduler 只能记录固定错误坐标。"""

    sensitive_exception = "https://private.invalid/provider-key-canary"

    def fail_once(*_args: object, **_kwargs: object) -> bool:
        """该测试冻结控制面的租户、权限、迁移和恢复合同，防止前端状态替代后端校验。 当前定义负责 fail_once 对应步骤。"""
        # 该测试冻结控制面的租户、权限、迁移和恢复合同，防止前端状态替代后端校验。
        raise RuntimeError(sensitive_exception)

    monkeypatch.setattr(cache_scheduler, "drain_response_cache_event_once", fail_once)
    with caplog.at_level(logging.WARNING):
        assert cache_scheduler.run_response_cache_outbox_once(object()) is False
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert sensitive_exception not in rendered
    assert all(
        record.__dict__.get("error_code") == "response_cache.unavailable"
        for record in caplog.records
    )
