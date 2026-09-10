"""Durable, fenced execution runtime for precompiled platform jobs."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from metahub_shared.db.connection import (
    connect_database,
    database_backend,
    database_operation_deadline,
    database_request_scope,
)
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from .catalog import (
    PlatformRuntimeProbeInput,
    handler_definition,
    register_job_handler,
    validate_job_input,
)
from .crypto import decrypt_job_value, encrypt_job_value

_WORKER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,95}$")
_EFFECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{2,95}$")
_OUTCOME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,95}$")
DEFAULT_LEASE_SECONDS = 45
MAX_EXPIRED_RECOVERY = 100


class PlatformJobCancelled(Exception):
    """Stop a handler after the durable cancellation fence has closed."""


class PlatformJobDeferred(Exception):
    """The root attempt was atomically handed to the external-work observer."""


class PlatformJobRetry(Exception):
    """Request a bounded retry using a public, fixed outcome code."""

    def __init__(self, code: str = "platform_job.retryable_failure") -> None:
        _valid_outcome(code)
        self.code = code
        super().__init__(code)


class PlatformJobFailure(Exception):
    """Terminal handler failure with a public, fixed outcome code."""

    def __init__(self, code: str = "platform_job.execution_failed") -> None:
        _valid_outcome(code)
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class PlatformJobOuterFence:
    scheduler_code: str
    partition_code: str
    lease_token: str
    fencing_generation: int
    registry_generation: int


@dataclass(frozen=True, slots=True, repr=False)
class PlatformJobLease:
    job_id: str
    attempt_id: str
    type_code: str
    generation: int
    attempt_number: int
    worker_code: str
    lease_token: str
    expires_at: datetime
    input_envelope: str
    input_sha256: str
    outer_fence: PlatformJobOuterFence | None = None


@dataclass(slots=True, repr=False)
class PlatformJobContext:
    """Fenced capabilities exposed only to trusted, in-process handlers."""

    config: AppConfig
    lease: PlatformJobLease
    previous_checkpoint: object | None = None

    def defer_external(
        self,
        value: object,
        *,
        timeout_seconds: int,
        apply: Callable[[Any], None] | None = None,
    ) -> None:
        from .external import defer_platform_job

        defer_platform_job(self, value, timeout_seconds=timeout_seconds, apply=apply)

    def checkpoint(self, value: object) -> str:
        prepared = encrypt_job_value(
            self.config,
            value,
            kind="checkpoint",
            owner_id=self.lease.job_id,
            type_code=self.lease.type_code,
        )
        connection = _connect(self.config)
        try:
            connection.execute("BEGIN IMMEDIATE")
            job = _live_lease(connection, self.config, self.lease)
            _cancel_if_requested(connection, job, self.lease)
            sequence = int(
                connection.execute(
                    """SELECT coalesce(MAX(sequence_number),0)+1
                       FROM platform_job_checkpoints
                       WHERE platform_job_id=? AND generation=?""",
                    (self.lease.job_id, self.lease.generation),
                ).fetchone()[0]
            )
            checkpoint_id = _id("pjc_")
            connection.execute(
                """
                INSERT INTO platform_job_checkpoints(
                  platform_job_checkpoint_id,platform_job_id,platform_job_attempt_id,
                  generation,sequence_number,checkpoint_envelope,checkpoint_sha256,created_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    checkpoint_id,
                    self.lease.job_id,
                    self.lease.attempt_id,
                    self.lease.generation,
                    sequence,
                    prepared.envelope,
                    prepared.plaintext_sha256,
                    _db_time(_database_now(connection)),
                ),
            )
            job = _live_lease(connection, self.config, self.lease)
            _cancel_if_requested(connection, job, self.lease)
            connection.commit()
            return checkpoint_id
        except PlatformJobCancelled:
            _commit_if_open(connection)
            raise
        except (ApiError, PlatformJobFailure):
            _rollback(connection)
            raise
        except (sqlite3.Error, DbError, TypeError, ValueError):
            _rollback(connection)
            raise ApiError("platform_job.unavailable") from None
        finally:
            connection.close()

    def commit_effect(
        self,
        effect_key: str,
        value: object,
        apply: Callable[[Any], None] | None = None,
    ) -> bool:
        """Commit an effect declaration and its database mutation atomically."""

        if _EFFECT_RE.fullmatch(effect_key) is None:
            raise ApiError("platform_job.input_invalid")
        digest = _digest(value)
        connection = _connect(self.config)
        try:
            connection.execute("BEGIN IMMEDIATE")
            job = _live_lease(connection, self.config, self.lease)
            _cancel_if_requested(connection, job, self.lease)
            committed = _commit_effect_in_transaction(
                connection,
                job_id=self.lease.job_id,
                generation=self.lease.generation,
                effect_key=effect_key,
                digest=digest,
                apply=apply,
            )
            job = _live_lease(connection, self.config, self.lease)
            _cancel_if_requested(connection, job, self.lease)
            connection.commit()
            return committed
        except PlatformJobCancelled:
            _rollback(connection)
            _fail_platform_job(
                self.config, self.lease, "platform_job.cancelled", retryable=False
            )
            raise
        except (ApiError, PlatformJobFailure):
            _rollback(connection)
            raise
        except (sqlite3.Error, DbError, TypeError, ValueError):
            _rollback(connection)
            raise ApiError("platform_job.unavailable") from None
        except BaseException:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def cancellation_requested(self) -> bool:
        connection = _connect(self.config)
        try:
            connection.execute("BEGIN")
            job = _live_lease(connection, self.config, self.lease)
            requested = job["status"] == "cancel_requested"
            connection.commit()
            return requested
        except (ApiError, PlatformJobFailure):
            _rollback(connection)
            raise
        except (sqlite3.Error, DbError, TypeError, ValueError):
            _rollback(connection)
            raise ApiError("platform_job.unavailable") from None
        finally:
            connection.close()


