"""One leased PRD-100 backup execution with real restore verification."""

from __future__ import annotations

from metahub_shared.infrastructure import (
    BackupRuntimeError,
    create_snapshot,
    restore_and_verify,
    store_backup,
)
from metahub_shared.runtime.config import AppConfig

from .service import (
    claim_backup,
    complete_backup,
    fail_backup,
    mark_backup_uploading,
    mark_backup_verifying,
)


def run_backup_once(config: AppConfig, *, worker_id: str) -> bool:
    claim = claim_backup(config, worker_id=worker_id)
    if claim is None:
        return False
    try:
        artifact = create_snapshot(config, backup_type=claim.backup_type)
        content_sha256 = _sha256(artifact.payload)
        mark_backup_uploading(
            config,
            backup_id=claim.backup_id,
            lease_token=claim.lease_token,
            source_watermark=artifact.source_watermark,
            size_bytes=len(artifact.payload),
            content_sha256=content_sha256,
            schema_head=artifact.schema_head,
        )
        stored = store_backup(config, backup_id=claim.backup_id, artifact=artifact)
        if stored.content_sha256 != content_sha256:
            raise BackupRuntimeError("backup.content_hash_invalid")
        mark_backup_verifying(
            config,
            backup_id=claim.backup_id,
            lease_token=claim.lease_token,
            object_key=stored.object_key,
            manifest_sha256=stored.manifest_sha256,
            kms_key_version=stored.kms_key_version,
        )
        verified = restore_and_verify(
            config,
            backup_id=claim.backup_id,
            object_key=stored.object_key,
            expected_manifest_sha256=stored.manifest_sha256,
        )
        complete_backup(
            config,
            backup_id=claim.backup_id,
            lease_token=claim.lease_token,
            restored_schema_head=verified.schema_head,
            manifest_sha256=verified.manifest_sha256,
            tenant_owner_sample_count=verified.tenant_owner_sample_count,
        )
        return True
    except BackupRuntimeError as exc:
        _fail(config, claim.backup_id, claim.lease_token, exc.code)
        return True
    except Exception:
        _fail(config, claim.backup_id, claim.lease_token, "backup.worker_unavailable")
        return True


def _fail(config: AppConfig, backup_id: str, lease_token: str, code: str) -> None:
    safe_code = code if code.startswith("backup.") else "backup.worker_unavailable"
    try:
        fail_backup(
            config,
            backup_id=backup_id,
            lease_token=lease_token,
            error_code=safe_code,
        )
    except Exception:
        # The durable lease remains for bounded expiry recovery.  Never replace the
        # original safe error with provider text or a secret-bearing traceback.
        return


def _sha256(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()


__all__ = ["run_backup_once"]
