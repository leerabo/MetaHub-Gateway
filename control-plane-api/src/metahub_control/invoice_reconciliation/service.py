"""PRD-104 不可变 Statement、发票产物、双审批和冻结水位对账服务。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from metahub_shared.business_verification import (
    BusinessFieldOwner,
    BusinessVerificationCryptoError,
    decrypt_business_field,
    encrypt_business_field,
)
from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.platform_access.service import (
    PlatformContext,
    authenticate_platform_session_in_transaction,
)
from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    authenticate_workspace_session_in_transaction,
)

from .models import (
    BillingStatementInfo,
    BillingStatementsResponse,
    InvoiceArtifactInfo,
    InvoiceCreateRequest,
    InvoiceDownloadGrantResponse,
    InvoiceInfo,
    InvoiceResponse,
    InvoiceReversalApprovalRequest,
    InvoiceReversalCreateRequest,
    InvoiceReversalInfo,
    InvoiceReversalResponse,
    ReconciliationCreateRequest,
    ReconciliationDifferenceInfo,
    ReconciliationDifferencesResponse,
    ReconciliationResponse,
    ReconciliationRunInfo,
)

_FINANCE_ROLES = frozenset({"owner", "finance_admin"})
_STATEMENT_ID_RE = re.compile(r"^bst_[0-9a-f]{32}$")
_INVOICE_ID_RE = re.compile(r"^ivr_[0-9a-f]{32}$")
_ARTIFACT_ID_RE = re.compile(r"^ina_[0-9a-f]{32}$")
_REVERSAL_ID_RE = re.compile(r"^irr_[0-9a-f]{32}$")
_RECONCILIATION_ID_RE = re.compile(r"^rec_[0-9a-f]{32}$")
_DIFFERENCE_ID_RE = re.compile(r"^rcd_[0-9a-f]{32}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_DOWNLOAD_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_PROVIDER = "mocktax"


@dataclass(frozen=True, slots=True)
class DownloadedInvoiceArtifact:
    content: bytes
    mime_type: str
    filename: str


def list_billing_statements(
    config: AppConfig,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> BillingStatementsResponse:
    """发布当前 Workspace 已关闭账期的缺失 Statement，并返回不可变摘要。"""

    connection = _connect(config, "invoice.unavailable")
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_workspace_session_in_transaction(
            connection, config, session_token=session_token, touch=False
        )
        _require_finance(actor)
        _require_schema(connection)
        _materialize_statements(connection, actor.workspace_id)
        rows = connection.execute(
            """
            SELECT * FROM billing_statements WHERE workspace_id=?
            ORDER BY issued_at DESC,billing_statement_id DESC LIMIT 100
            """,
            (actor.workspace_id,),
        ).fetchall()
        response = BillingStatementsResponse(
            statements=tuple(_statement(row) for row in rows),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("invoice.unavailable") from None
    finally:
        connection.close()


def create_invoice(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    payload: InvoiceCreateRequest,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> InvoiceResponse:
    """幂等锁定 Statement 与当前已验证 BillingProfile revision，并写 provider outbox。"""

    _validate_id(payload.billing_statement_id, _STATEMENT_ID_RE, "invoice.request_invalid")
    _validate_idempotency(idempotency_key, "invoice.request_invalid")
    request_sha256 = _intent_hash(
        "invoice.create",
        {
            "billing_statement_id": payload.billing_statement_id,
            "invoice_type": payload.invoice_type,
        },
    )
    idempotency_sha256 = _domain_hmac(config, "invoice-idempotency", idempotency_key)
    connection = _connect(config, "invoice.unavailable")
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_workspace_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=False,
        )
        _require_finance(actor)
        _require_schema(connection)
        _materialize_statements(connection, actor.workspace_id)
        replay = connection.execute(
            "SELECT * FROM invoice_requests WHERE workspace_id=? AND idempotency_sha256=?",
            (actor.workspace_id, idempotency_sha256),
        ).fetchone()
        if replay is not None:
            if not hmac.compare_digest(str(replay["request_sha256"]), request_sha256):
                raise ApiError("invoice.idempotency_conflict")
            replay_with_statement = _invoice_row(
                connection, str(replay["invoice_request_id"]), actor.workspace_id
            )
            response = _invoice_response(
                connection,
                replay_with_statement,
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return response
        statement = connection.execute(
            "SELECT * FROM billing_statements WHERE billing_statement_id=? AND workspace_id=?",
            (payload.billing_statement_id, actor.workspace_id),
        ).fetchone()
        if statement is None:
            raise ApiError("invoice.not_found")
        if int(statement["total_minor"]) <= 0 or statement["statement_kind"] == "credit":
            raise ApiError("invoice.conflict")
        profile = connection.execute(
            "SELECT * FROM workspace_billing_profiles WHERE workspace_id=?",
            (actor.workspace_id,),
        ).fetchone()
        if profile is None or profile["verification_status"] != "verified":
            raise ApiError("invoice.profile_not_ready")
        prior = connection.execute(
            """
            SELECT 1 FROM invoice_requests
            WHERE billing_statement_id=? AND status NOT IN('failed','reversed')
            """,
            (payload.billing_statement_id,),
        ).fetchone()
        if prior is not None:
            raise ApiError("invoice.conflict")
        now = _utcnow()
        invoice_id = _id("ivr_")
        connection.execute(
            """
            INSERT INTO invoice_requests(
              invoice_request_id,billing_statement_id,workspace_id,tenant_id,
              workspace_billing_profile_id,billing_profile_revision,invoice_type,provider,status,
              idempotency_sha256,request_sha256,state_revision,requested_by_membership_id,
              requested_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,'requested',?,?,1,?,?,?)
            """,
            (
                invoice_id,
                statement["billing_statement_id"],
                actor.workspace_id,
                actor.tenant_id,
                profile["workspace_billing_profile_id"],
                int(profile["revision"]),
                payload.invoice_type,
                _PROVIDER,
                idempotency_sha256,
                request_sha256,
                actor.membership_id,
                _db_time(now),
                _db_time(now),
            ),
        )
        effect_key = f"invoice.issue:{invoice_id}"
        connection.execute(
            """
            INSERT INTO invoice_provider_outbox(
              invoice_provider_outbox_id,invoice_request_id,reversal_request_id,command_type,
              effect_key,payload_sha256,status,attempt_count,lease_owner,lease_token,
              lease_expires_at,safe_error_code,created_at,updated_at)
            VALUES(?,?,NULL,'issue_invoice',?,?,'pending',0,NULL,NULL,NULL,NULL,?,?)
            """,
            (
                _id("ipo_"),
                invoice_id,
                effect_key,
                _sha256(
                    f"{request_sha256}\0{profile['revision']}\0{statement['statement_sha256']}"
                ),
                _db_time(now),
                _db_time(now),
            ),
        )
        created = _invoice_row(connection, invoice_id, actor.workspace_id)
        response = _invoice_response(connection, created, request_id=request_id, trace_id=trace_id)
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("invoice.unavailable") from None
    finally:
        connection.close()


def get_invoice(
    config: AppConfig,
    *,
    session_token: str,
    invoice_id: str,
    request_id: str,
    trace_id: str,
) -> InvoiceResponse:
    _validate_id(invoice_id, _INVOICE_ID_RE, "invoice.not_found")
    connection = _connect(config, "invoice.unavailable")
    try:
        connection.execute("BEGIN")
        actor = authenticate_workspace_session_in_transaction(
            connection, config, session_token=session_token, touch=False
        )
        _require_finance(actor)
        _require_schema(connection)
        row = _invoice_row(connection, invoice_id, actor.workspace_id)
        response = _invoice_response(connection, row, request_id=request_id, trace_id=trace_id)
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("invoice.unavailable") from None
    finally:
        connection.close()


def issue_invoice_download_token(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    invoice_id: str,
    artifact_id: str,
    request_id: str,
    trace_id: str,
) -> InvoiceDownloadGrantResponse:
    _validate_id(invoice_id, _INVOICE_ID_RE, "invoice.download_invalid")
    _validate_id(artifact_id, _ARTIFACT_ID_RE, "invoice.download_invalid")
    connection = _connect(config, "invoice.unavailable")
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_workspace_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=False,
        )
        _require_finance(actor)
        _require_schema(connection)
        artifact = connection.execute(
            """
            SELECT artifact.* FROM invoice_artifacts artifact
            WHERE artifact.invoice_artifact_id=? AND artifact.invoice_request_id=?
              AND artifact.workspace_id=? AND artifact.tenant_id=?
            """,
            (artifact_id, invoice_id, actor.workspace_id, actor.tenant_id),
        ).fetchone()
        if artifact is None:
            raise ApiError("invoice.download_invalid")
        raw_token = secrets.token_urlsafe(32)
        token_hmac = _domain_hmac(config, "invoice-download", raw_token)
        now = _utcnow()
        expires_at = now + timedelta(
            seconds=min(120, config.business_verification_download_ttl_seconds)
        )
        connection.execute(
            """
            INSERT INTO invoice_download_grants(
              invoice_download_grant_id,invoice_artifact_id,workspace_id,tenant_id,
              workspace_session_id,token_hmac,status,expires_at,created_at,used_at)
            VALUES(?,?,?,?,?,?,'active',?,?,NULL)
            """,
            (
                _id("idg_"),
                artifact_id,
                actor.workspace_id,
                actor.tenant_id,
                actor.workspace_session_id,
                token_hmac,
                _db_time(expires_at),
                _db_time(now),
            ),
        )
        connection.commit()
        return InvoiceDownloadGrantResponse(
            invoice_request_id=invoice_id,
            invoice_artifact_id=artifact_id,
            download_token=raw_token,
            expires_at=expires_at,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("invoice.unavailable") from None
    finally:
        connection.close()


def consume_invoice_download(
    config: AppConfig,
    *,
    session_token: str,
    raw_token: str,
) -> DownloadedInvoiceArtifact:
    """原子消费绑定当前 WorkspaceSession 的一次性 token，随后读取 owner-bound 对象。"""

    if _DOWNLOAD_TOKEN_RE.fullmatch(raw_token) is None:
        raise ApiError("invoice.download_invalid")
    token_hmac = _domain_hmac(config, "invoice-download", raw_token)
    connection = _connect(config, "invoice.unavailable")
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_workspace_session_in_transaction(
            connection, config, session_token=session_token, touch=False
        )
        _require_finance(actor)
        row = connection.execute(
            """
            SELECT grant.*,artifact.object_reference_ciphertext,artifact.mime_type,
              artifact.content_sha256,artifact.size_bytes,artifact.invoice_request_id,
              artifact.kms_key_version
            FROM invoice_download_grants grant
            JOIN invoice_artifacts artifact
              ON artifact.invoice_artifact_id=grant.invoice_artifact_id
            WHERE grant.token_hmac=? AND grant.workspace_session_id=?
              AND grant.workspace_id=? AND grant.tenant_id=?
            """,
            (token_hmac, actor.workspace_session_id, actor.workspace_id, actor.tenant_id),
        ).fetchone()
        now = _utcnow()
        if row is None or row["status"] != "active":
            raise ApiError("invoice.download_invalid")
        if _parse_time(row["expires_at"]) <= now:
            connection.execute(
                "UPDATE invoice_download_grants SET status='expired' WHERE token_hmac=?",
                (token_hmac,),
            )
            connection.commit()
            raise ApiError("invoice.download_invalid")
        updated = connection.execute(
            """
            UPDATE invoice_download_grants SET status='used',used_at=?
            WHERE token_hmac=? AND status='active'
            """,
            (_db_time(now), token_hmac),
        ).rowcount
        if updated != 1:
            raise ApiError("invoice.download_invalid")
        reference = _decrypt_reference(config, row)
        artifact_id = str(row["invoice_artifact_id"])
        mime_type = str(row["mime_type"])
        expected_hash = str(row["content_sha256"])
        expected_size = int(row["size_bytes"])
        invoice_id = str(row["invoice_request_id"])
        connection.commit()
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, BusinessVerificationCryptoError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("invoice.download_invalid") from None
    finally:
        connection.close()
    try:
        content = _read_artifact_object(config, reference)
        if len(content) != expected_size or not hmac.compare_digest(
            hashlib.sha256(content).hexdigest(), expected_hash
        ):
            raise OSError("artifact integrity mismatch")
    except OSError:
        raise ApiError("invoice.download_invalid") from None
    extension = "pdf" if mime_type == "application/pdf" else "csv"
    return DownloadedInvoiceArtifact(
        content=content,
        mime_type=mime_type,
        filename=f"invoice-{invoice_id[4:12]}-{artifact_id[4:12]}.{extension}",
    )


def create_invoice_reversal(
    config: AppConfig,
    *,
    platform_session_token: str,
    csrf_token: str,
    invoice_id: str,
    payload: InvoiceReversalCreateRequest,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> InvoiceReversalResponse:
    _validate_id(invoice_id, _INVOICE_ID_RE, "invoice.not_found")
    _validate_idempotency(idempotency_key, "invoice.request_invalid")
    request_sha256 = _intent_hash(
        "invoice.reversal.create",
        {"invoice_request_id": invoice_id, "reason_code": payload.reason_code},
    )
    idempotency_sha256 = _domain_hmac(config, "reversal-idempotency", idempotency_key)
    connection = _connect(config, "invoice.unavailable")
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_platform_session_in_transaction(
            connection,
            config,
            session_token=platform_session_token,
            csrf_token=csrf_token,
            touch=False,
        )
        _actor_domains(actor)
        replay = connection.execute(
            "SELECT * FROM invoice_reversal_requests WHERE idempotency_sha256=?",
            (idempotency_sha256,),
        ).fetchone()
        if replay is not None:
            if not hmac.compare_digest(str(replay["request_sha256"]), request_sha256):
                raise ApiError("invoice.idempotency_conflict")
            response = _reversal_response(
                connection,
                replay,
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return response
        invoice = connection.execute(
            "SELECT * FROM invoice_requests WHERE invoice_request_id=?", (invoice_id,)
        ).fetchone()
        if invoice is None:
            raise ApiError("invoice.not_found")
        if invoice["status"] != "issued":
            raise ApiError("invoice.conflict")
        now = _utcnow()
        reversal_id = _id("irr_")
        connection.execute(
            """
            INSERT INTO invoice_reversal_requests(
              invoice_reversal_request_id,invoice_request_id,requested_by_operator_id,
              reason_code,status,idempotency_sha256,request_sha256,state_revision,
              requested_at,updated_at)
            VALUES(?,?,?,?,'pending_approvals',?,?,1,?,?)
            """,
            (
                reversal_id,
                invoice_id,
                actor.operator_id,
                payload.reason_code,
                idempotency_sha256,
                request_sha256,
                _db_time(now),
                _db_time(now),
            ),
        )
        connection.execute(
            """
            UPDATE invoice_requests SET status='reversal_pending',
              state_revision=state_revision+1,updated_at=?
            WHERE invoice_request_id=? AND status='issued'
            """,
            (_db_time(now), invoice_id),
        )
        created = _reversal_row(connection, reversal_id)
        response = _reversal_response(connection, created, request_id=request_id, trace_id=trace_id)
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("invoice.unavailable") from None
    finally:
        connection.close()


def approve_invoice_reversal(
    config: AppConfig,
    *,
    platform_session_token: str,
    csrf_token: str,
    reversal_id: str,
    payload: InvoiceReversalApprovalRequest,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> InvoiceReversalResponse:
    _validate_id(reversal_id, _REVERSAL_ID_RE, "invoice.not_found")
    _validate_idempotency(idempotency_key, "invoice.request_invalid")
    idempotency_sha256 = _domain_hmac(config, "reversal-approval-idempotency", idempotency_key)
    connection = _connect(config, "invoice.unavailable")
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_platform_session_in_transaction(
            connection,
            config,
            session_token=platform_session_token,
            csrf_token=csrf_token,
            touch=False,
        )
        domains = _actor_domains(actor)
        reversal = _reversal_row(connection, reversal_id)
        request_sha256 = _intent_hash(
            "invoice.reversal.approve",
            {
                "invoice_reversal_request_id": reversal_id,
                "decision": payload.decision,
                "reason_code": payload.reason_code,
            },
        )
        replay = connection.execute(
            "SELECT * FROM invoice_reversal_approvals WHERE idempotency_sha256=?",
            (idempotency_sha256,),
        ).fetchone()
        if replay is not None:
            if not hmac.compare_digest(str(replay["request_sha256"]), request_sha256):
                raise ApiError("invoice.idempotency_conflict")
            response = _reversal_response(
                connection,
                reversal,
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return response
        if reversal["status"] != "pending_approvals":
            raise ApiError("invoice.reversal_conflict")
        if reversal["requested_by_operator_id"] == actor.operator_id:
            raise ApiError("invoice.reversal_conflict")
        existing = {
            str(row["approval_domain"])
            for row in connection.execute(
                """
                SELECT approval_domain FROM invoice_reversal_approvals
                WHERE invoice_reversal_request_id=?
                """,
                (reversal_id,),
            ).fetchall()
        }
        available = [
            domain
            for domain in ("billing", "compliance")
            if domain in domains and domain not in existing
        ]
        if not available:
            raise ApiError("invoice.reversal_conflict")
        approval_domain = available[0]
        now = _utcnow()
        connection.execute(
            """
            INSERT INTO invoice_reversal_approvals(
              invoice_reversal_approval_id,invoice_reversal_request_id,approval_domain,
              operator_id,decision,reason_code,idempotency_sha256,request_sha256,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                _id("ira_"),
                reversal_id,
                approval_domain,
                actor.operator_id,
                payload.decision,
                payload.reason_code,
                idempotency_sha256,
                request_sha256,
                _db_time(now),
            ),
        )
        if payload.decision == "reject":
            connection.execute(
                """
                UPDATE invoice_reversal_requests SET status='rejected',
                  state_revision=state_revision+1,updated_at=?
                WHERE invoice_reversal_request_id=? AND status='pending_approvals'
                """,
                (_db_time(now), reversal_id),
            )
            connection.execute(
                """
                UPDATE invoice_requests SET status='issued',
                  state_revision=state_revision+1,updated_at=?
                WHERE invoice_request_id=? AND status='reversal_pending'
                """,
                (_db_time(now), reversal["invoice_request_id"]),
            )
        else:
            approved = {
                str(row["approval_domain"])
                for row in connection.execute(
                    """
                    SELECT approval_domain FROM invoice_reversal_approvals
                    WHERE invoice_reversal_request_id=? AND decision='approve'
                    """,
                    (reversal_id,),
                ).fetchall()
            }
            if approved == {"billing", "compliance"}:
                effect_key = f"invoice.reverse:{reversal_id}"
                connection.execute(
                    """
                    INSERT INTO invoice_provider_outbox(
                      invoice_provider_outbox_id,invoice_request_id,reversal_request_id,
                      command_type,effect_key,payload_sha256,status,attempt_count,lease_owner,
                      lease_token,lease_expires_at,safe_error_code,created_at,updated_at)
                    VALUES(?,?,?,'reverse_invoice',?,?,'pending',0,NULL,NULL,NULL,NULL,?,?)
                    """,
                    (
                        _id("ipo_"),
                        reversal["invoice_request_id"],
                        reversal_id,
                        effect_key,
                        _sha256(f"{reversal_id}\0{reversal['request_sha256']}"),
                        _db_time(now),
                        _db_time(now),
                    ),
                )
                connection.execute(
                    """
                    UPDATE invoice_reversal_requests SET status='queued',
                      state_revision=state_revision+1,updated_at=?
                    WHERE invoice_reversal_request_id=? AND status='pending_approvals'
                    """,
                    (_db_time(now), reversal_id),
                )
        updated = _reversal_row(connection, reversal_id)
        response = _reversal_response(connection, updated, request_id=request_id, trace_id=trace_id)
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("invoice.unavailable") from None
    finally:
        connection.close()


