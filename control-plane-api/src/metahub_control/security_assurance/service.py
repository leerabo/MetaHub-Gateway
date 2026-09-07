"""Owner-qualified fixed-profile assurance execution and remediation closure."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.schema import table_exists
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.identity.security_service import (
    consume_global_step_up_grant_in_transaction,
)
from metahub_control.platform_access.service import (
    PlatformContext,
    authenticate_platform_session_in_transaction,
)
from metahub_control.workspace_access.service import (
    authenticate_workspace_session_in_transaction,
)

from .models import (
    AssessmentCreateCommand,
    AssessmentResponse,
    IsolationRunSummary,
    PenTestFindingSummary,
    RemediationCommand,
    RemediationResponse,
    RemediationSummary,
    RiskAcceptanceCommand,
    RiskAcceptanceSummary,
    ScenarioExecution,
    SecurityAssessmentSummary,
)
from .receipts import (
    UNKNOWN_SHA256,
    ExecutionReceipt,
    FileAssuranceReceiptVerifier,
)
from .receipts import (
    digest as receipt_digest,
)

_ASSESSMENT_ID = re.compile(r"^sas_[0-9a-f]{32}$")
_FINDING_ID = re.compile(r"^ptf_[0-9a-f]{32}$")


@dataclass(frozen=True, slots=True)
class RegisteredScenario:
    scenario_code: str
    target_kind: str
    owner_kind: str
    contract_code: str
    weakness_code: str
    signature_sha256: str


@dataclass(frozen=True, slots=True)
class AssessmentExecutionRequest:
    security_assessment_id: str
    workspace_id: str
    tenant_id: str
    profile_code: str
    profile_version: int
    profile_sha256: str
    profile_signature_sha256: str
    generation: int
    artifact_sha256: str
    code_tree_sha256: str
    migration_set_sha256: str
    contract_set_sha256: str
    lease_challenge_sha256: str
    started_at: datetime
    deadline_at: datetime
    scenarios: tuple[RegisteredScenario, ...]


AssuranceRunner = Callable[[AssessmentExecutionRequest], bytes]


def create_security_assessment(
    config: AppConfig,
    command: AssessmentCreateCommand,
    *,
    platform_session_token: str,
    platform_csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
    runner: AssuranceRunner | None = None,
) -> AssessmentResponse:
    request_sha = _digest(command.model_dump(mode="json"))
    key_sha = hashlib.sha256(idempotency_key.encode("ascii")).hexdigest()
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _platform_actor(
            connection,
            config,
            platform_session_token,
            platform_csrf_token,
            "platform.assurance.run",
        )
        _require_schema(connection)
        replay = _command_replay(
            connection,
            "platform_operator",
            actor.operator_id,
            "assessment.create",
            key_sha,
            request_sha,
        )
        if replay is not None:
            response = _assessment_response(
                connection, replay, request_id=request_id, trace_id=trace_id, replay=True
            )
            connection.commit()
            return response
        owner = connection.execute(
            """SELECT binding.workspace_id,binding.tenant_id
                 FROM workspace_tenant_bindings binding
                 JOIN workspaces workspace ON workspace.workspace_id=binding.workspace_id
                WHERE binding.workspace_id=? AND binding.status='active'
                  AND workspace.status='active'""",
            (command.workspace_id,),
        ).fetchone()
        if owner is None:
            raise ApiError("security_assurance.workspace_not_found")
        profile = connection.execute(
            """SELECT * FROM security_assurance_profiles
                WHERE profile_code=? AND profile_version=? AND status='active'""",
            (command.profile_code, command.profile_version),
        ).fetchone()
        if profile is None:
            raise ApiError("security_assurance.profile_invalid")
        scenarios = _registered_scenarios(connection)
        if len(scenarios) != 10:
            raise ApiError("security_assurance.registry_invalid")
        now = _utcnow()
        consume_global_step_up_grant_in_transaction(
            connection,
            config,
            account_id=actor.account_id,
            global_session_id=actor.global_session_id,
            grant=step_up_grant,
            action_code="platform.assurance.run",
            resource_type="security_assurance_profile",
            resource_id=command.profile_code,
            now=now,
        )
        generation = int(
            connection.execute(
                """SELECT COALESCE(MAX(generation),0)+1 FROM security_assessments
                    WHERE workspace_id=? AND tenant_id=? AND profile_code=?""",
                (owner["workspace_id"], owner["tenant_id"], command.profile_code),
            ).fetchone()[0]
        )
        scope_sha = _digest(
            {
                "profile_code": command.profile_code,
                "profile_version": command.profile_version,
                "scenarios": [
                    {
                        "scenario_code": item.scenario_code,
                        "signature_sha256": item.signature_sha256,
                    }
                    for item in scenarios
                ],
                "workspace_id": owner["workspace_id"],
            }
        )
        assessment_id = _id("sas_")
        stamp = _db_time(now)
        connection.execute(
            """INSERT INTO security_assessments(
                 security_assessment_id,workspace_id,tenant_id,profile_code,profile_version,
                 profile_sha256,profile_signature_sha256,code_tree_sha256,
                 migration_set_sha256,contract_set_sha256,artifact_sha256,scope_sha256,
                 status,generation,requester_operator_id,environment_receipt_sha256,
                 summary_sha256,revision,requested_at,started_at,finished_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'queued',?,?,NULL,NULL,1,?,NULL,NULL,?)""",
            (
                assessment_id,
                owner["workspace_id"],
                owner["tenant_id"],
                command.profile_code,
                command.profile_version,
                profile["profile_sha256"],
                profile["signature_sha256"],
                command.code_tree_sha256,
                command.migration_set_sha256,
                command.contract_set_sha256,
                command.artifact_sha256,
                scope_sha,
                generation,
                actor.operator_id,
                stamp,
                stamp,
            ),
        )
        _record_command(
            connection,
            "platform_operator",
            actor.operator_id,
            owner["workspace_id"],
            owner["tenant_id"],
            "assessment.create",
            key_sha,
            request_sha,
            assessment_id,
            1,
            now,
        )
        connection.commit()
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("security_assurance.unavailable") from None
    finally:
        connection.close()

    execute_security_assessment(
        config,
        assessment_id,
        runner=runner,
        request_id=request_id,
        trace_id=trace_id,
    )
    return get_security_assessment(
        config,
        assessment_id,
        platform_session_token=platform_session_token,
        request_id=request_id,
        trace_id=trace_id,
    )


def get_security_assessment(
    config: AppConfig,
    assessment_id: str,
    *,
    platform_session_token: str,
    request_id: str,
    trace_id: str,
) -> AssessmentResponse:
    _assessment_id(assessment_id)
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        _platform_actor(
            connection,
            config,
            platform_session_token,
            None,
            "platform.assurance.read",
        )
        _require_schema(connection)
        response = _assessment_response(
            connection, assessment_id, request_id=request_id, trace_id=trace_id
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("security_assurance.unavailable") from None
    finally:
        connection.close()


def execute_security_assessment(
    config: AppConfig,
    assessment_id: str,
    *,
    runner: AssuranceRunner | None,
    request_id: str,
    trace_id: str,
) -> None:
    _assessment_id(assessment_id)
    lease_token = secrets.token_urlsafe(32)
    lease_sha = hashlib.sha256(lease_token.encode("ascii")).hexdigest()
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        assessment = _assessment_row(connection, assessment_id)
        if assessment["status"] != "queued":
            raise ApiError("security_assurance.assessment_conflict")
        if not _requester_can_run(connection, assessment):
            raise ApiError("security_assurance.permission_revoked")
        maximum_duration_seconds = int(assessment["maximum_duration_seconds"])
        if maximum_duration_seconds not in {1800, 14_400}:
            raise ApiError("security_assurance.registry_invalid")
        now = _utcnow()
        stamp = _db_time(now)
        updated = connection.execute(
            """UPDATE security_assessments
                  SET status='running',started_at=?,revision=revision+1,updated_at=?
                WHERE security_assessment_id=? AND status='queued' AND revision=1""",
            (stamp, stamp, assessment_id),
        )
        if updated.rowcount != 1:
            raise ApiError("security_assurance.assessment_conflict")
        connection.execute(
            """INSERT INTO assurance_runner_leases(
                 security_assessment_id,workspace_id,tenant_id,worker_id,
                 lease_token_sha256,fencing_generation,lease_expires_at,
                 permission_revision,created_at,updated_at)
               VALUES(?,?,?,?,?,1,?,?,?,?)""",
            (
                assessment_id,
                assessment["workspace_id"],
                assessment["tenant_id"],
                "security-assurance-inline-v1",
                lease_sha,
                _db_time(now + timedelta(seconds=maximum_duration_seconds)),
                int(assessment["requester_revision"]),
                stamp,
                stamp,
            ),
        )
        scenarios = _registered_scenarios(connection)
        execution_request = AssessmentExecutionRequest(
            security_assessment_id=assessment_id,
            workspace_id=str(assessment["workspace_id"]),
            tenant_id=str(assessment["tenant_id"]),
            profile_code=str(assessment["profile_code"]),
            profile_version=int(assessment["profile_version"]),
            profile_sha256=str(assessment["profile_sha256"]),
            profile_signature_sha256=str(assessment["profile_signature_sha256"]),
            generation=int(assessment["generation"]),
            artifact_sha256=str(assessment["artifact_sha256"]),
            code_tree_sha256=str(assessment["code_tree_sha256"]),
            migration_set_sha256=str(assessment["migration_set_sha256"]),
            contract_set_sha256=str(assessment["contract_set_sha256"]),
            lease_challenge_sha256=lease_sha,
            started_at=now,
            deadline_at=now + timedelta(seconds=maximum_duration_seconds),
            scenarios=scenarios,
        )
        connection.commit()
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("security_assurance.unavailable") from None
    finally:
        connection.close()

    receipt = None
    try:
        trust_path = config.assurance_receipt_trust_bundle_path
        if runner is None or not trust_path:
            raise ValueError("runner or receipt trust unavailable")
        receipt = FileAssuranceReceiptVerifier(Path(trust_path))(
            execution_request, runner(execution_request),
        )
        results = tuple(
            run.result if all(item.cleanup_status == "deleted" for item in run.fixtures)
            else ScenarioExecution(
                scenario_code=run.result.scenario_code, outcome="error", severity="p0",
                duration_ms=run.result.duration_ms, evidence_sha256=run.cleanup_digest(),
            )
            for run in receipt.runs
        )
    except Exception:
        results = tuple(
            ScenarioExecution(
                scenario_code=scenario.scenario_code,
                outcome="error",
                severity="p0",
                duration_ms=0,
                evidence_sha256=_digest(
                    {"assessment": assessment_id, "scenario": scenario.scenario_code, "error": True}
                ),
            )
            for scenario in execution_request.scenarios
        )
    _persist_execution(
        config,
        execution_request,
        results,
        receipt=receipt,
        lease_sha=lease_sha,
        request_id=request_id,
        trace_id=trace_id,
    )


def apply_remediation(
    config: AppConfig,
    finding_id: str,
    command: RemediationCommand,
    *,
    platform_session_token: str,
    platform_csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> RemediationResponse:
    _finding_id(finding_id)
    request_sha = _digest(
        {"finding_id": finding_id, "command": command.model_dump(mode="json")}
    )
    key_sha = hashlib.sha256(idempotency_key.encode("ascii")).hexdigest()
    action = f"remediation.{command.operation}"
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _platform_actor(
            connection,
            config,
            platform_session_token,
            platform_csrf_token,
            "platform.assurance.remediate",
        )
        _require_schema(connection)
        replay = _command_replay(
            connection, "platform_operator", actor.operator_id, action, key_sha, request_sha
        )
        if replay is not None:
            remediation = _remediation_row(connection, replay)
            response = _remediation_response(
                connection,
                remediation,
                request_id=request_id,
                trace_id=trace_id,
                replay=True,
            )
            connection.commit()
            return response
        finding = _finding_row(connection, finding_id)
        if int(finding["revision"]) != command.expected_revision:
            raise ApiError("security_assurance.revision_conflict")
        now = _utcnow()
        consume_global_step_up_grant_in_transaction(
            connection,
            config,
            account_id=actor.account_id,
            global_session_id=actor.global_session_id,
            grant=step_up_grant,
            action_code="platform.assurance.remediate",
            resource_type="pen_test_finding",
            resource_id=finding_id,
            now=now,
        )
        stamp = _db_time(now)
        remediation_id = _id("rem_")
        if command.operation == "propose":
            if finding["status"] not in {"open", "risk_accepted"}:
                raise ApiError("security_assurance.finding_conflict")
            updated = connection.execute(
                """UPDATE pen_test_findings
                      SET status='remediation_pending',revision=revision+1,last_seen_at=?
                    WHERE pen_test_finding_id=? AND revision=?""",
                (stamp, finding_id, command.expected_revision),
            )
            if updated.rowcount != 1:
                raise ApiError("security_assurance.revision_conflict")
            connection.execute(
                """INSERT INTO security_remediations(
                     remediation_id,pen_test_finding_id,workspace_id,tenant_id,change_sha256,
                     verification_assessment_id,outcome,requester_operator_id,
                     approver_operator_id,finding_revision,created_at,verified_at)
                   VALUES(?,?,?,?,?,NULL,'proposed',?,NULL,?, ?,NULL)""",
                (
                    remediation_id,
                    finding_id,
                    finding["workspace_id"],
                    finding["tenant_id"],
                    command.change_sha256,
                    actor.operator_id,
                    command.expected_revision + 1,
                    stamp,
                ),
            )
            result_revision = command.expected_revision + 1
        else:
            if finding["status"] != "remediation_pending":
                raise ApiError("security_assurance.finding_conflict")
            proposal = _remediation_row(connection, str(command.remediation_id))
            if (
                proposal["pen_test_finding_id"] != finding_id
                or proposal["outcome"] != "proposed"
                or proposal["requester_operator_id"] == actor.operator_id
            ):
                raise ApiError("security_assurance.remediation_conflict")
            verification = _assessment_row(connection, str(command.verification_assessment_id))
            original = _assessment_row(connection, str(finding["security_assessment_id"]))
            if (
                verification["workspace_id"] != finding["workspace_id"]
                or verification["tenant_id"] != finding["tenant_id"]
                or verification["profile_code"] != original["profile_code"]
                or verification["profile_version"] != original["profile_version"]
                or int(verification["generation"]) <= int(original["generation"])
                or verification["status"] not in {"passed", "failed"}
                or _parse_time(verification["requested_at"]) <= _parse_time(proposal["created_at"])
            ):
                raise ApiError("security_assurance.verification_invalid")
            passed = verification["status"] == "passed"
            outcome = "verified_passed" if passed else "verification_failed"
            result_revision = command.expected_revision + (1 if passed else 0)
            connection.execute(
                """INSERT INTO security_remediations(
                     remediation_id,pen_test_finding_id,workspace_id,tenant_id,change_sha256,
                     verification_assessment_id,outcome,requester_operator_id,
                     approver_operator_id,finding_revision,created_at,verified_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    remediation_id,
                    finding_id,
                    finding["workspace_id"],
                    finding["tenant_id"],
                    proposal["change_sha256"],
                    verification["security_assessment_id"],
                    outcome,
                    proposal["requester_operator_id"],
                    actor.operator_id,
                    result_revision,
                    stamp,
                    stamp,
                ),
            )
            if passed:
                updated = connection.execute(
                    """UPDATE pen_test_findings
                          SET status='closed',closed_at=?,last_seen_at=?,revision=revision+1
                        WHERE pen_test_finding_id=? AND status='remediation_pending'
                          AND revision=?""",
                    (stamp, stamp, finding_id, command.expected_revision),
                )
                if updated.rowcount != 1:
                    raise ApiError("security_assurance.revision_conflict")
                _event(
                    connection,
                    "security.finding_closed",
                    finding["workspace_id"],
                    finding["tenant_id"],
                    "finding",
                    finding_id,
                    result_revision,
                    {
                        "verification_assessment_id": verification["security_assessment_id"],
                        "weakness_code": finding["weakness_code"],
                    },
                    request_id,
                    trace_id,
                    now,
                )
        _record_command(
            connection,
            "platform_operator",
            actor.operator_id,
            finding["workspace_id"],
            finding["tenant_id"],
            action,
            key_sha,
            request_sha,
            remediation_id,
            result_revision,
            now,
        )
        remediation = _remediation_row(connection, remediation_id)
        response = _remediation_response(
            connection, remediation, request_id=request_id, trace_id=trace_id
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("security_assurance.unavailable") from None
    finally:
        connection.close()


def apply_risk_acceptance(
    config: AppConfig,
    finding_id: str,
    command: RiskAcceptanceCommand,
    *,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
    workspace_session_token: str | None = None,
    workspace_csrf_token: str | None = None,
    platform_session_token: str | None = None,
    platform_csrf_token: str | None = None,
) -> RiskAcceptanceSummary:
    """Internal-only Workspace dual approval and Platform review command."""

    _finding_id(finding_id)
    request_sha = _digest(
        {"finding_id": finding_id, "command": command.model_dump(mode="json")}
    )
    key_sha = hashlib.sha256(idempotency_key.encode("ascii")).hexdigest()
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        finding = _finding_row(connection, finding_id)
        now = _utcnow()
        if command.operation == "review":
            if platform_session_token is None:
                raise ApiError("security_assurance.credential_conflict")
            actor = _platform_actor(
                connection,
                config,
                platform_session_token,
                platform_csrf_token,
                "platform.assurance.review",
            )
            actor_kind = "platform_operator"
            actor_id = actor.operator_id
        else:
            if workspace_session_token is None:
                raise ApiError("security_assurance.credential_conflict")
            workspace_actor = authenticate_workspace_session_in_transaction(
                connection,
                config,
                session_token=workspace_session_token,
                csrf_token=workspace_csrf_token,
                touch=False,
            )
            if (
                workspace_actor.workspace_id != finding["workspace_id"]
                or workspace_actor.tenant_id != finding["tenant_id"]
                or "security_assurance.accept" not in workspace_actor.permission_codes
            ):
                raise ApiError("security_assurance.finding_not_found")
            actor_kind = "workspace_member"
            actor_id = workspace_actor.membership_id
        if command.operation == "request":
            action = "risk.request"
        elif command.operation == "approve":
            action = (
                "risk.owner_approve"
                if workspace_actor.membership_role == "owner"
                else "risk.security_approve"
            )
        else:
            action = "risk.platform_review"
        replay = _command_replay(
            connection, actor_kind, actor_id, action, key_sha, request_sha
        )
        if replay is not None:
            result = _risk_summary(_risk_row(connection, replay))
            connection.commit()
            return result
        if command.operation == "request":
            if (
                command.scope_sha256 is None
                or command.compensating_controls is None
                or command.expires_at is None
                or command.risk_acceptance_id is not None
                or command.expected_revision is not None
                or command.approved is not None
            ):
                raise ApiError("security_assurance.request_invalid")
            if finding["status"] not in {"open", "remediation_pending"} or connection.execute(
                """SELECT 1 FROM security_risk_acceptances
                    WHERE pen_test_finding_id=? AND status IN(
                      'pending_owner','pending_security','pending_platform','active')""",
                (finding_id,),
            ).fetchone() is not None:
                raise ApiError("security_assurance.risk_conflict")
            expires = _as_utc(command.expires_at)
            if expires <= now or expires > now + timedelta(days=30):
                raise ApiError("security_assurance.risk_time_invalid")
            controls = sorted(set(command.compensating_controls))
            if controls != command.compensating_controls:
                raise ApiError("security_assurance.request_invalid")
            risk_id = _id("rsk_")
            stamp = _db_time(now)
            connection.execute(
                """INSERT INTO security_risk_acceptances(
                     risk_acceptance_id,pen_test_finding_id,workspace_id,tenant_id,
                     scope_sha256,compensating_controls_json,status,owner_membership_id,
                     owner_account_id,security_membership_id,security_account_id,
                     platform_reviewer_operator_id,expires_at,revision,requested_at,
                     approved_at,updated_at)
                   VALUES(?,?,?,?,?,?,'pending_owner',NULL,NULL,NULL,NULL,NULL,?,1,?,NULL,?)""",
                (
                    risk_id,
                    finding_id,
                    finding["workspace_id"],
                    finding["tenant_id"],
                    command.scope_sha256,
                    _json(controls),
                    _db_time(expires),
                    stamp,
                    stamp,
                ),
            )
            result_revision = 1
        elif command.operation == "approve":
            if (
                command.risk_acceptance_id is None
                or command.expected_revision is None
                or command.scope_sha256 is not None
                or command.compensating_controls is not None
                or command.expires_at is not None
                or command.approved is not None
            ):
                raise ApiError("security_assurance.request_invalid")
            risk = _risk_row(connection, command.risk_acceptance_id)
            if (
                risk["pen_test_finding_id"] != finding_id
                or int(risk["revision"]) != command.expected_revision
                or _parse_time(risk["expires_at"]) <= now
            ):
                raise ApiError("security_assurance.risk_conflict")
            stamp = _db_time(now)
            if risk["status"] == "pending_owner" and workspace_actor.membership_role == "owner":
                updated = connection.execute(
                    """UPDATE security_risk_acceptances
                          SET status='pending_security',owner_membership_id=?,owner_account_id=?,
                              revision=revision+1,updated_at=?
                        WHERE risk_acceptance_id=? AND status='pending_owner' AND revision=?""",
                    (
                        workspace_actor.membership_id,
                        workspace_actor.account_id,
                        stamp,
                        risk["risk_acceptance_id"],
                        command.expected_revision,
                    ),
                )
            elif (
                risk["status"] == "pending_security"
                and workspace_actor.membership_role == "security_admin"
                and risk["owner_account_id"] != workspace_actor.account_id
            ):
                updated = connection.execute(
                    """UPDATE security_risk_acceptances
                          SET status='pending_platform',security_membership_id=?,
                              security_account_id=?,revision=revision+1,updated_at=?
                        WHERE risk_acceptance_id=? AND status='pending_security' AND revision=?""",
                    (
                        workspace_actor.membership_id,
                        workspace_actor.account_id,
                        stamp,
                        risk["risk_acceptance_id"],
                        command.expected_revision,
                    ),
                )
            else:
                raise ApiError("security_assurance.risk_conflict")
            if updated.rowcount != 1:
                raise ApiError("security_assurance.revision_conflict")
            risk_id = str(risk["risk_acceptance_id"])
            result_revision = command.expected_revision + 1
        else:
            if (
                command.risk_acceptance_id is None
                or command.expected_revision is None
                or command.approved is None
                or command.scope_sha256 is not None
                or command.compensating_controls is not None
                or command.expires_at is not None
            ):
                raise ApiError("security_assurance.request_invalid")
            risk = _risk_row(connection, command.risk_acceptance_id)
            if (
                risk["pen_test_finding_id"] != finding_id
                or risk["status"] != "pending_platform"
                or int(risk["revision"]) != command.expected_revision
                or _parse_time(risk["expires_at"]) <= now
                or actor.account_id
                in {str(risk["owner_account_id"]), str(risk["security_account_id"])}
            ):
                raise ApiError("security_assurance.risk_conflict")
            status = "active" if command.approved else "revoked"
            stamp = _db_time(now)
            updated = connection.execute(
                """UPDATE security_risk_acceptances
                      SET status=?,platform_reviewer_operator_id=?,approved_at=?,
                          revision=revision+1,updated_at=?
                    WHERE risk_acceptance_id=? AND status='pending_platform' AND revision=?""",
                (
                    status,
                    actor.operator_id,
                    stamp if command.approved else None,
                    stamp,
                    risk["risk_acceptance_id"],
                    command.expected_revision,
                ),
            )
            if updated.rowcount != 1:
                raise ApiError("security_assurance.revision_conflict")
            if command.approved and finding["status"] in {"open", "remediation_pending"}:
                connection.execute(
                    """UPDATE pen_test_findings
                          SET status='risk_accepted',revision=revision+1,last_seen_at=?
                        WHERE pen_test_finding_id=? AND revision=?""",
                    (stamp, finding_id, finding["revision"]),
                )
            risk_id = str(risk["risk_acceptance_id"])
            result_revision = command.expected_revision + 1
        _record_command(
            connection,
            actor_kind,
            actor_id,
            finding["workspace_id"],
            finding["tenant_id"],
            action,
            key_sha,
            request_sha,
            risk_id,
            result_revision,
            now,
        )
        result = _risk_summary(_risk_row(connection, risk_id))
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("security_assurance.unavailable") from None
    finally:
        connection.close()


def process_security_assurance_once(config: AppConfig, *, worker_id: str) -> int:
    if not 8 <= len(worker_id) <= 120:
        raise ApiError("security_assurance.worker_invalid")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _require_schema(connection)
        now = _utcnow()
        stamp = _db_time(now)
        expiring = connection.execute(
            """SELECT * FROM security_risk_acceptances
                WHERE status='active' AND expires_at<=? ORDER BY expires_at LIMIT 100""",
            (stamp,),
        ).fetchall()
        for row in expiring:
            connection.execute(
                """UPDATE security_risk_acceptances
                      SET status='expired',revision=revision+1,updated_at=?
                    WHERE risk_acceptance_id=? AND status='active'""",
                (stamp, row["risk_acceptance_id"]),
            )
            connection.execute(
                """UPDATE pen_test_findings
                      SET status='remediation_pending',revision=revision+1,last_seen_at=?
                    WHERE pen_test_finding_id=? AND status='risk_accepted'""",
                (stamp, row["pen_test_finding_id"]),
            )
        delivered = connection.execute(
            """UPDATE security_assurance_event_outbox
                  SET status='delivered',attempt_count=attempt_count+1,
                      delivered_at=?,updated_at=?
                WHERE status='pending' AND next_attempt_at<=?""",
            (stamp, stamp, stamp),
        ).rowcount
        state = connection.execute(
            "SELECT * FROM security_assurance_scheduler_state WHERE singleton_id=1"
        ).fetchone()
        token_sha = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
        connection.execute(
            """UPDATE security_assurance_scheduler_state
                  SET deadline_watermark_at=?,lease_owner_id=?,lease_token_sha256=?,
                      lease_expires_at=?,fencing_generation=fencing_generation+1,
                      revision=revision+1,updated_at=? WHERE singleton_id=1 AND revision=?""",
            (
                stamp,
                worker_id,
                token_sha,
                _db_time(now + timedelta(seconds=30)),
                stamp,
                state["revision"],
            ),
        )
        connection.commit()
        return len(expiring) + int(delivered)
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("security_assurance.unavailable") from None
    finally:
        connection.close()


def _persist_execution(
    config: AppConfig,
    request: AssessmentExecutionRequest,
    results: tuple[ScenarioExecution, ...],
    *,
    receipt: ExecutionReceipt | None,
    lease_sha: str,
    request_id: str,
    trace_id: str,
) -> None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        assessment = _assessment_row(connection, request.security_assessment_id)
        lease = connection.execute(
            "SELECT * FROM assurance_runner_leases WHERE security_assessment_id=?",
            (request.security_assessment_id,),
        ).fetchone()
        if (
            assessment["status"] != "running"
            or lease is None
            or lease["lease_token_sha256"] != lease_sha
            or _parse_time(lease["lease_expires_at"]) <= _utcnow()
        ):
            raise ApiError("security_assurance.runner_fenced")
        if not _requester_can_run(
            connection,
            assessment,
            expected_revision=int(lease["permission_revision"]),
        ):
            results = tuple(
                ScenarioExecution(
                    scenario_code=scenario.scenario_code,
                    outcome="error",
                    severity="p0",
                    duration_ms=0,
                    evidence_sha256=_digest(
                        {
                            "assessment": request.security_assessment_id,
                            "scenario": scenario.scenario_code,
                            "permission_revoked": True,
                        }
                    ),
                )
                for scenario in request.scenarios
            )
        scenario_by_code = {item.scenario_code: item for item in request.scenarios}
        now = _utcnow()
        stamp = _db_time(now)
        environment_receipt = receipt.environment_digest() if receipt else None
        receipt_runs = {run.result.scenario_code: run for run in receipt.runs} if receipt else {}
        failed = False
        summary_items: list[dict[str, object]] = []
        for result in results:
            scenario = scenario_by_code[result.scenario_code]
            failed = failed or result.outcome != "passed"
            run_id = _id("itr_")
            run_receipt = receipt_runs.get(result.scenario_code)
            source_sha = run_receipt.source_subject_sha256 if run_receipt else UNKNOWN_SHA256
            target_sha = run_receipt.target_subject_sha256 if run_receipt else UNKNOWN_SHA256
            cleanup_receipt = run_receipt.cleanup_digest() if run_receipt else UNKNOWN_SHA256
            started = run_receipt.started_at if run_receipt else now
            finished = run_receipt.finished_at if run_receipt else now
            connection.execute(
                """INSERT INTO isolation_test_runs(
                     isolation_test_run_id,security_assessment_id,workspace_id,tenant_id,
                     scenario_code,source_subject_sha256,target_subject_sha256,
                     owner_registry_version,runner_image_sha256,seed_receipt_sha256,
                     cleanup_receipt_sha256,outcome,duration_ms,evidence_sha256,
                     fencing_generation,started_at,finished_at)
                   VALUES(?,?,?,?,?,?,?,1,?,?,?,?,?,?,1,?,?)""",
                (
                    run_id,
                    request.security_assessment_id,
                    request.workspace_id,
                    request.tenant_id,
                    result.scenario_code,
                    source_sha,
                    target_sha,
                    receipt.runner_image_sha256 if receipt else UNKNOWN_SHA256,
                    run_receipt.seed_digest() if run_receipt else UNKNOWN_SHA256,
                    cleanup_receipt,
                    result.outcome,
                    result.duration_ms,
                    result.evidence_sha256,
                    _db_time(started),
                    _db_time(finished),
                ),
            )
            for fixture in run_receipt.fixtures if run_receipt else ():
                connection.execute(
                    """INSERT INTO assurance_cleanup_journal(
                         cleanup_journal_id,isolation_test_run_id,workspace_id,tenant_id,
                         fixture_code,fixture_id_sha256,environment_receipt_sha256,status,
                         cleanup_receipt_sha256,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        _id("acj_"),
                        run_id,
                        request.workspace_id,
                        request.tenant_id,
                        fixture.fixture_code,
                        fixture.fixture_id_sha256,
                        environment_receipt,
                        fixture.cleanup_status,
                        receipt_digest(fixture.model_dump(mode="json")),
                        _db_time(fixture.created_at),
                        _db_time(fixture.cleanup_attempted_at),
                    ),
                )
            if result.outcome != "passed":
                finding_id = _id("ptf_")
                severity = str(result.severity)
                deadline = now + (timedelta(hours=24) if severity == "p0" else timedelta(days=7))
                connection.execute(
                    """INSERT INTO pen_test_findings(
                         pen_test_finding_id,security_assessment_id,isolation_test_run_id,
                         workspace_id,tenant_id,owner_kind,contract_code,severity,
                         weakness_code,status,sla_deadline_at,evidence_sha256,revision,
                         first_seen_at,last_seen_at,closed_at)
                       VALUES(?,?,?,?,?,?,?,?,?,'open',?,?,1,?,?,NULL)""",
                    (
                        finding_id,
                        request.security_assessment_id,
                        run_id,
                        request.workspace_id,
                        request.tenant_id,
                        scenario.owner_kind,
                        scenario.contract_code,
                        severity,
                        scenario.weakness_code,
                        _db_time(deadline),
                        result.evidence_sha256,
                        stamp,
                        stamp,
                    ),
                )
                _event(
                    connection,
                    "security.finding_created",
                    request.workspace_id,
                    request.tenant_id,
                    "finding",
                    finding_id,
                    1,
                    {
                        "contract_code": scenario.contract_code,
                        "severity": severity,
                        "weakness_code": scenario.weakness_code,
                    },
                    request_id,
                    trace_id,
                    now,
                )
            summary_items.append(
                {
                    "scenario_code": result.scenario_code,
                    "outcome": result.outcome,
                    "evidence_sha256": result.evidence_sha256,
                }
            )
        status = "failed" if failed else "passed"
        summary_sha = _digest(summary_items)
        updated = connection.execute(
            """UPDATE security_assessments
                  SET status=?,environment_receipt_sha256=?,summary_sha256=?,
                      finished_at=?,revision=revision+1,updated_at=?
                WHERE security_assessment_id=? AND status='running' AND revision=2""",
            (
                status,
                environment_receipt,
                summary_sha,
                stamp,
                stamp,
                request.security_assessment_id,
            ),
        )
        if updated.rowcount != 1:
            raise ApiError("security_assurance.runner_fenced")
        if failed:
            _event(
                connection,
                "security.assessment_failed",
                request.workspace_id,
                request.tenant_id,
                "assessment",
                request.security_assessment_id,
                3,
                {"failed_scenarios": sum(item.outcome != "passed" for item in results)},
                request_id,
                trace_id,
                now,
            )
        connection.commit()
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("security_assurance.unavailable") from None
    finally:
        connection.close()


def _assessment_response(
    connection: sqlite3.Connection,
    assessment_id: str,
    *,
    request_id: str,
    trace_id: str,
    replay: bool = False,
) -> AssessmentResponse:
    row = _assessment_row(connection, assessment_id)
    runs = connection.execute(
        """SELECT run.*,scenario.target_kind,scenario.owner_kind,
                  scenario.contract_code,scenario.weakness_code
             FROM isolation_test_runs run
             JOIN security_assurance_scenarios scenario
               ON scenario.scenario_code=run.scenario_code
            WHERE run.security_assessment_id=? ORDER BY scenario.position""",
        (assessment_id,),
    ).fetchall()
    findings = connection.execute(
        """SELECT * FROM pen_test_findings
            WHERE security_assessment_id=? ORDER BY
              CASE severity WHEN 'p0' THEN 0 WHEN 'p1' THEN 1 WHEN 'p2' THEN 2 ELSE 3 END,
              first_seen_at""",
        (assessment_id,),
    ).fetchall()
    finding_ids = [str(value["pen_test_finding_id"]) for value in findings]
    remediations = _related_rows(connection, "security_remediations", finding_ids)
    risks = _related_rows(connection, "security_risk_acceptances", finding_ids)
    return AssessmentResponse(
        assessment=SecurityAssessmentSummary(
            security_assessment_id=str(row["security_assessment_id"]),
            workspace_id=str(row["workspace_id"]),
            profile_code=row["profile_code"],
            profile_version=int(row["profile_version"]),
            profile_sha256=str(row["profile_sha256"]),
            profile_signature_sha256=str(row["profile_signature_sha256"]),
            code_tree_sha256=str(row["code_tree_sha256"]),
            migration_set_sha256=str(row["migration_set_sha256"]),
            contract_set_sha256=str(row["contract_set_sha256"]),
            artifact_sha256=str(row["artifact_sha256"]),
            scope_sha256=str(row["scope_sha256"]),
            status=row["status"],
            generation=int(row["generation"]),
            environment_receipt_sha256=row["environment_receipt_sha256"],
            summary_sha256=row["summary_sha256"],
            revision=int(row["revision"]),
            requested_at=_parse_time(row["requested_at"]),
            started_at=None if row["started_at"] is None else _parse_time(row["started_at"]),
            finished_at=(
                None if row["finished_at"] is None else _parse_time(row["finished_at"])
            ),
            runs=tuple(_run_summary(value) for value in runs),
            findings=tuple(
                _finding_summary(
                    value,
                    remediations=remediations.get(str(value["pen_test_finding_id"]), ()),
                    risks=risks.get(str(value["pen_test_finding_id"]), ()),
                )
                for value in findings
            ),
        ),
        idempotent_replay=replay,
        request_id=request_id,
        trace_id=trace_id,
    )


def _remediation_response(
    connection: sqlite3.Connection,
    remediation,
    *,
    request_id: str,
    trace_id: str,
    replay: bool = False,
) -> RemediationResponse:
    finding = _finding_row(connection, str(remediation["pen_test_finding_id"]))
    finding_id = str(finding["pen_test_finding_id"])
    return RemediationResponse(
        remediation=_remediation_summary(remediation),
        finding=_finding_summary(
            finding,
            remediations=_related_rows(
                connection, "security_remediations", [finding_id]
            ).get(finding_id, ()),
            risks=_related_rows(
                connection, "security_risk_acceptances", [finding_id]
            ).get(finding_id, ()),
        ),
        idempotent_replay=replay,
        request_id=request_id,
        trace_id=trace_id,
    )


def _run_summary(row) -> IsolationRunSummary:
    return IsolationRunSummary(
        isolation_test_run_id=str(row["isolation_test_run_id"]),
        scenario_code=row["scenario_code"],
        target_kind=row["target_kind"],
        owner_kind=row["owner_kind"],
        contract_code=str(row["contract_code"]),
        weakness_code=str(row["weakness_code"]),
        owner_registry_version=int(row["owner_registry_version"]),
        runner_image_sha256=str(row["runner_image_sha256"]),
        outcome=row["outcome"],
        duration_ms=int(row["duration_ms"]),
        evidence_sha256=str(row["evidence_sha256"]),
        cleanup_receipt_sha256=str(row["cleanup_receipt_sha256"]),
        started_at=_parse_time(row["started_at"]),
        finished_at=_parse_time(row["finished_at"]),
    )


def _finding_summary(row, *, remediations, risks) -> PenTestFindingSummary:
    return PenTestFindingSummary(
        pen_test_finding_id=str(row["pen_test_finding_id"]),
        isolation_test_run_id=str(row["isolation_test_run_id"]),
        owner_kind=str(row["owner_kind"]),
        contract_code=str(row["contract_code"]),
        severity=row["severity"],
        weakness_code=str(row["weakness_code"]),
        status=row["status"],
        sla_deadline_at=_parse_time(row["sla_deadline_at"]),
        evidence_sha256=str(row["evidence_sha256"]),
        revision=int(row["revision"]),
        first_seen_at=_parse_time(row["first_seen_at"]),
        last_seen_at=_parse_time(row["last_seen_at"]),
        closed_at=None if row["closed_at"] is None else _parse_time(row["closed_at"]),
        remediations=tuple(_remediation_summary(value) for value in remediations),
        risk_acceptances=tuple(_risk_summary(value) for value in risks),
    )


def _remediation_summary(row) -> RemediationSummary:
    return RemediationSummary(
        remediation_id=str(row["remediation_id"]),
        change_sha256=str(row["change_sha256"]),
        verification_assessment_id=row["verification_assessment_id"],
        outcome=row["outcome"],
        finding_revision=int(row["finding_revision"]),
        created_at=_parse_time(row["created_at"]),
        verified_at=None if row["verified_at"] is None else _parse_time(row["verified_at"]),
    )


def _risk_summary(row) -> RiskAcceptanceSummary:
    return RiskAcceptanceSummary(
        risk_acceptance_id=str(row["risk_acceptance_id"]),
        scope_sha256=str(row["scope_sha256"]),
        compensating_controls=tuple(json.loads(row["compensating_controls_json"])),
        status=row["status"],
        revision=int(row["revision"]),
        expires_at=_parse_time(row["expires_at"]),
        approved_at=None if row["approved_at"] is None else _parse_time(row["approved_at"]),
        updated_at=_parse_time(row["updated_at"]),
    )


def _related_rows(connection, table: str, finding_ids: list[str]):
    if not finding_ids:
        return {}
    if table not in {"security_remediations", "security_risk_acceptances"}:
        raise ValueError("invalid related table")
    placeholders = ",".join("?" for _ in finding_ids)
    order_column = "created_at" if table == "security_remediations" else "requested_at"
    rows = connection.execute(
        f"SELECT * FROM {table} WHERE pen_test_finding_id IN({placeholders}) "
        f"ORDER BY {order_column}",
        tuple(finding_ids),
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(str(row["pen_test_finding_id"]), []).append(row)
    return grouped


def _registered_scenarios(connection) -> tuple[RegisteredScenario, ...]:
    rows = connection.execute(
        "SELECT * FROM security_assurance_scenarios ORDER BY position"
    ).fetchall()
    return tuple(
        RegisteredScenario(
            scenario_code=str(row["scenario_code"]),
            target_kind=str(row["target_kind"]),
            owner_kind=str(row["owner_kind"]),
            contract_code=str(row["contract_code"]),
            weakness_code=str(row["weakness_code"]),
            signature_sha256=str(row["signature_sha256"]),
        )
        for row in rows
    )


def _platform_actor(connection, config, token, csrf, permission) -> PlatformContext:
    actor = authenticate_platform_session_in_transaction(
        connection,
        config,
        session_token=token,
        csrf_token=csrf,
        touch=False,
    )
    if permission not in actor.permissions:
        raise ApiError("platform.permission_denied")
    return actor


def _requester_can_run(connection, assessment, *, expected_revision: int | None = None) -> bool:
    revision = (
        int(assessment["requester_revision"])
        if expected_revision is None
        else expected_revision
    )
    return connection.execute(
        """SELECT 1 FROM platform_operators operator
             JOIN platform_operator_role_bindings binding
               ON binding.operator_id=operator.operator_id AND binding.status='active'
             JOIN platform_security_assurance_role_permissions permission
               ON permission.role_code=binding.role_code
            WHERE operator.operator_id=? AND operator.status='active'
              AND operator.revision=? AND permission.permission_code='platform.assurance.run'""",
        (assessment["requester_operator_id"], revision),
    ).fetchone() is not None


def _assessment_row(connection, assessment_id):
    row = connection.execute(
        """SELECT assessment.*,operator.revision requester_revision,
                  profile.maximum_duration_seconds
             FROM security_assessments assessment
             JOIN platform_operators operator
               ON operator.operator_id=assessment.requester_operator_id
             JOIN security_assurance_profiles profile
               ON profile.profile_code=assessment.profile_code
              AND profile.profile_version=assessment.profile_version
            WHERE assessment.security_assessment_id=?""",
        (assessment_id,),
    ).fetchone()
    if row is None:
        raise ApiError("security_assurance.assessment_not_found")
    return row


def _finding_row(connection, finding_id):
    row = connection.execute(
        "SELECT * FROM pen_test_findings WHERE pen_test_finding_id=?", (finding_id,)
    ).fetchone()
    if row is None:
        raise ApiError("security_assurance.finding_not_found")
    return row


def _remediation_row(connection, remediation_id):
    row = connection.execute(
        "SELECT * FROM security_remediations WHERE remediation_id=?", (remediation_id,)
    ).fetchone()
    if row is None:
        raise ApiError("security_assurance.remediation_not_found")
    return row


def _risk_row(connection, risk_id):
    row = connection.execute(
        "SELECT * FROM security_risk_acceptances WHERE risk_acceptance_id=?", (risk_id,)
    ).fetchone()
    if row is None:
        raise ApiError("security_assurance.risk_not_found")
    return row


def _event(
    connection,
    event_type,
    workspace_id,
    tenant_id,
    object_type,
    object_id,
    revision,
    payload,
    request_id,
    trace_id,
    now,
) -> None:
    event_id = _id("ase_")
    payload_json = _json(payload)
    stamp = _db_time(now)
    connection.execute(
        """INSERT INTO security_assurance_events(
             security_assurance_event_id,event_type,workspace_id,tenant_id,object_type,
             object_id,object_revision,payload_json,payload_sha256,request_id,trace_id,occurred_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event_id,
            event_type,
            workspace_id,
            tenant_id,
            object_type,
            object_id,
            revision,
            payload_json,
            hashlib.sha256(payload_json.encode("ascii")).hexdigest(),
            request_id,
            trace_id,
            stamp,
        ),
    )
    connection.execute(
        """INSERT INTO security_assurance_event_outbox(
             security_assurance_event_outbox_id,security_assurance_event_id,
             workspace_id,tenant_id,status,attempt_count,next_attempt_at,
             delivered_at,created_at,updated_at)
           VALUES(?,?,?,?,'pending',0,?,NULL,?,?)""",
        (_id("aso_"), event_id, workspace_id, tenant_id, stamp, stamp, stamp),
    )


def _command_replay(connection, actor_kind, actor_id, action, key_sha, request_sha):
    row = connection.execute(
        """SELECT request_sha256,result_object_id
             FROM security_assurance_action_commands
            WHERE actor_kind=? AND actor_id=? AND action_code=?
              AND idempotency_key_sha256=?""",
        (actor_kind, actor_id, action, key_sha),
    ).fetchone()
    if row is None:
        return None
    if row["request_sha256"] != request_sha:
        raise ApiError("security_assurance.idempotency_conflict")
    return str(row["result_object_id"])


def _record_command(
    connection,
    actor_kind,
    actor_id,
    workspace_id,
    tenant_id,
    action,
    key_sha,
    request_sha,
    result_id,
    result_revision,
    now,
) -> None:
    connection.execute(
        """INSERT INTO security_assurance_action_commands(
             security_assurance_action_command_id,actor_kind,actor_id,workspace_id,tenant_id,
             action_code,idempotency_key_sha256,request_sha256,result_object_id,
             result_revision,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            _id("asc_"),
            actor_kind,
            actor_id,
            workspace_id,
            tenant_id,
            action,
            key_sha,
            request_sha,
            result_id,
            result_revision,
            _db_time(now),
        ),
    )


def _require_schema(connection) -> None:
    if not _table_exists(connection, "security_assessments"):
        raise ApiError("security_assurance.unavailable")


def _table_exists(connection, name):
    return table_exists(connection, name)


def _assessment_id(value: str) -> None:
    if _ASSESSMENT_ID.fullmatch(value) is None:
        raise ApiError("security_assurance.assessment_not_found")


def _finding_id(value: str) -> None:
    if _FINDING_ID.fullmatch(value) is None:
        raise ApiError("security_assurance.finding_not_found")


def _connect(config):
    return connect_database(config.database_url)


def _rollback(connection) -> None:
    if connection.in_transaction:
        connection.rollback()


def _id(prefix):
    return prefix + uuid4().hex


def _utcnow():
    return datetime.now(UTC)


def _db_time(value):
    return _as_utc(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_time(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _as_utc(value):
    if value.tzinfo is None:
        raise ValueError("timezone required")
    return value.astimezone(UTC)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value):
    return hashlib.sha256(_json(value).encode("ascii")).hexdigest()


__all__ = [
    "AssessmentExecutionRequest",
    "AssuranceRunner",
    "RegisteredScenario",
    "apply_remediation",
    "apply_risk_acceptance",
    "create_security_assessment",
    "execute_security_assessment",
    "get_security_assessment",
    "process_security_assurance_once",
]
