"""实验完成事件 Outbox：按租户和租约恢复投递，载荷只包含冻结的安全聚合事实。

Recoverable source outbox for ``experiment.completed`` Webhooks.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.runtime.webhooks import WebhookEvent, build_webhook_event

# 该常量组固定事件租约、重试上限、退避和所有者标识，保证恢复有界。
EVENT_LEASE_SECONDS = 90
EVENT_MAX_ATTEMPTS = 10
EVENT_BACKOFF_SECONDS = (5, 30, 300)
_EVENT_ID = re.compile(r"^exev_[0-9a-f]{32}$")
_EXPERIMENT_ID = re.compile(r"^exp_[0-9a-f]{32}$")
_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


@dataclass(frozen=True, slots=True)
class ExperimentEventClaim:
    """按租户领取一条实验完成事件，并用所有者与租约到期时间约束并发更新。"""
    event_id: str
    tenant_id: str
    experiment_id: str
    actor_id: str
    source_request_id: str
    source_trace_id: str
    occurred_at: datetime
    event: WebhookEvent = field(repr=False)
    claim_token: str = field(default="", repr=False)
    lease_owner_id: str = ""
    dispatch_attempts: int = 0
    lease_generation: int = 0
    lease_expires_at: datetime | None = None


def claim_next_experiment_event(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> ExperimentEventClaim | None:
    """按租户领取一条实验完成事件，并用所有者与租约到期时间约束并发更新。"""
    owner = _worker_id(worker_id)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        current_text = _db_time(current)
        exhausted = connection.execute(
            """
            SELECT tenant_id,experiment_event_id,dispatch_attempts,lease_generation,
                   claim_token,lease_owner_id
            FROM experiment_event_outbox
            WHERE dispatch_status='processing' AND dispatch_attempts>=?
              AND unixepoch(lease_expires_at)<=unixepoch(?)
            ORDER BY tenant_id,experiment_event_id
            LIMIT 1
            """,
            (EVENT_MAX_ATTEMPTS, current_text),
        ).fetchone()
        if exhausted is not None:
            connection.execute(
                """
                UPDATE experiment_event_outbox
                SET dispatch_status='failed',next_attempt_at=NULL,claim_token=NULL,
                    lease_owner_id=NULL,lease_expires_at=NULL,
                    last_error_code='webhook.delivery_unavailable',updated_at=?
                WHERE tenant_id=? AND experiment_event_id=?
                  AND dispatch_status='processing' AND dispatch_attempts=?
                  AND lease_generation=? AND claim_token=? AND lease_owner_id=?
                  AND unixepoch(lease_expires_at)<=unixepoch(?)
                """,
                (
                    current_text,
                    exhausted["tenant_id"],
                    exhausted["experiment_event_id"],
                    exhausted["dispatch_attempts"],
                    exhausted["lease_generation"],
                    exhausted["claim_token"],
                    exhausted["lease_owner_id"],
                    current_text,
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
                raise ApiError("experiment.unavailable")
            _fail_pending_deliveries(
                connection,
                tenant_id=exhausted["tenant_id"],
                event_id=exhausted["experiment_event_id"],
                updated_at=current_text,
                failure_code="webhook.delivery_unavailable",
            )
        row = connection.execute(
            """
            SELECT event.*,outbox.dispatch_attempts,outbox.lease_generation
            FROM experiment_event_outbox outbox
            JOIN experiment_events event
              ON event.tenant_id=outbox.tenant_id
             AND event.experiment_event_id=outbox.experiment_event_id
            WHERE (outbox.dispatch_status='pending'
                AND unixepoch(outbox.next_attempt_at)<=unixepoch(?))
               OR (outbox.dispatch_status='processing'
                AND outbox.dispatch_attempts<?
                AND unixepoch(outbox.lease_expires_at)<=unixepoch(?))
            ORDER BY outbox.created_at,outbox.experiment_event_outbox_id LIMIT 1
            """,
            (current_text, EVENT_MAX_ATTEMPTS, current_text),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        token = uuid4().hex
        attempts = int(row["dispatch_attempts"]) + 1
        generation = int(row["lease_generation"]) + 1
        expires = current + timedelta(seconds=EVENT_LEASE_SECONDS)
        connection.execute(
            """
            UPDATE experiment_event_outbox
            SET dispatch_status='processing',dispatch_attempts=?,lease_generation=?,
                next_attempt_at=NULL,claim_token=?,lease_owner_id=?,lease_expires_at=?,
                last_error_code=NULL,dispatched_at=NULL,updated_at=?
            WHERE tenant_id=? AND experiment_event_id=?
              AND dispatch_attempts=? AND lease_generation=?
              AND dispatch_status IN ('pending','processing')
            """,
            (
                attempts,
                generation,
                token,
                owner,
                _db_time(expires),
                current_text,
                row["tenant_id"],
                row["experiment_event_id"],
                row["dispatch_attempts"],
                row["lease_generation"],
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            connection.rollback()
            return None
        try:
            claim = _claim_from_row(
                connection,
                row,
                token=token,
                owner=owner,
                attempts=attempts,
                generation=generation,
                expires=expires,
            )
        # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
        except (ApiError, TypeError, ValueError):
            _fail_pending_deliveries(
                connection,
                tenant_id=row["tenant_id"],
                event_id=row["experiment_event_id"],
                updated_at=current_text,
                failure_code="webhook.payload_invalid",
            )
            connection.execute(
                """
                UPDATE experiment_event_outbox
                SET dispatch_status='failed',next_attempt_at=NULL,claim_token=NULL,
                    lease_owner_id=NULL,lease_expires_at=NULL,
                    last_error_code='webhook.payload_invalid',updated_at=?
                WHERE tenant_id=? AND experiment_event_id=?
                  AND dispatch_status='processing' AND dispatch_attempts=?
                  AND lease_generation=? AND claim_token=? AND lease_owner_id=?
                """,
                (
                    current_text,
                    row["tenant_id"],
                    row["experiment_event_id"],
                    attempts,
                    generation,
                    token,
                    owner,
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
                raise ApiError("experiment.unavailable") from None
            connection.commit()
            return None
        connection.commit()
        return claim
    # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
    except ApiError:
        connection.rollback()
        raise
    # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
    except (sqlite3.Error, TypeError, ValueError):
        connection.rollback()
        # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
        raise ApiError("experiment.unavailable") from None
    finally:
        connection.close()


def dispatch_claimed_experiment_event(
    config: AppConfig,
    claim: ExperimentEventClaim,
    *,
    now: datetime | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """按租户领取一条实验完成事件，并用所有者与租约到期时间约束并发更新。"""
    def source_guard() -> None:
        """从冻结实验结果重建安全事件载荷，并校验源 request_id 与所有者链。"""
        if should_stop is not None and should_stop():
            # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
            raise ApiError("experiment.unavailable")
        _renew(config, claim, now=now)

    try:
        if should_stop is not None and should_stop():
            # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
            raise ApiError("experiment.unavailable")
        _freeze_snapshot(config, claim, current=now)
        from metahub_control.webhooks.service import dispatch_webhook_event

        dispatch_webhook_event(
            config,
            claim.tenant_id,
            claim.event,
            request_id=f"req_{claim.event_id}",
            trace_id=claim.source_trace_id,
            resume_existing_pending=True,
            source_lease_guard=source_guard,
            create_snapshot_if_missing=False,
        )
        return _finish(config, claim, error_code=None, now=now)
    # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
    except ValueError:
        return _finish(config, claim, error_code="webhook.payload_invalid", now=now)
    except Exception:
        return _finish(
            config,
            claim,
            error_code="webhook.delivery_unavailable",
            now=now,
        )


def drain_experiment_event_once(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """投递或恢复一条实验完成事件，失败按有界退避重试且不记录载荷正文。"""
    if should_stop is not None and should_stop():
        return False
    claim = claim_next_experiment_event(config, worker_id=worker_id, now=now)
    if claim is None:
        return False
    dispatch_claimed_experiment_event(
        config,
        claim,
        now=now,
        should_stop=should_stop,
    )
    return True


def recover_experiment_completed_source_request_id(
    connection: sqlite3.Connection,
    envelope: dict[str, object],
) -> str:
    """从冻结实验结果重建安全事件载荷，并校验源 request_id 与所有者链。

    Recover the hidden creation request owner from the immutable experiment graph.
    """

    expected_envelope = {
        "event_id",
        "event_type",
        "timestamp",
        "tenant_id",
        "actor_id",
        "object_id",
        "trace_id",
        "payload",
    }
    if type(envelope) is not dict or set(envelope) != expected_envelope:
        # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
        raise ApiError("webhook.delivery_unavailable")
    event_id = envelope.get("event_id")
    tenant_id = envelope.get("tenant_id")
    experiment_id = envelope.get("object_id")
    if (
        type(event_id) is not str
        or _EVENT_ID.fullmatch(event_id) is None
        or type(tenant_id) is not str
        or type(experiment_id) is not str
        or _EXPERIMENT_ID.fullmatch(experiment_id) is None
        or envelope.get("event_type") != "experiment.completed"
    ):
        # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
        raise ApiError("webhook.delivery_unavailable")
    payload = _reconstruct_payload(
        connection,
        tenant_id=tenant_id,
        experiment_id=experiment_id,
    )
    if envelope.get("payload") != payload:
        # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
        raise ApiError("webhook.delivery_unavailable")
    payload_json = _canonical_payload(payload)
    try:
        rows = connection.execute(
            """
            SELECT event.source_request_id
            FROM experiment_events event
            JOIN model_experiments experiment
              ON experiment.tenant_id=event.tenant_id
             AND experiment.experiment_id=event.experiment_id
            WHERE event.tenant_id=? AND event.experiment_event_id=?
              AND event.experiment_id=? AND event.event_type='experiment.completed'
              AND event.actor_type='user' AND event.actor_id=experiment.created_by
              AND event.object_type='experiment' AND event.object_id=experiment.experiment_id
              AND event.source_request_id=experiment.operation_request_id
              AND event.source_trace_id=experiment.operation_trace_id
              AND event.occurred_at=experiment.completed_at
              AND event.actor_id=? AND event.object_id=? AND event.source_trace_id=?
              AND event.occurred_at=? AND event.payload_json=?
            LIMIT 2
            """,
            (
                tenant_id,
                event_id,
                experiment_id,
                envelope.get("actor_id"),
                experiment_id,
                envelope.get("trace_id"),
                envelope.get("timestamp"),
                payload_json,
            ),
        ).fetchall()
    # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
    except sqlite3.Error as exc:
        # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
        raise ApiError("webhook.delivery_unavailable") from exc
    # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
    if len(rows) != 1 or type(rows[0]["source_request_id"]) is not str:
        # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
        raise ApiError("webhook.delivery_unavailable")
    return str(rows[0]["source_request_id"])


def _freeze_snapshot(
    config: AppConfig,
    claim: ExperimentEventClaim,
    *,
    current: datetime | None,
) -> None:
    """从冻结实验结果重建安全事件载荷，并校验源 request_id 与所有者链。"""
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        transaction_now = _transaction_now(connection, current)
        row = _active_claim(connection, claim, current=transaction_now)
        if row["delivery_snapshot_frozen_at"] is None:
            from metahub_control.webhooks.service import freeze_webhook_event_delivery_snapshot

            frozen = _db_time(transaction_now)
            freeze_webhook_event_delivery_snapshot(
                connection,
                claim.event,
                frozen_at=frozen,
            )
            connection.execute(
                """
                UPDATE experiment_event_outbox
                SET delivery_snapshot_frozen_at=?,updated_at=?
                WHERE tenant_id=? AND experiment_event_id=?
                  AND dispatch_status='processing' AND dispatch_attempts=?
                  AND lease_generation=? AND claim_token=? AND lease_owner_id=?
                  AND delivery_snapshot_frozen_at IS NULL
                """,
                (
                    frozen,
                    frozen,
                    claim.tenant_id,
                    claim.event_id,
                    claim.dispatch_attempts,
                    claim.lease_generation,
                    claim.claim_token,
                    claim.lease_owner_id,
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
                raise ApiError("experiment.unavailable")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _renew(
    config: AppConfig,
    claim: ExperimentEventClaim,
    *,
    now: datetime | None = None,
) -> None:
    """续租或完成事件时执行所有者 CAS，租约丢失后不得继续改变投递终态。"""
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        expires = current + timedelta(seconds=EVENT_LEASE_SECONDS)
        connection.execute(
            """
            UPDATE experiment_event_outbox
            SET lease_expires_at=?,updated_at=?
            WHERE tenant_id=? AND experiment_event_id=?
              AND dispatch_status='processing' AND dispatch_attempts=?
              AND lease_generation=? AND claim_token=? AND lease_owner_id=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                _db_time(expires),
                _db_time(current),
                claim.tenant_id,
                claim.event_id,
                claim.dispatch_attempts,
                claim.lease_generation,
                claim.claim_token,
                claim.lease_owner_id,
                _db_time(current),
            ),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
            raise ApiError("experiment.unavailable")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _finish(
    config: AppConfig,
    claim: ExperimentEventClaim,
    *,
    error_code: str | None,
    now: datetime | None,
) -> bool:
    """续租或完成事件时执行所有者 CAS，租约丢失后不得继续改变投递终态。"""
    terminal = error_code == "webhook.payload_invalid" or (
        error_code is not None and claim.dispatch_attempts >= EVENT_MAX_ATTEMPTS
    )
    delay = EVENT_BACKOFF_SECONDS[
        min(max(claim.dispatch_attempts - 1, 0), len(EVENT_BACKOFF_SECONDS) - 1)
    ]
    next_status = "dispatched" if error_code is None else "failed" if terminal else "pending"
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = _transaction_now(connection, now)
        now_text = _db_time(current)
        if terminal:
            _fail_pending_deliveries(
                connection,
                tenant_id=claim.tenant_id,
                event_id=claim.event_id,
                updated_at=now_text,
                failure_code=error_code or "webhook.delivery_unavailable",
            )
        connection.execute(
            """
            UPDATE experiment_event_outbox
            SET dispatch_status=?,next_attempt_at=?,claim_token=NULL,lease_owner_id=NULL,
                lease_expires_at=NULL,last_error_code=?,dispatched_at=?,updated_at=?
            WHERE tenant_id=? AND experiment_event_id=?
              AND dispatch_status='processing' AND dispatch_attempts=?
              AND lease_generation=? AND claim_token=? AND lease_owner_id=?
              AND unixepoch(lease_expires_at)>unixepoch(?)
            """,
            (
                next_status,
                (
                    _db_time(current + timedelta(seconds=delay))
                    if next_status == "pending"
                    else None
                ),
                error_code,
                now_text if next_status == "dispatched" else None,
                now_text,
                claim.tenant_id,
                claim.event_id,
                claim.dispatch_attempts,
                claim.lease_generation,
                claim.claim_token,
                claim.lease_owner_id,
                now_text,
            ),
        )
        changed = connection.execute("SELECT changes()").fetchone()[0] == 1
        if not changed:
            connection.rollback()
            return False
        connection.commit()
        return True
    # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
    except sqlite3.Error:
        connection.rollback()
        return False
    finally:
        connection.close()


