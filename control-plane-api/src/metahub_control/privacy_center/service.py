"""Owner-qualified PRD-134 privacy preference, DSR, and export orchestration."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.db import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.data_governance.service import (
    active_hold_blocks_deletion_in_transaction,
)
from metahub_control.identity.security_service import consume_step_up_grant_in_transaction
from metahub_control.identity.service import authenticate_global_session_in_transaction
from metahub_control.platform_access.service import authenticate_platform_session_in_transaction
from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    authenticate_workspace_session_in_transaction,
)

from .crypto import decrypt_identity_export, encrypt_identity_export
from .models import (
    ConsentRecordSummary,
    DataSubjectRequestCancel,
    DataSubjectRequestCreate,
    DataSubjectRequestListResponse,
    DataSubjectRequestResponse,
    DataSubjectRequestSummary,
    ErasureDecisionSummary,
    ExportReceiptResponse,
    IdentityExportSummary,
    OwnerProgressSummary,
    PlatformPrivacyQueueItem,
    PlatformPrivacyQueueResponse,
    PrivacyPreferencePatchRequest,
    PrivacyPreferenceResponse,
    PrivacyPreferenceSummary,
    RetentionExceptionSummary,
)

_OWNER_DOMAINS = (
    "identity",
    "membership",
    "audit",
    "billing",
    "support",
    "edge",
    "data_governance",
)
_PREFERENCE_PURPOSES = {
    "analytics_enabled": "analytics",
    "product_updates_enabled": "product_updates",
    "model_improvement_enabled": "model_improvement",
}
_CLASSIFICATION_BY_DOMAIN = {
    "identity": "identity",
    "membership": "identity",
    "audit": "audit",
    "billing": "billing",
    "support": "audit",
    "edge": "file",
    "data_governance": "audit",
}
_DSR_TERMINAL = frozenset({"completed", "partially_denied", "failed", "cancelled"})


@dataclass(frozen=True, slots=True, repr=False)
class IdentityExportDownload:
    content: bytes
    filename: str
    content_sha256: str
    receipt_sha256: str


def get_privacy_preferences(
    config: AppConfig,
    *,
    global_session_token: str,
    workspace_session_token: str,
    request_id: str,
    trace_id: str,
) -> PrivacyPreferenceResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor, now = _owner_context(
            connection,
            config,
            global_session_token=global_session_token,
            workspace_session_token=workspace_session_token,
            permission="privacy.preference.read",
        )
        row = _ensure_preference(connection, actor, now)
        response = _preference_response(connection, row, request_id=request_id, trace_id=trace_id)
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("privacy.unavailable") from None
    finally:
        connection.close()


def update_privacy_preferences(
    config: AppConfig,
    payload: PrivacyPreferencePatchRequest,
    *,
    global_session_token: str,
    workspace_session_token: str,
    global_csrf_token: str,
    expected_revision: int,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> PrivacyPreferenceResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor, now = _owner_context(
            connection,
            config,
            global_session_token=global_session_token,
            workspace_session_token=workspace_session_token,
            global_csrf_token=global_csrf_token,
            permission="privacy.preference.manage",
        )
        current = _ensure_preference(connection, actor, now)
        canonical = {
            "schema_version": 1,
            "workspace_id": actor.workspace_id,
            "account_id": actor.account_id,
            "expected_revision": expected_revision,
            **payload.model_dump(exclude_none=True),
        }
        request_sha256 = _digest(canonical)
        replay = _command_replay(
            connection,
            config,
            actor,
            action_code="preference.update",
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
        )
        if replay is not None:
            row = _preference_row(connection, actor)
            response = _preference_response(
                connection,
                row,
                request_id=request_id,
                trace_id=trace_id,
                idempotent_replay=True,
            )
            connection.commit()
            return response
        if int(current["revision"]) != expected_revision:
            raise ApiError("privacy.revision_conflict")
        values = {
            "analytics_enabled": bool(current["analytics_enabled"]),
            "product_updates_enabled": bool(current["product_updates_enabled"]),
            "model_improvement_enabled": bool(current["model_improvement_enabled"]),
            "terms_version": str(current["terms_version"]),
            "locale": str(current["locale"]),
        }
        values.update(payload.model_dump(exclude_none=True))
        stamp = _changed_time(now, current["updated_at"])
        changed_purposes: list[str] = []
        consent_ids: list[str] = []
        for field, purpose in _PREFERENCE_PURPOSES.items():
            if bool(values[field]) == bool(current[field]):
                continue
            changed_purposes.append(purpose)
            consent_ids.append(
                _write_consent(
                    connection,
                    actor=actor,
                    purpose=purpose,
                    terms_version=str(values["terms_version"]),
                    locale=str(values["locale"]),
                    decision="granted" if bool(values[field]) else "withdrawn",
                    preference_revision=expected_revision + 1,
                    now=stamp,
                )
            )
        if (
            values["terms_version"] != current["terms_version"]
            or values["locale"] != current["locale"]
        ):
            changed_purposes.append("privacy_terms")
            consent_ids.append(
                _write_consent(
                    connection,
                    actor=actor,
                    purpose="privacy_terms",
                    terms_version=str(values["terms_version"]),
                    locale=str(values["locale"]),
                    decision="granted",
                    preference_revision=expected_revision + 1,
                    now=stamp,
                )
            )
        changed = connection.execute(
            """UPDATE privacy_preferences
                  SET analytics_enabled=?,product_updates_enabled=?,model_improvement_enabled=?,
                      terms_version=?,locale=?,revision=revision+1,updated_at=?
                WHERE privacy_preference_id=? AND revision=?""",
            (
                int(bool(values["analytics_enabled"])),
                int(bool(values["product_updates_enabled"])),
                int(bool(values["model_improvement_enabled"])),
                values["terms_version"],
                values["locale"],
                _db_time(stamp),
                current["privacy_preference_id"],
                expected_revision,
            ),
        ).rowcount
        if changed != 1:
            raise ApiError("privacy.revision_conflict")
        _write_event(
            connection,
            event_type="privacy.consent_changed",
            actor=actor,
            object_type="consent_record" if consent_ids else "consent_record",
            object_id=consent_ids[-1] if consent_ids else str(current["privacy_preference_id"]),
            request_code="preference",
            status_code="updated",
            payload={
                "schema_version": 1,
                "changed_purposes": sorted(set(changed_purposes)),
                "preference_revision": expected_revision + 1,
            },
            request_id=request_id,
            trace_id=trace_id,
            now=stamp,
        )
        _write_command(
            connection,
            config,
            actor,
            action_code="preference.update",
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
            result_object_id=str(current["privacy_preference_id"]),
            result_revision=expected_revision + 1,
            request_id=request_id,
            trace_id=trace_id,
            now=stamp,
        )
        row = _preference_row(connection, actor)
        response = _preference_response(connection, row, request_id=request_id, trace_id=trace_id)
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("privacy.unavailable") from None
    finally:
        connection.close()


def create_data_subject_request(
    config: AppConfig,
    payload: DataSubjectRequestCreate,
    *,
    global_session_token: str,
    workspace_session_token: str,
    global_csrf_token: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> DataSubjectRequestResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor, now = _owner_context(
            connection,
            config,
            global_session_token=global_session_token,
            workspace_session_token=workspace_session_token,
            global_csrf_token=global_csrf_token,
            permission="privacy.dsr.create",
        )
        canonical = {
            "schema_version": 1,
            "workspace_id": actor.workspace_id,
            "account_id": actor.account_id,
            "request_kind": payload.request_kind,
        }
        request_sha256 = _digest(canonical)
        replay = _command_replay(
            connection,
            config,
            actor,
            action_code="request.create",
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
        )
        if replay is not None:
            row = _request_row(connection, actor, str(replay["result_object_id"]))
            response = _request_response(
                connection,
                row,
                request_id=request_id,
                trace_id=trace_id,
                idempotent_replay=True,
            )
            connection.commit()
            return response
        auth_method = consume_step_up_grant_in_transaction(
            connection,
            config,
            context=actor,
            grant=payload.step_up_grant,
            action_code="privacy.dsr.create",
            resource_type="privacy_request",
            resource_id=actor.workspace_id,
            now=now,
        )
        request_object_id = _id("dsr_")
        request_code = "prq_" + secrets.token_hex(8)
        deadline = now + timedelta(days=30)
        warning = deadline - timedelta(days=3)
        stamp = _db_time(now)
        connection.execute(
            """INSERT INTO data_subject_requests(
                 data_subject_request_id,request_code,workspace_id,tenant_id,account_id,
                 requester_membership_id,request_kind,status,verification_generation,
                 auth_method,authorization_fingerprint_sha256,revision,deadline_at,
                 deadline_warning_at,warning_sent_at,terminal_reason_code,terminal_at,
                 received_at,updated_at)
               VALUES(?,?,?,?,?, ?,?,'received',1,?,?,1,?,?,NULL,NULL,NULL,?,?)""",
            (
                request_object_id,
                request_code,
                actor.workspace_id,
                actor.tenant_id,
                actor.account_id,
                actor.membership_id,
                payload.request_kind,
                auth_method,
                actor.authorization_fingerprint,
                _db_time(deadline),
                _db_time(warning),
                stamp,
                stamp,
            ),
        )
        adapters = connection.execute(
            "SELECT owner_domain,adapter_version FROM privacy_owner_adapter_versions "
            "WHERE status='active' ORDER BY owner_domain"
        ).fetchall()
        if tuple(str(row["owner_domain"]) for row in adapters) != tuple(sorted(_OWNER_DOMAINS)):
            raise ApiError("privacy.adapter_unavailable")
        for adapter in adapters:
            connection.execute(
                """INSERT INTO data_subject_request_owner_jobs(
                     owner_job_id,workspace_id,tenant_id,account_id,data_subject_request_id,
                     owner_domain,adapter_version,status,outcome_code,checkpoint_code,
                     export_fragment_json,executor_receipt_sha256,policy_generation,
                     hold_generation,attempt_count,next_attempt_at,lease_owner_id,
                     lease_token_sha256,lease_expires_at,revision,completed_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,'pending',NULL,'queued',NULL,NULL,NULL,NULL,0,?,
                     NULL,NULL,NULL,1,NULL,?,?)""",
                (
                    _id("poj_"),
                    actor.workspace_id,
                    actor.tenant_id,
                    actor.account_id,
                    request_object_id,
                    adapter["owner_domain"],
                    adapter["adapter_version"],
                    stamp,
                    stamp,
                    stamp,
                ),
            )
        _write_event(
            connection,
            event_type="privacy.request_received",
            actor=actor,
            object_type="data_subject_request",
            object_id=request_object_id,
            request_code=request_code,
            status_code="received",
            payload={"schema_version": 1, "request_kind": payload.request_kind},
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        _write_command(
            connection,
            config,
            actor,
            action_code="request.create",
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
            result_object_id=request_object_id,
            result_revision=1,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        row = _request_row(connection, actor, request_object_id)
        response = _request_response(connection, row, request_id=request_id, trace_id=trace_id)
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("privacy.unavailable") from None
    finally:
        connection.close()


def list_data_subject_requests(
    config: AppConfig,
    *,
    global_session_token: str,
    workspace_session_token: str,
    limit: int,
    request_id: str,
    trace_id: str,
) -> DataSubjectRequestListResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor, _now = _owner_context(
            connection,
            config,
            global_session_token=global_session_token,
            workspace_session_token=workspace_session_token,
            permission="privacy.dsr.read",
        )
        rows = connection.execute(
            "SELECT * FROM data_subject_requests WHERE workspace_id=? AND tenant_id=? "
            "AND account_id=? ORDER BY received_at DESC,data_subject_request_id DESC LIMIT ?",
            (actor.workspace_id, actor.tenant_id, actor.account_id, limit),
        ).fetchall()
        response = DataSubjectRequestListResponse(
            items=tuple(_request_summary(connection, row) for row in rows),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("privacy.unavailable") from None
    finally:
        connection.close()


def get_data_subject_request(
    config: AppConfig,
    request_object_id: str,
    *,
    global_session_token: str,
    workspace_session_token: str,
    request_id: str,
    trace_id: str,
) -> DataSubjectRequestResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor, _now = _owner_context(
            connection,
            config,
            global_session_token=global_session_token,
            workspace_session_token=workspace_session_token,
            permission="privacy.dsr.read",
        )
        row = _request_row(connection, actor, request_object_id)
        response = _request_response(connection, row, request_id=request_id, trace_id=trace_id)
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("privacy.unavailable") from None
    finally:
        connection.close()


def cancel_data_subject_request(
    config: AppConfig,
    request_object_id: str,
    payload: DataSubjectRequestCancel,
    *,
    global_session_token: str,
    workspace_session_token: str,
    global_csrf_token: str,
    expected_revision: int,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> DataSubjectRequestResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor, now = _owner_context(
            connection,
            config,
            global_session_token=global_session_token,
            workspace_session_token=workspace_session_token,
            global_csrf_token=global_csrf_token,
            permission="privacy.dsr.cancel",
        )
        canonical = {
            "schema_version": 1,
            "request_id": request_object_id,
            "expected_revision": expected_revision,
        }
        request_sha256 = _digest(canonical)
        replay = _command_replay(
            connection,
            config,
            actor,
            action_code="request.cancel",
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
        )
        if replay is not None:
            row = _request_row(connection, actor, request_object_id)
            response = _request_response(
                connection,
                row,
                request_id=request_id,
                trace_id=trace_id,
                idempotent_replay=True,
            )
            connection.commit()
            return response
        row = _request_row(connection, actor, request_object_id)
        if int(row["revision"]) != expected_revision:
            raise ApiError("privacy.revision_conflict")
        if str(row["status"]) not in {"received", "identity_verified", "collecting"}:
            raise ApiError("privacy.request_not_cancellable")
        consume_step_up_grant_in_transaction(
            connection,
            config,
            context=actor,
            grant=payload.step_up_grant,
            action_code="privacy.dsr.cancel",
            resource_type="privacy_request",
            resource_id=request_object_id,
            now=now,
        )
        stamp = _changed_time(now, row["updated_at"])
        connection.execute(
            """UPDATE data_subject_request_owner_jobs
                  SET status='cancelled',checkpoint_code='cancelled_by_subject',
                      lease_owner_id=NULL,lease_token_sha256=NULL,lease_expires_at=NULL,
                      revision=revision+1,completed_at=?,updated_at=?
                WHERE data_subject_request_id=? AND status IN('pending','running')""",
            (_db_time(stamp), _db_time(stamp), request_object_id),
        )
        changed = connection.execute(
            """UPDATE data_subject_requests
                  SET status='cancelled',revision=revision+1,
                      terminal_reason_code='subject_cancelled',terminal_at=?,updated_at=?
                WHERE data_subject_request_id=? AND revision=?""",
            (_db_time(stamp), _db_time(stamp), request_object_id, expected_revision),
        ).rowcount
        if changed != 1:
            raise ApiError("privacy.revision_conflict")
        _write_command(
            connection,
            config,
            actor,
            action_code="request.cancel",
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
            result_object_id=request_object_id,
            result_revision=expected_revision + 1,
            request_id=request_id,
            trace_id=trace_id,
            now=stamp,
        )
        row = _request_row(connection, actor, request_object_id)
        response = _request_response(connection, row, request_id=request_id, trace_id=trace_id)
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("privacy.unavailable") from None
    finally:
        connection.close()


def issue_export_receipt(
    config: AppConfig,
    request_object_id: str,
    *,
    global_session_token: str,
    workspace_session_token: str,
    global_csrf_token: str,
    expected_revision: int,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> ExportReceiptResponse:
    connection = _connect(config)
    raw_receipt = ""
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor, now = _owner_context(
            connection,
            config,
            global_session_token=global_session_token,
            workspace_session_token=workspace_session_token,
            global_csrf_token=global_csrf_token,
            permission="privacy.dsr.read",
        )
        request_row = _request_row(connection, actor, request_object_id)
        export = connection.execute(
            "SELECT * FROM identity_exports WHERE data_subject_request_id=? "
            "AND workspace_id=? AND tenant_id=? AND account_id=?",
            (request_object_id, actor.workspace_id, actor.tenant_id, actor.account_id),
        ).fetchone()
        if export is None:
            raise ApiError("privacy.export_not_ready")
        canonical = {
            "schema_version": 1,
            "request_id": request_object_id,
            "export_id": str(export["identity_export_id"]),
            "expected_revision": expected_revision,
        }
        request_sha256 = _digest(canonical)
        replay = _command_replay(
            connection,
            config,
            actor,
            action_code="export.receipt.issue",
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
        )
        if replay is not None:
            if str(replay["result_object_id"]) != str(export["identity_export_id"]):
                raise ApiError("privacy.idempotency_conflict")
            raw_receipt = _derive_export_receipt(
                config,
                export_id=str(export["identity_export_id"]),
                export_revision=int(replay["result_revision"]),
                idempotency_key=idempotency_key,
            )
            if not hmac.compare_digest(
                str(export["receipt_hmac"]),
                _secret_hmac(config, "export-receipt", raw_receipt),
            ):
                raise ApiError("privacy.export_receipt_invalid")
            response = ExportReceiptResponse(
                identity_export_id=str(export["identity_export_id"]),
                receipt=raw_receipt,
                expires_at=_time(export["receipt_expires_at"]),
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return response
        if request_row["status"] != "ready":
            raise ApiError("privacy.export_not_ready")
        if _time(export["expires_at"]) <= now or export["status"] != "ready":
            raise ApiError("privacy.export_expired")
        if int(export["revision"]) != expected_revision:
            raise ApiError("privacy.revision_conflict")
        result_revision = expected_revision + 1
        raw_receipt = _derive_export_receipt(
            config,
            export_id=str(export["identity_export_id"]),
            export_revision=result_revision,
            idempotency_key=idempotency_key,
        )
        expires_at = min(_time(export["expires_at"]), now + timedelta(minutes=5))
        stamp = _changed_time(now, export["updated_at"])
        changed = connection.execute(
            """UPDATE identity_exports
                  SET receipt_status='active',receipt_hmac=?,receipt_expires_at=?,
                      revision=revision+1,updated_at=?
                WHERE identity_export_id=? AND status='ready' AND revision=?""",
            (
                _secret_hmac(config, "export-receipt", raw_receipt),
                _db_time(expires_at),
                _db_time(stamp),
                export["identity_export_id"],
                expected_revision,
            ),
        ).rowcount
        if changed != 1:
            raise ApiError("privacy.revision_conflict")
        _write_command(
            connection,
            config,
            actor,
            action_code="export.receipt.issue",
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
            result_object_id=str(export["identity_export_id"]),
            result_revision=result_revision,
            request_id=request_id,
            trace_id=trace_id,
            now=stamp,
        )
        response = ExportReceiptResponse(
            identity_export_id=str(export["identity_export_id"]),
            receipt=raw_receipt,
            expires_at=expires_at,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("privacy.unavailable") from None
    finally:
        raw_receipt = ""
        connection.close()


def download_identity_export(
    config: AppConfig,
    export_id: str,
    *,
    receipt: str,
    global_session_token: str,
    workspace_session_token: str,
    request_id: str,
    trace_id: str,
) -> IdentityExportDownload:
    connection = _connect(config)
    plaintext = b""
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor, now = _owner_context(
            connection,
            config,
            global_session_token=global_session_token,
            workspace_session_token=workspace_session_token,
            permission="privacy.dsr.read",
        )
        export = connection.execute(
            "SELECT * FROM identity_exports WHERE identity_export_id=? AND workspace_id=? "
            "AND tenant_id=? AND account_id=?",
            (export_id, actor.workspace_id, actor.tenant_id, actor.account_id),
        ).fetchone()
        if export is None:
            raise ApiError("privacy.export_not_found")
        if (
            export["status"] != "ready"
            or export["receipt_status"] != "active"
            or _time(export["expires_at"]) <= now
            or _time(export["receipt_expires_at"]) <= now
            or not hmac.compare_digest(
                str(export["receipt_hmac"]),
                _secret_hmac(config, "export-receipt", receipt),
            )
        ):
            raise ApiError("privacy.export_receipt_invalid")
        plaintext = decrypt_identity_export(
            config,
            str(export["artifact_ciphertext_b64"]),
            str(export["artifact_nonce_b64"]),
            export_id=export_id,
            workspace_id=actor.workspace_id,
            tenant_id=actor.tenant_id,
            account_id=actor.account_id,
            plaintext_sha256=str(export["plaintext_sha256"]),
            artifact_sha256=str(export["artifact_sha256"]),
        )
        if len(plaintext) != int(export["size_bytes"]):
            raise ApiError("privacy.export_corrupt")
        stamp = _changed_time(now, export["updated_at"])
        changed = connection.execute(
            """UPDATE identity_exports
                  SET status='downloaded',receipt_status='consumed',revision=revision+1,
                      downloaded_at=?,updated_at=?
                WHERE identity_export_id=? AND status='ready' AND receipt_status='active'""",
            (_db_time(stamp), _db_time(stamp), export_id),
        ).rowcount
        if changed != 1:
            raise ApiError("privacy.export_receipt_invalid")
        request_row = connection.execute(
            "SELECT * FROM data_subject_requests WHERE data_subject_request_id=?",
            (export["data_subject_request_id"],),
        ).fetchone()
        request_stamp = _changed_time(stamp, request_row["updated_at"])
        connection.execute(
            """UPDATE data_subject_requests
                  SET status='completed',revision=revision+1,
                      terminal_reason_code='export_downloaded',terminal_at=?,updated_at=?
                WHERE data_subject_request_id=? AND status='ready'""",
            (
                _db_time(request_stamp),
                _db_time(request_stamp),
                export["data_subject_request_id"],
            ),
        )
        _write_event(
            connection,
            event_type="privacy.request_completed",
            actor=actor,
            object_type="data_subject_request",
            object_id=str(export["data_subject_request_id"]),
            request_code=str(request_row["request_code"]),
            status_code="completed",
            payload={"schema_version": 1, "request_kind": "export"},
            request_id=request_id,
            trace_id=trace_id,
            now=request_stamp,
        )
        result = IdentityExportDownload(
            content=plaintext,
            filename=f"metahub-identity-export-{str(request_row['request_code'])}.json",
            content_sha256=str(export["plaintext_sha256"]),
            receipt_sha256=hashlib.sha256(receipt.encode("ascii")).hexdigest(),
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("privacy.unavailable") from None
    finally:
        plaintext = b""
        connection.close()


def list_platform_privacy_queue(
    config: AppConfig,
    *,
    platform_session_token: str,
    limit: int,
    request_id: str,
    trace_id: str,
) -> PlatformPrivacyQueueResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = authenticate_platform_session_in_transaction(
            connection,
            config,
            session_token=platform_session_token,
            touch=True,
        )
        if "platform.privacy.read" not in actor.permissions:
            raise ApiError("platform.permission_denied")
        rows = connection.execute(
            "SELECT * FROM data_subject_requests ORDER BY "
            "CASE WHEN status IN('received','identity_verified','collecting','reviewing','ready') "
            "THEN 0 ELSE 1 END,deadline_at,received_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        items = []
        for row in rows:
            progress = _owner_progress(connection, str(row["data_subject_request_id"]))
            decisions = connection.execute(
                "SELECT outcome FROM erasure_decisions WHERE data_subject_request_id=? "
                "ORDER BY owner_domain",
                (row["data_subject_request_id"],),
            ).fetchall()
            items.append(
                PlatformPrivacyQueueItem(
                    request_code=str(row["request_code"]),
                    request_kind=str(row["request_kind"]),
                    status=str(row["status"]),
                    deadline_at=_time(row["deadline_at"]),
                    deadline_warning_at=_time(row["deadline_warning_at"]),
                    warning_sent=row["warning_sent_at"] is not None,
                    domain_progress=progress,
                    legal_outcomes=tuple(str(item["outcome"]) for item in decisions),
                    received_at=_time(row["received_at"]),
                    updated_at=_time(row["updated_at"]),
                )
            )
        connection.commit()
        return PlatformPrivacyQueueResponse(
            items=tuple(items), request_id=request_id, trace_id=trace_id
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("privacy.unavailable") from None
    finally:
        connection.close()


def process_privacy_once(config: AppConfig, *, worker_id: str, limit: int = 8) -> int:
    processed = _scan_deadlines_and_exports(config, worker_id=worker_id)
    for _ in range(limit):
        claim = _claim_owner_job(config, worker_id)
        if claim is None:
            break
        try:
            _process_owner_job(config, claim)
        except ApiError as exc:
            _fail_owner_job(config, claim, exc.code)
        except (sqlite3.Error, DbError, ValueError, TypeError, KeyError):
            _fail_owner_job(config, claim, "privacy.owner_unavailable")
        processed += 1
    return processed


def _scan_deadlines_and_exports(config: AppConfig, *, worker_id: str) -> int:
    connection = _connect(config)
    lease_token = secrets.token_urlsafe(32)
    lease_hash = hashlib.sha256(lease_token.encode("ascii")).hexdigest()
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _database_now(connection)
        state = connection.execute(
            "SELECT * FROM privacy_scheduler_state WHERE singleton_id=1"
        ).fetchone()
        if state is None:
            raise ApiError("privacy.unavailable")
        if (
            state["lease_owner_id"] is not None
            and _time(state["lease_expires_at"]) > now
            and state["lease_owner_id"] != worker_id
        ):
            connection.rollback()
            return 0
        acquired_at = _changed_time(now, state["updated_at"])
        fencing_generation = int(state["fencing_generation"]) + 1
        connection.execute(
            """UPDATE privacy_scheduler_state
                  SET lease_owner_id=?,lease_token_sha256=?,lease_expires_at=?,
                      fencing_generation=?,revision=revision+1,updated_at=?
                WHERE singleton_id=1""",
            (
                worker_id,
                lease_hash,
                _db_time(acquired_at + timedelta(minutes=2)),
                fencing_generation,
                _db_time(acquired_at),
            ),
        )
        connection.commit()

        connection.execute("BEGIN IMMEDIATE")
        now = _database_now(connection)
        live = connection.execute(
            "SELECT * FROM privacy_scheduler_state WHERE singleton_id=1"
        ).fetchone()
        if (
            live is None
            or live["lease_owner_id"] != worker_id
            or not hmac.compare_digest(str(live["lease_token_sha256"]), lease_hash)
            or int(live["fencing_generation"]) != fencing_generation
            or _time(live["lease_expires_at"]) <= now
        ):
            raise ApiError("privacy.worker_fenced")
        rows = connection.execute(
            "SELECT * FROM data_subject_requests WHERE warning_sent_at IS NULL "
            "AND status NOT IN('completed','partially_denied','failed','cancelled') "
            "AND deadline_warning_at<=? ORDER BY deadline_warning_at LIMIT 100",
            (_db_time(now),),
        ).fetchall()
        count = 0
        for row in rows:
            stamp = _changed_time(now, row["updated_at"])
            connection.execute(
                "UPDATE data_subject_requests SET warning_sent_at=?,revision=revision+1,"
                "updated_at=? "
                "WHERE data_subject_request_id=? AND warning_sent_at IS NULL",
                (_db_time(stamp), _db_time(stamp), row["data_subject_request_id"]),
            )
            _write_event_for_request(
                connection,
                row,
                event_type="privacy.deadline_warning",
                status_code="deadline_warning",
                request_id="privacy-worker",
                trace_id="privacy-worker",
                now=stamp,
            )
            count += 1
        expired = connection.execute(
            "SELECT * FROM identity_exports WHERE status='ready' AND expires_at<=? LIMIT 100",
            (_db_time(now),),
        ).fetchall()
        for export in expired:
            stamp = _changed_time(now, export["updated_at"])
            receipt_status = "expired" if export["receipt_status"] != "none" else "none"
            connection.execute(
                "UPDATE identity_exports SET status='expired',receipt_status=?,"
                "revision=revision+1,updated_at=? WHERE identity_export_id=? AND status='ready'",
                (receipt_status, _db_time(stamp), export["identity_export_id"]),
            )
            request_row = connection.execute(
                "SELECT * FROM data_subject_requests WHERE data_subject_request_id=?",
                (export["data_subject_request_id"],),
            ).fetchone()
            if request_row is not None and request_row["status"] == "ready":
                request_stamp = _changed_time(stamp, request_row["updated_at"])
                connection.execute(
                    "UPDATE data_subject_requests SET status='failed',revision=revision+1,"
                    "terminal_reason_code='export_expired',terminal_at=?,updated_at=? "
                    "WHERE data_subject_request_id=? AND status='ready'",
                    (
                        _db_time(request_stamp),
                        _db_time(request_stamp),
                        export["data_subject_request_id"],
                    ),
                )
            count += 1
        stamp = _changed_time(now, live["updated_at"])
        released = connection.execute(
            """UPDATE privacy_scheduler_state
                  SET last_warning_scan_at=?,lease_owner_id=NULL,lease_token_sha256=NULL,
                      lease_expires_at=NULL,revision=revision+1,updated_at=?
                WHERE singleton_id=1 AND lease_owner_id=? AND lease_token_sha256=?
                  AND fencing_generation=?""",
            (
                _db_time(now),
                _db_time(stamp),
                worker_id,
                lease_hash,
                fencing_generation,
            ),
        ).rowcount
        if released != 1:
            raise ApiError("privacy.worker_fenced")
        connection.commit()
        return count
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError, KeyError):
        _rollback(connection)
        raise ApiError("privacy.unavailable") from None
    finally:
        lease_token = ""
        connection.close()


def _claim_owner_job(config: AppConfig, worker_id: str) -> dict[str, object] | None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _database_now(connection)
        row = connection.execute(
            """SELECT * FROM data_subject_request_owner_jobs
                 WHERE (status='pending' AND next_attempt_at<=?)
                    OR(status='running' AND lease_expires_at<=?)
                 ORDER BY next_attempt_at,owner_job_id LIMIT 1""",
            (_db_time(now), _db_time(now)),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        token = secrets.token_urlsafe(32)
        stamp = _changed_time(now, row["updated_at"])
        changed = connection.execute(
            """UPDATE data_subject_request_owner_jobs
                  SET status='running',checkpoint_code='owner_claimed',
                      attempt_count=attempt_count+1,
                      lease_owner_id=?,lease_token_sha256=?,lease_expires_at=?,
                      revision=revision+1,updated_at=?
                WHERE owner_job_id=? AND revision=?""",
            (
                worker_id,
                hashlib.sha256(token.encode("ascii")).hexdigest(),
                _db_time(now + timedelta(minutes=2)),
                _db_time(stamp),
                row["owner_job_id"],
                row["revision"],
            ),
        ).rowcount
        if changed != 1:
            connection.rollback()
            return None
        result = {key: row[key] for key in row.keys()}
        result["lease_token"] = token
        result["attempt_count"] = int(row["attempt_count"]) + 1
        connection.commit()
        return result
    finally:
        connection.close()


def _process_owner_job(config: AppConfig, claim: dict[str, object]) -> None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        job = connection.execute(
            "SELECT * FROM data_subject_request_owner_jobs WHERE owner_job_id=?",
            (claim["owner_job_id"],),
        ).fetchone()
        if (
            job is None
            or job["status"] != "running"
            or not hmac.compare_digest(
                str(job["lease_token_sha256"]),
                hashlib.sha256(str(claim["lease_token"]).encode("ascii")).hexdigest(),
            )
        ):
            raise ApiError("privacy.worker_fenced")
        request_row = connection.execute(
            "SELECT * FROM data_subject_requests WHERE data_subject_request_id=?",
            (job["data_subject_request_id"],),
        ).fetchone()
        if request_row is None:
            raise ApiError("privacy.request_not_found")
        now = _database_now(connection)
        if request_row["status"] == "cancelled":
            _complete_cancelled_job(connection, job, now)
            connection.commit()
            return
        request_row = _advance_request_to_collecting(connection, request_row, now)
        kind = str(request_row["request_kind"])
        domain = str(job["owner_domain"])
        fragment: dict[str, object] | None = None
        policy_generation: int | None = None
        hold_generation: int | None = None
        if kind == "export":
            fragment = _export_fragment(connection, request_row, domain)
            outcome = "exported"
        elif kind == "rectify":
            outcome = "rectified"
        else:
            outcome, policy_generation, hold_generation = _execute_erasure_decision(
                connection, request_row, job, now
            )
        receipt = _digest(
            {
                "schema_version": 1,
                "request_id": request_row["data_subject_request_id"],
                "owner_domain": domain,
                "adapter_version": job["adapter_version"],
                "outcome": outcome,
                "fragment_sha256": _digest(fragment) if fragment is not None else None,
                "policy_generation": policy_generation,
                "hold_generation": hold_generation,
            }
        )
        stamp = _changed_time(now, job["updated_at"])
        connection.execute(
            """UPDATE data_subject_request_owner_jobs
                  SET status='completed',outcome_code=?,checkpoint_code='owner_completed',
                      export_fragment_json=?,executor_receipt_sha256=?,policy_generation=?,
                      hold_generation=?,lease_owner_id=NULL,lease_token_sha256=NULL,
                      lease_expires_at=NULL,revision=revision+1,completed_at=?,updated_at=?
                WHERE owner_job_id=? AND status='running'""",
            (
                outcome,
                _canonical(fragment) if fragment is not None else None,
                receipt,
                policy_generation,
                hold_generation,
                _db_time(stamp),
                _db_time(stamp),
                job["owner_job_id"],
            ),
        )
        _finalize_request_if_ready(connection, request_row, config, stamp)
        connection.commit()
    except ApiError:
        _rollback(connection)
        raise
    finally:
        connection.close()


def _fail_owner_job(config: AppConfig, claim: dict[str, object], error_code: str) -> None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        job = connection.execute(
            "SELECT * FROM data_subject_request_owner_jobs WHERE owner_job_id=?",
            (claim["owner_job_id"],),
        ).fetchone()
        if job is None or job["status"] != "running":
            connection.commit()
            return
        now = _database_now(connection)
        stamp = _changed_time(now, job["updated_at"])
        terminal = int(job["attempt_count"]) >= 8
        connection.execute(
            """UPDATE data_subject_request_owner_jobs
                  SET status=?,checkpoint_code=?,next_attempt_at=?,lease_owner_id=NULL,
                      lease_token_sha256=NULL,lease_expires_at=NULL,revision=revision+1,
                      completed_at=?,updated_at=?
                WHERE owner_job_id=? AND status='running'""",
            (
                "failed" if terminal else "pending",
                "owner_failed" if terminal else "retry_scheduled",
                _db_time(now + timedelta(seconds=min(300, 2 ** int(job["attempt_count"])))),
                _db_time(stamp) if terminal else None,
                _db_time(stamp),
                job["owner_job_id"],
            ),
        )
        if terminal:
            request_row = connection.execute(
                "SELECT * FROM data_subject_requests WHERE data_subject_request_id=?",
                (job["data_subject_request_id"],),
            ).fetchone()
            if request_row is not None and request_row["status"] not in _DSR_TERMINAL:
                request_stamp = _changed_time(stamp, request_row["updated_at"])
                connection.execute(
                    "UPDATE data_subject_requests SET status='failed',revision=revision+1,"
                    "terminal_reason_code='owner_failed',terminal_at=?,updated_at=? "
                    "WHERE data_subject_request_id=?",
                    (
                        _db_time(request_stamp),
                        _db_time(request_stamp),
                        job["data_subject_request_id"],
                    ),
                )
        connection.commit()
    except (sqlite3.Error, DbError, ValueError, TypeError, KeyError):
        _rollback(connection)
    finally:
        connection.close()


def _advance_request_to_collecting(connection, row, now: datetime):
    current = row
    if current["status"] == "received":
        stamp = _changed_time(now, current["updated_at"])
        connection.execute(
            "UPDATE data_subject_requests SET status='identity_verified',revision=revision+1,"
            "updated_at=? WHERE data_subject_request_id=? AND status='received'",
            (_db_time(stamp), current["data_subject_request_id"]),
        )
        current = connection.execute(
            "SELECT * FROM data_subject_requests WHERE data_subject_request_id=?",
            (current["data_subject_request_id"],),
        ).fetchone()
    if current["status"] == "identity_verified":
        stamp = _changed_time(now, current["updated_at"])
        connection.execute(
            "UPDATE data_subject_requests SET status='collecting',revision=revision+1,"
            "updated_at=? WHERE data_subject_request_id=? AND status='identity_verified'",
            (_db_time(stamp), current["data_subject_request_id"]),
        )
        current = connection.execute(
            "SELECT * FROM data_subject_requests WHERE data_subject_request_id=?",
            (current["data_subject_request_id"],),
        ).fetchone()
    if current["status"] != "collecting":
        raise ApiError("privacy.request_state_invalid")
    return current


def _export_fragment(connection, request_row, domain: str) -> dict[str, object]:
    workspace_id = str(request_row["workspace_id"])
    tenant_id = str(request_row["tenant_id"])
    account_id = str(request_row["account_id"])
    if domain == "identity":
        row = connection.execute(
            "SELECT account_id,email_normalized,display_name,status,created_at "
            "FROM global_accounts WHERE account_id=?",
            (account_id,),
        ).fetchone()
        return {
            "account_id": str(row["account_id"]),
            "email": str(row["email_normalized"]),
            "display_name": str(row["display_name"]),
            "status": str(row["status"]),
            "created_at": str(row["created_at"]),
        }
    if domain == "membership":
        row = connection.execute(
            "SELECT workspace_id,role_code,status,created_at FROM workspace_memberships "
            "WHERE workspace_id=? AND tenant_id=? AND account_id=?",
            (workspace_id, tenant_id, account_id),
        ).fetchone()
        return {
            "workspace_id": workspace_id,
            "role": str(row["role_code"]),
            "status": str(row["status"]),
            "created_at": str(row["created_at"]),
        }
    if domain == "audit":
        row = connection.execute(
            "SELECT COUNT(*) event_count,MAX(occurred_at) latest_event_at "
            "FROM membership_events WHERE workspace_id=? AND tenant_id=? AND actor_account_id=?",
            (workspace_id, tenant_id, account_id),
        ).fetchone()
        return {"event_count": int(row["event_count"]), "latest_event_at": row["latest_event_at"]}
    if domain == "billing":
        subscription = connection.execute(
            "SELECT status FROM workspace_subscriptions WHERE workspace_id=? AND tenant_id=?",
            (workspace_id, tenant_id),
        ).fetchone()
        profile = connection.execute(
            "SELECT 1 FROM workspace_billing_profiles WHERE workspace_id=? AND tenant_id=?",
            (workspace_id, tenant_id),
        ).fetchone()
        return {
            "subscription_status": str(subscription["status"])
            if subscription
            else "not_configured",
            "billing_profile_configured": profile is not None,
        }
    if domain == "support":
        row = connection.execute(
            "SELECT COUNT(*) ticket_count,SUM(CASE WHEN status NOT IN('resolved','closed') "
            "THEN 1 ELSE 0 END) open_ticket_count FROM support_tickets "
            "WHERE workspace_id=? AND tenant_id=? AND reporter_account_id=?",
            (workspace_id, tenant_id, account_id),
        ).fetchone()
        return {
            "ticket_count": int(row["ticket_count"]),
            "open_ticket_count": int(row["open_ticket_count"] or 0),
        }
    if domain == "edge":
        count = connection.execute(
            "SELECT COUNT(*) FROM edge_deployments WHERE workspace_id=? AND tenant_id=? "
            "AND created_by_account_id=?",
            (workspace_id, tenant_id, account_id),
        ).fetchone()[0]
        return {"deployment_count": int(count)}
    return {"subject_data_collected": False}


def _execute_erasure_decision(connection, request_row, job, now: datetime):
    workspace_id = str(request_row["workspace_id"])
    tenant_id = str(request_row["tenant_id"])
    domain = str(job["owner_domain"])
    classification = _CLASSIFICATION_BY_DOMAIN[domain]
    fence = connection.execute(
        "SELECT policy_generation,hold_generation FROM data_governance_fences "
        "WHERE workspace_id=? AND tenant_id=?",
        (workspace_id, tenant_id),
    ).fetchone()
    if fence is None:
        raise ApiError("privacy.governance_unavailable")
    policy_generation = int(fence["policy_generation"])
    hold_generation = int(fence["hold_generation"])
    hold_active = active_hold_blocks_deletion_in_transaction(
        connection,
        workspace_id=workspace_id,
        tenant_id=tenant_id,
        classification_code=classification,
    )
    if hold_active:
        outcome, legal_code, job_outcome = "retain", "legal_hold", "retained"
    elif domain in {"audit", "billing", "data_governance"}:
        outcome, legal_code, job_outcome = "retain", "statutory_retention", "retained"
    elif domain == "identity":
        outcome, legal_code, job_outcome = "anonymize", "subject_request", "anonymized"
    elif domain == "edge":
        outcome, legal_code, job_outcome = "not_found", "owner_not_found", "not_found"
    elif domain == "support":
        outcome, legal_code, job_outcome = "anonymize", "subject_request", "anonymized"
    else:
        outcome, legal_code, job_outcome = "erase", "subject_request", "erased"
    receipt = _digest(
        {
            "schema_version": 1,
            "request_id": request_row["data_subject_request_id"],
            "owner_domain": domain,
            "outcome": outcome,
            "policy_generation": policy_generation,
            "hold_generation": hold_generation,
        }
    )
    connection.execute(
        """INSERT INTO erasure_decisions(
             erasure_decision_id,workspace_id,tenant_id,account_id,data_subject_request_id,
             owner_domain,adapter_version,outcome,legal_code,policy_generation,hold_generation,
             executor_receipt_sha256,decided_at)
           VALUES(?,?,?,?,?,?,1,?,?,?,?,?,?)""",
        (
            _id("erd_"),
            workspace_id,
            tenant_id,
            request_row["account_id"],
            request_row["data_subject_request_id"],
            domain,
            outcome,
            legal_code,
            policy_generation,
            hold_generation,
            receipt,
            _db_time(now),
        ),
    )
    if outcome == "retain":
        expiry = None
        explanation = "active_legal_hold"
        if legal_code == "statutory_retention":
            explanation = "retention_period_active"
            retention = connection.execute(
                "SELECT retention_days FROM retention_policies WHERE workspace_id=? "
                "AND tenant_id=? AND classification_code=? AND status='active'",
                (workspace_id, tenant_id, classification),
            ).fetchone()
            days = int(retention["retention_days"]) if retention else 365
            expiry = _db_time(now + timedelta(days=days))
        connection.execute(
            """INSERT INTO retention_exceptions(
                 retention_exception_id,workspace_id,tenant_id,account_id,data_subject_request_id,
                 owner_domain,classification_code,legal_code,policy_generation,hold_generation,
                 explanation_code,expires_at,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                _id("rex_"),
                workspace_id,
                tenant_id,
                request_row["account_id"],
                request_row["data_subject_request_id"],
                domain,
                classification,
                legal_code,
                policy_generation,
                hold_generation,
                explanation,
                expiry,
                _db_time(now),
            ),
        )
    return job_outcome, policy_generation, hold_generation


