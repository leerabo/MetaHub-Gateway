"""Owner-qualified PRD-132 ticket, SLA, attachment, and escalation service."""

from __future__ import annotations

import hashlib
import io
import json
import secrets
import sqlite3
import zipfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal

from metahub_shared.customer_support import (
    SupportContentCryptoError,
    SupportContentOwner,
    decrypt_support_content,
    encrypt_support_content,
    support_reference_hmac,
)
from metahub_shared.db import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.files import (
    EncryptedLocalBlobStore,
    FileBlobOwner,
    FileContractError,
    decrypt_file_blob,
    file_content_fingerprint,
)
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
    AttachmentCreateRequest,
    AttachmentMutationResponse,
    EscalationCreateRequest,
    EscalationMutationResponse,
    EscalationSummary,
    SatisfactionSurveySummary,
    SlaClockSummary,
    SupportTicketSummary,
    TicketAttachmentSummary,
    TicketCreateRequest,
    TicketListResponse,
    TicketMessageSummary,
    TicketMutationResponse,
    TicketPatchRequest,
)

ActorDomain = Literal["workspace", "platform"]
_TERMINAL_TICKET_STATUSES = frozenset({"resolved", "closed"})
_TICKET_TRANSITIONS = {
    "open": frozenset({"open", "triaged", "waiting_customer", "waiting_platform", "resolved"}),
    "triaged": frozenset({"triaged", "waiting_customer", "waiting_platform", "resolved"}),
    "waiting_customer": frozenset({"waiting_customer", "triaged", "waiting_platform", "resolved"}),
    "waiting_platform": frozenset({"waiting_platform", "triaged", "waiting_customer", "resolved"}),
    "resolved": frozenset({"resolved", "open", "closed"}),
    "closed": frozenset({"closed"}),
}
_EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
_PDF_ACTIVE_MARKERS = (b"/JavaScript", b"/JS", b"/Launch", b"/OpenAction")


@dataclass(frozen=True, slots=True)
class _Actor:
    domain: ActorDomain
    workspace_id: str | None
    tenant_id: str | None
    account_id: str | None
    operator_id: str | None
    membership_id: str | None
    legacy_user_id: str | None
    permissions: tuple[str, ...]
    authorization_fingerprint: str

    @property
    def command_domain(self) -> str:
        return "customer" if self.domain == "workspace" else "platform"

    @property
    def identity(self) -> str:
        value = self.account_id if self.domain == "workspace" else self.operator_id
        if value is None:
            raise ApiError("support.unavailable")
        return value


