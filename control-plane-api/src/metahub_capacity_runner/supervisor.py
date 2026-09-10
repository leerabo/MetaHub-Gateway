"""Single-owner background execution with durable restart and cancellation semantics."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import UTC, datetime

from filelock import FileLock, Timeout

from .chaos_measurement import measure_chaos
from .fault_execution import FaultExecutionStore
from .measurement import measure_load
from .settings import CapacityRunnerSettings
from .store import RunnerStore


class CapacityExecutor:
    def __init__(self, settings: CapacityRunnerSettings):
        self.settings = settings
        self.store = RunnerStore(settings.state_directory, max_queued_runs=settings.max_queued_runs)
        self.effects = FaultExecutionStore(self.store)
        self.stopped = threading.Event()
        self.worker: threading.Thread | None = None
        self.lock = FileLock(self.store.lock_path, timeout=0, thread_local=False)

    def start(self):
        if self.worker is not None or self.lock.is_locked:
            raise ValueError("runner.executor_busy")
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
        return self.store.submit(claims, self.settings.execution_target_digest(claims))

    def run(self):
        while not self.stopped.is_set():
            try:
                pending = self.effects.pending()
                if pending is not None:
                    if not asyncio.run(self.effects.reconcile(pending)):
                        self.stopped.wait(0.5)
                    continue
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
            measured = None
            try:
                target = self.settings.target(claims)
                if self.settings.execution_target_digest(claims) != target_sha256:
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

                def stopping(owner=claims.object_id, revision=generation):
                    return self.stopped.is_set() or self.store.stopping(owner, revision)

                def progress(count, elapsed, owner=claims.object_id, revision=generation):
                    self.store.progress(owner, revision, count, elapsed)

                outcome = asyncio.run(
                    measure_chaos(
                        claims,
                        target,
                        self.settings.fault(claims),
                        self.effects,
                        generation,
                        stopping,
                        progress,
                    )
                    if claims.operation == "chaos"
                    else measure_load(claims, target, stopping, progress)
                )
                measured = outcome
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
                        metrics=measured.metrics if measured is not None else {},
                    )
                except ValueError as exc:
                    if str(exc) == "runner.fault_stop_unconfirmed":
                        try:
                            self.effects.defer(
                                claims.object_id,
                                generation,
                                state="interrupted" if self.stopped.is_set() else "failed",
                                outcome="runner.interrupted"
                                if self.stopped.is_set()
                                else "runner.fault_stop_unconfirmed",
                                metrics=measured.metrics if measured is not None else {},
                            )
                        except (sqlite3.Error, ValueError):
                            self.stopped.set()
                    else:
                        self.stopped.set()
                except sqlite3.Error:
                    # Never take another job while ownership of this one is unresolved.
                    self.stopped.set()
