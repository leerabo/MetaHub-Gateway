"""Immutable deployment validation and delivery acceptance for PRD-125."""

from __future__ import annotations

import hmac
import json
import sqlite3
from datetime import timedelta

from metahub_shared.db.errors import DbError
from metahub_shared.private_gateway import (
    VALIDATION_CHECK_CODES,
    VALIDATION_PLAN_VERSION,
    VALIDATION_SCHEMA_VERSION,
    DeploymentValidationReport,
    ValidationCheckEvidence,
    validation_report_sha256,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.identity.security_service import consume_step_up_grant_in_transaction
from metahub_control.workspace_access.service import WorkspaceSessionContext

from .desired_state_service import _authenticated_agent
from .models import (
    EdgeDeliveryAcceptanceRequest,
    EdgeDeliveryAcceptanceResponse,
    EdgeDeliveryAcceptanceSummary,
    EdgeValidationCheckSummary,
    EdgeValidationResponse,
    EdgeValidationRunSummary,
    EdgeValidationStartRequest,
)
from .service import (
    _connect,
    _datetime,
    _db_time,
    _digest,
    _id,
    _identifier,
    _owned_deployment,
    _rollback,
    _secret_hmac,
    _utcnow,
    _workspace_context,
    _workspace_idempotency,
)

_CLOCK_SKEW = timedelta(minutes=5)


def start_workspace_validation(
    config: AppConfig,
    deployment_id: str,
    payload: EdgeValidationStartRequest,
    *,
    session_token: str,
    csrf_token: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> EdgeValidationResponse:
    action = (
        "edge.validation.start"
        if payload.action == "start"
        else "edge.validation.retry_failed"
    )
    request_sha256 = _digest(payload.model_dump(mode="json"))
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = _workspace_context(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            permission="edge.validation.manage",
        )
        idempotency_sha256 = _workspace_idempotency(
            config, context, action, idempotency_key
        )
        replay = _command(connection, context, action, idempotency_sha256)
        if replay is not None:
            if not hmac.compare_digest(str(replay["request_sha256"]), request_sha256):
                raise ApiError("edge.idempotency_conflict")
            row = _owned_validation(
                connection,
                context,
                deployment_id,
                str(replay["result_object_id"]),
            )
            result = _validation_response(
                connection, row, request_id, trace_id, replay=True
            )
            connection.commit()
            return result

        deployment = _owned_deployment(connection, context, deployment_id)
        if str(deployment["status"]) != "active":
            raise ApiError("edge.state_conflict")
        contract = _current_input_contract(connection, deployment)
        input_contract_sha256 = _digest(contract)
        source_id: str | None = None
        attempt = 1
        if payload.action == "start":
            check_codes = VALIDATION_CHECK_CODES
            if connection.execute(
                """
                SELECT 1 FROM edge_validation_runs
                WHERE workspace_id=? AND tenant_id=? AND edge_deployment_id=?
                  AND input_contract_sha256=? AND attempt=1
                """,
                (
                    context.workspace_id,
                    context.tenant_id,
                    deployment_id,
                    input_contract_sha256,
                ),
            ).fetchone():
                raise ApiError("edge.validation_conflict")
        else:
            source_id = str(payload.source_validation_run_id)
            source = _owned_validation(
                connection, context, deployment_id, source_id
            )
            if (
                str(source["status"]) != "completed"
                or str(source["outcome"]) != "failed"
                or not hmac.compare_digest(
                    str(source["input_contract_sha256"]), input_contract_sha256
                )
            ):
                raise ApiError("edge.validation_state_conflict")
            failed = connection.execute(
                """
                SELECT check_code FROM edge_validation_checks
                WHERE workspace_id=? AND tenant_id=? AND validation_run_id=?
                  AND outcome='failed'
                """,
                (context.workspace_id, context.tenant_id, source_id),
            ).fetchall()
            failed_codes = {str(row["check_code"]) for row in failed}
            check_codes = tuple(
                code for code in VALIDATION_CHECK_CODES if code in failed_codes
            )
            if not check_codes:
                raise ApiError("edge.validation_state_conflict")
            attempt = int(source["attempt"]) + 1

        validation_run_id = _id("dvr_")
        now = _db_time(_utcnow())
        connection.execute(
            """
            INSERT INTO edge_validation_runs(
              validation_run_id,workspace_id,tenant_id,edge_deployment_id,
              source_validation_run_id,attempt,plan_version,deployment_revision,
              installer_release_id,desired_generation,applied_generation,lkg_generation,
              input_contract_sha256,requested_check_codes_json,status,outcome,duration_ms,
              requested_by_account_id,idempotency_sha256,request_sha256,started_at,
              completed_at,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?, ?,?,?,?,?,?,?,'requested',NULL,NULL,?,?,?,NULL,NULL,?,?)
            """,
            (
                validation_run_id,
                context.workspace_id,
                context.tenant_id,
                deployment_id,
                source_id,
                attempt,
                VALIDATION_PLAN_VERSION,
                contract["deployment_revision"],
                contract["installer_release_id"],
                contract["desired_generation"],
                contract["applied_generation"],
                contract["lkg_generation"],
                input_contract_sha256,
                _canonical_json(check_codes),
                context.account_id,
                idempotency_sha256,
                request_sha256,
                now,
                now,
            ),
        )
        _insert_command(
            connection,
            context,
            action=action,
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            result_object_id=validation_run_id,
            now=now,
        )
        row = _owned_validation(
            connection, context, deployment_id, validation_run_id
        )
        result = _validation_response(
            connection, row, request_id, trace_id, replay=False
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError as exc:
        _rollback(connection)
        if "UNIQUE constraint failed" in str(exc):
            raise ApiError("edge.validation_conflict") from None
        raise ApiError("edge.unavailable") from None
    except (DbError, sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge.unavailable") from None
    finally:
        connection.close()


def get_workspace_validation(
    config: AppConfig,
    deployment_id: str,
    validation_run_id: str,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> EdgeValidationResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        context = _workspace_context(
            connection,
            config,
            session_token=session_token,
            csrf_token=None,
            permission="edge.validation.read",
        )
        _owned_deployment(connection, context, deployment_id)
        row = _owned_validation(
            connection, context, deployment_id, validation_run_id
        )
        result = _validation_response(
            connection, row, request_id, trace_id, replay=False
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (DbError, sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge.unavailable") from None
    finally:
        connection.close()


def get_agent_validation(
    config: AppConfig,
    deployment_id: str,
    validation_run_id: str,
    *,
    verified_fingerprint: str,
    edge_session_token: str,
    request_id: str,
    trace_id: str,
) -> EdgeValidationResponse:
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
        row = _agent_validation(connection, deployment_id, validation_run_id)
        result = _validation_response(
            connection, row, request_id, trace_id, replay=False
        )
        connection.rollback()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (DbError, sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge.unavailable") from None
    finally:
        connection.close()


def record_agent_validation(
    config: AppConfig,
    deployment_id: str,
    validation_run_id: str,
    report: DeploymentValidationReport,
    *,
    verified_fingerprint: str,
    edge_session_token: str,
    request_id: str,
    trace_id: str,
) -> EdgeValidationResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        agent = _authenticated_agent(
            connection,
            config,
            deployment_id=deployment_id,
            verified_fingerprint=verified_fingerprint,
            edge_session_token=edge_session_token,
        )
        _require_diagnostics_capability(agent)
        row = _agent_validation(connection, deployment_id, validation_run_id)
        _validate_report_binding(row, report)
        if str(row["status"]) == "completed":
            stored = _stored_report(connection, row)
            if not hmac.compare_digest(
                validation_report_sha256(stored), validation_report_sha256(report)
            ):
                raise ApiError("edge.idempotency_conflict")
            result = _validation_response(
                connection, row, request_id, trace_id, replay=True
            )
            connection.rollback()
            return result

        now_value = _utcnow()
        started = _datetime(report.started_at)
        completed = _datetime(report.completed_at)
        if (
            started < _datetime(row["created_at"]) - _CLOCK_SKEW
            or completed > now_value + _CLOCK_SKEW
        ):
            raise ApiError("edge.validation_report_invalid")
        created_at = _db_time(now_value)
        for check in report.checks:
            connection.execute(
                """
                INSERT INTO edge_validation_checks(
                  validation_check_id,workspace_id,tenant_id,edge_deployment_id,
                  validation_run_id,check_code,outcome,reason_code,recovery_action_code,
                  evidence_sha256,duration_ms,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    _id("dvc_"),
                    row["workspace_id"],
                    row["tenant_id"],
                    deployment_id,
                    validation_run_id,
                    check.code,
                    check.outcome,
                    check.reason_code,
                    check.recovery_action_code,
                    check.evidence_sha256,
                    check.duration_ms,
                    created_at,
                ),
            )
        updated = connection.execute(
            """
            UPDATE edge_validation_runs
            SET status='completed',outcome=?,duration_ms=?,started_at=?,completed_at=?,
              updated_at=?
            WHERE validation_run_id=? AND edge_deployment_id=? AND status='requested'
            """,
            (
                report.outcome,
                report.duration_ms,
                report.started_at,
                report.completed_at,
                created_at,
                validation_run_id,
                deployment_id,
            ),
        ).rowcount
        if updated != 1:
            raise ApiError("edge.validation_state_conflict")
        _insert_event(
            connection,
            row,
            event_type="edge.validation_completed",
            object_type="deployment_validation",
            object_id=validation_run_id,
            outcome=report.outcome,
            summary={
                "attempt": report.attempt,
                "check_count": len(report.checks),
                "failed_count": sum(check.outcome == "failed" for check in report.checks),
                "plan_version": report.plan_version,
            },
            request_id=request_id,
            trace_id=trace_id,
            now=created_at,
            domains=(
                ("workspace", "fleet", "security")
                if report.outcome == "failed"
                else ("workspace", "fleet")
            ),
        )
        completed_row = _agent_validation(
            connection, deployment_id, validation_run_id
        )
        result = _validation_response(
            connection, completed_row, request_id, trace_id, replay=False
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError as exc:
        _rollback(connection)
        if "edge_validation" in str(exc):
            raise ApiError("edge.validation_state_conflict") from None
        raise ApiError("edge.unavailable") from None
    except (DbError, sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge.unavailable") from None
    finally:
        connection.close()


def accept_workspace_delivery(
    config: AppConfig,
    deployment_id: str,
    payload: EdgeDeliveryAcceptanceRequest,
    *,
    session_token: str,
    csrf_token: str,
    step_up_grant: str,
    expected_revision: int,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> EdgeDeliveryAcceptanceResponse:
    action = "edge.delivery.accept"
    request_sha256 = _digest(payload.model_dump(mode="json"))
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = _workspace_context(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            permission="edge.delivery.accept",
        )
        idempotency_sha256 = _workspace_idempotency(
            config, context, action, idempotency_key
        )
        replay = _command(connection, context, action, idempotency_sha256)
        if replay is not None:
            if not hmac.compare_digest(str(replay["request_sha256"]), request_sha256):
                raise ApiError("edge.idempotency_conflict")
            acceptance = _owned_acceptance(
                connection, context, str(replay["result_object_id"])
            )
            result = EdgeDeliveryAcceptanceResponse(
                acceptance=_acceptance_summary(acceptance),
                idempotent_replay=True,
                request_id=_identifier(request_id),
                trace_id=_identifier(trace_id),
            )
            connection.commit()
            return result

        deployment = _owned_deployment(connection, context, deployment_id)
        if (
            str(deployment["status"]) != "active"
            or int(deployment["revision"]) != expected_revision
        ):
            raise ApiError("edge.revision_conflict")
        validation = _owned_validation(
            connection, context, deployment_id, payload.validation_run_id
        )
        if str(validation["status"]) != "completed":
            raise ApiError("edge.validation_state_conflict")
        contract = _current_input_contract(connection, deployment)
        if not hmac.compare_digest(
            str(validation["input_contract_sha256"]), _digest(contract)
        ):
            raise ApiError("edge.validation_state_conflict")
        effective = _effective_check_rows(connection, validation)
        if (
            tuple(effective) != VALIDATION_CHECK_CODES
            or any(str(row["outcome"]) != "passed" for row in effective.values())
        ):
            raise ApiError("edge.validation_failed")
        validation_set_sha256 = _validation_set_sha256(validation, effective)
        existing = connection.execute(
            """
            SELECT * FROM edge_delivery_acceptances
            WHERE workspace_id=? AND tenant_id=? AND edge_deployment_id=?
              AND validation_set_sha256=?
            """,
            (
                context.workspace_id,
                context.tenant_id,
                deployment_id,
                validation_set_sha256,
            ),
        ).fetchone()
        if existing is not None:
            raise ApiError("edge.delivery_already_accepted")

        now_value = _utcnow()
        consume_step_up_grant_in_transaction(
            connection,
            config,
            context=context,
            grant=step_up_grant,
            action_code=action,
            resource_type="edge_deployment",
            resource_id=deployment_id,
            now=now_value,
        )
        membership = connection.execute(
            """
            SELECT revision FROM workspace_memberships
            WHERE workspace_membership_id=? AND workspace_id=? AND tenant_id=?
              AND status='active'
            """,
            (context.membership_id, context.workspace_id, context.tenant_id),
        ).fetchone()
        if membership is None:
            raise ApiError("workspace_session.permission_denied")
        acceptance_id = _id("eda_")
        now = _db_time(now_value)
        connection.execute(
            """
            INSERT INTO edge_delivery_acceptances(
              delivery_acceptance_id,workspace_id,tenant_id,edge_deployment_id,
              validation_run_id,validation_set_sha256,deployment_revision,
              installer_release_id,desired_generation,applied_generation,lkg_generation,
              accepted_by_account_id,accepted_by_membership_id,membership_revision,
              step_up_grant_sha256,idempotency_sha256,request_sha256,accepted_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                acceptance_id,
                context.workspace_id,
                context.tenant_id,
                deployment_id,
                payload.validation_run_id,
                validation_set_sha256,
                contract["deployment_revision"],
                contract["installer_release_id"],
                contract["desired_generation"],
                contract["applied_generation"],
                contract["lkg_generation"],
                context.account_id,
                context.membership_id,
                int(membership["revision"]),
                _secret_hmac(config, "delivery-acceptance-step-up", step_up_grant),
                idempotency_sha256,
                request_sha256,
                now,
            ),
        )
        _insert_command(
            connection,
            context,
            action=action,
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            result_object_id=acceptance_id,
            now=now,
        )
        _insert_event(
            connection,
            validation,
            event_type="edge.delivery_accepted",
            object_type="delivery_acceptance",
            object_id=acceptance_id,
            outcome="accepted",
            summary={
                "check_count": len(effective),
                "deployment_revision": contract["deployment_revision"],
                "validation_run_id": payload.validation_run_id,
            },
            request_id=request_id,
            trace_id=trace_id,
            now=now,
            domains=("workspace", "fleet"),
        )
        acceptance = _owned_acceptance(connection, context, acceptance_id)
        result = EdgeDeliveryAcceptanceResponse(
            acceptance=_acceptance_summary(acceptance),
            request_id=_identifier(request_id),
            trace_id=_identifier(trace_id),
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError as exc:
        _rollback(connection)
        if "UNIQUE constraint failed" in str(exc):
            raise ApiError("edge.delivery_already_accepted") from None
        raise ApiError("edge.unavailable") from None
    except (DbError, sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge.unavailable") from None
    finally:
        connection.close()


def _current_input_contract(
    connection: sqlite3.Connection, deployment: sqlite3.Row
) -> dict[str, object]:
    owner = (
        str(deployment["workspace_id"]),
        str(deployment["tenant_id"]),
        str(deployment["edge_deployment_id"]),
    )
    installation = connection.execute(
        """
        SELECT * FROM edge_installations
        WHERE workspace_id=? AND tenant_id=? AND edge_deployment_id=? AND status='active'
        ORDER BY generation DESC,installation_id DESC LIMIT 1
        """,
        owner,
    ).fetchone()
    head = connection.execute(
        """
        SELECT * FROM edge_desired_state_heads
        WHERE workspace_id=? AND tenant_id=? AND edge_deployment_id=?
        """,
        owner,
    ).fetchone()
    identity = connection.execute(
        """
        SELECT identity.edge_device_identity_id,identity.generation identity_generation,
          certificate.edge_device_certificate_id,certificate.generation certificate_generation,
          certificate.certificate_sha256,certificate.not_after
        FROM edge_device_identities identity
        JOIN edge_device_certificates certificate
          ON certificate.edge_device_identity_id=identity.edge_device_identity_id
        WHERE identity.workspace_id=? AND identity.tenant_id=?
          AND identity.edge_deployment_id=? AND identity.status='active'
          AND certificate.status='active'
        ORDER BY identity.generation DESC,certificate.generation DESC LIMIT 1
        """,
        owner,
    ).fetchone()
    if installation is None or head is None or identity is None:
        raise ApiError("edge.validation_not_ready")
    return {
        "schema_version": VALIDATION_SCHEMA_VERSION,
        "plan_version": VALIDATION_PLAN_VERSION,
        "deployment_id": owner[2],
        "deployment_revision": int(deployment["revision"]),
        "revocation_generation": int(deployment["revocation_generation"]),
        "installation_id": str(installation["installation_id"]),
        "installation_generation": int(installation["generation"]),
        "installer_release_id": str(installation["installer_release_id"]),
        "desired_state_id": str(head["edge_desired_state_id"]),
        "signed_snapshot_id": str(head["edge_signed_snapshot_id"]),
        "desired_envelope_sha256": str(head["desired_envelope_sha256"]),
        "desired_generation": int(head["generation"]),
        "applied_generation": int(head["applied_generation"]),
        "lkg_generation": int(head["lkg_generation"]),
        "identity_id": str(identity["edge_device_identity_id"]),
        "identity_generation": int(identity["identity_generation"]),
        "certificate_id": str(identity["edge_device_certificate_id"]),
        "certificate_generation": int(identity["certificate_generation"]),
        "certificate_sha256": str(identity["certificate_sha256"]),
        "certificate_not_after": str(identity["not_after"]),
    }


def _validate_report_binding(
    row: sqlite3.Row, report: DeploymentValidationReport
) -> None:
    requested_codes = _requested_codes(row)
    if (
        report.validation_id != str(row["validation_run_id"])
        or report.deployment_id != str(row["edge_deployment_id"])
        or report.plan_version != str(row["plan_version"])
        or report.attempt != int(row["attempt"])
        or not hmac.compare_digest(
            report.input_contract_sha256, str(row["input_contract_sha256"])
        )
        or tuple(check.code for check in report.checks) != requested_codes
    ):
        raise ApiError("edge.validation_report_invalid")


def _stored_report(
    connection: sqlite3.Connection, row: sqlite3.Row
) -> DeploymentValidationReport:
    checks = tuple(
        ValidationCheckEvidence(
            code=str(check["check_code"]),
            outcome=str(check["outcome"]),
            reason_code=str(check["reason_code"]),
            recovery_action_code=(
                str(check["recovery_action_code"])
                if check["recovery_action_code"] is not None
                else None
            ),
            evidence_sha256=str(check["evidence_sha256"]),
            duration_ms=int(check["duration_ms"]),
        )
        for check in _check_rows(connection, row)
    )
    return DeploymentValidationReport(
        schema_version=VALIDATION_SCHEMA_VERSION,
        validation_id=str(row["validation_run_id"]),
        deployment_id=str(row["edge_deployment_id"]),
        plan_version=VALIDATION_PLAN_VERSION,
        attempt=int(row["attempt"]),
        input_contract_sha256=str(row["input_contract_sha256"]),
        outcome=str(row["outcome"]),
        checks=checks,
        started_at=str(row["started_at"]),
        completed_at=str(row["completed_at"]),
        duration_ms=int(row["duration_ms"]),
    )


def _validation_response(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    request_id: str,
    trace_id: str,
    *,
    replay: bool,
) -> EdgeValidationResponse:
    acceptance = connection.execute(
        """
        SELECT * FROM edge_delivery_acceptances
        WHERE workspace_id=? AND tenant_id=? AND validation_run_id=?
        """,
        (row["workspace_id"], row["tenant_id"], row["validation_run_id"]),
    ).fetchone()
    return EdgeValidationResponse(
        validation=_validation_summary(connection, row),
        delivery_acceptance=(
            _acceptance_summary(acceptance) if acceptance is not None else None
        ),
        idempotent_replay=replay,
        request_id=_identifier(request_id),
        trace_id=_identifier(trace_id),
    )


def _validation_summary(
    connection: sqlite3.Connection, row: sqlite3.Row
) -> EdgeValidationRunSummary:
    return EdgeValidationRunSummary(
        validation_run_id=str(row["validation_run_id"]),
        deployment_id=str(row["edge_deployment_id"]),
        source_validation_run_id=(
            str(row["source_validation_run_id"])
            if row["source_validation_run_id"] is not None
            else None
        ),
        attempt=int(row["attempt"]),
        plan_version=str(row["plan_version"]),
        deployment_revision=int(row["deployment_revision"]),
        installer_release_id=str(row["installer_release_id"]),
        desired_generation=int(row["desired_generation"]),
        applied_generation=int(row["applied_generation"]),
        lkg_generation=int(row["lkg_generation"]),
        input_contract_sha256=str(row["input_contract_sha256"]),
        requested_check_codes=_requested_codes(row),
        status=str(row["status"]),
        outcome=str(row["outcome"]) if row["outcome"] is not None else None,
        duration_ms=int(row["duration_ms"]) if row["duration_ms"] is not None else None,
        checks=tuple(_check_summary(item) for item in _check_rows(connection, row)),
        effective_checks=tuple(
            _check_summary(item)
            for item in _effective_check_rows(connection, row).values()
        ),
        started_at=_datetime(row["started_at"]) if row["started_at"] else None,
        completed_at=_datetime(row["completed_at"]) if row["completed_at"] else None,
        created_at=_datetime(row["created_at"]),
        updated_at=_datetime(row["updated_at"]),
    )


def _check_summary(row: sqlite3.Row) -> EdgeValidationCheckSummary:
    return EdgeValidationCheckSummary(
        validation_check_id=str(row["validation_check_id"]),
        code=str(row["check_code"]),
        outcome=str(row["outcome"]),
        reason_code=str(row["reason_code"]),
        recovery_action_code=(
            str(row["recovery_action_code"])
            if row["recovery_action_code"] is not None
            else None
        ),
        evidence_sha256=str(row["evidence_sha256"]),
        duration_ms=int(row["duration_ms"]),
        source_validation_run_id=str(row["validation_run_id"]),
        created_at=_datetime(row["created_at"]),
    )


def _acceptance_summary(row: sqlite3.Row) -> EdgeDeliveryAcceptanceSummary:
    return EdgeDeliveryAcceptanceSummary(
        delivery_acceptance_id=str(row["delivery_acceptance_id"]),
        validation_run_id=str(row["validation_run_id"]),
        validation_set_sha256=str(row["validation_set_sha256"]),
        deployment_revision=int(row["deployment_revision"]),
        installer_release_id=str(row["installer_release_id"]),
        desired_generation=int(row["desired_generation"]),
        applied_generation=int(row["applied_generation"]),
        lkg_generation=int(row["lkg_generation"]),
        accepted_by_account_id=str(row["accepted_by_account_id"]),
        accepted_at=_datetime(row["accepted_at"]),
    )


def _effective_check_rows(
    connection: sqlite3.Connection, current: sqlite3.Row
) -> dict[str, sqlite3.Row]:
    lineage: list[sqlite3.Row] = []
    seen: set[str] = set()
    row = current
    expected_attempt = int(current["attempt"])
    contract = str(current["input_contract_sha256"])
    while True:
        run_id = str(row["validation_run_id"])
        if (
            run_id in seen
            or int(row["attempt"]) != expected_attempt
            or str(row["input_contract_sha256"]) != contract
        ):
            raise ApiError("edge.validation_integrity_failed")
        seen.add(run_id)
        lineage.append(row)
        source_id = row["source_validation_run_id"]
        if source_id is None:
            if expected_attempt != 1:
                raise ApiError("edge.validation_integrity_failed")
            break
        expected_attempt -= 1
        source = connection.execute(
            """
            SELECT * FROM edge_validation_runs
            WHERE workspace_id=? AND tenant_id=? AND edge_deployment_id=?
              AND validation_run_id=?
            """,
            (
                current["workspace_id"],
                current["tenant_id"],
                current["edge_deployment_id"],
                source_id,
            ),
        ).fetchone()
        if source is None:
            raise ApiError("edge.validation_integrity_failed")
        row = source
    root_codes = _requested_codes(lineage[-1])
    if root_codes != VALIDATION_CHECK_CODES:
        raise ApiError("edge.validation_integrity_failed")
    effective: dict[str, sqlite3.Row] = {}
    for run in reversed(lineage):
        for check in _check_rows(connection, run):
            effective[str(check["check_code"])] = check
    return {
        code: effective[code]
        for code in VALIDATION_CHECK_CODES
        if code in effective
    }


def _validation_set_sha256(
    validation: sqlite3.Row, effective: dict[str, sqlite3.Row]
) -> str:
    return _digest(
        {
            "input_contract_sha256": str(validation["input_contract_sha256"]),
            "checks": [
                {
                    "code": code,
                    "outcome": str(row["outcome"]),
                    "reason_code": str(row["reason_code"]),
                    "evidence_sha256": str(row["evidence_sha256"]),
                    "source_validation_run_id": str(row["validation_run_id"]),
                }
                for code, row in effective.items()
            ],
        }
    )


def _check_rows(
    connection: sqlite3.Connection, row: sqlite3.Row
) -> tuple[sqlite3.Row, ...]:
    rows = connection.execute(
        """
        SELECT * FROM edge_validation_checks
        WHERE workspace_id=? AND tenant_id=? AND validation_run_id=?
        """,
        (row["workspace_id"], row["tenant_id"], row["validation_run_id"]),
    ).fetchall()
    by_code = {str(check["check_code"]): check for check in rows}
    return tuple(by_code[code] for code in VALIDATION_CHECK_CODES if code in by_code)


def _requested_codes(row: sqlite3.Row) -> tuple[str, ...]:
    values = json.loads(str(row["requested_check_codes_json"]))
    if (
        type(values) is not list
        or not values
        or any(type(value) is not str for value in values)
        or len(set(values)) != len(values)
        or tuple(values) != tuple(
            code for code in VALIDATION_CHECK_CODES if code in set(values)
        )
    ):
        raise ApiError("edge.validation_integrity_failed")
    return tuple(values)


def _owned_validation(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    deployment_id: str,
    validation_run_id: str,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT * FROM edge_validation_runs
        WHERE workspace_id=? AND tenant_id=? AND edge_deployment_id=?
          AND validation_run_id=?
        """,
        (
            context.workspace_id,
            context.tenant_id,
            deployment_id,
            validation_run_id,
        ),
    ).fetchone()
    if row is None:
        raise ApiError("edge.validation_not_found")
    return row


def _agent_validation(
    connection: sqlite3.Connection, deployment_id: str, validation_run_id: str
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT * FROM edge_validation_runs
        WHERE edge_deployment_id=? AND validation_run_id=?
        """,
        (deployment_id, validation_run_id),
    ).fetchone()
    if row is None:
        raise ApiError("edge.validation_not_found")
    return row


def _owned_acceptance(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    acceptance_id: str,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT * FROM edge_delivery_acceptances
        WHERE workspace_id=? AND tenant_id=? AND delivery_acceptance_id=?
        """,
        (context.workspace_id, context.tenant_id, acceptance_id),
    ).fetchone()
    if row is None:
        raise ApiError("edge.delivery_acceptance_not_found")
    return row


def _command(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    action: str,
    idempotency_sha256: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT * FROM edge_validation_commands
        WHERE workspace_id=? AND tenant_id=? AND actor_account_id=?
          AND action_code=? AND idempotency_sha256=?
        """,
        (
            context.workspace_id,
            context.tenant_id,
            context.account_id,
            action,
            idempotency_sha256,
        ),
    ).fetchone()


def _insert_command(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    *,
    action: str,
    idempotency_sha256: str,
    request_sha256: str,
    result_object_id: str,
    now: str,
) -> None:
    connection.execute(
        """
        INSERT INTO edge_validation_commands(
          edge_validation_command_id,workspace_id,tenant_id,actor_account_id,
          action_code,idempotency_sha256,request_sha256,result_object_id,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            _id("evc_"),
            context.workspace_id,
            context.tenant_id,
            context.account_id,
            action,
            idempotency_sha256,
            request_sha256,
            result_object_id,
            now,
        ),
    )


def _insert_event(
    connection: sqlite3.Connection,
    owner: sqlite3.Row,
    *,
    event_type: str,
    object_type: str,
    object_id: str,
    outcome: str,
    summary: dict[str, object],
    request_id: str,
    trace_id: str,
    now: str,
    domains: tuple[str, ...],
) -> None:
    event_id = _id("ede_")
    connection.execute(
        """
        INSERT INTO edge_delivery_events(
          edge_delivery_event_id,workspace_id,tenant_id,edge_deployment_id,event_type,
          object_type,object_id,outcome_code,summary_json,request_id,trace_id,occurred_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id,
            owner["workspace_id"],
            owner["tenant_id"],
            owner["edge_deployment_id"],
            event_type,
            object_type,
            object_id,
            outcome,
            _canonical_json(summary),
            _identifier(request_id),
            _identifier(trace_id),
            now,
        ),
    )
    for domain in domains:
        connection.execute(
            """
            INSERT INTO edge_delivery_outbox(
              edge_delivery_outbox_id,workspace_id,tenant_id,edge_delivery_event_id,
              delivery_domain,status,attempt_count,available_at,delivered_at,
              last_error_code,created_at,updated_at)
            VALUES(?,?,?, ?,?,'pending',0,?,NULL,NULL,?,?)
            """,
            (
                _id("edo_"),
                owner["workspace_id"],
                owner["tenant_id"],
                event_id,
                domain,
                now,
                now,
                now,
            ),
        )


def _require_diagnostics_capability(agent: sqlite3.Row) -> None:
    try:
        capabilities = json.loads(str(agent["capability_codes_json"]))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        raise ApiError("edge.authentication_failed") from None
    if (
        type(capabilities) is not list
        or any(type(value) is not str for value in capabilities)
        or "diagnostics" not in capabilities
    ):
        raise ApiError("edge.authentication_failed")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


__all__ = (
    "accept_workspace_delivery",
    "get_agent_validation",
    "get_workspace_validation",
    "record_agent_validation",
    "start_workspace_validation",
)
