"""Local encrypted backup artifacts and verify-before-apply restore journal."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from secrets import token_bytes, token_urlsafe
from typing import Literal, Protocol
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from metahub_shared.private_gateway import (
    BACKUP_DOMAIN_CODES,
    BackupKeyEnvelope,
    EdgeBackupContractError,
    EdgeBackupPackage,
    build_edge_backup,
    canonical_edge_json_bytes,
    parse_signed_backup,
    verify_edge_backup,
)
from pydantic import BaseModel, ConfigDict, Field

from .keystore import LocalKeyProtector, LocalKeyStoreError

_BACKUP_ID = re.compile(r"^ebk_[0-9a-f]{32}$")
_RESTORE_ID = re.compile(r"^lrj_[0-9a-f]{32}$")
_REPORT_ID = re.compile(r"^lbr_[0-9a-f]{32}$")
_DRILL_ID = re.compile(r"^pdd_[0-9a-f]{32}$")
_RESTORE_JOB_ID = re.compile(r"^rst_[0-9a-f]{32}$")
_OWNER_ID = re.compile(r"^[a-z0-9][a-z0-9_:-]{2,79}$")
_DEPLOYMENT_ID = re.compile(r"^edp_[0-9a-f]{32}$")
_CHUNK_FILE = re.compile(r"^(bch_[0-9a-f]{32})\.bin$")
_SIGNING_KEY_FORMAT = "metahub-edge-backup-signing-key-v1"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS local_edge_backups(
  backup_id TEXT PRIMARY KEY CHECK(length(backup_id)=36 AND substr(backup_id,1,4)='ebk_'),
  deployment_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  requested_by_account_id TEXT NOT NULL,
  source_generation INTEGER NOT NULL CHECK(source_generation>=1),
  schema_head TEXT NOT NULL,
  storage_kind TEXT NOT NULL CHECK(storage_kind IN('local','customer_object_store')),
  status TEXT NOT NULL CHECK(status IN('ready','deleted')),
  manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64),
  merkle_root TEXT NOT NULL CHECK(length(merkle_root)=64),
  kms_key_id_sha256 TEXT NOT NULL CHECK(length(kms_key_id_sha256)=64),
  chunk_count INTEGER NOT NULL CHECK(chunk_count>=4),
  encrypted_size_bytes INTEGER NOT NULL CHECK(encrypted_size_bytes>0),
  artifact_directory TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK(revision>=1)
);
CREATE INDEX IF NOT EXISTS idx_local_edge_backups_owner
  ON local_edge_backups(tenant_id,workspace_id,created_at DESC,backup_id);
CREATE TRIGGER IF NOT EXISTS local_edge_backup_identity_immutable
BEFORE UPDATE ON local_edge_backups FOR EACH ROW
WHEN NEW.backup_id!=OLD.backup_id OR NEW.deployment_id!=OLD.deployment_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.workspace_id!=OLD.workspace_id
  OR NEW.requested_by_account_id!=OLD.requested_by_account_id
  OR NEW.source_generation!=OLD.source_generation OR NEW.schema_head!=OLD.schema_head
  OR NEW.storage_kind!=OLD.storage_kind OR NEW.manifest_sha256!=OLD.manifest_sha256
  OR NEW.merkle_root!=OLD.merkle_root OR NEW.kms_key_id_sha256!=OLD.kms_key_id_sha256
  OR NEW.chunk_count!=OLD.chunk_count OR NEW.encrypted_size_bytes!=OLD.encrypted_size_bytes
  OR NEW.artifact_directory!=OLD.artifact_directory OR NEW.created_at!=OLD.created_at
  OR NEW.expires_at!=OLD.expires_at OR NEW.revision!=OLD.revision+1
  OR OLD.status='deleted' OR NEW.status!='deleted'
BEGIN SELECT RAISE(ABORT,'local_edge_backup_immutable'); END;
CREATE TRIGGER IF NOT EXISTS local_edge_backup_no_delete
BEFORE DELETE ON local_edge_backups
BEGIN SELECT RAISE(ABORT,'local_edge_backup_immutable'); END;

CREATE TABLE IF NOT EXISTS local_restore_journals(
  restore_id TEXT PRIMARY KEY CHECK(length(restore_id)=36 AND substr(restore_id,1,4)='lrj_'),
  backup_id TEXT NOT NULL REFERENCES local_edge_backups(backup_id),
  deployment_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  requested_by_account_id TEXT NOT NULL,
  target_schema_head TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('staging','verified','applying','succeeded','failed')),
  failure_code TEXT,
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL,
  completed_at TEXT,
  updated_at TEXT NOT NULL,
  CHECK((status IN('succeeded','failed') AND completed_at IS NOT NULL)
    OR(status NOT IN('succeeded','failed') AND completed_at IS NULL)),
  CHECK((status='failed' AND failure_code IS NOT NULL) OR status!='failed')
);
CREATE INDEX IF NOT EXISTS idx_local_restore_journals_owner
  ON local_restore_journals(tenant_id,workspace_id,created_at DESC,restore_id);
CREATE TRIGGER IF NOT EXISTS local_restore_journal_transition
BEFORE UPDATE ON local_restore_journals FOR EACH ROW
WHEN NEW.restore_id!=OLD.restore_id OR NEW.backup_id!=OLD.backup_id
  OR NEW.deployment_id!=OLD.deployment_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.workspace_id!=OLD.workspace_id
  OR NEW.requested_by_account_id!=OLD.requested_by_account_id
  OR NEW.target_schema_head!=OLD.target_schema_head OR NEW.created_at!=OLD.created_at
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
  OR OLD.status IN('succeeded','failed')
  OR (OLD.status='staging' AND NEW.status NOT IN('verified','failed'))
  OR (OLD.status='verified' AND NEW.status NOT IN('applying','failed'))
  OR (OLD.status='applying' AND NEW.status NOT IN('succeeded','failed'))
BEGIN SELECT RAISE(ABORT,'local_restore_transition_invalid'); END;
CREATE TRIGGER IF NOT EXISTS local_restore_journal_no_delete
BEFORE DELETE ON local_restore_journals
BEGIN SELECT RAISE(ABORT,'local_restore_journal_immutable'); END;

CREATE TABLE IF NOT EXISTS local_restore_steps(
  restore_step_id TEXT PRIMARY KEY,
  restore_id TEXT NOT NULL REFERENCES local_restore_journals(restore_id),
  sequence INTEGER NOT NULL CHECK(sequence>=1),
  step_code TEXT NOT NULL CHECK(step_code IN('staging','integrity','apply','complete','failed')),
  outcome TEXT NOT NULL CHECK(outcome IN('passed','failed')),
  evidence_sha256 TEXT NOT NULL CHECK(length(evidence_sha256)=64),
  occurred_at TEXT NOT NULL,
  UNIQUE(restore_id,sequence)
);
CREATE TRIGGER IF NOT EXISTS local_restore_step_immutable_update
BEFORE UPDATE ON local_restore_steps BEGIN SELECT RAISE(ABORT,'local_restore_step_immutable'); END;
CREATE TRIGGER IF NOT EXISTS local_restore_step_immutable_delete
BEFORE DELETE ON local_restore_steps BEGIN SELECT RAISE(ABORT,'local_restore_step_immutable'); END;

CREATE TABLE IF NOT EXISTS local_backup_report_outbox(
  report_id TEXT PRIMARY KEY CHECK(length(report_id)=36 AND substr(report_id,1,4)='lbr_'),
  report_kind TEXT NOT NULL CHECK(report_kind IN('backup','drill')),
  deployment_id TEXT NOT NULL,
  aggregate_id TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
  created_at TEXT NOT NULL,
  delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_local_backup_report_pending
  ON local_backup_report_outbox(delivered_at,created_at,report_id);
CREATE TRIGGER IF NOT EXISTS local_backup_report_delivery_only
BEFORE UPDATE ON local_backup_report_outbox FOR EACH ROW
WHEN NEW.report_id!=OLD.report_id OR NEW.report_kind!=OLD.report_kind
  OR NEW.deployment_id!=OLD.deployment_id OR NEW.aggregate_id!=OLD.aggregate_id
  OR NEW.payload_json!=OLD.payload_json OR NEW.payload_sha256!=OLD.payload_sha256
  OR NEW.created_at!=OLD.created_at OR OLD.delivered_at IS NOT NULL
  OR NEW.delivered_at IS NULL OR NEW.delivered_at<=OLD.created_at
BEGIN SELECT RAISE(ABORT,'local_backup_report_immutable'); END;
CREATE TRIGGER IF NOT EXISTS local_backup_report_no_delete
BEFORE DELETE ON local_backup_report_outbox
BEGIN SELECT RAISE(ABORT,'local_backup_report_immutable'); END;
"""


