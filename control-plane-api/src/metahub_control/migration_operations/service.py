"""Owner-qualified migration assessment, cutover, and compensating rollback."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from metahub_shared.db import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.identity.security_service import (
    consume_step_up_grant_in_transaction,
)
from metahub_control.platform_access.service import _authenticate_support
from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    authenticate_workspace_session_in_transaction,
)

from .models import (
    CutoverCheckpointSummary,
    MigrationAssessmentCreateRequest,
    MigrationAssessmentResponse,
    MigrationAssessmentSummary,
    MigrationDifferenceListResponse,
    MigrationDifferenceSummary,
    MigrationDomainSnapshot,
    MigrationPlanSummary,
    MigrationRunCreateRequest,
    MigrationRunResponse,
    MigrationRunSummary,
    MigrationTransitionRequest,
    RollbackPlanSummary,
)

_DOMAINS = (
    "audit",
    "edge",
    "identity",
    "ledger",
    "policy",
    "provider_reference",
    "workspace",
)
_PHASES = ("assessment", "backfill", "dual_read", "freeze", "cutover", "observe")
_COMPENSATION_ORDER = (
    "freeze_writes",
    "restore_head",
    "replay_receipts",
    "verify_owner",
)
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_LEGACY_ROLE_BY_WORKSPACE_ROLE = {
    "owner": "admin",
    "admin": "admin",
    "developer": "developer",
    "security_admin": "security_admin",
    "finance_admin": "finance_admin",
    "legal_admin": "viewer",
    "auditor": "viewer",
    "viewer": "viewer",
}
_GENERIC_ADAPTERS = {
    "provider_reference": (
        "providers",
        "tenant_id",
        ("provider_id", "provider_type", "status", "revision"),
    ),
    "policy": (
        "policies",
        "tenant_id",
        ("policy_id", "status", "priority", "version"),
    ),
    "ledger": (
        "finops_ledger_entries",
        "tenant_id",
        ("ledger_entry_id", "entry_type", "status", "sequence_number", "revision"),
    ),
    "audit": (
        "audit_logs",
        "tenant_id",
        ("audit_log_id", "action", "object_type", "object_id", "sequence_number"),
    ),
    "edge": (
        "edge_deployments",
        "workspace_id",
        ("edge_deployment_id", "mode", "status", "revision", "revocation_generation"),
    ),
}


@dataclass(frozen=True, slots=True)
class _Snapshot:
    domain_code: str
    source_count: int
    target_count: int
    source_sha256: str
    target_sha256: str
    source_sequence: int
    target_sequence: int
    source_watermark: int
    target_watermark: int

    @property
    def matches(self) -> bool:
        return (
            self.source_count == self.target_count
            and secrets.compare_digest(self.source_sha256, self.target_sha256)
            and self.source_sequence == self.target_sequence
            and self.source_watermark == self.target_watermark
        )


def create_migration_assessment(
    config: AppConfig,
    payload: MigrationAssessmentCreateRequest,
    *,
    session_token: str,
    csrf_token: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> MigrationAssessmentResponse:
    request_sha256 = _digest(payload.model_dump(mode="json"))
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = _workspace_context(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            permission="migration.manage",
        )
        replay = _command_replay(
            connection,
            context.account_id,
            "migration.assessment.create",
            _idempotency_sha256(context.account_id, idempotency_key),
        )
        if replay is not None:
            _verify_replay(replay, request_sha256)
            response = _assessment_response(
                connection,
                str(replay["result_object_id"]),
                request_id=request_id,
                trace_id=trace_id,
                replay=True,
            )
            connection.commit()
            return response
        _verify_adapter_catalog(connection, payload.domains)
        write_watermark_before = connection.total_changes
        snapshots = _capture_snapshots(connection, context, payload.domains)
        write_watermark_after = connection.total_changes
        if write_watermark_before != write_watermark_after:
            raise ApiError("migration.assessment_write_detected")
        permission_expansions = _permission_expansion_count(connection, context)
        differences = _differences_for_snapshots(snapshots)
        if permission_expansions:
            differences.append(
                (
                    "identity",
                    "permission_expansion",
                    permission_expansions,
                    0,
                    _EMPTY_SHA256,
                    _EMPTY_SHA256,
                    0,
                    0,
                    "review_role_mapping",
                )
            )
        assessment_id = _id("mas_")
        plan_id = _id("mpl_")
        now = _utcnow()
        now_value = _db_time(now)
        status = "validation_failed" if differences else "ready"
        domain_set_sha256 = _digest(list(payload.domains))
        object_count = sum(snapshot.source_count for snapshot in snapshots)
        credential_count = _credential_reentry_count(connection, context.tenant_id)
        connection.execute(
            """
            INSERT INTO migration_assessments(
              migration_assessment_id,workspace_id,tenant_id,source_kind,manifest_version,
              source_manifest_sha256,domain_set_sha256,status,object_count,
              blocking_difference_count,write_watermark_before,write_watermark_after,
              credential_reentry_required_count,permission_expansion_count,
              requested_by_account_id,reason_code,revision,created_at,completed_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)
            """,
            (
                assessment_id,
                context.workspace_id,
                context.tenant_id,
                payload.source_kind,
                payload.manifest_version,
                payload.source_manifest_sha256,
                domain_set_sha256,
                status,
                object_count,
                len(differences),
                write_watermark_before,
                write_watermark_after,
                credential_count,
                permission_expansions,
                context.account_id,
                payload.reason_code,
                now_value,
                now_value,
            ),
        )
        plan_payload = {
            "assessment_id": assessment_id,
            "domains": list(payload.domains),
            "manifest_sha256": payload.source_manifest_sha256,
            "phases": list(_PHASES),
        }
        plan_sha256 = _digest(plan_payload)
        connection.execute(
            """
            INSERT INTO migration_plans(
              migration_plan_id,workspace_id,tenant_id,migration_assessment_id,generation,
              status,phase_set_json,plan_sha256,planned_downtime_seconds,
              rollback_deadline_seconds,revision,created_at)
            VALUES(?,?,?,?,1,?,?,?,?,7200,1,?)
            """,
            (
                plan_id,
                context.workspace_id,
                context.tenant_id,
                assessment_id,
                "blocked" if differences else "ready",
                _canonical_json(list(_PHASES)),
                plan_sha256,
                min(7200, max(60, object_count // 100)),
                now_value,
            ),
        )
        for snapshot in snapshots:
            _insert_snapshot(connection, context, assessment_id, snapshot, now_value)
        _insert_differences(
            connection,
            context=context,
            assessment_id=assessment_id,
            run_id=None,
            differences=differences,
            now=now_value,
        )
        if differences:
            _insert_event(
                connection,
                context=context,
                assessment_id=assessment_id,
                run_id=None,
                event_type="migration.validation_failed",
                reason_code=payload.reason_code,
                payload={
                    "difference_count": len(differences),
                    "domain_set_sha256": domain_set_sha256,
                },
                request_id=request_id,
                trace_id=trace_id,
                now=now_value,
            )
        _insert_command(
            connection,
            account_id=context.account_id,
            action_code="migration.assessment.create",
            idempotency_sha256=_idempotency_sha256(context.account_id, idempotency_key),
            request_sha256=request_sha256,
            result_object_id=assessment_id,
            now=now_value,
        )
        response = _assessment_response(
            connection,
            assessment_id,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("migration.state_conflict") from None
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("migration.unavailable") from None
    finally:
        connection.close()


def create_migration_run(
    config: AppConfig,
    payload: MigrationRunCreateRequest,
    *,
    session_token: str,
    csrf_token: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> MigrationRunResponse:
    request_sha256 = _digest(payload.model_dump(mode="json"))
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = _workspace_context(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            permission="migration.manage",
        )
        replay = _command_replay(
            connection,
            context.account_id,
            "migration.run.create",
            _idempotency_sha256(context.account_id, idempotency_key),
        )
        if replay is not None:
            _verify_replay(replay, request_sha256)
            response = _run_response(
                connection,
                str(replay["result_object_id"]),
                request_id=request_id,
                trace_id=trace_id,
                replay=True,
            )
            connection.commit()
            return response
        assessment = _owned_assessment(connection, context, payload.assessment_id)
        if (
            int(assessment["revision"]) != payload.expected_assessment_revision
            or str(assessment["status"]) != "ready"
            or int(assessment["permission_expansion_count"]) != 0
        ):
            raise ApiError("migration.assessment_conflict")
        plan = connection.execute(
            "SELECT * FROM migration_plans WHERE migration_assessment_id=?",
            (payload.assessment_id,),
        ).fetchone()
        if plan is None or str(plan["status"]) != "ready":
            raise ApiError("migration.assessment_conflict")
        domains = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT domain_code FROM migration_assessment_snapshots "
                "WHERE migration_assessment_id=? ORDER BY domain_code",
                (payload.assessment_id,),
            ).fetchall()
        )
        current = _capture_snapshots(connection, context, domains)
        stored = _stored_snapshots(connection, payload.assessment_id)
        drift = _snapshot_drift(stored, current)
        run_id = _id("mrn_")
        generation = int(
            connection.execute(
                "SELECT coalesce(max(generation),0)+1 FROM migration_runs WHERE workspace_id=?",
                (context.workspace_id,),
            ).fetchone()[0]
        )
        head = connection.execute(
            "SELECT * FROM migration_cutover_heads WHERE workspace_id=? AND tenant_id=?",
            (context.workspace_id, context.tenant_id),
        ).fetchone()
        freeze_generation = (int(head["generation"]) + 1) if head is not None else 1
        now = _utcnow()
        now_value = _db_time(now)
        connection.execute(
            """
            INSERT INTO migration_runs(
              migration_run_id,workspace_id,tenant_id,migration_assessment_id,migration_plan_id,
              status,generation,freeze_generation,source_manifest_sha256,
              authorization_fingerprint_sha256,requested_by_account_id,
              blocking_difference_count,revision,started_at,updated_at,completed_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,NULL)
            """,
            (
                run_id,
                context.workspace_id,
                context.tenant_id,
                payload.assessment_id,
                plan["migration_plan_id"],
                "failed" if drift else "observing",
                generation,
                freeze_generation,
                assessment["source_manifest_sha256"],
                _digest(context.authorization_fingerprint),
                context.account_id,
                len(drift),
                now_value,
                now_value,
            ),
        )
        if drift:
            _insert_differences(
                connection,
                context=context,
                assessment_id=payload.assessment_id,
                run_id=run_id,
                differences=drift,
                now=now_value,
            )
            _insert_event(
                connection,
                context=context,
                assessment_id=payload.assessment_id,
                run_id=run_id,
                event_type="migration.validation_failed",
                reason_code=payload.reason_code,
                payload={"difference_count": len(drift), "generation": generation},
                request_id=request_id,
                trace_id=trace_id,
                now=now_value,
            )
        else:
            output_sha256 = _digest(
                [
                    {
                        "domain": item.domain_code,
                        "source": item.source_sha256,
                        "target": item.target_sha256,
                    }
                    for item in current
                ]
            )
            checkpoint_ids: list[str] = []
            for sequence, phase in enumerate(_PHASES[:4], 1):
                checkpoint_ids.append(
                    _insert_checkpoint(
                        connection,
                        context=context,
                        run_id=run_id,
                        phase=phase,
                        sequence=sequence,
                        input_sha256=str(assessment["source_manifest_sha256"]),
                        output_sha256=output_sha256,
                        watermark=sum(item.target_watermark for item in current),
                        freeze_generation=freeze_generation,
                        now=now_value,
                    )
                )
            rollback_plan_id = _id("mrp_")
            rollback_payload = {
                "run_id": run_id,
                "previous_run_id": head["active_migration_run_id"] if head else None,
                "last_safe_checkpoint_id": checkpoint_ids[-1],
                "compensation_order": list(_COMPENSATION_ORDER),
            }
            connection.execute(
                """
                INSERT INTO migration_rollback_plans(
                  migration_rollback_plan_id,workspace_id,tenant_id,migration_run_id,
                  previous_migration_run_id,last_safe_checkpoint_id,compensation_order_json,
                  rollback_plan_sha256,status,revision,created_at,updated_at,completed_at)
                VALUES(?,?,?,?,?,?,?,?,'ready',1,?,?,NULL)
                """,
                (
                    rollback_plan_id,
                    context.workspace_id,
                    context.tenant_id,
                    run_id,
                    head["active_migration_run_id"] if head else None,
                    checkpoint_ids[-1],
                    _canonical_json(list(_COMPENSATION_ORDER)),
                    _digest(rollback_payload),
                    now_value,
                    now_value,
                ),
            )
            _insert_event(
                connection,
                context=context,
                assessment_id=payload.assessment_id,
                run_id=run_id,
                event_type="migration.started",
                reason_code=payload.reason_code,
                payload={"generation": generation, "freeze_generation": freeze_generation},
                request_id=request_id,
                trace_id=trace_id,
                now=now_value,
            )
        _insert_command(
            connection,
            account_id=context.account_id,
            action_code="migration.run.create",
            idempotency_sha256=_idempotency_sha256(context.account_id, idempotency_key),
            request_sha256=request_sha256,
            result_object_id=run_id,
            now=now_value,
        )
        response = _run_response(connection, run_id, request_id=request_id, trace_id=trace_id)
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("migration.state_conflict") from None
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("migration.unavailable") from None
    finally:
        connection.close()


def execute_migration_cutover(
    config: AppConfig,
    run_id: str,
    payload: MigrationTransitionRequest,
    *,
    session_token: str,
    csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> MigrationRunResponse:
    return _transition_run(
        config,
        run_id,
        payload,
        operation="cutover",
        session_token=session_token,
        csrf_token=csrf_token,
        step_up_grant=step_up_grant,
        idempotency_key=idempotency_key,
        request_id=request_id,
        trace_id=trace_id,
    )


def execute_migration_rollback(
    config: AppConfig,
    run_id: str,
    payload: MigrationTransitionRequest,
    *,
    session_token: str,
    csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> MigrationRunResponse:
    return _transition_run(
        config,
        run_id,
        payload,
        operation="rollback",
        session_token=session_token,
        csrf_token=csrf_token,
        step_up_grant=step_up_grant,
        idempotency_key=idempotency_key,
        request_id=request_id,
        trace_id=trace_id,
    )


def _transition_run(
    config: AppConfig,
    run_id: str,
    payload: MigrationTransitionRequest,
    *,
    operation: str,
    session_token: str,
    csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> MigrationRunResponse:
    action_code = f"migration.{operation}.execute"
    request_sha256 = _digest(payload.model_dump(mode="json"))
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = _workspace_context(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            permission="migration.manage",
        )
        replay = _command_replay(
            connection,
            context.account_id,
            action_code,
            _idempotency_sha256(context.account_id, idempotency_key),
        )
        if replay is not None:
            _verify_replay(replay, request_sha256)
            response = _run_response(
                connection,
                str(replay["result_object_id"]),
                request_id=request_id,
                trace_id=trace_id,
                replay=True,
            )
            connection.commit()
            return response
        run = _owned_run(connection, context, run_id)
        if int(run["revision"]) != payload.expected_revision:
            raise ApiError("migration.revision_conflict")
        rollback_plan = connection.execute(
            "SELECT * FROM migration_rollback_plans WHERE migration_run_id=?", (run_id,)
        ).fetchone()
        if rollback_plan is None or str(rollback_plan["status"]) != "ready":
            raise ApiError("migration.state_conflict")
        now = _utcnow()
        consume_step_up_grant_in_transaction(
            connection,
            config,
            context=context,
            grant=step_up_grant,
            action_code=action_code,
            resource_type="migration_run",
            resource_id=run_id,
            now=now,
        )
        if operation == "cutover":
            _execute_cutover(
                connection,
                context=context,
                run=run,
                reason_code=payload.reason_code,
                request_id=request_id,
                trace_id=trace_id,
                now=now,
            )
        else:
            _execute_rollback(
                connection,
                context=context,
                run=run,
                rollback_plan=rollback_plan,
                reason_code=payload.reason_code,
                request_id=request_id,
                trace_id=trace_id,
                now=now,
            )
        _insert_command(
            connection,
            account_id=context.account_id,
            action_code=action_code,
            idempotency_sha256=_idempotency_sha256(context.account_id, idempotency_key),
            request_sha256=request_sha256,
            result_object_id=run_id,
            now=_db_time(now),
        )
        response = _run_response(connection, run_id, request_id=request_id, trace_id=trace_id)
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("migration.state_conflict") from None
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("migration.unavailable") from None
    finally:
        connection.close()


def list_migration_differences(
    config: AppConfig,
    migration_id: str,
    *,
    workspace_session_token: str | None,
    support_session_token: str | None,
    request_id: str,
    trace_id: str,
) -> MigrationDifferenceListResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        support = None
        workspace_context = None
        if workspace_session_token is not None:
            workspace_context = authenticate_workspace_session_in_transaction(
                connection,
                config,
                session_token=workspace_session_token,
                touch=True,
            )
            _require_permission(workspace_context, "migration.read")
        elif support_session_token is not None:
            support = _authenticate_support(connection, config, support_session_token)
            if str(support["source_kind"]) != "grant":
                raise ApiError("support.session_invalid")
        else:
            raise ApiError("workspace_session.required")
        assessment, run_id = _migration_owner_row(connection, migration_id)
        if workspace_context is not None:
            if (
                str(assessment["workspace_id"]) != workspace_context.workspace_id
                or str(assessment["tenant_id"]) != workspace_context.tenant_id
            ):
                raise ApiError("migration.not_found")
        else:
            assert support is not None
            if str(assessment["workspace_id"]) != str(support["workspace_id"]) or str(
                assessment["tenant_id"]
            ) != str(support["tenant_id"]):
                raise ApiError("migration.not_found")
            connection.execute(
                """
                INSERT INTO migration_support_access_audits(
                  migration_support_access_audit_id,workspace_id,tenant_id,
                  migration_assessment_id,migration_run_id,support_session_id,
                  support_grant_id,operator_id,operator_revision,field_scope,
                  request_id,trace_id,accessed_at)
                VALUES(?,?,?,?,?,?,?,?,?,'difference_summary',?,?,?)
                """,
                (
                    _id("msa_"),
                    assessment["workspace_id"],
                    assessment["tenant_id"],
                    assessment["migration_assessment_id"],
                    run_id,
                    support["support_session_id"],
                    support["source_id"],
                    support["operator_id"],
                    support["operator_revision"],
                    request_id,
                    trace_id,
                    _db_time(_utcnow()),
                ),
            )
        if run_id is None:
            rows = connection.execute(
                "SELECT * FROM migration_differences WHERE migration_assessment_id=? "
                "AND migration_run_id IS NULL ORDER BY domain_code,difference_code",
                (assessment["migration_assessment_id"],),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM migration_differences WHERE migration_assessment_id=? "
                "AND (migration_run_id IS NULL OR migration_run_id=?) "
                "ORDER BY migration_run_id,domain_code,difference_code",
                (assessment["migration_assessment_id"], run_id),
            ).fetchall()
        response = MigrationDifferenceListResponse(
            assessment_id=assessment["migration_assessment_id"],
            run_id=run_id,
            items=tuple(_difference_summary(row) for row in rows),
            support_scoped=support is not None,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("migration.unavailable") from None
    finally:
        connection.close()


def _execute_cutover(
    connection: sqlite3.Connection,
    *,
    context: WorkspaceSessionContext,
    run: sqlite3.Row,
    reason_code: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    if str(run["status"]) != "observing" or int(run["blocking_difference_count"]) != 0:
        raise ApiError("migration.state_conflict")
    assessment_id = str(run["migration_assessment_id"])
    domains = tuple(item.domain_code for item in _stored_snapshots(connection, assessment_id))
    current = _capture_snapshots(connection, context, domains)
    if _snapshot_drift(_stored_snapshots(connection, assessment_id), current):
        raise ApiError("migration.validation_changed")
    current_head = connection.execute(
        "SELECT * FROM migration_cutover_heads WHERE workspace_id=? AND tenant_id=?",
        (context.workspace_id, context.tenant_id),
    ).fetchone()
    if current_head is None:
        connection.execute(
            """
            INSERT INTO migration_cutover_heads(
              workspace_id,tenant_id,mode,active_migration_run_id,generation,revision,updated_at)
            VALUES(?,?,'workspace',?,1,1,?)
            """,
            (context.workspace_id, context.tenant_id, run["migration_run_id"], _db_time(now)),
        )
    else:
        update_time = _after(str(current_head["updated_at"]), now)
        updated = connection.execute(
            """
            UPDATE migration_cutover_heads SET mode='workspace',active_migration_run_id=?,
              generation=generation+1,revision=revision+1,updated_at=?
            WHERE workspace_id=? AND tenant_id=? AND revision=?
            """,
            (
                run["migration_run_id"],
                update_time,
                context.workspace_id,
                context.tenant_id,
                current_head["revision"],
            ),
        )
        if updated.rowcount != 1:
            raise ApiError("migration.revision_conflict")
    watermark = sum(item.target_watermark for item in current)
    _insert_checkpoint(
        connection,
        context=context,
        run_id=str(run["migration_run_id"]),
        phase="cutover",
        sequence=5,
        input_sha256=str(run["source_manifest_sha256"]),
        output_sha256=_digest([item.target_sha256 for item in current]),
        watermark=watermark,
        freeze_generation=int(run["freeze_generation"]),
        now=_db_time(now),
    )
    update_time = _after(str(run["updated_at"]), now)
    updated = connection.execute(
        """
        UPDATE migration_runs SET status='completed',revision=revision+1,
          updated_at=?,completed_at=?
        WHERE migration_run_id=? AND status='observing' AND revision=?
        """,
        (update_time, update_time, run["migration_run_id"], run["revision"]),
    )
    if updated.rowcount != 1:
        raise ApiError("migration.revision_conflict")
    _insert_event(
        connection,
        context=context,
        assessment_id=assessment_id,
        run_id=str(run["migration_run_id"]),
        event_type="migration.cutover_completed",
        reason_code=reason_code,
        payload={"freeze_generation": int(run["freeze_generation"]), "watermark": watermark},
        request_id=request_id,
        trace_id=trace_id,
        now=update_time,
    )


def _execute_rollback(
    connection: sqlite3.Connection,
    *,
    context: WorkspaceSessionContext,
    run: sqlite3.Row,
    rollback_plan: sqlite3.Row,
    reason_code: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    if str(run["status"]) not in {"observing", "completed", "failed"}:
        raise ApiError("migration.state_conflict")
    current_head = connection.execute(
        "SELECT * FROM migration_cutover_heads WHERE workspace_id=? AND tenant_id=?",
        (context.workspace_id, context.tenant_id),
    ).fetchone()
    if str(run["status"]) == "completed" and (
        current_head is None
        or str(current_head["active_migration_run_id"]) != str(run["migration_run_id"])
    ):
        raise ApiError("migration.state_conflict")
    first_time = _after(str(run["updated_at"]), now)
    first = connection.execute(
        """
        UPDATE migration_runs SET status='rolling_back',revision=revision+1,updated_at=?,
          completed_at=NULL
        WHERE migration_run_id=? AND revision=? AND status IN('observing','completed','failed')
        """,
        (first_time, run["migration_run_id"], run["revision"]),
    )
    if first.rowcount != 1:
        raise ApiError("migration.revision_conflict")
    previous_run_id = rollback_plan["previous_migration_run_id"]
    head_time = (
        _after(str(current_head["updated_at"]), _parse_time(first_time))
        if current_head
        else first_time
    )
    if current_head is None:
        connection.execute(
            """
            INSERT INTO migration_cutover_heads(
              workspace_id,tenant_id,mode,active_migration_run_id,generation,revision,updated_at)
            VALUES(?,?,'legacy',NULL,1,1,?)
            """,
            (context.workspace_id, context.tenant_id, head_time),
        )
    else:
        mode = "workspace" if previous_run_id is not None else "legacy"
        updated = connection.execute(
            """
            UPDATE migration_cutover_heads SET mode=?,active_migration_run_id=?,
              generation=generation+1,revision=revision+1,updated_at=?
            WHERE workspace_id=? AND tenant_id=? AND revision=?
            """,
            (
                mode,
                previous_run_id,
                head_time,
                context.workspace_id,
                context.tenant_id,
                current_head["revision"],
            ),
        )
        if updated.rowcount != 1:
            raise ApiError("migration.revision_conflict")
    finish_time = _after(first_time, _parse_time(head_time))
    _insert_checkpoint(
        connection,
        context=context,
        run_id=str(run["migration_run_id"]),
        phase="rollback",
        sequence=6,
        input_sha256=str(rollback_plan["rollback_plan_sha256"]),
        output_sha256=_digest(
            {"mode": "workspace" if previous_run_id else "legacy", "previous": previous_run_id}
        ),
        watermark=0,
        freeze_generation=int(run["freeze_generation"]),
        now=finish_time,
    )
    connection.execute(
        """
        UPDATE migration_rollback_plans SET status='completed',revision=revision+1,
          updated_at=?,completed_at=?
        WHERE migration_rollback_plan_id=? AND status='ready' AND revision=?
        """,
        (
            finish_time,
            finish_time,
            rollback_plan["migration_rollback_plan_id"],
            rollback_plan["revision"],
        ),
    )
    second_time = _after(finish_time, _parse_time(finish_time))
    second = connection.execute(
        """
        UPDATE migration_runs SET status='rolled_back',revision=revision+1,
          updated_at=?,completed_at=?
        WHERE migration_run_id=? AND status='rolling_back' AND revision=?
        """,
        (second_time, second_time, run["migration_run_id"], int(run["revision"]) + 1),
    )
    if second.rowcount != 1:
        raise ApiError("migration.revision_conflict")
    _insert_event(
        connection,
        context=context,
        assessment_id=str(run["migration_assessment_id"]),
        run_id=str(run["migration_run_id"]),
        event_type="migration.rolled_back",
        reason_code=reason_code,
        payload={"previous_run_configured": previous_run_id is not None},
        request_id=request_id,
        trace_id=trace_id,
        now=second_time,
    )


def _workspace_context(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    permission: str,
) -> WorkspaceSessionContext:
    context = authenticate_workspace_session_in_transaction(
        connection,
        config,
        session_token=session_token,
        csrf_token=csrf_token,
        touch=True,
    )
    _require_permission(context, permission)
    return context


def _require_permission(context: WorkspaceSessionContext, permission: str) -> None:
    if permission not in context.permission_codes:
        raise ApiError("migration.permission_denied")


def _capture_snapshots(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    domains: Iterable[str],
) -> tuple[_Snapshot, ...]:
    return tuple(_capture_domain(connection, context, domain) for domain in domains)


def _capture_domain(
    connection: sqlite3.Connection, context: WorkspaceSessionContext, domain: str
) -> _Snapshot:
    if domain == "identity":
        source = _projection(
            connection,
            """
            SELECT user.user_id,
              CASE user.role WHEN 'ops_admin' THEN 'admin' ELSE user.role END role_code,
              user.status
            FROM users user WHERE user.tenant_id=? ORDER BY user.user_id
            """,
            (context.tenant_id,),
        )
        target = _projection(
            connection,
            """
            SELECT member.legacy_user_id,
              CASE user.role WHEN 'ops_admin' THEN 'admin' ELSE user.role END role_code,
              member.status
            FROM workspace_memberships member
            JOIN users user ON user.tenant_id=member.tenant_id
              AND user.user_id=member.legacy_user_id
            WHERE member.workspace_id=? AND member.tenant_id=?
              AND member.legacy_user_id IS NOT NULL
            ORDER BY member.legacy_user_id
            """,
            (context.workspace_id, context.tenant_id),
        )
    elif domain == "workspace":
        source = _projection(
            connection,
            "SELECT tenant_id,status FROM tenants WHERE tenant_id=? ORDER BY tenant_id",
            (context.tenant_id,),
        )
        target = _projection(
            connection,
            """
            SELECT binding.tenant_id,
              CASE WHEN workspace.status='active' AND binding.status='active'
                THEN 'active' ELSE 'inactive' END status
            FROM workspace_tenant_bindings binding
            JOIN workspaces workspace ON workspace.workspace_id=binding.workspace_id
            WHERE binding.workspace_id=? AND binding.tenant_id=?
            ORDER BY binding.tenant_id
            """,
            (context.workspace_id, context.tenant_id),
        )
    else:
        try:
            table, owner_column, candidates = _GENERIC_ADAPTERS[domain]
        except KeyError:
            raise ApiError("migration.adapter_invalid") from None
        columns = _table_columns(connection, table)
        if owner_column not in columns or not candidates or candidates[0] not in columns:
            raise ApiError("migration.adapter_unavailable")
        fields = tuple(field for field in candidates if field in columns)
        owner_value = context.workspace_id if owner_column == "workspace_id" else context.tenant_id
        sql = f"SELECT {','.join(fields)} FROM {table} WHERE {owner_column}=? ORDER BY {fields[0]}"
        source = _projection(connection, sql, (owner_value,))
        target = source
    return _Snapshot(
        domain_code=domain,
        source_count=source[0],
        target_count=target[0],
        source_sha256=source[1],
        target_sha256=target[1],
        source_sequence=source[2],
        target_sequence=target[2],
        source_watermark=source[3],
        target_watermark=target[3],
    )


def _projection(
    connection: sqlite3.Connection, sql: str, parameters: tuple[object, ...]
) -> tuple[int, str, int, int]:
    digest = hashlib.sha256()
    count = 0
    sequence = 0
    for row in connection.execute(sql, parameters):
        values = [row[index] for index in range(len(row))]
        digest.update(_canonical_json(values).encode("utf-8"))
        digest.update(b"\n")
        count += 1
        for value in values:
            if type(value) is int:
                sequence = max(sequence, value)
    return count, digest.hexdigest(), max(sequence, count), count


def _table_columns(connection: sqlite3.Connection, table: str) -> frozenset[str]:
    if table not in {spec[0] for spec in _GENERIC_ADAPTERS.values()}:
        raise ApiError("migration.adapter_invalid")
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    if not rows:
        raise ApiError("migration.adapter_unavailable")
    return frozenset(str(row[1]) for row in rows)


def _verify_adapter_catalog(connection: sqlite3.Connection, domains: Iterable[str]) -> None:
    rows = connection.execute(
        "SELECT domain_code,adapter_version,owner_kind,source_prd "
        "FROM migration_source_adapters ORDER BY domain_code"
    ).fetchall()
    if tuple(str(row["domain_code"]) for row in rows) != _DOMAINS or any(
        str(row["adapter_version"]) != "migration-adapter-v1"
        or str(row["owner_kind"]) != "workspace_tenant"
        or str(row["source_prd"]) != "PRD-131"
        for row in rows
    ):
        raise ApiError("migration.adapter_unavailable")
    if not set(domains) <= set(_DOMAINS):
        raise ApiError("migration.adapter_invalid")


def _permission_expansion_count(
    connection: sqlite3.Connection, context: WorkspaceSessionContext
) -> int:
    rows = connection.execute(
        """
        SELECT member.role_code,user.role legacy_role
        FROM workspace_memberships member
        JOIN users user ON user.tenant_id=member.tenant_id
          AND user.user_id=member.legacy_user_id
        WHERE member.workspace_id=? AND member.tenant_id=? AND member.status='active'
          AND member.legacy_user_id IS NOT NULL
        """,
        (context.workspace_id, context.tenant_id),
    ).fetchall()
    return sum(
        1
        for row in rows
        if _LEGACY_ROLE_BY_WORKSPACE_ROLE.get(str(row["role_code"]))
        != ("admin" if str(row["legacy_role"]) == "ops_admin" else str(row["legacy_role"]))
    )


def _credential_reentry_count(connection: sqlite3.Connection, tenant_id: str) -> int:
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(provider_credentials)")}
    if "tenant_id" not in columns:
        return 0
    return int(
        connection.execute(
            "SELECT COUNT(*) FROM provider_credentials WHERE tenant_id=?", (tenant_id,)
        ).fetchone()[0]
    )


def _differences_for_snapshots(
    snapshots: Iterable[_Snapshot],
) -> list[tuple[str, str, int, int, str, str, int, int, str]]:
    differences: list[tuple[str, str, int, int, str, str, int, int, str]] = []
    for item in snapshots:
        common = (
            item.domain_code,
            item.source_count,
            item.target_count,
            item.source_sha256,
            item.target_sha256,
            item.source_sequence,
            item.target_sequence,
        )
        if item.source_count != item.target_count:
            differences.append(
                (common[0], "count_mismatch", *common[1:], "rebuild_target_projection")
            )
        if not secrets.compare_digest(item.source_sha256, item.target_sha256):
            differences.append((common[0], "hash_mismatch", *common[1:], "reassess_source"))
        if item.source_sequence != item.target_sequence:
            differences.append(
                (common[0], "sequence_mismatch", *common[1:], "replay_missing_sequence")
            )
        if item.source_watermark != item.target_watermark:
            differences.append((common[0], "watermark_mismatch", *common[1:], "wait_for_watermark"))
    return differences


def _snapshot_drift(
    expected: Iterable[_Snapshot], current: Iterable[_Snapshot]
) -> list[tuple[str, str, int, int, str, str, int, int, str]]:
    expected_by_domain = {item.domain_code: item for item in expected}
    differences = []
    for item in current:
        baseline = expected_by_domain.get(item.domain_code)
        if baseline is None:
            raise ApiError("migration.adapter_unavailable")
        drift = _Snapshot(
            domain_code=item.domain_code,
            source_count=baseline.source_count,
            target_count=item.source_count,
            source_sha256=baseline.source_sha256,
            target_sha256=item.source_sha256,
            source_sequence=baseline.source_sequence,
            target_sequence=item.source_sequence,
            source_watermark=baseline.source_watermark,
            target_watermark=item.source_watermark,
        )
        differences.extend(_differences_for_snapshots((drift,)))
    return differences


def _insert_snapshot(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    assessment_id: str,
    snapshot: _Snapshot,
    now: str,
) -> None:
    connection.execute(
        """
        INSERT INTO migration_assessment_snapshots(
          migration_assessment_snapshot_id,workspace_id,tenant_id,migration_assessment_id,
          domain_code,adapter_version,source_count,target_count,source_sha256,target_sha256,
          source_sequence,target_sequence,source_watermark,target_watermark,matches,created_at)
        VALUES(?,?,?,?,?,'migration-adapter-v1',?,?,?,?,?,?,?,?,?,?)
        """,
        (
            _id("msp_"),
            context.workspace_id,
            context.tenant_id,
            assessment_id,
            snapshot.domain_code,
            snapshot.source_count,
            snapshot.target_count,
            snapshot.source_sha256,
            snapshot.target_sha256,
            snapshot.source_sequence,
            snapshot.target_sequence,
            snapshot.source_watermark,
            snapshot.target_watermark,
            1 if snapshot.matches else 0,
            now,
        ),
    )


def _insert_differences(
    connection: sqlite3.Connection,
    *,
    context: WorkspaceSessionContext,
    assessment_id: str,
    run_id: str | None,
    differences: Iterable[tuple[str, str, int, int, str, str, int, int, str]],
    now: str,
) -> None:
    for (
        domain,
        code,
        source_count,
        target_count,
        source_sha256,
        target_sha256,
        source_sequence,
        target_sequence,
        remediation,
    ) in differences:
        connection.execute(
            """
            INSERT INTO migration_differences(
              migration_difference_id,workspace_id,tenant_id,migration_assessment_id,
              migration_run_id,domain_code,difference_code,severity,source_count,target_count,
              source_sha256,target_sha256,source_sequence,target_sequence,remediation_code,
              status,detected_at)
            VALUES(?,?,?,?,?,?,?,'blocking',?,?,?,?,?,?,?,'open',?)
            """,
            (
                _id("mdf_"),
                context.workspace_id,
                context.tenant_id,
                assessment_id,
                run_id,
                domain,
                code,
                source_count,
                target_count,
                source_sha256,
                target_sha256,
                source_sequence,
                target_sequence,
                remediation,
                now,
            ),
        )


def _insert_checkpoint(
    connection: sqlite3.Connection,
    *,
    context: WorkspaceSessionContext,
    run_id: str,
    phase: str,
    sequence: int,
    input_sha256: str,
    output_sha256: str,
    watermark: int,
    freeze_generation: int,
    now: str,
) -> str:
    checkpoint_id = _id("mcp_")
    connection.execute(
        """
        INSERT INTO migration_cutover_checkpoints(
          migration_cutover_checkpoint_id,workspace_id,tenant_id,migration_run_id,
          phase_code,phase_sequence,input_sha256,output_sha256,source_watermark,
          target_watermark,freeze_generation,status,worker_fencing_token_sha256,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,'verified',?,?)
        """,
        (
            checkpoint_id,
            context.workspace_id,
            context.tenant_id,
            run_id,
            phase,
            sequence,
            input_sha256,
            output_sha256,
            watermark,
            watermark,
            freeze_generation,
            _digest({"run_id": run_id, "phase": phase, "nonce": secrets.token_hex(16)}),
            now,
        ),
    )
    return checkpoint_id


def _insert_event(
    connection: sqlite3.Connection,
    *,
    context: WorkspaceSessionContext,
    assessment_id: str,
    run_id: str | None,
    event_type: str,
    reason_code: str,
    payload: object,
    request_id: str,
    trace_id: str,
    now: str,
) -> None:
    event_id = _id("mev_")
    connection.execute(
        """
        INSERT INTO migration_events(
          migration_event_id,workspace_id,tenant_id,migration_assessment_id,
          migration_run_id,event_type,actor_account_id,reason_code,payload_sha256,
          request_id,trace_id,occurred_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id,
            context.workspace_id,
            context.tenant_id,
            assessment_id,
            run_id,
            event_type,
            context.account_id,
            reason_code,
            _digest(payload),
            request_id,
            trace_id,
            now,
        ),
    )
    for domain in ("audit", "migration"):
        connection.execute(
            """
            INSERT INTO migration_event_outbox(
              migration_event_outbox_id,workspace_id,tenant_id,migration_event_id,
              delivery_domain,status,attempt_count,available_at,delivered_at,created_at,updated_at)
            VALUES(?,?,?,?,?,'pending',0,?,NULL,?,?)
            """,
            (_id("mob_"), context.workspace_id, context.tenant_id, event_id, domain, now, now, now),
        )


