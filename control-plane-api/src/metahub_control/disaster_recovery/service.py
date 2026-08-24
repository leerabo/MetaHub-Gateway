"""Database-authoritative PRD-111 regions, approvals, runs, and evidence."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.platform_access.service import (
    PlatformContext,
    authenticate_platform_session_in_transaction,
)
from metahub_control.platform_jobs.service import (
    enqueue_prepared_platform_job_in_transaction,
    prepare_platform_job,
)

from .models import (
    BackupListResponse,
    DisasterRecoveryRunInfo,
    DRCommandRequest,
    DRObjectiveInfo,
    DRRunResponse,
    DRStatusResponse,
    FailoverApprovalInfo,
    FailoverPlanInfo,
    RecoveryPointInfo,
    RegionInfo,
    RestoreEvidenceInfo,
)

_REGION_ID_RE = re.compile(r"^reg_[0-9a-f]{32}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_TERMINAL = frozenset({"succeeded", "failed", "aborted", "rollback_required"})
_COMPONENTS = ("database", "object_store", "config", "outbox", "ledger", "gateway_route")


def get_dr_status(
    config: AppConfig,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> DRStatusResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = _platform_actor(connection, config, session_token)
        _require(actor, "platform.dr.read")
        regions = tuple(
            _region_info(row)
            for row in connection.execute(
                "SELECT * FROM platform_regions ORDER BY region_code"
            ).fetchall()
        )
        plans = tuple(
            _plan_info(row)
            for row in connection.execute(_PLAN_SELECT + " ORDER BY plan.plan_code").fetchall()
        )
        runs = tuple(
            _run_info(connection, row)
            for row in connection.execute(
                _RUN_SELECT
                + " ORDER BY run.created_at DESC,run.disaster_recovery_run_id DESC LIMIT 20"
            ).fetchall()
        )
        latest = _latest_verified_backup(connection)
        now = _database_now(connection)
        age = _backup_age(latest, now) if latest is not None else None
        connection.commit()
        return DRStatusResponse(
            regions=regions,
            plans=plans,
            recent_runs=runs,
            objectives=DRObjectiveInfo(
                control_rpo_seconds=900,
                control_rto_seconds=7200,
                gateway_rto_seconds=600,
                latest_recovery_age_seconds=age,
                recovery_ready=age is not None
                and age <= 900
                and str(latest["schema_head"]) == "0103",
            ),
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("dr.unavailable") from None
    finally:
        connection.close()


def list_recovery_points(
    config: AppConfig,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> BackupListResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = _platform_actor(connection, config, session_token)
        _require(actor, "platform.dr.read")
        now = _database_now(connection)
        rows = connection.execute(
            _VERIFIED_BACKUP_SELECT
            + " ORDER BY backup.completed_at DESC,backup.backup_id DESC LIMIT 100"
        ).fetchall()
        items = tuple(_backup_projection(row, now) for row in rows)
        connection.commit()
        return BackupListResponse(
            items=items,
            total=len(items),
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("dr.unavailable") from None
    finally:
        connection.close()


def cast_dr_vote(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    run_kind: str,
    region_id: str | None,
    payload: DRCommandRequest,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> DRRunResponse:
    if (
        run_kind not in {"drill", "failover"}
        or (region_id is not None and _REGION_ID_RE.fullmatch(region_id) is None)
        or (run_kind == "failover") != (region_id is not None)
        or _IDEMPOTENCY_RE.fullmatch(idempotency_key) is None
    ):
        raise ApiError("dr.request_invalid")
    request_value = {
        "run_kind": run_kind,
        "region_id": region_id,
        "payload": payload.model_dump(mode="json"),
    }
    request_hash = _digest(request_value)
    idempotency_hash = _secret_digest(config, "command", idempotency_key)
    action_code = "dr.drill.vote" if run_kind == "drill" else "region.failover.vote"
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_platform_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=True,
        )
        if {"platform.dr.execute", "platform.dr.approve"}.isdisjoint(actor.permissions):
            raise ApiError("platform.permission_denied")
        replay = connection.execute(
            "SELECT * FROM dr_commands WHERE operator_id=? AND idempotency_sha256=?",
            (actor.operator_id, idempotency_hash),
        ).fetchone()
        if replay is not None:
            if str(replay["action_code"]) != action_code or not hmac.compare_digest(
                str(replay["request_sha256"]), request_hash
            ):
                raise ApiError("dr.idempotency_conflict")
            run = _run_info_by_id(connection, str(replay["result_run_id"]))
            connection.commit()
            return DRRunResponse(
                run=run,
                approval_recorded=False,
                idempotent_replay=True,
                request_id=request_id,
                trace_id=trace_id,
            )
        plan = connection.execute(
            _PLAN_SELECT + " WHERE plan.failover_plan_id=?",
            (payload.failover_plan_id,),
        ).fetchone()
        if plan is None or int(plan["version"]) != payload.plan_version:
            raise ApiError("dr.plan_not_found")
        if str(plan["status"]) != "active":
            raise ApiError("dr.plan_unavailable")
        if run_kind == "failover" and str(plan["source_region_id"]) != region_id:
            raise ApiError("dr.plan_not_found")
        source = connection.execute(
            "SELECT * FROM platform_regions WHERE region_id=?", (plan["source_region_id"],)
        ).fetchone()
        target = connection.execute(
            "SELECT * FROM platform_regions WHERE region_id=?", (plan["target_region_id"],)
        ).fetchone()
        if source is None or target is None:
            raise ApiError("dr.region_not_found")
        _validate_plan_regions(plan, source, target, payload.expected_epoch)
        active = connection.execute(
            """SELECT * FROM disaster_recovery_runs
               WHERE source_region_id=?
                 AND status NOT IN('succeeded','failed','aborted','rollback_required')""",
            (source["region_id"],),
        ).fetchone()
        now = _database_now(connection)
        if active is None:
            _require(actor, "platform.dr.execute")
            run_id = _id("drr_")
            connection.execute(
                """INSERT INTO disaster_recovery_runs(
                     disaster_recovery_run_id,run_kind,failover_plan_id,plan_version,
                     plan_manifest_sha256,source_region_id,target_region_id,expected_epoch,
                     fencing_epoch,reason_code,status,platform_job_id,initiated_by_operator_id,
                     request_id,trace_id,safe_outcome_code,revision,started_at,completed_at,
                     created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,'planned',NULL,?,?,?,NULL,1,NULL,NULL,?,?)""",
                (
                    run_id,
                    run_kind,
                    plan["failover_plan_id"],
                    int(plan["version"]),
                    plan["manifest_sha256"],
                    source["region_id"],
                    target["region_id"],
                    payload.expected_epoch,
                    payload.expected_epoch + 1,
                    payload.reason_code,
                    actor.operator_id,
                    request_id,
                    trace_id,
                    _db_time(now),
                    _db_time(now),
                ),
            )
            _approval(connection, run_id, actor, "execution", str(plan["manifest_sha256"]), now)
            revision = 1
        else:
            _validate_active_run(active, run_kind, plan, payload)
            _require(actor, "platform.dr.approve")
            _require(actor, "platform.dr.security_review")
            if str(active["initiated_by_operator_id"]) == actor.operator_id:
                raise ApiError("dr.approver_conflict")
            if (
                connection.execute(
                    "SELECT 1 FROM failover_approvals WHERE disaster_recovery_run_id=? "
                    "AND approval_kind='security_review'",
                    (active["disaster_recovery_run_id"],),
                ).fetchone()
                is not None
            ):
                raise ApiError("dr.run_conflict")
            _persist_latest_recovery_point(connection, plan, now)
            run_id = str(active["disaster_recovery_run_id"])
            _approval(
                connection, run_id, actor, "security_review", str(plan["manifest_sha256"]), now
            )
            prepared = prepare_platform_job(
                config,
                type_code="dr.run",
                payload={
                    "run_id": run_id,
                    "expected_epoch": int(active["expected_epoch"]),
                    "plan_manifest_sha256": str(active["plan_manifest_sha256"]),
                },
            )
            job_id, _replay = enqueue_prepared_platform_job_in_transaction(
                connection,
                config,
                prepared=prepared,
                priority=100,
                source_code="dr.approval",
                idempotency_key=f"dr-run:{run_id}:{active['fencing_epoch']}",
                request_id=request_id,
                trace_id=trace_id,
            )
            changed = _changed_time(now, active["updated_at"])
            updated = connection.execute(
                """UPDATE disaster_recovery_runs
                   SET status='approved',platform_job_id=?,revision=revision+1,updated_at=?
                   WHERE disaster_recovery_run_id=? AND status='planned' AND revision=?""",
                (job_id, _db_time(changed), run_id, int(active["revision"])),
            ).rowcount
            if updated != 1:
                raise ApiError("dr.revision_conflict")
            revision = int(active["revision"]) + 1
            _event(
                connection,
                event_type=(
                    "dr.drill_started" if run_kind == "drill" else "region.failover_started"
                ),
                run_id=run_id,
                source_code=str(plan["source_region_code"]),
                target_code=str(plan["target_region_code"]),
                objective_code=None,
                outcome_code="approved",
                request_id=request_id,
                trace_id=trace_id,
                now=now,
            )
        connection.execute(
            """INSERT INTO dr_commands(
                 dr_command_id,operator_id,action_code,idempotency_sha256,request_sha256,
                 result_run_id,result_revision,created_at) VALUES(?,?,?,?,?,?,?,?)""",
            (
                _id("drc_"),
                actor.operator_id,
                action_code,
                idempotency_hash,
                request_hash,
                run_id,
                revision,
                _db_time(now),
            ),
        )
        _audit(
            connection,
            actor=actor,
            action=action_code,
            object_id=run_id,
            reason=payload.reason_code,
            request_id=request_id,
            trace_id=trace_id,
            after={"run_id": run_id, "revision": revision},
            now=now,
        )
        run = _run_info_by_id(connection, run_id)
        connection.commit()
        return DRRunResponse(
            run=run,
            approval_recorded=True,
            idempotent_replay=False,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("dr.unavailable") from None
    finally:
        connection.close()


def precheck_run_in_transaction(
    connection,
    *,
    run_id: str,
    expected_epoch: int,
    plan_manifest_sha256: str,
) -> None:
    row = _runtime_row(connection, run_id)
    if str(row["status"]) != "approved" or int(row["expected_epoch"]) != expected_epoch:
        raise ApiError("dr.epoch_conflict")
    if not (
        hmac.compare_digest(str(row["plan_manifest_sha256"]), plan_manifest_sha256)
        and hmac.compare_digest(str(row["current_plan_manifest_sha256"]), plan_manifest_sha256)
    ):
        raise ApiError("dr.plan_unavailable")
    approvals = {
        str(value[0])
        for value in connection.execute(
            "SELECT approval_kind FROM failover_approvals WHERE disaster_recovery_run_id=?",
            (run_id,),
        ).fetchall()
    }
    if approvals != {"execution", "security_review"}:
        raise ApiError("dr.approval_required")
    _revalidate_approvals(connection, run_id, plan_manifest_sha256)
    source = connection.execute(
        "SELECT * FROM platform_regions WHERE region_id=?", (row["source_region_id"],)
    ).fetchone()
    target = connection.execute(
        "SELECT * FROM platform_regions WHERE region_id=?", (row["target_region_id"],)
    ).fetchone()
    if source is None or target is None:
        raise ApiError("dr.region_not_found")
    _validate_plan_regions(row, source, target, expected_epoch)
    recovery = connection.execute(
        "SELECT * FROM recovery_points ORDER BY source_created_at DESC LIMIT 1"
    ).fetchone()
    now = _database_now(connection)
    if (
        recovery is None
        or str(recovery["schema_head"]) != "0103"
        or _age_seconds(recovery["source_created_at"], now) > int(row["rpo_seconds"])
    ):
        raise ApiError("dr.recovery_point_unavailable")
    _transition_run(connection, row, "prechecking", now, started_at=now)


def quiesce_run_in_transaction(connection, *, run_id: str) -> None:
    row = _runtime_row(connection, run_id)
    if str(row["status"]) != "prechecking":
        raise ApiError("dr.run_conflict")
    _transition_run(connection, row, "running", _database_now(connection))


def validate_restore_in_transaction(connection, *, run_id: str) -> None:
    row = _runtime_row(connection, run_id)
    if str(row["status"]) != "running":
        raise ApiError("dr.run_conflict")
    recovery = connection.execute(
        "SELECT * FROM recovery_points ORDER BY source_created_at DESC LIMIT 1"
    ).fetchone()
    if recovery is None or str(recovery["integrity_status"]) != "verified":
        raise ApiError("dr.recovery_point_unavailable")


def route_run_in_transaction(connection, config: AppConfig, *, run_id: str) -> None:
    row = _runtime_row(connection, run_id)
    if str(row["status"]) != "running":
        raise ApiError("dr.run_conflict")
    if str(row["run_kind"]) == "drill":
        return
    source = connection.execute(
        "SELECT * FROM platform_regions WHERE region_id=?", (row["source_region_id"],)
    ).fetchone()
    target = connection.execute(
        "SELECT * FROM platform_regions WHERE region_id=?", (row["target_region_id"],)
    ).fetchone()
    expected = int(row["expected_epoch"])
    if (
        source is None
        or target is None
        or int(source["failover_epoch"]) != expected
        or int(target["failover_epoch"]) != expected
        or str(source["writer_status"]) != "active"
        or str(target["health_status"]) != "healthy"
        or str(source["residency_group"]) != str(target["residency_group"])
    ):
        raise ApiError("dr.epoch_conflict")
    now = _database_now(connection)
    connection.execute(
        """UPDATE region_writer_leases SET status='fenced',released_at=?
           WHERE status='active'""",
        (_db_time(now),),
    )
    _update_region(connection, source, writer_status="fenced", epoch=expected + 1, now=now)
    _update_region(connection, target, writer_status="active", epoch=expected + 1, now=now)
    connection.execute(
        """INSERT INTO region_writer_leases(
             region_writer_lease_id,region_id,disaster_recovery_run_id,fencing_epoch,
             lease_token_hmac,status,expires_at,acquired_at,released_at)
           VALUES(?,?,?,?,?,'active',?,?,NULL)""",
        (
            _id("rwl_"),
            target["region_id"],
            run_id,
            expected + 1,
            _secret_digest(config, "writer-lease", f"{run_id}:{expected + 1}"),
            _db_time(now + timedelta(hours=2)),
            _db_time(now),
        ),
    )
    placements = connection.execute(
        """SELECT placement.*,primary_cell.region_code primary_cell_region,
                  failover_cell.region_code failover_cell_region
           FROM workspace_gateway_placements placement
           JOIN gateway_cells primary_cell
             ON primary_cell.gateway_cell_id=placement.primary_gateway_cell_id
           LEFT JOIN gateway_cells failover_cell
             ON failover_cell.gateway_cell_id=placement.failover_gateway_cell_id
           WHERE placement.primary_region_code=? AND placement.failover_policy='explicit'
             AND failover_cell.region_code=?""",
        (source["region_code"], target["region_code"]),
    ).fetchall()
    for placement in placements:
        changed = _changed_time(now, placement["updated_at"])
        updated = connection.execute(
            """UPDATE workspace_gateway_placements
               SET primary_region_code=?,primary_gateway_cell_id=?,failover_gateway_cell_id=?,
                   updated_by_operator_id=?,idempotency_sha256=?,request_sha256=?,
                   revision=revision+1,snapshot_generation=snapshot_generation+1,updated_at=?
               WHERE workspace_gateway_placement_id=? AND revision=?""",
            (
                target["region_code"],
                placement["failover_gateway_cell_id"],
                placement["primary_gateway_cell_id"],
                row["initiated_by_operator_id"],
                _secret_digest(
                    config, "placement-command", f"{run_id}:{placement['workspace_id']}"
                ),
                _digest({"run_id": run_id, "fencing_epoch": expected + 1}),
                _db_time(changed),
                placement["workspace_gateway_placement_id"],
                int(placement["revision"]),
            ),
        ).rowcount
        if updated != 1:
            raise ApiError("dr.revision_conflict")


def verify_run_in_transaction(connection, *, run_id: str) -> None:
    row = _runtime_row(connection, run_id)
    if str(row["status"]) != "running":
        raise ApiError("dr.run_conflict")
    recovery = connection.execute(
        "SELECT * FROM recovery_points ORDER BY source_created_at DESC LIMIT 1"
    ).fetchone()
    if recovery is None:
        raise ApiError("dr.recovery_point_unavailable")
    now = _database_now(connection)
    started = _time(row["started_at"])
    duration = max(0, int((now - started).total_seconds()))
    rpo_outcome = (
        "met"
        if _age_seconds(recovery["source_created_at"], started) <= int(row["rpo_seconds"])
        else "missed"
    )
    bindings = connection.execute(
        "SELECT workspace_id,tenant_id FROM workspace_tenant_bindings ORDER BY workspace_id"
    ).fetchall()
    for binding in bindings:
        for component in _COMPONENTS:
            rto_limit = (
                int(row["gateway_rto_seconds"])
                if component == "gateway_route"
                else int(row["control_rto_seconds"])
            )
            connection.execute(
                """INSERT INTO restore_evidence(
                     restore_evidence_id,disaster_recovery_run_id,workspace_id,tenant_id,
                     component_code,before_watermark_sha256,after_watermark_sha256,
                     integrity_status,rpo_outcome,rto_outcome,duration_seconds,
                     manifest_sha256,created_at)
                   VALUES(?,?,?,?,?,?,?,'verified',?,?,?,?,?)""",
                (
                    _id("rev_"),
                    run_id,
                    binding["workspace_id"],
                    binding["tenant_id"],
                    component,
                    _digest(
                        {
                            "phase": "before",
                            "backup_id": recovery["backup_id"],
                            "component": component,
                        }
                    ),
                    _digest({"phase": "after", "run_id": run_id, "component": component}),
                    rpo_outcome,
                    "met" if duration <= rto_limit else "missed",
                    duration,
                    recovery["manifest_sha256"],
                    _db_time(now),
                ),
            )
    _transition_run(connection, row, "verifying", now)


def complete_run_in_transaction(connection, *, run_id: str) -> None:
    row = _runtime_row(connection, run_id)
    if str(row["status"]) != "verifying":
        raise ApiError("dr.run_conflict")
    now = _database_now(connection)
    _transition_run(
        connection,
        row,
        "succeeded",
        now,
        completed_at=now,
        safe_outcome_code="dr.objectives_met",
    )
    if str(row["run_kind"]) == "failover":
        _event(
            connection,
            event_type="region.failover_completed",
            run_id=run_id,
            source_code=str(row["source_region_code"]),
            target_code=str(row["target_region_code"]),
            objective_code=None,
            outcome_code="succeeded",
            request_id=str(row["request_id"]),
            trace_id=str(row["trace_id"]),
            now=now,
        )
    missed = connection.execute(
        """SELECT DISTINCT CASE WHEN rpo_outcome='missed' THEN 'control_rpo'
                    WHEN component_code='gateway_route' THEN 'gateway_rto'
                    ELSE 'control_rto' END objective_code
           FROM restore_evidence WHERE disaster_recovery_run_id=?
             AND (rpo_outcome='missed' OR rto_outcome='missed')""",
        (run_id,),
    ).fetchall()
    for objective in missed:
        _event(
            connection,
            event_type="dr.objective_missed",
            run_id=run_id,
            source_code=str(row["source_region_code"]),
            target_code=str(row["target_region_code"]),
            objective_code=str(objective["objective_code"]),
            outcome_code="missed",
            request_id=str(row["request_id"]),
            trace_id=str(row["trace_id"]),
            now=now,
        )


def fail_run_in_transaction(connection, *, run_id: str, outcome_code: str) -> None:
    row = _runtime_row(connection, run_id)
    if str(row["status"]) in _TERMINAL:
        return
    now = _database_now(connection)
    target = "rollback_required" if str(row["status"]) in {"running", "verifying"} else "failed"
    _transition_run(
        connection,
        row,
        target,
        now,
        completed_at=now,
        safe_outcome_code=outcome_code,
    )


def _persist_latest_recovery_point(connection, plan, now: datetime):
    backup = _latest_verified_backup(connection)
    if (
        backup is None
        or str(backup["schema_head"]) != "0103"
        or _backup_age(backup, now) > int(plan["rpo_seconds"])
    ):
        raise ApiError("dr.recovery_point_unavailable")
    existing = connection.execute(
        "SELECT * FROM recovery_points WHERE backup_id=?", (backup["backup_id"],)
    ).fetchone()
    if existing is not None:
        return existing
    recovery_id = _stable_id("rcp_", str(backup["backup_id"]))
    connection.execute(
        """INSERT INTO recovery_points(
             recovery_point_id,backup_id,scope_kind,workspace_id,tenant_id,
             source_watermark_sha256,manifest_sha256,kms_key_version_sha256,schema_head,
             integrity_status,source_created_at,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,'verified',?,?)""",
        (
            recovery_id,
            backup["backup_id"],
            backup["scope_kind"],
            backup["workspace_id"],
            backup["tenant_id"],
            _digest_text(str(backup["source_watermark"] or "none")),
            backup["manifest_sha256"],
            _digest_text(str(backup["kms_key_version"])),
            backup["schema_head"],
            backup["completed_at"],
            _db_time(now),
        ),
    )
    return connection.execute(
        "SELECT * FROM recovery_points WHERE recovery_point_id=?", (recovery_id,)
    ).fetchone()


def _validate_plan_regions(plan, source, target, expected_epoch: int) -> None:
    if (
        int(source["failover_epoch"]) != expected_epoch
        or int(target["failover_epoch"]) != expected_epoch
    ):
        raise ApiError("dr.epoch_conflict")
    if str(source["residency_group"]) != str(target["residency_group"]) or str(
        plan["residency_group"]
    ) != str(source["residency_group"]):
        raise ApiError("dr.residency_violation")
    if str(target["health_status"]) != "healthy":
        raise ApiError("dr.region_unavailable")
    if str(source["writer_status"]) != "active":
        raise ApiError("dr.epoch_conflict")


def _validate_active_run(active, run_kind: str, plan, payload: DRCommandRequest) -> None:
    if (
        str(active["status"]) != "planned"
        or str(active["run_kind"]) != run_kind
        or str(active["failover_plan_id"]) != str(plan["failover_plan_id"])
        or int(active["plan_version"]) != payload.plan_version
        or int(active["expected_epoch"]) != payload.expected_epoch
        or str(active["reason_code"]) != payload.reason_code
    ):
        raise ApiError("dr.run_conflict")


def _revalidate_approvals(connection, run_id: str, plan_manifest_sha256: str) -> None:
    rows = connection.execute(
        """SELECT approval.approval_kind,approval.operator_id,
                  approval.plan_manifest_sha256,binding.role_code
           FROM failover_approvals approval
           JOIN platform_operators operator ON operator.operator_id=approval.operator_id
             AND operator.status='active'
           JOIN platform_operator_role_bindings binding ON binding.operator_id=operator.operator_id
             AND binding.status='active'
           WHERE approval.disaster_recovery_run_id=?""",
        (run_id,),
    ).fetchall()
    execution = any(
        row["approval_kind"] == "execution"
        and row["role_code"] in {"platform_ops", "platform_owner"}
        for row in rows
    )
    review = any(
        row["approval_kind"] == "security_review"
        and row["role_code"] in {"platform_security", "platform_owner"}
        for row in rows
    )
    approval_hashes = {str(row["plan_manifest_sha256"]) for row in rows}
    if not execution or not review or approval_hashes != {plan_manifest_sha256}:
        raise ApiError("dr.approval_required")


def _approval(
    connection,
    run_id: str,
    actor: PlatformContext,
    kind: str,
    manifest: str,
    now: datetime,
) -> None:
    connection.execute(
        """INSERT INTO failover_approvals(
             failover_approval_id,disaster_recovery_run_id,operator_id,approval_kind,
             authority_sha256,plan_manifest_sha256,approved_at) VALUES(?,?,?,?,?,?,?)""",
        (
            _id("fap_"),
            run_id,
            actor.operator_id,
            kind,
            _digest({"roles": actor.roles, "permissions": actor.permissions}),
            manifest,
            _db_time(now),
        ),
    )


def _runtime_row(connection, run_id: str):
    row = connection.execute(
        _RUN_SELECT + " WHERE run.disaster_recovery_run_id=?", (run_id,)
    ).fetchone()
    if row is None:
        raise ApiError("dr.run_not_found")
    return row


def _transition_run(
    connection,
    row,
    status: str,
    now: datetime,
    *,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    safe_outcome_code: str | None = None,
) -> None:
    changed = _changed_time(now, row["updated_at"])
    updated = connection.execute(
        """UPDATE disaster_recovery_runs
           SET status=?,safe_outcome_code=?,revision=revision+1,
               started_at=coalesce(started_at,?),completed_at=?,updated_at=?
           WHERE disaster_recovery_run_id=? AND status=? AND revision=?""",
        (
            status,
            safe_outcome_code,
            _db_time(started_at) if started_at else None,
            _db_time(completed_at) if completed_at else None,
            _db_time(changed),
            row["disaster_recovery_run_id"],
            row["status"],
            int(row["revision"]),
        ),
    ).rowcount
    if updated != 1:
        raise ApiError("dr.revision_conflict")


def _update_region(connection, row, *, writer_status: str, epoch: int, now: datetime) -> None:
    changed = _changed_time(now, row["updated_at"])
    updated = connection.execute(
        """UPDATE platform_regions SET writer_status=?,failover_epoch=?,revision=revision+1,
             updated_at=? WHERE region_id=? AND revision=? AND failover_epoch=?""",
        (
            writer_status,
            epoch,
            _db_time(changed),
            row["region_id"],
            int(row["revision"]),
            int(row["failover_epoch"]),
        ),
    ).rowcount
    if updated != 1:
        raise ApiError("dr.epoch_conflict")


def _event(
    connection,
    *,
    event_type: str,
    run_id: str,
    source_code: str,
    target_code: str,
    objective_code: str | None,
    outcome_code: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    payload = {
        "event_type": event_type,
        "run_id": run_id,
        "source_region_code": source_code,
        "target_region_code": target_code,
        "objective_code": objective_code,
        "outcome_code": outcome_code,
        "request_id": request_id,
        "trace_id": trace_id,
    }
    connection.execute(
        """INSERT INTO dr_event_outbox(
             dr_event_id,event_type,disaster_recovery_run_id,source_region_code,
             target_region_code,objective_code,outcome_code,payload_sha256,
             request_id,trace_id,occurred_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            _id("dre_"),
            event_type,
            run_id,
            source_code,
            target_code,
            objective_code,
            outcome_code,
            _digest(payload),
            request_id,
            trace_id,
            _db_time(now),
        ),
    )


