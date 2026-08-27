"""Encrypted support-bundle ingestion, metadata projection, and authorized download."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from metahub_shared.db.errors import DbError
from metahub_shared.private_gateway import (
    MAX_SUPPORT_BUNDLE_BYTES,
    SupportBundleUploadMetadata,
    SupportBundleUploadReceipt,
    ValidationContractError,
    support_bundle_approval_proof_sha256,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.platform_access.service import _authenticate_support

from .desired_state_service import _authenticated_agent
from .models import (
    EdgeSupportBundleEntrySummary,
    EdgeSupportBundleResponse,
    EdgeSupportBundleSummary,
)
from .service import _connect, _datetime, _db_time, _id, _identifier, _rollback, _utcnow
from .validation_service import _insert_event, _require_diagnostics_capability

_BUNDLE_ID = re.compile(r"^esb_[0-9a-f]{32}$")
_OBJECT_REFERENCE = re.compile(
    r"^edge-support-bundles/esb_[0-9a-f]{32}\.[0-9a-f]{16}\.enc$"
)
_CLOCK_SKEW = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class EdgeSupportBundleUploadAuthorization:
    deployment_id: str
    workspace_id: str
    tenant_id: str
    edge_session_id: str
    identity_generation: int
    revocation_generation: int


@dataclass(frozen=True, slots=True, repr=False)
class EdgeSupportBundleDownload:
    support_bundle_id: str
    path: Path
    object_size_bytes: int
    temporary: bool

    def cleanup(self) -> None:
        if self.temporary:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                return


class _StorageError(RuntimeError):
    pass


class _SupportBundleObjectStore:
    def __init__(self, config: AppConfig) -> None:
        candidate = Path(config.edge_support_bundle_root)
        try:
            if candidate.exists() and candidate.is_symlink():
                raise OSError
            candidate.mkdir(parents=True, exist_ok=True)
            self._root = candidate.resolve(strict=True)
            self._incoming = self._root / ".incoming"
            self._incoming.mkdir(parents=True, exist_ok=True)
            if self._incoming.is_symlink():
                raise OSError
        except OSError:
            raise _StorageError from None
        self._config = config
        self._s3 = config.environment == "prod"

    def staging_path(self) -> Path:
        try:
            handle = tempfile.NamedTemporaryFile(
                prefix="support-upload-",
                suffix=".part",
                dir=self._incoming,
                delete=False,
            )
            path = Path(handle.name)
            handle.close()
            if path.resolve(strict=True).parent != self._incoming or path.is_symlink():
                raise OSError
            return path
        except OSError:
            raise _StorageError from None

    def publish(
        self,
        staged: Path,
        *,
        object_reference: str,
        object_sha256: str,
        object_size_bytes: int,
        manifest_sha256: str,
    ) -> None:
        self._validate_staged(staged, object_sha256, object_size_bytes)
        if self._s3:
            self._publish_s3(
                staged,
                object_reference=object_reference,
                object_sha256=object_sha256,
                object_size_bytes=object_size_bytes,
                manifest_sha256=manifest_sha256,
            )
            return
        target = self._local_target(object_reference, create_parent=True)
        try:
            if target.exists():
                self._verify_file(target, object_sha256, object_size_bytes)
                return
            os.link(staged, target)
            self._verify_file(target, object_sha256, object_size_bytes)
        except FileExistsError:
            self._verify_file(target, object_sha256, object_size_bytes)
        except _StorageError:
            raise
        except OSError:
            raise _StorageError from None

    def materialize(
        self,
        *,
        object_reference: str,
        object_sha256: str,
        object_size_bytes: int,
    ) -> tuple[Path, bool]:
        if not self._s3:
            target = self._local_target(object_reference, create_parent=False)
            self._verify_file(target, object_sha256, object_size_bytes)
            return target, False
        staged = self.staging_path()
        client = None
        body = None
        try:
            client = self._s3_client()
            result = client.get_object(
                Bucket=self._bucket(), Key=object_reference, ChecksumMode="ENABLED"
            )
            body = result["Body"]
            digest = hashlib.sha256()
            size = 0
            with staged.open("wb") as writer:
                while chunk := body.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_SUPPORT_BUNDLE_BYTES + 64:
                        raise _StorageError
                    digest.update(chunk)
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            if size != object_size_bytes or not hmac.compare_digest(
                digest.hexdigest(), object_sha256
            ):
                raise _StorageError
            return staged, True
        except _StorageError:
            staged.unlink(missing_ok=True)
            raise
        except Exception:
            staged.unlink(missing_ok=True)
            raise _StorageError from None
        finally:
            if body is not None:
                try:
                    body.close()
                except Exception:
                    pass
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def delete(self, object_reference: str) -> None:
        if self._s3:
            client = None
            try:
                client = self._s3_client()
                client.delete_object(Bucket=self._bucket(), Key=object_reference)
                return
            except Exception:
                raise _StorageError from None
            finally:
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass
        target = self._local_target(object_reference, create_parent=False)
        try:
            if target.is_symlink():
                raise _StorageError
            target.unlink(missing_ok=True)
        except _StorageError:
            raise
        except OSError:
            raise _StorageError from None

    def _publish_s3(
        self,
        staged: Path,
        *,
        object_reference: str,
        object_sha256: str,
        object_size_bytes: int,
        manifest_sha256: str,
    ) -> None:
        client = None
        try:
            client = self._s3_client()
            try:
                head = client.head_object(Bucket=self._bucket(), Key=object_reference)
            except Exception:
                head = None
            if head is None:
                with staged.open("rb") as body:
                    client.put_object(
                        Bucket=self._bucket(),
                        Key=object_reference,
                        Body=body,
                        ContentLength=object_size_bytes,
                        ContentType="application/vnd.metahub.edge-support-bundle-encrypted",
                        ChecksumSHA256=base64.b64encode(bytes.fromhex(object_sha256)).decode("ascii"),
                        Metadata={
                            "manifest-sha256": manifest_sha256,
                            "object-sha256": object_sha256,
                            "schema-version": "1",
                        },
                    )
                head = client.head_object(Bucket=self._bucket(), Key=object_reference)
            metadata = head.get("Metadata") or {}
            if (
                int(head.get("ContentLength", -1)) != object_size_bytes
                or metadata.get("manifest-sha256") != manifest_sha256
                or metadata.get("object-sha256") != object_sha256
            ):
                raise _StorageError
        except _StorageError:
            raise
        except Exception:
            raise _StorageError from None
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def _s3_client(self):
        try:
            import boto3
            from botocore.config import Config

            return boto3.client(
                "s3",
                endpoint_url=self._config.object_store_endpoint_url,
                region_name=self._config.object_store_region,
                aws_access_key_id=self._config.object_store_access_key_id,
                aws_secret_access_key=self._config.object_store_secret_access_key,
                config=Config(
                    connect_timeout=self._config.infrastructure_probe_timeout_seconds,
                    read_timeout=max(10, self._config.infrastructure_probe_timeout_seconds),
                    retries={"max_attempts": 2, "mode": "standard"},
                    s3={
                        "addressing_style": (
                            "path" if self._config.object_store_force_path_style else "virtual"
                        )
                    },
                ),
            )
        except Exception:
            raise _StorageError from None

    def _bucket(self) -> str:
        bucket = self._config.object_store_bucket
        if not bucket:
            raise _StorageError
        return bucket

    def _validate_staged(self, path: Path, expected_sha256: str, expected_size: int) -> None:
        try:
            if path.resolve(strict=True).parent != self._incoming or path.is_symlink():
                raise _StorageError
        except OSError:
            raise _StorageError from None
        self._verify_file(path, expected_sha256, expected_size)

    @staticmethod
    def _verify_file(path: Path, expected_sha256: str, expected_size: int) -> None:
        digest = hashlib.sha256()
        size = 0
        try:
            if path.is_symlink() or not path.is_file():
                raise _StorageError
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_SUPPORT_BUNDLE_BYTES + 64:
                        raise _StorageError
                    digest.update(chunk)
        except _StorageError:
            raise
        except OSError:
            raise _StorageError from None
        if size != expected_size or not hmac.compare_digest(digest.hexdigest(), expected_sha256):
            raise _StorageError

    def _local_target(self, object_reference: str, *, create_parent: bool) -> Path:
        if _OBJECT_REFERENCE.fullmatch(object_reference) is None:
            raise _StorageError
        target = self._root.joinpath(*object_reference.split("/"))
        try:
            parent = target.parent
            if create_parent:
                parent.mkdir(parents=True, exist_ok=True)
            if parent.is_symlink():
                raise _StorageError
            resolved = parent.resolve(strict=True)
            if self._root != resolved and self._root not in resolved.parents:
                raise _StorageError
            return resolved / target.name
        except _StorageError:
            raise
        except OSError:
            raise _StorageError from None


def prepare_support_bundle_staging(config: AppConfig) -> Path:
    try:
        return _SupportBundleObjectStore(config).staging_path()
    except _StorageError:
        raise ApiError("edge.support_bundle_storage_unavailable") from None


def authorize_agent_support_bundle_upload(
    config: AppConfig,
    deployment_id: str,
    *,
    verified_fingerprint: str,
    edge_session_token: str,
) -> EdgeSupportBundleUploadAuthorization:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        agent = _authenticated_agent(
            connection,
            config,
            deployment_id=deployment_id,
            verified_fingerprint=verified_fingerprint,
            edge_session_token=edge_session_token,
        )
        _require_diagnostics_capability(agent)
        authorization = _upload_authorization(agent, deployment_id)
        connection.commit()
        return authorization
    except ApiError:
        _rollback(connection)
        raise
    except (DbError, sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge.unavailable") from None
    finally:
        connection.close()


def store_agent_support_bundle(
    config: AppConfig,
    authorization: EdgeSupportBundleUploadAuthorization,
    metadata: SupportBundleUploadMetadata,
    staged_path: Path,
    *,
    verified_fingerprint: str,
    edge_session_token: str,
    request_id: str,
    trace_id: str,
) -> SupportBundleUploadReceipt:
    now = _utcnow()
    _validate_upload(
        metadata,
        authorization,
        now=now,
        ttl_seconds=config.edge_support_bundle_ttl_seconds,
    )
    entry_summary_json = _entry_summary_json(metadata)
    object_reference = _object_reference(metadata)
    store = _SupportBundleObjectStore(config)
    connection = _connect(config)
    published = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        agent = _authenticated_agent(
            connection,
            config,
            deployment_id=authorization.deployment_id,
            verified_fingerprint=verified_fingerprint,
            edge_session_token=edge_session_token,
        )
        _require_diagnostics_capability(agent)
        current = _upload_authorization(agent, authorization.deployment_id)
        if current != authorization:
            raise ApiError("edge.authentication_failed")
        existing = connection.execute(
            "SELECT * FROM edge_support_bundles WHERE support_bundle_id=?",
            (metadata.manifest.bundle_id,),
        ).fetchone()
        if existing is not None:
            if not _same_upload(
                existing,
                authorization,
                metadata,
                entry_summary_json=entry_summary_json,
                object_reference=object_reference,
            ):
                raise ApiError("edge.support_bundle_conflict")
            store.publish(
                staged_path,
                object_reference=object_reference,
                object_sha256=metadata.object_sha256,
                object_size_bytes=metadata.object_size_bytes,
                manifest_sha256=metadata.manifest_sha256,
            )
            receipt = _upload_receipt(
                existing, replay=True, request_id=request_id, trace_id=trace_id
            )
            connection.commit()
            return receipt
        store.publish(
            staged_path,
            object_reference=object_reference,
            object_sha256=metadata.object_sha256,
            object_size_bytes=metadata.object_size_bytes,
            manifest_sha256=metadata.manifest_sha256,
        )
        published = True
        stamp = _db_time(now)
        connection.execute(
            """
            INSERT INTO edge_support_bundles(
              support_bundle_id,workspace_id,tenant_id,edge_deployment_id,status,revision,
              manifest_sha256,entry_summary_json,scan_version,scan_outcome,
              archive_size_bytes,object_sha256,object_size_bytes,object_reference,
              approved_by_account_id,approval_proof_sha256,source_edge_session_id,
              source_identity_generation,source_revocation_generation,approved_at,
              expires_at,created_at,updated_at)
            VALUES(?,?,?,?,'ready',1,?,?,?,'clean',?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                metadata.manifest.bundle_id,
                authorization.workspace_id,
                authorization.tenant_id,
                authorization.deployment_id,
                metadata.manifest_sha256,
                entry_summary_json,
                metadata.manifest.scan_version,
                metadata.manifest.archive_size_bytes,
                metadata.object_sha256,
                metadata.object_size_bytes,
                object_reference,
                metadata.approved_by_account_id,
                metadata.approval_proof_sha256,
                authorization.edge_session_id,
                authorization.identity_generation,
                authorization.revocation_generation,
                metadata.approved_at,
                metadata.manifest.expires_at,
                stamp,
                stamp,
            ),
        )
        owner = connection.execute(
            "SELECT * FROM edge_deployments WHERE edge_deployment_id=?",
            (authorization.deployment_id,),
        ).fetchone()
        if owner is None:
            raise ApiError("edge.authentication_failed")
        _insert_event(
            connection,
            owner,
            event_type="edge.support_bundle_ready",
            object_type="support_bundle",
            object_id=metadata.manifest.bundle_id,
            outcome="ready",
            summary={
                "archive_size_bytes": metadata.manifest.archive_size_bytes,
                "entry_count": len(metadata.manifest.entries),
                "object_size_bytes": metadata.object_size_bytes,
                "scan_outcome": "clean",
            },
            request_id=request_id,
            trace_id=trace_id,
            now=stamp,
            domains=("workspace", "security"),
        )
        row = connection.execute(
            "SELECT * FROM edge_support_bundles WHERE support_bundle_id=?",
            (metadata.manifest.bundle_id,),
        ).fetchone()
        receipt = _upload_receipt(
            row, replay=False, request_id=request_id, trace_id=trace_id
        )
        connection.commit()
        return receipt
    except ApiError:
        remove_published = published and connection.in_transaction
        _rollback(connection)
        if remove_published:
            _delete_object_quietly(store, object_reference)
        raise
    except _StorageError:
        remove_published = published and connection.in_transaction
        _rollback(connection)
        if remove_published:
            _delete_object_quietly(store, object_reference)
        raise ApiError("edge.support_bundle_storage_unavailable") from None
    except (DbError, sqlite3.Error, KeyError, TypeError, ValueError):
        remove_published = published and connection.in_transaction
        _rollback(connection)
        if remove_published:
            _delete_object_quietly(store, object_reference)
        raise ApiError(
            "edge.support_bundle_storage_unavailable" if published else "edge.unavailable"
        ) from None
    finally:
        connection.close()


