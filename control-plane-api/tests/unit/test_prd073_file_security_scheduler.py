"""PRD-073 文件安全 source-outbox 调度器的启动恢复、唤醒和脱敏日志测试。"""

from __future__ import annotations

import logging
import threading

import metahub_control.file_security.scheduler as security_scheduler
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_file_security_scheduler_drains_on_start_and_wakes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """证明 Control lifespan 启动即 drain，路由 wake 可提前下一轮且停服清理状态。"""

    first_call = threading.Event()
    second_call = threading.Event()
    lock = threading.Lock()
    calls = 0

    def run_once(
        _config: object,
        *,
        stop_event: threading.Event | None = None,
    ) -> bool:
        """记录调度 tick；返回空队列，使 worker 进入可唤醒等待。"""

        assert stop_event is not None
        nonlocal calls
        with lock:
            calls += 1
            current = calls
        if current == 1:
            first_call.set()
        if current >= 2:
            second_call.set()
        return False

    monkeypatch.setattr(security_scheduler, "run_file_security_event_once", run_once)
    app = FastAPI()
    app.state.config = object()
    security_scheduler.install_file_security_event_scheduler(app)

    with TestClient(app):
        assert first_call.wait(timeout=2.0)
        # worker 可能仍在首次 tick 与 clear 之间，重复无副作用 wake 可消除该竞态。
        for _index in range(20):
            security_scheduler.wake_file_security_event_scheduler(app)
            if second_call.wait(timeout=0.05):
                break
        assert second_call.is_set()
    assert not hasattr(app.state, "file_security_source_wake_event")
    assert not hasattr(app.state, "file_security_source_wake_loop")


def test_file_security_scheduler_waits_for_cooperative_worker_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """lifespan 必须等待 to_thread 观察停服标记并退出，不能取消外层 task 后遗留写库线程。"""

    started = threading.Event()
    finished = threading.Event()

    def wait_for_stop(
        _config: object,
        *,
        stop_event: threading.Event | None = None,
    ) -> bool:
        """模拟正在同步投递的 worker；只在 scheduler 传入线程安全停服标记后返回。"""

        assert stop_event is not None
        started.set()
        assert stop_event.wait(timeout=2.0)
        finished.set()
        return False

    monkeypatch.setattr(security_scheduler, "run_file_security_event_once", wait_for_stop)
    app = FastAPI()
    app.state.config = object()
    security_scheduler.install_file_security_event_scheduler(app)

    with TestClient(app):
        assert started.wait(timeout=2.0)
        assert not finished.is_set()
    assert finished.is_set()


def test_file_security_scheduler_logs_only_fixed_unknown_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """未知异常正文可能含 Endpoint/Secret，scheduler 日志只能保留固定事件和错误码。"""

    sensitive_exception = "https://private.example/secret-token"

    def fail_once(*_args: object, **_kwargs: object) -> bool:
        """模拟含敏感正文的未知 worker 异常。"""

        # URL canary 只存在调用栈，scheduler 必须用固定错误码替代异常正文。
        raise RuntimeError(sensitive_exception)

    monkeypatch.setattr(security_scheduler, "drain_file_security_event_once", fail_once)
    with caplog.at_level(logging.WARNING):
        assert security_scheduler.run_file_security_event_once(object()) is False
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert sensitive_exception not in rendered
    assert all(
        record.__dict__.get("error_code") == "file.security_scan_unavailable"
        for record in caplog.records
    )
