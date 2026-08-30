"""Owner-qualified PRD-135 compliance evidence, exception, and report service."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.db import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.machine_identity.service import MachineAuthContext
from metahub_control.platform_access.service import (
    authenticate_platform_session_in_transaction,
)
from metahub_control.workspace_access.service import (
    authenticate_workspace_session_in_transaction,
)

from .models import (
    ComplianceControlState,
    ComplianceControlSummary,
    ComplianceEvidenceResponse,
    ComplianceEvidenceSubmitRequest,
    ComplianceExceptionCommand,
    ComplianceExceptionResponse,
    ComplianceExceptionSummary,
    ComplianceReportItemSummary,
    ComplianceReportListResponse,
    ComplianceReportSummary,
    ControlMappingSummary,
    EvidenceSummary,
    PlatformComplianceControlsResponse,
    RemediationTaskSummary,
)

_CONTROL_IDS = (
    "ctl_00000000000000000000000000000001",
    "ctl_00000000000000000000000000000002",
    "ctl_00000000000000000000000000000003",
)
_CONTROL_SET_VERSION = "2026.08.1"
_GENERATOR_VERSION = "prd135-v1"
_SIGNING_DOMAIN = b"prd135/evidence-envelope/v1\0"
_ISSUED_SKEW = timedelta(minutes=5)
_MAX_CAPTURE_AGE = timedelta(days=30)
_EXPIRING_WINDOW = timedelta(hours=24)
_EXCEPTION_MAX = timedelta(days=90)
_LEASE_TTL = timedelta(seconds=30)


def get_platform_controls(
    config: AppConfig,
    *,
    platform_session_token: str,
    workspace_id: str | None,
    request_id: str,
    trace_id: str,
) -> PlatformComplianceControlsResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = authenticate_platform_session_in_transaction(
            connection, config, session_token=platform_session_token, touch=True
        )
        if "platform.compliance.read" not in actor.permissions:
            raise ApiError("platform.permission_denied")
        _verify_catalog(connection)
        owner = None
        if workspace_id is not None:
            owner = _workspace_owner(connection, workspace_id)
        now = _database_now(connection)
        states = tuple(
            _control_state(connection, row, owner=owner, now=now)
            for row in _control_rows(connection)
        )
        connection.commit()
        return PlatformComplianceControlsResponse(
            control_set_version=_CONTROL_SET_VERSION,
            workspace_id=workspace_id,
            controls=states,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("compliance.unavailable") from None
    finally:
        connection.close()


def submit_evidence(
    config: AppConfig,
    payload: ComplianceEvidenceSubmitRequest,
    *,
    machine: MachineAuthContext,
    access_token: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> ComplianceEvidenceResponse:
    if (
        machine.workspace_id != payload.workspace_id
        or "compliance.collect" not in machine.permission_codes
        or machine.audience != "metahub-control"
    ):
        raise ApiError("compliance.owner_mismatch")
    unsigned = payload.model_dump(mode="json", exclude={"signature_sha256"})
    signed_value = {"envelope": unsigned, "idempotency_key": idempotency_key}
    envelope_sha256 = _digest(unsigned)
    request_sha256 = _digest(payload.model_dump(mode="json"))
    expected_signature = hmac.new(
        access_token.encode("ascii"),
        _SIGNING_DOMAIN + _canonical(signed_value).encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_signature, payload.signature_sha256):
        raise ApiError("compliance.signature_invalid")

    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _verify_catalog(connection)
        now = _database_now(connection)
        replay = _command_replay(
            connection,
            subject_type="service_account",
            subject_id=machine.service_account_id,
            action_code="compliance.evidence.submit",
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
        )
        if replay is not None:
            evidence = _evidence_row(connection, str(replay["result_id"]))
            report = _latest_report_row(connection, machine.workspace_id, machine.tenant_id)
            if report is None:
                raise ApiError("compliance.unavailable")
            remediation = _remediation_for_evidence(
                connection, str(evidence["compliance_evidence_id"])
            )
            result = ComplianceEvidenceResponse(
                evidence=_evidence_summary(evidence, now),
                remediation=None if remediation is None else _remediation_summary(remediation),
                report_id=str(report["compliance_report_id"]),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return result

        issued_at = _as_utc(payload.issued_at)
        captured_at = _as_utc(payload.captured_at)
        valid_until = _as_utc(payload.valid_until)
        if abs(now - issued_at) > _ISSUED_SKEW:
            raise ApiError("compliance.envelope_expired")
        if captured_at > now + _ISSUED_SKEW or captured_at < now - _MAX_CAPTURE_AGE:
            raise ApiError("compliance.evidence_time_invalid")
        control = _control_row(connection, payload.compliance_control_id)
        if (
            str(control["evidence_kind"]) != payload.evidence_kind
            or int(control["collector_schema_version"]) != payload.schema_version
        ):
            raise ApiError("compliance.schema_invalid")
        if valid_until <= now or valid_until > captured_at + timedelta(
            seconds=int(control["freshness_seconds"])
        ):
            raise ApiError("compliance.evidence_time_invalid")
        nonce_sha256 = hashlib.sha256(payload.nonce.encode("ascii")).hexdigest()
        if connection.execute(
            "SELECT 1 FROM compliance_collector_replay_guards "
            "WHERE collector_service_account_id=? AND nonce_sha256=?",
            (machine.service_account_id, nonce_sha256),
        ).fetchone():
            raise ApiError("compliance.replay_detected")

        outcome, reason = _evaluate(payload)
        sequence = int(
            connection.execute(
                "SELECT COALESCE(MAX(evidence_sequence),0)+1 FROM compliance_evidence"
            ).fetchone()[0]
        )
        generation = int(
            connection.execute(
                "SELECT COALESCE(MAX(evidence_generation),0)+1 FROM compliance_evidence "
                "WHERE workspace_id=? AND tenant_id=? AND compliance_control_id=?",
                (machine.workspace_id, machine.tenant_id, payload.compliance_control_id),
            ).fetchone()[0]
        )
        evidence_id = _id("evd_")
        stamp = _db_time(now)
        receipt_sha256 = _digest(
            {
                "workspace_id": machine.workspace_id,
                "control_id": payload.compliance_control_id,
                "generation": generation,
                "envelope_sha256": envelope_sha256,
                "collector": machine.authorization_fingerprint,
            }
        )
        connection.execute(
            """INSERT INTO compliance_collector_replay_guards(
                 replay_guard_id,workspace_id,tenant_id,collector_service_account_id,
                 nonce_sha256,envelope_sha256,expires_at,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                _id("cgr_"),
                machine.workspace_id,
                machine.tenant_id,
                machine.service_account_id,
                nonce_sha256,
                envelope_sha256,
                _db_time(now + _MAX_CAPTURE_AGE),
                stamp,
            ),
        )
        connection.execute(
            """INSERT INTO compliance_evidence(
                 compliance_evidence_id,workspace_id,tenant_id,compliance_control_id,
                 evidence_sequence,evidence_generation,collector_service_account_id,
                 collector_version,evidence_kind,schema_version,artifact_sha256,
                 classification,scope_state,observed_state,sample_count,failed_count,
                 outcome,outcome_reason_code,envelope_sha256,collector_authorization_sha256,
                 immutable_receipt_sha256,captured_at,valid_until,submitted_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                evidence_id,
                machine.workspace_id,
                machine.tenant_id,
                payload.compliance_control_id,
                sequence,
                generation,
                machine.service_account_id,
                payload.collector_version,
                payload.evidence_kind,
                payload.schema_version,
                payload.artifact_sha256,
                payload.classification,
                payload.scope_state,
                payload.observed_state,
                payload.sample_count,
                payload.failed_count,
                outcome,
                reason,
                envelope_sha256,
                machine.authorization_fingerprint,
                receipt_sha256,
                _db_time(captured_at),
                _db_time(valid_until),
                stamp,
            ),
        )
        remediation = None
        if outcome == "failed":
            remediation_id = _id("rem_")
            connection.execute(
                """INSERT INTO compliance_remediation_tasks(
                     remediation_task_id,workspace_id,tenant_id,compliance_control_id,
                     failed_evidence_id,failed_evidence_generation,status,owner_code,
                     verified_evidence_id,verified_evidence_generation,revision,due_at,
                     verified_at,closed_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,'open',?,NULL,NULL,1,?,NULL,NULL,?,?)""",
                (
                    remediation_id,
                    machine.workspace_id,
                    machine.tenant_id,
                    payload.compliance_control_id,
                    evidence_id,
                    generation,
                    control["owner_code"],
                    _db_time(now + timedelta(days=7)),
                    stamp,
                    stamp,
                ),
            )
            remediation = connection.execute(
                "SELECT * FROM compliance_remediation_tasks WHERE remediation_task_id=?",
                (remediation_id,),
            ).fetchone()
            _emit_event(
                connection,
                event_type="compliance.control_failed",
                workspace_id=machine.workspace_id,
                tenant_id=machine.tenant_id,
                object_type="evidence",
                object_id=evidence_id,
                object_generation=generation,
                actor_type="service_account",
                actor_id=machine.service_account_id,
                request_id=request_id,
                trace_id=trace_id,
                now=now,
            )
        report_id = _generate_report(connection, machine.workspace_id, machine.tenant_id, now=now)
        _write_command(
            connection,
            workspace_id=machine.workspace_id,
            tenant_id=machine.tenant_id,
            subject_type="service_account",
            subject_id=machine.service_account_id,
            action_code="compliance.evidence.submit",
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
            result_type="evidence",
            result_id=evidence_id,
            result_generation=generation,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        evidence = _evidence_row(connection, evidence_id)
        connection.commit()
        return ComplianceEvidenceResponse(
            evidence=_evidence_summary(evidence, now),
            remediation=None if remediation is None else _remediation_summary(remediation),
            report_id=report_id,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("compliance.unavailable") from None
    finally:
        connection.close()


def apply_exception_command(
    config: AppConfig,
    payload: ComplianceExceptionCommand,
    *,
    platform_session_token: str,
    platform_csrf_token: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> ComplianceExceptionResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_platform_session_in_transaction(
            connection,
            config,
            session_token=platform_session_token,
            csrf_token=platform_csrf_token,
            touch=True,
        )
        if "platform.compliance.exception" not in actor.permissions:
            raise ApiError("platform.permission_denied")
        _verify_catalog(connection)
        now = _database_now(connection)
        request_sha256 = _digest(payload.model_dump(mode="json"))
        action_code = f"compliance.exception.{payload.operation}"
        replay = _command_replay(
            connection,
            subject_type="platform_operator",
            subject_id=actor.operator_id,
            action_code=action_code,
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
        )
        if replay is not None:
            row = _exception_row(connection, str(replay["result_id"]))
            report = _latest_report_row(connection, str(row["workspace_id"]), str(row["tenant_id"]))
            if report is None:
                raise ApiError("compliance.unavailable")
            connection.commit()
            return ComplianceExceptionResponse(
                exception=_exception_summary(row),
                report_id=str(report["compliance_report_id"]),
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )

        if payload.operation == "request":
            assert payload.workspace_id is not None
            assert payload.compliance_control_id is not None
            assert payload.evidence_generation is not None
            assert payload.reason_code is not None
            assert payload.compensating_controls is not None
            assert payload.starts_at is not None and payload.expires_at is not None
            owner = _workspace_owner(connection, payload.workspace_id)
            evidence = connection.execute(
                "SELECT * FROM compliance_evidence WHERE workspace_id=? AND tenant_id=? "
                "AND compliance_control_id=? AND evidence_generation=?",
                (
                    owner["workspace_id"],
                    owner["tenant_id"],
                    payload.compliance_control_id,
                    payload.evidence_generation,
                ),
            ).fetchone()
            if evidence is None:
                raise ApiError("compliance.evidence_not_found")
            if evidence["outcome"] not in {"failed", "inconclusive"}:
                raise ApiError("compliance.exception_conflict")
            starts_at = _as_utc(payload.starts_at)
            expires_at = _as_utc(payload.expires_at)
            if (
                starts_at < now - _ISSUED_SKEW
                or starts_at > now + timedelta(days=1)
                or expires_at > starts_at + _EXCEPTION_MAX
            ):
                raise ApiError("compliance.exception_time_invalid")
            duplicate = connection.execute(
                "SELECT 1 FROM compliance_exceptions WHERE workspace_id=? AND tenant_id=? "
                "AND compliance_control_id=? AND evidence_generation=? "
                "AND status IN('pending','active')",
                (
                    owner["workspace_id"],
                    owner["tenant_id"],
                    payload.compliance_control_id,
                    payload.evidence_generation,
                ),
            ).fetchone()
            if duplicate:
                raise ApiError("compliance.exception_conflict")
            exception_id = _id("cex_")
            stamp = _db_time(now)
            connection.execute(
                """INSERT INTO compliance_exceptions(
                     compliance_exception_id,workspace_id,tenant_id,compliance_control_id,
                     evidence_generation,reason_code,compensating_controls,
                     requester_account_id,approver_account_id,status,review_generation,
                     revision,starts_at,expires_at,approved_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?, ?,NULL,'pending',1,1,?,?,NULL,?,?)""",
                (
                    exception_id,
                    owner["workspace_id"],
                    owner["tenant_id"],
                    payload.compliance_control_id,
                    payload.evidence_generation,
                    payload.reason_code,
                    _canonical(payload.compensating_controls),
                    actor.account_id,
                    _db_time(starts_at),
                    _db_time(expires_at),
                    stamp,
                    stamp,
                ),
            )
            result_generation = 1
        else:
            assert payload.compliance_exception_id is not None
            assert payload.review_generation is not None
            current = _exception_row(connection, payload.compliance_exception_id)
            if (
                current["status"] != "pending"
                or int(current["review_generation"]) != payload.review_generation
            ):
                raise ApiError("compliance.revision_conflict")
            if current["requester_account_id"] == actor.account_id:
                raise ApiError("compliance.self_approval_denied")
            if _time(current["expires_at"]) <= now:
                raise ApiError("compliance.exception_expired")
            changed = _changed_time(now, current["updated_at"])
            result_generation = int(current["review_generation"]) + 1
            cursor = connection.execute(
                """UPDATE compliance_exceptions
                   SET approver_account_id=?,status='active',review_generation=?,
                       revision=revision+1,approved_at=?,updated_at=?
                   WHERE compliance_exception_id=? AND status='pending'
                     AND review_generation=? AND revision=?""",
                (
                    actor.account_id,
                    result_generation,
                    _db_time(changed),
                    _db_time(changed),
                    payload.compliance_exception_id,
                    payload.review_generation,
                    current["revision"],
                ),
            )
            if cursor.rowcount != 1:
                raise ApiError("compliance.revision_conflict")
            exception_id = payload.compliance_exception_id
            _emit_event(
                connection,
                event_type="compliance.exception_approved",
                workspace_id=str(current["workspace_id"]),
                tenant_id=str(current["tenant_id"]),
                object_type="exception",
                object_id=exception_id,
                object_generation=result_generation,
                actor_type="platform_operator",
                actor_id=actor.operator_id,
                request_id=request_id,
                trace_id=trace_id,
                now=changed,
            )
        row = _exception_row(connection, exception_id)
        report_id = _generate_report(
            connection, str(row["workspace_id"]), str(row["tenant_id"]), now=now
        )
        _write_command(
            connection,
            workspace_id=str(row["workspace_id"]),
            tenant_id=str(row["tenant_id"]),
            subject_type="platform_operator",
            subject_id=actor.operator_id,
            action_code=action_code,
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
            result_type="exception",
            result_id=exception_id,
            result_generation=result_generation,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        connection.commit()
        return ComplianceExceptionResponse(
            exception=_exception_summary(row),
            report_id=report_id,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("compliance.unavailable") from None
    finally:
        connection.close()


def list_workspace_reports(
    config: AppConfig,
    workspace_id: str,
    *,
    workspace_session_token: str,
    request_id: str,
    trace_id: str,
) -> ComplianceReportListResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = authenticate_workspace_session_in_transaction(
            connection, config, session_token=workspace_session_token, touch=True
        )
        if actor.workspace_id != workspace_id:
            raise ApiError("compliance.report_not_found")
        if (
            actor.membership_role not in {"security_admin", "auditor"}
            or "compliance.report.read" not in actor.permission_codes
        ):
            raise ApiError("workspace_session.permission_denied")
        rows = connection.execute(
            "SELECT * FROM compliance_reports WHERE workspace_id=? AND tenant_id=? "
            "ORDER BY report_version DESC LIMIT 6",
            (actor.workspace_id, actor.tenant_id),
        ).fetchall()
        now = _database_now(connection)
        reports = tuple(_report_summary(connection, row, now=now) for row in rows)
        connection.commit()
        return ComplianceReportListResponse(
            current=reports[0] if reports else None,
            recent=reports[1:],
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("compliance.unavailable") from None
    finally:
        connection.close()


def process_compliance_once(config: AppConfig, *, worker_id: str, limit: int = 50) -> int:
    lease = _acquire_lease(config, worker_id)
    if lease is None:
        return 0
    generation, token_sha256 = lease
    connection = _connect(config)
    processed = 0
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _database_now(connection)
        _require_lease(connection, worker_id, generation, token_sha256, now)
        expiring = connection.execute(
            """SELECT evidence.* FROM compliance_evidence evidence
               WHERE evidence.valid_until>? AND evidence.valid_until<=?
                 AND NOT EXISTS(SELECT 1 FROM compliance_events event
                   WHERE event.event_type='compliance.evidence_expiring'
                     AND event.object_id=evidence.compliance_evidence_id
                     AND event.object_generation=evidence.evidence_generation)
               ORDER BY evidence.valid_until LIMIT ?""",
            (_db_time(now), _db_time(now + _EXPIRING_WINDOW), limit),
        ).fetchall()
        for row in expiring:
            _emit_event(
                connection,
                event_type="compliance.evidence_expiring",
                workspace_id=str(row["workspace_id"]),
                tenant_id=str(row["tenant_id"]),
                object_type="evidence",
                object_id=str(row["compliance_evidence_id"]),
                object_generation=int(row["evidence_generation"]),
                actor_type="scheduler",
                actor_id=worker_id,
                request_id="compliance-worker",
                trace_id="compliance-worker",
                now=now,
            )
            processed += 1
        expired = connection.execute(
            "SELECT * FROM compliance_exceptions WHERE status IN('pending','active') "
            "AND expires_at<=? ORDER BY expires_at LIMIT ?",
            (_db_time(now), limit),
        ).fetchall()
        for row in expired:
            changed = _changed_time(now, row["updated_at"])
            connection.execute(
                "UPDATE compliance_exceptions SET status='expired',revision=revision+1,"
                "updated_at=? WHERE compliance_exception_id=? AND revision=?",
                (_db_time(changed), row["compliance_exception_id"], row["revision"]),
            )
            processed += 1
        owners = connection.execute(
            "SELECT workspace_id,tenant_id FROM workspace_tenant_bindings "
            "WHERE status='active' ORDER BY workspace_id LIMIT ?",
            (limit,),
        ).fetchall()
        for owner in owners:
            if _report_needs_refresh(
                connection, str(owner["workspace_id"]), str(owner["tenant_id"]), now
            ):
                _generate_report(
                    connection, str(owner["workspace_id"]), str(owner["tenant_id"]), now=now
                )
                processed += 1
        outbox = connection.execute(
            "SELECT * FROM compliance_event_outbox WHERE status IN('pending','failed') "
            "AND next_attempt_at<=? ORDER BY next_attempt_at LIMIT ?",
            (_db_time(now), limit),
        ).fetchall()
        for row in outbox:
            changed = _changed_time(now, row["updated_at"])
            connection.execute(
                """UPDATE compliance_event_outbox
                   SET status='delivered',attempt_count=attempt_count+1,delivered_at=?,
                       last_error_code=NULL,revision=revision+1,updated_at=?
                   WHERE compliance_event_outbox_id=? AND revision=?""",
                (
                    _db_time(changed),
                    _db_time(changed),
                    row["compliance_event_outbox_id"],
                    row["revision"],
                ),
            )
            processed += 1
        changed = _changed_time(
            now,
            connection.execute(
                "SELECT updated_at FROM compliance_scheduler_state WHERE singleton_id=1"
            ).fetchone()[0],
        )
        cursor = connection.execute(
            """UPDATE compliance_scheduler_state
               SET freshness_watermark_at=?,lease_owner_id=NULL,lease_token_sha256=NULL,
                   lease_expires_at=NULL,revision=revision+1,updated_at=?
               WHERE singleton_id=1 AND lease_owner_id=? AND lease_token_sha256=?
                 AND fencing_generation=?""",
            (
                _db_time(now),
                _db_time(changed),
                worker_id,
                token_sha256,
                generation,
            ),
        )
        if cursor.rowcount != 1:
            raise ApiError("compliance.worker_fenced")
        connection.commit()
        return processed
    except ApiError:
        _rollback(connection)
        _release_lease(config, worker_id, generation, token_sha256)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _rollback(connection)
        _release_lease(config, worker_id, generation, token_sha256)
        raise ApiError("compliance.unavailable") from None
    finally:
        connection.close()


def _control_state(connection, control, *, owner, now: datetime) -> ComplianceControlState:
    evidence = None
    exception = None
    remediation = None
    report_outcome = None
    explanation = None
    if owner is not None:
        evidence = _latest_evidence(
            connection, owner["workspace_id"], owner["tenant_id"], control["compliance_control_id"]
        )
        if evidence is None:
            report_outcome, explanation = "not_implemented", "evidence_missing"
        else:
            exception = _active_exception(connection, evidence, now)
            remediation = _latest_remediation(
                connection,
                owner["workspace_id"],
                owner["tenant_id"],
                control["compliance_control_id"],
            )
            report_outcome, explanation = _project_outcome(evidence, exception, now)
    return ComplianceControlState(
        control=_control_summary(connection, control),
        report_outcome=report_outcome,
        explanation_code=explanation,
        evidence=None if evidence is None else _evidence_summary(evidence, now),
        exception=None if exception is None else _exception_summary(exception),
        remediation=None if remediation is None else _remediation_summary(remediation),
    )


def _generate_report(connection, workspace_id: str, tenant_id: str, *, now: datetime) -> str:
    version = int(
        connection.execute(
            "SELECT COALESCE(MAX(report_version),0)+1 FROM compliance_reports "
            "WHERE workspace_id=? AND tenant_id=?",
            (workspace_id, tenant_id),
        ).fetchone()[0]
    )
    evidence_watermark = _evidence_watermark(connection, workspace_id, tenant_id)
    exception_watermark = _exception_watermark(connection, workspace_id, tenant_id)
    items: list[dict[str, object]] = []
    expiry_candidates = [now + _EXPIRING_WINDOW]
    implemented_count = gap_count = stale_count = 0
    for control in _control_rows(connection):
        evidence = _latest_evidence(
            connection, workspace_id, tenant_id, control["compliance_control_id"]
        )
        exception = None if evidence is None else _active_exception(connection, evidence, now)
        remediation = _latest_remediation(
            connection, workspace_id, tenant_id, control["compliance_control_id"]
        )
        if evidence is None:
            outcome, explanation = "not_implemented", "evidence_missing"
        else:
            outcome, explanation = _project_outcome(evidence, exception, now)
            valid_until = _time(evidence["valid_until"])
            if valid_until > now:
                expiry_candidates.append(valid_until)
        if exception is not None:
            expiry_candidates.append(_time(exception["expires_at"]))
        if outcome == "implemented":
            implemented_count += 1
        elif outcome == "evidence_stale":
            stale_count += 1
        elif outcome not in {"not_applicable", "exception_active"}:
            gap_count += 1
        items.append(
            {
                "control_id": str(control["compliance_control_id"]),
                "outcome": outcome,
                "explanation": explanation,
                "evidence_id": None
                if evidence is None
                else str(evidence["compliance_evidence_id"]),
                "evidence_generation": None
                if evidence is None
                else int(evidence["evidence_generation"]),
                "artifact_sha256": None if evidence is None else str(evidence["artifact_sha256"]),
                "classification": None if evidence is None else str(evidence["classification"]),
                "valid_until": None if evidence is None else str(evidence["valid_until"]),
                "exception_id": None
                if exception is None
                else str(exception["compliance_exception_id"]),
                "remediation_id": None
                if remediation is None
                else str(remediation["remediation_task_id"]),
            }
        )
    report_id = _id("crp_")
    stamp = _db_time(now)
    summary_sha256 = _digest(items)
    expires_at = max(now + timedelta(seconds=1), min(expiry_candidates))
    connection.execute(
        """INSERT INTO compliance_reports(
             compliance_report_id,workspace_id,tenant_id,report_version,control_set_version,
             evidence_high_watermark,exception_high_watermark,generator_version,summary_sha256,
             implemented_count,gap_count,stale_count,expires_at,generated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            report_id,
            workspace_id,
            tenant_id,
            version,
            _CONTROL_SET_VERSION,
            evidence_watermark,
            exception_watermark,
            _GENERATOR_VERSION,
            summary_sha256,
            implemented_count,
            gap_count,
            stale_count,
            _db_time(expires_at),
            stamp,
        ),
    )
    connection.executemany(
        """INSERT INTO compliance_report_items(
             compliance_report_id,workspace_id,tenant_id,compliance_control_id,
             report_outcome,explanation_code,compliance_evidence_id,evidence_generation,
             evidence_artifact_sha256,evidence_classification,evidence_valid_until,
             compliance_exception_id,remediation_task_id)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                report_id,
                workspace_id,
                tenant_id,
                item["control_id"],
                item["outcome"],
                item["explanation"],
                item["evidence_id"],
                item["evidence_generation"],
                item["artifact_sha256"],
                item["classification"],
                item["valid_until"],
                item["exception_id"],
                item["remediation_id"],
            )
            for item in items
        ],
    )
    return report_id


def _report_summary(connection, row, *, now: datetime) -> ComplianceReportSummary:
    item_rows = connection.execute(
        """SELECT item.*,control.control_code,control.title
           FROM compliance_report_items item
           JOIN compliance_controls control
             ON control.compliance_control_id=item.compliance_control_id
           WHERE item.compliance_report_id=? ORDER BY control.control_code""",
        (row["compliance_report_id"],),
    ).fetchall()
    stale = (
        _time(row["expires_at"]) <= now
        or int(row["evidence_high_watermark"])
        != _evidence_watermark(connection, str(row["workspace_id"]), str(row["tenant_id"]))
        or int(row["exception_high_watermark"])
        != _exception_watermark(connection, str(row["workspace_id"]), str(row["tenant_id"]))
    )
    return ComplianceReportSummary(
        compliance_report_id=str(row["compliance_report_id"]),
        workspace_id=str(row["workspace_id"]),
        report_version=int(row["report_version"]),
        control_set_version=str(row["control_set_version"]),
        evidence_high_watermark=int(row["evidence_high_watermark"]),
        generator_version=str(row["generator_version"]),
        summary_sha256=str(row["summary_sha256"]),
        implemented_count=int(row["implemented_count"]),
        gap_count=int(row["gap_count"]),
        stale_count=int(row["stale_count"]),
        stale=stale,
        expires_at=_time(row["expires_at"]),
        generated_at=_time(row["generated_at"]),
        items=tuple(
            ComplianceReportItemSummary(
                compliance_control_id=str(item["compliance_control_id"]),
                control_code=str(item["control_code"]),
                title=str(item["title"]),
                report_outcome=str(item["report_outcome"]),
                explanation_code=str(item["explanation_code"]),
                evidence_generation=(
                    None
                    if item["evidence_generation"] is None
                    else int(item["evidence_generation"])
                ),
                evidence_artifact_sha256=item["evidence_artifact_sha256"],
                evidence_classification=item["evidence_classification"],
                evidence_valid_until=(
                    None
                    if item["evidence_valid_until"] is None
                    else _time(item["evidence_valid_until"])
                ),
                compliance_exception_id=item["compliance_exception_id"],
                remediation_task_id=item["remediation_task_id"],
            )
            for item in item_rows
        ),
    )


def _control_summary(connection, row) -> ComplianceControlSummary:
    mappings = connection.execute(
        "SELECT * FROM compliance_control_mappings WHERE compliance_control_id=? "
        "ORDER BY framework_code,clause_code",
        (row["compliance_control_id"],),
    ).fetchall()
    return ComplianceControlSummary(
        compliance_control_id=str(row["compliance_control_id"]),
        control_code=str(row["control_code"]),
        control_version=int(row["control_version"]),
        control_set_version=str(row["control_set_version"]),
        title=str(row["title"]),
        owner_code=str(row["owner_code"]),
        responsibility=str(row["responsibility"]),
        evidence_kind=str(row["evidence_kind"]),
        collector_schema_version=int(row["collector_schema_version"]),
        freshness_seconds=int(row["freshness_seconds"]),
        criticality=str(row["criticality"]),
        evaluation_rule_code=str(row["evaluation_rule_code"]),
        evaluation_rule_sha256=str(row["evaluation_rule_sha256"]),
        mappings=tuple(
            ControlMappingSummary(
                framework_code=str(item["framework_code"]),
                framework_version=str(item["framework_version"]),
                clause_code=str(item["clause_code"]),
                statement_template_code=str(item["statement_template_code"]),
            )
            for item in mappings
        ),
    )


def _evidence_summary(row, now: datetime) -> EvidenceSummary:
    valid_until = _time(row["valid_until"])
    freshness = (
        "expired"
        if valid_until <= now
        else "expiring"
        if valid_until <= now + _EXPIRING_WINDOW
        else "current"
    )
    return EvidenceSummary(
        compliance_evidence_id=str(row["compliance_evidence_id"]),
        workspace_id=str(row["workspace_id"]),
        compliance_control_id=str(row["compliance_control_id"]),
        evidence_sequence=int(row["evidence_sequence"]),
        evidence_generation=int(row["evidence_generation"]),
        collector_service_account_id=str(row["collector_service_account_id"]),
        collector_version=str(row["collector_version"]),
        evidence_kind=str(row["evidence_kind"]),
        schema_version=int(row["schema_version"]),
        artifact_sha256=str(row["artifact_sha256"]),
        classification=str(row["classification"]),
        scope_state=str(row["scope_state"]),
        observed_state=str(row["observed_state"]),
        sample_count=int(row["sample_count"]),
        failed_count=int(row["failed_count"]),
        outcome=str(row["outcome"]),
        outcome_reason_code=str(row["outcome_reason_code"]),
        immutable_receipt_sha256=str(row["immutable_receipt_sha256"]),
        captured_at=_time(row["captured_at"]),
        valid_until=valid_until,
        submitted_at=_time(row["submitted_at"]),
        freshness=freshness,
    )


def _exception_summary(row) -> ComplianceExceptionSummary:
    return ComplianceExceptionSummary(
        compliance_exception_id=str(row["compliance_exception_id"]),
        workspace_id=str(row["workspace_id"]),
        compliance_control_id=str(row["compliance_control_id"]),
        evidence_generation=int(row["evidence_generation"]),
        reason_code=str(row["reason_code"]),
        compensating_controls=tuple(json.loads(str(row["compensating_controls"]))),
        status=str(row["status"]),
        review_generation=int(row["review_generation"]),
        revision=int(row["revision"]),
        starts_at=_time(row["starts_at"]),
        expires_at=_time(row["expires_at"]),
        approved_at=None if row["approved_at"] is None else _time(row["approved_at"]),
        created_at=_time(row["created_at"]),
        updated_at=_time(row["updated_at"]),
    )


def _remediation_summary(row) -> RemediationTaskSummary:
    return RemediationTaskSummary(
        remediation_task_id=str(row["remediation_task_id"]),
        compliance_control_id=str(row["compliance_control_id"]),
        failed_evidence_generation=int(row["failed_evidence_generation"]),
        status=str(row["status"]),
        owner_code=str(row["owner_code"]),
        due_at=_time(row["due_at"]),
        revision=int(row["revision"]),
    )


def _project_outcome(evidence, exception, now: datetime) -> tuple[str, str]:
    if exception is not None:
        return "exception_active", "approved_exception"
    if _time(evidence["valid_until"]) <= now:
        return "evidence_stale", "evidence_expired"
    return {
        "passed": ("implemented", "evidence_passed"),
        "failed": ("not_implemented", "evidence_failed"),
        "inconclusive": ("partially_implemented", "evidence_inconclusive"),
        "not_applicable": ("not_applicable", "scope_not_present"),
    }[str(evidence["outcome"])]


def _evaluate(payload: ComplianceEvidenceSubmitRequest) -> tuple[str, str]:
    if payload.scope_state == "absent":
        return "not_applicable", "scope_not_present"
    if payload.observed_state == "effective":
        return "passed", "all_samples_effective"
    if payload.observed_state == "ineffective":
        return "failed", "sample_failure_observed"
    return "inconclusive", "collector_inconclusive"


def _verify_catalog(connection) -> None:
    controls = connection.execute(
        "SELECT compliance_control_id,control_set_version,status,seeded "
        "FROM compliance_controls ORDER BY compliance_control_id"
    ).fetchall()
    if [str(row["compliance_control_id"]) for row in controls] != list(_CONTROL_IDS) or any(
        row["control_set_version"] != _CONTROL_SET_VERSION
        or row["status"] != "active"
        or int(row["seeded"]) != 1
        for row in controls
    ):
        raise ApiError("compliance.catalog_invalid")
    if connection.execute("SELECT COUNT(*) FROM compliance_control_mappings").fetchone()[0] != 6:
        raise ApiError("compliance.catalog_invalid")


def _control_rows(connection):
    return connection.execute(
        "SELECT * FROM compliance_controls WHERE status='active' ORDER BY control_code"
    ).fetchall()


def _control_row(connection, control_id: str):
    row = connection.execute(
        "SELECT * FROM compliance_controls WHERE compliance_control_id=? AND status='active'",
        (control_id,),
    ).fetchone()
    if row is None:
        raise ApiError("compliance.control_not_found")
    return row


def _workspace_owner(connection, workspace_id: str):
    row = connection.execute(
        "SELECT workspace_id,tenant_id FROM workspace_tenant_bindings "
        "WHERE workspace_id=? AND status='active'",
        (workspace_id,),
    ).fetchone()
    if row is None:
        raise ApiError("compliance.workspace_not_found")
    return row


def _latest_evidence(connection, workspace_id: str, tenant_id: str, control_id: str):
    return connection.execute(
        "SELECT * FROM compliance_evidence WHERE workspace_id=? AND tenant_id=? "
        "AND compliance_control_id=? ORDER BY evidence_generation DESC LIMIT 1",
        (workspace_id, tenant_id, control_id),
    ).fetchone()


def _evidence_row(connection, evidence_id: str):
    row = connection.execute(
        "SELECT * FROM compliance_evidence WHERE compliance_evidence_id=?", (evidence_id,)
    ).fetchone()
    if row is None:
        raise ApiError("compliance.evidence_not_found")
    return row


def _exception_row(connection, exception_id: str):
    row = connection.execute(
        "SELECT * FROM compliance_exceptions WHERE compliance_exception_id=?", (exception_id,)
    ).fetchone()
    if row is None:
        raise ApiError("compliance.exception_not_found")
    return row


def _active_exception(connection, evidence, now: datetime):
    return connection.execute(
        "SELECT * FROM compliance_exceptions WHERE workspace_id=? AND tenant_id=? "
        "AND compliance_control_id=? AND evidence_generation=? AND status='active' "
        "AND starts_at<=? AND expires_at>? ORDER BY review_generation DESC LIMIT 1",
        (
            evidence["workspace_id"],
            evidence["tenant_id"],
            evidence["compliance_control_id"],
            evidence["evidence_generation"],
            _db_time(now),
            _db_time(now),
        ),
    ).fetchone()


def _latest_remediation(connection, workspace_id: str, tenant_id: str, control_id: str):
    return connection.execute(
        "SELECT * FROM compliance_remediation_tasks WHERE workspace_id=? AND tenant_id=? "
        "AND compliance_control_id=? ORDER BY created_at DESC LIMIT 1",
        (workspace_id, tenant_id, control_id),
    ).fetchone()


def _remediation_for_evidence(connection, evidence_id: str):
    return connection.execute(
        "SELECT * FROM compliance_remediation_tasks WHERE failed_evidence_id=?", (evidence_id,)
    ).fetchone()


def _latest_report_row(connection, workspace_id: str, tenant_id: str):
    return connection.execute(
        "SELECT * FROM compliance_reports WHERE workspace_id=? AND tenant_id=? "
        "ORDER BY report_version DESC LIMIT 1",
        (workspace_id, tenant_id),
    ).fetchone()


def _evidence_watermark(connection, workspace_id: str, tenant_id: str) -> int:
    return int(
        connection.execute(
            "SELECT COALESCE(MAX(evidence_sequence),0) FROM compliance_evidence "
            "WHERE workspace_id=? AND tenant_id=?",
            (workspace_id, tenant_id),
        ).fetchone()[0]
    )


def _exception_watermark(connection, workspace_id: str, tenant_id: str) -> int:
    return int(
        connection.execute(
            "SELECT COALESCE(MAX(rowid),0) FROM compliance_exceptions "
            "WHERE workspace_id=? AND tenant_id=?",
            (workspace_id, tenant_id),
        ).fetchone()[0]
    )


def _report_needs_refresh(connection, workspace_id: str, tenant_id: str, now: datetime) -> bool:
    row = _latest_report_row(connection, workspace_id, tenant_id)
    return (
        row is None
        or _time(row["expires_at"]) <= now
        or (
            int(row["evidence_high_watermark"])
            != _evidence_watermark(connection, workspace_id, tenant_id)
            or int(row["exception_high_watermark"])
            != _exception_watermark(connection, workspace_id, tenant_id)
        )
    )


def _command_replay(
    connection,
    *,
    subject_type: str,
    subject_id: str,
    action_code: str,
    idempotency_key: str,
    request_sha256: str,
):
    key_sha256 = hashlib.sha256(idempotency_key.encode("ascii")).hexdigest()
    row = connection.execute(
        "SELECT * FROM compliance_action_commands WHERE subject_type=? AND subject_id=? "
        "AND action_code=? AND idempotency_sha256=?",
        (subject_type, subject_id, action_code, key_sha256),
    ).fetchone()
    if row is not None and row["request_sha256"] != request_sha256:
        raise ApiError("compliance.idempotency_conflict")
    return row


def _write_command(
    connection,
    *,
    workspace_id: str,
    tenant_id: str,
    subject_type: str,
    subject_id: str,
    action_code: str,
    idempotency_key: str,
    request_sha256: str,
    result_type: str,
    result_id: str,
    result_generation: int,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    connection.execute(
        """INSERT INTO compliance_action_commands(
             compliance_action_command_id,workspace_id,tenant_id,subject_type,subject_id,
             action_code,idempotency_sha256,request_sha256,result_type,result_id,
             result_generation,request_id,trace_id,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            _id("cac_"),
            workspace_id,
            tenant_id,
            subject_type,
            subject_id,
            action_code,
            hashlib.sha256(idempotency_key.encode("ascii")).hexdigest(),
            request_sha256,
            result_type,
            result_id,
            result_generation,
            request_id,
            trace_id,
            _db_time(now),
        ),
    )


