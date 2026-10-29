"""Durable external-work observation without spending the root retry budget."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from . import runtime as r
from .catalog import handler_definition
from .crypto import decrypt_job_value, encrypt_job_value

RESUMPTION_SECONDS = 45
POLL_SECONDS = 5


@dataclass(frozen=True, slots=True, repr=False)
class ExternalJobLease:
    job_id: str
    attempt_id: str
    job_generation: int
    resume_generation: int
    type_code: str
    worker_code: str
    lease_token: str
    continuation_envelope: str
    continuation_sha256: str
    acknowledged: bool
    outer_fence: r.PlatformJobOuterFence | None = None


def defer_platform_job(
    context: r.PlatformJobContext,
    value: object,
    *,
    timeout_seconds: int,
    apply: Callable[[Any], None] | None,
) -> None:
    definition = handler_definition(context.lease.type_code)
    if (
        definition is None
        or definition.external_handler is None
        or type(timeout_seconds) is not int
        or not 1 <= timeout_seconds <= 7 * 86400
    ):
        raise ApiError("platform_job.input_invalid")
    prepared = encrypt_job_value(
        context.config,
        value,
        kind="checkpoint",
        owner_id=context.lease.job_id,
        type_code=context.lease.type_code + ":external-v1",
    )
    connection = r._connect(context.config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        job = r._live_lease(connection, context.config, context.lease)
        r._cancel_if_requested(connection, job, context.lease)
        if job["status"] != "running":
            raise ApiError("platform_job.lease_lost")
        if apply is not None:
            apply(connection)
        job = r._live_lease(connection, context.config, context.lease)
        r._cancel_if_requested(connection, job, context.lease)
        now = r._database_now(connection)
        stamp = r._db_time(now)
        connection.execute(
            """INSERT INTO platform_job_external_waits(
                 platform_job_id,platform_job_attempt_id,job_generation,status,resume_generation,
                 continuation_envelope,continuation_sha256,acknowledged,observation_count,
                 failure_count,deadline_at,next_poll_at,created_at,updated_at)
               VALUES(?,?,?,'waiting',0,?,?,0,0,0,?,?,?,?)""",
            (
                context.lease.job_id,
                context.lease.attempt_id,
                context.lease.generation,
                prepared.envelope,
                prepared.plaintext_sha256,
                r._db_time(now + timedelta(seconds=timeout_seconds)),
                stamp,
                stamp,
                stamp,
            ),
        )
        r._release_lease(connection, context.config, context.lease, "released", now)
        if context.lease.outer_fence is not None:
            r._live_outer_fence(connection, context.config, context.lease.outer_fence)
        r._require_attempt_time(job, r._database_now(connection))
        connection.commit()
    except r.PlatformJobCancelled:
        # Roll back domain changes before the ordinary cancellation path finalizes.
        r._rollback(connection)
        r._fail_platform_job(
            context.config, context.lease, "platform_job.cancelled", retryable=False
        )
        raise
    except BaseException:
        r._rollback(connection)
        raise
    finally:
        connection.close()
    raise r.PlatformJobDeferred


def _active_count(connection, queue_code: str, now) -> int:
    return sum(
        int(
            connection.execute(
                f"""SELECT COUNT(*) FROM {table} lease
            JOIN platform_jobs job ON job.platform_job_id=lease.platform_job_id
            WHERE lease.status='active' AND julianday(lease.expires_at)>julianday(?)
              AND job.queue_code=?""",
                (r._db_time(now), queue_code),
            ).fetchone()[0]
        )
        for table in ("platform_scheduler_leases", "platform_job_resumption_leases")
    )


def _recover(connection, now) -> None:
    expired = connection.execute(
        """SELECT lease.* FROM platform_job_resumption_leases lease
           JOIN platform_jobs job ON job.platform_job_id=lease.platform_job_id
           WHERE lease.status='active' AND julianday(lease.expires_at)<=julianday(?)
           ORDER BY lease.expires_at,lease.resumption_lease_id LIMIT ?"""
        + (
            " FOR UPDATE OF job, lease SKIP LOCKED"
            if getattr(connection, "backend", "sqlite") == "postgresql"
            else ""
        ),
        (r._db_time(now), r.MAX_EXPIRED_RECOVERY),
    ).fetchall()
    _expire_rows(connection, expired, now)


def _expire_rows(connection, rows, now) -> None:
    for row in rows:
        connection.execute(
            "UPDATE platform_job_resumption_leases SET status='expired',released_at=? "
            "WHERE resumption_lease_id=? AND status='active'",
            (r._db_time(now), row["resumption_lease_id"]),
        )
        connection.execute(
            """UPDATE platform_job_external_waits SET status='waiting',next_poll_at=?,updated_at=?,
                 failure_count=failure_count+1,safe_outcome_code='platform_job.lease_expired'
               WHERE platform_job_id=? AND resume_generation=? AND status='leased'""",
            (r._db_time(now), r._db_time(now), row["platform_job_id"], row["resume_generation"]),
        )


def fence_external_worker_in_transaction(connection, *, worker_code: str, now) -> int:
    rows = connection.execute(
        """SELECT lease.* FROM platform_job_resumption_leases lease
           JOIN platform_jobs job ON job.platform_job_id=lease.platform_job_id
           WHERE lease.worker_code=? AND lease.status='active'"""
        + (
            " FOR UPDATE OF job, lease"
            if getattr(connection, "backend", "sqlite") == "postgresql"
            else ""
        ),
        (worker_code,),
    ).fetchall()
    _expire_rows(connection, rows, now)
    return len(rows)


def claim_external_job(
    config: AppConfig,
    *,
    worker_code: str,
    outer_fence: r.PlatformJobOuterFence | None = None,
) -> ExternalJobLease | None:
    if r._WORKER_RE.fullmatch(worker_code) is None:
        raise ApiError("platform_job.request_invalid")
    connection = r._connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = r._database_now(connection)
        if outer_fence is not None:
            r._live_outer_fence(connection, config, outer_fence, now=now)
        _recover(connection, now)
        row = connection.execute(
            """SELECT w.*,j.type_code,j.queue_code,q.max_concurrency
               FROM platform_job_external_waits w
               JOIN platform_jobs j ON j.platform_job_id=w.platform_job_id
               JOIN platform_work_queues q ON q.queue_code=j.queue_code
               WHERE w.status='waiting' AND julianday(w.next_poll_at)<=julianday(?)
                 AND j.status IN('running','cancel_requested') AND j.generation=w.job_generation
                 AND (SELECT COUNT(*) FROM platform_scheduler_leases active
                      JOIN platform_jobs owner ON owner.platform_job_id=active.platform_job_id
                      WHERE active.status='active' AND owner.queue_code=j.queue_code
                        AND julianday(active.expires_at)>julianday(?))
                   + (SELECT COUNT(*) FROM platform_job_resumption_leases active
                      JOIN platform_jobs owner ON owner.platform_job_id=active.platform_job_id
                      WHERE active.status='active' AND owner.queue_code=j.queue_code
                        AND julianday(active.expires_at)>julianday(?)) < q.max_concurrency
                 AND NOT EXISTS(SELECT 1 FROM platform_jobs pending
                   JOIN platform_job_types t ON t.type_code=pending.type_code
                   WHERE pending.status='queued' AND t.status='active' AND pending.attempt_count<20
                     AND pending.queue_code=j.queue_code
                     AND julianday(pending.scheduled_at)<julianday(w.next_poll_at))
               ORDER BY w.next_poll_at,j.priority DESC,w.platform_job_id LIMIT 1"""
            + (
                " FOR UPDATE OF j, w, q SKIP LOCKED"
                if getattr(connection, "backend", "sqlite") == "postgresql"
                else ""
            ),
            (r._db_time(now), r._db_time(now), r._db_time(now)),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        now = r._database_now(connection)
        if _active_count(connection, str(row["queue_code"]), now) >= int(row["max_concurrency"]):
            connection.commit()
            return None
        token = secrets.token_urlsafe(32)
        generation = int(row["resume_generation"]) + 1
        updated = connection.execute(
            "UPDATE platform_job_external_waits SET status='leased',"
            "resume_generation=?,updated_at=? "
            "WHERE platform_job_id=? AND status='waiting' AND resume_generation=?",
            (generation, r._db_time(now), row["platform_job_id"], row["resume_generation"]),
        ).rowcount
        if updated != 1:
            raise ApiError("platform_job.lease_lost")
        connection.execute(
            """INSERT INTO platform_job_resumption_leases(
                 resumption_lease_id,platform_job_id,resume_generation,worker_code,
                 lease_token_hmac,status,acquired_at,expires_at)
               VALUES(?,?,?,?,?,'active',?,?)""",
            (
                r._id("pjr_"),
                row["platform_job_id"],
                generation,
                worker_code,
                r._lease_hmac(config, token),
                r._db_time(now),
                r._db_time(now + timedelta(seconds=RESUMPTION_SECONDS)),
            ),
        )
        if outer_fence is not None:
            r._live_outer_fence(connection, config, outer_fence)
        connection.commit()
        return ExternalJobLease(
            job_id=str(row["platform_job_id"]),
            attempt_id=str(row["platform_job_attempt_id"]),
            job_generation=int(row["job_generation"]),
            resume_generation=generation,
            type_code=str(row["type_code"]),
            worker_code=worker_code,
            lease_token=token,
            continuation_envelope=str(row["continuation_envelope"]),
            continuation_sha256=str(row["continuation_sha256"]),
            acknowledged=bool(row["acknowledged"]),
            outer_fence=outer_fence,
        )
    except BaseException:
        r._rollback(connection)
        raise
    finally:
        connection.close()


def _live(connection, config: AppConfig, lease: ExternalJobLease):
    if lease.outer_fence is not None:
        r._live_outer_fence(connection, config, lease.outer_fence)
    row = connection.execute(
        """SELECT j.*,w.deadline_at,w.failure_count,w.acknowledged,l.expires_at
           FROM platform_jobs j
           JOIN platform_job_external_waits w ON w.platform_job_id=j.platform_job_id
           JOIN platform_job_resumption_leases l ON l.platform_job_id=w.platform_job_id
           JOIN platform_job_attempts a ON a.platform_job_attempt_id=w.platform_job_attempt_id
           WHERE j.platform_job_id=? AND j.generation=?
             AND j.status IN('running','cancel_requested')
             AND w.job_generation=j.generation AND w.platform_job_attempt_id=?
             AND a.status='running' AND a.generation=j.generation
             AND a.platform_job_id=j.platform_job_id
             AND w.status='leased' AND w.resume_generation=?
             AND l.resume_generation=w.resume_generation
             AND l.worker_code=? AND l.lease_token_hmac=? AND l.status='active'"""
        + (
            " FOR UPDATE OF j, w, l"
            if getattr(connection, "backend", "sqlite") == "postgresql"
            else ""
        ),
        (
            lease.job_id,
            lease.job_generation,
            lease.attempt_id,
            lease.resume_generation,
            lease.worker_code,
            r._lease_hmac(config, lease.lease_token),
        ),
    ).fetchone()
    if row is None or r._time(row["expires_at"]) <= r._database_now(connection):
        raise ApiError("platform_job.lease_lost")
    return row


def _stop_reason(row, now) -> str | None:
    if row["status"] == "cancel_requested":
        return "platform_job.cancelled"
    if r._time(row["deadline_at"]) <= now:
        return "platform_job.timeout"
    return None


def _release(connection, lease: ExternalJobLease, now) -> None:
    if (
        connection.execute(
            "UPDATE platform_job_resumption_leases SET status='released',released_at=? "
            "WHERE platform_job_id=? AND resume_generation=? AND status='active'",
            (r._db_time(now), lease.job_id, lease.resume_generation),
        ).rowcount
        != 1
    ):
        raise ApiError("platform_job.lease_lost")


@dataclass(slots=True, repr=False)
class ExternalJobContext:
    config: AppConfig
    lease: ExternalJobLease
    resolved: bool = False

    def stop_reason(self) -> str | None:
        connection = r._connect(self.config)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = _live(connection, self.config, self.lease)
            reason = _stop_reason(row, r._database_now(connection))
            connection.commit()
            return reason
        finally:
            r._rollback(connection)
            connection.close()

    def commit_effect(
        self,
        effect_key: str,
        value: object,
        apply: Callable[[Any], None] | None = None,
    ) -> bool:
        """Commit one short database phase without ending or releasing the external wait."""
        if r._EFFECT_RE.fullmatch(effect_key) is None:
            raise ApiError("platform_job.input_invalid")
        digest = r._digest(value)
        connection = r._connect(self.config)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = _live(connection, self.config, self.lease)
            reason = _stop_reason(row, r._database_now(connection))
            if reason is not None:
                raise ApiError(reason)
            committed = r._commit_effect_in_transaction(
                connection,
                job_id=self.lease.job_id,
                generation=self.lease.job_generation,
                effect_key=effect_key,
                digest=digest,
                apply=apply,
            )
            row = _live(connection, self.config, self.lease)
            reason = _stop_reason(row, r._database_now(connection))
            if reason is not None:
                raise ApiError(reason)
            connection.commit()
            return committed
        finally:
            r._rollback(connection)
            connection.close()

    def defer(self, *, acknowledged: bool = False, outcome: str | None = None) -> str:
        if outcome is not None:
            r._valid_outcome(outcome)
        connection = r._connect(self.config)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = _live(connection, self.config, self.lease)
            now = r._database_now(connection)
            delay = (
                POLL_SECONDS if outcome is None else min(30, 2 ** min(5, int(row["failure_count"])))
            )
            connection.execute(
                """UPDATE platform_job_external_waits SET status='waiting',acknowledged=?,
                     observation_count=observation_count+1,failure_count=failure_count+?,
                     safe_outcome_code=?,next_poll_at=?,updated_at=? WHERE platform_job_id=?""",
                (
                    int(bool(row["acknowledged"]) or acknowledged),
                    int(outcome is not None),
                    outcome,
                    r._db_time(now + timedelta(seconds=delay)),
                    r._db_time(now),
                    self.lease.job_id,
                ),
            )
            _release(connection, self.lease, now)
            if self.lease.outer_fence is not None:
                r._live_outer_fence(connection, self.config, self.lease.outer_fence)
            if r._time(row["expires_at"]) <= r._database_now(connection):
                raise ApiError("platform_job.lease_lost")
            connection.commit()
            self.resolved = True
            return "platform_job.external_wait"
        finally:
            r._rollback(connection)
            connection.close()

    def finish(
        self,
        *,
        status: Literal["succeeded", "failed", "cancelled"],
        outcome: str,
        value: object,
        stop_confirmed: bool,
        apply: Callable[[Any, str, str], None] | None = None,
    ) -> str:
        r._valid_outcome(outcome)
        if status not in {"succeeded", "failed", "cancelled"} or stop_confirmed is not True:
            raise ApiError("platform_job.input_invalid")
        connection = r._connect(self.config)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = _live(connection, self.config, self.lease)
            now = r._database_now(connection)
            reason = _stop_reason(row, now)
            if reason is not None:
                status = "cancelled" if reason == "platform_job.cancelled" else "failed"
                outcome = reason
            if apply is not None:
                apply(connection, status, outcome)
            current = _live(connection, self.config, self.lease)
            now = r._database_now(connection)
            if _stop_reason(current, now) != reason:
                raise ApiError("platform_job.lease_lost")
            connection.execute(
                """INSERT INTO platform_job_effects(platform_job_effect_id,platform_job_id,
                     generation,effect_key,effect_sha256,committed_at)
                   VALUES(?,?,?,'external.complete',?,?)""",
                (
                    r._id("pje_"),
                    self.lease.job_id,
                    self.lease.job_generation,
                    r._digest({"status": status, "outcome": outcome, "value": value}),
                    r._db_time(now),
                ),
            )
            connection.execute(
                """UPDATE platform_job_external_waits
                   SET status='completed',completed_at=?,updated_at=?,
                     observation_count=observation_count+1,safe_outcome_code=?
                   WHERE platform_job_id=?""",
                (r._db_time(now), r._db_time(now), outcome, self.lease.job_id),
            )
            _release(connection, self.lease, now)
            connection.execute(
                "UPDATE platform_job_attempts SET status=?,safe_outcome_code=?,completed_at=? "
                "WHERE platform_job_attempt_id=? AND status='running'",
                (status, outcome, r._db_time(now), self.lease.attempt_id),
            )
            connection.execute(
                "UPDATE platform_jobs SET status=?,safe_outcome_code=?,completed_at=?,"
                "revision=revision+1,updated_at=? WHERE platform_job_id=? AND generation=?",
                (
                    status,
                    outcome,
                    r._db_time(now),
                    r._db_time(r._changed_time(now, row["updated_at"])),
                    self.lease.job_id,
                    self.lease.job_generation,
                ),
            )
            r._event(
                connection,
                row,
                self.lease.attempt_id,
                "platform_job.failed" if status == "failed" else "platform_job.completed",
                outcome,
                now,
            )
            # Keep the outer fence and resumption deadline valid through terminal writes.
            if self.lease.outer_fence is not None:
                r._live_outer_fence(connection, self.config, self.lease.outer_fence)
            if r._time(row["expires_at"]) <= r._database_now(connection):
                raise ApiError("platform_job.lease_lost")
            if _stop_reason(row, r._database_now(connection)) != reason:
                raise ApiError("platform_job.lease_lost")
            connection.commit()
            self.resolved = True
            return outcome
        finally:
            r._rollback(connection)
            connection.close()


def run_external_job_once(
    config: AppConfig,
    *,
    worker_code: str,
    outer_fence: r.PlatformJobOuterFence | None = None,
) -> bool:
    lease = claim_external_job(config, worker_code=worker_code, outer_fence=outer_fence)
    if lease is None:
        return False
    context = ExternalJobContext(config, lease)
    try:
        value = decrypt_job_value(
            config,
            lease.continuation_envelope,
            expected_sha256=lease.continuation_sha256,
            kind="checkpoint",
            owner_id=lease.job_id,
            type_code=lease.type_code + ":external-v1",
        )
        definition = handler_definition(lease.type_code)
        if definition is None or definition.external_handler is None:
            return bool(context.defer(outcome="platform_job.type_unavailable"))
        context.stop_reason()
        definition.external_handler(context, value)
        if not context.resolved:
            context.defer(outcome="platform_job.execution_failed")
    except Exception:
        if not context.resolved:
            try:
                context.defer(outcome="platform_job.external_unavailable")
            except Exception:
                # Ownership may be gone or storage unavailable. The durable lease is reclaimable.
                pass
    return True


__all__ = ["ExternalJobContext", "ExternalJobLease", "claim_external_job", "run_external_job_once"]