class LocalBackupError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class LocalBackupSummary(_StrictModel):
    backup_id: str = Field(pattern=_BACKUP_ID.pattern)
    deployment_id: str = Field(pattern=_DEPLOYMENT_ID.pattern)
    source_generation: int = Field(ge=1)
    schema_head: str = Field(pattern=r"^[0-9]{4}$")
    storage_kind: Literal["local", "customer_object_store"]
    status: Literal["ready", "deleted"]
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    merkle_root: str = Field(pattern=r"^[0-9a-f]{64}$")
    kms_key_id_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    chunk_count: int = Field(ge=4)
    encrypted_size_bytes: int = Field(gt=0)
    created_at: str
    expires_at: str
    revision: int = Field(ge=1)


class LocalRestoreSummary(_StrictModel):
    restore_id: str = Field(pattern=_RESTORE_ID.pattern)
    backup_id: str = Field(pattern=_BACKUP_ID.pattern)
    deployment_id: str = Field(pattern=_DEPLOYMENT_ID.pattern)
    target_schema_head: str = Field(pattern=r"^[0-9]{4}$")
    status: Literal["staging", "verified", "applying", "succeeded", "failed"]
    failure_code: str | None = None
    revision: int = Field(ge=1)
    created_at: str
    completed_at: str | None = None
    updated_at: str


class LocalDrillContext(_StrictModel):
    drill_id: str = Field(pattern=_DRILL_ID.pattern)
    restore_job_id: str = Field(pattern=_RESTORE_JOB_ID.pattern)
    expected_revision: int = Field(ge=2)
    source_backup_completed_at: datetime
    device_reenrollment_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    minimal_request_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class LocalCloudReport(_StrictModel):
    report_id: str = Field(pattern=_REPORT_ID.pattern)
    report_kind: Literal["backup", "drill"]
    deployment_id: str = Field(pattern=_DEPLOYMENT_ID.pattern)
    aggregate_id: str
    payload_json: bytes = Field(repr=False)
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: str


class RestoreApplier(Protocol):
    def __call__(
        self,
        domains: Mapping[str, bytes],
        *,
        tenant_id: str,
        workspace_id: str,
        source_deployment_id: str,
        restore_id: str,
    ) -> None: ...


