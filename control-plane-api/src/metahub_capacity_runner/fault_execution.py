"""Original-execution fault ownership, durable cleanup and generation fencing."""

from __future__ import annotations

import json

from .contracts import CapacityPermitClaims, execution_digest
from .fault_client import FaultLeaseClient, fault_http_client
from .fault_contracts import FaultLeaseInfo
from .settings import CapacityFaultBinding
from .store import RunnerStore


class FaultExecutionStore:
    def __init__(self, store: RunnerStore):
        self.store = store

    def prepare(self, claims: CapacityPermitClaims, generation: int, binding: CapacityFaultBinding):
        if claims.operation != "chaos" or not binding.accepts(claims):
            raise ValueError("runner.scope_conflict")
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._owner(connection, claims.object_id, generation)
            if row["state"] != "running" or row["execution_sha256"] != execution_digest(claims):
                raise ValueError("runner.execution_fenced")
            existing = connection.execute(
                "SELECT * FROM fault_effects WHERE object_id=?", (claims.object_id,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["binding_json"] != binding.model_dump_json()
                    or existing["generation"] != generation
                    or existing["state"] != "prepared"
                ):
                    raise ValueError("runner.execution_fenced")
            else:
                connection.execute(
                    """INSERT INTO fault_effects(
                    object_id,generation,binding_json,state) VALUES(?,?,?,'prepared')""",
                    (claims.object_id, generation, binding.model_dump_json()),
                )
            connection.commit()

    def reversed(self, claims, generation, info: FaultLeaseInfo):
        if (
            info.object_id != claims.object_id
            or info.execution_sha256 != execution_digest(claims)
            or info.state == "active"
            or info.active_connections != 0
        ):
            raise ValueError("runner.fault_stop_unconfirmed")
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._owner(connection, claims.object_id, generation)
            effect = connection.execute(
                "SELECT * FROM fault_effects WHERE object_id=?", (claims.object_id,)
            ).fetchone()
            if effect is None:
                raise ValueError("runner.execution_fenced")
            binding = CapacityFaultBinding.model_validate_json(effect["binding_json"])
            if info.binding_code != binding.binding_code:
                raise ValueError("runner.fault_stop_unconfirmed")
            connection.execute(
                """UPDATE fault_effects SET state='reversed',reversal_json=?
                WHERE object_id=?""",
                (info.model_dump_json(), claims.object_id),
            )
            connection.commit()

    def defer(self, object_id, generation, *, state, outcome, metrics):
        if state not in {"failed", "cancelled", "interrupted"}:
            raise ValueError("runner.state_invalid")
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._owner(connection, object_id, generation)
            if row["safe_outcome_code"] == "runner.stop_requested":
                state, outcome = "cancelled", "runner.cancelled"
            updated = connection.execute(
                """UPDATE fault_effects SET
                terminal_state=?,terminal_outcome=? WHERE object_id=?""",
                (state, outcome, object_id),
            )
            if updated.rowcount != 1:
                raise ValueError("runner.execution_fenced")
            connection.execute(
                """UPDATE executions SET state='stopping',
                safe_outcome_code='runner.fault_stop_pending',metrics_json=?,
                updated_at=CURRENT_TIMESTAMP WHERE object_id=?""",
                (json.dumps(metrics, separators=(",", ":")), object_id),
            )
            connection.commit()

    def pending(self):
        with self.store.connection() as connection:
            row = connection.execute("""SELECT e.claims_json,e.generation,e.metrics_json,
                f.binding_json,f.state,f.terminal_state,f.terminal_outcome,f.reversal_json
                FROM executions e JOIN fault_effects f ON f.object_id=e.object_id
                WHERE e.state='stopping' ORDER BY e.created_at,e.object_id LIMIT 1""").fetchone()
            return dict(row) if row else None

    async def reconcile(self, pending) -> bool:
        claims = CapacityPermitClaims.model_validate_json(pending["claims_json"])
        binding = CapacityFaultBinding.model_validate_json(pending["binding_json"])
        if pending["state"] != "reversed":
            async with fault_http_client() as client:
                proof = await FaultLeaseClient(client, binding, claims).release()
            if proof is None:
                return False
            self.reversed(claims, pending["generation"], proof)
        self.store.finish(
            claims.object_id,
            pending["generation"],
            result=None,
            state=pending["terminal_state"],
            outcome=pending["terminal_outcome"],
            metrics=json.loads(pending["metrics_json"] or "{}"),
        )
        return True

    @staticmethod
    def _owner(connection, object_id, generation):
        row = connection.execute(
            "SELECT * FROM executions WHERE object_id=? AND generation=?", (object_id, generation)
        ).fetchone()
        if row is None or row["state"] not in {"running", "stopping"}:
            raise ValueError("runner.execution_fenced")
        return row
