"""Backup status, restore approval, and isolated DR drill state for PRD-129."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from datetime import UTC, datetime, timedelta

from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.identity.security_service import (
    consume_step_up_grant_in_transaction,
)
from metahub_control.platform_access.service import PlatformContext

from .backup_models import (
    AgentBackupReportRequest,
    AgentDrillReportRequest,
    BackupReportResponse,
    BackupReportSummary,
    BackupStatusResponse,
    BackupStatusSummary,
    PlatformBackupStatusListResponse,
    RestoreDrillMutationRequest,
    RestoreDrillResponse,
    RestoreDrillSummary,
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
    _utcnow,
    _workspace_context,
)
from .upgrade_service import _authenticated_agent

_DEFAULT_RPO_SECONDS = 86_400
_VERIFICATION_CODES = frozenset(
    {
        "manifest_integrity",
        "owner_binding",
        "schema_compatibility",
        "config_consistency",
        "usage_cursor",
        "audit_chain",
        "secret_reference",
        "device_reenrollment",
        "minimal_request",
    }
)


def report_agent_backup(
    config: AppConfig,
    deployment_id: str,
    payload: AgentBackupReportRequest,
    *,
    verified_fingerprint: str,
    edge_session_token: str,
    request_id: str,
    trace_id: str,
) -> BackupReportResponse:
    request_sha256 = _digest(payload.model_dump(mode="json", exclude={"attempt_nonce"}))
    idempotency_sha256 = _agent_idempotency(
        config, deployment_id, "edge.backup.report", payload.attempt_nonce
    )
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        agent = _authenticated_agent(
            connection, config, deployment_id, verified_fingerprint, edge_session_token
        )
        _require_backup_capability(agent)
        replay = _action_replay(
            connection, "agent", deployment_id, "edge.backup.report", idempotency_sha256
        )
        if replay is not None:
            _verify_replay(replay, request_sha256)
            row = _backup_row(connection, deployment_id, str(replay["result_object_id"]))
            result = _backup_response(row, deployment_id, request_id, trace_id, replay=True)
            connection.commit()
            return result

        current = connection.execute(
            "SELECT * FROM edge_backup_statuses WHERE edge_deployment_id=?",
            (deployment_id,),
        ).fetchone()
        if current is not None and payload.source_generation < int(
            current["latest_source_generation"]
        ):
            raise ApiError("edge_backup.generation_conflict")
        now = _db_time(_utcnow())
        connection.execute(
            """
            INSERT INTO edge_backup_reports(
              edge_backup_id,workspace_id,tenant_id,edge_deployment_id,
              source_generation,schema_head,storage_kind,status,size_bucket,chunk_count,
              manifest_sha256,kms_key_id_sha256,check_outcome,failure_code,started_at,
              completed_at,retention_until,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                payload.backup_id,
                agent["workspace_id"],
                agent["tenant_id"],
                deployment_id,
                payload.source_generation,
                payload.schema_head,
                payload.storage_kind,
                payload.status,
                payload.size_bucket,
                payload.chunk_count,
                payload.manifest_sha256,
                payload.kms_key_id_sha256,
                payload.check_outcome,
                payload.failure_code,
                _db_time(payload.started_at.astimezone(UTC)),
                _db_time(payload.completed_at.astimezone(UTC)),
                _db_time(payload.retention_until.astimezone(UTC)),
                now,
            ),
        )
        _upsert_status(connection, agent, payload, now=now)
        _insert_action(
            connection,
            actor_domain="agent",
            actor_id=deployment_id,
            action_code="edge.backup.report",
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            result_object_id=payload.backup_id,
            now=now,
        )
        _event(
            connection,
            agent,
            event_type=(
                "edge.backup_completed"
                if payload.status == "completed"
                else "edge.backup_failed"
            ),
            object_type="backup",
            object_id=payload.backup_id,
            reason_code=payload.failure_code or "backup_verified",
            actor_domain="agent",
            actor_id=deployment_id,
            projection={
                "source_generation": payload.source_generation,
                "size_bucket": payload.size_bucket,
                "check_outcome": payload.check_outcome,
                "manifest_sha256": payload.manifest_sha256,
            },
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        row = _backup_row(connection, deployment_id, payload.backup_id)
        result = _backup_response(row, deployment_id, request_id, trace_id)
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("edge_backup.report_conflict") from None
    except (DbError, sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge_backup.unavailable") from None
    finally:
        connection.close()


def get_workspace_backup_status(
    config: AppConfig,
    deployment_id: str,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> BackupStatusResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        context = _workspace_context(
            connection,
            config,
            session_token=session_token,
            csrf_token=None,
            permission="edge.backup.read",
        )
        deployment = _owned_deployment(connection, context, deployment_id)
        result = BackupStatusResponse(
            status=_status_summary(connection, deployment, now=_utcnow()),
            request_id=_identifier(request_id),
            trace_id=_identifier(trace_id),
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (DbError, sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge_backup.unavailable") from None
    finally:
        connection.close()


def mutate_workspace_restore_drill(
    config: AppConfig,
    deployment_id: str,
    payload: RestoreDrillMutationRequest,
    *,
    session_token: str,
    csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> RestoreDrillResponse:
    action_code = "edge.drill.request" if payload.action == "request" else "edge.restore.approve"
    request_sha256 = _digest(payload.model_dump(mode="json"))
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = _workspace_context(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            permission="edge.drill.manage",
        )
        deployment = _owned_deployment(connection, context, deployment_id)
        idempotency_sha256 = _workspace_action_idempotency(
            config, context.account_id, action_code, idempotency_key
        )
        replay = _action_replay(
            connection,
            "workspace",
            context.account_id,
            action_code,
            idempotency_sha256,
        )
        if replay is not None:
            _verify_replay(replay, request_sha256)
            row = _drill_row(connection, deployment_id, str(replay["result_object_id"]))
            result = _drill_response(
                connection, row, deployment_id, request_id, trace_id, replay=True
            )
            connection.commit()
            return result
        now = _utcnow()
        now_db = _db_time(now)
        if payload.action == "request":
            drill_id = _request_drill(
                connection, deployment, context.account_id, payload, now=now_db
            )
            consume_step_up_grant_in_transaction(
                connection,
                config,
                context=context,
                grant=step_up_grant,
                action_code=action_code,
                resource_type="edge_deployment",
                resource_id=deployment_id,
                now=now,
            )
            event_type = "edge.drill_requested"
            reason_code = payload.reason_code
        else:
            if "edge.restore.approve" not in context.permission_codes:
                raise ApiError("workspace_session.permission_denied")
            drill_id = str(payload.drill_id)
            row = _drill_row(connection, deployment_id, drill_id)
            restore = _restore_row(connection, str(row["restore_job_id"]))
            if (
                str(row["status"]) != "requested"
                or str(restore["status"]) != "requested"
                or int(row["revision"]) != payload.expected_revision
                or int(restore["revision"]) != payload.expected_revision
                or hmac.compare_digest(
                    str(restore["requested_by_account_id"]), context.account_id
                )
            ):
                raise ApiError("edge_backup.approval_conflict")
            consume_step_up_grant_in_transaction(
                connection,
                config,
                context=context,
                grant=step_up_grant,
                action_code=action_code,
                resource_type="private_dr_drill",
                resource_id=drill_id,
                now=now,
            )
            changed = _changed_stamp(now, row["updated_at"])
            if connection.execute(
                """
                UPDATE edge_restore_jobs SET status='approved',current_step='staging',
                  approved_by_account_id=?,approved_at=?,revision=revision+1,updated_at=?
                WHERE restore_job_id=? AND status='requested' AND revision=?
                """,
                (
                    context.account_id,
                    now_db,
                    changed,
                    restore["restore_job_id"],
                    payload.expected_revision,
                ),
            ).rowcount != 1:
                raise ApiError("edge_backup.approval_conflict")
            if connection.execute(
                """
                UPDATE private_dr_drills SET status='approved',revision=revision+1,
                  updated_at=? WHERE private_dr_drill_id=? AND status='requested' AND revision=?
                """,
                (changed, drill_id, payload.expected_revision),
            ).rowcount != 1:
                raise ApiError("edge_backup.approval_conflict")
            event_type = "edge.restore_approved"
            reason_code = payload.reason_code

        _insert_action(
            connection,
            actor_domain="workspace",
            actor_id=context.account_id,
            action_code=action_code,
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            result_object_id=drill_id,
            now=now_db,
        )
        _event(
            connection,
            deployment,
            event_type=event_type,
            object_type="drill" if payload.action == "request" else "restore",
            object_id=drill_id if payload.action == "request" else str(
                _drill_row(connection, deployment_id, drill_id)["restore_job_id"]
            ),
            reason_code=reason_code,
            actor_domain="workspace",
            actor_id=context.account_id,
            projection={"drill_id": drill_id, "action": payload.action},
            request_id=request_id,
            trace_id=trace_id,
            now=now_db,
        )
        current = _drill_row(connection, deployment_id, drill_id)
        result = _drill_response(connection, current, deployment_id, request_id, trace_id)
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("edge_backup.state_conflict") from None
    except (DbError, sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge_backup.unavailable") from None
    finally:
        connection.close()


def report_agent_restore_drill(
    config: AppConfig,
    deployment_id: str,
    drill_id: str,
    payload: AgentDrillReportRequest,
    *,
    verified_fingerprint: str,
    edge_session_token: str,
    request_id: str,
    trace_id: str,
) -> RestoreDrillResponse:
    request_sha256 = _digest(payload.model_dump(mode="json", exclude={"attempt_nonce"}))
    idempotency_sha256 = _agent_idempotency(
        config, deployment_id, "edge.drill.report", payload.attempt_nonce
    )
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        agent = _authenticated_agent(
            connection, config, deployment_id, verified_fingerprint, edge_session_token
        )
        _require_backup_capability(agent)
        replay = _action_replay(
            connection, "agent", deployment_id, "edge.drill.report", idempotency_sha256
        )
        if replay is not None:
            _verify_replay(replay, request_sha256)
            row = _drill_row(connection, deployment_id, str(replay["result_object_id"]))
            result = _drill_response(
                connection, row, deployment_id, request_id, trace_id, replay=True
            )
            connection.commit()
            return result
        drill = _drill_row(connection, deployment_id, drill_id)
        restore = _restore_row(connection, payload.restore_job_id)
        if (
            str(drill["restore_job_id"]) != payload.restore_job_id
            or str(drill["status"]) not in {"approved", "running"}
            or str(restore["status"]) not in {"approved", "running", "verifying"}
            or int(drill["revision"]) != payload.expected_revision
            or int(restore["revision"]) != payload.expected_revision
            or {item.verification_code for item in payload.verifications}
            != _VERIFICATION_CODES
        ):
            raise ApiError("edge_backup.drill_report_conflict")
        now_value = _utcnow()
        now = _db_time(now_value)
        for verification in payload.verifications:
            connection.execute(
                """
                INSERT INTO edge_restore_verifications(
                  restore_verification_id,workspace_id,tenant_id,restore_job_id,
                  verification_code,outcome,evidence_sha256,checked_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    _id("rvf_"),
                    agent["workspace_id"],
                    agent["tenant_id"],
                    payload.restore_job_id,
                    verification.verification_code,
                    verification.outcome,
                    verification.evidence_sha256,
                    _db_time(verification.checked_at.astimezone(UTC)),
                ),
            )
        objective_outcome = (
            "met"
            if payload.outcome == "succeeded"
            and payload.actual_rpo_seconds <= int(drill["rpo_objective_seconds"])
            and payload.actual_rto_seconds <= int(drill["rto_objective_seconds"])
            else "missed"
        )
        changed = _changed_stamp(now_value, drill["updated_at"])
        if connection.execute(
            """
            UPDATE edge_restore_jobs SET status=?,current_step='complete',completed_at=?,
              revision=revision+1,updated_at=? WHERE restore_job_id=?
              AND status IN('approved','running','verifying') AND revision=?
            """,
            (
                payload.outcome,
                now,
                changed,
                payload.restore_job_id,
                payload.expected_revision,
            ),
        ).rowcount != 1:
            raise ApiError("edge_backup.drill_report_conflict")
        if connection.execute(
            """
            UPDATE private_dr_drills SET status=?,actual_rpo_seconds=?,actual_rto_seconds=?,
              objective_outcome=?,started_at=coalesce(started_at,?),completed_at=?,
              revision=revision+1,updated_at=? WHERE private_dr_drill_id=?
              AND status IN('approved','running') AND revision=?
            """,
            (
                payload.outcome,
                payload.actual_rpo_seconds,
                payload.actual_rto_seconds,
                objective_outcome,
                now,
                now,
                changed,
                drill_id,
                payload.expected_revision,
            ),
        ).rowcount != 1:
            raise ApiError("edge_backup.drill_report_conflict")
        _insert_action(
            connection,
            actor_domain="agent",
            actor_id=deployment_id,
            action_code="edge.drill.report",
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            result_object_id=drill_id,
            now=now,
        )
        _event(
            connection,
            agent,
            event_type=(
                "edge.restore_completed"
                if payload.outcome == "succeeded"
                else "edge.restore_failed"
            ),
            object_type="restore",
            object_id=payload.restore_job_id,
            reason_code=(
                "restore_verified" if payload.outcome == "succeeded" else "restore_failed"
            ),
            actor_domain="agent",
            actor_id=deployment_id,
            projection={"drill_id": drill_id, "outcome": payload.outcome},
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        _event(
            connection,
            agent,
            event_type=(
                "edge.drill_completed"
                if objective_outcome == "met"
                else "edge.dr_objective_missed"
            ),
            object_type="drill",
            object_id=drill_id,
            reason_code=("objectives_met" if objective_outcome == "met" else "objective_missed"),
            actor_domain="agent",
            actor_id=deployment_id,
            projection={
                "actual_rpo_seconds": payload.actual_rpo_seconds,
                "actual_rto_seconds": payload.actual_rto_seconds,
                "objective_outcome": objective_outcome,
                "production_untouched": True,
            },
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        row = _drill_row(connection, deployment_id, drill_id)
        result = _drill_response(connection, row, deployment_id, request_id, trace_id)
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("edge_backup.drill_report_conflict") from None
    except (DbError, sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge_backup.unavailable") from None
    finally:
        connection.close()


def list_platform_backup_statuses(
    config: AppConfig,
    context: PlatformContext,
    *,
    request_id: str,
    trace_id: str,
) -> PlatformBackupStatusListResponse:
    if "platform.edge_backup.read" not in context.permissions:
        raise ApiError("platform.permission_denied")
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        deployments = connection.execute(
            """
            SELECT deployment.* FROM edge_deployments deployment
            WHERE EXISTS(SELECT 1 FROM edge_backup_statuses status
              WHERE status.edge_deployment_id=deployment.edge_deployment_id)
            ORDER BY deployment.updated_at DESC,deployment.edge_deployment_id DESC LIMIT 200
            """
        ).fetchall()
        result = PlatformBackupStatusListResponse(
            statuses=tuple(
                _status_summary(connection, deployment, now=_utcnow())
                for deployment in deployments
            ),
            request_id=_identifier(request_id),
            trace_id=_identifier(trace_id),
        )
        connection.rollback()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (DbError, sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge_backup.unavailable") from None
    finally:
        connection.close()


def _request_drill(
    connection: sqlite3.Connection,
    deployment: sqlite3.Row,
    account_id: str,
    payload: RestoreDrillMutationRequest,
    *,
    now: str,
) -> str:
    backup = _backup_row(
        connection, str(deployment["edge_deployment_id"]), str(payload.source_backup_id)
    )
    if str(backup["status"]) != "completed" or str(backup["check_outcome"]) != "verified":
        raise ApiError("edge_backup.source_ineligible")
    active = connection.execute(
        """
        SELECT 1 FROM private_dr_drills WHERE edge_deployment_id=?
          AND status IN('requested','approved','running') LIMIT 1
        """,
        (deployment["edge_deployment_id"],),
    ).fetchone()
    if active is not None:
        raise ApiError("edge_backup.drill_active")
    restore_id = _id("rst_")
    drill_id = _id("pdd_")
    connection.execute(
        """
        INSERT INTO edge_restore_jobs(
          restore_job_id,workspace_id,tenant_id,edge_deployment_id,source_backup_id,
          target_kind,status,current_step,requested_by_account_id,approved_by_account_id,
          device_reenrollment_required,expected_backup_generation,revision,reason_code,
          requested_at,approved_at,completed_at,updated_at)
        VALUES(?,?,?,?,?,'drill','requested','approval',?,NULL,1,?,1,?,?,NULL,NULL,?)
        """,
        (
            restore_id,
            deployment["workspace_id"],
            deployment["tenant_id"],
            deployment["edge_deployment_id"],
            backup["edge_backup_id"],
            account_id,
            backup["source_generation"],
            payload.reason_code,
            now,
            now,
        ),
    )
    connection.execute(
        """
        INSERT INTO private_dr_drills(
          private_dr_drill_id,workspace_id,tenant_id,edge_deployment_id,restore_job_id,
          status,quarter_code,rpo_objective_seconds,rto_objective_seconds,
          actual_rpo_seconds,actual_rto_seconds,isolated_target_sha256,
          production_untouched,objective_outcome,revision,created_at,started_at,
          completed_at,updated_at)
        VALUES(?,?,?,?,?,'requested',?,?,?,NULL,NULL,?,1,NULL,1,?,NULL,NULL,?)
        """,
        (
            drill_id,
            deployment["workspace_id"],
            deployment["tenant_id"],
            deployment["edge_deployment_id"],
            restore_id,
            payload.quarter_code,
            payload.rpo_objective_seconds,
            payload.rto_objective_seconds,
            payload.isolated_target_sha256,
            now,
            now,
        ),
    )
    return drill_id


def _upsert_status(
    connection: sqlite3.Connection,
    agent: sqlite3.Row,
    payload: AgentBackupReportRequest,
    *,
    now: str,
) -> None:
    row = connection.execute(
        "SELECT * FROM edge_backup_statuses WHERE edge_deployment_id=?",
        (agent["edge_deployment_id"],),
    ).fetchone()
    status = "current" if payload.status == "completed" else "failed"
    success = (
        _db_time(payload.completed_at.astimezone(UTC))
        if payload.status == "completed"
        else None
    )
    if row is None:
        connection.execute(
            """
            INSERT INTO edge_backup_statuses(
              workspace_id,tenant_id,edge_deployment_id,latest_backup_id,
              latest_source_generation,status,rpo_objective_seconds,last_success_at,
              last_check_outcome,revision,updated_at)
            VALUES(?,?,?,?,?,?,?, ?,?,1,?)
            """,
            (
                agent["workspace_id"],
                agent["tenant_id"],
                agent["edge_deployment_id"],
                payload.backup_id,
                payload.source_generation,
                status,
                _DEFAULT_RPO_SECONDS,
                success,
                payload.check_outcome,
                now,
            ),
        )
        return
    changed = _changed_stamp(_datetime(now), row["updated_at"])
    connection.execute(
        """
        UPDATE edge_backup_statuses SET latest_backup_id=?,latest_source_generation=?,
          status=?,last_success_at=coalesce(?,last_success_at),last_check_outcome=?,
          revision=revision+1,updated_at=? WHERE edge_deployment_id=? AND revision=?
        """,
        (
            payload.backup_id,
            max(payload.source_generation, int(row["latest_source_generation"])),
            status,
            success,
            payload.check_outcome,
            changed,
            agent["edge_deployment_id"],
            row["revision"],
        ),
    )


def _status_summary(
    connection: sqlite3.Connection, deployment: sqlite3.Row, *, now: datetime
) -> BackupStatusSummary:
    deployment_id = str(deployment["edge_deployment_id"])
    row = connection.execute(
        "SELECT * FROM edge_backup_statuses WHERE edge_deployment_id=?", (deployment_id,)
    ).fetchone()
    backups = connection.execute(
        """
        SELECT * FROM edge_backup_reports WHERE edge_deployment_id=?
        ORDER BY completed_at DESC,edge_backup_id DESC LIMIT 12
        """,
        (deployment_id,),
    ).fetchall()
    drills = connection.execute(
        """
        SELECT drill.*,restore.source_backup_id,restore.requested_by_account_id,
          restore.approved_by_account_id
        FROM private_dr_drills drill JOIN edge_restore_jobs restore
          ON restore.restore_job_id=drill.restore_job_id
        WHERE drill.edge_deployment_id=?
        ORDER BY drill.created_at DESC,drill.private_dr_drill_id DESC LIMIT 8
        """,
        (deployment_id,),
    ).fetchall()
    if row is None:
        return BackupStatusSummary(
            deployment_id=deployment_id,
            status="never",
            rpo_objective_seconds=_DEFAULT_RPO_SECONDS,
            latest_source_generation=0,
            last_success_at=None,
            last_check_outcome=None,
            revision=1,
            updated_at=_datetime(deployment["updated_at"]),
            latest_backup=None,
            recent_backups=tuple(_backup_summary(item) for item in backups),
            recent_drills=tuple(_drill_summary(item) for item in drills),
        )
    status = str(row["status"])
    if status == "current" and row["last_success_at"] is not None:
        if now - _datetime(row["last_success_at"]) > timedelta(
            seconds=int(row["rpo_objective_seconds"])
        ):
            status = "overdue"
    latest = next(
        (item for item in backups if item["edge_backup_id"] == row["latest_backup_id"]),
        None,
    )
    return BackupStatusSummary(
        deployment_id=deployment_id,
        status=status,  # type: ignore[arg-type]
        rpo_objective_seconds=int(row["rpo_objective_seconds"]),
        latest_source_generation=int(row["latest_source_generation"]),
        last_success_at=(
            _datetime(row["last_success_at"]) if row["last_success_at"] is not None else None
        ),
        last_check_outcome=row["last_check_outcome"],
        revision=int(row["revision"]),
        updated_at=_datetime(row["updated_at"]),
        latest_backup=_backup_summary(latest) if latest is not None else None,
        recent_backups=tuple(_backup_summary(item) for item in backups),
        recent_drills=tuple(_drill_summary(item) for item in drills),
    )


def _backup_response(
    row: sqlite3.Row,
    deployment_id: str,
    request_id: str,
    trace_id: str,
    *,
    replay: bool = False,
) -> BackupReportResponse:
    return BackupReportResponse(
        deployment_id=deployment_id,
        backup=_backup_summary(row),
        idempotent_replay=replay,
        request_id=_identifier(request_id),
        trace_id=_identifier(trace_id),
    )


def _backup_summary(row: sqlite3.Row) -> BackupReportSummary:
    return BackupReportSummary(
        backup_id=str(row["edge_backup_id"]),
        source_generation=int(row["source_generation"]),
        schema_head=str(row["schema_head"]),
        storage_kind=str(row["storage_kind"]),  # type: ignore[arg-type]
        status=str(row["status"]),  # type: ignore[arg-type]
        size_bucket=str(row["size_bucket"]),  # type: ignore[arg-type]
        chunk_count=int(row["chunk_count"]),
        manifest_sha256=row["manifest_sha256"],
        kms_key_id_sha256=row["kms_key_id_sha256"],
        check_outcome=str(row["check_outcome"]),  # type: ignore[arg-type]
        failure_code=row["failure_code"],
        started_at=_datetime(row["started_at"]),
        completed_at=_datetime(row["completed_at"]),
        retention_until=_datetime(row["retention_until"]),
    )


def _drill_response(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    deployment_id: str,
    request_id: str,
    trace_id: str,
    *,
    replay: bool = False,
) -> RestoreDrillResponse:
    projected = connection.execute(
        """
        SELECT drill.*,restore.source_backup_id,restore.requested_by_account_id,
          restore.approved_by_account_id
        FROM private_dr_drills drill JOIN edge_restore_jobs restore
          ON restore.restore_job_id=drill.restore_job_id
        WHERE drill.private_dr_drill_id=?
        """,
        (row["private_dr_drill_id"],),
    ).fetchone()
    if projected is None:
        raise ApiError("edge_backup.drill_not_found")
    return RestoreDrillResponse(
        deployment_id=deployment_id,
        drill=_drill_summary(projected),
        idempotent_replay=replay,
        request_id=_identifier(request_id),
        trace_id=_identifier(trace_id),
    )


def _drill_summary(row: sqlite3.Row) -> RestoreDrillSummary:
    return RestoreDrillSummary(
        drill_id=str(row["private_dr_drill_id"]),
        restore_job_id=str(row["restore_job_id"]),
        source_backup_id=str(row["source_backup_id"]),
        status=str(row["status"]),  # type: ignore[arg-type]
        quarter_code=str(row["quarter_code"]),
        rpo_objective_seconds=int(row["rpo_objective_seconds"]),
        rto_objective_seconds=int(row["rto_objective_seconds"]),
        actual_rpo_seconds=(
            int(row["actual_rpo_seconds"]) if row["actual_rpo_seconds"] is not None else None
        ),
        actual_rto_seconds=(
            int(row["actual_rto_seconds"]) if row["actual_rto_seconds"] is not None else None
        ),
        isolated_target_sha256=str(row["isolated_target_sha256"]),
        production_untouched=True,
        objective_outcome=row["objective_outcome"],
        revision=int(row["revision"]),
        requested_by_account_id=str(row["requested_by_account_id"]),
        approved_by_account_id=row["approved_by_account_id"],
        created_at=_datetime(row["created_at"]),
        started_at=_datetime(row["started_at"]) if row["started_at"] is not None else None,
        completed_at=(
            _datetime(row["completed_at"]) if row["completed_at"] is not None else None
        ),
        updated_at=_datetime(row["updated_at"]),
    )


def _backup_row(
    connection: sqlite3.Connection, deployment_id: str, backup_id: str
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM edge_backup_reports WHERE edge_deployment_id=? AND edge_backup_id=?",
        (deployment_id, backup_id),
    ).fetchone()
    if row is None:
        raise ApiError("edge_backup.not_found")
    return row


def _drill_row(
    connection: sqlite3.Connection, deployment_id: str, drill_id: str
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM private_dr_drills WHERE edge_deployment_id=? AND private_dr_drill_id=?",
        (deployment_id, drill_id),
    ).fetchone()
    if row is None:
        raise ApiError("edge_backup.drill_not_found")
    return row


def _restore_row(connection: sqlite3.Connection, restore_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM edge_restore_jobs WHERE restore_job_id=?", (restore_id,)
    ).fetchone()
    if row is None:
        raise ApiError("edge_backup.drill_not_found")
    return row


def _action_replay(
    connection: sqlite3.Connection,
    actor_domain: str,
    actor_id: str,
    action_code: str,
    idempotency_sha256: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT * FROM edge_backup_action_commands
        WHERE actor_domain=? AND actor_id=? AND action_code=? AND idempotency_sha256=?
        """,
        (actor_domain, actor_id, action_code, idempotency_sha256),
    ).fetchone()


def _insert_action(
    connection: sqlite3.Connection,
    *,
    actor_domain: str,
    actor_id: str,
    action_code: str,
    idempotency_sha256: str,
    request_sha256: str,
    result_object_id: str,
    now: str,
) -> None:
    connection.execute(
        """
        INSERT INTO edge_backup_action_commands(
          edge_backup_action_command_id,actor_domain,actor_id,action_code,
          idempotency_sha256,request_sha256,result_object_id,created_at)
        VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            _id("bac_"),
            actor_domain,
            actor_id,
            action_code,
            idempotency_sha256,
            request_sha256,
            result_object_id,
            now,
        ),
    )


def _verify_replay(row: sqlite3.Row, request_sha256: str) -> None:
    if not hmac.compare_digest(str(row["request_sha256"]), request_sha256):
        raise ApiError("edge_backup.idempotency_conflict")


def _event(
    connection: sqlite3.Connection,
    owner: sqlite3.Row,
    *,
    event_type: str,
    object_type: str,
    object_id: str,
    reason_code: str,
    actor_domain: str,
    actor_id: str,
    projection: dict[str, object],
    request_id: str,
    trace_id: str,
    now: str,
) -> None:
    event_id = _id("bev_")
    connection.execute(
        """
        INSERT INTO edge_backup_events(
          edge_backup_event_id,workspace_id,tenant_id,edge_deployment_id,event_type,
          object_type,object_id,reason_code,actor_domain,actor_id,projection_sha256,
          request_id,trace_id,occurred_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id,
            owner["workspace_id"],
            owner["tenant_id"],
            owner["edge_deployment_id"],
            event_type,
            object_type,
            object_id,
            reason_code,
            actor_domain,
            actor_id,
            _digest(projection),
            _identifier(request_id),
            _identifier(trace_id),
            now,
        ),
    )
    domains = ("workspace", "security") if object_type in {"restore", "drill"} else (
        "workspace",
        "operations",
    )
    for domain in domains:
        connection.execute(
            """
            INSERT INTO edge_backup_outbox(
              edge_backup_outbox_id,workspace_id,tenant_id,edge_backup_event_id,
              delivery_domain,status,attempt_count,available_at,delivered_at,
              created_at,updated_at)
            VALUES(?,?,?,?,?,'pending',0,?,NULL,?,?)
            """,
            (
                _id("bou_"),
                owner["workspace_id"],
                owner["tenant_id"],
                event_id,
                domain,
                now,
                now,
                now,
            ),
        )


def _require_backup_capability(agent: sqlite3.Row) -> None:
    try:
        capabilities = json.loads(str(agent["capability_codes_json"]))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        raise ApiError("edge.authentication_failed") from None
    if type(capabilities) is not list or "backup" not in capabilities:
        raise ApiError("edge.authentication_failed")


def _agent_idempotency(
    config: AppConfig, deployment_id: str, action: str, nonce: str
) -> str:
    return hmac.new(
        config.identity_session_secret.encode("utf-8"),
        f"metahub/prd129/agent/v1\0{deployment_id}\0{action}\0{nonce}".encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _workspace_action_idempotency(
    config: AppConfig, account_id: str, action: str, value: str
) -> str:
    return hmac.new(
        config.workspace_session_secret.encode("utf-8"),
        f"metahub/prd129/workspace/v1\0{account_id}\0{action}\0{value}".encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _changed_stamp(now: datetime, previous: object) -> str:
    candidate = _db_time(now)
    return candidate if candidate > str(previous) else _db_time(
        _datetime(previous) + timedelta(milliseconds=1)
    )


__all__ = (
    "get_workspace_backup_status",
    "list_platform_backup_statuses",
    "mutate_workspace_restore_drill",
    "report_agent_backup",
    "report_agent_restore_drill",
)