def _assessment_response(
    connection: sqlite3.Connection,
    assessment_id: str,
    *,
    request_id: str,
    trace_id: str,
    replay: bool = False,
) -> MigrationAssessmentResponse:
    row = connection.execute(
        "SELECT * FROM migration_assessments WHERE migration_assessment_id=?",
        (assessment_id,),
    ).fetchone()
    plan = connection.execute(
        "SELECT * FROM migration_plans WHERE migration_assessment_id=?", (assessment_id,)
    ).fetchone()
    if row is None or plan is None:
        raise ApiError("migration.not_found")
    snapshots = connection.execute(
        "SELECT * FROM migration_assessment_snapshots WHERE migration_assessment_id=? "
        "ORDER BY domain_code",
        (assessment_id,),
    ).fetchall()
    return MigrationAssessmentResponse(
        assessment=MigrationAssessmentSummary(
            assessment_id=row["migration_assessment_id"],
            workspace_id=row["workspace_id"],
            source_kind=row["source_kind"],
            manifest_version=row["manifest_version"],
            source_manifest_sha256=row["source_manifest_sha256"],
            status=row["status"],
            object_count=row["object_count"],
            blocking_difference_count=row["blocking_difference_count"],
            write_watermark_before=row["write_watermark_before"],
            write_watermark_after=row["write_watermark_after"],
            credential_reentry_required_count=row["credential_reentry_required_count"],
            permission_expansion_count=row["permission_expansion_count"],
            revision=row["revision"],
            created_at=_parse_time(row["created_at"]),
            completed_at=_parse_time(row["completed_at"]),
            snapshots=tuple(_snapshot_summary(item) for item in snapshots),
            plan=_plan_summary(plan),
        ),
        idempotent_replay=replay,
        request_id=request_id,
        trace_id=trace_id,
    )


