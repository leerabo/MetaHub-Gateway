"""Real snapshot, envelope encryption, object storage, and isolated restore verification."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from metahub_shared.db import connect_database, database_backend
from metahub_shared.runtime.config import AppConfig

_MAX_BACKUP_BYTES = 10 * 1024**4


@dataclass(frozen=True, slots=True)
class BackupArtifact:
    payload: bytes
    source_watermark: str
    schema_head: str
    tenant_owner_sample_count: int


@dataclass(frozen=True, slots=True)
class StoredBackup:
    object_key: str
    content_sha256: str
    manifest_sha256: str
    kms_key_version: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class RestoreVerification:
    schema_head: str
    manifest_sha256: str
    tenant_owner_sample_count: int


class BackupRuntimeError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def create_snapshot(config: AppConfig, *, backup_type: str) -> BackupArtifact:
    if backup_type not in {"full", "incremental"}:
        raise BackupRuntimeError("backup.request_invalid")
    backend = database_backend(config.database_url)
    if backend == "sqlite":
        if backup_type != "full":
            raise BackupRuntimeError("backup.incremental_unsupported")
        return _sqlite_snapshot(config)
    if backend == "postgresql":
        return _postgres_snapshot(config, backup_type=backup_type)
    raise BackupRuntimeError("backup.database_unavailable")


def store_backup(
    config: AppConfig,
    *,
    backup_id: str,
    artifact: BackupArtifact,
) -> StoredBackup:
    if len(artifact.payload) > _MAX_BACKUP_BYTES:
        raise BackupRuntimeError("backup.snapshot_too_large")
    content_sha256 = hashlib.sha256(artifact.payload).hexdigest()
    manifest = {
        "backup_id": backup_id,
        "content_sha256": content_sha256,
        "schema_head": artifact.schema_head,
        "size_bytes": len(artifact.payload),
        "source_watermark": artifact.source_watermark,
        "tenant_owner_sample_count": artifact.tenant_owner_sample_count,
        "version": 1,
    }
    manifest_bytes = _canonical(manifest)
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    data_key, wrapped_key, key_version = _generate_data_key(config, backup_id=backup_id)
    try:
        nonce = os.urandom(12)
        aad = _aad(backup_id, manifest_sha256)
        ciphertext = AESGCM(data_key).encrypt(nonce, artifact.payload, aad)
    finally:
        data_key = b""
    envelope = _canonical(
        {
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
            "key_provider": config.kms_provider,
            "manifest": manifest,
            "manifest_sha256": manifest_sha256,
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "version": 1,
            "wrapped_data_key": base64.b64encode(wrapped_key).decode("ascii"),
        }
    )
    object_key = f"backups/v1/{backup_id[:8]}/{backup_id}.mhb"
    _s3_put(config, object_key=object_key, payload=envelope, content_sha256=manifest_sha256)
    return StoredBackup(
        object_key=object_key,
        content_sha256=content_sha256,
        manifest_sha256=manifest_sha256,
        kms_key_version=key_version,
        size_bytes=len(artifact.payload),
    )


def restore_and_verify(
    config: AppConfig,
    *,
    backup_id: str,
    object_key: str,
    expected_manifest_sha256: str,
) -> RestoreVerification:
    envelope_bytes = _s3_get(config, object_key=object_key)
    envelope = _strict_object(envelope_bytes)
    if set(envelope) != {
        "ciphertext",
        "key_provider",
        "manifest",
        "manifest_sha256",
        "nonce",
        "version",
        "wrapped_data_key",
    } or envelope.get("version") != 1 or envelope.get("key_provider") != config.kms_provider:
        raise BackupRuntimeError("backup.envelope_invalid")
    manifest = envelope.get("manifest")
    if type(manifest) is not dict or set(manifest) != {
        "backup_id",
        "content_sha256",
        "schema_head",
        "size_bytes",
        "source_watermark",
        "tenant_owner_sample_count",
        "version",
    }:
        raise BackupRuntimeError("backup.manifest_invalid")
    if (
        manifest.get("version") != 1
        or type(manifest.get("size_bytes")) is not int
        or not 0 <= manifest["size_bytes"] <= _MAX_BACKUP_BYTES
        or type(manifest.get("tenant_owner_sample_count")) is not int
        or not 0 <= manifest["tenant_owner_sample_count"] <= 10_000
        or type(manifest.get("source_watermark")) is not str
        or not 1 <= len(manifest["source_watermark"]) <= 256
        or type(manifest.get("schema_head")) is not str
        or len(manifest["schema_head"]) != 4
        or type(manifest.get("content_sha256")) is not str
        or len(manifest["content_sha256"]) != 64
    ):
        raise BackupRuntimeError("backup.manifest_invalid")
    manifest_bytes = _canonical(manifest)
    actual_manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if (
        not _same(actual_manifest_sha256, expected_manifest_sha256)
        or not _same(str(envelope.get("manifest_sha256") or ""), expected_manifest_sha256)
        or manifest.get("backup_id") != backup_id
    ):
        raise BackupRuntimeError("backup.manifest_invalid")
    try:
        wrapped = base64.b64decode(str(envelope["wrapped_data_key"]), validate=True)
        nonce = base64.b64decode(str(envelope["nonce"]), validate=True)
        ciphertext = base64.b64decode(str(envelope["ciphertext"]), validate=True)
    except (KeyError, ValueError):
        raise BackupRuntimeError("backup.envelope_invalid") from None
    data_key = _decrypt_data_key(config, backup_id=backup_id, wrapped_key=wrapped)
    try:
        payload = AESGCM(data_key).decrypt(
            nonce, ciphertext, _aad(backup_id, expected_manifest_sha256)
        )
    except Exception:
        raise BackupRuntimeError("backup.decrypt_failed") from None
    finally:
        data_key = b""
    if (
        len(payload) != manifest.get("size_bytes")
        or not _same(hashlib.sha256(payload).hexdigest(), str(manifest.get("content_sha256")))
    ):
        raise BackupRuntimeError("backup.content_hash_invalid")
    backend = database_backend(config.database_url)
    if backend == "sqlite":
        schema_head, samples = _verify_sqlite_restore(payload)
    elif backend == "postgresql":
        schema_head, samples = _verify_postgres_restore(config, payload)
    else:
        raise BackupRuntimeError("backup.database_unavailable")
    if schema_head != manifest.get("schema_head"):
        raise BackupRuntimeError("backup.schema_head_invalid")
    if samples != manifest.get("tenant_owner_sample_count"):
        raise BackupRuntimeError("backup.restore_owner_failed")
    return RestoreVerification(
        schema_head=schema_head,
        manifest_sha256=actual_manifest_sha256,
        tenant_owner_sample_count=samples,
    )


def _sqlite_snapshot(config: AppConfig) -> BackupArtifact:
    source = connect_database(config.database_url)
    handle = tempfile.NamedTemporaryFile(prefix="metahub-backup-", suffix=".sqlite3", delete=False)
    target_path = Path(handle.name)
    handle.close()
    destination = sqlite3.connect(target_path)
    try:
        source.backup(destination)
        destination.commit()
        schema_head = str(
            destination.execute(
                "SELECT version FROM schema_migrations WHERE status='applied' "
                "ORDER BY version DESC LIMIT 1"
            ).fetchone()[0]
        )
        watermark = str(destination.execute("PRAGMA data_version").fetchone()[0])
        samples = _sqlite_owner_samples(destination)
        destination.close()
        payload = target_path.read_bytes()
        return BackupArtifact(
            payload=payload,
            source_watermark=f"sqlite-data-version:{watermark}",
            schema_head=schema_head,
            tenant_owner_sample_count=samples,
        )
    except (sqlite3.Error, OSError, IndexError, TypeError):
        raise BackupRuntimeError("backup.snapshot_failed") from None
    finally:
        source.close()
        try:
            destination.close()
        except sqlite3.Error:
            pass
        target_path.unlink(missing_ok=True)


def _postgres_snapshot(config: AppConfig, *, backup_type: str) -> BackupArtifact:
    if backup_type != "full":
        raise BackupRuntimeError("backup.incremental_unsupported")
    connection = connect_database(config.database_url)
    try:
        watermark = str(connection.execute("SELECT pg_current_wal_lsn()").fetchone()[0])
        schema_head = str(
            connection.execute(
                "SELECT version FROM schema_migrations WHERE status='applied' "
                "ORDER BY version DESC LIMIT 1"
            ).fetchone()[0]
        )
        samples = int(
            connection.execute(
                "SELECT COUNT(*) FROM (SELECT 1 FROM workspace_tenant_bindings LIMIT 100) owners"
            ).fetchone()[0]
        )
        connection.commit()
    except Exception:
        raise BackupRuntimeError("backup.snapshot_failed") from None
    finally:
        connection.close()
    handle = tempfile.NamedTemporaryFile(prefix="metahub-backup-", suffix=".dump", delete=False)
    path = Path(handle.name)
    handle.close()
    process_environment = os.environ.copy()
    process_environment["PGDATABASE"] = config.database_url or ""
    try:
        result = subprocess.run(
            [
                "pg_dump",
                "--format=custom",
                "--compress=6",
                "--no-owner",
                "--no-privileges",
                f"--file={path}",
            ],
            env=process_environment,
            check=False,
            capture_output=True,
            timeout=3600,
        )
        if result.returncode != 0:
            raise BackupRuntimeError("backup.snapshot_failed")
        payload = path.read_bytes()
        return BackupArtifact(
            payload=payload,
            source_watermark=f"postgresql-lsn:{watermark}",
            schema_head=schema_head,
            tenant_owner_sample_count=samples,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise BackupRuntimeError("backup.snapshot_failed") from None
    finally:
        process_environment["PGDATABASE"] = ""
        path.unlink(missing_ok=True)


def _generate_data_key(config: AppConfig, *, backup_id: str) -> tuple[bytes, bytes, str]:
    if config.kms_provider == "test":
        if not config.kms_test_master_key:
            raise BackupRuntimeError("backup.kms_unavailable")
        data_key = os.urandom(32)
        wrapping_key = hashlib.sha256(config.kms_test_master_key.encode("utf-8")).digest()
        nonce = os.urandom(12)
        wrapped = nonce + AESGCM(wrapping_key).encrypt(nonce, data_key, _key_aad(backup_id))
        return data_key, wrapped, "test-v1"
    if config.kms_provider != "aws" or not config.kms_key_id:
        raise BackupRuntimeError("backup.kms_unavailable")
    try:
        client = _boto_client(config, "kms")
        result = client.generate_data_key(
            KeyId=config.kms_key_id,
            KeySpec="AES_256",
            EncryptionContext={"domain": "metahub-backup-v1", "backup_id": backup_id},
        )
        data_key = bytes(result["Plaintext"])
        wrapped = bytes(result["CiphertextBlob"])
        key_version = "kmsv_" + hashlib.sha256(
            str(result.get("KeyId") or config.kms_key_id).encode("utf-8")
        ).hexdigest()[:24]
        if len(data_key) != 32 or not wrapped:
            raise ValueError
        return data_key, wrapped, key_version
    except Exception:
        raise BackupRuntimeError("backup.kms_unavailable") from None


def _decrypt_data_key(config: AppConfig, *, backup_id: str, wrapped_key: bytes) -> bytes:
    if config.kms_provider == "test":
        if not config.kms_test_master_key or len(wrapped_key) < 29:
            raise BackupRuntimeError("backup.kms_unavailable")
        wrapping_key = hashlib.sha256(config.kms_test_master_key.encode("utf-8")).digest()
        try:
            return AESGCM(wrapping_key).decrypt(
                wrapped_key[:12], wrapped_key[12:], _key_aad(backup_id)
            )
        except Exception:
            raise BackupRuntimeError("backup.kms_unavailable") from None
    if config.kms_provider != "aws":
        raise BackupRuntimeError("backup.kms_unavailable")
    try:
        result = _boto_client(config, "kms").decrypt(
            CiphertextBlob=wrapped_key,
            EncryptionContext={"domain": "metahub-backup-v1", "backup_id": backup_id},
        )
        key = bytes(result["Plaintext"])
        if len(key) != 32:
            raise ValueError
        return key
    except Exception:
        raise BackupRuntimeError("backup.kms_unavailable") from None


def _s3_put(
    config: AppConfig, *, object_key: str, payload: bytes, content_sha256: str
) -> None:
    if not config.object_store_bucket:
        raise BackupRuntimeError("backup.object_store_unavailable")
    try:
        _boto_client(config, "s3").put_object(
            Bucket=config.object_store_bucket,
            Key=object_key,
            Body=payload,
            ContentType="application/vnd.metahub.backup-envelope+json",
            Metadata={"manifest-sha256": content_sha256, "schema-version": "1"},
        )
    except Exception:
        raise BackupRuntimeError("backup.object_store_unavailable") from None


def _s3_get(config: AppConfig, *, object_key: str) -> bytes:
    if not config.object_store_bucket:
        raise BackupRuntimeError("backup.object_store_unavailable")
    try:
        body = _boto_client(config, "s3").get_object(
            Bucket=config.object_store_bucket, Key=object_key
        )["Body"]
        payload = body.read(_MAX_BACKUP_BYTES + 1)
        body.close()
        if len(payload) > _MAX_BACKUP_BYTES:
            raise ValueError
        return bytes(payload)
    except Exception:
        raise BackupRuntimeError("backup.object_store_unavailable") from None


def _boto_client(config: AppConfig, service: str):
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        raise BackupRuntimeError("backup.provider_driver_missing") from None
    options: dict[str, Any] = {
        "region_name": config.object_store_region,
        "config": Config(
            connect_timeout=config.infrastructure_probe_timeout_seconds,
            read_timeout=max(10, config.infrastructure_probe_timeout_seconds),
            retries={"max_attempts": 2, "mode": "standard"},
            s3={
                "addressing_style": (
                    "path" if config.object_store_force_path_style else "virtual"
                )
            },
        ),
    }
    if service == "s3":
        options.update(
            endpoint_url=config.object_store_endpoint_url,
            aws_access_key_id=config.object_store_access_key_id,
            aws_secret_access_key=config.object_store_secret_access_key,
        )
    return boto3.client(service, **options)


def _verify_sqlite_restore(payload: bytes) -> tuple[str, int]:
    handle = tempfile.NamedTemporaryFile(prefix="metahub-restore-", suffix=".sqlite3", delete=False)
    path = Path(handle.name)
    try:
        handle.write(payload)
        handle.close()
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupRuntimeError("backup.restore_integrity_failed")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise BackupRuntimeError("backup.restore_owner_failed")
            schema_head = str(
                connection.execute(
                    "SELECT version FROM schema_migrations WHERE status='applied' "
                    "ORDER BY version DESC LIMIT 1"
                ).fetchone()[0]
            )
            return schema_head, _sqlite_owner_samples(connection)
        finally:
            connection.close()
    except BackupRuntimeError:
        raise
    except (OSError, sqlite3.Error, IndexError, TypeError):
        raise BackupRuntimeError("backup.restore_failed") from None
    finally:
        try:
            handle.close()
        except OSError:
            pass
        path.unlink(missing_ok=True)


def _verify_postgres_restore(config: AppConfig, payload: bytes) -> tuple[str, int]:
    if not config.backup_restore_database_url:
        raise BackupRuntimeError("backup.restore_target_missing")
    if _database_identity(config.database_url) == _database_identity(
        config.backup_restore_database_url
    ):
        raise BackupRuntimeError("backup.restore_target_unsafe")
    handle = tempfile.NamedTemporaryFile(prefix="metahub-restore-", suffix=".dump", delete=False)
    path = Path(handle.name)
    handle.write(payload)
    handle.close()
    process_environment = os.environ.copy()
    process_environment["PGDATABASE"] = config.backup_restore_database_url
    try:
        result = subprocess.run(
            [
                "pg_restore",
                "--clean",
                "--if-exists",
                "--no-owner",
                "--no-privileges",
                f"--jobs={max(1, min(config.database_pool_max_size, 8))}",
                str(path),
            ],
            env=process_environment,
            check=False,
            capture_output=True,
            timeout=7200,
        )
        if result.returncode != 0:
            raise BackupRuntimeError("backup.restore_failed")
        connection = connect_database(config.backup_restore_database_url)
        try:
            invalid = int(
                connection.execute(
                    "SELECT COUNT(*) FROM pg_constraint WHERE NOT convalidated"
                ).fetchone()[0]
            )
            if invalid:
                raise BackupRuntimeError("backup.restore_integrity_failed")
            schema_head = str(
                connection.execute(
                    "SELECT version FROM schema_migrations WHERE status='applied' "
                    "ORDER BY version DESC LIMIT 1"
                ).fetchone()[0]
            )
            samples = int(
                connection.execute(
                    "SELECT COUNT(*) FROM "
                    "(SELECT 1 FROM workspace_tenant_bindings LIMIT 100) owners"
                ).fetchone()[0]
            )
            connection.commit()
            return schema_head, samples
        finally:
            connection.close()
    except BackupRuntimeError:
        raise
    except (OSError, subprocess.TimeoutExpired, IndexError, TypeError, ValueError):
        raise BackupRuntimeError("backup.restore_failed") from None
    finally:
        process_environment["PGDATABASE"] = ""
        path.unlink(missing_ok=True)


def _sqlite_owner_samples(connection: sqlite3.Connection) -> int:
    return int(
        connection.execute(
            """
            SELECT COUNT(*) FROM (
              SELECT binding.workspace_id FROM workspace_tenant_bindings binding
              JOIN workspaces workspace ON workspace.workspace_id=binding.workspace_id
              JOIN tenants tenant ON tenant.tenant_id=binding.tenant_id
              LIMIT 100
            ) owners
            """
        ).fetchone()[0]
    )


def _strict_object(payload: bytes) -> dict[str, object]:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=unique)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise BackupRuntimeError("backup.envelope_invalid") from None
    if type(value) is not dict:
        raise BackupRuntimeError("backup.envelope_invalid")
    return value


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def _aad(backup_id: str, manifest_sha256: str) -> bytes:
    return f"metahub/backup/v1\0{backup_id}\0{manifest_sha256}".encode("ascii")


def _key_aad(backup_id: str) -> bytes:
    return f"metahub/backup/data-key/v1\0{backup_id}".encode("ascii")


def _same(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left, right)


def _database_identity(database_url: str | None) -> tuple[str, str, int | None, str]:
    parsed = urlparse(database_url or "")
    return parsed.scheme.lower(), parsed.hostname or "", parsed.port, parsed.path


__all__ = [
    "BackupArtifact",
    "BackupRuntimeError",
    "RestoreVerification",
    "StoredBackup",
    "create_snapshot",
    "restore_and_verify",
    "store_backup",
]
