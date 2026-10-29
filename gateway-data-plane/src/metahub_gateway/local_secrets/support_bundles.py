"""Customer-approved local support-bundle generation and upload lifecycle."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

from metahub_shared.private_gateway import (
    SUPPORT_BUNDLE_COLLECTOR_CODES,
    EncryptedSupportBundle,
    PreparedSupportBundle,
    SupportBundleBuilder,
    SupportBundleManifest,
    ValidationContractError,
    encrypt_support_bundle,
    support_bundle_approval_proof_sha256,
    support_bundle_manifest_sha256,
)
from pydantic import BaseModel, ConfigDict, Field

type LocalSupportBundleStatus = Literal[
    "preview_ready", "uploading", "uploaded", "failed", "cancelled", "expired"
]
type LocalSupportBundleTerminalStatus = Literal["failed", "cancelled", "expired"]

_DEPLOYMENT_ID = re.compile(r"^edp_[0-9a-f]{32}$")
_OWNER_ID = re.compile(r"^[a-z0-9][a-z0-9_:-]{2,159}$")
_BUNDLE_ARTIFACT = re.compile(r"^(esb_[0-9a-f]{32})\.(?:zip|enc)$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS local_support_bundles(
  bundle_id TEXT PRIMARY KEY CHECK(length(bundle_id)=36 AND substr(bundle_id,1,4)='esb_'),
  deployment_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  requested_by_account_id TEXT NOT NULL,
  approved_by_account_id TEXT,
  status TEXT NOT NULL CHECK(status IN
    ('preview_ready','uploading','uploaded','failed','cancelled','expired')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  manifest_json TEXT NOT NULL,
  manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64),
  archive_path TEXT,
  encrypted_path TEXT,
  object_sha256 TEXT CHECK(object_sha256 IS NULL OR length(object_sha256)=64),
  object_size_bytes INTEGER CHECK(object_size_bytes IS NULL OR object_size_bytes>0),
  cloud_object_reference TEXT,
  failure_code TEXT,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  approved_at TEXT,
  uploaded_at TEXT,
  updated_at TEXT NOT NULL,
  CHECK((status='preview_ready' AND archive_path IS NOT NULL AND encrypted_path IS NULL
    AND approved_by_account_id IS NULL AND approved_at IS NULL)
    OR(status='uploading' AND archive_path IS NOT NULL AND encrypted_path IS NULL
      AND approved_by_account_id IS NOT NULL AND approved_at IS NOT NULL
      AND object_sha256 IS NULL AND object_size_bytes IS NULL
      AND cloud_object_reference IS NULL AND uploaded_at IS NULL AND failure_code IS NULL)
    OR(status='uploaded' AND archive_path IS NULL AND encrypted_path IS NULL
      AND object_sha256 IS NOT NULL AND object_size_bytes IS NOT NULL
      AND cloud_object_reference IS NOT NULL AND uploaded_at IS NOT NULL
      AND approved_by_account_id IS NOT NULL AND approved_at IS NOT NULL
      AND failure_code IS NULL)
    OR(status IN('failed','cancelled','expired') AND archive_path IS NULL
      AND encrypted_path IS NULL AND object_sha256 IS NULL
      AND object_size_bytes IS NULL AND cloud_object_reference IS NULL
      AND uploaded_at IS NULL
      AND ((status='failed' AND failure_code IS NOT NULL)
        OR(status IN('cancelled','expired') AND failure_code IS NULL))))
);
CREATE INDEX IF NOT EXISTS idx_local_support_bundle_owner
  ON local_support_bundles(tenant_id,workspace_id,created_at DESC,bundle_id);
CREATE TRIGGER IF NOT EXISTS local_support_bundle_identity_immutable
BEFORE UPDATE ON local_support_bundles FOR EACH ROW
WHEN NEW.bundle_id!=OLD.bundle_id OR NEW.deployment_id!=OLD.deployment_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.workspace_id!=OLD.workspace_id
  OR NEW.requested_by_account_id!=OLD.requested_by_account_id
  OR NEW.manifest_json!=OLD.manifest_json OR NEW.manifest_sha256!=OLD.manifest_sha256
  OR NEW.created_at!=OLD.created_at OR NEW.expires_at!=OLD.expires_at
  OR NEW.revision!=OLD.revision+1
  OR (OLD.status!='preview_ready' AND (
    NEW.approved_by_account_id IS NOT OLD.approved_by_account_id
    OR NEW.approved_at IS NOT OLD.approved_at))
  OR (OLD.status='preview_ready' AND NEW.status NOT IN('uploading','cancelled','expired'))
  OR (OLD.status='uploading' AND NEW.status NOT IN('uploaded','failed'))
  OR OLD.status IN('uploaded','failed','cancelled','expired')
BEGIN SELECT RAISE(ABORT,'local_support_bundle_transition_invalid'); END;
CREATE TRIGGER IF NOT EXISTS local_support_bundle_no_delete
BEFORE DELETE ON local_support_bundles
BEGIN SELECT RAISE(ABORT,'local_support_bundle_immutable'); END;
"""