def _run_response(
    connection: sqlite3.Connection,
    run_id: str,
    *,
    request_id: str,
    trace_id: str,
    replay: bool = False,
) -> MigrationRunResponse:
    row = connection.execute(
        "SELECT * FROM migration_runs WHERE migration_run_id=?", (run_id,)
    ).fetchone()
    if row is None:
        raise ApiError("migration.not_found")
    checkpoints = connection.execute(
        "SELECT * FROM migration_cutover_checkpoints WHERE migration_run_id=? "
        "ORDER BY phase_sequence",
        (run_id,),
    ).fetchall()
    rollback = connection.execute(
        "SELECT * FROM migration_rollback_plans WHERE migration_run_id=?", (run_id,)
    ).fetchone()
    return MigrationRunResponse(
        run=MigrationRunSummary(
            run_id=row["migration_run_id"],
            assessment_id=row["migration_assessment_id"],
            plan_id=row["migration_plan_id"],
            status=row["status"],
            generation=row["generation"],
            freeze_generation=row["freeze_generation"],
            blocking_difference_count=row["blocking_difference_count"],
            revision=row["revision"],
            started_at=_parse_time(row["started_at"]),
            updated_at=_parse_time(row["updated_at"]),
            completed_at=_optional_time(row["completed_at"]),
            checkpoints=tuple(_checkpoint_summary(item) for item in checkpoints),
            rollback_plan=_rollback_summary(rollback) if rollback is not None else None,
        ),
        idempotent_replay=replay,
        request_id=request_id,
        trace_id=trace_id,
    )


