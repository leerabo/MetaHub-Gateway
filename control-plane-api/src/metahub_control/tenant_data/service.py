"""Tenant-scoped export and deletion lifecycle for PRD-089.

The service deliberately exposes projections rather than database rows.  All mutations re-check
the live permission snapshot inside their SQLite transaction and use tenant-qualified predicates.
"""

# Destructive SQLite statements stay as contiguous literals so reviewers can audit their
# exact columns, predicates, and CAS guards without reconstructing SQL fragments.
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import hmac
import io
import json
import re
import sqlite3
import zipfile
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from metahub_shared.tenant_data.crypto import (
    TenantDataCryptoError,
    TenantDataEnvelopeOwner,
    decrypt_tenant_data,
    encrypt_tenant_data,
)

from metahub_control.audit import AuditRequestMeta, record_audit_log
from metahub_control.auth import AuthContext, require_transaction_permission
from metahub_control.data_governance.service import (
    active_hold_blocks_deletion_in_transaction,
    claim_deletion_fence_in_transaction,
)

from .models import (
    DeletionRequestInfo,
    ExportJobInfo,
    TenantDataManagementResponse,
    TenantDataStateInfo,
    TenantDeletionRequest,
    TenantDeletionResponse,
    TenantExportResponse,
)

_ID = re.compile(r"^(tex|del)_[0-9a-f]{32}$")
_HEX = re.compile(r"^[0-9a-f]{64}$")
_EMPTY_META = AuditRequestMeta(client_ip=None, user_agent=None)
_MAX_ZIP_BYTES = 100 * 1024 * 1024


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _secret(config: AppConfig) -> str:
    value = config.tenant_data_encryption_secret
    if type(value) is not str or len(value.encode("utf-8")) < 32:
        raise ApiError("tenant_data.crypto_unavailable")
    return value


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("tenant_data.unavailable") from None


