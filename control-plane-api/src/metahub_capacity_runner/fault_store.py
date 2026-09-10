"""Durable fault leases and cancellation tombstones; never stores transferred bytes."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .contracts import execution_digest
from .fault_contracts import FaultBinding, FaultLeaseCommand, FaultLeaseInfo

_APPLICATION_ID = 0x4D464C54


class FaultLeaseStore:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "fault-leases.sqlite3"
        self.lock_path = directory / "fault-proxy.lock"
        self.mutex = threading.RLock()
        self.deadlines: dict[str, float] = {}
        self.end_deadlines: dict[str, float] = {}
        with self.connection() as connection:
            app_id = connection.execute("PRAGMA application_id").fetchone()[0]
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if (
                app_id not in {0, _APPLICATION_ID}
                or version not in {0, 1}
                or (
                    app_id == 0
                    and connection.execute(
                        "SELECT 1 FROM sqlite_schema WHERE type='table' LIMIT 1"
                    ).fetchone()
                )
            ):
                raise ValueError("fault.state_invalid")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(f"PRAGMA application_id={_APPLICATION_ID}")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS fault_leases(
                    object_id TEXT PRIMARY KEY,
                    execution_sha256 TEXT NOT NULL,
                    binding_code TEXT NOT NULL,
                    binding_sha256 TEXT NOT NULL,
                    fault_code TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN(
                        'active','released','expired','interrupted')),
                    sequence INTEGER NOT NULL CHECK(sequence>=1),
                    command_sha256 TEXT NOT NULL,
                    lease_until REAL NOT NULL,
                    end_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS fault_binding_owner
                    ON fault_leases(binding_code) WHERE state='active';
                PRAGMA user_version=1;
            """)

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.path, timeout=2)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def interrupt(self):
        # Called only by the lifetime-lock owner, before listeners open/after they close.
        with self.mutex, self.connection() as connection:
            connection.execute("UPDATE fault_leases SET state='interrupted' WHERE state='active'")
            connection.commit()
            self.deadlines.clear()
            self.end_deadlines.clear()

    def _expire(self, connection, now: float):
        connection.execute(
            "UPDATE fault_leases SET state='expired' WHERE state='active' "
            "AND (lease_until<=? OR end_at<=?)",
            (now, now),
        )
        for object_id, deadline in list(self.deadlines.items()):
            if time.monotonic() >= deadline:
                connection.execute(
                    "UPDATE fault_leases SET state='expired' WHERE object_id=? AND state='active'",
                    (object_id,),
                )

    def _prune(self, connection):
        active = {
            row[0]
            for row in connection.execute("SELECT object_id FROM fault_leases WHERE state='active'")
        }
        self.deadlines = {key: value for key, value in self.deadlines.items() if key in active}
        self.end_deadlines = {
            key: value for key, value in self.end_deadlines.items() if key in active
        }

    def command(self, command: FaultLeaseCommand, binding: FaultBinding) -> FaultLeaseInfo:
        now = time.time()
        if (
            command.expires_at.timestamp() <= now
            or command.issued_at.timestamp() > now + 1
            or command.expires_at.timestamp() > now + 31
            or command.binding_code != binding.binding_code
            or not binding.accepts(command.execution)
        ):
            raise ValueError("fault.command_invalid")
        object_id = command.execution.object_id
        digest = execution_digest(command.execution)
        fingerprint = hashlib.sha256(command.model_dump_json().encode("ascii")).hexdigest()
        with self.mutex, self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire(connection, now)
            row = connection.execute(
                "SELECT * FROM fault_leases WHERE object_id=?",
                (object_id,),
            ).fetchone()
            if row is not None:
                if (row["execution_sha256"], row["binding_sha256"]) != (digest, binding.digest()):
                    raise ValueError("fault.scope_conflict")
                if command.action == "status":
                    connection.commit()
                    self._prune(connection)
                    return self._info(row)
                if command.sequence == row["sequence"] and fingerprint == row["command_sha256"]:
                    connection.commit()
                    self._prune(connection)
                    return self._info(row)
                if command.sequence <= row["sequence"]:
                    raise ValueError("fault.sequence_conflict")
                if row["state"] != "active":
                    connection.commit()
                    self._prune(connection)
                    return self._info(row)
                if object_id not in self.end_deadlines:
                    raise ValueError("fault.recovery_required")
                if command.action == "apply":
                    raise ValueError("fault.already_applied")
                end_at = row["end_at"]
            else:
                if command.action in {"renew", "status"}:
                    raise ValueError("fault.execution_not_found")
                if command.action == "apply" and command.execution.expires_at.timestamp() <= now:
                    raise ValueError("fault.start_window_expired")
                end_at = now + command.execution.duration_seconds
            state = "released" if command.action == "release" else "active"
            lease_until = min(command.expires_at.timestamp(), end_at, now + 30)
            if (
                state == "active"
                and connection.execute(
                    "SELECT 1 FROM fault_leases WHERE binding_code=? "
                    "AND state='active' AND object_id<>?",
                    (binding.binding_code, object_id),
                ).fetchone()
            ):
                raise ValueError("fault.binding_busy")
            connection.execute(
                """INSERT INTO fault_leases VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(object_id) DO UPDATE SET
                state=excluded.state,sequence=excluded.sequence,
                command_sha256=excluded.command_sha256,lease_until=excluded.lease_until""",
                (
                    object_id,
                    digest,
                    binding.binding_code,
                    binding.digest(),
                    command.execution.fault_code,
                    state,
                    command.sequence,
                    fingerprint,
                    lease_until,
                    end_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM fault_leases WHERE object_id=?", (object_id,)
            ).fetchone()
            connection.commit()
            if state == "active":
                self.end_deadlines.setdefault(
                    object_id, time.monotonic() + command.execution.duration_seconds
                )
                self.deadlines[object_id] = min(
                    self.end_deadlines[object_id],
                    time.monotonic() + max(0, lease_until - time.time()),
                )
            self._prune(connection)
            return self._info(row)

    def active_fault(self, binding_code: str) -> str | None:
        with self.mutex, self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._expire(connection, time.time())
            row = connection.execute(
                "SELECT object_id,fault_code FROM fault_leases "
                "WHERE binding_code=? AND state='active'",
                (binding_code,),
            ).fetchone()
            # A new process cannot reuse persisted active leases before startup recovery.
            if row is not None and row["object_id"] not in self.deadlines:
                raise ValueError("fault.recovery_required")
            connection.commit()
            self._prune(connection)
            return row["fault_code"] if row else None

    @staticmethod
    def _info(row) -> FaultLeaseInfo:
        return FaultLeaseInfo(
            object_id=row["object_id"],
            execution_sha256=row["execution_sha256"],
            binding_code=row["binding_code"],
            state=row["state"],
            sequence=row["sequence"],
            lease_until=datetime.fromtimestamp(row["lease_until"], UTC),
            end_at=datetime.fromtimestamp(row["end_at"], UTC),
        )
