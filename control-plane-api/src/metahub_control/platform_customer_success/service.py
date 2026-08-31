"""Owner-qualified PRD-140 directory, plan, task, health, and renewal services."""

from __future__ import annotations

import base64
import binascii
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

from metahub_control.identity.security_service import (
    consume_global_step_up_grant_in_transaction,
)
from metahub_control.platform_access import service as platform_access_service
from metahub_control.platform_access.service import authenticate_platform_session_in_transaction

from .models import (
    AccountDetail,
    ContactSummary,
    CustomerAccountListResponse,
    CustomerHealthResponse,
    CustomerSuccessAccountSummary,
    CustomerSuccessHealthResponse,
    CustomerSuccessRenewalSummary,
    CustomerTaskCommand,
    CustomerTaskResponse,
    CustomerTaskSummary,
    FinanceAccountSummary,
    FinanceRenewalSummary,
    HealthFactor,
    OwnerAccountSummary,
    OwnerRenewalSummary,
    QbrSummary,
    RenewalPipelineResponse,
    SuccessPlanCommand,
    SuccessPlanResponse,
    SuccessPlanSummary,
    SupportAccountSummary,
    SupportCustomerHealthResponse,
    TaskBatchSummary,
)
from .permissions import (
    BATCH_APPROVE,
    CONTACT_READ,
    PLAN_MANAGE,
    RENEWAL_READ,
    TASK_MANAGE,
    account_scope,
    health_scope,
    require,
)

_ACCOUNT = re.compile(r"^csa_[0-9a-f]{32}$")
_CURSOR_DOMAIN = b"prd140/platform/customer-success-cursor/v1"
_IDEMPOTENCY_DOMAIN = b"prd140/platform/customer-success-idempotency/v1"
_ACCOUNT_FILTERS = {
    "customer_account_id",
    "workspace_id",
    "slug",
    "kind",
    "region",
    "operational_status",
    "activation",
    "adoption",
    "health_risk",
    "renewal_status",
    "view",
}
_RENEWAL_FILTERS = {"status", "risk", "renewal_from", "renewal_to", "region", "customer_account_id"}


