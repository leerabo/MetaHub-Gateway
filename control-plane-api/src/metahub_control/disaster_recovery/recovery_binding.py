"""Immutable recovery selection shared by approval and all DR execution phases."""

from __future__ import annotations

import hashlib
import math
import re
from datetime import UTC, datetime

from metahub_shared.runtime.errors import ApiError


def schema_head(connection) -> str:
    if connection.execute(
        "SELECT 1 FROM schema_migrations WHERE status='failed' LIMIT 1"
    ).fetchone():
        raise ApiError("dr.recovery_point_unavailable")
    row = connection.execute(
        "SELECT version FROM schema_migrations WHERE status='applied' ORDER BY version DESC LIMIT 1"
    ).fetchone()
    if row is None or not re.fullmatch(r"[0-9]{4}", str(row[0])):
        raise ApiError("dr.recovery_point_unavailable")
    return str(row[0])


def source_age(value: object, now: datetime) -> int:
    try:
        source = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        source = source.replace(tzinfo=UTC) if source.tzinfo is None else source
        seconds = (now - source).total_seconds()
        if seconds < 0:
            raise ValueError
        return math.ceil(seconds)
    except (TypeError, ValueError, OverflowError):
        raise ApiError("dr.recovery_point_unavailable") from None


def latest_platform_backup(connection):
    return connection.execute(
        VERIFIED_BACKUP_SELECT + " AND backup.scope_kind='platform' AND backup.workspace_id IS NULL"
        " AND backup.tenant_id IS NULL AND backup.backup_type='full'"
        " AND backup.source_backend=? AND backup.schema_head=?"
        " ORDER BY backup.created_at DESC,backup.backup_id DESC LIMIT 1",
        (getattr(connection, "backend", "sqlite"), schema_head(connection)),
    ).fetchone()


def require_recoverable_backup(connection, backup, *, now: datetime, rpo_seconds: int) -> None:
    if backup is None or (
        backup["scope_kind"] != "platform"
        or backup["workspace_id"] is not None
        or backup["tenant_id"] is not None
        or backup["backup_type"] != "full"
        or backup["source_backend"] != getattr(connection, "backend", "sqlite")
        or backup["schema_head"] != schema_head(connection)
    ):
        raise ApiError("dr.recovery_point_unavailable")
    created = _time(backup["created_at"])
    completed = _time(backup["completed_at"])
    if completed < created or completed > now or source_age(created, now) > rpo_seconds:
        raise ApiError("dr.recovery_point_unavailable")


def bind_recovery_in_transaction(connection, *, run, recovery, job_id: str, now: datetime) -> None:
    backup = connection.execute(
        VERIFIED_BACKUP_SELECT + " AND backup.backup_id=?", (recovery["backup_id"],)
    ).fetchone()
    require_recoverable_backup(connection, backup, now=now, rpo_seconds=int(run["rpo_seconds"]))
    _require_recovery_matches_backup(recovery, backup)
    connection.execute(
        """INSERT INTO dr_run_recovery_bindings(
             disaster_recovery_run_id,recovery_point_id,platform_job_id,plan_manifest_sha256,
             source_region_id,target_region_id,expected_epoch,fencing_epoch,manifest_sha256,
             content_sha256,object_key_sha256,schema_head,bound_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            run["disaster_recovery_run_id"],
            recovery["recovery_point_id"],
            job_id,
            run["plan_manifest_sha256"],
            run["source_region_id"],
            run["target_region_id"],
            run["expected_epoch"],
            run["fencing_epoch"],
            backup["manifest_sha256"],
            backup["content_sha256"],
            _digest(backup["object_key"]),
            backup["schema_head"],
            now.isoformat().replace("+00:00", "Z"),
        ),
    )


def bound_recovery_in_transaction(connection, *, run, job_id: str | None = None):
    binding = connection.execute(
        "SELECT * FROM dr_run_recovery_bindings WHERE disaster_recovery_run_id=?",
        (run["disaster_recovery_run_id"],),
    ).fetchone()
    if binding is None:
        raise ApiError("dr.recovery_point_unavailable")
    if any(
        binding[key] != run[key]
        for key in (
            "platform_job_id",
            "plan_manifest_sha256",
            "source_region_id",
            "target_region_id",
            "expected_epoch",
            "fencing_epoch",
        )
    ) or (job_id is not None and binding["platform_job_id"] != job_id):
        raise ApiError("dr.run_conflict")
    recovery = connection.execute(
        "SELECT * FROM recovery_points WHERE recovery_point_id=?", (binding["recovery_point_id"],)
    ).fetchone()
    if recovery is None:
        raise ApiError("dr.recovery_point_unavailable")
    backup = connection.execute(
        VERIFIED_BACKUP_SELECT + " AND backup.backup_id=?", (recovery["backup_id"],)
    ).fetchone()
    if (
        backup is None
        or any(
            binding[key] != backup[key]
            for key in (
                "manifest_sha256",
                "content_sha256",
                "schema_head",
            )
        )
        or binding["object_key_sha256"] != _digest(backup["object_key"])
    ):
        raise ApiError("dr.recovery_point_unavailable")
    _require_recovery_matches_backup(recovery, backup)
    # Freshness is measured at the start, not repeatedly after restoration consumes time.
    require_recoverable_backup(
        connection,
        backup,
        now=_time(run["started_at"] or binding["bound_at"]),
        rpo_seconds=int(run["rpo_seconds"]),
    )
    return recovery


def _require_recovery_matches_backup(recovery, backup) -> None:
    if any(
        recovery[key] != backup[key]
        for key in (
            "backup_id",
            "scope_kind",
            "workspace_id",
            "tenant_id",
            "schema_head",
            "manifest_sha256",
        )
    ) or (
        recovery["source_watermark_sha256"] != _digest(backup["source_watermark"])
        or recovery["kms_key_version_sha256"] != _digest(backup["kms_key_version"])
        or _time(recovery["source_created_at"]) != _time(backup["created_at"])
        or recovery["integrity_status"] != "verified"
    ):
        raise ApiError("dr.recovery_point_unavailable")


def _time(value) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
    except (TypeError, ValueError, OverflowError):
        raise ApiError("dr.recovery_point_unavailable") from None


def _digest(value) -> str:
    if not isinstance(value, str) or not value:
        raise ApiError("dr.recovery_point_unavailable")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


VERIFIED_BACKUP_SELECT = """
SELECT backup.* FROM backup_sets backup
JOIN backup_restore_verifications verification ON verification.backup_id=backup.backup_id
  AND verification.status='passed' AND verification.integrity_status='ok'
  AND verification.manifest_sha256=backup.manifest_sha256
  AND verification.schema_head=backup.schema_head
WHERE backup.status='ready' AND backup.manifest_sha256 IS NOT NULL
  AND backup.kms_key_version IS NOT NULL AND backup.schema_head IS NOT NULL
  AND backup.object_key IS NOT NULL AND backup.content_sha256 IS NOT NULL
  AND backup.source_watermark IS NOT NULL AND backup.completed_at IS NOT NULL
"""