def _active_claim(
    connection: sqlite3.Connection,
    claim: ExperimentEventClaim,
    *,
    current: datetime,
) -> sqlite3.Row:
    """按租户领取一条实验完成事件，并用所有者与租约到期时间约束并发更新。"""
    row = connection.execute(
        """
        SELECT delivery_snapshot_frozen_at FROM experiment_event_outbox
        WHERE tenant_id=? AND experiment_event_id=? AND dispatch_status='processing'
          AND dispatch_attempts=? AND lease_generation=?
          AND claim_token=? AND lease_owner_id=?
          AND unixepoch(lease_expires_at)>unixepoch(?)
        """,
        (
            claim.tenant_id,
            claim.event_id,
            claim.dispatch_attempts,
            claim.lease_generation,
            claim.claim_token,
            claim.lease_owner_id,
            _db_time(current),
        ),
    ).fetchone()
    if row is None:
        # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
        raise ApiError("experiment.unavailable")
    return row


def _claim_from_row(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    token: str,
    owner: str,
    attempts: int,
    generation: int,
    expires: datetime,
) -> ExperimentEventClaim:
    """按租户领取一条实验完成事件，并用所有者与租约到期时间约束并发更新。"""
    event_id = str(row["experiment_event_id"])
    experiment_id = str(row["experiment_id"])
    if (
        _EVENT_ID.fullmatch(event_id) is None
        or _EXPERIMENT_ID.fullmatch(experiment_id) is None
        or row["event_type"] != "experiment.completed"
        or row["actor_type"] != "user"
        or row["object_type"] != "experiment"
        or row["object_id"] != experiment_id
    ):
        # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
        raise ApiError("experiment.unavailable")
    payload = _reconstruct_payload(
        connection,
        tenant_id=str(row["tenant_id"]),
        experiment_id=experiment_id,
    )
    if row["payload_json"] != _canonical_payload(payload):
        # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
        raise ApiError("experiment.unavailable")
    source = connection.execute(
        """
        SELECT created_by,operation_request_id,operation_trace_id,completed_at
        FROM model_experiments
        WHERE tenant_id=? AND experiment_id=? AND status='completed'
        """,
        (row["tenant_id"], experiment_id),
    ).fetchone()
    if (
        source is None
        or row["actor_id"] != source["created_by"]
        or row["source_request_id"] != source["operation_request_id"]
        or row["source_trace_id"] != source["operation_trace_id"]
        or row["occurred_at"] != source["completed_at"]
    ):
        # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
        raise ApiError("experiment.unavailable")
    occurred_at = _time(row["occurred_at"])
    try:
        event = build_webhook_event(
            tenant_id=str(row["tenant_id"]),
            event_type="experiment.completed",
            event_id=event_id,
            timestamp=occurred_at,
            actor_id=str(row["actor_id"]),
            object_id=experiment_id,
            trace_id=str(row["source_trace_id"]),
            source_request_id=str(row["source_request_id"]),
            payload=payload,
        )
    # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
    except ValueError:
        # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
        raise ApiError("experiment.unavailable") from None
    return ExperimentEventClaim(
        event_id=event_id,
        tenant_id=str(row["tenant_id"]),
        experiment_id=experiment_id,
        actor_id=str(row["actor_id"]),
        source_request_id=str(row["source_request_id"]),
        source_trace_id=str(row["source_trace_id"]),
        occurred_at=occurred_at,
        event=event,
        claim_token=token,
        lease_owner_id=owner,
        dispatch_attempts=attempts,
        lease_generation=generation,
        lease_expires_at=expires,
    )