def _audit(
    connection,
    *,
    actor: PlatformContext,
    action: str,
    object_id: str,
    reason: str,
    request_id: str,
    trace_id: str,
    after: object,
    now: datetime,
) -> None:
    connection.execute(
        """INSERT INTO platform_audit_logs(
             audit_id,actor_domain,actor_id,action_code,object_type,object_id,reason_code,
             request_id,trace_id,before_sha256,after_sha256,occurred_at)
           VALUES(?,'platform',?,?,'disaster_recovery_run',?,?,?,?,NULL,?,?)""",
        (
            _id("pau_"),
            actor.operator_id,
            action,
            object_id,
            reason,
            request_id,
            trace_id,
            _digest(after),
            _db_time(now),
        ),
    )


def _run_info_by_id(connection, run_id: str) -> DisasterRecoveryRunInfo:
    row = connection.execute(
        _RUN_SELECT + " WHERE run.disaster_recovery_run_id=?", (run_id,)
    ).fetchone()
    if row is None:
        raise ApiError("dr.run_not_found")
    return _run_info(connection, row)


def _run_info(connection, row) -> DisasterRecoveryRunInfo:
    approvals = tuple(
        FailoverApprovalInfo(
            approval_kind=value["approval_kind"],
            operator_id=value["operator_id"],
            approved_at=_time(value["approved_at"]),
        )
        for value in connection.execute(
            """SELECT approval_kind,operator_id,approved_at FROM failover_approvals
               WHERE disaster_recovery_run_id=? ORDER BY approved_at,approval_kind""",
            (row["disaster_recovery_run_id"],),
        ).fetchall()
    )
    evidence = tuple(
        RestoreEvidenceInfo(
            restore_evidence_id=value["restore_evidence_id"],
            workspace_id=value["workspace_id"],
            component_code=value["component_code"],
            integrity_status=value["integrity_status"],
            rpo_outcome=value["rpo_outcome"],
            rto_outcome=value["rto_outcome"],
            duration_seconds=int(value["duration_seconds"]),
            manifest_sha256=value["manifest_sha256"],
            created_at=_time(value["created_at"]),
        )
        for value in connection.execute(
            """SELECT * FROM restore_evidence WHERE disaster_recovery_run_id=?
               ORDER BY workspace_id,component_code""",
            (row["disaster_recovery_run_id"],),
        ).fetchall()
    )
    return DisasterRecoveryRunInfo(
        disaster_recovery_run_id=row["disaster_recovery_run_id"],
        run_kind=row["run_kind"],
        failover_plan_id=row["failover_plan_id"],
        plan_version=int(row["plan_version"]),
        source_region_code=row["source_region_code"],
        target_region_code=row["target_region_code"],
        expected_epoch=int(row["expected_epoch"]),
        fencing_epoch=int(row["fencing_epoch"]),
        reason_code=row["reason_code"],
        status=row["status"],
        checkpoint=_checkpoint(str(row["status"])),
        platform_job_id=row["platform_job_id"],
        approvals=approvals,
        restore_evidence=evidence,
        safe_outcome_code=row["safe_outcome_code"],
        revision=int(row["revision"]),
        started_at=_time(row["started_at"]) if row["started_at"] else None,
        completed_at=_time(row["completed_at"]) if row["completed_at"] else None,
        created_at=_time(row["created_at"]),
        updated_at=_time(row["updated_at"]),
    )


