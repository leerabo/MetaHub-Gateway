"""Real snapshot, envelope encryption, object storage, and isolated restore verification."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlparse

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from metahub_shared.db import connect_database, database_backend
from metahub_shared.runtime.config import AppConfig

from .backup_consistency import TableFingerprint, fingerprint_database
from .restore_control import RestoreControl, RestoreStopped

_MAX_BACKUP_BYTES = 10 * 1024**4


@dataclass(frozen=True, slots=True)
class BackupArtifact:
    payload: bytes
    source_watermark: str
    schema_head: str
    tenant_owner_sample_count: int
    consistency: tuple[TableFingerprint, ...] | None = None
    source_database_identity_sha256: str | None = None


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
    consistency: tuple[TableFingerprint, ...] | None = None
    source_watermark: str = ""
    content_sha256: str = ""


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
        "version": 1 if artifact.consistency is None else 2,
    }
    if artifact.consistency is not None:
        manifest["consistency"] = [item.model_dump() for item in artifact.consistency]
        manifest["source_database_identity_sha256"] = artifact.source_database_identity_sha256
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
    control: RestoreControl | None = None,
    sqlite_target: Path | None = None,
) -> RestoreVerification:
    if control is not None:
        control.check()
    envelope_bytes = _s3_get(config, object_key=object_key)
    if control is not None:
        control.check()
    envelope = _strict_object(envelope_bytes)
    if (
        set(envelope)
        != {
            "ciphertext",
            "key_provider",
            "manifest",
            "manifest_sha256",
            "nonce",
            "version",
            "wrapped_data_key",
        }
        or envelope.get("version") != 1
        or envelope.get("key_provider") != config.kms_provider
    ):
        raise BackupRuntimeError("backup.envelope_invalid")
    manifest = envelope.get("manifest")
    fields = {
        "backup_id",
        "content_sha256",
        "schema_head",
        "size_bytes",
        "source_watermark",
        "tenant_owner_sample_count",
        "version",
    }
    if type(manifest) is not dict or type(manifest.get("version")) is not int:
        raise BackupRuntimeError("backup.manifest_invalid")
    version = manifest["version"]
    if version not in {1, 2} or set(manifest) != fields | (
        {"consistency", "source_database_identity_sha256"} if version == 2 else set()
    ):
        raise BackupRuntimeError("backup.manifest_invalid")
    if (
        type(manifest.get("size_bytes")) is not int
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
    expected_consistency = None
    source_identity = None
    if version == 2:
        try:
            source_identity = manifest["source_database_identity_sha256"]
            if source_identity is not None and (
                type(source_identity) is not str
                or re.fullmatch(r"[0-9a-f]{64}", source_identity) is None
            ):
                raise ValueError
            values = manifest["consistency"]
            if type(values) is not list or not 4 <= len(values) <= 1024:
                raise ValueError
            expected_consistency = tuple(TableFingerprint.model_validate(value) for value in values)
            names = [value.table_name for value in expected_consistency]
            if names != sorted(set(names)):
                raise ValueError
        except ValueError:
            raise BackupRuntimeError("backup.manifest_invalid") from None
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
    if control is not None:
        control.check()
    if len(payload) != manifest.get("size_bytes") or not _same(
        hashlib.sha256(payload).hexdigest(), str(manifest.get("content_sha256"))
    ):
        raise BackupRuntimeError("backup.content_hash_invalid")
    backend = database_backend(config.database_url)
    if backend == "sqlite":
        schema_head, samples, consistency = _verify_sqlite_restore(
            payload,
            expected_consistency=expected_consistency,
            target=sqlite_target,
            control=control,
        )
    elif backend == "postgresql":
        if version == 2 and source_identity is None:
            raise BackupRuntimeError("backup.manifest_invalid")
        schema_head, samples, consistency = _verify_postgres_restore(
            config,
            payload,
            expected_consistency=expected_consistency,
            source_database_identity_sha256=source_identity,
            control=control,
        )
    else:
        raise BackupRuntimeError("backup.database_unavailable")
    if schema_head != manifest.get("schema_head"):
        raise BackupRuntimeError("backup.schema_head_invalid")
    if samples != manifest.get("tenant_owner_sample_count"):
        raise BackupRuntimeError("backup.restore_owner_failed")
    if control is not None:
        control.check()
    return RestoreVerification(
        schema_head=schema_head,
        manifest_sha256=actual_manifest_sha256,
        tenant_owner_sample_count=samples,
        consistency=consistency,
        source_watermark=str(manifest["source_watermark"]),
        content_sha256=str(manifest["content_sha256"]),
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
        consistency = fingerprint_database(destination, backend="sqlite")
        destination.close()
        payload = target_path.read_bytes()
        return BackupArtifact(
            payload=payload,
            source_watermark=f"sqlite-data-version:{watermark}",
            schema_head=schema_head,
            tenant_owner_sample_count=samples,
            consistency=consistency,
        )
    except (sqlite3.Error, OSError, IndexError, TypeError, ValueError):
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
    _validate_postgres_url(config.database_url)
    connection = None
    path = None
    process_environment = _postgres_environment(config.database_url)
    try:
        connection = connect_database(config.database_url)
        handle = tempfile.NamedTemporaryFile(prefix="metahub-backup-", suffix=".dump", delete=False)
        path = Path(handle.name)
        handle.close()
        connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        source_identity = _database_identity_digest(connection)
        snapshot = str(connection.execute("SELECT pg_export_snapshot()").fetchone()[0])
        if re.fullmatch(r"[0-9A-Fa-f]+-[0-9A-Fa-f]+-[0-9]+", snapshot) is None:
            raise BackupRuntimeError("backup.snapshot_failed")
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
        consistency = fingerprint_database(connection, backend="postgresql")
        result = subprocess.run(
            [
                "pg_dump",
                "--format=custom",
                "--compress=6",
                "--no-owner",
                "--no-privileges",
                f"--snapshot={snapshot}",
                f"--file={path}",
            ],
            env=process_environment,
            check=False,
            capture_output=True,
            timeout=3600,
        )
        if result.returncode != 0:
            raise BackupRuntimeError("backup.snapshot_failed")
        connection.commit()
        payload = path.read_bytes()
        return BackupArtifact(
            payload=payload,
            source_watermark=f"postgresql-lsn:{watermark}",
            schema_head=schema_head,
            tenant_owner_sample_count=samples,
            consistency=consistency,
            source_database_identity_sha256=source_identity,
        )
    except BackupRuntimeError:
        raise
    except Exception:
        raise BackupRuntimeError("backup.snapshot_failed") from None
    finally:
        process_environment.clear()
        try:
            if connection is not None:
                connection.close()
        finally:
            if path is not None:
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
        key_version = (
            "kmsv_"
            + hashlib.sha256(
                str(result.get("KeyId") or config.kms_key_id).encode("utf-8")
            ).hexdigest()[:24]
        )
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


def _s3_put(config: AppConfig, *, object_key: str, payload: bytes, content_sha256: str) -> None:
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
        try:
            payload = body.read(_MAX_BACKUP_BYTES + 1)
        finally:
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
                "addressing_style": ("path" if config.object_store_force_path_style else "virtual")
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


def _verify_sqlite_restore(
    payload: bytes,
    *,
    expected_consistency: tuple[TableFingerprint, ...] | None = None,
    target: Path | None = None,
    control: RestoreControl | None = None,
) -> tuple[str, int, tuple[TableFingerprint, ...] | None]:
    handle = (
        tempfile.NamedTemporaryFile(prefix="metahub-restore-", suffix=".sqlite3", delete=False)
        if target is None
        else target.open("xb")
    )
    path = Path(handle.name)
    try:
        for start in range(0, len(payload), 1024 * 1024):
            if control is not None:
                control.check()
            handle.write(payload[start : start + 1024 * 1024])
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        stopped = []
        if control is not None:

            def progress():
                try:
                    control.check()
                    return 0
                except Exception as exc:
                    stopped.append(exc)
                    return 1

            connection.set_progress_handler(progress, 1000)
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
            consistency = _verify_consistency(connection, "sqlite", expected_consistency, control)
            return schema_head, _sqlite_owner_samples(connection), consistency
        finally:
            connection.close()
            if stopped:
                raise stopped[0]
    except BackupRuntimeError:
        raise
    except (OSError, sqlite3.Error, IndexError, TypeError):
        raise BackupRuntimeError("backup.restore_failed") from None
    finally:
        try:
            handle.close()
        except OSError:
            pass
        if target is None:
            path.unlink(missing_ok=True)


def _verify_postgres_restore(
    config: AppConfig,
    payload: bytes,
    *,
    expected_consistency: tuple[TableFingerprint, ...] | None = None,
    source_database_identity_sha256: str | None = None,
    control: RestoreControl | None = None,
) -> tuple[str, int, tuple[TableFingerprint, ...] | None]:
    if not config.backup_restore_database_url:
        raise BackupRuntimeError("backup.restore_target_missing")
    _validate_postgres_url(config.database_url)
    _validate_postgres_url(config.backup_restore_database_url)
    if _database_identity(config.database_url) == _database_identity(
        config.backup_restore_database_url
    ):
        raise BackupRuntimeError("backup.restore_target_unsafe")
    path = None
    connection = None
    locked = False
    process_environment = _postgres_environment(config.backup_restore_database_url)
    try:
        if source_database_identity_sha256 is None:
            # Legacy manifests lack an immutable source identity and require live validation.
            source = connect_database(config.database_url)
            try:
                source_database_identity_sha256 = _database_identity_digest(source)
            finally:
                source.close()
        connection = connect_database(config.backup_restore_database_url)
        if _database_identity_digest(connection) == source_database_identity_sha256:
            raise BackupRuntimeError("backup.restore_target_unsafe")
        # Session lock spans pg_restore and verification, excluding other restore workers.
        locked = bool(connection.execute("SELECT pg_try_advisory_lock(111,100)").fetchone()[0])
        if not locked:
            raise BackupRuntimeError("backup.restore_target_busy")
        if connection.execute(
            """SELECT EXISTS(
                 SELECT 1 FROM pg_catalog.pg_class c
                 JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                 WHERE n.nspname NOT IN('pg_catalog','information_schema')
                   AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%'
                 UNION ALL
                 SELECT 1 FROM pg_catalog.pg_proc p
                 JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace
                 WHERE n.nspname NOT IN('pg_catalog','information_schema'))"""
        ).fetchone()[0]:
            raise BackupRuntimeError("backup.restore_target_not_empty")
        connection.commit()
        handle = tempfile.NamedTemporaryFile(
            prefix="metahub-restore-", suffix=".dump", delete=False
        )
        path = Path(handle.name)
        try:
            handle.write(payload)
        finally:
            handle.close()
        arguments = [
            "pg_restore",
            "--dbname=" + process_environment["PGDATABASE"],
            "--single-transaction",
            "--exit-on-error",
            "--no-owner",
            "--no-privileges",
            str(path),
        ]
        if control is None:
            result = subprocess.run(
                arguments, env=process_environment, check=False, capture_output=True, timeout=7200
            )
            if result.returncode != 0:
                raise BackupRuntimeError("backup.restore_failed")
        else:
            control.run(arguments, env=process_environment)
        connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        if control is not None:
            control.check()
            connection.execute("SET LOCAL statement_timeout='30s'")
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
                "SELECT COUNT(*) FROM (SELECT 1 FROM workspace_tenant_bindings LIMIT 100) owners"
            ).fetchone()[0]
        )
        consistency = _verify_consistency(connection, "postgresql", expected_consistency, control)
        connection.commit()
        return schema_head, samples, consistency
    except (BackupRuntimeError, RestoreStopped):
        raise
    except Exception:
        raise BackupRuntimeError("backup.restore_failed") from None
    finally:
        process_environment.clear()
        if connection is not None:
            try:
                connection.rollback()
                if locked:
                    connection.execute("SELECT pg_advisory_unlock(111,100)")
                    connection.commit()
            finally:
                connection.close()
        if path is not None:
            path.unlink(missing_ok=True)


def _verify_consistency(connection, backend, expected, control=None):
    if expected is None:
        return None
    try:
        actual = (
            fingerprint_database(connection, backend=backend)
            if control is None
            else fingerprint_database(connection, backend=backend, check=control.check)
        )
    except RestoreStopped:
        raise
    except Exception:
        raise BackupRuntimeError("backup.restore_consistency_failed") from None
    if actual != expected:
        raise BackupRuntimeError("backup.restore_consistency_failed")
    return actual


def _validate_postgres_url(url: str | None) -> None:
    try:
        parsed = urlparse(url or "")
        allowed = {"sslmode", "sslcert", "sslkey", "sslrootcert", "sslcrl", "connect_timeout"}
        query = parse_qsl(parsed.query, strict_parsing=True)
        if (
            parsed.scheme not in {"postgres", "postgresql"}
            or not parsed.hostname
            or re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}", unquote(parsed.path[1:])) is None
            or parsed.fragment
            or any(key not in allowed for key, _value in query)
            or len({key for key, _value in query}) != len(query)
        ):
            raise ValueError
        _ = parsed.port
    except ValueError:
        raise BackupRuntimeError("backup.database_url_unsafe") from None


def _postgres_environment(url: str | None) -> dict[str, str]:
    # libpq variables must not override the explicitly validated source/target.
    _validate_postgres_url(url)
    parsed = urlparse(url or "")
    values = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("PG", "METAHUB_"))
    }
    values["PGDATABASE"] = unquote(parsed.path[1:])
    values["PGHOST"] = parsed.hostname or ""
    values["PGPORT"] = str(parsed.port or 5432)
    if parsed.username is not None:
        values["PGUSER"] = unquote(parsed.username)
    if parsed.password is not None:
        values["PGPASSWORD"] = unquote(parsed.password)
    for name, value in parse_qsl(parsed.query, strict_parsing=True):
        values["PG" + name.upper()] = value
    values["PGCONNECT_TIMEOUT"] = "10"
    return values


def _server_database_identity(connection) -> tuple:
    return tuple(
        connection.execute(
            "SELECT inet_server_addr()::text,inet_server_port(),current_database()"
        ).fetchone()
    )


def _database_identity_digest(connection) -> str:
    return hashlib.sha256(_canonical(_server_database_identity(connection))).hexdigest()


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
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
        "ascii"
    )


def _aad(backup_id: str, manifest_sha256: str) -> bytes:
    return f"metahub/backup/v1\0{backup_id}\0{manifest_sha256}".encode("ascii")


def _key_aad(backup_id: str) -> bytes:
    return f"metahub/backup/data-key/v1\0{backup_id}".encode("ascii")


def _same(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left, right)


def _database_identity(database_url: str | None) -> tuple[str, str, int | None, str]:
    parsed = urlparse(database_url or "")
    host = (parsed.hostname or "").lower().rstrip(".")
    if host in {"localhost", "127.0.0.1", "::1"}:
        host = "loopback"
    return "postgresql", host, parsed.port or 5432, unquote(parsed.path)


__all__ = [
    "BackupArtifact",
    "BackupRuntimeError",
    "RestoreVerification",
    "StoredBackup",
    "create_snapshot",
    "restore_and_verify",
    "store_backup",
]