def _reconstruct_payload(
    connection: sqlite3.Connection,
    *,
    tenant_id: str,
    experiment_id: str,
) -> dict[str, object]:
    """从冻结实验结果重建安全事件载荷，并校验源 request_id 与所有者链。"""
    experiment = connection.execute(
        """
        SELECT * FROM model_experiments
        WHERE tenant_id=? AND experiment_id=? AND status='completed'
        """,
        (tenant_id, experiment_id),
    ).fetchone()
    rows = connection.execute(
        """
        SELECT * FROM experiment_results
        WHERE tenant_id=? AND experiment_id=? ORDER BY variant
        """,
        (tenant_id, experiment_id),
    ).fetchall()
    if experiment is None or len(rows) != 2 or [row["variant"] for row in rows] != ["a", "b"]:
        # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
        raise ApiError("experiment.unavailable")
    facts_through_at = rows[0]["facts_through_at"]
    totals = {
        "assigned_count": 0,
        "gateway_success_count": 0,
        "gateway_failure_count": 0,
        "unresolved_count": 0,
        "cost_unknown_count": 0,
        "feedback_count": 0,
    }
    for result in rows:
        variant = str(result["variant"])
        if (
            result["model_id"] != experiment[f"model_{variant}_id"]
            or result["provider_id"] != experiment[f"model_{variant}_provider_id"]
            or result["provider_code"] != experiment[f"model_{variant}_provider_code"]
            or result["route_config_fingerprint"]
            != experiment[f"model_{variant}_route_config_fingerprint"]
            or result["assigned_count"] != experiment[f"model_{variant}_assigned_count"]
            or result["statistics_version"] != 1
            or result["facts_version"] != experiment["facts_version"]
            or result["facts_through_at"] != facts_through_at
            or result["feedback_enabled"] != experiment["feedback_collection_enabled"]
        ):
            # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
            raise ApiError("experiment.unavailable")
        for metric in totals:
            totals[metric] += _nonnegative_int(result[metric])
            if totals[metric] > 9_223_372_036_854_775_807:
                # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
                raise ApiError("experiment.unavailable")
    if totals["assigned_count"] != _nonnegative_int(experiment["assigned_count"]):
        # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
        raise ApiError("experiment.unavailable")
    return {
        "schema_version": 1,
        "experiment_id": experiment_id,
        "project_id": experiment["project_id"],
        "environment_id": experiment["environment_id"],
        "model_alias_id": experiment["model_alias_id"],
        "model_a_id": experiment["model_a_id"],
        "model_b_id": experiment["model_b_id"],
        "status": "completed",
        "completion_reason": experiment["completion_reason"],
        "assigned_count": experiment["assigned_count"],
        "model_a_assigned_count": experiment["model_a_assigned_count"],
        "model_b_assigned_count": experiment["model_b_assigned_count"],
        "gateway_success_count": totals["gateway_success_count"],
        "gateway_failure_count": totals["gateway_failure_count"],
        "unresolved_count": totals["unresolved_count"],
        "cost_unknown_count": totals["cost_unknown_count"],
        "feedback_count": totals["feedback_count"],
        "facts_version": experiment["facts_version"],
        "facts_through_at": facts_through_at,
    }