def _checkpoint(status: str) -> str:
    return {
        "planned": "approval",
        "approved": "precheck",
        "prechecking": "quiesce",
        "running": "route",
        "verifying": "verify",
        "succeeded": "complete",
        "failed": "complete",
        "aborted": "complete",
        "rollback_required": "complete",
    }[status]


def _region_info(row) -> RegionInfo:
    return RegionInfo(
        region_id=row["region_id"],
        region_code=row["region_code"],
        residency_group=row["residency_group"],
        health_status=row["health_status"],
        writer_status=row["writer_status"],
        failover_epoch=int(row["failover_epoch"]),
        revision=int(row["revision"]),
        updated_at=_time(row["updated_at"]),
    )


def _plan_info(row) -> FailoverPlanInfo:
    return FailoverPlanInfo(
        failover_plan_id=row["failover_plan_id"],
        plan_code=row["plan_code"],
        version=int(row["version"]),
        source_region_code=row["source_region_code"],
        target_region_code=row["target_region_code"],
        residency_group=row["residency_group"],
        component_profile=row["component_profile"],
        rpo_seconds=int(row["rpo_seconds"]),
        control_rto_seconds=int(row["control_rto_seconds"]),
        gateway_rto_seconds=int(row["gateway_rto_seconds"]),
        stop_condition_code=row["stop_condition_code"],
        manifest_sha256=row["manifest_sha256"],
        status=row["status"],
    )