class LocalSupportBundleError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class LocalSupportBundleSummary(_StrictModel):
    bundle_id: str = Field(pattern=r"^esb_[0-9a-f]{32}$")
    deployment_id: str = Field(pattern=r"^edp_[0-9a-f]{32}$")
    status: LocalSupportBundleStatus
    revision: int = Field(ge=1)
    manifest: SupportBundleManifest
    cloud_object_reference: str | None = None
    failure_code: str | None = None
    created_at: str
    expires_at: str
    approved_at: str | None = None
    uploaded_at: str | None = None


class SupportBundleUploader(Protocol):
    def __call__(
        self,
        bundle: EncryptedSupportBundle,
        *,
        tenant_id: str,
        workspace_id: str,
        approval_account_id: str,
        approval_proof_sha256: str,
        approved_at: str,
    ) -> str: ...


class LocalSupportBundleStore:
    """Persist preview consent separately from encrypted upload side effects."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        output_root: str | Path,
        deployment_id: str,
        encryption_secret: bytes,
        input_provider: Callable[[str, str, bytes], tuple[Mapping[str, object], Sequence[str]]],
        uploader: SupportBundleUploader,
        ttl_seconds: int = 86_400,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        raw_output_root = Path(output_root)
        self._database_path = Path(database_path).resolve()
        self._output_root = raw_output_root.resolve()
        self._deployment_id = deployment_id
        self._encryption_secret = bytes(encryption_secret)
        self._scanner_key = hashlib.sha256(
            b"metahub/support-bundle/scanner/v1\0" + self._encryption_secret
        ).digest()
        self._input_provider = input_provider
        self._uploader = uploader
        self._ttl_seconds = ttl_seconds
        self._now = now
        self._lock = threading.RLock()
        if (
            not 300 <= ttl_seconds <= 604_800
            or len(self._encryption_secret) < 32
            or _DEPLOYMENT_ID.fullmatch(deployment_id) is None
            or self._output_root in {self._database_path, self._database_path.parent}
            or raw_output_root.is_symlink()
        ):
            raise LocalSupportBundleError("local_support_bundle.configuration_invalid")
        self._output_root.mkdir(parents=True, exist_ok=True)
        if self._output_root.is_symlink() or not self._output_root.is_dir():
            raise LocalSupportBundleError("local_support_bundle.configuration_invalid")
        self._initialize()

    def preview(
        self,
        *,
        tenant_id: str,
        workspace_id: str,
        account_id: str,
        collector_codes: Sequence[str],
    ) -> LocalSupportBundleSummary:
        _validate_owners(tenant_id, workspace_id, account_id)
        now = self._clock()
        bundle_id = _id("esb_")
        try:
            values, known_hmacs = self._input_provider(tenant_id, workspace_id, self._scanner_key)
            if set(values) != set(SUPPORT_BUNDLE_COLLECTOR_CODES):
                raise LocalSupportBundleError("local_support_bundle.collection_failed")
            collectors = {
                code: (lambda value=values[code]: value) for code in SUPPORT_BUNDLE_COLLECTOR_CODES
            }
            builder = SupportBundleBuilder(
                self._output_root,
                collectors,
                scanner_hmac_key=self._scanner_key,
                known_secret_hmacs=known_hmacs,
                now=lambda: now,
            )
            prepared = builder.build(
                bundle_id=bundle_id,
                deployment_id=self._deployment_id,
                collector_codes=collector_codes,
                expires_at=now + timedelta(seconds=self._ttl_seconds),
            )
            manifest_json = _canonical_manifest(prepared.manifest)
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO local_support_bundles(
                      bundle_id,deployment_id,tenant_id,workspace_id,requested_by_account_id,
                      approved_by_account_id,status,revision,manifest_json,manifest_sha256,
                      archive_path,encrypted_path,object_sha256,object_size_bytes,
                      cloud_object_reference,failure_code,created_at,expires_at,approved_at,
                      uploaded_at,updated_at)
                    VALUES(?,?,?,?,?,NULL,'preview_ready',1,?,?,?,NULL,NULL,NULL,NULL,NULL,
                      ?,?,NULL,NULL,?)
                    """,
                    (
                        bundle_id,
                        self._deployment_id,
                        tenant_id,
                        workspace_id,
                        account_id,
                        manifest_json,
                        support_bundle_manifest_sha256(prepared.manifest),
                        str(prepared.archive_path),
                        prepared.manifest.created_at,
                        prepared.manifest.expires_at,
                        prepared.manifest.created_at,
                    ),
                )
                row = self._owned_row(connection, bundle_id, tenant_id, workspace_id)
                connection.commit()
                return self._summary(row)
            except Exception:
                prepared.archive_path.unlink(missing_ok=True)
                raise
            finally:
                connection.close()
        except LocalSupportBundleError:
            raise
        except ValidationContractError as exc:
            raise LocalSupportBundleError(_map_contract_error(exc.code)) from None
        except (OSError, sqlite3.Error, TypeError, ValueError):
            raise LocalSupportBundleError("local_support_bundle.unavailable") from None

    def approve(
        self,
        bundle_id: str,
        *,
        tenant_id: str,
        workspace_id: str,
        account_id: str,
        expected_revision: int,
    ) -> LocalSupportBundleSummary:
        _validate_owners(tenant_id, workspace_id, account_id)
        if type(expected_revision) is not int or expected_revision < 1:
            raise LocalSupportBundleError("local_support_bundle.request_invalid")
        now = self._clock()
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = self._owned_row(connection, bundle_id, tenant_id, workspace_id)
                if int(row["revision"]) != expected_revision:
                    raise LocalSupportBundleError("local_support_bundle.revision_conflict")
                if str(row["status"]) != "preview_ready":
                    raise LocalSupportBundleError("local_support_bundle.state_conflict")
                if _parse_time(str(row["expires_at"])) <= now:
                    self._terminal(connection, row, "expired", now=now)
                    connection.commit()
                    self._unlink_artifacts(bundle_id)
                    raise LocalSupportBundleError("local_support_bundle.expired")
                updated = connection.execute(
                    """
                    UPDATE local_support_bundles
                    SET status='uploading',approved_by_account_id=?,approved_at=?,
                      revision=revision+1,updated_at=?
                    WHERE bundle_id=? AND revision=? AND status='preview_ready'
                    """,
                    (account_id, _time(now), _time(now), bundle_id, expected_revision),
                ).rowcount
                if updated != 1:
                    raise LocalSupportBundleError("local_support_bundle.revision_conflict")
                row = self._owned_row(connection, bundle_id, tenant_id, workspace_id)
                connection.commit()
            except LocalSupportBundleError:
                connection.rollback()
                raise
            except sqlite3.Error:
                connection.rollback()
                raise LocalSupportBundleError("local_support_bundle.unavailable") from None
            finally:
                connection.close()

            archive = Path(str(row["archive_path"]))
            self._validate_archive(row, archive)
            encrypted_path = self._output_root / f"{bundle_id}.enc"
            manifest = self._manifest(row)
            uploading_revision = expected_revision + 1
            approved_at = str(row["approved_at"])
            approval_proof_sha256 = support_bundle_approval_proof_sha256(
                bundle_id=bundle_id,
                deployment_id=self._deployment_id,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                manifest_sha256=str(row["manifest_sha256"]),
                approved_by_account_id=account_id,
                approved_at=approved_at,
                local_revision=uploading_revision,
            )
            try:
                encrypted = encrypt_support_bundle(
                    prepared=PreparedSupportBundle(
                        manifest=manifest,
                        archive_path=archive,
                    ),
                    output_path=encrypted_path,
                    encryption_secret=self._encryption_secret,
                )
                reference = self._uploader(
                    encrypted,
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    approval_account_id=account_id,
                    approval_proof_sha256=approval_proof_sha256,
                    approved_at=approved_at,
                )
                if not _valid_reference(reference):
                    raise LocalSupportBundleError("local_support_bundle.upload_failed")
            except LocalSupportBundleError as exc:
                self._fail_upload_safely(
                    bundle_id,
                    tenant_id,
                    workspace_id,
                    failure_code=_failure_code(exc.code),
                    now=now,
                )
                raise
            except ValidationContractError as exc:
                code = _map_contract_error(exc.code)
                self._fail_upload_safely(
                    bundle_id,
                    tenant_id,
                    workspace_id,
                    failure_code=_failure_code(code),
                    now=now,
                )
                raise LocalSupportBundleError(code) from None
            except Exception:
                self._fail_upload_safely(
                    bundle_id,
                    tenant_id,
                    workspace_id,
                    failure_code="upload_failed",
                    now=now,
                )
                raise LocalSupportBundleError("local_support_bundle.upload_failed") from None
            finally:
                encrypted_path.unlink(missing_ok=True)

            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                current = self._owned_row(connection, bundle_id, tenant_id, workspace_id)
                if (
                    str(current["status"]) != "uploading"
                    or int(current["revision"]) != uploading_revision
                ):
                    raise LocalSupportBundleError("local_support_bundle.state_conflict")
                updated = connection.execute(
                    """
                    UPDATE local_support_bundles
                    SET status='uploaded',archive_path=NULL,encrypted_path=NULL,
                      object_sha256=?,object_size_bytes=?,cloud_object_reference=?,
                      uploaded_at=?,revision=revision+1,updated_at=?
                    WHERE bundle_id=? AND status='uploading' AND revision=?
                    """,
                    (
                        encrypted.object_sha256,
                        encrypted.object_size_bytes,
                        reference,
                        _time(now),
                        _time(now),
                        bundle_id,
                        uploading_revision,
                    ),
                ).rowcount
                if updated != 1:
                    raise LocalSupportBundleError("local_support_bundle.state_conflict")
                result = self._summary(
                    self._owned_row(connection, bundle_id, tenant_id, workspace_id)
                )
                connection.commit()
            except LocalSupportBundleError:
                connection.rollback()
                raise
            except sqlite3.Error:
                connection.rollback()
                raise LocalSupportBundleError("local_support_bundle.unavailable") from None
            finally:
                connection.close()
            try:
                archive.unlink(missing_ok=True)
            except OSError:
                raise LocalSupportBundleError("local_support_bundle.cleanup_failed") from None
            return result

    def cancel(
        self,
        bundle_id: str,
        *,
        tenant_id: str,
        workspace_id: str,
        expected_revision: int,
    ) -> LocalSupportBundleSummary:
        _validate_owners(tenant_id, workspace_id)
        if type(expected_revision) is not int or expected_revision < 1:
            raise LocalSupportBundleError("local_support_bundle.request_invalid")
        now = self._clock()
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = self._owned_row(connection, bundle_id, tenant_id, workspace_id)
                if int(row["revision"]) != expected_revision:
                    raise LocalSupportBundleError("local_support_bundle.revision_conflict")
                if str(row["status"]) != "preview_ready":
                    raise LocalSupportBundleError("local_support_bundle.state_conflict")
                self._terminal(connection, row, "cancelled", now=now)
                result = self._summary(
                    self._owned_row(connection, bundle_id, tenant_id, workspace_id)
                )
                connection.commit()
                self._unlink_artifacts(bundle_id)
                return result
            except LocalSupportBundleError:
                connection.rollback()
                raise
            except sqlite3.Error:
                connection.rollback()
                raise LocalSupportBundleError("local_support_bundle.unavailable") from None
            finally:
                connection.close()

    def _fail_upload(
        self,
        bundle_id: str,
        tenant_id: str,
        workspace_id: str,
        *,
        failure_code: str,
        now: datetime,
    ) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._owned_row(connection, bundle_id, tenant_id, workspace_id)
            if str(row["status"]) == "uploading":
                self._terminal(
                    connection,
                    row,
                    "failed",
                    failure_code=failure_code,
                    now=now,
                )
                connection.commit()
                self._unlink_artifacts(bundle_id)
            else:
                connection.rollback()
        finally:
            connection.close()

    def _fail_upload_safely(
        self,
        bundle_id: str,
        tenant_id: str,
        workspace_id: str,
        *,
        failure_code: str,
        now: datetime,
    ) -> None:
        try:
            self._fail_upload(
                bundle_id,
                tenant_id,
                workspace_id,
                failure_code=failure_code,
                now=now,
            )
        except (LocalSupportBundleError, OSError, sqlite3.Error):
            # Preserve the fixed upload error. Startup recovery owns interrupted cleanup.
            return

    @staticmethod
    def _terminal(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        status: LocalSupportBundleTerminalStatus,
        *,
        failure_code: str | None = None,
        now: datetime,
    ) -> None:
        connection.execute(
            """
            UPDATE local_support_bundles
            SET status=?,archive_path=NULL,encrypted_path=NULL,failure_code=?,
              revision=revision+1,updated_at=?
            WHERE bundle_id=? AND revision=?
            """,
            (status, failure_code, _time(now), row["bundle_id"], row["revision"]),
        )

    def _owned_row(
        self,
        connection: sqlite3.Connection,
        bundle_id: str,
        tenant_id: str,
        workspace_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT * FROM local_support_bundles
            WHERE bundle_id=? AND deployment_id=? AND tenant_id=? AND workspace_id=?
            """,
            (bundle_id, self._deployment_id, tenant_id, workspace_id),
        ).fetchone()
        if row is None:
            raise LocalSupportBundleError("local_support_bundle.not_found")
        return row

    def _manifest(self, row: sqlite3.Row) -> SupportBundleManifest:
        try:
            manifest = SupportBundleManifest.model_validate_json(str(row["manifest_json"]))
        except (TypeError, ValueError):
            raise LocalSupportBundleError("local_support_bundle.integrity_failed") from None
        if (
            manifest.bundle_id != str(row["bundle_id"])
            or manifest.deployment_id != str(row["deployment_id"])
            or manifest.created_at != str(row["created_at"])
            or manifest.expires_at != str(row["expires_at"])
            or support_bundle_manifest_sha256(manifest) != str(row["manifest_sha256"])
        ):
            raise LocalSupportBundleError("local_support_bundle.integrity_failed")
        return manifest

    def _summary(self, row: sqlite3.Row) -> LocalSupportBundleSummary:
        return LocalSupportBundleSummary(
            bundle_id=str(row["bundle_id"]),
            deployment_id=str(row["deployment_id"]),
            status=str(row["status"]),
            revision=int(row["revision"]),
            manifest=self._manifest(row),
            cloud_object_reference=row["cloud_object_reference"],
            failure_code=row["failure_code"],
            created_at=str(row["created_at"]),
            expires_at=str(row["expires_at"]),
            approved_at=row["approved_at"],
            uploaded_at=row["uploaded_at"],
        )

    def _validate_archive(self, row: sqlite3.Row, archive: Path) -> None:
        expected = self._output_root / f"{row['bundle_id']}.zip"
        try:
            if archive.resolve() != expected or archive.is_symlink() or not archive.is_file():
                raise LocalSupportBundleError("local_support_bundle.integrity_failed")
        except OSError:
            raise LocalSupportBundleError("local_support_bundle.integrity_failed") from None

    def _unlink_artifacts(self, bundle_id: str) -> None:
        for suffix in ("zip", "enc"):
            path = self._output_root / f"{bundle_id}.{suffix}"
            try:
                if path.is_symlink() or path.is_file():
                    path.unlink(missing_ok=True)
            except OSError:
                raise LocalSupportBundleError("local_support_bundle.cleanup_failed") from None

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(_SCHEMA)
            now = self._clock()
            rows = connection.execute(
                "SELECT * FROM local_support_bundles WHERE status IN('preview_ready','uploading')"
            ).fetchall()
            cleanup_ids: list[str] = []
            active_preview_ids: set[str] = set()
            for row in rows:
                next_status = (
                    "expired"
                    if _parse_time(str(row["expires_at"])) <= now
                    else ("failed" if str(row["status"]) == "uploading" else None)
                )
                if next_status is not None:
                    self._terminal(
                        connection,
                        row,
                        next_status,
                        failure_code=("interrupted" if next_status == "failed" else None),
                        now=now,
                    )
                    cleanup_ids.append(str(row["bundle_id"]))
                else:
                    self._manifest(row)
                    self._validate_archive(row, Path(str(row["archive_path"])))
                    active_preview_ids.add(str(row["bundle_id"]))
            connection.commit()
            for bundle_id in cleanup_ids:
                self._unlink_artifacts(bundle_id)
            for path in self._output_root.iterdir():
                artifact = _BUNDLE_ARTIFACT.fullmatch(path.name)
                if path.name.startswith(".support-bundle-"):
                    if path.is_symlink():
                        path.unlink(missing_ok=True)
                    elif path.is_dir():
                        shutil.rmtree(path, ignore_errors=True)
                elif path.name.endswith(".tmp") and path.is_file():
                    path.unlink(missing_ok=True)
                elif artifact is not None and artifact.group(1) not in active_preview_ids:
                    if path.is_symlink() or path.is_file():
                        path.unlink(missing_ok=True)
        except (OSError, sqlite3.Error, ValueError):
            raise LocalSupportBundleError("local_support_bundle.unavailable") from None
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
            raise LocalSupportBundleError("local_support_bundle.unavailable") from None

    def _clock(self) -> datetime:
        try:
            value = self._now()
            if value.tzinfo is None:
                raise ValueError
            return value.astimezone(UTC)
        except Exception:
            raise LocalSupportBundleError("local_support_bundle.unavailable") from None


def _canonical_manifest(manifest: SupportBundleManifest) -> str:
    return json.dumps(
        manifest.model_dump(mode="json"),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _map_contract_error(code: str) -> str:
    if code == "support_bundle.sensitive_detected":
        return "local_support_bundle.sensitive_detected"
    if code == "support_bundle.size_exceeded":
        return "local_support_bundle.size_exceeded"
    if code in {"support_bundle.collector_invalid", "support_bundle.request_invalid"}:
        return "local_support_bundle.request_invalid"
    if code in {"support_bundle.integrity_failed", "support_bundle.encryption_failed"}:
        return "local_support_bundle.integrity_failed"
    return "local_support_bundle.generation_failed"


def _failure_code(code: str) -> str:
    prefix = "local_support_bundle."
    return code.removeprefix(prefix) if code.startswith(prefix) else "upload_failed"


def _validate_owners(*values: str) -> None:
    if any(type(value) is not str or _OWNER_ID.fullmatch(value) is None for value in values):
        raise LocalSupportBundleError("local_support_bundle.request_invalid")


def _valid_reference(value: object) -> bool:
    return (
        type(value) is str
        and 4 <= len(value) <= 160
        and value.startswith("esb_")
        and all(character.isalnum() or character in "._:-/" for character in value)
    )


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


def _time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or _time(parsed) != value:
        raise ValueError("time")
    return parsed.astimezone(UTC)


__all__ = (
    "LocalSupportBundleError",
    "LocalSupportBundleStore",
    "LocalSupportBundleSummary",
    "LocalSupportBundleStatus",
    "SupportBundleUploader",
)
