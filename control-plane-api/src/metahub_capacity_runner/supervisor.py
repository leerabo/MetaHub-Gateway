"""Single-owner background execution with durable restart and cancellation semantics."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import UTC, datetime

from filelock import FileLock, Timeout

from .measurement import measure_load
from .settings import CapacityRunnerSettings
from .store import RunnerStore


class CapacityExecutor:
    def __init__(self, settings: CapacityRunnerSettings):
        self.settings = settings
        self.store = RunnerStore(settings.state_directory, max_queued_runs=settings.max_queued_runs)
        self.stopped = threading.Event()
        self.worker: threading.Thread | None = None
        self.lock = FileLock(self.store.lock_path, timeout=0, thread_local=False)

    def start(self):
        try:
            self.lock.acquire()
        except Timeout:
            raise ValueError("runner.executor_busy") from None
        try:
            self.store.interrupted()
            self.worker = threading.Thread(target=self.run, name="capacity-executor", daemon=False)
            self.worker.start()
        except BaseException:
            self.lock.release()
            raise

    def close(self):
        self.stopped.set()
        if self.worker is not None:
            self.worker.join()
        self.lock.release()

    def submit(self, claims):
        self.store.require_not_killed(claims)
        try:
            return self.store.status(claims)
        except ValueError as exc:
            if str(exc) != "runner.execution_not_found":
                raise
        if self.worker is None or not self.worker.is_alive() or self.stopped.is_set():
            raise ValueError("runner.executor_unavailable")
        target = self.settings.target(claims)
        return self.store.submit(claims, target.digest())

    def run(self):
        while not self.stopped.is_set():
            try:
                claimed = self.store.claim()
            except sqlite3.Error:
                self.stopped.wait(0.5)
                continue
            except ValueError:
                self.stopped.set()
                return
            if claimed is None:
                self.stopped.wait(0.1)
                continue
            claims, generation, target_sha256 = claimed
            try:
                target = self.settings.target(claims)
                if target.digest() != target_sha256:
                    raise ValueError("runner.target_changed")
                if claims.expires_at <= datetime.now(UTC):
                    self.store.finish(
                        claims.object_id,
                        generation,
                        result=None,
                        state="failed",
                        outcome="runner.permit_expired",
                        metrics={},
                    )
                    continue
                outcome = asyncio.run(
                    measure_load(
                        claims,
                        target,
                        lambda owner=claims.object_id, revision=generation: (
                            self.stopped.is_set() or self.store.stopping(owner, revision)
                        ),
                        lambda count, elapsed, owner=claims.object_id, revision=generation: (
                            self.store.progress(owner, revision, count, elapsed)
                        ),
                    )
                )
                self.store.finish(
                    claims.object_id,
                    generation,
                    result=None if self.stopped.is_set() else outcome.result,
                    state="interrupted" if self.stopped.is_set() else outcome.state,
                    outcome="runner.interrupted" if self.stopped.is_set() else outcome.outcome,
                    metrics=outcome.metrics,
                )
            except Exception:
                try:
                    self.store.finish(
                        claims.object_id,
                        generation,
                        result=None,
                        state="failed",
                        outcome="runner.execution_failed",
                        metrics={},
                    )
                except (sqlite3.Error, ValueError):
                    # Never take another job while ownership of this one is unresolved.
                    self.stopped.set()