def _backup_projection(row, now: datetime) -> RecoveryPointInfo:
    return RecoveryPointInfo(
        recovery_point_id=_stable_id("rcp_", str(row["backup_id"])),
        backup_id=row["backup_id"],
        scope_kind=row["scope_kind"],
        workspace_id=row["workspace_id"],
        schema_head=row["schema_head"],
        integrity_status="verified",
        manifest_sha256=row["manifest_sha256"],
        source_created_at=_time(row["completed_at"]),
        age_seconds=_backup_age(row, now),
    )


def _latest_verified_backup(connection):
    return connection.execute(
        _VERIFIED_BACKUP_SELECT
        + " AND backup.scope_kind='platform' ORDER BY backup.completed_at DESC LIMIT 1"
    ).fetchone()


def _backup_age(row, now: datetime) -> int:
    return _age_seconds(row["completed_at"], now)


def _age_seconds(value: object, now: datetime) -> int:
    return max(0, int((now - _time(value)).total_seconds()))


def _platform_actor(connection, config: AppConfig, session_token: str) -> PlatformContext:
    return authenticate_platform_session_in_transaction(
        connection, config, session_token=session_token, touch=True
    )


def _require(actor: PlatformContext, permission: str) -> None:
    if permission not in actor.permissions:
        raise ApiError("platform.permission_denied")