class LocalBackupSigningKeyStore:
    """Persist a purpose-specific Ed25519 seed protected outside backup artifacts."""

    def __init__(self, path: str | Path, protector: LocalKeyProtector) -> None:
        self._path = Path(path).resolve()
        self._protector = protector

    def load_or_create(self) -> Ed25519PrivateKey:
        if self._path.exists():
            return self.load()
        seed = bytearray(token_bytes(32))
        temporary: Path | None = None
        try:
            protected = self._protector.protect(
                bytes(seed), purpose="edge-backup-signing-key"
            )
            document = canonical_edge_json_bytes(
                {
                    "format": _SIGNING_KEY_FORMAT,
                    "protected_seed": _b64(protected),
                },
                maximum=65_536,
            )
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="xb", dir=self._path.parent, prefix=".backup-key-", delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(document)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, self._path)
            except FileExistsError:
                pass
            finally:
                temporary.unlink(missing_ok=True)
            return self.load()
        except (LocalKeyStoreError, OSError, TypeError, ValueError):
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise LocalBackupError("local_backup.signing_key_unavailable") from None
        finally:
            seed[:] = b"\x00" * len(seed)

    def load(self) -> Ed25519PrivateKey:
        try:
            raw = self._path.read_bytes()
            if not 1 <= len(raw) <= 65_536:
                raise ValueError
            document = json.loads(raw, object_pairs_hook=_unique_object)
            if set(document) != {"format", "protected_seed"} or document["format"] != (
                _SIGNING_KEY_FORMAT
            ):
                raise ValueError
            seed = bytearray(
                self._protector.unprotect(
                    _unb64(document["protected_seed"]),
                    purpose="edge-backup-signing-key",
                )
            )
            if len(seed) != 32:
                raise ValueError
            return Ed25519PrivateKey.from_private_bytes(bytes(seed))
        except (LocalKeyStoreError, OSError, TypeError, ValueError, json.JSONDecodeError):
            raise LocalBackupError("local_backup.signing_key_unavailable") from None
        finally:
            if "seed" in locals():
                seed[:] = b"\x00" * len(seed)