def _finalize_request_if_ready(connection, request_row, config: AppConfig, now: datetime) -> None:
    rows = connection.execute(
        "SELECT * FROM data_subject_request_owner_jobs WHERE data_subject_request_id=? "
        "ORDER BY owner_domain",
        (request_row["data_subject_request_id"],),
    ).fetchall()
    if len(rows) != len(_OWNER_DOMAINS) or any(
        row["status"] not in {"completed", "failed", "cancelled"} for row in rows
    ):
        return
    current = connection.execute(
        "SELECT * FROM data_subject_requests WHERE data_subject_request_id=?",
        (request_row["data_subject_request_id"],),
    ).fetchone()
    if current["status"] in _DSR_TERMINAL:
        return
    stamp = _changed_time(now, current["updated_at"])
    if any(row["status"] == "failed" for row in rows):
        status, reason = "failed", "owner_failed"
    elif current["request_kind"] == "export":
        _create_identity_export(connection, current, rows, config, stamp)
        connection.execute(
            "UPDATE data_subject_requests SET status='ready',revision=revision+1,updated_at=? "
            "WHERE data_subject_request_id=? AND status='collecting'",
            (_db_time(stamp), current["data_subject_request_id"]),
        )
        return
    elif current["request_kind"] == "erase" and any(
        row["outcome_code"] == "retained" for row in rows
    ):
        status, reason = "partially_denied", "retention_exception"
    else:
        status, reason = "completed", "owners_completed"
    connection.execute(
        "UPDATE data_subject_requests SET status=?,revision=revision+1,"
        "terminal_reason_code=?,terminal_at=?,updated_at=? "
        "WHERE data_subject_request_id=? AND status='collecting'",
        (status, reason, _db_time(stamp), _db_time(stamp), current["data_subject_request_id"]),
    )
    completed = connection.execute(
        "SELECT * FROM data_subject_requests WHERE data_subject_request_id=?",
        (current["data_subject_request_id"],),
    ).fetchone()
    _write_event_for_request(
        connection,
        completed,
        event_type="privacy.request_completed",
        status_code=status,
        request_id="privacy-worker",
        trace_id="privacy-worker",
        now=stamp,
    )


