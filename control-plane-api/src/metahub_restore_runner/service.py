"""Bound background restoration; the control continuation owns durable redelivery."""

from __future__ import annotations

import math
import sqlite3
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime

from filelock import FileLock, Timeout
from metahub_shared.infrastructure.restore_dispatch import (
    RestoreDispatch,
    RestoreObservation,
    RestorePermit,
    dispatch_digest,
)
from metahub_shared.infrastructure.restore_execution import RestoreExecutor, restore_request_digest

from .settings import RestoreRunnerSettings


class RestoreService:
    def __init__(self, settings: RestoreRunnerSettings):
        self.settings = settings
        self.config, self.admin_url = settings.load_runtime()
        self.token, self.secret = settings.credentials()
        self.executor = RestoreExecutor(settings.state_directory)
        self.bindings = self.executor.directory / "dispatches.sqlite3"
        self.lock = FileLock(self.executor.directory / "service.lock", timeout=0, thread_local=False)
        self.stopped = threading.Event()
        self.guard = threading.RLock()
        self.pool: ThreadPoolExecutor | None = None
        self.pending: dict[str, Future] = {}
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS restore_dispatches(
                  execution_id TEXT PRIMARY KEY, dispatch_sha256 TEXT NOT NULL,
                  created_at TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS restore_dispatch_immutable
                BEFORE UPDATE ON restore_dispatches
                BEGIN SELECT RAISE(ABORT,'restore_dispatch_immutable'); END;
                CREATE TRIGGER IF NOT EXISTS restore_dispatch_retained
                BEFORE DELETE ON restore_dispatches
                BEGIN SELECT RAISE(ABORT,'restore_dispatch_retained'); END;
            """)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.bindings, timeout=5)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def start(self) -> None:
        with self.guard:
            if self.pool is not None or self.stopped.is_set():
                raise ValueError("restore.executor_unavailable")
            try:
                self.lock.acquire()
            except Timeout:
                raise ValueError("restore.executor_busy") from None
            try:
                self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="restore-executor")
            except BaseException:
                self.lock.release()
                raise

    def close(self) -> None:
        with self.guard:
            self.stopped.set()
            pool, self.pool = self.pool, None
        try:
            if pool is not None:
                pool.shutdown(wait=True, cancel_futures=True)
        finally:
            self.lock.release()

    def ready(self) -> bool:
        with self.guard:
            return self.pool is not None and self.lock.is_locked and not self.stopped.is_set()

    def exchange(self, permit: RestorePermit) -> RestoreObservation:
        if not self.ready():
            raise ValueError("restore.executor_unavailable")
        dispatch = permit.dispatch
        if (dispatch.target_code, dispatch.region_code, dispatch.environment_class) != (
            self.settings.target_code, self.settings.region_code, self.settings.environment_class
        ):
            raise ValueError("restore.scope_conflict")
        now = datetime.now(UTC)
        if permit.expires_at <= now or permit.issued_at.timestamp() > now.timestamp() + 5:
            raise ValueError("restore.permit_invalid")
        self._bind(dispatch, create=permit.action != "status")
        if permit.action != "status":
            self.executor.register(
                self.config, dispatch.request, postgres_admin_url=self.admin_url,
                cancel_requested=permit.action == "cancel" or dispatch.deadline_at <= now,
            )
        result = self.executor.result(dispatch.request)
        if result is None or result.status == "stop_pending":
            self._enqueue(permit)
        return RestoreObservation(
            execution_id=dispatch.request.execution_id,
            dispatch_sha256=dispatch_digest(dispatch),
            request_sha256=restore_request_digest(dispatch.request),
            result=result,
        )

    def _bind(self, dispatch: RestoreDispatch, *, create: bool) -> None:
        digest = dispatch_digest(dispatch)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT dispatch_sha256 FROM restore_dispatches WHERE execution_id=?",
                (dispatch.request.execution_id,),
            ).fetchone()
            if row is None:
                if not create:
                    raise ValueError("restore.execution_not_found")
                connection.execute(
                    "INSERT INTO restore_dispatches VALUES(?,?,?)",
                    (dispatch.request.execution_id, digest, datetime.now(UTC).isoformat()),
                )
            elif row[0] != digest:
                raise ValueError("restore.scope_conflict")

    def _enqueue(self, permit: RestorePermit) -> None:
        execution_id = permit.dispatch.request.execution_id
        with self.guard:
            if not self.ready():
                raise ValueError("restore.executor_unavailable")
            if execution_id in self.pending:
                return
            if len(self.pending) >= self.settings.max_pending:
                raise ValueError("restore.queue_full")
            future = self.pool.submit(self._execute, permit)
            self.pending[execution_id] = future
            future.add_done_callback(lambda completed: self._release(execution_id, completed))

    def _release(self, execution_id: str, future: Future) -> None:
        with self.guard:
            if self.pending.get(execution_id) is future:
                del self.pending[execution_id]

    def _execute(self, permit: RestorePermit) -> None:
        now = datetime.now(UTC)
        if permit.expires_at <= now:
            return
        dispatch = permit.dispatch
        remaining = math.ceil((dispatch.deadline_at - now).total_seconds())
        if remaining <= 0:
            self.executor.cancel(dispatch.request)
        self.executor.execute(
            self.config,
            dispatch.request,
            postgres_admin_url=self.admin_url,
            stopping=self.stopped.is_set,
            timeout_seconds=max(1, min(7200, remaining)),
        )