def _digest(secret: str, domain: str, value: str) -> str:
    return hmac.new(
        secret.encode("utf-8"), (domain + ":" + value).encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _request_meta(request_id: str, trace_id: str) -> tuple[str, str]:
    return request_id or "request-unknown", trace_id or "trace-unknown"


def _require_live(
    connection: sqlite3.Connection, context: AuthContext, permission: str
) -> AuthContext:
    try:
        return require_transaction_permission(
            connection,
            context,
            permission,
            unavailable_error_code="tenant_data.unavailable",
        )
    except ApiError:
        raise
    except Exception:
        raise ApiError("tenant_data.unavailable") from None


def _state_row(connection: sqlite3.Connection, tenant_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM tenant_data_states WHERE tenant_id=?", (tenant_id,)
    ).fetchone()
    if row is None:
        raise ApiError("tenant_data.unavailable")
    return row


def _export_info(row: sqlite3.Row) -> ExportJobInfo:
    manifest = None
    if row["manifest_json"]:
        try:
            value = json.loads(row["manifest_json"])
            manifest = value if isinstance(value, dict) else None
        except (TypeError, ValueError):
            manifest = None
    return ExportJobInfo(
        tenant_export_job_id=row["tenant_export_job_id"],
        status=row["status"],
        schema_version=row["schema_version"],
        snapshot_at=row["snapshot_at"],
        usage_snapshot_sequence=int(row["usage_snapshot_sequence"]),
        audit_snapshot_sequence=int(row["audit_snapshot_sequence"]),
        manifest=manifest,
        content_size=row["content_size"],
        content_sha256=row["content_sha256"],
        expires_at=row["expires_at"],
        download_count=int(row["download_count"]),
        error_code=row["error_code"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
    )


def _deletion_info(row: sqlite3.Row) -> DeletionRequestInfo:
    return DeletionRequestInfo(
        deletion_request_id=row["deletion_request_id"],
        status=row["status"],
        tenant_revision=int(row["tenant_revision"]),
        archived_rows=int(row["archived_rows"]),
        deleted_rows=int(row["deleted_rows"]),
        retained_records=int(row["retained_records"]),
        schema_manifest_sha256=row["schema_manifest_sha256"],
        cache_invalidated_at=row["cache_invalidated_at"],
        completed_at=row["completed_at"],
        error_code=row["error_code"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def get_data_management(
    config: AppConfig, context: AuthContext, *, request_id: str, trace_id: str
) -> TenantDataManagementResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        live = _require_live(connection, context, "tenant_data.read")
        state = _state_row(connection, live.user.tenant_id)
        exports = connection.execute(
            "SELECT * FROM tenant_export_jobs WHERE tenant_id=? ORDER BY created_at DESC LIMIT 20",
            (live.user.tenant_id,),
        ).fetchall()
        deletion = connection.execute(
            "SELECT * FROM deletion_requests WHERE tenant_id=? ORDER BY created_at DESC LIMIT 1",
            (live.user.tenant_id,),
        ).fetchone()
        result = TenantDataManagementResponse(
            state=TenantDataStateInfo(
                lifecycle_status=state["lifecycle_status"],
                revision=int(state["revision"]),
                cache_generation=int(state["cache_generation"]),
                updated_at=state["updated_at"],
            ),
            exports=[_export_info(row) for row in exports],
            deletion_request=_deletion_info(deletion) if deletion else None,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    finally:
        connection.close()


def create_export(
    config: AppConfig,
    context: AuthContext,
    *,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> TenantExportResponse:
    if not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 200:
        raise ApiError("tenant_data.request_invalid")
    secret = _secret(config)
    key_hash = _digest(secret, "idempotency/v1", idempotency_key)
    fingerprint = _digest(secret, "export-request/v1", "{}")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        live = _require_live(connection, context, "tenant_data.export")
        tenant_id = live.user.tenant_id
        state = _state_row(connection, tenant_id)
        if state["lifecycle_status"] != "active":
            raise ApiError("tenant_data.deletion_in_progress")
        existing = connection.execute(
            "SELECT * FROM tenant_export_jobs WHERE tenant_id=? AND idempotency_key_hash=?",
            (tenant_id, key_hash),
        ).fetchone()
        if existing:
            if not hmac.compare_digest(existing["request_fingerprint"], fingerprint):
                raise ApiError("tenant_data.idempotency_conflict")
            connection.commit()
            return TenantExportResponse(
                export_job=_export_info(existing),
                status_url=f"/tenant/exports/{existing['tenant_export_job_id']}",
                download_url=f"/tenant/exports/{existing['tenant_export_job_id']}/download",
                request_id=request_id,
                trace_id=trace_id,
            )
        now = _db_time(_now())
        job_id = f"tex_{uuid4().hex}"
        usage_sequence = (
            int(connection.execute("SELECT COALESCE(MAX(rowid),0) FROM usage_ledger").fetchone()[0])
            if _table_exists(connection, "usage_ledger")
            else 0
        )
        audit_sequence = (
            int(connection.execute("SELECT COALESCE(MAX(rowid),0) FROM audit_logs").fetchone()[0])
            if _table_exists(connection, "audit_logs")
            else 0
        )
        connection.execute(
            """INSERT INTO tenant_export_jobs(
              tenant_export_job_id,tenant_id,requested_by_user_id,request_id,trace_id,
              idempotency_key_hash,request_fingerprint,status,schema_version,snapshot_at,
              usage_snapshot_sequence,audit_snapshot_sequence,expires_at,created_at,updated_at)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                job_id,
                tenant_id,
                live.user.user_id,
                request_id,
                trace_id,
                key_hash,
                fingerprint,
                "pending",
                "metahub.tenant-export/v1",
                now,
                usage_sequence,
                audit_sequence,
                _db_time(_now() + timedelta(hours=24)),
                now,
                now,
            ),
        )
        _write_lifecycle_event(
            connection,
            tenant_id,
            "tenant.exported",
            job_id,
            live,
            request_id,
            trace_id,
            {"status": "pending", "job_id_hash": hashlib.sha256(job_id.encode()).hexdigest()},
        )
        record_audit_log(
            connection,
            live,
            action="tenant.export_requested",
            object_type="tenant_export_job",
            object_id=job_id,
            object_name=None,
            before_summary={},
            after_summary={
                "tenant_export_job_id": job_id,
                "status": "pending",
                "schema_version": "metahub.tenant-export/v1",
                "snapshot_at": now,
                "usage_snapshot_sequence": usage_sequence,
                "audit_snapshot_sequence": audit_sequence,
            },
            request_meta=_EMPTY_META,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM tenant_export_jobs WHERE tenant_export_job_id=?", (job_id,)
        ).fetchone()
        return TenantExportResponse(
            export_job=_export_info(row),
            status_url=f"/tenant/exports/{job_id}",
            download_url=f"/tenant/exports/{job_id}/download",
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        connection.rollback()
        raise
    except sqlite3.IntegrityError:
        connection.rollback()
        raise ApiError("tenant_data.unavailable") from None
    finally:
        connection.close()


def get_export(
    config: AppConfig, context: AuthContext, job_id: str, *, request_id: str, trace_id: str
) -> TenantExportResponse:
    if not _ID.fullmatch(job_id or "") or not job_id.startswith("tex_"):
        raise ApiError("tenant_data.not_found")
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        live = _require_live(connection, context, "tenant_data.read")
        row = connection.execute(
            "SELECT * FROM tenant_export_jobs WHERE tenant_id=? AND tenant_export_job_id=?",
            (live.user.tenant_id, job_id),
        ).fetchone()
        if row is None:
            raise ApiError("tenant_data.not_found")
        result = TenantExportResponse(
            export_job=_export_info(row),
            status_url=f"/tenant/exports/{job_id}",
            download_url=f"/tenant/exports/{job_id}/download",
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    finally:
        connection.close()


def download_export(
    config: AppConfig, context: AuthContext, job_id: str
) -> tuple[bytes, ExportJobInfo]:
    if not _ID.fullmatch(job_id or "") or not job_id.startswith("tex_"):
        raise ApiError("tenant_data.not_found")
    secret = _secret(config)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        live = _require_live(connection, context, "tenant_data.download")
        row = connection.execute(
            "SELECT * FROM tenant_export_jobs WHERE tenant_id=? AND tenant_export_job_id=?",
            (live.user.tenant_id, job_id),
        ).fetchone()
        if row is None:
            raise ApiError("tenant_data.not_found")
        if row["status"] != "completed":
            raise ApiError("tenant_data.export_not_ready")
        if row["expires_at"] <= _db_time(_now()):
            raise ApiError("tenant_data.export_expired")
        try:
            content = decrypt_tenant_data(
                row["content_ciphertext"],
                secret=secret,
                kind="export",
                owner=TenantDataEnvelopeOwner(
                    live.user.tenant_id,
                    job_id,
                    "zip",
                    row["schema_version"],
                    int(row["content_key_version"]),
                ),
            )
        except TenantDataCryptoError:
            raise ApiError("tenant_data.crypto_unavailable") from None
        if (
            len(content) != int(row["content_size"] or -1)
            or hashlib.sha256(content).hexdigest() != row["content_sha256"]
        ):
            raise ApiError("tenant_data.export_failed")
        updated = _db_time(_now())
        connection.execute(
            "UPDATE tenant_export_jobs SET download_count=download_count+1,generation=generation+1,updated_at=? WHERE tenant_id=? AND tenant_export_job_id=? AND generation=?",
            (updated, live.user.tenant_id, job_id, int(row["generation"])),
        )
        connection.commit()
        fresh = connection.execute(
            "SELECT * FROM tenant_export_jobs WHERE tenant_export_job_id=?", (job_id,)
        ).fetchone()
        return content, _export_info(fresh)
    except ApiError:
        connection.rollback()
        raise
    finally:
        connection.close()


def create_deletion_request(
    config: AppConfig,
    context: AuthContext,
    payload: TenantDeletionRequest,
    *,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> TenantDeletionResponse:
    if not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 200:
        raise ApiError("tenant_data.request_invalid")
    secret = _secret(config)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        live = _require_live(connection, context, "tenant_data.delete")
        tenant_id = live.user.tenant_id
        state = _state_row(connection, tenant_id)
        tenant = connection.execute(
            "SELECT slug,status FROM tenants WHERE tenant_id=?", (tenant_id,)
        ).fetchone()
        if tenant is None:
            raise ApiError("tenant_data.not_found")
        if int(payload.tenant_revision) != int(state["revision"]):
            raise ApiError("tenant_data.revision_conflict")
        if payload.confirmation != tenant["slug"]:
            raise ApiError("tenant_data.confirmation_mismatch")
        key_hash = _digest(secret, "deletion-idempotency/v1", idempotency_key)
        fingerprint = _digest(
            secret,
            "deletion-request/v1",
            json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":")),
        )
        existing = connection.execute(
            "SELECT * FROM deletion_requests WHERE tenant_id=? AND idempotency_key_hash=?",
            (tenant_id, key_hash),
        ).fetchone()
        if existing:
            if not hmac.compare_digest(existing["request_fingerprint"], fingerprint):
                raise ApiError("tenant_data.idempotency_conflict")
            connection.commit()
            return TenantDeletionResponse(
                deletion_request=_deletion_info(existing), request_id=request_id, trace_id=trace_id
            )
        active = connection.execute(
            "SELECT 1 FROM deletion_requests WHERE tenant_id=? AND status IN ('requested','quiescing','deleting')",
            (tenant_id,),
        ).fetchone()
        if active:
            raise ApiError("tenant_data.deletion_in_progress")
        now = _db_time(_now())
        deletion_id = f"del_{uuid4().hex}"
        manifest_hash = _manifest_hash(connection)
        actor_hash = hashlib.sha256(("actor:" + live.user.user_id).encode()).hexdigest()
        confirmation_hash = _digest(secret, "confirmation/v1", payload.confirmation)
        connection.execute(
            "INSERT INTO deletion_requests(deletion_request_id,tenant_id,actor_id_hash,request_id,trace_id,idempotency_key_hash,request_fingerprint,status,tenant_revision,confirmation_hash,retention_policy_version,schema_manifest_sha256,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                deletion_id,
                tenant_id,
                actor_hash,
                request_id,
                trace_id,
                key_hash,
                fingerprint,
                "requested",
                int(state["revision"]),
                confirmation_hash,
                1,
                manifest_hash,
                now,
                now,
            ),
        )
        connection.execute(
            "UPDATE tenant_data_states SET lifecycle_status='deletion_requested',revision=revision+1,active_deletion_request_id=?,updated_at=? WHERE tenant_id=? AND revision=?",
            (deletion_id, now, tenant_id, int(state["revision"])),
        )
        _write_lifecycle_event(
            connection,
            tenant_id,
            "tenant.deletion_requested",
            deletion_id,
            live,
            request_id,
            trace_id,
            {"status": "requested", "tenant_revision": int(state["revision"])},
        )
        record_audit_log(
            connection,
            live,
            action="tenant.deletion_requested",
            object_type="deletion_request",
            object_id=deletion_id,
            object_name=None,
            before_summary={},
            after_summary={
                "deletion_request_id": deletion_id,
                "status": "requested",
                "tenant_revision": int(state["revision"]),
                "archived_rows": 0,
                "deleted_rows": 0,
                "retained_records": 0,
                "schema_manifest_sha256": manifest_hash,
                "error_code": None,
            },
            request_meta=_EMPTY_META,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM deletion_requests WHERE deletion_request_id=?", (deletion_id,)
        ).fetchone()
        return TenantDeletionResponse(
            deletion_request=_deletion_info(row), request_id=request_id, trace_id=trace_id
        )
    except ApiError:
        connection.rollback()
        raise
    except sqlite3.IntegrityError:
        connection.rollback()
        raise ApiError("tenant_data.unavailable") from None
    finally:
        connection.close()


def run_tenant_data_once(config: AppConfig, *, worker_id: str = "tenant-data-worker") -> int:
    """Claim and process a bounded number of jobs; safe to call from a scheduler or tests."""
    processed = 0
    connection = _connect(config)
    try:
        row = connection.execute(
            "SELECT * FROM tenant_export_jobs WHERE status='pending' OR (status='processing' AND lease_expires_at<?) ORDER BY created_at LIMIT 1",
            (_db_time(_now()),),
        ).fetchone()
        if row:
            processed += _process_export(connection, config, row, worker_id)
        row = connection.execute(
            "SELECT * FROM deletion_requests WHERE status='requested' OR "
            "(status IN ('quiescing','deleting') AND (lease_expires_at<? OR lease_owner_id=?)) "
            "ORDER BY created_at LIMIT 1",
            (_db_time(_now()), worker_id),
        ).fetchone()
        if row:
            processed += _process_deletion(connection, config, row, worker_id)
        return processed
    finally:
        connection.close()


def _process_export(
    connection: sqlite3.Connection, config: AppConfig, row: sqlite3.Row, worker_id: str
) -> int:
    tenant_id, job_id = row["tenant_id"], row["tenant_export_job_id"]
    now = _db_time(_now())
    token = uuid4().hex
    connection.execute("BEGIN IMMEDIATE")
    current = connection.execute(
        "SELECT * FROM tenant_export_jobs WHERE tenant_id=? AND tenant_export_job_id=?",
        (tenant_id, job_id),
    ).fetchone()
    if current is None or current["status"] in ("completed", "failed", "expired"):
        connection.rollback()
        return 0
    attempt = int(current["attempt_count"]) + 1
    if attempt > 3:
        connection.execute(
            "UPDATE tenant_export_jobs SET status='failed',error_code='tenant_data.export_failed',generation=generation+1,updated_at=?,lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL WHERE tenant_id=? AND tenant_export_job_id=? AND generation=?",
            (now, tenant_id, job_id, int(current["generation"])),
        )
        connection.commit()
        return 1
    connection.execute(
        "UPDATE tenant_export_jobs SET status='processing',attempt_count=?,generation=generation+1,lease_owner_id=?,lease_token=?,lease_expires_at=?,updated_at=? WHERE tenant_id=? AND tenant_export_job_id=? AND generation=?",
        (
            attempt,
            worker_id,
            token,
            _db_time(_now() + timedelta(seconds=120)),
            now,
            tenant_id,
            job_id,
            int(current["generation"]),
        ),
    )
    connection.commit()
    try:
        content, manifest = _build_export_zip(connection, tenant_id, job_id, current)
        encrypted = encrypt_tenant_data(
            content,
            secret=_secret(config),
            kind="export",
            owner=TenantDataEnvelopeOwner(tenant_id, job_id, "zip", "metahub.tenant-export/v1"),
        )
        connection.execute("BEGIN IMMEDIATE")
        latest = connection.execute(
            "SELECT * FROM tenant_export_jobs WHERE tenant_id=? AND tenant_export_job_id=?",
            (tenant_id, job_id),
        ).fetchone()
        if latest is None or latest["lease_token"] != token or latest["status"] != "processing":
            connection.rollback()
            return 0
        now = _db_time(_now())
        connection.execute(
            "UPDATE tenant_export_jobs SET status='completed',manifest_json=?,content_ciphertext=?,content_key_version=1,content_size=?,content_sha256=?,completed_at=?,updated_at=?,generation=generation+1,lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL WHERE tenant_id=? AND tenant_export_job_id=? AND generation=?",
            (
                json.dumps(manifest, sort_keys=True, separators=(",", ":")),
                encrypted,
                len(content),
                hashlib.sha256(content).hexdigest(),
                now,
                now,
                tenant_id,
                job_id,
                int(latest["generation"]),
            ),
        )
        connection.commit()
        return 1
    except Exception:
        connection.execute("BEGIN IMMEDIATE")
        latest = connection.execute(
            "SELECT * FROM tenant_export_jobs WHERE tenant_id=? AND tenant_export_job_id=?",
            (tenant_id, job_id),
        ).fetchone()
        if latest and latest["lease_token"] == token:
            if int(latest["attempt_count"]) >= 3:
                connection.execute(
                    "UPDATE tenant_export_jobs SET status='failed',error_code='tenant_data.export_failed',generation=generation+1,updated_at=?,lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL WHERE tenant_id=? AND tenant_export_job_id=? AND generation=?",
                    (_db_time(_now()), tenant_id, job_id, int(latest["generation"])),
                )
            else:
                connection.execute(
                    "UPDATE tenant_export_jobs SET generation=generation+1,updated_at=?,lease_expires_at=? WHERE tenant_id=? AND tenant_export_job_id=? AND generation=?",
                    (
                        _db_time(_now()),
                        _db_time(_now()),
                        tenant_id,
                        job_id,
                        int(latest["generation"]),
                    ),
                )
        connection.commit()
        return 1


def _build_export_zip(
    connection: sqlite3.Connection, tenant_id: str, job_id: str, row: sqlite3.Row
) -> tuple[bytes, dict[str, Any]]:
    projection: dict[str, Any] = {
        "schema_version": "metahub.tenant-export/v1",
        "job_id_hash": hashlib.sha256(job_id.encode()).hexdigest(),
        "snapshot_at": row["snapshot_at"],
    }
    tenant = connection.execute(
        "SELECT tenant_id,slug,status,created_at,updated_at FROM tenants WHERE tenant_id=?",
        (tenant_id,),
    ).fetchone()
    tenant_doc = (
        {key: tenant[key] for key in ("tenant_id", "slug", "status", "created_at", "updated_at")}
        if tenant
        else {"tenant_id": tenant_id}
    )
    usage = _safe_usage_summary(connection, tenant_id)
    audit = _safe_audit_summary(connection, tenant_id)
    failures = _safe_failure_summary(connection, tenant_id)
    payloads = {
        "tenant.json": tenant_doc,
        "usage/summary.json": usage,
        "audit/summary.json": audit,
        "failures/summary.json": failures,
    }
    projection["files"] = list(payloads)
    projection["counts"] = {
        "usage": usage.get("record_count", 0),
        "audit": audit.get("record_count", 0),
        "failures": failures.get("record_count", 0),
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(projection, sort_keys=True, ensure_ascii=True, separators=(",", ":")),
        )
        for name, value in payloads.items():
            archive.writestr(
                name, json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
            )
    content = output.getvalue()
    if not content or len(content) > _MAX_ZIP_BYTES:
        raise ApiError("tenant_data.export_failed")
    return content, projection


def _safe_usage_summary(connection: sqlite3.Connection, tenant_id: str) -> dict[str, Any]:
    if not _table_exists(connection, "usage_ledger"):
        return {"record_count": 0, "total_tokens": 0, "total_cost_cny": "0"}
    row = connection.execute(
        "SELECT COUNT(*) AS count, COALESCE(SUM(total_tokens),0) AS tokens, COALESCE(SUM(cost_cny),0) AS cost FROM usage_ledger WHERE tenant_id=?",
        (tenant_id,),
    ).fetchone()
    return {
        "record_count": int(row["count"]),
        "total_tokens": int(row["tokens"] or 0),
        "total_cost_cny": str(row["cost"] or 0),
    }


def _safe_audit_summary(connection: sqlite3.Connection, tenant_id: str) -> dict[str, Any]:
    if not _table_exists(connection, "audit_logs"):
        return {"record_count": 0, "actions": {}}
    rows = connection.execute(
        "SELECT action,COUNT(*) AS count FROM audit_logs WHERE tenant_id=? GROUP BY action ORDER BY action",
        (tenant_id,),
    ).fetchall()
    return {
        "record_count": sum(int(row["count"]) for row in rows),
        "actions": {row["action"]: int(row["count"]) for row in rows},
    }


def _safe_failure_summary(connection: sqlite3.Connection, tenant_id: str) -> dict[str, Any]:
    if not _table_exists(connection, "request_logs"):
        return {"record_count": 0, "error_codes": {}}
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(request_logs)")}
    if "status" not in columns:
        return {"record_count": 0, "error_codes": {}}
    rows = connection.execute(
        "SELECT COALESCE(error_code,'unknown') AS code,COUNT(*) AS count FROM request_logs WHERE tenant_id=? AND status='failed' GROUP BY error_code ORDER BY code",
        (tenant_id,),
    ).fetchall()
    return {
        "record_count": sum(int(row["count"]) for row in rows),
        "error_codes": {row["code"]: int(row["count"]) for row in rows},
    }


def _process_deletion(
    connection: sqlite3.Connection, config: AppConfig, row: sqlite3.Row, worker_id: str
) -> int:
    tenant_id, deletion_id = row["tenant_id"], row["deletion_request_id"]
    connection.execute("BEGIN IMMEDIATE")
    current = connection.execute(
        "SELECT * FROM deletion_requests WHERE tenant_id=? AND deletion_request_id=?",
        (tenant_id, deletion_id),
    ).fetchone()
    if current is None or current["status"] == "completed":
        connection.rollback()
        return 0
    token = uuid4().hex
    now = _db_time(_now())
    if current["status"] == "requested":
        connection.execute(
            "UPDATE deletion_requests SET status='quiescing',generation=generation+1,lease_owner_id=?,lease_token=?,lease_expires_at=?,updated_at=? WHERE tenant_id=? AND deletion_request_id=? AND generation=?",
            (
                worker_id,
                token,
                _db_time(_now() + timedelta(seconds=120)),
                now,
                tenant_id,
                deletion_id,
                int(current["generation"]),
            ),
        )
        connection.commit()
        return 1
    governance_owner = _governance_owner(connection, tenant_id)
    if governance_owner is not None:
        if active_hold_blocks_deletion_in_transaction(
            connection,
            workspace_id=str(governance_owner["workspace_id"]),
            tenant_id=tenant_id,
        ):
            connection.rollback()
            return 0
        claim_deletion_fence_in_transaction(
            connection,
            workspace_id=str(governance_owner["workspace_id"]),
            tenant_id=tenant_id,
        )
    connection.execute(
        "UPDATE deletion_requests SET status='deleting',generation=generation+1,lease_owner_id=?,lease_token=?,lease_expires_at=?,updated_at=? WHERE tenant_id=? AND deletion_request_id=? AND generation=?",
        (
            worker_id,
            token,
            _db_time(_now() + timedelta(seconds=120)),
            now,
            tenant_id,
            deletion_id,
            int(current["generation"]),
        ),
    )
    connection.commit()
    try:
        archived, deleted = _erase_tenant(
            connection,
            tenant_id,
            deletion_id,
            config,
            governance_workspace_id=(
                str(governance_owner["workspace_id"])
                if governance_owner is not None
                else None
            ),
        )
        connection.execute("BEGIN EXCLUSIVE")
        latest = connection.execute(
            "SELECT * FROM deletion_requests WHERE tenant_id=? AND deletion_request_id=?",
            (tenant_id, deletion_id),
        ).fetchone()
        if latest is None or latest["lease_token"] != token:
            connection.rollback()
            return 0
        now = _db_time(_now())
        connection.execute(
            "UPDATE tenants SET status='disabled',updated_at=? WHERE tenant_id=?", (now, tenant_id)
        )
        connection.execute(
            "UPDATE tenant_data_states SET lifecycle_status='deleted',revision=revision+1,cache_generation=cache_generation+1,updated_at=? WHERE tenant_id=?",
            (now, tenant_id),
        )
        connection.execute(
            "UPDATE deletion_requests SET status='completed',archived_rows=?,deleted_rows=?,retained_records=(SELECT COUNT(*) FROM tenant_lifecycle_events WHERE tenant_id=?),cache_invalidated_at=?,completed_at=?,generation=generation+1,lease_owner_id=NULL,lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE tenant_id=? AND deletion_request_id=? AND generation=?",
            (
                archived,
                deleted,
                tenant_id,
                now,
                now,
                now,
                tenant_id,
                deletion_id,
                int(latest["generation"]),
            ),
        )
        connection.commit()
        return 1
    except Exception:
        connection.rollback()
        return 1


def _erase_tenant(
    connection: sqlite3.Connection,
    tenant_id: str,
    deletion_id: str,
    config: AppConfig,
    *,
    governance_workspace_id: str | None = None,
) -> tuple[int, int]:
    connection.execute("BEGIN EXCLUSIVE")
    if governance_workspace_id is not None and active_hold_blocks_deletion_in_transaction(
        connection,
        workspace_id=governance_workspace_id,
        tenant_id=tenant_id,
    ):
        raise ApiError("data_governance.hold_active")
    connection.execute("PRAGMA defer_foreign_keys=ON")
    _verify_manifest(connection)
    if _table_exists(connection, "tenant_deletion_manifest_extensions"):
        manifests = connection.execute(
            """SELECT manifest.source_table,manifest.disposition,manifest.retention_class,
                      COALESCE(extension.tenant_key_column,'tenant_id') AS tenant_key_column
                 FROM tenant_deletion_table_manifest manifest
                 LEFT JOIN tenant_deletion_manifest_extensions extension
                   ON extension.source_table=manifest.source_table
                WHERE manifest.disposition IN ('delete','archive_then_delete')
                ORDER BY manifest.source_table"""
        ).fetchall()
    else:
        manifests = connection.execute(
            """SELECT source_table,disposition,retention_class,
                      'tenant_id' AS tenant_key_column
                 FROM tenant_deletion_table_manifest
                WHERE disposition IN ('delete','archive_then_delete')
                ORDER BY source_table"""
        ).fetchall()
    archived = deleted = 0
    dropped_triggers: list[str] = []
    for item in manifests:
        table = item["source_table"]
        tenant_key_column = item["tenant_key_column"]
        if (
            not _safe_identifier(table)
            or not _safe_identifier(tenant_key_column)
            or not _table_exists(connection, table)
            or not any(
                column["name"] == tenant_key_column
                for column in connection.execute(f'PRAGMA table_info("{table}")')
            )
        ):
            raise ApiError("tenant_data.schema_changed")
        triggers = connection.execute(
            "SELECT trigger_name,trigger_sql FROM tenant_deletion_trigger_manifest WHERE source_table=?",
            (table,),
        ).fetchall()
        for trigger in triggers:
            if _safe_identifier(trigger["trigger_name"]):
                dropped_triggers.append(trigger["trigger_sql"])
                connection.execute(f'DROP TRIGGER "{trigger["trigger_name"]}"')
        count = int(
            connection.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE "{tenant_key_column}"=?',
                (tenant_id,),
            ).fetchone()[0]
        )
        if count and item["disposition"] == "archive_then_delete":
            # Retention stores only a bounded aggregate; raw rows remain encrypted nowhere in logs.
            archive_payload = json.dumps(
                {"source_table": table, "row_count": count}, sort_keys=True
            ).encode()
            encrypted = encrypt_tenant_data(
                archive_payload,
                secret=_secret(config),
                kind="archive",
                owner=TenantDataEnvelopeOwner(
                    tenant_id, deletion_id, table, "metahub.tenant-retention/v1"
                ),
            )
            connection.execute(
                "INSERT INTO tenant_retention_archives(tenant_retention_archive_id,tenant_id,deletion_request_id,source_table,segment_no,schema_version,retention_class,row_count,uncompressed_size,compressed_size,content_ciphertext,content_key_version,content_sha256,retain_until,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"tra_{uuid4().hex}",
                    tenant_id,
                    deletion_id,
                    table,
                    0,
                    "metahub.tenant-retention/v1",
                    item["retention_class"] or "operational",
                    count,
                    len(archive_payload),
                    len(encrypted),
                    hashlib.sha256(archive_payload).hexdigest(),
                    1,
                    hashlib.sha256(archive_payload).hexdigest(),
                    _db_time(_now() + timedelta(days=2555)),
                    _db_time(_now()),
                ),
            )
            archived += count
        connection.execute(
            f'DELETE FROM "{table}" WHERE "{tenant_key_column}"=?', (tenant_id,)
        )
        if connection.execute(
            f'SELECT 1 FROM "{table}" WHERE "{tenant_key_column}"=? LIMIT 1',
            (tenant_id,),
        ).fetchone():
            raise ApiError("tenant_data.retention_failed")
        deleted += count
    # Restore every trigger from the immutable migration manifest before committing.
    for trigger_sql in dropped_triggers:
        connection.execute(trigger_sql)
    connection.execute("PRAGMA foreign_key_check")
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise ApiError("tenant_data.retention_failed")
    connection.commit()
    return archived, deleted


def _governance_owner(
    connection: sqlite3.Connection, tenant_id: str
) -> sqlite3.Row | None:
    if not _table_exists(connection, "data_governance_fences"):
        return None
    return connection.execute(
        "SELECT workspace_id FROM data_governance_fences WHERE tenant_id=?", (tenant_id,)
    ).fetchone()


def _write_lifecycle_event(
    connection: sqlite3.Connection,
    tenant_id: str,
    event_type: str,
    object_id: str,
    context: AuthContext,
    request_id: str,
    trace_id: str,
    payload: dict[str, Any],
) -> None:
    payload_json = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    event_id = f"tle_{uuid4().hex}"
    actor_hash = hashlib.sha256(("actor:" + context.user.user_id).encode()).hexdigest()
    connection.execute(
        "INSERT INTO tenant_lifecycle_events(tenant_lifecycle_event_id,tenant_id,event_type,object_id,actor_id_hash,request_id,trace_id,payload_json,payload_sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            event_id,
            tenant_id,
            event_type,
            object_id,
            actor_hash,
            request_id,
            trace_id,
            payload_json,
            hashlib.sha256(payload_json.encode()).hexdigest(),
            _db_time(_now()),
        ),
    )
    webhook_event_id = f"evt_{uuid4().hex}"
    empty_subscriptions = "[]"
    out_payload = json.dumps(
        {
            "status": payload.get("status"),
            "object_id_hash": hashlib.sha256(object_id.encode()).hexdigest(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    connection.execute(
        "INSERT INTO tenant_lifecycle_event_outbox(tenant_lifecycle_event_outbox_id,tenant_id,tenant_lifecycle_event_id,webhook_event_id,payload_json,payload_sha256,status,attempt_count,generation,next_attempt_at,subscription_snapshot_json,snapshot_frozen_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"tlo_{uuid4().hex}",
            tenant_id,
            event_id,
            webhook_event_id,
            out_payload,
            hashlib.sha256(out_payload.encode()).hexdigest(),
            "pending",
            0,
            0,
            _db_time(_now()),
            empty_subscriptions,
            _db_time(_now()),
            _db_time(_now()),
            _db_time(_now()),
        ),
    )


def _manifest_hash(connection: sqlite3.Connection) -> str:
    if _table_exists(connection, "tenant_deletion_manifest_extensions"):
        rows = connection.execute(
            """SELECT manifest.source_table,manifest.disposition,manifest.retention_class,
                      manifest.table_sql_sha256,manifest.column_signature_sha256,
                      manifest.trigger_set_sha256,
                      COALESCE(extension.tenant_key_column,'tenant_id') AS tenant_key_column
                 FROM tenant_deletion_table_manifest manifest
                 LEFT JOIN tenant_deletion_manifest_extensions extension
                   ON extension.source_table=manifest.source_table
                ORDER BY manifest.source_table"""
        ).fetchall()
    else:
        rows = connection.execute(
            """SELECT source_table,disposition,retention_class,table_sql_sha256,
                      column_signature_sha256,trigger_set_sha256,
                      'tenant_id' AS tenant_key_column
                 FROM tenant_deletion_table_manifest ORDER BY source_table"""
        ).fetchall()
    return hashlib.sha256(
        json.dumps([tuple(row) for row in rows], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _verify_manifest(connection: sqlite3.Connection) -> None:
    """Refuse destructive DDL when a later migration changed a tenant table or trigger."""
    if _table_exists(connection, "tenant_deletion_manifest_extensions"):
        rows = connection.execute(
            """SELECT manifest.source_table,manifest.table_sql_sha256,
                      manifest.column_signature_sha256,manifest.trigger_set_sha256,
                      COALESCE(extension.tenant_key_column,'tenant_id') AS tenant_key_column
                 FROM tenant_deletion_table_manifest manifest
                 LEFT JOIN tenant_deletion_manifest_extensions extension
                   ON extension.source_table=manifest.source_table
                ORDER BY manifest.source_table"""
        ).fetchall()
    else:
        rows = connection.execute(
            """SELECT source_table,table_sql_sha256,column_signature_sha256,
                      trigger_set_sha256,'tenant_id' AS tenant_key_column
                 FROM tenant_deletion_table_manifest ORDER BY source_table"""
        ).fetchall()
    registered_tables = {row["source_table"] for row in rows}
    for schema_row in connection.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ):
        table = schema_row["name"]
        if not _safe_identifier(table):
            raise ApiError("tenant_data.schema_changed")
        if table not in registered_tables and any(
            column["name"] == "tenant_id"
            for column in connection.execute(f'PRAGMA table_info("{table}")')
        ):
            raise ApiError("tenant_data.schema_changed")
    for row in rows:
        table = row["source_table"]
        tenant_key_column = row["tenant_key_column"]
        if not _safe_identifier(table) or not _safe_identifier(tenant_key_column):
            raise ApiError("tenant_data.schema_changed")
        schema = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if schema is None:
            raise ApiError("tenant_data.schema_changed")
        table_hash = hashlib.sha256((schema["sql"] or "").encode()).hexdigest()
        columns = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        if not any(column["name"] == tenant_key_column for column in columns):
            raise ApiError("tenant_data.schema_changed")
        column_text = "\x1e".join(
            "\x1f".join(
                "" if value is None else str(value)
                for value in (
                    column["cid"],
                    column["name"],
                    column["type"],
                    column["notnull"],
                    column["dflt_value"],
                    column["pk"],
                )
            )
            for column in columns
        )
        column_hash = hashlib.sha256(column_text.encode()).hexdigest()
        trigger_rows = connection.execute(
            "SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND tbl_name=? ORDER BY name",
            (table,),
        ).fetchall()
        trigger_text = "\x1e".join(
            f"{trigger['name']}\x1f{hashlib.sha256((trigger['sql'] or '').encode()).hexdigest()}"
            for trigger in trigger_rows
        )
        trigger_hash = hashlib.sha256(trigger_text.encode()).hexdigest()
        if (table_hash, column_hash, trigger_hash) != (
            row["table_sql_sha256"],
            row["column_signature_sha256"],
            row["trigger_set_sha256"],
        ):
            raise ApiError("tenant_data.schema_changed")


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _safe_identifier(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value or ""))


__all__ = [
    "create_deletion_request",
    "create_export",
    "download_export",
    "get_data_management",
    "get_export",
    "run_tenant_data_once",
]