def _canonical_payload(payload: dict[str, object]) -> str:
    """从冻结实验结果重建安全事件载荷，并校验源 request_id 与所有者链。"""
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _nonnegative_int(value: object) -> int:
    """封装实验事件声明或规范化字段，避免租户、状态和计数在恢复时漂移。"""
    if type(value) is not int or not 0 <= value <= 9_223_372_036_854_775_807:
        # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
        raise ApiError("experiment.unavailable")
    return value


def _fail_pending_deliveries(
    connection: sqlite3.Connection,
    *,
    tenant_id: object,
    event_id: object,
    updated_at: str,
    failure_code: str,
) -> None:
    """封装实验事件声明或规范化字段，避免租户、状态和计数在恢复时漂移。"""
    from metahub_control.webhooks.service import fail_pending_webhook_event_deliveries

    fail_pending_webhook_event_deliveries(
        connection,
        tenant_id=tenant_id,
        event_id=event_id,
        event_type="experiment.completed",
        updated_at=updated_at,
        failure_code=failure_code,
    )


def _worker_id(value: object) -> str:
    """提供 Outbox 数据库时间、连接或工作者标识，调用方负责资源与事务边界。"""
    if type(value) is not str or _WORKER_ID.fullmatch(value) is None:
        # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
        raise ApiError("experiment.unavailable")
    return value