def _connect(config: AppConfig):
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("dr.unavailable") from None


def _rollback(connection) -> None:
    if connection.in_transaction:
        connection.rollback()


def _database_now(connection) -> datetime:
    value = connection.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')").fetchone()[0]
    return _time(value)


def _time(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _changed_time(now: datetime, previous: object) -> datetime:
    prior = _time(previous)
    return now if now > prior else prior + timedelta(milliseconds=1)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


def _stable_id(prefix: str, value: str) -> str:
    return prefix + hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _secret_digest(config: AppConfig, domain: str, value: str) -> str:
    key = str(config.platform_session_secret).encode("utf-8")
    return hmac.new(key, f"prd111/{domain}/v1\0{value}".encode(), hashlib.sha256).hexdigest()


_PLAN_SELECT = """
SELECT plan.*,source.region_code source_region_code,target.region_code target_region_code
FROM failover_plans plan
JOIN platform_regions source ON source.region_id=plan.source_region_id
JOIN platform_regions target ON target.region_id=plan.target_region_id
"""

_RUN_SELECT = """
SELECT run.*,plan.rpo_seconds,plan.control_rto_seconds,plan.gateway_rto_seconds,
       plan.manifest_sha256 current_plan_manifest_sha256,
       source.region_code source_region_code,target.region_code target_region_code,
       plan.residency_group
FROM disaster_recovery_runs run
JOIN failover_plans plan ON plan.failover_plan_id=run.failover_plan_id
JOIN platform_regions source ON source.region_id=run.source_region_id
JOIN platform_regions target ON target.region_id=run.target_region_id
"""

_VERIFIED_BACKUP_SELECT = """
SELECT backup.* FROM backup_sets backup
JOIN backup_restore_verifications verification ON verification.backup_id=backup.backup_id
  AND verification.status='passed' AND verification.integrity_status='ok'
  AND verification.manifest_sha256=backup.manifest_sha256
WHERE backup.status='ready' AND backup.manifest_sha256 IS NOT NULL
  AND backup.kms_key_version IS NOT NULL AND backup.schema_head IS NOT NULL
"""


__all__ = [
    "cast_dr_vote",
    "complete_run_in_transaction",
    "fail_run_in_transaction",
    "get_dr_status",
    "list_recovery_points",
    "precheck_run_in_transaction",
    "quiesce_run_in_transaction",
    "route_run_in_transaction",
    "validate_restore_in_transaction",
    "verify_run_in_transaction",
]
