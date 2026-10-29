"""Transactional PRD-147 inventory, command, approval, and cutover service."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.identity.security_service import (
    consume_global_step_up_grant_in_transaction,
)
from metahub_control.platform_access import service as platform_access_service
from metahub_control.platform_access.service import (
    PlatformContext,
    authenticate_platform_session_in_transaction,
)
from metahub_control.platform_jobs.service import (
    enqueue_prepared_platform_job_in_transaction,
    prepare_platform_job,
)

from .batches import batch_item_status, sync_console_migration_batch, sync_migration_batches
from .frontend_manifest import FRONTEND_PRODUCTION_SURFACE_MANIFEST_DIGEST
from .manifest import (
    DOMAIN_BY_CODE,
    DOMAIN_MANIFEST,
    MANDATORY_RECONCILIATION_CHECKS,
    DomainDefinition,
)
from .models import (
    BackfillCommand,
    BackfillControlCommand,
    BackfillPrepareCommand,
    ConsoleContractStatus,
    ConsoleMigrationCommandResponse,
    ConsoleMigrationStatusResponse,
    CutoverCommand,
    CutoverSecurityReviewCommand,
    DomainStatus,
    MigrationStatusItem,
    RollbackCommand,
    RollbackSecurityReviewCommand,
    RuntimeSurfaceStatus,
    SchedulerRegistryStatus,
)
from .worker import JOB_TYPE, ConsoleDomainMigrationJobInput

_TERMINAL = frozenset({"active", "rolled_back"})
_BACKFILL_CANCEL_STATES = frozenset(
    {"inventoried", "backfill_queued", "backfilling", "shadowing"}
)
_APPROVAL_TTL = timedelta(minutes=15)
_CURSOR_TTL = timedelta(minutes=10)
_RECONCILIATION_TTL = timedelta(minutes=15)


def get_console_migration_status(
    config: AppConfig,
    *,
    session_token: str,
    domain_code: str | None,
    state: str | None,
    workspace_ref: str | None,
    cursor: str | None,
    limit: int,
    request_id: str,
    trace_id: str,
) -> ConsoleMigrationStatusResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = authenticate_platform_session_in_transaction(
            connection, config, session_token=session_token, touch=True
        )
        _require(actor, "platform.console_migration.read")
        fingerprint = _permission_fingerprint(actor)
        contract_generation, route_epoch, write_fenced, contract_status = _contract_head(
            connection
        )
        filters = {
            "domain_code": domain_code,
            "state": state,
            "workspace_ref": workspace_ref,
        }
        offset = _decode_cursor(
            config,
            cursor,
            fingerprint=fingerprint,
            filters=filters,
            contract_generation=contract_generation,
        )
        sql = "SELECT * FROM console_domain_migrations WHERE 1=1"
        parameters: list[object] = []
        if domain_code is not None:
            sql += " AND domain_code=?"
            parameters.append(domain_code)
        if state is not None:
            sql += " AND state=?"
            parameters.append(state)
        sql += " ORDER BY domain_code,updated_at DESC,console_domain_migration_id"
        rows = connection.execute(sql, parameters).fetchall()
        if workspace_ref is not None:
            rows = [
                row
                for row in rows
                if hmac.compare_digest(
                    _workspace_ref(config, row["workspace_id"], row["tenant_id"]),
                    workspace_ref,
                )
            ]
        page = rows[offset : offset + limit]
        scheduler = _scheduler_status(connection)
        items = tuple(
            _status_item(connection, config, actor, row, scheduler.manifest_matches)
            for row in page
        )
        next_cursor = None
        if offset + limit < len(rows):
            next_cursor = _encode_cursor(
                config,
                offset=offset + limit,
                fingerprint=fingerprint,
                filters=filters,
                contract_generation=contract_generation,
            )
        counts = {
            row["domain_code"]: row
            for row in connection.execute(
                """SELECT domain_code,count(*) migration_count,
                     sum(CASE WHEN state='blocked' THEN 1 ELSE 0 END) blocked_count,
                     sum(CASE WHEN state='active' THEN 1 ELSE 0 END) active_count
                   FROM console_domain_migrations GROUP BY domain_code"""
            ).fetchall()
        }
        domains = tuple(
            _domain_status(definition.code, definition.source_migration, counts)
            for definition in DOMAIN_MANIFEST
        )
        runtime_surfaces = tuple(
            RuntimeSurfaceStatus(
                runtime_surface_code=row["runtime_surface_code"],
                execution_class=row["execution_class"],
                queue_capacity=int(row["queue_capacity"]),
                batch_limit=int(row["batch_limit"]),
                healthy_replica_count=int(row["healthy_count"]),
                degraded_replica_count=int(row["degraded_count"]),
            )
            for row in connection.execute(
                """WITH ranked_observations AS (
                     SELECT observation.*,
                       row_number() OVER (
                         PARTITION BY runtime_surface_code,replica_pseudonym
                         ORDER BY observed_at DESC,runtime_surface_observation_id DESC
                       ) AS observation_rank
                     FROM runtime_surface_observations observation
                   ), latest_observations AS (
                     SELECT * FROM ranked_observations WHERE observation_rank=1
                   )
                   SELECT manifest.runtime_surface_code,manifest.execution_class,
                     manifest.capacity_limit AS queue_capacity,manifest.batch_limit,
                     sum(CASE WHEN observation.runtime_surface_observation_id IS NOT NULL
                       AND unixepoch(observation.observed_at)>=unixepoch('now','-120 seconds')
                       AND observation.dropped_count=0
                       AND observation.backend_failure_count=0
                       AND observation.queued_count<manifest.capacity_limit
                       AND observation.shutdown_timeout_count=0
                       THEN 1 ELSE 0 END) healthy_count,
                     sum(CASE WHEN observation.runtime_surface_observation_id IS NOT NULL
                       AND NOT (
                         unixepoch(observation.observed_at)>=unixepoch('now','-120 seconds')
                         AND observation.dropped_count=0
                         AND observation.backend_failure_count=0
                         AND observation.queued_count<manifest.capacity_limit
                         AND observation.shutdown_timeout_count=0
                       ) THEN 1 ELSE 0 END) degraded_count
                   FROM runtime_surface_manifest manifest
                   LEFT JOIN latest_observations observation
                     USING(runtime_surface_code)
                   GROUP BY manifest.runtime_surface_code,manifest.execution_class,
                     manifest.capacity_limit,manifest.batch_limit
                   ORDER BY manifest.runtime_surface_code"""
            ).fetchall()
        )
        blocking = int(
            connection.execute(
                """SELECT
                   (SELECT count(*) FROM workspace_domain_reconciliation_checks
                    WHERE status!='matched')
                   +(SELECT count(*) FROM platform_domain_reconciliation_checks
                     WHERE status!='matched')"""
            ).fetchone()[0]
        )
        connection.commit()
        return ConsoleMigrationStatusResponse(
            domains=domains,
            items=items,
            next_cursor=next_cursor,
            scheduler=scheduler,
            runtime_surfaces=runtime_surfaces,
            contract=ConsoleContractStatus(
                contract_generation=contract_generation,
                route_deny_epoch=route_epoch,
                legacy_write_fenced=write_fenced,
                status=contract_status,
                blocking_check_count=blocking,
            ),
            permission_fingerprint=fingerprint,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, TypeError, ValueError, KeyError):
        _rollback(connection)
        raise ApiError("console_migration.dependency_unavailable") from None
    finally:
        connection.close()


def execute_backfill_command(
    config: AppConfig,
    payload: BackfillCommand,
    *,
    session_token: str,
    csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> ConsoleMigrationCommandResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _authenticate_write(connection, config, session_token, csrf_token)
        _require_role_permission(
            actor, "platform_ops", "platform.console_migration.backfill"
        )
        action_code = f"backfill.{payload.action}"
        replay = _existing_command(
            connection, config, actor, action_code, idempotency_key, payload
        )
        if replay is not None:
            result = _command_response(
                connection, actor, replay, request_id, trace_id, replay=True
            )
            connection.commit()
            return result
        now = _now()
        if isinstance(payload, BackfillPrepareCommand):
            result_id, migration_id, result_state, result_revision, generation, owner_ref = (
                _prepare_backfill(connection, config, payload, now)
            )
            resource_id = owner_ref or payload.domain_code
        else:
            migration = _migration(connection, payload.migration_id)
            owner_ref = _workspace_ref(
                config, migration["workspace_id"], migration["tenant_id"]
            )
            resource_id = payload.migration_id
            result_state, result_revision = _transition_backfill(
                connection, migration, payload, now
            )
            result_id = payload.migration_id
            migration_id = payload.migration_id
            generation = int(migration["operation_generation"])
            if payload.action in {"start", "resume"}:
                _enqueue_migration_job(
                    connection,
                    config,
                    migration_id=migration_id,
                    operation_generation=generation,
                    action="backfill",
                    result_revision=result_revision,
                    target_generation=None,
                    request_id=request_id,
                    trace_id=trace_id,
                )
        _consume_step_up(
            connection,
            config,
            actor,
            step_up_grant,
            action_code=f"console_migration.{action_code}",
            resource_id=resource_id,
            now=now,
        )
        command = _record_command(
            connection,
            config,
            actor,
            action_code=action_code,
            payload=payload,
            idempotency_key=idempotency_key,
            migration_id=migration_id,
            batch_id=result_id if result_id.startswith("cmb_") else None,
            workspace_ref=owner_ref,
            result_id=result_id,
            result_state=result_state,
            result_revision=result_revision,
            now=now,
        )
        _audit(
            connection,
            config,
            actor,
            action_code=action_code,
            owner_ref=owner_ref,
            old_state=None,
            new_state=result_state,
            quoted_revision=payload.expected_revision,
            idempotency_hmac=command["idempotency_key_hmac"],
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        connection.commit()
        return _build_command_response(
            connection,
            actor,
            operation_id=result_id,
            migration_id=migration_id,
            state=result_state,
            revision=result_revision,
            generation=generation,
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, TypeError, ValueError, KeyError):
        _rollback(connection)
        raise ApiError("console_migration.dependency_unavailable") from None
    finally:
        connection.close()


def execute_cutover_command(
    config: AppConfig,
    payload: CutoverCommand,
    *,
    session_token: str,
    csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> ConsoleMigrationCommandResponse:
    return _execute_transition_command(
        config,
        payload,
        action_kind="cutover",
        session_token=session_token,
        csrf_token=csrf_token,
        step_up_grant=step_up_grant,
        idempotency_key=idempotency_key,
        request_id=request_id,
        trace_id=trace_id,
    )


def execute_rollback_command(
    config: AppConfig,
    payload: RollbackCommand,
    *,
    session_token: str,
    csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> ConsoleMigrationCommandResponse:
    return _execute_transition_command(
        config,
        payload,
        action_kind="rollback",
        session_token=session_token,
        csrf_token=csrf_token,
        step_up_grant=step_up_grant,
        idempotency_key=idempotency_key,
        request_id=request_id,
        trace_id=trace_id,
    )


def _execute_transition_command(
    config: AppConfig,
    payload: CutoverCommand | RollbackCommand,
    *,
    action_kind: str,
    session_token: str,
    csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> ConsoleMigrationCommandResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _authenticate_write(connection, config, session_token, csrf_token)
        _require_transition_authority(actor, payload.action)
        action_code = f"{action_kind}.{payload.action}"
        replay = _existing_command(
            connection, config, actor, action_code, idempotency_key, payload
        )
        if replay is not None:
            result = _command_response(
                connection, actor, replay, request_id, trace_id, replay=True
            )
            connection.commit()
            return result
        migration = _migration(connection, payload.migration_id)
        _expect_revision(migration, payload.expected_revision)
        now = _now()
        owner_ref = _workspace_ref(
            config, migration["workspace_id"], migration["tenant_id"]
        )
        _consume_step_up(
            connection,
            config,
            actor,
            step_up_grant,
            action_code=f"console_migration.{action_code}",
            resource_id=payload.migration_id,
            now=now,
        )
        old_state = str(migration["state"])
        if payload.action == "prepare":
            state, revision = _prepare_transition(
                connection, migration, payload, action_kind, now
            )
        elif payload.action == "approve":
            state, revision = _approve_transition(
                connection, config, actor, migration, action_kind, now
            )
        elif payload.action == "security_review":
            state, revision = _review_transition(
                connection, actor, migration, payload, action_kind, now
            )
        elif payload.action == "execute":
            state, revision = _start_transition(
                connection, config, actor, migration, action_kind, now
            )
        elif payload.action == "resume" and action_kind == "rollback":
            if migration["state"] != "blocked":
                raise ApiError("console_migration.conflict")
            state, revision = _update_migration_state(
                connection, migration, "rolling_back", now
            )
        else:
            raise ApiError("console_migration.invalid")
        if payload.action == "execute" or (
            payload.action == "resume" and action_kind == "rollback"
        ):
            _enqueue_migration_job(
                connection,
                config,
                migration_id=payload.migration_id,
                operation_generation=int(migration["operation_generation"]),
                action=action_kind,
                result_revision=revision,
                target_generation=(
                    int(payload.target_generation) if action_kind == "rollback" else None
                ),
                request_id=request_id,
                trace_id=trace_id,
            )
        command = _record_command(
            connection,
            config,
            actor,
            action_code=action_code,
            payload=payload,
            idempotency_key=idempotency_key,
            migration_id=payload.migration_id,
            batch_id=None,
            workspace_ref=owner_ref,
            result_id=payload.migration_id,
            result_state=state,
            result_revision=revision,
            now=now,
        )
        _audit(
            connection,
            config,
            actor,
            action_code=action_code,
            owner_ref=owner_ref,
            old_state=old_state,
            new_state=state,
            quoted_revision=payload.expected_revision,
            idempotency_hmac=command["idempotency_key_hmac"],
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        connection.commit()
        return _build_command_response(
            connection,
            actor,
            operation_id=payload.migration_id,
            migration_id=payload.migration_id,
            state=state,
            revision=revision,
            generation=int(migration["operation_generation"]),
            request_id=request_id,
            trace_id=trace_id,
        )
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError as exc:
        _rollback(connection)
        if "UNIQUE" in str(exc):
            raise ApiError("console_migration.conflict") from None
        raise ApiError("console_migration.dependency_unavailable") from None
    except (sqlite3.Error, DbError, TypeError, ValueError, KeyError):
        _rollback(connection)
        raise ApiError("console_migration.dependency_unavailable") from None
    finally:
        connection.close()


def _prepare_backfill(
    connection: sqlite3.Connection,
    config: AppConfig,
    payload: BackfillPrepareCommand,
    now: datetime,
) -> tuple[str, str | None, str, int, int, str | None]:
    definition = DOMAIN_BY_CODE[payload.domain_code]
    stamp = _db_time(now)
    if payload.scope == "all_active":
        if payload.expected_revision != 0:
            raise ApiError("console_migration.conflict")
        batch_id = _id("cmb_")
        owners = connection.execute(
            """SELECT workspace_id,tenant_id FROM workspace_tenant_bindings
               WHERE status='active' ORDER BY workspace_id,tenant_id"""
        ).fetchall()
        generation = int(
            connection.execute(
                """SELECT coalesce(max(operation_generation),0)+1
                   FROM platform_console_domain_migrations WHERE domain_code=?""",
                (payload.domain_code,),
            ).fetchone()[0]
        )
        owner_refs = [
            _workspace_ref(config, owner["workspace_id"], owner["tenant_id"])
            for owner in owners
        ]
        connection.execute(
            """INSERT INTO platform_console_domain_migrations(
               platform_console_domain_migration_id,domain_code,operation_generation,
               state,workspace_total,workspace_ready,workspace_blocked,manifest_digest,
               revision,created_at,updated_at)
               VALUES(?,?,?,'inventoried',?,0,0,?,1,?,?)""",
            (
                _id("pcd_"),
                payload.domain_code,
                generation,
                len(owners),
                _sha(
                    {
                        "domain_code": payload.domain_code,
                        "operation_generation": generation,
                        "owner_refs": owner_refs,
                    }
                ),
                stamp,
                stamp,
            ),
        )
        connection.execute(
            """INSERT INTO platform_console_migration_batches(
               platform_console_migration_batch_id,domain_code,scope_code,workspace_ref,
               status,workspace_total,workspace_completed,workspace_blocked,
               operation_generation,revision,created_at,updated_at)
               VALUES(?,?,?,NULL,'prepared',?,0,0,?,1,?,?)""",
            (
                batch_id,
                payload.domain_code,
                payload.scope,
                len(owners),
                generation,
                stamp,
                stamp,
            ),
        )
        for sequence, owner in enumerate(owners, start=1):
            head = _workspace_migration_head(
                connection,
                owner["workspace_id"],
                owner["tenant_id"],
                payload.domain_code,
            )
            if head is None or str(head["state"]) in _TERMINAL:
                migration_id, child_generation = _create_workspace_migration(
                    connection, definition, owner, now
                )
                item_status = "inventoried"
            else:
                migration_id = str(head["console_domain_migration_id"])
                child_generation = int(head["operation_generation"])
                item_status = batch_item_status(str(head["state"]))
            connection.execute(
                """INSERT INTO platform_console_migration_batch_items(
                   platform_console_migration_batch_item_id,
                   platform_console_migration_batch_id,console_domain_migration_id,
                   workspace_id,tenant_id,operation_generation,sequence_number,status,
                   revision,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,1,?,?)""",
                (
                    _id("cbi_"),
                    batch_id,
                    migration_id,
                    owner["workspace_id"],
                    owner["tenant_id"],
                    child_generation,
                    sequence,
                    item_status,
                    stamp,
                    stamp,
                ),
            )
        sync_console_migration_batch(connection, batch_id, now)
        batch = connection.execute(
            """SELECT status,revision FROM platform_console_migration_batches
               WHERE platform_console_migration_batch_id=?""",
            (batch_id,),
        ).fetchone()
        return batch_id, None, str(batch["status"]), int(batch["revision"]), generation, None

    owner = _resolve_workspace(connection, config, payload.workspace_ref or "")
    head = _workspace_migration_head(
        connection,
        owner["workspace_id"],
        owner["tenant_id"],
        payload.domain_code,
    )
    current_revision = 0 if head is None else int(head["revision"])
    if payload.expected_revision != current_revision or (
        head is not None and head["state"] not in _TERMINAL
    ):
        raise ApiError("console_migration.conflict")
    migration_id, generation = _create_workspace_migration(
        connection, definition, owner, now
    )
    return (
        migration_id,
        migration_id,
        "inventoried",
        1,
        generation,
        payload.workspace_ref,
    )


def _workspace_migration_head(
    connection: sqlite3.Connection,
    workspace_id: object,
    tenant_id: object,
    domain_code: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """SELECT migration.* FROM console_domain_migration_heads head
           JOIN console_domain_migrations migration USING(console_domain_migration_id)
           WHERE head.workspace_id=? AND head.tenant_id=? AND head.domain_code=?""",
        (workspace_id, tenant_id, domain_code),
    ).fetchone()


def _create_workspace_migration(
    connection: sqlite3.Connection,
    definition: DomainDefinition,
    owner: sqlite3.Row,
    now: datetime,
) -> tuple[str, int]:
    head = _workspace_migration_head(
        connection,
        owner["workspace_id"],
        owner["tenant_id"],
        definition.code,
    )
    generation = 1 if head is None else int(head["operation_generation"]) + 1
    migration_id = _id("cdm_")
    stamp = _db_time(now)
    connection.execute(
        """INSERT INTO console_domain_migrations(
           console_domain_migration_id,workspace_id,tenant_id,domain_code,
           operation_generation,source_migration_version,target_migration_version,
           source_inventory_revision,target_generation,contract_epoch,state,
           progress_basis_points,reconciliation_id,reconciliation_digest,
           route_manifest_revision,scheduler_manifest_revision,approved_generation,
           reviewed_generation,last_error_code,revision,created_at,updated_at,completed_at)
           VALUES(?,?,?,?,?,?,?,?,0,1,'inventoried',0,NULL,NULL,1,1,NULL,NULL,NULL,1,?,?,NULL)""",
        (
            migration_id,
            owner["workspace_id"],
            owner["tenant_id"],
            definition.code,
            generation,
            definition.source_migration,
            definition.source_migration,
            generation,
            stamp,
            stamp,
        ),
    )
    connection.execute(
        """INSERT INTO console_domain_migration_heads(
           workspace_id,tenant_id,domain_code,console_domain_migration_id,
           operation_generation,revision,updated_at)
           VALUES(?,?,?,?,?,1,?)
           ON CONFLICT(workspace_id,tenant_id,domain_code) DO UPDATE SET
             console_domain_migration_id=excluded.console_domain_migration_id,
             operation_generation=excluded.operation_generation,
             revision=console_domain_migration_heads.revision+1,
             updated_at=excluded.updated_at""",
        (
            owner["workspace_id"],
            owner["tenant_id"],
            definition.code,
            migration_id,
            generation,
            stamp,
        ),
    )
    return migration_id, generation


def _transition_backfill(
    connection: sqlite3.Connection,
    migration: sqlite3.Row,
    payload: BackfillControlCommand,
    now: datetime,
) -> tuple[str, int]:
    _expect_revision(migration, payload.expected_revision)
    reconciliation_generation = 0
    if migration["reconciliation_id"] is not None:
        reconciliation_generation = int(migration["operation_generation"])
    if payload.reconciliation_generation != reconciliation_generation:
        raise ApiError("console_migration.conflict")
    current = str(migration["state"])
    if payload.action == "start" and current == "inventoried":
        target = "backfill_queued"
    elif payload.action == "resume" and current == "blocked":
        target = "backfilling"
    elif payload.action == "cancel" and current in _BACKFILL_CANCEL_STATES:
        target = "blocked"
    else:
        raise ApiError("console_migration.conflict")
    result = _update_migration_state(
        connection,
        migration,
        target,
        now,
        error_code="operator_cancelled" if payload.action == "cancel" else None,
    )
    sync_migration_batches(
        connection,
        str(migration["console_domain_migration_id"]),
        target,
        now,
    )
    return result


def _prepare_transition(
    connection: sqlite3.Connection,
    migration: sqlite3.Row,
    payload: CutoverCommand | RollbackCommand,
    action_kind: str,
    now: datetime,
) -> tuple[str, int]:
    if action_kind == "cutover":
        if migration["state"] != "ready":
            raise ApiError("console_migration.conflict")
        _validate_reconciliation(connection, migration, now=now)
        return _update_migration_state(connection, migration, "quiescing", now)
    if migration["state"] not in {
        "ready",
        "quiescing",
        "cutting_over",
        "validating",
        "active",
    }:
        raise ApiError("console_migration.conflict")
    target = int(payload.target_generation)
    current_target = int(migration["target_generation"])
    if current_target < 2 or target >= current_target:
        raise ApiError("console_migration.rollback_unavailable")
    return _update_migration_state(connection, migration, "rollback_queued", now)


def _approve_transition(
    connection: sqlite3.Connection,
    config: AppConfig,
    actor: PlatformContext,
    migration: sqlite3.Row,
    action_kind: str,
    now: datetime,
) -> tuple[str, int]:
    expected_state = "quiescing" if action_kind == "cutover" else "rollback_queued"
    if migration["state"] != expected_state:
        raise ApiError("console_migration.conflict")
    prepare = _prepare_command(connection, migration, action_kind)
    if prepare["operator_id"] == actor.operator_id:
        raise ApiError("console_migration.conflict")
    _validate_prepare_authority(connection, prepare)
    reconciliation = _validate_reconciliation(connection, migration, now=now)
    route_digest, scheduler_digest = _manifest_digests(connection)
    contract_generation, registry_generation = _head_generations(connection)
    authority_fingerprint = _saved_authority_fingerprint(connection, actor)
    connection.execute(
        """INSERT INTO platform_console_migration_approvals(
           platform_console_migration_approval_id,console_domain_migration_id,
           action_kind,operation_generation,prepare_operator_id,approver_operator_id,
           approver_operator_revision,approver_platform_session_id,
           approver_global_session_id,approver_factor_id,approver_factor_revision,
           contract_generation,registry_generation,migration_revision,reconciliation_digest,
           route_manifest_digest,scheduler_manifest_digest,permission_fingerprint,
           status,expires_at,created_at,consumed_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?,NULL)""",
        (
            _id("cma_"),
            migration["console_domain_migration_id"],
            action_kind,
            migration["operation_generation"],
            prepare["operator_id"],
            actor.operator_id,
            actor.operator_revision,
            actor.platform_session_id,
            actor.global_session_id,
            actor.factor_id,
            actor.factor_revision,
            contract_generation,
            registry_generation,
            migration["revision"],
            reconciliation["expected_digest_hmac"],
            route_digest,
            scheduler_digest,
            authority_fingerprint,
            _db_time(now + _APPROVAL_TTL),
            _db_time(now),
        ),
    )
    return str(migration["state"]), int(migration["revision"])


def _review_transition(
    connection: sqlite3.Connection,
    actor: PlatformContext,
    migration: sqlite3.Row,
    payload: CutoverCommand | RollbackCommand,
    action_kind: str,
    now: datetime,
) -> tuple[str, int]:
    expected_state = "quiescing" if action_kind == "cutover" else "rollback_queued"
    if migration["state"] != expected_state or not isinstance(
        payload, (CutoverSecurityReviewCommand, RollbackSecurityReviewCommand)
    ):
        raise ApiError("console_migration.conflict")
    prepare = _prepare_command(connection, migration, action_kind)
    if prepare["operator_id"] == actor.operator_id:
        raise ApiError("console_migration.conflict")
    _validate_prepare_authority(connection, prepare)
    reconciliation = _validate_reconciliation(
        connection,
        migration,
        reconciliation_id=payload.reconciliation_id,
        revision=payload.reconciliation_revision,
        now=now,
    )
    negative = connection.execute(
        """SELECT * FROM workspace_domain_reconciliation_checks
           WHERE domain_reconciliation_id=? AND check_type='cross_domain_negative'
             AND status='matched'""",
        (reconciliation["domain_reconciliation_id"],),
    ).fetchone()
    if negative is None:
        raise ApiError("console_migration.reconciliation_failed")
    authority_fingerprint = _saved_authority_fingerprint(connection, actor)
    contract_generation, registry_generation = _head_generations(connection)
    connection.execute(
        """INSERT INTO platform_console_migration_security_reviews(
           platform_console_migration_security_review_id,console_domain_migration_id,
           action_kind,operation_generation,prepare_operator_id,reviewer_operator_id,
           reviewer_operator_revision,reviewer_platform_session_id,
           reviewer_global_session_id,reviewer_factor_id,reviewer_factor_revision,
           contract_generation,registry_generation,reconciliation_id,reconciliation_revision,
           check_profile,profile_version,mandatory_case_count,artifact_digest,
           tool_version,permission_fingerprint,status,expires_at,created_at,consumed_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'console-domain-negative-v1',1,?,?,?,?,'active',?,?,NULL)""",
        (
            _id("cms_"),
            migration["console_domain_migration_id"],
            action_kind,
            migration["operation_generation"],
            prepare["operator_id"],
            actor.operator_id,
            actor.operator_revision,
            actor.platform_session_id,
            actor.global_session_id,
            actor.factor_id,
            actor.factor_revision,
            contract_generation,
            registry_generation,
            reconciliation["domain_reconciliation_id"],
            reconciliation["revision"],
            int(negative["expected_count"]),
            negative["artifact_digest"],
            negative["tool_version"],
            authority_fingerprint,
            _db_time(now + _APPROVAL_TTL),
            _db_time(now),
        ),
    )
    return str(migration["state"]), int(migration["revision"])


def _start_transition(
    connection: sqlite3.Connection,
    config: AppConfig,
    actor: PlatformContext,
    migration: sqlite3.Row,
    action_kind: str,
    now: datetime,
) -> tuple[str, int]:
    expected_state = "quiescing" if action_kind == "cutover" else "rollback_queued"
    if migration["state"] != expected_state:
        raise ApiError("console_migration.conflict")
    generation = migration["operation_generation"]
    approval = connection.execute(
        """SELECT * FROM platform_console_migration_approvals
           WHERE console_domain_migration_id=? AND action_kind=?
             AND operation_generation=? AND status='active' AND expires_at>?""",
        (migration["console_domain_migration_id"], action_kind, generation, _db_time(now)),
    ).fetchone()
    if approval is None:
        raise ApiError("console_migration.approval_required")
    review = connection.execute(
        """SELECT * FROM platform_console_migration_security_reviews
           WHERE console_domain_migration_id=? AND action_kind=?
             AND operation_generation=? AND status='active' AND expires_at>?""",
        (migration["console_domain_migration_id"], action_kind, generation, _db_time(now)),
    ).fetchone()
    if review is None:
        raise ApiError("console_migration.security_review_required")
    participant_operators = {
        approval["prepare_operator_id"],
        actor.operator_id,
        approval["approver_operator_id"],
        review["reviewer_operator_id"],
    }
    if len(participant_operators) != 4:
        raise ApiError("console_migration.conflict")
    participant_accounts = {
        row["account_id"]
        for row in connection.execute(
            f"""SELECT account_id FROM platform_operators
                 WHERE operator_id IN ({','.join('?' for _ in participant_operators)})""",
            tuple(participant_operators),
        ).fetchall()
    }
    if len(participant_accounts) != 4:
        raise ApiError("console_migration.conflict")
    prepare = _prepare_command(connection, migration, action_kind)
    _validate_prepare_authority(connection, prepare)
    _validate_saved_authority(
        connection,
        approval["approver_operator_id"],
        operator_revision=int(approval["approver_operator_revision"]),
        platform_session_id=str(approval["approver_platform_session_id"]),
        global_session_id=str(approval["approver_global_session_id"]),
        factor_id=str(approval["approver_factor_id"]),
        factor_revision=int(approval["approver_factor_revision"]),
        role="platform_owner",
        permission="platform.console_migration.approve",
        fingerprint=approval["permission_fingerprint"],
    )
    _validate_saved_authority(
        connection,
        review["reviewer_operator_id"],
        operator_revision=int(review["reviewer_operator_revision"]),
        platform_session_id=str(review["reviewer_platform_session_id"]),
        global_session_id=str(review["reviewer_global_session_id"]),
        factor_id=str(review["reviewer_factor_id"]),
        factor_revision=int(review["reviewer_factor_revision"]),
        role="platform_security",
        permission="platform.console_migration.security_review",
        fingerprint=review["permission_fingerprint"],
    )
    reconciliation = _validate_reconciliation(connection, migration, now=now)
    route_digest, scheduler_digest = _manifest_digests(connection)
    contract_generation, registry_generation = _head_generations(connection)
    if any(
        int(row["contract_generation"]) != contract_generation
        for row in (prepare, approval, review)
    ):
        raise ApiError("console_migration.contract_not_ready")
    if any(
        int(row["registry_generation"]) != registry_generation
        for row in (prepare, approval, review)
    ):
        raise ApiError("console_migration.registry_drift")
    if not hmac.compare_digest(route_digest, str(approval["route_manifest_digest"])):
        raise ApiError("console_migration.contract_not_ready")
    if not hmac.compare_digest(scheduler_digest, str(approval["scheduler_manifest_digest"])):
        raise ApiError("console_migration.registry_drift")
    if not hmac.compare_digest(
        str(reconciliation["expected_digest_hmac"]),
        str(approval["reconciliation_digest"]),
    ):
        raise ApiError("console_migration.reconciliation_failed")
    _require_no_active_backfill_lease(connection, migration, now)
    connection.execute(
        """UPDATE platform_console_migration_approvals
           SET status='consumed',consumed_at=?
           WHERE platform_console_migration_approval_id=?""",
        (_db_time(now), approval["platform_console_migration_approval_id"]),
    )
    connection.execute(
        """UPDATE platform_console_migration_security_reviews
           SET status='consumed',consumed_at=?
           WHERE platform_console_migration_security_review_id=?""",
        (_db_time(now), review["platform_console_migration_security_review_id"]),
    )
    if action_kind == "cutover":
        _release_contract(connection, migration, route_digest, scheduler_digest, now)
        return _update_migration_state(connection, migration, "cutting_over", now)
    return _update_migration_state(connection, migration, "rolling_back", now)


def _release_contract(
    connection: sqlite3.Connection,
    migration: sqlite3.Row,
    route_digest: str,
    scheduler_digest: str,
    now: datetime,
) -> None:
    current = connection.execute(
        "SELECT generation FROM console_contract_release_heads WHERE head_key='production'"
    ).fetchone()
    generation = 1 if current is None else int(current["generation"]) + 1
    release_id = _id("ccr_")
    target_digest = _sha(
        {
            "migration_id": migration["console_domain_migration_id"],
            "operation_generation": migration["operation_generation"],
            "target_generation": migration["target_generation"],
        }
    )
    navigation_digest = _navigation_digest(connection)
    permission_digest = _sha(
        [
            tuple(row)
            for row in connection.execute(
                """SELECT role_code,permission_code
                   FROM platform_console_migration_role_permissions
                   ORDER BY role_code,permission_code"""
            ).fetchall()
        ]
    )
    frontend_digest = FRONTEND_PRODUCTION_SURFACE_MANIFEST_DIGEST
    telemetry_digest = _sha({"events": 4, "schema": 1})
    stamp = _db_time(now)
    connection.execute(
        """INSERT INTO console_contract_releases(
           console_contract_release_id,generation,status,target_generation_digest,
           navigation_contract_digest,route_manifest_digest,api_manifest_digest,
           permission_manifest_digest,frontend_manifest_digest,
           telemetry_manifest_digest,scheduler_manifest_digest,revision,
           created_at,released_at)
           VALUES(?,?,'released',?,?,?,?,?,?,?,?,1,?,?)""",
        (
            release_id,
            generation,
            target_digest,
            navigation_digest,
            route_digest,
            _sha({"api": "console-migration", "version": 1}),
            permission_digest,
            frontend_digest,
            telemetry_digest,
            scheduler_digest,
            stamp,
            stamp,
        ),
    )
    items = (
        ("target-generation", "domain", migration["target_generation"], target_digest),
        ("workspace-navigation", "navigation", generation, navigation_digest),
        ("legacy-route-deny", "route", generation, route_digest),
        ("console-migration-api", "api", 1, _sha({"api": "console-migration", "version": 1})),
        ("migration-permissions", "permission", 1, permission_digest),
        ("console-web-runtime", "frontend", generation, frontend_digest),
        ("migration-telemetry", "telemetry", 1, telemetry_digest),
        ("scheduler-registry", "scheduler", 1, scheduler_digest),
    )
    connection.executemany(
        """INSERT INTO console_contract_release_items(
           console_contract_release_id,generation,item_code,item_kind,item_generation,item_digest)
           VALUES(?,?,?,?,?,?)""",
        [(release_id, generation, *item) for item in items],
    )
    connection.execute(
        """INSERT INTO console_contract_release_heads(
           head_key,console_contract_release_id,generation,revision,updated_at)
           VALUES('production',?,?,1,?)
           ON CONFLICT(head_key) DO UPDATE SET
             console_contract_release_id=excluded.console_contract_release_id,
             generation=excluded.generation,
             revision=console_contract_release_heads.revision+1,
             updated_at=excluded.updated_at""",
        (release_id, generation, stamp),
    )
    updated = connection.execute(
        """UPDATE console_ingress_contract_heads
           SET route_deny_epoch=route_deny_epoch+1,contract_generation=?,
             route_manifest_digest=?,legacy_write_fenced=1,status='active',
             revision=revision+1,updated_at=?
           WHERE head_key='production'""",
        (generation, route_digest, stamp),
    ).rowcount
    if updated != 1:
        raise ApiError("console_migration.contract_not_ready")


def _validate_reconciliation(
    connection: sqlite3.Connection,
    migration: sqlite3.Row,
    *,
    reconciliation_id: str | None = None,
    revision: int | None = None,
    now: datetime | None = None,
) -> sqlite3.Row:
    checked_after = (now or _now()) - _RECONCILIATION_TTL
    row = connection.execute(
        """SELECT * FROM workspace_domain_reconciliations
           WHERE console_domain_migration_id=? AND operation_generation=?""",
        (migration["console_domain_migration_id"], migration["operation_generation"]),
    ).fetchone()
    if (
        row is None
        or row["status"] != "matched"
        or row["check_profile"] != "console-domain-cutover-v1"
        or int(row["check_profile_version"]) != 1
        or int(row["expected_count"]) != int(row["observed_count"])
        or int(row["coverage_basis_points"]) != 10_000
        or _parse_time(row["checked_at"]) < checked_after
        or (reconciliation_id is not None and row["domain_reconciliation_id"] != reconciliation_id)
        or (revision is not None and int(row["revision"]) != revision)
    ):
        raise ApiError("console_migration.reconciliation_failed")
    checks = connection.execute(
        """SELECT check_type,status,expected_count,observed_count,checked_at
             FROM workspace_domain_reconciliation_checks
           WHERE domain_reconciliation_id=?""",
        (row["domain_reconciliation_id"],),
    ).fetchall()
    observed = {str(check["check_type"]): str(check["status"]) for check in checks}
    expected = set(MANDATORY_RECONCILIATION_CHECKS)
    if (
        set(observed) != expected
        or any(value != "matched" for value in observed.values())
        or any(
            int(check["expected_count"]) != int(check["observed_count"])
            or _parse_time(check["checked_at"]) < checked_after
            for check in checks
        )
    ):
        raise ApiError("console_migration.reconciliation_failed")
    return row


def _require_no_active_backfill_lease(
    connection: sqlite3.Connection,
    migration: sqlite3.Row,
    now: datetime,
) -> None:
    active = connection.execute(
        """SELECT 1 FROM console_backfill_cursors
           WHERE console_domain_migration_id=? AND status='running'
             AND lease_token_hmac IS NOT NULL AND lease_expires_at>?
           LIMIT 1""",
        (migration["console_domain_migration_id"], _db_time(now)),
    ).fetchone()
    if active is not None:
        raise ApiError("console_migration.lease_active")


def _prepare_command(
    connection: sqlite3.Connection, migration: sqlite3.Row, action_kind: str
) -> sqlite3.Row:
    row = connection.execute(
        """SELECT * FROM platform_console_migration_commands
           WHERE console_domain_migration_id=? AND action_code=?
           ORDER BY created_at DESC LIMIT 1""",
        (migration["console_domain_migration_id"], f"{action_kind}.prepare"),
    ).fetchone()
    if row is None:
        raise ApiError("console_migration.conflict")
    return row


def _validate_saved_authority(
    connection: sqlite3.Connection,
    operator_id: str,
    *,
    operator_revision: int,
    platform_session_id: str,
    global_session_id: str,
    factor_id: str,
    factor_revision: int,
    role: str,
    permission: str,
    fingerprint: str,
) -> None:
    session = _active_session_epoch(
        connection,
        operator_id=operator_id,
        platform_session_id=platform_session_id,
        global_session_id=global_session_id,
        factor_id=factor_id,
        operator_revision=operator_revision,
        factor_revision=factor_revision,
    )
    roles, permissions, _authority = platform_access_service._authority(
        connection, operator_id
    )
    current = _permission_fingerprint(
        PlatformContext(
            platform_session_id=platform_session_id,
            operator_id=operator_id,
            account_id=str(session["account_id"]),
            global_session_id=global_session_id,
            factor_id=factor_id,
            factor_revision=factor_revision,
            operator_revision=operator_revision,
            roles=roles,
            permissions=permissions,
            expires_at=_parse_time(session["absolute_expires_at"]),
        )
    )
    if (
        role not in roles
        or permission not in permissions
        or not hmac.compare_digest(current, str(fingerprint))
    ):
        raise ApiError("console_migration.permission_revoked")


def _saved_authority_fingerprint(
    connection: sqlite3.Connection, actor: PlatformContext
) -> str:
    _active_session_epoch(
        connection,
        operator_id=actor.operator_id,
        platform_session_id=actor.platform_session_id,
        global_session_id=actor.global_session_id,
        factor_id=actor.factor_id,
        operator_revision=actor.operator_revision,
        factor_revision=actor.factor_revision,
    )
    roles, permissions, _authority = platform_access_service._authority(
        connection, actor.operator_id
    )
    if roles != actor.roles or permissions != actor.permissions:
        raise ApiError("console_migration.permission_revoked")
    return _permission_fingerprint(actor)


def _validate_prepare_authority(
    connection: sqlite3.Connection, command: sqlite3.Row
) -> None:
    _validate_saved_authority(
        connection,
        str(command["operator_id"]),
        operator_revision=int(command["operator_revision"]),
        platform_session_id=str(command["platform_session_id"]),
        global_session_id=str(command["global_session_id"]),
        factor_id=str(command["factor_id"]),
        factor_revision=int(command["factor_revision"]),
        role="platform_ops",
        permission="platform.console_migration.backfill",
        fingerprint=str(command["permission_fingerprint"]),
    )


def _active_session_epoch(
    connection: sqlite3.Connection,
    *,
    operator_id: str,
    platform_session_id: str,
    global_session_id: str,
    factor_id: str,
    operator_revision: int,
    factor_revision: int,
) -> sqlite3.Row:
    row = connection.execute(
        """SELECT session.account_id,session.status,session.operator_revision,
             session.factor_revision,session.absolute_expires_at,session.idle_expires_at,
             operator.status operator_status,operator.revision current_operator_revision,
             account.status account_status,account.login_enabled,
             account.credential_revision current_credential_revision,
             account.session_revision current_account_session_revision,
             global_session.status global_status,
             global_session.credential_revision global_credential_revision,
             global_session.session_revision global_session_revision,
             global_session.expires_at global_expires_at,
             global_session.idle_expires_at global_idle_expires_at,
             factor.status factor_status,factor.revision current_factor_revision
           FROM platform_sessions session
           JOIN platform_operators operator ON operator.operator_id=session.operator_id
           JOIN global_accounts account ON account.account_id=session.account_id
           JOIN global_sessions global_session
             ON global_session.session_id=session.global_session_id
           JOIN account_mfa_factors factor ON factor.factor_id=session.factor_id
           WHERE session.platform_session_id=? AND session.operator_id=?
             AND session.global_session_id=? AND session.factor_id=?""",
        (platform_session_id, operator_id, global_session_id, factor_id),
    ).fetchone()
    now = _now()
    if (
        row is None
        or row["status"] != "active"
        or row["operator_status"] != "active"
        or row["account_status"] != "active"
        or row["login_enabled"] != 1
        or row["global_status"] != "active"
        or row["factor_status"] != "active"
        or int(row["operator_revision"]) != operator_revision
        or int(row["current_operator_revision"]) != operator_revision
        or int(row["factor_revision"]) != factor_revision
        or int(row["current_factor_revision"]) != factor_revision
        or int(row["global_credential_revision"])
        != int(row["current_credential_revision"])
        or int(row["global_session_revision"])
        != int(row["current_account_session_revision"])
        or any(
            _parse_time(row[field]) <= now
            for field in (
                "absolute_expires_at",
                "idle_expires_at",
                "global_expires_at",
                "global_idle_expires_at",
            )
        )
    ):
        raise ApiError("console_migration.permission_revoked")
    return row


def _existing_command(
    connection: sqlite3.Connection,
    config: AppConfig,
    actor: PlatformContext,
    action_code: str,
    idempotency_key: str,
    payload: object,
) -> sqlite3.Row | None:
    key_hmac = _secret_hmac(config, "idempotency", idempotency_key)
    row = connection.execute(
        """SELECT * FROM platform_console_migration_commands
           WHERE operator_id=? AND action_code=? AND idempotency_key_hmac=?""",
        (actor.operator_id, action_code, key_hmac),
    ).fetchone()
    if row is None:
        return None
    digest = _secret_hmac(config, "request", _json(payload.model_dump(mode="json")))
    if not hmac.compare_digest(str(row["request_digest_hmac"]), digest):
        raise ApiError("console_migration.conflict")
    return row


def _record_command(
    connection: sqlite3.Connection,
    config: AppConfig,
    actor: PlatformContext,
    *,
    action_code: str,
    payload: object,
    idempotency_key: str,
    migration_id: str | None,
    batch_id: str | None,
    workspace_ref: str | None,
    result_id: str,
    result_state: str,
    result_revision: int,
    now: datetime,
) -> sqlite3.Row:
    key_hmac = _secret_hmac(config, "idempotency", idempotency_key)
    request_hmac = _secret_hmac(
        config, "request", _json(payload.model_dump(mode="json"))
    )
    command_id = _id("cmc_")
    contract_generation, registry_generation = _head_generations(connection)
    connection.execute(
        """INSERT INTO platform_console_migration_commands(
           platform_console_migration_command_id,operator_id,operator_revision,
           platform_session_id,global_session_id,factor_id,factor_revision,
           contract_generation,registry_generation,action_code,
           console_domain_migration_id,platform_console_migration_batch_id,
           workspace_ref,expected_revision,idempotency_key_hmac,request_digest_hmac,
           permission_fingerprint,result_object_id,result_state,result_revision,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            command_id,
            actor.operator_id,
            actor.operator_revision,
            actor.platform_session_id,
            actor.global_session_id,
            actor.factor_id,
            actor.factor_revision,
            contract_generation,
            registry_generation,
            action_code,
            migration_id,
            batch_id,
            workspace_ref,
            payload.expected_revision,
            key_hmac,
            request_hmac,
            _permission_fingerprint(actor),
            result_id,
            result_state,
            result_revision,
            _db_time(now),
        ),
    )
    return connection.execute(
        """SELECT * FROM platform_console_migration_commands
           WHERE platform_console_migration_command_id=?""",
        (command_id,),
    ).fetchone()


