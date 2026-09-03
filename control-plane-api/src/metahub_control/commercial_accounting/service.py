"""Database-authoritative commercial accounting workflows for PRD-144."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.db.errors import DbError
from metahub_shared.runtime.config import AppConfig
from metahub_shared.runtime.errors import ApiError

from metahub_control.identity.security_service import (
    consume_global_step_up_grant_in_transaction,
    consume_step_up_grant_in_transaction,
)
from metahub_control.platform_access.service import (
    PlatformContext,
    authenticate_platform_session_in_transaction,
)
from metahub_control.workspace_access.service import (
    WorkspaceSessionContext,
    authenticate_workspace_session_in_transaction,
)

from .models import (
    BillingDisputeCommandResponse,
    BillingDisputeCreateRequest,
    BillingDisputeDecisionRequest,
    BillingDisputeInfo,
    PlatformInvoiceInfo,
    PlatformPlanCommandRequest,
    PlatformPlanCommandResponse,
    PlatformPlanListResponse,
    PlatformPlanReleaseInfo,
    PlatformSubscriptionListResponse,
    ProviderCostRateCommandRequest,
    ProviderCostRateCommandResponse,
    ProviderCostRateInfo,
    ProviderReferenceRateCommandRequest,
    ProviderReferenceRateCommandResponse,
    ProviderReferenceRateInfo,
    SubscriptionProjectionInfo,
    WorkspaceProviderCostRateListResponse,
    WorkspaceSubscriptionResponse,
)

_PLAN_CODE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$", re.ASCII)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CURRENCIES = frozenset(("CNY", "USD"))
_PLAN_STATUSES = frozenset(
    ("legacy_reconciled", "candidate", "review_required", "released", "retired")
)
_BILLING_PERIODS = frozenset(("none", "monthly", "yearly"))
_PROJECTION_STATES = frozenset(
    ("current", "partial", "stale", "reconciliation_required", "unknown")
)
_DISPUTE_STATUSES = frozenset(
    ("submitted", "under_review", "approved", "rejected", "withdrawn")
)
_RATE_STATUSES = frozenset(("active", "disabled"))
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 100


def command_platform_plan(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    expected_revision: int,
    command: PlatformPlanCommandRequest,
    request_id: str,
    trace_id: str,
) -> PlatformPlanCommandResponse:
    intent = {
        "command": command.model_dump(mode="json", exclude_none=True),
        "expected_revision": expected_revision,
        "schema_version": 1,
    }
    request_sha = _sha(intent)
    action = f"plan.{command.action}"
    owner_sha = _sha({"domain": "platform", "object": "plan_catalog"})
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_platform_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=False,
        )
        _require_platform(actor, "platform.commercial.plan.manage")
        replay = _replay_command(
            connection,
            config,
            actor_domain="platform",
            actor_id=actor.operator_id,
            action=action,
            owner_sha=owner_sha,
            idempotency_key=idempotency_key,
            request_sha=request_sha,
            error="platform.idempotency_conflict",
        )
        if replay is not None:
            row = _plan_release_row(connection, str(replay["result_id"]))
            if row is None:
                raise ApiError("platform.unavailable")
            result = PlatformPlanCommandResponse(
                plan=_plan_info(row, actor),
                command_id=str(replay["commercial_accounting_command_id"]),
                idempotent_replay=True,
                permission_fingerprint=_platform_fingerprint(actor),
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return result

        now = _now()
        stamp = _db_time(now)
        before_sha: str | None = None
        released_event = False
        if command.action == "create_candidate":
            plan_code = str(command.plan_code)
            catalog = connection.execute(
                "SELECT * FROM saas_plan_catalogs WHERE plan_code=?", (plan_code,)
            ).fetchone()
            if catalog is None:
                if expected_revision != 0:
                    raise ApiError("platform.revision_conflict")
                plan_catalog_id = f"spc_{uuid4().hex}"
                connection.execute(
                    """
                    INSERT INTO saas_plan_catalogs(
                      plan_catalog_id,plan_code,display_name,status,display_order,revision,
                      created_by_operator_id,created_at,updated_at)
                    VALUES(?,?,?,'draft',?,1,?,?,?)
                    """,
                    (
                        plan_catalog_id,
                        plan_code,
                        command.display_name,
                        command.display_order,
                        actor.operator_id,
                        stamp,
                        stamp,
                    ),
                )
                version_number = 1
            else:
                if catalog["status"] == "retired" or (
                    catalog["display_name"] != command.display_name
                    or int(catalog["display_order"]) != command.display_order
                ):
                    raise ApiError("saas_catalog.conflict")
                plan_catalog_id = str(catalog["plan_catalog_id"])
                head = connection.execute(
                    "SELECT revision FROM platform_plan_catalog_release_heads "
                    "WHERE plan_catalog_id=?",
                    (plan_catalog_id,),
                ).fetchone()
                head_revision = int(head["revision"]) if head is not None else 0
                if head_revision != expected_revision:
                    raise ApiError("platform.revision_conflict")
                if connection.execute(
                    "SELECT 1 FROM platform_plan_catalog_releases "
                    "WHERE plan_catalog_id=? AND status IN('candidate','review_required')",
                    (plan_catalog_id,),
                ).fetchone() is not None:
                    raise ApiError("saas_catalog.conflict")
                version_number = int(
                    connection.execute(
                        "SELECT coalesce(max(version_number),0)+1 FROM saas_plan_versions "
                        "WHERE plan_catalog_id=?",
                        (plan_catalog_id,),
                    ).fetchone()[0]
                )
            consume_global_step_up_grant_in_transaction(
                connection,
                config,
                account_id=actor.account_id,
                global_session_id=actor.global_session_id,
                grant=step_up_grant,
                action_code="commercial_plan.create_candidate",
                resource_type="platform_plan_catalog",
                resource_id=plan_code,
                now=now,
            )
            entitlements = command.entitlements.model_dump(mode="json")
            entitlements_json = _json(entitlements)
            entitlements_sha = _digest(entitlements_json)
            contract_sha = _sha(
                {
                    "billing_period": command.billing_period,
                    "currency": command.currency,
                    "entitlements_sha256": entitlements_sha,
                    "plan_code": plan_code,
                    "trial_days": command.trial_days,
                    "unit_price_minor": command.unit_price_minor,
                    "version_number": version_number,
                }
            )
            plan_version_id = f"spv_{uuid4().hex}"
            release_id = f"pplr_{uuid4().hex}"
            connection.execute(
                """
                INSERT INTO saas_plan_versions(
                  plan_version_id,plan_catalog_id,version_number,status,billing_period,
                  currency,unit_price_minor,trial_days,entitlements_json,
                  entitlements_sha256,created_by_operator_id,published_at,created_at)
                VALUES(?,?,?,'draft',?,?,?,?,?,?,?,NULL,?)
                """,
                (
                    plan_version_id,
                    plan_catalog_id,
                    version_number,
                    command.billing_period,
                    command.currency,
                    command.unit_price_minor,
                    command.trial_days,
                    entitlements_json,
                    entitlements_sha,
                    actor.operator_id,
                    stamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO platform_plan_catalog_releases(
                  platform_plan_catalog_release_id,plan_catalog_id,plan_version_id,
                  plan_code,billing_period,currency,unit_price_minor,
                  entitlements_sha256,contract_sha256,status,revision,release_generation,
                  candidate_by_operator_id,governance_approval_id,released_by_operator_id,
                  created_at,updated_at,released_at)
                VALUES(?,?,?,?,?,?,?,?,?,'candidate',1,0,?,NULL,NULL,?,?,NULL)
                """,
                (
                    release_id,
                    plan_catalog_id,
                    plan_version_id,
                    plan_code,
                    command.billing_period,
                    command.currency,
                    command.unit_price_minor,
                    entitlements_sha,
                    contract_sha,
                    actor.operator_id,
                    stamp,
                    stamp,
                ),
            )
            new_revision = 1
        else:
            if _SAFE_ID.fullmatch(str(command.plan_version_id)) is None:
                raise ApiError("platform.request_invalid")
            row = connection.execute(
                """
                SELECT release.*,catalog.status catalog_status
                FROM platform_plan_catalog_releases release
                JOIN saas_plan_catalogs catalog USING(plan_catalog_id)
                WHERE release.plan_version_id=?
                """,
                (command.plan_version_id,),
            ).fetchone()
            if row is None:
                raise ApiError("saas_catalog.not_found")
            if int(row["revision"]) != expected_revision:
                raise ApiError("platform.revision_conflict")
            release_id = str(row["platform_plan_catalog_release_id"])
            plan_catalog_id = str(row["plan_catalog_id"])
            plan_version_id = str(row["plan_version_id"])
            before_sha = _sha(
                {
                    "contract_sha256": row["contract_sha256"],
                    "revision": row["revision"],
                    "status": row["status"],
                }
            )
            consume_global_step_up_grant_in_transaction(
                connection,
                config,
                account_id=actor.account_id,
                global_session_id=actor.global_session_id,
                grant=step_up_grant,
                action_code=f"commercial_plan.{command.action}",
                resource_type="platform_plan_catalog_release",
                resource_id=release_id,
                now=now,
            )
            new_revision = expected_revision + 1
            if command.action == "submit_review":
                if row["status"] != "candidate":
                    raise ApiError("saas_catalog.conflict")
                updated = connection.execute(
                    """
                    UPDATE platform_plan_catalog_releases
                    SET status='review_required',revision=?,updated_at=?
                    WHERE platform_plan_catalog_release_id=? AND status='candidate'
                      AND revision=?
                    """,
                    (new_revision, stamp, release_id, expected_revision),
                ).rowcount
            elif command.action == "release":
                if row["status"] != "review_required":
                    raise ApiError("saas_catalog.conflict")
                approval = connection.execute(
                    """
                    SELECT approval.*
                    FROM platform_commercial_governance_approvals approval
                    WHERE approval.commercial_governance_approval_id=?
                      AND approval.object_kind='plan_release' AND approval.object_id=?
                      AND approval.candidate_sha256=? AND approval.candidate_revision=?
                      AND approval.consumed_at IS NULL
                      AND unixepoch(approval.expires_at)>unixepoch(?)
                      AND EXISTS(
                        SELECT 1 FROM platform_operator_role_bindings binding
                        JOIN platform_commercial_role_permissions permission
                          ON permission.role_code=binding.role_code
                        WHERE binding.operator_id=approval.approved_by_operator_id
                          AND binding.status='active'
                          AND permission.permission_code=
                            'platform.commercial.governance.approve')
                    """,
                    (
                        command.governance_approval_id,
                        release_id,
                        row["contract_sha256"],
                        row["revision"],
                        stamp,
                    ),
                ).fetchone()
                if approval is None or approval["approved_by_operator_id"] in {
                    actor.operator_id,
                    row["candidate_by_operator_id"],
                } or actor.operator_id == row["candidate_by_operator_id"]:
                    raise ApiError("saas_catalog.conflict")
                head = connection.execute(
                    "SELECT * FROM platform_plan_catalog_release_heads "
                    "WHERE plan_catalog_id=?",
                    (plan_catalog_id,),
                ).fetchone()
                generation = int(head["release_generation"]) + 1 if head else 1
                updated = connection.execute(
                    """
                    UPDATE platform_plan_catalog_releases
                    SET status='released',revision=?,release_generation=?,
                      governance_approval_id=?,released_by_operator_id=?,updated_at=?,
                      released_at=?
                    WHERE platform_plan_catalog_release_id=? AND status='review_required'
                      AND revision=?
                    """,
                    (
                        new_revision,
                        generation,
                        command.governance_approval_id,
                        actor.operator_id,
                        stamp,
                        stamp,
                        release_id,
                        expected_revision,
                    ),
                ).rowcount
                if updated == 1:
                    version_updated = connection.execute(
                        """
                        UPDATE saas_plan_versions SET status='published',published_at=?
                        WHERE plan_version_id=? AND status='draft'
                        """,
                        (stamp, plan_version_id),
                    ).rowcount
                    if version_updated != 1:
                        raise ApiError("saas_catalog.conflict")
                    connection.execute(
                        """
                        UPDATE saas_plan_catalogs
                        SET status='active',revision=revision+1,updated_at=?
                        WHERE plan_catalog_id=? AND status IN('draft','active')
                        """,
                        (stamp, plan_catalog_id),
                    )
                    connection.execute(
                        """
                        INSERT INTO platform_plan_catalog_release_heads(
                          plan_catalog_id,platform_plan_catalog_release_id,
                          release_generation,revision,updated_at)
                        VALUES(?,?,?,?,?)
                        ON CONFLICT(plan_catalog_id) DO UPDATE SET
                          platform_plan_catalog_release_id=
                            excluded.platform_plan_catalog_release_id,
                          release_generation=excluded.release_generation,
                          revision=platform_plan_catalog_release_heads.revision+1,
                          updated_at=excluded.updated_at
                        """,
                        (plan_catalog_id, release_id, generation, 1, stamp),
                    )
                    approval_updated = connection.execute(
                        """
                        UPDATE platform_commercial_governance_approvals
                        SET consumed_at=?
                        WHERE commercial_governance_approval_id=? AND consumed_at IS NULL
                        """,
                        (stamp, command.governance_approval_id),
                    ).rowcount
                    if approval_updated != 1:
                        raise ApiError("saas_catalog.conflict")
                    released_event = True
            else:
                head = connection.execute(
                    "SELECT platform_plan_catalog_release_id "
                    "FROM platform_plan_catalog_release_heads WHERE plan_catalog_id=?",
                    (plan_catalog_id,),
                ).fetchone()
                if row["status"] != "released" or head is None or not hmac.compare_digest(
                    str(head["platform_plan_catalog_release_id"]), release_id
                ):
                    raise ApiError("saas_catalog.conflict")
                updated = connection.execute(
                    """
                    UPDATE platform_plan_catalog_releases
                    SET status='retired',revision=?,updated_at=?
                    WHERE platform_plan_catalog_release_id=? AND status='released'
                      AND revision=?
                    """,
                    (new_revision, stamp, release_id, expected_revision),
                ).rowcount
                if updated == 1:
                    connection.execute(
                        """
                        UPDATE saas_plan_catalogs
                        SET status='retired',revision=revision+1,updated_at=?
                        WHERE plan_catalog_id=?
                        """,
                        (stamp, plan_catalog_id),
                    )
            if updated != 1:
                raise ApiError("platform.revision_conflict")

        result_sha = _sha(
            {
                "platform_plan_catalog_release_id": release_id,
                "revision": new_revision,
                "status": (
                    "candidate"
                    if command.action == "create_candidate"
                    else {
                        "submit_review": "review_required",
                        "release": "released",
                        "retire": "retired",
                    }[command.action]
                ),
            }
        )
        command_id = _insert_command(
            connection,
            config,
            actor_domain="platform",
            actor_id=actor.operator_id,
            action=action,
            object_kind="platform_plan_catalog_release",
            object_id=release_id,
            owner_sha=owner_sha,
            idempotency_key=idempotency_key,
            request_sha=request_sha,
            expected_revision=expected_revision,
            permission_revision=actor.operator_revision,
            result_id=release_id,
            result_sha=result_sha,
            now=now,
        )
        if released_event:
            _write_event(
                connection,
                event_type="plan.released",
                object_kind="platform_plan_catalog_release",
                object_id=release_id,
                owner_sha=owner_sha,
                revision=new_revision,
                payload={
                    "contract_sha256": row["contract_sha256"],
                    "plan_code": row["plan_code"],
                    "plan_version_id": plan_version_id,
                    "release_generation": generation,
                    "status": "released",
                },
                now=now,
            )
        _audit(
            connection,
            actor_domain="platform",
            actor_id=actor.operator_id,
            action=f"commercial.plan.{command.action}",
            object_type="platform_plan_release",
            object_id=release_id,
            before_sha=before_sha,
            after_sha=result_sha,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        result_row = _plan_release_row(connection, release_id)
        if result_row is None:
            raise ApiError("platform.unavailable")
        result = PlatformPlanCommandResponse(
            plan=_plan_info(result_row, actor),
            command_id=command_id,
            permission_fingerprint=_platform_fingerprint(actor),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("saas_catalog.conflict") from None
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def list_platform_plans(
    config: AppConfig,
    *,
    session_token: str,
    query: Mapping[str, str],
    request_id: str,
    trace_id: str,
) -> PlatformPlanListResponse:
    parsed = _plan_query(config, query)
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = authenticate_platform_session_in_transaction(
            connection, config, session_token=session_token, touch=True
        )
        _require_platform(actor, "platform.commercial.plan.read")
        clauses: list[str] = []
        parameters: list[object] = []
        for key, column in (
            ("plan_code", "release.plan_code"),
            ("status", "release.status"),
            ("currency", "release.currency"),
            ("billing_period", "release.billing_period"),
            ("release_generation", "release.release_generation"),
        ):
            value = parsed.get(key)
            if value is not None:
                clauses.append(f"{column}=?")
                parameters.append(value)
        cursor = parsed.get("cursor_values")
        if cursor is not None:
            clauses.append(
                "(release.plan_code,release.release_generation,"
                "release.platform_plan_catalog_release_id)>(?,?,?)"
            )
            parameters.extend(cursor)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit = int(parsed["limit"])
        rows = connection.execute(
            f"""
            SELECT release.*,catalog.display_name,
              head.platform_plan_catalog_release_id head_release_id,
              head.revision head_revision
            FROM platform_plan_catalog_releases release
            JOIN saas_plan_catalogs catalog USING(plan_catalog_id)
            LEFT JOIN platform_plan_catalog_release_heads head
              ON head.plan_catalog_id=release.plan_catalog_id
            {where}
            ORDER BY release.plan_code,release.release_generation,
              release.platform_plan_catalog_release_id
            LIMIT ?
            """,
            (*parameters, limit + 1),
        ).fetchall()
        page = rows[:limit]
        next_cursor = (
            _encode_cursor(
                config,
                "platform-plan",
                [
                    str(page[-1]["plan_code"]),
                    int(page[-1]["release_generation"]),
                    str(page[-1]["platform_plan_catalog_release_id"]),
                ],
            )
            if len(rows) > limit and page
            else None
        )
        reference_clauses = [
            "head.provider_reference_rate_release_id="
            "release.provider_reference_rate_release_id"
        ]
        reference_parameters: list[object] = []
        if parsed.get("currency") is not None:
            reference_clauses.append("release.currency=?")
            reference_parameters.append(parsed["currency"])
        reference_rows = connection.execute(
            f"""
            SELECT release.*,
              head.provider_reference_rate_release_id head_release_id,
              head.revision head_revision
            FROM platform_provider_reference_rate_heads head
            JOIN platform_provider_reference_rate_releases release
              ON {' AND '.join(reference_clauses)}
            ORDER BY release.provider_id,release.model_id,release.currency
            LIMIT 100
            """,
            tuple(reference_parameters),
        ).fetchall()
        result = PlatformPlanListResponse(
            plans=tuple(_plan_info(row, actor) for row in page),
            reference_rates=tuple(_reference_rate_info(row, actor) for row in reference_rows),
            next_cursor=next_cursor,
            limit=limit,
            permission_fingerprint=_platform_fingerprint(actor),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def list_platform_subscriptions(
    config: AppConfig,
    *,
    session_token: str,
    query: Mapping[str, str],
    request_id: str,
    trace_id: str,
) -> PlatformSubscriptionListResponse:
    parsed = _subscription_query(config, query)
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        actor = authenticate_platform_session_in_transaction(
            connection, config, session_token=session_token, touch=True
        )
        _require_platform(actor, "platform.commercial.subscription.read")
        clauses: list[str] = []
        parameters: list[object] = []
        if parsed.get("dispute_id") is not None:
            clauses.append(
                "EXISTS(SELECT 1 FROM billing_dispute_cases dispute "
                "WHERE dispute.workspace_id=projection.workspace_id "
                "AND dispute.tenant_id=projection.tenant_id "
                "AND dispute.billing_dispute_case_id=?)"
            )
            parameters.append(parsed["dispute_id"])
        else:
            for key, column in (
                ("status", "projection.subscription_status"),
                ("plan_catalog_id", "projection.plan_catalog_id"),
                ("projection_state", "projection.projection_state"),
            ):
                value = parsed.get(key)
                if value is not None:
                    clauses.append(f"{column}=?")
                    parameters.append(value)
            if parsed.get("from") is not None:
                clauses.append("projection.projected_at>=?")
                parameters.append(parsed["from"])
            if parsed.get("until") is not None:
                clauses.append("projection.projected_at<?")
                parameters.append(parsed["until"])
            if parsed.get("cursor_values") is not None:
                clauses.append("projection.workspace_subscription_projection_id>?")
                parameters.extend(parsed["cursor_values"])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit = int(parsed["limit"])
        rows = connection.execute(
            f"""
            SELECT projection.*,catalog.plan_code,catalog.display_name
            FROM workspace_subscription_projections projection
            JOIN saas_plan_catalogs catalog USING(plan_catalog_id)
            {where}
            ORDER BY projection.workspace_subscription_projection_id
            LIMIT ?
            """,
            (*parameters, limit + 1),
        ).fetchall()
        page = rows[:limit]
        owners = tuple((str(row["workspace_id"]), str(row["tenant_id"])) for row in page)
        invoices = _invoice_map(connection, owners)
        disputes = _dispute_map(connection, owners, actor=actor)
        next_cursor = (
            _encode_cursor(
                config,
                "platform-subscription",
                [str(page[-1]["workspace_subscription_projection_id"])],
            )
            if len(rows) > limit and page and parsed.get("dispute_id") is None
            else None
        )
        result = PlatformSubscriptionListResponse(
            subscriptions=tuple(
                _subscription_info(
                    row,
                    invoices=invoices.get((row["workspace_id"], row["tenant_id"]), ()),
                    disputes=disputes.get((row["workspace_id"], row["tenant_id"]), ()),
                    can_submit=False,
                )
                for row in page
            ),
            next_cursor=next_cursor,
            limit=limit,
            permission_fingerprint=_platform_fingerprint(actor),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def get_workspace_subscription(
    config: AppConfig,
    *,
    session_token: str,
    query: Mapping[str, str],
    request_id: str,
    trace_id: str,
) -> WorkspaceSubscriptionResponse:
    parsed = _workspace_subscription_query(config, query)
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        context = authenticate_workspace_session_in_transaction(
            connection, config, session_token=session_token, touch=True
        )
        _require_workspace(context, "workspace.commercial.subscription.read")
        row = connection.execute(
            """
            SELECT projection.*,catalog.plan_code,catalog.display_name
            FROM workspace_subscription_projections projection
            JOIN saas_plan_catalogs catalog USING(plan_catalog_id)
            WHERE projection.workspace_id=? AND projection.tenant_id=?
            """,
            (context.workspace_id, context.tenant_id),
        ).fetchone()
        if row is not None and parsed.get("cursor_values") is not None:
            if parsed["cursor_values"] != [row["workspace_subscription_projection_id"]]:
                row = None
        owners = ((context.workspace_id, context.tenant_id),) if row is not None else ()
        invoices = _invoice_map(connection, owners)
        disputes = _dispute_map(connection, owners, context=context)
        permission_fingerprint = context.authorization_fingerprint
        result = WorkspaceSubscriptionResponse(
            subscription=(
                _subscription_info(
                    row,
                    invoices=invoices.get((context.workspace_id, context.tenant_id), ()),
                    disputes=disputes.get((context.workspace_id, context.tenant_id), ()),
                    can_submit=(
                        "workspace.commercial.dispute.submit" in context.permission_codes
                    ),
                )
                if row is not None
                else None
            ),
            permission_revision=context.permission_revision,
            permission_fingerprint=permission_fingerprint,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("workspace_session.unavailable") from None
    finally:
        connection.close()


def list_workspace_provider_cost_rates(
    config: AppConfig,
    *,
    session_token: str,
    query: Mapping[str, str],
    request_id: str,
    trace_id: str,
) -> WorkspaceProviderCostRateListResponse:
    parsed = _rate_query(config, query)
    connection = _connect(config)
    try:
        connection.execute("BEGIN")
        context = authenticate_workspace_session_in_transaction(
            connection, config, session_token=session_token, touch=True
        )
        _require_workspace(context, "workspace.provider_cost.read")
        clauses = ["rate.workspace_id=?", "rate.tenant_id=?"]
        parameters: list[object] = [context.workspace_id, context.tenant_id]
        for key, column in (
            ("provider_id", "rate.provider_id"),
            ("model_id", "rate.model_id"),
            ("currency", "rate.currency"),
            ("status", "head.adoption_state"),
        ):
            value = parsed.get(key)
            if value is not None:
                clauses.append(f"{column}=?")
                parameters.append(value)
        effective_at = parsed.get("effective_at")
        if effective_at is not None:
            clauses.extend(
                (
                    "version.effective_from<=?",
                    "(version.effective_until IS NULL OR version.effective_until>?)",
                )
            )
            parameters.extend((effective_at, effective_at))
        if parsed.get("cursor_values") is not None:
            clauses.append("rate.workspace_provider_cost_rate_id>?")
            parameters.extend(parsed["cursor_values"])
        limit = int(parsed["limit"])
        rows = connection.execute(
            f"""
            SELECT rate.*,version.*,head.head_revision,head.adoption_state,
              provider.provider_code,provider.name provider_name,model.model_alias
            FROM workspace_provider_cost_rates rate
            JOIN workspace_provider_cost_rate_heads head
              USING(workspace_provider_cost_rate_id)
            JOIN workspace_provider_cost_rate_versions version
              ON version.workspace_provider_cost_rate_version_id=
                 head.workspace_provider_cost_rate_version_id
            JOIN providers provider
              ON provider.tenant_id=rate.tenant_id AND provider.provider_id=rate.provider_id
            JOIN models model
              ON model.tenant_id=rate.tenant_id AND model.model_id=rate.model_id
            WHERE {' AND '.join(clauses)}
            ORDER BY rate.workspace_provider_cost_rate_id
            LIMIT ?
            """,
            (*parameters, limit + 1),
        ).fetchall()
        page = rows[:limit]
        next_cursor = (
            _encode_cursor(
                config,
                "workspace-provider-cost",
                [str(page[-1]["workspace_provider_cost_rate_id"])],
            )
            if len(rows) > limit and page
            else None
        )
        result = WorkspaceProviderCostRateListResponse(
            rates=tuple(_provider_cost_info(row, context) for row in page),
            next_cursor=next_cursor,
            limit=limit,
            permission_revision=context.permission_revision,
            permission_fingerprint=context.authorization_fingerprint,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("workspace_session.unavailable") from None
    finally:
        connection.close()


def refresh_subscription_projections(config: AppConfig, *, limit: int = 50) -> int:
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ApiError("platform.request_invalid")
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            """
            SELECT subscription.*,version.plan_catalog_id,version.entitlements_sha256,
              version.currency,release.platform_plan_catalog_release_id,
              coalesce((SELECT source.status FROM subscription_orders source
                WHERE source.subscription_id=subscription.subscription_id
                ORDER BY source.updated_at DESC,source.order_id DESC LIMIT 1),'unknown')
                order_status,
              coalesce((SELECT source.amount_minor FROM subscription_orders source
                WHERE source.subscription_id=subscription.subscription_id
                ORDER BY source.updated_at DESC,source.order_id DESC LIMIT 1),0)
                order_amount_minor,
              coalesce((SELECT source.captured_amount_minor FROM subscription_orders source
                WHERE source.subscription_id=subscription.subscription_id
                ORDER BY source.updated_at DESC,source.order_id DESC LIMIT 1),0)
                captured_amount_minor,
              coalesce((SELECT source.refunded_amount_minor FROM subscription_orders source
                WHERE source.subscription_id=subscription.subscription_id
                ORDER BY source.updated_at DESC,source.order_id DESC LIMIT 1),0)
                refunded_amount_minor,
              coalesce((SELECT source.state_revision FROM subscription_orders source
                WHERE source.subscription_id=subscription.subscription_id
                ORDER BY source.updated_at DESC,source.order_id DESC LIMIT 1),0)
                order_revision,
              coalesce((SELECT payment.status FROM payment_intents payment
                JOIN subscription_orders source USING(order_id)
                WHERE source.subscription_id=subscription.subscription_id
                ORDER BY payment.updated_at DESC,payment.payment_intent_id DESC LIMIT 1),
                'unknown') payment_status,
              coalesce((SELECT payment.state_revision FROM payment_intents payment
                JOIN subscription_orders source USING(order_id)
                WHERE source.subscription_id=subscription.subscription_id
                ORDER BY payment.updated_at DESC,payment.payment_intent_id DESC LIMIT 1),0)
                payment_revision,
              coalesce((SELECT request.status FROM invoice_requests request
                WHERE request.workspace_id=subscription.workspace_id
                  AND request.tenant_id=subscription.tenant_id
                ORDER BY request.updated_at DESC,request.invoice_request_id DESC LIMIT 1),
                'unknown') invoice_status,
              coalesce((SELECT request.state_revision FROM invoice_requests request
                WHERE request.workspace_id=subscription.workspace_id
                  AND request.tenant_id=subscription.tenant_id
                ORDER BY request.updated_at DESC,request.invoice_request_id DESC LIMIT 1),0)
                invoice_revision,
              coalesce((SELECT sum(invoice.total_minor)
                FROM platform_invoice_workspace_bindings binding
                JOIN platform_invoices invoice USING(platform_invoice_id)
                WHERE binding.workspace_id=subscription.workspace_id
                  AND binding.tenant_id=subscription.tenant_id),0) invoiced_minor,
              coalesce((SELECT dispute.status FROM billing_dispute_cases dispute
                WHERE dispute.workspace_id=subscription.workspace_id
                  AND dispute.tenant_id=subscription.tenant_id
                ORDER BY dispute.updated_at DESC,dispute.billing_dispute_case_id DESC LIMIT 1),
                'unknown') dispute_status,
              coalesce((SELECT dispute.state_revision FROM billing_dispute_cases dispute
                WHERE dispute.workspace_id=subscription.workspace_id
                  AND dispute.tenant_id=subscription.tenant_id
                ORDER BY dispute.updated_at DESC,dispute.billing_dispute_case_id DESC LIMIT 1),0)
                dispute_revision
            FROM workspace_subscriptions subscription
            JOIN saas_plan_versions version
              ON version.plan_version_id=subscription.plan_version_id
            LEFT JOIN platform_plan_catalog_releases release
              ON release.plan_version_id=subscription.plan_version_id
            ORDER BY subscription.updated_at,subscription.subscription_id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        changed = 0
        now = _now()
        stamp = _db_time(now)
        for row in rows:
            existing = connection.execute(
                """
                SELECT * FROM workspace_subscription_projections
                WHERE workspace_id=? AND tenant_id=?
                """,
                (row["workspace_id"], row["tenant_id"]),
            ).fetchone()
            current_due = max(
                0, int(row["order_amount_minor"]) - int(row["captured_amount_minor"])
            )
            source_watermark = max(
                int(row["state_revision"]),
                int(row["order_revision"]),
                int(row["payment_revision"]),
                int(row["invoice_revision"]),
                int(row["dispute_revision"]),
            )
            projection_state = (
                "partial"
                if "unknown"
                in {
                    row["order_status"],
                    row["payment_status"],
                    row["invoice_status"],
                }
                else "current"
            )
            source_sha = _sha(
                {
                    "captured_amount_minor": row["captured_amount_minor"],
                    "dispute_revision": row["dispute_revision"],
                    "invoice_revision": row["invoice_revision"],
                    "invoiced_minor": row["invoiced_minor"],
                    "order_revision": row["order_revision"],
                    "payment_revision": row["payment_revision"],
                    "plan_version_id": row["plan_version_id"],
                    "refunded_amount_minor": row["refunded_amount_minor"],
                    "state_revision": row["state_revision"],
                    "subscription_id": row["subscription_id"],
                }
            )
            values = {
                "subscription_id": row["subscription_id"],
                "plan_catalog_id": row["plan_catalog_id"],
                "plan_version_id": row["plan_version_id"],
                "platform_plan_catalog_release_id": row[
                    "platform_plan_catalog_release_id"
                ],
                "subscription_status": row["status"],
                "current_period_started_at": row["current_period_started_at"],
                "current_period_ends_at": row["current_period_ends_at"],
                "entitlement_sha256": row["entitlements_sha256"],
                "currency": row["currency"],
                "current_due_minor": current_due,
                "paid_minor": int(row["captured_amount_minor"]),
                "refunded_minor": int(row["refunded_amount_minor"]),
                "invoiced_minor": int(row["invoiced_minor"]),
                "order_status": row["order_status"],
                "payment_status": row["payment_status"],
                "invoice_status": row["invoice_status"],
                "dispute_status": row["dispute_status"],
                "projection_state": projection_state,
                "source_watermark": source_watermark,
                "source_sha256": source_sha,
            }
            comparable = tuple(values)
            if existing is not None and all(existing[key] == values[key] for key in comparable):
                continue
            revision = int(existing["revision"]) + 1 if existing is not None else 1
            generation = (
                int(existing["projection_generation"]) + 1 if existing is not None else 1
            )
            if existing is None:
                projection_id = f"wspj_{uuid4().hex}"
                connection.execute(
                    """
                    INSERT INTO workspace_subscription_projections(
                      workspace_subscription_projection_id,workspace_id,tenant_id,
                      subscription_id,plan_catalog_id,plan_version_id,
                      platform_plan_catalog_release_id,subscription_status,
                      current_period_started_at,current_period_ends_at,entitlement_sha256,
                      currency,current_due_minor,paid_minor,refunded_minor,invoiced_minor,
                      order_status,payment_status,invoice_status,dispute_status,
                      projection_state,source_watermark,source_sha256,revision,
                      projection_generation,projected_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        projection_id,
                        row["workspace_id"],
                        row["tenant_id"],
                        *values.values(),
                        revision,
                        generation,
                        stamp,
                    ),
                )
                subscription_changed = True
            else:
                subscription_changed = any(
                    existing[key] != values[key]
                    for key in (
                        "plan_version_id",
                        "subscription_status",
                        "current_period_started_at",
                        "current_period_ends_at",
                    )
                )
                connection.execute(
                    """
                    UPDATE workspace_subscription_projections SET
                      subscription_id=?,plan_catalog_id=?,plan_version_id=?,
                      platform_plan_catalog_release_id=?,subscription_status=?,
                      current_period_started_at=?,current_period_ends_at=?,
                      entitlement_sha256=?,currency=?,current_due_minor=?,paid_minor=?,
                      refunded_minor=?,invoiced_minor=?,order_status=?,payment_status=?,
                      invoice_status=?,dispute_status=?,projection_state=?,source_watermark=?,
                      source_sha256=?,revision=?,projection_generation=?,projected_at=?
                    WHERE workspace_subscription_projection_id=? AND revision=?
                    """,
                    (
                        *values.values(),
                        revision,
                        generation,
                        stamp,
                        existing["workspace_subscription_projection_id"],
                        existing["revision"],
                    ),
                )
            if subscription_changed:
                _write_event(
                    connection,
                    event_type="subscription.changed",
                    object_kind="workspace_subscription",
                    object_id=row["subscription_id"],
                    owner_sha=_owner_sha(row["workspace_id"], row["tenant_id"]),
                    revision=revision,
                    payload={
                        "plan_version_id": row["plan_version_id"],
                        "projection_revision": revision,
                        "status": row["status"],
                        "subscription_id": row["subscription_id"],
                        "workspace_ref": _workspace_ref(
                            row["workspace_id"], row["tenant_id"]
                        ),
                    },
                    now=now,
                )
            changed += 1
        connection.commit()
        return changed
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def create_workspace_billing_dispute(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    expected_source_revision: int,
    command: BillingDisputeCreateRequest,
    request_id: str,
    trace_id: str,
) -> BillingDisputeCommandResponse:
    intent = {
        "command": command.model_dump(mode="json", exclude_none=True),
        "expected_source_revision": expected_source_revision,
        "schema_version": 1,
    }
    request_sha = _sha(intent)
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = authenticate_workspace_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=False,
        )
        _require_workspace(context, "workspace.commercial.dispute.submit")
        owner_sha = _owner_sha(context.workspace_id, context.tenant_id)
        replay = _replay_command(
            connection,
            config,
            actor_domain="workspace",
            actor_id=context.membership_id,
            action="billing_dispute.submit",
            owner_sha=owner_sha,
            idempotency_key=idempotency_key,
            request_sha=request_sha,
            error="billing_dispute.idempotency_conflict",
        )
        if replay is not None:
            row = _dispute_row(connection, str(replay["result_id"]))
            if row is None:
                raise ApiError("billing_dispute.integrity_failed")
            result = BillingDisputeCommandResponse(
                dispute=_dispute_info(row, context=context),
                command_id=str(replay["commercial_accounting_command_id"]),
                idempotent_replay=True,
                permission_revision=context.permission_revision,
                permission_fingerprint=context.authorization_fingerprint,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return result
        source = _resolve_dispute_source(
            connection,
            workspace_id=context.workspace_id,
            tenant_id=context.tenant_id,
            source_kind=command.source_kind,
            source_id=command.source_id,
            source_line_id=command.source_line_id,
        )
        if int(source["revision"]) != expected_source_revision:
            raise ApiError("billing_dispute.revision_conflict")
        now = _now()
        consume_step_up_grant_in_transaction(
            connection,
            config,
            context=context,
            grant=step_up_grant,
            action_code="commercial_dispute.submit",
            resource_type="commercial_billing_source",
            resource_id=str(source["source_id"]),
            now=now,
        )
        case_id = f"bdc_{uuid4().hex}"
        item_id = f"bdci_{uuid4().hex}"
        transition_id = f"bdct_{uuid4().hex}"
        stamp = _db_time(now)
        connection.execute(
            """
            INSERT INTO billing_dispute_cases(
              billing_dispute_case_id,workspace_id,tenant_id,source_kind,source_id,
              currency,disputed_amount_minor,reason_code,status,source_revision,
              source_sha256,state_revision,submitted_by_membership_id,created_at,
              updated_at,resolved_at)
            VALUES(?,?,?,?,?,?,?,?,'submitted',?,?,1,?,?,?,NULL)
            """,
            (
                case_id,
                context.workspace_id,
                context.tenant_id,
                command.source_kind,
                source["source_id"],
                source["currency"],
                source["amount_minor"],
                command.reason_code,
                source["revision"],
                source["source_sha256"],
                context.membership_id,
                stamp,
                stamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO billing_dispute_case_items(
              billing_dispute_case_item_id,billing_dispute_case_id,source_line_id,currency,
              source_amount_minor,disputable_amount_minor,source_revision,source_sha256,
              created_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                item_id,
                case_id,
                source["source_line_id"],
                source["currency"],
                source["amount_minor"],
                source["amount_minor"],
                source["revision"],
                source["source_sha256"],
                stamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO billing_dispute_case_transitions(
              billing_dispute_case_transition_id,billing_dispute_case_id,action,
              from_status,to_status,expected_revision,new_revision,actor_domain,actor_id,
              decision_code,idempotency_hmac,request_sha256,created_at)
            VALUES(?,?,'submit',NULL,'submitted',0,1,'workspace',?,NULL,?,?,?)
            """,
            (
                transition_id,
                case_id,
                context.membership_id,
                _idempotency_hmac(config, idempotency_key),
                request_sha,
                stamp,
            ),
        )
        result_sha = _sha(
            {
                "billing_dispute_case_id": case_id,
                "source_sha256": source["source_sha256"],
                "state_revision": 1,
                "status": "submitted",
            }
        )
        command_id = _insert_command(
            connection,
            config,
            actor_domain="workspace",
            actor_id=context.membership_id,
            action="billing_dispute.submit",
            object_kind="billing_dispute_case",
            object_id=case_id,
            owner_sha=owner_sha,
            idempotency_key=idempotency_key,
            request_sha=request_sha,
            expected_revision=expected_source_revision,
            permission_revision=context.permission_revision,
            result_id=case_id,
            result_sha=result_sha,
            now=now,
        )
        _write_event(
            connection,
            event_type="billing_dispute.submitted",
            object_kind="billing_dispute_case",
            object_id=case_id,
            owner_sha=owner_sha,
            revision=1,
            payload={
                "billing_dispute_case_id": case_id,
                "currency": source["currency"],
                "source_sha256": source["source_sha256"],
                "status": "submitted",
                "workspace_ref": _workspace_ref(context.workspace_id, context.tenant_id),
            },
            now=now,
        )
        _audit(
            connection,
            actor_domain="workspace",
            actor_id=context.membership_id,
            action="commercial.billing_dispute.submitted",
            object_type="billing_dispute_case",
            object_id=case_id,
            before_sha=None,
            after_sha=result_sha,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        row = _dispute_row(connection, case_id)
        if row is None:
            raise ApiError("billing_dispute.integrity_failed")
        result = BillingDisputeCommandResponse(
            dispute=_dispute_info(row, context=context),
            command_id=command_id,
            permission_revision=context.permission_revision,
            permission_fingerprint=context.authorization_fingerprint,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("billing_dispute.transition_invalid") from None
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("billing_dispute.unavailable") from None
    finally:
        connection.close()


def decide_platform_billing_dispute(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    expected_case_revision: int,
    dispute_id: str,
    command: BillingDisputeDecisionRequest,
    request_id: str,
    trace_id: str,
) -> BillingDisputeCommandResponse:
    if re.fullmatch(r"bdc_[0-9a-f]{32}", dispute_id) is None:
        raise ApiError("platform.request_invalid")
    intent = {
        "command": command.model_dump(mode="json", exclude_none=True),
        "dispute_id": dispute_id,
        "expected_case_revision": expected_case_revision,
        "schema_version": 1,
    }
    request_sha = _sha(intent)
    action = f"billing_dispute.{command.action}"
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_platform_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=False,
        )
        _require_platform(actor, "platform.commercial.dispute.decide")
        row = _dispute_row(connection, dispute_id)
        if row is None:
            raise ApiError("billing_dispute.not_found")
        owner_sha = _owner_sha(row["workspace_id"], row["tenant_id"])
        replay = _replay_command(
            connection,
            config,
            actor_domain="platform",
            actor_id=actor.operator_id,
            action=action,
            owner_sha=owner_sha,
            idempotency_key=idempotency_key,
            request_sha=request_sha,
            error="platform.idempotency_conflict",
        )
        if replay is not None:
            replay_row = _dispute_row(connection, str(replay["result_id"]))
            if replay_row is None:
                raise ApiError("billing_dispute.integrity_failed")
            adjustment = connection.execute(
                "SELECT platform_commercial_adjustment_id FROM platform_commercial_adjustments "
                "WHERE billing_dispute_case_id=?",
                (dispute_id,),
            ).fetchone()
            result = BillingDisputeCommandResponse(
                dispute=_dispute_info(replay_row, actor=actor),
                command_id=str(replay["commercial_accounting_command_id"]),
                adjustment_id=(adjustment[0] if adjustment is not None else None),
                idempotent_replay=True,
                permission_revision=actor.operator_revision,
                permission_fingerprint=_platform_fingerprint(actor),
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return result
        if int(row["state_revision"]) != expected_case_revision:
            raise ApiError("billing_dispute.revision_conflict")
        if int(row["source_revision"]) != command.expected_source_revision:
            raise ApiError("billing_dispute.integrity_failed")
        submitter = connection.execute(
            "SELECT account_id FROM workspace_memberships WHERE workspace_membership_id=?",
            (row["submitted_by_membership_id"],),
        ).fetchone()
        if submitter is None:
            raise ApiError("billing_dispute.integrity_failed")
        if hmac.compare_digest(str(submitter["account_id"]), actor.account_id):
            raise ApiError("billing_dispute.self_review_forbidden")
        source = _resolve_stored_dispute_source(connection, row)
        if (
            source["currency"] != row["currency"]
            or int(source["amount_minor"]) != int(row["disputed_amount_minor"])
            or int(source["revision"]) != int(row["source_revision"])
            or not hmac.compare_digest(str(source["source_sha256"]), str(row["source_sha256"]))
        ):
            raise ApiError("billing_dispute.integrity_failed")
        current_status = str(row["status"])
        if command.action == "start_review":
            if current_status != "submitted":
                raise ApiError("billing_dispute.transition_invalid")
            next_status = "under_review"
        else:
            if current_status != "under_review":
                raise ApiError("billing_dispute.transition_invalid")
            next_status = "approved" if command.action == "approve_credit" else "rejected"
        now = _now()
        consume_global_step_up_grant_in_transaction(
            connection,
            config,
            account_id=actor.account_id,
            global_session_id=actor.global_session_id,
            grant=step_up_grant,
            action_code=f"commercial_dispute.{command.action}",
            resource_type="billing_dispute_case",
            resource_id=dispute_id,
            now=now,
        )
        new_revision = expected_case_revision + 1
        stamp = _db_time(now)
        updated = connection.execute(
            """
            UPDATE billing_dispute_cases
            SET status=?,state_revision=?,updated_at=?,resolved_at=?
            WHERE billing_dispute_case_id=? AND state_revision=? AND status=?
            """,
            (
                next_status,
                new_revision,
                stamp,
                stamp if next_status in {"approved", "rejected"} else None,
                dispute_id,
                expected_case_revision,
                current_status,
            ),
        ).rowcount
        if updated != 1:
            raise ApiError("billing_dispute.revision_conflict")
        connection.execute(
            """
            INSERT INTO billing_dispute_case_transitions(
              billing_dispute_case_transition_id,billing_dispute_case_id,action,
              from_status,to_status,expected_revision,new_revision,actor_domain,actor_id,
              decision_code,idempotency_hmac,request_sha256,created_at)
            VALUES(?,?,?,?,?,?,?,'platform',?,?,?,?,?)
            """,
            (
                f"bdct_{uuid4().hex}",
                dispute_id,
                command.action,
                current_status,
                next_status,
                expected_case_revision,
                new_revision,
                actor.operator_id,
                command.decision_code,
                _idempotency_hmac(config, idempotency_key),
                request_sha,
                stamp,
            ),
        )
        adjustment_id: str | None = None
        if command.action in {"approve_credit", "reject"}:
            credited = 0
            if command.action == "approve_credit":
                credited = int(
                    connection.execute(
                        "SELECT sum(disputable_amount_minor) FROM billing_dispute_case_items "
                        "WHERE billing_dispute_case_id=?",
                        (dispute_id,),
                    ).fetchone()[0]
                    or 0
                )
                if not 0 < credited <= int(row["disputed_amount_minor"]):
                    raise ApiError("billing_dispute.adjustment_invalid")
                adjustment_id = f"bcad_{uuid4().hex}"
                connection.execute(
                    """
                    INSERT INTO platform_commercial_adjustments(
                      platform_commercial_adjustment_id,billing_dispute_case_id,
                      workspace_id,tenant_id,currency,amount_minor,adjustment_kind,
                      source_sha256,created_at)
                    VALUES(?,?,?,?,?,?,'dispute_credit',?,?)
                    """,
                    (
                        adjustment_id,
                        dispute_id,
                        row["workspace_id"],
                        row["tenant_id"],
                        row["currency"],
                        credited,
                        _sha(
                            {
                                "billing_dispute_case_id": dispute_id,
                                "credited_amount_minor": credited,
                                "currency": row["currency"],
                                "source_sha256": row["source_sha256"],
                            }
                        ),
                        stamp,
                    ),
                )
            connection.execute(
                """
                INSERT INTO billing_dispute_case_decisions(
                  billing_dispute_case_decision_id,billing_dispute_case_id,decision,
                  decision_code,credited_amount_minor,currency,decided_by_operator_id,
                  source_revision,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"bdcd_{uuid4().hex}",
                    dispute_id,
                    next_status,
                    command.decision_code,
                    credited,
                    row["currency"],
                    actor.operator_id,
                    row["source_revision"],
                    stamp,
                ),
            )
        result_sha = _sha(
            {
                "billing_dispute_case_id": dispute_id,
                "source_sha256": row["source_sha256"],
                "state_revision": new_revision,
                "status": next_status,
            }
        )
        command_id = _insert_command(
            connection,
            config,
            actor_domain="platform",
            actor_id=actor.operator_id,
            action=action,
            object_kind="billing_dispute_case",
            object_id=dispute_id,
            owner_sha=owner_sha,
            idempotency_key=idempotency_key,
            request_sha=request_sha,
            expected_revision=expected_case_revision,
            permission_revision=actor.operator_revision,
            result_id=dispute_id,
            result_sha=result_sha,
            now=now,
        )
        if next_status in {"approved", "rejected"}:
            _write_event(
                connection,
                event_type="billing_dispute.decided",
                object_kind="billing_dispute_case",
                object_id=dispute_id,
                owner_sha=owner_sha,
                revision=new_revision,
                payload={
                    "billing_dispute_case_id": dispute_id,
                    "decision_code": command.decision_code,
                    "source_sha256": row["source_sha256"],
                    "status": next_status,
                    "workspace_ref": _workspace_ref(row["workspace_id"], row["tenant_id"]),
                },
                now=now,
            )
        _audit(
            connection,
            actor_domain="platform",
            actor_id=actor.operator_id,
            action=f"commercial.billing_dispute.{command.action}",
            object_type="billing_dispute_case",
            object_id=dispute_id,
            before_sha=_dispute_state_sha(row),
            after_sha=result_sha,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        result_row = _dispute_row(connection, dispute_id)
        if result_row is None:
            raise ApiError("billing_dispute.integrity_failed")
        result = BillingDisputeCommandResponse(
            dispute=_dispute_info(result_row, actor=actor),
            command_id=command_id,
            adjustment_id=adjustment_id,
            permission_revision=actor.operator_revision,
            permission_fingerprint=_platform_fingerprint(actor),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("billing_dispute.adjustment_conflict") from None
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("billing_dispute.unavailable") from None
    finally:
        connection.close()


def command_workspace_provider_cost_rate(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    expected_head_revision: int,
    command: ProviderCostRateCommandRequest,
    request_id: str,
    trace_id: str,
) -> ProviderCostRateCommandResponse:
    intent = {
        "command": command.model_dump(mode="json", exclude_none=True),
        "expected_head_revision": expected_head_revision,
        "schema_version": 1,
    }
    request_sha = _sha(intent)
    action = f"provider_cost_rate.{command.action}"
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        context = authenticate_workspace_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=False,
        )
        _require_workspace(context, "workspace.provider_cost.manage")
        owner_sha = _owner_sha(context.workspace_id, context.tenant_id)
        replay = _replay_command(
            connection,
            config,
            actor_domain="workspace",
            actor_id=context.membership_id,
            action=action,
            owner_sha=owner_sha,
            idempotency_key=idempotency_key,
            request_sha=request_sha,
            error="billing_dispute.idempotency_conflict",
        )
        if replay is not None:
            row = _provider_cost_row_by_version(
                connection, context, str(replay["result_id"])
            )
            if row is None:
                raise ApiError("model_price.version_conflict")
            result = ProviderCostRateCommandResponse(
                rate=_provider_cost_info(row, context),
                command_id=str(replay["commercial_accounting_command_id"]),
                idempotent_replay=True,
                permission_revision=context.permission_revision,
                permission_fingerprint=context.authorization_fingerprint,
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return result
        current = None
        if command.action == "disable":
            current = _owned_provider_cost_row(
                connection, context, str(command.workspace_provider_cost_rate_id)
            )
            if current is None:
                raise ApiError("model_price.model_not_found")
            provider_id = str(current["provider_id"])
            model_id = str(current["model_id"])
            currency = str(current["currency"])
        else:
            provider_id = str(command.provider_id)
            model_id = str(command.model_id)
            currency = str(command.currency)
            _owned_provider_model(connection, context, provider_id, model_id)
            current = _provider_cost_row_for_key(
                connection, context, provider_id, model_id, currency
            )
        current_revision = int(current["head_revision"]) if current is not None else 0
        if current_revision != expected_head_revision:
            raise ApiError("model_price.version_conflict")
        now = _now()
        resource_id = (
            str(current["workspace_provider_cost_rate_id"])
            if current is not None
            else f"{provider_id}:{model_id}:{currency}"
        )
        consume_step_up_grant_in_transaction(
            connection,
            config,
            context=context,
            grant=step_up_grant,
            action_code=f"provider_cost_rate.{command.action}",
            resource_type="workspace_provider_cost_rate",
            resource_id=resource_id,
            now=now,
        )
        stamp = _db_time(now)
        if current is None:
            rate_id = f"pcr_{uuid4().hex}"
            connection.execute(
                """
                INSERT INTO workspace_provider_cost_rates(
                  workspace_provider_cost_rate_id,workspace_id,tenant_id,provider_id,
                  model_id,currency,created_at)
                VALUES(?,?,?,?,?,?,?)
                """,
                (
                    rate_id,
                    context.workspace_id,
                    context.tenant_id,
                    provider_id,
                    model_id,
                    currency,
                    stamp,
                ),
            )
            version_number = 1
        else:
            rate_id = str(current["workspace_provider_cost_rate_id"])
            version_number = int(
                connection.execute(
                    "SELECT coalesce(max(version_number),0)+1 "
                    "FROM workspace_provider_cost_rate_versions "
                    "WHERE workspace_provider_cost_rate_id=?",
                    (rate_id,),
                ).fetchone()[0]
            )
        if command.action == "create_version":
            input_rate = int(command.input_rate_micros_per_1k)
            output_rate = int(command.output_rate_micros_per_1k)
            effective_from = _db_time(command.effective_from)
            effective_until = (
                _db_time(command.effective_until)
                if command.effective_until is not None
                else None
            )
            source_type = "manual"
            source_id = f"manual_{uuid4().hex}"
            status = "active"
            adoption_state = "active"
        elif command.action == "adopt_reference":
            reference = connection.execute(
                """
                SELECT * FROM platform_provider_reference_rate_releases
                WHERE provider_reference_rate_release_id=? AND provider_id=?
                  AND model_id=? AND currency=? AND status='released'
                """,
                (
                    command.provider_reference_rate_release_id,
                    provider_id,
                    model_id,
                    currency,
                ),
            ).fetchone()
            if reference is None:
                raise ApiError("model_price.model_not_found")
            input_rate = int(reference["input_rate_micros_per_1k"])
            output_rate = int(reference["output_rate_micros_per_1k"])
            effective_from = str(reference["effective_from"])
            effective_until = reference["effective_until"]
            source_type = "platform_reference"
            source_id = str(reference["provider_reference_rate_release_id"])
            status = "active"
            adoption_state = "active"
        else:
            if current is None:
                raise ApiError("model_price.model_not_found")
            input_rate = int(current["input_rate_micros_per_1k"])
            output_rate = int(current["output_rate_micros_per_1k"])
            effective_from = stamp
            effective_until = None
            source_type = "manual"
            source_id = f"disable_{uuid4().hex}"
            status = "disabled"
            adoption_state = "disabled"
        if connection.execute(
            """
            SELECT 1 FROM workspace_provider_cost_rate_versions
            WHERE workspace_provider_cost_rate_id=? AND effective_from=?
            """,
            (rate_id, effective_from),
        ).fetchone() is not None:
            raise ApiError("model_price.effective_conflict")
        if status == "active" and connection.execute(
            """
            SELECT 1 FROM workspace_provider_cost_rate_versions
            WHERE workspace_provider_cost_rate_id=? AND status='active'
              AND effective_from<coalesce(?,'9999-12-31T23:59:59Z')
              AND(effective_until IS NULL OR effective_until>?)
            LIMIT 1
            """,
            (rate_id, effective_until, effective_from),
        ).fetchone() is not None:
            raise ApiError("model_price.effective_conflict")
        version_id = f"pcrv_{uuid4().hex}"
        source_sha = _sha(
            {
                "source_id": source_id,
                "source_type": source_type,
                "workspace_ref": _workspace_ref(context.workspace_id, context.tenant_id),
            }
        )
        content_sha = _sha(
            {
                "currency": currency,
                "effective_from": effective_from,
                "effective_until": effective_until,
                "input_rate_micros_per_1k": input_rate,
                "model_id": model_id,
                "output_rate_micros_per_1k": output_rate,
                "provider_id": provider_id,
                "status": status,
            }
        )
        connection.execute(
            """
            INSERT INTO workspace_provider_cost_rate_versions(
              workspace_provider_cost_rate_version_id,workspace_provider_cost_rate_id,
              version_number,input_rate_micros_per_1k,output_rate_micros_per_1k,
              effective_from,effective_until,source_type,source_id,source_sha256,status,
              revision,content_sha256,created_by_membership_id,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)
            """,
            (
                version_id,
                rate_id,
                version_number,
                input_rate,
                output_rate,
                effective_from,
                effective_until,
                source_type,
                source_id,
                source_sha,
                status,
                content_sha,
                context.membership_id,
                stamp,
            ),
        )
        next_revision = current_revision + 1
        if current is None:
            connection.execute(
                """
                INSERT INTO workspace_provider_cost_rate_heads(
                  workspace_provider_cost_rate_id,workspace_provider_cost_rate_version_id,
                  head_revision,adoption_state,updated_at)
                VALUES(?,?,?,?,?)
                """,
                (rate_id, version_id, next_revision, adoption_state, stamp),
            )
        else:
            updated = connection.execute(
                """
                UPDATE workspace_provider_cost_rate_heads
                SET workspace_provider_cost_rate_version_id=?,head_revision=?,
                  adoption_state=?,updated_at=?
                WHERE workspace_provider_cost_rate_id=? AND head_revision=?
                """,
                (
                    version_id,
                    next_revision,
                    adoption_state,
                    stamp,
                    rate_id,
                    current_revision,
                ),
            ).rowcount
            if updated != 1:
                raise ApiError("model_price.version_conflict")
        result_sha = _sha(
            {
                "content_sha256": content_sha,
                "head_revision": next_revision,
                "version_id": version_id,
            }
        )
        command_id = _insert_command(
            connection,
            config,
            actor_domain="workspace",
            actor_id=context.membership_id,
            action=action,
            object_kind="workspace_provider_cost_rate",
            object_id=rate_id,
            owner_sha=owner_sha,
            idempotency_key=idempotency_key,
            request_sha=request_sha,
            expected_revision=expected_head_revision,
            permission_revision=context.permission_revision,
            result_id=version_id,
            result_sha=result_sha,
            now=now,
        )
        _write_event(
            connection,
            event_type="provider_cost_rate.changed",
            object_kind="workspace_provider_cost_rate",
            object_id=rate_id,
            owner_sha=owner_sha,
            revision=next_revision,
            payload={
                "content_sha256": content_sha,
                "head_revision": next_revision,
                "status": adoption_state,
                "workspace_provider_cost_rate_id": rate_id,
                "workspace_ref": _workspace_ref(context.workspace_id, context.tenant_id),
            },
            now=now,
        )
        _audit(
            connection,
            actor_domain="workspace",
            actor_id=context.membership_id,
            action=f"commercial.provider_cost_rate.{command.action}",
            object_type="workspace_provider_cost_rate",
            object_id=rate_id,
            before_sha=(str(current["content_sha256"]) if current is not None else None),
            after_sha=result_sha,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        result_row = _provider_cost_row_by_version(connection, context, version_id)
        if result_row is None:
            raise ApiError("model_price.version_conflict")
        result = ProviderCostRateCommandResponse(
            rate=_provider_cost_info(result_row, context),
            command_id=command_id,
            permission_revision=context.permission_revision,
            permission_fingerprint=context.authorization_fingerprint,
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("model_price.effective_conflict") from None
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("workspace_session.unavailable") from None
    finally:
        connection.close()


def command_platform_provider_reference_rate(
    config: AppConfig,
    *,
    session_token: str,
    csrf_token: str,
    step_up_grant: str,
    idempotency_key: str,
    expected_revision: int,
    provider_id: str,
    command: ProviderReferenceRateCommandRequest,
    request_id: str,
    trace_id: str,
) -> ProviderReferenceRateCommandResponse:
    if _SAFE_ID.fullmatch(provider_id) is None:
        raise ApiError("platform.request_invalid")
    intent = {
        "command": command.model_dump(mode="json", exclude_none=True),
        "expected_revision": expected_revision,
        "provider_id": provider_id,
        "schema_version": 1,
    }
    request_sha = _sha(intent)
    action = f"provider_reference_rate.{command.action}"
    owner_sha = _sha({"domain": "platform", "object": "provider_reference_rate"})
    connection = _connect(config)
    try:
        connection.execute("BEGIN IMMEDIATE")
        actor = authenticate_platform_session_in_transaction(
            connection,
            config,
            session_token=session_token,
            csrf_token=csrf_token,
            touch=False,
        )
        _require_platform(actor, "platform.commercial.plan.manage")
        replay = _replay_command(
            connection,
            config,
            actor_domain="platform",
            actor_id=actor.operator_id,
            action=action,
            owner_sha=owner_sha,
            idempotency_key=idempotency_key,
            request_sha=request_sha,
            error="platform.idempotency_conflict",
        )
        if replay is not None:
            row = _reference_rate_row(connection, str(replay["result_id"]))
            if row is None:
                raise ApiError("platform.unavailable")
            result = ProviderReferenceRateCommandResponse(
                rate=_reference_rate_info(row, actor),
                command_id=str(replay["commercial_accounting_command_id"]),
                idempotent_replay=True,
                permission_fingerprint=_platform_fingerprint(actor),
                request_id=request_id,
                trace_id=trace_id,
            )
            connection.commit()
            return result
        now = _now()
        stamp = _db_time(now)
        if command.action == "create_candidate":
            model_id = str(command.model_id)
            currency = str(command.currency)
            known_model = connection.execute(
                """
                SELECT 1
                FROM providers provider
                JOIN models model
                  ON model.tenant_id=provider.tenant_id
                 AND model.provider_id=provider.provider_id
                WHERE provider.provider_id=? AND model.model_id=?
                  AND provider.status='active' AND model.status='active'
                LIMIT 1
                """,
                (provider_id, model_id),
            ).fetchone()
            if known_model is None:
                raise ApiError("saas_catalog.not_found")
            head = connection.execute(
                """
                SELECT revision FROM platform_provider_reference_rate_heads
                WHERE provider_id=? AND model_id=? AND currency=?
                """,
                (provider_id, model_id, currency),
            ).fetchone()
            head_revision = int(head["revision"]) if head is not None else 0
            if head_revision != expected_revision:
                raise ApiError("platform.revision_conflict")
            resource_id = f"{provider_id}:{model_id}:{currency}"
        else:
            release = connection.execute(
                """
                SELECT * FROM platform_provider_reference_rate_releases
                WHERE provider_reference_rate_release_id=? AND provider_id=?
                """,
                (command.provider_reference_rate_release_id, provider_id),
            ).fetchone()
            if release is None:
                raise ApiError("saas_catalog.not_found")
            if int(release["revision"]) != expected_revision:
                raise ApiError("platform.revision_conflict")
            model_id = str(release["model_id"])
            currency = str(release["currency"])
            resource_id = str(release["provider_reference_rate_release_id"])
        consume_global_step_up_grant_in_transaction(
            connection,
            config,
            account_id=actor.account_id,
            global_session_id=actor.global_session_id,
            grant=step_up_grant,
            action_code=f"provider_reference_rate.{command.action}",
            resource_type="platform_provider_reference_rate",
            resource_id=resource_id,
            now=now,
        )
        if command.action == "create_candidate":
            release_id = f"pprr_{uuid4().hex}"
            effective_from = _db_time(command.effective_from)
            effective_until = (
                _db_time(command.effective_until)
                if command.effective_until is not None
                else None
            )
            content_sha = _sha(
                {
                    "currency": currency,
                    "effective_from": effective_from,
                    "effective_until": effective_until,
                    "input_rate_micros_per_1k": command.input_rate_micros_per_1k,
                    "model_id": model_id,
                    "output_rate_micros_per_1k": command.output_rate_micros_per_1k,
                    "provider_id": provider_id,
                    "source_version": command.source_version,
                }
            )
            connection.execute(
                """
                INSERT INTO platform_provider_reference_rate_releases(
                  provider_reference_rate_release_id,provider_id,model_id,currency,
                  input_rate_micros_per_1k,output_rate_micros_per_1k,effective_from,
                  effective_until,source_version,content_sha256,status,revision,
                  release_generation,candidate_by_operator_id,governance_approval_id,
                  released_by_operator_id,created_at,updated_at,released_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,'candidate',1,0,?,NULL,NULL,?,?,NULL)
                """,
                (
                    release_id,
                    provider_id,
                    model_id,
                    currency,
                    command.input_rate_micros_per_1k,
                    command.output_rate_micros_per_1k,
                    effective_from,
                    effective_until,
                    command.source_version,
                    content_sha,
                    actor.operator_id,
                    stamp,
                    stamp,
                ),
            )
            new_revision = 1
        elif command.action == "release":
            if release["status"] != "candidate":
                raise ApiError("saas_catalog.conflict")
            approval = connection.execute(
                """
                SELECT * FROM platform_commercial_governance_approvals
                WHERE commercial_governance_approval_id=?
                  AND object_kind='provider_reference_rate' AND object_id=?
                  AND candidate_sha256=? AND candidate_revision=?
                  AND consumed_at IS NULL AND unixepoch(expires_at)>unixepoch(?)
                  AND EXISTS(
                    SELECT 1 FROM platform_operator_role_bindings binding
                    JOIN platform_commercial_role_permissions permission
                      ON permission.role_code=binding.role_code
                    WHERE binding.operator_id=approved_by_operator_id
                      AND binding.status='active'
                      AND permission.permission_code=
                        'platform.commercial.governance.approve')
                """,
                (
                    command.governance_approval_id,
                    release["provider_reference_rate_release_id"],
                    release["content_sha256"],
                    release["revision"],
                    stamp,
                ),
            ).fetchone()
            if approval is None or approval["approved_by_operator_id"] in {
                actor.operator_id,
                release["candidate_by_operator_id"],
            }:
                raise ApiError("saas_catalog.conflict")
            head = connection.execute(
                """
                SELECT * FROM platform_provider_reference_rate_heads
                WHERE provider_id=? AND model_id=? AND currency=?
                """,
                (provider_id, model_id, currency),
            ).fetchone()
            generation = int(head["release_generation"]) + 1 if head is not None else 1
            new_revision = int(release["revision"]) + 1
            connection.execute(
                """
                UPDATE platform_provider_reference_rate_releases
                SET status='released',revision=?,release_generation=?,
                  governance_approval_id=?,released_by_operator_id=?,updated_at=?,released_at=?
                WHERE provider_reference_rate_release_id=? AND status='candidate'
                  AND revision=?
                """,
                (
                    new_revision,
                    generation,
                    command.governance_approval_id,
                    actor.operator_id,
                    stamp,
                    stamp,
                    release["provider_reference_rate_release_id"],
                    expected_revision,
                ),
            )
            connection.execute(
                """
                INSERT INTO platform_provider_reference_rate_heads(
                  provider_id,model_id,currency,provider_reference_rate_release_id,
                  release_generation,revision,updated_at)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(provider_id,model_id,currency) DO UPDATE SET
                  provider_reference_rate_release_id=excluded.provider_reference_rate_release_id,
                  release_generation=excluded.release_generation,
                  revision=platform_provider_reference_rate_heads.revision+1,
                  updated_at=excluded.updated_at
                """,
                (provider_id, model_id, currency, resource_id, generation, 1, stamp),
            )
            connection.execute(
                "UPDATE platform_commercial_governance_approvals SET consumed_at=? "
                "WHERE commercial_governance_approval_id=? AND consumed_at IS NULL",
                (stamp, command.governance_approval_id),
            )
            release_id = resource_id
            content_sha = str(release["content_sha256"])
        else:
            if release["status"] != "released":
                raise ApiError("saas_catalog.conflict")
            new_revision = int(release["revision"]) + 1
            updated = connection.execute(
                """
                UPDATE platform_provider_reference_rate_releases
                SET status='retired',revision=?,updated_at=?
                WHERE provider_reference_rate_release_id=? AND status='released'
                  AND revision=?
                """,
                (new_revision, stamp, resource_id, expected_revision),
            ).rowcount
            if updated != 1:
                raise ApiError("platform.revision_conflict")
            connection.execute(
                """
                UPDATE platform_provider_reference_rate_heads
                SET revision=revision+1,updated_at=?
                WHERE provider_reference_rate_release_id=?
                """,
                (stamp, resource_id),
            )
            release_id = resource_id
            content_sha = str(release["content_sha256"])
        result_sha = _sha(
            {
                "content_sha256": content_sha,
                "provider_reference_rate_release_id": release_id,
                "revision": new_revision,
            }
        )
        command_id = _insert_command(
            connection,
            config,
            actor_domain="platform",
            actor_id=actor.operator_id,
            action=action,
            object_kind="provider_reference_rate",
            object_id=release_id,
            owner_sha=owner_sha,
            idempotency_key=idempotency_key,
            request_sha=request_sha,
            expected_revision=expected_revision,
            permission_revision=actor.operator_revision,
            result_id=release_id,
            result_sha=result_sha,
            now=now,
        )
        _audit(
            connection,
            actor_domain="platform",
            actor_id=actor.operator_id,
            action=f"commercial.provider_reference_rate.{command.action}",
            object_type="provider_reference_rate",
            object_id=release_id,
            before_sha=(
                str(release["content_sha256"])
                if command.action != "create_candidate"
                else None
            ),
            after_sha=result_sha,
            request_id=request_id,
            trace_id=trace_id,
            now=now,
        )
        row = _reference_rate_row(connection, release_id)
        if row is None:
            raise ApiError("platform.unavailable")
        result = ProviderReferenceRateCommandResponse(
            rate=_reference_rate_info(row, actor),
            command_id=command_id,
            permission_fingerprint=_platform_fingerprint(actor),
            request_id=request_id,
            trace_id=trace_id,
        )
        connection.commit()
        return result
    except ApiError:
        _rollback(connection)
        raise
    except sqlite3.IntegrityError:
        _rollback(connection)
        raise ApiError("saas_catalog.conflict") from None
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        _rollback(connection)
        raise ApiError("platform.unavailable") from None
    finally:
        connection.close()


def _plan_info(row: sqlite3.Row, actor: PlatformContext) -> PlatformPlanReleaseInfo:
    status = str(row["status"])
    actions: tuple[str, ...] = ()
    if "platform.commercial.plan.manage" in actor.permissions:
        actions = {
            "candidate": ("submit_review",),
            "review_required": ("release",),
            "released": ("retire",),
        }.get(status, ())
    return PlatformPlanReleaseInfo(
        platform_plan_catalog_release_id=row["platform_plan_catalog_release_id"],
        plan_catalog_id=row["plan_catalog_id"],
        plan_version_id=row["plan_version_id"],
        plan_code=row["plan_code"],
        display_name=row["display_name"],
        billing_period=row["billing_period"],
        currency=row["currency"],
        unit_price_minor=int(row["unit_price_minor"]),
        entitlements_sha256=row["entitlements_sha256"],
        contract_sha256=row["contract_sha256"],
        status=status,
        revision=int(row["revision"]),
        release_generation=int(row["release_generation"]),
        head_revision=(int(row["head_revision"]) if row["head_revision"] is not None else None),
        is_head=row["head_release_id"] == row["platform_plan_catalog_release_id"],
        readiness={
            "legacy_reconciled": "legacy",
            "candidate": "candidate",
            "review_required": "review",
            "released": "released",
            "retired": "retired",
        }[status],
        actions=actions,
        created_at=_parse_time(row["created_at"]),
        updated_at=_parse_time(row["updated_at"]),
        released_at=_optional_time(row["released_at"]),
    )


def _reference_rate_info(
    row: sqlite3.Row, actor: PlatformContext
) -> ProviderReferenceRateInfo:
    status = str(row["status"])
    actions: tuple[str, ...] = ()
    if "platform.commercial.plan.manage" in actor.permissions:
        actions = {"candidate": ("release",), "released": ("retire",)}.get(status, ())
    keys = set(row.keys())
    return ProviderReferenceRateInfo(
        provider_reference_rate_release_id=row["provider_reference_rate_release_id"],
        provider_id=row["provider_id"],
        model_id=row["model_id"],
        currency=row["currency"],
        input_rate_micros_per_1k=int(row["input_rate_micros_per_1k"]),
        output_rate_micros_per_1k=int(row["output_rate_micros_per_1k"]),
        effective_from=_parse_time(row["effective_from"]),
        effective_until=_optional_time(row["effective_until"]),
        source_version=row["source_version"],
        content_sha256=row["content_sha256"],
        status=status,
        revision=int(row["revision"]),
        release_generation=int(row["release_generation"]),
        head_revision=(
            int(row["head_revision"])
            if "head_revision" in keys and row["head_revision"] is not None
            else None
        ),
        is_head=(
            "head_release_id" in keys
            and row["head_release_id"] == row["provider_reference_rate_release_id"]
        ),
        actions=actions,
        created_at=_parse_time(row["created_at"]),
        updated_at=_parse_time(row["updated_at"]),
        released_at=_optional_time(row["released_at"]),
    )


def _subscription_info(
    row: sqlite3.Row,
    *,
    invoices: tuple[PlatformInvoiceInfo, ...],
    disputes: tuple[BillingDisputeInfo, ...],
    can_submit: bool,
) -> SubscriptionProjectionInfo:
    return SubscriptionProjectionInfo(
        workspace_customer_ref=_workspace_ref(row["workspace_id"], row["tenant_id"]),
        subscription_id=row["subscription_id"],
        plan_catalog_id=row["plan_catalog_id"],
        plan_version_id=row["plan_version_id"],
        platform_plan_catalog_release_id=row["platform_plan_catalog_release_id"],
        plan_code=row["plan_code"],
        display_name=row["display_name"],
        subscription_status=row["subscription_status"],
        current_period_started_at=_parse_time(row["current_period_started_at"]),
        current_period_ends_at=_optional_time(row["current_period_ends_at"]),
        entitlement_sha256=row["entitlement_sha256"],
        currency=row["currency"],
        current_due_minor=row["current_due_minor"],
        paid_minor=row["paid_minor"],
        refunded_minor=row["refunded_minor"],
        invoiced_minor=row["invoiced_minor"],
        order_status=row["order_status"],
        payment_status=row["payment_status"],
        invoice_status=row["invoice_status"],
        dispute_status=row["dispute_status"],
        projection_state=row["projection_state"],
        source_watermark=int(row["source_watermark"]),
        source_sha256=row["source_sha256"],
        revision=int(row["revision"]),
        projection_generation=int(row["projection_generation"]),
        invoices=invoices,
        disputes=disputes,
        actions=("submit_dispute",) if can_submit else (),
        projected_at=_parse_time(row["projected_at"]),
    )


def _invoice_map(
    connection: sqlite3.Connection,
    owners: tuple[tuple[str, str], ...],
) -> dict[tuple[str, str], tuple[PlatformInvoiceInfo, ...]]:
    if not owners:
        return {}
    placeholders = ",".join("(?,?)" for _ in owners)
    parameters = tuple(value for owner in owners for value in owner)
    rows = connection.execute(
        f"""
        SELECT binding.workspace_id,binding.tenant_id,invoice.*
        FROM platform_invoice_workspace_bindings binding
        JOIN platform_invoices invoice USING(platform_invoice_id)
        WHERE (binding.workspace_id,binding.tenant_id) IN ({placeholders})
        ORDER BY invoice.updated_at DESC,invoice.platform_invoice_id
        """,
        parameters,
    ).fetchall()
    result: dict[tuple[str, str], list[PlatformInvoiceInfo]] = {}
    for row in rows:
        result.setdefault((row["workspace_id"], row["tenant_id"]), []).append(
            PlatformInvoiceInfo(
                platform_invoice_id=row["platform_invoice_id"],
                currency=row["currency"],
                subtotal_minor=int(row["subtotal_minor"]),
                tax_rate_basis_points=int(row["tax_rate_basis_points"]),
                tax_minor=int(row["tax_minor"]),
                total_minor=int(row["total_minor"]),
                status=row["status"],
                invoice_number_masked=row["invoice_number_masked"],
                artifact_manifest_sha256=row["artifact_manifest_sha256"],
                source_revision=int(row["source_revision"]),
                generation=int(row["generation"]),
                updated_at=_parse_time(row["updated_at"]),
            )
        )
    return {key: tuple(value) for key, value in result.items()}


def _dispute_map(
    connection: sqlite3.Connection,
    owners: tuple[tuple[str, str], ...],
    *,
    actor: PlatformContext | None = None,
    context: WorkspaceSessionContext | None = None,
) -> dict[tuple[str, str], tuple[BillingDisputeInfo, ...]]:
    if not owners:
        return {}
    placeholders = ",".join("(?,?)" for _ in owners)
    parameters = tuple(value for owner in owners for value in owner)
    rows = connection.execute(
        f"""
        SELECT dispute.*,decision.decision_code,decision.credited_amount_minor
        FROM billing_dispute_cases dispute
        LEFT JOIN billing_dispute_case_decisions decision USING(billing_dispute_case_id)
        WHERE (dispute.workspace_id,dispute.tenant_id) IN ({placeholders})
        ORDER BY dispute.updated_at DESC,dispute.billing_dispute_case_id
        """,
        parameters,
    ).fetchall()
    result: dict[tuple[str, str], list[BillingDisputeInfo]] = {}
    for row in rows:
        result.setdefault((row["workspace_id"], row["tenant_id"]), []).append(
            _dispute_info(row, actor=actor, context=context)
        )
    return {key: tuple(value) for key, value in result.items()}


def _dispute_info(
    row: sqlite3.Row,
    *,
    actor: PlatformContext | None = None,
    context: WorkspaceSessionContext | None = None,
) -> BillingDisputeInfo:
    status = str(row["status"])
    actions: tuple[str, ...] = ()
    if actor is not None and "platform.commercial.dispute.decide" in actor.permissions:
        actions = {
            "submitted": ("start_review",),
            "under_review": ("approve_credit", "reject"),
        }.get(status, ())
    elif context is not None and status == "submitted":
        actions = ("withdraw",)
    keys = set(row.keys())
    return BillingDisputeInfo(
        billing_dispute_case_id=row["billing_dispute_case_id"],
        source_kind=row["source_kind"],
        source_ref=_safe_source_ref(row["source_kind"], row["source_id"]),
        currency=row["currency"],
        disputed_amount_minor=int(row["disputed_amount_minor"]),
        reason_code=row["reason_code"],
        status=status,
        source_revision=int(row["source_revision"]),
        state_revision=int(row["state_revision"]),
        decision_code=(row["decision_code"] if "decision_code" in keys else None),
        credited_amount_minor=(
            int(row["credited_amount_minor"] or 0)
            if "credited_amount_minor" in keys
            else 0
        ),
        actions=actions,
        created_at=_parse_time(row["created_at"]),
        updated_at=_parse_time(row["updated_at"]),
        resolved_at=_optional_time(row["resolved_at"]),
    )


def _provider_cost_info(
    row: sqlite3.Row, context: WorkspaceSessionContext
) -> ProviderCostRateInfo:
    status = "disabled" if row["adoption_state"] == "disabled" else "active"
    return ProviderCostRateInfo(
        workspace_provider_cost_rate_id=row["workspace_provider_cost_rate_id"],
        workspace_provider_cost_rate_version_id=row[
            "workspace_provider_cost_rate_version_id"
        ],
        provider_id=row["provider_id"],
        provider_code=row["provider_code"],
        provider_name=row["provider_name"],
        model_id=row["model_id"],
        model_alias=row["model_alias"],
        currency=row["currency"],
        version_number=int(row["version_number"]),
        input_rate_micros_per_1k=int(row["input_rate_micros_per_1k"]),
        output_rate_micros_per_1k=int(row["output_rate_micros_per_1k"]),
        effective_from=_parse_time(row["effective_from"]),
        effective_until=_optional_time(row["effective_until"]),
        source_type=row["source_type"],
        source_ref=_safe_source_ref(row["source_type"], row["source_id"]),
        status=status,
        revision=int(row["revision"]),
        head_revision=int(row["head_revision"]),
        adoption_state=row["adoption_state"],
        content_sha256=row["content_sha256"],
        actions=("create_version", "adopt_reference", "disable")
        if "workspace.provider_cost.manage" in context.permission_codes
        else (),
        created_at=_parse_time(row["created_at"]),
    )


def _resolve_dispute_source(
    connection: sqlite3.Connection,
    *,
    workspace_id: str,
    tenant_id: str,
    source_kind: str,
    source_id: str,
    source_line_id: str | None,
) -> dict[str, object]:
    if _SAFE_ID.fullmatch(source_id) is None or (
        source_line_id is not None and _SAFE_ID.fullmatch(source_line_id) is None
    ):
        raise ApiError("billing_dispute.request_invalid")
    if source_kind == "statement":
        row = connection.execute(
            """
            SELECT billing_statement_id,currency,total_minor,immutable_version,statement_sha256
            FROM billing_statements
            WHERE billing_statement_id=? AND workspace_id=? AND tenant_id=?
            """,
            (source_id, workspace_id, tenant_id),
        ).fetchone()
        if row is None:
            raise ApiError("billing_dispute.scope_not_found")
        result = {
            "source_id": row["billing_statement_id"],
            "source_line_id": row["billing_statement_id"],
            "currency": row["currency"],
            "amount_minor": int(row["total_minor"]),
            "revision": int(row["immutable_version"]),
            "source_sha256": row["statement_sha256"],
        }
    elif source_kind == "invoice":
        row = connection.execute(
            """
            SELECT invoice.*,binding.source_sha256 binding_sha256
            FROM platform_invoice_workspace_bindings binding
            JOIN platform_invoices invoice USING(platform_invoice_id)
            WHERE invoice.platform_invoice_id=? AND binding.workspace_id=?
              AND binding.tenant_id=?
            """,
            (source_id, workspace_id, tenant_id),
        ).fetchone()
        if row is None:
            raise ApiError("billing_dispute.scope_not_found")
        result = {
            "source_id": row["platform_invoice_id"],
            "source_line_id": row["platform_invoice_id"],
            "currency": row["currency"],
            "amount_minor": int(row["total_minor"]),
            "revision": int(row["source_revision"]),
            "source_sha256": _sha(
                {
                    "binding_sha256": row["binding_sha256"],
                    "currency": row["currency"],
                    "platform_invoice_id": row["platform_invoice_id"],
                    "source_revision": row["source_revision"],
                    "status": row["status"],
                    "total_minor": row["total_minor"],
                }
            ),
        }
    elif source_kind == "statement_line" and source_line_id is not None:
        row = connection.execute(
            """
            SELECT summary.billing_summary_id,summary.currency,summary.amount_minor,
              summary.summary_revision,summary.summary_sha256
            FROM billing_statements statement
            JOIN platform_billing_summaries summary
              ON summary.workspace_id=statement.workspace_id
             AND summary.tenant_id=statement.tenant_id
             AND summary.billing_cycle_id=statement.billing_cycle_id
            WHERE statement.billing_statement_id=? AND summary.billing_summary_id=?
              AND statement.workspace_id=? AND statement.tenant_id=?
            """,
            (source_id, source_line_id, workspace_id, tenant_id),
        ).fetchone()
        if row is None:
            raise ApiError("billing_dispute.scope_not_found")
        result = {
            "source_id": row["billing_summary_id"],
            "source_line_id": row["billing_summary_id"],
            "currency": row["currency"],
            "amount_minor": int(row["amount_minor"]),
            "revision": int(row["summary_revision"]),
            "source_sha256": row["summary_sha256"],
        }
    else:
        raise ApiError("billing_dispute.request_invalid")
    if int(result["amount_minor"]) <= 0 or result["currency"] not in _CURRENCIES:
        raise ApiError("billing_dispute.adjustment_invalid")
    if _SHA256.fullmatch(str(result["source_sha256"])) is None:
        raise ApiError("billing_dispute.integrity_failed")
    return result


def _resolve_stored_dispute_source(
    connection: sqlite3.Connection, row: sqlite3.Row
) -> dict[str, object]:
    if row["source_kind"] == "statement_line":
        summary = connection.execute(
            """
            SELECT billing_summary_id,currency,amount_minor,summary_revision,summary_sha256
            FROM platform_billing_summaries
            WHERE billing_summary_id=? AND workspace_id=? AND tenant_id=?
            """,
            (row["source_id"], row["workspace_id"], row["tenant_id"]),
        ).fetchone()
        if summary is None:
            raise ApiError("billing_dispute.integrity_failed")
        return {
            "source_id": summary["billing_summary_id"],
            "source_line_id": summary["billing_summary_id"],
            "currency": summary["currency"],
            "amount_minor": int(summary["amount_minor"]),
            "revision": int(summary["summary_revision"]),
            "source_sha256": summary["summary_sha256"],
        }
    return _resolve_dispute_source(
        connection,
        workspace_id=str(row["workspace_id"]),
        tenant_id=str(row["tenant_id"]),
        source_kind=str(row["source_kind"]),
        source_id=str(row["source_id"]),
        source_line_id=None,
    )


def _dispute_row(connection: sqlite3.Connection, dispute_id: str) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT dispute.*,decision.decision_code,decision.credited_amount_minor
        FROM billing_dispute_cases dispute
        LEFT JOIN billing_dispute_case_decisions decision USING(billing_dispute_case_id)
        WHERE dispute.billing_dispute_case_id=?
        """,
        (dispute_id,),
    ).fetchone()


def _dispute_state_sha(row: sqlite3.Row) -> str:
    return _sha(
        {
            "billing_dispute_case_id": row["billing_dispute_case_id"],
            "source_sha256": row["source_sha256"],
            "state_revision": row["state_revision"],
            "status": row["status"],
        }
    )


def _owned_provider_model(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    provider_id: str,
    model_id: str,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT provider.provider_id,model.model_id
        FROM providers provider
        JOIN models model
          ON model.tenant_id=provider.tenant_id AND model.provider_id=provider.provider_id
        WHERE provider.tenant_id=? AND provider.provider_id=? AND model.model_id=?
          AND provider.status='active' AND model.status='active'
        """,
        (context.tenant_id, provider_id, model_id),
    ).fetchone()
    if row is None:
        raise ApiError("model_price.model_not_found")
    return row


def _provider_cost_base_query() -> str:
    return """
        SELECT rate.*,version.*,head.head_revision,head.adoption_state,
          provider.provider_code,provider.name provider_name,model.model_alias
        FROM workspace_provider_cost_rates rate
        JOIN workspace_provider_cost_rate_heads head USING(workspace_provider_cost_rate_id)
        JOIN workspace_provider_cost_rate_versions version
          ON version.workspace_provider_cost_rate_id=
             rate.workspace_provider_cost_rate_id
         AND version.workspace_provider_cost_rate_version_id=selected_version_id
        JOIN providers provider
          ON provider.tenant_id=rate.tenant_id AND provider.provider_id=rate.provider_id
        JOIN models model
          ON model.tenant_id=rate.tenant_id AND model.model_id=rate.model_id
    """


def _provider_cost_row_for_key(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    provider_id: str,
    model_id: str,
    currency: str,
) -> sqlite3.Row | None:
    query = _provider_cost_base_query().replace(
        "selected_version_id", "head.workspace_provider_cost_rate_version_id"
    )
    return connection.execute(
        query
        + " WHERE rate.workspace_id=? AND rate.tenant_id=? AND rate.provider_id=? "
        "AND rate.model_id=? AND rate.currency=?",
        (context.workspace_id, context.tenant_id, provider_id, model_id, currency),
    ).fetchone()


def _owned_provider_cost_row(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    rate_id: str,
) -> sqlite3.Row | None:
    query = _provider_cost_base_query().replace(
        "selected_version_id", "head.workspace_provider_cost_rate_version_id"
    )
    return connection.execute(
        query
        + " WHERE rate.workspace_id=? AND rate.tenant_id=? "
        "AND rate.workspace_provider_cost_rate_id=?",
        (context.workspace_id, context.tenant_id, rate_id),
    ).fetchone()


def _provider_cost_row_by_version(
    connection: sqlite3.Connection,
    context: WorkspaceSessionContext,
    version_id: str,
) -> sqlite3.Row | None:
    query = _provider_cost_base_query().replace("selected_version_id", "?")
    return connection.execute(
        query
        + " WHERE rate.workspace_id=? AND rate.tenant_id=? "
        "AND version.workspace_provider_cost_rate_version_id=?",
        (version_id, context.workspace_id, context.tenant_id, version_id),
    ).fetchone()


def _plan_release_row(
    connection: sqlite3.Connection, release_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT release.*,catalog.display_name,
          head.platform_plan_catalog_release_id head_release_id,
          head.revision head_revision
        FROM platform_plan_catalog_releases release
        JOIN saas_plan_catalogs catalog USING(plan_catalog_id)
        LEFT JOIN platform_plan_catalog_release_heads head
          ON head.plan_catalog_id=release.plan_catalog_id
        WHERE release.platform_plan_catalog_release_id=?
        """,
        (release_id,),
    ).fetchone()


def _reference_rate_row(
    connection: sqlite3.Connection, release_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT release.*,
          head.provider_reference_rate_release_id head_release_id,
          head.revision head_revision
        FROM platform_provider_reference_rate_releases release
        LEFT JOIN platform_provider_reference_rate_heads head
          ON head.provider_id=release.provider_id AND head.model_id=release.model_id
         AND head.currency=release.currency
        WHERE release.provider_reference_rate_release_id=?
        """,
        (release_id,),
    ).fetchone()


def _owner_sha(workspace_id: object, tenant_id: object) -> str:
    return _sha({"tenant_id": tenant_id, "workspace_id": workspace_id})


def _insert_command(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    actor_domain: str,
    actor_id: str,
    action: str,
    object_kind: str,
    object_id: str,
    owner_sha: str,
    idempotency_key: str,
    request_sha: str,
    expected_revision: int,
    permission_revision: int,
    result_id: str,
    result_sha: str,
    now: datetime,
) -> str:
    command_id = f"cacm_{uuid4().hex}"
    connection.execute(
        """
        INSERT INTO commercial_accounting_commands(
          commercial_accounting_command_id,actor_domain,actor_id,action,object_kind,
          object_id,owner_sha256,idempotency_hmac,request_sha256,expected_revision,
          permission_revision,result_id,result_sha256,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            command_id,
            actor_domain,
            actor_id,
            action,
            object_kind,
            object_id,
            owner_sha,
            _idempotency_hmac(config, idempotency_key),
            request_sha,
            expected_revision,
            permission_revision,
            result_id,
            result_sha,
            _db_time(now),
        ),
    )
    return command_id


def _replay_command(
    connection: sqlite3.Connection,
    config: AppConfig,
    *,
    actor_domain: str,
    actor_id: str,
    action: str,
    owner_sha: str,
    idempotency_key: str,
    request_sha: str,
    error: str,
) -> sqlite3.Row | None:
    row = connection.execute(
        """
        SELECT * FROM commercial_accounting_commands
        WHERE actor_domain=? AND actor_id=? AND idempotency_hmac=?
        """,
        (actor_domain, actor_id, _idempotency_hmac(config, idempotency_key)),
    ).fetchone()
    if row is None:
        return None
    if (
        row["action"] != action
        or not hmac.compare_digest(str(row["owner_sha256"]), owner_sha)
        or not hmac.compare_digest(str(row["request_sha256"]), request_sha)
    ):
        raise ApiError(error)
    return row


def _write_event(
    connection: sqlite3.Connection,
    *,
    event_type: str,
    object_kind: str,
    object_id: str,
    owner_sha: str,
    revision: int,
    payload: Mapping[str, object],
    now: datetime,
) -> str:
    event_id = f"caev_{uuid4().hex}"
    outbox_id = f"caob_{uuid4().hex}"
    payload_json = _json(dict(payload))
    stamp = _db_time(now)
    connection.execute(
        """
        INSERT INTO commercial_accounting_events(
          commercial_accounting_event_id,event_type,object_kind,object_id,owner_sha256,
          revision,payload_sha256,payload_json,occurred_at)
        VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            event_id,
            event_type,
            object_kind,
            object_id,
            owner_sha,
            revision,
            _digest(payload_json),
            payload_json,
            stamp,
        ),
    )
    connection.execute(
        """
        INSERT INTO commercial_accounting_event_outbox(
          commercial_accounting_event_outbox_id,commercial_accounting_event_id,status,
          attempt_count,lease_generation,lease_owner_id,lease_token_hmac,
          lease_expires_at,next_attempt_at,dispatched_at,created_at,updated_at)
        VALUES(?,?,'pending',0,0,NULL,NULL,NULL,?,NULL,?,?)
        """,
        (outbox_id, event_id, stamp, stamp, stamp),
    )
    return event_id


def _audit(
    connection: sqlite3.Connection,
    *,
    actor_domain: str,
    actor_id: str,
    action: str,
    object_type: str,
    object_id: str,
    before_sha: str | None,
    after_sha: str,
    request_id: str,
    trace_id: str,
    now: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO platform_audit_logs(
          audit_id,actor_domain,actor_id,action_code,object_type,object_id,reason_code,
          request_id,trace_id,before_sha256,after_sha256,occurred_at)
        VALUES(?,?,?,?,?,?,'commercial_accounting.command',?,?,?,?,?)
        """,
        (
            f"pau_{uuid4().hex}",
            actor_domain,
            actor_id,
            action,
            object_type,
            object_id,
            request_id,
            trace_id,
            before_sha,
            after_sha,
            _db_time(now),
        ),
    )


def _idempotency_hmac(config: AppConfig, value: str) -> str:
    payload = f"metahub-commercial-accounting-v1\x00{value}".encode()
    return hmac.new(_cursor_secret(config), payload, hashlib.sha256).hexdigest()


def _plan_query(config: AppConfig, query: Mapping[str, str]) -> dict[str, object]:
    allowed = {
        "plan_code",
        "status",
        "currency",
        "billing_period",
        "release_generation",
        "cursor",
        "limit",
    }
    _exact_query(query, allowed, "platform.request_invalid")
    parsed: dict[str, object] = {"limit": _limit(query.get("limit"), "platform.request_invalid")}
    if "plan_code" in query:
        parsed["plan_code"] = _matched(query["plan_code"], _PLAN_CODE, "platform.request_invalid")
    if "status" in query:
        parsed["status"] = _enum(query["status"], _PLAN_STATUSES, "platform.request_invalid")
    if "currency" in query:
        parsed["currency"] = _enum(query["currency"], _CURRENCIES, "platform.request_invalid")
    if "billing_period" in query:
        parsed["billing_period"] = _enum(
            query["billing_period"], _BILLING_PERIODS, "platform.request_invalid"
        )
    if "release_generation" in query:
        parsed["release_generation"] = _integer(
            query["release_generation"], 0, 2_147_483_647, "platform.request_invalid"
        )
    if "cursor" in query:
        parsed["cursor_values"] = _decode_cursor(
            config, "platform-plan", query["cursor"], 3, "platform.request_invalid"
        )
    return parsed


def _subscription_query(config: AppConfig, query: Mapping[str, str]) -> dict[str, object]:
    allowed = {
        "status",
        "plan_catalog_id",
        "projection_state",
        "from",
        "until",
        "dispute_id",
        "cursor",
        "limit",
    }
    _exact_query(query, allowed, "platform.request_invalid")
    if "dispute_id" in query and set(query) != {"dispute_id"}:
        raise ApiError("platform.request_invalid")
    parsed: dict[str, object] = {"limit": _limit(query.get("limit"), "platform.request_invalid")}
    if "dispute_id" in query:
        parsed["dispute_id"] = _safe_id(query["dispute_id"], "platform.request_invalid")
        return parsed
    if "status" in query:
        parsed["status"] = _safe_text(query["status"], "platform.request_invalid")
    if "plan_catalog_id" in query:
        parsed["plan_catalog_id"] = _safe_id(query["plan_catalog_id"], "platform.request_invalid")
    if "projection_state" in query:
        parsed["projection_state"] = _enum(
            query["projection_state"], _PROJECTION_STATES, "platform.request_invalid"
        )
    for key in ("from", "until"):
        if key in query:
            parsed[key] = _timestamp(query[key], "platform.request_invalid")
    if parsed.get("from") is not None and parsed.get("until") is not None:
        if str(parsed["from"]) >= str(parsed["until"]):
            raise ApiError("platform.request_invalid")
    if "cursor" in query:
        parsed["cursor_values"] = _decode_cursor(
            config,
            "platform-subscription",
            query["cursor"],
            1,
            "platform.request_invalid",
        )
    return parsed


def _workspace_subscription_query(
    config: AppConfig, query: Mapping[str, str]
) -> dict[str, object]:
    _exact_query(query, {"period", "cursor"}, "workspace_session.unavailable")
    parsed: dict[str, object] = {}
    if "period" in query and query["period"] != "current":
        raise ApiError("workspace_session.unavailable")
    if "cursor" in query:
        parsed["cursor_values"] = _decode_cursor(
            config,
            "workspace-subscription",
            query["cursor"],
            1,
            "workspace_session.unavailable",
        )
    return parsed


def _rate_query(config: AppConfig, query: Mapping[str, str]) -> dict[str, object]:
    allowed = {
        "provider_id",
        "model_id",
        "currency",
        "status",
        "effective_at",
        "cursor",
        "limit",
    }
    _exact_query(query, allowed, "model_price.query_invalid")
    parsed: dict[str, object] = {"limit": _limit(query.get("limit"), "model_price.query_invalid")}
    for key in ("provider_id", "model_id"):
        if key in query:
            parsed[key] = _safe_id(query[key], "model_price.query_invalid")
    if "currency" in query:
        parsed["currency"] = _enum(query["currency"], _CURRENCIES, "model_price.query_invalid")
    if "status" in query:
        parsed["status"] = _enum(query["status"], _RATE_STATUSES, "model_price.query_invalid")
    if "effective_at" in query:
        parsed["effective_at"] = _timestamp(query["effective_at"], "model_price.query_invalid")
    if "cursor" in query:
        parsed["cursor_values"] = _decode_cursor(
            config,
            "workspace-provider-cost",
            query["cursor"],
            1,
            "model_price.query_invalid",
        )
    return parsed


def _exact_query(query: Mapping[str, str], allowed: set[str], error: str) -> None:
    if set(query) - allowed or any(
        _control(key) or _control(value) for key, value in query.items()
    ):
        raise ApiError(error)


def _limit(value: str | None, error: str) -> int:
    return _integer(value, 1, _MAX_LIMIT, error) if value is not None else _DEFAULT_LIMIT


def _integer(value: str, minimum: int, maximum: int, error: str) -> int:
    if not value.isdigit() or (len(value) > 1 and value.startswith("0")):
        raise ApiError(error)
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise ApiError(error)
    return parsed


def _enum(value: str, allowed: frozenset[str], error: str) -> str:
    if value not in allowed:
        raise ApiError(error)
    return value


def _matched(value: str, pattern: re.Pattern[str], error: str) -> str:
    if pattern.fullmatch(value) is None:
        raise ApiError(error)
    return value


def _safe_id(value: str, error: str) -> str:
    return _matched(value, _SAFE_ID, error)


def _safe_text(value: str, error: str) -> str:
    if not 1 <= len(value) <= 64 or _control(value):
        raise ApiError(error)
    return value


def _timestamp(value: str, error: str) -> str:
    if len(value) > 40 or _control(value):
        raise ApiError(error)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ApiError(error) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ApiError(error)
    return _db_time(parsed)


def _encode_cursor(config: AppConfig, purpose: str, values: list[object]) -> str:
    payload = _json({"purpose": purpose, "values": values, "version": 1}).encode("ascii")
    signature = hmac.new(_cursor_secret(config), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload + signature).rstrip(b"=").decode("ascii")


def _decode_cursor(
    config: AppConfig,
    purpose: str,
    value: str,
    count: int,
    error: str,
) -> list[object]:
    try:
        padding = "=" * (-len(value) % 4)
        raw = base64.urlsafe_b64decode((value + padding).encode("ascii"))
        if len(raw) <= 32:
            raise ValueError
        payload, signature = raw[:-32], raw[-32:]
        expected = hmac.new(_cursor_secret(config), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        parsed = json.loads(payload.decode("ascii"))
    except (ValueError, UnicodeError, json.JSONDecodeError):
        raise ApiError(error) from None
    if (
        type(parsed) is not dict
        or set(parsed) != {"purpose", "values", "version"}
        or parsed["purpose"] != purpose
        or parsed["version"] != 1
        or type(parsed["values"]) is not list
        or len(parsed["values"]) != count
        or any(type(item) not in {str, int} for item in parsed["values"])
    ):
        raise ApiError(error)
    return parsed["values"]


def _cursor_secret(config: AppConfig) -> bytes:
    return config.workspace_session_secret.encode("utf-8")


def _platform_fingerprint(actor: PlatformContext) -> str:
    return _sha(
        {
            "operator_id": actor.operator_id,
            "permissions": actor.permissions,
            "roles": actor.roles,
        }
    )


def _workspace_ref(workspace_id: object, tenant_id: object) -> str:
    return f"wref_{_digest(f'{workspace_id}\x1f{tenant_id}')[:32]}"


def _safe_source_ref(kind: object, source_id: object) -> str:
    return f"ref_{_digest(f'{kind}\x1f{source_id}')[:32]}"


def _require_platform(actor: PlatformContext, permission: str) -> None:
    if permission not in actor.permissions:
        raise ApiError("platform.permission_denied")


def _require_workspace(context: WorkspaceSessionContext, permission: str) -> None:
    if permission not in context.permission_codes:
        raise ApiError("workspace_session.permission_denied")


def _control(value: str) -> bool:
    return any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value)


def _sha(value: object) -> str:
    return _digest(_json(value))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _connect(config: AppConfig) -> sqlite3.Connection:
    try:
        return connect_database(config.database_url)
    except DbError:
        raise ApiError("platform.unavailable") from None


def _rollback(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        connection.rollback()


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _parse_time(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("invalid database timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # SQLite datetime('now') is UTC but has no offset; preserve those legacy rows.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    elif parsed.utcoffset() is None:
        raise ValueError("invalid database timestamp")
    return parsed.astimezone(UTC)


def _optional_time(value: object) -> datetime | None:
    return None if value is None else _parse_time(value)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [name for name in globals() if not name.startswith("_")]
