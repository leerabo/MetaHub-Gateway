"""Bounded per-replica coordinator for the PRD-147 scheduler registry."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import sqlite3
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from contextvars import copy_context
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial

from fastapi import FastAPI
from metahub_shared.db.connection import (
    connect_database,
    database_commit_guard,
    database_request_scope,
)
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .execution import scheduler_queue_scope
from .manifest import (
    SCHEDULER_BY_CODE,
    SCHEDULER_MANIFEST,
    SCHEDULER_QUEUES_BY_CODE,
    SCHEDULER_QUEUES_BY_SCHEDULER,
)

logger = logging.getLogger("metahub.control.console_migration.scheduler")
ContextFactory = Callable[[FastAPI], AbstractAsyncContextManager[None]]
DEFAULT_LEASE_SECONDS = 15.0
DEFAULT_POLL_SECONDS = 1.0
DEFAULT_SHUTDOWN_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class SchedulerContextBinding:
    scheduler_code: str
    context_factory: ContextFactory


@dataclass(frozen=True, slots=True, repr=False)
class SchedulerLeaseProof:
    scheduler_code: str
    queue_code: str
    partition_code: str
    token: str
    generation: int
    registry_generation: int
    deadline: datetime


@dataclass(slots=True)
class _CoordinatorStats:
    accepted_count: int = 0
    dropped_count: int = 0
    persisted_count: int = 0
    backend_failure_count: int = 0
    shutdown_timeout_count: int = 0


@dataclass(slots=True)
class _ActiveContext:
    binding: SchedulerContextBinding
    manager: AbstractAsyncContextManager[None]
    leases: tuple[SchedulerLeaseProof, ...]


@asynccontextmanager
async def scheduler_coordinator_lifespan(
    app: FastAPI,
    bindings: Sequence[SchedulerContextBinding],
    *,
    replica_code: str,
):
    if len(bindings) > 64 or len({item.scheduler_code for item in bindings}) != len(
        bindings
    ):
        raise RuntimeError("invalid scheduler coordinator bindings")
    stop = asyncio.Event()
    app.state.scheduler_registry_leases = {}
    app.state.scheduler_queue_leases = {}
    app.state.scheduler_active_contexts = ()
    app.state.scheduler_active_partitions = ()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scheduler-registry-db")
    app.state.scheduler_database_executor = executor
    task = asyncio.create_task(
        _coordinator_loop(app, tuple(bindings), replica_code, stop),
        name="scheduler-lease-coordinator",
    )
    app.state.scheduler_coordinator_count = 1
    try:
        yield
    finally:
        stop.set()
        timeout = float(
            getattr(
                app.state.config,
                "console_migration_scheduler_shutdown_seconds",
                DEFAULT_SHUTDOWN_SECONDS,
            )
        )
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=max(0.1, timeout))
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        finally:
            app.state.scheduler_database_executor = None
            await asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=True)


async def _coordinator_loop(
    app: FastAPI,
    bindings: tuple[SchedulerContextBinding, ...],
    replica_code: str,
    stop: asyncio.Event,
) -> None:
    config = app.state.config
    lease_seconds = float(
        getattr(
            config,
            "console_migration_scheduler_lease_seconds",
            DEFAULT_LEASE_SECONDS,
        )
    )
    poll_seconds = float(
        getattr(
            config,
            "console_migration_scheduler_poll_seconds",
            DEFAULT_POLL_SECONDS,
        )
    )
    lease_seconds = max(3.0, lease_seconds)
    poll_seconds = min(max(0.05, poll_seconds), lease_seconds / 3)
    active: dict[tuple[str, str], _ActiveContext] = {}
    stats = _CoordinatorStats()
    try:
        while not stop.is_set():
            await _renew_active(app, replica_code, active, stats, lease_seconds)
            for binding in bindings:
                if stop.is_set():
                    break
                await _renew_active(app, replica_code, active, stats, lease_seconds)
                queues = _queue_claim_order(binding.scheduler_code, replica_code)
                if all((binding.scheduler_code, queue.code) in active for queue in queues):
                    continue
                lease = None
                for queue in queues:
                    key = (binding.scheduler_code, queue.code)
                    if key in active:
                        continue
                    try:
                        lease = await _coordinator_database_call(
                            app,
                            claim_scheduler_lease,
                            config,
                            scheduler_code=binding.scheduler_code,
                            queue_code=queue.code,
                            replica_code=replica_code,
                            lease_seconds=lease_seconds,
                        )
                    except ApiError as exc:
                        stats.backend_failure_count += 1
                        _warning(binding.scheduler_code, exc.code)
                        break
                    if lease is not None:
                        break
                if lease is None:
                    continue
                leases = (lease,)
                manager = binding.context_factory(app)
                try:
                    with scheduler_queue_scope(lease), database_commit_guard(
                        _lease_commit_guard(config, leases)
                    ):
                        await manager.__aenter__()
                except Exception:
                    stats.backend_failure_count += 1
                    await _coordinator_database_call(
                        app,
                        release_scheduler_leases,
                        config,
                        leases,
                        replica_code=replica_code,
                        error_code="scheduler_start_failed",
                    )
                    _warning(binding.scheduler_code, "scheduler_start_failed")
                    continue
                active[(binding.scheduler_code, lease.queue_code)] = _ActiveContext(
                    binding=binding,
                    manager=manager,
                    leases=leases,
                )
                stats.accepted_count += 1
                stats.persisted_count += 1
            _publish_active_state(app, active)
            await _observe(app, replica_code, active, stats)
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
            except TimeoutError:
                continue
    finally:
        await asyncio.gather(*(
            _stop_active(app, replica_code, active, key, stats)
            for key in reversed(tuple(active))
        ))
        _publish_active_state(app, active)
        await _observe(app, replica_code, active, stats)


async def _renew_active(
    app: FastAPI,
    replica_code: str,
    active: dict[tuple[str, str], _ActiveContext],
    stats: _CoordinatorStats,
    lease_seconds: float,
) -> None:
    children = sorted(
        tuple(active.items()),
        key=lambda item: min(lease.deadline for lease in item[1].leases),
    )
    if not children or min(
        (lease.deadline - datetime.now(UTC)).total_seconds()
        for lease in children[0][1].leases
    ) > lease_seconds / 2:
        return
    # Align active deadlines in one writer admission, including not-yet-due queues.
    try:
        results = await _coordinator_database_call(
            app,
            _renew_scheduler_lease_batch,
            app.state.config,
            tuple(child.leases for _key, child in children),
            replica_code=replica_code,
            lease_seconds=lease_seconds,
        )
    except ApiError as exc:
        results = (exc,) * len(children)
    expired = []
    for (key, child), renewed in zip(children, results, strict=True):
        scheduler_code, _queue_code = key
        if isinstance(renewed, ApiError):
            stats.backend_failure_count += 1
            _warning(scheduler_code, renewed.code)
            renewed = None
        if renewed is None:
            stats.dropped_count += 1
            expired.append(key)
            continue
        child.leases = renewed
        stats.persisted_count += len(renewed)
    _publish_active_state(app, active)
    await asyncio.gather(*(
        _stop_active(app, replica_code, active, key, stats) for key in expired
    ))


async def _stop_active(
    app: FastAPI,
    replica_code: str,
    active: dict[tuple[str, str], _ActiveContext],
    key: tuple[str, str],
    stats: _CoordinatorStats,
) -> None:
    child = active.pop(key, None)
    if child is None:
        return
    scheduler_code, _queue_code = key
    _publish_active_state(app, active)
    timeout = float(
        getattr(
            app.state.config,
            "console_migration_scheduler_shutdown_seconds",
            DEFAULT_SHUTDOWN_SECONDS,
        )
    )
    error_code = None
    try:
        await asyncio.wait_for(
            child.manager.__aexit__(None, None, None), timeout=max(0.1, timeout)
        )
    except TimeoutError:
        stats.shutdown_timeout_count += 1
        error_code = "scheduler_shutdown_timeout"
    except Exception:
        stats.backend_failure_count += 1
        error_code = "scheduler_shutdown_failed"
    try:
        released = await _coordinator_database_call(
            app,
            release_scheduler_leases,
            app.state.config,
            child.leases,
            replica_code=replica_code,
            error_code=error_code,
        )
        if released:
            stats.persisted_count += len(child.leases)
    except ApiError as exc:
        stats.backend_failure_count += 1
        _warning(scheduler_code, exc.code)


def _queue_claim_order(scheduler_code: str, replica_code: str):
    queues = SCHEDULER_QUEUES_BY_SCHEDULER.get(scheduler_code, ())
    if not queues:
        return ()
    offset = int.from_bytes(
        hashlib.sha256(f"{replica_code}\x1f{scheduler_code}".encode()).digest()[:8],
        "big",
    ) % len(queues)
    return queues[offset:] + queues[:offset]


def _publish_active_state(
    app: FastAPI,
    active: dict[tuple[str, str], _ActiveContext],
) -> None:
    grouped: dict[str, list[SchedulerLeaseProof]] = {}
    for child in active.values():
        for lease in child.leases:
            grouped.setdefault(lease.scheduler_code, []).append(lease)
    app.state.scheduler_queue_leases = {
        scheduler_code: tuple(sorted(leases, key=lambda item: item.queue_code))
        for scheduler_code, leases in grouped.items()
    }
    app.state.scheduler_registry_leases = {
        scheduler_code: _primary_lease(leases)
        for scheduler_code, leases in app.state.scheduler_queue_leases.items()
    }
    app.state.scheduler_active_contexts = tuple(sorted(grouped))
    app.state.scheduler_active_partitions = tuple(
        sorted(
            f"{lease.scheduler_code}:{lease.partition_code}"
            for leases in grouped.values()
            for lease in leases
        )
    )


def claim_scheduler_lease(
    config: AppConfig,
    *,
    scheduler_code: str,
    replica_code: str,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
    queue_code: str | None = None,
) -> SchedulerLeaseProof | None:
    queue = _queue_definition(scheduler_code, queue_code)
    leases = _claim_scheduler_leases(
        config,
        scheduler_code=scheduler_code,
        replica_code=replica_code,
        lease_seconds=lease_seconds,
        queues=(queue,),
    )
    return None if leases is None else leases[0]


def claim_scheduler_leases(
    config: AppConfig,
    *,
    scheduler_code: str,
    replica_code: str,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
) -> tuple[SchedulerLeaseProof, ...] | None:
    return _claim_scheduler_leases(
        config,
        scheduler_code=scheduler_code,
        replica_code=replica_code,
        lease_seconds=lease_seconds,
        queues=SCHEDULER_QUEUES_BY_SCHEDULER.get(scheduler_code, ()),
    )


def _claim_scheduler_leases(
    config: AppConfig,
    *,
    scheduler_code: str,
    replica_code: str,
    lease_seconds: float,
    queues,
) -> tuple[SchedulerLeaseProof, ...] | None:
    definition = _definition(scheduler_code)
    if not _condition_enabled(config, definition.config_condition):
        return None
    if not queues:
        raise ApiError("console_migration.contract_not_ready")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        registry_generation = _validate_registry(connection, definition)
        _validate_queue_contracts(connection, scheduler_code)
        now = _database_now(connection)
        deadline = now + timedelta(seconds=max(3.0, lease_seconds))
        owner = _secret_hmac(config, "replica", replica_code)
        pending = []
        for queue in queues:
            current = connection.execute(
                """SELECT * FROM scheduler_leases
                   WHERE scheduler_code=? AND partition_code=?""",
                (scheduler_code, queue.partition_code),
            ).fetchone()
            if current is not None and _parse_time(current["lease_deadline_at"]) > now:
                connection.rollback()
                return None
            generation = (
                1 if current is None else int(current["fencing_generation"]) + 1
            )
            token = secrets.token_urlsafe(32)
            pending.append((queue, current, generation, token))
        for queue, current, generation, token in pending:
            token_hmac = _secret_hmac(config, "lease", token)
            if current is None:
                connection.execute(
                    """INSERT INTO scheduler_leases(
                       scheduler_code,partition_code,lease_owner_pseudonym,
                       lease_deadline_at,fencing_generation,lease_token_hmac,
                       last_started_at,last_completed_at,last_error_code,revision,updated_at)
                       VALUES(?,?,?,?,?,?,?,NULL,NULL,1,?)""",
                    (
                        scheduler_code,
                        queue.partition_code,
                        owner,
                        _db_time(deadline),
                        generation,
                        token_hmac,
                        _db_time(now),
                        _db_time(now),
                    ),
                )
                continue
            updated = connection.execute(
                """UPDATE scheduler_leases SET
                   lease_owner_pseudonym=?,lease_deadline_at=?,fencing_generation=?,
                   lease_token_hmac=?,last_started_at=?,last_completed_at=NULL,
                   last_error_code=NULL,revision=revision+1,updated_at=?
                   WHERE scheduler_code=? AND partition_code=?
                     AND fencing_generation=? AND lease_deadline_at<=?""",
                (
                    owner,
                    _db_time(deadline),
                    generation,
                    token_hmac,
                    _db_time(now),
                    _db_time(now),
                    scheduler_code,
                    queue.partition_code,
                    current["fencing_generation"],
                    _db_time(now),
                ),
            ).rowcount
            if updated != 1:
                connection.rollback()
                return None
        connection.commit()
        return tuple(
            SchedulerLeaseProof(
                scheduler_code=scheduler_code,
                queue_code=queue.code,
                partition_code=queue.partition_code,
                token=token,
                generation=generation,
                registry_generation=registry_generation,
                deadline=deadline,
            )
            for queue, _current, generation, token in pending
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, TypeError, ValueError, KeyError):
        _rollback(connection)
        raise ApiError("console_migration.dependency_unavailable") from None
    finally:
        connection.close()


def renew_scheduler_lease(
    config: AppConfig,
    lease: SchedulerLeaseProof,
    *,
    replica_code: str,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
) -> SchedulerLeaseProof | None:
    renewed = renew_scheduler_leases(
        config,
        (lease,),
        replica_code=replica_code,
        lease_seconds=lease_seconds,
    )
    return None if renewed is None else renewed[0]


def renew_scheduler_leases(
    config: AppConfig,
    leases: tuple[SchedulerLeaseProof, ...],
    *,
    replica_code: str,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
) -> tuple[SchedulerLeaseProof, ...] | None:
    scheduler_code = _lease_scheduler_code(leases)
    definition = _definition(scheduler_code)
    if not _condition_enabled(config, definition.config_condition):
        return None
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        renewed = _renew_leases_in_transaction(
            connection, config, leases, replica_code=replica_code,
            lease_seconds=lease_seconds,
        )
        if renewed is None:
            connection.rollback()
            return None
        connection.commit()
        return renewed
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, TypeError, ValueError, KeyError):
        _rollback(connection)
        raise ApiError("console_migration.dependency_unavailable") from None
    finally:
        connection.close()


def _renew_scheduler_lease_batch(
    config: AppConfig,
    groups: tuple[tuple[SchedulerLeaseProof, ...], ...],
    *,
    replica_code: str,
    lease_seconds: float = DEFAULT_LEASE_SECONDS,
) -> tuple[tuple[SchedulerLeaseProof, ...] | ApiError | None, ...]:
    if not groups:
        return ()
    count = sum(len(leases) for leases in groups)
    partitions = {(lease.scheduler_code, lease.partition_code)
                  for leases in groups for lease in leases}
    if (len(groups) > len(SCHEDULER_QUEUES_BY_CODE)
        or any(not leases for leases in groups)
        or count > len(SCHEDULER_QUEUES_BY_CODE) or len(partitions) != count):
        raise ApiError("console_migration.contract_not_ready")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        results = []
        for leases in groups:
            connection.execute("SAVEPOINT scheduler_renew_group")
            try:
                renewed = _renew_leases_in_transaction(
                    connection, config, leases, replica_code=replica_code,
                    lease_seconds=lease_seconds,
                )
            except ApiError as exc:
                renewed = exc
            if renewed is None or isinstance(renewed, ApiError):
                connection.execute("ROLLBACK TO SAVEPOINT scheduler_renew_group")
            connection.execute("RELEASE SAVEPOINT scheduler_renew_group")
            results.append(renewed)
        connection.commit()
        return tuple(results)
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, TypeError, ValueError, KeyError):
        _rollback(connection)
        raise ApiError("console_migration.dependency_unavailable") from None
    finally:
        connection.close()


def _renew_leases_in_transaction(
    connection,
    config: AppConfig,
    leases: tuple[SchedulerLeaseProof, ...],
    *,
    replica_code: str,
    lease_seconds: float,
) -> tuple[SchedulerLeaseProof, ...] | None:
    scheduler_code = _lease_scheduler_code(leases)
    definition = _definition(scheduler_code)
    if not _condition_enabled(config, definition.config_condition):
        return None
    registry_generation = _validate_registry(connection, definition)
    _validate_queue_contracts(connection, scheduler_code)
    if any(registry_generation != lease.registry_generation for lease in leases):
        return None
    if getattr(connection, "backend", "sqlite") == "postgresql":
        # Evaluate expiry only after row-lock waits, not against transaction start.
        for lease in sorted(leases, key=lambda item: item.partition_code):
            connection.execute(
                """SELECT 1 FROM scheduler_leases
                   WHERE scheduler_code=? AND partition_code=? FOR UPDATE""",
                (lease.scheduler_code, lease.partition_code),
            ).fetchone()
    now = _database_now(connection)
    deadline = now + timedelta(seconds=max(3.0, lease_seconds))
    for lease in leases:
        queue = _queue_definition(scheduler_code, lease.queue_code)
        if queue.partition_code != lease.partition_code:
            raise ApiError("console_migration.contract_not_ready")
        updated = connection.execute(
            """UPDATE scheduler_leases SET lease_deadline_at=?,revision=revision+1,
                 updated_at=?
               WHERE scheduler_code=? AND partition_code=?
                 AND lease_owner_pseudonym=? AND fencing_generation=?
                 AND lease_token_hmac=? AND lease_deadline_at>?""",
            (
                _db_time(deadline),
                _db_time(now),
                lease.scheduler_code,
                lease.partition_code,
                _secret_hmac(config, "replica", replica_code),
                lease.generation,
                _secret_hmac(config, "lease", lease.token),
                _db_time(now),
            ),
        ).rowcount
        if updated != 1:
            return None
    return tuple(
        SchedulerLeaseProof(
            scheduler_code=lease.scheduler_code,
            queue_code=lease.queue_code,
            partition_code=lease.partition_code,
            token=lease.token,
            generation=lease.generation,
            registry_generation=lease.registry_generation,
            deadline=deadline,
        )
        for lease in leases
    )


def release_scheduler_lease(
    config: AppConfig,
    lease: SchedulerLeaseProof,
    *,
    replica_code: str,
    error_code: str | None = None,
) -> bool:
    return release_scheduler_leases(
        config,
        (lease,),
        replica_code=replica_code,
        error_code=error_code,
    )


def release_scheduler_leases(
    config: AppConfig,
    leases: tuple[SchedulerLeaseProof, ...],
    *,
    replica_code: str,
    error_code: str | None = None,
) -> bool:
    _lease_scheduler_code(leases)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _database_now(connection)
        released = 0
        for lease in leases:
            released += connection.execute(
                """UPDATE scheduler_leases SET lease_deadline_at=?,last_completed_at=?,
                     last_error_code=?,revision=revision+1,updated_at=?
                   WHERE scheduler_code=? AND partition_code=?
                     AND lease_owner_pseudonym=? AND fencing_generation=?
                     AND lease_token_hmac=?""",
                (
                    _db_time(now),
                    _db_time(now),
                    error_code,
                    _db_time(now),
                    lease.scheduler_code,
                    lease.partition_code,
                    _secret_hmac(config, "replica", replica_code),
                    lease.generation,
                    _secret_hmac(config, "lease", lease.token),
                ),
            ).rowcount
        connection.commit()
        return released == len(leases)
    except (sqlite3.Error, DbError, TypeError, ValueError, KeyError):
        _rollback(connection)
        raise ApiError("console_migration.dependency_unavailable") from None
    finally:
        connection.close()


def require_scheduler_fences(
    connection,
    config: AppConfig,
    leases: tuple[SchedulerLeaseProof, ...],
) -> None:
    """Validate every queue lease inside the business transaction being committed."""

    scheduler_code = _lease_scheduler_code(leases)
    head = connection.execute(
        """SELECT generation,manifest_digest FROM scheduler_registry_heads
           WHERE head_key='production'"""
    ).fetchone()
    expected_head = hashlib.sha256(
        "|".join(
            f"{item.code}:{item.manifest_digest}"
            for item in sorted(SCHEDULER_MANIFEST, key=lambda item: item.code)
        ).encode("ascii")
    ).hexdigest()
    registry_generation = leases[0].registry_generation
    if (
        head is None
        or int(head["generation"]) != registry_generation
        or not hmac.compare_digest(str(head["manifest_digest"]), expected_head)
    ):
        raise ApiError("console_migration.registry_drift")
    now = _database_now(connection)
    for lease in leases:
        queue = SCHEDULER_QUEUES_BY_CODE.get(lease.queue_code)
        if queue is None or queue.scheduler_code != scheduler_code:
            raise ApiError("console_migration.contract_not_ready")
        row = connection.execute(
            """SELECT lease.fencing_generation,queue.manifest_digest
               FROM scheduler_leases lease
               JOIN scheduler_queue_contracts queue
                 ON queue.scheduler_code=lease.scheduler_code
                AND queue.queue_code=? AND queue.enabled=1
               WHERE lease.scheduler_code=? AND lease.partition_code=?
                 AND lease.fencing_generation=? AND lease.lease_token_hmac=?
                 AND lease.lease_deadline_at>?""",
            (
                lease.queue_code,
                lease.scheduler_code,
                lease.partition_code,
                lease.generation,
                _secret_hmac(config, "lease", lease.token),
                _db_time(now),
            ),
        ).fetchone()
        if row is None or not hmac.compare_digest(
            str(row["manifest_digest"]), queue.manifest_digest
        ):
            raise ApiError("platform_job.lease_lost")


def _lease_commit_guard(
    config: AppConfig,
    leases: tuple[SchedulerLeaseProof, ...],
):
    def guard(connection) -> None:
        require_scheduler_fences(connection, config, leases)

    return guard


def _primary_lease(
    leases: tuple[SchedulerLeaseProof, ...],
) -> SchedulerLeaseProof:
    scheduler_code = _lease_scheduler_code(leases)
    preferred = f"{scheduler_code}.jobs"
    return next((lease for lease in leases if lease.queue_code == preferred), leases[0])


def _lease_scheduler_code(leases: tuple[SchedulerLeaseProof, ...]) -> str:
    if not leases or len({lease.scheduler_code for lease in leases}) != 1:
        raise ApiError("console_migration.contract_not_ready")
    return leases[0].scheduler_code


def _queue_definition(scheduler_code: str, queue_code: str | None):
    requested = queue_code or f"{scheduler_code}.main"
    queue = SCHEDULER_QUEUES_BY_CODE.get(requested)
    if queue is None or queue.scheduler_code != scheduler_code:
        raise ApiError("console_migration.contract_not_ready")
    return queue


async def _observe(
    app: FastAPI,
    replica_code: str,
    active: dict[tuple[str, str], _ActiveContext],
    stats: _CoordinatorStats,
) -> None:
    try:
        await _coordinator_database_call(
            app,
            _write_observation,
            app.state.config,
            replica_code,
            stats,
            len(active),
        )
    except ApiError:
        stats.backend_failure_count += 1


async def _coordinator_database_call(app: FastAPI, operation, *args, **kwargs):
    executor = getattr(app.state, "scheduler_database_executor", None)
    context = copy_context()
    call = partial(context.run, _database_operation, operation, *args, **kwargs)
    return await asyncio.get_running_loop().run_in_executor(executor, call)


def _database_operation(operation, *args, **kwargs):
    # Create the scope inside the worker thread, never around a child lifespan.
    with database_request_scope(reuse_connections=True):
        return operation(*args, **kwargs)


def _write_observation(
    config: AppConfig,
    replica_code: str,
    stats: _CoordinatorStats,
    active_count: int,
) -> None:
    replica = _secret_hmac(config, "replica", replica_code)
    observation_id = "rso_" + hashlib.sha256(
        f"prd147/coordinator/{replica}".encode("ascii")
    ).hexdigest()[:32]
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """INSERT INTO runtime_surface_observations(
               runtime_surface_observation_id,runtime_surface_code,replica_pseudonym,
               accepted_count,dropped_count,persisted_count,backend_failure_count,
               queued_count,shutdown_timeout_count,observed_at)
               VALUES(?,'scheduler-lease-coordinator',?,?,?,?,?,?,?,?)
               ON CONFLICT(runtime_surface_observation_id) DO UPDATE SET
                 accepted_count=excluded.accepted_count,
                 dropped_count=excluded.dropped_count,
                 persisted_count=excluded.persisted_count,
                 backend_failure_count=excluded.backend_failure_count,
                 queued_count=excluded.queued_count,
                 shutdown_timeout_count=excluded.shutdown_timeout_count,
                 observed_at=excluded.observed_at""",
            (
                observation_id,
                replica,
                stats.accepted_count,
                stats.dropped_count,
                stats.persisted_count,
                stats.backend_failure_count,
                active_count,
                stats.shutdown_timeout_count,
                _db_time(datetime.now(UTC)),
            ),
        )
        connection.commit()
    except (sqlite3.Error, DbError, TypeError, ValueError, KeyError):
        _rollback(connection)
        raise ApiError("console_migration.dependency_unavailable") from None
    finally:
        connection.close()


def _validate_registry(connection: sqlite3.Connection, definition) -> int:
    row = connection.execute(
        """SELECT enabled,manifest_digest FROM scheduler_registry_entries
           WHERE scheduler_code=?""",
        (definition.code,),
    ).fetchone()
    if row is None or row["enabled"] != 1:
        raise ApiError("console_migration.contract_not_ready")
    if not hmac.compare_digest(str(row["manifest_digest"]), definition.manifest_digest):
        raise ApiError("console_migration.registry_drift")
    head = connection.execute(
        """SELECT generation,manifest_digest FROM scheduler_registry_heads
           WHERE head_key='production'"""
    ).fetchone()
    expected = hashlib.sha256(
        "|".join(
            f"{item.code}:{item.manifest_digest}"
            for item in sorted(SCHEDULER_MANIFEST, key=lambda item: item.code)
        ).encode("ascii")
    ).hexdigest()
    if head is None or not hmac.compare_digest(str(head["manifest_digest"]), expected):
        raise ApiError("console_migration.registry_drift")
    return int(head["generation"])


def _validate_queue_contracts(connection, scheduler_code: str) -> None:
    expected = SCHEDULER_QUEUES_BY_SCHEDULER.get(scheduler_code, ())
    rows = connection.execute(
        """SELECT queue_code,owner_domain,partition_policy_code,manifest_digest,enabled
           FROM scheduler_queue_contracts WHERE scheduler_code=? ORDER BY queue_code""",
        (scheduler_code,),
    ).fetchall()
    actual = {str(row["queue_code"]): row for row in rows}
    if len(actual) != len(expected):
        raise ApiError("console_migration.registry_drift")
    for queue in expected:
        row = actual.get(queue.code)
        if (
            row is None
            or int(row["enabled"]) != 1
            or str(row["owner_domain"]) != queue.owner_domain
            or str(row["partition_policy_code"]) != queue.partition_policy_code
            or not hmac.compare_digest(
                str(row["manifest_digest"]), queue.manifest_digest
            )
        ):
            raise ApiError("console_migration.registry_drift")


def _definition(scheduler_code: str):
    definition = SCHEDULER_BY_CODE.get(scheduler_code)
    if definition is None:
        raise ApiError("console_migration.contract_not_ready")
    return definition


def _condition_enabled(config: AppConfig, condition: str) -> bool:
    return condition == "always" or bool(getattr(config, condition, False))


def _secret_hmac(config: AppConfig, purpose: str, value: str) -> str:
    return hmac.new(
        config.platform_session_secret.encode(),
        f"prd147/scheduler/{purpose}/v1\x00{value}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("console_migration.dependency_unavailable") from None


def _rollback(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        connection.rollback()


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _database_now(connection) -> datetime:
    statement = (
        "SELECT clock_timestamp()"
        if getattr(connection, "backend", "sqlite") == "postgresql"
        else "SELECT CURRENT_TIMESTAMP"
    )
    return _parse_time(connection.execute(statement).fetchone()[0])


def _parse_time(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("invalid timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _warning(scheduler_code: str, error_code: str) -> None:
    logger.warning(
        "scheduler registry coordinator iteration failed",
        extra={
            "event": "console_migration.scheduler_iteration_failed",
            "scheduler_code": scheduler_code,
            "error_code": error_code,
        },
    )


__all__ = [
    "SchedulerContextBinding",
    "SchedulerLeaseProof",
    "claim_scheduler_lease",
    "claim_scheduler_leases",
    "release_scheduler_lease",
    "release_scheduler_leases",
    "require_scheduler_fences",
    "renew_scheduler_lease",
    "renew_scheduler_leases",
    "scheduler_coordinator_lifespan",
]