def _snapshot_summary(row: sqlite3.Row) -> MigrationDomainSnapshot:
    return MigrationDomainSnapshot(
        domain_code=row["domain_code"],
        adapter_version=row["adapter_version"],
        source_count=row["source_count"],
        target_count=row["target_count"],
        source_sha256=row["source_sha256"],
        target_sha256=row["target_sha256"],
        source_sequence=row["source_sequence"],
        target_sequence=row["target_sequence"],
        source_watermark=row["source_watermark"],
        target_watermark=row["target_watermark"],
        matches=bool(row["matches"]),
    )


def _plan_summary(row: sqlite3.Row) -> MigrationPlanSummary:
    return MigrationPlanSummary(
        plan_id=row["migration_plan_id"],
        assessment_id=row["migration_assessment_id"],
        generation=row["generation"],
        status=row["status"],
        phases=tuple(json.loads(str(row["phase_set_json"]))),
        plan_sha256=row["plan_sha256"],
        planned_downtime_seconds=row["planned_downtime_seconds"],
        rollback_deadline_seconds=row["rollback_deadline_seconds"],
        revision=row["revision"],
        created_at=_parse_time(row["created_at"]),
    )


def _checkpoint_summary(row: sqlite3.Row) -> CutoverCheckpointSummary:
    return CutoverCheckpointSummary(
        checkpoint_id=row["migration_cutover_checkpoint_id"],
        run_id=row["migration_run_id"],
        phase_code=row["phase_code"],
        phase_sequence=row["phase_sequence"],
        input_sha256=row["input_sha256"],
        output_sha256=row["output_sha256"],
        source_watermark=row["source_watermark"],
        target_watermark=row["target_watermark"],
        freeze_generation=row["freeze_generation"],
        status=row["status"],
        created_at=_parse_time(row["created_at"]),
    )