def list_customer_accounts(
    config: AppConfig,
    *,
    session_token: str,
    limit: int,
    cursor: str | None,
    filters: dict[str, str],
    request_id: str,
    trace_id: str,
) -> CustomerAccountListResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = _actor(connection, config, session_token, None)
        scope = account_scope(actor)
        _require_schema(connection)
        normalized = _account_filters(filters)
        view = normalized.pop("view", "directory")
        if view not in {"directory", "detail", "contact"}:
            raise ApiError("platform.request_invalid")
        account_id = normalized.get("customer_account_id")
        if view in {"detail", "contact"} and (account_id is None or len(normalized) != 1):
            raise ApiError("platform.request_invalid")
        if view == "detail" and scope not in {"customer_success", "owner"}:
            raise ApiError("platform.permission_denied")
        if view == "contact":
            require(actor, CONTACT_READ)
        digest = _digest({"filters": normalized, "scope": scope, "view": view})
        boundary = _decode_cursor(config, actor, cursor, digest, "account") if cursor else None
        clauses: list[str] = []
        values: list[object] = []
        _append_account_filters(normalized, clauses, values)
        if boundary is not None:
            clauses.append(
                "(account.updated_at<? OR(account.updated_at=? AND account.customer_account_id>?))"
            )
            values.extend((boundary[0], boundary[0], boundary[1]))
        where = " AND ".join(clauses) if clauses else "1=1"
        rows = connection.execute(
            f"""SELECT account.*,
                (SELECT count(*) FROM platform_customer_success_plans plan
                  WHERE plan.customer_account_id=account.customer_account_id) plan_count,
                (SELECT count(*) FROM platform_customer_tasks task
                  WHERE task.customer_account_id=account.customer_account_id
                    AND task.status IN('open','in_progress')) open_task_count
                FROM platform_customer_account_projections account
                WHERE {where}
                ORDER BY account.updated_at DESC,account.customer_account_id LIMIT ?""",
            (*values, limit + 1),
        ).fetchall()
        visible = rows[:limit]
        has_more = len(rows) > limit
        next_cursor = None
        if has_more:
            last = visible[-1]
            next_cursor = _encode_cursor(
                config,
                actor,
                digest,
                "account",
                str(last["updated_at"]),
                str(last["customer_account_id"]),
            )
        items = tuple(_account_summary(row, scope) for row in visible)
        detail = (
            _account_detail(connection, account_id) if view == "detail" and account_id else None
        )
        contacts = None
        if view == "contact" and account_id:
            contacts = _contacts(connection, account_id)
            _audit(
                connection,
                actor=actor,
                action="platform.customer_contact_read",
                object_type="customer_account",
                object_id=account_id,
                reason="customer_success_contact_view",
                request_id=request_id,
                trace_id=trace_id,
                after_hash=_digest([contact.customer_contact_id for contact in contacts]),
                now=_utcnow(),
            )
        if view != "directory" and not items:
            raise ApiError("platform.workspace_not_found")
        response = CustomerAccountListResponse(
            items=items,
            limit=limit,
            has_more=has_more,
            next_cursor=next_cursor,
            detail=detail,
            contacts=contacts,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def get_customer_health(
    config: AppConfig,
    *,
    session_token: str,
    customer_account_id: str,
    request_id: str,
    trace_id: str,
) -> CustomerHealthResponse:
    if _ACCOUNT.fullmatch(customer_account_id) is None:
        raise ApiError("platform.workspace_not_found")
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = _actor(connection, config, session_token, None)
        scope = health_scope(actor)
        _require_schema(connection)
        row = connection.execute(
            """SELECT snapshot.*,head.source_sequence,head.source_fingerprint head_fingerprint,
                      head.projection_sha256 head_projection_sha256,
                      head.freshness_watermark,head.projected_at head_projected_at
                 FROM platform_customer_health_heads head
                 JOIN platform_customer_health_snapshots snapshot
                   ON snapshot.customer_account_id=head.customer_account_id
                  AND snapshot.customer_health_snapshot_id=head.customer_health_snapshot_id
                  AND snapshot.customer_health_snapshot_sequence=head.source_sequence
                WHERE head.customer_account_id=?""",
            (customer_account_id,),
        ).fetchone()
        if row is None:
            if (
                connection.execute(
                    """SELECT 1 FROM platform_customer_account_projections
                        WHERE customer_account_id=?""",
                    (customer_account_id,),
                ).fetchone()
                is None
            ):
                raise ApiError("platform.workspace_not_found")
            raise ApiError("platform.unavailable")
        now = _utcnow()
        evaluated = _parse_time(row["evaluated_at"])
        if (
            evaluated > now
            or row["source_fingerprint"] != row["head_fingerprint"]
            or row["health_projection_sha256"] != row["head_projection_sha256"]
        ):
            raise ApiError("platform.unavailable")
        stale = (now - evaluated).total_seconds() > 7200
        factors = _health_factors(row, support_only=scope == "support")
        risk_codes, action_codes = _health_codes(row, support_only=scope == "support")
        common = {
            "customer_account_id": customer_account_id,
            "formula_version": row["formula_version"],
            "risk_level": row["risk_level"],
            "factors": factors,
            "risk_codes": risk_codes,
            "action_codes": action_codes,
            "freshness": "stale" if stale else "fresh",
            "source_sequence": int(row["source_sequence"]),
            "evaluated_at": evaluated,
            "request_id": request_id,
            "trace_id": trace_id,
        }
        if scope == "support":
            response = SupportCustomerHealthResponse(**common)
        else:
            response = CustomerSuccessHealthResponse(
                **common,
                window_start=_parse_time(row["window_start"]),
                window_end=_parse_time(row["window_end"]),
                comparison_start=_parse_time(row["comparison_start"]),
                comparison_end=_parse_time(row["comparison_end"]),
                score_bps=int(row["score_bps"]),
            )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def apply_success_plan_command(
    config: AppConfig,
    command: SuccessPlanCommand,
    *,
    session_token: str,
    csrf_token: str,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> SuccessPlanResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _actor(connection, config, session_token, csrf_token)
        require(actor, PLAN_MANAGE)
        _require_schema(connection)
        action = f"plan.{command.operation}"
        request_hash = _digest({"action": action, "command": command.model_dump(mode="json")})
        replay = _replay(
            connection, config, actor, action, idempotency_key, request_hash, SuccessPlanResponse
        )
        if replay is not None:
            connection.commit()
            return replay.model_copy(update={"idempotent_replay": True})
        account = _account_row(connection, command.customer_account_id)
        now = _utcnow()
        if command.operation == "create":
            _require_current_health(account, command.source_health_sequence)
            plan_id = _id("csp_")
            stamp = _db_time(now)
            connection.execute(
                """INSERT INTO platform_customer_success_plans(
                  customer_success_plan_id,customer_account_id,workspace_id,tenant_id,
                  objective_code,starts_on,ends_on,target_health_bps,target_activation_steps,
                  target_adoption_bps,source_health_sequence,status,revision,
                  created_by_operator_id,updated_by_operator_id,created_at,updated_at,completed_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,'draft',1,?,?,?,?,NULL)""",
                (
                    plan_id,
                    account["customer_account_id"],
                    account["workspace_id"],
                    account["tenant_id"],
                    command.objective_code,
                    command.starts_on.isoformat(),
                    command.ends_on.isoformat(),
                    command.target_health_bps,
                    command.target_activation_steps,
                    command.target_adoption_bps,
                    command.source_health_sequence,
                    actor.operator_id,
                    actor.operator_id,
                    stamp,
                    stamp,
                ),
            )
        else:
            plan_id = str(command.customer_success_plan_id)
            plan = _plan_row(connection, plan_id, command.customer_account_id)
            if int(plan["revision"]) != command.expected_revision:
                raise ApiError("platform.revision_conflict")
            _require_plan_transition(plan["status"], command.operation)
            target = {
                "activate": "active",
                "revise_targets": plan["status"],
                "complete": "completed",
                "cancel": "canceled",
            }[command.operation]
            if command.operation == "complete":
                open_required = connection.execute(
                    """SELECT count(*) FROM platform_customer_tasks
                        WHERE customer_success_plan_id=? AND required=1
                          AND status!='completed'""",
                    (plan_id,),
                ).fetchone()[0]
                current_sequence = account["latest_health_sequence"]
                if open_required or current_sequence != plan["source_health_sequence"]:
                    raise ApiError("platform.revision_conflict")
            stamp = _next_stamp(plan["updated_at"], now)
            if command.operation == "revise_targets":
                _require_current_health(account, command.source_health_sequence)
                connection.execute(
                    """UPDATE platform_customer_success_plans
                          SET ends_on=?,target_health_bps=?,target_activation_steps=?,
                              target_adoption_bps=?,source_health_sequence=?,revision=revision+1,
                              updated_by_operator_id=?,updated_at=?
                        WHERE customer_success_plan_id=?""",
                    (
                        command.ends_on.isoformat(),
                        command.target_health_bps,
                        command.target_activation_steps,
                        command.target_adoption_bps,
                        command.source_health_sequence,
                        actor.operator_id,
                        stamp,
                        plan_id,
                    ),
                )
            else:
                completed = stamp if target == "completed" else None
                connection.execute(
                    """UPDATE platform_customer_success_plans
                          SET status=?,revision=revision+1,updated_by_operator_id=?,
                              updated_at=?,completed_at=?
                        WHERE customer_success_plan_id=?""",
                    (target, actor.operator_id, stamp, completed, plan_id),
                )
            if target == "completed":
                changed = _plan_row(connection, plan_id, command.customer_account_id)
                _event(
                    connection,
                    account,
                    event_type="success_plan.completed",
                    object_kind="success_plan",
                    object_id=plan_id,
                    revision=int(changed["revision"]),
                    reason="plan_completed",
                    safe_count=0,
                    occurred_at=stamp,
                )
        row = _plan_row(connection, plan_id, command.customer_account_id)
        response = SuccessPlanResponse(
            plan=_plan_summary(row), request_id=request_id, trace_id=trace_id
        )
        _record_command(
            connection,
            config,
            actor,
            account,
            action,
            idempotency_key,
            request_hash,
            command.expected_revision,
            "success_plan",
            plan_id,
            int(row["revision"]),
            response,
            request_id,
            trace_id,
            now,
        )
        _audit(
            connection,
            actor=actor,
            action=f"platform.customer_success_{command.operation}",
            object_type="customer_success_plan",
            object_id=plan_id,
            reason="customer_success_command",
            request_id=request_id,
            trace_id=trace_id,
            after_hash=request_hash,
            now=now,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def apply_customer_task_command(
    config: AppConfig,
    command: CustomerTaskCommand,
    *,
    session_token: str,
    csrf_token: str,
    step_up_grant: str | None,
    idempotency_key: str,
    request_id: str,
    trace_id: str,
) -> CustomerTaskResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = _actor(connection, config, session_token, csrf_token)
        require(actor, TASK_MANAGE)
        _require_schema(connection)
        action = f"task.{command.operation}"
        request_hash = _digest({"action": action, "command": command.model_dump(mode="json")})
        replay = _replay(
            connection, config, actor, action, idempotency_key, request_hash, CustomerTaskResponse
        )
        if replay is not None:
            connection.commit()
            return replay.model_copy(update={"idempotent_replay": True})
        now = _utcnow()
        task_row = None
        batch_row = None
        account = None
        if command.operation == "create_one":
            account = _account_row(connection, str(command.customer_account_id))
            _validate_task_links(connection, account, command)
            _require_active_operator(connection, str(command.assignee_operator_id))
            task_id = _insert_task(connection, account, actor, command, now=now)
            task_row = _task_row(connection, task_id)
            result_kind, result_id, result_revision = "customer_task", task_id, 1
        elif command.operation == "transition_one":
            task_row = _task_row(connection, str(command.customer_task_id))
            account = _account_row(connection, str(task_row["customer_account_id"]))
            if int(task_row["revision"]) != command.expected_revision:
                raise ApiError("platform.revision_conflict")
            _require_task_transition(task_row["status"], str(command.target_status))
            stamp = _next_stamp(task_row["updated_at"], now)
            completed = stamp if command.target_status == "completed" else None
            connection.execute(
                """UPDATE platform_customer_tasks
                      SET status=?,revision=revision+1,updated_by_operator_id=?,
                          updated_at=?,completed_at=? WHERE customer_task_id=?""",
                (
                    command.target_status,
                    actor.operator_id,
                    stamp,
                    completed,
                    command.customer_task_id,
                ),
            )
            task_row = _task_row(connection, str(command.customer_task_id))
            result_kind, result_id, result_revision = (
                "customer_task",
                str(command.customer_task_id),
                int(task_row["revision"]),
            )
        elif command.operation == "prepare_batch":
            require(actor, TASK_MANAGE)
            accounts = tuple(sorted(command.customer_account_ids or ()))
            rows = [_account_row(connection, account_id) for account_id in accounts]
            _require_active_operator(connection, str(command.assignee_operator_id))
            account = rows[0]
            manifest = json.dumps(accounts, separators=(",", ":"), ensure_ascii=True)
            batch_id = _id("ctb_")
            stamp = _db_time(now)
            connection.execute(
                """INSERT INTO platform_customer_task_batches(
                  customer_task_batch_id,prepared_by_operator_id,approved_by_operator_id,
                  account_manifest_json,manifest_sha256,task_type,priority,reason_code,
                  assignee_operator_id,due_at,status,requested_permission_revision,
                  approval_generation,result_count,revision,created_at,updated_at)
                VALUES(?, ?,NULL,?,?,?,?,?,?,?,'awaiting_approval',?,1,0,1,?,?)""",
                (
                    batch_id,
                    actor.operator_id,
                    manifest,
                    _sha(manifest),
                    command.task_type,
                    command.priority,
                    command.reason_code,
                    command.assignee_operator_id,
                    _db_time(command.due_at),
                    actor.operator_revision,
                    stamp,
                    stamp,
                ),
            )
            batch_row = _batch_row(connection, batch_id)
            result_kind, result_id, result_revision = "task_batch", batch_id, 1
        else:
            require(actor, BATCH_APPROVE)
            if not step_up_grant:
                raise ApiError("platform.step_up_required")
            batch_id = str(command.customer_task_batch_id)
            batch_row = _batch_row(connection, batch_id)
            if (
                int(batch_row["revision"]) != command.expected_revision
                or batch_row["status"] != "awaiting_approval"
                or batch_row["prepared_by_operator_id"] == actor.operator_id
            ):
                raise ApiError("platform.revision_conflict")
            _require_batch_requester(connection, batch_row)
            consume_global_step_up_grant_in_transaction(
                connection,
                config,
                account_id=actor.account_id,
                global_session_id=actor.global_session_id,
                grant=step_up_grant,
                action_code="platform.customer_success.batch.approve",
                resource_type="customer_task_batch",
                resource_id=batch_id,
                now=now,
            )
            manifest = tuple(json.loads(batch_row["account_manifest_json"]))
            if (
                not manifest
                or len(manifest) > 100
                or manifest != tuple(sorted(set(manifest)))
                or any(_ACCOUNT.fullmatch(value) is None for value in manifest)
                or _sha(json.dumps(manifest, separators=(",", ":"), ensure_ascii=True))
                != batch_row["manifest_sha256"]
            ):
                raise ApiError("platform.unavailable")
            rows = [_account_row(connection, account_id) for account_id in manifest]
            account = rows[0]
            count = 0
            if command.decision == "approved":
                for row in rows:
                    _insert_batch_task(connection, row, actor, batch_row, now)
                    count += 1
            stamp = _next_stamp(batch_row["updated_at"], now)
            status = "applied" if command.decision == "approved" else "rejected"
            connection.execute(
                """UPDATE platform_customer_task_batches SET approved_by_operator_id=?,
                      status=?,approval_generation=approval_generation+1,result_count=?,
                      revision=revision+1,updated_at=? WHERE customer_task_batch_id=?""",
                (actor.operator_id, status, count, stamp, batch_id),
            )
            connection.execute(
                """INSERT INTO platform_customer_task_batch_approvals(
                  customer_task_batch_approval_id,customer_task_batch_id,
                  approver_operator_id,decision,reason_code,permission_revision,
                  manifest_sha256,decided_at) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    _id("cta_"),
                    batch_id,
                    actor.operator_id,
                    command.decision,
                    command.reason_code,
                    actor.operator_revision,
                    batch_row["manifest_sha256"],
                    stamp,
                ),
            )
            batch_row = _batch_row(connection, batch_id)
            result_kind, result_id, result_revision = (
                "task_batch",
                batch_id,
                int(batch_row["revision"]),
            )
        response = CustomerTaskResponse(
            task=None if task_row is None else _task_summary(task_row),
            batch=None if batch_row is None else _batch_summary(batch_row),
            request_id=request_id,
            trace_id=trace_id,
        )
        assert account is not None
        _record_command(
            connection,
            config,
            actor,
            account,
            action,
            idempotency_key,
            request_hash,
            command.expected_revision,
            result_kind,
            result_id,
            result_revision,
            response,
            request_id,
            trace_id,
            now,
        )
        _audit(
            connection,
            actor=actor,
            action=f"platform.customer_{command.operation}",
            object_type=result_kind,
            object_id=result_id,
            reason="customer_task_command",
            request_id=request_id,
            trace_id=trace_id,
            after_hash=request_hash,
            now=now,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError, json.JSONDecodeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def list_renewal_pipeline(
    config: AppConfig,
    *,
    session_token: str,
    limit: int,
    cursor: str | None,
    filters: dict[str, str],
    request_id: str,
    trace_id: str,
) -> RenewalPipelineResponse:
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = _actor(connection, config, session_token, None)
        require(actor, RENEWAL_READ)
        scope = account_scope(actor)
        _require_schema(connection)
        normalized = _renewal_filters(filters)
        digest = _digest({"filters": normalized, "scope": scope})
        boundary = _decode_cursor(config, actor, cursor, digest, "renewal") if cursor else None
        clauses: list[str] = []
        values: list[object] = []
        mapping = {
            "status": "renewal.status",
            "risk": "renewal.health_risk_code",
            "region": "account.region_code",
            "customer_account_id": "renewal.customer_account_id",
        }
        for key, column in mapping.items():
            if key in normalized:
                clauses.append(f"{column}=?")
                values.append(normalized[key])
        if "renewal_from" in normalized:
            clauses.append("renewal.renewal_at>=?")
            values.append(normalized["renewal_from"])
        if "renewal_to" in normalized:
            clauses.append("renewal.renewal_at<=?")
            values.append(normalized["renewal_to"])
        if boundary:
            clauses.append(
                "(renewal.renewal_at>? OR(renewal.renewal_at=? AND "
                "renewal.renewal_opportunity_id>?))"
            )
            values.extend((boundary[0], boundary[0], boundary[1]))
        where = " AND ".join(clauses) if clauses else "1=1"
        rows = connection.execute(
            f"""SELECT renewal.*,account.display_name,
                (SELECT count(*) FROM platform_customer_success_plans plan
                  WHERE plan.customer_account_id=renewal.customer_account_id
                    AND plan.status IN('draft','active','at_risk')) open_plan_count,
                (SELECT count(*) FROM platform_customer_tasks task
                  WHERE task.customer_account_id=renewal.customer_account_id
                    AND task.status IN('open','in_progress')) open_task_count
              FROM platform_renewal_opportunities renewal
              JOIN platform_customer_account_projections account
                ON account.customer_account_id=renewal.customer_account_id
              WHERE {where}
              ORDER BY renewal.renewal_at,renewal.renewal_opportunity_id LIMIT ?""",
            (*values, limit + 1),
        ).fetchall()
        visible = rows[:limit]
        has_more = len(rows) > limit
        next_cursor = None
        if has_more:
            last = visible[-1]
            next_cursor = _encode_cursor(
                config,
                actor,
                digest,
                "renewal",
                str(last["renewal_at"]),
                str(last["renewal_opportunity_id"]),
            )
        response = RenewalPipelineResponse(
            items=tuple(_renewal_summary(row, scope) for row in visible),
            limit=limit,
            has_more=has_more,
            next_cursor=next_cursor,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return response
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, DbError, ValueError, TypeError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def _account_filters(filters: dict[str, str]) -> dict[str, str]:
    if not set(filters) <= _ACCOUNT_FILTERS or any(value == "" for value in filters.values()):
        raise ApiError("platform.request_invalid")
    values = dict(sorted(filters.items()))
    allowed = {
        "kind": {"personal", "organization"},
        "operational_status": {
            "active",
            "suspended",
            "deletion_pending",
            "deleted",
            "migration_conflict",
        },
        "activation": {"not_started", "in_progress", "activated"},
        "adoption": {"unknown", "low", "medium", "high"},
        "health_risk": {"unknown", "healthy", "watch", "at_risk", "critical"},
    }
    if any(key in values and values[key] not in catalog for key, catalog in allowed.items()):
        raise ApiError("platform.request_invalid")
    if (
        "customer_account_id" in values
        and _ACCOUNT.fullmatch(values["customer_account_id"]) is None
    ):
        raise ApiError("platform.request_invalid")
    return values


def _append_account_filters(
    filters: dict[str, str], clauses: list[str], values: list[object]
) -> None:
    mapping = {
        "customer_account_id": "customer_account_id",
        "workspace_id": "workspace_id",
        "slug": "slug",
        "kind": "workspace_kind",
        "region": "region_code",
        "operational_status": "operational_status",
        "activation": "activation_phase",
        "adoption": "adoption_band",
        "health_risk": "health_risk",
        "renewal_status": "subscription_status",
    }
    for key, column in mapping.items():
        if key in filters:
            clauses.append(f"account.{column}=?")
            values.append(filters[key])


def _renewal_filters(filters: dict[str, str]) -> dict[str, str]:
    if not set(filters) <= _RENEWAL_FILTERS or any(value == "" for value in filters.values()):
        raise ApiError("platform.request_invalid")
    for key in ("renewal_from", "renewal_to"):
        if key in filters:
            parsed = _parse_time(filters[key])
            if parsed.utcoffset() is None or not filters[key].endswith("Z"):
                raise ApiError("platform.request_invalid")
    return dict(sorted(filters.items()))


def _account_summary(row: sqlite3.Row, scope: str):
    if scope == "support":
        return SupportAccountSummary(
            customer_account_id=row["customer_account_id"],
            workspace_id=row["workspace_id"],
            operational_status=row["operational_status"],
            health_risk=row["health_risk"],
            open_support_ticket_count=int(row["open_support_ticket_count"]),
            breached_sla_count=int(row["breached_sla_count"]),
            projection_updated_at=_parse_time(row["updated_at"]),
        )
    if scope == "finance":
        return FinanceAccountSummary(
            customer_account_id=row["customer_account_id"],
            workspace_id=row["workspace_id"],
            region=row["region_code"],
            subscription_id=row["subscription_id"],
            plan_version_id=row["plan_version_id"],
            subscription_status=row["subscription_status"],
            renewal_at=_optional_time(row["renewal_at"]),
            projection_updated_at=_parse_time(row["updated_at"]),
        )
    if scope == "owner":
        return OwnerAccountSummary(
            customer_account_id=row["customer_account_id"],
            workspace_id=row["workspace_id"],
            display_name=row["display_name"],
            slug=row["slug"],
            kind=row["workspace_kind"],
            region=row["region_code"],
            operational_status=row["operational_status"],
            activation=row["activation_phase"],
            activation_completed_steps=int(row["activation_completed_steps"]),
            adoption=row["adoption_band"],
            health_risk=row["health_risk"],
            subscription_id=row["subscription_id"],
            plan_version_id=row["plan_version_id"],
            subscription_status=row["subscription_status"],
            renewal_at=_optional_time(row["renewal_at"]),
            open_support_ticket_count=int(row["open_support_ticket_count"]),
            breached_sla_count=int(row["breached_sla_count"]),
            plan_count=int(row["plan_count"]),
            open_task_count=int(row["open_task_count"]),
            revision=int(row["revision"]),
            projection_updated_at=_parse_time(row["updated_at"]),
        )
    return CustomerSuccessAccountSummary(
        customer_account_id=row["customer_account_id"],
        workspace_id=row["workspace_id"],
        display_name=row["display_name"],
        slug=row["slug"],
        kind=row["workspace_kind"],
        region=row["region_code"],
        operational_status=row["operational_status"],
        activation=row["activation_phase"],
        activation_completed_steps=int(row["activation_completed_steps"]),
        adoption=row["adoption_band"],
        health_risk=row["health_risk"],
        renewal_status=row["subscription_status"],
        open_support_ticket_count=int(row["open_support_ticket_count"]),
        plan_count=int(row["plan_count"]),
        open_task_count=int(row["open_task_count"]),
        revision=int(row["revision"]),
        projection_updated_at=_parse_time(row["updated_at"]),
    )


def _account_detail(connection: sqlite3.Connection, account_id: str) -> AccountDetail:
    plans = connection.execute(
        """SELECT * FROM platform_customer_success_plans WHERE customer_account_id=?
            ORDER BY updated_at DESC,customer_success_plan_id DESC LIMIT 21""",
        (account_id,),
    ).fetchall()
    tasks = connection.execute(
        """SELECT * FROM platform_customer_tasks WHERE customer_account_id=?
            ORDER BY CASE WHEN status IN('open','in_progress') THEN 0 ELSE 1 END,
              updated_at DESC,customer_task_id DESC LIMIT 51""",
        (account_id,),
    ).fetchall()
    qbr = connection.execute(
        """SELECT * FROM platform_qbr_summaries WHERE customer_account_id=?
            ORDER BY quarter DESC,generated_at DESC LIMIT 9""",
        (account_id,),
    ).fetchall()
    operations = int(
        connection.execute(
            "SELECT count(*) FROM platform_customer_success_commands WHERE customer_account_id=?",
            (account_id,),
        ).fetchone()[0]
    )
    return AccountDetail(
        plans=tuple(_plan_summary(row) for row in plans[:20]),
        plans_count=min(len(plans), 20),
        plans_truncated=len(plans) > 20,
        tasks=tuple(_task_summary(row) for row in tasks[:50]),
        tasks_count=min(len(tasks), 50),
        tasks_truncated=len(tasks) > 50,
        qbr_summaries=tuple(_qbr_summary(row) for row in qbr[:8]),
        qbr_count=min(len(qbr), 8),
        qbr_truncated=len(qbr) > 8,
        operation_count=min(operations, 50),
        operations_truncated=operations > 50,
    )


def _contacts(connection: sqlite3.Connection, account_id: str) -> tuple[ContactSummary, ...]:
    rows = connection.execute(
        """SELECT contact.*,global_account.display_name,global_account.email_normalized,
                  global_account.phone_e164,global_account.status account_status,
                  membership.status membership_status,
                  membership.revision current_membership_revision
             FROM platform_customer_contacts contact
             JOIN global_accounts global_account ON global_account.account_id=contact.account_id
             JOIN workspace_memberships membership
               ON membership.workspace_membership_id=contact.workspace_membership_id
              AND membership.workspace_id=contact.workspace_id
            WHERE contact.customer_account_id=?
            ORDER BY contact.contact_role,contact.customer_contact_id""",
        (account_id,),
    ).fetchall()
    result = []
    for row in rows:
        if (
            row["status"] != "active"
            or row["account_status"] != "active"
            or row["membership_status"] != "active"
            or int(row["current_membership_revision"]) != int(row["membership_revision"])
        ):
            continue
        result.append(
            ContactSummary(
                customer_contact_id=row["customer_contact_id"],
                contact_role=row["contact_role"],
                status=row["status"],
                account_id=row["account_id"],
                display_name=row["display_name"],
                email=row["email_normalized"],
                phone=row["phone_e164"],
                membership_revision=int(row["membership_revision"]),
                notification_preference_revision=int(row["notification_preference_revision"]),
                preference_summary=tuple(json.loads(row["preference_summary_json"])),
            )
        )
    return tuple(result)


def _health_factors(row: sqlite3.Row, *, support_only: bool) -> tuple[HealthFactor, ...]:
    specs = (
        (
            "usage_trend",
            "usage_trend_score_bps",
            "usage_trend_contribution_bps",
            "current_request_count",
            "previous_request_count",
        ),
        (
            "reliability",
            "reliability_score_bps",
            "reliability_contribution_bps",
            "successful_request_count",
            "current_request_count",
        ),
        (
            "cost_anomaly_health",
            "cost_anomaly_health_score_bps",
            "cost_anomaly_health_contribution_bps",
            "unresolved_cost_anomaly_count",
            "critical_cost_anomaly_count",
        ),
        (
            "budget_readiness",
            "budget_readiness_score_bps",
            "budget_readiness_contribution_bps",
            "active_budget_rule_count",
            None,
        ),
        (
            "team_activity",
            "team_activity_score_bps",
            "team_activity_contribution_bps",
            "recently_active_user_count",
            "eligible_user_count",
        ),
        (
            "ticket_health",
            "ticket_health_score_bps",
            "ticket_health_contribution_bps",
            "open_support_ticket_count",
            "sla_breached_count",
        ),
    )
    selected = specs[-1:] if support_only else specs
    return tuple(
        HealthFactor(
            code=code,
            score_bps=None if row[score] is None else int(row[score]),
            contribution_bps=int(row[contribution]),
            current_value=int(row[current]),
            comparison_value=None if comparison is None else int(row[comparison]),
        )
        for code, score, contribution, current, comparison in selected
    )


def _health_codes(
    row: sqlite3.Row, *, support_only: bool
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    codes: list[str] = []
    actions: list[str] = []
    if not support_only:
        for flag, code, action in (
            ("usage_declining", "usage_declining", "review_adoption"),
            ("high_error_rate", "high_error_rate", "review_reliability"),
            ("cost_anomaly_open", "cost_anomaly_open", "review_cost"),
            ("budget_missing", "budget_missing", "review_budget"),
            ("team_inactive", "team_inactive", "review_activation"),
        ):
            if int(row[flag]):
                codes.append(code)
                actions.append(action)
    if int(row["sla_breached_count"]):
        codes.append("sla_breached")
        actions.append("review_support")
    elif int(row["sla_at_risk_count"]):
        codes.append("sla_at_risk")
        actions.append("review_support")
    return tuple(codes), tuple(dict.fromkeys(actions))


def _plan_summary(row: sqlite3.Row) -> SuccessPlanSummary:
    return SuccessPlanSummary(
        customer_success_plan_id=row["customer_success_plan_id"],
        customer_account_id=row["customer_account_id"],
        objective_code=row["objective_code"],
        starts_on=datetime.fromisoformat(row["starts_on"]).date(),
        ends_on=datetime.fromisoformat(row["ends_on"]).date(),
        target_health_bps=int(row["target_health_bps"]),
        target_activation_steps=int(row["target_activation_steps"]),
        target_adoption_bps=int(row["target_adoption_bps"]),
        source_health_sequence=None
        if row["source_health_sequence"] is None
        else int(row["source_health_sequence"]),
        status=row["status"],
        revision=int(row["revision"]),
        created_by_operator_id=row["created_by_operator_id"],
        updated_by_operator_id=row["updated_by_operator_id"],
        created_at=_parse_time(row["created_at"]),
        updated_at=_parse_time(row["updated_at"]),
        completed_at=_optional_time(row["completed_at"]),
    )


def _task_summary(row: sqlite3.Row) -> CustomerTaskSummary:
    return CustomerTaskSummary(
        customer_task_id=row["customer_task_id"],
        customer_account_id=row["customer_account_id"],
        customer_success_plan_id=row["customer_success_plan_id"],
        renewal_opportunity_id=row["renewal_opportunity_id"],
        task_type=row["task_type"],
        priority=row["priority"],
        reason_code=row["reason_code"],
        assignee_operator_id=row["assignee_operator_id"],
        due_at=_parse_time(row["due_at"]),
        required=bool(row["required"]),
        status=row["status"],
        due_generation=int(row["due_generation"]),
        revision=int(row["revision"]),
        created_at=_parse_time(row["created_at"]),
        updated_at=_parse_time(row["updated_at"]),
        completed_at=_optional_time(row["completed_at"]),
    )


def _batch_summary(row: sqlite3.Row) -> TaskBatchSummary:
    return TaskBatchSummary(
        customer_task_batch_id=row["customer_task_batch_id"],
        prepared_by_operator_id=row["prepared_by_operator_id"],
        approved_by_operator_id=row["approved_by_operator_id"],
        account_count=len(json.loads(row["account_manifest_json"])),
        manifest_sha256=row["manifest_sha256"],
        task_type=row["task_type"],
        priority=row["priority"],
        reason_code=row["reason_code"],
        assignee_operator_id=row["assignee_operator_id"],
        due_at=_parse_time(row["due_at"]),
        status=row["status"],
        result_count=int(row["result_count"]),
        revision=int(row["revision"]),
        created_at=_parse_time(row["created_at"]),
        updated_at=_parse_time(row["updated_at"]),
    )


def _qbr_summary(row: sqlite3.Row) -> QbrSummary:
    return QbrSummary(
        qbr_summary_id=row["qbr_summary_id"],
        quarter=row["quarter"],
        health_sequence=None if row["health_sequence"] is None else int(row["health_sequence"]),
        adoption_bps=None if row["adoption_bps"] is None else int(row["adoption_bps"]),
        support_ticket_count=int(row["support_ticket_count"]),
        renewal_risk_count=int(row["renewal_risk_count"]),
        outcome_code=row["outcome_code"],
        action_code=row["action_code"],
        source_digest=row["source_digest"],
        generated_at=_parse_time(row["generated_at"]),
    )


def _renewal_summary(row: sqlite3.Row, scope: str):
    common = {
        "renewal_opportunity_id": row["renewal_opportunity_id"],
        "customer_account_id": row["customer_account_id"],
        "workspace_id": row["workspace_id"],
        "renewal_at": _parse_time(row["renewal_at"]),
        "payment_risk_code": row["payment_risk_code"],
        "invoice_status": row["invoice_status"],
        "status": row["status"],
        "revision": int(row["revision"]),
    }
    if scope == "finance":
        return FinanceRenewalSummary(
            **common,
            subscription_id=row["subscription_id"],
            plan_version_id=row["plan_version_id"],
            currency=row["currency"],
            amount_minor=None if row["amount_minor"] is None else int(row["amount_minor"]),
            amount_status=row["amount_status"],
        )
    customer = {
        **common,
        "display_name": row["display_name"],
        "health_risk_code": row["health_risk_code"],
        "adoption_risk_code": row["adoption_risk_code"],
        "open_plan_count": int(row["open_plan_count"]),
        "open_task_count": int(row["open_task_count"]),
    }
    if scope == "owner":
        return OwnerRenewalSummary(
            **customer,
            subscription_id=row["subscription_id"],
            plan_version_id=row["plan_version_id"],
            currency=row["currency"],
            amount_minor=None if row["amount_minor"] is None else int(row["amount_minor"]),
            amount_status=row["amount_status"],
        )
    return CustomerSuccessRenewalSummary(**customer)


def _require_current_health(account, source_sequence) -> None:
    if (
        account["latest_health_sequence"] is None
        or int(account["latest_health_sequence"]) != source_sequence
    ):
        raise ApiError("platform.revision_conflict")


def _require_plan_transition(status: str, operation: str) -> None:
    allowed = {
        "activate": {"draft"},
        "revise_targets": {"draft", "active", "at_risk"},
        "complete": {"active", "at_risk"},
        "cancel": {"draft", "active", "at_risk"},
    }
    if status not in allowed[operation]:
        raise ApiError("platform.revision_conflict")


def _require_task_transition(status: str, target: str) -> None:
    allowed = {
        "open": {"in_progress", "completed", "canceled"},
        "in_progress": {"open", "completed", "canceled"},
        "completed": set(),
        "canceled": set(),
    }
    if target not in allowed[status]:
        raise ApiError("platform.revision_conflict")


def _require_active_operator(connection, operator_id: str) -> None:
    row = connection.execute(
        "SELECT status FROM platform_operators WHERE operator_id=?", (operator_id,)
    ).fetchone()
    if row is None or row["status"] != "active":
        raise ApiError("platform.workspace_not_found")


def _validate_task_links(connection, account, command) -> None:
    if command.customer_success_plan_id is not None:
        _plan_row(connection, command.customer_success_plan_id, account["customer_account_id"])
    if command.renewal_opportunity_id is not None:
        row = connection.execute(
            """SELECT 1 FROM platform_renewal_opportunities
                WHERE renewal_opportunity_id=? AND customer_account_id=?""",
            (command.renewal_opportunity_id, account["customer_account_id"]),
        ).fetchone()
        if row is None:
            raise ApiError("platform.workspace_not_found")


def _require_batch_requester(connection, batch) -> None:
    requester = connection.execute(
        "SELECT status,revision FROM platform_operators WHERE operator_id=?",
        (batch["prepared_by_operator_id"],),
    ).fetchone()
    if requester is None or requester["status"] != "active":
        raise ApiError("platform.permission_denied")
    _roles, permissions, _fingerprint = platform_access_service._authority(
        connection, batch["prepared_by_operator_id"]
    )
    if TASK_MANAGE not in permissions:
        raise ApiError("platform.permission_denied")
    if int(requester["revision"]) != int(batch["requested_permission_revision"]):
        raise ApiError("platform.revision_conflict")


def _insert_task(connection, account, actor, command, *, now: datetime) -> str:
    task_id = _id("ctk_")
    stamp = _db_time(now)
    connection.execute(
        """INSERT INTO platform_customer_tasks(
          customer_task_id,customer_account_id,workspace_id,tenant_id,
          customer_success_plan_id,renewal_opportunity_id,task_type,priority,reason_code,
          assignee_operator_id,due_at,required,status,due_generation,revision,
          created_by_operator_id,updated_by_operator_id,created_at,updated_at,completed_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'open',1,1,?,?,?,?,NULL)""",
        (
            task_id,
            account["customer_account_id"],
            account["workspace_id"],
            account["tenant_id"],
            command.customer_success_plan_id,
            command.renewal_opportunity_id,
            command.task_type,
            command.priority,
            command.reason_code,
            command.assignee_operator_id,
            _db_time(command.due_at),
            int(bool(command.required)),
            actor.operator_id,
            actor.operator_id,
            stamp,
            stamp,
        ),
    )
    return task_id


def _insert_batch_task(connection, account, actor, batch, now: datetime) -> str:
    task_id = _id("ctk_")
    stamp = _db_time(now)
    connection.execute(
        """INSERT INTO platform_customer_tasks(
          customer_task_id,customer_account_id,workspace_id,tenant_id,
          customer_success_plan_id,renewal_opportunity_id,task_type,priority,reason_code,
          assignee_operator_id,due_at,required,status,due_generation,revision,
          created_by_operator_id,updated_by_operator_id,created_at,updated_at,completed_at)
        VALUES(?,?,?,?,NULL,NULL,?,?,?,?,?,0,'open',1,1,?,?,?,?,NULL)""",
        (
            task_id,
            account["customer_account_id"],
            account["workspace_id"],
            account["tenant_id"],
            batch["task_type"],
            batch["priority"],
            batch["reason_code"],
            batch["assignee_operator_id"],
            batch["due_at"],
            actor.operator_id,
            actor.operator_id,
            stamp,
            stamp,
        ),
    )
    return task_id


def _event(
    connection,
    account,
    *,
    event_type: str,
    object_kind: str,
    object_id: str,
    revision: int,
    reason: str,
    safe_count: int,
    occurred_at: str,
) -> str:
    event_id = _id("cse_")
    payload = {
        "customer_account_id": account["customer_account_id"],
        "event_id": event_id,
        "object_id": object_id,
        "occurred_at": occurred_at,
        "reason_code": reason,
        "revision": revision,
        "safe_count": safe_count,
        "schema_version": "customer-success-event-v1",
        "source_digest": account["source_revision_sha256"],
        "tenant_id": account["tenant_id"],
        "workspace_id": account["workspace_id"],
    }
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    payload_hash = _sha(payload_json)
    connection.execute(
        """INSERT INTO platform_customer_success_events(
          customer_success_event_id,customer_account_id,workspace_id,tenant_id,event_type,
          object_kind,object_id,object_revision,reason_code,safe_count,payload_json,
          payload_sha256,schema_version,occurred_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event_id,
            account["customer_account_id"],
            account["workspace_id"],
            account["tenant_id"],
            event_type,
            object_kind,
            object_id,
            revision,
            reason,
            safe_count,
            payload_json,
            payload_hash,
            "customer-success-event-v1",
            occurred_at,
        ),
    )
    connection.execute(
        """INSERT INTO platform_customer_success_event_outbox(
          customer_success_event_outbox_id,customer_success_event_id,customer_account_id,
          workspace_id,tenant_id,source_outbox_id,status,attempt_count,fencing_generation,
          lease_owner_id,lease_token_sha256,lease_expires_at,next_attempt_at,
          subscription_snapshot_json,snapshot_frozen_at,last_error_code,delivered_at,
          recovery_watermark,created_at,updated_at)
        VALUES(?,?,?,?,?,NULL,'pending',0,0,NULL,NULL,NULL,?,NULL,NULL,NULL,NULL,0,?,?)""",
        (
            _id("cso_"),
            event_id,
            account["customer_account_id"],
            account["workspace_id"],
            account["tenant_id"],
            occurred_at,
            occurred_at,
            occurred_at,
        ),
    )
    return event_id


def _record_command(
    connection,
    config,
    actor,
    account,
    action,
    key,
    request_hash,
    expected,
    result_kind,
    result_id,
    result_revision,
    response,
    request_id,
    trace_id,
    now,
):
    connection.execute(
        """INSERT INTO platform_customer_success_commands(
          customer_success_command_id,operator_id,customer_account_id,workspace_id,tenant_id,
          action_code,idempotency_key_hmac,request_sha256,expected_revision,result_object_kind,
          result_object_id,result_revision,result_json,status,request_id,trace_id,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'completed',?,?,?)""",
        (
            _id("csc_"),
            actor.operator_id,
            account["customer_account_id"],
            account["workspace_id"],
            account["tenant_id"],
            action,
            _idempotency_hmac(config, key),
            request_hash,
            expected,
            result_kind,
            result_id,
            result_revision,
            response.model_dump_json(exclude_none=False),
            request_id,
            trace_id,
            _db_time(now),
        ),
    )


def _replay(connection, config, actor, action, key, request_hash, model):
    row = connection.execute(
        """SELECT request_sha256,result_json FROM platform_customer_success_commands
            WHERE operator_id=? AND action_code=? AND idempotency_key_hmac=?""",
        (actor.operator_id, action, _idempotency_hmac(config, key)),
    ).fetchone()
    if row is None:
        return None
    if not hmac.compare_digest(row["request_sha256"], request_hash):
        raise ApiError("platform.idempotency_conflict")
    return model.model_validate_json(row["result_json"])


def _audit(
    connection,
    *,
    actor,
    action,
    object_type,
    object_id,
    reason,
    request_id,
    trace_id,
    after_hash,
    now,
):
    connection.execute(
        """INSERT INTO platform_audit_logs(
          audit_id,actor_domain,actor_id,action_code,object_type,object_id,reason_code,
          request_id,trace_id,before_sha256,after_sha256,occurred_at)
        VALUES(?,'platform',?,?,?,?,?,?,?,NULL,?,?)""",
        (
            _id("pau_"),
            actor.operator_id,
            action,
            object_type,
            object_id,
            reason,
            request_id,
            trace_id,
            after_hash,
            _db_time(now),
        ),
    )


def _account_row(connection: sqlite3.Connection, account_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM platform_customer_account_projections WHERE customer_account_id=?",
        (account_id,),
    ).fetchone()
    if row is None:
        raise ApiError("platform.workspace_not_found")
    return row


def _plan_row(connection, plan_id, account_id):
    row = connection.execute(
        """SELECT * FROM platform_customer_success_plans
            WHERE customer_success_plan_id=? AND customer_account_id=?""",
        (plan_id, account_id),
    ).fetchone()
    if row is None:
        raise ApiError("platform.workspace_not_found")
    return row


def _task_row(connection, task_id):
    row = connection.execute(
        "SELECT * FROM platform_customer_tasks WHERE customer_task_id=?", (task_id,)
    ).fetchone()
    if row is None:
        raise ApiError("platform.workspace_not_found")
    return row


def _batch_row(connection, batch_id):
    row = connection.execute(
        "SELECT * FROM platform_customer_task_batches WHERE customer_task_batch_id=?", (batch_id,)
    ).fetchone()
    if row is None:
        raise ApiError("platform.workspace_not_found")
    return row


def _actor(connection, config, token, csrf):
    return authenticate_platform_session_in_transaction(
        connection, config, session_token=token, csrf_token=csrf, touch=True
    )


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("platform.unavailable") from None


def _require_schema(connection: sqlite3.Connection) -> None:
    if (
        connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version='0132' AND status='applied'"
        ).fetchone()
        is None
    ):
        raise ApiError("platform.unavailable")


def _encode_cursor(config, actor, filter_digest, kind, boundary_time, boundary_id):
    payload = json.dumps(
        {
            "boundary_id": boundary_id,
            "boundary_time": boundary_time,
            "filter_digest": filter_digest,
            "kind": kind,
            "operator_revision": actor.operator_revision,
            "permission_digest": _digest(actor.permissions),
            "platform_session_id": actor.platform_session_id,
            "roles": actor.roles,
            "v": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    signature = hmac.new(
        config.platform_session_secret.encode("utf-8"),
        _CURSOR_DOMAIN + b"\0" + payload,
        hashlib.sha256,
    ).digest()
    return _b64(payload) + "." + _b64(signature)


def _decode_cursor(config, actor, cursor, filter_digest, kind):
    try:
        if len(cursor) > 2048 or cursor.count(".") != 1:
            raise ValueError
        encoded, signed = cursor.split(".")
        payload = _unb64(encoded)
        signature = _unb64(signed)
        expected = hmac.new(
            config.platform_session_secret.encode("utf-8"),
            _CURSOR_DOMAIN + b"\0" + payload,
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        parsed = json.loads(payload.decode("ascii"))
        if (
            set(parsed)
            != {
                "boundary_id",
                "boundary_time",
                "filter_digest",
                "kind",
                "operator_revision",
                "permission_digest",
                "platform_session_id",
                "roles",
                "v",
            }
            or parsed["v"] != 1
            or parsed["filter_digest"] != filter_digest
            or parsed["kind"] != kind
            or parsed["operator_revision"] != actor.operator_revision
            or parsed["permission_digest"] != _digest(actor.permissions)
            or parsed["platform_session_id"] != actor.platform_session_id
            or tuple(parsed["roles"]) != actor.roles
        ):
            raise ValueError
        _parse_time(parsed["boundary_time"])
        return str(parsed["boundary_time"]), str(parsed["boundary_id"])
    except (ValueError, TypeError, UnicodeError, binascii.Error, json.JSONDecodeError):
        raise ApiError("platform.request_invalid") from None


def _idempotency_hmac(config: AppConfig, value: str) -> str:
    return hmac.new(
        config.platform_session_secret.encode("utf-8"),
        _IDEMPOTENCY_DOMAIN + b"\0" + value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _digest(value: object) -> str:
    return _sha(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True))


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _id(prefix: str) -> str:
    return prefix + uuid4().hex


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _optional_time(value):
    return None if value is None else _parse_time(value)


def _next_stamp(previous: str, now: datetime) -> str:
    return _db_time(max(now, _parse_time(previous) + timedelta(seconds=1)))


def _rollback(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        connection.rollback()


__all__ = [
    "apply_customer_task_command",
    "apply_success_plan_command",
    "get_customer_health",
    "list_customer_accounts",
    "list_renewal_pipeline",
]