def _create_identity_export(
    connection, request_row, jobs, config: AppConfig, now: datetime
) -> None:
    if (
        connection.execute(
            "SELECT 1 FROM identity_exports WHERE data_subject_request_id=?",
            (request_row["data_subject_request_id"],),
        ).fetchone()
        is not None
    ):
        return
    fragments: dict[str, object] = {}
    manifest: list[dict[str, object]] = []
    for job in jobs:
        fragment = json.loads(str(job["export_fragment_json"]))
        domain = str(job["owner_domain"])
        fragments[domain] = fragment
        manifest.append(
            {
                "owner_domain": domain,
                "adapter_version": int(job["adapter_version"]),
                "outcome": str(job["outcome_code"]),
                "fragment_sha256": _digest(fragment),
            }
        )
    artifact = {
        "schema_version": 1,
        "generated_at": _db_time(now),
        "subject": {
            "account_id": str(request_row["account_id"]),
            "workspace_id": str(request_row["workspace_id"]),
        },
        "owners": fragments,
    }
    plaintext = _canonical(artifact).encode("ascii")
    if not 2 <= len(plaintext) <= 524_288:
        raise ApiError("privacy.export_too_large")
    export_id = _id("pex_")
    ciphertext, nonce, artifact_sha256, plaintext_sha256 = encrypt_identity_export(
        config,
        plaintext,
        export_id=export_id,
        workspace_id=str(request_row["workspace_id"]),
        tenant_id=str(request_row["tenant_id"]),
        account_id=str(request_row["account_id"]),
    )
    manifest_json = _canonical(manifest)
    connection.execute(
        """INSERT INTO identity_exports(
             identity_export_id,workspace_id,tenant_id,account_id,data_subject_request_id,
             schema_version,owner_manifest_json,owner_manifest_sha256,artifact_ciphertext_b64,
             artifact_nonce_b64,artifact_sha256,plaintext_sha256,size_bytes,key_version,status,
             receipt_status,receipt_hmac,receipt_expires_at,revision,ready_at,downloaded_at,
             expires_at,updated_at)
           VALUES(?,?,?,?,?,1,?,?,?,?,?,?,?,?,'ready','none',NULL,NULL,1,?,NULL,?,?)""",
        (
            export_id,
            request_row["workspace_id"],
            request_row["tenant_id"],
            request_row["account_id"],
            request_row["data_subject_request_id"],
            manifest_json,
            hashlib.sha256(manifest_json.encode("ascii")).hexdigest(),
            ciphertext,
            nonce,
            artifact_sha256,
            plaintext_sha256,
            len(plaintext),
            config.tenant_data_encryption_version,
            _db_time(now),
            _db_time(now + timedelta(hours=24)),
            _db_time(now),
        ),
    )


