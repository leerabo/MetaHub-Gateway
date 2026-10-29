"""Database-authoritative infrastructure projection and backup request state."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.db import connect_database, database_backend
from metahub_shared.db.errors import DbError
from metahub_shared.infrastructure import probe_kms, probe_object_store, probe_redis
from metahub_shared.infrastructure.probes import InfrastructureProbe
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.data_governance.service import (
    active_governance_policy_in_transaction,
    residency_group_for_region_in_transaction,
    write_residency_evidence_in_transaction,
)
from metahub_control.platform_access.service import (
    PlatformContext,
    authenticate_platform_session_in_transaction,
)

from .models import (
    BackupCreateRequest,
    BackupInfo,
    BackupResponse,
    BackupVerificationInfo,
    InfrastructureResourceInfo,
    InfrastructureStatusResponse,
)

_BACKUP_ID_RE = re.compile(r"^bkp_[0-9a-f]{32}$")
_WORKSPACE_ID_RE = re.compile(r"^wsp_[0-9a-f]{32}$")
_TENANT_ID_RE = re.compile(
    r"^(?:ten_[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12})$"
)
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_SCHEMA_HEAD_RE = re.compile(r"^[0-9]{4}$")
_RESOURCE_ORDER = (
    "database_cluster",
    "cache_cluster",
    "object_store",
    "key_ring",
)


@dataclass(frozen=True, slots=True, repr=False)
class BackupClaim:
    backup_id: str
    backup_type: str
    source_backend: str
    lease_token: str


def get_infrastructure_status(
    config: AppConfig,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> InfrastructureStatusResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = authenticate_platform_session_in_transaction(
            connection, config, session_token=session_token, touch=True
        )
        _require(actor, "platform.infrastructure.read")
        _require_schema(connection)
        connection.commit()
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("infrastructure.unavailable") from None
    finally:
        connection.close()

    now = _utcnow()
    database_probe = _probe_database(config)
    probes = {
        "database_cluster": database_probe,
        "cache_cluster": probe_redis(
            config.redis_url, timeout_seconds=config.infrastructure_probe_timeout_seconds
        ),
        "object_store": probe_object_store(
            endpoint_url=config.object_store_endpoint_url,
            region=config.object_store_region,
            bucket=config.object_store_bucket,
            access_key_id=config.object_store_access_key_id,
            secret_access_key=config.object_store_secret_access_key,
            force_path_style=config.object_store_force_path_style,
            timeout_seconds=config.infrastructure_probe_timeout_seconds,
        ),
        "key_ring": probe_kms(
            provider=config.kms_provider,
            key_id=config.kms_key_id,
            test_master_key=config.kms_test_master_key,
            region=config.object_store_region,
            timeout_seconds=config.infrastructure_probe_timeout_seconds,
        ),
    }
    _persist_probes(config, probes=probes, now=now)
    resources = tuple(
        _resource_info(config, kind, probes[kind], now=now) for kind in _RESOURCE_ORDER
    )
    unavailable = any(item.status == "unavailable" for item in resources)
    degraded = any(item.status in {"degraded", "not_configured"} for item in resources)
    return InfrastructureStatusResponse(
        resources=resources,
        overall_status="unavailable" if unavailable else "degraded" if degraded else "healthy",
        rpo_minutes=15,
        rto_minutes=120,
        request_id=request_id,
        trace_id=trace_id,
    )


def create_backup(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    payload: BackupCreateRequest,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> BackupResponse:
    if _IDEMPOTENCY_RE.fullmatch(idempotency_key) is None:
        raise ApiError("infrastructure.request_invalid")
    _validate_backup_request(payload)
    canonical = payload.model_dump(mode="json", exclude_none=False)
    request_sha256 = _digest(canonical)
    idempotency_sha256 = _secret_digest(
        config, "backup-idempotency", idempotency_key
    )
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_platform_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=True,
        )
        _require(actor, "platform.backup.create")
        _require_schema(connection)
        existing = connection.execute(
            """
            SELECT backup_id,request_sha256 FROM backup_sets
            WHERE requested_by_operator_id=? AND idempotency_sha256=?
            """,
            (actor.operator_id, idempotency_sha256),
        ).fetchone()
        if existing is not None:
            if not hmac.compare_digest(str(existing["request_sha256"]), request_sha256):
                raise ApiError("infrastructure.idempotency_conflict")
            response = _backup_response(
                connection,
                str(existing["backup_id"]),
                request_id=request_id,
                trace_id=trace_id,
                idempotent_replay=True,
            )
            connection.commit()
            return response

        governance = None
        residency_group = None
        if payload.scope_kind == "workspace":
            owner = connection.execute(
                """
                SELECT region.region_code FROM workspace_tenant_bindings binding
                JOIN workspace_regions region ON region.workspace_id=binding.workspace_id
                  AND region.tenant_id=binding.tenant_id AND region.status='active'
                WHERE binding.workspace_id=? AND binding.tenant_id=?
                  AND binding.status='active'
                """,
                (payload.workspace_id, payload.tenant_id),
            ).fetchone()
            if owner is None:
                raise ApiError("infrastructure.backup_owner_not_found")
            governance = active_governance_policy_in_transaction(
                connection,
                workspace_id=str(payload.workspace_id),
                tenant_id=str(payload.tenant_id),
            )
            residency_group = residency_group_for_region_in_transaction(
                connection, str(owner["region_code"])
            )
            if residency_group not in governance.allowed_groups:
                now = _utcnow()
                write_residency_evidence_in_transaction(
                    connection,
                    workspace_id=str(payload.workspace_id),
                    tenant_id=str(payload.tenant_id),
                    policy=governance,
                    component_code="backup",
                    region_code=str(owner["region_code"]),
                    storage_class="backup",
                    outcome="blocked",
                    violation_code="region_not_allowed",
                    observed_watermark=governance.generation,
                    manifest_sha256=governance.sha256,
                    request_id=request_id,
                    trace_id=trace_id,
                    now=now,
                )
                connection.commit()
                raise ApiError("infrastructure.residency_conflict")
        now = _utcnow()
        retention_days = payload.retention_days or config.backup_default_retention_days
        if governance is not None:
            governed_retention = int(
                connection.execute(
                    "SELECT MAX(retention_days+backup_grace_days) FROM retention_policies "
                    "WHERE workspace_id=? AND tenant_id=? AND generation=? AND status='active'",
                    (payload.workspace_id, payload.tenant_id, governance.generation),
                ).fetchone()[0]
            )
            retention_days = max(retention_days, governed_retention)
        backup_id = _id("bkp_")
        source_backend = database_backend(config.database_url)
        if source_backend not in {"sqlite", "postgresql"}:
            raise ApiError("infrastructure.unavailable")
        connection.execute(
            """
            INSERT INTO backup_sets(
              backup_id,backup_type,scope_kind,workspace_id,tenant_id,status,
              source_backend,requested_by_operator_id,idempotency_sha256,request_sha256,
              request_id,trace_id,retention_until,created_at,updated_at,
              governance_generation,residency_group)
            VALUES(?,?,?,?,?,'requested',?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                backup_id,
                payload.backup_type,
                payload.scope_kind,
                payload.workspace_id,
                payload.tenant_id,
                source_backend,
                actor.operator_id,
                idempotency_sha256,
                request_sha256,
                request_id,
                trace_id,
                _db_time(now + timedelta(days=retention_days)),
                _db_time(now),
                _db_time(now),
                governance.generation if governance is not None else None,
                residency_group,
            ),
        )
        connection.execute(
            """
            INSERT INTO backup_restore_verifications(
              backup_restore_verification_id,backup_id,status,created_at,updated_at)
            VALUES(?,?,'pending',?,?)
            """,
            (_id("brv_"), backup_id, _db_time(now), _db_time(now)),
        )
        _audit_backup_requested(
            connection,
            actor=actor,
            backup_id=backup_id,
            request_id=request_id,
            trace_id=trace_id,
            payload=canonical,
            now=now,
        )
        if governance is not None:
            write_residency_evidence_in_transaction(
                connection,
                workspace_id=str(payload.workspace_id),
                tenant_id=str(payload.tenant_id),
                policy=governance,
                component_code="backup",
                region_code=str(owner["region_code"]),
                storage_class="backup",
                outcome="compliant",
                violation_code=None,
                observed_watermark=governance.generation,
                manifest_sha256=governance.sha256,
                request_id=request_id,
                trace_id=trace_id,
                now=now,
            )
        response = _backup_response(
            connection,
            backup_id,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("infrastructure.unavailable") from None
    finally:
        connection.close()


def get_backup(
    config: AppConfig,
    *,
    session_token: str,
    backup_id: str,
    request_id: str,
    trace_id: str,
) -> BackupResponse:
    if _BACKUP_ID_RE.fullmatch(backup_id) is None:
        raise ApiError("infrastructure.request_invalid")
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = authenticate_platform_session_in_transaction(
            connection, config, session_token=session_token, touch=True
        )
        _require(actor, "platform.infrastructure.read")
        _require_schema(connection)
        result = _backup_response(
            connection,
            backup_id,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("infrastructure.unavailable") from None
    finally:
        connection.close()


def claim_backup(config: AppConfig, *, worker_id: str) -> BackupClaim | None:
    if not 8 <= len(worker_id) <= 128:
        raise ApiError("infrastructure.request_invalid")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_schema(connection)
        now = _utcnow()
        _fail_expired_claims(connection, now=now)
        row = connection.execute(
            """
            SELECT * FROM backup_sets
            WHERE status='requested' ORDER BY created_at,backup_id LIMIT 1
            """
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        if row["scope_kind"] == "workspace":
            policy = active_governance_policy_in_transaction(
                connection,
                workspace_id=str(row["workspace_id"]),
                tenant_id=str(row["tenant_id"]),
            )
            owner = connection.execute(
                "SELECT region_code FROM workspace_regions WHERE workspace_id=? "
                "AND tenant_id=? AND status='active'",
                (row["workspace_id"], row["tenant_id"]),
            ).fetchone()
            group = (
                residency_group_for_region_in_transaction(
                    connection, str(owner["region_code"])
                )
                if owner is not None
                else None
            )
            if (
                row["governance_generation"] is None
                or int(row["governance_generation"]) != policy.generation
                or row["residency_group"] != group
                or group not in policy.allowed_groups
            ):
                now = _utcnow()
                connection.execute(
                    "UPDATE backup_sets SET status='failed',"
                    "error_code='infrastructure.residency_conflict',completed_at=?,updated_at=? "
                    "WHERE backup_id=? AND status='requested'",
                    (_db_time(now), _db_time(now), row["backup_id"]),
                )
                write_residency_evidence_in_transaction(
                    connection,
                    workspace_id=str(row["workspace_id"]),
                    tenant_id=str(row["tenant_id"]),
                    policy=policy,
                    component_code="backup",
                    region_code=str(owner["region_code"]) if owner is not None else "unknown",
                    storage_class="backup",
                    outcome="blocked",
                    violation_code=(
                        "generation_stale"
                        if row["governance_generation"] is None
                        or int(row["governance_generation"]) != policy.generation
                        else "region_not_allowed"
                    ),
                    observed_watermark=int(row["governance_generation"] or 0),
                    manifest_sha256=policy.sha256,
                    request_id=str(row["request_id"]),
                    trace_id=str(row["trace_id"]),
                    now=now,
                )
                connection.commit()
                return None
        token = secrets.token_urlsafe(32)
        claimed = connection.execute(
            """
            UPDATE backup_sets
            SET status='snapshotting',lease_owner_id=?,lease_token_sha256=?,
              lease_expires_at=?,updated_at=?
            WHERE backup_id=? AND status='requested'
            """,
            (
                worker_id,
                hashlib.sha256(token.encode("ascii")).hexdigest(),
                _db_time(now + timedelta(minutes=5)),
                _db_time(now),
                row["backup_id"],
            ),
        ).rowcount
        if claimed != 1:
            connection.rollback()
            return None
        connection.commit()
        return BackupClaim(
            backup_id=str(row["backup_id"]),
            backup_type=str(row["backup_type"]),
            source_backend=str(row["source_backend"]),
            lease_token=token,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("infrastructure.unavailable") from None
    finally:
        connection.close()


def mark_backup_uploading(
    config: AppConfig,
    *,
    backup_id: str,
    lease_token: str,
    source_watermark: str,
    size_bytes: int,
    content_sha256: str,
    schema_head: str,
) -> None:
    if (
        _BACKUP_ID_RE.fullmatch(backup_id) is None
        or not 1 <= len(source_watermark) <= 256
        or not 0 <= size_bytes <= 10 * 1024**4
        or not _hash(content_sha256)
        or _SCHEMA_HEAD_RE.fullmatch(schema_head) is None
    ):
        raise ApiError("infrastructure.request_invalid")
    _transition(
        config,
        backup_id=backup_id,
        lease_token=lease_token,
        expected="snapshotting",
        target="uploading",
        assignments=(
            ("source_watermark", source_watermark),
            ("size_bytes", size_bytes),
            ("content_sha256", content_sha256),
            ("schema_head", schema_head),
        ),
    )


def mark_backup_verifying(
    config: AppConfig,
    *,
    backup_id: str,
    lease_token: str,
    object_key: str,
    manifest_sha256: str,
    kms_key_version: str,
) -> None:
    if (
        _BACKUP_ID_RE.fullmatch(backup_id) is None
        or not 16 <= len(object_key) <= 1024
        or not _hash(manifest_sha256)
        or not 1 <= len(kms_key_version) <= 128
    ):
        raise ApiError("infrastructure.request_invalid")
    _transition(
        config,
        backup_id=backup_id,
        lease_token=lease_token,
        expected="uploading",
        target="verifying",
        assignments=(
            ("object_key", object_key),
            ("manifest_sha256", manifest_sha256),
            ("kms_key_version", kms_key_version),
        ),
        start_verification=True,
    )


def complete_backup(
    config: AppConfig,
    *,
    backup_id: str,
    lease_token: str,
    restored_schema_head: str,
    manifest_sha256: str,
    tenant_owner_sample_count: int,
) -> None:
    if (
        _BACKUP_ID_RE.fullmatch(backup_id) is None
        or _SCHEMA_HEAD_RE.fullmatch(restored_schema_head) is None
        or not _hash(manifest_sha256)
        or not 0 <= tenant_owner_sample_count <= 10_000
    ):
        raise ApiError("infrastructure.request_invalid")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _utcnow()
        row = _leased_backup(
            connection,
            backup_id=backup_id,
            lease_token=lease_token,
            expected="verifying",
            now=now,
        )
        if (
            row["schema_head"] != restored_schema_head
            or row["manifest_sha256"] != manifest_sha256
        ):
            raise ApiError("infrastructure.backup_conflict")
        verified = connection.execute(
            """
            UPDATE backup_restore_verifications
            SET status='passed',schema_head=?,integrity_status='ok',manifest_sha256=?,
              tenant_owner_sample_count=?,error_code=NULL,completed_at=?,updated_at=?
            WHERE backup_id=? AND status='running'
            """,
            (
                restored_schema_head,
                manifest_sha256,
                tenant_owner_sample_count,
                _db_time(now),
                _db_time(now),
                backup_id,
            ),
        ).rowcount
        ready = connection.execute(
            """
            UPDATE backup_sets
            SET status='ready',lease_owner_id=NULL,lease_token_sha256=NULL,
              lease_expires_at=NULL,error_code=NULL,completed_at=?,updated_at=?
            WHERE backup_id=? AND status='verifying' AND lease_token_sha256=?
            """,
            (
                _db_time(now),
                _db_time(now),
                backup_id,
                hashlib.sha256(lease_token.encode("ascii")).hexdigest(),
            ),
        ).rowcount
        if verified != 1 or ready != 1:
            raise ApiError("infrastructure.backup_conflict")
        _infrastructure_event(
            connection,
            event_type="backup.completed",
            object_type="backup_set",
            object_id=backup_id,
            payload={"backup_id": backup_id, "status": "ready"},
            now=now,
        )
        connection.commit()
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("infrastructure.unavailable") from None
    finally:
        connection.close()


def fail_backup(
    config: AppConfig,
    *,
    backup_id: str,
    lease_token: str,
    error_code: str,
) -> None:
    if (
        _BACKUP_ID_RE.fullmatch(backup_id) is None
        or re.fullmatch(r"backup\.[a-z0-9_]{2,80}", error_code) is None
    ):
        raise ApiError("infrastructure.request_invalid")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _utcnow()
        row = connection.execute(
            "SELECT status,lease_token_sha256 FROM backup_sets WHERE backup_id=?",
            (backup_id,),
        ).fetchone()
        if row is None or row["status"] not in {"snapshotting", "uploading", "verifying"}:
            raise ApiError("infrastructure.backup_conflict")
        if not hmac.compare_digest(
            str(row["lease_token_sha256"]),
            hashlib.sha256(lease_token.encode("ascii")).hexdigest(),
        ):
            raise ApiError("infrastructure.backup_conflict")
        connection.execute(
            """
            UPDATE backup_restore_verifications
            SET status='failed',integrity_status='failed',error_code=?,
              started_at=COALESCE(started_at,?),completed_at=?,updated_at=?
            WHERE backup_id=? AND status IN('pending','running')
            """,
            (error_code, _db_time(now), _db_time(now), _db_time(now), backup_id),
        )
        failed = connection.execute(
            """
            UPDATE backup_sets
            SET status='failed',lease_owner_id=NULL,lease_token_sha256=NULL,
              lease_expires_at=NULL,error_code=?,completed_at=?,updated_at=?
            WHERE backup_id=? AND status IN('snapshotting','uploading','verifying')
            """,
            (error_code, _db_time(now), _db_time(now), backup_id),
        ).rowcount
        if failed != 1:
            raise ApiError("infrastructure.backup_conflict")
        _infrastructure_event(
            connection,
            event_type="backup.failed",
            object_type="backup_set",
            object_id=backup_id,
            payload={"backup_id": backup_id, "status": "failed", "error_code": error_code},
            now=now,
        )
        connection.commit()
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("infrastructure.unavailable") from None
    finally:
        connection.close()


def _transition(
    config: AppConfig,
    *,
    backup_id: str,
    lease_token: str,
    expected: str,
    target: str,
    assignments: tuple[tuple[str, object], ...],
    start_verification: bool = False,
) -> None:
    allowed_columns = {
        "source_watermark",
        "size_bytes",
        "content_sha256",
        "schema_head",
        "object_key",
        "manifest_sha256",
        "kms_key_version",
    }
    if any(column not in allowed_columns for column, _value in assignments):
        raise ApiError("infrastructure.request_invalid")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _utcnow()
        _leased_backup(
            connection,
            backup_id=backup_id,
            lease_token=lease_token,
            expected=expected,
            now=now,
        )
        clause = ",".join(f"{column}=?" for column, _value in assignments)
        values = tuple(value for _column, value in assignments)
        changed = connection.execute(
            f"""
            UPDATE backup_sets SET status=?,{clause},lease_expires_at=?,updated_at=?
            WHERE backup_id=? AND status=? AND lease_token_sha256=?
            """,
            (
                target,
                *values,
                _db_time(now + timedelta(minutes=5)),
                _db_time(now),
                backup_id,
                expected,
                hashlib.sha256(lease_token.encode("ascii")).hexdigest(),
            ),
        ).rowcount
        if changed != 1:
            raise ApiError("infrastructure.backup_conflict")
        if start_verification:
            started = connection.execute(
                """
                UPDATE backup_restore_verifications
                SET status='running',started_at=?,updated_at=?
                WHERE backup_id=? AND status='pending'
                """,
                (_db_time(now), _db_time(now), backup_id),
            ).rowcount
            if started != 1:
                raise ApiError("infrastructure.backup_conflict")
        connection.commit()
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("infrastructure.unavailable") from None
    finally:
        connection.close()


def _leased_backup(
    connection,
    *,
    backup_id: str,
    lease_token: str,
    expected: str,
    now: datetime,
):
    if not 20 <= len(lease_token) <= 128:
        raise ApiError("infrastructure.backup_conflict")
    row = connection.execute(
        "SELECT * FROM backup_sets WHERE backup_id=?", (backup_id,)
    ).fetchone()
    token_sha256 = hashlib.sha256(lease_token.encode("ascii")).hexdigest()
    if (
        row is None
        or row["status"] != expected
        or row["lease_token_sha256"] is None
        or not hmac.compare_digest(str(row["lease_token_sha256"]), token_sha256)
        or _datetime(row["lease_expires_at"]) <= now
    ):
        raise ApiError("infrastructure.backup_conflict")
    return row


def _fail_expired_claims(connection, *, now: datetime) -> None:
    rows = connection.execute(
        """
        SELECT backup_id FROM backup_sets
        WHERE status IN('snapshotting','uploading','verifying') AND lease_expires_at<=?
        ORDER BY lease_expires_at,backup_id LIMIT 100
        """,
        (_db_time(now),),
    ).fetchall()
    for row in rows:
        backup_id = str(row["backup_id"])
        connection.execute(
            """
            UPDATE backup_restore_verifications
            SET status='failed',integrity_status='failed',error_code='backup.lease_expired',
              started_at=COALESCE(started_at,?),completed_at=?,updated_at=?
            WHERE backup_id=? AND status IN('pending','running')
            """,
            (_db_time(now), _db_time(now), _db_time(now), backup_id),
        )
        connection.execute(
            """
            UPDATE backup_sets
            SET status='failed',lease_owner_id=NULL,lease_token_sha256=NULL,
              lease_expires_at=NULL,error_code='backup.lease_expired',
              completed_at=?,updated_at=?
            WHERE backup_id=? AND status IN('snapshotting','uploading','verifying')
            """,
            (_db_time(now), _db_time(now), backup_id),
        )
        _infrastructure_event(
            connection,
            event_type="backup.failed",
            object_type="backup_set",
            object_id=backup_id,
            payload={
                "backup_id": backup_id,
                "status": "failed",
                "error_code": "backup.lease_expired",
            },
            now=now,
        )


def _probe_database(config: AppConfig) -> InfrastructureProbe:
    started = time.perf_counter()
    connection = None
    try:
        connection = connect_database(config.database_url)
        backend = database_backend(config.database_url)
        if backend == "sqlite":
            version = str(connection.execute("SELECT sqlite_version()").fetchone()[0])
        else:
            version = str(connection.execute("SHOW server_version").fetchone()[0])
        connection.execute("SELECT 1").fetchone()
        return InfrastructureProbe(
            status="healthy",
            version_summary=f"{backend}-{version}"[:80],
            latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
            verified=True,
        )
    except DbError as exc:
        return InfrastructureProbe(
            status="unavailable",
            version_summary=None,
            latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
            verified=False,
            error_code=(
                "infrastructure.database_pool_exhausted"
                if exc.code == "db.pool_exhausted"
                else "infrastructure.database_unavailable"
            ),
        )
    except Exception:
        return InfrastructureProbe(
            status="unavailable",
            version_summary=None,
            latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
            verified=False,
            error_code="infrastructure.database_unavailable",
        )
    finally:
        if connection is not None:
            connection.close()


def _persist_probes(
    config: AppConfig,
    *,
    probes: dict[str, InfrastructureProbe],
    now: datetime,
) -> None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for kind, probe in probes.items():
            before = connection.execute(
                """
                SELECT observed_status,error_code FROM infrastructure_resources
                WHERE resource_kind=?
                """,
                (kind,),
            ).fetchone()
            if before is None:
                raise ApiError("infrastructure.unavailable")
            checked = _db_time(now)
            verified = checked if probe.verified else None
            connection.execute(
                """
                UPDATE infrastructure_resources
                SET observed_status=?,version_summary=?,latency_ms=?,last_checked_at=?,
                  last_verified_at=COALESCE(?,last_verified_at),error_code=?,
                  revision=revision+1,updated_at=?
                WHERE resource_kind=?
                """,
                (
                    probe.status,
                    probe.version_summary,
                    probe.latency_ms,
                    checked,
                    verified,
                    probe.error_code,
                    checked,
                    kind,
                ),
            )
            if probe.status in {"degraded", "unavailable"} and (
                before["observed_status"] != probe.status
                or before["error_code"] != probe.error_code
            ):
                _infrastructure_event(
                    connection,
                    event_type="infrastructure.degraded",
                    object_type="infrastructure_resource",
                    object_id=str(
                        connection.execute(
                            """
                            SELECT infrastructure_resource_id FROM infrastructure_resources
                            WHERE resource_kind=?
                            """,
                            (kind,),
                        ).fetchone()[0]
                    ),
                    payload={"resource_kind": kind, "status": probe.status},
                    now=now,
                )
        connection.commit()
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("infrastructure.unavailable") from None
    finally:
        connection.close()


def _resource_info(
    config: AppConfig,
    kind: str,
    probe: InfrastructureProbe,
    *,
    now: datetime,
) -> InfrastructureResourceInfo:
    provider = {
        "database_cluster": (
            "sqlite_local" if database_backend(config.database_url) == "sqlite" else "postgresql"
        ),
        "cache_cluster": "redis",
        "object_store": "s3_compatible",
        "key_ring": "kms",
    }[kind]
    return InfrastructureResourceInfo(
        resource_kind=kind,  # type: ignore[arg-type]
        provider_code=provider,  # type: ignore[arg-type]
        status=probe.status,
        version_summary=probe.version_summary,
        latency_ms=probe.latency_ms,
        verified=probe.verified,
        last_checked_at=now,
        last_verified_at=now if probe.verified else None,
        error_code=probe.error_code,
    )


def _backup_response(
    connection,
    backup_id: str,
    *,
    request_id: str,
    trace_id: str,
    idempotent_replay: bool = False,
) -> BackupResponse:
    row = connection.execute(
        "SELECT * FROM backup_sets WHERE backup_id=?", (backup_id,)
    ).fetchone()
    verification = connection.execute(
        "SELECT * FROM backup_restore_verifications WHERE backup_id=?", (backup_id,)
    ).fetchone()
    if row is None or verification is None:
        raise ApiError("infrastructure.backup_not_found")
    return BackupResponse(
        backup=BackupInfo(
            backup_id=str(row["backup_id"]),
            backup_type=str(row["backup_type"]),  # type: ignore[arg-type]
            scope_kind=str(row["scope_kind"]),  # type: ignore[arg-type]
            workspace_id=row["workspace_id"],
            tenant_id=row["tenant_id"],
            status=str(row["status"]),  # type: ignore[arg-type]
            source_backend=str(row["source_backend"]),  # type: ignore[arg-type]
            source_watermark=row["source_watermark"],
            size_bytes=row["size_bytes"],
            content_sha256=row["content_sha256"],
            manifest_sha256=row["manifest_sha256"],
            kms_key_version=row["kms_key_version"],
            schema_head=row["schema_head"],
            error_code=row["error_code"],
            retention_until=_datetime(row["retention_until"]),
            created_at=_datetime(row["created_at"]),
            updated_at=_datetime(row["updated_at"]),
            completed_at=_optional_datetime(row["completed_at"]),
            verification=BackupVerificationInfo(
                status=str(verification["status"]),  # type: ignore[arg-type]
                schema_head=verification["schema_head"],
                integrity_status=verification["integrity_status"],
                manifest_sha256=verification["manifest_sha256"],
                tenant_owner_sample_count=verification["tenant_owner_sample_count"],
                error_code=verification["error_code"],
                started_at=_optional_datetime(verification["started_at"]),
                completed_at=_optional_datetime(verification["completed_at"]),
            ),
        ),
        idempotent_replay=idempotent_replay,
        request_id=request_id,
        trace_id=trace_id,
    )


def _audit_backup_requested(
    connection,
    *,
    actor: PlatformContext,
    backup_id: str,
    request_id: str,
    trace_id: str,
    payload: object,
    now: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO platform_audit_logs(
          audit_id,actor_domain,actor_id,action_code,object_type,object_id,reason_code,
          request_id,trace_id,before_sha256,after_sha256,occurred_at)
        VALUES(?,'platform',?,'backup.requested','backup_set',?,'operator_requested',
          ?,?,NULL,?,?)
        """,
        (
            _id("pau_"),
            actor.operator_id,
            backup_id,
            request_id,
            trace_id,
            _digest(payload),
            _db_time(now),
        ),
    )


def _infrastructure_event(
    connection,
    *,
    event_type: str,
    object_type: str,
    object_id: str,
    payload: object,
    now: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO infrastructure_event_outbox(
          infrastructure_event_id,event_type,object_type,object_id,payload_sha256,
          status,attempt_count,next_attempt_at,created_at,updated_at)
        VALUES(?,?,?,?,?,'pending',0,?,?,?)
        """,
        (
            _id("ife_"),
            event_type,
            object_type,
            object_id,
            _digest(payload),
            _db_time(now),
            _db_time(now),
            _db_time(now),
        ),
    )


def _validate_backup_request(payload: BackupCreateRequest) -> None:
    if payload.scope_kind == "workspace" and (
        _WORKSPACE_ID_RE.fullmatch(payload.workspace_id or "") is None
        or _TENANT_ID_RE.fullmatch(payload.tenant_id or "") is None
    ):
        raise ApiError("infrastructure.request_invalid")


def _require_schema(connection) -> None:
    try:
        connection.execute("SELECT 1 FROM infrastructure_resources LIMIT 1").fetchone()
    except Exception:
        raise ApiError("infrastructure.unavailable") from None


def _require(context: PlatformContext, permission: str) -> None:
    if permission not in context.permissions:
        raise ApiError("platform.permission_denied")


def _connect(config: AppConfig):
    try:
        return connect_database(config.database_url)
    except DbError as exc:
        if exc.code == "db.pool_exhausted":
            raise ApiError("infrastructure.pool_exhausted") from None
        raise ApiError("infrastructure.unavailable") from None


def _rollback(connection) -> None:
    try:
        if connection.in_transaction:
            connection.rollback()
    except Exception:
        pass


def _secret_digest(config: AppConfig, purpose: str, value: str) -> str:
    return hmac.new(
        config.platform_session_secret.encode("utf-8"),
        f"prd100/{purpose}\0{value}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
            "ascii"
        )
    ).hexdigest()


def _hash(value: str) -> bool:
    return re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _optional_datetime(value: object | None) -> datetime | None:
    return _datetime(value) if value is not None else None


__all__ = [
    "BackupClaim",
    "claim_backup",
    "complete_backup",
    "create_backup",
    "fail_backup",
    "get_backup",
    "get_infrastructure_status",
    "mark_backup_uploading",
    "mark_backup_verifying",
]