def create_reconciliation(
    config: AppConfig,
    *,
    platform_session_token: str,
    csrf_token: str,
    payload: ReconciliationCreateRequest,
    request_id: str,
    trace_id: str,
) -> ReconciliationResponse:
    """在同一读写事务中冻结订单、支付、Meter、Statement 和 Invoice 水位及摘要。"""

    connection = _connect(config, "reconciliation.unavailable")
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_platform_session_in_transaction(
            connection,
            config,
            session_token=platform_session_token,
            csrf_token=csrf_token,
            touch=False,
        )
        _actor_domains(actor)
        _require_schema(connection)
        order_watermark, order_hash = _source_snapshot(
            connection, "subscription_orders", "rowid", "amount_minor", "currency"
        )
        payment_watermark, payment_hash = _source_snapshot(
            connection, "payment_attempts", "rowid", "provider_event_version", "provider_status"
        )
        meter_watermark, meter_hash = _source_snapshot(
            connection,
            "platform_meter_readings",
            "source_sequence",
            "quantity",
            "source_owner_sha256",
        )
        statement_watermark, statement_hash = _source_snapshot(
            connection, "billing_statements", "rowid", "total_minor", "currency"
        )
        invoice_watermark, invoice_hash = _source_snapshot(
            connection, "invoice_requests", "rowid", "state_revision", "status"
        )
        now = _utcnow()
        run_id = _id("rec_")
        connection.execute(
            """
            INSERT INTO reconciliation_runs(
              reconciliation_run_id,scope,order_watermark,payment_watermark,meter_watermark,
              statement_watermark,invoice_watermark,order_source_sha256,payment_source_sha256,
              meter_source_sha256,statement_source_sha256,invoice_source_sha256,status,
              lease_owner,lease_token,lease_expires_at,generation,safe_error_code,
              created_by_operator_id,created_at,completed_at,updated_at)
            VALUES(?,'all',?,?,?,?,?,?,?,?,?,?,'queued',NULL,NULL,NULL,1,NULL,?,?,NULL,?)
            """,
            (
                run_id,
                order_watermark,
                payment_watermark,
                meter_watermark,
                statement_watermark,
                invoice_watermark,
                order_hash,
                payment_hash,
                meter_hash,
                statement_hash,
                invoice_hash,
                actor.operator_id,
                _db_time(now),
                _db_time(now),
            ),
        )
        row = _reconciliation_row(connection, run_id)
        response = ReconciliationResponse(
            reconciliation=_reconciliation(row), request_id=request_id, trace_id=trace_id
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("reconciliation.unavailable") from None
    finally:
        connection.close()


def list_reconciliation_differences(
    config: AppConfig,
    *,
    platform_session_token: str,
    run_id: str,
    limit: int,
    cursor: str | None,
    request_id: str,
    trace_id: str,
) -> ReconciliationDifferencesResponse:
    _validate_id(run_id, _RECONCILIATION_ID_RE, "reconciliation.not_found")
    if cursor is not None and _DIFFERENCE_ID_RE.fullmatch(cursor) is None:
        raise ApiError("reconciliation.request_invalid")
    connection = _connect(config, "reconciliation.unavailable")
    try:
        connection.execute("BEGIN")
        actor = authenticate_platform_session_in_transaction(
            connection, config, session_token=platform_session_token, touch=False
        )
        _actor_domains(actor)
        run = _reconciliation_row(connection, run_id)
        rows = connection.execute(
            """
            SELECT * FROM reconciliation_differences
            WHERE reconciliation_run_id=? AND reconciliation_difference_id>?
            ORDER BY reconciliation_difference_id LIMIT ?
            """,
            (run_id, cursor or "", limit + 1),
        ).fetchall()
        next_cursor = (
            str(rows[limit - 1]["reconciliation_difference_id"]) if len(rows) > limit else None
        )
        visible = rows[:limit]
        response = ReconciliationDifferencesResponse(
            reconciliation=_reconciliation(run),
            differences=tuple(_difference(row) for row in visible),
            next_cursor=next_cursor,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("reconciliation.unavailable") from None
    finally:
        connection.close()


def process_invoice_provider_once(config: AppConfig, *, worker_id: str) -> bool:
    """租约领取一个 provider 命令，在事务外生成产物，再幂等提交 receipt 与 artifact。"""

    claimed = _claim_invoice_outbox(config, worker_id)
    if claimed is None:
        return False
    outbox_id, lease_token = claimed
    connection = _connect(config, "invoice.unavailable")
    try:
        row = connection.execute(
            """
            SELECT outbox.*,invoice.workspace_id,invoice.tenant_id,invoice.provider,
              invoice.invoice_type,invoice.status invoice_status,statement.currency,
              statement.total_minor,statement.statement_sha256
            FROM invoice_provider_outbox outbox
            JOIN invoice_requests invoice ON invoice.invoice_request_id=outbox.invoice_request_id
            JOIN billing_statements statement
              ON statement.billing_statement_id=invoice.billing_statement_id
            WHERE outbox.invoice_provider_outbox_id=? AND outbox.lease_token=?
            """,
            (outbox_id, lease_token),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return True
    try:
        artifacts = _provider_artifacts(config, row)
        _commit_provider_result(config, row, lease_token, artifacts)
    except (
        ApiError,
        OSError,
        sqlite3.Error,
        BusinessVerificationCryptoError,
        KeyError,
        ValueError,
    ):
        _fail_invoice_outbox(config, outbox_id, lease_token)
    return True


def process_reconciliation_once(config: AppConfig, *, worker_id: str) -> bool:
    claimed = _claim_reconciliation(config, worker_id)
    if claimed is None:
        return False
    run_id, lease_token = claimed
    connection = _connect(config, "reconciliation.unavailable")
    try:
        connection.execute("BEGIN IMMEDIATE")
        run = connection.execute(
            """
            SELECT * FROM reconciliation_runs
            WHERE reconciliation_run_id=? AND status='processing' AND lease_token=?
            """,
            (run_id, lease_token),
        ).fetchone()
        if run is None:
            connection.rollback()
            return True
        now = _utcnow()
        _detect_order_differences(connection, run, now)
        _detect_statement_differences(connection, run, now)
        _detect_meter_differences(connection, run, now)
        _detect_invoice_differences(connection, run, now)
        connection.execute(
            """
            UPDATE reconciliation_runs SET status='completed',lease_owner=NULL,lease_token=NULL,
              lease_expires_at=NULL,completed_at=?,updated_at=?
            WHERE reconciliation_run_id=? AND status='processing' AND lease_token=?
            """,
            (_db_time(now), _db_time(now), run_id, lease_token),
        )
        connection.commit()
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        _fail_reconciliation(config, run_id, lease_token)
    finally:
        connection.close()
    return True


def _materialize_statements(connection: sqlite3.Connection, workspace_id: str) -> None:
    cycles = connection.execute(
        """
        SELECT * FROM platform_billing_cycles cycle
        WHERE cycle.workspace_id=? AND cycle.status IN('closed','reconciliation_required')
          AND cycle.summary_set_sha256 IS NOT NULL
          AND NOT EXISTS(SELECT 1 FROM billing_statements statement
            WHERE statement.billing_cycle_id=cycle.billing_cycle_id)
        ORDER BY cycle.period_ends_at,cycle.billing_cycle_id
        """,
        (workspace_id,),
    ).fetchall()
    for cycle in cycles:
        summaries = connection.execute(
            """
            SELECT COALESCE(SUM(amount_minor),0) amount_minor,
              COALESCE(MAX(source_watermark),0) source_watermark
            FROM platform_billing_summaries WHERE billing_cycle_id=?
            """,
            (cycle["billing_cycle_id"],),
        ).fetchone()
        adjustment_minor = int(
            connection.execute(
                """
                SELECT COALESCE(SUM(amount_delta_minor),0)
                FROM platform_billing_adjustments WHERE billing_cycle_id=?
                """,
                (cycle["billing_cycle_id"],),
            ).fetchone()[0]
        )
        subtotal_minor = (
            int(cycle["base_amount_minor"]) + int(summaries["amount_minor"]) + adjustment_minor
        )
        tax_rate = 600 if cycle["currency"] == "CNY" else 0
        tax_minor = _tax_minor(subtotal_minor, tax_rate)
        total_minor = subtotal_minor + tax_minor
        statement_id = _id("bst_")
        canonical = {
            "billing_cycle_id": cycle["billing_cycle_id"],
            "currency": cycle["currency"],
            "immutable_version": 1,
            "meter_summary_set_sha256": cycle["summary_set_sha256"],
            "source_watermark": max(
                int(cycle["source_watermark"]), int(summaries["source_watermark"])
            ),
            "subtotal_minor": subtotal_minor,
            "tax_minor": tax_minor,
            "tax_rate_basis_points": tax_rate,
            "total_minor": total_minor,
        }
        connection.execute(
            """
            INSERT INTO billing_statements(
              billing_statement_id,workspace_id,tenant_id,billing_cycle_id,statement_kind,
              predecessor_statement_id,immutable_version,meter_summary_set_sha256,
              source_watermark,currency,subtotal_minor,tax_rate_basis_points,tax_minor,total_minor,
              status,statement_sha256,issued_at)
            VALUES(?,?,?,?,'standard',NULL,1,?,?,?,?,?,?,?,'published',?,?)
            """,
            (
                statement_id,
                cycle["workspace_id"],
                cycle["tenant_id"],
                cycle["billing_cycle_id"],
                cycle["summary_set_sha256"],
                canonical["source_watermark"],
                cycle["currency"],
                subtotal_minor,
                tax_rate,
                tax_minor,
                total_minor,
                _canonical_sha256(canonical),
                _db_time(_utcnow()),
            ),
        )


def _claim_invoice_outbox(config: AppConfig, worker_id: str) -> tuple[str, str] | None:
    connection = _connect(config, "invoice.unavailable")
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _utcnow()
        row = connection.execute(
            """
            SELECT * FROM invoice_provider_outbox
            WHERE status='pending' OR(status='processing' AND lease_expires_at<=?)
            ORDER BY created_at,invoice_provider_outbox_id LIMIT 1
            """,
            (_db_time(now),),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        lease_token = secrets.token_hex(16)
        updated = connection.execute(
            """
            UPDATE invoice_provider_outbox SET status='processing',attempt_count=attempt_count+1,
              lease_owner=?,lease_token=?,lease_expires_at=?,safe_error_code=NULL,updated_at=?
            WHERE invoice_provider_outbox_id=? AND(
              status='pending' OR(status='processing' AND lease_expires_at<=?))
            """,
            (
                worker_id,
                lease_token,
                _db_time(now + timedelta(seconds=30)),
                _db_time(now),
                row["invoice_provider_outbox_id"],
                _db_time(now),
            ),
        ).rowcount
        if updated != 1:
            connection.rollback()
            return None
        if row["command_type"] == "issue_invoice":
            connection.execute(
                """
                UPDATE invoice_requests SET status='processing',state_revision=state_revision+1,
                  updated_at=? WHERE invoice_request_id=? AND status='requested'
                """,
                (_db_time(now), row["invoice_request_id"]),
            )
        connection.commit()
        return str(row["invoice_provider_outbox_id"]), lease_token
    except sqlite3.Error:
        _rollback(connection)
        return None
    finally:
        connection.close()


def _provider_artifacts(config: AppConfig, row: sqlite3.Row) -> tuple[dict[str, Any], ...]:
    reverse = row["command_type"] == "reverse_invoice"
    kinds = ("red_letter_pdf",) if reverse else ("invoice_pdf", "invoice_csv")
    issued_at = _utcnow()
    provider_event_id = "evt_" + hashlib.sha256(str(row["effect_key"]).encode()).hexdigest()[:32]
    provider_invoice_id = (
        "tax_" + hashlib.sha256(str(row["invoice_request_id"]).encode()).hexdigest()[:24]
    )
    artifacts: list[dict[str, Any]] = []
    for kind in kinds:
        artifact_id = (
            "ina_" + hashlib.sha256(f"{row['effect_key']}\0{kind}".encode()).hexdigest()[:32]
        )
        if kind == "invoice_csv":
            content = (
                "invoice_reference,currency,total_minor\r\n"
                f"{row['invoice_request_id']},{row['currency']},{int(row['total_minor'])}\r\n"
            ).encode()
            mime_type = "text/csv"
            extension = "csv"
        else:
            label = "RED LETTER" if reverse else "INVOICE"
            content = _minimal_pdf(
                f"MetaHub {label} {row['invoice_request_id']} "
                f"{row['currency']} {int(row['total_minor'])}"
            )
            mime_type = "application/pdf"
            extension = "pdf"
        reference = f"{row['provider']}/{artifact_id[4:6]}/{artifact_id}.{extension}"
        _write_artifact_object(config, reference, content)
        artifacts.append(
            {
                "artifact_id": artifact_id,
                "artifact_kind": kind,
                "content": content,
                "content_sha256": hashlib.sha256(content).hexdigest(),
                "mime_type": mime_type,
                "reference": reference,
                "provider_event_id": provider_event_id,
                "provider_invoice_id": provider_invoice_id,
                "issued_at": issued_at,
            }
        )
    return tuple(artifacts)


def _commit_provider_result(
    config: AppConfig,
    row: sqlite3.Row,
    lease_token: str,
    artifacts: tuple[dict[str, Any], ...],
) -> None:
    connection = _connect(config, "invoice.unavailable")
    try:
        connection.execute("BEGIN IMMEDIATE")
        live = connection.execute(
            """
            SELECT * FROM invoice_provider_outbox
            WHERE invoice_provider_outbox_id=? AND status='processing' AND lease_token=?
            """,
            (row["invoice_provider_outbox_id"], lease_token),
        ).fetchone()
        if live is None:
            connection.rollback()
            return
        first = artifacts[0]
        receipt_id = (
            "ipr_"
            + hashlib.sha256(
                f"{row['provider']}\0{first['provider_event_id']}".encode()
            ).hexdigest()[:32]
        )
        raw_hash = _canonical_sha256(
            {
                "event_id": first["provider_event_id"],
                "invoice_request_id": row["invoice_request_id"],
                "event_type": "invoice.reversed"
                if row["command_type"] == "reverse_invoice"
                else "invoice.issued",
                "artifact_hashes": [item["content_sha256"] for item in artifacts],
            }
        )
        existing_receipt = connection.execute(
            "SELECT * FROM invoice_provider_receipts WHERE provider=? AND provider_event_id=?",
            (row["provider"], first["provider_event_id"]),
        ).fetchone()
        if existing_receipt is not None and not hmac.compare_digest(
            str(existing_receipt["raw_body_sha256"]), raw_hash
        ):
            raise ApiError("invoice.conflict")
        if existing_receipt is None:
            connection.execute(
                """
                INSERT INTO invoice_provider_receipts(
                  invoice_provider_receipt_id,invoice_request_id,provider,provider_event_id,
                  provider_event_version,event_type,raw_body_sha256,received_at)
                VALUES(?,?,?,?,1,?,?,?)
                """,
                (
                    receipt_id,
                    row["invoice_request_id"],
                    row["provider"],
                    first["provider_event_id"],
                    "invoice.reversed"
                    if row["command_type"] == "reverse_invoice"
                    else "invoice.issued",
                    raw_hash,
                    _db_time(first["issued_at"]),
                ),
            )
        else:
            receipt_id = str(existing_receipt["invoice_provider_receipt_id"])
        predecessor = connection.execute(
            """
            SELECT invoice_artifact_id FROM invoice_artifacts
            WHERE invoice_request_id=? AND artifact_kind='invoice_pdf'
            ORDER BY issued_at DESC LIMIT 1
            """,
            (row["invoice_request_id"],),
        ).fetchone()
        for item in artifacts:
            existing = connection.execute(
                "SELECT * FROM invoice_artifacts WHERE invoice_artifact_id=?",
                (item["artifact_id"],),
            ).fetchone()
            if existing is not None:
                if not hmac.compare_digest(str(existing["content_sha256"]), item["content_sha256"]):
                    raise ApiError("invoice.conflict")
                continue
            encrypted_reference = _encrypt_reference(
                config,
                str(row["workspace_id"]),
                item["artifact_id"],
                item["reference"],
            )
            provider_token = _domain_hmac(
                config,
                "provider-invoice-id",
                f"{item['provider_invoice_id']}\0{item['artifact_kind']}",
            )
            connection.execute(
                """
                INSERT INTO invoice_artifacts(
                  invoice_artifact_id,invoice_request_id,workspace_id,tenant_id,
                  provider_receipt_id,predecessor_artifact_id,artifact_kind,
                  provider_invoice_id_token,invoice_number_masked,object_reference_ciphertext,
                  content_sha256,mime_type,size_bytes,kms_key_version,issued_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    item["artifact_id"],
                    row["invoice_request_id"],
                    row["workspace_id"],
                    row["tenant_id"],
                    receipt_id,
                    predecessor["invoice_artifact_id"]
                    if predecessor is not None and item["artifact_kind"] == "red_letter_pdf"
                    else None,
                    item["artifact_kind"],
                    provider_token,
                    "****" + str(item["provider_invoice_id"])[-4:],
                    encrypted_reference,
                    item["content_sha256"],
                    item["mime_type"],
                    len(item["content"]),
                    config.business_verification_key_version,
                    _db_time(item["issued_at"]),
                ),
            )
        now = _utcnow()
        terminal = "reversed" if row["command_type"] == "reverse_invoice" else "issued"
        connection.execute(
            """
            UPDATE invoice_requests SET status=?,state_revision=state_revision+1,updated_at=?
            WHERE invoice_request_id=? AND status=?
            """,
            (
                terminal,
                _db_time(now),
                row["invoice_request_id"],
                "reversal_pending" if terminal == "reversed" else "processing",
            ),
        )
        if row["reversal_request_id"] is not None:
            connection.execute(
                """
                UPDATE invoice_reversal_requests SET status='completed',
                  state_revision=state_revision+1,updated_at=?
                WHERE invoice_reversal_request_id=? AND status='queued'
                """,
                (_db_time(now), row["reversal_request_id"]),
            )
        connection.execute(
            """
            UPDATE invoice_provider_outbox SET status='completed',lease_owner=NULL,
              lease_token=NULL,lease_expires_at=NULL,safe_error_code=NULL,updated_at=?
            WHERE invoice_provider_outbox_id=? AND status='processing' AND lease_token=?
            """,
            (_db_time(now), row["invoice_provider_outbox_id"], lease_token),
        )
        connection.commit()
    except Exception:
        _rollback(connection)
        raise
    finally:
        connection.close()


def _fail_invoice_outbox(config: AppConfig, outbox_id: str, lease_token: str) -> None:
    connection = _connect(config, "invoice.unavailable")
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT * FROM invoice_provider_outbox
            WHERE invoice_provider_outbox_id=? AND lease_token=?
            """,
            (outbox_id, lease_token),
        ).fetchone()
        if row is not None:
            now = _db_time(_utcnow())
            connection.execute(
                """
                UPDATE invoice_provider_outbox
                SET status='failed',lease_owner=NULL,lease_token=NULL,
                  lease_expires_at=NULL,safe_error_code='invoice.storage_unavailable',updated_at=?
                WHERE invoice_provider_outbox_id=? AND status='processing' AND lease_token=?
                """,
                (now, outbox_id, lease_token),
            )
            if row["command_type"] == "issue_invoice":
                connection.execute(
                    """
                    UPDATE invoice_requests SET status='failed',state_revision=state_revision+1,
                      updated_at=? WHERE invoice_request_id=? AND status='processing'
                    """,
                    (now, row["invoice_request_id"]),
                )
        connection.commit()
    except sqlite3.Error:
        _rollback(connection)
    finally:
        connection.close()


def _claim_reconciliation(config: AppConfig, worker_id: str) -> tuple[str, str] | None:
    connection = _connect(config, "reconciliation.unavailable")
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _utcnow()
        row = connection.execute(
            """
            SELECT * FROM reconciliation_runs
            WHERE status='queued' OR(status='processing' AND lease_expires_at<=?)
            ORDER BY created_at,reconciliation_run_id LIMIT 1
            """,
            (_db_time(now),),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        lease_token = secrets.token_hex(16)
        connection.execute(
            """
            UPDATE reconciliation_runs SET status='processing',lease_owner=?,lease_token=?,
              lease_expires_at=?,generation=generation+1,updated_at=?
            WHERE reconciliation_run_id=?
            """,
            (
                worker_id,
                lease_token,
                _db_time(now + timedelta(seconds=60)),
                _db_time(now),
                row["reconciliation_run_id"],
            ),
        )
        connection.commit()
        return str(row["reconciliation_run_id"]), lease_token
    except sqlite3.Error:
        _rollback(connection)
        return None
    finally:
        connection.close()


def _detect_order_differences(
    connection: sqlite3.Connection, run: sqlite3.Row, now: datetime
) -> None:
    rows = connection.execute(
        """
        SELECT orders.order_id,orders.workspace_id,orders.tenant_id,orders.currency,
          orders.captured_amount_minor,
          MAX(CASE WHEN attempt.provider_status='succeeded' AND attempt.rowid<=?
            THEN intent.amount_minor END) provider_amount,
          MAX(CASE WHEN attempt.provider_status='succeeded' AND attempt.rowid<=?
            THEN intent.payment_intent_id END) payment_intent_id
        FROM subscription_orders orders
        LEFT JOIN payment_intents intent ON intent.order_id=orders.order_id
        LEFT JOIN payment_attempts attempt ON attempt.payment_intent_id=intent.payment_intent_id
        WHERE orders.rowid<=? AND orders.status IN(
          'paid','fulfilled','partially_refunded','refunded')
        GROUP BY orders.order_id
        """,
        (run["payment_watermark"], run["payment_watermark"], run["order_watermark"]),
    ).fetchall()
    for row in rows:
        provider_amount = row["provider_amount"]
        if provider_amount is None:
            _insert_difference(
                connection,
                run,
                row,
                "paid_order_missing_capture",
                "order",
                row["order_id"],
                None,
                None,
                int(row["captured_amount_minor"]),
                now,
            )
        elif int(provider_amount) != int(row["captured_amount_minor"]):
            _insert_difference(
                connection,
                run,
                row,
                "order_payment_amount_mismatch",
                "order",
                row["order_id"],
                "payment_intent",
                row["payment_intent_id"],
                int(row["captured_amount_minor"]) - int(provider_amount),
                now,
            )


def _detect_statement_differences(
    connection: sqlite3.Connection, run: sqlite3.Row, now: datetime
) -> None:
    rows = connection.execute(
        """
        SELECT cycle.* FROM platform_billing_cycles cycle
        LEFT JOIN billing_statements statement ON statement.billing_cycle_id=cycle.billing_cycle_id
          AND statement.rowid<=?
        WHERE cycle.status IN('closed','reconciliation_required')
          AND statement.billing_statement_id IS NULL
        """,
        (run["statement_watermark"],),
    ).fetchall()
    for row in rows:
        _insert_difference(
            connection,
            run,
            row,
            "closed_cycle_missing_statement",
            "billing_cycle",
            row["billing_cycle_id"],
            None,
            None,
            int(row["base_amount_minor"]),
            now,
        )


def _detect_meter_differences(
    connection: sqlite3.Connection, run: sqlite3.Row, now: datetime
) -> None:
    rows = connection.execute(
        """
        SELECT summary.*,cycle.currency FROM platform_billing_summaries summary
        JOIN platform_billing_cycles cycle ON cycle.billing_cycle_id=summary.billing_cycle_id
        WHERE summary.source_watermark>?
          OR summary.source_watermark!=(SELECT COALESCE(MAX(reading.source_sequence),0)
            FROM platform_meter_readings reading
            WHERE reading.billing_cycle_id=summary.billing_cycle_id
              AND reading.meter_definition_id=summary.meter_definition_id
              AND reading.source_sequence<=?)
        """,
        (run["meter_watermark"], run["meter_watermark"]),
    ).fetchall()
    for row in rows:
        _insert_difference(
            connection,
            run,
            row,
            "meter_summary_watermark_mismatch",
            "meter_summary",
            row["billing_summary_id"],
            None,
            None,
            int(row["amount_minor"]),
            now,
        )


def _detect_invoice_differences(
    connection: sqlite3.Connection, run: sqlite3.Row, now: datetime
) -> None:
    rows = connection.execute(
        """
        SELECT invoice.*,statement.currency,statement.total_minor
        FROM invoice_requests invoice
        JOIN billing_statements statement
          ON statement.billing_statement_id=invoice.billing_statement_id
        LEFT JOIN invoice_artifacts artifact
          ON artifact.invoice_request_id=invoice.invoice_request_id
        WHERE invoice.rowid<=? AND invoice.status IN('issued','reversed')
        GROUP BY invoice.invoice_request_id HAVING COUNT(artifact.invoice_artifact_id)=0
        """,
        (run["invoice_watermark"],),
    ).fetchall()
    for row in rows:
        _insert_difference(
            connection,
            run,
            row,
            "issued_invoice_missing_artifact",
            "invoice_request",
            row["invoice_request_id"],
            None,
            None,
            int(row["total_minor"]),
            now,
        )


def _insert_difference(
    connection: sqlite3.Connection,
    run: sqlite3.Row,
    owner: sqlite3.Row,
    difference_type: str,
    left_type: str,
    left_id: object,
    right_type: str | None,
    right_id: object,
    amount: int,
    now: datetime,
) -> None:
    difference_id = (
        "rcd_"
        + hashlib.sha256(
            f"{run['reconciliation_run_id']}\0{difference_type}\0{left_type}\0{left_id}".encode()
        ).hexdigest()[:32]
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO reconciliation_differences(
          reconciliation_difference_id,reconciliation_run_id,workspace_id,tenant_id,
          difference_type,left_source_type,left_source_id,right_source_type,right_source_id,
          currency,amount_difference_minor,status,adjustment_reference,state_revision,
          detected_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,? ,?,'open',NULL,1,?,?)
        """,
        (
            difference_id,
            run["reconciliation_run_id"],
            owner["workspace_id"],
            owner["tenant_id"],
            difference_type,
            left_type,
            str(left_id),
            right_type,
            None if right_id is None else str(right_id),
            owner["currency"],
            amount,
            _db_time(now),
            _db_time(now),
        ),
    )


def _fail_reconciliation(config: AppConfig, run_id: str, lease_token: str) -> None:
    connection = _connect(config, "reconciliation.unavailable")
    try:
        now = _db_time(_utcnow())
        connection.execute(
            """
            UPDATE reconciliation_runs SET status='failed',lease_owner=NULL,lease_token=NULL,
              lease_expires_at=NULL,safe_error_code='reconciliation.worker_unavailable',
              completed_at=?,updated_at=?
            WHERE reconciliation_run_id=? AND status='processing' AND lease_token=?
            """,
            (now, now, run_id, lease_token),
        )
        connection.commit()
    except sqlite3.Error:
        _rollback(connection)
    finally:
        connection.close()


def _statement(row: sqlite3.Row) -> BillingStatementInfo:
    return BillingStatementInfo(
        billing_statement_id=row["billing_statement_id"],
        billing_cycle_id=row["billing_cycle_id"],
        statement_kind=row["statement_kind"],
        immutable_version=int(row["immutable_version"]),
        currency=row["currency"],
        subtotal_minor=int(row["subtotal_minor"]),
        tax_rate_basis_points=int(row["tax_rate_basis_points"]),
        tax_minor=int(row["tax_minor"]),
        total_minor=int(row["total_minor"]),
        status=row["status"],
        issued_at=_parse_time(row["issued_at"]),
    )


def _invoice_response(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    idempotent_replay: bool = False,
    request_id: str,
    trace_id: str,
) -> InvoiceResponse:
    artifacts = connection.execute(
        """
        SELECT * FROM invoice_artifacts WHERE invoice_request_id=?
        ORDER BY issued_at,invoice_artifact_id
        """,
        (row["invoice_request_id"],),
    ).fetchall()
    return InvoiceResponse(
        invoice=InvoiceInfo(
            invoice_request_id=row["invoice_request_id"],
            billing_statement_id=row["billing_statement_id"],
            invoice_type=row["invoice_type"],
            status=row["status"],
            currency=row["currency"],
            total_minor=int(row["total_minor"]),
            billing_profile_revision=int(row["billing_profile_revision"]),
            state_revision=int(row["state_revision"]),
            artifacts=tuple(
                InvoiceArtifactInfo(
                    invoice_artifact_id=item["invoice_artifact_id"],
                    artifact_kind=item["artifact_kind"],
                    invoice_number_masked=item["invoice_number_masked"],
                    mime_type=item["mime_type"],
                    size_bytes=int(item["size_bytes"]),
                    content_sha256=item["content_sha256"],
                    issued_at=_parse_time(item["issued_at"]),
                    download_action=(
                        f"/invoices/{row['invoice_request_id']}/artifacts/"
                        f"{item['invoice_artifact_id']}/download-token"
                    ),
                )
                for item in artifacts
            ),
            requested_at=_parse_time(row["requested_at"]),
            updated_at=_parse_time(row["updated_at"]),
        ),
        idempotent_replay=idempotent_replay,
        request_id=request_id,
        trace_id=trace_id,
    )


def _invoice_row(connection: sqlite3.Connection, invoice_id: str, workspace_id: str) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT invoice.*,statement.currency,statement.total_minor
        FROM invoice_requests invoice
        JOIN billing_statements statement
          ON statement.billing_statement_id=invoice.billing_statement_id
        WHERE invoice.invoice_request_id=? AND invoice.workspace_id=?
        """,
        (invoice_id, workspace_id),
    ).fetchone()
    if row is None:
        raise ApiError("invoice.not_found")
    return row


def _reversal_row(connection: sqlite3.Connection, reversal_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM invoice_reversal_requests WHERE invoice_reversal_request_id=?",
        (reversal_id,),
    ).fetchone()
    if row is None:
        raise ApiError("invoice.not_found")
    return row


def _reversal_response(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    idempotent_replay: bool = False,
    request_id: str,
    trace_id: str,
) -> InvoiceReversalResponse:
    domains = tuple(
        str(item["approval_domain"])
        for item in connection.execute(
            """
            SELECT approval_domain FROM invoice_reversal_approvals
            WHERE invoice_reversal_request_id=? AND decision='approve'
            ORDER BY approval_domain
            """,
            (row["invoice_reversal_request_id"],),
        ).fetchall()
    )
    return InvoiceReversalResponse(
        reversal=InvoiceReversalInfo(
            invoice_reversal_request_id=row["invoice_reversal_request_id"],
            invoice_request_id=row["invoice_request_id"],
            reason_code=row["reason_code"],
            status=row["status"],
            approved_domains=domains,
            state_revision=int(row["state_revision"]),
            requested_at=_parse_time(row["requested_at"]),
            updated_at=_parse_time(row["updated_at"]),
        ),
        idempotent_replay=idempotent_replay,
        request_id=request_id,
        trace_id=trace_id,
    )


def _reconciliation_row(connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM reconciliation_runs WHERE reconciliation_run_id=?", (run_id,)
    ).fetchone()
    if row is None:
        raise ApiError("reconciliation.not_found")
    return row


def _reconciliation(row: sqlite3.Row) -> ReconciliationRunInfo:
    return ReconciliationRunInfo(
        reconciliation_run_id=row["reconciliation_run_id"],
        scope=row["scope"],
        order_watermark=int(row["order_watermark"]),
        payment_watermark=int(row["payment_watermark"]),
        meter_watermark=int(row["meter_watermark"]),
        statement_watermark=int(row["statement_watermark"]),
        invoice_watermark=int(row["invoice_watermark"]),
        status=row["status"],
        generation=int(row["generation"]),
        created_at=_parse_time(row["created_at"]),
        completed_at=None if row["completed_at"] is None else _parse_time(row["completed_at"]),
    )


def _difference(row: sqlite3.Row) -> ReconciliationDifferenceInfo:
    return ReconciliationDifferenceInfo(
        reconciliation_difference_id=row["reconciliation_difference_id"],
        difference_type=row["difference_type"],
        left_source_type=row["left_source_type"],
        left_source_id=row["left_source_id"],
        right_source_type=row["right_source_type"],
        right_source_id=row["right_source_id"],
        currency=row["currency"],
        amount_difference_minor=int(row["amount_difference_minor"]),
        status=row["status"],
        adjustment_reference=row["adjustment_reference"],
        detected_at=_parse_time(row["detected_at"]),
    )


def _source_snapshot(
    connection: sqlite3.Connection,
    table: str,
    watermark_column: str,
    numeric_column: str,
    text_column: str,
) -> tuple[int, str]:
    allowed = {
        ("subscription_orders", "rowid", "amount_minor", "currency"),
        ("payment_attempts", "rowid", "provider_event_version", "provider_status"),
        ("platform_meter_readings", "source_sequence", "quantity", "source_owner_sha256"),
        ("billing_statements", "rowid", "total_minor", "currency"),
        ("invoice_requests", "rowid", "state_revision", "status"),
    }
    if (table, watermark_column, numeric_column, text_column) not in allowed:
        raise ValueError("source invalid")
    row = connection.execute(
        f"""SELECT COUNT(*) row_count,COALESCE(MAX({watermark_column}),0) watermark,
          COALESCE(SUM({numeric_column}),0) numeric_total,
          COALESCE(MIN({text_column}),'') text_min,COALESCE(MAX({text_column}),'') text_max
          FROM {table}"""  # noqa: S608 - identifiers are selected from the closed tuple set above.
    ).fetchone()
    watermark = int(row["watermark"])
    return watermark, _canonical_sha256(
        {
            "source": table,
            "watermark": watermark,
            "row_count": int(row["row_count"]),
            "numeric_total": int(row["numeric_total"]),
            "text_min": row["text_min"],
            "text_max": row["text_max"],
        }
    )


def _actor_domains(actor: PlatformContext) -> tuple[str, ...]:
    domains: list[str] = []
    if "platform_billing" in actor.roles and "platform.billing.read" in actor.permissions:
        domains.append("billing")
    if "platform_compliance" in actor.roles and "platform.compliance.read" in actor.permissions:
        domains.append("compliance")
    if not domains:
        raise ApiError("platform.permission_denied")
    return tuple(domains)


def _require_finance(actor: WorkspaceSessionContext) -> None:
    if actor.membership_role not in _FINANCE_ROLES:
        raise ApiError("workspace_session.permission_denied")


def _require_schema(connection: sqlite3.Connection) -> None:
    count = int(
        connection.execute(
            """
            SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
              'billing_statements','invoice_requests','invoice_provider_outbox',
              'invoice_provider_receipts','invoice_artifacts','invoice_download_grants',
              'invoice_reversal_requests','invoice_reversal_approvals','reconciliation_runs',
              'reconciliation_differences')
            """
        ).fetchone()[0]
    )
    if count != 10:
        raise ApiError("invoice.unavailable")


def _encrypt_reference(config: AppConfig, workspace_id: str, artifact_id: str, value: str) -> str:
    return encrypt_business_field(
        value,
        secret=_business_secret(config),
        owner=BusinessFieldOwner(
            workspace_id=workspace_id,
            object_id=artifact_id,
            field_name="invoice_object_reference",
            key_version=config.business_verification_key_version,
        ),
    )


def _decrypt_reference(config: AppConfig, row: sqlite3.Row) -> str:
    return decrypt_business_field(
        row["object_reference_ciphertext"],
        secret=_business_secret(config),
        owner=BusinessFieldOwner(
            workspace_id=str(row["workspace_id"]),
            object_id=str(row["invoice_artifact_id"]),
            field_name="invoice_object_reference",
            key_version=int(row["kms_key_version"]),
        ),
    )


def _business_secret(config: AppConfig) -> str:
    secret = config.business_verification_encryption_secret
    if type(secret) is not str or not secret:
        raise ApiError("invoice.unavailable")
    return secret


def _artifact_root(config: AppConfig) -> Path:
    return (Path(config.payment_receipt_storage_root).parent / "invoice-artifacts").resolve()


def _artifact_path(config: AppConfig, reference: str) -> Path:
    if (
        re.fullmatch(r"(?:mocktax|cloudtax)/[0-9a-f]{2}/ina_[0-9a-f]{32}\.(?:pdf|csv)", reference)
        is None
    ):
        raise OSError("artifact reference invalid")
    root = _artifact_root(config)
    candidate = (root / reference).resolve()
    if root not in candidate.parents:
        raise OSError("artifact outside root")
    return candidate


def _write_artifact_object(config: AppConfig, reference: str, content: bytes) -> None:
    path = _artifact_path(config, reference)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        existing = _read_artifact_object(config, reference)
        if not hmac.compare_digest(
            hashlib.sha256(existing).digest(), hashlib.sha256(content).digest()
        ):
            raise OSError("artifact replay conflict") from None
        return
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_artifact_object(config: AppConfig, reference: str) -> bytes:
    path = _artifact_path(config, reference)
    if not path.is_file() or path.is_symlink():
        raise OSError("artifact missing")
    content = path.read_bytes()
    if not content or len(content) > 20_971_520:
        raise OSError("artifact invalid")
    return content


def _minimal_pdf(text: str) -> bytes:
    safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 10 Tf 36 756 Td ({safe}) Tj ET".encode("ascii")
    objects = [
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n",
        (
            b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >> endobj\n"
        ),
        b"4 0 obj << /Length "
        + str(len(stream)).encode()
        + b" >> stream\n"
        + stream
        + b"\nendstream endobj\n",
        b"5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for item in objects:
        offsets.append(len(output))
        output.extend(item)
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(output)


def _domain_hmac(config: AppConfig, domain: str, value: str) -> str:
    secret = _business_secret(config).encode()
    key = hmac.new(
        secret, f"metahub/invoice-reconciliation/v1/{domain}".encode(), hashlib.sha256
    ).digest()
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()


def _tax_minor(subtotal_minor: int, rate_basis_points: int) -> int:
    magnitude = (abs(subtotal_minor) * rate_basis_points + 5_000) // 10_000
    return magnitude if subtotal_minor >= 0 else -magnitude


def _validate_id(value: str, pattern: re.Pattern[str], code: str) -> None:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise ApiError(code)


def _validate_idempotency(value: str, code: str) -> None:
    if type(value) is not str or _IDEMPOTENCY_RE.fullmatch(value) is None:
        raise ApiError(code)


def _intent_hash(operation: str, payload: object) -> str:
    return _sha256(f"{operation}\0{json.dumps(payload, sort_keys=True, separators=(',', ':'))}")


def _canonical_sha256(value: object) -> str:
    return _sha256(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


def _connect(config: AppConfig, code: str) -> sqlite3.Connection:
    if not config.database_url:
        raise ApiError(code)
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError(code) from None


def _rollback(connection: sqlite3.Connection) -> None:
    try:
        connection.rollback()
    except sqlite3.Error:
        return


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_time(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("time invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("time invalid")
    return parsed.astimezone(UTC)


__all__ = [
    "approve_invoice_reversal",
    "consume_invoice_download",
    "create_invoice",
    "create_invoice_reversal",
    "create_reconciliation",
    "get_invoice",
    "issue_invoice_download_token",
    "list_billing_statements",
    "list_reconciliation_differences",
    "process_invoice_provider_once",
    "process_reconciliation_once",
]
