"""Durable runner-local execution facts, independent of Control and tenant databases."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .contracts import (
    CapacityExecutionInfo,
    CapacityPermitClaims,
    CapacityRunnerResult,
    execution_digest,
)

_APPLICATION_ID = 0x4D434150
_TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}


class RunnerStore:
    def __init__(self, directory: Path, *, max_queued_runs: int = 20):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "executions.sqlite3"
        self.lock_path = directory / "executor.lock"
        self.max_queued_runs = max_queued_runs
        with self.connection() as connection:
            application_id = connection.execute("PRAGMA application_id").fetchone()[0]
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1, 2}:
                raise ValueError("runner.state_invalid")
            if application_id not in {0, _APPLICATION_ID}:
                raise ValueError("runner.state_invalid")
            if (
                application_id == 0
                and connection.execute(
                    "SELECT 1 FROM sqlite_schema WHERE type='table' LIMIT 1"
                ).fetchone()
            ):
                raise ValueError("runner.state_invalid")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(f"PRAGMA application_id={_APPLICATION_ID}")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS executions(
                    object_id TEXT PRIMARY KEY,
                    execution_sha256 TEXT NOT NULL,
                    target_sha256 TEXT NOT NULL,
                    claims_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN(
                        'queued','running','stopping','succeeded','failed','cancelled','interrupted')),
                    generation INTEGER NOT NULL DEFAULT 0,
                    safe_outcome_code TEXT NOT NULL,
                    sample_count INTEGER NOT NULL DEFAULT 0,
                    elapsed_ms INTEGER NOT NULL DEFAULT 0,
                    metrics_json TEXT,
                    result_json TEXT,
                    stop_scope TEXT NOT NULL DEFAULT 'runner_requests',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CHECK((state='succeeded')=(result_json IS NOT NULL))
                );
                CREATE TABLE IF NOT EXISTS kill_fences(
                    object_id TEXT PRIMARY KEY,
                    generation INTEGER NOT NULL CHECK(generation>=1)
                );
            """)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(executions)")}
            if "stop_scope" not in columns:
                connection.execute(
                    "ALTER TABLE executions ADD COLUMN stop_scope TEXT NOT NULL "
                    "DEFAULT 'runner_requests'"
                )
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS fault_effects(
                    object_id TEXT PRIMARY KEY REFERENCES executions(object_id),
                    generation INTEGER NOT NULL,
                    binding_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN('prepared','reversed')),
                    terminal_state TEXT NOT NULL DEFAULT 'interrupted'
                        CHECK(terminal_state IN('cancelled','failed','interrupted')),
                    terminal_outcome TEXT NOT NULL DEFAULT 'runner.interrupted',
                    reversal_json TEXT,
                    CHECK((state='reversed')=(reversal_json IS NOT NULL))
                );
                CREATE TRIGGER IF NOT EXISTS fault_effect_owner_insert
                BEFORE INSERT ON fault_effects
                WHEN NOT EXISTS(SELECT 1 FROM executions WHERE object_id=NEW.object_id
                    AND generation=NEW.generation AND state='running')
                BEGIN SELECT RAISE(ABORT,'fault effect owner invalid'); END;
                CREATE TRIGGER IF NOT EXISTS fault_effect_immutable_owner
                BEFORE UPDATE OF object_id,generation,binding_json ON fault_effects
                WHEN NEW.object_id<>OLD.object_id OR NEW.generation<>OLD.generation
                    OR NEW.binding_json<>OLD.binding_json
                BEGIN SELECT RAISE(ABORT,'fault effect owner immutable'); END;
                CREATE TRIGGER IF NOT EXISTS fault_effect_terminal_guard
                BEFORE UPDATE OF state ON executions
                WHEN NEW.state IN('succeeded','failed','cancelled','interrupted')
                    AND EXISTS(SELECT 1 FROM fault_effects WHERE object_id=NEW.object_id
                        AND state<>'reversed')
                BEGIN SELECT RAISE(ABORT,'fault reversal unconfirmed'); END;
                PRAGMA user_version=2;
            """)

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.path, timeout=2)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def submit(self, claims: CapacityPermitClaims, target_sha256: str) -> CapacityExecutionInfo:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            fence = connection.execute(
                "SELECT generation FROM kill_fences WHERE object_id=?", (claims.object_id,)
            ).fetchone()
            if fence is not None and int(fence[0]) > claims.kill_generation:
                raise ValueError("runner.killed")
            existing = connection.execute(
                "SELECT * FROM executions WHERE object_id=?", (claims.object_id,)
            ).fetchone()
            if existing is not None:
                self._binding(existing, claims)
                connection.commit()
                return self._info(existing)
            queued = connection.execute(
                "SELECT COUNT(*) FROM executions WHERE state IN('queued','running','stopping')"
            ).fetchone()[0]
            if queued >= self.max_queued_runs:
                raise ValueError("runner.queue_full")
            connection.execute(
                """INSERT INTO executions(
                object_id,execution_sha256,target_sha256,claims_json,state,safe_outcome_code)
                VALUES(?,?,?,?,'queued','runner.queued')""",
                (
                    claims.object_id,
                    execution_digest(claims),
                    target_sha256,
                    claims.model_dump_json(),
                ),
            )
            current = connection.execute(
                "SELECT * FROM executions WHERE object_id=?", (claims.object_id,)
            ).fetchone()
            connection.commit()
            return self._info(current)

    def status(self, claims: CapacityPermitClaims) -> CapacityExecutionInfo:
        with self.connection() as connection:
            current = connection.execute(
                "SELECT * FROM executions WHERE object_id=?", (claims.object_id,)
            ).fetchone()
            if current is None:
                raise ValueError("runner.execution_not_found")
            self._binding(current, claims)
            return self._info(current)

    def require_not_killed(self, claims: CapacityPermitClaims) -> None:
        with self.connection() as connection:
            fence = connection.execute(
                "SELECT generation FROM kill_fences WHERE object_id=?", (claims.object_id,)
            ).fetchone()
            if fence is not None and fence[0] > claims.kill_generation:
                raise ValueError("runner.killed")

    def cancel(self, claims: CapacityPermitClaims) -> CapacityExecutionInfo:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM executions WHERE object_id=?", (claims.object_id,)
            ).fetchone()
            if current is None:
                # Cancel may overtake a timed-out submit on a different connection.
                connection.execute(
                    """INSERT INTO executions(
                    object_id,execution_sha256,target_sha256,claims_json,state,safe_outcome_code,
                    stop_scope) VALUES(?,?,?,?,'cancelled','runner.cancelled',?)""",
                    (
                        claims.object_id,
                        execution_digest(claims),
                        "0" * 64,
                        claims.model_dump_json(),
                        "runner_requests_and_fault"
                        if claims.operation == "chaos"
                        else "runner_requests",
                    ),
                )
                current = connection.execute(
                    "SELECT * FROM executions WHERE object_id=?", (claims.object_id,)
                ).fetchone()
            self._binding(current, claims)
            self._cancel(connection, claims.object_id)
            current = connection.execute(
                "SELECT * FROM executions WHERE object_id=?", (claims.object_id,)
            ).fetchone()
            connection.commit()
            return self._info(current)

    def kill(self, object_id: str, generation: int) -> int:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO kill_fences VALUES(?,?)
                ON CONFLICT(object_id) DO UPDATE SET generation=max(generation,excluded.generation)
                """,
                (object_id, generation),
            )
            self._cancel(connection, object_id)
            accepted = int(
                connection.execute(
                    "SELECT generation FROM kill_fences WHERE object_id=?", (object_id,)
                ).fetchone()[0]
            )
            connection.commit()
            return accepted

    def claim(self):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM executions WHERE state='queued' "
                "ORDER BY created_at,object_id LIMIT 1"
            ).fetchone()
            if current is None:
                connection.commit()
                return None
            claims = CapacityPermitClaims.model_validate_json(current["claims_json"])
            if claims.expires_at <= datetime.now(UTC):
                connection.execute(
                    """UPDATE executions SET state='failed',
                    safe_outcome_code='runner.permit_expired',updated_at=CURRENT_TIMESTAMP,
                    stop_scope=CASE WHEN object_id LIKE 'che_%' THEN 'runner_requests_and_fault'
                        ELSE 'runner_requests' END
                    WHERE object_id=?""",
                    (claims.object_id,),
                )
                connection.commit()
                return None
            connection.execute(
                """UPDATE executions SET state='running',generation=generation+1,
                safe_outcome_code='runner.running',updated_at=CURRENT_TIMESTAMP WHERE object_id=?
                """,
                (current["object_id"],),
            )
            connection.commit()
            return (
                claims,
                int(current["generation"]) + 1,
                current["target_sha256"],
            )

    def interrupted(self) -> None:
        # Only the process owning executor.lock may recover abandoned executions.
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""UPDATE fault_effects SET
                terminal_state=CASE WHEN (SELECT safe_outcome_code FROM executions
                    WHERE executions.object_id=fault_effects.object_id)='runner.stop_requested'
                    OR terminal_state='cancelled'
                    THEN 'cancelled' ELSE 'interrupted' END,
                terminal_outcome=CASE WHEN (SELECT safe_outcome_code FROM executions
                    WHERE executions.object_id=fault_effects.object_id)='runner.stop_requested'
                    OR terminal_state='cancelled'
                    THEN 'runner.cancelled' ELSE 'runner.interrupted' END
                WHERE state='prepared' AND object_id IN(
                    SELECT object_id FROM executions WHERE state IN('running','stopping'))""")
            connection.execute("""UPDATE executions SET
                state=CASE WHEN safe_outcome_code='runner.stop_requested' OR EXISTS(
                    SELECT 1 FROM fault_effects WHERE fault_effects.object_id=executions.object_id
                        AND terminal_state='cancelled') THEN 'cancelled' ELSE 'interrupted' END,
                generation=generation+1,
                safe_outcome_code=CASE WHEN safe_outcome_code='runner.stop_requested' OR EXISTS(
                    SELECT 1 FROM fault_effects WHERE fault_effects.object_id=executions.object_id
                        AND terminal_state='cancelled')
                    THEN 'runner.cancelled' ELSE 'runner.interrupted' END,
                updated_at=CURRENT_TIMESTAMP,
                stop_scope=CASE WHEN object_id LIKE 'che_%' THEN 'runner_requests_and_fault'
                    ELSE 'runner_requests' END
                WHERE state IN('running','stopping') AND NOT EXISTS(
                    SELECT 1 FROM fault_effects WHERE fault_effects.object_id=executions.object_id
                    AND state='prepared')""")
            connection.execute("""UPDATE executions SET state='stopping',generation=generation+1,
                safe_outcome_code='runner.fault_stop_pending',updated_at=CURRENT_TIMESTAMP
                WHERE state IN('running','stopping') AND EXISTS(
                    SELECT 1 FROM fault_effects WHERE fault_effects.object_id=executions.object_id
                    AND state='prepared')""")
            connection.commit()

    def stopping(self, object_id: str, generation: int) -> bool:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT state,generation FROM executions WHERE object_id=?", (object_id,)
            ).fetchone()
            return row is None or row["state"] != "running" or row["generation"] != generation

    def progress(self, object_id: str, generation: int, count: int, elapsed_ms: int) -> None:
        with self.connection() as connection:
            connection.execute(
                """UPDATE executions SET sample_count=?,elapsed_ms=?,
                updated_at=CURRENT_TIMESTAMP WHERE object_id=? AND generation=?
                AND state IN('running','stopping')""",
                (count, elapsed_ms, object_id, generation),
            )
            connection.commit()

    def finish(
        self,
        object_id: str,
        generation: int,
        *,
        result: CapacityRunnerResult | None,
        state: str,
        outcome: str,
        metrics: dict,
    ) -> None:
        if state not in _TERMINAL:
            raise ValueError("runner.state_invalid")
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM executions WHERE object_id=? AND generation=?",
                (object_id, generation),
            ).fetchone()
            if current is None or current["state"] not in {"running", "stopping"}:
                raise ValueError("runner.execution_fenced")
            if (
                current["state"] == "stopping"
                and current["safe_outcome_code"] == "runner.stop_requested"
            ):
                state, outcome, result = "cancelled", "runner.cancelled", None
            fault = connection.execute(
                "SELECT state,reversal_json FROM fault_effects WHERE object_id=?",
                (object_id,),
            ).fetchone()
            if fault is not None and fault["state"] != "reversed":
                raise ValueError("runner.fault_stop_unconfirmed")
            if fault is not None:
                metrics = dict(metrics, fault_reversal=json.loads(fault["reversal_json"]))
            if result is not None and (
                result.object_id != object_id
                or result.execution_sha256 != current["execution_sha256"]
                or result.evidence_kind != "measured"
            ):
                raise ValueError("runner.scope_conflict")
            connection.execute(
                """UPDATE executions SET state=?,safe_outcome_code=?,metrics_json=?,
                result_json=?,stop_scope=?,updated_at=CURRENT_TIMESTAMP
                WHERE object_id=? AND generation=?""",
                (
                    state,
                    outcome,
                    json.dumps(metrics, separators=(",", ":")),
                    result.model_dump_json() if result else None,
                    "runner_requests_and_fault"
                    if object_id.startswith("che_")
                    else "runner_requests",
                    object_id,
                    generation,
                ),
            )
            connection.commit()

    @staticmethod
    def _cancel(connection, object_id: str):
        connection.execute(
            """UPDATE executions SET
            state=CASE WHEN state='queued' THEN 'cancelled' ELSE 'stopping' END,
            safe_outcome_code=CASE WHEN state='queued' THEN 'runner.cancelled'
                ELSE 'runner.stop_requested' END,updated_at=CURRENT_TIMESTAMP,
            stop_scope=CASE WHEN state='queued' AND object_id LIKE 'che_%'
                THEN 'runner_requests_and_fault' ELSE stop_scope END
            WHERE object_id=? AND state IN('queued','running','stopping')""",
            (object_id,),
        )
        connection.execute(
            """UPDATE fault_effects SET terminal_state='cancelled',
            terminal_outcome='runner.cancelled' WHERE object_id=? AND state='prepared'""",
            (object_id,),
        )

    @staticmethod
    def _binding(row, claims):
        if row["execution_sha256"] != execution_digest(claims):
            raise ValueError("runner.scope_conflict")

    @staticmethod
    def _info(row):
        return CapacityExecutionInfo(
            object_id=row["object_id"],
            execution_sha256=row["execution_sha256"],
            state=row["state"],
            safe_outcome_code=row["safe_outcome_code"],
            sample_count=row["sample_count"],
            elapsed_ms=row["elapsed_ms"],
            stop_scope=row["stop_scope"],
            result=CapacityRunnerResult.model_validate_json(row["result_json"])
            if row["result_json"]
            else None,
        )
