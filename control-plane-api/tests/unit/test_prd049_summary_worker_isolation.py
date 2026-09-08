"""Summary work must not queue behind unrelated default-executor jobs."""

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from contextvars import ContextVar

import pytest
from metahub_control.feedback import summary_runtime
from metahub_control.feedback.summary_runtime import FeedbackSummaryRuntime
from metahub_shared.db.connection import connect_database, database_commit_guard
from metahub_shared.runtime.errors import ApiError


def test_summary_runs_while_default_executor_is_saturated():
    async def scenario():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        entered = threading.Event()
        release = threading.Event()

        def unrelated_work():
            entered.set()
            assert release.wait(3)

        blocker = asyncio.create_task(asyncio.to_thread(unrelated_work))
        try:
            while not entered.is_set():
                await asyncio.sleep(0.001)
            runtime = FeedbackSummaryRuntime()
            assert await runtime.run(
                lambda _deadline: "summary", monotonic_deadline=time.monotonic() + 0.250,
            ) == "summary"
            assert not blocker.done()
        finally:
            release.set()
            await blocker

    asyncio.run(scenario())


def test_cancelled_http_waiter_keeps_slot_until_worker_exit():
    async def scenario():
        runtime = FeedbackSummaryRuntime()
        entered, release = threading.Event(), threading.Event()

        def operation(_deadline):
            entered.set()
            assert release.wait(3)

        first = asyncio.create_task(runtime.run(
            operation, monotonic_deadline=time.monotonic() + 2,
        ))
        try:
            while not entered.is_set():
                await asyncio.sleep(0.001)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            with pytest.raises(ApiError) as error:
                await runtime.run(
                    lambda _deadline: pytest.fail("worker overlapped"),
                    monotonic_deadline=time.monotonic() + 0.050,
                )
            assert error.value.code == "feedback.unavailable"
        finally:
            release.set()
        assert await runtime.run(
            lambda _deadline: "recovered", monotonic_deadline=time.monotonic() + 1,
        ) == "recovered"

    asyncio.run(scenario())


def test_rejected_executor_submission_returns_slot(monkeypatch):
    class UnavailableExecutor:
        def submit(self, *_args, **_kwargs):
            raise RuntimeError("executor closed")

    async def scenario():
        runtime = FeedbackSummaryRuntime()
        with monkeypatch.context() as patch:
            patch.setattr(summary_runtime, "_SUMMARY_EXECUTOR", UnavailableExecutor())
            with pytest.raises(ApiError) as error:
                await runtime.run(lambda _deadline: None,
                                  monotonic_deadline=time.monotonic() + 1)
            assert error.value.code == "feedback.unavailable"
        assert await runtime.run(
            lambda _deadline: "recovered", monotonic_deadline=time.monotonic() + 1,
        ) == "recovered"

    asyncio.run(scenario())


def test_summary_worker_inherits_real_database_commit_guard(tmp_path):
    database_url = f"sqlite:///{(tmp_path / 'guard.db').as_posix()}"
    with closing(connect_database(database_url)) as connection:
        connection.execute("CREATE TABLE guard_probe(id INTEGER)")
        connection.commit()

    def denied(_connection):
        raise ApiError("feedback.unavailable")

    def operation(_deadline):
        with closing(connect_database(database_url)) as connection:
            connection.execute("INSERT INTO guard_probe VALUES(1)")
            connection.commit()

    async def scenario():
        with database_commit_guard(denied), pytest.raises(ApiError) as error:
            await FeedbackSummaryRuntime().run(
                operation, monotonic_deadline=time.monotonic() + 1,
            )
        assert error.value.code == "feedback.unavailable"

    asyncio.run(scenario())
    with closing(connect_database(database_url)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM guard_probe").fetchone()[0] == 0


def test_summary_copies_each_call_context_without_leaking_worker_changes():
    marker = ContextVar("summary_test_context", default="outside")

    async def scenario():
        runtime = FeedbackSummaryRuntime()

        def operation(_deadline):
            value = marker.get()
            marker.set("worker-local")
            return value

        for expected in ("first", "second"):
            token = marker.set(expected)
            try:
                assert await runtime.run(
                    operation, monotonic_deadline=time.monotonic() + 1,
                ) == expected
                assert marker.get() == expected
            finally:
                marker.reset(token)
        assert marker.get() == "outside"

    asyncio.run(scenario())