def _command_response(
    connection: sqlite3.Connection,
    actor: PlatformContext,
    command: sqlite3.Row,
    request_id: str,
    trace_id: str,
    *,
    replay: bool,
) -> ConsoleMigrationCommandResponse:
    migration_id = command["console_domain_migration_id"]
    if migration_id is not None:
        source = _migration(connection, migration_id)
    else:
        source = connection.execute(
            """SELECT operation_generation FROM platform_console_migration_batches
               WHERE platform_console_migration_batch_id=?""",
            (command["platform_console_migration_batch_id"],),
        ).fetchone()
        if source is None:
            raise ApiError("console_migration.not_found")
    return _build_command_response(
        connection,
        actor,
        operation_id=str(command["result_object_id"]),
        migration_id=migration_id,
        state=str(command["result_state"]),
        revision=int(command["result_revision"]),
        generation=int(source["operation_generation"]),
        request_id=request_id,
        trace_id=trace_id,
        replay=replay,
    )


def _build_command_response(
    connection: sqlite3.Connection,
    actor: PlatformContext,
    *,
    operation_id: str,
    migration_id: str | None,
    state: str,
    revision: int,
    generation: int,
    request_id: str,
    trace_id: str,
    replay: bool = False,
) -> ConsoleMigrationCommandResponse:
    contract_generation, _epoch, _fenced, _status = _contract_head(connection)
    registry_generation = int(
        connection.execute(
            "SELECT generation FROM scheduler_registry_heads WHERE head_key='production'"
        ).fetchone()[0]
    )
    return ConsoleMigrationCommandResponse(
        operation_id=operation_id,
        migration_id=migration_id,
        state=state,
        revision=revision,
        operation_generation=generation,
        accepted=True,
        idempotent_replay=replay,
        polling_location_code="console-migration-status",
        contract_generation=contract_generation,
        registry_generation=registry_generation,
        permission_fingerprint=_permission_fingerprint(actor),
        request_id=request_id,
        trace_id=trace_id,
    )


