"""Fleet release, rollout, maintenance, and Agent upgrade state machines."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from metahub_shared.db.errors import DbError
from metahub_shared.private_gateway import (
    GatewayRelease,
    GatewayReleaseArtifact,
    GatewayReleaseManifest,
    MaintenanceWindowContract,
    UnsignedUpgradeCommand,
    UpgradeAuthority,
    UpgradeContractError,
    UpgradeExecutionReport,
    UpgradePreflightEvidence,
    canonical_edge_json_bytes,
    maintenance_window_open,
    sign_gateway_release,
    sign_upgrade_command,
    upgrade_report_sha256,
    verify_gateway_release,
)
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError
from pydantic import BaseModel, ConfigDict, Field

from metahub_control.identity.security_service import (
    consume_global_step_up_grant_in_transaction,
    consume_step_up_grant_in_transaction,
)
from metahub_control.platform_access.service import PlatformContext
from metahub_control.platform_jobs.catalog import register_job_handler
from metahub_control.platform_jobs.runtime import PlatformJobContext, PlatformJobFailure
from metahub_control.platform_jobs.service import (
    PreparedPlatformJob,
    enqueue_prepared_platform_job_in_transaction,
    prepare_platform_job,
)
from metahub_control.workspace_access.service import WorkspaceSessionContext

from .installer_service import _verified_release_row
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
    _workspace_idempotency,
)
from .upgrade_models import (
    AgentUpgradeCommandResponse,
    AgentUpgradeMutationRequest,
    AgentUpgradeReceiptResponse,
    AgentUpgradeReportRequest,
    CompatibilitySummary,
    DeploymentReleaseHeadSummary,
    FleetCompatibilityResponse,
    FleetRolloutMutationRequest,
    GatewayReleaseListResponse,
    GatewayReleaseResponse,
    GatewayReleaseSummary,
    MaintenanceWindowSummary,
    MaintenanceWindowUpdateRequest,
    PlatformGatewayReleaseMutationRequest,
    PlatformRolloutCreateRequest,
    RollbackAttemptSummary,
    RollbackCreateRequest,
    RollbackResponse,
    RolloutBatchSummary,
    RolloutListResponse,
    RolloutResponse,
    RolloutSummary,
    RolloutTargetSummary,
    WorkspaceUpgradeResponse,
)

_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_CURRENT_CONTROL_VERSION = "1.0.0"
_MAX_ROLLOUT_TARGETS = 1000
_COMMAND_TTL = timedelta(minutes=10)
_LEASE_TTL = timedelta(seconds=60)
_HEALTH_MAX_AGE = timedelta(minutes=10)


class FleetRolloutJobInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    rollout_id: str = Field(pattern=r"^rol_[0-9a-f]{32}$")
    expected_generation: int = Field(ge=1)


def list_platform_gateway_releases(
    config: AppConfig,
    context: PlatformContext,
    *,
    request_id: str,
    trace_id: str,
) -> GatewayReleaseListResponse:
    _require_platform(context, "platform.edge_release.read")
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        rows = connection.execute(
            "SELECT * FROM edge_gateway_releases "
            "ORDER BY submitted_at DESC,edge_gateway_release_id DESC LIMIT 100"
        ).fetchall()
        result = GatewayReleaseListResponse(
            releases=tuple(_release_summary(connection, row) for row in rows),
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
        raise ApiError("fleet.release_unavailable") from None
    finally:
        connection.close()


def mutate_platform_gateway_release(
    config: AppConfig,
    context: PlatformContext,
    payload: PlatformGatewayReleaseMutationRequest,
    *,
    step_up_grant: str | None,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> GatewayReleaseResponse:
    permission = (
        "platform.edge_release.approve"
        if payload.action == "approve"
        else "platform.edge_release.manage"
    )
    _require_platform(context, permission)
    action = f"fleet.release.{payload.action}"
    request_sha256 = _digest(payload.model_dump(mode="json"))
    idempotency_sha256 = _platform_idempotency(
        config, context.operator_id, action, idempotency_key
    )
    release_id = payload.release_id or _id("ger_")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        replay = _action_replay(
            connection, "platform", context.operator_id, action, idempotency_sha256
        )
        if replay is not None:
            if not hmac.compare_digest(str(replay["request_sha256"]), request_sha256):
                raise ApiError("fleet.release_idempotency_conflict")
            row = _release_row(connection, str(replay["result_object_id"]))
            result = _release_response(connection, row, request_id, trace_id, replay=True)
            connection.commit()
            return result

        now_value = _utcnow()
        now = _db_time(now_value)
        if payload.action == "submit":
            row = _submit_release(
                connection,
                config,
                context,
                payload,
                release_id=release_id,
                now=now,
            )
        else:
            if step_up_grant is None:
                raise ApiError("step_up.required")
            row = _release_row(connection, release_id)
            if int(row["revision"]) != payload.expected_revision:
                raise ApiError("fleet.release_revision_conflict")
            if payload.action == "approve":
                if str(row["status"]) != "approval_pending":
                    raise ApiError("fleet.release_state_conflict")
                if str(row["submitted_by_operator_id"]) == context.operator_id:
                    raise ApiError("fleet.release_self_approval_denied")
            elif str(row["status"]) not in {"approval_pending", "released"}:
                raise ApiError("fleet.release_state_conflict")
            consume_global_step_up_grant_in_transaction(
                connection,
                config,
                account_id=context.account_id,
                global_session_id=context.global_session_id,
                grant=step_up_grant,
                action_code=action,
                resource_type="edge_gateway_release",
                resource_id=release_id,
                now=now_value,
            )
            if payload.action == "approve":
                updated = connection.execute(
                    """
                    UPDATE edge_gateway_releases SET status='released',
                      approved_by_operator_id=?,approved_at=?,revision=revision+1
                    WHERE edge_gateway_release_id=? AND status='approval_pending' AND revision=?
                    """,
                    (context.operator_id, now, release_id, payload.expected_revision),
                ).rowcount
            else:
                updated = connection.execute(
                    """
                    UPDATE edge_gateway_releases SET status='withdrawn',withdrawn_at=?,
                      revision=revision+1 WHERE edge_gateway_release_id=?
                      AND status IN('approval_pending','released') AND revision=?
                    """,
                    (now, release_id, payload.expected_revision),
                ).rowcount
            if updated != 1:
                raise ApiError("fleet.release_revision_conflict")
            row = _release_row(connection, release_id)
        _insert_action(
            connection,
            actor_domain="platform",
            actor_id=context.operator_id,
            action=action,
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            result_object_id=release_id,
            now=now,
        )
        _platform_audit(
            connection,
            context,
            action=action,
            object_type="edge_gateway_release",
            object_id=release_id,
            reason=payload.reason_code,
            request_id=request_id,
            trace_id=trace_id,
            after={"status": str(row["status"]), "revision": int(row["revision"])},
            now=now,
        )
        result = _release_response(connection, row, request_id, trace_id, replay=False)
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except UpgradeContractError:
        _rollback(connection)
        raise ApiError("fleet.release_integrity_failed") from None
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("fleet.release_conflict") from None
    except (DbError, sqlite3.Error, KeyError, OSError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("fleet.release_unavailable") from None
    finally:
        connection.close()


def list_platform_rollouts(
    config: AppConfig,
    context: PlatformContext,
    *,
    request_id: str,
    trace_id: str,
) -> RolloutListResponse:
    _require_platform(context, "platform.edge_rollout.read")
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        rows = connection.execute(
            "SELECT * FROM edge_fleet_rollouts "
            "ORDER BY created_at DESC,rollout_id DESC LIMIT 50"
        ).fetchall()
        result = RolloutListResponse(
            rollouts=tuple(_rollout_summary(connection, row) for row in rows),
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
        raise ApiError("fleet.rollout_unavailable") from None
    finally:
        connection.close()


def create_platform_rollout(
    config: AppConfig,
    context: PlatformContext,
    release_id: str,
    payload: PlatformRolloutCreateRequest,
    *,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> RolloutResponse:
    _require_platform(context, "platform.edge_rollout.manage")
    action = "fleet.rollout.create"
    request_sha256 = _digest(
        {"release_id": release_id, **payload.model_dump(mode="json")}
    )
    idempotency_sha256 = _platform_idempotency(
        config, context.operator_id, action, idempotency_key
    )
    rollout_id = _id("rol_")
    prepared = prepare_platform_job(
        config,
        type_code="fleet.rollout.advance",
        payload={"rollout_id": rollout_id, "expected_generation": 1},
    )
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        replay = _action_replay(
            connection, "platform", context.operator_id, action, idempotency_sha256
        )
        if replay is not None:
            if not hmac.compare_digest(str(replay["request_sha256"]), request_sha256):
                raise ApiError("fleet.rollout_idempotency_conflict")
            row = _rollout_row(connection, str(replay["result_object_id"]))
            result = _rollout_response(connection, row, request_id, trace_id, replay=True)
            connection.commit()
            return result
        release = _release_row(connection, release_id)
        if str(release["status"]) != "released":
            raise ApiError("fleet.release_state_conflict")
        targets = _eligible_rollout_targets(
            connection, release, tuple(payload.deployment_ids)
        )
        if len(targets) > _MAX_ROLLOUT_TARGETS:
            raise ApiError("fleet.rollout_request_invalid")
        now_value = _utcnow()
        scheduled_at = _common_window_time(targets, now_value)
        now = _db_time(now_value)
        seed = hmac.new(
            config.platform_session_secret.encode("utf-8"),
            f"prd127/rollout-seed/v1\0{rollout_id}\0{release_id}".encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        ordered = sorted(
            targets,
            key=lambda item: (
                _stable_bucket(config, rollout_id, str(item["edge_deployment_id"])),
                str(item["edge_deployment_id"]),
            ),
        )
        canary_count = max(
            1, math.ceil(len(ordered) * payload.canary_basis_points / 10_000)
        )
        batch_groups = [ordered[:canary_count]]
        remainder = ordered[canary_count:]
        batch_groups.extend(
            remainder[index : index + payload.batch_size]
            for index in range(0, len(remainder), payload.batch_size)
        )
        connection.execute(
            """
            INSERT INTO edge_fleet_rollouts(
              rollout_id,edge_gateway_release_id,status,generation,cancel_watermark,
              rollout_seed_sha256,canary_basis_points,observation_seconds,batch_size,
              failure_threshold_basis_points,target_count,succeeded_count,failed_count,
              current_batch_sequence,platform_job_id,requested_by_operator_id,
              pause_reason_code,revision,request_id,trace_id,scheduled_at,started_at,
              paused_at,completed_at,created_at,updated_at)
            VALUES(?,?,'preparing',1,0,?,?,?,?,?, ?,0,0,0,NULL,?,NULL,1,?,?,?,NULL,NULL,NULL,?,?)
            """,
            (
                rollout_id,
                release_id,
                seed,
                payload.canary_basis_points,
                payload.observation_seconds,
                payload.batch_size,
                payload.failure_threshold_basis_points,
                len(ordered),
                context.operator_id,
                _identifier(request_id),
                _identifier(trace_id),
                _db_time(scheduled_at),
                now,
                now,
            ),
        )
        for sequence, group in enumerate(batch_groups):
            batch_id = _id("rbt_")
            connection.execute(
                """
                INSERT INTO edge_rollout_batches(
                  rollout_batch_id,rollout_id,sequence,batch_kind,status,target_count,
                  succeeded_count,failed_count,revision,started_at,observe_until,
                  completed_at,created_at,updated_at)
                VALUES(?,?,?,?,'pending',?,0,0,1,NULL,NULL,NULL,?,?)
                """,
                (
                    batch_id,
                    rollout_id,
                    sequence,
                    "canary" if sequence == 0 else "progressive",
                    len(group),
                    now,
                    now,
                ),
            )
            for target in group:
                head = target["head"]
                desired = int(head["desired_generation"]) + 1
                connection.execute(
                    """
                    INSERT INTO edge_rollout_targets(
                      rollout_target_id,workspace_id,tenant_id,edge_deployment_id,
                      rollout_id,rollout_batch_id,edge_gateway_release_id,stable_bucket,
                      maintenance_policy_generation,rollout_generation,desired_generation,
                      current_version,target_version,status,failure_reason_code,revision,
                      selected_at,started_at,completed_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?,'selected',NULL,1,?,NULL,NULL,?)
                    """,
                    (
                        _id("rtt_"),
                        target["workspace_id"],
                        target["tenant_id"],
                        target["edge_deployment_id"],
                        rollout_id,
                        batch_id,
                        release_id,
                        _stable_bucket(
                            config, rollout_id, str(target["edge_deployment_id"])
                        ),
                        target["policy"]["policy_generation"],
                        desired,
                        head["current_version"],
                        release["version"],
                        now,
                        now,
                    ),
                )
        job_id, _ = enqueue_prepared_platform_job_in_transaction(
            connection,
            config,
            prepared=prepared,
            priority=95,
            source_code="fleet.rollout",
            idempotency_key=f"prd127:{rollout_id}:generation:1",
            request_id=request_id,
            trace_id=trace_id,
            scheduled_at=scheduled_at,
        )
        inserted_rollout = _rollout_row(connection, rollout_id)
        updated = connection.execute(
            """
            UPDATE edge_fleet_rollouts SET platform_job_id=?,revision=revision+1,
              updated_at=? WHERE rollout_id=? AND revision=?
            """,
            (
                job_id,
                _changed_stamp(now_value, inserted_rollout["updated_at"]),
                rollout_id,
                inserted_rollout["revision"],
            ),
        ).rowcount
        if updated != 1:
            raise ApiError("fleet.rollout_revision_conflict")
        _insert_action(
            connection,
            actor_domain="platform",
            actor_id=context.operator_id,
            action=action,
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            result_object_id=rollout_id,
            now=now,
        )
        for target in ordered:
            _event(
                connection,
                workspace_id=str(target["workspace_id"]),
                tenant_id=str(target["tenant_id"]),
                deployment_id=str(target["edge_deployment_id"]),
                rollout_id=rollout_id,
                target_id=None,
                rollback_id=None,
                event_type="fleet.rollout_started",
                reason=payload.reason_code,
                actor_domain="platform",
                actor_id=context.operator_id,
                request_id=request_id,
                trace_id=trace_id,
                now=now,
            )
        row = _rollout_row(connection, rollout_id)
        result = _rollout_response(connection, row, request_id, trace_id, replay=False)
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("fleet.rollout_conflict") from None
    except (DbError, sqlite3.Error, KeyError, TypeError, ValueError, ZoneInfoNotFoundError):
        _rollback(connection)
        raise ApiError("fleet.rollout_unavailable") from None
    finally:
        connection.close()


def mutate_platform_rollout(
    config: AppConfig,
    context: PlatformContext,
    rollout_id: str,
    payload: FleetRolloutMutationRequest,
    *,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> RolloutResponse:
    _require_platform(context, "platform.edge_rollout.manage")
    action = f"fleet.rollout.{payload.action}"
    request_sha256 = _digest(
        {"rollout_id": rollout_id, **payload.model_dump(mode="json")}
    )
    idempotency_sha256 = _platform_idempotency(
        config, context.operator_id, action, idempotency_key
    )
    prepared: PreparedPlatformJob | None = None
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        replay = _action_replay(
            connection, "platform", context.operator_id, action, idempotency_sha256
        )
        if replay is not None:
            if not hmac.compare_digest(str(replay["request_sha256"]), request_sha256):
                raise ApiError("fleet.rollout_idempotency_conflict")
            row = _rollout_row(connection, rollout_id)
            result = _rollout_response(connection, row, request_id, trace_id, replay=True)
            connection.commit()
            return result
        row = _rollout_row(connection, rollout_id)
        if int(row["revision"]) != payload.expected_revision:
            raise ApiError("fleet.rollout_revision_conflict")
        now_value = _utcnow()
        now = _changed_stamp(now_value, row["updated_at"])
        if payload.action == "pause":
            if str(row["status"]) not in {"preparing", "running"}:
                raise ApiError("fleet.rollout_state_conflict")
            _pause_rollout(
                connection,
                row,
                reason=payload.reason_code,
                actor_domain="platform",
                actor_id=context.operator_id,
                request_id=request_id,
                trace_id=trace_id,
                now=now_value,
            )
        else:
            if str(row["status"]) != "paused" or connection.execute(
                "SELECT 1 FROM edge_rollout_targets WHERE rollout_id=? AND status='failed'",
                (rollout_id,),
            ).fetchone():
                raise ApiError("fleet.rollout_state_conflict")
            generation = int(row["generation"]) + 1
            targets = connection.execute(
                "SELECT * FROM edge_rollout_targets WHERE rollout_id=? ORDER BY stable_bucket",
                (rollout_id,),
            ).fetchall()
            scheduled = _common_window_time(
                [_target_with_policy(connection, target) for target in targets], now_value
            )
            prepared = prepare_platform_job(
                config,
                type_code="fleet.rollout.advance",
                payload={"rollout_id": rollout_id, "expected_generation": generation},
            )
            job_id, _ = enqueue_prepared_platform_job_in_transaction(
                connection,
                config,
                prepared=prepared,
                priority=95,
                source_code="fleet.rollout",
                idempotency_key=f"prd127:{rollout_id}:generation:{generation}",
                request_id=request_id,
                trace_id=trace_id,
                scheduled_at=scheduled,
            )
            updated = connection.execute(
                """
                UPDATE edge_fleet_rollouts SET status='preparing',generation=?,
                  platform_job_id=?,pause_reason_code=NULL,paused_at=NULL,
                  revision=revision+1,updated_at=?
                WHERE rollout_id=? AND status='paused' AND revision=?
                """,
                (generation, job_id, now, rollout_id, payload.expected_revision),
            ).rowcount
            if updated != 1:
                raise ApiError("fleet.rollout_revision_conflict")
            for target in targets:
                head = _release_head(connection, str(target["edge_deployment_id"]))
                desired = max(
                    int(head["desired_generation"]), int(target["desired_generation"])
                ) + 1
                connection.execute(
                    """
                    UPDATE edge_rollout_targets SET rollout_generation=?,desired_generation=?,
                      status='selected',failure_reason_code=NULL,revision=revision+1,updated_at=?
                    WHERE rollout_target_id=? AND status NOT IN(
                      'succeeded','failed','cancelled','rolled_back')
                    """,
                    (
                        generation,
                        desired,
                        _changed_stamp(now_value, target["updated_at"]),
                        target["rollout_target_id"],
                    ),
                )
            connection.execute(
                """
                UPDATE edge_rollout_batches SET status='pending',revision=revision+1,
                  started_at=NULL,observe_until=NULL,completed_at=NULL,updated_at=?
                WHERE rollout_id=? AND status NOT IN('completed','failed','cancelled')
                """,
                (now, rollout_id),
            )
        _insert_action(
            connection,
            actor_domain="platform",
            actor_id=context.operator_id,
            action=action,
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            result_object_id=rollout_id,
            now=_db_time(now_value),
        )
        current = _rollout_row(connection, rollout_id)
        result = _rollout_response(connection, current, request_id, trace_id, replay=False)
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (DbError, sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("fleet.rollout_unavailable") from None
    finally:
        connection.close()


def get_fleet_compatibility(
    config: AppConfig,
    context: PlatformContext,
    *,
    release_id: str | None,
    deployment_id: str | None,
    request_id: str,
    trace_id: str,
) -> FleetCompatibilityResponse:
    if not {
        "platform.edge_release.read",
        "platform.edge_rollout.read",
    }.intersection(context.permissions):
        raise ApiError("platform.permission_denied")
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        release = _release_row(connection, release_id) if release_id else None
        parameters: tuple[object, ...] = ()
        query = "SELECT * FROM edge_deployments"
        if deployment_id is not None:
            query += " WHERE edge_deployment_id=?"
            parameters = (deployment_id,)
        query += " ORDER BY edge_deployment_id LIMIT 1000"
        deployments = connection.execute(query, parameters).fetchall()
        items = tuple(
            _compatibility_summary(connection, item, release) for item in deployments
        )
        result = FleetCompatibilityResponse(
            items=items,
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
        raise ApiError("fleet.compatibility_unavailable") from None
    finally:
        connection.close()


def get_workspace_upgrade(
    config: AppConfig,
    deployment_id: str,
    *,
    session_token: str,
    request_id: str,
    trace_id: str,
) -> WorkspaceUpgradeResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        context = _workspace_context(
            connection,
            config,
            session_token=session_token,
            csrf_token=None,
            permission="edge.upgrade.read",
        )
        _owned_deployment(connection, context, deployment_id)
        result = _workspace_response(
            connection, context, deployment_id, request_id, trace_id
        )
        connection.rollback()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (DbError, sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge_upgrade.unavailable") from None
    finally:
        connection.close()


def update_workspace_maintenance(
    config: AppConfig,
    deployment_id: str,
    payload: MaintenanceWindowUpdateRequest,
    *,
    session_token: str,
    csrf_token: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> WorkspaceUpgradeResponse:
    action = "edge.maintenance.update"
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = _workspace_context(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            permission="edge.upgrade.manage",
        )
        _owned_deployment(connection, context, deployment_id)
        request_sha256 = _digest(payload.model_dump(mode="json"))
        idempotency_sha256 = _workspace_idempotency(
            config, context, action, idempotency_key
        )
        replay = _action_replay(
            connection, "workspace", context.account_id, action, idempotency_sha256
        )
        if replay is not None:
            if not hmac.compare_digest(str(replay["request_sha256"]), request_sha256):
                raise ApiError("edge_upgrade.idempotency_conflict")
            result = _workspace_response(
                connection, context, deployment_id, request_id, trace_id
            )
            connection.commit()
            return result
        existing = connection.execute(
            "SELECT * FROM edge_maintenance_windows WHERE edge_deployment_id=?",
            (deployment_id,),
        ).fetchone()
        actual_revision = int(existing["revision"]) if existing is not None else 0
        if actual_revision != payload.expected_revision:
            raise ApiError("edge_upgrade.revision_conflict")
        try:
            ZoneInfo(payload.timezone_name)
        except ZoneInfoNotFoundError:
            raise ApiError("edge_upgrade.maintenance_invalid") from None
        now_value = _utcnow()
        now = _db_time(now_value)
        generation = int(existing["policy_generation"]) + 1 if existing else 1
        window_id = str(existing["maintenance_window_id"]) if existing else _id("mwn_")
        MaintenanceWindowContract(
            maintenance_window_id=window_id,
            policy_generation=generation,
            release_channel=payload.release_channel,
            timezone_name=payload.timezone_name,
            weekdays=payload.weekdays,
            start_minute=payload.start_minute,
            duration_minutes=payload.duration_minutes,
            freeze_starts_at=payload.freeze_starts_at,
            freeze_ends_at=payload.freeze_ends_at,
        )
        if existing is None:
            connection.execute(
                """
                INSERT INTO edge_maintenance_windows(
                  maintenance_window_id,workspace_id,tenant_id,edge_deployment_id,
                  release_channel,timezone_name,weekdays_json,start_minute,duration_minutes,
                  freeze_starts_at,freeze_ends_at,status,policy_generation,revision,
                  reason_code,updated_by_account_id,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,1,?,?,?,?)
                """,
                (
                    window_id,
                    context.workspace_id,
                    context.tenant_id,
                    deployment_id,
                    payload.release_channel,
                    payload.timezone_name,
                    _canonical(payload.weekdays),
                    payload.start_minute,
                    payload.duration_minutes,
                    _optional_time(payload.freeze_starts_at),
                    _optional_time(payload.freeze_ends_at),
                    payload.status,
                    payload.reason_code,
                    context.account_id,
                    now,
                    now,
                ),
            )
        else:
            updated = connection.execute(
                """
                UPDATE edge_maintenance_windows SET release_channel=?,timezone_name=?,
                  weekdays_json=?,start_minute=?,duration_minutes=?,freeze_starts_at=?,
                  freeze_ends_at=?,status=?,policy_generation=policy_generation+1,
                  revision=revision+1,reason_code=?,updated_by_account_id=?,updated_at=?
                WHERE edge_deployment_id=? AND revision=?
                """,
                (
                    payload.release_channel,
                    payload.timezone_name,
                    _canonical(payload.weekdays),
                    payload.start_minute,
                    payload.duration_minutes,
                    _optional_time(payload.freeze_starts_at),
                    _optional_time(payload.freeze_ends_at),
                    payload.status,
                    payload.reason_code,
                    context.account_id,
                    _changed_stamp(now_value, existing["updated_at"]),
                    deployment_id,
                    payload.expected_revision,
                ),
            ).rowcount
            if updated != 1:
                raise ApiError("edge_upgrade.revision_conflict")
        _insert_action(
            connection,
            actor_domain="workspace",
            actor_id=context.account_id,
            action=action,
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            result_object_id=window_id,
            now=now,
        )
        result = _workspace_response(
            connection, context, deployment_id, request_id, trace_id
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except UpgradeContractError:
        _rollback(connection)
        raise ApiError("edge_upgrade.maintenance_invalid") from None
    except (DbError, sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge_upgrade.unavailable") from None
    finally:
        connection.close()


def request_workspace_rollback(
    config: AppConfig,
    deployment_id: str,
    payload: RollbackCreateRequest,
    *,
    session_token: str,
    csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> RollbackResponse:
    action = "edge.rollback.request"
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = _workspace_context(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            permission="edge.rollback.manage",
        )
        _owned_deployment(connection, context, deployment_id)
        request_sha256 = _digest(payload.model_dump(mode="json"))
        idempotency_sha256 = _workspace_idempotency(
            config, context, action, idempotency_key
        )
        replay = _action_replay(
            connection, "workspace", context.account_id, action, idempotency_sha256
        )
        if replay is not None:
            if not hmac.compare_digest(str(replay["request_sha256"]), request_sha256):
                raise ApiError("edge_upgrade.idempotency_conflict")
            row = _rollback_row(connection, str(replay["result_object_id"]))
            result = RollbackResponse(
                rollback=_rollback_summary(row),
                idempotent_replay=True,
                request_id=_identifier(request_id),
                trace_id=_identifier(trace_id),
            )
            connection.commit()
            return result
        head = _release_head(connection, deployment_id)
        if int(head["revision"]) != payload.expected_head_revision:
            raise ApiError("edge_upgrade.revision_conflict")
        source_id = head["current_release_id"]
        target_id = payload.target_release_id or head["lkg_release_id"]
        if (
            source_id is None
            or target_id is None
            or target_id != head["lkg_release_id"]
            or target_id == source_id
        ):
            raise ApiError("edge_upgrade.rollback_unavailable")
        source = _release_row(connection, str(source_id))
        target = _release_row(connection, str(target_id))
        if (
            str(source["status"]) != "released"
            or str(target["status"]) != "released"
            or int(source["down_migration_supported"]) != 1
            or int(source["schema_to"]) < int(target["schema_to"])
        ):
            raise ApiError("edge_upgrade.rollback_unavailable")
        policy = _maintenance_row(connection, deployment_id)
        if policy is None or str(policy["status"]) != "active":
            raise ApiError("edge_upgrade.maintenance_closed")
        now_value = _utcnow()
        if str(policy["release_channel"]) != str(
            target["release_channel"]
        ) or not maintenance_window_open(_maintenance_contract(policy), now_value):
            raise ApiError("edge_upgrade.maintenance_closed")
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
        preflight = _target_preflight(connection, deployment_id, target, now_value)
        desired = int(head["desired_generation"]) + 1
        rollback_id = _id("rba_")
        command_id = _id("ucm_")
        now = _db_time(now_value)
        connection.execute(
            """
            INSERT INTO edge_rollback_attempts(
              rollback_attempt_id,workspace_id,tenant_id,edge_deployment_id,
              source_release_id,target_release_id,source_rollout_id,desired_generation,
              status,reason_code,requested_by_account_id,upgrade_command_id,
              upgrade_receipt_id,revision,requested_at,completed_at,updated_at)
            VALUES(?,?,?,?,?,?,NULL,?,'requested',?,?,NULL,NULL,1,?,NULL,?)
            """,
            (
                rollback_id,
                context.workspace_id,
                context.tenant_id,
                deployment_id,
                source_id,
                target_id,
                desired,
                payload.reason_code,
                context.account_id,
                now,
                now,
            ),
        )
        _issue_command(
            connection,
            config,
            command_id=command_id,
            workspace_id=context.workspace_id,
            tenant_id=context.tenant_id,
            deployment_id=deployment_id,
            rollout_id=None,
            target_id=None,
            rollback_id=rollback_id,
            release=target,
            command_generation=desired,
            rollout_generation=desired,
            cancel_watermark=0,
            policy=policy,
            expected_current_version=str(head["current_version"]),
            command_kind="rollback",
            preflight=preflight,
            database_schema_from=int(source["schema_to"]),
            database_schema_to=int(target["schema_to"]),
            down_migration_supported=True,
            now=now_value,
        )
        connection.execute(
            """
            UPDATE edge_rollback_attempts SET status='issued',upgrade_command_id=?,
              revision=revision+1,updated_at=? WHERE rollback_attempt_id=? AND status='requested'
            """,
            (command_id, _changed_stamp(now_value, now), rollback_id),
        )
        connection.execute(
            """
            UPDATE edge_deployment_release_heads SET desired_generation=?,
              revision=revision+1,updated_at=? WHERE edge_deployment_id=? AND revision=?
            """,
            (
                desired,
                _changed_stamp(now_value, head["updated_at"]),
                deployment_id,
                head["revision"],
            ),
        )
        _insert_action(
            connection,
            actor_domain="workspace",
            actor_id=context.account_id,
            action=action,
            idempotency_sha256=idempotency_sha256,
            request_sha256=request_sha256,
            result_object_id=rollback_id,
            now=now,
        )
        row = _rollback_row(connection, rollback_id)
        result = RollbackResponse(
            rollback=_rollback_summary(row),
            request_id=_identifier(request_id),
            trace_id=_identifier(trace_id),
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except UpgradeContractError:
        _rollback(connection)
        raise ApiError("edge_upgrade.integrity_failed") from None
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("edge_upgrade.state_conflict") from None
    except (DbError, sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge_upgrade.unavailable") from None
    finally:
        connection.close()


def pull_agent_upgrade(
    config: AppConfig,
    deployment_id: str,
    *,
    verified_fingerprint: str,
    edge_session_token: str,
    request_id: str,
    trace_id: str,
) -> AgentUpgradeCommandResponse | None:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        agent = _authenticated_agent(
            connection, config, deployment_id, verified_fingerprint, edge_session_token
        )
        _require_upgrade_capability(agent)
        now = _utcnow()
        expired_leases = connection.execute(
            """
            SELECT * FROM edge_upgrade_commands WHERE edge_deployment_id=?
              AND status='leased' AND lease_expires_at<=? AND deadline_at>?
            ORDER BY command_generation
            """,
            (deployment_id, _db_time(now), _db_time(now)),
        ).fetchall()
        for row in expired_leases:
            connection.execute(
                """
                UPDATE edge_upgrade_commands SET status='issued',lease_nonce_sha256=NULL,
                  lease_expires_at=NULL,revision=revision+1,updated_at=?
                WHERE upgrade_command_id=? AND status='leased' AND revision=?
                """,
                (
                    _changed_stamp(now, row["updated_at"]),
                    row["upgrade_command_id"],
                    row["revision"],
                ),
            )
            if row["rollout_target_id"] is not None:
                target = connection.execute(
                    "SELECT * FROM edge_rollout_targets WHERE rollout_target_id=?",
                    (row["rollout_target_id"],),
                ).fetchone()
                if target is not None and str(target["status"]) == "leased":
                    connection.execute(
                        """
                        UPDATE edge_rollout_targets SET status='selected',
                          revision=revision+1,updated_at=?
                        WHERE rollout_target_id=? AND status='leased' AND revision=?
                        """,
                        (
                            _changed_stamp(now, target["updated_at"]),
                            target["rollout_target_id"],
                            target["revision"],
                        ),
                    )
        expired = connection.execute(
            """
            SELECT * FROM edge_upgrade_commands WHERE edge_deployment_id=?
              AND status IN('issued','leased','running') AND deadline_at<=?
            ORDER BY command_generation
            """,
            (deployment_id, _db_time(now)),
        ).fetchall()
        for row in expired:
            if row["rollout_id"] is not None:
                rollout = _rollout_row(connection, str(row["rollout_id"]))
                if str(rollout["status"]) in {"preparing", "running"}:
                    _pause_rollout(
                        connection,
                        rollout,
                        reason="command_deadline",
                        actor_domain="system",
                        actor_id="fleet-rollout-worker",
                        request_id=str(rollout["request_id"]),
                        trace_id=str(rollout["trace_id"]),
                        now=now,
                    )
                    continue
            connection.execute(
                """
                UPDATE edge_upgrade_commands SET status='expired',lease_nonce_sha256=NULL,
                  lease_expires_at=NULL,completed_at=?,revision=revision+1,updated_at=?
                WHERE upgrade_command_id=? AND status IN('issued','leased','running')
                  AND revision=?
                """,
                (
                    _db_time(now),
                    _changed_stamp(now, row["updated_at"]),
                    row["upgrade_command_id"],
                    row["revision"],
                ),
            )
            if row["rollback_attempt_id"] is not None:
                rollback = _rollback_row(connection, str(row["rollback_attempt_id"]))
                if str(rollback["status"]) == "issued":
                    connection.execute(
                        """
                        UPDATE edge_rollback_attempts SET status='failed',completed_at=?,
                          revision=revision+1,updated_at=?
                        WHERE rollback_attempt_id=? AND status='issued' AND revision=?
                        """,
                        (
                            _db_time(now),
                            _changed_stamp(now, rollback["updated_at"]),
                            rollback["rollback_attempt_id"],
                            rollback["revision"],
                        ),
                    )
        row = connection.execute(
            """
            SELECT * FROM edge_upgrade_commands WHERE edge_deployment_id=? AND status='issued'
            ORDER BY command_generation,upgrade_command_id LIMIT 1
            """,
            (deployment_id,),
        ).fetchone()
        result = (
            _agent_command_response(connection, row, request_id, trace_id)
            if row is not None
            else None
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (DbError, sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge_upgrade.unavailable") from None
    finally:
        connection.close()


def mutate_agent_upgrade(
    config: AppConfig,
    deployment_id: str,
    command_id: str,
    payload: AgentUpgradeMutationRequest,
    *,
    verified_fingerprint: str,
    edge_session_token: str,
    request_id: str,
    trace_id: str,
) -> AgentUpgradeCommandResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        agent = _authenticated_agent(
            connection, config, deployment_id, verified_fingerprint, edge_session_token
        )
        _require_upgrade_capability(agent)
        row = _command_row(connection, deployment_id, command_id)
        if int(row["revision"]) != payload.expected_revision:
            raise ApiError("edge_upgrade.revision_conflict")
        now_value = _utcnow()
        if _datetime(row["deadline_at"]) <= now_value:
            raise ApiError("edge_upgrade.command_expired")
        _current_authority(connection, row, now_value)
        nonce_sha256 = _attempt_nonce_sha256(
            deployment_id, command_id, payload.lease_nonce
        )
        if payload.action == "lease":
            if str(row["status"]) != "issued":
                raise ApiError("edge_upgrade.state_conflict")
            updated = connection.execute(
                """
                UPDATE edge_upgrade_commands SET status='leased',lease_nonce_sha256=?,
                  lease_expires_at=?,revision=revision+1,updated_at=?
                WHERE upgrade_command_id=? AND status='issued' AND revision=?
                """,
                (
                    nonce_sha256,
                    _db_time(now_value + _LEASE_TTL),
                    _changed_stamp(now_value, row["updated_at"]),
                    command_id,
                    payload.expected_revision,
                ),
            ).rowcount
            target_status = "leased"
        else:
            if (
                str(row["status"]) != "leased"
                or row["lease_nonce_sha256"] is None
                or not hmac.compare_digest(str(row["lease_nonce_sha256"]), nonce_sha256)
                or _datetime(row["lease_expires_at"]) <= now_value
            ):
                raise ApiError("edge_upgrade.lease_invalid")
            updated = connection.execute(
                """
                UPDATE edge_upgrade_commands SET status='running',started_at=?,
                  revision=revision+1,updated_at=?
                WHERE upgrade_command_id=? AND status='leased' AND revision=?
                """,
                (
                    _db_time(now_value),
                    _changed_stamp(now_value, row["updated_at"]),
                    command_id,
                    payload.expected_revision,
                ),
            ).rowcount
            target_status = "draining"
        if updated != 1:
            raise ApiError("edge_upgrade.revision_conflict")
        if row["rollout_target_id"] is not None:
            target = connection.execute(
                "SELECT * FROM edge_rollout_targets WHERE rollout_target_id=?",
                (row["rollout_target_id"],),
            ).fetchone()
            connection.execute(
                """
                UPDATE edge_rollout_targets SET status=?,started_at=coalesce(started_at,?),
                  revision=revision+1,updated_at=? WHERE rollout_target_id=?
                  AND status NOT IN('succeeded','failed','cancelled','rolled_back')
                """,
                (
                    target_status,
                    _db_time(now_value),
                    _changed_stamp(now_value, target["updated_at"]),
                    row["rollout_target_id"],
                ),
            )
        current = _command_row(connection, deployment_id, command_id)
        result = _agent_command_response(connection, current, request_id, trace_id)
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (DbError, sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge_upgrade.unavailable") from None
    finally:
        connection.close()


def record_agent_upgrade_report(
    config: AppConfig,
    deployment_id: str,
    payload: AgentUpgradeReportRequest,
    *,
    verified_fingerprint: str,
    edge_session_token: str,
    request_id: str,
    trace_id: str,
) -> AgentUpgradeReceiptResponse:
    report = payload.report
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        agent = _authenticated_agent(
            connection, config, deployment_id, verified_fingerprint, edge_session_token
        )
        _require_upgrade_capability(agent)
        row = _command_row(connection, deployment_id, report.upgrade_command_id)
        existing = connection.execute(
            "SELECT * FROM edge_upgrade_receipts WHERE upgrade_command_id=?",
            (report.upgrade_command_id,),
        ).fetchone()
        report_sha256 = upgrade_report_sha256(report)
        if existing is not None:
            if not hmac.compare_digest(str(existing["report_sha256"]), report_sha256):
                raise ApiError("edge_upgrade.receipt_conflict")
            result = AgentUpgradeReceiptResponse(
                deployment_id=deployment_id,
                upgrade_command_id=report.upgrade_command_id,
                outcome=report.outcome,
                idempotent_replay=True,
                request_id=_identifier(request_id),
                trace_id=_identifier(trace_id),
            )
            connection.commit()
            return result
        if (
            str(row["status"]) != "running"
            or report.deployment_id != deployment_id
            or report.command_generation != int(row["command_generation"])
            or report.command_kind != str(row["command_kind"])
            or not hmac.compare_digest(str(row["lease_nonce_sha256"]), payload.attempt_nonce)
        ):
            raise ApiError("edge_upgrade.receipt_invalid")
        envelope = _command_envelope(row)
        if (
            report.command_sha256 != envelope.command_sha256
            or report.release_manifest_sha256
            != envelope.command.release.manifest_sha256
            or report.from_version != str(row["expected_current_version"])
            or report.target_version != str(row["target_version"])
            or report.total_duration_ms > 600_000
        ):
            raise ApiError("edge_upgrade.receipt_invalid")
        now_value = _utcnow()
        now = _db_time(now_value)
        receipt_id = _id("urc_")
        connection.execute(
            """
            INSERT INTO edge_upgrade_receipts(
              upgrade_receipt_id,workspace_id,tenant_id,edge_deployment_id,
              upgrade_command_id,command_generation,outcome,reason_code,from_version,
              target_version,drain_duration_ms,total_duration_ms,report_json,report_sha256,
              attempt_nonce_sha256,request_id,trace_id,completed_at,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                receipt_id,
                row["workspace_id"],
                row["tenant_id"],
                deployment_id,
                report.upgrade_command_id,
                report.command_generation,
                report.outcome,
                report.reason_code,
                report.from_version,
                report.target_version,
                report.drain_duration_ms,
                report.total_duration_ms,
                _canonical(report.model_dump(mode="json")),
                report_sha256,
                payload.attempt_nonce,
                _identifier(request_id),
                _identifier(trace_id),
                _db_time(report.completed_at),
                now,
            ),
        )
        connection.execute(
            """
            UPDATE edge_upgrade_commands SET status=?,completed_at=?,revision=revision+1,
              updated_at=? WHERE upgrade_command_id=? AND status='running' AND revision=?
            """,
            (
                "completed" if report.outcome == "succeeded" else report.outcome,
                _db_time(report.completed_at),
                _changed_stamp(now_value, row["updated_at"]),
                report.upgrade_command_id,
                row["revision"],
            ),
        )
        if str(row["command_kind"]) == "upgrade":
            _record_rollout_target_report(
                connection,
                config,
                row,
                report,
                receipt_id=receipt_id,
                request_id=request_id,
                trace_id=trace_id,
                now=now_value,
            )
        else:
            _record_rollback_report(
                connection,
                row,
                report,
                receipt_id=receipt_id,
                request_id=request_id,
                trace_id=trace_id,
                now=now_value,
            )
        result = AgentUpgradeReceiptResponse(
            deployment_id=deployment_id,
            upgrade_command_id=report.upgrade_command_id,
            outcome=report.outcome,
            request_id=_identifier(request_id),
            trace_id=_identifier(trace_id),
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except UpgradeContractError:
        _rollback(connection)
        raise ApiError("edge_upgrade.receipt_invalid") from None
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("edge_upgrade.receipt_conflict") from None
    except (DbError, sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("edge_upgrade.unavailable") from None
    finally:
        connection.close()


def _rollout_job_handler(
    context: PlatformJobContext, payload: FleetRolloutJobInput
) -> str:
    context.checkpoint(
        {
            "rollout_id": payload.rollout_id,
            "generation": payload.expected_generation,
            "phase": "preflight",
        }
    )
    outcome: dict[str, str] = {}

    def apply(connection: sqlite3.Connection) -> None:
        row = _rollout_row(connection, payload.rollout_id)
        if (
            str(row["platform_job_id"]) != context.lease.job_id
            or int(row["generation"]) != payload.expected_generation
        ):
            raise PlatformJobFailure("fleet.rollout_stale")
        outcome["code"] = _advance_rollout(
            connection, context.config, row, now=_utcnow()
        )

    context.commit_effect(
        "fleet.rollout.advance",
        {
            "rollout_id": payload.rollout_id,
            "generation": payload.expected_generation,
        },
        apply,
    )
    code = outcome.get("code", "fleet.rollout_stale")
    context.checkpoint(
        {
            "rollout_id": payload.rollout_id,
            "generation": payload.expected_generation,
            "phase": "complete",
            "outcome": code,
        }
    )
    return code


def _advance_rollout(
    connection: sqlite3.Connection,
    config: AppConfig,
    rollout: sqlite3.Row,
    *,
    now: datetime,
) -> str:
    status = str(rollout["status"])
    if status == "paused":
        return "fleet.rollout_paused"
    if status in {"completed", "failed", "cancelled"}:
        return f"fleet.rollout_{status}"
    batch = connection.execute(
        "SELECT * FROM edge_rollout_batches WHERE rollout_id=? AND sequence=?",
        (rollout["rollout_id"], rollout["current_batch_sequence"]),
    ).fetchone()
    if batch is None:
        raise PlatformJobFailure("fleet.rollout_stale")
    if str(batch["status"]) == "observing":
        return _observe_batch(connection, config, rollout, batch, now=now)
    if str(batch["status"]) != "pending":
        raise PlatformJobFailure("fleet.rollout_stale")
    release = _release_row(connection, str(rollout["edge_gateway_release_id"]))
    targets = connection.execute(
        "SELECT * FROM edge_rollout_targets WHERE rollout_batch_id=? AND status='selected' "
        "ORDER BY stable_bucket,edge_deployment_id",
        (batch["rollout_batch_id"],),
    ).fetchall()
    if not targets:
        raise PlatformJobFailure("fleet.rollout_stale")
    try:
        for target in targets:
            policy = _maintenance_row(connection, str(target["edge_deployment_id"]))
            if (
                policy is None
                or int(policy["policy_generation"])
                != int(target["maintenance_policy_generation"])
                or str(policy["status"]) != "active"
                or str(policy["release_channel"]) != str(release["release_channel"])
                or not maintenance_window_open(_maintenance_contract(policy), now)
            ):
                raise ApiError("edge_upgrade.maintenance_closed")
            preflight = _target_preflight(
                connection, str(target["edge_deployment_id"]), release, now
            )
            _issue_target_command(
                connection, config, rollout, target, release, policy, preflight, now=now
            )
    except (ApiError, UpgradeContractError) as exc:
        reason = (
            exc.code if isinstance(exc, ApiError) else str(exc)
        ).replace("edge_upgrade.", "")
        _pause_rollout(
            connection,
            rollout,
            reason=reason,
            actor_domain="system",
            actor_id="fleet-rollout-worker",
            request_id=str(rollout["request_id"]),
            trace_id=str(rollout["trace_id"]),
            now=now,
        )
        return "fleet.rollout_paused"
    stamp = _db_time(now)
    connection.execute(
        """
        UPDATE edge_rollout_batches SET status='running',started_at=?,
          revision=revision+1,updated_at=? WHERE rollout_batch_id=? AND status='pending'
        """,
        (stamp, _changed_stamp(now, batch["updated_at"]), batch["rollout_batch_id"]),
    )
    connection.execute(
        """
        UPDATE edge_fleet_rollouts SET status='running',started_at=coalesce(started_at,?),
          revision=revision+1,updated_at=? WHERE rollout_id=? AND revision=?
        """,
        (
            stamp,
            _changed_stamp(now, rollout["updated_at"]),
            rollout["rollout_id"],
            rollout["revision"],
        ),
    )
    return "fleet.rollout_started"


def _observe_batch(
    connection: sqlite3.Connection,
    config: AppConfig,
    rollout: sqlite3.Row,
    batch: sqlite3.Row,
    *,
    now: datetime,
) -> str:
    if batch["observe_until"] is None or _datetime(batch["observe_until"]) > now:
        raise PlatformJobFailure("fleet.rollout_stale")
    targets = connection.execute(
        "SELECT * FROM edge_rollout_targets WHERE rollout_batch_id=?",
        (batch["rollout_batch_id"],),
    ).fetchall()
    failure_bps = int(batch["failed_count"]) * 10_000 // int(batch["target_count"])
    signals_current = all(
        _fresh_healthy_signal(
            connection,
            str(target["edge_deployment_id"]),
            batch["started_at"],
            now,
        )
        for target in targets
        if str(target["status"]) == "succeeded"
    )
    if failure_bps >= int(rollout["failure_threshold_basis_points"]) or not signals_current:
        _pause_rollout(
            connection,
            rollout,
            reason="failure_threshold" if failure_bps else "health_signal_missing",
            actor_domain="system",
            actor_id="fleet-rollout-worker",
            request_id=str(rollout["request_id"]),
            trace_id=str(rollout["trace_id"]),
            now=now,
        )
        return "fleet.rollout_paused"
    stamp = _db_time(now)
    connection.execute(
        """
        UPDATE edge_rollout_batches SET status='completed',completed_at=?,
          revision=revision+1,updated_at=? WHERE rollout_batch_id=? AND status='observing'
        """,
        (stamp, _changed_stamp(now, batch["updated_at"]), batch["rollout_batch_id"]),
    )
    next_batch = connection.execute(
        "SELECT * FROM edge_rollout_batches WHERE rollout_id=? AND sequence=?",
        (rollout["rollout_id"], int(batch["sequence"]) + 1),
    ).fetchone()
    if next_batch is None:
        connection.execute(
            """
            UPDATE edge_fleet_rollouts SET status='completed',completed_at=?,
              revision=revision+1,updated_at=? WHERE rollout_id=? AND revision=?
            """,
            (
                stamp,
                _changed_stamp(now, rollout["updated_at"]),
                rollout["rollout_id"],
                rollout["revision"],
            ),
        )
        return "fleet.rollout_completed"
    connection.execute(
        """
        UPDATE edge_fleet_rollouts SET current_batch_sequence=?,revision=revision+1,
          updated_at=? WHERE rollout_id=? AND revision=?
        """,
        (
            next_batch["sequence"],
            _changed_stamp(now, rollout["updated_at"]),
            rollout["rollout_id"],
            rollout["revision"],
        ),
    )
    current = _rollout_row(connection, str(rollout["rollout_id"]))
    return _advance_rollout(connection, config, current, now=now)


def _record_rollout_target_report(
    connection: sqlite3.Connection,
    config: AppConfig,
    command: sqlite3.Row,
    report: UpgradeExecutionReport,
    *,
    receipt_id: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    del receipt_id
    target = connection.execute(
        "SELECT * FROM edge_rollout_targets WHERE rollout_target_id=?",
        (command["rollout_target_id"],),
    ).fetchone()
    if target is None or str(target["status"]) in {
        "succeeded",
        "failed",
        "cancelled",
        "rolled_back",
    }:
        raise ApiError("edge_upgrade.receipt_invalid")
    target_status = (
        "succeeded"
        if report.outcome == "succeeded"
        else "cancelled"
        if report.outcome == "cancelled"
        else "failed"
    )
    connection.execute(
        """
        UPDATE edge_rollout_targets SET status=?,failure_reason_code=?,completed_at=?,
          revision=revision+1,updated_at=? WHERE rollout_target_id=? AND revision=?
        """,
        (
            target_status,
            None if target_status == "succeeded" else report.reason_code,
            _db_time(report.completed_at),
            _changed_stamp(now, target["updated_at"]),
            target["rollout_target_id"],
            target["revision"],
        ),
    )
    if report.outcome == "succeeded":
        _apply_release_head(
            connection,
            deployment_id=str(command["edge_deployment_id"]),
            release_id=str(command["edge_gateway_release_id"]),
            version=report.applied_version,
            generation=int(command["command_generation"]),
            rollback=False,
            now=now,
        )
    batch = connection.execute(
        "SELECT * FROM edge_rollout_batches WHERE rollout_batch_id=?",
        (target["rollout_batch_id"],),
    ).fetchone()
    counts = connection.execute(
        """
        SELECT sum(status='succeeded') succeeded,sum(status IN('failed','cancelled')) failed,
          sum(status IN('succeeded','failed','cancelled')) terminal
        FROM edge_rollout_targets WHERE rollout_batch_id=?
        """,
        (target["rollout_batch_id"],),
    ).fetchone()
    connection.execute(
        """
        UPDATE edge_rollout_batches SET succeeded_count=?,failed_count=?,
          revision=revision+1,updated_at=? WHERE rollout_batch_id=? AND revision=?
        """,
        (
            counts["succeeded"] or 0,
            counts["failed"] or 0,
            _changed_stamp(now, batch["updated_at"]),
            batch["rollout_batch_id"],
            batch["revision"],
        ),
    )
    rollout = _rollout_row(connection, str(target["rollout_id"]))
    total = connection.execute(
        """
        SELECT sum(status='succeeded') succeeded,sum(status IN('failed','cancelled')) failed
        FROM edge_rollout_targets WHERE rollout_id=?
        """,
        (target["rollout_id"],),
    ).fetchone()
    changed = _changed_stamp(now, rollout["updated_at"])
    connection.execute(
        """
        UPDATE edge_fleet_rollouts SET succeeded_count=?,failed_count=?,
          revision=revision+1,updated_at=? WHERE rollout_id=? AND revision=?
        """,
        (
            total["succeeded"] or 0,
            total["failed"] or 0,
            changed,
            rollout["rollout_id"],
            rollout["revision"],
        ),
    )
    rollout = _rollout_row(connection, str(target["rollout_id"]))
    failure_bps = int(total["failed"] or 0) * 10_000 // int(rollout["target_count"])
    if report.outcome != "succeeded" and failure_bps >= int(
        rollout["failure_threshold_basis_points"]
    ):
        _pause_rollout(
            connection,
            rollout,
            reason="failure_threshold",
            actor_domain="system",
            actor_id="fleet-rollout-worker",
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        return
    if int(counts["terminal"] or 0) == int(batch["target_count"]):
        current_batch = connection.execute(
            "SELECT * FROM edge_rollout_batches WHERE rollout_batch_id=?",
            (batch["rollout_batch_id"],),
        ).fetchone()
        observe_until = now + timedelta(seconds=int(rollout["observation_seconds"]))
        connection.execute(
            """
            UPDATE edge_rollout_batches SET status='observing',observe_until=?,
              revision=revision+1,updated_at=? WHERE rollout_batch_id=? AND status='running'
            """,
            (
                _db_time(observe_until),
                _changed_stamp(now, current_batch["updated_at"]),
                batch["rollout_batch_id"],
            ),
        )
        prepared = prepare_platform_job(
            config,
            type_code="fleet.rollout.advance",
            payload={
                "rollout_id": str(rollout["rollout_id"]),
                "expected_generation": int(rollout["generation"]),
            },
        )
        job_id, _ = enqueue_prepared_platform_job_in_transaction(
            connection,
            config,
            prepared=prepared,
            priority=95,
            source_code="fleet.rollout",
            idempotency_key=(
                f"prd127:{rollout['rollout_id']}:generation:{rollout['generation']}:"
                f"batch:{batch['sequence']}:observe"
            ),
            request_id=request_id,
            trace_id=trace_id,
            scheduled_at=observe_until,
        )
        current_rollout = _rollout_row(connection, str(rollout["rollout_id"]))
        connection.execute(
            """
            UPDATE edge_fleet_rollouts SET platform_job_id=?,revision=revision+1,
              updated_at=? WHERE rollout_id=? AND revision=?
            """,
            (
                job_id,
                _changed_stamp(now, current_rollout["updated_at"]),
                rollout["rollout_id"],
                current_rollout["revision"],
            ),
        )


def _record_rollback_report(
    connection: sqlite3.Connection,
    command: sqlite3.Row,
    report: UpgradeExecutionReport,
    *,
    receipt_id: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    row = _rollback_row(connection, str(command["rollback_attempt_id"]))
    status = "completed" if report.outcome == "succeeded" else "failed"
    connection.execute(
        """
        UPDATE edge_rollback_attempts SET status=?,upgrade_receipt_id=?,completed_at=?,
          revision=revision+1,updated_at=? WHERE rollback_attempt_id=? AND status='issued'
        """,
        (
            status,
            receipt_id,
            _db_time(report.completed_at),
            _changed_stamp(now, row["updated_at"]),
            row["rollback_attempt_id"],
        ),
    )
    if report.outcome == "succeeded":
        _apply_release_head(
            connection,
            deployment_id=str(command["edge_deployment_id"]),
            release_id=str(command["edge_gateway_release_id"]),
            version=report.applied_version,
            generation=int(command["command_generation"]),
            rollback=True,
            now=now,
        )
        _event(
            connection,
            workspace_id=str(command["workspace_id"]),
            tenant_id=str(command["tenant_id"]),
            deployment_id=str(command["edge_deployment_id"]),
            rollout_id=None,
            target_id=None,
            rollback_id=str(command["rollback_attempt_id"]),
            event_type="fleet.rollback_completed",
            reason="rollback_applied",
            actor_domain="agent",
            actor_id=str(command["edge_deployment_id"]),
            request_id=request_id,
            trace_id=trace_id,
            now=_db_time(now),
        )


def _submit_release(
    connection: sqlite3.Connection,
    config: AppConfig,
    context: PlatformContext,
    payload: PlatformGatewayReleaseMutationRequest,
    *,
    release_id: str,
    now: str,
) -> sqlite3.Row:
    installer = connection.execute(
        "SELECT * FROM installer_releases WHERE installer_release_id=?",
        (payload.installer_release_id,),
    ).fetchone()
    if installer is None or str(installer["status"]) != "released":
        raise ApiError("fleet.release_installer_unavailable")
    verified, artifacts = _verified_release_row(
        connection, installer, active_keys_only=True
    )
    source = verified.release.manifest
    gateway_artifacts = [
        GatewayReleaseArtifact(
            artifact_id=_id("gar_"),
            installer_artifact_id=str(item["installer_artifact_id"]),
            kind=str(item["kind"]),
            filename=str(item["filename"]),
            media_type=str(item["media_type"]),
            sha256=str(item["content_sha256"]),
            sbom_sha256=str(item["sbom_sha256"]),
            size_bytes=int(item["size_bytes"]),
        )
        for item in artifacts
    ]
    artifact_set_sha256 = hashlib.sha256(
        canonical_edge_json_bytes(
            [item.model_dump(mode="json") for item in gateway_artifacts]
        )
    ).hexdigest()
    manifest = GatewayReleaseManifest(
        release_id=release_id,
        installer_release_id=str(payload.installer_release_id),
        version=source.version,
        release_channel=payload.release_channel,
        artifact_set_sha256=artifact_set_sha256,
        sbom_sha256=str(installer["sbom_sha256"]),
        schema_from=payload.schema_from,
        schema_to=payload.schema_to,
        down_migration_supported=payload.down_migration_supported,
        compatible_from_versions=payload.compatible_from_versions,
        agent_min_version=source.agent_min_version,
        agent_max_version=source.agent_max_version,
        control_min_version=source.control_min_version,
        control_max_version=source.control_max_version,
        artifacts=gateway_artifacts,
    )
    private_key, key_id = _signing_key(config, "release")
    envelope = sign_gateway_release(manifest, private_key=private_key, key_id=key_id)
    signature = envelope.signature
    connection.execute(
        """
        INSERT INTO edge_gateway_releases(
          edge_gateway_release_id,installer_release_id,version,release_channel,status,
          manifest_json,manifest_sha256,artifact_set_sha256,sbom_sha256,
          signature_algorithm,signature_key_id,signing_public_key_b64,signature_value,
          signature_purpose,schema_from,schema_to,down_migration_supported,backup_required,
          compatible_from_versions_json,agent_min_version,agent_max_version,
          control_min_version,control_max_version,revision,submitted_by_operator_id,
          approved_by_operator_id,submitted_at,approved_at,withdrawn_at)
        VALUES(?,?,?,?,'approval_pending',?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,1,?,NULL,?,NULL,NULL)
        """,
        (
            release_id,
            payload.installer_release_id,
            source.version,
            payload.release_channel,
            _canonical(manifest.model_dump(mode="json")),
            envelope.manifest_sha256,
            artifact_set_sha256,
            installer["sbom_sha256"],
            signature.algorithm,
            signature.key_id,
            signature.public_key,
            signature.value,
            manifest.signature_purpose,
            payload.schema_from,
            payload.schema_to,
            int(payload.down_migration_supported),
            _canonical(payload.compatible_from_versions),
            source.agent_min_version,
            source.agent_max_version,
            source.control_min_version,
            source.control_max_version,
            context.operator_id,
            now,
        ),
    )
    for artifact in gateway_artifacts:
        connection.execute(
            """
            INSERT INTO edge_gateway_release_artifacts(
              edge_gateway_artifact_id,edge_gateway_release_id,installer_artifact_id,
              kind,filename,media_type,content_sha256,sbom_sha256,size_bytes,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                artifact.artifact_id,
                release_id,
                artifact.installer_artifact_id,
                artifact.kind,
                artifact.filename,
                artifact.media_type,
                artifact.sha256,
                artifact.sbom_sha256,
                artifact.size_bytes,
                now,
            ),
        )
    return _release_row(connection, release_id)


def _eligible_rollout_targets(
    connection: sqlite3.Connection,
    release: sqlite3.Row,
    deployment_ids: tuple[str, ...],
) -> list[dict[str, object]]:
    if len(deployment_ids) != len(set(deployment_ids)):
        raise ApiError("fleet.rollout_request_invalid")
    result: list[dict[str, object]] = []
    for deployment_id in deployment_ids:
        deployment = connection.execute(
            "SELECT * FROM edge_deployments WHERE edge_deployment_id=?",
            (deployment_id,),
        ).fetchone()
        head = _release_head(connection, deployment_id) if deployment is not None else None
        policy = _maintenance_row(connection, deployment_id) if deployment is not None else None
        if (
            deployment is None
            or str(deployment["status"]) != "active"
            or str(deployment["mode"]) != "connected"
            or head is None
            or policy is None
            or str(policy["status"]) != "active"
            or str(policy["release_channel"]) != str(release["release_channel"])
            or str(head["current_version"])
            not in json.loads(str(release["compatible_from_versions_json"]))
        ):
            raise ApiError("fleet.rollout_target_ineligible")
        current_release = connection.execute(
            "SELECT * FROM edge_gateway_releases WHERE version=? AND status='released'",
            (head["current_version"],),
        ).fetchone()
        if head["current_release_id"] is None and current_release is None:
            raise ApiError("fleet.rollout_n_minus_one_missing")
        if head["current_release_id"] is None:
            connection.execute(
                """
                UPDATE edge_deployment_release_heads SET current_release_id=?,lkg_release_id=?,
                  revision=revision+1,updated_at=? WHERE edge_deployment_id=? AND revision=?
                """,
                (
                    current_release["edge_gateway_release_id"],
                    current_release["edge_gateway_release_id"],
                    _changed_stamp(_utcnow(), head["updated_at"]),
                    deployment_id,
                    head["revision"],
                ),
            )
            head = _release_head(connection, deployment_id)
        result.append(
            {
                **dict(deployment),
                "head": head,
                "policy": policy,
            }
        )
    return result


def _issue_target_command(
    connection: sqlite3.Connection,
    config: AppConfig,
    rollout: sqlite3.Row,
    target: sqlite3.Row,
    release: sqlite3.Row,
    policy: sqlite3.Row,
    preflight: dict[str, object],
    *,
    now: datetime,
) -> None:
    head = _release_head(connection, str(target["edge_deployment_id"]))
    generation = max(
        int(target["desired_generation"]), int(head["desired_generation"]) + 1
    )
    if generation != int(target["desired_generation"]):
        connection.execute(
            """
            UPDATE edge_rollout_targets SET desired_generation=?,revision=revision+1,
              updated_at=? WHERE rollout_target_id=? AND revision=?
            """,
            (
                generation,
                _changed_stamp(now, target["updated_at"]),
                target["rollout_target_id"],
                target["revision"],
            ),
        )
    _issue_command(
        connection,
        config,
        command_id=_id("ucm_"),
        workspace_id=str(target["workspace_id"]),
        tenant_id=str(target["tenant_id"]),
        deployment_id=str(target["edge_deployment_id"]),
        rollout_id=str(rollout["rollout_id"]),
        target_id=str(target["rollout_target_id"]),
        rollback_id=None,
        release=release,
        command_generation=generation,
        rollout_generation=int(rollout["generation"]),
        cancel_watermark=int(rollout["cancel_watermark"]),
        policy=policy,
        expected_current_version=str(head["current_version"]),
        command_kind="upgrade",
        preflight=preflight,
        database_schema_from=int(release["schema_from"]),
        database_schema_to=int(release["schema_to"]),
        down_migration_supported=bool(release["down_migration_supported"]),
        now=now,
    )
    connection.execute(
        """
        UPDATE edge_deployment_release_heads SET desired_generation=?,revision=revision+1,
          updated_at=? WHERE edge_deployment_id=? AND revision=?
        """,
        (
            generation,
            _changed_stamp(now, head["updated_at"]),
            target["edge_deployment_id"],
            head["revision"],
        ),
    )


def _issue_command(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    command_id: str,
    workspace_id: str,
    tenant_id: str,
    deployment_id: str,
    rollout_id: str | None,
    target_id: str | None,
    rollback_id: str | None,
    release: sqlite3.Row,
    command_generation: int,
    rollout_generation: int,
    cancel_watermark: int,
    policy: sqlite3.Row,
    expected_current_version: str,
    command_kind: str,
    preflight: dict[str, object],
    database_schema_from: int,
    database_schema_to: int,
    down_migration_supported: bool,
    now: datetime,
) -> None:
    release_envelope = _release_envelope(connection, release)
    command = UnsignedUpgradeCommand(
        upgrade_command_id=command_id,
        deployment_id=deployment_id,
        rollout_id=rollout_id,
        rollout_target_id=target_id,
        rollback_attempt_id=rollback_id,
        command_kind=command_kind,
        command_generation=command_generation,
        rollout_generation=rollout_generation,
        cancel_watermark=cancel_watermark,
        expected_current_version=expected_current_version,
        target_version=str(release["version"]),
        release=release_envelope,
        maintenance=_maintenance_contract(policy),
        preflight=UpgradePreflightEvidence(
            health_snapshot_generation=int(preflight["health_snapshot_generation"]),
            backup_evidence_sha256=str(preflight["backup_evidence_sha256"]),
            database_schema_from=database_schema_from,
            database_schema_to=database_schema_to,
            down_migration_supported=down_migration_supported,
            checked_at=now,
        ),
        drain_timeout_ms=300_000,
        overall_timeout_ms=600_000,
        issued_at=now,
        deadline_at=now + _COMMAND_TTL,
    )
    private_key, key_id = _signing_key(config, "command")
    envelope = sign_upgrade_command(command, private_key=private_key, key_id=key_id)
    wire = _canonical(envelope.model_dump(mode="json"))
    connection.execute(
        """
        INSERT INTO edge_upgrade_commands(
          upgrade_command_id,workspace_id,tenant_id,edge_deployment_id,
          rollout_target_id,rollout_id,rollback_attempt_id,edge_gateway_release_id,
          command_generation,rollout_generation,cancel_watermark,
          maintenance_policy_generation,command_kind,status,expected_current_version,
          target_version,envelope_json,envelope_sha256,lease_nonce_sha256,
          lease_expires_at,deadline_at,revision,issued_at,started_at,completed_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'issued',?,?,?,?,NULL,NULL,?,1,?,NULL,NULL,?)
        """,
        (
            command_id,
            workspace_id,
            tenant_id,
            deployment_id,
            target_id,
            rollout_id,
            rollback_id,
            release["edge_gateway_release_id"],
            command_generation,
            rollout_generation,
            cancel_watermark,
            policy["policy_generation"],
            command_kind,
            expected_current_version,
            release["version"],
            wire,
            envelope.command_sha256,
            _db_time(now + _COMMAND_TTL),
            _db_time(now),
            _db_time(now),
        ),
    )


def _target_preflight(
    connection: sqlite3.Connection,
    deployment_id: str,
    release: sqlite3.Row,
    now: datetime,
) -> dict[str, object]:
    head = _release_head(connection, deployment_id)
    compatible = json.loads(str(release["compatible_from_versions_json"]))
    if str(head["current_version"]) not in compatible and str(head["lkg_version"]) != str(
        release["version"]
    ):
        raise ApiError("edge_upgrade.version_incompatible")
    index = connection.execute(
        "SELECT * FROM edge_fleet_deployment_index WHERE edge_deployment_id=?",
        (deployment_id,),
    ).fetchone()
    if (
        index is None
        or str(index["health_code"]) != "healthy"
        or int(index["health_score"]) < 80
        or _datetime(index["updated_at"]) < now - _HEALTH_MAX_AGE
    ):
        raise ApiError("edge_upgrade.health_unavailable")
    backup = connection.execute(
        """
        SELECT check_record.* FROM edge_validation_checks check_record
        JOIN edge_validation_runs run ON run.validation_run_id=check_record.validation_run_id
        WHERE check_record.edge_deployment_id=? AND check_record.check_code='backup_readiness'
          AND check_record.outcome='passed' AND run.status='completed' AND run.outcome='passed'
        ORDER BY check_record.created_at DESC LIMIT 1
        """,
        (deployment_id,),
    ).fetchone()
    if backup is None:
        raise ApiError("edge_upgrade.backup_unavailable")
    return {
        "health_snapshot_generation": int(index["source_health_generation"]),
        "backup_evidence_sha256": str(backup["evidence_sha256"]),
    }


def _fresh_healthy_signal(
    connection: sqlite3.Connection,
    deployment_id: str,
    started_at: object,
    now: datetime,
) -> bool:
    row = connection.execute(
        "SELECT * FROM edge_fleet_deployment_index WHERE edge_deployment_id=?",
        (deployment_id,),
    ).fetchone()
    return bool(
        row is not None
        and str(row["health_code"]) == "healthy"
        and int(row["health_score"]) >= 80
        and _datetime(row["updated_at"]) >= _datetime(started_at)
        and _datetime(row["updated_at"]) >= now - _HEALTH_MAX_AGE
    )


def _pause_rollout(
    connection: sqlite3.Connection,
    rollout: sqlite3.Row,
    *,
    reason: str,
    actor_domain: str,
    actor_id: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    safe_reason = _safe_reason(reason)
    stamp = _db_time(now)
    changed = _changed_stamp(now, rollout["updated_at"])
    updated = connection.execute(
        """
        UPDATE edge_fleet_rollouts SET status='paused',generation=generation+1,
          cancel_watermark=cancel_watermark+1,pause_reason_code=?,paused_at=?,
          revision=revision+1,updated_at=? WHERE rollout_id=? AND revision=?
          AND status IN('preparing','running')
        """,
        (safe_reason, stamp, changed, rollout["rollout_id"], rollout["revision"]),
    ).rowcount
    if updated != 1:
        raise ApiError("fleet.rollout_revision_conflict")
    current = _rollout_row(connection, str(rollout["rollout_id"]))
    commands = connection.execute(
        "SELECT * FROM edge_upgrade_commands WHERE rollout_id=? "
        "AND status IN('issued','leased','running')",
        (rollout["rollout_id"],),
    ).fetchall()
    for command in commands:
        connection.execute(
            """
            UPDATE edge_upgrade_commands SET status='cancelled',lease_nonce_sha256=NULL,
              lease_expires_at=NULL,completed_at=?,revision=revision+1,updated_at=?
            WHERE upgrade_command_id=? AND revision=?
            """,
            (
                stamp,
                _changed_stamp(now, command["updated_at"]),
                command["upgrade_command_id"],
                command["revision"],
            ),
        )
        if command["rollout_target_id"] is not None:
            target = connection.execute(
                "SELECT * FROM edge_rollout_targets WHERE rollout_target_id=?",
                (command["rollout_target_id"],),
            ).fetchone()
            if target is not None and str(target["status"]) not in {
                "succeeded",
                "failed",
                "cancelled",
                "rolled_back",
            }:
                connection.execute(
                    """
                    UPDATE edge_rollout_targets SET status='selected',rollout_generation=?,
                      revision=revision+1,updated_at=? WHERE rollout_target_id=? AND revision=?
                    """,
                    (
                        current["generation"],
                        _changed_stamp(now, target["updated_at"]),
                        target["rollout_target_id"],
                        target["revision"],
                    ),
                )
    targets = connection.execute(
        "SELECT DISTINCT workspace_id,tenant_id,edge_deployment_id FROM edge_rollout_targets "
        "WHERE rollout_id=?",
        (rollout["rollout_id"],),
    ).fetchall()
    event_type = "fleet.rollout_failed" if safe_reason in {
        "failure_threshold",
        "health_signal_missing",
    } else "fleet.rollout_paused"
    for target in targets:
        _event(
            connection,
            workspace_id=str(target["workspace_id"]),
            tenant_id=str(target["tenant_id"]),
            deployment_id=str(target["edge_deployment_id"]),
            rollout_id=str(rollout["rollout_id"]),
            target_id=None,
            rollback_id=None,
            event_type=event_type,
            reason=safe_reason,
            actor_domain=actor_domain,
            actor_id=actor_id,
            request_id=request_id,
            trace_id=trace_id,
            now=stamp,
        )


def _apply_release_head(
    connection: sqlite3.Connection,
    *,
    deployment_id: str,
    release_id: str,
    version: str,
    generation: int,
    rollback: bool,
    now: datetime,
) -> None:
    head = _release_head(connection, deployment_id)
    lkg_release_id = release_id if rollback else head["current_release_id"]
    lkg_version = version if rollback else head["current_version"]
    lkg_generation = generation if rollback else int(head["applied_generation"])
    connection.execute(
        """
        UPDATE edge_deployment_release_heads SET current_release_id=?,lkg_release_id=?,
          current_version=?,lkg_version=?,desired_generation=max(desired_generation,?),
          applied_generation=?,lkg_generation=?,revision=revision+1,updated_at=?
        WHERE edge_deployment_id=? AND revision=?
        """,
        (
            release_id,
            lkg_release_id,
            version,
            lkg_version,
            generation,
            generation,
            lkg_generation,
            _changed_stamp(now, head["updated_at"]),
            deployment_id,
            head["revision"],
        ),
    )


def _compatibility_summary(
    connection: sqlite3.Connection,
    deployment: sqlite3.Row,
    release: sqlite3.Row | None,
) -> CompatibilitySummary:
    head = _release_head(connection, str(deployment["edge_deployment_id"]))
    policy = _maintenance_row(connection, str(deployment["edge_deployment_id"]))
    reasons: list[str] = []
    if str(deployment["status"]) != "active":
        reasons.append("deployment_inactive")
    if str(deployment["mode"]) != "connected":
        reasons.append("isolated_mode")
    if policy is None or str(policy["status"]) != "active":
        reasons.append("maintenance_missing")
    if release is not None:
        compatible = json.loads(str(release["compatible_from_versions_json"]))
        if str(release["status"]) != "released":
            reasons.append("release_unavailable")
        if str(head["current_version"]) not in compatible:
            reasons.append("version_incompatible")
        if policy is not None and str(policy["release_channel"]) != str(
            release["release_channel"]
        ):
            reasons.append("channel_mismatch")
    return CompatibilitySummary(
        deployment_id=str(deployment["edge_deployment_id"]),
        current_version=str(head["current_version"]),
        lkg_version=str(head["lkg_version"]),
        release_id=(str(release["edge_gateway_release_id"]) if release else None),
        release_version=(str(release["version"]) if release else None),
        compatible=not reasons,
        reason_codes=tuple(reasons),
        maintenance=_maintenance_summary(policy) if policy is not None else None,
    )


def _workspace_response(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    deployment_id: str,
    request_id: str,
    trace_id: str,
) -> WorkspaceUpgradeResponse:
    policy = _maintenance_row(connection, deployment_id)
    head = _release_head(connection, deployment_id)
    targets = connection.execute(
        """
        SELECT target.*,batch.sequence batch_sequence FROM edge_rollout_targets target
        JOIN edge_rollout_batches batch ON batch.rollout_batch_id=target.rollout_batch_id
        WHERE target.workspace_id=? AND target.tenant_id=? AND target.edge_deployment_id=?
        ORDER BY target.selected_at DESC LIMIT 20
        """,
        (context.workspace_id, context.tenant_id, deployment_id),
    ).fetchall()
    rollbacks = connection.execute(
        """
        SELECT * FROM edge_rollback_attempts WHERE workspace_id=? AND tenant_id=?
          AND edge_deployment_id=? ORDER BY requested_at DESC LIMIT 20
        """,
        (context.workspace_id, context.tenant_id, deployment_id),
    ).fetchall()
    return WorkspaceUpgradeResponse(
        deployment_id=deployment_id,
        maintenance=_maintenance_summary(policy) if policy is not None else None,
        head=_head_summary(head),
        rollouts=tuple(_target_summary(item) for item in targets),
        rollback_attempts=tuple(_rollback_summary(item) for item in rollbacks),
        request_id=_identifier(request_id),
        trace_id=_identifier(trace_id),
    )


def _release_summary(
    connection: sqlite3.Connection, row: sqlite3.Row
) -> GatewayReleaseSummary:
    envelope = _release_envelope(connection, row)
    return GatewayReleaseSummary(
        release=envelope,
        status=str(row["status"]),
        revision=int(row["revision"]),
        submitted_by_operator_id=str(row["submitted_by_operator_id"]),
        approved_by_operator_id=(
            str(row["approved_by_operator_id"])
            if row["approved_by_operator_id"] is not None
            else None
        ),
        submitted_at=_datetime(row["submitted_at"]),
        approved_at=_datetime(row["approved_at"]) if row["approved_at"] else None,
        withdrawn_at=_datetime(row["withdrawn_at"]) if row["withdrawn_at"] else None,
    )


def _release_envelope(
    connection: sqlite3.Connection, row: sqlite3.Row
) -> GatewayRelease:
    value = {
        "manifest": json.loads(str(row["manifest_json"])),
        "manifest_sha256": str(row["manifest_sha256"]),
        "signature": {
            "algorithm": str(row["signature_algorithm"]),
            "key_id": str(row["signature_key_id"]),
            "public_key": str(row["signing_public_key_b64"]),
            "value": str(row["signature_value"]),
        },
    }
    public = _decode_public_key(str(row["signing_public_key_b64"]))
    return verify_gateway_release(
        value, trusted_keys={str(row["signature_key_id"]): public}
    ).release


def _release_response(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    request_id: str,
    trace_id: str,
    *,
    replay: bool,
) -> GatewayReleaseResponse:
    return GatewayReleaseResponse(
        release=_release_summary(connection, row),
        idempotent_replay=replay,
        request_id=_identifier(request_id),
        trace_id=_identifier(trace_id),
    )


def _rollout_summary(
    connection: sqlite3.Connection, row: sqlite3.Row
) -> RolloutSummary:
    release = _release_row(connection, str(row["edge_gateway_release_id"]))
    batches = connection.execute(
        "SELECT * FROM edge_rollout_batches WHERE rollout_id=? ORDER BY sequence",
        (row["rollout_id"],),
    ).fetchall()
    targets = connection.execute(
        """
        SELECT target.*,batch.sequence batch_sequence FROM edge_rollout_targets target
        JOIN edge_rollout_batches batch ON batch.rollout_batch_id=target.rollout_batch_id
        WHERE target.rollout_id=? ORDER BY batch.sequence,target.stable_bucket,
          target.edge_deployment_id
        """,
        (row["rollout_id"],),
    ).fetchall()
    return RolloutSummary(
        rollout_id=str(row["rollout_id"]),
        release_id=str(row["edge_gateway_release_id"]),
        release_version=str(release["version"]),
        release_channel=str(release["release_channel"]),
        status=str(row["status"]),
        generation=int(row["generation"]),
        cancel_watermark=int(row["cancel_watermark"]),
        canary_basis_points=int(row["canary_basis_points"]),
        observation_seconds=int(row["observation_seconds"]),
        target_count=int(row["target_count"]),
        succeeded_count=int(row["succeeded_count"]),
        failed_count=int(row["failed_count"]),
        current_batch_sequence=int(row["current_batch_sequence"]),
        pause_reason_code=(str(row["pause_reason_code"]) if row["pause_reason_code"] else None),
        revision=int(row["revision"]),
        batches=tuple(_batch_summary(item) for item in batches),
        targets=tuple(_target_summary(item) for item in targets),
        created_at=_datetime(row["created_at"]),
        updated_at=_datetime(row["updated_at"]),
    )


def _rollout_response(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    request_id: str,
    trace_id: str,
    *,
    replay: bool,
) -> RolloutResponse:
    return RolloutResponse(
        rollout=_rollout_summary(connection, row),
        idempotent_replay=replay,
        request_id=_identifier(request_id),
        trace_id=_identifier(trace_id),
    )


def _batch_summary(row: sqlite3.Row) -> RolloutBatchSummary:
    return RolloutBatchSummary(
        rollout_batch_id=str(row["rollout_batch_id"]),
        sequence=int(row["sequence"]),
        batch_kind=str(row["batch_kind"]),
        status=str(row["status"]),
        target_count=int(row["target_count"]),
        succeeded_count=int(row["succeeded_count"]),
        failed_count=int(row["failed_count"]),
        observe_until=_datetime(row["observe_until"]) if row["observe_until"] else None,
        updated_at=_datetime(row["updated_at"]),
    )


def _target_summary(row: sqlite3.Row) -> RolloutTargetSummary:
    return RolloutTargetSummary(
        rollout_target_id=str(row["rollout_target_id"]),
        deployment_id=str(row["edge_deployment_id"]),
        batch_sequence=int(row["batch_sequence"]),
        stable_bucket=int(row["stable_bucket"]),
        current_version=str(row["current_version"]),
        target_version=str(row["target_version"]),
        status=str(row["status"]),
        failure_reason_code=(
            str(row["failure_reason_code"]) if row["failure_reason_code"] else None
        ),
        updated_at=_datetime(row["updated_at"]),
    )


def _maintenance_summary(row: sqlite3.Row) -> MaintenanceWindowSummary:
    return MaintenanceWindowSummary(
        maintenance_window_id=str(row["maintenance_window_id"]),
        deployment_id=str(row["edge_deployment_id"]),
        release_channel=str(row["release_channel"]),
        timezone_name=str(row["timezone_name"]),
        weekdays=tuple(json.loads(str(row["weekdays_json"]))),
        start_minute=int(row["start_minute"]),
        duration_minutes=int(row["duration_minutes"]),
        freeze_starts_at=(
            _datetime(row["freeze_starts_at"]) if row["freeze_starts_at"] else None
        ),
        freeze_ends_at=(
            _datetime(row["freeze_ends_at"]) if row["freeze_ends_at"] else None
        ),
        status=str(row["status"]),
        policy_generation=int(row["policy_generation"]),
        revision=int(row["revision"]),
        reason_code=str(row["reason_code"]),
        updated_at=_datetime(row["updated_at"]),
    )


def _maintenance_contract(row: sqlite3.Row) -> MaintenanceWindowContract:
    return MaintenanceWindowContract(
        maintenance_window_id=str(row["maintenance_window_id"]),
        policy_generation=int(row["policy_generation"]),
        release_channel=str(row["release_channel"]),
        timezone_name=str(row["timezone_name"]),
        weekdays=json.loads(str(row["weekdays_json"])),
        start_minute=int(row["start_minute"]),
        duration_minutes=int(row["duration_minutes"]),
        freeze_starts_at=(
            _datetime(row["freeze_starts_at"]) if row["freeze_starts_at"] else None
        ),
        freeze_ends_at=(
            _datetime(row["freeze_ends_at"]) if row["freeze_ends_at"] else None
        ),
    )


def _head_summary(row: sqlite3.Row) -> DeploymentReleaseHeadSummary:
    return DeploymentReleaseHeadSummary(
        deployment_id=str(row["edge_deployment_id"]),
        current_release_id=(str(row["current_release_id"]) if row["current_release_id"] else None),
        lkg_release_id=(str(row["lkg_release_id"]) if row["lkg_release_id"] else None),
        current_version=str(row["current_version"]),
        lkg_version=str(row["lkg_version"]),
        desired_generation=int(row["desired_generation"]),
        applied_generation=int(row["applied_generation"]),
        lkg_generation=int(row["lkg_generation"]),
        revision=int(row["revision"]),
        updated_at=_datetime(row["updated_at"]),
    )


def _rollback_summary(row: sqlite3.Row) -> RollbackAttemptSummary:
    return RollbackAttemptSummary(
        rollback_attempt_id=str(row["rollback_attempt_id"]),
        deployment_id=str(row["edge_deployment_id"]),
        source_release_id=str(row["source_release_id"]),
        target_release_id=str(row["target_release_id"]),
        desired_generation=int(row["desired_generation"]),
        status=str(row["status"]),
        reason_code=str(row["reason_code"]),
        revision=int(row["revision"]),
        requested_at=_datetime(row["requested_at"]),
        completed_at=_datetime(row["completed_at"]) if row["completed_at"] else None,
        updated_at=_datetime(row["updated_at"]),
    )


def _agent_command_response(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    request_id: str,
    trace_id: str,
) -> AgentUpgradeCommandResponse:
    return AgentUpgradeCommandResponse(
        deployment_id=str(row["edge_deployment_id"]),
        command=_command_envelope(row),
        command_revision=int(row["revision"]),
        authority=_current_authority(connection, row, _utcnow()),
        request_id=_identifier(request_id),
        trace_id=_identifier(trace_id),
    )


def _current_authority(
    connection: sqlite3.Connection, row: sqlite3.Row, now: datetime
) -> UpgradeAuthority:
    policy = _maintenance_row(connection, str(row["edge_deployment_id"]))
    if (
        policy is None
        or int(policy["policy_generation"]) != int(row["maintenance_policy_generation"])
        or str(policy["status"]) != "active"
        or not maintenance_window_open(_maintenance_contract(policy), now)
    ):
        raise ApiError("edge_upgrade.maintenance_closed")
    if row["rollout_id"] is not None:
        rollout = _rollout_row(connection, str(row["rollout_id"]))
        generation = int(rollout["generation"])
        watermark = int(rollout["cancel_watermark"])
        if str(rollout["status"]) not in {"preparing", "running"}:
            raise ApiError("edge_upgrade.cancelled")
    else:
        generation = int(row["rollout_generation"])
        watermark = int(row["cancel_watermark"])
    if generation != int(row["rollout_generation"]) or watermark != int(
        row["cancel_watermark"]
    ):
        raise ApiError("edge_upgrade.cancelled")
    return UpgradeAuthority(
        rollout_generation=generation,
        cancel_watermark=watermark,
        maintenance_policy_generation=int(policy["policy_generation"]),
        observed_at=now,
    )


def _command_envelope(row: sqlite3.Row):
    try:
        from metahub_shared.private_gateway import UpgradeCommandEnvelope

        return UpgradeCommandEnvelope.model_validate_json(str(row["envelope_json"]))
    except ValueError:
        raise ApiError("edge_upgrade.integrity_failed") from None


def _common_window_time(targets: list[dict[str, object]], now: datetime) -> datetime:
    candidate = now.replace(second=0, microsecond=0)
    for offset in range(0, 8 * 24 * 60):
        instant = candidate + timedelta(minutes=offset)
        if all(
            maintenance_window_open(_maintenance_contract(target["policy"]), instant)
            for target in targets
        ):
            return instant
    raise ApiError("fleet.rollout_maintenance_unavailable")


def _target_with_policy(
    connection: sqlite3.Connection, target: sqlite3.Row
) -> dict[str, object]:
    policy = _maintenance_row(connection, str(target["edge_deployment_id"]))
    if policy is None:
        raise ApiError("fleet.rollout_target_ineligible")
    return {**dict(target), "policy": policy}


def _stable_bucket(config: AppConfig, rollout_id: str, deployment_id: str) -> int:
    digest = hmac.new(
        config.platform_session_secret.encode("utf-8"),
        f"prd127/cohort/v1\0{rollout_id}\0{deployment_id}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    return int.from_bytes(digest[:8], "big") % 10_000


def _signing_key(config: AppConfig, kind: str) -> tuple[Ed25519PrivateKey, str]:
    seed = hmac.new(
        config.platform_session_secret.encode("utf-8"),
        f"metahub/prd127/{kind}-signing-key/v1".encode("ascii"),
        hashlib.sha256,
    ).digest()
    private = Ed25519PrivateKey.from_private_bytes(seed)
    public = private.public_key().public_bytes_raw()
    key_id = f"edge-upgrade-{kind}-{hashlib.sha256(public).hexdigest()[:16]}"
    return private, key_id


def _decode_public_key(value: str) -> bytes:
    try:
        decoded = base64.urlsafe_b64decode(value + "=")
    except ValueError:
        raise UpgradeContractError("edge_upgrade.release_key_invalid") from None
    if len(decoded) != 32:
        raise UpgradeContractError("edge_upgrade.release_key_invalid")
    return decoded


def _action_replay(
    connection: sqlite3.Connection,
    actor_domain: str,
    actor_id: str,
    action: str,
    idempotency_sha256: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT * FROM edge_upgrade_action_commands WHERE actor_domain=? AND actor_id=?
          AND action_code=? AND idempotency_sha256=?
        """,
        (actor_domain, actor_id, action, idempotency_sha256),
    ).fetchone()


def _insert_action(
    connection: sqlite3.Connection,
    *,
    actor_domain: str,
    actor_id: str,
    action: str,
    idempotency_sha256: str,
    request_sha256: str,
    result_object_id: str,
    now: str,
) -> None:
    connection.execute(
        """
        INSERT INTO edge_upgrade_action_commands(
          edge_upgrade_action_command_id,actor_domain,actor_id,action_code,
          idempotency_sha256,request_sha256,result_object_id,created_at)
        VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            _id("fac_"),
            actor_domain,
            actor_id,
            action,
            idempotency_sha256,
            request_sha256,
            result_object_id,
            now,
        ),
    )


def _event(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    tenant_id: str,
    deployment_id: str,
    rollout_id: str | None,
    target_id: str | None,
    rollback_id: str | None,
    event_type: str,
    reason: str,
    actor_domain: str,
    actor_id: str,
    request_id: str,
    trace_id: str,
    now: str,
) -> None:
    event_id = _id("fue_")
    connection.execute(
        """
        INSERT INTO edge_fleet_upgrade_events(
          fleet_upgrade_event_id,workspace_id,tenant_id,edge_deployment_id,
          rollout_id,rollout_target_id,rollback_attempt_id,event_type,reason_code,
          actor_domain,actor_id,request_id,trace_id,occurred_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id,
            workspace_id,
            tenant_id,
            deployment_id,
            rollout_id,
            target_id,
            rollback_id,
            event_type,
            _safe_reason(reason),
            actor_domain,
            actor_id,
            _identifier(request_id),
            _identifier(trace_id),
            now,
        ),
    )
    for domain in ("workspace", "fleet", "security"):
        connection.execute(
            """
            INSERT INTO edge_fleet_upgrade_outbox(
              fleet_upgrade_outbox_id,workspace_id,tenant_id,fleet_upgrade_event_id,
              delivery_domain,status,attempt_count,available_at,delivered_at,created_at,updated_at)
            VALUES(?,?,?,?,?,'pending',0,?,NULL,?,?)
            """,
            (_id("fuo_"), workspace_id, tenant_id, event_id, domain, now, now, now),
        )


def _platform_audit(
    connection: sqlite3.Connection,
    context: PlatformContext,
    *,
    action: str,
    object_type: str,
    object_id: str,
    reason: str,
    request_id: str,
    trace_id: str,
    after: object,
    now: str,
) -> None:
    connection.execute(
        """
        INSERT INTO platform_audit_logs(
          audit_id,actor_domain,actor_id,action_code,object_type,object_id,reason_code,
          request_id,trace_id,before_sha256,after_sha256,occurred_at)
        VALUES(?,'platform',?,?,?,?,?,?,?,NULL,?,?)
        """,
        (
            _id("pau_"),
            context.operator_id,
            action,
            object_type,
            object_id,
            _safe_reason(reason),
            _identifier(request_id),
            _identifier(trace_id),
            _digest(after),
            now,
        ),
    )


def _release_row(connection: sqlite3.Connection, release_id: str | None) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM edge_gateway_releases WHERE edge_gateway_release_id=?",
        (release_id,),
    ).fetchone()
    if row is None:
        raise ApiError("fleet.release_not_found")
    return row


def _rollout_row(connection: sqlite3.Connection, rollout_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM edge_fleet_rollouts WHERE rollout_id=?", (rollout_id,)
    ).fetchone()
    if row is None:
        raise ApiError("fleet.rollout_not_found")
    return row


def _release_head(connection: sqlite3.Connection, deployment_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM edge_deployment_release_heads WHERE edge_deployment_id=?",
        (deployment_id,),
    ).fetchone()
    if row is None:
        raise ApiError("edge_upgrade.head_not_found")
    return row


def _maintenance_row(
    connection: sqlite3.Connection, deployment_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM edge_maintenance_windows WHERE edge_deployment_id=?",
        (deployment_id,),
    ).fetchone()


def _rollback_row(connection: sqlite3.Connection, rollback_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM edge_rollback_attempts WHERE rollback_attempt_id=?",
        (rollback_id,),
    ).fetchone()
    if row is None:
        raise ApiError("edge_upgrade.rollback_not_found")
    return row


def _command_row(
    connection: sqlite3.Connection, deployment_id: str, command_id: str
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM edge_upgrade_commands WHERE edge_deployment_id=? AND upgrade_command_id=?",
        (deployment_id, command_id),
    ).fetchone()
    if row is None:
        raise ApiError("edge_upgrade.command_not_found")
    return row


def _authenticated_agent(
    connection: sqlite3.Connection,
    config: AppConfig,
    deployment_id: str,
    verified_fingerprint: str,
    edge_session_token: str,
) -> sqlite3.Row:
    from .desired_state_service import _authenticated_agent as authenticate

    return authenticate(
        connection,
        config,
        deployment_id=deployment_id,
        verified_fingerprint=verified_fingerprint,
        edge_session_token=edge_session_token,
    )


def _require_upgrade_capability(agent: sqlite3.Row) -> None:
    try:
        capabilities = json.loads(str(agent["capability_codes_json"]))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        raise ApiError("edge.authentication_failed") from None
    if type(capabilities) is not list or "upgrade" not in capabilities:
        raise ApiError("edge.authentication_failed")


def _require_platform(context: PlatformContext, permission: str) -> None:
    if permission not in context.permissions:
        raise ApiError("platform.permission_denied")


def _platform_idempotency(
    config: AppConfig, operator_id: str, action: str, value: str
) -> str:
    if type(value) is not str or _IDEMPOTENCY_RE.fullmatch(value) is None:
        raise ApiError("fleet.request_invalid")
    return hmac.new(
        config.platform_session_secret.encode("utf-8"),
        f"metahub/prd127/platform-idempotency/v1\0{operator_id}\0{action}\0{value}".encode(
            "ascii"
        ),
        hashlib.sha256,
    ).hexdigest()


def _attempt_nonce_sha256(deployment_id: str, command_id: str, nonce: str) -> str:
    return hashlib.sha256(
        f"prd127\0{deployment_id}\0{command_id}\0{nonce}".encode("ascii")
    ).hexdigest()


def _changed_stamp(now: datetime, previous: object) -> str:
    prior = _datetime(previous)
    candidate = _db_time(now)
    return candidate if candidate > str(previous) else _db_time(prior + timedelta(milliseconds=1))


def _optional_time(value: datetime | None) -> str | None:
    return _db_time(value.astimezone(UTC)) if value is not None else None


def _canonical(value: object) -> str:
    return canonical_edge_json_bytes(value).decode("utf-8")


def _safe_reason(value: str) -> str:
    normalized = value.replace("edge_upgrade.", "").replace("fleet.", "")
    return normalized if re.fullmatch(r"[a-z][a-z0-9_.-]{1,79}", normalized) else "state_changed"


register_job_handler(
    "fleet.rollout.advance",
    input_model=FleetRolloutJobInput,
    handler=_rollout_job_handler,
)


__all__ = (
    "FleetRolloutJobInput",
    "create_platform_rollout",
    "get_fleet_compatibility",
    "get_workspace_upgrade",
    "list_platform_gateway_releases",
    "list_platform_rollouts",
    "mutate_agent_upgrade",
    "mutate_platform_gateway_release",
    "mutate_platform_rollout",
    "pull_agent_upgrade",
    "record_agent_upgrade_report",
    "request_workspace_rollback",
    "update_workspace_maintenance",
)
