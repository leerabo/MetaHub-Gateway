"""Fenced PRD-106 handler for progressive rollout steps."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Literal

from metahub_shared.db.connection import connect_database
from pydantic import BaseModel, ConfigDict, Field

from metahub_control.platform_jobs.catalog import register_job_handler
from metahub_control.platform_jobs.runtime import PlatformJobContext, PlatformJobFailure


class FeatureRolloutJobInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    flag_id: str = Field(pattern=r"^ffg_[0-9a-f]{32}$")
    rollout_id: str = Field(pattern=r"^fro_[0-9a-f]{32}$")
    action: Literal["start", "approve", "resume", "rollback", "kill"]


def _rollout_handler(context: PlatformJobContext, payload: FeatureRolloutJobInput) -> str:
    context.checkpoint(
        {
            "flag_id": payload.flag_id,
            "rollout_id": payload.rollout_id,
            "phase": "health_gate",
        }
    )
    outcome: dict[str, str] = {}

    def apply(connection) -> None:
        rollout = connection.execute(
            "SELECT * FROM feature_rollouts WHERE feature_rollout_id=? AND feature_flag_id=?",
            (payload.rollout_id, payload.flag_id),
        ).fetchone()
        if rollout is None or rollout["platform_job_id"] != context.lease.job_id:
            raise PlatformJobFailure("feature.rollout_stale")
        if payload.action == "kill":
            if rollout["status"] != "killed":
                raise PlatformJobFailure("feature.rollout_stale")
            outcome["code"] = "feature.killed"
            return
        if rollout["status"] != "scheduled":
            if rollout["status"] == "paused":
                outcome["code"] = "feature.rollout_paused"
                return
            raise PlatformJobFailure("feature.rollout_stale")
        health = connection.execute(
            """
            SELECT * FROM rollout_health_summaries
            WHERE service_code='gateway' AND region_code=? AND source_watermark=?
            """,
            (rollout["region_code"], rollout["health_source_watermark"]),
        ).fetchone()
        now = _database_now(connection)
        rollout_stamp = _db_time(_changed_time(now, rollout["updated_at"]))
        health_signature_valid = health is not None and hmac.compare_digest(
            str(health["summary_sha256"]),
            _health_signature(context.config, health),
        )
        if (
            health is None
            or not health_signature_valid
            or health["health_status"] != "healthy"
            or _time(health["expires_at"]) <= now
        ):
            health_status = (
                str(health["health_status"])
                if health_signature_valid and health is not None
                else "unavailable"
            )
            connection.execute(
                """
                UPDATE feature_rollouts SET status='paused',health_status=?,revision=revision+1,
                  updated_at=? WHERE feature_rollout_id=? AND revision=?
                """,
                (
                    health_status,
                    rollout_stamp,
                    payload.rollout_id,
                    int(rollout["revision"]),
                ),
            )
            _event(
                connection,
                rollout,
                "feature.rollout_paused",
                "health_unavailable",
                now,
            )
            outcome["code"] = "feature.rollout_paused"
            return
        flag = connection.execute(
            "SELECT * FROM feature_flags WHERE feature_flag_id=?", (payload.flag_id,)
        ).fetchone()
        if flag is None or flag["status"] != "active":
            connection.execute(
                """UPDATE feature_rollouts SET status='killed',health_status='healthy',
                     revision=revision+1,completed_at=?,updated_at=?
                   WHERE feature_rollout_id=? AND revision=?""",
                (
                    rollout_stamp,
                    rollout_stamp,
                    payload.rollout_id,
                    int(rollout["revision"]),
                ),
            )
            outcome["code"] = "feature.killed"
            return
        connection.execute(
            """
            UPDATE feature_flags SET current_version_id=?,rollout_percentage_bps=?,
              revision=revision+1,updated_at=? WHERE feature_flag_id=? AND revision=?
            """,
            (
                rollout["feature_flag_version_id"],
                int(rollout["target_percentage_bps"]),
                _db_time(_changed_time(now, flag["updated_at"])),
                payload.flag_id,
                int(flag["revision"]),
            ),
        )
        terminal = "rolled_back" if payload.action == "rollback" else "completed"
        connection.execute(
            """
            UPDATE feature_rollouts SET status=?,health_status='healthy',revision=revision+1,
              started_at=coalesce(started_at,?),completed_at=?,updated_at=?
            WHERE feature_rollout_id=? AND revision=?
            """,
            (
                terminal,
                rollout_stamp,
                rollout_stamp,
                rollout_stamp,
                payload.rollout_id,
                int(rollout["revision"]),
            ),
        )
        outcome["code"] = (
            "feature.rollback_completed"
            if payload.action == "rollback"
            else "feature.rollout_completed"
        )

    context.commit_effect(
        "feature.rollout.apply",
        {
            "flag_id": payload.flag_id,
            "rollout_id": payload.rollout_id,
            "action": payload.action,
        },
        apply,
    )
    if "code" not in outcome:
        connection = connect_database(context.config.database_url)
        try:
            row = connection.execute(
                "SELECT status FROM feature_rollouts WHERE feature_rollout_id=?",
                (payload.rollout_id,),
            ).fetchone()
            status = str(row[0]) if row is not None else "failed"
            outcome["code"] = {
                "completed": "feature.rollout_completed",
                "rolled_back": "feature.rollback_completed",
                "paused": "feature.rollout_paused",
                "killed": "feature.killed",
            }.get(status, "feature.rollout_stale")
        finally:
            connection.close()
    context.checkpoint(
        {
            "flag_id": payload.flag_id,
            "rollout_id": payload.rollout_id,
            "phase": "complete",
            "outcome": outcome["code"],
        }
    )
    return outcome["code"]


def _event(connection, rollout, event_type: str, reason: str, now: datetime) -> None:
    from uuid import uuid4

    connection.execute(
        """
        INSERT INTO feature_event_outbox(
          feature_event_outbox_id,event_type,feature_flag_id,feature_rollout_id,
          workspace_id,safe_reason_code,request_id,trace_id,created_at)
        VALUES(?,?,?,?,NULL,?,?,?,?)
        """,
        (
            "feo_" + uuid4().hex,
            event_type,
            rollout["feature_flag_id"],
            rollout["feature_rollout_id"],
            reason,
            rollout["request_id"],
            rollout["trace_id"],
            _db_time(now),
        ),
    )


def _database_now(connection) -> datetime:
    return _time(connection.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0])


def _health_signature(config, health) -> str:
    body = json.dumps(
        {
            "service_code": str(health["service_code"]),
            "region_code": str(health["region_code"]),
            "watermark": int(health["source_watermark"]),
            "health_status": str(health["health_status"]),
            "error_rate_bps": int(health["error_rate_bps"]),
            "latency_p95_ms": int(health["latency_p95_ms"]),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hmac.new(
        config.platform_session_secret.encode("utf-8"),
        f"prd107/health-summary/v1\0{body}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _time(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _changed_time(now: datetime, previous: object) -> datetime:
    prior = _time(previous)
    return now if now > prior else prior + timedelta(milliseconds=1)


register_job_handler(
    "feature.rollout.step",
    input_model=FeatureRolloutJobInput,
    handler=_rollout_handler,
)


__all__ = ["FeatureRolloutJobInput"]