def _audit(
    connection: sqlite3.Connection,
    config: AppConfig,
    actor: PlatformContext,
    *,
    action_code: str,
    owner_ref: str | None,
    old_state: str | None,
    new_state: str,
    quoted_revision: int,
    idempotency_hmac: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    connection.execute(
        """INSERT INTO platform_console_migration_audits(
           platform_console_migration_audit_id,operator_pseudonym,operator_revision,
           factor_revision,session_epoch_hmac,action_code,owner_ref,old_state,new_state,quoted_revision,
           idempotency_key_hmac,permission_fingerprint,result_code,request_id,trace_id,
           created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            _id("cau_"),
            _secret_hmac(config, "operator", actor.operator_id),
            actor.operator_revision,
            actor.factor_revision,
            _secret_hmac(
                config,
                "session-epoch",
                f"{actor.platform_session_id}\x1f{actor.global_session_id}\x1f{actor.factor_id}",
            ),
            action_code,
            owner_ref,
            old_state,
            new_state,
            quoted_revision,
            idempotency_hmac,
            _permission_fingerprint(actor),
            "accepted",
            request_id,
            trace_id,
            _db_time(now),
        ),
    )


def _status_item(
    connection: sqlite3.Connection,
    config: AppConfig,
    actor: PlatformContext,
    row: sqlite3.Row,
    scheduler_ready: bool,
) -> MigrationStatusItem:
    reconciliation = connection.execute(
        """SELECT status,reason_code FROM workspace_domain_reconciliations
           WHERE console_domain_migration_id=? AND operation_generation=?""",
        (row["console_domain_migration_id"], row["operation_generation"]),
    ).fetchone()
    approval = connection.execute(
        """SELECT 1 FROM platform_console_migration_approvals
           WHERE console_domain_migration_id=? AND operation_generation=?
             AND status='active' AND expires_at>strftime('%Y-%m-%dT%H:%M:%SZ','now')""",
        (row["console_domain_migration_id"], row["operation_generation"]),
    ).fetchone()
    review = connection.execute(
        """SELECT 1 FROM platform_console_migration_security_reviews
           WHERE console_domain_migration_id=? AND operation_generation=?
             AND status='active' AND expires_at>strftime('%Y-%m-%dT%H:%M:%SZ','now')""",
        (row["console_domain_migration_id"], row["operation_generation"]),
    ).fetchone()
    contract = connection.execute(
        """SELECT status,legacy_write_fenced FROM console_ingress_contract_heads
           WHERE head_key='production'"""
    ).fetchone()
    return MigrationStatusItem(
        migration_id=row["console_domain_migration_id"],
        workspace_ref=_workspace_ref(config, row["workspace_id"], row["tenant_id"]),
        domain_code=row["domain_code"],
        state=row["state"],
        progress_basis_points=int(row["progress_basis_points"]),
        operation_generation=int(row["operation_generation"]),
        target_generation=int(row["target_generation"]),
        revision=int(row["revision"]),
        reconciliation_status=(
            "missing" if reconciliation is None else reconciliation["status"]
        ),
        reason_code=(
            "reconciliation_missing"
            if reconciliation is None
            else reconciliation["reason_code"]
        ),
        approval_ready=approval is not None,
        security_review_ready=review is not None,
        route_ready=bool(contract["legacy_write_fenced"] and contract["status"] == "active"),
        scheduler_ready=scheduler_ready,
        last_safe_at=_parse_time(row["updated_at"]),
        allowed_actions=_allowed_actions(actor, str(row["state"])),
    )


def _allowed_actions(actor: PlatformContext, state: str) -> tuple[str, ...]:
    actions: list[str] = []
    permissions = set(actor.permissions)
    if "platform.console_migration.backfill" in permissions and "platform_ops" in actor.roles:
        if state == "inventoried":
            actions.extend(("backfill.start", "backfill.cancel"))
        elif state == "blocked":
            actions.extend(("backfill.resume", "rollback.resume"))
        elif state in {"backfill_queued", "backfilling", "shadowing"}:
            actions.append("backfill.cancel")
        elif state == "ready":
            actions.extend(("cutover.prepare", "rollback.prepare"))
    if state in {"quiescing", "rollback_queued"}:
        if "platform.console_migration.approve" in permissions and "platform_owner" in actor.roles:
            actions.append("cutover.approve" if state == "quiescing" else "rollback.approve")
        if (
            "platform.console_migration.security_review" in permissions
            and "platform_security" in actor.roles
        ):
            actions.append(
                "cutover.security_review"
                if state == "quiescing"
                else "rollback.security_review"
            )
        if "platform.console_migration.execute" in permissions and "platform_ops" in actor.roles:
            actions.append("cutover.execute" if state == "quiescing" else "rollback.execute")
    return tuple(actions)


def _scheduler_status(connection: sqlite3.Connection) -> SchedulerRegistryStatus:
    head = connection.execute(
        """SELECT generation,manifest_digest FROM scheduler_registry_heads
           WHERE head_key='production'"""
    ).fetchone()
    counts = connection.execute(
        """SELECT count(*) total,sum(enabled) enabled
           FROM scheduler_registry_entries"""
    ).fetchone()
    baseline = int(
        connection.execute("SELECT count(*) FROM scheduler_baseline_mappings").fetchone()[0]
    )
    final = int(
        connection.execute(
            """SELECT count(*) FROM scheduler_registry_entries
               WHERE scheduler_code!='platform-job-runtime'"""
        ).fetchone()[0]
    )
    queues = int(connection.execute("SELECT count(*) FROM scheduler_queue_contracts").fetchone()[0])
    active = int(
        connection.execute(
            """SELECT count(*) FROM scheduler_leases
               WHERE lease_deadline_at>strftime('%Y-%m-%dT%H:%M:%SZ','now')"""
        ).fetchone()[0]
    )
    # SQLite's seed digest uses a pipe-joined string rather than JSON.
    seeded = hashlib.sha256(
        "|".join(
            f"{row['scheduler_code']}:{row['manifest_digest']}"
            for row in connection.execute(
                """SELECT scheduler_code,manifest_digest
                   FROM scheduler_registry_entries ORDER BY scheduler_code"""
            ).fetchall()
        ).encode()
    ).hexdigest()
    return SchedulerRegistryStatus(
        baseline_slot_count=41,
        final_installer_count=36,
        cluster_context_count=37,
        queue_contract_count=queues,
        registry_generation=int(head["generation"]),
        enabled_context_count=int(counts["enabled"] or 0),
        active_lease_count=active,
        manifest_matches=(
            int(counts["total"]) == 37
            and baseline == 41
            and final == 36
            and hmac.compare_digest(str(head["manifest_digest"]), seeded)
        ),
    )


def _manifest_digests(connection: sqlite3.Connection) -> tuple[str, str]:
    route = connection.execute(
        """SELECT route_manifest_digest FROM console_ingress_contract_heads
           WHERE head_key='production'"""
    ).fetchone()
    scheduler = connection.execute(
        "SELECT manifest_digest FROM scheduler_registry_heads WHERE head_key='production'"
    ).fetchone()
    if route is None or scheduler is None:
        raise ApiError("console_migration.contract_not_ready")
    return str(route[0]), str(scheduler[0])


def _navigation_digest(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        """SELECT snapshot.disposition_sha256 FROM workspace_navigation_contract_heads head
           JOIN workspace_navigation_contract_snapshots snapshot
             USING(navigation_contract_snapshot_id,generation)
           ORDER BY head.updated_at DESC LIMIT 1"""
    ).fetchone()
    return _sha({"navigation": "empty"}) if row is None else str(row[0])


def _domain_status(
    domain_code: str,
    source_migration: str,
    counts: dict[str, sqlite3.Row],
) -> DomainStatus:
    row = counts.get(domain_code)
    return DomainStatus(
        domain_code=domain_code,
        source_migration=source_migration,
        migration_count=0 if row is None else int(row["migration_count"]),
        blocked_count=0 if row is None else int(row["blocked_count"] or 0),
        active_count=0 if row is None else int(row["active_count"] or 0),
    )


def _contract_head(connection: sqlite3.Connection) -> tuple[int, int, bool, str]:
    row = connection.execute(
        "SELECT * FROM console_ingress_contract_heads WHERE head_key='production'"
    ).fetchone()
    if row is None:
        raise ApiError("console_migration.contract_not_ready")
    return (
        int(row["contract_generation"]),
        int(row["route_deny_epoch"]),
        bool(row["legacy_write_fenced"]),
        str(row["status"]),
    )


def _head_generations(connection: sqlite3.Connection) -> tuple[int, int]:
    contract = connection.execute(
        "SELECT contract_generation FROM console_ingress_contract_heads WHERE head_key='production'"
    ).fetchone()
    registry = connection.execute(
        "SELECT generation FROM scheduler_registry_heads WHERE head_key='production'"
    ).fetchone()
    if contract is None or registry is None:
        raise ApiError("console_migration.contract_not_ready")
    return int(contract[0]), int(registry[0])


def _resolve_workspace(
    connection: sqlite3.Connection, config: AppConfig, workspace_ref: str
) -> sqlite3.Row:
    for row in connection.execute(
        """SELECT workspace_id,tenant_id FROM workspace_tenant_bindings
           WHERE status='active' ORDER BY workspace_id"""
    ).fetchall():
        if hmac.compare_digest(
            _workspace_ref(config, row["workspace_id"], row["tenant_id"]),
            workspace_ref,
        ):
            return row
    raise ApiError("console_migration.not_found")


def _migration(connection: sqlite3.Connection, migration_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM console_domain_migrations WHERE console_domain_migration_id=?",
        (migration_id,),
    ).fetchone()
    if row is None:
        raise ApiError("console_migration.not_found")
    return row


def _expect_revision(row: sqlite3.Row, expected: int) -> None:
    if int(row["revision"]) != expected:
        raise ApiError("console_migration.conflict")


def _update_migration_state(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    state: str,
    now: datetime,
    *,
    error_code: str | None = None,
) -> tuple[str, int]:
    revision = int(row["revision"]) + 1
    updated = connection.execute(
        """UPDATE console_domain_migrations
           SET state=?,revision=?,last_error_code=?,updated_at=?
           WHERE console_domain_migration_id=? AND revision=?""",
        (
            state,
            revision,
            error_code,
            _db_time(now),
            row["console_domain_migration_id"],
            row["revision"],
        ),
    ).rowcount
    if updated != 1:
        raise ApiError("console_migration.conflict")
    return state, revision


def _authenticate_write(
    connection: sqlite3.Connection,
    config: AppConfig,
    session_token: str,
    csrf_token: str,
) -> PlatformContext:
    return authenticate_platform_session_in_transaction(
        connection,
        config,
        session_token=session_token,
        csrf_token=csrf_token,
        touch=True,
    )


def _require(actor: PlatformContext, permission: str) -> None:
    if permission not in actor.permissions:
        raise ApiError("console_migration.permission_revoked")


def _require_role_permission(actor: PlatformContext, role: str, permission: str) -> None:
    _require(actor, permission)
    if role not in actor.roles:
        raise ApiError("console_migration.permission_revoked")


def _require_transition_authority(actor: PlatformContext, action: str) -> None:
    if action == "approve":
        _require_role_permission(
            actor, "platform_owner", "platform.console_migration.approve"
        )
    elif action == "security_review":
        _require_role_permission(
            actor,
            "platform_security",
            "platform.console_migration.security_review",
        )
    elif action == "execute":
        _require_role_permission(
            actor, "platform_ops", "platform.console_migration.execute"
        )
    else:
        _require_role_permission(
            actor, "platform_ops", "platform.console_migration.backfill"
        )


def _consume_step_up(
    connection: sqlite3.Connection,
    config: AppConfig,
    actor: PlatformContext,
    grant: str,
    *,
    action_code: str,
    resource_id: str,
    now: datetime,
) -> None:
    try:
        consume_global_step_up_grant_in_transaction(
            connection,
            config,
            account_id=actor.account_id,
            global_session_id=actor.global_session_id,
            grant=grant,
            action_code=action_code,
            resource_type="console_domain_migration",
            resource_id=resource_id,
            now=now,
        )
    except ApiError as exc:
        if exc.code.startswith("step_up."):
            raise ApiError("console_migration.permission_revoked") from None
        raise


def _permission_fingerprint(actor: PlatformContext) -> str:
    return _sha(
        {
            "account_id": actor.account_id,
            "factor_id": actor.factor_id,
            "operator_id": actor.operator_id,
            "operator_revision": actor.operator_revision,
            "factor_revision": actor.factor_revision,
            "global_session_id": actor.global_session_id,
            "platform_session_id": actor.platform_session_id,
            "roles": list(actor.roles),
            "permissions": list(actor.permissions),
        }
    )


def _workspace_ref(
    config: AppConfig, workspace_id: object, tenant_id: object
) -> str:
    return _secret_hmac(config, "workspace", f"{workspace_id}\x1f{tenant_id}")


def _enqueue_migration_job(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    migration_id: str,
    operation_generation: int,
    action: str,
    result_revision: int,
    target_generation: int | None,
    request_id: str,
    trace_id: str,
) -> None:
    try:
        payload = ConsoleDomainMigrationJobInput(
            migration_id=migration_id,
            operation_generation=operation_generation,
            result_revision=result_revision,
            action=action,
            target_generation=target_generation,
        )
        prepared = prepare_platform_job(config, type_code=JOB_TYPE, payload=payload)
        enqueue_prepared_platform_job_in_transaction(
            connection,
            config,
            prepared=prepared,
            priority=90 if action in {"cutover", "rollback"} else 50,
            source_code="console-migration",
            idempotency_key=(
                f"prd147-worker:{action}:{migration_id}:{result_revision}"
            ),
            request_id=request_id,
            trace_id=trace_id,
        )
    except (ApiError, ValueError):
        raise ApiError("console_migration.dependency_unavailable") from None


def _encode_cursor(
    config: AppConfig,
    *,
    offset: int,
    fingerprint: str,
    filters: dict[str, str | None],
    contract_generation: int,
) -> str:
    payload = {
        "contract_generation": contract_generation,
        "expires_at": int((_now() + _CURSOR_TTL).timestamp()),
        "filters": filters,
        "offset": offset,
        "permission_fingerprint": fingerprint,
        "version": 1,
    }
    encoded = base64.urlsafe_b64encode(_json(payload).encode()).decode().rstrip("=")
    signature = _secret_hmac(config, "cursor", encoded)
    return f"{encoded}.{signature}"


def _decode_cursor(
    config: AppConfig,
    cursor: str | None,
    *,
    fingerprint: str,
    filters: dict[str, str | None],
    contract_generation: int,
) -> int:
    if cursor is None:
        return 0
    try:
        encoded, signature = cursor.split(".", 1)
        if not hmac.compare_digest(signature, _secret_hmac(config, "cursor", encoded)):
            raise ValueError
        padding = "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded + padding))
        if (
            type(payload) is not dict
            or payload.get("version") != 1
            or payload.get("permission_fingerprint") != fingerprint
            or payload.get("filters") != filters
            or payload.get("contract_generation") != contract_generation
            or type(payload.get("expires_at")) is not int
            or payload["expires_at"] <= int(_now().timestamp())
            or type(payload.get("offset")) is not int
            or not 0 <= payload["offset"] <= 1_000_000
        ):
            raise ValueError
        return payload["offset"]
    except (ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError):
        raise ApiError("console_migration.invalid") from None


def _secret_hmac(config: AppConfig, purpose: str, value: str) -> str:
    return hmac.new(
        config.platform_session_secret.encode(),
        f"prd147/{purpose}/v1\x00{value}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _sha(value: object) -> str:
    return hashlib.sha256(_json(value).encode("ascii")).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("console_migration.dependency_unavailable") from None


def _rollback(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        connection.rollback()


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_time(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("invalid timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    elif parsed.utcoffset() is None:
        raise ValueError("invalid timestamp")
    return parsed.astimezone(UTC)


__all__ = [
    "execute_backfill_command",
    "execute_cutover_command",
    "execute_rollback_command",
    "get_console_migration_status",
]
