"""Private durable restore journal, with real targets and conservative crash recovery."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlparse

from filelock import FileLock, Timeout
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from metahub_shared.db import database_backend
from metahub_shared.runtime.config import AppConfig

from .backup_consistency import TableFingerprint
from .backups import BackupRuntimeError, RestoreVerification, restore_and_verify
from .restore_control import RestoreControl, RestoreStopped
from .restore_target import PostgresRestoreTarget

_HASH = r"^[0-9a-f]{64}$"
_TERMINAL = {"verified", "failed", "cancelled", "interrupted"}
_OUTCOMES = {
    "verified": "backup.restore_verified",
    "cancelled": "backup.restore_cancelled",
    "interrupted": "backup.restore_interrupted",
    "stop_pending": "backup.restore_stop_unconfirmed",
}
_PROOF_BINDINGS = ("manifest_sha256", "content_sha256", "source_watermark_sha256", "schema_head")


class RestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    execution_id: str = Field(pattern=r"^rex_[0-9a-f]{32}$")
    run_id: str = Field(pattern=r"^drr_[0-9a-f]{32}$")
    job_id: str = Field(pattern=r"^pjob_[0-9a-f]{32}$")
    job_generation: int = Field(ge=1)
    backup_id: str = Field(pattern=r"^bkp_[0-9a-f]{32}$")
    object_key: str = Field(pattern=r"^backups/v1/[a-z0-9_]{8}/bkp_[0-9a-f]{32}\.mhb$")
    manifest_sha256: str = Field(pattern=_HASH)
    content_sha256: str = Field(pattern=_HASH)
    source_watermark_sha256: str = Field(pattern=_HASH)
    schema_head: str = Field(pattern=r"^[0-9]{4}$")


class RestoreProof(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    manifest_sha256: str = Field(pattern=_HASH)
    content_sha256: str = Field(pattern=_HASH)
    source_watermark_sha256: str = Field(pattern=_HASH)
    schema_head: str = Field(pattern=r"^[0-9]{4}$")
    consistency: tuple[TableFingerprint, ...] = Field(min_length=4, max_length=1024)


class RestoreResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    execution_id: str = Field(pattern=r"^rex_[0-9a-f]{32}$")
    request_sha256: str = Field(pattern=_HASH)
    target_sha256: str = Field(pattern=_HASH)
    status: Literal["verified", "failed", "cancelled", "interrupted", "stop_pending"]
    outcome: str = Field(pattern=r"^backup\.[a-z_]{1,80}$")
    stop_confirmed: bool
    proof: RestoreProof | None = None

    @model_validator(mode="after")
    def coherent_result(self) -> Self:
        if (
            self.stop_confirmed != (self.status != "stop_pending")
            or (self.proof is not None) != (self.status == "verified")
            or (self.status in _OUTCOMES and self.outcome != _OUTCOMES[self.status])
            or (self.status == "failed" and self.outcome in _OUTCOMES.values())
        ):
            raise ValueError("backup.restore_result_invalid")
        return self


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def _proof(request: RestoreRequest, restored: RestoreVerification) -> RestoreProof:
    actual = RestoreProof(
        manifest_sha256=restored.manifest_sha256,
        content_sha256=restored.content_sha256,
        source_watermark_sha256=hashlib.sha256(restored.source_watermark.encode()).hexdigest(),
        schema_head=restored.schema_head,
        consistency=restored.consistency or (),
    )
    _check_proof_binding(request, actual)
    return actual


def _check_proof_binding(request: RestoreRequest, proof: RestoreProof) -> None:
    for field in _PROOF_BINDINGS:
        if getattr(proof, field) != getattr(request, field):
            raise BackupRuntimeError("backup.restore_binding_changed")


def _read_result(row, request: RestoreRequest) -> RestoreResult | None:
    if row["status"] == "running" and row["result_json"] is None:
        return None
    try:
        result = RestoreResult.model_validate_json(row["result_json"])
    except (ValidationError, TypeError):
        raise BackupRuntimeError("backup.restore_result_invalid") from None
    if (
        result.execution_id != request.execution_id
        or result.request_sha256 != row["request_sha256"]
        or result.target_sha256 != row["target_sha256"]
        or result.status != row["status"]
        or (row["cancel_requested"] and result.status not in {"cancelled", "stop_pending"})
        or (not row["started"] and result.status != "cancelled")
    ):
        raise BackupRuntimeError("backup.restore_binding_changed")
    if result.proof is not None:
        _check_proof_binding(request, result.proof)
    return result


class RestoreExecutor:
    """Run in an independent worker; callers observe the journal, not a held control DB lease."""

    def __init__(self, state_directory: Path):
        self.directory = state_directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.targets = self.directory / "targets"
        self.targets.mkdir(exist_ok=True, mode=0o700)
        if self.targets.resolve() != self.targets:
            raise BackupRuntimeError("backup.restore_target_unsafe")
        self.journal = self.directory / "restores.sqlite3"
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS restore_executions(
                  execution_id TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL,
                  target_sha256 TEXT NOT NULL, backend TEXT NOT NULL,
                  server_sha256 TEXT, target_oid INTEGER,
                  status TEXT NOT NULL CHECK(status IN
                    ('running','stop_pending','verified','failed','cancelled','interrupted')),
                  cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
                  result_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS restore_binding_immutable
                BEFORE UPDATE ON restore_executions
                WHEN NEW.execution_id<>OLD.execution_id OR NEW.request_sha256<>OLD.request_sha256
                  OR NEW.target_sha256<>OLD.target_sha256 OR NEW.backend<>OLD.backend
                  OR (OLD.server_sha256 IS NOT NULL AND NEW.server_sha256 IS NOT OLD.server_sha256)
                  OR (OLD.target_oid IS NOT NULL AND NEW.target_oid IS NOT OLD.target_oid)
                  OR OLD.status IN('verified','failed','cancelled','interrupted')
                BEGIN SELECT RAISE(ABORT,'restore_binding_immutable'); END;
                CREATE TRIGGER IF NOT EXISTS restore_fact_retained
                BEFORE DELETE ON restore_executions
                BEGIN SELECT RAISE(ABORT,'restore_fact_retained'); END;
            """)
            connection.execute("BEGIN IMMEDIATE")
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(restore_executions)")
            }
            if "started" not in columns:
                # Legacy running rows may already have external effects; never replay them.
                connection.execute(
                    "ALTER TABLE restore_executions ADD COLUMN started INTEGER NOT NULL "
                    "DEFAULT 1 CHECK(started IN (0,1))"
                )
            connection.execute("""
                CREATE TRIGGER IF NOT EXISTS restore_start_monotonic
                BEFORE UPDATE ON restore_executions
                WHEN NEW.started<OLD.started OR (OLD.started=0 AND NEW.started=1 AND
                  (OLD.status!='running' OR OLD.cancel_requested=1))
                BEGIN SELECT RAISE(ABORT,'restore_start_invalid'); END
            """)
            connection.execute("""
                CREATE TRIGGER IF NOT EXISTS restore_cancellation_monotonic
                BEFORE UPDATE ON restore_executions
                WHEN NEW.cancel_requested<OLD.cancel_requested
                BEGIN SELECT RAISE(ABORT,'restore_cancellation_immutable'); END
            """)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.journal, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def cancel(self, request: RestoreRequest) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection, request)
            if row["status"] not in _TERMINAL:
                connection.execute(
                    "UPDATE restore_executions SET cancel_requested=1 WHERE execution_id=?",
                    (request.execution_id,),
                )

    def result(self, request: RestoreRequest) -> RestoreResult | None:
        with self._connect() as connection:
            row = self._row(connection, request)
            return _read_result(row, request)

    @staticmethod
    def _row(connection, request):
        row = connection.execute(
            "SELECT * FROM restore_executions WHERE execution_id=?", (request.execution_id,)
        ).fetchone()
        if row is None:
            raise BackupRuntimeError("backup.restore_execution_not_found")
        if row["request_sha256"] != _digest(request.model_dump()):
            raise BackupRuntimeError("backup.restore_binding_changed")
        _read_result(row, request)
        return row

    def execute(
        self,
        config: AppConfig,
        request: RestoreRequest,
        *,
        postgres_admin_url: str | None = None,
        stopping: Callable[[], bool] = lambda: False,
        timeout_seconds: int = 7200,
    ) -> RestoreResult:
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 7200:
            raise BackupRuntimeError("backup.request_invalid")
        backend, target_hash, target, target_path = self._target(
            config, request, postgres_admin_url
        )
        lock = FileLock(self.directory / (request.execution_id + ".lock"), timeout=0)
        try:
            with lock:
                return self._execute_locked(
                    config,
                    request,
                    backend,
                    target_hash,
                    target,
                    target_path,
                    stopping,
                    timeout_seconds,
                )
        except Timeout:
            raise BackupRuntimeError("backup.restore_execution_busy") from None

    def register(
        self,
        config: AppConfig,
        request: RestoreRequest,
        *,
        postgres_admin_url: str | None = None,
        cancel_requested: bool = False,
    ) -> RestoreResult | None:
        """Persist admission or a cancellation tombstone before dispatching a worker."""
        if type(cancel_requested) is not bool:
            raise BackupRuntimeError("backup.request_invalid")
        backend, target_hash, _, _ = self._target(config, request, postgres_admin_url)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._register(connection, request, backend, target_hash, cancel_requested)
            return _read_result(row, request)

    def _target(self, config, request, postgres_admin_url):
        backend = database_backend(config.database_url)
        request_hash = _digest(request.model_dump())
        target_path = self.targets / (request.execution_id + ".sqlite3")
        if backend == "postgresql":
            if not postgres_admin_url:
                raise BackupRuntimeError("backup.restore_target_missing")
            target = PostgresRestoreTarget(postgres_admin_url, request.execution_id, request_hash)
            parsed = urlparse(target.database_url)
            target_hash = _digest([backend, parsed.hostname, parsed.port or 5432, parsed.path])
        elif backend == "sqlite":
            target = None
            target_hash = _digest([backend, str(target_path)])
        else:
            raise BackupRuntimeError("backup.database_unavailable")
        return backend, target_hash, target, target_path

    def _register(self, connection, request, backend, target_hash, cancel_requested=False):
        now = datetime.now(UTC).isoformat()
        connection.execute(
            """INSERT INTO restore_executions(execution_id,request_sha256,target_sha256,backend,
                 status,started,cancel_requested,created_at,updated_at)
               VALUES(?,?,?,?,'running',0,?,?,?) ON CONFLICT(execution_id) DO NOTHING""",
            (
                request.execution_id,
                _digest(request.model_dump()),
                target_hash,
                backend,
                int(cancel_requested),
                now,
                now,
            ),
        )
        row = self._row(connection, request)
        if row["target_sha256"] != target_hash or row["backend"] != backend:
            raise BackupRuntimeError("backup.restore_binding_changed")
        if cancel_requested and row["status"] not in _TERMINAL:
            connection.execute(
                "UPDATE restore_executions SET cancel_requested=1,updated_at=? "
                "WHERE execution_id=?",
                (now, request.execution_id),
            )
            row = self._row(connection, request)
        return row

    def _execute_locked(
        self, config, request, backend, target_hash, target, target_path, stopping, timeout_seconds
    ):
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._register(connection, request, backend, target_hash)
            if row["status"] in _TERMINAL:
                return _read_result(row, request)
            admitted = not row["started"]
            cancelled_before_start = admitted and row["cancel_requested"]
            if admitted and not cancelled_before_start:
                connection.execute(
                    "UPDATE restore_executions SET started=1,updated_at=? WHERE execution_id=?",
                    (datetime.now(UTC).isoformat(), request.execution_id),
                )
        if cancelled_before_start:
            return self._finish(request, target_hash, "cancelled", "backup.restore_cancelled", None)

        def should_stop():
            with self._connect() as connection:
                return bool(self._row(connection, request)["cancel_requested"]) or stopping()

        control = RestoreControl(should_stop, time.monotonic() + timeout_seconds)
        status, outcome, proof = "interrupted", "backup.restore_interrupted", None
        try:
            with target if target is not None else nullcontext():
                if target is not None:
                    self._bind_server(request, target)
                if admitted:
                    try:
                        control.check()
                        if target is not None:
                            target.create(lambda oid: self._bind_oid(request, oid))
                        configured = (
                            config
                            if target is None
                            else config.model_copy(
                                update={"backup_restore_database_url": target.database_url}
                            )
                        )
                        restored = restore_and_verify(
                            configured,
                            backup_id=request.backup_id,
                            object_key=request.object_key,
                            expected_manifest_sha256=request.manifest_sha256,
                            control=control,
                            sqlite_target=target_path if target is None else None,
                        )
                        proof = _proof(request, restored)
                        control.check()
                        status, outcome = "verified", "backup.restore_verified"
                    except (BackupRuntimeError, RestoreStopped) as exc:
                        status = "cancelled" if exc.code == "backup.restore_cancelled" else "failed"
                        outcome = exc.code
                        proof = None
                    except Exception:
                        status, outcome, proof = "failed", "backup.restore_failed", None
                if target is not None and not target.fence():
                    return self._finish(
                        request,
                        target_hash,
                        "stop_pending",
                        "backup.restore_stop_unconfirmed",
                        None,
                    )
                if not control.child_stop_confirmed or outcome == "backup.restore_stop_unconfirmed":
                    return self._finish(
                        request,
                        target_hash,
                        "stop_pending",
                        "backup.restore_stop_unconfirmed",
                        None,
                    )
                # Recheck cancellation after external effects are stopped, before recording success.
                if should_stop():
                    status, outcome, proof = "cancelled", "backup.restore_cancelled", None
                return self._finish(request, target_hash, status, outcome, proof)
        except Exception:
            # Failed observation/acquisition does not authorize a terminal state or a new restore.
            return self._finish(
                request, target_hash, "stop_pending", "backup.restore_stop_unconfirmed", None
            )

    def _bind_server(self, request, target):
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection, request)
            if row["server_sha256"] is not None and row["server_sha256"] != target.server_sha256:
                raise BackupRuntimeError("backup.restore_target_changed")
            connection.execute(
                "UPDATE restore_executions SET server_sha256=? WHERE execution_id=?",
                (target.server_sha256, request.execution_id),
            )
            target.oid = row["target_oid"]

    def _bind_oid(self, request, oid):
        with self._connect() as connection:
            self._row(connection, request)
            connection.execute(
                "UPDATE restore_executions SET target_oid=? WHERE execution_id=?",
                (oid, request.execution_id),
            )

    def _finish(self, request, target_hash, status, outcome, proof):
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection, request)
            if row["status"] in _TERMINAL or row["target_sha256"] != target_hash:
                raise BackupRuntimeError("backup.restore_binding_changed")
            if row["cancel_requested"] and status != "stop_pending":
                status, outcome, proof = "cancelled", "backup.restore_cancelled", None
            if proof is not None:
                _check_proof_binding(request, proof)
            result = RestoreResult(
                execution_id=request.execution_id,
                request_sha256=row["request_sha256"],
                target_sha256=target_hash,
                status=status,
                outcome=outcome,
                stop_confirmed=status != "stop_pending",
                proof=proof,
            )
            connection.execute(
                "UPDATE restore_executions SET status=?,result_json=?,updated_at=? "
                "WHERE execution_id=?",
                (
                    status,
                    result.model_dump_json(),
                    datetime.now(UTC).isoformat(),
                    request.execution_id,
                ),
            )
            return result


__all__ = ["RestoreExecutor", "RestoreProof", "RestoreRequest", "RestoreResult"]