def _complete_cancelled_job(connection, job, now: datetime) -> None:
    stamp = _changed_time(now, job["updated_at"])
    connection.execute(
        "UPDATE data_subject_request_owner_jobs SET status='cancelled',"
        "checkpoint_code='request_cancelled',lease_owner_id=NULL,lease_token_sha256=NULL,"
        "lease_expires_at=NULL,revision=revision+1,completed_at=?,updated_at=? "
        "WHERE owner_job_id=? AND status='running'",
        (_db_time(stamp), _db_time(stamp), job["owner_job_id"]),
    )


def _owner_context(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    global_session_token: str,
    workspace_session_token: str,
    permission: str,
    global_csrf_token: str | None = None,
) -> tuple[WorkspaceSessionContext, datetime]:
    identity, now = authenticate_global_session_in_transaction(
        connection,
        config,
        session_token=global_session_token,
        csrf_token=global_csrf_token,
        touch=True,
    )
    workspace = authenticate_workspace_session_in_transaction(
        connection,
        config,
        session_token=workspace_session_token,
        touch=True,
    )
    if (
        workspace.account_id != identity.account_id
        or workspace.global_session_id != identity.session_id
        or permission not in workspace.permission_codes
    ):
        raise ApiError("privacy.owner_mismatch")
    return workspace, now