def _rollback_summary(row: sqlite3.Row) -> RollbackPlanSummary:
    return RollbackPlanSummary(
        rollback_plan_id=row["migration_rollback_plan_id"],
        run_id=row["migration_run_id"],
        previous_run_id=row["previous_migration_run_id"],
        last_safe_checkpoint_id=row["last_safe_checkpoint_id"],
        compensation_order=tuple(json.loads(str(row["compensation_order_json"]))),
        rollback_plan_sha256=row["rollback_plan_sha256"],
        status=row["status"],
        revision=row["revision"],
        completed_at=_optional_time(row["completed_at"]),
    )


def _difference_summary(row: sqlite3.Row) -> MigrationDifferenceSummary:
    return MigrationDifferenceSummary(
        difference_id=row["migration_difference_id"],
        assessment_id=row["migration_assessment_id"],
        run_id=row["migration_run_id"],
        domain_code=row["domain_code"],
        difference_code=row["difference_code"],
        severity=row["severity"],
        source_count=row["source_count"],
        target_count=row["target_count"],
        source_sha256=row["source_sha256"],
        target_sha256=row["target_sha256"],
        source_sequence=row["source_sequence"],
        target_sequence=row["target_sequence"],
        remediation_code=row["remediation_code"],
        status=row["status"],
        detected_at=_parse_time(row["detected_at"]),
    )