def _emit_event(
    connection,
    *,
    event_type: str,
    workspace_id: str,
    tenant_id: str,
    object_type: str,
    object_id: str,
    object_generation: int,
    actor_type: str,
    actor_id: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    if connection.execute(
        "SELECT 1 FROM compliance_events WHERE event_type=? AND object_id=? "
        "AND object_generation=?",
        (event_type, object_id, object_generation),
    ).fetchone():
        return
    event_id = _id("cev_")
    stamp = _db_time(now)
    connection.execute(
        """INSERT INTO compliance_events(
             compliance_event_id,event_type,workspace_id,tenant_id,object_type,object_id,
             object_generation,actor_type,actor_id,payload_sha256,request_id,trace_id,occurred_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event_id,
            event_type,
            workspace_id,
            tenant_id,
            object_type,
            object_id,
            object_generation,
            actor_type,
            actor_id,
            _digest(
                {"event_type": event_type, "object_id": object_id, "generation": object_generation}
            ),
            request_id,
            trace_id,
            stamp,
        ),
    )
    connection.execute(
        """INSERT INTO compliance_event_outbox(
             compliance_event_outbox_id,compliance_event_id,workspace_id,tenant_id,status,
             attempt_count,next_attempt_at,delivered_at,last_error_code,revision,created_at,updated_at)
           VALUES(?,?,?,?, 'pending',0,?,NULL,NULL,1,?,?)""",
        (_id("ceo_"), event_id, workspace_id, tenant_id, stamp, stamp, stamp),
    )


def _acquire_lease(config: AppConfig, worker_id: str) -> tuple[int, str] | None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = _database_now(connection)
        row = connection.execute(
            "SELECT * FROM compliance_scheduler_state WHERE singleton_id=1"
        ).fetchone()
        if row is None:
            raise ApiError("compliance.unavailable")
        if (
            row["lease_owner_id"] is not None
            and row["lease_owner_id"] != worker_id
            and _time(row["lease_expires_at"]) > now
        ):
            connection.commit()
            return None
        generation = int(row["fencing_generation"]) + 1
        token_sha256 = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
        changed = _changed_time(now, row["updated_at"])
        connection.execute(
            """UPDATE compliance_scheduler_state
               SET lease_owner_id=?,lease_token_sha256=?,lease_expires_at=?,
                   fencing_generation=?,revision=revision+1,updated_at=?
               WHERE singleton_id=1 AND revision=?""",
            (
                worker_id,
                token_sha256,
                _db_time(now + _LEASE_TTL),
                generation,
                _db_time(changed),
                row["revision"],
            ),
        )
        connection.commit()
        return generation, token_sha256
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("compliance.unavailable") from None
    finally:
        connection.close()


def _require_lease(
    connection, worker_id: str, generation: int, token_sha256: str, now: datetime
) -> None:
    row = connection.execute(
        "SELECT * FROM compliance_scheduler_state WHERE singleton_id=1"
    ).fetchone()
    if (
        row is None
        or row["lease_owner_id"] != worker_id
        or row["lease_token_sha256"] != token_sha256
        or int(row["fencing_generation"]) != generation
        or _time(row["lease_expires_at"]) <= now
    ):
        raise ApiError("compliance.worker_fenced")


def _release_lease(config: AppConfig, worker_id: str, generation: int, token_sha256: str) -> None:
    try:
        connection = _connect(config)
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM compliance_scheduler_state WHERE singleton_id=1"
        ).fetchone()
        if (
            row is not None
            and row["lease_owner_id"] == worker_id
            and row["lease_token_sha256"] == token_sha256
            and int(row["fencing_generation"]) == generation
        ):
            now = _database_now(connection)
            changed = _changed_time(now, row["updated_at"])
            connection.execute(
                "UPDATE compliance_scheduler_state SET lease_owner_id=NULL,"
                "lease_token_sha256=NULL,lease_expires_at=NULL,revision=revision+1,updated_at=? "
                "WHERE singleton_id=1 AND revision=?",
                (_db_time(changed), row["revision"]),
            )
        connection.commit()
        connection.close()
    except Exception:
        return


def _connect(config: AppConfig):
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("compliance.unavailable") from None


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


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ApiError("compliance.request_invalid")
    return value.astimezone(UTC)


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


__all__ = [
    "apply_exception_command",
    "get_platform_controls",
    "list_workspace_reports",
    "process_compliance_once",
    "submit_evidence",
]