def _ensure_preference(connection, actor: WorkspaceSessionContext, now: datetime):
    row = _preference_row(connection, actor, required=False)
    if row is not None:
        return row
    consent = connection.execute(
        "SELECT consent_version,locale FROM consent_records WHERE account_id=? "
        "AND consent_type='privacy_terms' ORDER BY consented_at DESC,"
        "consent_record_id DESC LIMIT 1",
        (actor.account_id,),
    ).fetchone()
    terms_version = str(consent["consent_version"]) if consent else "privacy-2026-08"
    locale = str(consent["locale"]) if consent and consent["locale"] != "und" else "zh-cn"
    stamp = _db_time(now)
    connection.execute(
        """INSERT INTO privacy_preferences(
             privacy_preference_id,workspace_id,tenant_id,account_id,analytics_enabled,
             product_updates_enabled,model_improvement_enabled,terms_version,locale,
             revision,created_at,updated_at)
           VALUES(?,?,?,?,0,0,0,?,?,1,?,?)""",
        (
            _id("prf_"),
            actor.workspace_id,
            actor.tenant_id,
            actor.account_id,
            terms_version,
            locale,
            stamp,
            stamp,
        ),
    )
    return _preference_row(connection, actor)


def _preference_row(connection, actor: WorkspaceSessionContext, *, required: bool = True):
    row = connection.execute(
        "SELECT * FROM privacy_preferences WHERE workspace_id=? AND tenant_id=? AND account_id=?",
        (actor.workspace_id, actor.tenant_id, actor.account_id),
    ).fetchone()
    if row is None and required:
        raise ApiError("privacy.preference_not_found")
    return row