def _stored_snapshots(connection: sqlite3.Connection, assessment_id: str) -> tuple[_Snapshot, ...]:
    rows = connection.execute(
        "SELECT * FROM migration_assessment_snapshots WHERE migration_assessment_id=? "
        "ORDER BY domain_code",
        (assessment_id,),
    ).fetchall()
    return tuple(
        _Snapshot(
            domain_code=str(row["domain_code"]),
            source_count=int(row["source_count"]),
            target_count=int(row["target_count"]),
            source_sha256=str(row["source_sha256"]),
            target_sha256=str(row["target_sha256"]),
            source_sequence=int(row["source_sequence"]),
            target_sequence=int(row["target_sequence"]),
            source_watermark=int(row["source_watermark"]),
            target_watermark=int(row["target_watermark"]),
        )
        for row in rows
    )


def _owned_assessment(
    connection: sqlite3.Connection, context: WorkspaceSessionContext, assessment_id: str
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM migration_assessments WHERE migration_assessment_id=? "
        "AND workspace_id=? AND tenant_id=?",
        (assessment_id, context.workspace_id, context.tenant_id),
    ).fetchone()
    if row is None:
        raise ApiError("migration.not_found")
    return row


def _owned_run(
    connection: sqlite3.Connection, context: WorkspaceSessionContext, run_id: str
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM migration_runs WHERE migration_run_id=? AND workspace_id=? AND tenant_id=?",
        (run_id, context.workspace_id, context.tenant_id),
    ).fetchone()
    if row is None:
        raise ApiError("migration.not_found")
    return row