def get_workspace_support_bundle(
    config: AppConfig,
    bundle_id: str,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> EdgeSupportBundleResponse:
    if _BUNDLE_ID.fullmatch(bundle_id) is None:
        raise ApiError("edge.support_bundle_not_found")
    connection = _connect(config)
    expired_reference: str | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        from .service import _workspace_context

        context = _workspace_context(
            connection,
            config,
            session_token=session_token,
            csrf_token=None,
            permission="edge.support_bundle.read",
        )
        row = _owned_bundle(connection, context.workspace_id, context.tenant_id, bundle_id)
        if str(row["status"]) == "ready" and _datetime(row["expires_at"]) <= _utcnow():
            row = _expire_bundle(connection, row)
            expired_reference = str(row["object_reference"])
        response = EdgeSupportBundleResponse(
            bundle=_bundle_summary(row),
            request_id=_identifier(request_id),
            trace_id=_identifier(trace_id),
        )
        connection.commit()
    except ApiError:
        _rollback(connection)
        raise
    except (DbError, sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge.unavailable") from None
    finally:
        connection.close()
    if expired_reference is not None:
        try:
            _SupportBundleObjectStore(config).delete(expired_reference)
        except _StorageError:
            pass
        else:
            _mark_bundle_deleted_quietly(config, bundle_id, expired_reference)
    return response


def resolve_support_bundle_download(
    config: AppConfig,
    bundle_id: str,
    *,
    support_session_token: str,
    request_id: str,
    trace_id: str,
) -> EdgeSupportBundleDownload:
    if _BUNDLE_ID.fullmatch(bundle_id) is None:
        raise ApiError("edge.support_bundle_not_found")
    connection = _connect(config)
    download: EdgeSupportBundleDownload | None = None
    expired_reference: str | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        support = _authenticate_support(connection, config, support_session_token)
        if support["source_kind"] != "grant" or support["scope_code"] != (
            "workspace.diagnostics.read"
        ):
            raise ApiError("support.scope_denied")
        grant = connection.execute(
            "SELECT * FROM support_grants WHERE grant_id=?",
            (support["source_id"],),
        ).fetchone()
        if grant is None or grant["status"] != "active":
            raise ApiError("support.session_invalid")
        row = _owned_bundle(
            connection, str(support["workspace_id"]), str(support["tenant_id"]), bundle_id
        )
        if str(row["status"]) != "ready" or _datetime(row["expires_at"]) <= _utcnow():
            if str(row["status"]) == "ready":
                row = _expire_bundle(connection, row)
                expired_reference = str(row["object_reference"])
            connection.commit()
            raise ApiError("edge.support_bundle_expired")
        store = _SupportBundleObjectStore(config)
        path, temporary = store.materialize(
            object_reference=str(row["object_reference"]),
            object_sha256=str(row["object_sha256"]),
            object_size_bytes=int(row["object_size_bytes"]),
        )
        download = EdgeSupportBundleDownload(
            support_bundle_id=bundle_id,
            path=path,
            object_size_bytes=int(row["object_size_bytes"]),
            temporary=temporary,
        )
        authorized_until = min(
            _datetime(support["absolute_expires_at"]),
            _datetime(grant["expires_at"]),
            _datetime(row["expires_at"]),
        )
        connection.execute(
            """
            INSERT INTO edge_support_bundle_download_audits(
              support_bundle_download_id,workspace_id,tenant_id,support_bundle_id,
              support_grant_id,support_session_id,operator_id,grant_operator_revision,
              grant_workspace_revision,authorized_until,request_id,trace_id,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                _id("esd_"),
                row["workspace_id"],
                row["tenant_id"],
                bundle_id,
                grant["grant_id"],
                support["support_session_id"],
                support["operator_id"],
                grant["operator_revision"],
                grant["workspace_revision"],
                _db_time(authorized_until),
                _identifier(request_id),
                _identifier(trace_id),
                _db_time(_utcnow()),
            ),
        )
        connection.commit()
        return download
    except ApiError as exc:
        if connection.in_transaction:
            if exc.code in {"support.session_expired", "support.session_invalid"}:
                connection.commit()
            else:
                connection.rollback()
        if download is not None:
            download.cleanup()
        if expired_reference is not None:
            try:
                _SupportBundleObjectStore(config).delete(expired_reference)
            except _StorageError:
                pass
            else:
                _mark_bundle_deleted_quietly(config, bundle_id, expired_reference)
        raise
    except _StorageError:
        _rollback(connection)
        if download is not None:
            download.cleanup()
        raise ApiError("edge.support_bundle_integrity_failed") from None
    except (DbError, sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        if download is not None:
            download.cleanup()
        raise ApiError("edge.unavailable") from None
    finally:
        connection.close()


def reap_expired_support_bundles(
    config: AppConfig,
    *,
    now: datetime | None = None,
    limit: int = 64,
) -> int:
    """Expire a bounded batch, delete ciphertext, and converge to the deleted terminal state."""

    if type(limit) is not int or not 1 <= limit <= 256:
        raise ApiError("edge.support_bundle_storage_unavailable")
    current_value = now or _utcnow()
    if type(current_value) is not datetime or current_value.tzinfo is None:
        raise ApiError("edge.support_bundle_storage_unavailable")
    current = current_value.astimezone(UTC)
    connection = _connect(config)
    candidates: tuple[tuple[str, str], ...] = ()
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            """
            SELECT support_bundle_id,object_reference,status FROM edge_support_bundles
            WHERE status='expired' OR(status='ready' AND expires_at<=?)
            ORDER BY expires_at,support_bundle_id LIMIT ?
            """,
            (_db_time(current), limit),
        ).fetchall()
        for row in rows:
            if str(row["status"]) == "ready":
                changed = connection.execute(
                    """
                    UPDATE edge_support_bundles
                    SET status='expired',revision=revision+1,updated_at=?
                    WHERE support_bundle_id=? AND status='ready' AND expires_at<=?
                    """,
                    (_db_time(current), row["support_bundle_id"], _db_time(current)),
                ).rowcount
                if changed != 1:
                    raise ApiError("edge.support_bundle_storage_unavailable")
        candidates = tuple(
            (str(row["support_bundle_id"]), str(row["object_reference"]))
            for row in rows
        )
        connection.commit()
    except ApiError:
        _rollback(connection)
        raise
    except (DbError, sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge.support_bundle_storage_unavailable") from None
    finally:
        connection.close()

    try:
        store = _SupportBundleObjectStore(config)
    except _StorageError:
        raise ApiError("edge.support_bundle_storage_unavailable") from None
    deleted = 0
    for bundle_id, object_reference in candidates:
        try:
            store.delete(object_reference)
        except _StorageError:
            continue
        if _mark_bundle_deleted(config, bundle_id, object_reference, now=current):
            deleted += 1
    return deleted


def _validate_upload(
    metadata: SupportBundleUploadMetadata,
    authorization: EdgeSupportBundleUploadAuthorization,
    *,
    now: datetime,
    ttl_seconds: int,
) -> None:
    manifest = metadata.manifest
    if manifest.deployment_id != authorization.deployment_id:
        raise ApiError("edge.support_bundle_invalid")
    try:
        created_at = _datetime(manifest.created_at)
        approved_at = _datetime(metadata.approved_at)
        expires_at = _datetime(manifest.expires_at)
        expected_proof = support_bundle_approval_proof_sha256(
            bundle_id=manifest.bundle_id,
            deployment_id=authorization.deployment_id,
            tenant_id=authorization.tenant_id,
            workspace_id=authorization.workspace_id,
            manifest_sha256=metadata.manifest_sha256,
            approved_by_account_id=metadata.approved_by_account_id,
            approved_at=metadata.approved_at,
            local_revision=2,
        )
    except (TypeError, ValueError, ValidationContractError):
        raise ApiError("edge.support_bundle_invalid") from None
    if (
        approved_at > now + _CLOCK_SKEW
        or expires_at <= now
        or expires_at - created_at > timedelta(seconds=ttl_seconds)
    ):
        raise ApiError("edge.support_bundle_invalid")
    if not hmac.compare_digest(expected_proof, metadata.approval_proof_sha256):
        raise ApiError("edge.support_bundle_invalid")


def _delete_object_quietly(
    store: _SupportBundleObjectStore, object_reference: str
) -> None:
    try:
        store.delete(object_reference)
    except _StorageError:
        pass


def _upload_authorization(
    agent: sqlite3.Row | dict[str, Any], deployment_id: str
) -> EdgeSupportBundleUploadAuthorization:
    return EdgeSupportBundleUploadAuthorization(
        deployment_id=deployment_id,
        workspace_id=str(agent["workspace_id"]),
        tenant_id=str(agent["tenant_id"]),
        edge_session_id=str(agent["edge_session_id"]),
        identity_generation=int(agent["identity_generation"]),
        revocation_generation=int(agent["revocation_generation"]),
    )


def _entry_summary_json(metadata: SupportBundleUploadMetadata) -> str:
    value = [
        {
            "classification": entry.classification,
            "collector_code": entry.collector_code,
            "media_type": entry.media_type,
            "size_bytes": entry.size_bytes,
        }
        for entry in metadata.manifest.entries
    ]
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _object_reference(metadata: SupportBundleUploadMetadata) -> str:
    value = (
        f"edge-support-bundles/{metadata.manifest.bundle_id}."
        f"{metadata.object_sha256[:16]}.enc"
    )
    if _OBJECT_REFERENCE.fullmatch(value) is None:
        raise ApiError("edge.support_bundle_invalid")
    return value


def _same_upload(
    row: sqlite3.Row,
    authorization: EdgeSupportBundleUploadAuthorization,
    metadata: SupportBundleUploadMetadata,
    *,
    entry_summary_json: str,
    object_reference: str,
) -> bool:
    expected = (
        authorization.workspace_id,
        authorization.tenant_id,
        authorization.deployment_id,
        "ready",
        metadata.manifest_sha256,
        entry_summary_json,
        metadata.manifest.scan_version,
        "clean",
        metadata.manifest.archive_size_bytes,
        metadata.object_sha256,
        metadata.object_size_bytes,
        object_reference,
        metadata.approved_by_account_id,
        metadata.approval_proof_sha256,
        authorization.edge_session_id,
        authorization.identity_generation,
        authorization.revocation_generation,
        metadata.approved_at,
        metadata.manifest.expires_at,
    )
    actual = tuple(
        row[key]
        for key in (
            "workspace_id",
            "tenant_id",
            "edge_deployment_id",
            "status",
            "manifest_sha256",
            "entry_summary_json",
            "scan_version",
            "scan_outcome",
            "archive_size_bytes",
            "object_sha256",
            "object_size_bytes",
            "object_reference",
            "approved_by_account_id",
            "approval_proof_sha256",
            "source_edge_session_id",
            "source_identity_generation",
            "source_revocation_generation",
            "approved_at",
            "expires_at",
        )
    )
    return actual == expected


def _upload_receipt(
    row: sqlite3.Row,
    *,
    replay: bool,
    request_id: str,
    trace_id: str,
) -> SupportBundleUploadReceipt:
    return SupportBundleUploadReceipt(
        bundle_id=str(row["support_bundle_id"]),
        cloud_reference=str(row["support_bundle_id"]),
        status="ready",
        revision=int(row["revision"]),
        object_sha256=str(row["object_sha256"]),
        idempotent_replay=replay,
        request_id=_identifier(request_id),
        trace_id=_identifier(trace_id),
    )


def _owned_bundle(
    connection: sqlite3.Connection, workspace_id: str, tenant_id: str, bundle_id: str
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT * FROM edge_support_bundles
        WHERE workspace_id=? AND tenant_id=? AND support_bundle_id=?
        """,
        (workspace_id, tenant_id, bundle_id),
    ).fetchone()
    if row is None:
        raise ApiError("edge.support_bundle_not_found")
    return row


def _expire_bundle(connection: sqlite3.Connection, row: sqlite3.Row) -> sqlite3.Row:
    stamp = _db_time(_utcnow())
    updated = connection.execute(
        """
        UPDATE edge_support_bundles
        SET status='expired',revision=revision+1,updated_at=?
        WHERE support_bundle_id=? AND status='ready' AND revision=?
        """,
        (stamp, row["support_bundle_id"], row["revision"]),
    ).rowcount
    if updated != 1:
        raise ApiError("edge.support_bundle_conflict")
    return connection.execute(
        "SELECT * FROM edge_support_bundles WHERE support_bundle_id=?",
        (row["support_bundle_id"],),
    ).fetchone()


def _mark_bundle_deleted(
    config: AppConfig,
    bundle_id: str,
    object_reference: str,
    *,
    now: datetime | None = None,
) -> bool:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        changed = connection.execute(
            """
            UPDATE edge_support_bundles
            SET status='deleted',revision=revision+1,updated_at=?
            WHERE support_bundle_id=? AND object_reference=? AND status='expired'
            """,
            (_db_time(now or _utcnow()), bundle_id, object_reference),
        ).rowcount
        connection.commit()
        return changed == 1
    except (DbError, sqlite3.Error, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge.support_bundle_storage_unavailable") from None
    finally:
        connection.close()


def _mark_bundle_deleted_quietly(
    config: AppConfig, bundle_id: str, object_reference: str
) -> None:
    try:
        _mark_bundle_deleted(config, bundle_id, object_reference)
    except ApiError:
        return


def _bundle_summary(row: sqlite3.Row) -> EdgeSupportBundleSummary:
    try:
        entries = json.loads(str(row["entry_summary_json"]))
        if type(entries) is not list:
            raise ValueError
        summaries = tuple(EdgeSupportBundleEntrySummary.model_validate(item) for item in entries)
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ApiError("edge.support_bundle_integrity_failed") from None
    return EdgeSupportBundleSummary(
        support_bundle_id=str(row["support_bundle_id"]),
        deployment_id=str(row["edge_deployment_id"]),
        status=str(row["status"]),
        revision=int(row["revision"]),
        manifest_sha256=str(row["manifest_sha256"]),
        entries=summaries,
        scan_version=str(row["scan_version"]),
        scan_outcome=str(row["scan_outcome"]),
        archive_size_bytes=int(row["archive_size_bytes"]),
        object_size_bytes=int(row["object_size_bytes"]),
        approved_by_account_id=str(row["approved_by_account_id"]),
        approved_at=_datetime(row["approved_at"]),
        expires_at=_datetime(row["expires_at"]),
        created_at=_datetime(row["created_at"]),
    )


__all__ = (
    "EdgeSupportBundleDownload",
    "EdgeSupportBundleUploadAuthorization",
    "authorize_agent_support_bundle_upload",
    "get_workspace_support_bundle",
    "prepare_support_bundle_staging",
    "resolve_support_bundle_download",
    "store_agent_support_bundle",
)