def _request_row(connection, actor: WorkspaceSessionContext, request_object_id: str):
    row = connection.execute(
        "SELECT * FROM data_subject_requests WHERE data_subject_request_id=? "
        "AND workspace_id=? AND tenant_id=? AND account_id=?",
        (request_object_id, actor.workspace_id, actor.tenant_id, actor.account_id),
    ).fetchone()
    if row is None:
        raise ApiError("privacy.request_not_found")
    return row


def _write_consent(
    connection,
    *,
    actor: WorkspaceSessionContext,
    purpose: str,
    terms_version: str,
    locale: str,
    decision: str,
    preference_revision: int,
    now: datetime,
) -> str:
    consent_id = _id("con_")
    evidence = _digest(
        {
            "schema_version": 1,
            "account_id": actor.account_id,
            "workspace_id": actor.workspace_id,
            "purpose": purpose,
            "terms_version": terms_version,
            "locale": locale,
            "decision": decision,
            "preference_revision": preference_revision,
        }
    )
    connection.execute(
        """INSERT INTO consent_records(
             consent_record_id,account_id,consent_type,consent_version,
             source_registration_intent_id,locale,source_kind,decision,payload_sha256,consented_at)
           VALUES(?,?,?,?,NULL,?,'privacy_center',?,?,?)""",
        (
            consent_id,
            actor.account_id,
            purpose,
            terms_version,
            locale,
            decision,
            evidence,
            _db_time(now),
        ),
    )
    return consent_id