def _commit_effect_in_transaction(
    connection,
    *,
    job_id: str,
    generation: int,
    effect_key: str,
    digest: str,
    apply: Callable[[Any], None] | None,
) -> bool:
    """Caller holds the job lock and rechecks its own lease and stop fence before commit."""
    existing = connection.execute(
        "SELECT effect_sha256 FROM platform_job_effects WHERE platform_job_id=? AND effect_key=?",
        (job_id, effect_key),
    ).fetchone()
    if existing is not None:
        if not hmac.compare_digest(str(existing[0]), digest):
            raise ApiError("platform_job.effect_conflict")
        return False
    if apply is not None:
        apply(connection)
    connection.execute(
        """INSERT INTO platform_job_effects(
             platform_job_effect_id,platform_job_id,generation,effect_key,
             effect_sha256,committed_at) VALUES(?,?,?,?,?,?)""",
        (_id("pje_"), job_id, generation, effect_key, digest, _db_time(_database_now(connection))),
    )
    return True


def claim_platform_job(
    config: AppConfig,
    *,
    worker_code: str,
    lease_seconds: int | None = None,
    outer_fence: PlatformJobOuterFence | None = None,
) -> PlatformJobLease | None:
    if _WORKER_RE.fullmatch(worker_code) is None:
        raise ApiError("platform_job.request_invalid")
    ttl = lease_seconds or int(getattr(config, "platform_job_lease_seconds", DEFAULT_LEASE_SECONDS))
    if not 10 <= ttl <= 300:
        raise ApiError("platform_job.request_invalid")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _database_now(connection)
        if outer_fence is not None:
            _live_outer_fence(connection, config, outer_fence, now=now)
        _recover_expired_leases(connection, now)
        job = connection.execute(
            """
            SELECT job.*,type_record.timeout_seconds,queue.max_concurrency AS queue_max_concurrency
            FROM platform_jobs job
            JOIN platform_job_types type_record ON type_record.type_code=job.type_code
            JOIN platform_work_queues queue ON queue.queue_code=job.queue_code
            WHERE job.status='queued' AND julianday(job.scheduled_at)<=julianday(?)
              AND job.attempt_count<20
              AND type_record.status='active'
              AND (SELECT COUNT(*) FROM platform_scheduler_leases active_lease
                   JOIN platform_jobs active_job
                     ON active_job.platform_job_id=active_lease.platform_job_id
                   WHERE active_lease.status='active'
                     AND julianday(active_lease.expires_at)>julianday(?)
                     AND active_job.queue_code=job.queue_code)
                + (SELECT COUNT(*) FROM platform_job_resumption_leases resume
                   JOIN platform_jobs active_job
                     ON active_job.platform_job_id=resume.platform_job_id
                   WHERE resume.status='active' AND julianday(resume.expires_at)>julianday(?)
                     AND active_job.queue_code=job.queue_code) < queue.max_concurrency
            ORDER BY job.scheduled_at,
              (job.priority + queue.priority_weight) DESC,
              job.created_at,job.platform_job_id
            LIMIT 1
            """ + (
                " FOR UPDATE OF job, queue SKIP LOCKED"
                if getattr(connection, "backend", "sqlite") == "postgresql" else ""
            ),
            (_db_time(now), _db_time(now), _db_time(now)),
        ).fetchone()
        if job is None:
            connection.commit()
            return None
        if getattr(connection, "backend", "sqlite") == "postgresql":
            # The queue lock can be acquired after the SELECT's original snapshot.
            now = _database_now(connection)
            active_count = connection.execute(
                """SELECT COUNT(*) FROM platform_scheduler_leases active_lease
                   JOIN platform_jobs active_job
                     ON active_job.platform_job_id=active_lease.platform_job_id
                   WHERE active_lease.status='active'
                     AND julianday(active_lease.expires_at)>julianday(?)
                     AND active_job.queue_code=?""",
                (_db_time(now), job["queue_code"]),
            ).fetchone()[0]
            resume_count = connection.execute(
                """SELECT COUNT(*) FROM platform_job_resumption_leases lease
                   JOIN platform_jobs job ON job.platform_job_id=lease.platform_job_id
                   WHERE lease.status='active' AND julianday(lease.expires_at)>julianday(?)
                     AND job.queue_code=?""",
                (_db_time(now), job["queue_code"]),
            ).fetchone()[0]
            if int(active_count) + int(resume_count) >= int(job["queue_max_concurrency"]):
                connection.commit()
                return None
        if job["type_code"] == "console.domain.migrate" and outer_fence is None:
            raise ApiError("platform_job.lease_lost")
        generation = int(job["generation"]) + 1
        attempt_number = int(job["attempt_count"]) + 1
        attempt_id = _id("pja_")
        lease_id = _id("pjl_")
        token = secrets.token_urlsafe(32)
        expires = now + timedelta(seconds=min(ttl, int(job["timeout_seconds"])))
        changed_at = _changed_time(now, job["updated_at"])
        updated = connection.execute(
            """
            UPDATE platform_jobs SET status='leased',revision=revision+1,generation=?,
              attempt_count=?,safe_outcome_code=NULL,completed_at=NULL,updated_at=?
            WHERE platform_job_id=? AND status='queued' AND generation=? AND revision=?
            """,
            (
                generation,
                attempt_number,
                _db_time(changed_at),
                job["platform_job_id"],
                int(job["generation"]),
                int(job["revision"]),
            ),
        ).rowcount
        if updated != 1:
            connection.rollback()
            return None
        connection.execute(
            """
            INSERT INTO platform_job_attempts(
              platform_job_attempt_id,platform_job_id,attempt_number,generation,worker_code,
              status,safe_outcome_code,leased_at,started_at,completed_at)
            VALUES(?,?,?,?,?,'leased',NULL,?,NULL,NULL)
            """,
            (
                attempt_id,
                job["platform_job_id"],
                attempt_number,
                generation,
                worker_code,
                _db_time(now),
            ),
        )
        connection.execute(
            """
            INSERT INTO platform_scheduler_leases(
              platform_scheduler_lease_id,platform_job_id,platform_job_attempt_id,
              generation,worker_code,lease_token_hmac,status,expires_at,acquired_at,released_at)
            VALUES(?,?,?,?,?,?,'active',?,?,NULL)
            """,
            (
                lease_id,
                job["platform_job_id"],
                attempt_id,
                generation,
                worker_code,
                _lease_hmac(config, token),
                _db_time(expires),
                _db_time(now),
            ),
        )
        connection.commit()
        return PlatformJobLease(
            job_id=str(job["platform_job_id"]),
            attempt_id=attempt_id,
            type_code=str(job["type_code"]),
            generation=generation,
            attempt_number=attempt_number,
            worker_code=worker_code,
            lease_token=token,
            expires_at=expires,
            input_envelope=str(job["input_envelope"]),
            input_sha256=str(job["input_sha256"]),
            outer_fence=outer_fence,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("platform_job.unavailable") from None
    finally:
        connection.close()


def heartbeat_platform_job(config: AppConfig, lease: PlatformJobLease) -> bool:
    # Heartbeats must not borrow a handler's connection or retain its transaction.
    with database_request_scope(), database_operation_deadline(time.monotonic() + 2.0):
        try:
            return _heartbeat_platform_job(config, lease)
        except (ApiError, sqlite3.Error, DbError, TypeError, ValueError):
            return False


def _heartbeat_platform_job(config: AppConfig, lease: PlatformJobLease) -> bool:
    connection = _connect(config)
    try:
        if database_backend(config.database_url) == "sqlite":
            connection.execute("PRAGMA busy_timeout = 1000")
        connection.execute("BEGIN IMMEDIATE")
        now = _database_now(connection)
        job = _live_lease(connection, config, lease)
        _cancel_if_requested(connection, job, lease)
        ttl = int(getattr(config, "platform_job_lease_seconds", DEFAULT_LEASE_SECONDS))
        updated = connection.execute(
            """UPDATE platform_scheduler_leases SET expires_at=?
               WHERE platform_job_attempt_id=? AND status='active'
                 AND generation=? AND worker_code=? AND lease_token_hmac=?""",
            (
                _db_time(min(now + timedelta(seconds=ttl), _attempt_deadline(job))),
                lease.attempt_id,
                lease.generation,
                lease.worker_code,
                _lease_hmac(config, lease.lease_token),
            ),
        ).rowcount
        connection.commit()
        return updated == 1
    except PlatformJobCancelled:
        _commit_if_open(connection)
        return False
    except PlatformJobFailure as exc:
        _rollback(connection)
        _fail_platform_job(config, lease, exc.code, retryable=False)
        return False
    except ApiError:
        _rollback(connection)
        return False
    except (sqlite3.Error, DbError, TypeError, ValueError):
        _rollback(connection)
        return False
    finally:
        connection.close()


@contextmanager
def _renew_platform_job(config: AppConfig, lease: PlatformJobLease) -> Iterator[None]:
    if not heartbeat_platform_job(config, lease):
        raise ApiError("platform_job.lease_lost")
    stopped = threading.Event()

    def renew() -> None:
        while not stopped.wait(1.0):
            if not heartbeat_platform_job(config, lease):
                return

    worker = threading.Thread(
        target=renew, name=f"platform-job-heartbeat-{lease.attempt_id}", daemon=True
    )
    worker.start()
    try:
        yield
    finally:
        stopped.set()
        worker.join()


def run_platform_job_once(
    config: AppConfig,
    *,
    worker_code: str,
    outer_fence: PlatformJobOuterFence | None = None,
) -> bool:
    with database_request_scope(reuse_connections=True):
        return _run_platform_job_once_scoped(
            config,
            worker_code=worker_code,
            outer_fence=outer_fence,
        )


def _run_platform_job_once_scoped(
    config: AppConfig,
    *,
    worker_code: str,
    outer_fence: PlatformJobOuterFence | None,
) -> bool:
    from .external import run_external_job_once

    if run_external_job_once(config, worker_code=worker_code, outer_fence=outer_fence):
        return True
    lease = claim_platform_job(
        config,
        worker_code=worker_code,
        outer_fence=outer_fence,
    )
    if lease is None:
        return False
    try:
        with _renew_platform_job(config, lease):
            raw_input = decrypt_job_value(
                config,
                lease.input_envelope,
                expected_sha256=lease.input_sha256,
                kind="input",
                owner_id=lease.job_id,
                type_code=lease.type_code,
            )
            payload = validate_job_input(lease.type_code, raw_input)
            definition = handler_definition(lease.type_code)
            if definition is None:
                raise PlatformJobFailure("platform_job.type_unavailable")
            previous = _latest_checkpoint(config, lease)
            if not _start_platform_job(config, lease):
                return True
            context = PlatformJobContext(config=config, lease=lease, previous_checkpoint=previous)
            outcome = definition.handler(context, payload)
            _valid_outcome(outcome)
            _complete_platform_job(config, lease, outcome)
    except (PlatformJobCancelled, PlatformJobDeferred):
        pass
    except PlatformJobRetry as exc:
        _fail_platform_job(config, lease, exc.code, retryable=True)
    except PlatformJobFailure as exc:
        _fail_platform_job(config, lease, exc.code, retryable=False)
    except ApiError as exc:
        retryable = exc.code in {"platform_job.kms_unavailable", "platform_job.unavailable"}
        _fail_platform_job(config, lease, exc.code, retryable=retryable)
    except Exception:
        _fail_platform_job(
            config, lease, "platform_job.execution_failed", retryable=False
        )
    return True


def fence_platform_worker(config: AppConfig, *, worker_code: str) -> int:
    """Expire this process's remaining leases during bounded shutdown."""

    if _WORKER_RE.fullmatch(worker_code) is None:
        raise ApiError("platform_job.request_invalid")
    from .external import fence_external_worker_in_transaction

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _database_now(connection)
        updated = connection.execute(
            """UPDATE platform_scheduler_leases SET expires_at=?
               WHERE worker_code=? AND status='active'""",
            (_db_time(now), worker_code),
        ).rowcount
        _recover_expired_leases(connection, now)
        updated += fence_external_worker_in_transaction(
            connection, worker_code=worker_code, now=now
        )
        connection.commit()
        return int(updated)
    except (sqlite3.Error, DbError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("platform_job.unavailable") from None
    finally:
        connection.close()


def _start_platform_job(config: AppConfig, lease: PlatformJobLease) -> bool:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        job = _live_lease(connection, config, lease)
        _cancel_if_requested(connection, job, lease)
        now = _database_now(connection)
        changed_at = _changed_time(now, job["updated_at"])
        if connection.execute(
            """UPDATE platform_jobs SET status='running',revision=revision+1,
                 started_at=coalesce(started_at,?),updated_at=?
               WHERE platform_job_id=? AND generation=? AND status='leased'""",
            (
                _db_time(now),
                _db_time(changed_at),
                lease.job_id,
                lease.generation,
            ),
        ).rowcount != 1:
            raise ApiError("platform_job.lease_lost")
        connection.execute(
            "UPDATE platform_job_attempts SET status='running',started_at=? "
            "WHERE platform_job_attempt_id=? AND status='leased'",
            (_db_time(now), lease.attempt_id),
        )
        current = connection.execute(
            "SELECT * FROM platform_jobs WHERE platform_job_id=?", (lease.job_id,)
        ).fetchone()
        _event(connection, current, lease.attempt_id, "platform_job.started", None, now)
        connection.commit()
        return True
    except PlatformJobCancelled:
        _commit_if_open(connection)
        return False
    except (ApiError, PlatformJobFailure):
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("platform_job.unavailable") from None
    finally:
        connection.close()


def _complete_platform_job(config: AppConfig, lease: PlatformJobLease, outcome: str) -> None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        job = _live_lease(connection, config, lease)
        _cancel_if_requested(connection, job, lease)
        now = _database_now(connection)
        connection.execute(
            """UPDATE platform_job_attempts SET status='succeeded',safe_outcome_code=?,
                 completed_at=? WHERE platform_job_attempt_id=? AND status='running'""",
            (outcome, _db_time(now), lease.attempt_id),
        )
        _release_lease(connection, config, lease, "released", now)
        changed_at = _changed_time(now, job["updated_at"])
        if connection.execute(
            """UPDATE platform_jobs SET status='succeeded',revision=revision+1,
                 safe_outcome_code=?,completed_at=?,updated_at=?
               WHERE platform_job_id=? AND generation=? AND status='running'""",
            (outcome, _db_time(now), _db_time(changed_at), lease.job_id, lease.generation),
        ).rowcount != 1:
            raise ApiError("platform_job.lease_lost")
        current = connection.execute(
            "SELECT * FROM platform_jobs WHERE platform_job_id=?", (lease.job_id,)
        ).fetchone()
        _event(connection, current, lease.attempt_id, "platform_job.completed", outcome, now)
        if lease.outer_fence is not None:
            _live_outer_fence(connection, config, lease.outer_fence)
        _require_attempt_time(job, _database_now(connection))
        connection.commit()
    except PlatformJobCancelled:
        _commit_if_open(connection)
        raise
    except (ApiError, PlatformJobFailure):
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("platform_job.unavailable") from None
    finally:
        connection.close()


def _fail_platform_job(
    config: AppConfig,
    lease: PlatformJobLease,
    outcome: str,
    *,
    retryable: bool,
) -> None:
    _valid_outcome(outcome)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        job = _live_lease(connection, config, lease, require_unexpired=False)
        if job["status"] == "cancel_requested":
            _cancel_if_requested(connection, job, lease)
        now = _database_now(connection)
        if _attempt_deadline(job) <= now:
            outcome = "platform_job.timeout"
            retryable = False
        connection.execute(
            """UPDATE platform_job_attempts SET status='failed',safe_outcome_code=?,
                 completed_at=? WHERE platform_job_attempt_id=?
                 AND status IN('leased','running')""",
            (outcome, _db_time(now), lease.attempt_id),
        )
        _release_lease(connection, config, lease, "released", now)
        _event(connection, job, lease.attempt_id, "platform_job.failed", outcome, now)
        changed_at = _changed_time(now, job["updated_at"])
        if retryable and int(job["attempt_count"]) < int(job["max_attempts"]):
            delay = min(60, 2 ** max(0, int(job["attempt_count"]) - 1))
            connection.execute(
                """UPDATE platform_jobs SET status='queued',revision=revision+1,
                     safe_outcome_code=?,scheduled_at=?,completed_at=NULL,updated_at=?
                   WHERE platform_job_id=? AND generation=?
                     AND status IN('leased','running')""",
                (
                    outcome,
                    _db_time(now + timedelta(seconds=delay)),
                    _db_time(changed_at),
                    lease.job_id,
                    lease.generation,
                ),
            )
        elif retryable:
            _dead_letter(connection, job, lease, outcome, now, changed_at)
        else:
            connection.execute(
                """UPDATE platform_jobs SET status='failed',revision=revision+1,
                     safe_outcome_code=?,completed_at=?,updated_at=?
                   WHERE platform_job_id=? AND generation=?
                     AND status IN('leased','running')""",
                (
                    outcome,
                    _db_time(now),
                    _db_time(changed_at),
                    lease.job_id,
                    lease.generation,
                ),
            )
        connection.commit()
    except PlatformJobCancelled:
        _commit_if_open(connection)
    except (ApiError, sqlite3.Error, DbError, TypeError, ValueError):
        _rollback(connection)
    finally:
        connection.close()


def _recover_expired_leases(connection, now: datetime) -> None:
    rows = connection.execute(
        """SELECT lease.*,job.*,job.status AS job_status,
                  attempt.leased_at AS attempt_leased_at,type_record.timeout_seconds
           FROM platform_scheduler_leases lease
           JOIN platform_jobs job ON job.platform_job_id=lease.platform_job_id
           JOIN platform_job_attempts attempt
             ON attempt.platform_job_attempt_id=lease.platform_job_attempt_id
           JOIN platform_job_types type_record ON type_record.type_code=job.type_code
           WHERE lease.status='active' AND julianday(lease.expires_at)<=julianday(?)
           ORDER BY lease.expires_at,lease.platform_scheduler_lease_id LIMIT ?""" + (
            " FOR UPDATE OF job, lease SKIP LOCKED"
            if getattr(connection, "backend", "sqlite") == "postgresql" else ""
        ),
        (_db_time(now), MAX_EXPIRED_RECOVERY),
    ).fetchall()
    for row in rows:
        timed_out = _attempt_deadline(row) <= now
        outcome = "platform_job.timeout" if timed_out else "platform_job.lease_expired"
        lease = PlatformJobLease(
            job_id=str(row["platform_job_id"]),
            attempt_id=str(row["platform_job_attempt_id"]),
            type_code=str(row["type_code"]),
            generation=int(row["generation"]),
            attempt_number=int(row["attempt_count"]),
            worker_code=str(row["worker_code"]),
            lease_token="",
            expires_at=_time(row["expires_at"]),
            input_envelope="",
            input_sha256="",
        )
        changed_at = _changed_time(now, row["updated_at"])
        if row["job_status"] == "cancel_requested":
            _cancel_job(connection, row, lease, now, changed_at)
            continue
        connection.execute(
            "UPDATE platform_scheduler_leases SET status='expired',released_at=? "
            "WHERE platform_job_attempt_id=? AND status='active'",
            (_db_time(now), lease.attempt_id),
        )
        connection.execute(
            """UPDATE platform_job_attempts SET status=?,
                 safe_outcome_code=?,completed_at=?
               WHERE platform_job_attempt_id=? AND status IN('leased','running')""",
            ("failed" if timed_out else "stale", outcome, _db_time(now), lease.attempt_id),
        )
        _event(connection, row, lease.attempt_id, "platform_job.failed", outcome, now)
        if timed_out:
            connection.execute(
                """UPDATE platform_jobs SET status='failed',revision=revision+1,
                     safe_outcome_code=?,completed_at=?,updated_at=?
                   WHERE platform_job_id=? AND generation=?
                     AND status IN('leased','running')""",
                (outcome, _db_time(now), _db_time(changed_at), lease.job_id, lease.generation),
            )
        elif int(row["attempt_count"]) >= int(row["max_attempts"]):
            _dead_letter(connection, row, lease, outcome, now, changed_at)
        else:
            connection.execute(
                """UPDATE platform_jobs SET status='queued',revision=revision+1,
                     safe_outcome_code=?,scheduled_at=?,updated_at=?
                   WHERE platform_job_id=? AND generation=?
                     AND status IN('leased','running')""",
                (
                    outcome,
                    _db_time(now),
                    _db_time(changed_at),
                    lease.job_id,
                    lease.generation,
                ),
            )


def _dead_letter(
    connection,
    job,
    lease: PlatformJobLease,
    outcome: str,
    now: datetime,
    changed_at: datetime,
) -> None:
    connection.execute(
        """UPDATE platform_jobs SET status='dead_lettered',revision=revision+1,
             safe_outcome_code=?,completed_at=?,updated_at=?
           WHERE platform_job_id=? AND generation=?
             AND status IN('leased','running')""",
        (outcome, _db_time(now), _db_time(changed_at), lease.job_id, lease.generation),
    )
    connection.execute(
        """INSERT INTO platform_dead_letter_jobs(
             platform_dead_letter_job_id,platform_job_id,final_attempt_number,generation,
             safe_reason_code,created_at) VALUES(?,?,?,?,?,?)""",
        (
            _id("pjd_"),
            lease.job_id,
            lease.attempt_number,
            lease.generation,
            outcome,
            _db_time(now),
        ),
    )
    current = connection.execute(
        "SELECT * FROM platform_jobs WHERE platform_job_id=?", (lease.job_id,)
    ).fetchone()
    _event(connection, current, lease.attempt_id, "platform_job.dead_lettered", outcome, now)


def _cancel_if_requested(connection, job, lease: PlatformJobLease) -> None:
    if job["status"] != "cancel_requested":
        return
    now = _database_now(connection)
    changed_at = _changed_time(now, job["updated_at"])
    _cancel_job(connection, job, lease, now, changed_at)
    raise PlatformJobCancelled


def _cancel_job(
    connection,
    job,
    lease: PlatformJobLease,
    now: datetime,
    changed_at: datetime,
) -> None:
    connection.execute(
        """UPDATE platform_job_attempts SET status='cancelled',
             safe_outcome_code='platform_job.cancelled',completed_at=?
           WHERE platform_job_attempt_id=? AND status IN('leased','running')""",
        (_db_time(now), lease.attempt_id),
    )
    connection.execute(
        """UPDATE platform_scheduler_leases SET status='released',released_at=?
           WHERE platform_job_attempt_id=? AND status='active'""",
        (_db_time(now), lease.attempt_id),
    )
    connection.execute(
        """UPDATE platform_jobs SET status='cancelled',revision=revision+1,
             safe_outcome_code='platform_job.cancelled',completed_at=?,updated_at=?
           WHERE platform_job_id=? AND generation=? AND status='cancel_requested'""",
        (_db_time(now), _db_time(changed_at), lease.job_id, lease.generation),
    )
    current = connection.execute(
        "SELECT * FROM platform_jobs WHERE platform_job_id=?", (lease.job_id,)
    ).fetchone()
    _event(
        connection,
        current,
        lease.attempt_id,
        "platform_job.completed",
        "platform_job.cancelled",
        now,
    )


def _release_lease(
    connection,
    config: AppConfig,
    lease: PlatformJobLease,
    status: str,
    now: datetime,
) -> None:
    if connection.execute(
        """UPDATE platform_scheduler_leases SET status=?,released_at=?
           WHERE platform_job_attempt_id=? AND generation=? AND worker_code=?
             AND lease_token_hmac=? AND status='active'""",
        (
            status,
            _db_time(now),
            lease.attempt_id,
            lease.generation,
            lease.worker_code,
            _lease_hmac(config, lease.lease_token),
        ),
    ).rowcount:
        return
    # The token digest cannot be recovered from the database. Compare it before this helper.
    raise ApiError("platform_job.lease_lost")


def _live_lease(
    connection,
    config: AppConfig,
    lease: PlatformJobLease,
    *,
    require_unexpired: bool = True,
):
    if lease.outer_fence is not None:
        _live_outer_fence(connection, config, lease.outer_fence)
    row = connection.execute(
        """SELECT job.*,active_lease.expires_at,
                  attempt.leased_at AS attempt_leased_at,type_record.timeout_seconds
           FROM platform_jobs job
           JOIN platform_scheduler_leases active_lease
             ON active_lease.platform_job_id=job.platform_job_id
           JOIN platform_job_attempts attempt
             ON attempt.platform_job_attempt_id=active_lease.platform_job_attempt_id
           JOIN platform_job_types type_record ON type_record.type_code=job.type_code
           WHERE job.platform_job_id=? AND job.generation=?
             AND active_lease.platform_job_attempt_id=?
             AND active_lease.generation=? AND active_lease.worker_code=?
             AND active_lease.lease_token_hmac=? AND active_lease.status='active'""" + (
            " FOR UPDATE OF job, active_lease"
            if getattr(connection, "backend", "sqlite") == "postgresql" else ""
        ),
        (
            lease.job_id,
            lease.generation,
            lease.attempt_id,
            lease.generation,
            lease.worker_code,
            _lease_hmac(config, lease.lease_token),
        ),
    ).fetchone()
    if row is None:
        raise ApiError("platform_job.lease_lost")
    if row["status"] not in {"leased", "running", "cancel_requested"}:
        raise ApiError("platform_job.lease_lost")
    if require_unexpired:
        _require_attempt_time(row, _database_now(connection))
    return row


def _require_attempt_time(job, now: datetime) -> None:
    if _attempt_deadline(job) <= now:
        raise PlatformJobFailure("platform_job.timeout")
    if _time(job["expires_at"]) <= now:
        raise ApiError("platform_job.lease_lost")


def _attempt_deadline(job) -> datetime:
    return _time(job["attempt_leased_at"]) + timedelta(seconds=int(job["timeout_seconds"]))


def _live_outer_fence(
    connection,
    config: AppConfig,
    fence: PlatformJobOuterFence,
    *,
    now: datetime | None = None,
) -> None:
    current = now or _database_now(connection)
    row = connection.execute(
        """SELECT lease.fencing_generation,lease.lease_deadline_at,
                  head.generation registry_generation
           FROM scheduler_leases lease
           JOIN scheduler_registry_heads head ON head.head_key='production'
           WHERE lease.scheduler_code=? AND lease.partition_code=?
             AND lease.fencing_generation=? AND lease.lease_token_hmac=?
             AND julianday(lease.lease_deadline_at)>julianday(?)""" + (
            " FOR SHARE OF lease, head"
            if getattr(connection, "backend", "sqlite") == "postgresql" else ""
        ),
        (
            fence.scheduler_code,
            fence.partition_code,
            fence.fencing_generation,
            _scheduler_lease_hmac(config, fence.lease_token),
            _db_time(current),
        ),
    ).fetchone()
    if (
        row is None or int(row["registry_generation"]) != fence.registry_generation
        or _time(row["lease_deadline_at"]) <= _database_now(connection)
    ):
        raise ApiError("platform_job.lease_lost")


def require_platform_job_outer_fence(
    connection,
    config: AppConfig,
    fence: PlatformJobOuterFence,
    *,
    now: datetime | None = None,
) -> None:
    """Validate a platform-job registry fence inside a caller-owned transaction."""

    _live_outer_fence(connection, config, fence, now=now)


def _latest_checkpoint(config: AppConfig, lease: PlatformJobLease) -> object | None:
    connection = _connect(config)
    try:
        row = connection.execute(
            """SELECT checkpoint_envelope,checkpoint_sha256
               FROM platform_job_checkpoints WHERE platform_job_id=?
               ORDER BY generation DESC,sequence_number DESC LIMIT 1""",
            (lease.job_id,),
        ).fetchone()
    except (sqlite3.Error, DbError):
        raise ApiError("platform_job.unavailable") from None
    finally:
        connection.close()
    if row is None:
        return None
    return decrypt_job_value(
        config,
        str(row["checkpoint_envelope"]),
        expected_sha256=str(row["checkpoint_sha256"]),
        kind="checkpoint",
        owner_id=lease.job_id,
        type_code=lease.type_code,
    )


def _event(
    connection,
    job,
    attempt_id: str | None,
    event_type: str,
    outcome: str | None,
    now: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO platform_job_event_outbox(
          platform_job_event_outbox_id,platform_job_id,platform_job_attempt_id,event_type,
          type_code,generation,safe_outcome_code,request_id,trace_id,status,attempt_count,
          next_attempt_at,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,'pending',0,?,?,?)
        """,
        (
            _id("pjo_"),
            job["platform_job_id"],
            attempt_id,
            event_type,
            job["type_code"],
            int(job["generation"]),
            outcome,
            job["request_id"],
            job["trace_id"],
            _db_time(now),
            _db_time(now),
            _db_time(now),
        ),
    )


def _probe_handler(context: PlatformJobContext, payload: PlatformRuntimeProbeInput) -> str:
    context.checkpoint({"marker": payload.marker, "phase": "started"})
    if payload.outcome == "retry":
        raise PlatformJobRetry("platform_job.probe_retry")
    if payload.outcome == "fail":
        raise PlatformJobFailure("platform_job.probe_failed")
    context.commit_effect(
        "probe.completed",
        {"marker_sha256": hashlib.sha256(payload.marker.encode("utf-8")).hexdigest()},
    )
    return "platform_job.probe_completed"


def _connect(config: AppConfig):
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("platform_job.unavailable") from None


def _database_now(connection) -> datetime:
    # PostgreSQL CURRENT_TIMESTAMP is frozen at BEGIN, including a slow effect callback.
    query = (
        "SELECT clock_timestamp()"
        if getattr(connection, "backend", "sqlite") == "postgresql"
        else "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')"
    )
    return _time(connection.execute(query).fetchone()[0])


def _changed_time(now: datetime, previous: object) -> datetime:
    now = now.replace(microsecond=now.microsecond // 1000 * 1000)
    prior = _time(previous)
    return now if now > prior else prior + timedelta(milliseconds=1)


def _lease_hmac(config: AppConfig | None, token: str) -> str:
    if config is None:
        return ""
    return hmac.new(
        config.platform_session_secret.encode("utf-8"),
        b"prd106/lease/v1\0" + token.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _scheduler_lease_hmac(config: AppConfig, token: str) -> str:
    return hmac.new(
        config.platform_session_secret.encode(),
        f"prd147/scheduler/lease/v1\x00{token}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _digest(value: object) -> str:
    try:
        canonical = json.dumps(
            value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        raise ApiError("platform_job.input_invalid") from None
    return hashlib.sha256(canonical).hexdigest()


def _valid_outcome(value: str) -> None:
    if not isinstance(value, str) or _OUTCOME_RE.fullmatch(value) is None:
        raise PlatformJobFailure("platform_job.outcome_invalid")


def _rollback(connection) -> None:
    try:
        if connection.in_transaction:
            connection.rollback()
    except Exception:
        pass


def _commit_if_open(connection) -> None:
    if connection.in_transaction:
        connection.commit()


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _time(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


register_job_handler(
    "platform.runtime.probe",
    input_model=PlatformRuntimeProbeInput,
    handler=_probe_handler,
)


__all__ = [
    "PlatformJobCancelled",
    "PlatformJobContext",
    "PlatformJobFailure",
    "PlatformJobLease",
    "PlatformJobOuterFence",
    "PlatformJobRetry",
    "claim_platform_job",
    "fence_platform_worker",
    "heartbeat_platform_job",
    "require_platform_job_outer_fence",
    "run_platform_job_once",
]