def _connect(config: AppConfig) -> sqlite3.Connection:
    """提供 Outbox 数据库时间、连接或工作者标识，调用方负责资源与事务边界。"""
    try:
        return connect_database(config.database_url)
    # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
    except DbError:
        # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
        raise ApiError("experiment.unavailable") from None


def _time(value: object) -> datetime:
    """提供 Outbox 数据库时间、连接或工作者标识，调用方负责资源与事务边界。"""
    if type(value) is not str:
        # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
        raise ApiError("experiment.unavailable")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
    except ValueError:
        # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
        raise ApiError("experiment.unavailable") from None
    if parsed.tzinfo is None:
        # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
        raise ApiError("experiment.unavailable")
    return parsed.astimezone(UTC)


def _db_time(value: datetime) -> str:
    """提供 Outbox 数据库时间、连接或工作者标识，调用方负责资源与事务边界。"""
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _now(value: datetime | None) -> datetime:
    """封装实验事件声明或规范化字段，避免租户、状态和计数在恢复时漂移。"""
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).replace(microsecond=0)


def _transaction_now(
    connection: sqlite3.Connection,
    value: datetime | None,
) -> datetime:
    """封装实验事件声明或规范化字段，避免租户、状态和计数在恢复时漂移。"""
    if value is not None:
        return _now(value)
    row = connection.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%SZ','now')"
    ).fetchone()
    if row is None:
        # 事件租约、所有者或源快照失配时中止更新，防止重复投递与跨租户事件。
        raise ValueError
    return _time(row[0])


__all__ = [
    "ExperimentEventClaim",
    "claim_next_experiment_event",
    "dispatch_claimed_experiment_event",
    "drain_experiment_event_once",
    "recover_experiment_completed_source_request_id",
]