class LocalBackupStore:
    """Own immutable local artifacts and call restore only after full verification."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        output_root: str | Path,
        deployment_id: str,
        schema_head: str,
        signing_key: Ed25519PrivateKey,
        signing_key_id: str,
        kms_key_id: str,
        kms_key_version: int,
        wrap_data_key: Callable[[bytes], bytes],
        unwrap_data_key: Callable[[bytes], bytes],
        domain_provider: Callable[[str, str], Mapping[str, bytes]],
        restore_applier: RestoreApplier,
        customer_object_store_root: str | Path | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._database_path = Path(database_path).resolve()
        raw_root = Path(output_root)
        self._output_root = raw_root.resolve()
        raw_customer_root = (
            None if customer_object_store_root is None else Path(customer_object_store_root)
        )
        self._customer_object_store_root = (
            None if raw_customer_root is None else raw_customer_root.resolve()
        )
        self._deployment_id = deployment_id
        self._schema_head = schema_head
        self._signing_key = signing_key
        self._signing_key_id = signing_key_id
        self._kms_key_id = kms_key_id
        self._kms_key_version = kms_key_version
        self._wrap_data_key = wrap_data_key
        self._unwrap_data_key = unwrap_data_key
        self._domain_provider = domain_provider
        self._restore_applier = restore_applier
        self._now = now
        self._lock = threading.RLock()
        if (
            _DEPLOYMENT_ID.fullmatch(deployment_id) is None
            or re.fullmatch(r"[0-9]{4}", schema_head) is None
            or not isinstance(signing_key, Ed25519PrivateKey)
            or not 3 <= len(signing_key_id) <= 80
            or not 3 <= len(kms_key_id) <= 512
            or type(kms_key_version) is not int
            or kms_key_version < 1
            or any(
                not callable(value)
                for value in (
                    wrap_data_key,
                    unwrap_data_key,
                    domain_provider,
                    restore_applier,
                    now,
                )
            )
            or raw_root.is_symlink()
            or (raw_customer_root is not None and raw_customer_root.is_symlink())
            or self._database_path == self._output_root
            or self._customer_object_store_root in {
                self._database_path,
                self._output_root,
            }
        ):
            raise LocalBackupError("local_backup.configuration_invalid")
        self._output_root.mkdir(parents=True, exist_ok=True)
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        if self._output_root.is_symlink() or not self._output_root.is_dir():
            raise LocalBackupError("local_backup.configuration_invalid")
        if self._customer_object_store_root is not None:
            self._customer_object_store_root.mkdir(parents=True, exist_ok=True)
            if (
                self._customer_object_store_root.is_symlink()
                or not self._customer_object_store_root.is_dir()
            ):
                raise LocalBackupError("local_backup.configuration_invalid")
        self._initialize()

    def create(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        account_id: str,
        source_generation: int,
        retention_seconds: int,
        storage_kind: Literal["local", "customer_object_store"] = "local",
    ) -> LocalBackupSummary:
        _owners(tenant_id, workspace_id, account_id)
        if (
            type(source_generation) is not int
            or source_generation < 1
            or type(retention_seconds) is not int
            or not 3600 <= retention_seconds <= 31_536_000
            or storage_kind not in {"local", "customer_object_store"}
        ):
            raise LocalBackupError("local_backup.request_invalid")
        now = self._clock()
        backup_id = "ebk_" + uuid4().hex
        try:
            artifact_root = self._artifact_root(storage_kind)
            domains = self._domain_provider(tenant_id, workspace_id)
            package = build_edge_backup(
                backup_id=backup_id,
                workspace_id=workspace_id,
                tenant_id=tenant_id,
                source_deployment_id=self._deployment_id,
                source_generation=source_generation,
                source_schema_head=self._schema_head,
                domains=domains,
                key_id=self._signing_key_id,
                kms_key_id=self._kms_key_id,
                key_version=self._kms_key_version,
                wrap_data_key=self._wrap_data_key,
                signing_key=self._signing_key,
                created_at=now,
                expires_at=now + timedelta(seconds=retention_seconds),
            )
            artifact_directory, encrypted_size = self._write_package(
                package, artifact_root=artifact_root
            )
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO local_edge_backups(
                      backup_id,deployment_id,tenant_id,workspace_id,requested_by_account_id,
                      source_generation,schema_head,storage_kind,status,manifest_sha256,
                      merkle_root,kms_key_id_sha256,chunk_count,encrypted_size_bytes,
                      artifact_directory,created_at,expires_at,revision)
                    VALUES(?,?,?,?,?,?,?,?,'ready',?,?,?,?,?,?,?,?,1)
                    """,
                    (
                        backup_id,
                        self._deployment_id,
                        tenant_id,
                        workspace_id,
                        account_id,
                        source_generation,
                        self._schema_head,
                        storage_kind,
                        package.signed.manifest_sha256,
                        package.signed.manifest.chunk_merkle_root,
                        package.key_envelope.kms_key_id_sha256,
                        len(package.chunks),
                        encrypted_size,
                        str(artifact_directory),
                        _time(now),
                        _time(package.signed.manifest.expires_at),
                    ),
                )
                row = self._owned_backup(connection, backup_id, tenant_id, workspace_id)
                completed = self._clock()
                self._enqueue_report(
                    connection,
                    report_kind="backup",
                    aggregate_id=backup_id,
                    payload={
                        "backup_id": backup_id,
                        "source_generation": source_generation,
                        "schema_head": self._schema_head,
                        "storage_kind": storage_kind,
                        "status": "completed",
                        "size_bucket": _size_bucket(encrypted_size),
                        "chunk_count": len(package.chunks),
                        "manifest_sha256": package.signed.manifest_sha256,
                        "kms_key_id_sha256": package.key_envelope.kms_key_id_sha256,
                        "check_outcome": "verified",
                        "failure_code": None,
                        "started_at": _time(now),
                        "completed_at": _time(completed),
                        "retention_until": _time(package.signed.manifest.expires_at),
                        "attempt_nonce": token_urlsafe(32),
                    },
                    created_at=_time(completed),
                )
                connection.commit()
                return self._backup_summary(row)
            except Exception:
                connection.rollback()
                _remove_directory(artifact_directory)
                raise
            finally:
                connection.close()
        except LocalBackupError as exc:
            self._enqueue_failed_backup_best_effort(
                backup_id=backup_id,
                source_generation=source_generation,
                storage_kind=storage_kind,
                started_at=now,
                retention_seconds=retention_seconds,
                failure_code=exc.code,
            )
            raise
        except EdgeBackupContractError as exc:
            code = _contract_code(exc.code, "create")
            self._enqueue_failed_backup_best_effort(
                backup_id=backup_id,
                source_generation=source_generation,
                storage_kind=storage_kind,
                started_at=now,
                retention_seconds=retention_seconds,
                failure_code=code,
            )
            raise LocalBackupError(code) from None
        except (OSError, sqlite3.Error, TypeError, ValueError):
            self._enqueue_failed_backup_best_effort(
                backup_id=backup_id,
                source_generation=source_generation,
                storage_kind=storage_kind,
                started_at=now,
                retention_seconds=retention_seconds,
                failure_code="local_backup.unavailable",
            )
            raise LocalBackupError("local_backup.unavailable") from None

    def restore(
        self,
        backup_id: str,
        *,
        tenant_id: str,
        workspace_id: str,
        account_id: str,
        target_schema_head: str,
        drill_context: LocalDrillContext | None = None,
    ) -> LocalRestoreSummary:
        _owners(tenant_id, workspace_id, account_id)
        if _BACKUP_ID.fullmatch(backup_id) is None or re.fullmatch(
            r"[0-9]{4}", target_schema_head
        ) is None:
            raise LocalBackupError("local_restore.request_invalid")
        if drill_context is not None and not isinstance(drill_context, LocalDrillContext):
            raise LocalBackupError("local_restore.request_invalid")
        restore_id = "lrj_" + uuid4().hex
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                backup = self._owned_backup(connection, backup_id, tenant_id, workspace_id)
                if str(backup["status"]) != "ready":
                    raise LocalBackupError("local_restore.backup_unavailable")
                now = self._clock()
                if drill_context is not None and (
                    drill_context.source_backup_completed_at.tzinfo is None
                    or drill_context.source_backup_completed_at.astimezone(UTC) > now
                ):
                    raise LocalBackupError("local_restore.request_invalid")
                stamp = _time(now)
                connection.execute(
                    """
                    INSERT INTO local_restore_journals(
                      restore_id,backup_id,deployment_id,tenant_id,workspace_id,
                      requested_by_account_id,target_schema_head,status,failure_code,
                      revision,created_at,completed_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,'staging',NULL,1,?,NULL,?)
                    """,
                    (
                        restore_id,
                        backup_id,
                        self._deployment_id,
                        tenant_id,
                        workspace_id,
                        account_id,
                        target_schema_head,
                        stamp,
                        stamp,
                    ),
                )
                self._step(
                    connection,
                    restore_id,
                    1,
                    "staging",
                    "passed",
                    _sha(f"{backup_id}\0{target_schema_head}".encode("ascii")),
                    stamp,
                )
                connection.commit()
            except LocalBackupError:
                connection.rollback()
                raise
            except sqlite3.Error:
                connection.rollback()
                raise LocalBackupError("local_restore.unavailable") from None
            finally:
                connection.close()

            try:
                package = self._load_package(Path(str(backup["artifact_directory"])), backup)
                verified = verify_edge_backup(
                    package,
                    expected_workspace_id=workspace_id,
                    expected_tenant_id=tenant_id,
                    expected_source_deployment_id=self._deployment_id,
                    target_schema_head=target_schema_head,
                    unwrap_data_key=self._unwrap_data_key,
                    now=self._clock(),
                )
                self._transition(
                    restore_id,
                    from_status="staging",
                    to_status="verified",
                    step_code="integrity",
                    evidence_sha256=package.signed.manifest_sha256,
                )
                self._transition(
                    restore_id,
                    from_status="verified",
                    to_status="applying",
                    step_code="apply",
                    evidence_sha256=_sha(
                        canonical_edge_json_bytes(
                            {
                                "backup_id": backup_id,
                                "restore_id": restore_id,
                                "domains": list(BACKUP_DOMAIN_CODES),
                            }
                        )
                    ),
                )
                self._restore_applier(
                    verified.domains,
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    source_deployment_id=self._deployment_id,
                    restore_id=restore_id,
                )
                verification_evidence = {
                    "manifest_integrity": package.signed.manifest_sha256,
                    "owner_binding": _sha(
                        canonical_edge_json_bytes(
                            {
                                "deployment_id": self._deployment_id,
                                "tenant_id": tenant_id,
                                "workspace_id": workspace_id,
                            }
                        )
                    ),
                    "schema_compatibility": _sha(
                        f"{package.signed.manifest.source_schema_head}\0{target_schema_head}".encode(
                            "ascii"
                        )
                    ),
                    "config_consistency": _sha(verified.domains["config_policy"]),
                    "usage_cursor": _sha(verified.domains["ledger_watermark"]),
                    "audit_chain": _sha(verified.domains["audit_chain"]),
                    "secret_reference": _sha(verified.domains["local_secrets"]),
                }
                return self._finish(
                    restore_id,
                    succeeded=True,
                    drill_context=drill_context,
                    backup=backup,
                    restore_started_at=now,
                    verification_evidence=verification_evidence,
                )
            except EdgeBackupContractError as exc:
                self._finish(
                    restore_id,
                    succeeded=False,
                    failure_code=_contract_code(exc.code, "restore"),
                    drill_context=drill_context,
                    backup=backup,
                    restore_started_at=now,
                )
                raise LocalBackupError(_contract_code(exc.code, "restore")) from None
            except LocalBackupError as exc:
                self._finish(
                    restore_id,
                    succeeded=False,
                    failure_code=exc.code,
                    drill_context=drill_context,
                    backup=backup,
                    restore_started_at=now,
                )
                raise
            except Exception:
                self._finish(
                    restore_id,
                    succeeded=False,
                    failure_code="local_restore.apply_failed",
                    drill_context=drill_context,
                    backup=backup,
                    restore_started_at=now,
                )
                raise LocalBackupError("local_restore.apply_failed") from None

    def next_cloud_report(self) -> LocalCloudReport | None:
        """Return the oldest immutable, retry-stable cloud-safe report."""

        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM local_backup_report_outbox
                WHERE delivered_at IS NULL ORDER BY created_at,report_id LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            payload = str(row["payload_json"]).encode("ascii")
            if _sha(payload) != str(row["payload_sha256"]):
                raise LocalBackupError("local_backup.report_invalid")
            return LocalCloudReport(
                report_id=str(row["report_id"]),
                report_kind=str(row["report_kind"]),  # type: ignore[arg-type]
                deployment_id=str(row["deployment_id"]),
                aggregate_id=str(row["aggregate_id"]),
                payload_json=payload,
                payload_sha256=str(row["payload_sha256"]),
                created_at=str(row["created_at"]),
            )
        except LocalBackupError:
            raise
        except (UnicodeEncodeError, sqlite3.Error, TypeError, ValueError):
            raise LocalBackupError("local_backup.report_unavailable") from None
        finally:
            connection.close()

    def acknowledge_cloud_report(self, report_id: str, payload_sha256: str) -> bool:
        """Mark exactly one matching report delivered after validating the cloud receipt."""

        if (
            _REPORT_ID.fullmatch(report_id) is None
            or re.fullmatch(r"[0-9a-f]{64}", payload_sha256) is None
        ):
            raise LocalBackupError("local_backup.receipt_invalid")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM local_backup_report_outbox WHERE report_id=?",
                (report_id,),
            ).fetchone()
            if row is None or not hashlib.sha256(
                str(row["payload_sha256"]).encode("ascii")
            ).digest() == hashlib.sha256(payload_sha256.encode("ascii")).digest():
                raise LocalBackupError("local_backup.receipt_invalid")
            if row["delivered_at"] is not None:
                connection.rollback()
                return False
            delivered_at = _strictly_after(self._clock(), str(row["created_at"]))
            if connection.execute(
                """
                UPDATE local_backup_report_outbox SET delivered_at=?
                WHERE report_id=? AND delivered_at IS NULL
                """,
                (delivered_at, report_id),
            ).rowcount != 1:
                raise LocalBackupError("local_backup.receipt_invalid")
            connection.commit()
            return True
        except LocalBackupError:
            connection.rollback()
            raise
        except sqlite3.Error:
            connection.rollback()
            raise LocalBackupError("local_backup.report_unavailable") from None
        finally:
            connection.close()

    def pending_cloud_report_count(self) -> int:
        connection = self._connect()
        try:
            return int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM local_backup_report_outbox
                    WHERE delivered_at IS NULL
                    """
                ).fetchone()[0]
            )
        except sqlite3.Error:
            raise LocalBackupError("local_backup.report_unavailable") from None
        finally:
            connection.close()

    def _write_package(
        self, package: EdgeBackupPackage, *, artifact_root: Path
    ) -> tuple[Path, int]:
        staging = Path(
            tempfile.mkdtemp(prefix=".edge-backup-", dir=artifact_root)
        ).resolve()
        final = (artifact_root / package.signed.manifest.backup_id).resolve()
        if final.parent != artifact_root or final.exists():
            _remove_directory(staging)
            raise LocalBackupError("local_backup.storage_conflict")
        try:
            files: dict[str, bytes] = {
                "manifest.json": canonical_edge_json_bytes(
                    package.signed.model_dump(mode="json"), maximum=8_388_608
                ),
                "envelope.json": canonical_edge_json_bytes(
                    package.key_envelope.model_dump(mode="json"), maximum=65_536
                ),
            }
            files.update({f"{key}.bin": value for key, value in package.chunks.items()})
            total = 0
            for name, value in files.items():
                path = staging / name
                with path.open("xb") as stream:
                    stream.write(value)
                    stream.flush()
                    os.fsync(stream.fileno())
                total += len(value)
            os.replace(staging, final)
            return final, total
        except Exception:
            _remove_directory(staging)
            raise

    def _load_package(
        self, artifact_directory: Path, row: sqlite3.Row
    ) -> EdgeBackupPackage:
        resolved = artifact_directory.resolve()
        artifact_root = self._artifact_root(str(row["storage_kind"]))
        if (
            resolved.parent != artifact_root
            or resolved.name != str(row["backup_id"])
            or resolved.is_symlink()
            or not resolved.is_dir()
        ):
            raise LocalBackupError("local_restore.artifact_invalid")
        entries = tuple(resolved.iterdir())
        if any(item.is_symlink() or not item.is_file() for item in entries):
            raise LocalBackupError("local_restore.artifact_invalid")
        names = {item.name for item in entries}
        if not {"manifest.json", "envelope.json"} <= names:
            raise LocalBackupError("local_restore.artifact_invalid")
        manifest_raw = (resolved / "manifest.json").read_bytes()
        signed = parse_signed_backup(manifest_raw)
        try:
            envelope_raw = (resolved / "envelope.json").read_bytes()
            if canonical_edge_json_bytes(
                json.loads(envelope_raw, object_pairs_hook=_unique_object), maximum=65_536
            ) != envelope_raw:
                raise ValueError
            envelope = BackupKeyEnvelope.model_validate_json(envelope_raw, strict=True)
        except (TypeError, ValueError, json.JSONDecodeError):
            raise LocalBackupError("local_restore.artifact_invalid") from None
        expected_files = {
            "manifest.json",
            "envelope.json",
            *(f"{item.chunk_id}.bin" for item in signed.manifest.chunks),
        }
        if names != expected_files:
            raise LocalBackupError("local_restore.artifact_invalid")
        chunks: dict[str, bytes] = {}
        for item in entries:
            match = _CHUNK_FILE.fullmatch(item.name)
            if match is not None:
                chunks[match.group(1)] = item.read_bytes()
        package = EdgeBackupPackage(signed, envelope, chunks)
        if (
            signed.manifest.backup_id != row["backup_id"]
            or signed.manifest_sha256 != row["manifest_sha256"]
            or signed.manifest.chunk_merkle_root != row["merkle_root"]
            or envelope.kms_key_id_sha256 != row["kms_key_id_sha256"]
            or len(chunks) != int(row["chunk_count"])
        ):
            raise LocalBackupError("local_restore.artifact_invalid")
        return package

    def _transition(
        self,
        restore_id: str,
        *,
        from_status: str,
        to_status: str,
        step_code: str,
        evidence_sha256: str,
    ) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._restore_row(connection, restore_id)
            stamp = _strictly_after(self._clock(), str(row["updated_at"]))
            if connection.execute(
                """
                UPDATE local_restore_journals SET status=?,revision=revision+1,updated_at=?
                WHERE restore_id=? AND status=? AND revision=?
                """,
                (to_status, stamp, restore_id, from_status, row["revision"]),
            ).rowcount != 1:
                raise LocalBackupError("local_restore.state_conflict")
            self._step(
                connection,
                restore_id,
                int(row["revision"]) + 1,
                step_code,
                "passed",
                evidence_sha256,
                stamp,
            )
            connection.commit()
        except LocalBackupError:
            connection.rollback()
            raise
        except sqlite3.Error:
            connection.rollback()
            raise LocalBackupError("local_restore.unavailable") from None
        finally:
            connection.close()

    def _finish(
        self,
        restore_id: str,
        *,
        succeeded: bool,
        failure_code: str | None = None,
        drill_context: LocalDrillContext | None = None,
        backup: sqlite3.Row | None = None,
        restore_started_at: datetime | None = None,
        verification_evidence: Mapping[str, str] | None = None,
    ) -> LocalRestoreSummary:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._restore_row(connection, restore_id)
            if str(row["status"]) in {"succeeded", "failed"}:
                connection.rollback()
                return self._restore_summary(row)
            stamp = _strictly_after(self._clock(), str(row["updated_at"]))
            target = "succeeded" if succeeded else "failed"
            code = None if succeeded else (failure_code or "local_restore.failed")
            if connection.execute(
                """
                UPDATE local_restore_journals SET status=?,failure_code=?,
                  revision=revision+1,completed_at=?,updated_at=?
                WHERE restore_id=? AND status NOT IN('succeeded','failed') AND revision=?
                """,
                (target, code, stamp, stamp, restore_id, row["revision"]),
            ).rowcount != 1:
                raise LocalBackupError("local_restore.state_conflict")
            self._step(
                connection,
                restore_id,
                int(row["revision"]) + 1,
                "complete" if succeeded else "failed",
                "passed" if succeeded else "failed",
                _sha((code or "restore_succeeded").encode("ascii")),
                stamp,
            )
            if drill_context is not None:
                if backup is None or restore_started_at is None:
                    raise LocalBackupError("local_restore.report_invalid")
                self._enqueue_report(
                    connection,
                    report_kind="drill",
                    aggregate_id=drill_context.drill_id,
                    payload=self._drill_report_payload(
                        drill_context,
                        backup=backup,
                        succeeded=succeeded,
                        completed_at=stamp,
                        restore_started_at=restore_started_at,
                        failure_code=code,
                        verification_evidence=verification_evidence,
                    ),
                    created_at=stamp,
                )
            current = self._restore_row(connection, restore_id)
            connection.commit()
            return self._restore_summary(current)
        except LocalBackupError:
            connection.rollback()
            raise
        except sqlite3.Error:
            connection.rollback()
            raise LocalBackupError("local_restore.unavailable") from None
        finally:
            connection.close()

    def _artifact_root(self, storage_kind: str) -> Path:
        if storage_kind == "local":
            return self._output_root
        if storage_kind == "customer_object_store":
            if self._customer_object_store_root is None:
                raise LocalBackupError("local_backup.customer_store_unavailable")
            return self._customer_object_store_root
        raise LocalBackupError("local_backup.request_invalid")

    def _enqueue_failed_backup_best_effort(
        self,
        *,
        backup_id: str,
        source_generation: int,
        storage_kind: str,
        started_at: datetime,
        retention_seconds: int,
        failure_code: str,
    ) -> None:
        try:
            completed = self._clock()
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._enqueue_report(
                    connection,
                    report_kind="backup",
                    aggregate_id=backup_id,
                    payload={
                        "backup_id": backup_id,
                        "source_generation": source_generation,
                        "schema_head": self._schema_head,
                        "storage_kind": storage_kind,
                        "status": "failed",
                        "size_bucket": "lt_100mb",
                        "chunk_count": 0,
                        "manifest_sha256": None,
                        "kms_key_id_sha256": None,
                        "check_outcome": "failed",
                        "failure_code": failure_code,
                        "started_at": _time(started_at),
                        "completed_at": _time(completed),
                        "retention_until": _time(
                            max(completed, started_at)
                            + timedelta(seconds=retention_seconds)
                        ),
                        "attempt_nonce": token_urlsafe(32),
                    },
                    created_at=_time(completed),
                )
                connection.commit()
            except Exception:
                connection.rollback()
            finally:
                connection.close()
        except Exception:
            return

    def _enqueue_report(
        self,
        connection: sqlite3.Connection,
        *,
        report_kind: Literal["backup", "drill"],
        aggregate_id: str,
        payload: Mapping[str, object],
        created_at: str,
    ) -> None:
        if (
            (report_kind == "backup" and _BACKUP_ID.fullmatch(aggregate_id) is None)
            or (report_kind == "drill" and _DRILL_ID.fullmatch(aggregate_id) is None)
        ):
            raise LocalBackupError("local_backup.report_invalid")
        encoded = canonical_edge_json_bytes(dict(payload), maximum=65_536)
        connection.execute(
            """
            INSERT INTO local_backup_report_outbox(
              report_id,report_kind,deployment_id,aggregate_id,payload_json,
              payload_sha256,created_at,delivered_at)
            VALUES(?,?,?,?,?,?,?,NULL)
            """,
            (
                "lbr_" + uuid4().hex,
                report_kind,
                self._deployment_id,
                aggregate_id,
                encoded.decode("ascii"),
                _sha(encoded),
                created_at,
            ),
        )

    def _drill_report_payload(
        self,
        context: LocalDrillContext,
        *,
        backup: sqlite3.Row,
        succeeded: bool,
        completed_at: str,
        restore_started_at: datetime,
        failure_code: str | None,
        verification_evidence: Mapping[str, str] | None,
    ) -> dict[str, object]:
        completed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
        source_completed = context.source_backup_completed_at.astimezone(UTC)
        actual_rpo = max(0, int((restore_started_at - source_completed).total_seconds()))
        actual_rto = max(0, int((completed - restore_started_at).total_seconds()))
        fallback = _sha(
            canonical_edge_json_bytes(
                {
                    "backup_id": str(backup["backup_id"]),
                    "failure_code": failure_code or "restore_verified",
                }
            )
        )
        supplied = dict(verification_evidence or {})
        supplied.update(
            {
                "device_reenrollment": context.device_reenrollment_evidence_sha256,
                "minimal_request": context.minimal_request_evidence_sha256,
            }
        )
        codes = (
            "manifest_integrity",
            "owner_binding",
            "schema_compatibility",
            "config_consistency",
            "usage_cursor",
            "audit_chain",
            "secret_reference",
            "device_reenrollment",
            "minimal_request",
        )
        return {
            "restore_job_id": context.restore_job_id,
            "expected_revision": context.expected_revision,
            "outcome": "succeeded" if succeeded else "failed",
            "actual_rpo_seconds": actual_rpo,
            "actual_rto_seconds": actual_rto,
            "verifications": [
                {
                    "verification_code": code,
                    "outcome": "passed" if succeeded else "failed",
                    "evidence_sha256": supplied.get(code, fallback),
                    "checked_at": completed_at,
                }
                for code in codes
            ],
            "attempt_nonce": token_urlsafe(32),
        }

    def _step(
        self,
        connection: sqlite3.Connection,
        restore_id: str,
        sequence: int,
        step_code: str,
        outcome: str,
        evidence_sha256: str,
        occurred_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO local_restore_steps(
              restore_step_id,restore_id,sequence,step_code,outcome,evidence_sha256,occurred_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                "lrs_" + uuid4().hex,
                restore_id,
                sequence,
                step_code,
                outcome,
                evidence_sha256,
                occurred_at,
            ),
        )

    def _owned_backup(
        self,
        connection: sqlite3.Connection,
        backup_id: str,
        tenant_id: str,
        workspace_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT * FROM local_edge_backups
            WHERE backup_id=? AND deployment_id=? AND tenant_id=? AND workspace_id=?
            """,
            (backup_id, self._deployment_id, tenant_id, workspace_id),
        ).fetchone()
        if row is None:
            raise LocalBackupError("local_backup.not_found")
        return row

    def _restore_row(self, connection: sqlite3.Connection, restore_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM local_restore_journals WHERE restore_id=?", (restore_id,)
        ).fetchone()
        if row is None:
            raise LocalBackupError("local_restore.not_found")
        return row

    def _backup_summary(self, row: sqlite3.Row) -> LocalBackupSummary:
        return LocalBackupSummary(
            backup_id=str(row["backup_id"]),
            deployment_id=str(row["deployment_id"]),
            source_generation=int(row["source_generation"]),
            schema_head=str(row["schema_head"]),
            storage_kind=str(row["storage_kind"]),  # type: ignore[arg-type]
            status=str(row["status"]),  # type: ignore[arg-type]
            manifest_sha256=str(row["manifest_sha256"]),
            merkle_root=str(row["merkle_root"]),
            kms_key_id_sha256=str(row["kms_key_id_sha256"]),
            chunk_count=int(row["chunk_count"]),
            encrypted_size_bytes=int(row["encrypted_size_bytes"]),
            created_at=str(row["created_at"]),
            expires_at=str(row["expires_at"]),
            revision=int(row["revision"]),
        )

    def _restore_summary(self, row: sqlite3.Row) -> LocalRestoreSummary:
        return LocalRestoreSummary(
            restore_id=str(row["restore_id"]),
            backup_id=str(row["backup_id"]),
            deployment_id=str(row["deployment_id"]),
            target_schema_head=str(row["target_schema_head"]),
            status=str(row["status"]),  # type: ignore[arg-type]
            failure_code=row["failure_code"],
            revision=int(row["revision"]),
            created_at=str(row["created_at"]),
            completed_at=row["completed_at"],
            updated_at=str(row["updated_at"]),
        )

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)
            connection.commit()
        except sqlite3.Error:
            raise LocalBackupError("local_backup.unavailable") from None
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(self._database_path, timeout=5, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA secure_delete=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            return connection
        except sqlite3.Error:
            raise LocalBackupError("local_backup.unavailable") from None

    def _clock(self) -> datetime:
        try:
            value = self._now()
            if value.tzinfo is None:
                raise ValueError
            return value.astimezone(UTC)
        except Exception:
            raise LocalBackupError("local_backup.time_invalid") from None


def _owners(tenant_id: str, workspace_id: str, account_id: str) -> None:
    if any(
        type(value) is not str or _OWNER_ID.fullmatch(value) is None
        for value in (tenant_id, workspace_id, account_id)
    ):
        raise LocalBackupError("local_backup.request_invalid")


def _contract_code(code: str, operation: str) -> str:
    safe = code.removeprefix("edge_backup.")
    return f"local_{'backup' if operation == 'create' else 'restore'}.{safe}"


def _time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _strictly_after(now: datetime, previous: str) -> str:
    candidate = _time(now)
    if candidate > previous:
        return candidate
    prior = datetime.fromisoformat(previous.replace("Z", "+00:00"))
    return _time(prior + timedelta(milliseconds=1))


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _size_bucket(value: int) -> str:
    if value < 100 * 1024 * 1024:
        return "lt_100mb"
    if value < 1024 * 1024 * 1024:
        return "100mb_1gb"
    if value < 10 * 1024 * 1024 * 1024:
        return "1gb_10gb"
    return "gte_10gb"


def _b64(value: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _unb64(value: object) -> bytes:
    import base64

    if type(value) is not str or "=" in value:
        raise ValueError
    decoded = base64.b64decode(
        value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
    )
    if _b64(decoded) != value:
        raise ValueError
    return decoded


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _remove_directory(path: Path) -> None:
    if not path.exists():
        return
    for item in path.iterdir():
        if item.is_file() and not item.is_symlink():
            item.unlink()
        else:
            raise LocalBackupError("local_backup.cleanup_failed")
    path.rmdir()


__all__ = (
    "LocalBackupError",
    "LocalCloudReport",
    "LocalDrillContext",
    "LocalBackupSigningKeyStore",
    "LocalBackupStore",
    "LocalBackupSummary",
    "LocalRestoreSummary",
    "RestoreApplier",
)
