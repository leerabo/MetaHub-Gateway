"""Task-local routing for independently leased scheduler queues."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

from metahub_shared.runtime.errors import ApiError

from .manifest import SCHEDULER_QUEUES_BY_CODE

if TYPE_CHECKING:
    from .scheduler import SchedulerLeaseProof

_QUEUE_PROOF: ContextVar[SchedulerLeaseProof | None] = ContextVar(
    "scheduler_execution_queue", default=None
)


@contextmanager
def scheduler_queue_scope(proof: SchedulerLeaseProof):
    queue = SCHEDULER_QUEUES_BY_CODE.get(proof.queue_code)
    if queue is None or queue.scheduler_code != proof.scheduler_code:
        raise ApiError("console_migration.contract_not_ready")
    token = _QUEUE_PROOF.set(proof)
    try:
        yield
    finally:
        _QUEUE_PROOF.reset(token)


def scheduler_queue_proof(scheduler_code: str) -> SchedulerLeaseProof | None:
    proof = _QUEUE_PROOF.get()
    if proof is not None and proof.scheduler_code != scheduler_code:
        raise ApiError("console_migration.contract_not_ready")
    return proof


def selected_scheduler_work(scheduler_code: str, assignments: Mapping[str, str]) -> tuple[str, ...]:
    proof = scheduler_queue_proof(scheduler_code)
    if proof is None:
        return tuple(assignments)
    # This selects work, not authority: the database commit guard still fences it.
    return tuple(
        work for work, suffix in assignments.items()
        if proof.queue_code == f"{scheduler_code}.{suffix}"
    )