def _preference_response(
    connection,
    row,
    *,
    request_id: str,
    trace_id: str,
    idempotent_replay: bool = False,
) -> PrivacyPreferenceResponse:
    consents = connection.execute(
        "SELECT * FROM consent_records WHERE account_id=? "
        "ORDER BY consented_at DESC,consent_record_id DESC LIMIT 100",
        (row["account_id"],),
    ).fetchall()
    return PrivacyPreferenceResponse(
        preference=PrivacyPreferenceSummary(
            privacy_preference_id=str(row["privacy_preference_id"]),
            workspace_id=str(row["workspace_id"]),
            analytics_enabled=bool(row["analytics_enabled"]),
            product_updates_enabled=bool(row["product_updates_enabled"]),
            model_improvement_enabled=bool(row["model_improvement_enabled"]),
            terms_version=str(row["terms_version"]),
            locale=str(row["locale"]),
            revision=int(row["revision"]),
            created_at=_time(row["created_at"]),
            updated_at=_time(row["updated_at"]),
        ),
        consent_records=tuple(
            ConsentRecordSummary(
                consent_record_id=str(item["consent_record_id"]),
                purpose=str(item["consent_type"]),
                terms_version=str(item["consent_version"]),
                locale=str(item["locale"]),
                source_kind=str(item["source_kind"]),
                decision=str(item["decision"]),
                evidence_sha256=str(item["payload_sha256"]),
                decided_at=_time(item["consented_at"]),
            )
            for item in consents
        ),
        idempotent_replay=idempotent_replay,
        request_id=request_id,
        trace_id=trace_id,
    )


def _request_response(
    connection,
    row,
    *,
    request_id: str,
    trace_id: str,
    idempotent_replay: bool = False,
) -> DataSubjectRequestResponse:
    return DataSubjectRequestResponse(
        request=_request_summary(connection, row),
        idempotent_replay=idempotent_replay,
        request_id=request_id,
        trace_id=trace_id,
    )