def _migration_owner_row(
    connection: sqlite3.Connection, migration_id: str
) -> tuple[sqlite3.Row, str | None]:
    if migration_id.startswith("mas_"):
        row = connection.execute(
            "SELECT * FROM migration_assessments WHERE migration_assessment_id=?",
            (migration_id,),
        ).fetchone()
        run_id = None
    elif migration_id.startswith("mrn_"):
        run = connection.execute(
            "SELECT * FROM migration_runs WHERE migration_run_id=?", (migration_id,)
        ).fetchone()
        row = (
            connection.execute(
                "SELECT * FROM migration_assessments WHERE migration_assessment_id=?",
                (run["migration_assessment_id"],),
            ).fetchone()
            if run is not None
            else None
        )
        run_id = migration_id
    else:
        row = None
        run_id = None
    if row is None:
        raise ApiError("migration.not_found")
    return row, run_id


def _command_replay(
    connection: sqlite3.Connection,
    account_id: str,
    action_code: str,
    idempotency_sha256: str,
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM migration_action_commands WHERE actor_account_id=? "
        "AND action_code=? AND idempotency_sha256=?",
        (account_id, action_code, idempotency_sha256),
    ).fetchone()


def _insert_command(
    connection: sqlite3.Connection,
    *,
    account_id: str,
    action_code: str,
    idempotency_sha256: str,
    request_sha256: str,
    result_object_id: str,
    now: str,
) -> None:
    connection.execute(
        """
        INSERT INTO migration_action_commands(
          migration_action_command_id,actor_account_id,action_code,idempotency_sha256,
          request_sha256,result_object_id,created_at)
        VALUES(?,?,?,?,?,?,?)
        """,
        (
            _id("mac_"),
            account_id,
            action_code,
            idempotency_sha256,
            request_sha256,
            result_object_id,
            now,
        ),
    )


def _verify_replay(row: sqlite3.Row, request_sha256: str) -> None:
    if not secrets.compare_digest(str(row["request_sha256"]), request_sha256):
        raise ApiError("migration.idempotency_conflict")


def _connect(config: AppConfig) -> sqlite3.Connection:
    return connect_database(config.database_url)


def _rollback(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        connection.rollback()


def _id(prefix: str) -> str:
    return prefix + secrets.token_hex(16)


def _idempotency_sha256(account_id: str, idempotency_key: str) -> str:
    return _digest({"account_id": account_id, "idempotency_key": idempotency_key})


def _digest(value: object) -> str:
    if isinstance(value, str):
        raw = value.encode("utf-8")
    else:
        raw = _canonical_json(value).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_time(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _optional_time(value: object) -> datetime | None:
    return None if value is None else _parse_time(value)


def _after(previous: str, candidate: datetime) -> str:
    prior = _parse_time(previous)
    value = candidate.astimezone(UTC)
    if value <= prior:
        value = prior + timedelta(milliseconds=1)
    return _db_time(value)


__all__ = [
    "create_migration_assessment",
    "create_migration_run",
    "execute_migration_cutover",
    "execute_migration_rollback",
    "list_migration_differences",
]