def list_tickets(
    config: AppConfig,
    *,
    workspace_session_token: str | None,
    platform_session_token: str | None,
    limit: int,
    status_filter: str | None,
    severity_filter: str | None,
    workspace_filter: str | None,
    request_id: str,
    trace_id: str,
) -> TicketListResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = _authenticate(
            connection,
            config,
            workspace_token=workspace_session_token,
            platform_token=platform_session_token,
        )
        conditions: list[str] = []
        parameters: list[object] = []
        if actor.domain == "workspace":
            _require(actor, "support.ticket.read")
            conditions.append("workspace_id=?")
            parameters.append(actor.workspace_id)
            if "support.ticket.manage" not in actor.permissions:
                conditions.append("reporter_account_id=?")
                parameters.append(actor.account_id)
            if workspace_filter is not None and workspace_filter != actor.workspace_id:
                raise ApiError("support.not_found")
        else:
            _require(actor, "platform.support.ticket.read")
            if workspace_filter is not None:
                conditions.append("workspace_id=?")
                parameters.append(workspace_filter)
        if status_filter is not None:
            conditions.append("status=?")
            parameters.append(status_filter)
        if severity_filter is not None:
            conditions.append("severity=?")
            parameters.append(severity_filter)
        where = " AND ".join(conditions) if conditions else "1=1"
        rows = connection.execute(
            f"""SELECT * FROM support_tickets WHERE {where}
                ORDER BY CASE severity WHEN 'p1' THEN 1 WHEN 'p2' THEN 2
                         WHEN 'p3' THEN 3 ELSE 4 END,
                         updated_at DESC,support_ticket_id DESC LIMIT ?""",
            (*parameters, limit),
        ).fetchall()
        items = tuple(_ticket(connection, config, row, actor) for row in rows)
        connection.commit()
        return TicketListResponse(
            actor_domain=actor.domain,
            items=items,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, SupportContentCryptoError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("support.unavailable") from None
    finally:
        connection.close()


def create_ticket(
    config: AppConfig,
    payload: TicketCreateRequest,
    *,
    session_token: str,
    csrf_token: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> TicketMutationResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _authenticate(
            connection,
            config,
            workspace_token=session_token,
            workspace_csrf=csrf_token,
        )
        _require(actor, "support.ticket.create")
        request_hash = _request_hash("support.ticket.create", payload.model_dump(mode="json"))
        replay = _command_replay(
            connection, actor, "support.ticket.create", idempotency_key, request_hash
        )
        if replay is not None:
            row = _ticket_row(connection, replay, actor)
            connection.commit()
            return TicketMutationResponse(
                actor_domain="workspace",
                ticket=_ticket(connection, config, row, actor),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )

        now = _utcnow()
        now_db = _db_time(now)
        ticket_id = _id("tkt_")
        message_id = _id("msg_")
        policy = connection.execute(
            "SELECT * FROM support_sla_policies WHERE severity=? AND policy_version=1",
            (payload.severity,),
        ).fetchone()
        if policy is None or actor.workspace_id is None or actor.tenant_id is None:
            raise ApiError("support.unavailable")
        secret = _secret(config)
        subject = encrypt_support_content(
            payload.subject,
            secret=secret,
            owner=_content_owner(
                actor.workspace_id, actor.tenant_id, "ticket", ticket_id, "subject"
            ),
        )
        body = encrypt_support_content(
            payload.message,
            secret=secret,
            owner=_content_owner(
                actor.workspace_id, actor.tenant_id, "message", message_id, "body"
            ),
        )
        related_hmac = (
            None
            if payload.related_kind == "none"
            else support_reference_hmac(
                payload.related_reference,
                secret=secret,
                workspace_id=actor.workspace_id,
                tenant_id=actor.tenant_id,
                reference_kind=payload.related_kind,
            )
        )
        connection.execute(
            """INSERT INTO support_tickets(
                 support_ticket_id,workspace_id,tenant_id,reporter_account_id,
                 reporter_membership_id,category,severity,status,subject_ciphertext,
                 subject_key_version,related_kind,related_reference_hmac,
                 platform_assignee_operator_id,sla_policy_version,
                 authorization_fingerprint_sha256,revision,last_customer_activity_at,
                 last_platform_activity_at,resolved_at,closed_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,'open',?,1,?,?,NULL,1,?,1,?,NULL,NULL,NULL,?,?)""",
            (
                ticket_id,
                actor.workspace_id,
                actor.tenant_id,
                actor.account_id,
                actor.membership_id,
                payload.category,
                payload.severity,
                subject,
                payload.related_kind,
                related_hmac,
                actor.authorization_fingerprint,
                now_db,
                now_db,
                now_db,
            ),
        )
        connection.execute(
            """INSERT INTO support_ticket_messages(
                 ticket_message_id,workspace_id,tenant_id,support_ticket_id,
                 author_domain,author_account_id,author_operator_id,visibility,
                 body_ciphertext,body_key_version,created_at)
               VALUES(?,?,?,?,'customer',?,NULL,'customer',?,1,?)""",
            (
                message_id,
                actor.workspace_id,
                actor.tenant_id,
                ticket_id,
                actor.account_id,
                body,
                now_db,
            ),
        )
        first_deadline = now + timedelta(seconds=int(policy["first_response_seconds"]))
        resolution_deadline = now + timedelta(seconds=int(policy["resolution_seconds"]))
        risk_at = first_deadline - timedelta(seconds=int(policy["risk_lead_seconds"]))
        clock_id = _id("slc_")
        connection.execute(
            """INSERT INTO support_sla_clocks(
                 sla_clock_id,workspace_id,tenant_id,support_ticket_id,state,generation,
                 first_response_deadline_at,resolution_deadline_at,risk_at,
                 first_responded_at,paused_at,stopped_at,risk_generation,
                 escalation_generation,revision,created_at,updated_at)
               VALUES(?,?,?,?,'running',1,?,?,?,NULL,NULL,NULL,0,0,1,?,?)""",
            (
                clock_id,
                actor.workspace_id,
                actor.tenant_id,
                ticket_id,
                _db_time(first_deadline),
                _db_time(resolution_deadline),
                _db_time(risk_at),
                now_db,
                now_db,
            ),
        )
        _clock_event(
            connection,
            actor.workspace_id,
            actor.tenant_id,
            clock_id,
            ticket_id,
            "started",
            1,
            1,
            now_db,
        )
        _event(
            connection,
            workspace_id=actor.workspace_id,
            tenant_id=actor.tenant_id,
            ticket_id=ticket_id,
            event_type="support.ticket_created",
            source_object_id=ticket_id,
            source_revision=1,
            actor_domain="customer",
            metadata={"category": payload.category, "severity": payload.severity, "status": "open"},
            request_id=request_id,
            trace_id=trace_id,
            occurred_at=now_db,
            delivery_domains=("notification", "webhook"),
        )
        _event(
            connection,
            workspace_id=actor.workspace_id,
            tenant_id=actor.tenant_id,
            ticket_id=ticket_id,
            event_type="support.message_created",
            source_object_id=message_id,
            source_revision=1,
            actor_domain="customer",
            metadata={"visibility": "customer"},
            request_id=request_id,
            trace_id=trace_id,
            occurred_at=now_db,
        )
        _record_command(
            connection,
            actor,
            "support.ticket.create",
            idempotency_key,
            request_hash,
            ticket_id,
            now_db,
        )
        row = _ticket_row(connection, ticket_id, actor)
        result = _ticket(connection, config, row, actor)
        connection.commit()
        return TicketMutationResponse(
            actor_domain="workspace",
            ticket=result,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("support.state_conflict") from None
    except (sqlite3.Error, DbError, SupportContentCryptoError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("support.unavailable") from None
    finally:
        connection.close()


def patch_ticket(
    config: AppConfig,
    ticket_id: str,
    payload: TicketPatchRequest,
    *,
    workspace_session_token: str | None,
    platform_session_token: str | None,
    workspace_csrf_token: str | None,
    platform_csrf_token: str | None,
    expected_revision: int,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> TicketMutationResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _authenticate(
            connection,
            config,
            workspace_token=workspace_session_token,
            platform_token=platform_session_token,
            workspace_csrf=workspace_csrf_token,
            platform_csrf=platform_csrf_token,
        )
        row = _ticket_row(connection, ticket_id, actor)
        actor = _bind_actor_owner(actor, row)
        _authorize_ticket_mutation(actor, row, payload)
        request_hash = _request_hash(
            "support.ticket.patch",
            {"expected_revision": expected_revision, **payload.model_dump(mode="json")},
        )
        replay = _command_replay(
            connection, actor, "support.ticket.patch", idempotency_key, request_hash
        )
        if replay is not None:
            current = _ticket_row(connection, ticket_id, actor)
            result = _ticket(connection, config, current, actor)
            connection.commit()
            return TicketMutationResponse(
                actor_domain=actor.domain,
                ticket=result,
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
        if int(row["revision"]) != expected_revision:
            raise ApiError("support.revision_conflict")

        old_status = str(row["status"])
        next_status = payload.status or old_status
        if payload.message is not None and payload.status is None:
            if actor.domain == "workspace" and old_status == "waiting_customer":
                next_status = "waiting_platform"
            elif (
                actor.domain == "platform"
                and payload.message_visibility == "customer"
                and old_status in {"open", "triaged", "waiting_platform"}
            ):
                next_status = "waiting_customer"
        if next_status not in _TICKET_TRANSITIONS[old_status]:
            raise ApiError("support.state_conflict")
        _authorize_workspace_status(actor, old_status, next_status)

        now = _mutation_time(_utcnow(), str(row["updated_at"]))
        now_db = _db_time(now)
        new_revision = expected_revision + 1
        resolved_at = row["resolved_at"]
        closed_at = row["closed_at"]
        if next_status == "resolved" and old_status != "resolved":
            resolved_at = now_db
            closed_at = None
        elif next_status == "closed":
            resolved_at = resolved_at or now_db
            closed_at = now_db
        elif old_status == "resolved" and next_status == "open":
            resolved_at = None
            closed_at = None
        assignee = row["platform_assignee_operator_id"]
        if payload.assignee_operator_id is not None:
            if (
                connection.execute(
                    "SELECT 1 FROM platform_operators WHERE operator_id=? AND status='active'",
                    (payload.assignee_operator_id,),
                ).fetchone()
                is None
            ):
                raise ApiError("support.not_found")
            assignee = payload.assignee_operator_id
        last_customer = row["last_customer_activity_at"]
        last_platform = row["last_platform_activity_at"]
        if actor.domain == "workspace":
            last_customer = now_db
        else:
            last_platform = now_db
        connection.execute(
            """UPDATE support_tickets
               SET status=?,platform_assignee_operator_id=?,revision=?,
                   last_customer_activity_at=?,last_platform_activity_at=?,
                   resolved_at=?,closed_at=?,updated_at=?
               WHERE support_ticket_id=? AND revision=?""",
            (
                next_status,
                assignee,
                new_revision,
                last_customer,
                last_platform,
                resolved_at,
                closed_at,
                now_db,
                ticket_id,
                expected_revision,
            ),
        )
        if connection.total_changes < 1:
            raise ApiError("support.revision_conflict")

        message_id: str | None = None
        if payload.message is not None:
            message_id = _id("msg_")
            body = encrypt_support_content(
                payload.message,
                secret=_secret(config),
                owner=_content_owner(
                    str(row["workspace_id"]),
                    str(row["tenant_id"]),
                    "message",
                    message_id,
                    "body",
                ),
            )
            connection.execute(
                """INSERT INTO support_ticket_messages(
                     ticket_message_id,workspace_id,tenant_id,support_ticket_id,
                     author_domain,author_account_id,author_operator_id,visibility,
                     body_ciphertext,body_key_version,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,1,?)""",
                (
                    message_id,
                    row["workspace_id"],
                    row["tenant_id"],
                    ticket_id,
                    "customer" if actor.domain == "workspace" else "platform",
                    actor.account_id,
                    actor.operator_id,
                    payload.message_visibility,
                    body,
                    now_db,
                ),
            )

        _update_clock_for_patch(
            connection,
            row,
            payload,
            actor,
            old_status=old_status,
            next_status=next_status,
            ticket_revision=new_revision,
            now=now,
        )
        if payload.survey_rating is not None:
            _insert_survey(connection, config, row, actor, payload, now_db)
        if message_id is not None:
            _event(
                connection,
                workspace_id=str(row["workspace_id"]),
                tenant_id=str(row["tenant_id"]),
                ticket_id=ticket_id,
                event_type="support.message_created",
                source_object_id=message_id,
                source_revision=1,
                actor_domain=actor.command_domain,
                metadata={"visibility": payload.message_visibility},
                request_id=request_id,
                trace_id=trace_id,
                occurred_at=now_db,
                delivery_domains=("notification",)
                if actor.domain == "platform" and payload.message_visibility == "customer"
                else (),
            )
        event_type = (
            "support.ticket_resolved"
            if next_status == "resolved" and old_status != "resolved"
            else "support.ticket_reopened"
            if old_status == "resolved" and next_status == "open"
            else "support.ticket_updated"
        )
        _event(
            connection,
            workspace_id=str(row["workspace_id"]),
            tenant_id=str(row["tenant_id"]),
            ticket_id=ticket_id,
            event_type=event_type,
            source_object_id=ticket_id,
            source_revision=new_revision,
            actor_domain=actor.command_domain,
            metadata={"status": next_status},
            request_id=request_id,
            trace_id=trace_id,
            occurred_at=now_db,
            delivery_domains=("notification", "webhook")
            if event_type == "support.ticket_resolved"
            else (),
        )
        _record_command(
            connection,
            actor,
            "support.ticket.patch",
            idempotency_key,
            request_hash,
            ticket_id,
            now_db,
        )
        current = _ticket_row(connection, ticket_id, actor)
        result = _ticket(connection, config, current, actor)
        connection.commit()
        return TicketMutationResponse(
            actor_domain=actor.domain,
            ticket=result,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("support.state_conflict") from None
    except (sqlite3.Error, DbError, SupportContentCryptoError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("support.unavailable") from None
    finally:
        connection.close()


def create_attachment(
    config: AppConfig,
    ticket_id: str,
    payload: AttachmentCreateRequest,
    *,
    session_token: str,
    csrf_token: str,
    expected_revision: int,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> AttachmentMutationResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _authenticate(
            connection,
            config,
            workspace_token=session_token,
            workspace_csrf=csrf_token,
        )
        row = _ticket_row(connection, ticket_id, actor)
        actor = _bind_actor_owner(actor, row)
        _require(actor, "support.ticket.create")
        _require_participant(actor, row)
        request_hash = _request_hash(
            "support.attachment.create",
            {"expected_revision": expected_revision, **payload.model_dump(mode="json")},
        )
        replay = _command_replay(
            connection, actor, "support.attachment.create", idempotency_key, request_hash
        )
        if replay is not None:
            attachment = _attachment_row(connection, replay, ticket_id)
            connection.commit()
            return AttachmentMutationResponse(
                attachment=_attachment(attachment),
                ticket_revision=int(
                    connection.execute(
                        "SELECT revision FROM support_tickets WHERE support_ticket_id=?",
                        (ticket_id,),
                    ).fetchone()[0]
                ),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
        if int(row["revision"]) != expected_revision:
            raise ApiError("support.revision_conflict")
        if str(row["status"]) == "closed":
            raise ApiError("support.state_conflict")
        source = connection.execute(
            """SELECT file_id,tenant_id,uploaded_by,media_type,size_bytes,content_fingerprint,
                      status,dlp_scan_status,dlp_action,storage_backend,original_object_key
                 FROM uploaded_files WHERE tenant_id=? AND file_id=?""",
            (row["tenant_id"], payload.file_id),
        ).fetchone()
        if source is None:
            raise ApiError("support.attachment_not_found")
        if (
            source["status"] != "ready"
            or source["dlp_scan_status"] not in {"clean", "matched"}
            or source["dlp_action"] == "block"
            or source["storage_backend"] != "local_encrypted_v1"
            or not source["original_object_key"]
            or (
                "support.ticket.manage" not in actor.permissions
                and source["uploaded_by"] != actor.legacy_user_id
            )
        ):
            raise ApiError("support.attachment_not_found")

        now = _mutation_time(_utcnow(), str(row["updated_at"]))
        now_db = _db_time(now)
        attachment_id = _id("att_")
        job_id = _id("asj_")
        reference = support_reference_hmac(
            payload.file_id,
            secret=_secret(config),
            workspace_id=row["workspace_id"],
            tenant_id=row["tenant_id"],
            reference_kind="uploaded_file",
        )
        timeout_at = _db_time(now + timedelta(minutes=5))
        connection.execute(
            """INSERT INTO support_ticket_attachments(
                 ticket_attachment_id,workspace_id,tenant_id,support_ticket_id,
                 uploaded_by_account_id,source_file_id,source_reference_hmac,
                 source_fingerprint,intent_code,media_type,size_bytes,status,
                 scan_version,scan_outcome,download_generation,revision,
                 scan_timeout_at,scanned_at,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,'quarantined','ticket-attachment-scan-v1',
                      NULL,0,1,?,NULL,?,?)""",
            (
                attachment_id,
                row["workspace_id"],
                row["tenant_id"],
                ticket_id,
                actor.account_id,
                payload.file_id,
                reference,
                source["content_fingerprint"],
                payload.intent_code,
                source["media_type"],
                source["size_bytes"],
                timeout_at,
                now_db,
                now_db,
            ),
        )
        connection.execute(
            """INSERT INTO support_attachment_scan_jobs(
                 attachment_scan_job_id,workspace_id,tenant_id,ticket_attachment_id,
                 expected_attachment_revision,expected_source_fingerprint,status,
                 attempt_count,next_attempt_at,last_error_code,completed_at,created_at,updated_at)
               VALUES(?,?,?,?,1,?,'pending',0,?,NULL,NULL,?,?)""",
            (
                job_id,
                row["workspace_id"],
                row["tenant_id"],
                attachment_id,
                source["content_fingerprint"],
                now_db,
                now_db,
                now_db,
            ),
        )
        new_revision = expected_revision + 1
        connection.execute(
            """UPDATE support_tickets
               SET revision=?,last_customer_activity_at=?,updated_at=?
               WHERE support_ticket_id=? AND revision=?""",
            (new_revision, now_db, now_db, ticket_id, expected_revision),
        )
        _event(
            connection,
            workspace_id=str(row["workspace_id"]),
            tenant_id=str(row["tenant_id"]),
            ticket_id=ticket_id,
            event_type="support.attachment_quarantined",
            source_object_id=attachment_id,
            source_revision=1,
            actor_domain="customer",
            metadata={"intent_code": payload.intent_code, "status": "quarantined"},
            request_id=request_id,
            trace_id=trace_id,
            occurred_at=now_db,
        )
        _record_command(
            connection,
            actor,
            "support.attachment.create",
            idempotency_key,
            request_hash,
            attachment_id,
            now_db,
        )
        result = _attachment(_attachment_row(connection, attachment_id, ticket_id))
        connection.commit()
        return AttachmentMutationResponse(
            attachment=result,
            ticket_revision=new_revision,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("support.state_conflict") from None
    except (sqlite3.Error, DbError, SupportContentCryptoError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("support.attachment_unavailable") from None
    finally:
        connection.close()


def create_escalation(
    config: AppConfig,
    ticket_id: str,
    payload: EscalationCreateRequest,
    *,
    workspace_session_token: str | None,
    platform_session_token: str | None,
    workspace_csrf_token: str | None,
    platform_csrf_token: str | None,
    expected_revision: int,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> EscalationMutationResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _authenticate(
            connection,
            config,
            workspace_token=workspace_session_token,
            platform_token=platform_session_token,
            workspace_csrf=workspace_csrf_token,
            platform_csrf=platform_csrf_token,
        )
        row = _ticket_row(connection, ticket_id, actor)
        actor = _bind_actor_owner(actor, row)
        if actor.domain == "workspace":
            _require(actor, "support.ticket.create")
            _require_participant(actor, row)
        else:
            _require(actor, "platform.support.ticket.manage")
        request_hash = _request_hash(
            "support.escalation.create",
            {"expected_revision": expected_revision, **payload.model_dump(mode="json")},
        )
        replay = _command_replay(
            connection, actor, "support.escalation.create", idempotency_key, request_hash
        )
        if replay is not None:
            escalation = _escalation_row(connection, replay, ticket_id)
            connection.commit()
            return EscalationMutationResponse(
                escalation=_escalation(escalation),
                ticket_revision=int(
                    connection.execute(
                        "SELECT revision FROM support_tickets WHERE support_ticket_id=?",
                        (ticket_id,),
                    ).fetchone()[0]
                ),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
        if int(row["revision"]) != expected_revision:
            raise ApiError("support.revision_conflict")
        if row["status"] in _TERMINAL_TICKET_STATUSES:
            raise ApiError("support.state_conflict")
        now = _mutation_time(_utcnow(), str(row["updated_at"]))
        now_db = _db_time(now)
        escalation_id = _id("esc_")
        generation = int(
            connection.execute(
                """SELECT coalesce(max(generation),0)+1
                     FROM support_escalations WHERE support_ticket_id=?""",
                (ticket_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """INSERT INTO support_escalations(
                 escalation_id,workspace_id,tenant_id,support_ticket_id,source_code,
                 reason_code,generation,requested_by_domain,requested_by_account_id,
                 requested_by_operator_id,status,created_at,acknowledged_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,'open',?,NULL)""",
            (
                escalation_id,
                row["workspace_id"],
                row["tenant_id"],
                ticket_id,
                "customer_requested" if actor.domain == "workspace" else "platform_manual",
                payload.reason_code,
                generation,
                "customer" if actor.domain == "workspace" else "platform",
                actor.account_id,
                actor.operator_id,
                now_db,
            ),
        )
        new_revision = expected_revision + 1
        connection.execute(
            """UPDATE support_tickets SET revision=?,
                 last_customer_activity_at=CASE WHEN ?='workspace' THEN ?
                   ELSE last_customer_activity_at END,
                 last_platform_activity_at=CASE WHEN ?='platform' THEN ?
                   ELSE last_platform_activity_at END,
                 updated_at=? WHERE support_ticket_id=? AND revision=?""",
            (
                new_revision,
                actor.domain,
                now_db,
                actor.domain,
                now_db,
                now_db,
                ticket_id,
                expected_revision,
            ),
        )
        _event(
            connection,
            workspace_id=str(row["workspace_id"]),
            tenant_id=str(row["tenant_id"]),
            ticket_id=ticket_id,
            event_type="support.escalation_created",
            source_object_id=escalation_id,
            source_revision=1,
            actor_domain=actor.command_domain,
            metadata={
                "generation": generation,
                "source_code": (
                    "customer_requested" if actor.domain == "workspace" else "platform_manual"
                ),
            },
            request_id=request_id,
            trace_id=trace_id,
            occurred_at=now_db,
            delivery_domains=("notification", "webhook"),
        )
        _record_command(
            connection,
            actor,
            "support.escalation.create",
            idempotency_key,
            request_hash,
            escalation_id,
            now_db,
        )
        result = _escalation(_escalation_row(connection, escalation_id, ticket_id))
        connection.commit()
        return EscalationMutationResponse(
            escalation=result,
            ticket_revision=new_revision,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("support.state_conflict") from None
    except (sqlite3.Error, DbError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("support.unavailable") from None
    finally:
        connection.close()


def process_support_sla_once(
    config: AppConfig,
    *,
    worker_id: str,
    now: datetime | None = None,
    limit: int = 50,
) -> int:
    checked_now = (now or _utcnow()).astimezone(UTC)
    connection = _connect(config)
    lease_token = secrets.token_urlsafe(32)
    lease_hash = hashlib.sha256(lease_token.encode("ascii")).hexdigest()
    try:
        connection.execute("BEGIN IMMEDIATE")
        state = connection.execute(
            "SELECT * FROM support_scheduler_state WHERE singleton_id=1"
        ).fetchone()
        if state is None:
            raise ApiError("support.unavailable")
        if (
            state["lease_owner_id"] is not None
            and _time(str(state["lease_expires_at"])) > checked_now
            and state["lease_owner_id"] != worker_id
        ):
            connection.rollback()
            return 0
        acquired_at = _mutation_time(checked_now, str(state["updated_at"]))
        fencing = int(state["fencing_generation"]) + 1
        connection.execute(
            """UPDATE support_scheduler_state
               SET lease_owner_id=?,lease_token_sha256=?,lease_expires_at=?,
                   fencing_generation=?,revision=revision+1,updated_at=? WHERE singleton_id=1""",
            (
                worker_id,
                lease_hash,
                _db_time(acquired_at + timedelta(seconds=30)),
                fencing,
                _db_time(acquired_at),
            ),
        )
        connection.commit()

        connection.execute("BEGIN IMMEDIATE")
        live = connection.execute(
            "SELECT * FROM support_scheduler_state WHERE singleton_id=1"
        ).fetchone()
        if (
            live is None
            or live["lease_owner_id"] != worker_id
            or live["lease_token_sha256"] != lease_hash
            or int(live["fencing_generation"]) != fencing
        ):
            raise ApiError("support.unavailable")
        due = connection.execute(
            """SELECT clock.*,ticket.status ticket_status,ticket.revision ticket_revision,
                      ticket.severity,ticket.updated_at ticket_updated_at
                 FROM support_sla_clocks clock
                 JOIN support_tickets ticket ON ticket.support_ticket_id=clock.support_ticket_id
                WHERE clock.state='running' AND clock.risk_at<=?
                  AND ticket.status NOT IN('resolved','closed')
                  AND clock.risk_generation<clock.generation
                  AND clock.escalation_generation<clock.generation
                ORDER BY clock.risk_at,clock.sla_clock_id LIMIT ?""",
            (_db_time(checked_now), limit),
        ).fetchall()
        handled = 0
        for clock in due:
            event_time = _mutation_time(checked_now, str(clock["updated_at"]))
            event_time_db = _db_time(event_time)
            risk_generation = int(clock["generation"])
            clock_revision = int(clock["revision"]) + 1
            connection.execute(
                """UPDATE support_sla_clocks
                   SET risk_generation=?,escalation_generation=?,revision=?,updated_at=?
                   WHERE sla_clock_id=? AND revision=? AND state='running'
                     AND risk_generation<generation AND escalation_generation<generation""",
                (
                    risk_generation,
                    risk_generation,
                    clock_revision,
                    event_time_db,
                    clock["sla_clock_id"],
                    clock["revision"],
                ),
            )
            generation = int(
                connection.execute(
                    """SELECT coalesce(max(generation),0)+1
                         FROM support_escalations WHERE support_ticket_id=?""",
                    (clock["support_ticket_id"],),
                ).fetchone()[0]
            )
            escalation_id = _id("esc_")
            breached = _time(str(clock["resolution_deadline_at"])) <= checked_now
            source_code = "sla_breached" if breached else "sla_at_risk"
            connection.execute(
                """INSERT INTO support_escalations(
                     escalation_id,workspace_id,tenant_id,support_ticket_id,source_code,
                     reason_code,generation,requested_by_domain,requested_by_account_id,
                     requested_by_operator_id,status,created_at,acknowledged_at)
                   VALUES(?,?,?,?,?,?,?,'scheduler',NULL,NULL,'open',?,NULL)""",
                (
                    escalation_id,
                    clock["workspace_id"],
                    clock["tenant_id"],
                    clock["support_ticket_id"],
                    source_code,
                    source_code,
                    generation,
                    event_time_db,
                ),
            )
            for event_type in ("at_risk", "escalated"):
                _clock_event(
                    connection,
                    str(clock["workspace_id"]),
                    str(clock["tenant_id"]),
                    str(clock["sla_clock_id"]),
                    str(clock["support_ticket_id"]),
                    event_type,
                    int(clock["generation"]),
                    clock_revision,
                    event_time_db,
                )
            _event(
                connection,
                workspace_id=str(clock["workspace_id"]),
                tenant_id=str(clock["tenant_id"]),
                ticket_id=str(clock["support_ticket_id"]),
                event_type="support.sla_at_risk",
                source_object_id=str(clock["support_ticket_id"]),
                source_revision=int(clock["ticket_revision"]),
                actor_domain="scheduler",
                metadata={
                    "escalation_generation": generation,
                    "severity": str(clock["severity"]),
                    "source_code": source_code,
                },
                request_id="support-scheduler",
                trace_id=f"support-sla-{fencing}",
                occurred_at=event_time_db,
                delivery_domains=("notification", "webhook"),
            )
            handled += 1
        released_at = _mutation_time(checked_now, str(live["updated_at"]))
        connection.execute(
            """UPDATE support_scheduler_state
               SET watermark_at=?,lease_owner_id=NULL,lease_token_sha256=NULL,
                   lease_expires_at=NULL,revision=revision+1,updated_at=?
               WHERE singleton_id=1 AND lease_owner_id=? AND lease_token_sha256=?
                 AND fencing_generation=?""",
            (_db_time(checked_now), _db_time(released_at), worker_id, lease_hash, fencing),
        )
        connection.commit()
        return handled
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("support.unavailable") from None
    finally:
        connection.close()


def process_attachment_scans(
    config: AppConfig,
    *,
    now: datetime | None = None,
    limit: int = 20,
) -> int:
    checked_now = (now or _utcnow()).astimezone(UTC)
    connection = _connect(config)
    try:
        jobs = connection.execute(
            """SELECT job.*,attachment.support_ticket_id,attachment.source_file_id,
                      attachment.source_fingerprint,attachment.media_type,attachment.size_bytes,
                      attachment.status attachment_status,attachment.revision attachment_revision,
                      attachment.scan_timeout_at,attachment.updated_at attachment_updated_at,
                      file.status file_status,file.dlp_scan_status,file.dlp_action,
                      file.content_fingerprint,file.original_object_key,file.storage_backend
                 FROM support_attachment_scan_jobs job
                 JOIN support_ticket_attachments attachment
                   ON attachment.ticket_attachment_id=job.ticket_attachment_id
                 JOIN uploaded_files file ON file.tenant_id=attachment.tenant_id
                   AND file.file_id=attachment.source_file_id
                WHERE job.status='pending' AND job.next_attempt_at<=?
                ORDER BY job.next_attempt_at,job.attachment_scan_job_id LIMIT ?""",
            (_db_time(checked_now), limit),
        ).fetchall()
    except sqlite3.Error:
        connection.close()
        raise ApiError("support.attachment_unavailable") from None
    finally:
        if not connection.in_transaction:
            connection.close()
    handled = 0
    for job in jobs:
        outcome = "unavailable"
        terminal_status = "rejected"
        if _time(str(job["scan_timeout_at"])) <= checked_now:
            outcome = "timeout"
            terminal_status = "scan_timeout"
        elif (
            job["attachment_status"] != "quarantined"
            or int(job["attachment_revision"]) != int(job["expected_attachment_revision"])
            or job["source_fingerprint"] != job["expected_source_fingerprint"]
            or job["content_fingerprint"] != job["expected_source_fingerprint"]
        ):
            outcome = "owner_changed"
        else:
            try:
                outcome = _scan_uploaded_file(config, job)
                terminal_status = "available" if outcome == "clean" else "rejected"
            except FileContractError:
                if int(job["attempt_count"]) < 4 and checked_now + timedelta(seconds=30) < _time(
                    str(job["scan_timeout_at"])
                ):
                    _retry_scan(config, job, checked_now)
                    continue
                outcome = "unavailable"
        if _finish_scan(config, job, checked_now, terminal_status, outcome):
            handled += 1
    return handled


def _scan_uploaded_file(config: AppConfig, row: sqlite3.Row) -> str:
    if (
        row["file_status"] != "ready"
        or row["dlp_scan_status"] not in {"clean", "matched"}
        or row["dlp_action"] == "block"
        or row["storage_backend"] != "local_encrypted_v1"
        or type(row["original_object_key"]) is not str
    ):
        return "owner_changed"
    store = EncryptedLocalBlobStore(config.file_storage_root)
    ciphertext = store.read(row["original_object_key"])
    secret = config.file_encryption_secret
    content = decrypt_file_blob(
        ciphertext,
        secret=secret,
        owner=FileBlobOwner(
            tenant_id=str(row["tenant_id"]),
            file_id=str(row["source_file_id"]),
            blob_kind="original",
            content_fingerprint=str(row["source_fingerprint"]),
        ),
    )
    if (
        len(content) != int(row["size_bytes"])
        or file_content_fingerprint(
            content,
            secret=secret,
            tenant_id=str(row["tenant_id"]),
            file_id=str(row["source_file_id"]),
            blob_kind="original",
        )
        != row["source_fingerprint"]
    ):
        return "owner_changed"
    if _EICAR in content:
        return "malicious"
    media_type = str(row["media_type"])
    if media_type == "application/pdf" and any(marker in content for marker in _PDF_ACTIVE_MARKERS):
        return "malicious"
    if media_type.endswith("wordprocessingml.document"):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                infos = archive.infolist()
                if not infos or len(infos) > 256:
                    return "structure_invalid"
                total = 0
                for info in infos:
                    normalized = info.filename.replace("\\", "/")
                    if (
                        normalized.startswith("/")
                        or ".." in normalized.split("/")
                        or normalized.lower().endswith("vbaproject.bin")
                    ):
                        return "malicious"
                    total += info.file_size
                    if total > 200 * 1024 * 1024 or (
                        info.compress_size > 0 and info.file_size / info.compress_size > 100
                    ):
                        return "structure_invalid"
        except (zipfile.BadZipFile, OSError, ValueError):
            return "structure_invalid"
    if media_type == "text/plain" and b"\x00" in content:
        return "structure_invalid"
    return "clean"


def _finish_scan(
    config: AppConfig,
    job: sqlite3.Row,
    now: datetime,
    terminal_status: str,
    outcome: str,
) -> bool:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        live = connection.execute(
            """SELECT attachment.*,scan.status job_status,scan.attempt_count,
                      scan.updated_at job_updated_at
                 FROM support_ticket_attachments attachment
                 JOIN support_attachment_scan_jobs scan
                   ON scan.ticket_attachment_id=attachment.ticket_attachment_id
                WHERE attachment.ticket_attachment_id=?""",
            (job["ticket_attachment_id"],),
        ).fetchone()
        if (
            live is None
            or live["status"] != "quarantined"
            or live["job_status"] != "pending"
            or int(live["revision"]) != int(job["expected_attachment_revision"])
            or live["source_fingerprint"] != job["expected_source_fingerprint"]
        ):
            connection.rollback()
            return False
        changed = _mutation_time(now, str(live["updated_at"]), str(live["job_updated_at"]))
        changed_db = _db_time(changed)
        connection.execute(
            """UPDATE support_ticket_attachments
               SET status=?,scan_outcome=?,download_generation=?,revision=revision+1,
                   scanned_at=?,updated_at=? WHERE ticket_attachment_id=? AND revision=?
                   AND status='quarantined' AND source_fingerprint=?""",
            (
                terminal_status,
                outcome,
                1 if terminal_status == "available" else 0,
                changed_db,
                changed_db,
                live["ticket_attachment_id"],
                live["revision"],
                live["source_fingerprint"],
            ),
        )
        job_status = (
            "completed"
            if terminal_status == "available"
            else ("timed_out" if terminal_status == "scan_timeout" else "failed")
        )
        connection.execute(
            """UPDATE support_attachment_scan_jobs
               SET status=?,attempt_count=attempt_count+1,last_error_code=?,
                   completed_at=?,updated_at=?
               WHERE ticket_attachment_id=? AND status='pending'""",
            (
                job_status,
                "support.attachment_owner_changed"
                if outcome == "owner_changed"
                else ("support.attachment_unavailable" if outcome == "unavailable" else None),
                changed_db,
                changed_db,
                live["ticket_attachment_id"],
            ),
        )
        event_type = {
            "available": "support.attachment_available",
            "rejected": "support.attachment_rejected",
            "scan_timeout": "support.attachment_scan_timeout",
        }[terminal_status]
        _event(
            connection,
            workspace_id=str(live["workspace_id"]),
            tenant_id=str(live["tenant_id"]),
            ticket_id=str(live["support_ticket_id"]),
            event_type=event_type,
            source_object_id=str(live["ticket_attachment_id"]),
            source_revision=int(live["revision"]) + 1,
            actor_domain="scheduler",
            metadata={"scan_outcome": outcome, "status": terminal_status},
            request_id="support-attachment-scan",
            trace_id=f"attachment-{str(live['ticket_attachment_id'])[4:20]}",
            occurred_at=changed_db,
            delivery_domains=("notification",) if terminal_status != "available" else (),
        )
        connection.commit()
        return True
    except (sqlite3.Error, DbError, TypeError, ValueError):
        _rollback(connection)
        return False
    finally:
        connection.close()


def _retry_scan(config: AppConfig, job: sqlite3.Row, now: datetime) -> None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        changed = _mutation_time(now, str(job["updated_at"]))
        connection.execute(
            """UPDATE support_attachment_scan_jobs
               SET attempt_count=attempt_count+1,next_attempt_at=?,
                   last_error_code='support.attachment_unavailable',updated_at=?
               WHERE attachment_scan_job_id=? AND status='pending' AND attempt_count=?""",
            (
                _db_time(changed + timedelta(seconds=30)),
                _db_time(changed),
                job["attachment_scan_job_id"],
                job["attempt_count"],
            ),
        )
        connection.commit()
    except sqlite3.Error:
        _rollback(connection)
    finally:
        connection.close()


def _update_clock_for_patch(
    connection: sqlite3.Connection,
    ticket: sqlite3.Row,
    payload: TicketPatchRequest,
    actor: _Actor,
    *,
    old_status: str,
    next_status: str,
    ticket_revision: int,
    now: datetime,
) -> None:
    clock = connection.execute(
        "SELECT * FROM support_sla_clocks WHERE support_ticket_id=?",
        (ticket["support_ticket_id"],),
    ).fetchone()
    policy = connection.execute(
        "SELECT * FROM support_sla_policies WHERE severity=? AND policy_version=?",
        (ticket["severity"], ticket["sla_policy_version"]),
    ).fetchone()
    if clock is None or policy is None:
        raise ApiError("support.unavailable")
    state = str(clock["state"])
    generation = int(clock["generation"])
    first_deadline = _time(str(clock["first_response_deadline_at"]))
    resolution_deadline = _time(str(clock["resolution_deadline_at"]))
    risk_at = _time(str(clock["risk_at"]))
    first_responded = clock["first_responded_at"]
    paused_at = clock["paused_at"]
    stopped_at = clock["stopped_at"]
    events: list[str] = []

    platform_response = (
        actor.domain == "platform"
        and payload.message is not None
        and payload.message_visibility == "customer"
    )
    if platform_response and first_responded is None:
        first_responded = _db_time(now)
        risk_at = resolution_deadline - timedelta(seconds=int(policy["risk_lead_seconds"]))
        events.append("first_responded")
    if old_status == "resolved" and next_status == "open":
        state = "running"
        generation += 1
        first_deadline = now + timedelta(seconds=int(policy["first_response_seconds"]))
        resolution_deadline = now + timedelta(seconds=int(policy["resolution_seconds"]))
        risk_at = first_deadline - timedelta(seconds=int(policy["risk_lead_seconds"]))
        first_responded = None
        paused_at = None
        stopped_at = None
        events.append("reopened")
    elif next_status in _TERMINAL_TICKET_STATUSES and state != "stopped":
        state = "stopped"
        paused_at = None
        stopped_at = _db_time(now)
        events.append("stopped")
    elif (
        next_status == "waiting_customer"
        and bool(policy["pause_on_customer_wait"])
        and state == "running"
    ):
        state = "paused_customer"
        paused_at = _db_time(now)
        events.append("paused_customer")
    elif (
        old_status == "waiting_customer"
        and state == "paused_customer"
        and next_status != old_status
    ):
        pause_delta = now - _time(str(paused_at))
        first_deadline += pause_delta
        resolution_deadline += pause_delta
        risk_at += pause_delta
        state = "running"
        paused_at = None
        events.append("resumed")

    if payload.sla_action == "pause_policy":
        if state != "running":
            raise ApiError("support.state_conflict")
        state = "paused_policy"
        paused_at = _db_time(now)
        events.append("paused_policy")
    elif payload.sla_action == "resume":
        if state not in {"paused_customer", "paused_policy"}:
            raise ApiError("support.state_conflict")
        pause_delta = now - _time(str(paused_at))
        first_deadline += pause_delta
        resolution_deadline += pause_delta
        risk_at += pause_delta
        state = "running"
        paused_at = None
        events.append("resumed")

    changed = bool(events)
    if not changed:
        return
    clock_time = _mutation_time(now, str(clock["updated_at"]))
    clock_time_db = _db_time(clock_time)
    clock_revision = int(clock["revision"]) + 1
    connection.execute(
        """UPDATE support_sla_clocks
           SET state=?,generation=?,first_response_deadline_at=?,resolution_deadline_at=?,
               risk_at=?,first_responded_at=?,paused_at=?,stopped_at=?,revision=?,updated_at=?
           WHERE sla_clock_id=? AND revision=?""",
        (
            state,
            generation,
            _db_time(first_deadline),
            _db_time(resolution_deadline),
            _db_time(risk_at),
            first_responded,
            paused_at,
            stopped_at,
            clock_revision,
            clock_time_db,
            clock["sla_clock_id"],
            clock["revision"],
        ),
    )
    for event_type in dict.fromkeys(events):
        _clock_event(
            connection,
            str(ticket["workspace_id"]),
            str(ticket["tenant_id"]),
            str(clock["sla_clock_id"]),
            str(ticket["support_ticket_id"]),
            event_type,
            generation,
            clock_revision,
            clock_time_db,
        )


def _insert_survey(
    connection: sqlite3.Connection,
    config: AppConfig,
    ticket: sqlite3.Row,
    actor: _Actor,
    payload: TicketPatchRequest,
    now_db: str,
) -> None:
    if actor.domain != "workspace" or actor.account_id != ticket["reporter_account_id"]:
        raise ApiError("support.permission_denied")
    effective_status = payload.status or ticket["status"]
    if effective_status not in _TERMINAL_TICKET_STATUSES:
        raise ApiError("support.state_conflict")
    survey_id = _id("ssv_")
    ciphertext = None
    version = None
    if payload.survey_comment is not None:
        ciphertext = encrypt_support_content(
            payload.survey_comment,
            secret=_secret(config),
            owner=_content_owner(
                str(ticket["workspace_id"]),
                str(ticket["tenant_id"]),
                "survey",
                survey_id,
                "comment",
            ),
        )
        version = 1
    connection.execute(
        """INSERT INTO support_satisfaction_surveys(
             satisfaction_survey_id,workspace_id,tenant_id,support_ticket_id,
             submitted_by_account_id,rating,comment_ciphertext,comment_key_version,created_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            survey_id,
            ticket["workspace_id"],
            ticket["tenant_id"],
            ticket["support_ticket_id"],
            actor.account_id,
            payload.survey_rating,
            ciphertext,
            version,
            now_db,
        ),
    )
    _event(
        connection,
        workspace_id=str(ticket["workspace_id"]),
        tenant_id=str(ticket["tenant_id"]),
        ticket_id=str(ticket["support_ticket_id"]),
        event_type="support.survey_submitted",
        source_object_id=survey_id,
        source_revision=1,
        actor_domain="customer",
        metadata={"rating": payload.survey_rating},
        request_id="support-survey",
        trace_id=f"survey-{survey_id[4:20]}",
        occurred_at=now_db,
    )


def _ticket(
    connection: sqlite3.Connection,
    config: AppConfig,
    row: sqlite3.Row,
    actor: _Actor,
) -> SupportTicketSummary:
    workspace_id = str(row["workspace_id"])
    tenant_id = str(row["tenant_id"])
    ticket_id = str(row["support_ticket_id"])
    secret = _secret(config)
    subject = decrypt_support_content(
        row["subject_ciphertext"],
        secret=secret,
        owner=_content_owner(workspace_id, tenant_id, "ticket", ticket_id, "subject"),
    )
    message_sql = """SELECT * FROM support_ticket_messages WHERE support_ticket_id=?"""
    if actor.domain == "workspace":
        message_sql += " AND visibility='customer'"
    message_sql += " ORDER BY created_at,ticket_message_id"
    messages = tuple(
        TicketMessageSummary(
            message_id=str(message["ticket_message_id"]),
            author_domain=str(message["author_domain"]),
            visibility=str(message["visibility"]),
            body=decrypt_support_content(
                message["body_ciphertext"],
                secret=secret,
                owner=_content_owner(
                    workspace_id,
                    tenant_id,
                    "message",
                    str(message["ticket_message_id"]),
                    "body",
                ),
            ),
            created_at=_time(str(message["created_at"])),
        )
        for message in connection.execute(message_sql, (ticket_id,)).fetchall()
    )
    attachments = tuple(
        _attachment(item)
        for item in connection.execute(
            """SELECT * FROM support_ticket_attachments WHERE support_ticket_id=?
               ORDER BY created_at,ticket_attachment_id""",
            (ticket_id,),
        ).fetchall()
    )
    clock = connection.execute(
        "SELECT * FROM support_sla_clocks WHERE support_ticket_id=?", (ticket_id,)
    ).fetchone()
    if clock is None:
        raise ApiError("support.unavailable")
    escalations = tuple(
        _escalation(item)
        for item in connection.execute(
            """SELECT * FROM support_escalations WHERE support_ticket_id=?
               ORDER BY generation,escalation_id""",
            (ticket_id,),
        ).fetchall()
    )
    survey_row = connection.execute(
        "SELECT * FROM support_satisfaction_surveys WHERE support_ticket_id=?", (ticket_id,)
    ).fetchone()
    survey = None
    if survey_row is not None:
        comment = None
        if survey_row["comment_ciphertext"] is not None:
            comment = decrypt_support_content(
                survey_row["comment_ciphertext"],
                secret=secret,
                owner=_content_owner(
                    workspace_id,
                    tenant_id,
                    "survey",
                    str(survey_row["satisfaction_survey_id"]),
                    "comment",
                ),
            )
        survey = SatisfactionSurveySummary(
            rating=int(survey_row["rating"]),
            comment=comment,
            created_at=_time(str(survey_row["created_at"])),
        )
    return SupportTicketSummary(
        ticket_id=ticket_id,
        workspace_id=workspace_id,
        category=str(row["category"]),
        severity=str(row["severity"]),
        status=str(row["status"]),
        subject=subject,
        reported_by_me=actor.account_id is not None
        and actor.account_id == row["reporter_account_id"],
        assignee_operator_id=row["platform_assignee_operator_id"],
        revision=int(row["revision"]),
        last_customer_activity_at=_time(str(row["last_customer_activity_at"])),
        last_platform_activity_at=_optional_time(row["last_platform_activity_at"]),
        resolved_at=_optional_time(row["resolved_at"]),
        closed_at=_optional_time(row["closed_at"]),
        created_at=_time(str(row["created_at"])),
        updated_at=_time(str(row["updated_at"])),
        messages=messages,
        attachments=attachments,
        sla=SlaClockSummary(
            state=str(clock["state"]),
            generation=int(clock["generation"]),
            first_response_deadline_at=_time(str(clock["first_response_deadline_at"])),
            resolution_deadline_at=_time(str(clock["resolution_deadline_at"])),
            risk_at=_time(str(clock["risk_at"])),
            first_responded_at=_optional_time(clock["first_responded_at"]),
            risk_generation=int(clock["risk_generation"]),
            escalation_generation=int(clock["escalation_generation"]),
            revision=int(clock["revision"]),
        ),
        escalations=escalations,
        survey=survey,
    )


def _attachment(row: sqlite3.Row) -> TicketAttachmentSummary:
    return TicketAttachmentSummary(
        attachment_id=str(row["ticket_attachment_id"]),
        intent_code=str(row["intent_code"]),
        media_type=str(row["media_type"]),
        size_bytes=int(row["size_bytes"]),
        status=str(row["status"]),
        scan_version=str(row["scan_version"]),
        scan_outcome=row["scan_outcome"],
        revision=int(row["revision"]),
        created_at=_time(str(row["created_at"])),
        updated_at=_time(str(row["updated_at"])),
    )


def _escalation(row: sqlite3.Row) -> EscalationSummary:
    return EscalationSummary(
        escalation_id=str(row["escalation_id"]),
        source_code=str(row["source_code"]),
        reason_code=str(row["reason_code"]),
        generation=int(row["generation"]),
        status=str(row["status"]),
        created_at=_time(str(row["created_at"])),
    )


def _authenticate(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    workspace_token: str | None = None,
    platform_token: str | None = None,
    workspace_csrf: str | None = None,
    platform_csrf: str | None = None,
) -> _Actor:
    if (workspace_token is None) == (platform_token is None):
        raise ApiError("workspace_session.credential_conflict")
    if workspace_token is not None:
        context: WorkspaceSessionContext = authenticate_workspace_session_in_transaction(
            connection,
            config,
            session_token=workspace_token,
            csrf_token=workspace_csrf,
            touch=True,
        )
        return _Actor(
            domain="workspace",
            workspace_id=context.workspace_id,
            tenant_id=context.tenant_id,
            account_id=context.account_id,
            operator_id=None,
            membership_id=context.membership_id,
            legacy_user_id=context.legacy_user_id,
            permissions=context.permission_codes,
            authorization_fingerprint=context.authorization_fingerprint,
        )
    context: PlatformContext = authenticate_platform_session_in_transaction(
        connection,
        config,
        session_token=str(platform_token),
        csrf_token=platform_csrf,
        touch=True,
    )
    fingerprint = hashlib.sha256(
        json.dumps([context.roles, context.permissions], separators=(",", ":")).encode("ascii")
    ).hexdigest()
    return _Actor(
        domain="platform",
        workspace_id=None,
        tenant_id=None,
        account_id=None,
        operator_id=context.operator_id,
        membership_id=None,
        legacy_user_id=None,
        permissions=context.permissions,
        authorization_fingerprint=fingerprint,
    )


def _bind_actor_owner(actor: _Actor, row: sqlite3.Row) -> _Actor:
    if actor.domain == "workspace":
        if actor.workspace_id != row["workspace_id"] or actor.tenant_id != row["tenant_id"]:
            raise ApiError("support.not_found")
        return actor
    return replace(actor, workspace_id=str(row["workspace_id"]), tenant_id=str(row["tenant_id"]))


def _authorize_ticket_mutation(
    actor: _Actor, row: sqlite3.Row, payload: TicketPatchRequest
) -> None:
    if actor.domain == "workspace":
        _require(actor, "support.ticket.read")
        _require_participant(actor, row)
        if (
            payload.message_visibility == "internal"
            or payload.assignee_operator_id is not None
            or payload.sla_action is not None
        ):
            raise ApiError("support.permission_denied")
    else:
        _require(actor, "platform.support.ticket.manage")
        if payload.survey_rating is not None:
            raise ApiError("support.permission_denied")
        if payload.sla_action is not None:
            _require(actor, "platform.support.sla.manage")


def _authorize_workspace_status(actor: _Actor, old_status: str, next_status: str) -> None:
    if actor.domain != "workspace" or old_status == next_status:
        return
    if (old_status, next_status) not in {
        ("open", "resolved"),
        ("triaged", "resolved"),
        ("waiting_customer", "resolved"),
        ("waiting_platform", "resolved"),
        ("resolved", "open"),
        ("resolved", "closed"),
        ("waiting_customer", "waiting_platform"),
    }:
        raise ApiError("support.permission_denied")


def _require_participant(actor: _Actor, row: sqlite3.Row) -> None:
    if actor.domain != "workspace":
        return
    if (
        actor.account_id != row["reporter_account_id"]
        and "support.ticket.manage" not in actor.permissions
    ):
        raise ApiError("support.not_found")


def _require(actor: _Actor, permission: str) -> None:
    if permission not in actor.permissions:
        raise ApiError("support.permission_denied")


def _ticket_row(connection: sqlite3.Connection, ticket_id: str, actor: _Actor) -> sqlite3.Row:
    if actor.domain == "workspace":
        row = connection.execute(
            "SELECT * FROM support_tickets WHERE support_ticket_id=? AND workspace_id=?",
            (ticket_id, actor.workspace_id),
        ).fetchone()
        if (
            row is not None
            and actor.account_id != row["reporter_account_id"]
            and ("support.ticket.manage" not in actor.permissions)
        ):
            row = None
    else:
        row = connection.execute(
            "SELECT * FROM support_tickets WHERE support_ticket_id=?", (ticket_id,)
        ).fetchone()
    if row is None:
        raise ApiError("support.not_found")
    return row


def _attachment_row(
    connection: sqlite3.Connection, attachment_id: str, ticket_id: str
) -> sqlite3.Row:
    row = connection.execute(
        """SELECT * FROM support_ticket_attachments
           WHERE ticket_attachment_id=? AND support_ticket_id=?""",
        (attachment_id, ticket_id),
    ).fetchone()
    if row is None:
        raise ApiError("support.attachment_not_found")
    return row


def _escalation_row(
    connection: sqlite3.Connection, escalation_id: str, ticket_id: str
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM support_escalations WHERE escalation_id=? AND support_ticket_id=?",
        (escalation_id, ticket_id),
    ).fetchone()
    if row is None:
        raise ApiError("support.not_found")
    return row


def _event(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    tenant_id: str,
    ticket_id: str,
    event_type: str,
    source_object_id: str,
    source_revision: int,
    actor_domain: str,
    metadata: dict[str, object],
    request_id: str,
    trace_id: str,
    occurred_at: str,
    delivery_domains: tuple[str, ...] = (),
) -> str:
    event_id = _id("ste_")
    safe_metadata = json.dumps(metadata, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    connection.execute(
        """INSERT INTO support_ticket_events(
             support_ticket_event_id,workspace_id,tenant_id,support_ticket_id,event_type,
             source_object_id,source_revision,actor_domain,safe_metadata_json,
             request_id,trace_id,occurred_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event_id,
            workspace_id,
            tenant_id,
            ticket_id,
            event_type,
            source_object_id,
            source_revision,
            actor_domain,
            safe_metadata,
            request_id,
            trace_id,
            occurred_at,
        ),
    )
    for domain in delivery_domains:
        connection.execute(
            """INSERT INTO support_event_outbox(
                 support_event_outbox_id,workspace_id,tenant_id,support_ticket_event_id,
                 delivery_domain,status,attempt_count,next_attempt_at,last_error_code,
                 delivered_at,created_at,updated_at)
               VALUES(?,?,?,?,?,'pending',0,?,NULL,NULL,?,?)""",
            (
                _id("seo_"),
                workspace_id,
                tenant_id,
                event_id,
                domain,
                occurred_at,
                occurred_at,
                occurred_at,
            ),
        )
    return event_id


def _clock_event(
    connection: sqlite3.Connection,
    workspace_id: str,
    tenant_id: str,
    clock_id: str,
    ticket_id: str,
    event_type: str,
    generation: int,
    source_revision: int,
    occurred_at: str,
) -> None:
    connection.execute(
        """INSERT INTO support_sla_clock_events(
             sla_clock_event_id,workspace_id,tenant_id,sla_clock_id,support_ticket_id,
             event_type,clock_generation,source_revision,occurred_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            _id("sce_"),
            workspace_id,
            tenant_id,
            clock_id,
            ticket_id,
            event_type,
            generation,
            source_revision,
            occurred_at,
        ),
    )


def _command_replay(
    connection: sqlite3.Connection,
    actor: _Actor,
    action: str,
    idempotency_key: str,
    request_hash: str,
) -> str | None:
    row = connection.execute(
        """SELECT request_sha256,result_object_id FROM support_action_commands
           WHERE workspace_id=? AND actor_domain=? AND actor_identity=?
             AND action_code=? AND idempotency_sha256=?""",
        (
            actor.workspace_id,
            actor.command_domain,
            actor.identity,
            action,
            _idempotency_hash(idempotency_key),
        ),
    ).fetchone()
    if row is None:
        return None
    if row["request_sha256"] != request_hash:
        raise ApiError("support.idempotency_conflict")
    return str(row["result_object_id"])


def _record_command(
    connection: sqlite3.Connection,
    actor: _Actor,
    action: str,
    idempotency_key: str,
    request_hash: str,
    result_object_id: str,
    created_at: str,
) -> None:
    connection.execute(
        """INSERT INTO support_action_commands(
             support_action_command_id,workspace_id,tenant_id,actor_domain,actor_identity,
             actor_account_id,actor_operator_id,action_code,idempotency_sha256,
             request_sha256,result_object_id,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            _id("sac_"),
            actor.workspace_id,
            actor.tenant_id,
            actor.command_domain,
            actor.identity,
            actor.account_id,
            actor.operator_id,
            action,
            _idempotency_hash(idempotency_key),
            request_hash,
            result_object_id,
            created_at,
        ),
    )


def _request_hash(action: str, payload: dict[str, object]) -> str:
    encoded = json.dumps(
        {"action": action, "payload": payload},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _idempotency_hash(value: str) -> str:
    return hashlib.sha256(("metahub/prd132/idempotency/v1\x00" + value).encode("ascii")).hexdigest()


def _content_owner(
    workspace_id: str,
    tenant_id: str,
    object_kind: str,
    object_id: str,
    field_kind: str,
) -> SupportContentOwner:
    return SupportContentOwner(
        workspace_id=workspace_id,
        tenant_id=tenant_id,
        object_kind=object_kind,
        object_id=object_id,
        field_kind=field_kind,
        key_version=1,
    )


def _secret(config: AppConfig) -> str:
    secret = config.tenant_data_encryption_secret
    if type(secret) is not str:
        raise SupportContentCryptoError("support.unavailable")
    return secret


def _connect(config: AppConfig) -> sqlite3.Connection:
    return connect_database(config.database_url)


def _rollback(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        connection.rollback()


def _id(prefix: str) -> str:
    return prefix + secrets.token_hex(16)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _optional_time(value: object) -> datetime | None:
    return None if value is None else _time(str(value))


def _mutation_time(now: datetime, *previous: str) -> datetime:
    result = now.astimezone(UTC)
    for value in previous:
        parsed = _time(value) + timedelta(milliseconds=1)
        if parsed > result:
            result = parsed
    return result


__all__ = [
    "create_attachment",
    "create_escalation",
    "create_ticket",
    "list_tickets",
    "patch_ticket",
    "process_attachment_scans",
    "process_support_sla_once",
]