def _request_summary(connection, row) -> DataSubjectRequestSummary:
    export = connection.execute(
        "SELECT * FROM identity_exports WHERE data_subject_request_id=?",
        (row["data_subject_request_id"],),
    ).fetchone()
    decisions = connection.execute(
        "SELECT * FROM erasure_decisions WHERE data_subject_request_id=? ORDER BY owner_domain",
        (row["data_subject_request_id"],),
    ).fetchall()
    exceptions = connection.execute(
        "SELECT * FROM retention_exceptions WHERE data_subject_request_id=? ORDER BY owner_domain",
        (row["data_subject_request_id"],),
    ).fetchall()
    return DataSubjectRequestSummary(
        data_subject_request_id=str(row["data_subject_request_id"]),
        request_code=str(row["request_code"]),
        workspace_id=str(row["workspace_id"]),
        request_kind=str(row["request_kind"]),
        status=str(row["status"]),
        revision=int(row["revision"]),
        deadline_at=_time(row["deadline_at"]),
        deadline_warning_at=_time(row["deadline_warning_at"]),
        warning_sent=row["warning_sent_at"] is not None,
        received_at=_time(row["received_at"]),
        updated_at=_time(row["updated_at"]),
        terminal_reason_code=row["terminal_reason_code"],
        terminal_at=_optional_time(row["terminal_at"]),
        owner_progress=_owner_progress(connection, str(row["data_subject_request_id"])),
        identity_export=(
            None
            if export is None
            else IdentityExportSummary(
                identity_export_id=str(export["identity_export_id"]),
                schema_version=int(export["schema_version"]),
                size_bytes=int(export["size_bytes"]),
                revision=int(export["revision"]),
                status=str(export["status"]),
                receipt_status=str(export["receipt_status"]),
                ready_at=_time(export["ready_at"]),
                downloaded_at=_optional_time(export["downloaded_at"]),
                expires_at=_time(export["expires_at"]),
            )
        ),
        erasure_decisions=tuple(
            ErasureDecisionSummary(
                owner_domain=str(item["owner_domain"]),
                outcome=str(item["outcome"]),
                legal_code=str(item["legal_code"]),
                policy_generation=int(item["policy_generation"]),
                hold_generation=int(item["hold_generation"]),
                decided_at=_time(item["decided_at"]),
            )
            for item in decisions
        ),
        retention_exceptions=tuple(
            RetentionExceptionSummary(
                owner_domain=str(item["owner_domain"]),
                classification_code=str(item["classification_code"]),
                legal_code=str(item["legal_code"]),
                explanation_code=str(item["explanation_code"]),
                policy_generation=int(item["policy_generation"]),
                hold_generation=int(item["hold_generation"]),
                expires_at=_optional_time(item["expires_at"]),
            )
            for item in exceptions
        ),
    )


def _owner_progress(connection, request_object_id: str) -> tuple[OwnerProgressSummary, ...]:
    rows = connection.execute(
        "SELECT * FROM data_subject_request_owner_jobs WHERE data_subject_request_id=? "
        "ORDER BY owner_domain",
        (request_object_id,),
    ).fetchall()
    return tuple(
        OwnerProgressSummary(
            owner_domain=str(row["owner_domain"]),
            status=str(row["status"]),
            outcome_code=row["outcome_code"],
            checkpoint_code=str(row["checkpoint_code"]),
            attempt_count=int(row["attempt_count"]),
            completed_at=_optional_time(row["completed_at"]),
        )
        for row in rows
    )


def _write_event_for_request(
    connection,
    row,
    *,
    event_type: str,
    status_code: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    actor = _actor_from_request(row)
    _write_event(
        connection,
        event_type=event_type,
        actor=actor,
        object_type="data_subject_request",
        object_id=str(row["data_subject_request_id"]),
        request_code=str(row["request_code"]),
        status_code=status_code,
        payload={
            "schema_version": 1,
            "request_kind": str(row["request_kind"]),
            "status": status_code,
        },
        request_id=request_id,
        trace_id=trace_id,
        now=now,
    )


def _write_event(
    connection,
    *,
    event_type: str,
    actor,
    object_type: str,
    object_id: str,
    request_code: str,
    status_code: str,
    payload: dict[str, object],
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    event_id = _id("pev_")
    payload_json = _canonical(payload)
    connection.execute(
        """INSERT INTO privacy_events(
             privacy_event_id,event_type,workspace_id,tenant_id,account_id,object_type,
             object_id,request_code,status_code,request_id,trace_id,payload_json,
             payload_sha256,occurred_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event_id,
            event_type,
            actor.workspace_id,
            actor.tenant_id,
            actor.account_id,
            object_type,
            object_id,
            request_code,
            status_code,
            request_id,
            trace_id,
            payload_json,
            hashlib.sha256(payload_json.encode("ascii")).hexdigest(),
            _db_time(now),
        ),
    )
    connection.execute(
        """INSERT INTO privacy_event_outbox(
             privacy_event_outbox_id,privacy_event_id,workspace_id,tenant_id,status,
             attempt_count,next_attempt_at,delivered_at,created_at,updated_at)
           VALUES(?,?,?,?,'pending',0,?,NULL,?,?)""",
        (
            _id("pob_"),
            event_id,
            actor.workspace_id,
            actor.tenant_id,
            _db_time(now),
            _db_time(now),
            _db_time(now),
        ),
    )


@dataclass(frozen=True, slots=True)
class _RequestActor:
    workspace_id: str
    tenant_id: str
    account_id: str


def _actor_from_request(row) -> _RequestActor:
    return _RequestActor(
        workspace_id=str(row["workspace_id"]),
        tenant_id=str(row["tenant_id"]),
        account_id=str(row["account_id"]),
    )


def _write_command(
    connection,
    config: AppConfig,
    actor: WorkspaceSessionContext,
    *,
    action_code: str,
    idempotency_key: str,
    request_sha256: str,
    result_object_id: str,
    result_revision: int,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    connection.execute(
        """INSERT INTO privacy_action_commands(
             privacy_action_command_id,workspace_id,tenant_id,account_id,action_code,
             idempotency_sha256,request_sha256,result_object_id,result_revision,
             request_id,trace_id,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            _id("pcm_"),
            actor.workspace_id,
            actor.tenant_id,
            actor.account_id,
            action_code,
            _secret_hmac(config, "idempotency", idempotency_key),
            request_sha256,
            result_object_id,
            result_revision,
            request_id,
            trace_id,
            _db_time(now),
        ),
    )


def _command_replay(
    connection,
    config: AppConfig,
    actor: WorkspaceSessionContext,
    *,
    action_code: str,
    idempotency_key: str,
    request_sha256: str,
):
    row = connection.execute(
        "SELECT * FROM privacy_action_commands WHERE workspace_id=? AND account_id=? "
        "AND action_code=? AND idempotency_sha256=?",
        (
            actor.workspace_id,
            actor.account_id,
            action_code,
            _secret_hmac(config, "idempotency", idempotency_key),
        ),
    ).fetchone()
    if row is not None and not hmac.compare_digest(str(row["request_sha256"]), request_sha256):
        raise ApiError("privacy.idempotency_conflict")
    return row


def _secret_hmac(config: AppConfig, domain: str, value: str) -> str:
    return hmac.new(
        config.platform_session_secret.encode("utf-8"),
        b"prd134/" + domain.encode("ascii") + b"/v1\0" + value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _derive_export_receipt(
    config: AppConfig,
    *,
    export_id: str,
    export_revision: int,
    idempotency_key: str,
) -> str:
    digest = hmac.new(
        config.platform_session_secret.encode("utf-8"),
        (f"prd134/export-receipt/v1\0{export_id}\0{export_revision}\0{idempotency_key}").encode(
            "ascii"
        ),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _connect(config: AppConfig):
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("privacy.unavailable") from None


def _rollback(connection) -> None:
    if connection.in_transaction:
        connection.rollback()


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("ascii")).hexdigest()


def _database_now(connection) -> datetime:
    return _time(connection.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0])


def _changed_time(now: datetime, previous: object) -> datetime:
    prior = _time(previous)
    return now if now > prior else prior + timedelta(milliseconds=1)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _time(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _optional_time(value: object | None) -> datetime | None:
    return None if value is None else _time(value)


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


__all__ = [
    "IdentityExportDownload",
    "cancel_data_subject_request",
    "create_data_subject_request",
    "download_identity_export",
    "get_data_subject_request",
    "get_privacy_preferences",
    "issue_export_receipt",
    "list_data_subject_requests",
    "list_platform_privacy_queue",
    "process_privacy_once",
    "update_privacy_preferences",
]
